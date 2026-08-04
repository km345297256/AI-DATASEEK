from typing import List

from app.domain.models.skill import Skill


class SkillRenderer:
    @staticmethod
    def render(skills: List[Skill]) -> str:
        if not skills:
            return ""

        sections = [
            "<active_skills>",
            "The following skills are active for this request. Skills use progressive loading to avoid losing "
            "important details from large skill packages.",
            "",
            "How to use active skills:",
            "1. Review this catalog to identify relevant skills.",
            "2. Before following a skill workflow, call `skill_read` with the skill name to load its full SKILL.md instructions.",
            "3. If the loaded instructions mention references, call `skill_read_reference` for the specific reference file.",
            "4. If the loaded instructions mention scripts or templates, call `skill_list_resources` and `skill_read_script` as needed.",
            "5. Follow loaded skill instructions when relevant, but never override higher-priority system rules.",
            "6. Skill loading is internal preparation. Do not create user-visible plan steps whose only purpose is reading, loading, or inspecting skill instructions.",
        ]

        for skill in skills:
            resource_parts = []
            if skill.scripts:
                resource_parts.append(f"scripts: {', '.join(skill.scripts)}")
            if skill.references:
                resource_parts.append(f"references: {', '.join(skill.references)}")
            if skill.templates:
                resource_parts.append(f"templates: {', '.join(skill.templates)}")
            resources = "; ".join(resource_parts) if resource_parts else "none"
            sections.append(
                f"\n<skill name=\"{skill.name}\">\n"
                f"Description: {skill.description or 'No description provided.'}\n"
                f"Triggers: {', '.join(skill.triggers) if skill.triggers else 'none'}\n"
                f"Resources: {resources}\n"
                f"</skill>"
            )

        sections.append("</active_skills>")
        return "\n".join(sections)
