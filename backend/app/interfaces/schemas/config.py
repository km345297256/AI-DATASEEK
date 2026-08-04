from pydantic import BaseModel


class ClientConfigResponse(BaseModel):
    """Client runtime configuration response schema"""
    auth_provider: str
    default_agent_name: str
    default_model_provider: str
    default_model_name: str
