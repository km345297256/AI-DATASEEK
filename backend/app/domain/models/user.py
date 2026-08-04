from typing import Optional
from datetime import date, datetime, UTC
from pydantic import BaseModel, Field, field_validator, EmailStr
from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"
    SOFTWARE = "software"


class RegistrationStatus(str, Enum):
    """Lifecycle of a password-authenticated registration request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class User(BaseModel):
    """User domain model"""
    id: str
    fullname: str
    email: str  # Now required field for login
    password_hash: Optional[str] = None
    role: UserRole = UserRole.USER
    is_active: bool = True
    # Existing documents without this field are treated as approved.
    registration_status: RegistrationStatus = RegistrationStatus.APPROVED
    registration_reviewed_by: Optional[str] = None
    registration_reviewed_at: Optional[datetime] = None
    registration_review_note: Optional[str] = None
    token_balance: Optional[int] = 0
    token_daily_refill_override: Optional[int] = None
    token_last_refill_date: Optional[date] = None
    auto_enabled_skills: list[str] = Field(default_factory=list)
    installed_skill_ids: list[str] = Field(default_factory=list)
    installed_mcp_names: list[str] = Field(default_factory=list)
    installed_renderer_ids: list[str] = Field(default_factory=list)
    created_at: datetime = datetime.now(UTC)
    updated_at: datetime = datetime.now(UTC)
    last_login_at: Optional[datetime] = None
    
    @field_validator('fullname')
    @classmethod
    def validate_fullname(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError("Full name must be at least 2 characters long")
        return v.strip()
    
    @field_validator('email')
    @classmethod
    def validate_email(cls, v):
        if not v or '@' not in v:
            raise ValueError("Valid email is required")
        return v.strip().lower()
    
    def update_last_login(self):
        """Update last login timestamp"""
        self.last_login_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)
    
    def deactivate(self):
        """Deactivate user account"""
        self.is_active = False
        self.updated_at = datetime.now(UTC)
    
    def activate(self):
        """Activate user account"""
        self.is_active = True
        self.updated_at = datetime.now(UTC) 
