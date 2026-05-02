from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SURFACED_CLOUD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = "development"
    secret_key: str = Field(min_length=16)
    database_url: str = "postgresql+psycopg://surfaced:surfaced-password@localhost:5432/surfaced_cloud"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    admin_self_register_enabled: bool = True
    email_verification_required: bool = False
    public_update_base_url: str = ""
    access_token_minutes: int = 20
    refresh_token_days: int = 30

    @computed_field
    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

