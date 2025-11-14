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
        
        error_dict = ErrorResponse.model_validate(error).model_dump()
        # Parse full_payload JSON if exists
        if error.full_payload:
            try:
                error_dict["full_payload"] = json.loads(error.full_payload)
            except Exception:
                error_dict["full_payload"] = None
        return error_dict
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
            error_dict = ErrorResponse.model_validate(error).model_dump()
            # Parse full_payload JSON if exists
            if error.full_payload:
                try:
                    error_dict["full_payload"] = json.loads(error.full_payload)
                except Exception:
                    error_dict["full_payload"] = None
            result.append(error_dict)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

