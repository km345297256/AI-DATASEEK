from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging

from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.application.services.file_service import FileService
from app.application.services.agent_service import AgentService
from app.application.services.email_service import EmailService
from app.application.errors.exceptions import (
    UnauthorizedError, NotFoundError, BadRequestError
)
from app.interfaces.dependencies import get_auth_service, get_current_user, get_file_service, get_agent_service, get_token_service, get_email_service
from app.interfaces.schemas.base import APIResponse
from app.interfaces.schemas.auth import (
    LoginRequest, RegisterRequest, ChangePasswordRequest, ChangeFullnameRequest, RefreshTokenRequest,
    SendVerificationCodeRequest, ResetPasswordRequest,
    LoginResponse, RegisterResponse, AuthStatusResponse, RefreshTokenResponse,
    UserResponse
)
from app.core.config import get_settings
from app.domain.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _email_verification_page(title: str, message: str, is_success: bool) -> str:
    color = "#16a34a" if is_success else "#dc2626"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{title}</title>
        <style>
          body {{
            margin: 0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            background: #f4f4f0;
            color: #171717;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          }}
          .card {{
            width: min(520px, calc(100vw - 32px));
            padding: 32px;
            border-radius: 20px;
            background: #ffffff;
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.08);
            text-align: center;
          }}
          .status {{
            color: {color};
            font-size: 16px;
            font-weight: 700;
            letter-spacing: .08em;
            text-transform: uppercase;
          }}
          h1 {{
            margin: 16px 0 12px;
            font-size: 28px;
          }}
          p {{
            margin: 0;
            color: #555;
            line-height: 1.7;
          }}
        </style>
      </head>
      <body>
        <main class="card">
          <div class="status">AI-DataSeek</div>
          <h1>{title}</h1>
          <p>{message}</p>
        </main>
      </body>
    </html>
    """


def _build_email_verification_url(token: str) -> str:
    settings = get_settings()
    if not settings.server_host:
        raise BadRequestError("SERVER_HOST is required for email verification links")
    return f"{settings.server_host.rstrip('/')}/api/v1/auth/verify-email?token={token}"



@router.post("/login", response_model=APIResponse[LoginResponse])
async def login(
    request: LoginRequest,
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[LoginResponse]:
    """User login endpoint"""
    # Authenticate user and get tokens
    auth_result = await auth_service.login_with_tokens(request.email, request.password)
    
    # Return success response with tokens
    return APIResponse.success(LoginResponse(
        user=UserResponse.from_user(auth_result.user),
        access_token=auth_result.access_token,
        refresh_token=auth_result.refresh_token,
        token_type=auth_result.token_type
    ))


@router.post("/register", response_model=APIResponse[RegisterResponse])
async def register(
    request: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> APIResponse[RegisterResponse]:
    """Submit a user registration request for administrator review."""
    user = await auth_service.register_user(
        fullname=request.fullname,
        password=request.password,
        email=request.email
    )

    return APIResponse.success(RegisterResponse(
        user=UserResponse.from_user(user),
        verification_required=False,
        message="Registration submitted. Please wait for administrator approval before logging in."
    ))


@router.get("/verify-email", name="verify_email", response_class=HTMLResponse)
async def verify_email(
    token: str,
    auth_service: AuthService = Depends(get_auth_service),
    email_service: EmailService = Depends(get_email_service)
) -> HTMLResponse:
    """Handle legacy verification links without activating an account."""
    token_data = await email_service.consume_email_verification_token(token)
    if not token_data:
        return HTMLResponse(
            content=_email_verification_page(
                title="Verification link unavailable",
                message="Email verification is no longer used for registration. Please submit a registration request.",
                is_success=False,
            ),
            status_code=400,
        )

    try:
        await auth_service.verify_registered_user(token_data["user_id"], token_data["email"])
    except Exception:
        logger.exception("Failed to verify registered user from email token")
        return HTMLResponse(
            content=_email_verification_page(
                title="Verification link unavailable",
                message="Email verification is no longer used for registration. Please submit a registration request.",
                is_success=False,
            ),
            status_code=400,
        )

    return HTMLResponse(
        content=_email_verification_page(
            title="Registration pending",
            message="Your email was recognized, but an administrator must approve your registration before you can log in.",
            is_success=True,
        )
    )


@router.get("/status", response_model=APIResponse[AuthStatusResponse])
async def get_auth_status(
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[AuthStatusResponse]:
    """Get authentication status and configuration"""
    settings = get_settings()
    
    return APIResponse.success(AuthStatusResponse(
        auth_provider=settings.auth_provider
    ))


@router.post("/change-password", response_model=APIResponse[dict])
async def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    """Change user password endpoint"""
    # Change password for current user
    await auth_service.change_password(current_user.id, request.old_password, request.new_password)
    
    return APIResponse.success({})


@router.post("/change-fullname", response_model=APIResponse[UserResponse])
async def change_fullname(
    request: ChangeFullnameRequest,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[UserResponse]:
    """Change user fullname endpoint"""
    # Change fullname for current user
    updated_user = await auth_service.change_fullname(current_user.id, request.fullname)
    
    return APIResponse.success(UserResponse.from_user(updated_user))


@router.get("/me", response_model=APIResponse[UserResponse])
async def get_current_user_info(
    current_user: User = Depends(get_current_user)
) -> APIResponse[UserResponse]:
    """Get current user information"""
    return APIResponse.success(UserResponse.from_user(current_user))


@router.get("/user/{user_id}", response_model=APIResponse[UserResponse])
async def get_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[UserResponse]:
    """Get user information by ID (admin only)"""
    # Check if current user is admin
    if current_user.role != "admin":
        raise UnauthorizedError("Admin access required")
    
    user = await auth_service.get_user_by_id(user_id)
    
    if not user:
        raise NotFoundError("User not found")
    
    return APIResponse.success(UserResponse.from_user(user))


@router.post("/user/{user_id}/deactivate", response_model=APIResponse[dict])
async def deactivate_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    """Deactivate user account (admin only)"""
    # Check if current user is admin
    if current_user.role != "admin":
        raise UnauthorizedError("Admin access required")
    
    # Prevent self-deactivation
    if current_user.id == user_id:
        raise BadRequestError("Cannot deactivate your own account")
    
    await auth_service.deactivate_user(user_id)
    return APIResponse.success({})


@router.post("/user/{user_id}/activate", response_model=APIResponse[dict])
async def activate_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    """Activate user account (admin only)"""
    # Check if current user is admin
    if current_user.role != "admin":
        raise UnauthorizedError("Admin access required")
    
    await auth_service.activate_user(user_id)
    return APIResponse.success({})


@router.post("/refresh", response_model=APIResponse[RefreshTokenResponse])
async def refresh_token(
    request: RefreshTokenRequest,
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[RefreshTokenResponse]:
    """Refresh access token endpoint"""
    # Refresh access token
    token_result = await auth_service.refresh_access_token(request.refresh_token)
    
    return APIResponse.success(RefreshTokenResponse(
        access_token=token_result.access_token,
        token_type=token_result.token_type
    ))


@router.post("/logout", response_model=APIResponse[dict])
async def logout(
    current_user: User = Depends(get_current_user),
    bearer_credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    auth_service: AuthService = Depends(get_auth_service)
) -> APIResponse[dict]:
    """User logout endpoint"""
    if get_settings().auth_provider == "none":
        raise BadRequestError("Logout is not allowed")
    
    # Revoke token
    await auth_service.logout(bearer_credentials.credentials)
    
    return APIResponse.success({})


@router.post("/send-verification-code", response_model=APIResponse[dict])
async def send_verification_code(
    request: SendVerificationCodeRequest,
    auth_service: AuthService = Depends(get_auth_service),
    email_service: EmailService = Depends(get_email_service)
) -> APIResponse[dict]:
    """Send verification code for password reset"""
    if get_settings().auth_provider != "password":
        raise BadRequestError("Password reset is not available")
    
    # Check if user exists with this email
    user = await auth_service.user_repository.get_user_by_email(request.email)
    if not user:
        raise NotFoundError("User not found")
    
    if not user.is_active:
        raise BadRequestError("User account is inactive")
    
    # Send verification code
    await email_service.send_verification_code(request.email)
    
    return APIResponse.success({})


@router.post("/reset-password", response_model=APIResponse[dict])
async def reset_password(
    request: ResetPasswordRequest,
    auth_service: AuthService = Depends(get_auth_service),
    email_service: EmailService = Depends(get_email_service)
) -> APIResponse[dict]:
    """Reset password with verification code"""
    if get_settings().auth_provider != "password":
        raise BadRequestError("Password reset is not available")
    
    # Verify the verification code
    if not await email_service.verify_code(request.email, request.verification_code):
        raise UnauthorizedError("Invalid or expired verification code")
    
    # Reset password
    await auth_service.reset_password(request.email, request.new_password)
    
    return APIResponse.success({})
 
