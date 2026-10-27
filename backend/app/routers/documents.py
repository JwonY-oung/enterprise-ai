from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.dependencies import get_current_user
from app.models import Document, KnowledgeBase, User
from app.schemas import DocumentRead
from app.services import queue
from app.services.audit import audit
from app.services.documents import SUPPORTED_SUFFIXES, process_document

router = APIRouter(prefix="/documents", tags=["documents"])
ALLOWED_SUFFIXES = SUPPORTED_SUFFIXES


def process_in_new_session(document_id: int) -> None:
    with SessionLocal() as db:
        process_document(document_id, db)


def _enqueue_or_process(background_tasks: BackgroundTasks, db: Session, document_id: int) -> None:
    """task_mode=worker 时入队由 Worker 处理；否则在当前请求后台内联处理。"""
    if settings.task_mode == "worker":
        queue.enqueue_document(db, document_id)
    else:
        background_tasks.add_task(process_in_new_session, document_id)


@router.get("", response_model=list[DocumentRead])
def list_documents(
    knowledge_base_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    knowledge_base = db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id, KnowledgeBase.user_id == user.id
        )
    )
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return db.scalars(select(Document).where(Document.knowledge_base_id == knowledge_base_id)).all()


@router.post("/upload", response_model=DocumentRead, status_code=202)
def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    knowledge_base_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    knowledge_base = db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == knowledge_base_id, KnowledgeBase.user_id == user.id
        )
    )
    suffix = Path(file.filename or "").suffix.lower()
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    if suffix not in ALLOWED_SUFFIXES:
        supported = ", ".join(sorted(ALLOWED_SUFFIXES))
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {supported}")

    target_dir = settings.upload_dir / str(user.id) / str(knowledge_base_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{uuid4().hex}{suffix}"
    with target_path.open("wb") as output:
        while chunk := file.file.read(1024 * 1024):
            output.write(chunk)

    document = Document(
        knowledge_base_id=knowledge_base_id,
        filename=file.filename or target_path.name,
        file_path=str(target_path),
        status="pending" if settings.task_mode == "worker" else "processing",
        max_attempts=settings.worker_max_attempts,
    )
    db.add(document)
    db.flush()
    audit(db, user.id, "document.upload", "document", document.id, file.filename or "", request.client.host if request.client else None)
    db.commit()
    db.refresh(document)
    _enqueue_or_process(background_tasks, db, document.id)
    return document


def owned_document(db: Session, user_id: int, document_id: int) -> Document:
    document = db.scalar(
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .where(Document.id == document_id, KnowledgeBase.user_id == user_id)
    )
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.post("/{document_id}/reprocess", response_model=DocumentRead, status_code=202)
def reprocess_document(
    request: Request,
    document_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    document = owned_document(db, user.id, document_id)
    document.status = "pending" if settings.task_mode == "worker" else "processing"
    document.error_message = ""
    document.attempts = 0
    document.claimed_at = None
    db.flush()
    audit(db, user.id, "document.reprocess", "document", document.id, document.filename, request.client.host if request.client else None)
    db.commit()
    db.refresh(document)
    _enqueue_or_process(background_tasks, db, document.id)
    return document


@router.delete("/{document_id}", status_code=204)
def delete_document(
    request: Request,
    document_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    document = owned_document(db, user.id, document_id)
    path = Path(document.file_path)
    audit(db, user.id, "document.delete", "document", document.id, document.filename, request.client.host if request.client else None)
    db.delete(document)
    db.commit()
    path.unlink(missing_ok=True)
