from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://hypercopy:hypercopy_dev_2024@localhost:5432/hypercopy"

    # Auth
    JWT_SECRET: str = "change-me"
    JWT_ALGO: str = "HS256"
    JWT_EXPIRE_HOURS: int = 72

    # Hyperliquid
    HL_MAINNET: bool = False
    HL_ACCOUNT_ADDRESS: str = ""
    HL_API_SECRET_KEY: str = ""

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Legacy pipeline
    DATA_DIR: str = "data"
    OPENAI_API_KEY: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

@lru_cache()
def get_settings() -> Settings:
    return Settings()