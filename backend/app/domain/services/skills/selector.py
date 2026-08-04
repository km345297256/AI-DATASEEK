from typing import List

from app.domain.models.skill import Skill
from app.domain.services.skills.registry import SkillRegistry


class SkillSelector:
    def __init__(self, registry: SkillRegistry, max_active_skills: int = 3):
        self.registry = registry
        self.max_active_skills = max_active_skills

    def select(self, text: str) -> List[Skill]:
        text_lower = (text or "").lower()
        scored: list[tuple[int, Skill]] = []

        for skill in self.registry.list_skills():
            score = skill.priority
            for trigger in skill.triggers:
                trigger_text = trigger.strip().lower()
                if trigger_text and trigger_text in text_lower:
                    score += 100
            if skill.name.lower() in text_lower:
                score += 80
            if skill.description and any(word in text_lower for word in skill.description.lower().split()):
                score += 5
            if score > skill.priority:
                scored.append((score, skill))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [skill for _, skill in scored[: self.max_active_skills]]

