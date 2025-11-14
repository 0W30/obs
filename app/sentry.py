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
        # Validate project if filtering is enabled
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if payload.project != settings.SENTRY_PROJECT:
                logger.warning(
                    f"Rejected webhook from project '{payload.project}'. "
                    f"Expected project: '{settings.SENTRY_PROJECT}'"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Webhook from project '{payload.project}' is not allowed. "
                           f"Expected project: '{settings.SENTRY_PROJECT}'"
                )
        
        logger.info(f"Received webhook from project: {payload.project}, event_id: {payload.event_id}")
        # Check if error with this event_id already exists
        result = await db.execute(
            select(Error).where(Error.event_id == payload.event_id)
        )
        existing_error = result.scalar_one_or_none()
        
        if existing_error:
            logger.warning(f"Error with event_id {payload.event_id} already exists")
            return {"message": "Error already exists", "event_id": payload.event_id}
        
        # Convert timestamp to datetime
        error_timestamp = datetime.fromtimestamp(payload.timestamp)
        
        # Extract exception information
        exception_type = None
        exception_value = None
        stacktrace = None
        
        if payload.exception:
            exception_type = payload.exception.type
            exception_value = payload.exception.value
            stacktrace = payload.exception.stacktrace
        
        # Create new error record
        new_error = Error(
            event_id=payload.event_id,
            project=payload.project,
            message=payload.message,
            exception_type=exception_type,
            exception_value=exception_value,
            stacktrace=stacktrace,
            timestamp=error_timestamp
        )
        
        db.add(new_error)
        await db.commit()
        await db.refresh(new_error)
        
        logger.info(f"Successfully saved error with event_id {payload.event_id}")
        
        return {
            "message": "Error saved successfully",
            "event_id": payload.event_id,
            "id": new_error.id
        }
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Error processing Sentry webhook: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process webhook: {str(e)}"
        )

