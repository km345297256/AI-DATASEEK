from datetime import UTC, date, datetime
from typing import Optional

from app.application.errors.exceptions import BadRequestError
from app.domain.models.user import UserRole
from app.infrastructure.models.documents import RoleTokenQuotaDocument, UserDocument


DEFAULT_ROLE_TOKEN_QUOTAS = {
    UserRole.ADMIN: {"initial_tokens": None, "daily_refill_tokens": None},
    UserRole.USER: {"initial_tokens": None, "daily_refill_tokens": None},
    UserRole.SOFTWARE: {"initial_tokens": None, "daily_refill_tokens": None},
}


class TokenQuotaService:
    BYPASS_USER_IDS = {"anonymous", "local_admin"}

    async def get_role_quota(self, role: UserRole) -> RoleTokenQuotaDocument:
        doc = await RoleTokenQuotaDocument.find_one(RoleTokenQuotaDocument.role == role)
        if doc:
            return doc
        defaults = DEFAULT_ROLE_TOKEN_QUOTAS[role]
        doc = RoleTokenQuotaDocument(
            role=role,
            initial_tokens=defaults["initial_tokens"],
            daily_refill_tokens=defaults["daily_refill_tokens"],
        )
        await doc.insert()
        return doc

    async def list_role_quotas(self) -> list[RoleTokenQuotaDocument]:
        return [await self.get_role_quota(role) for role in UserRole]

    async def update_role_quota(
        self,
        role: UserRole,
        *,
        initial_tokens: Optional[int],
        daily_refill_tokens: Optional[int],
    ) -> RoleTokenQuotaDocument:
        if (initial_tokens is not None and initial_tokens < 0) or (
            daily_refill_tokens is not None and daily_refill_tokens < 0
        ):
            raise BadRequestError("Token quotas must be non-negative")
        doc = await self.get_role_quota(role)
        doc.initial_tokens = initial_tokens
        doc.daily_refill_tokens = daily_refill_tokens
        doc.updated_at = datetime.now(UTC)
        await doc.save()
        return doc

    async def initialize_user_quota(self, user: UserDocument) -> None:
        quota = await self.get_role_quota(user.role)
        user.token_balance = quota.initial_tokens
        user.token_last_refill_date = date.today()
        user.updated_at = datetime.now(UTC)
        await user.save()

    async def apply_daily_refill(self, user: UserDocument) -> UserDocument:
        today = date.today()
        if user.token_last_refill_date == today:
            return user
        if user.token_balance is None:
            user.token_last_refill_date = today
            user.updated_at = datetime.now(UTC)
            await user.save()
            return user
        quota = await self.get_role_quota(user.role)
        daily_refill = (
            user.token_daily_refill_override
            if user.token_daily_refill_override is not None
            else quota.daily_refill_tokens
        )
        if daily_refill is None:
            user.token_balance = None
        elif daily_refill > 0:
            user.token_balance = max(0, user.token_balance or 0) + daily_refill
        user.token_last_refill_date = today
        user.updated_at = datetime.now(UTC)
        await user.save()
        return user

    async def ensure_user_can_run_task(self, user_id: str) -> Optional[UserDocument]:
        """Compatibility hook: task execution is intentionally never quota-gated."""
        return None

    async def consume_user_tokens(self, user_id: Optional[str], amount: int) -> None:
        """Compatibility hook: usage is recorded, but balances are never deducted."""
        return None

    async def update_user_quota(
        self,
        user_id: str,
        *,
        token_balance: Optional[int] = None,
        token_daily_refill_override: Optional[int] = None,
        set_unlimited_balance: bool = False,
        clear_daily_refill_override: bool = False,
    ) -> UserDocument:
        user = await UserDocument.find_one(UserDocument.user_id == user_id)
        if not user:
            raise BadRequestError("User not found")
        user = await self.apply_daily_refill(user)
        if set_unlimited_balance:
            user.token_balance = None
        elif token_balance is not None:
            if token_balance < 0:
                raise BadRequestError("Token balance must be non-negative")
            user.token_balance = token_balance
        if clear_daily_refill_override:
            user.token_daily_refill_override = None
        elif token_daily_refill_override is not None:
            if token_daily_refill_override < 0:
                raise BadRequestError("Daily refill must be non-negative")
            user.token_daily_refill_override = token_daily_refill_override
        user.updated_at = datetime.now(UTC)
        await user.save()
        return user


def get_token_quota_service() -> TokenQuotaService:
    return TokenQuotaService()
