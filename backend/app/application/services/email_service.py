import smtplib
import logging
import random
import asyncio
import secrets
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Dict
from app.core.config import get_settings
from app.domain.external.cache import Cache
from app.application.errors.exceptions import BadRequestError

logger = logging.getLogger(__name__)


class EmailService:
    """Email service for sending verification codes and notifications"""
    
    # Class variables
    VERIFICATION_CODE_PREFIX = "verification_code:"
    VERIFICATION_CODE_EXPIRY_SECONDS = 300  # 5 minutes
    EMAIL_VERIFICATION_PREFIX = "email_verification:"
    EMAIL_VERIFICATION_EXPIRY_SECONDS = 24 * 60 * 60
    
    def __init__(self, cache: Cache):
        self.settings = get_settings()
        self.cache = cache

    @property
    def smtp_host(self) -> Optional[str]:
        return self.settings.email_host or self.settings.smtp_host

    @property
    def smtp_port(self) -> Optional[int]:
        return self.settings.email_port or self.settings.smtp_port

    @property
    def smtp_username(self) -> Optional[str]:
        return self.settings.email_username or self.settings.smtp_username or self.settings.smtp_user

    @property
    def smtp_password(self) -> Optional[str]:
        return self.settings.email_password or self.settings.smtp_password

    @property
    def smtp_from(self) -> Optional[str]:
        return self.settings.email_from or self.settings.smtp_from or self.smtp_username

    def _missing_smtp_fields(self) -> list[str]:
        required_fields = {
            "EMAIL_HOST or SMTP_HOST": self.smtp_host,
            "EMAIL_PORT or SMTP_PORT": self.smtp_port,
            "EMAIL_USERNAME or SMTP_USERNAME or SMTP_USER": self.smtp_username,
            "EMAIL_PASSWORD or SMTP_PASSWORD": self.smtp_password,
        }
        return [name for name, value in required_fields.items() if not value]

    def _ensure_smtp_configured(self) -> None:
        missing_fields = self._missing_smtp_fields()
        if missing_fields:
            logger.error("Email configuration is incomplete. Missing: %s", ", ".join(missing_fields))
            raise BadRequestError(f"Email configuration is incomplete: {', '.join(missing_fields)}")
    
    def _generate_verification_code(self) -> str:
        """Generate 6-digit verification code"""
        return f"{random.randint(100000, 999999)}"

    def _generate_email_verification_token(self) -> str:
        return secrets.token_urlsafe(32)
    
    async def _store_verification_code(self, email: str, code: str) -> None:
        """Store verification code with expiration time in cache"""
        now = datetime.now()
        # Create verification code data
        code_data = {
            "code": code,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.VERIFICATION_CODE_EXPIRY_SECONDS)).isoformat(),
            "attempts": 0
        }
        
        # Store in cache with TTL
        key = f"{self.VERIFICATION_CODE_PREFIX}{email}"
        await self.cache.set(key, code_data, ttl=self.VERIFICATION_CODE_EXPIRY_SECONDS)
    
    async def verify_code(self, email: str, code: str) -> bool:
        """Verify if the provided code is valid for the email"""
        key = f"{self.VERIFICATION_CODE_PREFIX}{email}"
        
        # Get stored data from cache
        stored_data = await self.cache.get(key)
        if not stored_data:
            return False
        
        # Check if code has expired (cache TTL should handle this, but double-check)
        expires_at = datetime.fromisoformat(stored_data["expires_at"])
        if datetime.now() > expires_at:
            await self.cache.delete(key)
            return False
        
        # Check attempts limit (max 3 attempts)
        if stored_data["attempts"] >= 3:
            await self.cache.delete(key)
            return False
        
        # Increment attempt count
        stored_data["attempts"] += 1
        
        # Check if code matches
        if stored_data["code"] == code:
            # Remove the code after successful verification
            await self.cache.delete(key)
            return True
        
        # Update attempt count in cache
        remaining_ttl = int((expires_at - datetime.now()).total_seconds())
        if remaining_ttl > 0:
            await self.cache.set(key, stored_data, ttl=remaining_ttl)
        
        return False

    async def create_email_verification_token(self, email: str, user_id: str) -> str:
        token = self._generate_email_verification_token()
        key = f"{self.EMAIL_VERIFICATION_PREFIX}{token}"
        await self.cache.set(
            key,
            {
                "email": email.lower(),
                "user_id": user_id,
                "created_at": datetime.now().isoformat(),
            },
            ttl=self.EMAIL_VERIFICATION_EXPIRY_SECONDS,
        )
        return token

    async def consume_email_verification_token(self, token: str) -> Optional[Dict]:
        key = f"{self.EMAIL_VERIFICATION_PREFIX}{token}"
        data = await self.cache.get(key)
        if data:
            await self.cache.delete(key)
        return data
    
    def _create_verification_email(self, email: str, code: str) -> MIMEMultipart:
        """Create verification email content"""
        msg = MIMEMultipart()
        msg['From'] = self.smtp_from
        msg['To'] = email
        msg['Subject'] = "Password Reset Verification Code"
        
        # Email body
        body = f"""
        <html>
        <body>
            <h2>Password Reset Verification</h2>
            <p>You have requested to reset your password. Please use the following verification code:</p>
            <h3 style="color: #007bff; font-size: 24px; letter-spacing: 2px;">{code}</h3>
            <p><strong>This code will expire in 5 minutes.</strong></p>
            <p>If you did not request this password reset, please ignore this email.</p>
            <br>
            <p>Best regards,<br>AI-DataSeek Team</p>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, 'html'))
        return msg

    def _create_email_verification_link_email(self, email: str, verify_url: str) -> MIMEMultipart:
        msg = MIMEMultipart()
        msg['From'] = self.smtp_from
        msg['To'] = email
        msg['Subject'] = "Verify your AI-DataSeek account"
        body = f"""
        <html>
        <body>
            <h2>Verify your AI-DataSeek account</h2>
            <p>Please click the link below to complete your registration:</p>
            <p><a href="{verify_url}" style="color: #007bff;">Verify email address</a></p>
            <p>This link will expire in 24 hours.</p>
            <p>If you did not register for AI-DataSeek, please ignore this email.</p>
            <br>
            <p>Best regards,<br>AI-DataSeek Team</p>
        </body>
        </html>
        """
        msg.attach(MIMEText(body, 'html'))
        return msg
    
    async def send_verification_code(self, email: str):
        """Send verification code to email address"""
        self._ensure_smtp_configured()
        
        # Check if there's an existing verification code that's too recent
        key = f"{self.VERIFICATION_CODE_PREFIX}{email}"
        existing_data = await self.cache.get(key)
        if existing_data:
            try:
                # Check if the existing code was created less than 60 seconds ago
                created_at = datetime.fromisoformat(existing_data["created_at"])
                time_since_creation = (datetime.now() - created_at).total_seconds()
                
                if time_since_creation < 60:
                    remaining_wait = int(60 - time_since_creation)
                    raise BadRequestError(f"Please wait {remaining_wait} seconds before requesting a new verification code")
            except (KeyError, ValueError):
                # Invalid data, continue with new code generation
                pass
        
        # Generate verification code
        code = self._generate_verification_code()
        logger.debug(f"Generated verification code: {code}")
        
        # Create email message
        msg = self._create_verification_email(email, code)
        logger.debug(f"Created email message: {msg}")
        
        # Send email using SMTP
        await self._send_smtp_email(msg, email)

        # Store verification code
        await self._store_verification_code(email, code)
        
        logger.info(f"Verification code sent to {email}")

    async def send_email_verification_link(self, email: str, verify_url: str) -> None:
        self._ensure_smtp_configured()
        msg = self._create_email_verification_link_email(email, verify_url)
        await self._send_smtp_email(msg, email)
        logger.info("Email verification link sent to %s", email)
    
    async def _send_smtp_email(self, msg: MIMEMultipart, email: str) -> None:
        """Send email using SMTP (runs in thread pool to avoid blocking)"""
        logger.debug(f"Sending email to {email}")
        server = None
        try:
            logger.debug(f"Creating SMTP server connection to {self.smtp_host}:{self.smtp_port}")
            if int(self.smtp_port) == 465:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                if self.settings.smtp_tls is not False:
                    server.starttls()
            logger.debug(f"SMTP server created, {server}")
            result = server.login(self.smtp_username, self.smtp_password)
            logger.debug(f"SMTP server login result: {result}")
            
            # Send email
            text = msg.as_string()
            result = server.sendmail(msg['From'], email, text)
            logger.debug(f"SMTP server sendmail result: {result}")
        finally:
            if server:
                server.quit()
    
    async def cleanup_expired_codes(self) -> None:
        """Clean up expired verification codes - Cache TTL handles this automatically"""
        # Cache automatically handles expiration via TTL, so this method is mainly for manual cleanup
        
        # Get all verification code keys
        pattern = f"{self.VERIFICATION_CODE_PREFIX}*"
        keys = await self.cache.keys(pattern)
        
        expired_count = 0
        for key in keys:
            data = await self.cache.get(key)
            if data:
                try:
                    expires_at = datetime.fromisoformat(data["expires_at"])
                    if datetime.now() > expires_at:
                        await self.cache.delete(key)
                        expired_count += 1
                except (KeyError, ValueError):
                    # Invalid data, delete it
                    await self.cache.delete(key)
                    expired_count += 1
        
        if expired_count > 0:
            logger.info(f"Cleaned up {expired_count} expired verification codes")
