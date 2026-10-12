import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.interview import prompts
from src.interview.models import (
    Answer,
    Interview,
    InterviewStatus,
    Question,
    QuestionDifficulty,
    QuestionSource,
)
from src.interview.schemas import AnswerCreate, InterviewCreate
from src.user.models import User
from src.utils.llm import LLMError, generate_json

logger = logging.getLogger(__name__)

INTERVIEW_NOT_FOUND = "Interview not found"
ALREADY_STARTED = "Interview has already been started"
NOT_IN_PROGRESS = "Interview is not in progress"
QUESTION_NOT_FOUND = "Question not found"
NOT_CURRENT_QUESTION = "You can only answer the current question"
AI_UNAVAILABLE = "Our AI interviewer is busy right now. Please try starting the interview again in a minute."
AI_UNAVAILABLE_ANSWER = "Our AI interviewer is busy right now. Your answer was not saved, please submit it again in a minute."

MAX_CONCEPT_LENGTH = 200  # matches Question.concept

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


def create_interview(db: Session, user: User, data: InterviewCreate) -> Interview:
    interview = Interview(user_id=user.id, **data.model_dump())
    db.add(interview)
    db.commit()
    db.refresh(interview)
    return interview


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
        "level": interview.level.value,
        "experience_years": interview.experience_years,
        "interview_type": interview.interview_type.value,
        "num_questions": interview.num_questions,
    }


def _build_topic_plan(interview: Interview) -> list[str]:
    data = generate_json(
        system=prompts.load("system"),
        prompt=prompts.render("topic_plan", **_interview_context(interview)),
        schema=TOPIC_PLAN_SCHEMA,
        tier="small",
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
    data = generate_json(
        system=prompts.load("system"),
        prompt=prompts.render(
            "planned_question",
            **_interview_context(interview),
            question_no=question_no,
            concept=prompts.escape_user_text(concept),
        ),
        schema=QUESTION_SCHEMA,
        tier="small",
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


def start_interview(db: Session, user: User, interview_id: uuid.UUID) -> tuple[Interview, Question]:
    """Builds the topic plan and question 1, then moves the interview to in_progress."""
    interview = get_interview(db, user, interview_id, lock=True)
    if interview.status != InterviewStatus.CREATED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_STARTED)

    try:
        interview.topic_plan = _build_topic_plan(interview)
        question = _generate_planned_question(interview, question_no=1)
    except LLMError as e:
        logger.warning("Could not start interview %s: %s", interview_id, e)
        # undo everything (topic_plan included), so status stays "created" and the user can retry
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE)

    db.add(question)
    interview.current_question_no = 1
    interview.status = InterviewStatus.IN_PROGRESS
    db.commit()
    db.refresh(interview)
    db.refresh(question)
    return interview, question


def list_questions(db: Session, user: User, interview_id: uuid.UUID) -> list[Question]:
    get_interview(db, user, interview_id)  # 404 if missing or not the owner
    questions = db.scalars(
        select(Question).where(Question.interview_id == interview_id).order_by(Question.order_no)
    ).all()
    return list(questions)


def _clean_points(points: list[str], limit: int = 4) -> list[str]:
    return [p.strip() for p in points if p.strip()][:limit]


def _build_final_feedback(interview: Interview, answered: list[tuple[Question, Answer]]) -> dict:
    """One AI call over the whole interview. Returns {"scores", "summary", "strengths", "improvements"}."""
    transcript = "\n\n".join(
        f"Question {question.order_no} (concept: {prompts.escape_user_text(question.concept)})\n"
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


def submit_answer(
    db: Session, user: User, interview_id: uuid.UUID, data: AnswerCreate
) -> tuple[Answer, Question | None, Interview]:
    """Saves the answer to the current question and creates the next one. No feedback per answer:
    after the last answer, the whole interview is scored once and the interview is completed.
    Returns (answer, next question or None, interview)."""
    interview = get_interview(db, user, interview_id, lock=True)
    if interview.status != InterviewStatus.IN_PROGRESS:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_IN_PROGRESS)

    question = db.scalar(
        select(Question).where(Question.id == data.question_id, Question.interview_id == interview.id)
    )
    if question is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=QUESTION_NOT_FOUND)
    # also stops answering the same question twice: after an answer, current_question_no moves on
    if question.order_no != interview.current_question_no:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_CURRENT_QUESTION)

    answer = Answer(interview_id=interview.id, question_id=question.id, answer_text=data.answer_text)
    db.add(answer)
    is_last = interview.current_question_no >= interview.num_questions
    try:
        if is_last:
            db.flush()  # sends the new answer to the database so the query below includes it
            answered = db.execute(
                select(Question, Answer)
                .join(Answer, Answer.question_id == Question.id)
                .where(Question.interview_id == interview.id)
                .order_by(Question.order_no)
            ).tuples().all()
            feedback = _build_final_feedback(interview, list(answered))
        else:
            next_question = _generate_planned_question(interview, question.order_no + 1)
    except LLMError as e:
        logger.warning("Could not process answer for interview %s: %s", interview_id, e)
        db.rollback()  # nothing saved, the user can submit the same answer again
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=AI_UNAVAILABLE_ANSWER)

    if is_last:
        for (_, saved_answer), score in zip(answered, feedback["scores"]):
            saved_answer.score = score
        # average worked out here, not by the AI, so it always matches the per-question scores
        interview.overall_score = round(sum(feedback["scores"]) / len(feedback["scores"]), 1)
        interview.final_feedback = {k: feedback[k] for k in ("summary", "strengths", "improvements")}
        interview.status = InterviewStatus.COMPLETED
        next_question = None
    else:
        db.add(next_question)
        interview.current_question_no = next_question.order_no
    db.commit()
    db.refresh(answer)
    db.refresh(interview)
    if next_question is not None:
        db.refresh(next_question)
    return answer, next_question, interview
