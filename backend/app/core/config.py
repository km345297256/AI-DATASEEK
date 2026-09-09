import os
import json
import logging
from pydantic_settings import BaseSettings
from functools import lru_cache
from pydantic import Field
from typing import Literal

logger = logging.getLogger(__name__)


def _parse_extra_headers() -> dict | None:
    raw = os.environ.get("EXTRA_HEADERS")
    if not raw:
        return None
    try:
        headers = json.loads(raw)
        if isinstance(headers, dict):
            return headers
        logger.warning("EXTRA_HEADERS is not a JSON object, ignoring")
    except json.JSONDecodeError:
        logger.warning("EXTRA_HEADERS is not valid JSON, ignoring")
    return None


class Settings(BaseSettings):
    
    # Model provider configuration
    api_key: str | None = None
    api_base: str | None = None
    
    # Model configuration
    model_name: str = "gpt-4o"
    model_provider: str = "openai"
    temperature: float = 0.7
    max_tokens: int = 2000
    # Per-request engineering limits, not cumulative task quotas or advertised
    # provider capacities. Offline token estimates include tool definitions.
    model_context_capacity_tokens: int = Field(default=131_072, ge=4096, le=2_000_000)
    model_context_safety_tokens: int = Field(default=2048, ge=256, le=65_536)
    # Analysis has no total tool/call/token/time quota. The removed MODEL_TASK_*
    # and ANALYSIS_BUDGET_* environment values intentionally have no effect.
    model_trace_store_timeout_seconds: float = Field(default=3.0, ge=0.1, le=30)
    # Existing saved profiles retain their own defaults; sessions without a
    # profile use progressive Cordis schema disclosure. Trials are opt-in.
    tool_selection_mode: Literal["all", "on_demand"] = "on_demand"
    tool_preset_id: str = "general"
    code_mode_enabled: bool = False
    domain_subagents_enabled: bool = False
    # Execution responses often contain a complete script or structured result.
    # Keep a larger floor than planner responses so valid output is not cut off.
    execution_max_tokens: int = 4096
    # Direct one-shot model calls may use the client's small built-in retry
    # budget. BaseAgent disables that inner loop and owns its bounded policy.
    llm_client_max_retries: int = 2
    llm_retry_attempts: int = 4
    llm_retry_base_seconds: float = 1.0
    llm_retry_max_seconds: float = 8.0
    # Once the bounded tool budget is exhausted, the Agent gets one tool-free
    # turn to synthesize the evidence it already collected. Keep this separate
    # from provider/network retries and from the dataset quicklook synthesis
    # deadline, which has its own larger professional-analysis budget.
    agent_finalization_timeout_seconds: float = 45.0
    # Lightweight dataset routing runs before any sandbox allocation. A slow or
    # unavailable classifier must fall back to the normal Agent path promptly.
    dataset_request_resolver_timeout_seconds: float = 8.0
    # Suggested questions are optional page bootstrap data. Keep their model
    # call below the browser API timeout so an unavailable provider can fall
    # back to local questions without turning the whole panel into an error.
    dataset_suggested_question_timeout_seconds: float = 8.0

    # System-owned safety gate. It always runs before Planner and is not part
    # of a user-editable Agent profile. If review is unavailable, it fails closed.
    safety_review_model_provider: str | None = None
    safety_review_model_name: str | None = None
    safety_review_model_base: str | None = None
    safety_review_model_api_key: str | None = None
    safety_review_temperature: float = 0.0
    safety_review_max_tokens: int = 600
    safety_review_timeout_seconds: float = 20.0

    # Vision model configuration
    vision_model_name: str | None = None
    vision_model_provider: str | None = None
    vision_model_base: str | None = None
    vision_model_api_key: str | None = None
    vision_temperature: float | None = None
    vision_max_tokens: int | None = None
    
    # MongoDB configuration
    mongodb_uri: str = "mongodb://mongodb:27017"
    mongodb_database: str = "ai_dataseek"
    mongodb_username: str | None = None
    mongodb_password: str | None = None

    # File storage configuration
    # "gridfs" keeps legacy MongoDB GridFS behavior, "minio" stores new files in MinIO,
    # "hybrid" writes new files to MinIO and reads legacy GridFS ObjectId files.
    file_storage_provider: str = "gridfs"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "ai-dataseek-files"
    minio_secure: bool = False
    minio_region: str | None = None
    minio_presigned_expire_seconds: int = 1800
    minio_object_prefix: str = "files"
    large_upload_part_size: int = 16 * 1024 * 1024
    large_upload_session_expire_hours: int = 24

    # Oversized text tool results are durably spilled before they reach model
    # context, SSE, Redis, or Mongo session events. Existing file storage owns
    # the bytes; a private Mongo record maps the opaque locator to that object.
    spill_enabled: bool = True
    analysis_jobs_enabled: bool = True
    # Dedicated Fernet key. No fallback to model credentials; existing model
    # configuration and legacy MCP credentials are deliberately not migrated.
    credential_encryption_key: str = ""
    spill_max_inline_bytes: int = 50_000
    spill_max_artifact_bytes: int = 32 * 1024 * 1024
    spill_preview_bytes: int = 12_000
    spill_read_chunk_bytes: int = 16_000
    spill_store_timeout_seconds: float = 5.0
    spill_artifact_retention_hours: int = 168
    spill_reaper_interval_seconds: int = 300
    spill_artifact_identity_key: str | None = None

    # Data-center dataset registry. Managed uploads are stored in a Docker volume
    # shared by backend and local Worker; host_path locations stay node-local.
    dataset_storage_root: str = "/data/datasets"
    dataset_managed_volume: str = "ai-dataseek-dataset-data"
    dataset_host_path_allowlist: str = "/data,/mnt,/srv,/opt/datasets"
    # Optional root that exposes the machine filesystem inside the Docker
    # daemon's mount namespace (Snap Docker uses /var/lib/snapd/hostfs).
    dataset_docker_host_root: str = ""

    # Data-center SSO and analysis-tool usage reporting.
    sso_uid_url: str = "https://space.4fair.cn/oidc-server/sso/uid"
    analysis_tool_usage_url: str = "https://space.4fair.cn/tds-trading/order/order.addAnalysisToolCount"
    analysis_tool_source: str = "数据中心"
    external_integration_timeout_seconds: float = 10.0

    # Redis configuration
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    
    # Sandbox configuration
    sandbox_address: str | None = None
    sandbox_isolation: str = "session"  # "session" creates one sandbox per session; "shared" uses SANDBOX_ADDRESS
    sandbox_image: str | None = None
    sandbox_name_prefix: str | None = None
    sandbox_ttl_minutes: int | None = 30
    sandbox_network: str | None = None  # Docker network bridge name
    sandbox_chrome_args: str | None = ""
    sandbox_https_proxy: str | None = None
    sandbox_http_proxy: str | None = None
    sandbox_no_proxy: str | None = None
    sandbox_pool_size: int = 0  # 0=disabled, >0=number of pre-warmed containers
    sandbox_docker_create_timeout_seconds: int = 60
    # The local execution node is system-managed. Keep the limit optional so
    # existing installations that manage it through the admin API retain their
    # current value; deployments can opt into an explicit host budget.
    sandbox_max_concurrent: int | None = None
    # Jupyter is intentionally independent from Agent sandboxes. Every task
    # receives a private container, kernel token and writable Docker volume.
    jupyter_image: str = "ai-dataseek-jupyter:latest"
    jupyter_memory_limit: str = "2g"
    jupyter_nano_cpus: int = 2_000_000_000
    jupyter_pids_limit: int = 256
    # The container is reachable only on the private Docker network by the
    # task's Sandbox browser. It has no published host port.
    jupyter_network_disabled: bool = False
    jupyter_ttl_minutes: int = 60
    # A short bounded queue absorbs normal bursts instead of surfacing a raw
    # scheduler capacity error to the user.
    sandbox_capacity_wait_seconds: float = 60.0
    sandbox_capacity_poll_seconds: float = 1.0
    # Paused containers preserve a session filesystem but still retain host
    # memory and Docker metadata. Reclaim them after an inactivity window.
    sandbox_paused_destroy_after_minutes: int | None = None
    sandbox_resume_ready_timeout_seconds: float = 30.0
    sandbox_hydration_concurrency: int = 3

    # Browser engine configuration
    browser_engine: str = "browser_use"  # "playwright" or "browser_use"
    
    # Search engine configuration
    search_provider: str | None = "bing_web"  # "baidu", "baidu_web", "google", "bing", "bing_web", "tavily", "serper", "custom"
    baidu_search_api_key: str | None = None
    bing_search_api_key: str | None = None
    google_search_api_key: str | None = None
    google_search_engine_id: str | None = None
    tavily_api_key: str | None = None
    # Serper.dev search configuration (SEARCH_PROVIDER=serper)
    serper_api_key: str | None = None
    # Custom search API configuration (SEARCH_PROVIDER=custom)
    search_api_url: str | None = None
    search_api_key: str | None = None
    search_api_key_header: str = "Authorization"
    search_api_key_header_prefix: str = "Bearer "
    search_api_key_param: str = ""
    search_api_method: str = "POST"
    search_query_field: str = "q"
    search_result_field: str = "results"
    search_title_field: str = "title"
    search_link_field: str = "link"
    search_snippet_field: str = "snippet"
    
    # Auth configuration
    server_host: str | None = None
    auth_provider: str = "none"  # Authentication is permanently disabled.
    password_salt: str | None = None
    password_hash_rounds: int = 10
    password_hash_algorithm: str = "pbkdf2_sha256"
    local_auth_email: str = "admin@example.com"
    local_auth_password: str = "admin"
    
    # Email configuration
    email_host: str | None = None  # "smtp.gmail.com"
    email_port: int | None = None  # 587
    email_username: str | None = None
    email_password: str | None = None
    email_from: str | None = None
    # Backward-compatible SMTP aliases used by existing deployments.
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: bool | None = True
    
    # JWT configuration
    jwt_secret_key: str = "your-secret-key-here"  # Should be set in production
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7
    
    # Extra headers for LLM requests (parsed from EXTRA_HEADERS env var, JSON)
    extra_headers: dict | None = None
    
    # MCP configuration
    mcp_config_path: str = "/etc/mcp.json"

    # Cordis plugin catalog/control-plane runtime. Tool execution remains in
    # the existing sandbox; this process only owns registration and reloads.
    plugin_runtime_enabled: bool = True
    plugin_runtime_node_executable: str = "node"
    plugin_runtime_host_path: str = ""
    plugin_runtime_tools_dir: str = ""
    plugin_runtime_execution_contract_dir: str = ""
    plugin_runtime_request_timeout_seconds: float = 5.0
    plugin_runtime_startup_timeout_seconds: float = 15.0
    plugin_runtime_shutdown_timeout_seconds: float = 3.0
    plugin_runtime_max_response_frame_bytes: int = 8 * 1024 * 1024

    # Stable deployment secret used only for opaque execution-environment
    # identities. Existing installations temporarily fall back to API_KEY;
    # production deployments should provide an independent random value.
    execution_snapshot_identity_key: str | None = None

    # Skill configuration
    skills_enabled: bool = True
    skills_dir: str = "skills"
    user_skills_dir: str = "skills/users"
    max_active_skills: int = 3
    
    # Logging configuration
    log_level: str = "INFO"
    app_timezone: str = "Asia/Shanghai"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # Retired deployment keys (including task quotas) cannot reactivate
        # removed behavior or prevent loading the current configuration.
        extra = "ignore"
        
    def validate(self):
        """Validate configuration settings"""
        if not self.api_key:
            raise ValueError("API key is required")
        if (
            self.execution_snapshot_identity_key is not None
            and len(self.execution_snapshot_identity_key.encode("utf-8")) < 32
        ):
            raise ValueError(
                "EXECUTION_SNAPSHOT_IDENTITY_KEY must contain at least 32 bytes"
            )
        # Lifecycle settings remain valid even while new spill creation is
        # disabled because the reaper still cleans older tombstones.
        if self.spill_max_inline_bytes < 1024:
            raise ValueError("SPILL_MAX_INLINE_BYTES must be at least 1024")
        if self.spill_max_artifact_bytes < self.spill_max_inline_bytes:
            raise ValueError(
                "SPILL_MAX_ARTIFACT_BYTES must be at least SPILL_MAX_INLINE_BYTES"
            )
        if self.spill_preview_bytes < 0:
            raise ValueError("SPILL_PREVIEW_BYTES must not be negative")
        if self.spill_read_chunk_bytes < 1024:
            raise ValueError("SPILL_READ_CHUNK_BYTES must be at least 1024")
        maximum_safe_read = max(
            1024,
            (self.spill_max_inline_bytes - 4096) // 2,
        )
        if self.spill_read_chunk_bytes > maximum_safe_read:
            raise ValueError(
                "SPILL_READ_CHUNK_BYTES is too large for SPILL_MAX_INLINE_BYTES"
            )
        if self.spill_store_timeout_seconds <= 0:
            raise ValueError("SPILL_STORE_TIMEOUT_SECONDS must be positive")
        if self.spill_artifact_retention_hours <= 0:
            raise ValueError("SPILL_ARTIFACT_RETENTION_HOURS must be positive")
        if self.spill_reaper_interval_seconds <= 0:
            raise ValueError("SPILL_REAPER_INTERVAL_SECONDS must be positive")
        if (
            self.spill_artifact_identity_key is not None
            and len(self.spill_artifact_identity_key.encode("utf-8")) < 32
        ):
            raise ValueError(
                "SPILL_ARTIFACT_IDENTITY_KEY must contain at least 32 bytes"
            )

@lru_cache()
def get_settings() -> Settings:
    """Get application settings"""
    api_key = os.getenv("API_KEY")
    if api_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = api_key
    if api_key and not os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["DEEPSEEK_API_KEY"] = api_key
    if not os.environ.get("DEEPSEEK_API_BASE") and os.getenv("API_BASE"):
        os.environ["DEEPSEEK_API_BASE"] = os.getenv("API_BASE")
    settings = Settings()
    settings.extra_headers = _parse_extra_headers()
    settings.validate()
    return settings 
