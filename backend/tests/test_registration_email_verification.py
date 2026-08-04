import pytest

from app.application.errors.exceptions import BadRequestError, ForbiddenError, UnauthorizedError, ValidationError
from app.application.services.auth_service import AuthService
from app.application.services.email_service import EmailService
from app.domain.models.user import RegistrationStatus
from app.infrastructure.models.documents import UserDocument
from app.interfaces.api.auth_routes import _build_email_verification_url


class FakeUserRepository:
    def __init__(self):
        self.users = {}

    async def create_user(self, user):
        self.users[user.id] = user
        return user

    async def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    async def get_user_by_fullname(self, fullname):
        return next((user for user in self.users.values() if user.fullname == fullname), None)

    async def get_user_by_email(self, email):
        return next((user for user in self.users.values() if user.email == email.lower()), None)

    async def update_user(self, user):
        self.users[user.id] = user
        return user

    async def delete_user(self, user_id):
        return self.users.pop(user_id, None) is not None

    async def list_users(self, limit=100, offset=0):
        return list(self.users.values())[offset:offset + limit]

    async def fullname_exists(self, fullname):
        return any(user.fullname == fullname for user in self.users.values())

    async def email_exists(self, email):
        return any(user.email == email.lower() for user in self.users.values())


class FakeTokenService:
    pass


class FakeCache:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ttl=None):
        self.values[key] = value
        return True

    async def delete(self, key):
        return self.values.pop(key, None) is not None

    async def keys(self, pattern):
        prefix = pattern.rstrip("*")
        return [key for key in self.values if key.startswith(prefix)]


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", "test")
    monkeypatch.setenv("AUTH_PROVIDER", "password")
    monkeypatch.setenv("PASSWORD_SALT", "test-salt")
    from app.core.config import get_settings

    async def find_no_persisted_document(*args, **kwargs):
        return None

    monkeypatch.setattr(UserDocument, "find_one", find_no_persisted_document)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_register_user_creates_pending_inactive_user():
    auth_service = AuthService(FakeUserRepository(), FakeTokenService())

    user = await auth_service.register_user(
        fullname="Test User",
        email="Test@Example.com",
        password="password123",
    )

    assert user.email == "test@example.com"
    assert user.is_active is False
    assert user.registration_status == RegistrationStatus.PENDING


@pytest.mark.asyncio
async def test_register_user_rejects_duplicate_email_even_when_inactive():
    repo = FakeUserRepository()
    auth_service = AuthService(repo, FakeTokenService())
    await auth_service.register_user(
        fullname="First User",
        email="same@example.com",
        password="password123",
    )

    with pytest.raises(ValidationError, match="Email already exists"):
        await auth_service.register_user(
            fullname="Second User",
            email="SAME@example.com",
            password="password123",
        )


@pytest.mark.asyncio
async def test_legacy_email_verification_does_not_activate_user():
    repo = FakeUserRepository()
    auth_service = AuthService(repo, FakeTokenService())
    user = await auth_service.register_user(
        fullname="Verify User",
        email="verify@example.com",
        password="password123",
    )

    verified_user = await auth_service.verify_registered_user(user.id, "verify@example.com")

    assert verified_user.is_active is False
    assert verified_user.registration_status == RegistrationStatus.PENDING
    assert (await repo.get_user_by_id(user.id)).is_active is False


@pytest.mark.asyncio
async def test_verify_registered_user_rejects_mismatched_email():
    auth_service = AuthService(FakeUserRepository(), FakeTokenService())
    user = await auth_service.register_user(
        fullname="Verify User",
        email="verify@example.com",
        password="password123",
    )

    with pytest.raises(UnauthorizedError):
        await auth_service.verify_registered_user(user.id, "other@example.com")


@pytest.mark.asyncio
async def test_pending_registration_cannot_log_in():
    repo = FakeUserRepository()
    auth_service = AuthService(repo, FakeTokenService())
    await auth_service.register_user(
        fullname="Pending User",
        email="pending@example.com",
        password="password123",
    )

    with pytest.raises(ForbiddenError, match="pending administrator approval"):
        await auth_service.authenticate_user("pending@example.com", "password123")


@pytest.mark.asyncio
async def test_approved_registration_can_log_in():
    repo = FakeUserRepository()
    auth_service = AuthService(repo, FakeTokenService())
    user = await auth_service.register_user(
        fullname="Approved User",
        email="approved@example.com",
        password="password123",
    )

    approved = await auth_service.decide_registration(
        user.id,
        RegistrationStatus.APPROVED,
        reviewer_user_id="admin-1",
        note="Identity confirmed",
    )
    authenticated = await auth_service.authenticate_user("approved@example.com", "password123")

    assert approved.is_active is True
    assert approved.registration_reviewed_by == "admin-1"
    assert approved.registration_review_note == "Identity confirmed"
    assert authenticated is not None
    assert authenticated.id == user.id


@pytest.mark.asyncio
async def test_rejected_registration_cannot_log_in_or_be_reviewed_twice():
    repo = FakeUserRepository()
    auth_service = AuthService(repo, FakeTokenService())
    user = await auth_service.register_user(
        fullname="Rejected User",
        email="rejected@example.com",
        password="password123",
    )

    rejected = await auth_service.decide_registration(
        user.id,
        RegistrationStatus.REJECTED,
        reviewer_user_id="admin-1",
        note="Information incomplete",
    )

    assert rejected.is_active is False
    with pytest.raises(ForbiddenError, match="rejected"):
        await auth_service.authenticate_user("rejected@example.com", "password123")
    with pytest.raises(BadRequestError, match="already been reviewed"):
        await auth_service.decide_registration(
            user.id,
            RegistrationStatus.APPROVED,
            reviewer_user_id="admin-2",
        )


@pytest.mark.asyncio
async def test_email_verification_token_is_single_use():
    email_service = EmailService(FakeCache())

    token = await email_service.create_email_verification_token("User@Example.com", "user-1")
    first_result = await email_service.consume_email_verification_token(token)
    second_result = await email_service.consume_email_verification_token(token)

    assert first_result == {"email": "user@example.com", "user_id": "user-1", "created_at": first_result["created_at"]}
    assert second_result is None


def test_email_service_accepts_smtp_alias_settings(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")

    from app.core.config import get_settings

    get_settings.cache_clear()
    email_service = EmailService(FakeCache())

    assert email_service._missing_smtp_fields() == []
    assert email_service.smtp_host == "smtp.example.com"
    assert email_service.smtp_port == 587
    assert email_service.smtp_username == "sender@example.com"
    assert email_service.smtp_from == "sender@example.com"


def test_email_verification_url_uses_server_host(monkeypatch):
    monkeypatch.setenv("SERVER_HOST", "https://fair.example.com/")

    from app.core.config import get_settings

    get_settings.cache_clear()

    assert (
        _build_email_verification_url("token-123")
        == "https://fair.example.com/api/v1/auth/verify-email?token=token-123"
    )
