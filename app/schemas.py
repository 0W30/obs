"""
Pydantic schemas for request/response validation.
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ExceptionInfo(BaseModel):
    """Schema for exception information from Sentry."""
    type: str = Field(..., alias="type")
    value: str = Field(..., alias="value")
    stacktrace: Optional[str] = Field(None, alias="stacktrace")

    class Config:
        populate_by_name = True


class SentryWebhookPayload(BaseModel):
    """Schema for Sentry webhook payload."""
    event_id: str
    project: str
    message: str
    timestamp: int
    exception: Optional[ExceptionInfo] = None

    class Config:
        populate_by_name = True


class ErrorResponse(BaseModel):
    """Schema for error response."""
    id: int
    event_id: str
    project: str
    message: str
    exception_type: Optional[str] = None
    exception_value: Optional[str] = None
    stacktrace: Optional[str] = None
    timestamp: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class ErrorNotFoundResponse(BaseModel):
    """Schema for not found error response."""
    error: str = "not_found"

