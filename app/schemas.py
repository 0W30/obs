"""
Pydantic schemas for request/response validation.
"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field


class SentryException(BaseModel):
    """Schema for exception information from Sentry."""
    type: Optional[str] = None
    value: Optional[str] = None
    mechanism: Optional[Dict[str, Any]] = None


class SentryStacktraceFrame(BaseModel):
    """Schema for stacktrace frame."""
    filename: Optional[str] = None
    function: Optional[str] = None
    lineno: Optional[int] = None
    abs_path: Optional[str] = None


class SentryStacktrace(BaseModel):
    """Schema for stacktrace."""
    frames: Optional[List[SentryStacktraceFrame]] = None


class SentryEvent(BaseModel):
    """Schema for Sentry event data."""
    event_id: Optional[str] = None
    message: Optional[str] = None
    title: Optional[str] = None
    platform: Optional[str] = None
    timestamp: Optional[float] = None
    level: Optional[str] = None
    logger: Optional[str] = None
    exceptions: Optional[List[SentryException]] = None
    stacktrace: Optional[SentryStacktrace] = None
    tags: Optional[Dict[str, str]] = None
    extra: Optional[Dict[str, Any]] = None


class SentryIssue(BaseModel):
    """Schema for Sentry issue data."""
    id: Optional[str] = None
    shortId: Optional[str] = None
    title: Optional[str] = None
    culprit: Optional[str] = None
    permalink: Optional[str] = None
    logger: Optional[str] = None
    level: Optional[str] = None
    status: Optional[str] = None
    assignedTo: Optional[Dict[str, Any]] = None
    project: Optional[Dict[str, Any]] = None


class SentryProject(BaseModel):
    """Schema for Sentry project data."""
    id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None


class SentryWebhookData(BaseModel):
    """Schema for Sentry webhook data section."""
    issue: Optional[SentryIssue] = None
    event: Optional[SentryEvent] = None
    project: Optional[SentryProject] = None
    
    class Config:
        extra = "allow"  # Allow extra fields


class SentryWebhookPayload(BaseModel):
    """
    Schema for Sentry webhook payload.
    
    Real Sentry webhook structure:
    {
        "action": "created",  # created, resolved, assigned, etc.
        "installation": {...},
        "data": {
            "issue": {...},
            "event": {...},
            "project": {...}
        },
        "actor": {...}
    }
    """
    action: Optional[str] = None  # created, resolved, assigned, etc.
    installation: Optional[Dict[str, Any]] = None
    data: Optional[SentryWebhookData] = None
    actor: Optional[Dict[str, Any]] = None

    class Config:
        populate_by_name = True
        extra = "allow"  # Allow extra fields for flexibility


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

