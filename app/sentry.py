"""
Sentry/GlitchTip webhook receiver.

ВАЖНО: Этот сервис НЕ подключается к Sentry!
Он только ПРИНИМАЕТ webhook запросы от Sentry/GlitchTip.

Архитектура:
1. Ваш проект → Sentry SDK → отправляет ошибки в Sentry/GlitchTip
2. Sentry/GlitchTip → отправляет webhook POST запрос на этот сервис
3. Этот сервис → принимает webhook и сохраняет в БД

Настройка:
- В Sentry/GlitchTip настройте webhook URL: http://your-server:8002/sentry/webhook
- Sentry/GlitchTip сам будет отправлять запросы на этот endpoint
- Этот сервис просто слушает и принимает входящие POST запросы
"""
import json
import logging
import re
import hashlib
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Error
from app.schemas import SentryWebhookPayload
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sentry", tags=["sentry"])


async def _process_glitchtip_webhook(payload_dict: dict, db: AsyncSession):
    """
    Process GlitchTip webhook in Slack/Microsoft Teams format.
    
    Format:
    {
        "alias": "GlitchTip",
        "text": "GlitchTip Alert",
        "attachments": [{
            "title": "Error message",
            "title_link": "http://.../issues/4",
            "fields": [
                {"title": "Project", "value": "back"},
                {"title": "Environment", "value": "development"},
                ...
            ]
        }],
        "sections": [{
            "activityTitle": "Error message",
            "activitySubtitle": "[View Issue BACK-4](http://...)"
        }]
    }
    """
    try:
        attachments = payload_dict.get("attachments", [])
        if not attachments:
            logger.warning("GlitchTip webhook has no attachments")
            return
        
        attachment = attachments[0]  # Use first attachment
        
        # Extract message/error title
        message = attachment.get("title") or "No message"
        
        # Extract project information from fields
        project_name = "unknown"
        project_slug = None
        environment = None
        server_name = None
        
        fields = attachment.get("fields", [])
        for field in fields:
            field_title = field.get("title", "").lower()
            field_value = field.get("value", "")
            if field_title == "project":
                project_name = field_value
                project_slug = field_value
            elif field_title == "environment":
                environment = field_value
            elif field_title == "server name":
                server_name = field_value
        
        # Optional project filtering
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(
                    f"Rejected GlitchTip webhook from project '{project_name}'. "
                    f"Expected project: '{settings.SENTRY_PROJECT}'"
                )
                return
        
        # Extract issue information from title_link or sections
        issue_permalink = attachment.get("title_link")
        issue_id = None
        issue_short_id = None
        
        if issue_permalink:
            # Try to extract issue ID from URL (e.g., /issues/4)
            match = re.search(r'/issues/(\d+)', issue_permalink)
            if match:
                issue_id = match.group(1)
        
        # Try to extract short ID from sections
        sections = payload_dict.get("sections", [])
        if sections and len(sections) > 0:
            activity_subtitle = sections[0].get("activitySubtitle", "")
            # Extract short ID like "BACK-4" from "[View Issue BACK-4](...)"
            match = re.search(r'\[View Issue\s+([^\]]+)\]', activity_subtitle)
            if match:
                issue_short_id = match.group(1).strip()
        
        # Extract exception type from message (e.g., "AttributeError: ...")
        exception_type = None
        exception_value = None
        if message and ":" in message:
            parts = message.split(":", 1)
            if len(parts) == 2:
                exception_type = parts[0].strip()
                exception_value = parts[1].strip()
        
        # Generate event_id from issue_id or use hash of message
        if issue_id:
            event_id = f"glitchtip-{issue_id}"
        else:
            event_id = f"glitchtip-{hashlib.md5(message.encode()).hexdigest()[:8]}"
        
        # Check if error with this event_id already exists
        result = await db.execute(
            select(Error).where(Error.event_id == event_id)
        )
        existing_error = result.scalar_one_or_none()
        
        if existing_error:
            logger.warning(f"Error with event_id {event_id} already exists")
            return
        
        # Create new error record
        new_error = Error(
            event_id=event_id,
            project=project_name,
            project_slug=project_slug,
            project_id=None,
            message=message,
            exception_type=exception_type,
            exception_value=exception_value,
            stacktrace=None,  # Not available in GlitchTip format
            timestamp=datetime.now(),
            # Issue fields
            issue_id=issue_id,
            issue_short_id=issue_short_id,
            issue_title=message,
            issue_culprit=None,
            issue_permalink=issue_permalink,
            issue_level=None,
            issue_status=None,
            issue_logger=None,
            # Event fields
            event_platform=None,
            event_logger=None,
            event_level=None,
            # Full payload
            full_payload=json.dumps(payload_dict, indent=2, default=str)
        )
        
        db.add(new_error)
        await db.commit()
        await db.refresh(new_error)
        
        logger.info(f"Successfully saved GlitchTip error with event_id {event_id}, project: {project_name}")
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Error processing GlitchTip webhook: {str(e)}", exc_info=True)
        raise


@router.post("/webhook", status_code=status.HTTP_201_CREATED)
async def sentry_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Handle Sentry/GlitchTip webhook POST request.
    
    Этот endpoint ПРИНИМАЕТ webhook от Sentry/GlitchTip.
    Sentry/GlitchTip сам отправляет POST запросы на этот URL.
    
    Настройка в Sentry/GlitchTip:
    - Settings → Integrations → Webhooks
    - URL: http://your-server:8002/sentry/webhook
    - События: Issue Created
    
    Этот сервис НЕ подключается к Sentry - он только слушает входящие запросы.
    """
    logger.info("=" * 60)
    logger.info("🔔 WEBHOOK ENDPOINT CALLED - /sentry/webhook")
    logger.info("=" * 60)
    logger.info(f"Request method: {request.method}")
    logger.info(f"Request URL: {request.url}")
    logger.info(f"Request path: {request.url.path}")
    logger.info(f"Request client: {request.client.host if request.client else 'unknown'}")
    logger.info(f"Request headers: {dict(request.headers)}")
    
    try:
        # Parse JSON manually to handle validation errors better
        try:
            payload_dict = await request.json()
            logger.info(f"=== Full payload received ===")
            logger.info(f"Payload keys: {list(payload_dict.keys())}")
            logger.info(f"Payload action: {payload_dict.get('action', 'N/A')}")
            # Log full payload structure for debugging (truncated)
            payload_str = json.dumps(payload_dict, indent=2, default=str)
            logger.info(f"Payload structure (first 3000 chars):\n{payload_str[:3000]}")
        except Exception as e:
            logger.error(f"Failed to parse JSON: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON: {str(e)}"
            )
        
        # Check if this is a GlitchTip Slack/Microsoft Teams format webhook
        is_glitchtip_format = (
            "alias" in payload_dict and 
            "attachments" in payload_dict and 
            isinstance(payload_dict.get("attachments"), list) and
            len(payload_dict.get("attachments", [])) > 0
        )
        
        if is_glitchtip_format:
            logger.info("Detected GlitchTip Slack/Microsoft Teams webhook format")
            # Process GlitchTip format webhook
            await _process_glitchtip_webhook(payload_dict, db)
            return {"message": "GlitchTip webhook processed successfully"}
        
        # Validate payload with Pydantic (try flexible validation)
        try:
            payload = SentryWebhookPayload(**payload_dict)
            logger.info(f"Payload validated successfully. Action: {payload.action}")
        except Exception as validation_error:
            from pydantic import ValidationError
            logger.warning(f"Pydantic validation failed, trying flexible parsing...")
            logger.warning(f"Validation error: {str(validation_error)}")
            
            # Try to create payload with minimal required fields
            try:
                # Create a minimal valid payload structure
                flexible_payload_dict = {
                    "action": payload_dict.get("action", "created"),
                    "data": payload_dict.get("data", {}),
                    "installation": payload_dict.get("installation"),
                    "actor": payload_dict.get("actor")
                }
                payload = SentryWebhookPayload(**flexible_payload_dict)
                logger.info(f"Flexible validation succeeded")
            except Exception as e2:
                logger.error(f"Flexible validation also failed: {str(e2)}")
                if isinstance(validation_error, ValidationError):
                    error_messages = []
                    for err in validation_error.errors():
                        error_messages.append(f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}")
                    error_detail = "; ".join(error_messages)
                else:
                    error_detail = str(validation_error)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Validation error: {error_detail}. Full payload logged."
                )
        # Only process "created" actions (new issues)
        action = payload.action or "unknown"
        if action != "created":
            logger.info(f"Ignoring webhook action: {action} (only processing 'created')")
            return {"message": f"Action '{action}' ignored, only 'created' actions are processed"}
        
        # Extract data from Sentry/GlitchTip webhook payload
        # Handle case where data might be None or missing
        if not payload.data:
            # Try to extract directly from payload_dict as fallback
            logger.warning("Payload.data is None, trying to extract from raw payload")
            if "data" in payload_dict:
                data_dict = payload_dict["data"]
                issue = data_dict.get("issue") if isinstance(data_dict, dict) else None
                event = data_dict.get("event") if isinstance(data_dict, dict) else None
                project = data_dict.get("project") if isinstance(data_dict, dict) else None
            else:
                logger.error("Webhook payload missing 'data' field")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Webhook payload missing 'data' field"
                )
        else:
            issue = payload.data.issue
            event = payload.data.event
            project = payload.data.project
        
        # Get project information (handle both Pydantic models and raw dicts)
        project_name = "unknown"
        project_slug = None
        project_id = None
        
        if project:
            if isinstance(project, dict):
                project_name = project.get("name") or project.get("slug") or "unknown"
                project_slug = project.get("slug")
                project_id = project.get("id")
            else:
                project_name = project.name or project.slug or "unknown"
                project_slug = project.slug
                project_id = project.id
        elif issue:
            if isinstance(issue, dict):
                issue_project = issue.get("project")
                if isinstance(issue_project, dict):
                    project_name = issue_project.get("name") or issue_project.get("slug") or "unknown"
                    project_slug = issue_project.get("slug")
                    project_id = issue_project.get("id")
            elif hasattr(issue, 'project') and issue.project:
                if isinstance(issue.project, dict):
                    project_name = issue.project.get("name") or issue.project.get("slug", "unknown")
                    project_slug = issue.project.get("slug")
                    project_id = issue.project.get("id")
        
        # Optional project filtering
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(
                    f"Rejected webhook from project '{project_name}'. "
                    f"Expected project: '{settings.SENTRY_PROJECT}'"
                )
                return {
                    "message": f"Webhook from project '{project_name}' ignored",
                    "expected_project": settings.SENTRY_PROJECT
                }
        
        # Get event_id (handle both Pydantic models and raw dicts)
        event_id = "unknown"
        if event:
            if isinstance(event, dict):
                event_id = event.get("event_id") or event.get("id") or "unknown"
            elif event.event_id:
                event_id = event.event_id
        
        if event_id == "unknown" and issue:
            if isinstance(issue, dict):
                event_id = issue.get("id") or issue.get("event_id") or "unknown"
            elif issue.id:
                event_id = issue.id
        
        logger.info(f"Received webhook action: {payload.action}, project: {project_name}, event_id: {event_id}")
        
        # Check if error with this event_id already exists
        result = await db.execute(
            select(Error).where(Error.event_id == event_id)
        )
        existing_error = result.scalar_one_or_none()
        
        if existing_error:
            logger.warning(f"Error with event_id {event_id} already exists")
            return {"message": "Error already exists", "event_id": event_id}
        
        # Extract message (handle both Pydantic models and raw dicts)
        message = "No message"
        if event:
            if isinstance(event, dict):
                message = event.get("message") or event.get("title") or message
            else:
                message = event.message or event.title or message
        
        if message == "No message" and issue:
            if isinstance(issue, dict):
                message = issue.get("title") or issue.get("culprit") or issue.get("message") or message
            else:
                message = issue.title or issue.culprit or message
        
        # Extract timestamp (handle both Pydantic models and raw dicts)
        error_timestamp = datetime.now()
        if event:
            if isinstance(event, dict):
                ts = event.get("timestamp")
                if ts:
                    try:
                        error_timestamp = datetime.fromtimestamp(float(ts))
                    except (ValueError, TypeError):
                        pass
            elif event.timestamp:
                try:
                    error_timestamp = datetime.fromtimestamp(event.timestamp)
                except (ValueError, TypeError):
                    pass
        
        # Extract exception information (handle both Pydantic models and raw dicts)
        exception_type = None
        exception_value = None
        stacktrace = None
        
        if event:
            exceptions_list = None
            if isinstance(event, dict):
                exceptions_list = event.get("exceptions")
            elif event.exceptions:
                exceptions_list = event.exceptions
            
            if exceptions_list and len(exceptions_list) > 0:
                exc = exceptions_list[0]
                if isinstance(exc, dict):
                    exception_type = exc.get("type")
                    exception_value = exc.get("value")
                else:
                    exception_type = exc.type
                    exception_value = exc.value
        
        # Extract stacktrace (handle both Pydantic models and raw dicts)
        if event:
            stacktrace_frames = None
            
            if isinstance(event, dict):
                # Try to get stacktrace from event dict
                event_stacktrace = event.get("stacktrace")
                if event_stacktrace:
                    if isinstance(event_stacktrace, dict):
                        stacktrace_frames = event_stacktrace.get("frames")
                    elif hasattr(event_stacktrace, "frames"):
                        stacktrace_frames = event_stacktrace.frames
            else:
                # Try to get stacktrace from Pydantic model
                if event.stacktrace and event.stacktrace.frames:
                    stacktrace_frames = event.stacktrace.frames
            
            if stacktrace_frames:
                stacktrace_lines = []
                for frame in reversed(stacktrace_frames):  # Reverse to show call order
                    if isinstance(frame, dict):
                        frame_str = f"  File \"{frame.get('filename', 'unknown')}\", line {frame.get('lineno', '?')}, in {frame.get('function', 'unknown')}"
                    else:
                        frame_str = f"  File \"{frame.filename or 'unknown'}\", line {frame.lineno or '?'}, in {frame.function or 'unknown'}"
                    stacktrace_lines.append(frame_str)
                stacktrace = "\n".join(stacktrace_lines)
        
        # Extract additional issue fields
        issue_id = None
        issue_short_id = None
        issue_title = None
        issue_culprit = None
        issue_permalink = None
        issue_level = None
        issue_status = None
        issue_logger = None
        
        if issue:
            if isinstance(issue, dict):
                issue_id = issue.get("id")
                issue_short_id = issue.get("shortId")
                issue_title = issue.get("title")
                issue_culprit = issue.get("culprit")
                issue_permalink = issue.get("permalink")
                issue_level = issue.get("level")
                issue_status = issue.get("status")
                issue_logger = issue.get("logger")
            else:
                issue_id = issue.id
                issue_short_id = issue.shortId
                issue_title = issue.title
                issue_culprit = issue.culprit
                issue_permalink = issue.permalink
                issue_level = issue.level
                issue_status = issue.status
                issue_logger = issue.logger
        
        # Extract additional event fields
        event_platform = None
        event_logger = None
        event_level = None
        
        if event:
            if isinstance(event, dict):
                event_platform = event.get("platform")
                event_logger = event.get("logger")
                event_level = event.get("level")
            else:
                event_platform = event.platform
                event_logger = event.logger
                event_level = event.level
        
        # Store full payload as JSON for complete trace
        full_payload_json = json.dumps(payload_dict, indent=2, default=str)
        
        # Create new error record with full trace information
        new_error = Error(
            event_id=event_id,
            project=project_name,
            project_slug=project_slug,
            project_id=project_id,
            message=message,
            exception_type=exception_type,
            exception_value=exception_value,
            stacktrace=stacktrace,
            timestamp=error_timestamp,
            # Issue fields
            issue_id=issue_id,
            issue_short_id=issue_short_id,
            issue_title=issue_title,
            issue_culprit=issue_culprit,
            issue_permalink=issue_permalink,
            issue_level=issue_level,
            issue_status=issue_status,
            issue_logger=issue_logger,
            # Event fields
            event_platform=event_platform,
            event_logger=event_logger,
            event_level=event_level,
            # Full payload
            full_payload=full_payload_json
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

