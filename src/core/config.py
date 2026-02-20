"""
Application settings loaded from .env via pydantic-settings.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc"
    REDIS_URL: str = "redis://localhost:6379/0"
    SECRET_KEY: str = "changeme"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 180
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    CREDENTIAL_ENCRYPTION_KEY: str = "changeme32byteskeyplaceholder123"
    ENVIRONMENT: str = "development"
    CLAWBOT_BASE_URL: str = "http://localhost:18789/v1"
    CLAWBOT_TOKEN: str = ""
    CLAWBOT_AGENT_ID: str = "main"

    # SMTP — email notifications (optional; leave SMTP_HOST empty to disable)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@sitedoc.ai"
    SMTP_TLS: bool = False       # True = SMTP_SSL (port 465)
    SMTP_STARTTLS: bool = True   # True = STARTTLS (port 587)

    # Frontend URL for email links
    APP_URL: str = "http://localhost:3000"

    # Admin alert email — receives agent failure notifications (override via env var)
    ADMIN_ALERT_EMAIL: str = "saleturnkey@gmail.com"

    # Managed hosting server
    HOSTING_SERVER_IP: str = "69.10.55.138"
    HOSTING_SSH_USER: str = "sitedoc"
    HOSTING_SSH_KEY_PATH: str = ""  # path to SSH private key for hosting server
    HOSTING_PROVISION_SCRIPT: str = "/opt/sitedoc-infra/scripts/provision-site.sh"
    HOSTING_TEARDOWN_SCRIPT: str = "/opt/sitedoc-infra/scripts/teardown-site.sh"


settings = Settings()
