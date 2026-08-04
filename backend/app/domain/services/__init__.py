from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent_domain_service import AgentDomainService

__all__ = [
    'AgentDomainService',
]


def __getattr__(name: str) -> Any:
    """Keep public service exports without importing every runtime dependency."""
    if name == "AgentDomainService":
        from .agent_domain_service import AgentDomainService
        return AgentDomainService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
