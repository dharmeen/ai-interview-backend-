import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.billing import services as billing
from src.interview import prompts, speech
from src.interview.models import (
    Answer,
    Interview,
    InterviewLevel,
    InterviewProfile,
    InterviewStatus,
    InterviewType,
    Question,
    QuestionDifficulty,
    QuestionSource,
)
from src.interview.schemas import AnswerCreate, InterviewCreate
from src.user.models import User
from src.utils.db import Local_Session
from src.utils.llm import LLMError, generate_json
from src.utils.settings import settings

logger = logging.getLogger(__name__)

INTERVIEW_NOT_FOUND = "Interview not found"
ALREADY_STARTED = "Interview has already been started"
NOT_IN_PROGRESS = "Interview is not in progress"
QUESTION_NOT_FOUND = "Question not found"
NOT_CURRENT_QUESTION = "You can only answer the current question"
AI_UNAVAILABLE = "Our AI interviewer is busy right now. Please try starting the interview again in a minute."
NOT_COMPLETED = "Interview is not completed yet"
PROFILE_NOT_FOUND = "Saved profile not found"
RESUME_REQUIRED = "Upload your resume, or pick a saved profile."
AI_UNAVAILABLE_RESUME = "Couldn't read the details from your resume right now. Please fill them in."
COACH_TOO_EARLY = "Answer a couple of questions first"
AI_UNAVAILABLE_COACH = "No coaching tip right now."
VOICE_UNAVAILABLE = "The interviewer's voice is not available right now. You can still read the question."
AI_UNAVAILABLE_FEEDBACK = "Our AI interviewer is busy right now. Your answers are saved, please try again in a minute."
AI_UNAVAILABLE_SUGGESTIONS ="Role suggestions are not available right now."
AI_UNAVAILABLE_ANSWER ="Our AI interviewer is busy right now. Your answer was not saved, please submit it again in a minute."

MAX_CONCEPT_LENGTH = 200  # matches Question.concept

# saved profiles and rounds
MAX_PROFILES_LISTED = 20
PROFILE_FIELDS = (
    "id", "role", "level", "experience_years", "interview_type", "num_questions",
    "job_description", "resume_filename", "updated_at",
)
# how many earlier questions the AI is told to avoid; enough for ~5 past rounds, short enough for a prompt
MAX_HISTORY_QUESTIONS = 40
# what each round on the same profile asks of the AI; the last one repeats for later rounds
DIFFICULTY_NOTES = (
    "This is their first round: use the normal difficulty for their level and experience.",
    "This is round 2: make it noticeably harder than a first round. Fewer basics, more 'how' and "
    "'why' questions, and ask for trade-offs.",
    "This is round 3: make it harder again. Go into trade-offs, edge cases, failure handling and "
    "what changes at larger scale.",
    "This is round 4 or later: make it the hardest yet for their level, like a demanding "
    "interviewer testing a strong candidate: design decisions, edge cases, debugging under "
    "pressure, and what would break first.",
)

# questions are written at the same time; more than this risks the provider's rate limit
MAX_PARALLEL_AI_CALLS = 5
# how long /start waits for the background preparation before writing the questions itself
PREPARE_WAIT_SECONDS = 60
FEEDBACK_ATTEMPTS = 2
# deciding on a follow-up usually takes ~2 s; past this the interview just moves on without one
FOLLOW_UP_TIMEOUT_SECONDS = 6.0
# shorter answers ("I don't know", "no idea") have nothing to follow up on, so skip the AI call
MIN_ANSWER_FOR_FOLLOW_UP = 25
# coaching pop-up: needs a couple of answers to see a pattern, and looks at the last few
COACH_MIN_ANSWERS = 2
RESUME_DETAILS_TIMEOUT_SECONDS = 15.0
# (minimum years, level), highest first; matches the rules in the resume_details prompt
LEVEL_BY_YEARS = (
    (6, InterviewLevel.SENIOR),
    (3, InterviewLevel.MID),
    (1, InterviewLevel.JUNIOR),
    (0, InterviewLevel.FRESHER),
)
COACH_RECENT_ANSWERS = 3
COACH_TIMEOUT_SECONDS = 15.0
COACH_CACHE_SIZE = 500

# background jobs running in this process, so a request can wait for one instead of repeating it
_jobs_lock = threading.Lock()
_preparing: dict[uuid.UUID, threading.Event] = {}
_scoring: set[uuid.UUID] = set()
# coach tips already made, keyed by (interview id, number of answers); a dict keeps insertion order
_coach_tips: dict[tuple[uuid.UUID, int], dict] = {}

# A real interview mixes question kinds instead of only walking through the CV.
# The order is fixed here (not left to the AI) so every interview gets a fair mix:
# it opens on the candidate's own work as a warm-up, and any first 3+ questions are balanced.
QUESTION_KIND_ORDER: dict[InterviewType, list[str]] = {
    InterviewType.TECHNICAL: ["resume", "concept", "scenario", "concept", "resume", "scenario", "concept", "scenario"],
    InterviewType.HR: ["resume", "behavioural", "scenario", "behavioural", "resume", "scenario", "behavioural"],
    InterviewType.MIXED: ["resume", "concept", "behavioural", "scenario", "concept", "resume", "behavioural", "scenario"],
}
QUESTION_KIND_GUIDES = {
    "resume": (
        "Ask about this item from the candidate's resume: refer to it by name, then dig into what they "
        "did themselves, why they chose that approach, problems they hit, or what they would do differently."
    ),
    "concept": (
        "Test their understanding of this concept directly, as an interviewer checking fundamentals for the role. "
        "Do not mention the resume. Ask how or why something works, or to compare options, not for a textbook definition."
    ),
    "scenario": (
        "Describe a short, realistic situation at work for this role (2 to 3 sentences) and ask what they would "
        "do, how they would approach or debug it, or which trade-offs they would weigh. Do not mention the resume."
    ),
    "behavioural": (
        "Ask a behavioural question about a real past experience, for example \"Tell me about a time when...\". "
        "If the resume shows a fitting job or project, you may mention it."
    ),
}


def _question_kinds(interview: Interview) -> list[str]:
    """Kind of each question in order; the pattern repeats for long interviews."""
    order = QUESTION_KIND_ORDER[interview.interview_type]
    return [order[i % len(order)] for i in range(interview.num_questions)]

# JSON shapes the model must return (llm.py checks every reply against them)
TOPIC_PLAN_SCHEMA = {
    "type": "object",
    "properties": {"concepts": {"type": "array", "items": {"type": "string"}}},
    "required": ["concepts"],
    "additionalProperties": False,
}
QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "difficulty": {"type": "string", "enum": [d.value for d in QuestionDifficulty]},
    },
    "required": ["question", "difficulty"],
    "additionalProperties": False,
}
FOLLOW_UP_SCHEMA = {
    "type": "object",
    "properties": {
        "follow_up": {"type": "boolean"},
        "question": {"type": "string"},
        "difficulty": {"type": "string", "enum": [d.value for d in QuestionDifficulty]},
    },
    "required": ["follow_up", "question", "difficulty"],
    "additionalProperties": False,
}
RESUME_DETAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "experience_years": {"type": "integer", "minimum": 0, "maximum": 40},  # matches InterviewCreate
        "level": {"type": "string", "enum": [lv.value for lv in InterviewLevel]},
    },
    "required": ["role", "experience_years", "level"],
    "additionalProperties": False,
}
COACH_TIP_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "tips": {"type": "array", "items": {"type": "string"}},
        "encouragement": {"type": "string"},
    },
    "required": ["headline", "tips", "encouragement"],
    "additionalProperties": False,
}
ROLE_SUGGESTIONS_SCHEMA = {
    "type": "object",
    "properties": {"roles": {"type": "array", "items": {"type": "string"}}},
    "required": ["roles"],
    "additionalProperties": False,
}
FINAL_FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 10}},
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "improvements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "summary", "strengths", "improvements"],
    "additionalProperties": False,
}


def _get_profile(db: Session, user: User, profile_id: uuid.UUID) -> InterviewProfile:
    # filtered by user too, so someone else's profile looks exactly like a missing one
    profile = db.scalar(
        select(InterviewProfile).where(InterviewProfile.id == profile_id, InterviewProfile.user_id == user.id)
    )
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROFILE_NOT_FOUND)
    return profile


def _started_count(db: Session, profile_id: uuid.UUID) -> int:
    """Interviews actually started from the profile: one that was only created isn't a round."""
    return db.scalar(
        select(func.count()).select_from(Interview).where(
            Interview.profile_id == profile_id, Interview.status != InterviewStatus.CREATED
        )
    )


def _question_history(db: Session, profile_id: uuid.UUID) -> list[dict]:
    """Main questions from earlier rounds on this profile, newest first, so they aren't asked again."""
    rows = db.execute(
        select(Question.concept, Question.text)
        .join(Interview, Interview.id == Question.interview_id)
        .where(Interview.profile_id == profile_id, Interview.status != InterviewStatus.CREATED, Question.sub_no == 0)
        .order_by(Interview.created_at.desc(), Question.order_no)
        .limit(MAX_HISTORY_QUESTIONS)
    ).all()
    return [{"concept": concept, "text": text} for concept, text in rows]


def create_interview(
    db: Session,
    user: User,
    data: InterviewCreate,
    resume: tuple[str, str] | None,
    profile_id: uuid.UUID | None = None,
) -> Interview:
    """Creates the interview and saves its details as a profile for next time.
    With a profile (picked, or found by the same role) this is its next round: harder, and
    without the questions asked before. `resume` is (text, file name); None = the profile's."""
    # 402 early, before the user fills in everything; the credit itself is spent on start
    billing.check_can_create(user, data.num_questions)

    if profile_id is not None:
        profile = _get_profile(db, user, profile_id)
    else:
        # one profile per role: typing the same role again continues it instead of starting over
        profile = db.scalar(
            select(InterviewProfile)
            .where(InterviewProfile.user_id == user.id, func.lower(InterviewProfile.role) == data.role.lower())
            .order_by(InterviewProfile.updated_at.desc())
            .limit(1)
        )
    if resume is None:
        if profile is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=RESUME_REQUIRED)
        resume = (profile.resume_text, profile.resume_filename)
    resume_text, resume_filename = resume

    # save what was filled in, so next time the form comes back filled
    fields = {**data.model_dump(), "resume_text": resume_text, "resume_filename": resume_filename}
    if profile is None:
        profile = InterviewProfile(user_id=user.id, **fields)
        db.add(profile)
        db.flush()  # gives it an id
        difficulty_round, history = 1, []
    else:
        for key, value in fields.items():
            setattr(profile, key, value)
        profile.updated_at = func.now()  # listed first next time, even if nothing changed
        difficulty_round = _started_count(db, profile.id) + 1
        history = _question_history(db, profile.id)

    interview = Interview(
        user_id=user.id,
        profile_id=profile.id,
        difficulty_round=difficulty_round,
        previous_questions=history,
        **fields,
    )
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return interview


def read_resume_details(resume_text: str, resume_filename: str) -> dict:
    """Reads the target role, years of experience and level from a resume, to fill in the form.
    Nothing is saved; the user can still change every field."""
    try:
        data = generate_json(
            system=prompts.load("system"),
            prompt=prompts.render(
                "resume_details",
                resume=prompts.escape_user_text(resume_text),
                today=date.today().strftime("%B %Y"),  # for jobs that run "to Present"
            ),
            schema=RESUME_DETAILS_SCHEMA,
            tier="fast",  # the user is waiting with the form open
            max_tokens=500,
            timeout=RESUME_DETAILS_TIMEOUT_SECONDS,
        )
    except LLMError as e:
        logger.info("Could not read resume details: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_RESUME)

    role = " ".join(data["role"].split())[:MAX_ROLE_LENGTH]
    years = data["experience_years"]
    # the level follows from the years, so it always matches them; the AI may only raise it
    # (e.g. a "Lead Engineer" title with 5 years)
    by_years = next(level for min_years, level in LEVEL_BY_YEARS if years >= min_years)
    order = list(InterviewLevel)
    level = max(by_years, InterviewLevel(data["level"]), key=order.index)
    return {
        "role": role if len(role) >= 2 else None,
        "experience_years": years,
        "level": level,
        "resume_filename": resume_filename,
    }


def list_profiles(db: Session, user: User) -> list[dict]:
    """The user's saved profiles, most recently used first, with how far they've got on each."""
    profiles = db.scalars(
        select(InterviewProfile)
        .where(InterviewProfile.user_id == user.id)
        .order_by(InterviewProfile.updated_at.desc())
        .limit(MAX_PROFILES_LISTED)
    ).all()
    result = []
    for profile in profiles:
        rounds_done = _started_count(db, profile.id)
        last_score = db.scalar(
            select(Interview.overall_score)
            .where(Interview.profile_id == profile.id, Interview.overall_score.is_not(None))
            .order_by(Interview.created_at.desc())
            .limit(1)
        )
        result.append(
            {
                **{c: getattr(profile, c) for c in PROFILE_FIELDS},
                "rounds_done": rounds_done,
                "next_round": rounds_done + 1,
                "last_score": last_score,
            }
        )
    return result


def delete_profile(db: Session, user: User, profile_id: uuid.UUID) -> None:
    """Removes a saved profile. Its interviews and reports stay."""
    db.delete(_get_profile(db, user, profile_id))
    db.commit()


MAX_ROLE_SUGGESTIONS = 6
MAX_ROLE_LENGTH = 100  # matches Interview.role
# many users type the same prefixes ("pyth", "data sc"), so keep recent answers in memory;
# a dict keeps insertion order, so the first key is the oldest
_ROLE_CACHE_SIZE = 500
_role_cache: dict[str, list[str]] = {}


def suggest_roles(query: str) -> list[str]:
    """AI suggestions of job titles for a partly typed role. Raises 503 if the AI fails."""
    key = " ".join(query.lower().split())
    if key in _role_cache:
        return _role_cache[key]
    try:
        data = generate_json(
            system=prompts.load("system"),
            prompt=prompts.render(
                "role_suggestions", query=prompts.escape_user_text(key), limit=MAX_ROLE_SUGGESTIONS
            ),
            schema=ROLE_SUGGESTIONS_SCHEMA,
            tier="fast",  # shown while typing, so speed matters most
            max_tokens=4000,
        )
    except LLMError as e:
        logger.warning("Could not suggest roles: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_SUGGESTIONS)

    roles: list[str] = []
    for raw in data["roles"]:
        role = " ".join(raw.split())
        if 2 <= len(role) <= MAX_ROLE_LENGTH and role.lower() not in {r.lower() for r in roles}:
            roles.append(role)
    roles = roles[:MAX_ROLE_SUGGESTIONS]

    if len(_role_cache) >= _ROLE_CACHE_SIZE:
        _role_cache.pop(next(iter(_role_cache)))
    _role_cache[key] = roles
    return roles


def list_interviews(db: Session, user: User, limit: int, offset: int) -> tuple[list[Interview], int]:
    """Returns (interviews on this page, total count for this user)."""
    total = db.scalar(select(func.count()).select_from(Interview).where(Interview.user_id == user.id))
    interviews = db.scalars(
        select(Interview)
        .where(Interview.user_id == user.id)
        .order_by(Interview.created_at.desc(), Interview.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(interviews), total


def get_interview(db: Session, user: User, interview_id: uuid.UUID, lock: bool = False) -> Interview:
    # filtering by user_id too means someone else's interview looks exactly like a missing one,
    # so nobody can find out which interview ids exist
    query = select(Interview).where(Interview.id == interview_id, Interview.user_id == user.id)
    if lock:
        # FOR UPDATE: a second request for the same row waits until this transaction ends
        query = query.with_for_update()
    interview = db.scalar(query)
    if interview is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=INTERVIEW_NOT_FOUND)
    return interview


def _difficulty_note(difficulty_round: int) -> str:
    return DIFFICULTY_NOTES[min(difficulty_round, len(DIFFICULTY_NOTES)) - 1]


def _interview_context(interview: Interview) -> dict:
    """Placeholder values shared by the prompts. User-typed text is escaped."""
    history = interview.previous_questions or []
    return {
        "role": prompts.escape_user_text(interview.role),
        "job_description": prompts.escape_user_text(interview.job_description),
        "resume": prompts.escape_user_text(interview.resume_text),
        "level": interview.level.value,
        "experience_years": interview.experience_years,
        "interview_type": interview.interview_type.value,
        "num_questions": interview.num_questions,
        "difficulty_round": interview.difficulty_round,
        "difficulty_note": _difficulty_note(interview.difficulty_round),
        # earlier rounds on the same profile: the AI must not ask these again
        "previous_concepts": prompts.escape_user_text(
            "\n".join(dict.fromkeys(f"- {q['concept']}" for q in history))  # unique, order kept
        ),
        "previous_questions": prompts.escape_user_text("\n".join(f"- {q['text']}" for q in history)),
    }


def _concept_key(concept: str) -> str:
    return " ".join(concept.lower().split())


def _build_topic_plan(interview: Interview) -> list[str]:
    prompt = prompts.render(
        "topic_plan",
        **_interview_context(interview),
        slots="\n".join(f"{i}. {kind}" for i, kind in enumerate(_question_kinds(interview), 1)),
    )
    earlier = {_concept_key(q["concept"]) for q in interview.previous_questions or []}

    for attempt in range(2):
        data = generate_json(
            system=prompts.load("system"),
            prompt=prompt,
            schema=TOPIC_PLAN_SCHEMA,
            tier="fast",  # /start may be waiting for it
        )
        # clean up: trim, drop blanks and duplicates, keep within the column size
        concepts: list[str] = []
        for raw in data["concepts"]:
            concept = str(raw).strip()[:MAX_CONCEPT_LENGTH]
            if concept and _concept_key(concept) not in {_concept_key(c) for c in concepts}:
                concepts.append(concept)

        # the schema can't force an exact list length, so check it here
        if len(concepts) < interview.num_questions:
            raise LLMError(f"Topic plan too short: {len(concepts)} of {interview.num_questions}")
        concepts = concepts[: interview.num_questions]

        # the fast model sometimes reuses a topic from an earlier round: ask once more, naming them
        repeated = [c for c in concepts if _concept_key(c) in earlier]
        if not repeated or attempt == 1:
            return concepts
        prompt += (
            "\n\nYour last answer reused these concepts from earlier rounds: "
            + "; ".join(prompts.escape_user_text(c) for c in repeated)
            + ". Replace each with a new concept of the same kind."
        )
    return concepts


def _generate_planned_question(interview: Interview, question_no: int) -> Question:
    concept = interview.topic_plan[question_no - 1]
    kind = _question_kinds(interview)[question_no - 1]
    context = _interview_context(interview)
    if kind in ("concept", "scenario"):
        # the model leans on the CV whenever it can see it, so hide it to keep these general
        context["resume"] = "(not used for this question)"
    data = generate_json(
        system=prompts.load("system"),
        prompt=prompts.render(
            "planned_question",
            **context,
            question_no=question_no,
            concept=prompts.escape_user_text(concept),
            kind=kind,
            kind_guide=QUESTION_KIND_GUIDES[kind],
        ),
        schema=QUESTION_SCHEMA,
        # question 1 may be awaited by /start; the others are written in the background
        tier="fast" if question_no == 1 else "small",
    )
    text = data["question"].strip()
    if not text:
        raise LLMError("AI returned an empty question")
    return Question(
        interview_id=interview.id,
        order_no=question_no,
        text=text,
        concept=concept,
        difficulty=QuestionDifficulty(data["difficulty"]),
        source=QuestionSource.PLANNED,
    )


def _generate_all_questions(interview: Interview) -> tuple[list[str], list[Question]]:
    """Topic plan, then every question at the same time. Raises LLMError if any call fails."""
    interview.topic_plan = _build_topic_plan(interview)
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_AI_CALLS) as pool:
        questions = list(
            pool.map(
                lambda n: _generate_planned_question(interview, n),
                range(1, interview.num_questions + 1),
            )
        )
    return interview.topic_plan, questions


def _has_first_question(db: Session, interview: Interview) -> bool:
    if interview.topic_plan is None:
        return False
    return db.scalar(
        select(Question.id).where(
            Question.interview_id == interview.id, Question.order_no == 1, Question.sub_no == 0
        )
    ) is not None


def mark_preparing(interview_id: uuid.UUID) -> None:
    """Called before prepare_interview is scheduled, so a quick /start knows to wait for it."""
    with _jobs_lock:
        _preparing.setdefault(interview_id, threading.Event())


def _done_preparing(interview_id: uuid.UUID) -> None:
    with _jobs_lock:
        event = _preparing.pop(interview_id, None)
    if event:
        event.set()  # wakes up a /start that is waiting


def _save_missing_questions(db: Session, interview_id: uuid.UUID, questions: list[Question]) -> None:
    """Saves the questions whose position is still empty. submit_answer may have written one
    itself meanwhile; both lock the interview row first, so they never write the same position."""
    if db.scalar(select(Interview.id).where(Interview.id == interview_id).with_for_update()) is None:
        db.rollback()
        return  # deleted meanwhile (e.g. the account was removed)
    existing = set(
        db.scalars(
            select(Question.order_no).where(Question.interview_id == interview_id, Question.sub_no == 0)
        )
    )
    added = [q for q in questions if q.order_no not in existing]
    db.add_all(added)
    db.commit()
    for q in added:
        speech.prewarm(q.id, q.text)


def prepare_interview(interview_id: uuid.UUID) -> None:
    """Background task after the interview is created: writes the topic plan and all questions,
    so /start and /answers only read the database instead of waiting for the AI.
    Question 1 is saved as soon as it's ready, so /start never waits for the rest.
    If this fails, /start (or submit_answer, for a later question) writes them itself."""
    try:
        with Local_Session() as db:
            interview = db.get(Interview, interview_id)
            if interview is None or interview.status != InterviewStatus.CREATED or interview.topic_plan:
                return
            # detach it and end the transaction, so no connection sits idle during the AI calls
            db.expunge(interview)
            db.rollback()

            with ThreadPoolExecutor(max_workers=MAX_PARALLEL_AI_CALLS) as pool:
                try:
                    interview.topic_plan = _build_topic_plan(interview)
                    futures = [
                        pool.submit(_generate_planned_question, interview, n)
                        for n in range(1, interview.num_questions + 1)
                    ]
                    first = futures[0].result()
                except LLMError as e:
                    logger.warning("Could not prepare interview %s: %s", interview_id, e)
                    return

                locked = db.scalar(select(Interview).where(Interview.id == interview_id).with_for_update())
                # the user may have started it meanwhile (and /start then wrote the questions itself)
                if locked is None or locked.status != InterviewStatus.CREATED or locked.topic_plan:
                    db.rollback()
                    for f in futures:
                        f.cancel()
                    return
                locked.topic_plan = interview.topic_plan
                db.add(first)
                db.commit()
                _done_preparing(interview_id)  # /start can go ahead now
                speech.prewarm(first.id, first.text)  # the voice for question 1

                try:
                    rest = [f.result() for f in futures[1:]]
                except LLMError as e:
                    # submit_answer writes any missing question when the candidate reaches it
                    logger.warning("Could not prepare later questions of interview %s: %s", interview_id, e)
                    return
            _save_missing_questions(db, interview_id, rest)
    except Exception:
        logger.exception("Preparing interview %s failed", interview_id)
    finally:
        _done_preparing(interview_id)


def start_interview(db: Session, user: User, interview_id: uuid.UUID) -> tuple[Interview, Question]:
    """Moves the interview to in_progress and returns question 1. The questions are normally
    ready from prepare_interview; if not, they are generated here."""
    with _jobs_lock:
        preparing = _preparing.get(interview_id)
    if preparing is not None:
        # still being prepared in the background: waiting is quicker than starting over
        preparing.wait(timeout=PREPARE_WAIT_SECONDS)

    interview = get_interview(db, user, interview_id, lock=True)
    if interview.status != InterviewStatus.CREATED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_STARTED)
    # cheap check first, so we don't pay for AI calls the user can't pay for
    billing.check_can_create(user, interview.num_questions)

    if not _has_first_question(db, interview):
        try:
            _, questions = _generate_all_questions(interview)
        except LLMError as e:
            logger.warning("Could not start interview %s: %s", interview_id, e)
            # undo everything (topic_plan included), so status stays "created" and the user can retry
            db.rollback()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE)
        db.add_all(questions)

    try:
        # spent only after the questions exist; the commit below saves it together with the start
        interview.credit_type = billing.use_interview_credit(db, user.id, interview.num_questions)
    except HTTPException:
        db.rollback()
        raise
    interview.current_question_no = 1
    interview.current_sub_no = 0
    interview.status = InterviewStatus.IN_PROGRESS
    db.commit()
    db.refresh(interview)
    question = db.scalar(
        select(Question).where(
            Question.interview_id == interview.id, Question.order_no == 1, Question.sub_no == 0
        )
    )
    # usually ready already; does nothing then
    speech.prewarm(question.id, question.text)
    return interview, question


def get_question_audio(db: Session, user: User, interview_id: uuid.UUID, question_id: uuid.UUID) -> bytes:
    """The question read aloud by the interviewer, as mp3."""
    interview = get_interview(db, user, interview_id)  # 404 if missing or not the owner
    question = db.scalar(
        select(Question).where(
            Question.id == question_id,
            Question.interview_id == interview.id,
            # questions written ahead stay hidden until the candidate reaches them
            Question.order_no <= interview.current_question_no,
        )
    )
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=QUESTION_NOT_FOUND)
    if not settings.TTS_ENABLED:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=VOICE_UNAVAILABLE)
    question_text = question.text
    db.rollback()  # don't keep a transaction open while the audio is made
    try:
        return speech.get_audio(question_id, question_text)
    except speech.SpeechError as e:
        logger.warning("No audio for question %s: %s", question_id, e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=VOICE_UNAVAILABLE)


def list_questions(db: Session, user: User, interview_id: uuid.UUID) -> list[Question]:
    interview = get_interview(db, user, interview_id)  # 404 if missing or not the owner
    # later questions are already written, but the candidate only sees the ones reached so far
    questions = db.scalars(
        select(Question)
        .where(Question.interview_id == interview_id, Question.order_no <= interview.current_question_no)
        .order_by(Question.order_no, Question.sub_no)
    ).all()
    return list(questions)


def _clean_points(points: list[str], limit: int = 4) -> list[str]:
    return [p.strip() for p in points if p.strip()][:limit]


def _build_final_feedback(interview: Interview, answered: list[tuple[Question, Answer]]) -> dict:
    """One AI call over the whole interview. Returns {"scores", "summary", "strengths", "improvements"}."""
    transcript = "\n\n".join(
        f"Question {question.order_no}{' (follow-up)' if question.sub_no else ''}"
        f" (concept: {prompts.escape_user_text(question.concept)})\n"
        f"<question>{prompts.escape_user_text(question.text)}</question>\n"
        f"<answer>{prompts.escape_user_text(answer.answer_text)}</answer>"
        for question, answer in answered
    )
    data = generate_json(
        system=prompts.load("system"),
        prompt=prompts.render(
            "final_feedback",
            **_interview_context(interview),
            num_answers=len(answered),
            transcript=transcript,
        ),
        schema=FINAL_FEEDBACK_SCHEMA,
        tier="large",  # judging the whole interview needs more care than writing a question
    )
    if len(data["scores"]) != len(answered):
        raise LLMError(f"AI gave {len(data['scores'])} scores for {len(answered)} answers")
    summary = data["summary"].strip()
    if not summary:
        raise LLMError("AI returned an empty summary")
    return {
        "scores": data["scores"],
        "summary": summary,
        "strengths": _clean_points(data["strengths"]),
        "improvements": _clean_points(data["improvements"]),
    }


def _answered(db: Session, interview_id: uuid.UUID) -> list[tuple[Question, Answer]]:
    return list(
        db.execute(
            select(Question, Answer)
            .join(Answer, Answer.question_id == Question.id)
            .where(Question.interview_id == interview_id)
            .order_by(Question.order_no, Question.sub_no)
        ).tuples().all()
    )


def get_coach_tip(db: Session, user: User, interview_id: uuid.UUID) -> dict:
    """A short coaching tip from the latest answers, shown as a pop-up during the interview so the
    candidate can do better on the rest. Only for the candidate: it doesn't change any score."""
    interview = get_interview(db, user, interview_id)
    if interview.status != InterviewStatus.IN_PROGRESS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_IN_PROGRESS)
    answered = _answered(db, interview_id)
    if len(answered) < COACH_MIN_ANSWERS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=COACH_TOO_EARLY)

    key = (interview_id, len(answered))
    with _jobs_lock:
        if key in _coach_tips:
            return _coach_tips[key]  # e.g. the page was reloaded: don't pay for it twice

    recent = answered[-COACH_RECENT_ANSWERS:]
    transcript = "\n\n".join(
        f"<question>{prompts.escape_user_text(q.text)}</question>\n<answer>{prompts.escape_user_text(a.answer_text)}</answer>"
        for q, a in recent
    )
    context = _interview_context(interview)
    db.rollback()  # don't keep a transaction open during the AI call
    try:
        data = generate_json(
            system=prompts.load("system"),
            prompt=prompts.render("coach_tip", **context, transcript=transcript),
            schema=COACH_TIP_SCHEMA,
            tier="fast",
            max_tokens=1500,
            timeout=COACH_TIMEOUT_SECONDS,
        )
    except LLMError as e:
        logger.info("No coach tip for interview %s: %s", interview_id, e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_COACH)

    tips = _clean_points(data["tips"], limit=3)
    headline = data["headline"].strip()
    if not tips or not headline:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_COACH)
    tip = {
        "headline": headline,
        "tips": tips,
        "encouragement": data["encouragement"].strip(),
        "after_answers": len(answered),
    }
    with _jobs_lock:
        if len(_coach_tips) >= COACH_CACHE_SIZE:
            _coach_tips.pop(next(iter(_coach_tips)))  # the oldest
        _coach_tips[key] = tip
    return tip


def needs_feedback(interview: Interview) -> bool:
    return interview.status == InterviewStatus.COMPLETED and interview.final_feedback is None


def score_interview(interview_id: uuid.UUID) -> None:
    """Scores the finished interview with one AI call and saves the feedback.
    Runs in the background after the last answer, and again from POST /{id}/feedback if that failed.
    Raises LLMError if the AI fails every attempt."""
    with _jobs_lock:
        if interview_id in _scoring:
            return  # already running
        _scoring.add(interview_id)
    try:
        with Local_Session() as db:
            interview = db.get(Interview, interview_id)
            if interview is None or not needs_feedback(interview):
                return
            answered = _answered(db, interview_id)
            # detach everything and end the transaction, so no connection sits idle during the AI call
            db.expunge_all()
            db.rollback()

            for attempt in range(1, FEEDBACK_ATTEMPTS + 1):
                try:
                    feedback = _build_final_feedback(interview, answered)
                    break
                except LLMError as e:
                    logger.warning("Scoring interview %s failed (attempt %s): %s", interview_id, attempt, e)
                    if attempt == FEEDBACK_ATTEMPTS:
                        raise

            interview = db.scalar(select(Interview).where(Interview.id == interview_id).with_for_update())
            if interview is None or not needs_feedback(interview):
                db.rollback()
                return
            scores = dict(zip((a.id for _, a in answered), feedback["scores"]))
            for _, saved_answer in _answered(db, interview_id):
                saved_answer.score = scores.get(saved_answer.id)
            # average worked out here, not by the AI, so it always matches the per-question scores
            interview.overall_score = round(sum(feedback["scores"]) / len(feedback["scores"]), 1)
            interview.final_feedback = {k: feedback[k] for k in ("summary", "strengths", "improvements")}
            db.commit()
    finally:
        with _jobs_lock:
            _scoring.discard(interview_id)


def score_interview_in_background(interview_id: uuid.UUID) -> None:
    try:
        score_interview(interview_id)
    except LLMError:
        pass  # already logged; the report page retries through POST /{id}/feedback
    except Exception:
        logger.exception("Scoring interview %s failed", interview_id)


def retry_feedback(db: Session, user: User, interview_id: uuid.UUID) -> Interview:
    """For the report page when background scoring failed: scores the interview now."""
    interview = get_interview(db, user, interview_id)
    if interview.status != InterviewStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_COMPLETED)
    if needs_feedback(interview):
        db.rollback()  # don't keep a transaction open during the AI call
        try:
            score_interview(interview_id)
        except LLMError:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_FEEDBACK)
        db.refresh(interview)
    return interview


def _maybe_follow_up(db: Session, interview: Interview, question: Question, answer: Answer) -> Question | None:
    """Like a real interviewer, asks one follow-up that builds on the answer when it's worth it
    (vague, interesting, wrong, or strong enough to go deeper). The AI decides; None means move on.
    Never fails the request: if the AI is slow or down, the interview simply moves on."""
    if question.sub_no > 0:
        return None  # one follow-up per question, like an interviewer who then changes topic
    if len(answer.answer_text) < MIN_ANSWER_FOR_FOLLOW_UP:
        return None  # "I don't know" and the like: nothing to build on
    asked = db.scalar(
        select(func.count()).select_from(Question).where(
            Question.interview_id == interview.id, Question.sub_no > 0
        )
    )
    if asked >= max(1, interview.num_questions // 2):
        return None  # keeps the interview close to the length the user chose

    try:
        data = generate_json(
            system=prompts.load("system"),
            prompt=prompts.render(
                "follow_up",
                role=prompts.escape_user_text(interview.role),
                level=interview.level.value,
                experience_years=interview.experience_years,
                concept=prompts.escape_user_text(question.concept),
                question=prompts.escape_user_text(question.text),
                answer=prompts.escape_user_text(answer.answer_text),
            ),
            schema=FOLLOW_UP_SCHEMA,
            tier="fast",  # the candidate is waiting for the next question
            max_tokens=1000,
            timeout=FOLLOW_UP_TIMEOUT_SECONDS,
        )
    except LLMError as e:
        logger.info("No follow-up for interview %s: %s", interview.id, e)
        return None

    text = data["question"].strip()
    if not data["follow_up"] or not text:
        return None
    return Question(
        interview_id=interview.id,
        order_no=question.order_no,
        sub_no=1,
        text=text,
        concept=question.concept,
        difficulty=QuestionDifficulty(data["difficulty"]),
        source=QuestionSource.FOLLOW_UP,
        triggered_by_answer_id=answer.id,
    )


def submit_answer(
    db: Session, user: User, interview_id: uuid.UUID, data: AnswerCreate
) -> tuple[Answer, Question | None, Interview]:
    """Saves the answer to the current question and returns the next one: a follow-up on this
    answer when the AI thinks one is worth asking, otherwise the next main question (already
    written by prepare_interview). After the last answer the interview is completed straight away; the score
    and feedback are added in the background by score_interview, so the caller must schedule it
    when needs_feedback(interview) is true.
    Returns (answer, next question or None, interview)."""
    interview = get_interview(db, user, interview_id, lock=True)
    if interview.status != InterviewStatus.IN_PROGRESS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_IN_PROGRESS)

    question = db.scalar(
        select(Question).where(Question.id == data.question_id, Question.interview_id == interview.id)
    )
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=QUESTION_NOT_FOUND)
    # also stops answering the same question twice: after an answer, the current position moves on
    if (question.order_no, question.sub_no) != (interview.current_question_no, interview.current_sub_no):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_CURRENT_QUESTION)

    answer = Answer(interview_id=interview.id, question_id=question.id, answer_text=data.answer_text)
    db.add(answer)
    db.flush()  # gives the answer its id, which a follow-up points back to

    next_question = _maybe_follow_up(db, interview, question, answer)
    if next_question is not None:
        db.add(next_question)
        interview.current_sub_no = next_question.sub_no
    elif interview.current_question_no >= interview.num_questions:
        interview.status = InterviewStatus.COMPLETED  # feedback follows in the background
    else:
        next_no = question.order_no + 1
        next_question = db.scalar(
            select(Question).where(
                Question.interview_id == interview.id, Question.order_no == next_no, Question.sub_no == 0
            )
        )
        if next_question is None:
            # interviews started before questions were prepared up front: write it now
            try:
                next_question = _generate_planned_question(interview, next_no)
            except LLMError as e:
                logger.warning("Could not process answer for interview %s: %s", interview_id, e)
                db.rollback()  # nothing saved, the user can submit the same answer again
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_ANSWER)
            db.add(next_question)
        interview.current_question_no = next_no
        interview.current_sub_no = 0
    db.commit()
    db.refresh(answer)
    db.refresh(interview)
    if next_question is not None:
        db.refresh(next_question)
        # a follow-up is new; a main question is usually ready already, and then this does nothing
        speech.prewarm(next_question.id, next_question.text)
    return answer, next_question, interview
