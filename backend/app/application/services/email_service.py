import smtplib
import logging
import secrets
import asyncio
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
    
    def __init__(self, cache: Cache):
        self.settings = get_settings()
        self.cache = cache
    
    def _generate_verification_code(self) -> str:
        """Generate 6-digit verification code"""
        return str(secrets.randbelow(900000) + 100000)
    
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
        """Atomically verify and consume a one-time password-reset code."""
        key = f"{self.VERIFICATION_CODE_PREFIX}{email}"
        consume = getattr(self.cache, "consume_verification_code", None)
        if not callable(consume):
            logger.error(
                "Cache does not support atomic verification-code consumption"
            )
            return False
        try:
            return bool(await consume(key, code, 3))
        except Exception as exc:
            logger.error(
                "Verification-code authorization unavailable: %s",
                type(exc).__name__,
            )
            return False
    
    def _create_verification_email(self, email: str, code: str) -> MIMEMultipart:
        """Create verification email content"""
        msg = MIMEMultipart()
        msg['From'] = self.settings.email_from or self.settings.email_username
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
            <p>Best regards,<br>AI Manus Team</p>
        </body>
        </html>
        """
        
        msg.attach(MIMEText(body, 'html'))
        return msg
    
    async def send_verification_code(self, email: str):
        """Send verification code to email address"""
        # Check if email configuration is available
        if not all([
            self.settings.email_host,
            self.settings.email_port,
            self.settings.email_username,
            self.settings.email_password
        ]):
            logger.error("Email configuration is incomplete, simulating email send")
            raise BadRequestError("Email configuration is incomplete")
        
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
        logger.debug("Generated password-reset verification code")
        
        # Create email message
        msg = self._create_verification_email(email, code)
        logger.debug("Created password-reset email message")
        
        # Send email using SMTP
        await self._send_smtp_email(msg, email)

        # Store verification code
        await self._store_verification_code(email, code)
        
        logger.info("Password-reset verification code sent")
    
    async def _send_smtp_email(self, msg: MIMEMultipart, email: str) -> None:
        """Send email using SMTP (runs in thread pool to avoid blocking)"""
        def send_sync() -> None:
            logger.debug("Sending password-reset email")
            server = None
            try:
                logger.debug("Creating SMTP server connection")
                server = smtplib.SMTP_SSL(
                    self.settings.email_host, self.settings.email_port
                )
                logger.debug("SMTP server connection created")
                server.login(
                    self.settings.email_username,
                    self.settings.email_password,
                )
                server.sendmail(msg["From"], email, msg.as_string())
                logger.debug("SMTP message accepted for delivery")
            finally:
                if server:
                    server.quit()

        # smtplib is synchronous; keep DNS, connect, TLS, login, and send off
        # the FastAPI event loop so one slow mail server cannot stall all API
        # requests handled by this process.
        await asyncio.to_thread(send_sync)
    
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
