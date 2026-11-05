"""HW2 tests: interactive «حمله» reply attack, public hits, boosted economy,
clearer reward track, Top-3 board, and the phase-evolving boss artwork.

Phase HW2 overhauled the Halloween event:

* the ⚔️ attack **button** is gone — players reply to the Boss Menu with the
  exact word «حمله» (TASK 1);
* every landed hit is announced **publicly** to the group (TASK 2);
* seed loot pays **3–6** seeds and the menu advertises the **exact** cooldown
  (TASK 3), with all four milestone rewards multiplied;
* the Reward Track reads cleanly (🟢 / 🔒 / ✅), the menu shows the **Top-3**
  damage dealers and the Main-Card/crit hint (TASK 4);
* the boss **artwork evolves** at 75% / 50% / 25% HP (TASK 5);
* ``/hw_start`` fires the hype launch broadcast (TASK 6).
"""

from datetime import datetime, timedelta, timezone
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, FSInputFile, Message
import pytest
from sqlalchemy import select

from sib.core.text import format_html
from sib.database.connection import get_session, seed_characters
from sib.database.models import (
    Character,
    Group,
    HalloweenAttack,
    HalloweenEventState,
    HalloweenLootDrop,
    PlayerCard,
)
from sib.handlers.halloween import (
    ATTACK_CRIT_LINE,
    ATTACK_DEFEAT_LINE,
    ATTACK_EVOLUTION_LINE,
    ATTACK_INSTRUCTION,
    ATTACK_TRIGGER_WORD,
    BOSS_MENU_TITLE,
    _resolve_boss_photo,
    format_boss_menu_caption,
    format_reward_track_caption,
    handle_halloween_attack,
    handle_halloween_attack_trigger,
    handle_halloween_claim_milestone,
    handle_halloween_command,
    handle_halloween_group_drop_claim,
    handle_halloween_loot,
    handle_halloween_rewards,
)
from sib.keyboards.halloween import (
    get_halloween_main_keyboard,
    get_halloween_reward_track_keyboard,
    get_loot_drop_keyboard,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.halloween import (
    DAY7_WEAKENING_ANNOUNCEMENT,
    DEFEAT_ANNOUNCEMENT,
    END_ANNOUNCEMENT,
    LOOT_COOLDOWN_ALERT,
    MILESTONE_REWARDS,
    MILESTONE_REWARD_LABELS,
    NO_SEEDS_ALERT,
    SEED_LOOT_MAX,
    SEED_LOOT_MIN,
    START_ANNOUNCEMENT,
    START_TEST_CONFIRMATION,
    SeedLootStatus,
    HalloweenAlreadyClaimedError,
    HalloweenCooldownError,
    HalloweenMilestoneLockedError,
    HalloweenService,
    boss_art_for_phase,
    format_duration_fa,
    milestone_reward_line,
    phase_for_hp,
    seed_loot_range_fa,
)
from sib.services.items import ItemService
from sib.services.maintenance.service import MaintenanceService
from sib.services.panel import PanelOwnershipService
from sib.services.player_cards import PlayerCardService
from sib.services.sib_claim import fa_digits
from sib.tests.test_parties import create_test_user

GROUP_ID = -100777
MENU_MESSAGE_ID = 501


# ---------------------------------------------------------------------------
# Mocks / helpers
# ---------------------------------------------------------------------------


def _mock_group_message(user_id: int, text: str = "هالووین", chat_id: int = GROUP_ID):
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = SimpleNamespace(
        id=user_id,
        first_name="شکارچی",
        last_name=None,
        username="hunter",
        language_code="fa",
        is_bot=False,
    )
    msg.chat = SimpleNamespace(
        id=chat_id,
        type="supergroup",
        title="گروه شکارچیان هالووین",
        username=None,
    )
    sent = SimpleNamespace(message_id=MENU_MESSAGE_ID, chat=msg.chat, content_type="photo")
    msg.reply_to_message = None
    msg.answer_photo = AsyncMock(return_value=sent)
    msg.answer = AsyncMock(return_value=sent)
    return msg


def _mock_attack_reply(
    user_id: int,
    *,
    menu_message_id: int = MENU_MESSAGE_ID,
    menu_caption: str | None = BOSS_MENU_TITLE,
    text: str = ATTACK_TRIGGER_WORD,
    chat_id: int = GROUP_ID,
):
    """A group message from ``user_id`` replying to the Boss Menu message."""
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = SimpleNamespace(
        id=user_id,
        first_name="شکارچی",
        last_name=None,
        username="hunter",
        language_code="fa",
        is_bot=False,
    )
    msg.chat = SimpleNamespace(
        id=chat_id, type="supergroup", title="گروه شکارچیان هالووین", username=None
    )
    msg.reply_to_message = SimpleNamespace(
        message_id=menu_message_id,
        caption=menu_caption,
        text=None,
        chat=msg.chat,
        content_type="photo",
    )
    msg.answer = AsyncMock()
    msg.answer_photo = AsyncMock()
    return msg


def _mock_bot(chat_id: int = GROUP_ID):
    chat = SimpleNamespace(id=chat_id, type="supergroup", title="گروه", username=None)
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=998, chat=chat))
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=999, chat=chat))
    bot.edit_message_media = AsyncMock()
    bot.edit_message_caption = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


def _mock_callback(
    user_id: int,
    data: str,
    chat_id: int = GROUP_ID,
    message_id: int = MENU_MESSAGE_ID,
):
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = SimpleNamespace(
        id=user_id,
        first_name="شکارچی",
        last_name=None,
        username="hunter",
        language_code="fa",
        is_bot=False,
    )
    msg = MagicMock()
    msg.chat = SimpleNamespace(
        id=chat_id, type="supergroup", title="گروه شکارچیان هالووین", username=None
    )
    msg.message_id = message_id
    msg.content_type = "photo"
    cb.message = msg
    cb.answer = AsyncMock()

    bot = _mock_bot(chat_id)
    bot.edit_message_media = AsyncMock(return_value=msg)
    bot.edit_message_caption = AsyncMock(return_value=msg)
    bot.edit_message_text = AsyncMock(return_value=msg)
    return cb, bot


async def _settle(db_session) -> None:
    """Release the fixture session's SQLite locks before a handler opens its own.

    Handlers run in a second connection from ``get_session()``; a lingering
    transaction in the test session would deadlock the file-backed test DB.
    """
    await db_session.commit()


async def _claim_menu_panel(db_session, user_id: int, *, chat_id: int = GROUP_ID) -> None:
    """Register ``MENU_MESSAGE_ID`` as ``user_id``'s personal Boss Menu panel."""
    async with get_session() as session:
        await PanelOwnershipService.claim(
            session,
            chat_id=chat_id,
            message_id=MENU_MESSAGE_ID,
            user_id=user_id,
            panel_type="halloween",
        )
    await _settle(db_session)


async def _open_menu(db_session, uid: int, *, text: str = "هالووین", chat_id: int = GROUP_ID):
    """Send the Boss Menu through the real handler (also claims the panel)."""
    await _settle(db_session)
    msg = _mock_group_message(uid, text, chat_id=chat_id)
    bot = _mock_bot(chat_id)
    await handle_halloween_command(msg, bot)
    return msg, bot


def _main_keyboard_buttons():
    return [button for row in get_halloween_main_keyboard().inline_keyboard for button in row]


def _track_rows(caption: str):
    """Only the milestone rows of the Reward Track caption (legend excluded)."""
    return [line for line in caption.split("\n") if "\u066a** \u2014" in line]


# ---------------------------------------------------------------------------
# 1. TASK 1 + TASK 4 — Boss Menu: no attack button, reply instruction,
#    exact loot timer, seed range, damage hint, Top-3 board
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halloween_menu_requires_flag(db_session):
    user = await create_test_user(db_session, "بازیکن اول")
    uid = user.id
    await ItemService.grant(db_session, uid, "pumpkin_seed", 5)
    await db_session.commit()

    # Flag defaults to False: typing «هالووین» must stay completely silent.
    msg_off = _mock_group_message(uid)
    await handle_halloween_command(msg_off)
    assert msg_off.answer_photo.await_count == 0
    assert msg_off.answer.await_count == 0

    # Enable flag: typing «هالووین» sends the phase-1 boss portrait with the
    # exact status lines and the two remaining styled buttons.
    await ConfigService.set(db_session, "halloween_enabled", True)
    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state.started_at = now
    state.ends_at = now + timedelta(days=6, hours=4, minutes=30)
    await db_session.commit()

    msg_on = _mock_group_message(uid)
    await handle_halloween_command(msg_on)
    assert msg_on.answer_photo.await_count == 1

    kwargs = msg_on.answer_photo.await_args.kwargs
    assert isinstance(kwargs["photo"], FSInputFile)
    assert kwargs["photo"].path.endswith("sibomortis.jpg")
    caption = kwargs["caption"]
    # The Boss Menu is delivered as HTML, so the ** markers are <b> tags.
    assert kwargs["parse_mode"] == "HTML"
    assert caption.startswith(format_html(BOSS_MENU_TITLE))
    assert format_html("💀 جون: **۵۰۰۰۰۰ از ۵۰۰۰۰۰** (**۱۰۰٪**)") in caption
    assert format_html("🎃 دونه کدو: **۵**") in caption
    assert f"⌛ پایان: {fa_digits(6)} روز و {fa_digits(4)} ساعت" in caption
    # TASK 4: damage basis hint (Main Card power + 10% crit).
    assert format_html("💥 آسیب بر پایهٔ **قدرت کارت اصلی**") in caption
    assert f"{fa_digits(10)}٪ شانس {format_html('**ضربهٔ حیاتی**')}" in caption
    # TASK 4: Top-3 damage dealers section (nobody has attacked yet).
    assert format_html("🏆 **برترین آسیب‌زنندگان**") in caption
    assert "هنوز کسی ضربه نزده" in caption
    # TASK 1: the reply instruction replaces the button.
    assert ATTACK_INSTRUCTION in caption
    assert "برای حمله، روی همین پیام ریپلای کنید و بنویسید" in caption
    assert ATTACK_TRIGGER_WORD in caption

    buttons = _main_keyboard_buttons()
    assert [(b.text, b.callback_data, b.style) for b in buttons] == [
        ("🎁 مسیر جوایز", "hw:rewards", "primary"),
        ("🎃 دریافت غنیمت (دونه کدو)", "hw:loot", "primary"),
    ]
    # TASK 1: the ⚔️ attack button is gone from the keyboard entirely.
    assert not any("hw:attack" in (b.callback_data or "") for b in buttons)
    assert "⚔️ حمله به سیبومورتیس" not in caption


@pytest.mark.asyncio
async def test_menu_shows_exact_loot_timer_and_top3_board(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    veteran = await create_test_user(db_session, "شکارچی کهنه")
    rookie = await create_test_user(db_session, "نوکار")
    uid, uid2 = veteran.id, rookie.id

    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state_id = state.id

    # Top-3 damage dealers of the whole event, ordered by total damage.
    for attacker, damage in ((uid, 900), (uid2, 400), (uid, 300)):
        db_session.add(
            HalloweenAttack(
                event_id=state_id,
                user_id=attacker,
                damage=damage,
                is_crit=False,
                boss_hp_after=500_000 - damage,
            )
        )
    # Loot claimed 1h ago -> exactly 3h left of the 4h cooldown.
    await HalloweenService.claim_seed_loot(db_session, uid, now=now - timedelta(hours=1))
    await db_session.commit()

    status = await HalloweenService.get_seed_loot_status(db_session, uid, now=now)
    assert status.ready is False
    assert status.remaining_seconds == 3 * 3600
    top3 = await HalloweenService.get_leaderboard(db_session, state_id, limit=3)
    caption = format_boss_menu_caption(state, 7, now=now, loot=status, top3=top3)

    # TASK 3: exact remaining time, e.g. «۳ ساعت و ۱۵ دقیقه».
    assert f"⏳ زمان تا غنیمت بعدی: {format_duration_fa(status.remaining_seconds)}" in caption
    assert "۳ ساعت" in caption
    # TASK 3: the loot range the player can actually expect.
    assert f"🎁 هر غنیمت: **{seed_loot_range_fa()} دونه کدو**" in caption
    # TASK 4: medals + damage per player, Persian digits only.
    assert f"🥇 \u200eشکارچی کهنه\u200e: {fa_digits(1200)} آسیب" in caption
    assert f"🥈 \u200eنوکار\u200e: {fa_digits(400)} آسیب" in caption
    assert "🥉" not in caption  # only two attackers exist
    assert not any(digit in caption for digit in "0123456789")

    # A player who never claimed sees "ready" instead of a countdown.
    other = await HalloweenService.get_seed_loot_status(db_session, uid2, now=now)
    assert other.ready is True
    other_caption = format_boss_menu_caption(state, 0, now=now, loot=other, top3=top3)
    assert "زمان تا غنیمت بعدی" not in other_caption
    assert "✅ غنیمت آماده است" in other_caption


@pytest.mark.asyncio
async def test_format_duration_fa_reads_like_persian():
    assert format_duration_fa(3 * 3600 + 15 * 60) == "۳ ساعت و ۱۵ دقیقه"
    assert format_duration_fa(4 * 3600) == "۴ ساعت"
    assert format_duration_fa(9 * 60) == "۹ دقیقه"
    # A countdown always rounds UP, so nobody is told "1 minute" and
    # then has to wait a second longer.
    assert format_duration_fa(60) == "۱ دقیقه"
    assert format_duration_fa(61) == "۲ دقیقه"
    assert format_duration_fa(30) == "کمتر از یک دقیقه"
    assert format_duration_fa(-5) == "کمتر از یک دقیقه"
    assert seed_loot_range_fa() == f"{fa_digits(SEED_LOOT_MIN)} \u062a\u0627 {fa_digits(SEED_LOOT_MAX)}"


# ---------------------------------------------------------------------------
# 2. TASK 1 + TASK 2 — reply «حمله»: seed spent, damage dealt, public hype
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attack_by_replying_to_menu_consumes_seed_and_hits(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    await seed_characters(db_session)
    user = await create_test_user(db_session, "مهاجم پیامی")
    uid = user.id
    igris = await db_session.scalar(select(Character).where(Character.code == "igris"))
    card = await PlayerCardService.acquire(db_session, uid, igris.id)
    await PlayerCardService.set_favorite(db_session, uid, card.id, expected_main_card_id=0)
    await ItemService.grant(db_session, uid, "pumpkin_seed", 3)

    # Open the menu for real so the panel registry knows message 501 IS the menu.
    menu_msg, _ = await _open_menu(db_session, uid)
    assert menu_msg.answer_photo.await_count == 1
    async with get_session() as session:
        owner_id, panel_type = await PanelOwnershipService.get_panel_context(
            session, GROUP_ID, MENU_MESSAGE_ID, 0
        )
    assert panel_type == "halloween"
    assert owner_id == uid

    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    state_id, hp_before = state.id, int(state.boss_hp)
    await _settle(db_session)

    bot = _mock_bot()
    await handle_halloween_attack_trigger(_mock_attack_reply(uid), bot)

    async with get_session() as verify:
        after = await verify.get(HalloweenEventState, state_id)
        attacks = list(
            (
                await verify.scalars(
                    select(HalloweenAttack).where(HalloweenAttack.event_id == state_id)
                )
            ).all()
        )
        assert len(attacks) == 1
        damage = int(attacks[0].damage)
        assert damage > 0
        assert int(after.boss_hp) == hp_before - damage
        # exactly one seed spent per «حمله»
        assert await ItemService.get_total(verify, uid, "pumpkin_seed") == 2

    # TASK 2: the hit is announced PUBLICLY to the group with the exact copy.
    sent = [call.args[1] for call in bot.send_message.await_args_list]
    hits = [line for line in sent if "به سیبومورتیس" in line]
    assert len(hits) == 1
    assert hits[0].startswith(
        format_html(
            f"⚔️ **\u200eشکارچی\u200e** به سیبومورتیس "
            f"**{fa_digits(damage)} آسیب** زد!"
        )
    )
    # The public line is HTML: bold is a <b> tag, never a raw "**".
    assert "**" not in hits[0]
    assert "<b>" in hits[0]
    assert not any(digit in hits[0] for digit in "0123456789")
    # The attacker's own menu is refreshed in place (no extra spam message).
    assert bot.edit_message_media.await_count == 1
    edited = bot.edit_message_media.await_args
    assert edited.kwargs["chat_id"] == GROUP_ID
    assert edited.kwargs["message_id"] == MENU_MESSAGE_ID
    # The refreshed menu is HTML too: bold arrives as a <b> tag.
    assert (
        format_html(f"🎃 دونه کدو: **{fa_digits(2)}**")
        in edited.kwargs["media"].caption
    )
    assert edited.kwargs["media"].parse_mode == "HTML"


@pytest.mark.asyncio
async def test_attack_trigger_is_silent_outside_the_menu(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    user = await create_test_user(db_session, "مهاجم ساکت")
    uid = user.id
    await ItemService.grant(db_session, uid, "pumpkin_seed", 4)
    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state_id = state.id
    await _settle(db_session)

    # (a) «حمله» with no reply at all -> ignored, seeds intact.
    no_reply = _mock_attack_reply(uid)
    no_reply.reply_to_message = None
    bot = _mock_bot()
    await handle_halloween_attack_trigger(no_reply, bot)
    assert bot.send_message.await_count == 0
    assert no_reply.answer.await_count == 0

    # (b) «حمله» as a reply to an unrelated message -> ignored.
    unrelated = _mock_attack_reply(
        uid, menu_message_id=4242, menu_caption="🍎 سیب روزانه‌ات را گرفتی!"
    )
    await handle_halloween_attack_trigger(unrelated, bot)
    assert bot.send_message.await_count == 0

    # (c) a longer sentence containing the word -> NOT the exact trigger word.
    chatty = _mock_attack_reply(uid, text="حمله به سیبومورتیس!")
    await handle_halloween_attack_trigger(chatty, bot)
    assert bot.send_message.await_count == 0

    # (d) «هالووین» opens the menu; it is not an attack.
    wrong_word = _mock_attack_reply(uid, text="هالووین")
    await handle_halloween_attack_trigger(wrong_word, bot)
    assert bot.send_message.await_count == 0

    async with get_session() as verify:
        assert await ItemService.get_total(verify, uid, "pumpkin_seed") == 4
        assert (
            await verify.scalar(
                select(HalloweenAttack).where(HalloweenAttack.event_id == state_id)
            )
            is None
        )


@pytest.mark.asyncio
async def test_attack_trigger_respects_flag_and_reports_no_seeds(db_session):
    user = await create_test_user(db_session, "بی‌دونه")
    uid = user.id
    await _settle(db_session)

    # Event OFF: completely silent, even on a valid menu reply.
    bot_off = _mock_bot()
    await handle_halloween_attack_trigger(_mock_attack_reply(uid), bot_off)
    assert bot_off.send_message.await_count == 0
    assert bot_off.edit_message_media.await_count == 0

    await ConfigService.set(db_session, "halloween_enabled", True)
    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    hp_before = int(state.boss_hp)
    await _settle(db_session)

    # Menu panel is registered, but the player holds 0 seeds.
    await _claim_menu_panel(db_session, uid)
    bot = _mock_bot()
    empty = _mock_attack_reply(uid)
    await handle_halloween_attack_trigger(empty, bot)
    assert empty.answer.await_args.args[0] == NO_SEEDS_ALERT
    assert bot.send_message.await_count == 0  # nothing to announce
    assert bot.edit_message_media.await_count == 0

    state_after = await db_session.get(HalloweenEventState, state.id)
    await db_session.refresh(state_after)
    assert int(state_after.boss_hp) == hp_before


@pytest.mark.asyncio
async def test_reply_to_another_players_menu_never_spends_their_seeds(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    owner = await create_test_user(db_session, "صاحب پنل")
    intruder = await create_test_user(db_session, "مزاحم")
    await ItemService.grant(db_session, owner.id, "pumpkin_seed", 5)
    await ItemService.grant(db_session, intruder.id, "pumpkin_seed", 1)
    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state_id, hp_before = state.id, int(state.boss_hp)
    await _settle(db_session)

    # The menu belongs to `owner`; `intruder` replies «حمله» to it.
    await _claim_menu_panel(db_session, owner.id)

    bot = _mock_bot()
    await handle_halloween_attack_trigger(_mock_attack_reply(intruder.id), bot)

    async with get_session() as verify:
        # The intruder attacks for THEMSELVES only: their own seed is gone.
        assert await ItemService.get_total(verify, intruder.id, "pumpkin_seed") == 0
        assert await ItemService.get_total(verify, owner.id, "pumpkin_seed") == 5
        after = await verify.get(HalloweenEventState, state_id)
        assert int(after.boss_hp) < hp_before
    # Someone else's personal menu is never edited…
    assert bot.edit_message_media.await_count == 0
    # …but the hit is still announced to the group.
    assert "به سیبومورتیس" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_attack_is_silent_once_the_event_is_off(db_session):
    """No attack without the flag, even on a perfectly formed menu reply."""
    assert CONFIG_DEFAULTS["halloween_enabled"] is False
    await ConfigService.set(db_session, "halloween_enabled", False)
    user = await create_test_user(db_session, "کنجکاو")
    uid = user.id
    await ItemService.grant(db_session, uid, "pumpkin_seed", 3)
    await _settle(db_session)
    await _claim_menu_panel(db_session, uid)

    bot = _mock_bot()
    await handle_halloween_attack_trigger(_mock_attack_reply(uid), bot)
    assert bot.send_message.await_count == 0
    assert await ConfigService.get(db_session, "halloween_enabled") is False
    async with get_session() as verify:
        assert await ItemService.get_total(verify, uid, "pumpkin_seed") == 3


# ---------------------------------------------------------------------------
# 3. TASK 3 — seed loot 3–6 per claim + enforced 4h cooldown + menu timer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loot_button_grants_3_to_6_seeds_and_enforces_4h_cooldown(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    user = await create_test_user(db_session, "جمع‌کننده دونه")
    uid = user.id
    await db_session.commit()
    assert (SEED_LOOT_MIN, SEED_LOOT_MAX) == (3, 6)

    t0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    first = await HalloweenService.claim_seed_loot(
        db_session, uid, rng=random.Random(7), now=t0
    )
    await db_session.commit()
    # HW2: 3..6 seeds instead of the old 1..3.
    assert SEED_LOOT_MIN <= first.seeds_granted <= SEED_LOOT_MAX
    assert first.cooldown_seconds == 14_400
    assert await ItemService.get_total(db_session, uid, "pumpkin_seed") == first.seeds_granted

    # Second claim inside 4 hours raises the exact Persian cooldown error.
    with pytest.raises(HalloweenCooldownError) as exc_info:
        await HalloweenService.claim_seed_loot(
            db_session, uid, now=t0 + timedelta(hours=3, minutes=59)
        )
    assert str(exc_info.value) == LOOT_COOLDOWN_ALERT
    assert exc_info.value.remaining_seconds == 60
    await db_session.rollback()

    # The handler surfaces the alert and leaves the menu untouched.
    await _settle(db_session)
    cb, bot = _mock_callback(uid, "hw:loot")
    await handle_halloween_loot(cb, bot)
    cb.answer.assert_awaited_once_with(LOOT_COOLDOWN_ALERT, show_alert=True)
    assert bot.edit_message_media.await_count == 0

    # Right after a successful claim the menu advertises the FULL fresh cooldown
    # instead of a stale timer (TASK 3).
    await db_session.commit()
    fresh = await HalloweenService.claim_seed_loot(
        db_session, uid, rng=random.Random(5), now=t0 + timedelta(hours=4, seconds=1)
    )
    state = await HalloweenService.ensure_active_event(db_session, now=t0)
    caption = format_boss_menu_caption(
        state,
        fresh.total_seeds,
        now=t0 + timedelta(hours=4, seconds=1),
        loot=SeedLootStatus(ready=False, remaining_seconds=fresh.cooldown_seconds),
    )
    assert f"⏳ زمان تا غنیمت بعدی: {format_duration_fa(fresh.cooldown_seconds)}" in caption
    assert "۴ ساعت" in caption
    await db_session.commit()


@pytest.mark.asyncio
async def test_every_seed_roll_lands_in_the_advertised_range(db_session):
    """The advertised range is the real range for every possible RNG draw."""
    await ConfigService.set(db_session, "halloween_enabled", True)
    players = [
        await create_test_user(db_session, f"جمع‌کننده {index}")
        for index in range(12)
    ]
    await db_session.commit()
    t0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    rolls = set()
    for offset, player in enumerate(players):
        outcome = await HalloweenService.claim_seed_loot(
            db_session, player.id, rng=random.Random(offset), now=t0
        )
        rolls.add(outcome.seeds_granted)
    await db_session.commit()

    assert rolls and rolls <= {3, 4, 5, 6}
    assert all(SEED_LOOT_MIN <= roll <= SEED_LOOT_MAX for roll in rolls)


# ---------------------------------------------------------------------------
# 4. TASK 5 — boss evolution artwork driven by global HP
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boss_artwork_evolves_with_hp_thresholds(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    await db_session.commit()

    max_hp = 1000
    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state.max_hp = max_hp
    state.boss_hp = max_hp
    await db_session.commit()

    async def photo_for(hp: int) -> str:
        state.boss_hp = hp
        await db_session.commit()
        async with get_session() as session:
            photo = await _resolve_boss_photo(session, state)
        assert isinstance(photo, FSInputFile)
        return photo.path

    # Thresholds use the same «<= milestone» convention as the reward track.
    assert (await photo_for(1000)).endswith("assets/halloween/sibomortis.jpg")
    assert phase_for_hp(1000, max_hp) == 1
    assert (await photo_for(751)).endswith("sibomortis.jpg")
    assert (await photo_for(750)).endswith("sibomortis_75.jpg")  # crossed 75%
    assert phase_for_hp(750, max_hp) == 2
    assert (await photo_for(501)).endswith("sibomortis_75.jpg")
    assert (await photo_for(500)).endswith("sibomortis_50.jpg")  # crossed 50%
    assert phase_for_hp(500, max_hp) == 3
    assert (await photo_for(251)).endswith("sibomortis_50.jpg")
    assert (await photo_for(250)).endswith("sibomortis_25.jpg")  # crossed 25%
    assert phase_for_hp(250, max_hp) == 4
    assert (await photo_for(0)).endswith("sibomortis_25.jpg")
    assert phase_for_hp(0, max_hp) == 5

    # Each phase keeps a /setasset-overridable key and a bundled 1024px file.
    assert boss_art_for_phase(1)[0] == "halloween_boss"
    assert boss_art_for_phase(2)[0] == "halloween_boss_75"
    assert boss_art_for_phase(3)[0] == "halloween_boss_50"
    assert boss_art_for_phase(4)[0] == "halloween_boss_25"
    for phase in (1, 2, 3, 4, 5):
        assert boss_art_for_phase(phase)[1].is_file()

    # The boss talks back: the menu taunt changes with the phase.
    state.boss_hp = max_hp
    await db_session.commit()
    caption_full = format_boss_menu_caption(state, 0, now=now)
    state.boss_hp = 400
    await db_session.commit()
    caption_mid = format_boss_menu_caption(state, 0, now=now)
    assert "استوار ایستاده" in caption_full
    assert "خشمگین" in caption_mid
    assert caption_full != caption_mid


@pytest.mark.asyncio
async def test_attack_reports_phase_evolution_and_announces_it(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    await seed_characters(db_session)
    user = await create_test_user(db_session, "تحول‌ساز")
    uid = user.id
    igris = await db_session.scalar(select(Character).where(Character.code == "igris"))
    card = await PlayerCardService.acquire(db_session, uid, igris.id)
    await PlayerCardService.set_favorite(db_session, uid, card.id, expected_main_card_id=0)
    await ItemService.grant(db_session, uid, "pumpkin_seed", 4)

    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state.max_hp = 100
    state.boss_hp = 100  # one hit crosses the 75% threshold immediately
    await db_session.commit()

    outcome = await HalloweenService.attack_boss(db_session, uid, now=now)
    await db_session.commit()
    assert outcome.phase_before == 1
    assert outcome.boss_hp_after <= 75
    assert outcome.phase_after == phase_for_hp(outcome.boss_hp_after, 100)
    assert outcome.evolved is True
    assert outcome.hp_percent_after <= 75

    # The killing blow is announced with the defeat line on top of the hit.
    await ItemService.grant(db_session, uid, "pumpkin_seed", 1)
    state.boss_hp = 1
    await db_session.commit()
    await _claim_menu_panel(db_session, uid)

    bot = _mock_bot()
    await handle_halloween_attack_trigger(_mock_attack_reply(uid), bot)
    public = bot.send_message.await_args.args[1]
    assert public.startswith(format_html("⚔️ **\u200eشکارچی\u200e** به سیبومورتیس **"))
    assert format_html(ATTACK_DEFEAT_LINE) in public
    assert format_html("🏆 **سیبومورتیس سقوط کرد!**") in public
    assert "**" not in public
    # …and the refreshed menu shows the collapsed-boss artwork.
    assert bot.edit_message_media.await_args.kwargs["media"].media.path.endswith(
        "sibomortis_25.jpg"
    )
    # Copy sanity for the other two hype lines.
    assert ATTACK_CRIT_LINE.startswith("💥")
    assert "دگرگون" in ATTACK_EVOLUTION_LINE


# ---------------------------------------------------------------------------
# 5. Attack math: seed consumption, HP floor, attack log (legacy button path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_attack_button_still_hits_and_posts_publicly(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    await seed_characters(db_session)
    user = await create_test_user(db_session, "مهاجم اصلی")
    uid = user.id
    igris = await db_session.scalar(select(Character).where(Character.code == "igris"))
    card = await PlayerCardService.acquire(db_session, uid, igris.id)
    await PlayerCardService.set_favorite(db_session, uid, card.id, expected_main_card_id=0)
    state = await HalloweenService.ensure_active_event(db_session)
    state_id = state.id
    await db_session.commit()

    # 0 seeds: rejected with the exact Persian alert, boss HP untouched.
    await _settle(db_session)
    cb_empty, bot_empty = _mock_callback(uid, "hw:attack")
    await handle_halloween_attack(cb_empty, bot_empty)
    cb_empty.answer.assert_awaited_once_with(NO_SEEDS_ALERT, show_alert=True)

    # Grant 2 seeds and attack via the stale button: one seed consumed, damage
    # logged, public hype message AND the menu refreshed in the same click.
    await ItemService.grant(db_session, uid, "pumpkin_seed", 2)
    await db_session.commit()

    cb_hit, bot_hit = _mock_callback(uid, "hw:attack")
    await handle_halloween_attack(cb_hit, bot_hit)
    cb_hit.answer.assert_awaited_once_with(None, show_alert=False)
    assert bot_hit.send_message.await_count == 1
    assert "به سیبومورتیس" in bot_hit.send_message.await_args.args[1]
    assert bot_hit.edit_message_media.await_count == 1

    async with get_session() as verify:
        updated_state = await verify.get(HalloweenEventState, state_id)
        attacks = list(
            (
                await verify.scalars(
                    select(HalloweenAttack).where(HalloweenAttack.event_id == state_id)
                )
            ).all()
        )
        assert len(attacks) == 1
        assert attacks[0].damage > 0
        assert updated_state.boss_hp == 500_000 - attacks[0].damage
        assert await ItemService.get_total(verify, uid, "pumpkin_seed") == 1

        # Overkill clamp: boss_hp never drops below 0.
        updated_state.boss_hp = 5
        await verify.flush()
        finisher = await HalloweenService.attack_boss(verify, uid, rng=random.Random(3))
        assert finisher.boss_hp_after == 0
        assert updated_state.boss_hp == 0
        assert await ItemService.get_total(verify, uid, "pumpkin_seed") == 0
        assert finisher.phase_after == 5 and finisher.evolved is True


# ---------------------------------------------------------------------------
# 6. TASK 3 + TASK 4 — boosted reward track, readable UI, new amounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_milestone_reward_table_matches_the_spec():
    assert MILESTONE_REWARDS == {
        75: {"apples": 500, "pumpkin_seed": 3},
        50: {"magic_powder": 5, "apple_gold": 5},
        25: {"apple_diamond": 3},
        0: {"apples": 1500, "apple_obsidian": 1},
    }
    apple, apple_emoji = MILESTONE_REWARD_LABELS["apples"]
    seed, seed_emoji = MILESTONE_REWARD_LABELS["pumpkin_seed"]
    powder, powder_emoji = MILESTONE_REWARD_LABELS["magic_powder"]
    gold, gold_emoji = MILESTONE_REWARD_LABELS["apple_gold"]
    diamond, diamond_emoji = MILESTONE_REWARD_LABELS["apple_diamond"]
    obsidian, obsidian_emoji = MILESTONE_REWARD_LABELS["apple_obsidian"]

    assert "کارت امبر" not in milestone_reward_line(75)
    assert f"**{fa_digits(500)} {apple}** {apple_emoji}" in milestone_reward_line(75)
    assert f"**{fa_digits(3)} {seed}** {seed_emoji}" in milestone_reward_line(75)
    assert f"**{fa_digits(5)} {powder}** {powder_emoji}" in milestone_reward_line(50)
    assert f"**{fa_digits(5)} {gold}** {gold_emoji}" in milestone_reward_line(50)
    assert milestone_reward_line(25).startswith("**کارت امبر** 🎴")
    assert f"**{fa_digits(3)} {diamond}** {diamond_emoji}" in milestone_reward_line(25)
    line0 = milestone_reward_line(0)
    assert f"**{fa_digits(1500)} {apple}** {apple_emoji}" in line0
    assert f"**{fa_digits(1)} {obsidian}** {obsidian_emoji}" in line0
    # The Top-3 victory bonus is an advertised suffix, never a stray "+".
    assert f" (+**{fa_digits(1000)} {apple} " in line0
    assert line0.rstrip().endswith("🏆)")


@pytest.mark.asyncio
async def test_reward_track_claims_correct_milestones(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    await seed_characters(db_session)

    u1 = await create_test_user(db_session, "رتبه یک")
    u2 = await create_test_user(db_session, "رتبه دو")
    u3 = await create_test_user(db_session, "رتبه سه")
    u4 = await create_test_user(db_session, "رتبه چهار")
    ids = (u1.id, u2.id, u3.id, u4.id)

    state = await HalloweenService.ensure_active_event(db_session)
    state.max_hp = 1000
    state.boss_hp = 800  # 80%: all four milestones still locked
    for uid, dmg in zip(ids, (400, 300, 200, 100)):
        db_session.add(
            HalloweenAttack(
                event_id=state.id,
                user_id=uid,
                damage=dmg,
                is_crit=False,
                boss_hp_after=max(0, 1000 - dmg),
            )
        )
    await db_session.commit()
    state_id = state.id

    with pytest.raises(HalloweenMilestoneLockedError):
        await HalloweenService.claim_milestone(db_session, ids[0], 75)
    await db_session.rollback()

    # Boss at 250 / 1000 (25%): 75%, 50%, 25% unlocked; 0% still locked.
    st = await db_session.get(HalloweenEventState, state_id)
    st.boss_hp = 250
    await db_session.commit()

    await HalloweenService.claim_milestone(db_session, ids[0], 75)
    await HalloweenService.claim_milestone(db_session, ids[0], 50)
    m25 = await HalloweenService.claim_milestone(db_session, ids[0], 25)
    await db_session.commit()

    # HW2 amounts: 500 apples + 3 seeds / 5 powder + 5 gold / Amber + 3 diamond.
    assert m25.unlocked_amber is True
    assert await AppleWalletService.get_balance(db_session, ids[0]) == 500
    assert await ItemService.get_total(db_session, ids[0], "pumpkin_seed") == 3
    assert await ItemService.get_total(db_session, ids[0], "magic_powder") == 5
    assert await ItemService.get_total(db_session, ids[0], "apple_gold") == 5
    assert await ItemService.get_total(db_session, ids[0], "apple_diamond") == 3
    assert m25.summary["card"] == "amber"
    assert m25.summary["apple_diamond"] == 3

    amber = await db_session.scalar(select(Character).where(Character.code == "amber"))
    owned_amber = await db_session.scalar(
        select(PlayerCard).where(
            PlayerCard.user_id == ids[0], PlayerCard.character_id == amber.id
        )
    )
    assert owned_amber is not None

    # Idempotency: claiming 25% again for the same user is rejected cleanly.
    with pytest.raises(HalloweenAlreadyClaimedError):
        await HalloweenService.claim_milestone(db_session, ids[0], 25)
    await db_session.rollback()

    with pytest.raises(HalloweenMilestoneLockedError):
        await HalloweenService.claim_milestone(db_session, ids[0], 0)
    await db_session.rollback()

    # Boss dies: Top 3 get 1500 + 1000 bonus Apples + 1 Obsidian;
    # rank #4 gets 1500 + 1 Obsidian.
    st = await db_session.get(HalloweenEventState, state_id)
    st.boss_hp = 0
    await db_session.commit()

    await HalloweenService.claim_milestone(db_session, ids[0], 0)
    await HalloweenService.claim_milestone(db_session, ids[3], 0)
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, ids[0]) == 500 + 1500 + 1000
    assert await ItemService.get_total(db_session, ids[0], "apple_obsidian") == 1
    assert await AppleWalletService.get_balance(db_session, ids[3]) == 1500
    assert await ItemService.get_total(db_session, ids[3], "apple_obsidian") == 1

    # TASK 4: Reward Track screen copy + semantic button styles.
    _state, statuses = await HalloweenService.get_reward_track(db_session, ids[3])
    screen = format_reward_track_caption(_state, statuses)
    assert screen.startswith("🎁 **مسیر جوایز هالووین**")
    assert "\U0001f7e2 \u0622\u0645\u0627\u062f\u0647\u0654 \u062f\u0631\u06cc\u0627\u0641\u062a \u00b7 \U0001f512 \u0642\u0641\u0644 \u00b7 \u2705 \u062f\u0631\u06cc\u0627\u0641\u062a \u0634\u062f\u0647" in screen
    rows = _track_rows(screen)
    assert len(rows) == 4
    for pct in (75, 50, 25, 0):
        row = rows[[75, 50, 25, 0].index(pct)]
        assert row.startswith(f"**{fa_digits(pct)}٪**")
        assert milestone_reward_line(pct) in screen
    # ids[3] has claimed only the 0% reward so far; with the boss dead the
    # remaining three milestones are unlockable for them.
    assert sum("\u2705 \u062f\u0631\u06cc\u0627\u0641\u062a \u0634\u062f\u0647" in row for row in rows) == 1
    assert sum("\U0001f7e2 \u0622\u0645\u0627\u062f\u0647\u0654 \u062f\u0631\u06cc\u0627\u0641\u062a" in row for row in rows) == 3
    assert sum("\U0001f512 \u0642\u0641\u0644" in row for row in rows) == 0
    await _settle(db_session)
    cb_rw, bot_rw = _mock_callback(ids[3], "hw:rewards")
    await handle_halloween_rewards(cb_rw, bot_rw)
    assert bot_rw.edit_message_media.await_count == 1
    kb = get_halloween_reward_track_keyboard(statuses)
    for row in kb.inline_keyboard:
        for button in row:
            assert button.style in {"success", "primary", "danger"}

    # Claiming 50% via button callback works and updates the screen cleanly.
    cb_c50, bot_c50 = _mock_callback(ids[3], "hw:claim:50")
    await handle_halloween_claim_milestone(cb_c50, bot_c50)
    alert = cb_c50.answer.await_args.args[0]
    assert "**" not in alert
    assert f"جایزه {fa_digits(50)}٪" in alert
    assert "پودر جادویی" in alert and "سیب طلایی" in alert
    assert await AppleWalletService.get_balance(db_session, ids[3]) == 1500
    assert await ItemService.get_total(db_session, ids[3], "magic_powder") == 5


@pytest.mark.asyncio
async def test_reward_track_locked_badges_and_button_labels(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    user = await create_test_user(db_session, "تماشاگر")
    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state.max_hp = 1000
    state.boss_hp = 990  # nothing unlocked yet
    await db_session.commit()

    _state, statuses = await HalloweenService.get_reward_track(
        db_session, user.id, now=now
    )
    screen = format_reward_track_caption(_state, statuses)
    rows = _track_rows(screen)
    assert len(rows) == 4
    assert all("🔒 قفل" in row for row in rows)
    assert "🟢 آمادهٔ دریافت\n" not in "\n".join(rows)
    kb = get_halloween_reward_track_keyboard(statuses)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels[0].startswith(f"🔒 مرحله {fa_digits(75)}٪")
    assert labels[-1] == "🔙 بازگشت به منوی هالووین"

    # A dead boss never invites another attack in the menu.
    state.boss_hp = 0
    await db_session.commit()
    caption = format_boss_menu_caption(state, 3, now=now)
    assert "🏆 **سیبومورتیس شکست خورد!**" in caption
    assert ATTACK_INSTRUCTION not in caption


@pytest.mark.asyncio
async def test_seed_claim_outcome_and_status_roundtrip(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    uid = (await create_test_user(db_session, "زمان‌سنج")).id
    await db_session.commit()
    t0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    # No claim yet -> ready, no timer line.
    status = await HalloweenService.get_seed_loot_status(db_session, uid, now=t0)
    assert status == SeedLootStatus(
        ready=True, remaining_seconds=0, next_claim_at=None, total_claims=0
    )

    outcome = await HalloweenService.claim_seed_loot(
        db_session, uid, rng=random.Random(1), now=t0
    )
    await db_session.commit()
    moment = t0 + timedelta(minutes=1)
    status = await HalloweenService.get_seed_loot_status(db_session, uid, now=moment)
    assert status.ready is False
    assert status.remaining_seconds == 14_400 - 60
    assert status.total_claims == 1
    assert outcome.cooldown_seconds == 14_400

    # …and the menu prints exactly that countdown.
    state = await HalloweenService.ensure_active_event(db_session, now=t0)
    caption = format_boss_menu_caption(state, 0, now=moment, loot=status)
    assert f"⏳ زمان تا غنیمت بعدی: {format_duration_fa(status.remaining_seconds)}" in caption
    assert f"{fa_digits(3)} ساعت و {fa_digits(59)} دقیقه" in caption


# ---------------------------------------------------------------------------
# 7. TASK 6 — /hw_start hype broadcast, /hw_end, /hw_status
# ---------------------------------------------------------------------------


def _mock_private_command(user_id: int, text: str):
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = SimpleNamespace(
        id=user_id,
        first_name="مدیر",
        last_name=None,
        username="admin_user",
        language_code="fa",
        is_bot=False,
    )
    msg.chat = SimpleNamespace(id=user_id, type="private", title=None, username="admin_user")
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    msg.bot = bot
    return msg, bot


@pytest.mark.asyncio
async def test_hw_start_requires_admin_and_initializes_state(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_start, handle_hw_status

    admin_id = 990001
    non_admin_id = 990002
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    await _settle(db_session)
    # Non-admin invocation must NOT create event state or flip the flag.
    non_admin_msg, non_admin_bot = _mock_private_command(non_admin_id, "/hw_start 250000")
    await handle_hw_start(non_admin_msg, non_admin_bot)
    assert await HalloweenService.is_enabled(db_session) is False
    assert (await db_session.scalars(select(HalloweenEventState))).first() is None
    assert non_admin_bot.send_message.await_count == 0

    # Authorized admin initializes a 7-day state row with boss_hp == max_hp.
    await _settle(db_session)
    admin_msg, admin_bot = _mock_private_command(admin_id, "/hw_start 250000")
    before = datetime.now(timezone.utc) - timedelta(seconds=2)
    await handle_hw_start(admin_msg, admin_bot)
    after = datetime.now(timezone.utc) + timedelta(seconds=2)

    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    assert state is not None
    assert state.boss_hp == 250_000
    assert state.max_hp == 250_000
    assert state.is_active is True
    started = (
        state.started_at
        if state.started_at.tzinfo
        else state.started_at.replace(tzinfo=timezone.utc)
    )
    ends = state.ends_at if state.ends_at.tzinfo else state.ends_at.replace(tzinfo=timezone.utc)
    assert before <= started <= after
    assert abs((ends - started) - timedelta(days=7)) < timedelta(seconds=1)

    # /hw_status reports live boss HP, remaining time and the attacker count.
    u1 = await create_test_user(db_session, "مهاجم فعال")
    db_session.add(
        HalloweenAttack(
            event_id=state.id,
            user_id=u1.id,
            damage=1500,
            is_crit=False,
            boss_hp_after=248_500,
        )
    )
    await _settle(db_session)

    status_msg, _ = _mock_private_command(admin_id, "/hw_status")
    await handle_hw_status(status_msg)
    status_reply = status_msg.answer.await_args.args[0]
    assert "250,000" in status_reply
    assert "باقی‌مانده" in status_reply
    assert f"تعداد مهاجمان فعال: {1}" in status_reply


@pytest.mark.asyncio
async def test_hw_start_hype_broadcast_and_enables_flag(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_start

    admin_id = 990011
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    db_session.add_all(
        [
            Group(id=-1009101, title="گروه فعال اول", is_active=True),
            Group(id=-1009102, title="گروه فعال دوم", is_active=True),
            Group(id=-1009103, title="گروه غیرفعال", is_active=False),
        ]
    )
    await db_session.commit()

    # Cache a False first to prove cache busting works immediately on /hw_start.
    assert await HalloweenService.is_enabled() is False

    await _settle(db_session)
    admin_msg, admin_bot = _mock_private_command(admin_id, "/hw_start 500000")
    await handle_hw_start(admin_msg, admin_bot)

    # Both the cached zero-session read AND the DB read immediately see True.
    assert await HalloweenService.is_enabled() is True
    assert await HalloweenService.is_enabled(db_session) is True

    # TASK 6: the exact hype copy (three lines, no max_hp placeholder any more).
    assert START_ANNOUNCEMENT == (
        "🚨 **سیبومورتیس از راه رسید!** 🎃\n"
        "تاریکی اواریا را فرا گرفته. برای حمله به این هیولای افسانه‌ای، "
        "کلمه «هالووین» را در گروه بزنید و بجنگید!\n"
        "جایزه نهایی: کارت افسانه‌ای امبر! 🎴"
    )
    assert "{max_hp}" not in START_ANNOUNCEMENT
    sent_calls = [
        (call.args[0], call.args[1]) for call in admin_bot.send_message.await_args_list
    ]
    assert len(sent_calls) == 2
    assert set(sent_calls) == {
        (-1009101, format_html(START_ANNOUNCEMENT)),
        (-1009102, format_html(START_ANNOUNCEMENT)),
    }


@pytest.mark.asyncio
async def test_hw_end_disables_flag(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_end, handle_hw_start

    admin_id = 990021
    non_admin_id = 990022
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    db_session.add(Group(id=-1009201, title="گروه اعلان پایان", is_active=True))
    await db_session.commit()

    await _settle(db_session)
    start_msg, start_bot = _mock_private_command(admin_id, "/hw_start 300000")
    await handle_hw_start(start_msg, start_bot)
    assert await HalloweenService.is_enabled() is True

    # Non-admin /hw_end is ignored.
    bad_msg, bad_bot = _mock_private_command(non_admin_id, "/hw_end")
    await handle_hw_end(bad_msg, bad_bot)
    assert await HalloweenService.is_enabled() is True

    # Admin /hw_end flips is_active=False, disables flag & cache, broadcasts the end line.
    end_msg, end_bot = _mock_private_command(admin_id, "/hw_end")
    await handle_hw_end(end_msg, end_bot)

    assert await HalloweenService.is_enabled() is False
    assert await HalloweenService.is_enabled(db_session) is False

    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    await db_session.refresh(state)
    assert state.is_active is False

    sent_calls = [
        (call.args[0], call.args[1]) for call in end_bot.send_message.await_args_list
    ]
    assert sent_calls == [(-1009201, format_html(END_ANNOUNCEMENT))]


@pytest.mark.asyncio
async def test_top_3_damage_rewards_are_granted_once(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_end, handle_hw_start

    admin_id = 990031
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    players = [await create_test_user(db_session, f"قهرمان {idx}") for idx in range(1, 5)]
    uids = [p.id for p in players]
    await db_session.commit()

    # Scenario A: boss drops to 0 HP via a killing blow -> 1000/700/500/0.
    await _settle(db_session)
    start_msg, start_bot = _mock_private_command(admin_id, "/hw_start 1000")
    await handle_hw_start(start_msg, start_bot)

    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    event1_id = state.id

    for uid, dmg in zip(uids, (500, 300, 150, 45)):
        db_session.add(
            HalloweenAttack(
                event_id=event1_id,
                user_id=uid,
                damage=dmg,
                is_crit=False,
                boss_hp_after=max(1, 1000 - dmg),
            )
        )
    state.boss_hp = 5  # next attack is the killing blow
    await ItemService.grant(db_session, uids[3], "pumpkin_seed", 1)
    await db_session.commit()

    await HalloweenService.attack_boss(db_session, uids[3], rng=random.Random(1))
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, uids[0]) == 1000
    assert await AppleWalletService.get_balance(db_session, uids[1]) == 700
    assert await AppleWalletService.get_balance(db_session, uids[2]) == 500
    assert await AppleWalletService.get_balance(db_session, uids[3]) == 0

    await db_session.refresh(state)
    assert state.leaderboard_finalized is True
    assert state.leaderboard_finalized_at is not None

    # Later maintenance ticks AND /hw_end must not pay the Top-3 twice.
    await _settle(db_session)
    await MaintenanceService.run_once()
    end_msg, end_bot = _mock_private_command(admin_id, "/hw_end")
    await handle_hw_end(end_msg, end_bot)
    await handle_hw_end(end_msg, end_bot)

    assert await AppleWalletService.get_balance(db_session, uids[0]) == 1000
    assert await AppleWalletService.get_balance(db_session, uids[1]) == 700
    assert await AppleWalletService.get_balance(db_session, uids[2]) == 500
    assert await AppleWalletService.get_balance(db_session, uids[3]) == 0

    # Scenario B: event ended BEFORE the boss dies still pays the Top-3 once.
    await _settle(db_session)
    start2_msg, start2_bot = _mock_private_command(admin_id, "/hw_start 50000")
    await handle_hw_start(start2_msg, start2_bot)
    state2 = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    for uid, dmg in zip(uids, (100, 900, 400, 50)):  # u2 (#1), u3 (#2), u1 (#3)
        db_session.add(
            HalloweenAttack(
                event_id=state2.id,
                user_id=uid,
                damage=dmg,
                is_crit=False,
                boss_hp_after=48_000,
            )
        )
    await db_session.commit()

    await _settle(db_session)
    end2_msg, end2_bot = _mock_private_command(admin_id, "/hw_end")
    await handle_hw_end(end2_msg, end2_bot)
    await handle_hw_end(end2_msg, end2_bot)

    assert await AppleWalletService.get_balance(db_session, uids[1]) == 700 + 1000
    assert await AppleWalletService.get_balance(db_session, uids[2]) == 500 + 700
    assert await AppleWalletService.get_balance(db_session, uids[0]) == 1000 + 500
    assert await AppleWalletService.get_balance(db_session, uids[3]) == 0


# ---------------------------------------------------------------------------
# 8. Silent test mode (/hw_start <max_hp> --test & /hw_end --test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hw_start_test_mode_skips_broadcast(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_start

    admin_id = 990041
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    db_session.add_all(
        [
            Group(id=-1009401, title="گروه عمومی یک", is_active=True),
            Group(id=-1009402, title="گروه عمومی دو", is_active=True),
        ]
    )
    tester = await create_test_user(db_session, "ادمین تستر")
    tester_id = tester.id
    await db_session.commit()

    # Warm the flag cache with False before starting in silent test mode.
    assert await HalloweenService.is_enabled() is False

    await _settle(db_session)
    admin_msg, admin_bot = _mock_private_command(admin_id, "/hw_start 5000 --test")
    await handle_hw_start(admin_msg, admin_bot)

    assert await HalloweenService.is_enabled() is True
    assert await HalloweenService.is_enabled(db_session) is True
    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    assert state is not None
    assert state.boss_hp == 5000
    assert state.max_hp == 5000
    assert state.is_active is True

    # ZERO broadcast messages; private confirmation exact.
    assert admin_bot.send_message.await_count == 0
    admin_msg.answer.assert_awaited_once_with(START_TEST_CONFIRMATION)

    # Maintenance ticks stay silent in public groups while the interactive
    # attack still works inside the test group.
    prev_bot = MaintenanceService._bot
    MaintenanceService._bot = admin_bot
    try:
        admin_bot.send_photo = AsyncMock()
        await MaintenanceService.run_once()
        assert admin_bot.send_message.await_count == 0
        assert admin_bot.send_photo.await_count == 0
    finally:
        MaintenanceService._bot = prev_bot

    await _settle(db_session)
    test_group_msg, _ = await _open_menu(db_session, tester_id, chat_id=-1009499)
    assert test_group_msg.answer_photo.await_count == 1
    caption = test_group_msg.answer_photo.await_args.kwargs["caption"]
    assert f"{fa_digits(5000)} از {fa_digits(5000)}" in caption
    assert ATTACK_INSTRUCTION in caption

    # Attacking in a test group is still public inside THAT group only.
    await ItemService.grant(db_session, tester_id, "pumpkin_seed", 2)
    await db_session.commit()
    await _claim_menu_panel(db_session, tester_id, chat_id=-1009499)
    attack_bot = _mock_bot(-1009499)
    await handle_halloween_attack_trigger(
        _mock_attack_reply(tester_id, chat_id=-1009499), attack_bot
    )
    assert attack_bot.send_message.await_args.args[0] == -1009499
    assert "به سیبومورتیس" in attack_bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_hw_end_test_mode_skips_broadcast(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_end, handle_hw_start

    admin_id = 990051
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    db_session.add(Group(id=-1009501, title="گروه عمومی فعال", is_active=True))
    p1 = await create_test_user(db_session, "تستر اول")
    p2 = await create_test_user(db_session, "تستر دوم")
    uid1, uid2 = p1.id, p2.id
    await db_session.commit()

    await _settle(db_session)
    start_msg, start_bot = _mock_private_command(admin_id, "/hw_start 5000 --test")
    await handle_hw_start(start_msg, start_bot)
    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    db_session.add_all(
        [
            HalloweenAttack(
                event_id=state.id,
                user_id=uid1,
                damage=800,
                is_crit=False,
                boss_hp_after=4200,
            ),
            HalloweenAttack(
                event_id=state.id,
                user_id=uid2,
                damage=400,
                is_crit=False,
                boss_hp_after=3800,
            ),
        ]
    )
    await db_session.commit()

    await _settle(db_session)
    end_msg, end_bot = _mock_private_command(admin_id, "/hw_end --test")
    await handle_hw_end(end_msg, end_bot)

    # Rewards settled, state inactive, flag & cache disabled.
    assert await HalloweenService.is_enabled() is False
    assert await HalloweenService.is_enabled(db_session) is False
    await db_session.refresh(state)
    assert state.is_active is False
    assert state.leaderboard_finalized is True
    assert await AppleWalletService.get_balance(db_session, uid1) == 1000
    assert await AppleWalletService.get_balance(db_session, uid2) == 700

    # ZERO broadcast messages; private confirmation exact.
    assert end_bot.send_message.await_count == 0
    end_msg.answer.assert_awaited_once_with("✅ ایونت تستی به پایان رسید.")


# ---------------------------------------------------------------------------
# 9. Day-7 weakening, group loot chests (now 3–6 seeds), group broadcasts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_7_weakening_triggers_correctly(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    group = Group(id=-1009001, title="گروه آزمایشی یک", is_active=True)
    db_session.add(group)

    now = datetime.now(timezone.utc)
    state = await HalloweenService.ensure_active_event(db_session, now=now)
    state.max_hp = 500_000
    state.boss_hp = 400_000
    # Day 5 (less than 6 days elapsed): must NOT weaken yet.
    state.started_at = now - timedelta(days=5)
    state.ends_at = state.started_at + timedelta(days=7)
    state_id = state.id
    await db_session.commit()

    fake_bot = _mock_bot(-1009001)
    prev_bot = MaintenanceService._bot
    MaintenanceService._bot = fake_bot
    try:
        await _settle(db_session)
        await MaintenanceService.run_once()
        st = await db_session.get(HalloweenEventState, state_id)
        await db_session.refresh(st)
        assert st.last_day_weakened is False
        assert st.boss_hp == 400_000

        # Day 7 (6 days + 1 minute): weakens by 80% -> 80,000 HP exactly once.
        st.started_at = datetime.now(timezone.utc) - timedelta(days=6, minutes=1)
        await db_session.commit()

        await _settle(db_session)
        await MaintenanceService.run_once()
        await db_session.refresh(st)
        assert st.last_day_weakened is True
        assert st.hp_before_weaken == 400_000
        assert st.boss_hp == 80_000
        # Day-7 weakening collapses the boss into its final evolution phase.
        assert phase_for_hp(int(st.boss_hp), int(st.max_hp)) == 4

        sent_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
        assert sent_texts.count(DAY7_WEAKENING_ANNOUNCEMENT) == 1

        # Second run: no second weakening, no second announcement.
        await _settle(db_session)
        await MaintenanceService.run_once()
        await db_session.refresh(st)
        assert st.boss_hp == 80_000
        after_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
        assert after_texts.count(DAY7_WEAKENING_ANNOUNCEMENT) == 1

        # boss_hp == 0 with defeated_at NULL: stamp + announce exactly once.
        st.boss_hp = 0
        await db_session.commit()

        await _settle(db_session)
        await MaintenanceService.run_once()
        await db_session.refresh(st)
        assert st.defeated_at is not None
        await _settle(db_session)
        await MaintenanceService.run_once()

        all_texts = [call.args[1] for call in fake_bot.send_message.await_args_list]
        assert all_texts.count(DEFEAT_ANNOUNCEMENT) == 1
    finally:
        MaintenanceService._bot = prev_bot


@pytest.mark.asyncio
async def test_random_loot_drop_gives_3_to_6_seeds(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    group = Group(id=-1009002, title="گروه غنیمت شبانه", is_active=True)
    db_session.add(group)
    players = [await create_test_user(db_session, f"شکارچی {i}") for i in range(4)]
    uids = [p.id for p in players]
    await db_session.commit()

    fake_bot = _mock_bot(-1009002)
    prev_bot = MaintenanceService._bot
    MaintenanceService._bot = fake_bot
    try:
        await _settle(db_session)
        await MaintenanceService.run_once()
    finally:
        MaintenanceService._bot = prev_bot

    drop = await db_session.scalar(
        select(HalloweenLootDrop).where(HalloweenLootDrop.group_id == -1009002)
    )
    assert drop is not None
    assert drop.status == "ACTIVE"
    assert drop.max_claims == 3
    assert fake_bot.send_photo.await_count == 1
    # The chest advertises the SAME boosted range the claim pays out.
    assert "۳ تا ۶ دونه کدو" in fake_bot.send_photo.await_args.kwargs["caption"]
    drop_id = drop.id

    kb = get_loot_drop_keyboard(drop_id, 0, 3)
    assert kb.inline_keyboard[0][0].style == "success"

    # First 3 distinct players each receive 3..6 seeds; duplicate & 4th lose.
    await _settle(db_session)
    cb_dup, bot_dup = _mock_callback(
        uids[0], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777
    )
    await handle_halloween_group_drop_claim(cb_dup, bot_dup)
    seeds_u0 = await ItemService.get_total(db_session, uids[0], "pumpkin_seed")
    assert "**" not in cb_dup.answer.await_args.args[0]
    assert f"{fa_digits(3)} دونه کدو" in cb_dup.answer.await_args.args[0] or (
        f"{fa_digits(4)} دونه کدو" in cb_dup.answer.await_args.args[0]
        or f"{fa_digits(5)} دونه کدو" in cb_dup.answer.await_args.args[0]
        or f"{fa_digits(6)} دونه کدو" in cb_dup.answer.await_args.args[0]
    )
    assert SEED_LOOT_MIN <= seeds_u0 <= SEED_LOOT_MAX

    # Duplicate click by player 0 does not grant extra seeds.
    cb_dup2, bot_dup2 = _mock_callback(
        uids[0], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777
    )
    await handle_halloween_group_drop_claim(cb_dup2, bot_dup2)
    assert await ItemService.get_total(db_session, uids[0], "pumpkin_seed") == seeds_u0

    # Players 1 and 2 take slots 2/3 and 3/3 -> chest depletes.
    for idx in (1, 2):
        cb_i, bot_i = _mock_callback(
            uids[idx], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777
        )
        await handle_halloween_group_drop_claim(cb_i, bot_i)
        total = await ItemService.get_total(db_session, uids[idx], "pumpkin_seed")
        assert SEED_LOOT_MIN <= total <= SEED_LOOT_MAX

    # The 4th player clicks after depletion -> 0 seeds.
    cb_4, bot_4 = _mock_callback(
        uids[3], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777
    )
    await handle_halloween_group_drop_claim(cb_4, bot_4)
    assert await ItemService.get_total(db_session, uids[3], "pumpkin_seed") == 0
