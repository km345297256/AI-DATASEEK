import os
import json
import logging
from pydantic_settings import BaseSettings
from functools import lru_cache

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

    # Data-center dataset registry. Managed uploads are stored in a Docker volume
    # shared by backend and local Worker; host_path locations stay node-local.
    dataset_storage_root: str = "/data/datasets"
    dataset_managed_volume: str = "ai-dataseek-dataset-data"
    dataset_host_path_allowlist: str = "/data,/mnt,/srv,/opt/datasets"
    # Optional root that exposes the machine filesystem inside the Docker
    # daemon's mount namespace (Snap Docker uses /var/lib/snapd/hostfs).
    dataset_docker_host_root: str = ""

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
        
    def validate(self):
        """Validate configuration settings"""
        if not self.api_key:
            raise ValueError("API key is required")

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
