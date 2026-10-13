"""Configuration module for sib bot using Pydantic Settings."""

from pathlib import Path
from typing import List, Union
from pydantic import Field, AliasChoices, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram Bot Settings
    BOT_TOKEN: str = Field(
        default="",
        validation_alias=AliasChoices("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        description="Telegram Bot Token from BotFather",
    )
    BOT_USERNAME: str = Field(
        default="sib_game_bot",
        validation_alias=AliasChoices("BOT_USERNAME", "TELEGRAM_BOT_USERNAME"),
        description="Telegram Bot Username",
    )
    BOT_USER_ID: int = Field(
        default=-900000000001,
        description="Internal user ID for game bots",
    )
    ADMIN_IDS: Union[str, List[int]] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ADMIN_IDS", "TELEGRAM_ADMIN_IDS"),
        description="List of Telegram admin user IDs",
    )

    # Official Channel & Membership Gate
    OFFICIAL_CHANNEL_ID: str = Field(default="@sib_official", description="Official channel username or ID")
    OFFICIAL_CHANNEL_LINK: str = Field(default="https://t.me/sib_official", description="Official channel invite link")
    MEMBERSHIP_CHECK_ENABLED: bool = Field(default=True, description="Enable or disable membership verification")

    # Database
    DATABASE_URL: str = Field(
        default="mysql+asyncmy://sib:sib_password@127.0.0.1:3306/sib",
        description="Async database connection string",
    )
    DATABASE_SYNC_URL: str = Field(
        default="mysql+pymysql://sib:sib_password@127.0.0.1:3306/sib",
        description="Sync database connection string for Alembic migrations",
    )

    # Timeouts (in seconds)
    SOLO_MATCHMAKING_TIMEOUT_SECONDS: int = Field(default=25, description="Solo matchmaking timeout before bot fallback")
    TEAM_MATCHMAKING_TIMEOUT_SECONDS: int = Field(default=30, description="2v2/4v4 matchmaking timeout before bot fallback")
    MATCHMAKING_TIMEOUT_SECONDS: int = Field(default=600, description="Deprecated legacy timeout; mode-specific deadlines are used")
    ACCEPTANCE_TIMEOUT_SECONDS: int = Field(default=120, description="Match acceptance timeout")
    # Legacy bot toggles remain readable for deployment compatibility; bot
    # fallback is now part of the standard matchmaking lifecycle.
    BOT_OPPONENTS_ENABLED: bool = Field(default=False, description="Deprecated: bot fallback is automatic")
    BOT_OPPONENT_DELAY_SECONDS: int = Field(default=30, description="Deprecated: fallback uses queue deadlines")
    
    # Anti-spam / Rate Limiting
    RATE_LIMIT_MESSAGE_COUNT: int = Field(default=10, description="Maximum normal messages per window")
    RATE_LIMIT_MESSAGE_WINDOW: int = Field(default=5, description="Normal message rate-limit window")
    RATE_LIMIT_COMMAND_COUNT: int = Field(default=3, description="Maximum commands per window")
    RATE_LIMIT_COMMAND_WINDOW: int = Field(default=10, description="Command rate-limit window")
    RATE_LIMIT_WAKE_WORD_COUNT: int = Field(default=2, description="Maximum wake-word messages per window")
    RATE_LIMIT_WAKE_WORD_WINDOW: int = Field(default=10, description="Wake-word rate-limit window")
    RATE_LIMIT_CALLBACK_COUNT: int = Field(default=30, description="Maximum callbacks per window")
    RATE_LIMIT_CALLBACK_WINDOW: int = Field(default=5, description="Callback rate-limit window")

    # Game Turn Timeouts (in seconds)
    HIGH_CARD_TIMEOUT: int = Field(default=60, description="High card turn timeout")
    ROCK_PAPER_SCISSORS_TIMEOUT: int = Field(default=60, description="Rock Paper Scissors turn timeout")
    TIC_TAC_TOE_TIMEOUT: int = Field(default=60, description="Tic-Tac-Toe turn timeout")
    CONNECT_FOUR_TIMEOUT: int = Field(default=60, description="Connect Four turn timeout")
    BATTLESHIP_TIMEOUT: int = Field(default=90, description="Battleship turn timeout")

    # Direct Apple Duel (UTC server deadlines; six scenes, five gaps).
    APPLE_DUEL_EXPIRY_SECONDS: int = Field(default=60, ge=1, le=3600)
    APPLE_DUEL_SCENE_SECONDS: float = Field(default=4.0, ge=0.1, le=60)

    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                import json
                try:
                    return [int(x) for x in json.loads(v)]
                except Exception:
                    pass
            return [int(item.strip()) for item in v.split(",") if item.strip()]
        if isinstance(v, (int, float)):
            return [int(v)]
        if isinstance(v, list):
            return [int(item) for item in v]
        return []


settings = Settings()
