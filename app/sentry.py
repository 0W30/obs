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
from typing import Optional, Dict, Any
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


async def _fetch_glitchtip_issue_details(issue_id: str, base_url: str, api_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch detailed issue information from GlitchTip API.
    
    Returns None if API is not configured or request fails.
    """
    if not api_token:
        logger.info("GlitchTip API token not configured, skipping detailed fetch")
        return None
    
    try:
        # Extract base URL from issue permalink if not provided
        if not base_url and issue_id:
            # We'll need to extract it from the webhook URL
            pass
        
        if not base_url:
            logger.warning("GlitchTip base URL not configured")
            return None
        
        # Remove trailing slash
        base_url = base_url.rstrip('/')
        
        # GlitchTip API endpoint for issue details
        url = f"{base_url}/api/0/issues/{issue_id}/"
        
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                issue_data = response.json()
                logger.info(f"Successfully fetched GlitchTip issue {issue_id} details")
                return issue_data
            else:
                logger.warning(f"Failed to fetch GlitchTip issue {issue_id}: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Error fetching GlitchTip issue details: {str(e)}")
        return None


async def _fetch_glitchtip_latest_event(issue_id: str, base_url: str, api_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch latest event for an issue from GlitchTip API.
    This contains stacktrace, breadcrumbs, etc.
    """
    if not api_token:
        return None
    
    try:
        base_url = base_url.rstrip('/')
        
        # Get latest event for the issue
        url = f"{base_url}/api/0/issues/{issue_id}/events/latest/"
        
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                event_data = response.json()
                logger.info(f"Successfully fetched GlitchTip latest event for issue {issue_id}")
                return event_data
            else:
                logger.warning(f"Failed to fetch GlitchTip latest event for issue {issue_id}: {response.status_code}")
                return None
    except Exception as e:
        logger.warning(f"Error fetching GlitchTip latest event: {str(e)}")
        return None


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
        
        # Try to fetch detailed information from GlitchTip API if configured
        stacktrace = None
        stacktrace_files = None
        stacktrace_detailed = None
        breadcrumbs = None
        additional_event_data = {}
        
        if issue_id and settings.GLITCHTIP_API_TOKEN:
            # Extract base URL from issue_permalink if GLITCHTIP_BASE_URL not set
            base_url = settings.GLITCHTIP_BASE_URL
            if not base_url and issue_permalink:
                # Extract base URL from permalink (e.g., http://glitchtip.example.com/uvi/issues/3)
                match = re.search(r'(https?://[^/]+)', issue_permalink)
                if match:
                    base_url = match.group(1)
                    logger.info(f"Extracted GlitchTip base URL from permalink: {base_url}")
            
            if base_url:
                # Fetch latest event which contains stacktrace and breadcrumbs
                event_data = await _fetch_glitchtip_latest_event(issue_id, base_url, settings.GLITCHTIP_API_TOKEN)
                if event_data:
                    additional_event_data = event_data
                    # Extract stacktrace from event
                    if "entries" in event_data:
                        for entry in event_data.get("entries", []):
                            if entry.get("type") == "exception" and "data" in entry:
                                exc_data = entry["data"]
                                if "values" in exc_data and len(exc_data["values"]) > 0:
                                    exc = exc_data["values"][0]
                                    if "stacktrace" in exc:
                                        stacktrace_info = exc["stacktrace"]
                                        if "frames" in stacktrace_info:
                                            stacktrace_frames = stacktrace_info["frames"]
                                            # Process frames similar to standard Sentry webhook
                                            stacktrace_lines = []
                                            files_info = []
                                            detailed_lines = []
                                            
                                            for frame in reversed(stacktrace_frames):
                                                filename = frame.get('filename', 'unknown')
                                                abs_path = frame.get('abs_path') or filename
                                                lineno = frame.get('lineno', '?')
                                                function = frame.get('function', 'unknown')
                                                context_line = frame.get('context_line')
                                                pre_context = frame.get('pre_context', [])
                                                post_context = frame.get('post_context', [])
                                                vars_dict = frame.get('vars', {})
                                                
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
                                            logger.info(f"Extracted stacktrace from GlitchTip API: {len(stacktrace_lines)} frames")
                            
                            # Extract breadcrumbs
                            if entry.get("type") == "breadcrumbs" and "data" in entry:
                                breadcrumbs_data = entry["data"]
                                if "values" in breadcrumbs_data:
                                    breadcrumbs = json.dumps(breadcrumbs_data["values"], indent=2, default=str)
                                    logger.info(f"Extracted {len(breadcrumbs_data['values'])} breadcrumbs from GlitchTip API")
        
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
            # Breadcrumbs and detailed stacktrace (from API if available)
            breadcrumbs=breadcrumbs,
            stacktrace_files=stacktrace_files,
            stacktrace_detailed=stacktrace_detailed,
            # Full payload (include API data if fetched)
            full_payload=json.dumps({**payload_dict, "api_event_data": additional_event_data} if additional_event_data else payload_dict, indent=2, default=str)
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
        
        # Extract exception information and stacktrace (handle both Pydantic models and raw dicts)
        exception_type = None
        exception_value = None
        stacktrace = None
        stacktrace_files = None
        stacktrace_detailed = None
        stacktrace_frames = None
        
        if event:
            exceptions_list = None
            if isinstance(event, dict):
                exceptions_list = event.get("exceptions")
                logger.info(f"Event dict keys: {list(event.keys()) if isinstance(event, dict) else 'N/A'}")
                if exceptions_list:
                    logger.info(f"Found {len(exceptions_list)} exceptions in event")
            elif hasattr(event, 'exceptions') and event.exceptions:
                exceptions_list = event.exceptions
                logger.info(f"Found {len(exceptions_list)} exceptions in event (Pydantic)")
            
            if exceptions_list and len(exceptions_list) > 0:
                exc = exceptions_list[0]
                if isinstance(exc, dict):
                    exception_type = exc.get("type")
                    exception_value = exc.get("value")
                    logger.info(f"Exception type: {exception_type}, value: {exception_value}")
                    # Stacktrace is usually inside exception, not in event
                    exc_stacktrace = exc.get("stacktrace")
                    logger.info(f"Exception stacktrace type: {type(exc_stacktrace)}, keys: {list(exc_stacktrace.keys()) if isinstance(exc_stacktrace, dict) else 'N/A'}")
                    if exc_stacktrace:
                        if isinstance(exc_stacktrace, dict):
                            stacktrace_frames = exc_stacktrace.get("frames")
                            logger.info(f"Found {len(stacktrace_frames) if stacktrace_frames else 0} frames in exception stacktrace")
                        else:
                            stacktrace_frames = None
                    else:
                        stacktrace_frames = None
                        logger.warning("No stacktrace found in exception")
                else:
                    exception_type = exc.type
                    exception_value = exc.value
                    # Try to get stacktrace from exception
                    if hasattr(exc, 'stacktrace') and exc.stacktrace:
                        if hasattr(exc.stacktrace, 'frames'):
                            stacktrace_frames = exc.stacktrace.frames
                        else:
                            stacktrace_frames = None
                    else:
                        stacktrace_frames = None
            
            # If stacktrace not found in exceptions, try event level
            if not stacktrace_frames:
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
                    if hasattr(event, 'stacktrace') and event.stacktrace:
                        if hasattr(event.stacktrace, 'frames'):
                            stacktrace_frames = event.stacktrace.frames
        
        # Process stacktrace frames if found
        if stacktrace_frames:
            stacktrace_lines = []
            files_info = []
            detailed_lines = []
            
            for frame in reversed(stacktrace_frames):  # Reverse to show call order
                if isinstance(frame, dict):
                    filename = frame.get('filename', 'unknown')
                    abs_path = frame.get('abs_path') or filename
                    lineno = frame.get('lineno', '?')
                    function = frame.get('function', 'unknown')
                    context_line = frame.get('context_line')
                    pre_context = frame.get('pre_context', [])
                    post_context = frame.get('post_context', [])
                    vars_dict = frame.get('vars', {})
                else:
                    filename = frame.filename or 'unknown'
                    abs_path = getattr(frame, 'abs_path', None) or filename
                    lineno = frame.lineno or '?'
                    function = frame.function or 'unknown'
                    context_line = getattr(frame, 'context_line', None)
                    pre_context = getattr(frame, 'pre_context', []) or []
                    post_context = getattr(frame, 'post_context', []) or []
                    vars_dict = getattr(frame, 'vars', {}) or {}
                
                # Simple stacktrace line
                frame_str = f"  File \"{filename}\", line {lineno}, in {function}"
                stacktrace_lines.append(frame_str)
                
                # Detailed file information
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
                
                # Detailed stacktrace with context
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
            logger.info(f"Extracted stacktrace with {len(stacktrace_lines)} frames, {len(files_info)} files")
        
        # Extract breadcrumbs (handle both Pydantic models and raw dicts)
        breadcrumbs = None
        if event:
            if isinstance(event, dict):
                event_breadcrumbs = event.get("breadcrumbs")
                if event_breadcrumbs:
                    logger.info(f"Breadcrumbs in event dict: {type(event_breadcrumbs)}, keys: {list(event_breadcrumbs.keys()) if isinstance(event_breadcrumbs, dict) else 'N/A'}")
                    if isinstance(event_breadcrumbs, list):
                        breadcrumbs = json.dumps(event_breadcrumbs, indent=2, default=str)
                        logger.info(f"Saved {len(event_breadcrumbs)} breadcrumbs (list)")
                    elif isinstance(event_breadcrumbs, dict) and "values" in event_breadcrumbs:
                        breadcrumbs = json.dumps(event_breadcrumbs.get("values", []), indent=2, default=str)
                        logger.info(f"Saved {len(event_breadcrumbs.get('values', []))} breadcrumbs (dict with values)")
                    else:
                        logger.warning(f"Breadcrumbs format not recognized: {type(event_breadcrumbs)}")
                else:
                    logger.info("No breadcrumbs found in event")
            else:
                # Try to get breadcrumbs from Pydantic model
                if hasattr(event, 'breadcrumbs') and event.breadcrumbs:
                    if isinstance(event.breadcrumbs, list):
                        breadcrumbs = json.dumps([b.model_dump() if hasattr(b, 'model_dump') else b for b in event.breadcrumbs], indent=2, default=str)
                        logger.info(f"Saved {len(event.breadcrumbs)} breadcrumbs (Pydantic list)")
                    elif isinstance(event.breadcrumbs, dict) and "values" in event.breadcrumbs:
                        breadcrumbs = json.dumps(event.breadcrumbs.get("values", []), indent=2, default=str)
                        logger.info(f"Saved {len(event.breadcrumbs.get('values', []))} breadcrumbs (Pydantic dict)")
                else:
                    logger.info("No breadcrumbs found in event (Pydantic)")
        
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
            # Breadcrumbs and detailed stacktrace
            breadcrumbs=breadcrumbs,
            stacktrace_files=stacktrace_files,
            stacktrace_detailed=stacktrace_detailed,
            # Full payload
            full_payload=full_payload_json
        )
        
        db.add(new_error)
        await db.commit()
        await db.refresh(new_error)
        
        logger.info(f"Successfully saved error with event_id {event_id}")
        logger.info(f"  - Stacktrace: {'Yes' if stacktrace else 'No'}")
        logger.info(f"  - Stacktrace files: {'Yes' if stacktrace_files else 'No'}")
        logger.info(f"  - Breadcrumbs: {'Yes' if breadcrumbs else 'No'}")
        
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

