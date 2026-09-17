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

    PASSWORD_RESET_EXPIRE_HOURS: int = 24
    ACTIVATION_TOKEN_EXPIRE_HOURS: int = 72
    FRONTEND_URL: str = "http://localhost:5173"

    SEED_SUPER_ADMIN_EMAIL: str = "admin@payflow.ai"
    SEED_SUPER_ADMIN_PASSWORD: str = "Admin@12345"
    SEED_SUPER_ADMIN_NAME: str = "Platform Super Admin"

    PASSWORD_MIN_LENGTH: int = 8


settings = Settings()
