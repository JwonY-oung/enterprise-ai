from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class KnowledgeBaseUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class KnowledgeBaseRead(ORMModel):
    id: int
    name: str
    description: str
    created_at: datetime


class DocumentRead(ORMModel):
    id: int
    knowledge_base_id: int
    filename: str
    status: str
    error_message: str
    created_at: datetime


class DocumentDetail(DocumentRead):
    chunk_count: int


class ChatRequest(BaseModel):
    knowledge_base_id: int
    question: str = Field(min_length=1)
    session_id: int | None = None


class Citation(BaseModel):
    document_id: int
    filename: str
    chunk_id: int
    page_number: int | None = None
    score: float
    excerpt: str


class ChatResponse(BaseModel):
    session_id: int
    answer: str
    citations: list[Citation]


class ChatMessageRead(ORMModel):
    id: int
    role: str
    content: str
    created_at: datetime


class ChatSessionRead(ORMModel):
    id: int
    knowledge_base_id: int
    title: str
    created_at: datetime
    message_count: int
    last_message_at: datetime | None = None


class ChatSessionDetail(ChatSessionRead):
    messages: list[ChatMessageRead]


class AuditLogRead(ORMModel):
    id: int
    action: str
    entity_type: str
    entity_id: int | None
    detail: str
    ip: str
    created_at: datetime
