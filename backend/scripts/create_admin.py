"""Idempotently create or repair the first AI-DataSeek administrator.

Prefer environment variables so the password does not appear in shell history
or the process list. The script never logs or prints the password.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import secrets
import sys

from beanie import init_beanie


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.application.services.auth_service import AuthService  # noqa: E402
from app.application.services.token_service import TokenService  # noqa: E402
from app.domain.models.user import RegistrationStatus, User, UserRole  # noqa: E402
from app.domain.services.token_quota_service import TokenQuotaService  # noqa: E402
from app.infrastructure.models.documents import (  # noqa: E402
    RoleTokenQuotaDocument,
    UserDocument,
)
from app.infrastructure.repositories.user_repository import MongoUserRepository  # noqa: E402
from app.infrastructure.storage.mongodb import get_mongodb  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or repair an active approved administrator account.",
    )
    parser.add_argument(
        "--email",
        default=os.getenv("AI_DATASEEK_ADMIN_EMAIL"),
        help="Administrator email (or AI_DATASEEK_ADMIN_EMAIL).",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("AI_DATASEEK_ADMIN_PASSWORD"),
        help=(
            "Administrator password (or AI_DATASEEK_ADMIN_PASSWORD). "
            "Prefer the environment variable to avoid shell history."
        ),
    )
    parser.add_argument(
        "--fullname",
        default=os.getenv("AI_DATASEEK_ADMIN_FULLNAME", "AI-DataSeek Administrator"),
        help="Display name (or AI_DATASEEK_ADMIN_FULLNAME).",
    )
    return parser


def _validated_args() -> argparse.Namespace:
    args = _parser().parse_args()
    args.email = (args.email or "").strip().lower()
    args.fullname = (args.fullname or "").strip()
    if not args.email or "@" not in args.email:
        raise SystemExit("A valid --email or AI_DATASEEK_ADMIN_EMAIL is required")
    if not args.password or len(args.password) < 6:
        raise SystemExit(
            "A password of at least 6 characters is required via --password "
            "or AI_DATASEEK_ADMIN_PASSWORD"
        )
    if len(args.fullname) < 2:
        raise SystemExit("Administrator fullname must contain at least 2 characters")
    return args


async def _create_or_update(args: argparse.Namespace) -> tuple[str, str]:
    mongodb = get_mongodb()
    await mongodb.initialize()
    try:
        from app.core.config import get_settings

        settings = get_settings()
        await init_beanie(
            database=mongodb.client[settings.mongodb_database],
            document_models=[UserDocument, RoleTokenQuotaDocument],
        )

        repository = MongoUserRepository()
        auth_service = AuthService(repository, TokenService())
        password_hash = auth_service._hash_password(args.password)
        now = datetime.now(UTC)
        existing = await UserDocument.find_one(UserDocument.email == args.email)
        if existing:
            existing.fullname = args.fullname
            existing.password_hash = password_hash
            existing.role = UserRole.ADMIN
            existing.is_active = True
            existing.registration_status = RegistrationStatus.APPROVED
            existing.registration_reviewed_by = existing.user_id
            existing.registration_reviewed_at = now
            existing.registration_review_note = "Initialized by create_admin.py"
            existing.updated_at = now
            await existing.save()
            await TokenQuotaService().apply_daily_refill(existing)
            return "updated", existing.user_id

        user = User(
            id=f"admin_{secrets.token_urlsafe(12)}",
            fullname=args.fullname,
            email=args.email,
            password_hash=password_hash,
            role=UserRole.ADMIN,
            is_active=True,
            registration_status=RegistrationStatus.APPROVED,
            created_at=now,
            updated_at=now,
        )
        created = await repository.create_user(user)
        created_doc = await UserDocument.find_one(UserDocument.user_id == created.id)
        if created_doc is None:
            raise RuntimeError("Administrator was created but could not be reloaded")
        created_doc.registration_reviewed_by = created_doc.user_id
        created_doc.registration_reviewed_at = now
        created_doc.registration_review_note = "Initialized by create_admin.py"
        await created_doc.save()
        await TokenQuotaService().initialize_user_quota(created_doc)
        return "created", created.id
    finally:
        await mongodb.shutdown()


def main() -> None:
    args = _validated_args()
    action, user_id = asyncio.run(_create_or_update(args))
    print(f"Administrator {action}: email={args.email} user_id={user_id}")


if __name__ == "__main__":
    main()
