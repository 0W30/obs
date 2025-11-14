"""
Database configuration and session management for SQLite.
"""
import os
from pathlib import Path
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings

# Database URL
DATABASE_URL = settings.DATABASE_URL

# Ensure data directory exists
# Extract path from SQLite URL (remove sqlite+aiosqlite:/// prefix)
db_path = DATABASE_URL.replace("sqlite+aiosqlite:///", "")
# Handle relative paths (./data/errors.db -> data/errors.db)
if db_path.startswith("./"):
    db_path = db_path[2:]
# Handle absolute paths (already start with /)
# For relative paths, ensure they're relative to current directory
if not db_path.startswith("/"):
    db_path = os.path.abspath(db_path)

db_dir = os.path.dirname(db_path)
if db_dir and not os.path.exists(db_dir):
    Path(db_dir).mkdir(parents=True, exist_ok=True)

# Create async engine
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
)

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Base class for models
Base = declarative_base()


async def get_db():
    """
    Dependency for getting database session.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """
    Initialize database - create tables if they don't exist.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

