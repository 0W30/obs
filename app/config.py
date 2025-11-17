"""
Application configuration.
"""
import os
from typing import Optional


class Settings:
    """Application settings."""
    
    # Database
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/errors.db")
    
    # Sentry/GlitchTip configuration
    SENTRY_DSN: Optional[str] = os.getenv("SENTRY_DSN", None)
    SENTRY_PROJECT: Optional[str] = os.getenv("SENTRY_PROJECT", None)  # Optional project filter
    SENTRY_FILTER_BY_PROJECT: bool = os.getenv("SENTRY_FILTER_BY_PROJECT", "false").lower() == "true"
    
    # GlitchTip API configuration (optional, for fetching detailed issue info)
    GLITCHTIP_API_TOKEN: Optional[str] = os.getenv("GLITCHTIP_API_TOKEN", None)
    GLITCHTIP_BASE_URL: Optional[str] = os.getenv("GLITCHTIP_BASE_URL", None)  # e.g., http://glitchtip.example.com
    
    # API
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))


settings = Settings()

