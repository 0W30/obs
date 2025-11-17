"""
API endpoints for retrieving errors.
"""
import json
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.database import get_db
from app.models import Error
from app.schemas import ErrorResponse, ErrorNotFoundResponse

router = APIRouter(prefix="/errors", tags=["errors"])


@router.get("/latest", response_model=ErrorResponse | ErrorNotFoundResponse)
async def get_latest_error(db: AsyncSession = Depends(get_db)):
    """
    Get the latest error.
    Returns ErrorNotFoundResponse if no errors exist.
    """
    try:
        result = await db.execute(
            select(Error).order_by(desc(Error.created_at)).limit(1)
        )
        error = result.scalar_one_or_none()
        
        if error is None:
            return ErrorNotFoundResponse()
        
        # Parse JSON fields before validation
        full_payload_dict = None
        if error.full_payload:
            try:
                full_payload_dict = json.loads(error.full_payload)
            except Exception:
                full_payload_dict = None
        
        breadcrumbs_dict = None
        if error.breadcrumbs:
            try:
                breadcrumbs_dict = json.loads(error.breadcrumbs)
            except Exception:
                breadcrumbs_dict = None
        
        stacktrace_files_list = None
        if error.stacktrace_files:
            try:
                stacktrace_files_list = json.loads(error.stacktrace_files)
            except Exception:
                stacktrace_files_list = None
        
        # Create error dict manually to avoid validation issues
        error_dict = {
            "id": error.id,
            "event_id": error.event_id,
            "project": error.project,
            "project_slug": error.project_slug,
            "project_id": error.project_id,
            "message": error.message,
            "exception_type": error.exception_type,
            "exception_value": error.exception_value,
            "stacktrace": error.stacktrace,
            "timestamp": error.timestamp,
            "created_at": error.created_at,
            "issue_id": error.issue_id,
            "issue_short_id": error.issue_short_id,
            "issue_title": error.issue_title,
            "issue_culprit": error.issue_culprit,
            "issue_permalink": error.issue_permalink,
            "issue_level": error.issue_level,
            "issue_status": error.issue_status,
            "issue_logger": error.issue_logger,
            "event_platform": error.event_platform,
            "event_logger": error.event_logger,
            "event_level": error.event_level,
            "breadcrumbs": breadcrumbs_dict,
            "stacktrace_files": stacktrace_files_list,
            "stacktrace_detailed": error.stacktrace_detailed,
            "full_payload": full_payload_dict,
        }
        
        return ErrorResponse.model_validate(error_dict)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("", response_model=List[ErrorResponse])
async def get_all_errors(db: AsyncSession = Depends(get_db)):
    """
    Get all errors.
    Returns empty list if no errors exist.
    """
    try:
        result = await db.execute(
            select(Error).order_by(desc(Error.created_at))
        )
        errors = result.scalars().all()
        result = []
        for error in errors:
            # Parse JSON fields before validation
            full_payload_dict = None
            if error.full_payload:
                try:
                    full_payload_dict = json.loads(error.full_payload)
                except Exception:
                    full_payload_dict = None
            
            breadcrumbs_dict = None
            if error.breadcrumbs:
                try:
                    breadcrumbs_dict = json.loads(error.breadcrumbs)
                except Exception:
                    breadcrumbs_dict = None
            
            stacktrace_files_list = None
            if error.stacktrace_files:
                try:
                    stacktrace_files_list = json.loads(error.stacktrace_files)
                except Exception:
                    stacktrace_files_list = None
            
            # Create error dict manually to avoid validation issues
            error_dict = {
                "id": error.id,
                "event_id": error.event_id,
                "project": error.project,
                "project_slug": error.project_slug,
                "project_id": error.project_id,
                "message": error.message,
                "exception_type": error.exception_type,
                "exception_value": error.exception_value,
                "stacktrace": error.stacktrace,
                "timestamp": error.timestamp,
                "created_at": error.created_at,
                "issue_id": error.issue_id,
                "issue_short_id": error.issue_short_id,
                "issue_title": error.issue_title,
                "issue_culprit": error.issue_culprit,
                "issue_permalink": error.issue_permalink,
                "issue_level": error.issue_level,
                "issue_status": error.issue_status,
                "issue_logger": error.issue_logger,
                "event_platform": error.event_platform,
                "event_logger": error.event_logger,
                "event_level": error.event_level,
                "breadcrumbs": breadcrumbs_dict,
                "stacktrace_files": stacktrace_files_list,
                "stacktrace_detailed": error.stacktrace_detailed,
                "full_payload": full_payload_dict,
            }
            
            result.append(ErrorResponse.model_validate(error_dict))
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

