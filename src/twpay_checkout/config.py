"""Application settings loaded from .env (see .env.example)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All secrets live in .env; nothing here may be hardcoded or committed."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    base_url: str = ""

    ecpay_merchant_id: str = ""
    ecpay_hash_key: str = ""
    ecpay_hash_iv: str = ""

    newebpay_merchant_id: str = ""
    newebpay_hash_key: str = ""
    newebpay_hash_iv: str = ""

    database_url: str = "sqlite:///twpay.db"
