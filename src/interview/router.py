import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Response, status
from sqlalchemy.orm import Session

from src.interview import services
from src.interview.resume import read_resume
from src.interview.schemas import (
    AnswerCreate,
    AnswerResponse,
    AnswerSubmitResponse,
    InterviewCreate,
    InterviewCreateForm,
    InterviewListResponse,
    InterviewResponse,
    InterviewStartResponse,
    QuestionResponse,
    RoleSuggestions,
)
from src.user.dependencies import get_current_user
from src.user.models import User
from src.utils.db import get_db

# every route in this router needs a logged-in user
router = APIRouter(prefix="/interviews", tags=["Interviews"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InterviewResponse)
def create_interview(
    # multipart/form-data: the interview fields plus the resume file (PDF or DOCX, max 5 MB)
    form: Annotated[InterviewCreateForm, Form()],
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    resume_text, resume_filename = read_resume(form.resume)
    data = InterviewCreate.model_validate(form.model_dump(exclude={"resume"}))
    interview = services.create_interview(db, current_user, data, resume_text, resume_filename)
    # write all the questions now, while the user reads the details page, so /start is instant
    services.mark_preparing(interview.id)
    background_tasks.add_task(services.prepare_interview, interview.id)
    return interview


# declared before "/{interview_id}" so "role-suggestions" isn't read as an interview id
@router.get("/role-suggestions", response_model=RoleSuggestions)
def role_suggestions(
    q: str = Query(min_length=2, max_length=100),
    current_user: User = Depends(get_current_user),
):
    return RoleSuggestions(suggestions=services.suggest_roles(q))


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
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    answer, next_question, interview = services.submit_answer(db, current_user, interview_id, data)
    if services.needs_feedback(interview):
        # last answer: reply now and score in the background; the report page waits for it
        background_tasks.add_task(services.score_interview_in_background, interview.id)
    interview_data = InterviewResponse.model_validate(interview)
    return AnswerSubmitResponse(
        answer=AnswerResponse.model_validate(answer),
        next_question=QuestionResponse.model_validate(next_question) if next_question else None,
        feedback=interview_data.final_feedback,
        interview=interview_data,
    )


@router.post("/{interview_id}/feedback", response_model=InterviewResponse)
def retry_feedback(
    interview_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Scores a completed interview whose background scoring failed. Does nothing if it has feedback."""
    return services.retry_feedback(db, current_user, interview_id)


@router.get(
    "/{interview_id}/questions/{question_id}/audio",
    response_class=Response,
    responses={200: {"content": {"audio/mpeg": {}}, "description": "The question read aloud (mp3)"}},
)
def question_audio(
    interview_id: uuid.UUID,
    question_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The interviewer reading the question aloud. 503 if the voice isn't available; the question text still works."""
    audio = services.get_question_audio(db, current_user, interview_id, question_id)
    # private: only this user may see it; a question's text never changes, so the browser can keep it
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})
