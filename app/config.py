"""
Application configuration.
"""
import os
from typing import Optional


class Settings:
    """Application settings."""
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/errors.db")
    
    # Sentry configuration
    SENTRY_PROJECT: Optional[str] = os.getenv("SENTRY_PROJECT", None)
    SENTRY_ORGANIZATION: Optional[str] = os.getenv("SENTRY_ORGANIZATION", None)
    SENTRY_DSN: Optional[str] = os.getenv("SENTRY_DSN", None)
    # If True, only accept webhooks from the specified project
    SENTRY_FILTER_BY_PROJECT: bool = os.getenv("SENTRY_FILTER_BY_PROJECT", "false").lower() == "true"
    
    # API
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))


settings = Settings()

