import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.billing import services as billing
from src.interview import prompts, speech
from src.interview.models import (
    Answer,
    Interview,
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
VOICE_UNAVAILABLE = "The interviewer's voice is not available right now. You can still read the question."
AI_UNAVAILABLE_FEEDBACK = "Our AI interviewer is busy right now. Your answers are saved, please try again in a minute."
AI_UNAVAILABLE_SUGGESTIONS ="Role suggestions are not available right now."
AI_UNAVAILABLE_ANSWER ="Our AI interviewer is busy right now. Your answer was not saved, please submit it again in a minute."

MAX_CONCEPT_LENGTH = 200  # matches Question.concept

# questions are written at the same time; more than this risks the provider's rate limit
MAX_PARALLEL_AI_CALLS = 5
# how long /start waits for the background preparation before writing the questions itself
PREPARE_WAIT_SECONDS = 60
FEEDBACK_ATTEMPTS = 2
# deciding on a follow-up usually takes ~2 s; past this the interview just moves on without one
FOLLOW_UP_TIMEOUT_SECONDS = 6.0
# shorter answers ("I don't know", "no idea") have nothing to follow up on, so skip the AI call
MIN_ANSWER_FOR_FOLLOW_UP = 25

# background jobs running in this process, so a request can wait for one instead of repeating it
_jobs_lock = threading.Lock()
_preparing: dict[uuid.UUID, threading.Event] = {}
_scoring: set[uuid.UUID] = set()

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


def create_interview(
    db: Session, user: User, data: InterviewCreate, resume_text: str, resume_filename: str
) -> Interview:
    # 402 early, before the user fills in everything; the credit itself is spent on start
    billing.check_can_create(user, data.num_questions)
    interview = Interview(
        user_id=user.id, resume_text=resume_text, resume_filename=resume_filename, **data.model_dump()
    )
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return interview


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


def _interview_context(interview: Interview) -> dict:
    """Placeholder values shared by the prompts. User-typed text is escaped."""
    return {
        "role": prompts.escape_user_text(interview.role),
        "job_description": prompts.escape_user_text(interview.job_description),
        "resume": prompts.escape_user_text(interview.resume_text),
        "level": interview.level.value,
        "experience_years": interview.experience_years,
        "interview_type": interview.interview_type.value,
        "num_questions": interview.num_questions,
    }


def _build_topic_plan(interview: Interview) -> list[str]:
    data = generate_json(
        system=prompts.load("system"),
        prompt=prompts.render(
            "topic_plan",
            **_interview_context(interview),
            slots="\n".join(f"{i}. {kind}" for i, kind in enumerate(_question_kinds(interview), 1)),
        ),
        schema=TOPIC_PLAN_SCHEMA,
        tier="fast",  # /start may be waiting for it
    )
    # clean up: trim, drop blanks and duplicates, keep within the column size
    concepts: list[str] = []
    for raw in data["concepts"]:
        concept = str(raw).strip()[:MAX_CONCEPT_LENGTH]
        if concept and concept.lower() not in {c.lower() for c in concepts}:
            concepts.append(concept)

    # the schema can't force an exact list length, so check it here
    if len(concepts) < interview.num_questions:
        raise LLMError(f"Topic plan too short: {len(concepts)} of {interview.num_questions}")
    return concepts[: interview.num_questions]


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
