"""
FastAPI application main file.
"""
import logging
from datetime import datetime
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
    logger.info("=" * 60)
    logger.info("Starting Sentry Error Collector...")
    logger.info("=" * 60)
    try:
        await init_db()
        logger.info("✅ Database initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {str(e)}", exc_info=True)
        raise
    
    # Log available routes
    logger.info("📡 Available endpoints:")
    logger.info("   GET  / - API info")
    logger.info("   GET  /health - Health check")
    logger.info("   POST /sentry/webhook - Sentry/GlitchTip webhook")
    logger.info("   GET  /errors - All errors")
    logger.info("   GET  /errors/latest - Latest error")
    logger.info("   GET  /errors/latest/stacktrace - Latest error stacktrace")
    logger.info("   GET  /config - Configuration")
    logger.info("=" * 60)
    
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

# Add logging middleware to log all requests (including webhooks)
class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        client_host = request.client.host if request.client else 'unknown'
        path = request.url.path
        
        # Skip detailed logging for healthcheck (too noisy)
        if path == "/health" and request.headers.get('user-agent', '').startswith('Python-urllib'):
            # Just log briefly for healthcheck
            response = await call_next(request)
            return response
        
        # Full logging for all other requests
        logger.info(f"🔔 INCOMING REQUEST: {request.method} {path} from {client_host}")
        logger.info(f"   Full URL: {request.url}")
        logger.info(f"   User-Agent: {request.headers.get('user-agent', 'N/A')}")
        logger.info(f"   Content-Type: {request.headers.get('content-type', 'N/A')}")
        
        response = await call_next(request)
        logger.info(f"✅ RESPONSE: {response.status_code} for {request.method} {path}")
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



