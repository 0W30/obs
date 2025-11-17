"""
SQLAlchemy models for the database.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from app.database import Base


class Error(Base):
    """
    Model for storing Sentry/GlitchTip errors with full trace information.
    """
    __tablename__ = "errors"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, unique=True, index=True, nullable=False)
    project = Column(String, nullable=False)
    project_slug = Column(String, nullable=True)
    project_id = Column(String, nullable=True)
    message = Column(Text, nullable=False)
    exception_type = Column(String, nullable=True)
    exception_value = Column(Text, nullable=True)
    stacktrace = Column(Text, nullable=True)
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    
    # Additional fields from issue
    issue_id = Column(String, nullable=True, index=True)
    issue_short_id = Column(String, nullable=True)
    issue_title = Column(Text, nullable=True)
    issue_culprit = Column(Text, nullable=True)
    issue_permalink = Column(Text, nullable=True)
    issue_level = Column(String, nullable=True)
    issue_status = Column(String, nullable=True)
    issue_logger = Column(String, nullable=True)
    
    # Additional fields from event
    event_platform = Column(String, nullable=True)
    event_logger = Column(String, nullable=True)
    event_level = Column(String, nullable=True)
    
    # Breadcrumbs - хлебные крошки (события перед ошибкой)
    breadcrumbs = Column(Text, nullable=True)  # JSON array of breadcrumbs
    
    # Детальная информация о файлах в стектрейсе
    stacktrace_files = Column(Text, nullable=True)  # JSON array with detailed file info (filename, path, lines, context)
    stacktrace_detailed = Column(Text, nullable=True)  # Расширенный стектрейс с контекстом кода
    
    # Full payload as JSON for complete trace
    full_payload = Column(Text, nullable=True)  # Store complete webhook payload as JSON

    def __repr__(self):
        return f"<Error(id={self.id}, event_id={self.event_id}, project={self.project})>"

