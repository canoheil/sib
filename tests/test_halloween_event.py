"""HW1-IMPL-2 tests: Halloween Boss Menu, seed loot & cooldown, attacks, reward track, Day-7 weakening, and group drops."""

from datetime import datetime, timedelta, timezone
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, FSInputFile, Message
import pytest
from sqlalchemy import select

from sib.database.connection import seed_characters
from sib.database.models import (
    Character,
    Group,
    HalloweenAttack,
    HalloweenEventState,
    HalloweenLootDrop,
    HalloweenRewardClaim,
    PlayerCard,
)
from sib.handlers.halloween import (
    handle_halloween_attack,
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
from sib.services.config_store import ConfigService
from sib.services.halloween import (
    DAY7_WEAKENING_ANNOUNCEMENT,
    DEFEAT_ANNOUNCEMENT,
    LOOT_COOLDOWN_ALERT,
    NO_SEEDS_ALERT,
    HalloweenAlreadyClaimedError,
    HalloweenCooldownError,
    HalloweenMilestoneLockedError,
    HalloweenService,
)
from sib.services.items import ItemService
from sib.services.maintenance.service import MaintenanceService
from sib.services.player_cards import PlayerCardService
from sib.tests.test_parties import create_test_user


def _mock_group_message(user_id: int, text: str = "هالووین", chat_id: int = -100777):
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
    sent = SimpleNamespace(
        message_id=501,
        chat=msg.chat,
        content_type="photo",
    )
    msg.answer_photo = AsyncMock(return_value=sent)
    msg.answer = AsyncMock(return_value=sent)
    return msg


def _mock_callback(
    user_id: int,
    data: str,
    chat_id: int = -100777,
    message_id: int = 501,
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
        id=chat_id,
        type="supergroup",
        title="گروه شکارچیان هالووین",
        username=None,
    )
    msg.message_id = message_id
    msg.content_type = "photo"
    cb.message = msg
    cb.answer = AsyncMock()

    bot = MagicMock()
    bot.edit_message_media = AsyncMock(return_value=msg)
    bot.edit_message_caption = AsyncMock(return_value=msg)
    bot.edit_message_text = AsyncMock(return_value=msg)
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=999, chat=msg.chat))
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=998, chat=msg.chat))
    bot.delete_message = AsyncMock()
    return cb, bot


# ---------------------------------------------------------------------------
# 1. Menu gate, caption format, semantic button styles & loot cooldown
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

    # Enable flag: typing «هالووین» sends sibomortis.jpg + exact status lines + 3 styled buttons.
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
    assert "💀 جون سیبومورتیس: 500,000 / 500,000" in caption
    assert "🎃 دونه کدوی شما: 5" in caption
    assert "⏳ ۶ روز و ۴ ساعت باقی مانده" in caption

    kb = kwargs["reply_markup"]
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert [(b.text, b.callback_data, b.style) for b in buttons] == [
        ("⚔️ حمله به سیبومورتیس", "hw:attack", "success"),
        ("🎁 مسیر جوایز", "hw:rewards", "primary"),
        ("🎃 دریافت غنیمت (دونه کدو)", "hw:loot", "primary"),
    ]


@pytest.mark.asyncio
async def test_loot_button_grants_seeds_and_enforces_4h_cooldown(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    user = await create_test_user(db_session, "جمع‌کننده دونه")
    uid = user.id
    await db_session.commit()

    t0 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    first = await HalloweenService.claim_seed_loot(
        db_session, uid, rng=random.Random(7), now=t0
    )
    await db_session.commit()
    assert 1 <= first.seeds_granted <= 3
    assert await ItemService.get_total(db_session, uid, "pumpkin_seed") == first.seeds_granted

    # Second claim inside 4 hours raises exact Persian cooldown error.
    with pytest.raises(HalloweenCooldownError) as exc_info:
        await HalloweenService.claim_seed_loot(
            db_session, uid, now=t0 + timedelta(hours=3, minutes=59)
        )
    assert str(exc_info.value) == LOOT_COOLDOWN_ALERT
    await db_session.rollback()

    # Handler surfaces exact alert on cooldown click.
    cb, bot = _mock_callback(uid, "hw:loot")
    # Claim via handler once after advancing cooldown on DB:
    cd_row = (await db_session.scalars(select(HalloweenEventState))).first()
    assert cd_row is not None
    await handle_halloween_loot(cb, bot)
    cb.answer.assert_awaited_once_with(LOOT_COOLDOWN_ALERT, show_alert=True)

    # After 4 hours elapsed, next claim succeeds again.
    second = await HalloweenService.claim_seed_loot(
        db_session, uid, rng=random.Random(11), now=t0 + timedelta(hours=4, seconds=1)
    )
    await db_session.commit()
    assert second.total_seeds == first.seeds_granted + second.seeds_granted


# ---------------------------------------------------------------------------
# 2. Attack logic: seed consumption, Main Card power + crit, HP floor & log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attack_requires_seeds_and_deducts_hp(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    await seed_characters(db_session)
    user = await create_test_user(db_session, "مهاجم اصلی")
    uid = user.id
    igris = await db_session.scalar(
        select(Character).where(Character.code == "igris")
    )
    card = await PlayerCardService.acquire(db_session, uid, igris.id)
    await PlayerCardService.set_favorite(
        db_session, uid, card.id, expected_main_card_id=0
    )
    state = await HalloweenService.ensure_active_event(db_session)
    state_id = state.id
    await db_session.commit()

    # 0 seeds: rejected with exact Persian alert, boss HP untouched.
    cb_empty, bot_empty = _mock_callback(uid, "hw:attack")
    await handle_halloween_attack(cb_empty, bot_empty)
    cb_empty.answer.assert_awaited_once_with(NO_SEEDS_ALERT, show_alert=True)

    # Grant 2 seeds and attack via handler: consumes 1 seed, deducts Main Card damage, edits menu.
    await ItemService.grant(db_session, uid, "pumpkin_seed", 2)
    await db_session.commit()

    cb_hit, bot_hit = _mock_callback(uid, "hw:attack")
    await handle_halloween_attack(cb_hit, bot_hit)
    alert_sent = cb_hit.answer.await_args.args[0]
    assert alert_sent.startswith("⚔️ شما ")
    assert "آسیب وارد کردید! (کریتیکال:" in alert_sent
    assert bot_hit.edit_message_media.await_count == 1

    async with __import__(
        "sib.database.connection", fromlist=["get_session"]
    ).get_session() as verify:
        updated_state = await verify.get(HalloweenEventState, state_id)
        attacks = list(
            (
                await verify.scalars(
                    select(HalloweenAttack).where(
                        HalloweenAttack.event_id == state_id
                    )
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
        finisher = await HalloweenService.attack_boss(
            verify, uid, rng=random.Random(3)
        )
        assert finisher.boss_hp_after == 0
        assert updated_state.boss_hp == 0
        assert await ItemService.get_total(verify, uid, "pumpkin_seed") == 0


# ---------------------------------------------------------------------------
# 3. Milestone Reward Track (75%, 50%, 25% Amber, 0% + Top-3 1000 bonus)
# ---------------------------------------------------------------------------


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
    state.boss_hp = 800  # 80%: all 4 milestones still locked
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

    # At 80% HP, 75% is locked.
    with pytest.raises(HalloweenMilestoneLockedError):
        await HalloweenService.claim_milestone(db_session, ids[0], 75)
    await db_session.rollback()

    # Drop boss to 250 / 1000 (25%): 75%, 50%, 25% unlocked; 0% still locked.
    st = await db_session.get(HalloweenEventState, state_id)
    st.boss_hp = 250
    await db_session.commit()

    await HalloweenService.claim_milestone(db_session, ids[0], 75)
    await HalloweenService.claim_milestone(db_session, ids[0], 50)
    m25 = await HalloweenService.claim_milestone(db_session, ids[0], 25)
    await db_session.commit()

    assert m25.unlocked_amber is True
    assert await AppleWalletService.get_balance(db_session, ids[0]) == 150
    assert await ItemService.get_total(db_session, ids[0], "pumpkin_seed") == 2
    assert await ItemService.get_total(db_session, ids[0], "magic_powder") == 3
    assert await ItemService.get_total(db_session, ids[0], "apple_gold") == 2
    assert await ItemService.get_total(db_session, ids[0], "apple_diamond") == 1

    amber = await db_session.scalar(
        select(Character).where(Character.code == "amber")
    )
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

    # 0% still locked while boss_hp == 250.
    with pytest.raises(HalloweenMilestoneLockedError):
        await HalloweenService.claim_milestone(db_session, ids[0], 0)
    await db_session.rollback()

    # Boss dies (boss_hp = 0): Top 3 get 500 + 1000 = 1500 Apples + 1 Obsidian; Rank #4 gets 500 + 1 Obsidian.
    st = await db_session.get(HalloweenEventState, state_id)
    st.boss_hp = 0
    await db_session.commit()

    await HalloweenService.claim_milestone(db_session, ids[0], 0)
    await HalloweenService.claim_milestone(db_session, ids[3], 0)
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, ids[0]) == 150 + 1500
    assert await ItemService.get_total(db_session, ids[0], "apple_obsidian") == 1
    assert await AppleWalletService.get_balance(db_session, ids[3]) == 500
    assert await ItemService.get_total(db_session, ids[3], "apple_obsidian") == 1

    # UI check on Reward Track screen + semantic button styles.
    cb_rw, bot_rw = _mock_callback(ids[3], "hw:rewards")
    await handle_halloween_rewards(cb_rw, bot_rw)
    assert bot_rw.edit_message_media.await_count == 1
    _, statuses = await HalloweenService.get_reward_track(db_session, ids[3])
    kb = get_halloween_reward_track_keyboard(statuses)
    for row in kb.inline_keyboard:
        for button in row:
            assert button.style in {"success", "primary", "danger"}

    # Claiming 75% via button callback works and updates screen cleanly.
    cb_c75, bot_c75 = _mock_callback(ids[3], "hw:claim:75")
    await handle_halloween_claim_milestone(cb_c75, bot_c75)
    assert await AppleWalletService.get_balance(db_session, ids[3]) == 500 + 150


# ---------------------------------------------------------------------------
# 4. Day-7 weakening & boss defeat group announcements in MaintenanceService
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

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=10, chat=SimpleNamespace(id=-1009001))
    )
    fake_bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(message_id=11, chat=SimpleNamespace(id=-1009001))
    )
    prev_bot = MaintenanceService._bot
    MaintenanceService._bot = fake_bot
    try:
        await MaintenanceService.run_once()
        st = await db_session.get(HalloweenEventState, state_id)
        await db_session.refresh(st)
        assert st.last_day_weakened is False
        assert st.boss_hp == 400_000

        # Move started_at to 6 days + 1 minute ago (Day 7!): weakens by 80% -> 80,000 HP once.
        st.started_at = datetime.now(timezone.utc) - timedelta(days=6, minutes=1)
        await db_session.commit()

        await MaintenanceService.run_once()
        await db_session.refresh(st)
        assert st.last_day_weakened is True
        assert st.hp_before_weaken == 400_000
        assert st.boss_hp == 80_000

        sent_texts = [
            call.args[1] for call in fake_bot.send_message.await_args_list
        ]
        assert sent_texts.count(DAY7_WEAKENING_ANNOUNCEMENT) == 1

        # Second maintenance run does NOT weaken a second time or re-announce.
        await MaintenanceService.run_once()
        await db_session.refresh(st)
        assert st.boss_hp == 80_000
        sent_texts_after = [
            call.args[1] for call in fake_bot.send_message.await_args_list
        ]
        assert sent_texts_after.count(DAY7_WEAKENING_ANNOUNCEMENT) == 1

        # When boss_hp reaches 0 with defeated_at == NULL: marks defeated_at & announces once.
        st.boss_hp = 0
        await db_session.commit()

        await MaintenanceService.run_once()
        await db_session.refresh(st)
        assert st.defeated_at is not None
        await MaintenanceService.run_once()

        all_texts = [
            call.args[1] for call in fake_bot.send_message.await_args_list
        ]
        assert all_texts.count(DEFEAT_ANNOUNCEMENT) == 1
    finally:
        MaintenanceService._bot = prev_bot


# ---------------------------------------------------------------------------
# 5. Group Random Loot Drops: spawn via maintenance + first 3 claimers get seeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_random_loot_drop_gives_seeds(db_session):
    await ConfigService.set(db_session, "halloween_enabled", True)
    group = Group(id=-1009002, title="گروه غنیمت شبانه", is_active=True)
    db_session.add(group)
    players = [await create_test_user(db_session, f"شکارچی {i}") for i in range(4)]
    uids = [p.id for p in players]
    await db_session.commit()

    fake_bot = MagicMock()
    fake_bot.send_photo = AsyncMock(
        return_value=SimpleNamespace(message_id=777, chat=SimpleNamespace(id=-1009002))
    )
    fake_bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=778, chat=SimpleNamespace(id=-1009002))
    )
    prev_bot = MaintenanceService._bot
    MaintenanceService._bot = fake_bot
    try:
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
    drop_id = drop.id

    kb = get_loot_drop_keyboard(drop_id, 0, 3)
    assert kb.inline_keyboard[0][0].style == "success"

    # First 3 distinct players each receive 1..3 pumpkin_seed via button callback; duplicate & 4th rejected!
    cb_dup, bot_dup = _mock_callback(uids[0], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777)
    await handle_halloween_group_drop_claim(cb_dup, bot_dup)
    seeds_u0 = await ItemService.get_total(db_session, uids[0], "pumpkin_seed")
    assert 1 <= seeds_u0 <= 3

    # Duplicate click by player 0 does not grant extra seeds.
    cb_dup2, bot_dup2 = _mock_callback(uids[0], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777)
    await handle_halloween_group_drop_claim(cb_dup2, bot_dup2)
    assert await ItemService.get_total(db_session, uids[0], "pumpkin_seed") == seeds_u0

    # Players 1 and 2 claim slots 2/3 and 3/3 -> chest depletes.
    for idx in (1, 2):
        cb_i, bot_i = _mock_callback(uids[idx], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777)
        await handle_halloween_group_drop_claim(cb_i, bot_i)
        assert 1 <= await ItemService.get_total(db_session, uids[idx], "pumpkin_seed") <= 3

    # 4th player clicks after depletion -> receives 0 seeds.
    cb_4, bot_4 = _mock_callback(uids[3], f"hw_drop:{drop_id}", chat_id=-1009002, message_id=777)
    await handle_halloween_group_drop_claim(cb_4, bot_4)
    assert await ItemService.get_total(db_session, uids[3], "pumpkin_seed") == 0


# ---------------------------------------------------------------------------
# 6. Admin controls (/hw_start, /hw_end, /hw_status) & Top-3 finalization
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

    # Non-admin invocation must NOT create event state or flip flag.
    non_admin_msg, non_admin_bot = _mock_private_command(non_admin_id, "/hw_start 250000")
    await handle_hw_start(non_admin_msg, non_admin_bot)
    assert await HalloweenService.is_enabled(db_session) is False
    assert (await db_session.scalars(select(HalloweenEventState))).first() is None
    assert non_admin_bot.send_message.await_count == 0

    # Authorized admin initializes 7-day state row with exact boss_hp == max_hp.
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
    started = state.started_at.replace(tzinfo=timezone.utc) if state.started_at.tzinfo is None else state.started_at
    ends = state.ends_at.replace(tzinfo=timezone.utc) if state.ends_at.tzinfo is None else state.ends_at
    assert before <= started <= after
    assert abs((ends - started) - timedelta(days=7)) < timedelta(seconds=1)

    # /hw_status reports live boss HP, remaining time, and active attacker count.
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
    await db_session.commit()

    status_msg, _ = _mock_private_command(admin_id, "/hw_status")
    await handle_hw_status(status_msg)
    status_reply = status_msg.answer.await_args.args[0]
    assert "250,000" in status_reply
    assert "باقی مانده" in status_reply
    assert "تعداد مهاجمان فعال: 1" in status_reply


@pytest.mark.asyncio
async def test_hw_start_broadcasts_and_enables_flag(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_start

    admin_id = 990011
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    g_active_1 = Group(id=-1009101, title="گروه فعال اول", is_active=True)
    g_active_2 = Group(id=-1009102, title="گروه فعال دوم", is_active=True)
    g_inactive = Group(id=-1009103, title="گروه غیرفعال", is_active=False)
    db_session.add_all([g_active_1, g_active_2, g_inactive])
    await db_session.commit()

    # Populate cached False first to prove cache busting works immediately on /hw_start!
    assert await HalloweenService.is_enabled() is False

    admin_msg, admin_bot = _mock_private_command(admin_id, "/hw_start 500000")
    await handle_hw_start(admin_msg, admin_bot)

    # Both cached zero-session read AND DB read immediately see True!
    assert await HalloweenService.is_enabled() is True
    assert await HalloweenService.is_enabled(db_session) is True

    expected_broadcast = (
        "🎃 ایونت هالووین آغاز شد! سیبومورتیس با 500000 جون به اواریا آمد. کلمه «هالووین» را بزنید!"
    )
    sent_calls = [(call.args[0], call.args[1]) for call in admin_bot.send_message.await_args_list]
    assert len(sent_calls) == 2
    assert set(sent_calls) == {
        (-1009101, expected_broadcast),
        (-1009102, expected_broadcast),
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

    start_msg, start_bot = _mock_private_command(admin_id, "/hw_start 300000")
    await handle_hw_start(start_msg, start_bot)
    assert await HalloweenService.is_enabled() is True

    # Non-admin /hw_end is ignored.
    bad_msg, bad_bot = _mock_private_command(non_admin_id, "/hw_end")
    await handle_hw_end(bad_msg, bad_bot)
    assert await HalloweenService.is_enabled() is True

    # Admin /hw_end flips event is_active=False, disables flag & cache, and broadcasts exact text.
    end_msg, end_bot = _mock_private_command(admin_id, "/hw_end")
    await handle_hw_end(end_msg, end_bot)

    assert await HalloweenService.is_enabled() is False
    assert await HalloweenService.is_enabled(db_session) is False

    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    await db_session.refresh(state)
    assert state.is_active is False

    sent_calls = [(call.args[0], call.args[1]) for call in end_bot.send_message.await_args_list]
    assert sent_calls == [(-1009201, "🎃 ایونت هالووین به پایان رسید.")]


@pytest.mark.asyncio
async def test_top_3_damage_rewards_are_granted_once(db_session, monkeypatch):
    from sib.core.config import settings
    from sib.handlers.admin import handle_hw_end, handle_hw_start

    admin_id = 990031
    monkeypatch.setattr(settings, "ADMIN_IDS", [admin_id])

    players = [await create_test_user(db_session, f"قهرمان {idx}") for idx in range(1, 5)]
    uids = [p.id for p in players]
    await db_session.commit()

    # Scenario A: Boss drops to 0 HP via killing blow -> #1=1000, #2=700, #3=500, #4=0.
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
    state.boss_hp = 5  # Next attack is the killing blow!
    await ItemService.grant(db_session, uids[3], "pumpkin_seed", 1)
    await db_session.commit()

    # Player #4 lands the killing blow (still ranked #4 in total damage):
    await HalloweenService.attack_boss(db_session, uids[3], rng=random.Random(1))
    await db_session.commit()

    assert await AppleWalletService.get_balance(db_session, uids[0]) == 1000
    assert await AppleWalletService.get_balance(db_session, uids[1]) == 700
    assert await AppleWalletService.get_balance(db_session, uids[2]) == 500
    assert await AppleWalletService.get_balance(db_session, uids[3]) == 0

    await db_session.refresh(state)
    assert state.leaderboard_finalized is True
    assert state.leaderboard_finalized_at is not None

    # Subsequent maintenance ticks AND /hw_end MUST NOT pay Top-3 a second time!
    await MaintenanceService.run_once()
    end_msg, end_bot = _mock_private_command(admin_id, "/hw_end")
    await handle_hw_end(end_msg, end_bot)
    await handle_hw_end(end_msg, end_bot)

    assert await AppleWalletService.get_balance(db_session, uids[0]) == 1000
    assert await AppleWalletService.get_balance(db_session, uids[1]) == 700
    assert await AppleWalletService.get_balance(db_session, uids[2]) == 500
    assert await AppleWalletService.get_balance(db_session, uids[3]) == 0

    # Scenario B: New event ended via /hw_end BEFORE boss reaches 0 HP -> still pays Top-3 once!
    start2_msg, start2_bot = _mock_private_command(admin_id, "/hw_start 50000")
    await handle_hw_start(start2_msg, start2_bot)
    state2 = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    for uid, dmg in zip(uids, (100, 900, 400, 50)):  # Order now: u2 (#1), u3 (#2), u1 (#3)
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

    end2_msg, end2_bot = _mock_private_command(admin_id, "/hw_end")
    await handle_hw_end(end2_msg, end2_bot)
    await handle_hw_end(end2_msg, end2_bot)

    assert await AppleWalletService.get_balance(db_session, uids[1]) == 700 + 1000
    assert await AppleWalletService.get_balance(db_session, uids[2]) == 500 + 700
    assert await AppleWalletService.get_balance(db_session, uids[0]) == 1000 + 500
    assert await AppleWalletService.get_balance(db_session, uids[3]) == 0


# ---------------------------------------------------------------------------
# 7. Silent test mode (/hw_start <max_hp> --test & /hw_end --test)
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

    # Warm flag cache with False before starting in silent test mode.
    assert await HalloweenService.is_enabled() is False

    admin_msg, admin_bot = _mock_private_command(admin_id, "/hw_start 5000 --test")
    await handle_hw_start(admin_msg, admin_bot)

    # Event state initialized & feature flag enabled immediately:
    assert await HalloweenService.is_enabled() is True
    assert await HalloweenService.is_enabled(db_session) is True
    state = await db_session.scalar(
        select(HalloweenEventState).order_by(HalloweenEventState.id.desc()).limit(1)
    )
    assert state is not None
    assert state.boss_hp == 5000
    assert state.max_hp == 5000
    assert state.is_active is True

    # ZERO broadcast messages sent to any active group; private confirmation exact:
    assert admin_bot.send_message.await_count == 0
    admin_msg.answer.assert_awaited_once_with(
        "✅ ایونت در حالت تست (بی‌صدا) فعال شد."
    )

    # Even background maintenance ticks stay completely silent in public groups while menus work in test group!
    prev_bot = MaintenanceService._bot
    MaintenanceService._bot = admin_bot
    try:
        admin_bot.send_photo = AsyncMock()
        await MaintenanceService.run_once()
        assert admin_bot.send_message.await_count == 0
        assert admin_bot.send_photo.await_count == 0
    finally:
        MaintenanceService._bot = prev_bot

    test_group_msg = _mock_group_message(tester_id, "هالووین", chat_id=-1009499)
    await handle_halloween_command(test_group_msg)
    assert test_group_msg.answer_photo.await_count == 1
    assert "5,000 / 5,000" in test_group_msg.answer_photo.await_args.kwargs["caption"]


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

    end_msg, end_bot = _mock_private_command(admin_id, "/hw_end --test")
    await handle_hw_end(end_msg, end_bot)

    # Rewards settled, state inactive, flag & cache disabled:
    assert await HalloweenService.is_enabled() is False
    assert await HalloweenService.is_enabled(db_session) is False
    await db_session.refresh(state)
    assert state.is_active is False
    assert state.leaderboard_finalized is True
    assert await AppleWalletService.get_balance(db_session, uid1) == 1000
    assert await AppleWalletService.get_balance(db_session, uid2) == 700

    # ZERO broadcast messages sent to any group; private confirmation exact:
    assert end_bot.send_message.await_count == 0
    end_msg.answer.assert_awaited_once_with("✅ ایونت تستی به پایان رسید.")


