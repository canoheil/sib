"""Services for independent internal game-bot participants."""

import json
import random
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.config import settings
from sib.database.models import MatchPlayer, User


# Party bots (the "fill empty slots with bots" option) get their own ID
# namespace so they can never collide with a per-match bot identity.
PARTY_BOT_ID_OFFSET = 1_000_000

# One shared pool for every game and bot-fill path. Keep the names game-neutral
# so adding a strategy never requires adding another display-name mapping.
BOT_DISPLAY_NAME_POOL = (
    "جنگجوی سیبی",
    "سیب کوچولو",
    "سیب گنده",
    "کاپیتان سیب",
    "پهلوان سیب",
    "فرمانده سیب",
    "سیب خندان",
    "سیب دلاور",
    "سیب ماجراجو",
    "سیب‌باز زرنگ",
    "شاه‌سیب",
    "گردباد سیبی",
    "سیب آتشین",
    "سیب شیرین",
    "سیب ترش",
    "سیب خوش‌شانس",
    "سیب فضایی",
    "سیب سرخ‌پوش",
    "سیب سبزپوش",
    "استاد سیب",
    "سیب کوچولوی شجاع",
    "سیب تندوتیز",
    "سیب کماندار",
    "سیب تاج‌دار",
)
BOT_DISPLAY_NAME_PREFIX = "🤖 "
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _unprefixed_bot_name(name: str) -> str:
    name = name.strip()
    if name.startswith(BOT_DISPLAY_NAME_PREFIX):
        return name[len(BOT_DISPLAY_NAME_PREFIX):].strip()
    return name


def choose_bot_display_names(
    count: int,
    *,
    existing_names: Iterable[str] = (),
    rng: Any = None,
) -> list[str]:
    """Randomly choose marked, unique names for one match or party.

    When a roster is larger than the pool, numbered Persian variants keep
    every display name unique instead of failing or silently duplicating one.
    ``rng`` is injectable for deterministic tests; production uses ``random``.
    """
    if count <= 0:
        return []

    chooser = rng or random
    used_names = {_unprefixed_bot_name(name) for name in existing_names}
    available = [name for name in BOT_DISPLAY_NAME_POOL if name not in used_names]
    selected = list(chooser.sample(available, min(count, len(available))))
    names = [f"{BOT_DISPLAY_NAME_PREFIX}{name}" for name in selected]
    used_names.update(selected)

    suffix = 2
    while len(names) < count:
        base_name = chooser.choice(BOT_DISPLAY_NAME_POOL)
        candidate = f"{base_name} {str(suffix).translate(_PERSIAN_DIGITS)}"
        while candidate in used_names:
            suffix += 1
            candidate = f"{base_name} {str(suffix).translate(_PERSIAN_DIGITS)}"
        names.append(f"{BOT_DISPLAY_NAME_PREFIX}{candidate}")
        used_names.add(candidate)
        suffix += 1

    return names


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
        display_names: Optional[Sequence[str]] = None,
    ) -> list[tuple[User, str, int]]:
        """Create ``count`` bot participants for one friendly party team.

        Returns ``[(user, profile_json, team), ...]``. IDs remain in the
        internal party-bot namespace; display names come from the shared pool.
        """
        names = (
            list(display_names)
            if display_names is not None
            else choose_bot_display_names(count)
        )
        if len(names) != count:
            raise ValueError("One display name is required for each party bot.")

        created: list[tuple[User, str, int]] = []
        for index, name in enumerate(names, start=1):
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
                    first_name=name,
                    username=f"sib_party_bot_{party_id}_{ordinal}",
                    language_code="fa",
                    is_bot=True,
                    is_active=True,
                )
                session.add(user)
                await session.flush()
            elif not user.is_bot:
                raise ValueError(f"Reserved party-bot identity {user_id} belongs to a real user.")
            else:
                user.first_name = name
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
        names = choose_bot_display_names(count)
        created: list[tuple[User, str]] = []
        for ordinal, name in enumerate(names, start=1):
            user_id = settings.BOT_USER_ID - (match_id * 8) - ordinal
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
            elif not user.is_bot:
                raise ValueError(f"Reserved match-bot identity {user_id} belongs to a real user.")
            else:
                user.first_name = name
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
            first_name=choose_bot_display_names(1)[0],
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
