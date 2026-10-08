"""Inline keyboard and compact callback contracts for Invasion screens."""

from aiogram.types import InlineKeyboardMarkup

from sib.database.models import InvasionStatus
from sib.keyboards.common import primary_button, success_button


_INVASION_CALLBACK_PREFIX = "invasion"
_VALID_ACTIONS = frozenset({"join", "status", "brace"})
_MAX_SIGNED_BIGINT = (1 << 63) - 1
_MAX_SIGNED_INTEGER = (1 << 31) - 1


def invasion_callback_data(
    action: str,
    invasion_id: int,
    *,
    wave_sequence: int | None = None,
) -> str:
    """Build a compact callback with only persisted Invasion/Wave identity."""
    if action not in _VALID_ACTIONS:
        raise ValueError("unsupported Invasion callback action")
    if (
        isinstance(invasion_id, bool)
        or not isinstance(invasion_id, int)
        or not 0 < invasion_id <= _MAX_SIGNED_BIGINT
    ):
        raise ValueError("invasion_id must be a positive signed 64-bit integer")

    if action == "brace":
        if (
            isinstance(wave_sequence, bool)
            or not isinstance(wave_sequence, int)
            or not 0 < wave_sequence <= _MAX_SIGNED_INTEGER
        ):
            raise ValueError("brace callbacks require a positive Wave sequence")
        data = f"{_INVASION_CALLBACK_PREFIX}:brace:{invasion_id}:{wave_sequence}"
    else:
        if wave_sequence is not None:
            raise ValueError("only brace callbacks may include a Wave sequence")
        data = f"{_INVASION_CALLBACK_PREFIX}:{action}:{invasion_id}"

    if len(data.encode("utf-8")) > 64:  # Telegram callback_data limit
        raise ValueError("Invasion callback_data exceeds Telegram's 64-byte limit")
    return data


def _canonical_positive_int(raw_value: str, maximum: int) -> int | None:
    if not raw_value.isascii() or not raw_value.isdecimal():
        return None
    try:
        value = int(raw_value)
    except ValueError:  # pragma: no cover - guarded by isdecimal
        return None
    if value <= 0 or value > maximum or str(value) != raw_value:
        return None
    return value


def parse_invasion_callback(
    data: str | None,
) -> tuple[str, int] | tuple[str, int, int] | None:
    """Parse exact callbacks: ``invasion:<action>:<id>[:<wave>]``."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > 64:
        return None
    parts = data.split(":")
    if not parts or parts[0] != _INVASION_CALLBACK_PREFIX:
        return None

    if len(parts) == 3 and parts[1] in {"join", "status"}:
        invasion_id = _canonical_positive_int(parts[2], _MAX_SIGNED_BIGINT)
        return (parts[1], invasion_id) if invasion_id is not None else None

    if len(parts) == 4 and parts[1] == "brace":
        invasion_id = _canonical_positive_int(parts[2], _MAX_SIGNED_BIGINT)
        wave_sequence = _canonical_positive_int(parts[3], _MAX_SIGNED_INTEGER)
        if invasion_id is not None and wave_sequence is not None:
            return "brace", invasion_id, wave_sequence
    return None


def get_invasion_keyboard(
    invasion_id: int,
    status: str,
    *,
    current_wave_sequence: int | None = None,
    wave_actionable: bool = False,
    viewer_is_participant: bool | None = None,
) -> InlineKeyboardMarkup:
    """Render actions from persisted screen state; callbacks remain authoritative."""
    rows = []
    if status != InvasionStatus.FINISHED and viewer_is_participant is not True:
        rows.append(
            [
                success_button(
                    text="🛡 پیوستن به دفاع",
                    callback_data=invasion_callback_data("join", invasion_id),
                )
            ]
        )

    if (
        status == InvasionStatus.ACTIVE
        and current_wave_sequence is not None
        and wave_actionable
    ):
        # A Telegram Group has one shared inline keyboard. Keep Brace available
        # for other participants even when this viewer has used their action;
        # the participant-specific screen text reports availability, while the
        # domain rejects that viewer's repeat attempt authoritatively.
        rows.append(
            [
                success_button(
                    text="🛡 Brace",
                    callback_data=invasion_callback_data(
                        "brace",
                        invasion_id,
                        wave_sequence=current_wave_sequence,
                    ),
                )
            ]
        )

    rows.append(
        [
            primary_button(
                text=(
                    "📋 نتیجه نهایی"
                    if status == InvasionStatus.FINISHED
                    else "📊 وضعیت یورش"
                ),
                callback_data=invasion_callback_data("status", invasion_id),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "get_invasion_keyboard",
    "invasion_callback_data",
    "parse_invasion_callback",
]
