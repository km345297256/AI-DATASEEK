from typing import List, Optional

from app.domain.models.renderer import Renderer, RendererScope
from app.domain.models.workspace import personal_workspace_id
from app.domain.repositories.renderer_repository import RendererRepository
from app.infrastructure.models.documents import RendererDocument


class MongoRendererRepository(RendererRepository):
    async def save(self, renderer: Renderer) -> Renderer:
        doc = await RendererDocument.find_one(RendererDocument.renderer_id == renderer.id)
        if doc:
            doc.update_from_domain(renderer)
            await doc.save()
            return doc.to_domain()
        doc = RendererDocument.from_domain(renderer)
        await doc.insert()
        return doc.to_domain()

    async def list_accessible(self, user_id: str) -> List[Renderer]:
        docs = await RendererDocument.find().to_list()
        return [
            doc.to_domain()
            for doc in docs
            if (
                doc.scope == RendererScope.GLOBAL
                or doc.user_id == user_id
                or doc.owner_user_id == user_id
                or doc.workspace_id == personal_workspace_id(user_id)
            )
        ]

    async def get_accessible_by_id(self, renderer_id: str, user_id: str) -> Optional[Renderer]:
        doc = await RendererDocument.find_one(RendererDocument.renderer_id == renderer_id)
        if not doc:
            return None
        if (
            doc.scope != RendererScope.GLOBAL
            and doc.user_id != user_id
            and doc.owner_user_id != user_id
            and doc.workspace_id != personal_workspace_id(user_id)
        ):
            return None
        return doc.to_domain()

    async def delete(self, renderer_id: str) -> None:
        doc = await RendererDocument.find_one(RendererDocument.renderer_id == renderer_id)
        if doc:
            await doc.delete()
