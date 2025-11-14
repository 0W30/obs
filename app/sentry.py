"""
Sentry webhook handler.
"""
import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Error
from app.schemas import SentryWebhookPayload
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sentry", tags=["sentry"])


@router.post("/webhook", status_code=status.HTTP_201_CREATED)
async def sentry_webhook(
    payload: SentryWebhookPayload,
    db: AsyncSession = Depends(get_db)
):
    """
    Handle Sentry webhook POST request.
    Validates payload and saves error to database.
    
    If SENTRY_FILTER_BY_PROJECT is enabled and SENTRY_PROJECT is set,
    only accepts webhooks from the specified project.
    """
    try:
        # Only process "created" actions (new issues)
        if payload.action != "created":
            logger.info(f"Ignoring webhook action: {payload.action} (only processing 'created')")
            return {"message": f"Action '{payload.action}' ignored, only 'created' actions are processed"}
        
        # Extract data from Sentry webhook payload
        issue = payload.data.issue
        event = payload.data.event
        project = payload.data.project
        
        # Get project slug/name
        project_name = "unknown"
        if project:
            project_name = project.slug or project.name or "unknown"
        elif issue and issue.project:
            project_name = issue.project.get("slug", "unknown") if isinstance(issue.project, dict) else "unknown"
        
        # Get event_id
        event_id = "unknown"
        if event and event.event_id:
            event_id = event.event_id
        elif issue and issue.id:
            event_id = issue.id
        
        # Validate project if filtering is enabled
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(
                    f"Rejected webhook from project '{project_name}'. "
                    f"Expected project: '{settings.SENTRY_PROJECT}'"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Webhook from project '{project_name}' is not allowed. "
                           f"Expected project: '{settings.SENTRY_PROJECT}'"
                )
        
        logger.info(f"Received webhook action: {payload.action}, project: {project_name}, event_id: {event_id}")
        
        # Check if error with this event_id already exists
        result = await db.execute(
            select(Error).where(Error.event_id == event_id)
        )
        existing_error = result.scalar_one_or_none()
        
        if existing_error:
            logger.warning(f"Error with event_id {event_id} already exists")
            return {"message": "Error already exists", "event_id": event_id}
        
        # Extract message
        message = "No message"
        if event and event.message:
            message = event.message
        elif event and event.title:
            message = event.title
        elif issue and issue.title:
            message = issue.title
        elif issue and issue.culprit:
            message = issue.culprit
        
        # Extract timestamp
        error_timestamp = datetime.now()
        if event and event.timestamp:
            error_timestamp = datetime.fromtimestamp(event.timestamp)
        
        # Extract exception information
        exception_type = None
        exception_value = None
        stacktrace = None
        
        if event and event.exceptions and len(event.exceptions) > 0:
            # Get first exception (usually the most relevant)
            exc = event.exceptions[0]
            exception_type = exc.type
            exception_value = exc.value
        
        # Extract stacktrace - can be in event.stacktrace or in exception.stacktrace
        if event:
            stacktrace_frames = None
            # Try to get stacktrace from event first
            if event.stacktrace and event.stacktrace.frames:
                stacktrace_frames = event.stacktrace.frames
            # Or from first exception if available
            elif event.exceptions and len(event.exceptions) > 0:
                # Note: Sentry exceptions may have stacktrace in mechanism or elsewhere
                # For now, we use event.stacktrace if available
                pass
            
            if stacktrace_frames:
                stacktrace_lines = []
                for frame in reversed(stacktrace_frames):  # Reverse to show call order
                    frame_str = f"  File \"{frame.filename or 'unknown'}\", line {frame.lineno or '?'}, in {frame.function or 'unknown'}"
                    stacktrace_lines.append(frame_str)
                stacktrace = "\n".join(stacktrace_lines)
        
        # Create new error record
        new_error = Error(
            event_id=event_id,
            project=project_name,
            message=message,
            exception_type=exception_type,
            exception_value=exception_value,
            stacktrace=stacktrace,
            timestamp=error_timestamp
        )
        
        db.add(new_error)
        await db.commit()
        await db.refresh(new_error)
        
        logger.info(f"Successfully saved error with event_id {event_id}")
        
        return {
            "message": "Error saved successfully",
            "event_id": event_id,
            "id": new_error.id
        }
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Error processing Sentry webhook: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process webhook: {str(e)}"
        )

