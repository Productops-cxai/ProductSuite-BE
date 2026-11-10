from __future__ import annotations

import os
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secrets_crypto import SecretsCryptoError, decrypt_settings_values


def resolve_env_file() -> str:
    """Pick which dotenv file to load.

    Priority:
    1. ``DOTENV_FILE`` process env (explicit path)
    2. ``.env.production`` when process ``APP_ENV=production``
    3. ``.env`` (local / default)
    """
    explicit = (os.environ.get("DOTENV_FILE") or "").strip()
    if explicit:
        return explicit
    app_env = (os.environ.get("APP_ENV") or "").strip().lower()
    if app_env == "production":
        return ".env.production"
    return ".env"


_ENV_FILE = resolve_env_file()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, extra="ignore")

    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/PayFlowDB"
    APP_NAME: str = "Platform Suite API"
    APP_ENV: str = "development"
    DEBUG: bool = True

    JWT_SECRET: str = "change-me-platform-suite-secret-key"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_EXPIRE_MINUTES: int = 15  # default 15 minutes

    def refresh_token_ttl(self):  # timedelta — used by security / cookies / identity
        from datetime import timedelta

        return timedelta(minutes=max(1, int(self.JWT_REFRESH_EXPIRE_MINUTES or 1)))

    def refresh_token_max_age_seconds(self) -> int:
        return max(60, int(self.refresh_token_ttl().total_seconds()))

    # Browser session cookies (HttpOnly). Leave COOKIE_SECURE unset to follow APP_ENV.
    # Cross-site API (app.example.com → api.example.com): COOKIE_SAMESITE=none + COOKIE_SECURE=true
    # and usually COOKIE_DOMAIN=.example.com
    COOKIE_SECURE: bool | None = None
    COOKIE_SAMESITE: str = "lax"
    COOKIE_DOMAIN: str = ""

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

    # AI workflow suggestion — an OpenAI-compatible chat API, chosen by LLM_PROVIDER.
    LLM_PROVIDER: str = "groq"  # groq | together
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    TOGETHER_API_KEY: str = ""
    TOGETHER_MODEL: str = "openai/gpt-oss-120b"
    # Together counts the model's reasoning in max_tokens: too small and the JSON reply is cut off.
    TOGETHER_MAX_TOKENS: int = 16000
    # The GROQ_* settings below apply to both providers (names kept so existing .env files still work).
    GROQ_TIMEOUT_SECONDS: int = 60
    GROQ_REASONING_EFFORT: str = "low"  # low | medium | high
    # FREE-TIER BATCHING: free tier allows 8k tokens/min, so send 3 segments per call.
    # Set 0 to send all segments in one call (paid tier).
    GROQ_SEGMENTS_PER_CALL: int = 3

    # Collection email — SendGrid (empty key = log-only).
    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = ""
    SENDGRID_FROM_NAME: str = "PayFlow"
    # Signed Event Webhook verification key from SendGrid.
    # Empty = accept events without signature check (local/dev only).
    SENDGRID_WEBHOOK_VERIFICATION_KEY: str = ""

    # Collection SMS — Twilio (empty credentials = log-only).
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""
    # Public API base for Twilio StatusCallback / inbound webhooks (e.g. https://api.example.com).
    # Empty = omit StatusCallback on send (local/dev).
    PUBLIC_API_BASE: str = ""

    # Workflow worker (PostgreSQL due-queue).
    WORKFLOW_WORKER_ID: str = ""
    WORKFLOW_WORKER_CONCURRENCY: int = 8
    WORKFLOW_WORKER_BATCH_SIZE: int = 50
    WORKFLOW_WORKER_SWEEP_SECONDS: int = 20
    # Reclaim RUNNING jobs whose lock is older than this (crash / dead worker).
    WORKFLOW_WORKER_STALE_SECONDS: int = 300
    WORKFLOW_JOB_NOTIFY_CHANNEL: str = "workflow_jobs"
    # Re-check every Active workflow for accounts that were never enrolled. 0 = off.
    WORKFLOW_GAP_FILL_SECONDS: int = 3600
    # Import / AI-suggest jobs run inside the API process. After this long without
    # progress they are treated as cut off by a restart and closed by the worker.
    IMPORT_JOB_STALE_MINUTES: int = 15
    AI_SUGGEST_JOB_STALE_MINUTES: int = 30
    # Customer portal base (token appended). Empty → {FRONTEND_URL}/pay
    WORKFLOW_PAYMENT_LINK_BASE: str = ""
    # Opaque payment-link token TTL (hours). Default 14 days.
    PAYMENT_LINK_TTL_HOURS: int = 336

    # Stripe Checkout (empty secret → local mock checkout for development).
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    # Operator brand shown for Third Party clients on the customer portal.
    PAYFLOW_OPERATOR_BRAND_NAME: str = "PayFlow Collections"
    PAYFLOW_OPERATOR_LOGO_URL: str = ""

    # Uploads — empty S3_BUCKET = local disk under ./uploads (dev).
    # Prod: set S3_BUCKET + rely on EC2 IAM role (no AWS keys in .env).
    S3_BUCKET: str = ""
    AWS_REGION: str = "ca-central-1"

    # Throttling keys on the caller's address. Behind nginx / a load balancer every
    # request comes from the proxy, so set this to true there (and only there: a
    # directly exposed API must not trust a header the caller can set).
    TRUST_FORWARDED_FOR: bool = False

    # Comma-separated browser origins for CORS. Empty + production → FRONTEND_URL only.
    # Dev default remains allow-all when empty and APP_ENV != production.
    CORS_ORIGINS: str = ""

    @model_validator(mode="before")
    @classmethod
    def _decrypt_enc_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        try:
            return decrypt_settings_values(data)
        except SecretsCryptoError:
            raise

    def cors_allow_origins(self) -> list[str]:
        """Explicit origins only — required for credentialed (cookie) browser requests."""
        raw = (self.CORS_ORIGINS or "").strip()
        if raw:
            return [o.strip() for o in raw.split(",") if o.strip()]
        if (self.APP_ENV or "").strip().lower() == "production":
            fe = (self.FRONTEND_URL or "").rstrip("/")
            origins = [fe] if fe else []
            if fe.startswith("https://") and "://www." not in fe:
                # Also allow www twin when FRONTEND_URL is apex.
                host = fe.removeprefix("https://")
                origins.append(f"https://www.{host}")
            return origins or ["https://testatncri.com"]
        # Dev: never "*" with allow_credentials=True (cookies would be blocked).
        fe = (self.FRONTEND_URL or "").rstrip("/")
        origins = {
            fe,
            "http://localhost:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:5174",
        }
        return [o for o in origins if o]


settings = Settings()
