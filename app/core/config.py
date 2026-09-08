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
    openai_api_key: str
    # gpt-5.6-terra is OpenAI's mid tier — balances cost and quality, cheaper
    # than their flagship (gpt-6-astra). Reasonable default for a per-message
    # conversational agent on a low-ARPU product; revisit with real usage
    # data if quality falls short. The TikTok comment classifier (Jalon 7)
    # is a separate decision and must not inherit this choice automatically.
    agent_model: str = "gpt-5.6-terra"
    voyage_api_key: str = ""
    # voyage-4-lite is the cost-efficient default for development; voyage-4
    # or voyage-4-large are drop-in upgrades (same embedding space, same
    # 1024-dim default) if search relevance needs it later.
    voyage_model: str = "voyage-4-lite"
    # First-pass RAG cosine-distance cutoff (pgvector <=> / 1 - cosine sim).
    # Measured 2026-09-08 on Boutique Awa with voyage-4-lite (lower = closer):
    #   "robe rouge pour une soirée": 0.3088 robe rouge, 0.3724 robe noire,
    #       0.5574 pagne, 0.5855 lipstick
    #   "vous avez des baskets pr homme svp": 0.3042 / 0.3433 the two
    #       sneakers, 0.4443 mocassins
    #   "ciment 50kg pour chantier": 0.7174 and up (all unrelated)
    #   similar(robe rouge): 0.1088 robe noire
    # 0.50 sits in the gap between the last useful in-catalogue match
    # (~0.44) and the out-of-catalogue floor (~0.72). Jalon 5 (evaluation)
    # will retune this on a larger, more varied query set.
    rag_max_distance: float = 0.50
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_webhook_verify_token: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_business_access_token: str = ""


settings = Settings()
