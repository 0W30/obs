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
    SENTRY_DSN: Optional[str] = os.getenv("SENTRY_DSN", None)
    
    # API
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))


settings = Settings()

