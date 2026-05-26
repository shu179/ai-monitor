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
    email_code_ttl_minutes: int = 10
    email_code_resend_seconds: int = 60
    email_code_max_attempts: int = 5
    email_provider: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = 15
    tencent_ses_secret_id: str = ""
    tencent_ses_secret_key: str = ""
    tencent_ses_region: str = "ap-hongkong"
    tencent_ses_endpoint: str = "ses.tencentcloudapi.com"
    tencent_ses_from: str = ""
    tencent_ses_reply_to: str = ""
    tencent_ses_template_id: int = 0
    docs_enabled: bool = True
    public_update_base_url: str = ""
    access_token_minutes: int = 20
    refresh_token_days: int = 30
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    db_pool_timeout_seconds: int = 30
    db_statement_timeout_ms: int = 5000
    worker_db_statement_timeout_ms: int = 60000

    @computed_field
    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
