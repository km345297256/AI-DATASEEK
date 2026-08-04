import hashlib
import secrets
from typing import Optional
from datetime import datetime, UTC
from app.domain.models.user import User, UserRole, RegistrationStatus
from app.domain.repositories.user_repository import UserRepository
from app.application.errors.exceptions import UnauthorizedError, ValidationError, BadRequestError, ForbiddenError
from app.core.config import get_settings
from app.application.services.token_service import TokenService
from app.domain.models.auth import AuthToken
from app.domain.services.token_quota_service import TokenQuotaService
from app.infrastructure.models.documents import UserDocument
import logging

logger = logging.getLogger(__name__)


class AuthService:
    """Authentication service handling user authentication and authorization"""
    
    def __init__(self, user_repository: UserRepository, token_service: TokenService):
        self.user_repository = user_repository
        self.settings = get_settings()
        self.token_service = token_service
    
    def _hash_password(self, password: str) -> str:
        """Hash password using configured algorithm"""
        salt = self.settings.password_salt or ''
        
        return self._pbkdf2_sha256(password, salt)
    
    def _pbkdf2_sha256(self, password: str, salt: str) -> str:
        """PBKDF2 with SHA-256 implementation"""
        password_bytes = password.encode('utf-8')
        salt_bytes = salt.encode('utf-8')
        
        # Use configured rounds
        rounds = self.settings.password_hash_rounds or 10
        
        # Generate hash
        hash_bytes = hashlib.pbkdf2_hmac('sha256', password_bytes, salt_bytes, rounds)
        
        # Return salt + hash as hex string
        return salt + hash_bytes.hex()
    
    def _verify_password(self, password: str, password_hash: str) -> bool:
        """Verify password against hash"""
        if not password_hash:
            return False
        
        try:
            # Generate hash with extracted salt
            generated_hash = self._hash_password(password)

            return generated_hash == password_hash
        except Exception as e:
            logger.error(f"Password verification error: {e}")
            return False
    
    def _generate_user_id(self) -> str:
        """Generate unique user ID"""
        return secrets.token_urlsafe(16)
    
    async def register_user(self, fullname: str, password: str, email: str, role: UserRole = UserRole.USER) -> User:
        """Register a new user"""
        logger.info(f"Registering user: {email}")

        if self.settings.auth_provider != "password":
            raise BadRequestError("Registration is not allowed")
        
        # Validate input
        if not fullname or len(fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        
        if not email or '@' not in email:
            raise ValidationError("Valid email is required")
        
        if not password or len(password) < 6:
            raise ValidationError("Password must be at least 6 characters long")
        
        # Check if email already exists
        if await self.user_repository.email_exists(email):
            raise ValidationError("Email already exists")
        
        # Hash password
        password_hash = self._hash_password(password)
        
        # Create user
        user = User(
            id=self._generate_user_id(),
            fullname=fullname.strip(),
            email=email.lower(),
            password_hash=password_hash,
            role=role,
            is_active=False,
            registration_status=RegistrationStatus.PENDING,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Save to database
        created_user = await self.user_repository.create_user(user)
        user_doc = await UserDocument.find_one({"user_id": created_user.id})
        if user_doc:
            await TokenQuotaService().initialize_user_quota(user_doc)
            created_user = user_doc.to_domain()
        
        logger.info(f"User registered successfully: {created_user.id}")
        return created_user

    async def verify_registered_user(self, user_id: str, email: str) -> User:
        """Validate a legacy verification token without bypassing approval."""
        user = await self.user_repository.get_user_by_id(user_id)
        if not user or user.email != email.lower():
            raise UnauthorizedError("Invalid or expired verification link")
        return user

    async def decide_registration(
        self,
        user_id: str,
        status: RegistrationStatus,
        reviewer_user_id: str,
        note: Optional[str] = None,
    ) -> User:
        """Approve or reject a pending registration request."""
        if status not in {RegistrationStatus.APPROVED, RegistrationStatus.REJECTED}:
            raise ValidationError("Registration decision must be approved or rejected")

        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        if user.registration_status != RegistrationStatus.PENDING:
            raise BadRequestError("Registration request has already been reviewed")

        user.registration_status = status
        user.is_active = status == RegistrationStatus.APPROVED
        user.registration_reviewed_by = reviewer_user_id
        user.registration_reviewed_at = datetime.now(UTC)
        user.registration_review_note = (note or "").strip() or None
        user.updated_at = datetime.now(UTC)
        return await self.user_repository.update_user(user)
    
    async def authenticate_user(self, email: str, password: str) -> Optional[User]:
        """Authenticate user by email and password"""
        logger.debug(f"Authenticating user: {email}")
        
        # Handle different auth providers
        if self.settings.auth_provider == "none":
            # No authentication required - return a default user
            return User(
                id="anonymous",
                fullname="anonymous",
                email="anonymous@localhost",
                role=UserRole.USER,
                is_active=True
            )
        
        elif self.settings.auth_provider == "local":
            # Local authentication using configured credentials
            if (email == self.settings.local_auth_email and 
                password == self.settings.local_auth_password):
                return User(
                    id="local_admin",
                    fullname="Local Admin",
                    email=email,
                    role=UserRole.ADMIN,
                    is_active=True
                )
            else:
                logger.warning(f"Local authentication failed for user: {email}")
                return None
        
        elif self.settings.auth_provider == "password":
            # Database password authentication
            user = await self.user_repository.get_user_by_email(email)
            if not user:
                logger.warning(f"User not found: {email}")
                return None
            
            if not user.password_hash:
                logger.warning(f"User has no password hash: {email}")
                return None
            
            # Verify password
            if not self._verify_password(password, user.password_hash):
                logger.warning(f"Invalid password for user: {email}")
                return None

            if user.registration_status == RegistrationStatus.PENDING:
                raise ForbiddenError("Registration is pending administrator approval")
            if user.registration_status == RegistrationStatus.REJECTED:
                raise ForbiddenError("Registration request was rejected by an administrator")
            if not user.is_active:
                logger.warning(f"User account is inactive: {email}")
                raise ForbiddenError("User account is inactive")
            
            # Update last login
            user.update_last_login()
            await self.user_repository.update_user(user)
            
            logger.info(f"User authenticated successfully: {email}")
            return user
        
        else:
            raise ValueError(f"Unsupported auth provider: {self.settings.auth_provider}")
    
    async def login_with_tokens(self, email: str, password: str) -> AuthToken:
        """Authenticate user and return JWT tokens"""
        user = await self.authenticate_user(email, password)
        
        if not user:
            raise UnauthorizedError("Invalid email or password")
        
        # Generate JWT tokens
        access_token = self.token_service.create_access_token(user)
        refresh_token = self.token_service.create_refresh_token(user)
        
        return AuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            user=user
        )
    
    async def refresh_access_token(self, refresh_token: str) -> AuthToken:
        """Refresh access token using refresh token"""
        payload = self.token_service.verify_token(refresh_token)
        
        if not payload:
            raise UnauthorizedError("Invalid refresh token")
        
        if payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid token type")
        
        # Get user from database
        user_id = payload.get("sub")
        user = await self.user_repository.get_user_by_id(user_id)
        
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or inactive")
        
        # Generate new access token
        new_access_token = self.token_service.create_access_token(user)
        
        return AuthToken(
            access_token=new_access_token,
            token_type="bearer"
        )
    
    async def verify_token(self, token: str) -> Optional[User]:
        """Verify JWT token and return user"""
        user_info = self.token_service.get_user_from_token(token)
        
        if not user_info:
            return None
        
        # For database users, verify user still exists and is active
        if self.settings.auth_provider == "password":
            user = await self.user_repository.get_user_by_id(user_info["id"])
            if not user or not user.is_active:
                return None
            return user
        
        # For local/none authentication, create user from token info
        return User(
            id=user_info["id"],
            fullname=user_info["fullname"],
            email=user_info.get("email"),
            role=UserRole(user_info.get("role", "user")),
            is_active=user_info.get("is_active", True)
        )
    
    async def logout(self, token: str) -> bool:
        """Logout user by revoking token"""
        if self.settings.auth_provider == "none":
            raise BadRequestError("Logout is not allowed")
        return self.token_service.revoke_token(token)
    
    async def change_password(self, user_id: str, old_password: str, new_password: str) -> bool:
        """Change user password"""
        logger.info(f"Changing password for user: {user_id}")
        
        # Get user
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        
        # Verify old password
        if not user.password_hash or not self._verify_password(old_password, user.password_hash):
            raise UnauthorizedError("Invalid old password")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = self._hash_password(new_password)
        
        # Update user password
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        
        await self.user_repository.update_user(user)
        
        logger.info(f"Password changed successfully for user: {user_id}")
        return True
    
    async def change_fullname(self, user_id: str, new_fullname: str) -> User:
        """Change user fullname"""
        logger.info(f"Changing fullname for user: {user_id}")
        
        # Get user
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        
        # Validate new fullname
        if not new_fullname or len(new_fullname.strip()) < 2:
            raise ValidationError("Full name must be at least 2 characters long")
        
        # Update user fullname
        user.fullname = new_fullname.strip()
        user.updated_at = datetime.utcnow()
        
        updated_user = await self.user_repository.update_user(user)
        
        logger.info(f"Fullname changed successfully for user: {user_id}")
        return updated_user
    
    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        return await self.user_repository.get_user_by_id(user_id)
    
    async def deactivate_user(self, user_id: str) -> bool:
        """Deactivate user account"""
        logger.info(f"Deactivating user: {user_id}")
        
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        
        user.deactivate()
        await self.user_repository.update_user(user)
        
        logger.info(f"User deactivated successfully: {user_id}")
        return True
    
    async def activate_user(self, user_id: str) -> bool:
        """Activate user account"""
        logger.info(f"Activating user: {user_id}")
        
        user = await self.user_repository.get_user_by_id(user_id)
        if not user:
            raise ValidationError("User not found")
        if user.registration_status != RegistrationStatus.APPROVED:
            raise BadRequestError("User registration must be approved before activation")
        
        user.activate()
        await self.user_repository.update_user(user)
        
        logger.info(f"User activated successfully: {user_id}")
        return True
    
    async def reset_password(self, email: str, new_password: str) -> bool:
        """Reset user password with email"""
        logger.info(f"Resetting password for user: {email}")
        
        if self.settings.auth_provider != "password":
            raise BadRequestError("Password reset is not allowed")
        
        # Get user by email
        user = await self.user_repository.get_user_by_email(email)
        if not user:
            raise ValidationError("User not found")
        
        if not user.is_active:
            raise UnauthorizedError("User account is inactive")
        
        # Validate new password
        if not new_password or len(new_password) < 6:
            raise ValidationError("New password must be at least 6 characters long")
        
        # Hash new password
        new_password_hash = self._hash_password(new_password)
        
        # Update user password
        user.password_hash = new_password_hash
        user.updated_at = datetime.utcnow()
        
        await self.user_repository.update_user(user)
        
        logger.info(f"Password reset successfully for user: {email}")
        return True 
