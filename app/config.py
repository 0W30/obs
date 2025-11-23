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
    SENTRY_WEBHOOK_SECRET: Optional[str] = os.getenv("SENTRY_WEBHOOK_SECRET", None)  # Secret for webhook signature verification
    SENTRY_API_TOKEN: Optional[str] = os.getenv("SENTRY_API_TOKEN", None)  # API token for fetching project info
    SENTRY_ORG: Optional[str] = os.getenv("SENTRY_ORG", None)  # Sentry organization slug
    SENTRY_BASE_URL: Optional[str] = os.getenv("SENTRY_BASE_URL", "https://sentry.io")  # Sentry instance URL
    
    # GlitchTip API configuration (optional, for fetching detailed issue info)
    GLITCHTIP_API_TOKEN: Optional[str] = os.getenv("GLITCHTIP_API_TOKEN", None)
    GLITCHTIP_BASE_URL: Optional[str] = os.getenv("GLITCHTIP_BASE_URL", None)  # e.g., http://glitchtip.example.com
    
    # External service configuration (for sending resolved errors)
    RESOLVE_SERVICE_URL: Optional[str] = os.getenv("RESOLVE_SERVICE_URL", None)  # e.g., http://resolve-service:8000
    RESOLVE_SERVICE_ENABLED: bool = os.getenv("RESOLVE_SERVICE_ENABLED", "false").lower() == "true"
    TRACKER_QUEUE: str = os.getenv("TRACKER_QUEUE", "TEST")  # Queue name for tracker
    
    # API
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))


settings = Settings()

