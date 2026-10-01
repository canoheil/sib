"""Services for independent internal game-bot participants."""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.config import settings
from sib.database.models import MatchPlayer, User


# Party bots (the "fill empty slots with bots" option) get their own ID
# namespace so they can never collide with a per-match bot identity.
PARTY_BOT_ID_OFFSET = 1_000_000

# Game-appropriate Persian bot names. Every party/match bot is a REAL User row
# with ``is_bot=True`` and a "🤖" name prefix, so it is always distinguishable
# from a Telegram player in rosters, boards and keyboards.
GAME_BOT_NAMES = {
    "battleship": "ناخدای سیب",
    "connect_four": "مهره‌چین سیب",
    "tic_tac_toe": "دوزباز سیب",
    "rock_paper_scissors": "قیچی‌باز سیب",
    "high_card": "کارت‌باز سیب",
    "apple_battle": "سیب کوچولو",
}

DEFAULT_BOT_NAME = "سیب کوچولو"


def bot_display_name(game_code: str, ordinal: int) -> str:
    """Game-appropriate, clearly-marked bot display name."""
    prefix = GAME_BOT_NAMES.get(game_code, DEFAULT_BOT_NAME)
    return f"🤖 {prefix} {ordinal}"


def _bot_profile(game_code: str, mode: str) -> str:
    return json.dumps(
        {
            "version": 1,
            "kind": "little_apple",
            "difficulty": "challenging" if mode == "solo" else "normal",
            "personality": "tactical",
            "strategy": game_code,
            "loadout": {},
        },
        ensure_ascii=False,
    )


class BotService:
    """Creates one persistent User identity and profile per bot participant."""

    @staticmethod
    async def create_party_bots(
        session: AsyncSession,
        *,
        party_id: int,
        count: int,
        game_code: str,
        mode: str,
        team: int,
        ordinal_offset: int = 0,
    ) -> list[tuple[User, str, int]]:
        """Create ``count`` bot participants for one friendly party team.

        Returns ``[(user, profile_json, team), ...]``. IDs come from the party
        bot namespace, names are game-appropriate, and each bot is a distinct
        persisted User (``is_bot=True``) - the same abstraction per-match bots
        already use, so rosters, boards and bot turns need no special casing.
        """
        created: list[tuple[User, str, int]] = []
        for index in range(1, count + 1):
            ordinal = ordinal_offset + index
            user_id = (
                settings.BOT_USER_ID
                - PARTY_BOT_ID_OFFSET
                - (party_id * 8)
                - ordinal
            )
            user = await session.get(User, user_id)
            if user is None:
                user = User(
                    id=user_id,
                    first_name=bot_display_name(game_code, ordinal),
                    username=f"sib_party_bot_{party_id}_{ordinal}",
                    language_code="fa",
                    is_bot=True,
                    is_active=True,
                )
                session.add(user)
                await session.flush()
            created.append((user, _bot_profile(game_code, mode), team))
        return created

    @staticmethod
    async def create_match_bots(
        session: AsyncSession,
        *,
        match_id: int,
        count: int,
        mode: str,
        game_code: str,
    ) -> list[tuple[User, str]]:
        """Create independent bot users and their per-match profile JSON.

        IDs are deterministically allocated from the internal bot namespace,
        with an eight-ID stride per match (the largest supported team is four).
        Each bot is still a distinct User and MatchPlayer, not a shared team
        controller. Profiles are persisted on MatchPlayer for extensibility.
        """
        created: list[tuple[User, str]] = []
        for ordinal in range(1, count + 1):
            user_id = settings.BOT_USER_ID - (match_id * 8) - ordinal
            name = "سیب کوچولو" if mode == "solo" else f"بات {ordinal}"
            user = await session.get(User, user_id)
            if user is None:
                user = User(
                    id=user_id,
                    first_name=name,
                    username=f"sib_bot_{match_id}_{ordinal}",
                    language_code="fa",
                    is_bot=True,
                    is_active=True,
                )
                session.add(user)
                await session.flush()
            profile = json.dumps(
                {
                    "version": 1,
                    "kind": "little_apple",
                    "difficulty": "challenging" if mode == "solo" else "normal",
                    "personality": "tactical",
                    "strategy": game_code,
                    "loadout": {},
                },
                ensure_ascii=False,
            )
            created.append((user, profile))
        return created

    @staticmethod
    async def get_or_create_bot(session: AsyncSession) -> User:
        """Legacy singleton identity kept for compatibility with old records."""
        bot = await session.get(User, settings.BOT_USER_ID)
        if bot:
            return bot
        bot = User(
            id=settings.BOT_USER_ID,
            first_name="سیب کوچولو",
            username="goje_game_bot",
            language_code="fa",
            is_bot=True,
            is_active=True,
        )
        session.add(bot)
        await session.flush()
        return bot


def bot_profile_for(player: MatchPlayer) -> dict:
    """Decode a bot participant's persisted profile, with safe legacy defaults."""
    if not player.bot_profile:
        return {
            "version": 1,
            "kind": "little_apple",
            "difficulty": "normal",
            "personality": "balanced",
            "strategy": None,
            "loadout": {},
        }
    try:
        profile = json.loads(player.bot_profile)
    except (TypeError, ValueError):
        profile = {}
    return {
        "version": profile.get("version", 1),
        "kind": profile.get("kind", "little_apple"),
        "difficulty": profile.get("difficulty", "normal"),
        "personality": profile.get("personality", "balanced"),
        "strategy": profile.get("strategy"),
        "loadout": profile.get("loadout", {}),
    }
