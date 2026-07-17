from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.storage.redis import get_redis
from app.interfaces.dependencies import get_agent_service, get_claw_service
from app.interfaces.api.routes import router
from app.interfaces.api.openai_routes import router as openai_router
from app.infrastructure.logging import setup_logging
from app.domain.utils.error_reporting import safe_exception_summary
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.upload_body_limit import UploadBodyLimitMiddleware
from app.infrastructure.models.documents import (
    AgentDocument,
    SessionDocument,
    UserDocument,
    ClawDocument,
    TurnSubmissionDocument,
    TurnQuotaDocument,
    TurnOutputEventDocument,
)
from beanie import init_beanie

# Initialize logging system
setup_logging()
logger = logging.getLogger(__name__)

# Load configuration
settings = get_settings()


# Create lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Code executed on startup
    logger.info("Application startup - Manus AI Agent initializing")
    
    # Initialize MongoDB and Beanie
    await get_mongodb().initialize()

    # Initialize Beanie
    await init_beanie(
        database=get_mongodb().client[settings.mongodb_database],
        document_models=[
            AgentDocument,
            SessionDocument,
            UserDocument,
            ClawDocument,
            TurnSubmissionDocument,
            TurnQuotaDocument,
            TurnOutputEventDocument,
        ]
    )
    logger.info("Successfully initialized Beanie")
    
    # Initialize Redis
    await get_redis().initialize()
    if settings.claw_enabled:
        get_claw_service().start_maintenance()
    
    try:
        yield
    finally:
        # Code executed on shutdown
        logger.info("Application shutdown - Manus AI Agent terminating")
        logger.info("Cleaning up AgentService instance")
        try:
            await asyncio.wait_for(get_agent_service().shutdown(), timeout=30.0)
            logger.info("AgentService shutdown completed successfully")
        except asyncio.TimeoutError:
            logger.warning("AgentService shutdown timed out after 30 seconds")
        except Exception as e:
            logger.error(
                "Error during AgentService cleanup: %s",
                safe_exception_summary(e),
            )

        if settings.claw_enabled:
            logger.info("Cleaning up ClawService instance")
            try:
                await asyncio.wait_for(get_claw_service().shutdown(), timeout=10.0)
                logger.info("ClawService shutdown completed successfully")
            except asyncio.TimeoutError:
                logger.warning("ClawService shutdown timed out after 10 seconds")
            except Exception as e:
                logger.error(
                    "Error during ClawService cleanup: %s",
                    safe_exception_summary(e),
                )

        # Services must stop before their database/cache clients are closed.
        await get_mongodb().shutdown()
        await get_redis().shutdown()

app = FastAPI(title="Manus AI Agent", lifespan=lifespan)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_allowed_origins(),
    # Authentication uses explicit Bearer headers, not ambient cookies.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    UploadBodyLimitMiddleware,
    max_body_bytes=settings.multipart_upload_max_body_bytes,
    paths={"/api/v1/files", "/api/v1/claw/upload"},
    error_detail="Upload request body is too large",
)
app.add_middleware(
    UploadBodyLimitMiddleware,
    max_body_bytes=settings.chat_max_body_bytes,
    path_patterns={r"/api/v1/sessions/[^/]+/chat"},
    error_detail="Chat request body is too large",
)

# Register exception handlers
register_exception_handlers(app)


@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}

# Register routes
app.include_router(router, prefix="/api/v1")
# OpenAI-compatible proxy (used by OpenClaw containers for LLM requests)
app.include_router(openai_router)
