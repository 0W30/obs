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
import re
import hashlib
import hmac
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


async def _fetch_sentry_project_info(project_id: str, event_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch project information from Sentry API using project ID.
    Extracts organization and project slug from event URL if available.
    
    Returns project info dict with 'name' and 'slug' keys, or None if failed.
    """
    if not settings.SENTRY_API_TOKEN:
        logger.debug("SENTRY_API_TOKEN not configured, skipping project info fetch")
        return None
    
    try:
        # Try to extract organization and project slug from event URL
        org_slug = None
        project_slug = None
        
        if event_url:
            # Try to extract from URL like: https://sentry.io/api/0/projects/{org}/{project}/events/...
            match = re.search(r'/projects/([^/]+)/([^/]+)/', event_url)
            if match:
                org_slug = match.group(1)
                project_slug = match.group(2)
                logger.debug(f"Extracted org={org_slug}, project_slug={project_slug} from event URL")
        
        # If not found in event URL, try web_url
        if not org_slug and event_url:
            # Try to extract from web_url like: https://sentry.io/organizations/{org}/issues/...
            match = re.search(r'/organizations/([^/]+)/', event_url)
            if match:
                org_slug = match.group(1)
        
        # If still no org, use SENTRY_ORG from config
        if not org_slug:
            org_slug = settings.SENTRY_ORG
        
        if not org_slug:
            logger.warning("Cannot fetch project info: organization slug not found (set SENTRY_ORG or provide event URL)")
            return None
        
        # Use project_slug if available, otherwise use project_id
        project_identifier = project_slug or project_id
        
        base_url = settings.SENTRY_BASE_URL.rstrip('/')
        url = f"{base_url}/api/0/projects/{org_slug}/{project_identifier}/"
        headers = {
            "Authorization": f"Bearer {settings.SENTRY_API_TOKEN}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                project_data = response.json()
                logger.info(f"Fetched Sentry project info: name={project_data.get('name')}, slug={project_data.get('slug')}")
                return {
                    "name": project_data.get("name"),
                    "slug": project_data.get("slug"),
                    "id": str(project_data.get("id", project_id))
                }
            else:
                logger.warning(f"Failed to fetch Sentry project info: status={response.status_code}, project_id={project_id}")
                return None
    except Exception as e:
        logger.warning(f"Error fetching Sentry project info: {str(e)}")
        return None


async def _send_to_resolve_service(error: Error) -> bool:
    """
    Send error data to external resolve service.
    Returns True if successful, False otherwise.
    Does not raise exceptions - logs errors but doesn't interrupt main flow.
    
    IMPORTANT: Resolve service requires non-empty stacktrace.
    If stacktrace is empty, request is not sent.
    """
    if not settings.RESOLVE_SERVICE_ENABLED or not settings.RESOLVE_SERVICE_URL:
        return False
    
    try:
        # Prepare payload according to resolve service contract
        # Use stacktrace_detailed if available and not empty, otherwise use stacktrace
        stacktrace = ""
        if error.stacktrace_detailed and error.stacktrace_detailed.strip():
            stacktrace = error.stacktrace_detailed
        elif error.stacktrace and error.stacktrace.strip():
            stacktrace = error.stacktrace
        
        # Resolve service requires non-empty stacktrace
        # If stacktrace is empty, skip sending to avoid 400 error
        if not stacktrace or not stacktrace.strip():
            logger.warning(
                f"Skipping resolve service: empty stacktrace for event_id={error.event_id}, "
                f"stacktrace_detailed={'present' if error.stacktrace_detailed else 'None'}, "
                f"stacktrace={'present' if error.stacktrace else 'None'}"
            )
            return False
        
        # Extract project name - use project_slug (short name like "uni-s") if available, otherwise use project (full name)
        # Resolve service expects project_slug (short identifier), not full project name
        project_name = error.project_slug or "unknown"
        
        # Prepare payload according to resolve service contract
        # stacktrace is required and must be non-empty
        payload = {
            "exception_type": error.exception_type,
            "exception_value": error.exception_value,
            "message": error.message or "",
            "project_name": project_name,
            "send_to_tracker": True,
            "stacktrace": stacktrace,  # Required - must be non-empty
            "tracker_queue": settings.TRACKER_QUEUE
        }
        
        url = f"{settings.RESOLVE_SERVICE_URL.rstrip('/')}/resolve"
        headers = {"Content-Type": "application/json"}
        
        logger.info(
            f"Sending to resolve service: URL={url}, event_id={error.event_id}, "
            f"has_stacktrace={bool(stacktrace)}, stacktrace_length={len(stacktrace) if stacktrace else 0}"
        )
        logger.debug(f"Payload preview: {json.dumps({**payload, 'stacktrace': payload['stacktrace'][:200] + '...' if len(payload['stacktrace']) > 200 else payload['stacktrace']}, indent=2)}")
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code in (200, 201):
                logger.info(f"Successfully sent error to resolve service: event_id={error.event_id}")
                return True
            else:
                # Log response details for debugging
                try:
                    response_text = response.text[:500]  # First 500 chars
                    logger.warning(
                        f"Failed to send error to resolve service: "
                        f"status={response.status_code}, "
                        f"url={url}, "
                        f"event_id={error.event_id}, "
                        f"response={response_text}"
                    )
                except Exception:
                    logger.warning(
                        f"Failed to send error to resolve service: "
                        f"status={response.status_code}, "
                        f"url={url}, "
                        f"event_id={error.event_id}"
                    )
                return False
    except Exception as e:
        logger.warning(f"Error sending to resolve service: {str(e)}, event_id={error.event_id}")
        return False


def _extract_stacktrace_from_event(event: Any) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Extract exception and stacktrace from event for "triggered" action.
    Works with both dict and Pydantic models.
    
    Primary format for "triggered" action:
    - event.exception.values[].stacktrace.frames
    
    Fallback formats:
    - event.stacktrace.frames (event level)
    - entries[].data.values[].stacktrace.frames (alternative format)
    
    Returns: (exception_type, exception_value, stacktrace, stacktrace_files, stacktrace_detailed)
    """
    if not event:
        return None, None, None, None, None
    
    # Convert to dict if needed
    event_dict = event if isinstance(event, dict) else (event.model_dump() if hasattr(event, 'model_dump') else {})
    
    exception_type = None
    exception_value = None
    stacktrace_frames = None
    
    # Primary format: event.exception.values[] (for "triggered" action)
    # This is the format Sentry uses for triggered webhooks
    event_exception = _get_value(event, 'exception')
    if event_exception:
            # Handle both dict and Pydantic models
            if isinstance(event_exception, dict):
                exception_values = event_exception.get('values', [])
            elif hasattr(event_exception, 'values'):
                exception_values = event_exception.values
            else:
                exception_values = _get_value(event_exception, 'values', default=[])
            
            if exception_values and len(exception_values) > 0:
                exc = exception_values[0]
                
                # Extract exception info
                exception_type = _get_value(exc, 'type')
                exception_value = _get_value(exc, 'value')
                
                # Extract stacktrace
                exc_stacktrace = _get_value(exc, 'stacktrace')
                if exc_stacktrace:
                    if isinstance(exc_stacktrace, dict):
                        stacktrace_frames = exc_stacktrace.get('frames')
                    elif hasattr(exc_stacktrace, 'frames'):
                        stacktrace_frames = exc_stacktrace.frames
                    else:
                        stacktrace_frames = _get_value(exc_stacktrace, 'frames')
    
    # If stacktrace not found, try event level
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
    
    # If still not found, try entries format (alternative format)
    if not stacktrace_frames and 'entries' in event_dict:
        entries = event_dict.get('entries', [])
        for entry in entries:
            entry_type = entry.get('type')
            if entry_type == 'exception' and 'data' in entry:
                exc_data = entry['data']
                if 'values' in exc_data and exc_data['values']:
                    exc = exc_data['values'][0]
                    if 'stacktrace' in exc and 'frames' in exc['stacktrace']:
                        stacktrace_frames = exc['stacktrace']['frames']
                        # Also extract exception info if not already found
                        if not exception_type:
                            exception_type = exc.get('type')
                        if not exception_value:
                            exception_value = exc.get('value')
                        break
    
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
    - "triggered" - новое событие для существующей issue (единственный поддерживаемый action)
    - Другие actions игнорируются
    
    Настройка в Sentry:
    - Settings → Integrations → Webhooks
    - URL: http://your-server:8002/sentry/webhook
    - События: Issue Triggered (или Issue Alert Rules)
    
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
        
        # Check action early - only process "triggered" actions
        # This service only handles "triggered" webhooks from Sentry alerts
        if action != "triggered":
            logger.info(f"Ignoring webhook action: {action} (only 'triggered' actions are processed)")
            return {
                "message": f"Action '{action}' ignored, only 'triggered' actions are processed",
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
        # For "triggered" action, project might be inside event instead of data.project
        # Also, for "triggered" action, event.project might be a number (project ID) instead of an object
        event_project = _get_value(event, 'project')
        event_url = _get_value(event, 'url', 'web_url')  # For extracting org/project from URL
        
        # Handle different project formats
        if isinstance(event_project, dict):
            # Project is an object with name/slug
            project_name = _get_value(event_project, 'name', 'slug', default='unknown') or 'unknown'
            project_slug = _get_value(event_project, 'slug')
            project_id = _get_value(event_project, 'id') or str(event_project.get('id', ''))
        elif isinstance(event_project, (int, str)):
            # Project is just an ID (for "triggered" action)
            project_id = str(event_project)
            project_name = 'unknown'  # Will try to get from API or other sources
            project_slug = None
            
            # Try to fetch project info from Sentry API if configured
            if settings.SENTRY_API_TOKEN:
                logger.info(f"Fetching project info from Sentry API for project_id={project_id}")
                project_info = await _fetch_sentry_project_info(project_id, event_url)
                if project_info:
                    project_name = project_info.get('name', 'unknown')
                    project_slug = project_info.get('slug')
                    logger.info(f"Got project name from API: {project_name}")
        else:
            project_name = 'unknown'
            project_slug = None
            project_id = None
        
        # Try to get project name from other sources
        if project_name == 'unknown':
            project_name = (
                _get_value(project, 'name', 'slug', default='unknown') or 
                _get_value(issue, 'project', 'name', 'slug', default='unknown') or
                'unknown'
            )
        
        if not project_slug:
            project_slug = (
                _get_value(project, 'slug') or 
                _get_value(issue, 'project', 'slug') or
                None
            )
        
        if not project_id:
            project_id = (
                _get_value(project, 'id') or 
                _get_value(issue, 'project', 'id') or
                None
            )
        
        # Optional project filtering
        if settings.SENTRY_FILTER_BY_PROJECT and settings.SENTRY_PROJECT:
            if project_name != settings.SENTRY_PROJECT:
                logger.warning(f"Rejected webhook from project '{project_name}'")
                return {"message": f"Webhook from project '{project_name}' ignored", "expected_project": settings.SENTRY_PROJECT}
        
        # Get event_id from event (should be unique per event in Sentry)
        # For "triggered" action, issue might not exist, so get issue_id from event.issue_id
        event_id = _get_value(event, 'event_id', 'id')
        
        # Extract issue_id - for "triggered" action it's in event.issue_id, not data.issue
        issue_id_from_event = _get_value(event, 'issue_id')
        issue_id_from_issue = _get_value(issue, 'id') if issue else None
        issue_id_value = issue_id_from_event or issue_id_from_issue
        
        if not event_id or event_id == issue_id_value:
            # Fallback: use issue_id with timestamp to handle re-occurrences
            if issue_id_value:
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
        
        # Log event structure for debugging if stacktrace is missing
        if not stacktrace and event:
            event_dict = event if isinstance(event, dict) else (event.model_dump() if hasattr(event, 'model_dump') else {})
            logger.warning(f"No stacktrace found in event. Event keys: {list(event_dict.keys())[:20]}")
            logger.debug(f"Event structure (first 1000 chars): {json.dumps(event_dict, indent=2, default=str)[:1000]}")
        
        # Extract breadcrumbs
        breadcrumbs = _extract_breadcrumbs_from_event(event)
        
        # Extract issue fields
        # For "triggered" action, issue might not exist, so get from event
        issue_id = issue_id_value  # Already extracted above
        issue_short_id = _get_value(issue, 'shortId') if issue else None
        issue_title = (
            _get_value(issue, 'title') if issue else 
            _get_value(event, 'title', 'message', default=None)
        )
        issue_culprit = (
            _get_value(issue, 'culprit') if issue else 
            _get_value(event, 'culprit', default=None)
        )
        issue_permalink = (
            _get_value(issue, 'permalink') if issue else 
            _get_value(event, 'web_url', 'url', default=None)
        )
        issue_level = (
            _get_value(issue, 'level') if issue else 
            _get_value(event, 'level', default=None)
        )
        issue_status = _get_value(issue, 'status') if issue else None
        issue_logger = (
            _get_value(issue, 'logger') if issue else 
            _get_value(event, 'logger', default=None)
        )
        
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
