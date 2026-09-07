from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
import asyncio

from app.core.config import get_settings
from app.domain.external.plugin_runtime import PluginRuntimeError
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.storage.redis import get_redis
from app.interfaces.dependencies import (
    get_agent_service,
    get_jupyter_service,
    get_plugin_runtime,
)
from app.interfaces.api.routes import router
from app.infrastructure.logging import setup_logging
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.middleware.sso_auth import SSOAuthorizationMiddleware
from app.infrastructure.models.documents import (
    AgentDocument,
    AgentProfileDocument,
    APIKeyDocument,
    ApprovalRequestDocument,
    AuditLogDocument,
    DataCenterDatasetDocument,
    DataProductDocument,
    ExecutionEnvironmentSnapshotDocument,
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
    SessionEventReservationDocument,
    SpillArtifactDocument,
    SkillDocument,
    StoredFileDocument,
    TemporaryDatasetDocument,
    TaskFeedbackDocument,
    JupyterSessionDocument,
    TokenUsageDocument,
    UserDocument,
    WorkspaceDocument,
    WorkspaceMemberDocument,
)
from app.domain.services.safety.policy_store import ensure_safety_rule_seeds
from app.infrastructure.external.file.spill_factory import get_spill_artifact_store
from app.infrastructure.external.analysis_job_factory import get_analysis_job_service
from app.infrastructure.models.analysis_job import AnalysisJobDocument
from app.infrastructure.models.credential import CredentialDocument
from app.infrastructure.models.tool_approval import ToolApprovalDocument
from app.infrastructure.models.model_trace import ModelTraceDocument
from app.infrastructure.external.tool_approval_factory import get_tool_approval_service
from app.infrastructure.external.sandbox.sandbox_pool import SandboxPool, set_sandbox_pool, get_sandbox_pool
from app.infrastructure.external.sandbox.node_health import (
    WARM_POOL_TARGET_KEY,
    ensure_local_default_node,
)
from app.infrastructure.external.sandbox.node_monitor import ExecutionNodeMonitor
from beanie import init_beanie

# Initialize logging system
setup_logging()
logger = logging.getLogger(__name__)

# Load configuration
settings = get_settings()
execution_node_monitor: ExecutionNodeMonitor | None = None
jupyter_reaper_task: asyncio.Task | None = None
spill_reaper_task: asyncio.Task | None = None
analysis_job_monitor_task: asyncio.Task | None = None


async def _maintain_analysis_jobs() -> None:
    while True:
        try:
            await get_analysis_job_service().maintain()
            await get_tool_approval_service().maintain()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Analysis job maintenance failed error_type=%s", type(error).__name__)
        await asyncio.sleep(5)


async def _reap_idle_jupyter_sessions() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            removed = await get_jupyter_service().reap_idle()
            if removed:
                logger.info("Removed %s idle Jupyter sessions", removed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Failed to reap idle Jupyter sessions error_type=%s",
                type(error).__name__,
            )


async def _reap_expired_spill_artifacts() -> None:
    interval = max(30, settings.spill_reaper_interval_seconds)
    while True:
        try:
            removed = await get_spill_artifact_store().reap_expired(limit=100)
            if removed:
                logger.info("Removed %s expired spill artifacts", removed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Failed to reap expired spill artifacts error_type=%s",
                type(error).__name__,
            )
        await asyncio.sleep(interval)


# Create lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global execution_node_monitor, jupyter_reaper_task, spill_reaper_task, analysis_job_monitor_task
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
            SessionEventReservationDocument,
            ExecutionEnvironmentSnapshotDocument,
            SpillArtifactDocument,
            AnalysisJobDocument,
            CredentialDocument,
            ToolApprovalDocument,
            ModelTraceDocument,
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
            DataProductDocument,
            TemporaryDatasetDocument,
            TaskFeedbackDocument,
            JupyterSessionDocument,
        ]
    )
    await ensure_safety_rule_seeds()
    logger.info("Successfully initialized Beanie")
    jupyter_reaper_task = asyncio.create_task(_reap_idle_jupyter_sessions())
    # Cleanup remains active even when creation of new spills is disabled, so
    # tombstones and artifacts from an earlier configuration are not stranded.
    spill_reaper_task = asyncio.create_task(_reap_expired_spill_artifacts())
    analysis_job_monitor_task = asyncio.create_task(_maintain_analysis_jobs())

    local_node = await ensure_local_default_node()

    execution_node_monitor = ExecutionNodeMonitor(interval_seconds=30)
    execution_node_monitor.start()
    logger.info("Execution node monitor started")

    # Initialize Redis
    await get_redis().initialize()

    # Keep a node-local manager even at target zero so stale warm containers
    # left by an unclean restart are converged instead of consuming capacity.
    if settings.sandbox_isolation == "session":
        warm_pool_target = max(
            0,
            int(
                (local_node.runtime_config or {}).get(
                    WARM_POOL_TARGET_KEY,
                    settings.sandbox_pool_size,
                )
            ),
        )
        pool = SandboxPool(warm_pool_target)
        set_sandbox_pool(pool)
        pool.start_background_init()
        logger.info("Sandbox warm pool manager started with target size %s", warm_pool_target)

    # Start the external catalog only after the existing infrastructure has
    # initialized successfully, but before the app accepts requests.
    plugin_runtime = get_plugin_runtime()
    app.state.plugin_runtime = plugin_runtime
    if plugin_runtime is not None:
        try:
            snapshot = await plugin_runtime.start()
            logger.info(
                "Cordis catalog ready revision=%s plugins=%s tools=%s",
                snapshot.revision[:12],
                snapshot.plugin_count,
                snapshot.tool_count,
            )
        except PluginRuntimeError as exc:
            # Catalog failure must not expose the stale Python manifest view.
            # The injected runtime stays unhealthy, so new toolkits fail closed.
            logger.error(
                "Cordis plugin runtime unavailable error_type=%s",
                type(exc).__name__,
            )

    try:
        yield
    finally:
        # Code executed on shutdown
        logger.info("Application shutdown - AI-DataSeek terminating")

        if jupyter_reaper_task:
            jupyter_reaper_task.cancel()
            await asyncio.gather(jupyter_reaper_task, return_exceptions=True)
            jupyter_reaper_task = None

        if spill_reaper_task:
            spill_reaper_task.cancel()
            await asyncio.gather(spill_reaper_task, return_exceptions=True)
            spill_reaper_task = None

        pool = get_sandbox_pool()
        if pool:
            await pool.shutdown()
            set_sandbox_pool(None)

        if execution_node_monitor:
            await execution_node_monitor.stop()
            execution_node_monitor = None

        logger.info("Cleaning up AgentService instance")
        try:
            await asyncio.wait_for(get_agent_service().shutdown(), timeout=30.0)
            logger.info("AgentService shutdown completed successfully")
        except asyncio.TimeoutError:
            logger.warning("AgentService shutdown timed out after 30 seconds")
        except Exception as error:
            logger.error(
                "Error during AgentService cleanup error_type=%s",
                type(error).__name__,
            )

        try:
            await get_analysis_job_service().shutdown()
            await get_tool_approval_service().shutdown()
        finally:
            if analysis_job_monitor_task:
                analysis_job_monitor_task.cancel()
                await asyncio.gather(analysis_job_monitor_task, return_exceptions=True)
                analysis_job_monitor_task = None

        if plugin_runtime is not None:
            try:
                await plugin_runtime.shutdown()
            except Exception as error:
                logger.error(
                    "Error during Cordis plugin runtime cleanup error_type=%s",
                    type(error).__name__,
                )

        # Runner cleanup persists final sandbox/allocation state and may still
        # consume Redis streams. Close shared stores only after it completes.
        await get_redis().shutdown()
        await get_mongodb().shutdown()

app = FastAPI(title="AI-DataSeek", lifespan=lifespan)

# Configure CORS
browser_origin = settings.server_host.rstrip("/") if settings.server_host else None
app.add_middleware(
    CORSMiddleware,
    allow_origins=[browser_origin] if browser_origin else [],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# The supported deployment uses one built-in administrator and deliberately
# sends no browser credentials. Keep the legacy SSO boundary available only
# for deployments that explicitly select an authenticated provider.
if settings.auth_provider != "none":
    app.add_middleware(SSOAuthorizationMiddleware)

# Register exception handlers
register_exception_handlers(app)

# Register routes
app.include_router(router, prefix="/api/v1")
