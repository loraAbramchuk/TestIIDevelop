from pathlib import Path

from pydantic import Field, SecretStr, model_validator
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
    processing_concurrency: int = Field(default=1, ge=1)
    ffprobe_timeout_seconds: int = Field(default=30, ge=1)
    processing_timeout_seconds: int = Field(default=900, ge=1)
    max_video_duration_seconds: int = Field(default=7200, ge=1)
    max_video_long_side: int = Field(default=1920, ge=1)
    max_video_short_side: int = Field(default=1080, ge=1)
    max_video_fps: int = Field(default=60, ge=1, le=60)
    video_create_limit_per_hour: int = Field(default=30, ge=1)
    video_write_limit_per_minute: int = Field(default=120, ge=1)

    @model_validator(mode="after")
    def validate_video_limits(self):
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
        if self.max_video_long_side < self.max_video_short_side:
            raise ValueError("Video long side must be at least the short side")
        return self

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
