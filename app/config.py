from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_name: str = "Видеоплатформа"
    db_host: str = "localhost"
    db_port: int = 5432
    postgres_db: str = "video"
    postgres_user: str = "video"
    postgres_password: SecretStr = Field(min_length=16)
    media_root: Path = Path(".local/media")
    auth_cookie_secure: bool = False
    session_lifetime_hours: int = Field(default=336, ge=1, le=2160)
    auth_require_verified_email: bool = True
    auth_mail_secret: SecretStr | None = Field(default=None, min_length=32)
    mail_backend: Literal["auto", "smtp", "mailpit"] = "auto"
    mailpit_host: str = "127.0.0.1"
    mailpit_port: int = Field(default=1025, ge=1, le=65535)
    mailpit_ui_url: str = "http://localhost:8025"
    public_base_url: str = "http://localhost:8080"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    smtp_from: str = ""
    smtp_security: str = "starttls"
    smtp_timeout_seconds: int = Field(default=10, ge=1, le=30)
    email_verification_minutes: int = Field(default=60, ge=5, le=1440)
    password_reset_minutes: int = Field(default=30, ge=5, le=60)
    max_upload_bytes: int = Field(default=1024**3, ge=1)
    owner_media_quota_bytes: int = Field(default=10 * 1024**3, ge=1)
    media_capacity_bytes: int = Field(default=100 * 1024**3, ge=1)
    media_min_free_bytes: int = Field(default=2 * 1024**3, ge=0)
    preview_budget_bytes: int = Field(default=16 * 1024**2, ge=1)
    max_uploads_per_owner: int = Field(default=2, ge=1)
    max_uploads_total: int = Field(default=4, ge=1)
    max_pending_video_jobs: int = Field(default=100, ge=1)
    upload_idle_timeout_seconds: int = Field(default=60, ge=1)
    upload_timeout_seconds: int = Field(default=3600, ge=1)
    upload_lease_seconds: int = Field(default=120, ge=1)
    upload_heartbeat_seconds: int = Field(default=15, ge=1)
    processing_concurrency: int = Field(default=1, ge=1, le=4)
    processing_lease_seconds: int = Field(default=60, ge=3)
    processing_heartbeat_seconds: int = Field(default=5, ge=1)
    processing_max_attempts: int = Field(default=3, ge=1, le=10)
    processing_retry_seconds: int = Field(default=15, ge=1)
    processing_memory_bytes: int = Field(default=768 * 1024**2, ge=128 * 1024**2)
    ffprobe_timeout_seconds: int = Field(default=30, ge=1)
    processing_timeout_seconds: int = Field(default=900, ge=1)
    max_video_duration_seconds: int = Field(default=7200, ge=1)
    max_video_long_side: int = Field(default=1920, ge=1)
    max_video_short_side: int = Field(default=1080, ge=1)
    max_video_fps: int = Field(default=60, ge=1, le=60)
    video_create_limit_per_hour: int = Field(default=30, ge=1)
    video_write_limit_per_minute: int = Field(default=120, ge=1)
    interaction_write_limit_per_minute: int = Field(default=120, ge=1)
    comment_create_limit_per_minute: int = Field(default=10, ge=1)
    playlist_write_limit_per_minute: int = Field(default=120, ge=1)
    playlist_create_limit_per_hour: int = Field(default=30, ge=1)
    playlist_max_items: int = Field(default=1000, ge=1, le=10000)
    admin_write_limit_per_minute: int = Field(default=30, ge=1)
    report_create_limit_per_hour: int = Field(default=10, ge=1)
    moderation_write_limit_per_minute: int = Field(default=120, ge=1)
    upload_limit_per_hour: int = Field(default=30, ge=1)
    upload_cleanup_interval_seconds: int = Field(default=15, ge=1)

    @field_validator("auth_mail_secret", "smtp_password", mode="before")
    @classmethod
    def empty_mail_secret(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_video_limits(self):
        from urllib.parse import urlsplit

        base = urlsplit(self.public_base_url)
        if (
            base.scheme not in ("http", "https")
            or not base.hostname
            or base.username
            or base.password
            or base.query
            or base.fragment
            or base.path not in ("", "/")
        ):
            raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin")
        if self.smtp_security not in ("starttls", "tls", "plain"):
            raise ValueError("SMTP_SECURITY must be starttls, tls or plain")
        if self.smtp_security == "plain" and self.smtp_host not in (
            "",
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise ValueError("Plain SMTP is allowed only on loopback for isolated tests")
        if self.smtp_from:
            from email_validator import EmailNotValidError, validate_email

            try:
                validate_email(self.smtp_from, check_deliverability=False)
            except EmailNotValidError:
                raise ValueError("Invalid SMTP_FROM") from None
        if self.smtp_username and not self.smtp_password:
            raise ValueError("SMTP_PASSWORD is required with SMTP_USERNAME")
        if self.mail_backend == "mailpit" and not self.local_mail:
            raise ValueError("Mailpit requires a loopback HTTP origin and non-Secure local cookies")
        if self.local_mail:
            from app.mail.policy import LOOPBACK

            if self.mailpit_host not in LOOPBACK | {"mailpit"}:
                raise ValueError("Mailpit must use loopback or the local Compose service")
            inbox = urlsplit(self.mailpit_ui_url)
            if (
                inbox.scheme != "http"
                or inbox.hostname not in LOOPBACK
                or inbox.username
                or inbox.password
                or inbox.query
                or inbox.fragment
                or inbox.path not in ("", "/")
            ):
                raise ValueError("MAILPIT_UI_URL must be a loopback HTTP origin")
        if self.mail_configured and self.auth_cookie_secure and base.scheme != "https":
            raise ValueError("Secure deployments require an HTTPS PUBLIC_BASE_URL")
        if max(self.media_capacity_bytes, self.upload_reservation_bytes()) > 2**63 - 1:
            raise ValueError("Media limits must fit PostgreSQL bigint")
        if self.owner_media_quota_bytes < self.upload_reservation_bytes():
            raise ValueError("Owner quota must cover an upload and its processing reservation")
        if self.media_capacity_bytes < self.owner_media_quota_bytes:
            raise ValueError("Media capacity must be at least the owner quota")
        if self.max_uploads_total < self.max_uploads_per_owner:
            raise ValueError("Total uploads must be at least uploads per owner")
        if self.upload_lease_seconds <= 2 * self.upload_heartbeat_seconds:
            raise ValueError("Upload lease must exceed two heartbeat intervals")
        if self.upload_timeout_seconds < self.upload_idle_timeout_seconds:
            raise ValueError("Upload total timeout must cover idle timeout")
        if self.processing_lease_seconds <= 2 * self.processing_heartbeat_seconds:
            raise ValueError("Processing lease must exceed two heartbeat intervals")
        if self.max_video_long_side < self.max_video_short_side:
            raise ValueError("Video long side must be at least the short side")
        return self

    @property
    def local_mail(self) -> bool:
        from app.mail.policy import uses_mailpit

        return uses_mailpit(
            self.mail_backend, self.smtp_host, self.auth_cookie_secure, self.public_base_url
        )

    @property
    def mail_configured(self) -> bool:
        return bool(
            self.auth_mail_secret and (self.local_mail or self.smtp_host and self.smtp_from)
        )

    def upload_reservation_bytes(self, content_length: int | None = None) -> int:
        size = self.max_upload_bytes if content_length is None else content_length
        if not 1 <= size <= self.max_upload_bytes:
            raise ValueError("Invalid upload size")
        return 2 * size + self.preview_budget_bytes

    @property
    def session_cookie_name(self) -> str:
        return "__Host-video_session" if self.auth_cookie_secure else "video_session"

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.postgres_db,
        )
