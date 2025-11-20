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
from typing import Optional, Dict, Any, List, Tuple
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models import Error
from app.schemas import SentryWebhookPayload
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sentry", tags=["sentry"])


def _get_value(obj: Any, *keys, default: Any = None) -> Any:
    """Helper to get value from dict or Pydantic model."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if value is not None:
                return value
        return default
    else:
        for key in keys:
            value = getattr(obj, key, None)
            if value is not None:
                return value
        return default


def _extract_stacktrace_from_frames(stacktrace_frames: List[Any]) -> Tuple[str, str, str]:
    """
    Extract stacktrace information from frames.
    Accepts both dict and Pydantic model frames.
    Returns: (stacktrace, stacktrace_files_json, stacktrace_detailed)
    """
    stacktrace_lines = []
    files_info = []
    detailed_lines = []
    
    for frame in reversed(stacktrace_frames):
        # Handle both dict and Pydantic models
        if hasattr(frame, 'model_dump'):
            # Pydantic model - convert to dict first
            frame_dict = frame.model_dump() if hasattr(frame, 'model_dump') else {}
            filename = frame_dict.get('filename', 'unknown')
            abs_path = frame_dict.get('abs_path') or filename
            lineno = frame_dict.get('lineno', '?')
            function = frame_dict.get('function', 'unknown')
            context_line = frame_dict.get('context_line')
            pre_context = frame_dict.get('pre_context', [])
            post_context = frame_dict.get('post_context', [])
            vars_dict = frame_dict.get('vars', {})
        else:
            # Dict or other object
            filename = _get_value(frame, 'filename', default='unknown')
            abs_path = _get_value(frame, 'abs_path') or filename
            lineno = _get_value(frame, 'lineno', default='?')
            function = _get_value(frame, 'function', default='unknown')
            context_line = _get_value(frame, 'context_line')
            pre_context = _get_value(frame, 'pre_context', default=[])
            post_context = _get_value(frame, 'post_context', default=[])
            vars_dict = _get_value(frame, 'vars', default={})
        
        frame_str = f"  File \"{filename}\", line {lineno}, in {function}"
        stacktrace_lines.append(frame_str)
        
        file_info = {
            "filename": filename,
            "abs_path": abs_path,
            "line": lineno,
            "function": function,
            "context_line": context_line,
            "pre_context": pre_context,
            "post_context": post_context,
            "vars": vars_dict
        }
        files_info.append(file_info)
        
        detailed_frame = f"File \"{abs_path}\", line {lineno}, in {function}\n"
        if pre_context:
            for pre_line in pre_context:
                detailed_frame += f"  {pre_line}\n"
        if context_line:
            detailed_frame += f"> {context_line}\n"
        if post_context:
            for post_line in post_context:
                detailed_frame += f"  {post_line}\n"
        if vars_dict:
            detailed_frame += f"  Variables: {json.dumps(vars_dict, indent=2, default=str)}\n"
        detailed_lines.append(detailed_frame)
    
    stacktrace = "\n".join(stacktrace_lines)
    stacktrace_files = json.dumps(files_info, indent=2, default=str)
    stacktrace_detailed = "\n".join(detailed_lines)
    
    return stacktrace, stacktrace_files, stacktrace_detailed


async def _send_to_resolve_service(error: Error) -> bool:
    """
    Send error data to external resolve service.
    Returns True if successful, False otherwise.
    Does not raise exceptions - logs errors but doesn't interrupt main flow.
    """
    if not settings.RESOLVE_SERVICE_ENABLED or not settings.RESOLVE_SERVICE_URL:
        return False
    
    try:
        # Prepare payload according to resolve service contract
        stacktrace = error.stacktrace_detailed or error.stacktrace or ""
        
        # Extract project name - use from error, fallback to "unknown"
        project_name = error.project or "unknown"
        
        # Prepare payload according to resolve service contract
        # stacktrace is required, others are optional but should be present
        payload = {
            "exception_type": error.exception_type,
            "exception_value": error.exception_value,
            "message": error.message or "",
            "project_name": project_name,
            "send_to_tracker": True,
            "stacktrace": stacktrace,  # Required - at least empty string
            "tracker_queue": settings.TRACKER_QUEUE
        }
        
        url = f"{settings.RESOLVE_SERVICE_URL.rstrip('/')}/resole"
        headers = {"Content-Type": "application/json"}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code in (200, 201):
                logger.info(f"Successfully sent error to resolve service: event_id={error.event_id}")
                return True
            else:
                logger.warning(f"Failed to send error to resolve service: status={response.status_code}, event_id={error.event_id}")
                return False
    except Exception as e:
        logger.warning(f"Error sending to resolve service: {str(e)}, event_id={error.event_id}")
        return False


async def _fetch_glitchtip_latest_event(issue_id: str, base_url: str, api_token: str) -> Optional[Dict[str, Any]]:
    """Fetch latest event for an issue from GlitchTip API."""
    try:
        base_url = base_url.rstrip('/')
        url = f"{base_url}/api/0/issues/{issue_id}/events/latest/"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                logger.info(f"Fetched GlitchTip latest event for issue {issue_id}")
                return response.json()
            else:
                logger.warning(f"Failed to fetch GlitchTip event for issue {issue_id}: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Error fetching GlitchTip event: {str(e)}")
        return None


def _extract_stacktrace_from_glitchtip_event(event_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract stacktrace from GlitchTip API event data."""
    if "entries" not in event_data:
        return None, None, None
    
    stacktrace_frames = None
    breadcrumbs_data = None
    
    for entry in event_data.get("entries", []):
        entry_type = entry.get("type")
        
        # Extract stacktrace from exception entry
        if entry_type == "exception" and "data" in entry:
            exc_data = entry["data"]
            if "values" in exc_data and exc_data["values"]:
                exc = exc_data["values"][0]
                if "stacktrace" in exc and "frames" in exc["stacktrace"]:
                    stacktrace_frames = exc["stacktrace"]["frames"]
        
        # Extract breadcrumbs
        if entry_type == "breadcrumbs" and "data" in entry:
            breadcrumbs_data = entry["data"].get("values")
    
    if stacktrace_frames:
        stacktrace, stacktrace_files, stacktrace_detailed = _extract_stacktrace_from_frames(stacktrace_frames)
        logger.info(f"Extracted stacktrace from GlitchTip API: {len(stacktrace_frames)} frames")
        return stacktrace, stacktrace_files, stacktrace_detailed
    
    return None, None, None


async def _process_glitchtip_webhook(payload_dict: dict, db: AsyncSession):
    """Process GlitchTip webhook in Slack/Microsoft Teams format."""
    try:
        attachments = payload_dict.get("attachments", [])
        if not attachments:
            logger.warning("GlitchTip webhook has no attachments")
            return
        
        attachment = attachments[0]
        message = attachment.get("title") or "No message"
        
        # Extract project information from fields
        project_name = "unknown"
        project_slug = None
        fields = attachment.get("fields", [])
        for field in fields:
            field_title = field.get("title", "").lower()
            field_value = field.get("value", "")
            if field_title == "project":
                project_name = field_value
                project_slug = field_value
        
        # Optional project filtering
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(f"Rejected GlitchTip webhook from project '{project_name}'")
                return
        
        # Extract issue information
        issue_permalink = attachment.get("title_link")
        issue_id = None
        issue_short_id = None
        
        if issue_permalink:
            match = re.search(r'/issues/(\d+)', issue_permalink)
            if match:
                issue_id = match.group(1)
        
        sections = payload_dict.get("sections", [])
        if sections:
            activity_subtitle = sections[0].get("activitySubtitle", "")
            match = re.search(r'\[View Issue\s+([^\]]+)\]', activity_subtitle)
            if match:
                issue_short_id = match.group(1).strip()
        
        # Extract exception type from message
        exception_type = None
        exception_value = None
        if message and ":" in message:
            parts = message.split(":", 1)
            if len(parts) == 2:
                exception_type = parts[0].strip()
                exception_value = parts[1].strip()
        
        # Generate event_id
        if issue_id:
            event_id = f"glitchtip-{issue_id}"
        else:
            event_id = f"glitchtip-{hashlib.md5(message.encode()).hexdigest()[:8]}"
        
        # Check if error already exists
        result = await db.execute(select(Error).where(Error.event_id == event_id))
        if result.scalar_one_or_none():
            logger.warning(f"Error with event_id {event_id} already exists")
            return
        
        # Try to fetch detailed information from GlitchTip API
        stacktrace = None
        stacktrace_files = None
        stacktrace_detailed = None
        breadcrumbs = None
        additional_event_data = {}
        
        if issue_id and settings.GLITCHTIP_API_TOKEN:
            base_url = settings.GLITCHTIP_BASE_URL
            if not base_url and issue_permalink:
                match = re.search(r'(https?://[^/]+)', issue_permalink)
                if match:
                    base_url = match.group(1)
            
            if base_url:
                event_data = await _fetch_glitchtip_latest_event(issue_id, base_url, settings.GLITCHTIP_API_TOKEN)
                if event_data:
                    additional_event_data = event_data
                    stacktrace, stacktrace_files, stacktrace_detailed = _extract_stacktrace_from_glitchtip_event(event_data)
                    
                    # Extract breadcrumbs
                    for entry in event_data.get("entries", []):
                        if entry.get("type") == "breadcrumbs" and "data" in entry:
                            breadcrumbs_values = entry["data"].get("values")
                            if breadcrumbs_values:
                                breadcrumbs = json.dumps(breadcrumbs_values, indent=2, default=str)
                                logger.info(f"Extracted {len(breadcrumbs_values)} breadcrumbs from GlitchTip API")
        
        # Create new error record
        new_error = Error(
            event_id=event_id,
            project=project_name,
            project_slug=project_slug,
            project_id=None,
            message=message,
            exception_type=exception_type,
            exception_value=exception_value,
            stacktrace=stacktrace,
            timestamp=datetime.now(),
            issue_id=issue_id,
            issue_short_id=issue_short_id,
            issue_title=message,
            issue_culprit=None,
            issue_permalink=issue_permalink,
            issue_level=None,
            issue_status=None,
            issue_logger=None,
            event_platform=None,
            event_logger=None,
            event_level=None,
            breadcrumbs=breadcrumbs,
            stacktrace_files=stacktrace_files,
            stacktrace_detailed=stacktrace_detailed,
            full_payload=json.dumps({**payload_dict, "api_event_data": additional_event_data} if additional_event_data else payload_dict, indent=2, default=str)
        )
        
        db.add(new_error)
        await db.commit()
        await db.refresh(new_error)
        
        logger.info(f"Saved GlitchTip error: event_id={event_id}, project={project_name}")
        
        # Send to resolve service (non-blocking)
        if settings.RESOLVE_SERVICE_ENABLED:
            await _send_to_resolve_service(new_error)
        
    except Exception as e:
        await db.rollback()
        logger.error(f"Error processing GlitchTip webhook: {str(e)}", exc_info=True)
        raise


def _extract_stacktrace_from_event(event: Any) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Extract exception and stacktrace from event.
    Works with both dict and Pydantic models.
    Returns: (exception_type, exception_value, stacktrace, stacktrace_files, stacktrace_detailed)
    """
    if not event:
        return None, None, None, None, None
    
    exceptions_list = _get_value(event, 'exceptions')
    exception_type = None
    exception_value = None
    stacktrace_frames = None
    
    if exceptions_list and len(exceptions_list) > 0:
        exc = exceptions_list[0]
        
        # Handle both dict and Pydantic models
        exception_type = _get_value(exc, 'type')
        exception_value = _get_value(exc, 'value')
        
        # Stacktrace is usually inside exception
        exc_stacktrace = _get_value(exc, 'stacktrace')
        if exc_stacktrace:
            # Handle Pydantic model - convert to dict if needed
            if hasattr(exc_stacktrace, 'frames'):
                stacktrace_frames = exc_stacktrace.frames
            elif isinstance(exc_stacktrace, dict):
                stacktrace_frames = exc_stacktrace.get('frames')
            else:
                stacktrace_frames = _get_value(exc_stacktrace, 'frames')
    
    # If stacktrace not found in exception, try event level
    if not stacktrace_frames:
        event_stacktrace = _get_value(event, 'stacktrace')
        if event_stacktrace:
            # Handle Pydantic model - convert to dict if needed
            if hasattr(event_stacktrace, 'frames'):
                stacktrace_frames = event_stacktrace.frames
            elif isinstance(event_stacktrace, dict):
                stacktrace_frames = event_stacktrace.get('frames')
            else:
                stacktrace_frames = _get_value(event_stacktrace, 'frames')
    
    if stacktrace_frames:
        stacktrace, stacktrace_files, stacktrace_detailed = _extract_stacktrace_from_frames(stacktrace_frames)
        logger.info(f"Extracted stacktrace: {len(stacktrace_frames)} frames")
        return exception_type, exception_value, stacktrace, stacktrace_files, stacktrace_detailed
    
    return exception_type, exception_value, None, None, None


def _extract_breadcrumbs_from_event(event: Any) -> Optional[str]:
    """
    Extract breadcrumbs from event.
    Works with both dict and Pydantic models.
    Breadcrumbs can be:
    - A list directly
    - A dict with "values" key containing the list
    - A Pydantic model with "values" attribute
    """
    if not event:
        return None
    
    event_breadcrumbs = _get_value(event, 'breadcrumbs')
    if not event_breadcrumbs:
        return None
    
    # Handle Pydantic model
    if hasattr(event_breadcrumbs, 'model_dump'):
        breadcrumbs_dict = event_breadcrumbs.model_dump()
        if "values" in breadcrumbs_dict:
            return json.dumps(breadcrumbs_dict["values"], indent=2, default=str)
        return json.dumps(breadcrumbs_dict, indent=2, default=str)
    
    # Handle dict
    if isinstance(event_breadcrumbs, list):
        return json.dumps(event_breadcrumbs, indent=2, default=str)
    elif isinstance(event_breadcrumbs, dict):
        if "values" in event_breadcrumbs:
            return json.dumps(event_breadcrumbs["values"], indent=2, default=str)
        return json.dumps(event_breadcrumbs, indent=2, default=str)
    
    return None


@router.post("/webhook", status_code=status.HTTP_201_CREATED)
async def sentry_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle Sentry/GlitchTip webhook POST request.
    
    Этот endpoint ПРИНИМАЕТ webhook от Sentry/GlitchTip.
    Sentry/GlitchTip сам отправляет POST запросы на этот URL.
    
    Поддерживаемые форматы:
    1. GlitchTip Slack/Microsoft Teams формат (упрощенный)
       - Определяется по полям: alias, attachments
       - Если настроен GLITCHTIP_API_TOKEN, делает запрос к API для получения детальной информации
    
    2. Стандартный Sentry webhook формат (полный)
       - Структура: {action, data: {issue, event, project}}
       - Извлекает из event:
         * exceptions[].type, exceptions[].value
         * exceptions[].stacktrace.frames (или event.stacktrace.frames)
         * event.breadcrumbs (или breadcrumbs.values)
         * Все детали о файлах, контексте кода, переменных
    
    Извлекаемые данные:
    - Stacktrace с файлами, строками, функциями
    - Детальная информация о файлах (context_line, pre_context, post_context, vars)
    - Breadcrumbs (хлебные крошки - события перед ошибкой)
    - Exception type и value
    - Вся информация о проекте, issue, event
    
    Настройка в Sentry/GlitchTip:
    - Settings → Integrations → Webhooks
    - URL: http://your-server:8002/sentry/webhook
    - События: Issue Created
    
    Этот сервис НЕ подключается к Sentry - он только слушает входящие запросы.
    """
    logger.info("🔔 WEBHOOK ENDPOINT CALLED - /sentry/webhook")
    
    try:
        # Parse JSON
        try:
            payload_dict = await request.json()
            logger.info(f"Payload keys: {list(payload_dict.keys())}, action: {payload_dict.get('action', 'N/A')}")
        except Exception as e:
            logger.error(f"Failed to parse JSON: {str(e)}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {str(e)}")
        
        # Check if this is a GlitchTip Slack/Microsoft Teams format webhook
        is_glitchtip_format = (
            "alias" in payload_dict and 
            "attachments" in payload_dict and 
            isinstance(payload_dict.get("attachments"), list) and
            len(payload_dict.get("attachments", [])) > 0
        )
        
        if is_glitchtip_format:
            logger.info("Detected GlitchTip Slack/Microsoft Teams webhook format")
            try:
                await _process_glitchtip_webhook(payload_dict, db)
                return {"message": "GlitchTip webhook processed successfully", "status": "ok"}
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error processing GlitchTip webhook: {str(e)}", exc_info=True)
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to process GlitchTip webhook: {str(e)}")
        
        # Validate payload with Pydantic (standard Sentry format)
        try:
            payload = SentryWebhookPayload(**payload_dict)
        except Exception as validation_error:
            from pydantic import ValidationError
            logger.warning(f"Pydantic validation failed, trying flexible parsing...")
            try:
                flexible_payload_dict = {
                    "action": payload_dict.get("action", "created"),
                    "data": payload_dict.get("data", {}),
                    "installation": payload_dict.get("installation"),
                    "actor": payload_dict.get("actor")
                }
                payload = SentryWebhookPayload(**flexible_payload_dict)
            except Exception:
                if isinstance(validation_error, ValidationError):
                    error_messages = [f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in validation_error.errors()]
                    error_detail = "; ".join(error_messages)
                else:
                    error_detail = str(validation_error)
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Validation error: {error_detail}. Full payload logged.")
        
        # Only process "created" actions
        action = payload.action or "unknown"
        if action != "created":
            logger.info(f"Ignoring webhook action: {action}")
            return {"message": f"Action '{action}' ignored, only 'created' actions are processed"}
        
        # Extract data from payload
        if not payload.data:
            if "data" in payload_dict:
                data_dict = payload_dict["data"]
                issue = data_dict.get("issue") if isinstance(data_dict, dict) else None
                event = data_dict.get("event") if isinstance(data_dict, dict) else None
                project = data_dict.get("project") if isinstance(data_dict, dict) else None
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload missing 'data' field")
        else:
            issue = payload.data.issue
            event = payload.data.event
            project = payload.data.project
        
        # Extract project information
        project_name = _get_value(project, 'name', 'slug', default='unknown') or _get_value(issue, 'project', 'name', 'slug', default='unknown') or 'unknown'
        project_slug = _get_value(project, 'slug') or _get_value(issue, 'project', 'slug')
        project_id = _get_value(project, 'id') or _get_value(issue, 'project', 'id')
        
        # Optional project filtering
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(f"Rejected webhook from project '{project_name}'")
                return {"message": f"Webhook from project '{project_name}' ignored", "expected_project": settings.SENTRY_PROJECT}
        
        # Get event_id
        event_id = _get_value(event, 'event_id', 'id') or _get_value(issue, 'id', 'event_id', default='unknown')
        
        logger.info(f"Received webhook: action={payload.action}, project={project_name}, event_id={event_id}")
        
        # Check if error already exists
        result = await db.execute(select(Error).where(Error.event_id == event_id))
        if result.scalar_one_or_none():
            logger.warning(f"Error with event_id {event_id} already exists")
            return {"message": "Error already exists", "event_id": event_id}
        
        # Extract message
        message = _get_value(event, 'message', 'title') or _get_value(issue, 'title', 'culprit', 'message', default='No message')
        
        # Extract timestamp
        error_timestamp = datetime.now()
        ts = _get_value(event, 'timestamp')
        if ts:
            try:
                error_timestamp = datetime.fromtimestamp(float(ts))
            except (ValueError, TypeError):
                pass
        
        # Extract exception and stacktrace
        exception_type, exception_value, stacktrace, stacktrace_files, stacktrace_detailed = _extract_stacktrace_from_event(event)
        
        # Extract breadcrumbs
        breadcrumbs = _extract_breadcrumbs_from_event(event)
        
        # Extract issue fields
        issue_id = _get_value(issue, 'id')
        issue_short_id = _get_value(issue, 'shortId')
        issue_title = _get_value(issue, 'title')
        issue_culprit = _get_value(issue, 'culprit')
        issue_permalink = _get_value(issue, 'permalink')
        issue_level = _get_value(issue, 'level')
        issue_status = _get_value(issue, 'status')
        issue_logger = _get_value(issue, 'logger')
        
        # Extract event fields
        event_platform = _get_value(event, 'platform')
        event_logger = _get_value(event, 'logger')
        event_level = _get_value(event, 'level')
        
        # Create new error record
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
            issue_id=issue_id,
            issue_short_id=issue_short_id,
            issue_title=issue_title,
            issue_culprit=issue_culprit,
            issue_permalink=issue_permalink,
            issue_level=issue_level,
            issue_status=issue_status,
            issue_logger=issue_logger,
            event_platform=event_platform,
            event_logger=event_logger,
            event_level=event_level,
            breadcrumbs=breadcrumbs,
            stacktrace_files=stacktrace_files,
            stacktrace_detailed=stacktrace_detailed,
            full_payload=json.dumps(payload_dict, indent=2, default=str)
        )
        
        db.add(new_error)
        await db.commit()
        await db.refresh(new_error)
        
        logger.info(f"Saved error: event_id={event_id}, stacktrace={'Yes' if stacktrace else 'No'}, breadcrumbs={'Yes' if breadcrumbs else 'No'}")
        
        # Send to resolve service (non-blocking)
        if settings.RESOLVE_SERVICE_ENABLED:
            await _send_to_resolve_service(new_error)
        
        return {"message": "Error saved successfully", "event_id": event_id, "id": new_error.id}
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error processing Sentry webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to process webhook: {str(e)}")
