"""持久化任务队列：文档处理任务的入队、认领与断点恢复。

配合 `app.worker` 使用。设计要点：
- 每个 Document 自身即任务单元，用 status + attempts + claimed_at 描述队列状态；
- `worker` 进程认领任务后标记 processing 并记录 claimed_at；
- 若进程中途崩溃，该任务停留在 processing 且 claimed_at 陈旧，重启后会被重新认领（断点恢复）；
- 失败任务若 attempts < max_attempts，会被自动重试。
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Document


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue_document(db: Session, document_id: int) -> None:
    """将文档置为待处理（重置重试计数）。"""
    document = db.get(Document, document_id)
    if not document:
        return
    document.status = "pending"
    document.attempts = 0
    document.claimed_at = None
    document.error_message = ""
    db.commit()


def claim_next(db: Session, worker_id: str) -> Document | None:
    """按优先级认领下一个任务：待处理 > 失败可重试 > 中断超时。认领时占用并提交。"""
    stale_before = _now() - timedelta(seconds=settings.worker_stale_timeout_seconds)

    pending = db.scalars(
        select(Document).where(Document.status == "pending").order_by(Document.id)
    ).all()
    candidates = pending or db.scalars(
        select(Document)
        .where(
            Document.status == "failed",
            Document.attempts < Document.max_attempts,
        )
        .order_by(Document.id)
    ).all()
    candidates = candidates or db.scalars(
        select(Document)
        .where(Document.status == "processing", Document.claimed_at < stale_before)
        .order_by(Document.id)
    ).all()

    for document in candidates:
        document.status = "processing"
        document.attempts += 1
        document.claimed_at = _now()
        db.commit()
        db.refresh(document)
        return document
    return None


def mark_ready(db: Session, document_id: int) -> None:
    document = db.get(Document, document_id)
    if document:
        document.status = "ready"
        document.claimed_at = None
        db.commit()


def mark_failed(db: Session, document_id: int, message: str) -> None:
    document = db.get(Document, document_id)
    if document:
        document.status = "failed"
        document.error_message = message[:1000]
        document.claimed_at = None
        db.commit()


def recover_interrupted(db: Session) -> int:
    """把中断的任务（processing 且 claimed_at 为空或过期）重置回 pending，返回数量。"""
    stale_before = _now() - timedelta(seconds=settings.worker_stale_timeout_seconds)
    documents = db.scalars(
        select(Document).where(Document.status == "processing")
    ).all()
    recovered = 0
    for document in documents:
        if document.claimed_at is None or document.claimed_at < stale_before:
            document.status = "pending"
            recovered += 1
    db.commit()
    return recovered
