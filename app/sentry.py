"""
Sentry webhook receiver.

ВАЖНО: Этот сервис НЕ подключается к Sentry!
Он только ПРИНИМАЕТ webhook запросы от Sentry.

Архитектура:
1. Ваш проект → Sentry SDK → отправляет ошибки в Sentry
2. Sentry → отправляет webhook POST запрос на этот сервис
3. Этот сервис → принимает webhook и сохраняет в БД

Настройка:
- В Sentry настройте webhook URL: http://your-server:8002/sentry/webhook
- Sentry сам будет отправлять запросы на этот endpoint
- Этот сервис просто слушает и принимает входящие POST запросы

Примечание: Для GlitchTip используйте отдельный endpoint /glitchtip/webhook
"""
import json
import logging
import hashlib
import hmac
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
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


def _verify_sentry_webhook_signature(request: Request, body: bytes) -> bool:
    """
    Verify Sentry webhook signature using HMAC-SHA256.
    
    Sentry sends signature in X-Sentry-Signature header.
    Format can be:
    - "sentry_signature=hex_string" (newer format)
    - Just hex string (older format)
    
    Returns True if signature is valid or if secret is not configured.
    Returns False if signature is invalid.
    """
    if not settings.SENTRY_WEBHOOK_SECRET:
        # If secret is not configured, skip verification
        return True
    
    signature_header = request.headers.get("X-Sentry-Signature")
    if not signature_header:
        logger.warning("Missing X-Sentry-Signature header in webhook request")
        return False
    
    # Extract signature value
    # Format can be "sentry_signature=xxx" or just "xxx"
    if "=" in signature_header:
        # Newer format: "sentry_signature=hex_string"
        parts = signature_header.split("=", 1)
        if len(parts) == 2 and parts[0].strip() == "sentry_signature":
            signature = parts[1].strip()
        else:
            logger.warning(f"Invalid signature header format: {signature_header}")
            return False
    else:
        # Older format: just hex string
        signature = signature_header.strip()
    
    try:
        # Compute expected signature
        expected_signature = hmac.new(
            settings.SENTRY_WEBHOOK_SECRET.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()
        
        # Compare signatures using constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(signature, expected_signature):
            logger.warning("Invalid webhook signature - request rejected")
            return False
        
        logger.debug("Webhook signature verified successfully")
        return True
    except Exception as e:
        logger.error(f"Error verifying webhook signature: {str(e)}", exc_info=True)
        return False


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
        
        url = f"{settings.RESOLVE_SERVICE_URL.rstrip('/')}/resolve"
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
    Handle Sentry webhook POST request.
    
    Этот endpoint ПРИНИМАЕТ webhook от Sentry.
    Sentry сам отправляет POST запросы на этот URL.
    
    Стандартный Sentry webhook формат:
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
    
    Обрабатываемые actions:
    - "created" - новая issue создана (первое событие)
    - "triggered" - новое событие для существующей issue (повторение ошибки)
    - Другие actions (resolved, assigned и т.д.) игнорируются
    
    Настройка в Sentry:
    - Settings → Integrations → Webhooks
    - URL: http://your-server:8002/sentry/webhook
    - События: Issue Created, Issue Triggered (или все события)
    
    Этот сервис НЕ подключается к Sentry - он только слушает входящие запросы.
    
    Примечание: Для GlitchTip используйте отдельный endpoint /glitchtip/webhook
    """
    logger.info("🔔 SENTRY WEBHOOK ENDPOINT CALLED - /sentry/webhook")
    
    try:
        # Get request body for signature verification
        body = await request.body()
        
        # Verify webhook signature if secret is configured
        if not _verify_sentry_webhook_signature(request, body):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid webhook signature"
            )
        
        # Parse JSON
        try:
            payload_dict = json.loads(body.decode('utf-8'))
            action = payload_dict.get('action', 'N/A')
            logger.info(f"Sentry payload keys: {list(payload_dict.keys())}, action: {action}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Failed to parse JSON: {str(e)}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {str(e)}")
        
        # Check action early - process "created" and "triggered" actions
        # "created" = new issue created (first occurrence)
        # "triggered" = new event for existing issue (re-occurrence)
        # Other actions like "resolved", "assigned" are ignored
        if action not in ("created", "triggered"):
            logger.info(f"Ignoring webhook action: {action} (only 'created' and 'triggered' actions are processed)")
            return {
                "message": f"Action '{action}' ignored, only 'created' and 'triggered' actions are processed",
                "status": "ignored"
            }
        
        # Log payload structure for debugging
        logger.info(f"Payload structure: action={action}, has_data={bool(payload_dict.get('data'))}")
        if payload_dict.get("data"):
            data_dict = payload_dict["data"]
            logger.info(f"  data.issue: {bool(data_dict.get('issue'))}, data.event: {bool(data_dict.get('event'))}, data.project: {bool(data_dict.get('project'))}")
            if data_dict.get("event"):
                event_dict = data_dict["event"]
                logger.info(f"  event keys: {list(event_dict.keys())[:10]}")  # First 10 keys
                if "tags" in event_dict:
                    tags_value = event_dict["tags"]
                    logger.info(f"  event.tags type: {type(tags_value).__name__}, value preview: {str(tags_value)[:200]}")
        
        # Log full payload structure for debugging (first 2000 chars) - only in debug mode
        payload_preview = json.dumps(payload_dict, indent=2, default=str)
        logger.debug(f"Full payload preview (first 2000 chars):\n{payload_preview[:2000]}")
        
        # Validate payload with Pydantic (standard Sentry format)
        # Use model_validate with mode='json' for more flexible parsing
        try:
            payload = SentryWebhookPayload.model_validate(payload_dict)
        except Exception as validation_error:
            from pydantic import ValidationError
            logger.warning(f"Pydantic validation failed for action '{action}', trying flexible parsing...")
            if isinstance(validation_error, ValidationError):
                error_messages = [f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in validation_error.errors()]
                logger.debug(f"Validation errors: {error_messages}")
            
            # Try to create payload with minimal validation - just extract what we need
            try:
                # Create a minimal valid structure
                flexible_payload_dict = {
                    "action": payload_dict.get("action"),
                    "data": payload_dict.get("data", {}),
                    "installation": payload_dict.get("installation"),
                    "actor": payload_dict.get("actor")
                }
                # Use model_validate with mode='json' and allow extra fields
                payload = SentryWebhookPayload.model_validate(flexible_payload_dict, strict=False)
                logger.info("Flexible parsing succeeded")
            except Exception as flexible_error:
                # Last resort: log full payload and try to process without strict validation
                logger.error(f"Both validation attempts failed. Original: {str(validation_error)}, Flexible: {str(flexible_error)}")
                logger.error(f"Full payload (first 5000 chars):\n{payload_preview[:5000]}")
                
                # Try to extract data directly from dict without Pydantic validation
                # This allows processing even if schema doesn't match exactly
                if "data" in payload_dict and isinstance(payload_dict["data"], dict):
                    logger.warning("Attempting to process payload without strict Pydantic validation...")
                    # We'll process it manually below
                    payload = None
                else:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Validation error: {str(validation_error)}. Full payload logged."
                    )
        
        # Extract data from payload
        # Handle both validated payload and raw dict (when validation failed but we still want to process)
        if payload is None or not payload.data:
            # Fallback to direct dict extraction
            if "data" in payload_dict:
                data_dict = payload_dict["data"]
                issue = data_dict.get("issue") if isinstance(data_dict, dict) else None
                event = data_dict.get("event") if isinstance(data_dict, dict) else None
                project = data_dict.get("project") if isinstance(data_dict, dict) else None
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload missing 'data' field")
        else:
            # Use validated payload
            issue = payload.data.issue
            event = payload.data.event
            project = payload.data.project
            
            # If validated payload has None values, try to get from raw dict
            if issue is None or event is None:
                if "data" in payload_dict:
                    data_dict = payload_dict["data"]
                    if issue is None:
                        issue = data_dict.get("issue")
                    if event is None:
                        event = data_dict.get("event")
                    if project is None:
                        project = data_dict.get("project")
        
        # Extract project information
        project_name = _get_value(project, 'name', 'slug', default='unknown') or _get_value(issue, 'project', 'name', 'slug', default='unknown') or 'unknown'
        project_slug = _get_value(project, 'slug') or _get_value(issue, 'project', 'slug')
        project_id = _get_value(project, 'id') or _get_value(issue, 'project', 'id')
        
        # Optional project filtering
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(f"Rejected webhook from project '{project_name}'")
                return {"message": f"Webhook from project '{project_name}' ignored", "expected_project": settings.SENTRY_PROJECT}
        
        # Get event_id from event (should be unique per event in Sentry)
        # If event_id is same as issue_id, it might be re-occurrence - add timestamp
        event_id = _get_value(event, 'event_id', 'id')
        if not event_id or event_id == _get_value(issue, 'id'):
            # Fallback: use issue_id with timestamp to handle re-occurrences
            issue_id_value = _get_value(issue, 'id', 'event_id', default='unknown')
            if issue_id_value != 'unknown':
                timestamp_str = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
                event_id = f"sentry-{issue_id_value}-{timestamp_str}"
            else:
                event_id = f"unknown-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:-3]}"
        
        action_value = payload.action if payload else action
        logger.info(f"Received webhook: action={action_value}, project={project_name}, event_id={event_id}")
        
        # Check if error already exists (prevent duplicate events within same second)
        result = await db.execute(select(Error).where(Error.event_id == event_id))
        if result.scalar_one_or_none():
            logger.info(f"Error with event_id {event_id} already exists - skipping duplicate")
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
