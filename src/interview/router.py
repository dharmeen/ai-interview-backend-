import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from src.interview import services
from src.interview.schemas import (
    AnswerCreate,
    AnswerResponse,
    AnswerSubmitResponse,
    InterviewCreate,
    InterviewListResponse,
    InterviewResponse,
    InterviewStartResponse,
    QuestionResponse,
)
from src.user.dependencies import get_current_user
from src.user.models import User
from src.utils.db import get_db

# every route in this router needs a logged-in user
router = APIRouter(prefix="/interviews", tags=["Interviews"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InterviewResponse)
def create_interview(
    data: InterviewCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return services.create_interview(db, current_user, data)


@router.get("", response_model=InterviewListResponse)
def list_interviews(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items, total = services.list_interviews(db, current_user, limit, offset)
    return InterviewListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/{interview_id}", response_model=InterviewResponse)
def get_interview(
    interview_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return services.get_interview(db, current_user, interview_id)


@router.post("/{interview_id}/start", response_model=InterviewStartResponse)
def start_interview(
    interview_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    interview, question = services.start_interview(db, current_user, interview_id)
    return InterviewStartResponse(
        interview=InterviewResponse.model_validate(interview),
        question=QuestionResponse.model_validate(question),
    )


@router.get("/{interview_id}/questions", response_model=list[QuestionResponse])
def list_questions(
    interview_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return services.list_questions(db, current_user, interview_id)


@router.post("/{interview_id}/answers", response_model=AnswerSubmitResponse)
def submit_answer(
    interview_id: uuid.UUID,
    data: AnswerCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    answer, next_question, interview = services.submit_answer(db, current_user, interview_id, data)
    interview_data = InterviewResponse.model_validate(interview)
    return AnswerSubmitResponse(
        answer=AnswerResponse.model_validate(answer),
        next_question=QuestionResponse.model_validate(next_question) if next_question else None,
        feedback=interview_data.final_feedback,
        interview=interview_data,
    )
