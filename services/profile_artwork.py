"""Resolve profile artwork from the player's existing Level and selected skin.

Green Apple's Level 1–30 mapping remains owned by
``services.player_level_art``. The three additional skins use six files each
under ``assets/profile_skins/<skin-key>/stage_01.jpg`` … ``stage_06.jpg``.
Until those Part 2 assets are present, rendering safely falls back to the
existing Green Apple Level artwork (then to the caller's existing fallback).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sib.services import player_level_art
from sib.services.profile_skin import (
    DEFAULT_PROFILE_SKIN,
    ProfileSkin,
    normalize_profile_skin,
)

PROFILE_SKIN_ART_DIR = (
    Path(__file__).resolve().parent.parent / "assets" / "profile_skins"
)
LEVELS_PER_STAGE = 5
STAGE_COUNT = 6


@dataclass(frozen=True)
class ProfileArtworkResolution:
    """Logical artwork target for one normalized skin and current Level.

    ``path`` is the intended asset path even before the Part 2 art files are
    installed. ``stage`` is None for Green Apple, whose existing mapping is
    one independent file per Level.
    """

    skin: ProfileSkin
    level: int
    stage: Optional[int]
    path: Path


class ProfileArtworkResolver:
    """Single authoritative resolver for profile skin + current Level art."""

    @staticmethod
    def stage_for_level(level) -> int:
        """Map a clamped Level into the six five-level stages (1–6)."""
        bounded_level = player_level_art.clamp_level(level)
        return min(
            STAGE_COUNT,
            ((bounded_level - 1) // LEVELS_PER_STAGE) + 1,
        )

    @classmethod
    def resolve(cls, level, skin=DEFAULT_PROFILE_SKIN) -> ProfileArtworkResolution:
        """Resolve the intended asset path without deriving or changing Level.

        ``level`` must be the already-derived PlayerXPService Level. The
        existing clamp and filename helper keep Green Apple's 30-file mapping
        exactly as it is today.
        """
        bounded_level = player_level_art.clamp_level(level)
        selected = normalize_profile_skin(skin)

        if selected is ProfileSkin.GREEN_APPLE:
            return ProfileArtworkResolution(
                skin=selected,
                level=bounded_level,
                stage=None,
                path=(
                    player_level_art.LEVEL_ART_DIR
                    / player_level_art.level_art_filename(bounded_level)
                ),
            )

        stage = cls.stage_for_level(bounded_level)
        return ProfileArtworkResolution(
            skin=selected,
            level=bounded_level,
            stage=stage,
            path=(
                PROFILE_SKIN_ART_DIR
                / selected.value
                / f"stage_{stage:02d}.jpg"
            ),
        )

    @classmethod
    def profile_photo(
        cls,
        progress,
        skin=DEFAULT_PROFILE_SKIN,
        fallback_photo=None,
    ):
        """Return the selected skin's Telegram photo or a safe legacy fallback.

        The level is read only from the existing ``LevelProgress``. For Green
        Apple this delegates directly to its untouched legacy photo resolver.
        Missing new-skin artwork also uses that resolver until Part 2 supplies
        the stage files.
        """
        selected = normalize_profile_skin(skin)
        if selected is ProfileSkin.GREEN_APPLE:
            return player_level_art.profile_photo(progress, fallback_photo)

        resolution = cls.resolve(getattr(progress, "level", None), selected)
        if resolution.path.is_file():
            from aiogram.types import FSInputFile

            return FSInputFile(resolution.path)

        return player_level_art.profile_photo(progress, fallback_photo)
