"""Canonical profile-skin choices and persisted per-user selection.

A skin is presentation-only: selecting one stores a stable key on the User
record and never reads or writes XP/progression. Unknown persisted keys are
normalized to Green Apple so an unsupported value can never break a profile.
"""

from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.models import User


class ProfileSkin(str, Enum):
    """Stable storage keys for the four selectable profile skins."""

    GREEN_APPLE = "green_apple"
    RED_APPLE = "red_apple"
    GOLDEN_APPLE = "golden_apple"
    MOON_APPLE = "moon_apple"


DEFAULT_PROFILE_SKIN = ProfileSkin.GREEN_APPLE
PROFILE_SKINS = tuple(ProfileSkin)

_PROFILE_SKIN_LABELS = {
    ProfileSkin.GREEN_APPLE: "Green Apple",
    ProfileSkin.RED_APPLE: "Red Apple",
    ProfileSkin.GOLDEN_APPLE: "Golden Apple",
    ProfileSkin.MOON_APPLE: "Moon Apple",
}


def normalize_profile_skin(value) -> ProfileSkin:
    """Return a supported skin, safely defaulting missing/unknown values.

    This is deliberately tolerant because older accounts, imports, or a
    future skin key may not match the current allow-list. Rendering should
    always remain available and fall back to Green Apple in those cases.
    """
    if isinstance(value, ProfileSkin):
        return value
    if value is None:
        return DEFAULT_PROFILE_SKIN
    try:
        return ProfileSkin(str(value).strip().lower())
    except (TypeError, ValueError):
        return DEFAULT_PROFILE_SKIN


def require_profile_skin(value) -> ProfileSkin:
    """Parse a user-requested skin strictly (unlike persisted-value reads)."""
    if isinstance(value, ProfileSkin):
        return value
    if not isinstance(value, str):
        raise ValueError("A valid profile skin is required")
    try:
        return ProfileSkin(value.strip().lower())
    except ValueError as exc:
        raise ValueError(f"Unknown profile skin: {value!r}") from exc


def profile_skin_label(value) -> str:
    """Human-readable display name for a normalized profile skin."""
    return _PROFILE_SKIN_LABELS[normalize_profile_skin(value)]


class ProfileSkinService:
    """The read/write boundary for a user's selected profile skin."""

    @staticmethod
    async def get_selected_skin(
        session: AsyncSession, user_id: int
    ) -> ProfileSkin:
        """Get the user's skin; absent accounts/values behave as Green Apple."""
        user = await session.get(User, int(user_id))
        return normalize_profile_skin(
            getattr(user, "profile_skin", None) if user is not None else None
        )

    @staticmethod
    async def set_selected_skin(
        session: AsyncSession, user_id: int, skin
    ) -> ProfileSkin:
        """Persist one supported choice without touching progression state."""
        selected = require_profile_skin(skin)
        user = await session.get(User, int(user_id))
        if user is None:
            raise LookupError(f"Cannot select a profile skin for missing user {user_id}")
        user.profile_skin = selected.value
        await session.flush()
        return selected
