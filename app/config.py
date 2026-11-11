from pathlib import Path

from pydantic import Field, SecretStr
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
