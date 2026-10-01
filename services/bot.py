"""Services for independent internal game-bot participants."""

import json

from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.config import settings
from sib.database.models import MatchPlayer, User


class BotService:
    """Creates one persistent User identity and profile per bot participant."""

    PARTY_BOT_NAMES = (
        "باتِ سیبیِ بهار 🤖",
        "باتِ سیبیِ دانا 🤖",
        "باتِ سیبیِ جنگاور 🤖",
        "باتِ سیبیِ تیزپا 🤖",
        "باتِ سیبیِ زرنگ 🤖",
        "باتِ سیبیِ یاقوتی 🤖",
        "باتِ سیبیِ مهربون 🤖",
        "باتِ سیبیِ کاوشگر 🤖",
    )

    @classmethod
    async def create_party_bots(
        cls,
        session: AsyncSession,
        *,
        party_id: int,
        count: int,
        mode: str,
        game_code: str,
    ) -> list[tuple[User, str]]:
        """Create stable, distinctly named identities for Friendly party slots.

        Party bots use a reserved negative-ID range separate from the
        match-scoped fallback bot identities. Repeating a fill attempt for
        the same party/ordinal resolves to the same User row.
        """
        if count < 0 or count > len(cls.PARTY_BOT_NAMES):
            raise ValueError("Friendly party bot count is outside the supported roster")
        created: list[tuple[User, str]] = []
        for ordinal in range(1, count + 1):
            user_id = settings.BOT_USER_ID - 1_000_000_000_000 - (party_id * 8) - ordinal
            name = cls.PARTY_BOT_NAMES[(ordinal - 1) % len(cls.PARTY_BOT_NAMES)]
            user = await session.get(User, user_id)
            if user is None:
                user = User(
                    id=user_id,
                    first_name=name,
                    username=f"sib_friendly_bot_{party_id}_{ordinal}",
                    language_code="fa",
                    is_bot=True,
                    is_active=True,
                )
                session.add(user)
                await session.flush()
            elif not user.is_bot:
                raise ValueError("Reserved Friendly bot identity is already used by a human")

            profile = json.dumps(
                {
                    "version": 1,
                    "kind": "friendly_party_bot",
                    "difficulty": "normal",
                    "personality": "tactical",
                    "strategy": game_code,
                    "loadout": {},
                },
                ensure_ascii=False,
            )
            created.append((user, profile))
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
