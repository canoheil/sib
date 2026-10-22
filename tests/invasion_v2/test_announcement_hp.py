"""Phase 8D: live enemy HP on the announcement + explicit damage line on joins.

SQLite-only DB and mocked Telegram; no bot startup or live group.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, SendPhoto

from sib.database.models import Inv2Attack, Inv2Invasion, PlayerCard
from sib.services.invasion_v2 import presentation
from sib.services.sib_claim import fa_digits
from sib.tests.invasion_v2.test_presentation_phase5 import NOW, create_inv, db

HP = "⚔️ جان شیاسیب"
DAMAGE = "آسیب وارده به دشمن"


def _join_bot(**edit_kwargs):
    return SimpleNamespace(send_photo=AsyncMock(return_value=True),
                           edit_message_caption=AsyncMock(return_value=True, **edit_kwargs))


async def _attack(factory, inv_id, *, user_id=1, card_id=101, damage=41, crit=False,
                  narrative="ضربهٔ سنگین بر زره"):
    async with factory.begin() as s:
        s.add(PlayerCard(id=card_id, user_id=user_id, character_id=10, level=1, is_favorite=True))
        attack = Inv2Attack(invasion_id=inv_id, user_id=user_id, card_id=card_id,
                            card_power=20, damage=damage, is_crit=crit,
                            narrative=narrative, joined_at=NOW)
        s.add(attack)
        await s.flush()
        return attack.id


async def _set_hp(factory, inv_id, remaining, *, status="ACTIVE"):
    """Simulate the handler's committed CAS: the attack row and the HP change
    are durable before any presentation runs."""
    async with factory.begin() as s:
        inv = await s.get(Inv2Invasion, inv_id)
        inv.enemy_hp_remaining = remaining
        inv.status = status


# --- Announcement HP ---------------------------------------------------------


@pytest.mark.asyncio
async def test_announcement_caption_contains_hp(db):
    factory, _ = db
    inv_id = await create_inv(factory)  # PENDING, hp 200
    bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(
        chat=SimpleNamespace(id=-100), message_id=123)))
    await presentation.InvasionPresentation().announce(bot, inv_id)
    kwargs = bot.send_photo.await_args.kwargs
    caption = kwargs["caption"]
    assert HP in caption
    assert caption.endswith(f"\n\n{HP}: **{fa_digits(200)}** / **{fa_digits(200)}**")
    assert len(caption) <= 1024
    # The narrative is stored so later HP edits rebuild the same prose.
    async with factory() as s:
        inv = await s.get(Inv2Invasion, inv_id)
        assert inv.announcement_narrative is not None
        assert caption.startswith(inv.announcement_narrative)
        assert inv.message_id == 123 and inv.message_chat_id == -100
    # The normal callback remains backward compatible.
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "⚔️ پیوستن (عادی)"
    assert button.callback_data == f"inv2:join:{inv_id}"
    buff_button = kwargs["reply_markup"].inline_keyboard[1][0]
    assert buff_button.text == "💥 پیوستن با سیب باف"
    assert buff_button.callback_data == f"inv2:joinbuff:{inv_id}"


@pytest.mark.asyncio
async def test_join_edits_announcement_to_new_hp(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 0.01)
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41)
    await _set_hp(factory, inv_id, 159)
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    bot.send_photo.assert_awaited_once()
    await asyncio.sleep(0.2)
    assert bot.edit_message_caption.await_count == 1
    kwargs = bot.edit_message_caption.await_args.kwargs
    assert kwargs["chat_id"] == -100 and kwargs["message_id"] == 123
    assert kwargs["caption"].endswith(f"\n\n{HP}: **{fa_digits(159)}** / **{fa_digits(200)}**")
    # The edit is a metered send: photo + successful edit.
    assert presentation.record_send_success.await_count == 2


@pytest.mark.asyncio
async def test_hp_edit_keeps_the_join_button(db, monkeypatch):
    """Regression: Telegram clears an inline keyboard whenever an edit omits
    reply_markup, so the live HP edit must re-send the join button. Without
    this, the button vanished after the first join and nobody else could join.
    """
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 0.01)
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41)
    await _set_hp(factory, inv_id, 159)
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    await asyncio.sleep(0.2)
    assert bot.edit_message_caption.await_count == 1
    markup = bot.edit_message_caption.await_args.kwargs.get("reply_markup")
    assert markup is not None, "live HP edit dropped the join button"
    button = markup.inline_keyboard[0][0]
    assert button.text == "⚔️ پیوستن (عادی)"
    assert button.callback_data == f"inv2:join:{inv_id}"
    buff_button = markup.inline_keyboard[1][0]
    assert buff_button.text == "💥 پیوستن با سیب باف"
    assert buff_button.callback_data == f"inv2:joinbuff:{inv_id}"


@pytest.mark.asyncio
async def test_kill_edit_removes_the_join_button(db):
    """At 0 HP the battle is over: the synchronous kill edit clears the button
    (the request omits reply_markup, which is how Telegram removes it)."""
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", hp=1, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41, crit=True)
    await _set_hp(factory, inv_id, 0, status="VICTORY")
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    assert bot.edit_message_caption.await_count == 1
    assert bot.edit_message_caption.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_kill_edits_announcement_to_zero(db):
    factory, _ = db
    inv_id = await create_inv(factory, status="ACTIVE", hp=1, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41, crit=True)
    await _set_hp(factory, inv_id, 0, status="VICTORY")  # the killing blow is committed
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    # The kill edit is synchronous (not debounced) and must not be skipped.
    assert bot.edit_message_caption.await_count == 1
    assert inv_id not in presentation._pending_edits
    kwargs = bot.edit_message_caption.await_args.kwargs
    assert kwargs["caption"].endswith(f"\n\n{HP}: **{fa_digits(0)}** / **{fa_digits(1)}**")


@pytest.mark.asyncio
async def test_edit_failure_does_not_break_join(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 0.01)
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41)
    await _set_hp(factory, inv_id, 159)
    bot = _join_bot(side_effect=TelegramBadRequest(
        method=EditMessageCaption(chat_id=-100, message_id=123, caption=""),
        message="message to edit not found"))
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    bot.send_photo.assert_awaited_once()  # the join delivery survived
    await asyncio.sleep(0.2)
    presentation.record_send_failure.assert_awaited_once_with(-100)


@pytest.mark.asyncio
async def test_not_modified_is_swallowed(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 0.01)
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41)
    await _set_hp(factory, inv_id, 159)
    bot = _join_bot(side_effect=TelegramBadRequest(
        method=EditMessageCaption(chat_id=-100, message_id=123, caption=""),
        message="Message is not modified"))
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    await asyncio.sleep(0.2)
    assert bot.edit_message_caption.await_count == 1
    # Expected back-to-back join no-op: swallowed, not a breaker failure.
    presentation.record_send_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_throttle_coalesces_edits(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 0.2)
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    first_attack = await _attack(factory, inv_id, user_id=1, damage=10)
    second_attack = await _attack(factory, inv_id, user_id=2, card_id=102, damage=20)
    bot = _join_bot()
    await _set_hp(factory, inv_id, 190)  # join 1 committed
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, first_attack)
    first_task = presentation._pending_edits.get(inv_id)
    assert first_task is not None and not first_task.done()
    await _set_hp(factory, inv_id, 170)  # join 2 committed inside the same window
    await presentation.InvasionPresentation().on_join(bot, inv_id, 2, second_attack)
    assert presentation._pending_edits.get(inv_id) is first_task  # coalesced
    await asyncio.sleep(0.5)
    assert bot.send_photo.await_count == 2
    assert bot.edit_message_caption.await_count == 1  # one coalesced edit
    # The single edit reflects the latest committed HP, not the stale one.
    caption = bot.edit_message_caption.await_args.kwargs["caption"]
    assert caption.endswith(f"\n\n{HP}: **{fa_digits(170)}** / **{fa_digits(200)}**")
    assert inv_id not in presentation._pending_edits


@pytest.mark.asyncio
async def test_no_edit_after_finalize(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 0.01)
    inv_id = await create_inv(factory, status="VICTORY", hp=1, message_id=123)
    async with factory.begin() as s:
        inv = await s.get(Inv2Invasion, inv_id)
        inv.enemy_hp_remaining = 0
        inv.announcement_narrative = "سایهٔ شیاسیب از بارو کنار رفت و میدان آرام گرفت."
    bot = _join_bot()
    service = presentation.InvasionPresentation()
    assert await service.finalize(bot, inv_id) is True
    edits_before = bot.edit_message_caption.await_count  # the victory caption edit
    # A late join debounce must never touch the caption of a finished battle.
    presentation.schedule_hp_edit(bot, inv_id)
    await asyncio.sleep(0.2)
    assert bot.edit_message_caption.await_count == edits_before
    assert inv_id not in presentation._pending_edits


# --- Join damage line --------------------------------------------------------


@pytest.mark.asyncio
async def test_join_caption_contains_damage_line(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 30)  # keep the debounce out of the test
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=67)
    await _set_hp(factory, inv_id, 133)
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert caption == f"ضربهٔ سنگین بر زره\n\n🔥 {DAMAGE}: **{fa_digits(67)}**"
    assert len(caption) <= 1024


@pytest.mark.asyncio
async def test_crit_uses_bomb_emoji(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 30)
    inv_id = await create_inv(factory, status="ACTIVE", hp=200, message_id=123)
    attack_id = await _attack(factory, inv_id, damage=41, crit=True)
    await _set_hp(factory, inv_id, 159)
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert caption.endswith(f"\n\n💥 {DAMAGE}: **{fa_digits(41)}**")
    assert "🔥" not in caption


@pytest.mark.asyncio
async def test_kill_uses_win_emoji(db, monkeypatch):
    factory, _ = db
    monkeypatch.setattr(presentation, "HP_EDIT_DEBOUNCE_SECONDS", 30)
    inv_id = await create_inv(factory, status="ACTIVE", hp=1, message_id=123)
    # A critical killing blow still carries the win marker, not the crit one.
    attack_id = await _attack(factory, inv_id, damage=41, crit=True)
    await _set_hp(factory, inv_id, 0, status="VICTORY")
    bot = _join_bot()
    await presentation.InvasionPresentation().on_join(bot, inv_id, 1, attack_id)
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert caption.endswith(f"\n\n🏆 {DAMAGE}: **{fa_digits(41)}**")
    assert "💥" not in caption


@pytest.mark.parametrize("crit,kill", [(False, False), (True, False), (True, True), (False, True)])
def test_join_caption_stays_under_1024_chars(crit, kill):
    narrative = ("کلمه بلند " * 200).strip()  # far beyond the limit on its own
    for damage in (9, 67, 999999):
        caption = presentation.build_join_caption(narrative, damage, is_crit=crit, is_kill=kill)
        assert len(caption) <= 1024
        assert caption.endswith(f"{DAMAGE}: **{fa_digits(damage)}**")
        assert "\n\n" in caption


def test_join_caption_truncates_narrative_if_needed():
    narrative = " ".join(f"واژه{fa_digits(i)}" for i in range(250))
    caption = presentation.build_join_caption(narrative, 67, is_crit=False, is_kill=False)
    assert len(caption) <= 1024
    body, sep, damage_line = caption.rpartition("\n\n")
    assert sep and damage_line == f"🔥 {DAMAGE}: **{fa_digits(67)}**"
    assert body.endswith("…")
    truncated = body[:-1]
    # Truncation stops at a word boundary: the kept text is a whole-word prefix.
    assert narrative.startswith(truncated)
    assert narrative[len(truncated)] == " "
    # A short narrative is never touched.
    short = "پای پله‌های برج، داریوش با سیب سرخ راه بالا را گرفت."
    assert presentation.build_join_caption(short, 67, is_crit=False, is_kill=False) == \
        f"{short}\n\n🔥 {DAMAGE}: **{fa_digits(67)}**"
