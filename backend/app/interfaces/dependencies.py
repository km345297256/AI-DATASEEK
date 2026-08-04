import logging
from functools import lru_cache
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.external.search import get_search_engine
from app.domain.models.user import User, UserRole

# Import all required services
from app.application.services.agent_service import AgentService
from app.application.services.file_service import FileService
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.application.services.email_service import EmailService
from app.infrastructure.external.cache import get_cache

# Import all required dependencies for agent service
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from app.infrastructure.external.task.redis_task import RedisStreamTask
from app.infrastructure.repositories.mongo_agent_repository import MongoAgentRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.infrastructure.repositories.mongo_mcp_repository import MongoMCPRepository
from app.infrastructure.repositories.user_repository import MongoUserRepository
from app.application.services.api_key_service import APIKeyService
from app.infrastructure.repositories.api_key_repository import MongoAPIKeyRepository
from app.application.services.agent_profile_service import AgentProfileService
from app.infrastructure.repositories.agent_profile_repository import MongoAgentProfileRepository


# Configure logging
logger = logging.getLogger(__name__)

@lru_cache()
def get_agent_service() -> AgentService:
    """
    Get agent service instance with all required dependencies
    
    This function creates and returns an AgentService instance with all
    necessary dependencies. Uses lru_cache for singleton pattern.
    """
    logger.info("Creating AgentService instance")
    
    # Create all dependencies
    agent_repository = MongoAgentRepository()
    session_repository = MongoSessionRepository()
    sandbox_cls = DockerSandbox
    task_cls = RedisStreamTask
    file_storage = get_file_storage()
    search_engine = get_search_engine()
    mcp_repository = MongoMCPRepository()
    
    # Create AgentService instance
    return AgentService(
        agent_repository=agent_repository,
        session_repository=session_repository,
        sandbox_cls=sandbox_cls,
        task_cls=task_cls,
        file_storage=file_storage,
        search_engine=search_engine,
        mcp_repository=mcp_repository,
    )


@lru_cache()
def get_file_service() -> FileService:
    """
    Get file service instance with required dependencies
    
    This function creates and returns a FileService instance with
    the necessary file storage and token service dependencies.
    """
    logger.info("Creating FileService instance")
    
    # Get dependencies
    file_storage = get_file_storage()
    token_service = get_token_service()
    
    return FileService(
        file_storage=file_storage,
        token_service=token_service,
    )


@lru_cache()
def get_auth_service() -> AuthService:
    """
    Get authentication service instance with required dependencies
    
    This function creates and returns an AuthService instance with
    the necessary user repository dependency.
    """
    logger.info("Creating AuthService instance")
    
    # Get user repository dependency
    user_repository = MongoUserRepository()
    
    return AuthService(
        user_repository=user_repository,
        token_service=get_token_service(),
    )


def get_user_repository() -> MongoUserRepository:
    return MongoUserRepository()


@lru_cache()
def get_token_service() -> TokenService:
    """Get token service instance"""
    logger.info("Creating TokenService instance")
    return TokenService()


@lru_cache()
def get_email_service() -> EmailService:
    """Get email service instance"""
    logger.info("Creating EmailService instance")
    cache = get_cache()
    return EmailService(cache=cache)


@lru_cache()
def get_api_key_service() -> APIKeyService:
    """Get API key service instance"""
    logger.info("Creating APIKeyService instance")
    api_key_repository = MongoAPIKeyRepository()
    user_repository = MongoUserRepository()
    return APIKeyService(api_key_repository=api_key_repository, user_repository=user_repository)


@lru_cache()
def get_agent_profile_service() -> AgentProfileService:
    """Get agent profile service instance"""
    logger.info("Creating AgentProfileService instance")
    return AgentProfileService(repository=MongoAgentProfileRepository())


def _system_user() -> User:
    """Return the single system identity used by every API caller."""
    return User(
        id="anonymous",
        fullname="AI-DataSeek System",
        email="system@localhost",
        role=UserRole.ADMIN,
        is_active=True,
        token_balance=None,
    )


async def get_current_user() -> User:
    """Return the system administrator; the product has no caller authentication."""
    return _system_user()


async def get_optional_current_user() -> User:
    """Return the system administrator for formerly optional-auth endpoints."""
    return _system_user()


async def verify_signature() -> str:
    """Keep signed-URL call sites compatible without requiring a signature."""
    return ""


async def verify_signature_websocket() -> str:
    """Keep WebSocket call sites compatible without requiring a signature."""
    return ""
