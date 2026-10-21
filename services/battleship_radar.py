"""Private radar delivery for friendly Battleship matches.

A friendly match lives in ONE Telegram group and shares ONE board message
between both teams, so Battleship's per-side radar (the shooting grid and the
player's own guess map) may never be rendered there. This module owns the two
surfaces that replaced it:

1. the **neutral public board** (``public_view``): rosters, both fleets'
   remaining cells, the ordered shot history and whose turn it is - every
   fact on it is public;
2. the **private radar panel** (``present_panel``): one player's grid,
   delivered as a Bot API *ephemeral* message (visible only to that player),
   or - when ephemeral delivery is unavailable - as a normal DM, or finally a
   callback alert with no board content at all.

Delivery contract (same philosophy as the High Card private card):

* the game state is committed before any of this runs, so a delivery failure
  can never affect the match;
* delivery is best-effort and ordered by privacy: ephemeral -> DM -> alert;
* **no failure path may ever post the radar into the group chat.**
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from aiogram import Bot
from aiogram.types import (
    CallbackQuery,
    EphemeralMessageParameters,
    InlineKeyboardMarkup,
    Message,
)

from sib.core.logging import Events, log_event, logger
from sib.keyboards.party import get_friendly_rematch_keyboard

RADAR_CALLBACK_PREFIX = "bs_radar:"
SHOT_CALLBACK_PREFIX = "bs_shot:"
CLOSE_CALLBACK_PREFIX = "bs_close:"
# Legacy boards rendered before this change carry live grid cells in the
# GROUP message. They stay playable, but they are never rendered again.
LEGACY_SHOT_CALLBACK_PREFIX = "bs_action:"

#: callback.answer() text is capped by Telegram; the alert must fit.
ALERT_MAX_LENGTH = 200

DELIVERY_EPHEMERAL = "ephemeral"
DELIVERY_DM = "dm"
DELIVERY_FAILED = "failed"


def supports_private_radar(engine: Any) -> bool:
    """True when the engine renders a neutral board + a private panel."""
    return bool(getattr(engine, "friendly_private_radar", False))


def is_radar_participant(engine: Any, state: Dict[str, Any], player_id: int) -> bool:
    """True when ``player_id`` is a player of this match (never a spectator).

    Defence in depth: the panel renderers must never be reached for someone
    who has no team, otherwise they would see the default team's radar.
    """
    team_code_of = getattr(engine, "team_code_of", None)
    if team_code_of is None:  # pragma: no cover - defensive
        return False
    return team_code_of(state, player_id) is not None


@dataclass
class PanelDelivery:
    """Result of one private-panel delivery attempt."""

    mode: str
    chat_id: Optional[int] = None
    ephemeral_message_id: Optional[int] = None
    message_id: Optional[int] = None
    error: Optional[str] = None

    @property
    def delivered(self) -> bool:
        return self.mode != DELIVERY_FAILED


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def public_view(
    engine: Any,
    match_id: int,
    state: Dict[str, Any],
    *,
    finished: bool = False,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Neutral group-board (text, keyboard) for a friendly match.

    ``render_public`` is already viewer-neutral and carries its own result
    line, so it must NOT be post-processed by ``shape_friendly_final_text``
    (that would duplicate the winner line).
    """
    text = engine.render_public(state, finished=finished)
    if finished:
        return text, get_friendly_rematch_keyboard(match_id)
    return text, engine.get_public_keyboard(match_id)


def panel_view(
    engine: Any,
    match_id: int,
    state: Dict[str, Any],
    player_id: int,
    *,
    notice: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Private radar panel (text, keyboard) for one player."""
    text = engine.render_radar_panel(state, player_id, notice=notice)
    keyboard = engine.get_radar_panel_keyboard(state, match_id, player_id)
    return text, keyboard


def panel_alert_hint(state: Dict[str, Any], player_id: int) -> str:
    """≤200-character alert used when no private channel could be reached."""
    is_my_turn = state.get("current_player_id") == player_id
    if state.get("finished"):
        text = "🏁 بازی تمام شد. برای دیدن نتیجه «🔄 بروزرسانی» را بزن."
    elif is_my_turn:
        text = (
            "🎯 رادار خصوصی باز نشد. «🔄 بروزرسانی» را بزن و دوباره «رادار من» "
            "را امتحان کن. محدودهٔ شلیک: A1 تا H8."
        )
    else:
        text = "⏳ رادار خصوصی باز نشد و الان نوبت تو نیست. «🔄 بروزرسانی» را بزن."
    return text[:ALERT_MAX_LENGTH]


# ---------------------------------------------------------------------------
# Message identity helpers
# ---------------------------------------------------------------------------


def ephemeral_message_id(message: Any) -> Optional[int]:
    """Ephemeral id of a message, or None for a normal (group/DM) message.

    Ephemeral messages always carry ``message_id == 0`` and are addressed by
    ``(chat_id, receiver_user_id, ephemeral_message_id)`` instead.
    """
    if message is None:
        return None
    value = getattr(message, "ephemeral_message_id", None)
    try:
        return int(value) if value else None
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def is_private_message(message: Any) -> bool:
    """True for a DM (a normal private-chat message, not an ephemeral one)."""
    if message is None or ephemeral_message_id(message):
        return False
    chat = getattr(message, "chat", None)
    return getattr(chat, "type", None) == "private"


def _chat_id_of(message: Any) -> Optional[int]:
    chat = getattr(message, "chat", None) if message is not None else None
    value = getattr(chat, "id", None)
    return int(value) if value is not None else None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def _send_ephemeral(
    bot: Bot,
    *,
    chat_id: int,
    player_id: int,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup],
    callback_query_id: Optional[str],
    replace: bool,
) -> Message:
    """One Bot API ephemeral send (Bot API 10.3 parameter object)."""
    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="Markdown",
        ephemeral_message_parameters=EphemeralMessageParameters(
            receiver_user_id=player_id,
            callback_query_id=callback_query_id,
            replace_callback_query_message=replace,
        ),
    )


async def _send_dm(
    bot: Bot,
    *,
    player_id: int,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup],
) -> Message:
    return await bot.send_message(
        chat_id=player_id,
        text=text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def present_panel(
    bot: Bot,
    callback: CallbackQuery,
    *,
    chat_id: Optional[int],
    player_id: int,
    engine: Any,
    match_id: int,
    state: Dict[str, Any],
    notice: Optional[str] = None,
) -> PanelDelivery:
    """Show the radar panel to ``player_id`` only.

    Order of attempts:

    1. the tap came FROM a panel -> edit that panel in place (no resend);
    2. ephemeral message replacing the tapped message (``replace_...=True``);
    3. ephemeral message as a separate whisper (``replace_...=False``);
    4. normal DM;
    5. give up -> the caller answers with a ≤200-char alert (no board text).
    """
    if not is_radar_participant(engine, state, player_id):
        # A spectator (or a forged callback) never gets a board.
        return PanelDelivery(DELIVERY_FAILED, chat_id=chat_id, error="not a player")

    text, keyboard = panel_view(
        engine, match_id, state, player_id, notice=notice
    )
    tapped = getattr(callback, "message", None)
    origin_ephemeral = ephemeral_message_id(tapped) is not None

    # 1. The tap came FROM a panel (its grid, or «بروزرسانی رادار»): edit that
    #    exact message in place. Bot API requires replace_callback_query_message
    #    to be False for callbacks originating from an ephemeral message.
    if origin_ephemeral or is_private_message(tapped):
        if await edit_panel(
            bot,
            tapped,
            player_id=player_id,
            text=text,
            keyboard=keyboard,
        ):
            return PanelDelivery(
                DELIVERY_EPHEMERAL,
                chat_id=_chat_id_of(tapped) or chat_id,
                ephemeral_message_id=ephemeral_message_id(tapped),
            )
        # Expired/vanished panel: fall through to a fresh delivery.

    if chat_id is not None:
        # «replace» is only legal for callbacks that did NOT originate from an
        # ephemeral message; try that form first, then the other one.
        for replace in ((False, True) if origin_ephemeral else (True, False)):
            try:
                sent = await _send_ephemeral(
                    bot,
                    chat_id=chat_id,
                    player_id=player_id,
                    text=text,
                    keyboard=keyboard,
                    callback_query_id=getattr(callback, "id", None),
                    replace=replace,
                )
                return PanelDelivery(
                    DELIVERY_EPHEMERAL,
                    chat_id=chat_id,
                    ephemeral_message_id=ephemeral_message_id(sent),
                )
            except Exception as exc:
                logger.warning(
                    "Ephemeral radar delivery failed for match %s/user %s "
                    "(replace=%s): %s",
                    match_id,
                    player_id,
                    replace,
                    exc,
                )

    # 3. Normal DM (private by definition; needs the user to have started the bot).
    try:
        sent = await _send_dm(
            bot, player_id=player_id, text=text, keyboard=keyboard
        )
        log_event(
            Events.BATTLESHIP_RADAR_EVENT,
            {
                "match_id": match_id,
                "user_id": player_id,
                "action": "dm_fallback",
            },
        )
        return PanelDelivery(
            DELIVERY_DM,
            chat_id=player_id,
            message_id=getattr(sent, "message_id", None),
        )
    except Exception as exc:
        logger.warning(
            "DM radar delivery failed for match %s/user %s: %s",
            match_id,
            player_id,
            exc,
        )
        return PanelDelivery(DELIVERY_FAILED, chat_id=chat_id, error=str(exc))


async def edit_panel(
    bot: Bot,
    message: Any,
    *,
    player_id: int,
    text: str,
    keyboard: Optional[InlineKeyboardMarkup],
) -> bool:
    """Edit an already delivered panel (ephemeral or DM). Never raises.

    A normal Telegram edit helper must NOT be used here: an ephemeral message
    has ``message_id == 0`` and is addressed by its own id, and mixing the two
    would either fail or corrupt the group board pointer.
    """
    if message is None:
        return False

    target_id = ephemeral_message_id(message)
    if target_id:
        chat_id = _chat_id_of(message)
        if chat_id is None:
            return False
        for parse_mode in ("Markdown", None):
            try:
                await bot.edit_ephemeral_message_text(
                    chat_id=chat_id,
                    receiver_user_id=player_id,
                    ephemeral_message_id=target_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                )
                return True
            except Exception as exc:
                logger.warning(
                    "Ephemeral radar edit failed for user %s (parse_mode=%s): %s",
                    player_id,
                    parse_mode,
                    exc,
                )
        return False

    if not is_private_message(message):
        # A group message is NEVER a panel: editing it with radar content is
        # exactly the leak this module exists to prevent.
        return False

    chat_id = _chat_id_of(message)
    target_id = getattr(message, "message_id", None)
    if chat_id is None or not target_id:
        return False
    for parse_mode in ("Markdown", None):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=target_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=parse_mode,
            )
            return True
        except Exception as exc:
            logger.warning(
                "DM radar edit failed for user %s (parse_mode=%s): %s",
                player_id,
                parse_mode,
                exc,
            )
    return False


async def close_panel(bot: Bot, message: Any, *, player_id: int) -> bool:
    """Remove the caller's private panel. Never raises."""
    if message is None:
        return False
    chat_id = _chat_id_of(message)
    target_id = ephemeral_message_id(message)
    if chat_id is None:
        return False
    try:
        if target_id:
            await bot.delete_ephemeral_message(
                chat_id=chat_id,
                receiver_user_id=player_id,
                ephemeral_message_id=target_id,
            )
            return True
        if is_private_message(message) and getattr(message, "message_id", None):
            await bot.delete_message(
                chat_id=chat_id, message_id=message.message_id
            )
            return True
    except Exception as exc:
        logger.warning("Radar panel close failed for user %s: %s", player_id, exc)
    return False


# ---------------------------------------------------------------------------
# Small DB-free helpers used by the handlers
# ---------------------------------------------------------------------------


def shot_notice(engine: Any, state: Dict[str, Any], player_id: int) -> Optional[str]:
    """«شما به B4 زدید: …» line for the player's most recent own shot."""
    last = engine.last_shot_of(state, player_id)
    if not last or last.get("r") is None or last.get("c") is None:
        return None
    return (
        f"🎯 شما به {engine._coord_label(int(last['r']), int(last['c']))} زدید: "
        f"{engine.shot_result_text(last.get('result', 'miss'), last.get('sunk_size'))}"
    )


def parse_match_id(data: Optional[str]) -> Optional[int]:
    """Match id of a ``prefix:{match_id}[:...]`` callback payload."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def parse_shot(data: Optional[str]) -> Optional[Tuple[int, int, int]]:
    """``(match_id, r, c)`` of a ``bs_shot:{match}:{r}:{c}`` payload."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 4:
        return None
    try:
        return int(parts[1]), int(parts[2]), int(parts[3])
    except (TypeError, ValueError):
        return None


def load_state(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """Deserialize a stored ``game_state`` blob safely."""
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None
