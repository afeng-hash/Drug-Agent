"""Request/Response Pydantic models for the API layer."""

from datetime import datetime

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Incoming chat message."""
    message: str = Field(..., min_length=1, max_length=2000, description="用户消息")


class SessionResponse(BaseModel):
    """Session creation/query response."""
    session_id: str
    status: str
    created_at: str
    expires_at: str | None = None


class SessionDetailResponse(BaseModel):
    """Detailed session info with message history."""
    session_id: str
    status: str
    created_at: str
    expires_at: str | None = None
    messages: list[dict] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    postgres: str
    milvus: str
    llm: str


class ErrorResponse(BaseModel):
    """Standard error response."""
    code: str
    message: str
    detail: str | None = None
