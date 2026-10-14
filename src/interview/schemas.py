import uuid
from datetime import datetime

from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.interview.models import (
    InterviewLevel,
    InterviewStatus,
    InterviewType,
    QuestionDifficulty,
    QuestionSource,
)


class InterviewCreate(BaseModel):
    role: str = Field(min_length=2, max_length=100, examples=["Python Backend Developer"])
    level: InterviewLevel
    experience_years: int = Field(ge=0, le=40)
    job_description: str | None = Field(default=None, max_length=5000)
    interview_type: InterviewType
    num_questions: int = Field(default=5, ge=3, le=15)

    # mode="before" runs before the length checks, so " ab " is stripped first
    @field_validator("role", mode="before")
    @classmethod
    def strip_role(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("job_description", mode="before")
    @classmethod
    def empty_job_description_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class InterviewCreateForm(InterviewCreate):
    """POST /interviews body, sent as multipart/form-data so the CV file can come with it.
    The file lives in the same model because FastAPI can't mix a form model with a separate File()."""

    resume: UploadFile  # required: questions are based on the CV


class RoleSuggestions(BaseModel):
    suggestions: list[str]


class FinalFeedback(BaseModel):
    summary: str
    strengths: list[str]
    improvements: list[str]


class InterviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    level: InterviewLevel
    experience_years: int
    job_description: str | None
    resume_filename: str | None  # the resume text itself is not sent back
    interview_type: InterviewType
    num_questions: int
    status: InterviewStatus
    overall_score: float | None
    topic_plan: list[str] | None
    current_question_no: int
    current_sub_no: int  # 1 while a follow-up to current_question_no is waiting for an answer
    switch_count: int
    # null until scored: added in the background a few seconds after the interview is completed
    final_feedback: FinalFeedback | None
    created_at: datetime
    updated_at: datetime


class InterviewListResponse(BaseModel):
    items: list[InterviewResponse]
    total: int
    limit: int
    offset: int


class QuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    interview_id: uuid.UUID
    order_no: int
    sub_no: int  # 0 = main question, 1 = follow-up to main question order_no
    text: str
    concept: str
    difficulty: QuestionDifficulty
    source: QuestionSource
    triggered_by_answer_id: uuid.UUID | None
    created_at: datetime


class InterviewStartResponse(BaseModel):
    interview: InterviewResponse
    question: QuestionResponse


class AnswerCreate(BaseModel):
    question_id: uuid.UUID
    answer_text: str = Field(min_length=1, max_length=5000)

    # mode="before" runs before the length checks, so "   " counts as empty
    @field_validator("answer_text", mode="before")
    @classmethod
    def strip_answer(cls, v):
        return v.strip() if isinstance(v, str) else v


class AnswerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question_id: uuid.UUID
    answer_text: str
    score: int | None  # null until the interview ends
    created_at: datetime


class AnswerSubmitResponse(BaseModel):
    answer: AnswerResponse
    next_question: QuestionResponse | None  # None when that was the last question
    feedback: FinalFeedback | None  # null now: scoring happens in the background, see GET /interviews/{id}
    interview: InterviewResponse  # status "completed" after the last answer
