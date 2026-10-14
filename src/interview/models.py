import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.utils.db import Base


class InterviewLevel(str, enum.Enum):
    FRESHER = "fresher"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"


class InterviewType(str, enum.Enum):
    TECHNICAL = "technical"
    HR = "hr"
    MIXED = "mixed"


class InterviewStatus(str, enum.Enum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class QuestionDifficulty(str, enum.Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class QuestionSource(str, enum.Enum):
    PLANNED = "planned"  # comes from the topic plan
    CONCEPT_SWITCH = "concept_switch"  # asked after switching away from a concept
    FOLLOW_UP = "follow_up"  # digs into the candidate's previous answer


def _enum_column(enum_cls: type[enum.Enum]) -> Enum:
    # stored as VARCHAR with the lowercase values ("mid", not "MID");
    # native_enum=False avoids Postgres ENUM types, which are painful to change in migrations
    return Enum(
        enum_cls,
        native_enum=False,
        length=20,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class InterviewProfile(Base):
    """The details of an interview set-up, saved so the user can pick them again next time.
    One per role per user; every interview made from it is a new, harder round."""

    __tablename__ = "interview_profiles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    level: Mapped[InterviewLevel] = mapped_column(_enum_column(InterviewLevel), nullable=False)
    experience_years: Mapped[int] = mapped_column(Integer, nullable=False)
    interview_type: Mapped[InterviewType] = mapped_column(_enum_column(InterviewType), nullable=False)
    num_questions: Mapped[int] = mapped_column(Integer, nullable=False)
    job_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_text: Mapped[str] = mapped_column(Text, nullable=False)
    resume_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # bumped on every use, so the most recent profile is listed first
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Interview(Base):
    __tablename__ = "interviews"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # the saved profile this was made from; deleting the profile keeps the interview
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("interview_profiles.id", ondelete="SET NULL"), index=True, nullable=True
    )
    # 1 = first interview on this profile; each later round is asked to be harder
    difficulty_round: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    # questions from earlier rounds on the same profile, so they aren't asked again:
    # [{"concept": str, "text": str}], newest first; saved when the interview is created
    previous_questions: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    level: Mapped[InterviewLevel] = mapped_column(_enum_column(InterviewLevel), nullable=False)
    experience_years: Mapped[int] = mapped_column(Integer, nullable=False)
    job_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # plain text read from the uploaded CV; required for new interviews (null only on old rows)
    resume_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interview_type: Mapped[InterviewType] = mapped_column(_enum_column(InterviewType), nullable=False)
    num_questions: Mapped[int] = mapped_column(Integer, default=5, server_default="5", nullable=False)
    status: Mapped[InterviewStatus] = mapped_column(
        _enum_column(InterviewStatus),
        default=InterviewStatus.CREATED,
        server_default=InterviewStatus.CREATED.value,
        nullable=False,
    )
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ordered list of concept names, one per question; filled by POST /interviews/{id}/start
    topic_plan: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    current_question_no: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 0 = the main question current_question_no is waiting for an answer, 1 = its follow-up is
    current_sub_no: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    switch_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # {"summary": str, "strengths": [str], "improvements": [str]}; filled once when the interview ends
    final_feedback: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # "free" or "paid": which credit paid for it; null until the interview starts
    credit_type: Mapped[str | None] = mapped_column(String(10), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Question(Base):
    __tablename__ = "questions"
    # one question per position, so two parallel requests can't both save question 1
    __table_args__ = (UniqueConstraint("interview_id", "order_no", "sub_no"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_no: Mapped[int] = mapped_column(Integer, nullable=False)  # main question number: 1, 2, 3...
    # 0 = the main question, 1 = a follow-up to it (it shares the main question's order_no)
    sub_no: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    concept: Mapped[str] = mapped_column(String(200), nullable=False)
    difficulty: Mapped[QuestionDifficulty] = mapped_column(_enum_column(QuestionDifficulty), nullable=False)
    source: Mapped[QuestionSource] = mapped_column(_enum_column(QuestionSource), nullable=False)
    # no ForeignKey yet: the answers table comes in a later phase
    triggered_by_answer_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # unique: each question can be answered only once
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 0-10, filled by the final feedback when the interview ends (null until then)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
