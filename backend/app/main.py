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
from app.interfaces.middleware.office_origin_guard import OfficeOriginGuard
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
from app.domain.utils.shutdown import cancel_and_join, cleanup_steps, join_cleanup
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


async def _cleanup_application(plugin_runtime) -> None:
    """Settle consumers before dependencies, without skipping later resources."""
    global execution_node_monitor, jupyter_reaper_task, spill_reaper_task, analysis_job_monitor_task

    async def stop_background_tasks():
        global jupyter_reaper_task, spill_reaper_task, analysis_job_monitor_task
        tasks = [task for task in (jupyter_reaper_task, spill_reaper_task, analysis_job_monitor_task)
                 if task is not None]
        try:
            await cancel_and_join(tasks)
        finally:
            jupyter_reaper_task = spill_reaper_task = analysis_job_monitor_task = None

    async def stop_node_monitor():
        global execution_node_monitor
        try:
            if execution_node_monitor is not None:
                await execution_node_monitor.stop()
        finally:
            execution_node_monitor = None

    async def stop_pool():
        pool = get_sandbox_pool()
        try:
            if pool is not None:
                await pool.shutdown()
        finally:
            set_sandbox_pool(None)

    async def stop_plugins():
        if plugin_runtime is not None:
            await plugin_runtime.shutdown()

    async def close(name, operation):
        try:
            await operation()
        except BaseException as error:
            logger.error("Application cleanup failed resource=%s error_type=%s", name, type(error).__name__)
            raise

    # In particular, background maintenance cannot enqueue more work while its
    # services drain. Runner/sandbox final writes still have both shared stores.
    steps = (
        ("background_tasks", stop_background_tasks),
        ("node_monitor", stop_node_monitor),
        ("sandbox_pool", stop_pool),
        ("agent_service", lambda: get_agent_service().shutdown()),
        ("analysis_jobs", lambda: get_analysis_job_service().shutdown()),
        ("tool_approvals", lambda: get_tool_approval_service().shutdown()),
        ("plugin_runtime", stop_plugins),
        ("redis", lambda: get_redis().shutdown()),
        ("mongodb", lambda: get_mongodb().shutdown()),
    )
    await cleanup_steps(*(lambda name=name, operation=operation: close(name, operation)
                          for name, operation in steps))


async def _shutdown_application(app: FastAPI, plugin_runtime) -> None:
    task = getattr(app.state, "application_shutdown_task", None)
    if task is None:
        task = asyncio.create_task(_cleanup_application(plugin_runtime))
        app.state.application_shutdown_task = task
    await join_cleanup(task)


# Create lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global execution_node_monitor, jupyter_reaper_task, spill_reaper_task, analysis_job_monitor_task
    # Code executed on startup
    logger.info("Application startup - AI-DataSeek initializing")
    app.state.application_shutdown_task = None

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

    get_agent_service().start_input_recovery()
    try:
        yield
    finally:
        # Code executed on shutdown
        logger.info("Application shutdown - AI-DataSeek terminating")

        await _shutdown_application(app, plugin_runtime)

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
app.add_middleware(OfficeOriginGuard)

# Register exception handlers
register_exception_handlers(app)

# Register routes
app.include_router(router, prefix="/api/v1")
