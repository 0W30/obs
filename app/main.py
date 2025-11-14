"""
FastAPI application main file.
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.routers import errors
from app import sentry
from app.config import settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for startup and shutdown events.
    """
    # Startup
    logger.info("Starting application...")
    try:
        await init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}", exc_info=True)
        raise
    
    yield
    
    # Shutdown
    logger.info("Shutting down application...")


# Create FastAPI app
app = FastAPI(
    title="Sentry Error Collector",
    description="Service for collecting and managing Sentry errors",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://localhost:3000", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(sentry.router)
app.include_router(errors.router)


@app.get("/")
async def root():
    """
    Root endpoint.
    """
    return {
        "message": "Sentry Error Collector API",
        "version": "1.0.0",
        "endpoints": {
            "sentry_webhook": "/sentry/webhook",
            "latest_error": "/errors/latest",
            "all_errors": "/errors",
            "config": "/config"
        },
        "sentry_config": {
            "project": settings.SENTRY_PROJECT or "not configured",
            "organization": settings.SENTRY_ORGANIZATION or "not configured",
            "filter_by_project": settings.SENTRY_FILTER_BY_PROJECT,
            "dsn_configured": bool(settings.SENTRY_DSN)
        }
    }


@app.get("/config")
async def get_config():
    """
    Get Sentry configuration (without sensitive data).
    """
    return {
        "sentry": {
            "project": settings.SENTRY_PROJECT or None,
            "organization": settings.SENTRY_ORGANIZATION or None,
            "filter_by_project": settings.SENTRY_FILTER_BY_PROJECT,
            "dsn_configured": bool(settings.SENTRY_DSN)
        },
        "database": {
            "url": settings.DATABASE_URL.split("://")[0] + "://***"  # Hide actual path
        }
    }


@app.get("/health")
async def health():
    """
    Health check endpoint.
    """
    return {"status": "healthy"}

