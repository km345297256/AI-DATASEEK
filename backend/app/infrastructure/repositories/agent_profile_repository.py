import logging
from typing import Optional, List
from app.domain.models.agent_profile import AgentProfile
from app.domain.models.workspace import personal_workspace_id
from app.domain.repositories.agent_profile_repository import AgentProfileRepository
from app.infrastructure.models.documents import AgentProfileDocument

logger = logging.getLogger(__name__)


class MongoAgentProfileRepository(AgentProfileRepository):

    async def create(self, profile: AgentProfile) -> AgentProfile:
        doc = AgentProfileDocument.from_domain(profile)
        await doc.create()
        return doc.to_domain()

    async def get_by_id(self, profile_id: str) -> Optional[AgentProfile]:
        doc = await AgentProfileDocument.find_one(AgentProfileDocument.profile_id == profile_id)
        return doc.to_domain() if doc else None

    async def list_for_user(self, user_id: str) -> List[AgentProfile]:
        # Fetch all, filter in Python — avoids Beanie expression edge cases with None/bool
        all_docs = await AgentProfileDocument.find().to_list()
        return [
            doc.to_domain()
            for doc in all_docs
            if (
                doc.scope == "global"
                or doc.user_id == user_id
                or doc.owner_user_id == user_id
                or doc.workspace_id == personal_workspace_id(user_id)
            ) and doc.is_active
        ]

    async def update(self, profile: AgentProfile) -> AgentProfile:
        doc = await AgentProfileDocument.find_one(AgentProfileDocument.profile_id == profile.id)
        if not doc:
            raise ValueError(f"AgentProfile not found: {profile.id}")
        doc.update_from_domain(profile)
        await doc.save()
        return doc.to_domain()

    async def delete(self, profile_id: str) -> None:
        doc = await AgentProfileDocument.find_one(AgentProfileDocument.profile_id == profile_id)
        if doc:
            await doc.delete()
