"""Card trading handlers (Phase 13 UI).

Trade is a GROUP-facing feature: it is reachable from the group main menu
(«🔁 مبادله کارت») and the whole proposal → accept → double-confirmation
flow happens with inline buttons inside the group, next to the players.

Flow (card ↔ card only):

    hub (apple_trade)
      └─ «➕ پیشنهاد مبادله»            trade_new
          └─ choose MY card            trade_offer:<card_id>
              └─ shared offer message  trade_join:<proposer>:<card>
                  └─ choose MY card    trade_pick:<proposer>:<card>:<card>
                      └─ trade detail  trade_ok / trade_cancel (<id>)

Everything is state-guarded server-side by ``TradeService``: ownership,
same-level rule, daily quota, single pending trade per card, expiry and the
atomic swap. Duplicate button presses are idempotent.
"""

from typing import Optional

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.keyboards.common import danger_button, primary_button, success_button
from sib.database.connection import get_session
from sib.database.models import Character, PlayerCard, Trade, TradeStatus
from sib.services.card_progression import card_attributes
from sib.services.nav import back_label_for, back_target, parse_callback
from sib.services.panel import PanelOwnershipService
from sib.services.player_cards import PlayerCardService
from sib.services.telegram_ui import safe_answer_callback, safe_edit_callback_message
from sib.services.trading import TradeError, TradeService

router = Router(name="trading")

TRADE_PAGE_SIZE = 6


async def _character_name(session, card: PlayerCard) -> str:
    character = await session.get(Character, card.character_id)
    return character.name_fa if character else "نامشخص"


async def _card_label(session, card: PlayerCard) -> str:
    name = await _character_name(session, card)
    return f"{name} (سطح {card.level})"


async def _cards_of_level(session, user_id: int, level: Optional[int] = None) -> list:
    """The user's cards (optionally only one level), newest first."""
    from sqlalchemy import select

    stmt = (
        select(PlayerCard)
        .where(PlayerCard.user_id == user_id)
        .order_by(PlayerCard.id.desc())
    )
    cards = list((await session.scalars(stmt)).all())
    if level is not None:
        cards = [c for c in cards if c.level == level]

    main_card = await PlayerCardService.get_main_card(session, user_id)
    if main_card is not None:
        # Keep the Main Card first as before, but don't promote multiple raw
        # favorite flags if legacy data contains duplicates.
        cards.sort(key=lambda card: card.id != main_card.id)
    return cards


# ---------------------------------------------------------------------------
# Hub («مبادله کارت»)
# ---------------------------------------------------------------------------


async def _pending_view(
    user_id: int,
    *,
    origin: Optional[str] = None,
    chat_type: Optional[str] = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Trade hub: my open offers + limits + how to start a trade."""
    async with get_session() as session:
        trades = await TradeService.list_pending(session, user_id)
        can = await TradeService.can_trade_today(session, user_id)
        my_cards = await _cards_of_level(session, user_id)

        lines = ["🔁 **مبادله کارت**\n"]
        lines.append(
            "سهمیه امروز: " + ("✅ در دسترس" if can else "❌ استفاده‌شده") + " (۱ مبادله در روز)"
        )
        lines.append(f"🎴 کارت‌های قابل مبادله: `{len(my_cards)}`")
        lines.append("")
        lines.append(
            "کارت ↔ کارت، فقط بین کارت‌های **هم‌سطح** و با **تأیید هر دو طرف**. "
            "هر کارت فقط می‌تونه تو یه مبادله‌ی باز باشه."
        )
        lines.append("")

        buttons: list[list[InlineKeyboardButton]] = []
        if my_cards:
            buttons.append(
                [success_button(text="➕ پیشنهاد مبادله", callback_data="trade_new")]
            )

        if not trades:
            lines.append("فعلاً معامله‌ی بازی نداری.")
        else:
            lines.append("**معاملات باز شما:**")
        for trade in trades:
            proposer_card = await session.get(PlayerCard, trade.proposer_card_id)
            recipient_card = await session.get(PlayerCard, trade.recipient_card_id)
            if not proposer_card or not recipient_card:
                continue
            p_name = await _card_label(session, proposer_card)
            r_name = await _card_label(session, recipient_card)
            side = "پیشنهاد شما" if trade.proposer_id == user_id else "پیشنهاد دریافتی"
            lines.append(
                f"#{trade.id} ({side}): {p_name} ⇄ {r_name} | "
                f"تأیید: {'✅' if trade.proposer_confirmed else '⏳'} / "
                f"{'✅' if trade.recipient_confirmed else '⏳'}"
            )
            buttons.append(
                [
                    success_button(
                        text=f"✅ تأیید #{trade.id}", callback_data=f"trade_ok:{trade.id}"
                    ),
                    danger_button(
                        text=f"❌ لغو #{trade.id}", callback_data=f"trade_cancel:{trade.id}"
                    ),
                ]
            )

        buttons.append([primary_button(text="🔄 بروزرسانی", callback_data="trade_refresh")])
    buttons.append(
        [danger_button(text=back_label_for(origin), callback_data=back_target(origin))]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _claim_args(callback: CallbackQuery) -> tuple:
    """(chat_id, message_id) for the panel claim, if available."""
    chat = getattr(callback.message, "chat", None) if callback.message else None
    msg_id = getattr(callback.message, "message_id", None) if callback.message else None
    return (getattr(chat, "id", None), msg_id)


@router.message(Command("trade"))
async def cmd_trade(message: Message, bot: Bot):
    user = message.from_user
    if not user:
        return
    chat = message.chat
    text, kb = await _pending_view(user.id, chat_type=getattr(chat, "type", None))
    sent = await message.reply(text, reply_markup=kb, parse_mode="Markdown")
    if sent is not None and sent.message_id:
        async with get_session() as session:
            await PanelOwnershipService.claim(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                user_id=user.id,
                panel_type="trade",
            )
            await session.commit()


@router.callback_query(F.data.startswith("apple_trade"))
async def handle_apple_trade(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return

    _base, origin = parse_callback(callback.data)
    prev_message_id = getattr(callback.message, "message_id", None)
    chat_type = getattr(getattr(callback.message, "chat", None), "type", None)
    text, kb = await _pending_view(user.id, origin=origin, chat_type=chat_type)
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback,
        bot,
        text,
        reply_markup=kb,
    )

    from sib.services.panel import PanelOwnershipService

    chat_id, msg_id = _claim_args(callback)
    if chat_id and msg_id and callback.from_user:
        async with get_session() as session:
            if prev_message_id and prev_message_id != msg_id:
                await PanelOwnershipService.move_claim(
                    session,
                    chat_id,
                    prev_message_id,
                    msg_id,
                    callback.from_user.id,
                    panel_type="trade",
                )
            else:
                await PanelOwnershipService.claim(
                    session, chat_id, msg_id, callback.from_user.id, panel_type="trade"
                )
            await session.commit()


@router.callback_query(F.data.startswith("trade_refresh"))
async def handle_trade_refresh(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    _base, origin = parse_callback(callback.data)
    chat_type = getattr(getattr(callback.message, "chat", None), "type", None)
    text, kb = await _pending_view(user.id, origin=origin, chat_type=chat_type)
    await safe_answer_callback(callback, "بروزرسانی شد")
    await safe_edit_callback_message(callback, bot, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# Proposal: choose MY card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("trade_new"))
async def handle_trade_new(callback: CallbackQuery, bot: Bot):
    """Pick which of MY cards I want to offer."""
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    parts = base.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    async with get_session() as session:
        cards = await _cards_of_level(session, user.id)

    if not cards:
        await safe_answer_callback(callback, "هنوز کارتی برای مبادله نداری 🍎", show_alert=True)
        return

    total_pages = max(1, (len(cards) + TRADE_PAGE_SIZE - 1) // TRADE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    window = cards[page * TRADE_PAGE_SIZE : (page + 1) * TRADE_PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    async with get_session() as session:
        for card in window:
            label = await _card_label(session, card)
            rows.append(
                [primary_button(text=f"🍎 {label}", callback_data=f"trade_offer:{card.id}")]
            )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(primary_button(text="◀️ قبلی", callback_data=f"trade_new:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(primary_button(text="▶️ بعدی", callback_data=f"trade_new:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([danger_button(text="↩️ بازگشت به مبادله", callback_data="trade_refresh")])

    text = (
        "🤝 **پیشنهاد مبادله**\n\n"
        "کدوم کارتت رو پیشنهاد می‌کنی؟\n"
        "بعد از انتخاب، پیام پیشنهاد تو همین گروه می‌آد و هر بازیکنی که کارت هم‌سطح داشته باشه "
        "می‌تونه قبولش کنه."
    )
    if total_pages > 1:
        text += f"\n\n📖 صفحه {page + 1} از {total_pages}"
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback, bot, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("trade_offer:"))
async def handle_trade_offer(callback: CallbackQuery, bot: Bot):
    """Publish a shared offer message for one of MY cards."""
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    try:
        card_id = int(base.split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return

    async with get_session() as session:
        card = await session.get(PlayerCard, card_id)
        if not card or card.user_id != user.id:
            await safe_answer_callback(
                callback, "این کارت متعلق به شما نیست 🍏", show_alert=True
            )
            return
        label = await _card_label(session, card)
        level = card.level
        character = await session.get(Character, card.character_id)
        attrs = card_attributes(card, character) if character else None
        stats = (
            f"💪{attrs.power} ⚡{attrs.speed} 🧠{attrs.intelligence}" if attrs else "—"
        )

    name = user.first_name or "یک سیبی"
    text = (
        "🤝 **پیشنهاد مبادله کارت**\n\n"
        f"🍎 {name} کارت **{label}** رو برای مبادله گذاشته.\n"
        f"📊 {stats}\n\n"
        f"هر بازیکنی که کارت **سطح {level}** داشته باشه می‌تونه این پیشنهاد رو قبول کنه. "
        "مبادله فقط با تأیید هر دو طرف انجام می‌شه."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                success_button(
                    text="🤝 مبادله می‌کنم",
                    callback_data=f"trade_join:{user.id}:{card_id}",
                )
            ],
            [danger_button(text="✖️ بستن پیشنهاد", callback_data=f"trade_close:{card_id}")],
        ]
    )

    if callback.message is None:
        return
    # A NEW message: the offer is a shared group surface, never a personal
    # panel (everyone with a same-level card must be able to press it).
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await safe_answer_callback(callback, "پیشنهاد مبادله در گروه ثبت شد 🤝")


@router.callback_query(F.data.startswith("trade_close:"))
async def handle_trade_close(callback: CallbackQuery, bot: Bot):
    """The offerer takes their own offer off the table."""
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    try:
        card_id = int(base.split(":")[1])
    except (IndexError, ValueError):
        card_id = None

    closed = False
    if card_id is not None:
        async with get_session() as session:
            card = await session.get(PlayerCard, card_id)
            closed = bool(card and card.user_id == user.id)
    if not closed:
        await safe_answer_callback(
            callback, "فقط صاحب پیشنهاد می‌تونه ببندتش 🍏", show_alert=True
        )
        return
    await safe_answer_callback(callback, "پیشنهاد بسته شد.")
    if callback.message:
        await safe_edit_callback_message(
            callback,
            bot,
            "✖️ **این پیشنهاد مبادله بسته شد.**",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [primary_button(text="🔁 مبادله کارت", callback_data="apple_trade")]
                ]
            ),
        )


# ---------------------------------------------------------------------------
# Proposal: the counterparty picks THEIR card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("trade_join:"))
async def handle_trade_join(callback: CallbackQuery, bot: Bot):
    """Someone accepts the offer: show THEIR same-level cards."""
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    parts = base.split(":")
    try:
        proposer_id = int(parts[1])
        proposer_card_id = int(parts[2])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return

    if proposer_id == user.id:
        await safe_answer_callback(
            callback, "این پیشنهاد خودت است؛ منتظر یک بازیکن دیگر باش 🍏", show_alert=True
        )
        return

    async with get_session() as session:
        proposer_card = await session.get(PlayerCard, proposer_card_id)
        if (
            not proposer_card
            or proposer_card.user_id != proposer_id
        ):
            await safe_answer_callback(
                callback, "این پیشنهاد دیگر معتبر نیست 🍏", show_alert=True
            )
            return
        my_cards = await _cards_of_level(session, user.id, level=proposer_card.level)
        proposer_label = await _card_label(session, proposer_card)

    if not my_cards:
        await safe_answer_callback(
            callback,
            f"برای این مبادله به یک کارت سطح {proposer_card.level} نیاز داری 🍏",
            show_alert=True,
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    async with get_session() as session:
        for card in my_cards[:TRADE_PAGE_SIZE]:
            label = await _card_label(session, card)
            rows.append(
                [
                    success_button(
                        text=f"🍎 {label}",
                        callback_data=f"trade_pick:{proposer_id}:{proposer_card_id}:{card.id}",
                    )
                ]
            )
    text = (
        "🤝 **مبادله**\n\n"
        f"کارت طرف مقابل: **{proposer_label}**\n"
        f"کدام کارت سطح {proposer_card.level} خودت را می‌دهی؟"
    )
    await safe_answer_callback(callback)
    await safe_edit_callback_message(
        callback, bot, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("trade_pick:"))
async def handle_trade_pick(callback: CallbackQuery, bot: Bot):
    """Create the trade from the two chosen cards."""
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    parts = base.split(":")
    try:
        proposer_id = int(parts[1])
        proposer_card_id = int(parts[2])
        my_card_id = int(parts[3])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return

    if proposer_id == user.id:
        await safe_answer_callback(
            callback, "نمی‌توانی پیشنهاد خودت را بپذیری 🍏", show_alert=True
        )
        return

    async with get_session() as session:
        try:
            trade = await TradeService.create(
                session,
                proposer_id=proposer_id,
                recipient_id=user.id,
                proposer_card_id=proposer_card_id,
                recipient_card_id=my_card_id,
            )
            await session.commit()
            trade_id = trade.id
            detail_text, detail_kb = await _trade_detail_view(session, trade, user.id)
        except TradeError as exc:
            await safe_answer_callback(callback, str(exc), show_alert=True)
            return

    await safe_answer_callback(callback, "معامله ثبت شد؛ هر دو طرف باید تأیید کنند 🤝")
    await safe_edit_callback_message(callback, bot, detail_text, reply_markup=detail_kb)

    # One short group notification: the proposer must not miss the trade.
    if callback.message is not None:
        try:
            await callback.message.answer(
                f"🔔 معامله #{trade_id} ثبت شد؛ هر دو طرف با دکمه‌های پیام بالا تأیید کنند.",
            )
        except Exception:  # pragma: no cover - presentation only
            pass


# ---------------------------------------------------------------------------
# Confirmation / cancellation (shared trade messages)
# ---------------------------------------------------------------------------


async def _trade_detail_view(session, trade: Trade, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Shared trade card: both cards + both confirmations + actions."""
    proposer_card = await session.get(PlayerCard, trade.proposer_card_id)
    recipient_card = await session.get(PlayerCard, trade.recipient_card_id)
    proposer_name = await _card_label(session, proposer_card) if proposer_card else "?"
    recipient_name = await _card_label(session, recipient_card) if recipient_card else "?"

    status_line = {
        TradeStatus.PENDING: "⏳ در انتظار تأیید",
        TradeStatus.COMPLETED: "✅ انجام شد",
        TradeStatus.CANCELLED: "❌ لغو شد",
        TradeStatus.EXPIRED: "⌛️ منقضی شد",
        TradeStatus.FAILED: "❌ انجام نشد",
    }.get(trade.status, trade.status)

    lines = [
        f"🔁 **معامله #{trade.id}** — {status_line}",
        "",
        f"🍎 پیشنهاد‌دهنده: **{proposer_name}**",
        f"✅ تأیید: {'✅' if trade.proposer_confirmed else '⏳'}",
        "",
        f"🍏 طرف مقابل: **{recipient_name}**",
        f"✅ تأیید: {'✅' if trade.recipient_confirmed else '⏳'}",
        "",
    ]

    is_participant = viewer_id in (trade.proposer_id, trade.recipient_id)
    rows: list[list[InlineKeyboardButton]] = []
    if trade.status == TradeStatus.PENDING and is_participant:
        lines.append("برای انجام مبادله، هر دو طرف باید «تأیید» را بزنند.")
        rows.append(
            [
                success_button(text="✅ تأیید مبادله", callback_data=f"trade_ok:{trade.id}"),
                danger_button(text="❌ لغو", callback_data=f"trade_cancel:{trade.id}"),
            ]
        )
    elif trade.status == TradeStatus.COMPLETED:
        lines.append("🎉 کارت‌ها جابه‌جا شدند.")
    elif trade.status == TradeStatus.FAILED:
        lines.append("این مبادله انجام نشد؛ کارت‌ها دست‌نخورده ماندند و سهمیه امروزتان آزاد است.")
    elif not is_participant:
        lines.append("این معامله بین دو بازیکن دیگر است.")

    rows.append([primary_button(text="🔄 بروزرسانی", callback_data=f"trade_show:{trade.id}")])
    rows.append([primary_button(text="🔁 مبادله کارت", callback_data="apple_trade")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_after_transition(
    callback: CallbackQuery, bot: Bot, trade_id: int, user_id: int, answer: str
) -> None:
    """Re-render after confirm/cancel.

    The SAME trade can be pressed from the personal hub (owner-scoped panel)
    or from a shared group trade card. Whichever message was pressed keeps
    its own identity: the hub stays a hub, the shared card stays a trade card.
    """
    await safe_answer_callback(callback, answer)

    async with get_session() as session:
        trade = await TradeService.get_trade(session, trade_id)
        if trade is None:
            return
        detail_text, detail_kb = await _trade_detail_view(session, trade, user_id)

    chat_id, msg_id = _claim_args(callback)
    on_personal_hub = False
    if chat_id and msg_id:
        from sib.services.panel import PanelOwnershipService

        async with get_session() as session:
            owner_id, panel_type = await PanelOwnershipService.get_panel_context(
                session, chat_id, msg_id, user_id
            )
            on_personal_hub = panel_type == "trade" and owner_id == user_id

    if on_personal_hub:
        hub_text, hub_kb = await _pending_view(user_id)
        await safe_edit_callback_message(callback, bot, hub_text, reply_markup=hub_kb)
    else:
        await safe_edit_callback_message(callback, bot, detail_text, reply_markup=detail_kb)


@router.callback_query(F.data.startswith("trade_ok:"))
async def handle_trade_ok(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    try:
        trade_id = int(base.split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return

    from sib.services.player_xp import PlayerXPService

    async with get_session() as session:
        PlayerXPService.set_notification_chat(
            session,
            getattr(getattr(callback.message, "chat", None), "id", None),
        )
        try:
            trade = await TradeService.confirm(session, trade_id, user.id)
            await session.commit()
        except TradeError as exc:
            await safe_answer_callback(callback, str(exc), show_alert=True)
            return

    if trade.status == TradeStatus.COMPLETED:
        await PlayerXPService.deliver_pending_notifications(
            bot,
            fallback_chat_id=getattr(getattr(callback.message, "chat", None), "id", None),
        )
        answer = "مبادله انجام شد! 🎉"
    elif user.id == trade.proposer_id and trade.proposer_confirmed:
        answer = "تأیید شما ثبت شد؛ منتظر تأیید طرف مقابل."
    elif user.id == trade.recipient_id and trade.recipient_confirmed:
        answer = "تأیید شما ثبت شد؛ منتظر تأیید طرف مقابل."
    else:
        answer = "تأیید ثبت شد."
    await _render_after_transition(callback, bot, trade_id, user.id, answer)


@router.callback_query(F.data.startswith("trade_cancel:"))
async def handle_trade_cancel(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    try:
        trade_id = int(base.split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return

    async with get_session() as session:
        try:
            await TradeService.cancel(session, trade_id, user.id)
            await session.commit()
        except TradeError as exc:
            await safe_answer_callback(callback, str(exc), show_alert=True)
            return

    await _render_after_transition(callback, bot, trade_id, user.id, "معامله لغو شد.")


@router.callback_query(F.data.startswith("trade_show:"))
async def handle_trade_show(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    if not user:
        return
    base, _origin = parse_callback(callback.data)
    try:
        trade_id = int(base.split(":")[1])
    except (IndexError, ValueError):
        await safe_answer_callback(callback, "درخواست نامعتبر است 🍏", show_alert=True)
        return

    async with get_session() as session:
        trade = await TradeService.get_trade(session, trade_id)
        if trade is None:
            await safe_answer_callback(callback, "این معامله پیدا نشد 🍏", show_alert=True)
            return
        detail_text, detail_kb = await _trade_detail_view(session, trade, user.id)

    await safe_answer_callback(callback, "بروزرسانی شد")
    await safe_edit_callback_message(callback, bot, detail_text, reply_markup=detail_kb)
