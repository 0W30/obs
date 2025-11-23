"""
GlitchTip webhook receiver.

GlitchTip использует Slack/Microsoft Teams совместимый формат webhook,
отличающийся от стандартного Sentry формата.

Настройка:
- В GlitchTip настройте webhook URL: http://your-server:8002/glitchtip/webhook
- GlitchTip будет отправлять POST запросы на этот endpoint
"""
import json
import logging
import re
import hashlib
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
import httpx
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Error
from app.config import settings

# Import shared utilities from sentry module
from app.sentry import (
    _get_value,
    _send_to_resolve_service,
    _extract_stacktrace_from_frames
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/glitchtip", tags=["glitchtip"])


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
        
        # Generate unique event_id with timestamp to handle re-occurrences
        # Each webhook call creates a new event, even for the same issue
        # This allows tracking error re-occurrences even after resolution
        timestamp_str = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]  # milliseconds precision
        if issue_id:
            event_id = f"glitchtip-{issue_id}-{timestamp_str}"
        else:
            message_hash = hashlib.md5(message.encode()).hexdigest()[:8]
            event_id = f"glitchtip-{message_hash}-{timestamp_str}"
        
        # Note: We don't check for duplicates anymore - each webhook call is a new event
        
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


@router.post("/webhook", status_code=status.HTTP_201_CREATED)
async def glitchtip_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Handle GlitchTip webhook POST request.
    
    GlitchTip использует Slack/Microsoft Teams совместимый формат webhook,
    который отличается от стандартного Sentry формата.
    
    Формат:
    - Определяется по полям: alias, attachments
    - Если настроен GLITCHTIP_API_TOKEN, делает запрос к API для получения детальной информации
    
    Настройка в GlitchTip:
    - Settings → Integrations → Webhooks
    - URL: http://your-server:8002/glitchtip/webhook
    - События: Issue Created
    """
    logger.info("🔔 GLITCHTIP WEBHOOK ENDPOINT CALLED - /glitchtip/webhook")
    
    try:
        # Get request body
        body = await request.body()
        
        # Parse JSON
        try:
            payload_dict = json.loads(body.decode('utf-8'))
            logger.info(f"GlitchTip payload keys: {list(payload_dict.keys())}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.error(f"Failed to parse JSON: {str(e)}", exc_info=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON: {str(e)}")
        
        # Verify this is GlitchTip format
        is_glitchtip_format = (
            "alias" in payload_dict and 
            "attachments" in payload_dict and 
            isinstance(payload_dict.get("attachments"), list) and
            len(payload_dict.get("attachments", [])) > 0
        )
        
        if not is_glitchtip_format:
            logger.warning("Received webhook that doesn't match GlitchTip format")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook payload doesn't match GlitchTip Slack/Microsoft Teams format"
            )
        
        logger.info("Detected GlitchTip Slack/Microsoft Teams webhook format")
        
        # Process webhook
        await _process_glitchtip_webhook(payload_dict, db)
        
        return {"message": "GlitchTip webhook processed successfully", "status": "ok"}
        
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Error processing GlitchTip webhook: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to process GlitchTip webhook: {str(e)}"
        )

