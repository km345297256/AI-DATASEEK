from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.storage.redis import get_redis
from app.interfaces.dependencies import get_agent_service
from app.interfaces.api.routes import router
from app.infrastructure.logging import setup_logging
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.infrastructure.models.documents import (
    AgentDocument,
    AgentProfileDocument,
    APIKeyDocument,
    ApprovalRequestDocument,
    AuditLogDocument,
    DataCenterDatasetDocument,
    ExecutionNodeDocument,
    FileUploadSessionDocument,
    MCPConfigDocument,
    ModelConfigurationDocument,
    NodeCredentialDocument,
    RendererDocument,
    RoleTokenQuotaDocument,
    SafetyRuleDocument,
    SafetyRuleSeedStateDocument,
    SandboxAllocationDocument,
    SandboxRecordDocument,
    SessionDocument,
    SessionEventDocument,
    SkillDocument,
    StoredFileDocument,
    TemporaryDatasetDocument,
    TokenUsageDocument,
    UserDocument,
    WorkspaceDocument,
    WorkspaceMemberDocument,
)
from app.domain.services.safety.policy_store import ensure_safety_rule_seeds
from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool, set_sandbox_pool, get_sandbox_pool
from app.infrastructure.external.sandbox.node_monitor import ExecutionNodeMonitor
from beanie import init_beanie

# Initialize logging system
setup_logging()
logger = logging.getLogger(__name__)

# Load configuration
settings = get_settings()
execution_node_monitor: ExecutionNodeMonitor | None = None


# Create lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global execution_node_monitor
    # Code executed on startup
    logger.info("Application startup - AI-DataSeek initializing")

    # Initialize MongoDB and Beanie
    await get_mongodb().initialize()

    # Initialize Beanie
    await init_beanie(
        database=get_mongodb().client[settings.mongodb_database],
        document_models=[
            AgentDocument,
            SessionDocument,
            UserDocument,
            APIKeyDocument,
            AgentProfileDocument,
            ModelConfigurationDocument,
            SandboxRecordDocument,
            SessionEventDocument,
            MCPConfigDocument,
            SkillDocument,
            RendererDocument,
            WorkspaceDocument,
            WorkspaceMemberDocument,
            AuditLogDocument,
            ApprovalRequestDocument,
            TokenUsageDocument,
            StoredFileDocument,
            FileUploadSessionDocument,
            ExecutionNodeDocument,
            SandboxAllocationDocument,
            NodeCredentialDocument,
            RoleTokenQuotaDocument,
            SafetyRuleDocument,
            SafetyRuleSeedStateDocument,
            DataCenterDatasetDocument,
            TemporaryDatasetDocument,
        ]
    )
    await ensure_safety_rule_seeds()
    logger.info("Successfully initialized Beanie")

    execution_node_monitor = ExecutionNodeMonitor(interval_seconds=30)
    execution_node_monitor.start()
    logger.info("Execution node monitor started")

    # Initialize Redis
    await get_redis().initialize()

    # Initialize sandbox warm pool if configured
    if settings.sandbox_isolation == "session" and settings.sandbox_pool_size > 0:
        pool = SandboxPool(settings.sandbox_pool_size)
        set_sandbox_pool(pool)
        pool.start_background_init()
        logger.info(f"Sandbox warm pool started with target size {settings.sandbox_pool_size}")

    try:
        yield
    finally:
        # Code executed on shutdown
        logger.info("Application shutdown - AI-DataSeek terminating")

        pool = get_sandbox_pool()
        if pool:
            await pool.shutdown()

        if execution_node_monitor:
            await execution_node_monitor.stop()
            execution_node_monitor = None

        # Disconnect from MongoDB
        await get_mongodb().shutdown()
        # Disconnect from Redis
        await get_redis().shutdown()

        logger.info("Cleaning up AgentService instance")
        try:
            await asyncio.wait_for(get_agent_service().shutdown(), timeout=30.0)
            logger.info("AgentService shutdown completed successfully")
        except asyncio.TimeoutError:
            logger.warning("AgentService shutdown timed out after 30 seconds")
        except Exception as e:
            logger.error(f"Error during AgentService cleanup: {str(e)}")

app = FastAPI(title="AI-DataSeek", lifespan=lifespan)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register exception handlers
register_exception_handlers(app)

# Register routes
app.include_router(router, prefix="/api/v1")
