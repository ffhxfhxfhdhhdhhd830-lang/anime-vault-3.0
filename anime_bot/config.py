from functools import lru_cache
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""), alias="TELEGRAM_BOT_TOKEN"
    )
    owner_telegram_id: int = Field(default=0, alias="OWNER_TELEGRAM_ID")
    mongodb_uri: SecretStr = Field(
        default=SecretStr("mongodb://localhost:27017"), alias="MONGODB_URI"
    )
    mongodb_database: str = Field(
        default="anime_command_center", alias="MONGODB_DATABASE"
    )
    default_region: str = Field(default="IN", alias="DEFAULT_REGION")
    timezone: str = Field(default="Asia/Kolkata", alias="TIMEZONE")
    tmdb_api_key: SecretStr = Field(default=SecretStr(""), alias="TMDB_API_KEY")
    streaming_api_key: SecretStr = Field(
        default=SecretStr(""), alias="STREAMING_API_KEY"
    )
    log_channel: str = Field(default="", alias="LOG_CHANNEL")
    pics: str = Field(default="", alias="PICS")
    cache_ttl_seconds: int = Field(default=21600, alias="CACHE_TTL_SECONDS", ge=60)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    allow_groups: bool = Field(default=False, alias="ALLOW_GROUPS")
    deployment_provider: str = Field(
        default="Not configured", alias="DEPLOYMENT_PROVIDER"
    )

    @field_validator("default_region")
    @classmethod
    def region_uppercase(cls, value: str) -> str:
        return value.upper()

    @property
    def log_channel_id(self) -> int | None:
        value = self.log_channel.strip()
        return int(value) if value else None

    @property
    def pic_sources(self) -> tuple[str, ...]:
        return tuple(source for source in self.pics.split() if source)

    def validate_runtime(self) -> None:
        if not self.telegram_bot_token.get_secret_value().strip():
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is required. Copy .env.example to .env and configure it."
            )
        if self.owner_telegram_id <= 0:
            raise ValueError("OWNER_TELEGRAM_ID must be your numeric Telegram user ID.")
        if self.log_channel.strip():
            try:
                int(self.log_channel.strip())
            except ValueError as exc:
                raise ValueError("LOG_CHANNEL must be a numeric Telegram chat ID.") from exc


@lru_cache
def get_settings() -> Settings:
    return Settings()

