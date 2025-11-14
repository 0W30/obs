"""
FastAPI application main file.
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
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

# Add logging middleware to log all requests
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        logger.info(f"Incoming request: {request.method} {request.url.path} from {request.client.host if request.client else 'unknown'}")
        response = await call_next(request)
        logger.info(f"Response: {response.status_code} for {request.method} {request.url.path}")
        return response

app.add_middleware(LoggingMiddleware)

# Configure CORS - allow all origins for webhook (Sentry sends from their servers)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins for webhook compatibility
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


@app.post("/test-webhook")
async def test_webhook(request: Request):
    """
    Test endpoint to check if webhook endpoint is reachable.
    """
    logger.info("Test webhook endpoint called")
    try:
        body = await request.body()
        logger.info(f"Test webhook body: {body.decode('utf-8', errors='ignore')[:500]}")
        return {
            "status": "received",
            "message": "Test webhook endpoint is working",
            "body_length": len(body)
        }
    except Exception as e:
        logger.error(f"Error in test webhook: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}

