import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.dependencies import get_current_user
from app.models import ChatMessage, ChatSession, KnowledgeBase, User
from app.schemas import ChatRequest, ChatResponse, ChatSessionDetail, ChatSessionRead
from app.services.audit import audit
from app.services.rag import answer_question

router = APIRouter(prefix="/chat", tags=["chat"])


def sse_event(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def owned_session(db: Session, user_id: int, session_id: int) -> ChatSession:
    session = db.scalar(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def session_summary(db: Session, session: ChatSession) -> ChatSessionRead:
    message_count, last_message_at = db.execute(
        select(func.count(ChatMessage.id), func.max(ChatMessage.created_at)).where(
            ChatMessage.session_id == session.id
        )
    ).one()
    return ChatSessionRead(
        id=session.id,
        knowledge_base_id=session.knowledge_base_id,
        title=session.title,
        created_at=session.created_at,
        message_count=message_count,
        last_message_at=last_message_at,
    )


@router.get("/sessions", response_model=list[ChatSessionRead])
def list_sessions(
    knowledge_base_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    statement = select(ChatSession).where(ChatSession.user_id == user.id)
    if knowledge_base_id is not None:
        statement = statement.where(ChatSession.knowledge_base_id == knowledge_base_id)
    sessions = db.scalars(statement.order_by(ChatSession.created_at.desc())).all()
    summaries = [session_summary(db, session) for session in sessions]
    return sorted(
        summaries,
        key=lambda item: item.last_message_at or item.created_at,
        reverse=True,
    )


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
def get_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = owned_session(db, user.id, session_id)
    messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    ).all()
    return ChatSessionDetail(**session_summary(db, session).model_dump(), messages=messages)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = owned_session(db, user.id, session_id)
    db.delete(session)
    db.commit()
    return Response(status_code=204)


def prepare_chat(
    payload: ChatRequest,
    user: User,
    db: Session,
) -> tuple[KnowledgeBase, ChatSession, list[tuple[str, str]]]:
    knowledge_base = db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == payload.knowledge_base_id, KnowledgeBase.user_id == user.id
        )
    )
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    session = db.get(ChatSession, payload.session_id) if payload.session_id else None
    if payload.session_id and not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session and (session.user_id != user.id or session.knowledge_base_id != knowledge_base.id):
        raise HTTPException(status_code=404, detail="Session not found")
    if not session:
        session = ChatSession(
            user_id=user.id,
            knowledge_base_id=knowledge_base.id,
            title=payload.question[:80],
        )
        db.add(session)
        db.flush()

    recent_messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(settings.chat_history_messages)
    ).all()
    history = [(message.role, message.content) for message in reversed(recent_messages)]
    return knowledge_base, session, history


@router.post("/ask", response_model=ChatResponse)
def ask(payload: ChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    knowledge_base, session, history = prepare_chat(payload, user, db)

    db.add(ChatMessage(session_id=session.id, role="user", content=payload.question))
    answer, citations = answer_question(
        db,
        user.id,
        knowledge_base.id,
        payload.question,
        history=history,
    )
    db.add(ChatMessage(session_id=session.id, role="assistant", content=answer))
    audit(db, user.id, "chat.ask", "chat_session", session.id, payload.question[:500])
    db.commit()
    return ChatResponse(session_id=session.id, answer=answer, citations=citations)


@router.post("/ask/stream")
def ask_stream(
    payload: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    knowledge_base, session, history = prepare_chat(payload, user, db)

    def event_stream() -> Iterator[str]:
        try:
            yield sse_event("status", {"stage": "retrieving", "message": "正在检索知识库"})
            db.add(ChatMessage(session_id=session.id, role="user", content=payload.question))
            answer, citations = answer_question(
                db,
                user.id,
                knowledge_base.id,
                payload.question,
                history=history,
            )
            yield sse_event("status", {"stage": "answering", "message": "正在生成回答"})
            for character in answer:
                yield sse_event("token", {"content": character})
            db.add(ChatMessage(session_id=session.id, role="assistant", content=answer))
            audit(db, user.id, "chat.ask", "chat_session", session.id, payload.question[:500])
            db.commit()
            yield sse_event(
                "citations",
                {"items": [citation.model_dump() for citation in citations]},
            )
            yield sse_event("done", {"session_id": session.id})
        except Exception:
            db.rollback()
            yield sse_event("error", {"message": "回答生成失败，请稍后重试"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
