from typing import List, Optional

from app.domain.models.skill import Skill, SkillScope
from app.domain.models.workspace import personal_workspace_id
from app.domain.repositories.skill_repository import SkillRepository
from app.infrastructure.models.documents import SkillDocument


class MongoSkillRepository(SkillRepository):
    async def save(self, skill: Skill) -> Skill:
        existing = await SkillDocument.find_one(SkillDocument.skill_id == skill.id)
        if not existing:
            existing = await SkillDocument.find_one(SkillDocument.path == skill.path)
        if existing:
            existing.update_from_domain(skill)
            await existing.save()
            return existing.to_domain()

        doc = SkillDocument.from_domain(skill)
        await doc.create()
        return doc.to_domain()

    async def list_accessible(self, user_id: str) -> List[Skill]:
        docs = await SkillDocument.find().to_list()
        skills = [
            doc.to_domain()
            for doc in docs
            if (
                doc.scope == SkillScope.GLOBAL
                or doc.user_id == user_id
                or doc.owner_user_id == user_id
                or doc.workspace_id == personal_workspace_id(user_id)
            )
        ]
        return sorted(skills, key=lambda skill: (skill.scope != SkillScope.GLOBAL, skill.name))

    async def get_accessible_by_name(self, name: str, user_id: str) -> Optional[Skill]:
        normalized = name.strip().lower()
        for skill in await self.list_accessible(user_id):
            if skill.name.lower() == normalized:
                return skill
        return None
