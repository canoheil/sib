"""Card screens: view a card, open its Card Upgrade screen, upgrade it.

    apple_cards  ->  card_panel:<id>  ->  card_upgrade:<id>  ->  card_upgrade_do:<id>:<level>

Handlers only parse callbacks, call the services and render. All rules
(ownership, levels, costs, atomic spend) live in ``CardUpgradeService``.
"""

from typing import Optional

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from sib.database.connection import get_session
from sib.database.models import Character, PlayerCard
from sib.keyboards.cards import get_card_detail_keyboard, get_card_upgrade_keyboard
from sib.services.card_upgrade import (
    CardUpgradeError,
    CardUpgradeService,
    InsufficientResourcesError,
    StaleUpgradeError,
)
from sib.services.card_upgrade_render import (
    missing_alert_text,
    remember_art,
    render_upgrade_screen,
    resolve_art,
    success_toast,
)
from sib.services.character_art import photo_arg, resolve_character_photo
from sib.services.panel import PanelOwnershipService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.wiki import CollectionService

router = Router(name="cards")

PANEL_TYPE = "card_panel"
NOT_FOUND_TEXT = "کارت پیدا نشد."


def _parse_ids(data: Optional[str], expected_parts: int) -> Optional[list[int]]:
    """Integer segments after the callback prefix, or None when malformed."""
    parts = (data or "").split(":")[1:]
    if len(parts) != expected_parts:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


async def _claim(callback: CallbackQuery, user_id: int, prev_message_id) -> None:
    """Bind the (possibly replaced) panel message to its opener."""
    chat = getattr(callback.message, "chat", None)
    msg_id = getattr(callback.message, "message_id", None)
    if chat is None or not msg_id:
        return
    async with get_session() as session:
        if prev_message_id and prev_message_id != msg_id:
            await PanelOwnershipService.move_claim(
                session, chat.id, prev_message_id, msg_id, user_id, panel_type=PANEL_TYPE
            )
        else:
            await PanelOwnershipService.claim(
                session, chat.id, msg_id, user_id, panel_type=PANEL_TYPE
            )
        await session.commit()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
async def _card_view(user_id: int, card_id: int):
    """(text, keyboard, photo) of the view-only card screen."""
    async with get_session() as session:
        detail = await CollectionService.render_card_detail(session, user_id, card_id)
        photo = None
        if detail:
            card = await session.get(PlayerCard, card_id)
            character = await session.get(Character, card.character_id)
            if character is not None:
                file_id, fs_path = await resolve_character_photo(session, character)
                photo = photo_arg(file_id, fs_path)
    if not detail:
        return NOT_FOUND_TEXT, InlineKeyboardMarkup(inline_keyboard=[]), None
    return detail, get_card_detail_keyboard(card_id), photo


async def _upgrade_view(user_id: int, card_id: int):
    """(text, keyboard, photo) of the Card Upgrade screen."""
    async with get_session() as session:
        try:
            preview = await CardUpgradeService.preview(session, user_id, card_id)
        except CardUpgradeError:
            return NOT_FOUND_TEXT, InlineKeyboardMarkup(inline_keyboard=[]), None
        photo = await resolve_art(session)
    kb = get_card_upgrade_keyboard(
        card_id, preview.level, can_upgrade=not preview.is_max_level
    )
    return render_upgrade_screen(preview), kb, photo


async def _show(callback: CallbackQuery, bot: Bot, user_id: int, view) -> None:
    text, kb, photo = view
    prev_message_id = getattr(callback.message, "message_id", None)
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb, photo=photo)
    if photo is not None:
        remember_art(callback.message)
    await _claim(callback, user_id, prev_message_id)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("card_panel:"))
async def handle_card_panel(callback: CallbackQuery, bot: Bot):
    """View-only card screen (+ the single ⬆️ Card Upgrade action)."""
    ids = _parse_ids(callback.data, 1)
    user = callback.from_user
    if not user or ids is None:
        await safe_answer_callback(callback, NOT_FOUND_TEXT, show_alert=True)
        return
    view = await _card_view(user.id, ids[0])
    await safe_answer_callback(callback)
    await _show(callback, bot, user.id, view)


@router.callback_query(F.data.startswith("card_upgrade:"))
async def handle_card_upgrade_screen(callback: CallbackQuery, bot: Bot):
    """Dedicated Card Upgrade screen."""
    ids = _parse_ids(callback.data, 1)
    user = callback.from_user
    if not user or ids is None:
        await safe_answer_callback(callback, NOT_FOUND_TEXT, show_alert=True)
        return
    view = await _upgrade_view(user.id, ids[0])
    await safe_answer_callback(callback)
    await _show(callback, bot, user.id, view)


@router.callback_query(F.data.startswith("card_upgrade_do:"))
async def handle_card_upgrade_do(callback: CallbackQuery, bot: Bot):
    """Perform ONE upgrade level (atomic, see CardUpgradeService)."""
    ids = _parse_ids(callback.data, 2)
    user = callback.from_user
    if not user or ids is None:
        await safe_answer_callback(callback, NOT_FOUND_TEXT, show_alert=True)
        return
    card_id, seen_level = ids

    toast: str = ""
    alert = False
    async with get_session() as session:
        try:
            result = await CardUpgradeService.upgrade(
                session, user.id, card_id, expected_level=seen_level
            )
            toast = success_toast(result)
        except InsufficientResourcesError as exc:
            toast, alert = missing_alert_text(exc), True
        except StaleUpgradeError as exc:
            toast = str(exc)
        except CardUpgradeError as exc:
            toast, alert = str(exc), True

    # Always re-render from the authoritative state (also after refusals).
    view = await _upgrade_view(user.id, card_id)
    await safe_answer_callback(callback, toast, show_alert=alert)
    await _show(callback, bot, user.id, view)
