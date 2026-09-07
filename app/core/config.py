from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from a `.env` file.

    `environment` is the single source of truth for "which environment am I on"
    and must be checked before touching real data.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "local"
    database_url: str
    redis_url: str
    anthropic_api_key: str = ""
    voyage_api_key: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_webhook_verify_token: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_business_access_token: str = ""


settings = Settings()
