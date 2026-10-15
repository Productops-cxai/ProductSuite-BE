from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/PayFlowDB"
    APP_NAME: str = "Platform Suite API"
    APP_ENV: str = "development"
    DEBUG: bool = True

    JWT_SECRET: str = "change-me-platform-suite-secret-key"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_EXPIRE_DAYS: int = 7

    # Password-reset and activation link TTLs (minutes). Overridden by .env — restart API after change.
    PASSWORD_RESET_EXPIRE_MINUTES: int = 5
    ACTIVATION_TOKEN_EXPIRE_MINUTES: int = 5
    FRONTEND_URL: str = "http://localhost:5173"

    SEED_SUPER_ADMIN_EMAIL: str = "admin@payflow.ai"
    SEED_SUPER_ADMIN_PASSWORD: str = "Admin@12345"
    SEED_SUPER_ADMIN_NAME: str = "Platform Super Admin"

    PASSWORD_MIN_LENGTH: int = 8

    # Optional SMTP — when SMTP_HOST is empty, emails are logged only (email_logs).
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_USE_TLS: bool = True

    # AI workflow suggestion — Groq (OpenAI-compatible API).
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_TIMEOUT_SECONDS: int = 60
    GROQ_REASONING_EFFORT: str = "low"  # low | medium | high
    # FREE-TIER BATCHING: free tier allows 8k tokens/min, so send 3 segments per call.
    # Set 0 to send all segments in one call (paid tier).
    GROQ_SEGMENTS_PER_CALL: int = 3

    # Collection email — SendGrid (empty key = log-only).
    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = ""
    SENDGRID_FROM_NAME: str = "PayFlow"

    # Collection SMS — Twilio (empty credentials = log-only).
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    # Workflow worker (PostgreSQL due-queue).
    WORKFLOW_WORKER_ID: str = ""
    WORKFLOW_WORKER_CONCURRENCY: int = 8
    WORKFLOW_WORKER_BATCH_SIZE: int = 50
    WORKFLOW_WORKER_SWEEP_SECONDS: int = 20
    # Reclaim RUNNING jobs whose lock is older than this (crash / dead worker).
    WORKFLOW_WORKER_STALE_SECONDS: int = 300
    WORKFLOW_JOB_NOTIFY_CHANNEL: str = "workflow_jobs"
    WORKFLOW_PAYMENT_LINK_BASE: str = "https://pay.payflow.local/pay"


settings = Settings()
