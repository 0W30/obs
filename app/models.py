"""
SQLAlchemy models for the database.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from app.database import Base


class Error(Base):
    """
    Model for storing Sentry errors.
    """
    __tablename__ = "errors"

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String, unique=True, index=True, nullable=False)
    project = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    exception_type = Column(String, nullable=True)
    exception_value = Column(Text, nullable=True)
    stacktrace = Column(Text, nullable=True)
    timestamp = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<Error(id={self.id}, event_id={self.event_id}, project={self.project})>"

