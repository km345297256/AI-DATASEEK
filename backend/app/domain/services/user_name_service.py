from typing import Iterable

from app.infrastructure.models.documents import UserDocument


class UserNameService:
    @staticmethod
    async def resolve(user_ids: Iterable[str]) -> dict[str, str]:
        ids = {user_id for user_id in user_ids if user_id}
        if not ids:
            return {}
        users = await UserDocument.find({"user_id": {"$in": list(ids)}}).to_list()
        return {user.user_id: user.fullname for user in users}

