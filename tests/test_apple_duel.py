"""Isolated DB tests: real engine/economy, Telegram mocked at transport boundary."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser
from sib.database.models import (
    AppleBattle,
    AppleDuel,
    AppleDuelSlot,
    SeedlingAward,
)
from sib.handlers.apple_duel import (
    DuelCommand,
    callback_duel,
    command_duel,
    invitation_keyboard,
    normalize_command,
    validate_reply,
)
from sib.services.apple_battle import AppleBattleService
from sib.services.apple_duel import AppleDuelService as Service
from sib.services.apple_duel import DuelError, now_utc
from sib.services.apple_duel_presentation import AppleDuelPresentation as Presentation
from sib.services.apple_duel_presentation import compose_frame, scene_text
from sib.services.player_cards import CardError, PlayerCardService
from sib.services.seedling import SeedlingService
from sib.services.session import SessionService
from sib.tests.conftest import requires_row_locks
from sib.tests.test_apple_battle import _card
from sqlalchemy import func, select, update


@pytest.fixture(autouse=True)
def _disable_incidental_infections(monkeypatch):
    # These legacy tests exercise duel mechanics independently of the new
    # random disease roll; disease-specific scenarios live in integration tests.
    from sib.services import apple_duel

    monkeypatch.setattr(apple_duel, "_duel_infection_chance", AsyncMock(return_value=0))


async def create_test_user(session, user_id):
    from sib.database.models import User

    user = await session.get(User, user_id)
    if user:
        return user
    user = User(
        id=user_id,
        first_name="مریم" if user_id == 701 else "علی",
        is_bot=False,
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


def telegram_message(text="دوئل سیبی", **changes):
    a = TelegramUser(id=701, first_name="مریم", is_bot=False)
    b = TelegramUser(id=702, first_name="علی", is_bot=False)
    target = Message(
        message_id=1, date=now_utc(), chat=Chat(id=-701, type="supergroup"), from_user=b
    )
    values = dict(
        message_id=2,
        date=now_utc(),
        chat=target.chat,
        from_user=a,
        text=text,
        reply_to_message=target,
    )
    values.update(changes)
    return Message(**values)


@pytest.mark.parametrize(
    "text",
    ["دوئل سیبی", "دوعل سیبی", "  دوئل   سيبي! ", "دوعل\u200cسیبی؟", "دوئل\tسیبی"],
)
async def test_command_aliases(text):
    message = telegram_message(text)
    assert await DuelCommand()(message)
    assert len(validate_reply(message)) == 2


@pytest.mark.parametrize(
    "text", ["دوئل", "دوئل سیبی الان", "دویل سیبی", "سلام دوئل سیبی", "نبرد سیبی"]
)
async def test_unrelated_not_commands(text):
    assert not await DuelCommand()(telegram_message(text))


@pytest.mark.parametrize(
    "kind",
    [
        "no_reply",
        "self",
        "bot",
        "no_sender",
        "private",
        "anonymous",
        "anonymous_target",
        "no_target_sender",
        "sender_bot",
    ],
)
def test_invalid_reply_is_persian(kind):
    msg = telegram_message()
    if kind == "no_reply":
        msg = msg.model_copy(update={"reply_to_message": None})
    elif kind == "private":
        msg = msg.model_copy(update={"chat": Chat(id=701, type="private")})
    elif kind in ("no_sender", "anonymous", "sender_bot"):
        changes = (
            {"from_user": None}
            if kind == "no_sender"
            else {"sender_chat": Chat(id=-2, type="supergroup")}
            if kind == "anonymous"
            else {"from_user": msg.from_user.model_copy(update={"is_bot": True})}
        )
        msg = msg.model_copy(update=changes)
    else:
        changes = (
            {"from_user": msg.from_user}
            if kind == "self"
            else {"from_user": None}
            if kind == "no_target_sender"
            else {"sender_chat": Chat(id=-3, type="supergroup")}
            if kind == "anonymous_target"
            else {
                "from_user": msg.reply_to_message.from_user.model_copy(
                    update={"is_bot": True}
                )
            }
        )
        msg = msg.model_copy(
            update={"reply_to_message": msg.reply_to_message.model_copy(update=changes)}
        )
    with pytest.raises(DuelError) as error:
        validate_reply(msg)
    assert any("\u0600" <= c <= "\u06ff" for c in str(error.value))


def test_keyboard_exact_three():
    kb = invitation_keyboard("a" * 32)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["✅ قبول", "❌ رد", "🔄 تعویض کارت"]
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    assert normalize_command("ك ي") == "ک ی"


async def setup_duel(session, *, main=True):
    a = await create_test_user(session, 701)
    b = await create_test_user(session, 702)
    ca = await _card(session, a, "duel_a")
    cb = await _card(session, b, "duel_b")
    cb.level = 3  # Duel has no equal-level queue restriction.
    if main:
        await PlayerCardService.set_favorite(session, a.id, ca.id)
        await PlayerCardService.set_favorite(session, b.id, cb.id)
    duel = await Service.create(session, -701, a.id, b.id)
    duel.message_id = 90
    await session.commit()
    return duel, a, b, ca, cb


async def accept(session, duel):
    return await Service.act(
        session, duel.id, duel.recipient_id, duel.group_id, "accept", message_id=90
    )


async def test_invitation_without_main_and_missing_cards(db_session):
    a = await create_test_user(db_session, 701)
    b = await create_test_user(db_session, 702)
    await _card(db_session, a, "duel_a")
    with pytest.raises(DuelError, match="هیچ کارتی"):
        await Service.create(db_session, -701, a.id, b.id)
    assert await db_session.scalar(select(func.count()).select_from(AppleDuel)) == 0
    await _card(db_session, b, "duel_b")
    duel = await Service.create(db_session, -701, a.id, b.id)
    assert duel.status == "PENDING"
    duel.message_id = 90
    await db_session.commit()
    with pytest.raises(DuelError, match="کارت اصلی"):
        await accept(db_session, duel)
    await db_session.refresh(duel)
    assert duel.status == "PENDING" and duel.battle_id is None


@pytest.mark.parametrize(
    "actor,group,message_id",
    [(701, -701, 90), (703, -701, 90), (702, -702, 90), (702, -701, 91)],
)
@pytest.mark.parametrize("action", ["accept", "reject", "change"])
async def test_role_and_context(db_session, actor, group, message_id, action):
    duel, *_ = await setup_duel(db_session)
    with pytest.raises(DuelError):
        await Service.act(
            db_session, duel.id, actor, group, action, message_id=message_id
        )
    assert duel.status == "PENDING"
    assert await db_session.scalar(select(func.count()).select_from(AppleBattle)) == 0


async def test_change_and_accept_freezes_current_main(db_session, monkeypatch):
    duel, a, b, ca, cb = await setup_duel(db_session)
    await Service.act(db_session, duel.id, b.id, -701, "change", message_id=90)
    assert duel.status == "PENDING" and duel.battle_id is None
    new_card = await _card(db_session, b, "duel_new")
    await PlayerCardService.set_favorite(db_session, b.id, new_card.id)
    engine = AsyncMock(wraps=AppleBattleService.compute_and_persist_result)
    monkeypatch.setattr(AppleBattleService, "compute_and_persist_result", engine)
    await accept(db_session, duel)
    await db_session.commit()
    engine.assert_awaited_once()
    battle = await db_session.get(AppleBattle, duel.battle_id)
    assert battle.player2_card_id == new_card.id
    snapshot = json.loads(duel.snapshot_json)
    assert [c["card_id"] for c in snapshot["cards"]] == [ca.id, new_card.id]
    assert snapshot["events"] == []
    with pytest.raises(CardError, match="شروع شده"):
        await PlayerCardService.set_favorite(db_session, b.id, cb.id)
    saved = battle.result_json
    ca.level = 6
    await db_session.flush()
    await accept(db_session, duel)
    assert battle.result_json == saved and snapshot["cards"][0]["level"] == 1
    engine.assert_awaited_once()


async def test_reject_replay_no_reward(db_session):
    duel, _, b, *_ = await setup_duel(db_session)
    await Service.act(db_session, duel.id, b.id, -701, "reject", message_id=90)
    await accept(db_session, duel)
    assert duel.status == "REJECTED"
    assert await db_session.scalar(select(func.count()).select_from(AppleBattle)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SeedlingAward)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AppleDuelSlot)) == 0


async def test_expiry_restart_and_accept_boundary(db_session):
    duel, *_ = await setup_duel(db_session)
    when = now_utc()
    duel.expires_at = when
    await db_session.commit()
    await Service.act(db_session, duel.id, 702, -701, "accept", now=when)
    assert duel.status == "EXPIRED"
    assert not await Service.expire(db_session, duel.id, now=when)
    assert not await SessionService.has_active_session(db_session, 701)
    assert duel.battle_id is None


async def test_conflicting_invitations_and_shared_session_guard(db_session):
    duel, a, b, *_ = await setup_duel(db_session)
    c = await create_test_user(db_session, 703)
    await _card(db_session, c, "duel_c")
    with pytest.raises(DuelError, match="فعال"):
        await Service.create(db_session, -702, c.id, a.id)
    assert await SessionService.has_active_session(db_session, a.id)
    assert "دوئل" in await SessionService.get_active_session_info(db_session, b.id)
    from sib.core.errors import ActiveSessionExistsException
    from sib.services.party import PartyService

    with pytest.raises(ActiveSessionExistsException):
        await PartyService.create_party(db_session, -701, a.id, "apple_battle", "solo")


async def test_result_reward_atomic_and_idempotent(db_session):
    duel, a, b, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    battle = await db_session.get(AppleBattle, duel.battle_id)
    winner = battle.winner_id
    loser = b.id if winner == a.id else a.id
    assert battle.status == "COMPLETED" and battle.result_json
    assert await SeedlingService.get_total(db_session, winner) == 1
    assert await SeedlingService.get_total(db_session, loser) == 0
    result = battle.result_json
    await accept(db_session, duel)
    assert not await AppleBattleService._award_seedling(db_session, battle.id, winner)
    assert await SeedlingService.get_total(db_session, winner) == 1
    assert battle.result_json == result
    from sib.database.models import AppleTransaction, AppleWallet, Match, PlayerStats

    transactions = list(
        (
            await db_session.scalars(
                select(AppleTransaction).where(
                    AppleTransaction.reason == "apple_battle_win",
                    AppleTransaction.ref_type == "apple_battle",
                    AppleTransaction.ref_id == battle.id,
                )
            )
        ).all()
    )
    assert len(transactions) == 1
    assert transactions[0].user_id == winner
    assert transactions[0].amount == 5
    assert transactions[0].idempotency_key == f"apple_battle_win:{battle.id}"
    assert transactions[0].reward_multiplier == 1
    assert (await db_session.get(AppleWallet, winner)).balance == 5
    assert await db_session.get(AppleWallet, loser) is None
    for table in (Match, PlayerStats):
        assert await db_session.scalar(select(func.count()).select_from(table)) == 0
    assert not await Service.expire(
        db_session, duel.id, now=duel.expires_at + timedelta(days=1)
    )


@pytest.mark.golden_hour
async def test_result_reward_uses_golden_hour_multiplier(db_session):
    from sib.database.models import AppleTransaction, AppleWallet
    from sib.services.golden_hour import TEHRAN

    duel, *_ = await setup_duel(db_session)
    when = datetime(2026, 10, 26, 20, 30, tzinfo=TEHRAN).astimezone(timezone.utc)
    duel.expires_at = when + timedelta(minutes=5)
    await db_session.commit()

    await Service.act(
        db_session,
        duel.id,
        duel.recipient_id,
        duel.group_id,
        "accept",
        message_id=90,
        now=when,
    )
    await db_session.commit()

    battle = await db_session.get(AppleBattle, duel.battle_id)
    transaction = await db_session.scalar(
        select(AppleTransaction).where(
            AppleTransaction.reason == "apple_battle_win",
            AppleTransaction.ref_id == battle.id,
        )
    )
    assert transaction is not None
    assert transaction.user_id == battle.winner_id
    assert transaction.amount == 10
    assert transaction.reward_multiplier == 2
    assert (await db_session.get(AppleWallet, battle.winner_id)).balance == 10


async def test_apple_payment_failure_rolls_back_duel_acceptance(db_session, monkeypatch):
    from sib.database.models import AppleTransaction
    from sib.services.apple_wallet import AppleWalletService

    duel, *_ = await setup_duel(db_session)

    async def fail_earn(*args, **kwargs):
        raise RuntimeError("apple credit unavailable")

    monkeypatch.setattr(AppleWalletService, "earn", fail_earn)
    with pytest.raises(RuntimeError, match="apple credit unavailable"):
        await accept(db_session, duel)

    await db_session.refresh(duel)
    assert duel.status == "PENDING" and duel.battle_id is None
    assert await db_session.scalar(select(func.count()).select_from(AppleBattle)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SeedlingAward)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AppleTransaction)) == 0


async def test_payment_failure_rolls_back_acceptance(db_session, monkeypatch):
    duel, *_ = await setup_duel(db_session)
    original = AppleBattleService._award_seedling

    async def fail_after_write(*args):
        await original(*args)
        raise RuntimeError("payment unavailable")

    monkeypatch.setattr(AppleBattleService, "_award_seedling", fail_after_write)
    with pytest.raises(RuntimeError):
        await accept(db_session, duel)
    await db_session.refresh(duel)
    assert duel.status == "PENDING" and duel.battle_id is None
    assert await db_session.scalar(select(func.count()).select_from(AppleBattle)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SeedlingAward)) == 0
    monkeypatch.setattr(AppleBattleService, "_award_seedling", original)
    await accept(db_session, duel)
    assert duel.status == "ACCEPTED"


@pytest.mark.parametrize("choice", [701, 702])
async def test_future_explicit_draw_resolved_once(db_session, monkeypatch, choice):
    duel, *_ = await setup_duel(db_session)
    engine = AppleBattleService.compute_and_persist_result

    async def draw(session, battle_id):
        battle = await engine(session, battle_id)
        data = json.loads(battle.result_json)
        data["winner_id"] = None
        battle.winner_id = None
        battle.result_json = json.dumps(data)
        return battle

    monkeypatch.setattr(AppleBattleService, "compute_and_persist_result", draw)
    calls = []

    def fair_choice(candidates):
        calls.append(candidates)
        return choice

    await Service.act(db_session, duel.id, 702, -701, "accept", tie_choice=fair_choice)
    await Service.act(db_session, duel.id, 702, -701, "accept", tie_choice=fair_choice)
    battle = await db_session.get(AppleBattle, duel.battle_id)
    assert calls == [[701, 702]] and battle.winner_id == choice
    assert json.loads(battle.result_json)["tie_break"] == "uniform_random"


async def test_unowned_or_inactive_card_and_open_trade(db_session):
    duel, a, b, ca, cb = await setup_duel(db_session)
    from sib.database.models import Character, Trade, TradeStatus

    trade = Trade(
        proposer_id=a.id,
        recipient_id=b.id,
        proposer_card_id=ca.id,
        recipient_card_id=cb.id,
        status=TradeStatus.PENDING,
        expires_at=now_utc() + timedelta(hours=1),
    )
    db_session.add(trade)
    await db_session.flush()
    with pytest.raises(DuelError, match="مبادله"):
        await accept(db_session, duel)
    trade.status = TradeStatus.CANCELLED
    await db_session.flush()
    char = await db_session.get(Character, ca.character_id)
    char.is_active = False
    await db_session.flush()
    with pytest.raises(DuelError, match="دسترس"):
        await accept(db_session, duel)
    char.is_active = True
    cb.user_id = a.id
    await db_session.flush()
    with pytest.raises(DuelError, match="کارت اصلی"):
        await accept(db_session, duel)
    assert duel.status == "PENDING"


def test_images_six_distinct_and_both_art_present():
    import io

    from PIL import Image

    arts = []
    for color in ("red", "blue"):
        buf = io.BytesIO()
        Image.new("RGB", (300, 450), color).save(buf, "PNG")
        arts.append(buf.getvalue())
    images = [compose_frame(arts, stage) for stage in range(6)]
    assert len({hashlib.sha256(raw).hexdigest() for raw in images}) == 6
    for raw in images:
        image = Image.open(io.BytesIO(raw))
        assert image.size == (1000, 650)
        pixels = list(image.convert("RGB").get_flattened_data())
        assert sum(r > 180 and b < 30 for r, g, b in pixels) > 10000
        assert sum(b > 180 and r < 30 for r, g, b in pixels) > 10000


def test_duel_plate_library_manifest_and_files():
    import json as json_lib

    from PIL import Image
    from sib.services.apple_duel_presentation import (
        DUEL_MANIFEST_PATH,
        DUEL_SCENES_DIR,
    )

    raw = json_lib.loads(DUEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    scenes = [s for s in raw["scenes"] if s["status"] == "ready"]
    assert [s["index"] for s in scenes] == [0, 1, 2, 3, 4, 5]
    for scene in scenes:
        path = DUEL_SCENES_DIR / scene["file"]
        assert scene["cast_free"] and path.is_file()
        assert path.stat().st_size <= 400 * 1024
        with Image.open(path) as image:
            assert image.size == (1000, 650)


def _band(image):
    return image.convert("RGB").crop((470, 5, 530, 35))


def _mean_abs_diff(a, b):
    pa, pb = list(a.get_flattened_data()), list(b.get_flattened_data())
    return sum(abs(x - y) for p1, p2 in zip(pa, pb) for x, y in zip(p1, p2)) / (
        len(pa) * 3
    )


async def test_every_scene_photo_uses_its_plate(db_session):
    import io

    from PIL import Image
    from sib.services import apple_duel_presentation as presentation

    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    for index in range(6):
        plate = presentation.scene_plate_bytes(index)
        assert plate is not None
        plain = compose_frame([None, None], index)
        plated = compose_frame([None, None], index, plate)
        plate_image = Image.open(io.BytesIO(plate))
        plated_image = Image.open(io.BytesIO(plated))
        plain_image = Image.open(io.BytesIO(plain))
        # The plate really paints the backdrop: close to the plate, far from
        # the procedural fallback.
        assert _mean_abs_diff(_band(plated_image), _band(plate_image)) < 15
        assert _mean_abs_diff(_band(plated_image), _band(plain_image)) > 30
        photo = await Presentation._photo(SimpleNamespace(), duel, index)
        assert photo.filename == f"duel-{duel.id}-{index}.jpg"
        assert Image.open(io.BytesIO(photo.data)).size == (1000, 650)


async def test_missing_plate_falls_back_to_procedural(db_session, monkeypatch):
    from sib.services import apple_duel_presentation as presentation

    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    monkeypatch.setattr(presentation, "_plate_files", lambda: {})
    presentation.scene_plate_bytes.cache_clear()
    try:
        assert presentation.scene_plate_bytes(0) is None
        photo = await Presentation._photo(SimpleNamespace(), duel, 0)
        assert photo.filename == f"duel-{duel.id}-0.jpg"
    finally:
        presentation.scene_plate_bytes.cache_clear()


async def test_scenes_order_restart_and_transport_failure(db_session, monkeypatch):
    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    battle = await db_session.get(AppleBattle, duel.battle_id)
    result = battle.result_json
    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=RuntimeError("photo")),
        send_message=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))
    for expected in range(6):
        await db_session.execute(
            update(AppleDuel)
            .where(AppleDuel.id == duel.id)
            .values(next_scene_at=now_utc() - timedelta(seconds=1))
        )
        await db_session.commit()
        await Presentation.tick(bot, duel.id)
        await db_session.refresh(duel)
        assert duel.cursor == expected + 1
        # Another worker/restart cannot claim this scene again.
        assert await Service.claim_scene(db_session, duel.id) is None
        await db_session.commit()
    assert duel.status == "COMPLETED" and duel.terminal_presented
    captions = [call.args[1] for call in bot.send_message.await_args_list]
    assert captions == [scene_text(duel, battle, i) for i in range(6)]
    assert battle.result_json == result
    assert await SeedlingService.get_total(db_session, battle.winner_id) == 1
    assert not await SessionService.has_active_session(db_session, 701)


async def test_total_transport_failure_then_recover_final(db_session, monkeypatch):
    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    result = (await db_session.get(AppleBattle, duel.battle_id)).result_json
    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=RuntimeError("offline")),
        send_message=AsyncMock(side_effect=RuntimeError("offline")),
        edit_message_text=AsyncMock(side_effect=RuntimeError("offline")),
    )
    from sib.services import apple_duel_presentation as presentation

    monkeypatch.setattr(presentation, "DUEL_SCENE_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        Presentation, "_photo", AsyncMock(side_effect=RuntimeError("offline"))
    )
    await Presentation.recover_and_publish(bot)
    task = presentation._play_tasks.get(duel.id)
    if task:
        await task
    await db_session.rollback()
    await db_session.refresh(duel)
    # Failed deliveries return their scene: nothing is skipped while the
    # transport is down, and result/reward stay exactly-once throughout.
    assert duel.cursor == 0 and duel.status == "ACCEPTED"
    assert not duel.terminal_presented
    battle = await db_session.get(AppleBattle, duel.battle_id)
    assert battle.result_json == result
    assert await SeedlingService.get_total(db_session, battle.winner_id) == 1
    bot.send_photo.side_effect = None
    bot.send_message.side_effect = None
    await db_session.execute(
        update(AppleDuel)
        .where(AppleDuel.id == duel.id)
        .values(next_scene_at=now_utc() - timedelta(seconds=1))
    )
    await db_session.commit()
    await Presentation.recover_and_publish(bot)
    task = presentation._play_tasks.get(duel.id)
    if task:
        await task
    await db_session.rollback()
    await db_session.refresh(duel)
    assert duel.cursor == 6 and duel.status == "COMPLETED"
    assert duel.terminal_presented
    assert await SeedlingService.get_total(db_session, battle.winner_id) == 1


async def test_scene_delivery_failure_retries_same_scene(db_session, monkeypatch):
    from datetime import timezone

    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    bot = SimpleNamespace(
        send_photo=AsyncMock(side_effect=RuntimeError("flood limit")),
        send_message=AsyncMock(side_effect=RuntimeError("flood limit")),
        edit_message_text=AsyncMock(),
    )
    monkeypatch.setattr(Presentation, "_photo", AsyncMock(return_value="photo"))
    await Presentation.tick(bot, duel.id)
    await db_session.rollback()
    await db_session.refresh(duel)
    # The failed scene is returned to the durable queue, not skipped.
    assert duel.cursor == 0 and duel.status == "ACCEPTED"
    next_at = duel.next_scene_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=timezone.utc)
    assert next_at > now_utc()
    bot.send_photo.side_effect = None
    bot.send_message.side_effect = None
    await db_session.execute(
        update(AppleDuel)
        .where(AppleDuel.id == duel.id)
        .values(next_scene_at=now_utc() - timedelta(seconds=1))
    )
    await db_session.commit()
    await Presentation.tick(bot, duel.id)
    await db_session.rollback()
    await db_session.refresh(duel)
    assert duel.cursor == 1
    battle = await db_session.get(AppleBattle, duel.battle_id)
    assert bot.send_message.await_args.args[1] == scene_text(duel, battle, 0)


async def test_release_scene_never_rewinds_a_newer_claim(db_session):
    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    assert await Service.claim_scene(db_session, duel.id) == 0
    due = now_utc() + timedelta(seconds=30)
    assert await Service.claim_scene(db_session, duel.id, now=due) == 1
    # A stale worker must not rewind scenes a newer claim already passed.
    assert await Service.release_scene(db_session, duel.id, 0) is False
    await db_session.refresh(duel)
    assert duel.cursor == 2
    assert await Service.release_scene(db_session, duel.id, 1) is True
    await db_session.refresh(duel)
    assert duel.cursor == 1
    await db_session.commit()


@pytest.mark.parametrize("error", ["message is not modified", "chat not found"])
async def test_terminal_edit_failure_fallback(db_session, error):
    from aiogram.exceptions import TelegramBadRequest

    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    await db_session.commit()
    battle = await db_session.get(AppleBattle, duel.battle_id)
    send = AsyncMock()
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(
            side_effect=TelegramBadRequest(method="editMessageText", message=error)
        ),
        send_message=send,
    )
    await Presentation.publish_terminal(bot, duel, battle)
    await db_session.rollback()
    await db_session.refresh(duel)
    assert duel.terminal_presented
    if error == "message is not modified":
        send.assert_not_awaited()
    else:
        send.assert_awaited_once()


@pytest.mark.parametrize(
    "race", ["duplicate_accept", "accept_expire", "overlapping_invite"]
)
async def test_concurrent_transitions_cas_layer(db_session, race):
    """Concurrent workers against the status CAS and slot primary key.

    Runs on every backend, including SQLite: it proves the application-level
    compare-and-swap/unique constraints, while ``test_mysql_concurrent_transitions``
    remains the MySQL/MariaDB ``FOR UPDATE`` row-lock proof.
    """
    import asyncio

    from sib.database.connection import get_session
    from sqlalchemy.exc import IntegrityError, OperationalError

    duel, a, _, *_ = await setup_duel(db_session)
    if race == "overlapping_invite":
        c = await create_test_user(db_session, 703)
        await _card(db_session, c, "duel_c")
        await Service.act(db_session, duel.id, 702, -701, "reject")
        await db_session.commit()

    async def worker(operation):
        try:
            async with get_session() as session:
                if operation == "accept":
                    await Service.act(session, duel.id, 702, -701, "accept")
                elif operation == "expire":
                    await Service.expire(
                        session, duel.id, now=now_utc() + timedelta(minutes=5)
                    )
                else:
                    await Service.create(
                        session, -701, a.id, 702 if operation == "invite_b" else 703
                    )
        except (DuelError, IntegrityError, OperationalError):
            return False
        return True

    operations = {
        "duplicate_accept": ["accept", "accept"],
        "accept_expire": ["accept", "expire"],
        "overlapping_invite": ["invite_b", "invite_c"],
    }[race]
    await asyncio.gather(*(worker(op) for op in operations))
    await db_session.rollback()
    await db_session.refresh(duel)
    if race == "overlapping_invite":
        pending = await db_session.scalar(
            select(func.count())
            .select_from(AppleDuel)
            .where(AppleDuel.status == "PENDING", AppleDuel.inviter_id == a.id)
        )
        slots = await db_session.scalar(select(func.count()).select_from(AppleDuelSlot))
        assert pending == 1 and slots == 2
    else:
        assert duel.status in ("ACCEPTED", "EXPIRED")
        count = await db_session.scalar(select(func.count()).select_from(AppleBattle))
        assert count == (1 if duel.status == "ACCEPTED" else 0)
        awards = await db_session.scalar(
            select(func.count()).select_from(SeedlingAward)
        )
        assert awards == count


async def test_maintenance_expiry_without_telegram(db_session):
    duel, *_ = await setup_duel(db_session)
    duel.expires_at = now_utc() - timedelta(seconds=1)
    await db_session.commit()
    await Presentation.recover_and_publish(None)
    await db_session.refresh(duel)
    assert duel.status == "EXPIRED" and duel.battle_id is None


async def test_handler_invalid_feedback_no_db(monkeypatch):
    msg = telegram_message(reply_to_message=None)
    reply = AsyncMock()
    monkeypatch.setattr(Message, "reply", reply)
    await command_duel(msg, SimpleNamespace())
    assert "ریپلای" in reply.call_args.args[0]


async def test_callback_malformed_rejected(monkeypatch):
    import sib.handlers.apple_duel as handlers

    answer = AsyncMock()
    monkeypatch.setattr(handlers, "safe_answer_callback", answer)
    query = CallbackQuery(
        id="1",
        from_user=telegram_message().from_user,
        chat_instance="1",
        message=telegram_message(),
        data="ad:accept:bad",
    )
    await callback_duel(query, SimpleNamespace())
    assert "معتبر" in answer.call_args.args[1]


async def test_existing_card_selection_flow_preserves_invitation(
    db_session, monkeypatch
):
    import sib.handlers.start as start
    from sib.database.models import PanelOwner
    from sib.handlers.apple_duel import open_existing_cards
    from sib.handlers.cards import handle_card_main_set

    duel, _, b, _, cb = await setup_duel(db_session, main=False)
    original_message = telegram_message().model_copy(update={"message_id": 90})
    sent = original_message.model_copy(update={"message_id": 91, "text": "کارت‌های من"})
    bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
    query = CallbackQuery(
        id="select",
        from_user=original_message.reply_to_message.from_user,
        chat_instance="1",
        message=original_message,
        data=f"ad:change:{duel.id}",
    )
    editor = AsyncMock()
    monkeypatch.setattr(start, "safe_edit_callback_message", editor)
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock())
    await open_existing_cards(query, bot)
    owner = await db_session.get(PanelOwner, (-701, 91))
    assert owner.owner_user_id == b.id and owner.panel_type == "cards"
    kb = editor.call_args.kwargs["reply_markup"]
    assert any(
        button.callback_data == f"card_panel:{cb.id}"
        for row in kb.inline_keyboard
        for button in row
    )
    import sib.handlers.cards as cards_handler

    monkeypatch.setattr(cards_handler, "_show", AsyncMock())
    monkeypatch.setattr(
        cards_handler, "_card_view", AsyncMock(return_value=("کارت", None, None))
    )
    selection = query.model_copy(
        update={"message": sent, "data": f"card_main_set:{cb.id}:0"}
    )
    await handle_card_main_set(selection, bot)
    await db_session.refresh(duel)
    assert duel.status == "PENDING" and duel.message_id == 90 and duel.battle_id is None
    assert (await PlayerCardService.get_main_card(db_session, b.id)).id == cb.id


@pytest.mark.parametrize("text", ["دوئل سیبی", "دوعل سیبی"])
async def test_real_command_creates_persisted_invitation(db_session, monkeypatch, text):
    import sib.handlers.apple_duel as handler

    a = await create_test_user(db_session, 701)
    b = await create_test_user(db_session, 702)
    await _card(db_session, a, "duel_a")
    await _card(db_session, b, "duel_b")
    sent = telegram_message().model_copy(update={"message_id": 90})
    transport = AsyncMock(return_value=sent)
    monkeypatch.setattr(Message, "reply", transport)
    await handler.command_duel(telegram_message(text), SimpleNamespace())
    duel = await db_session.scalar(select(AppleDuel))
    assert duel.status == "PENDING" and duel.message_id == 90
    assert len(transport.call_args.kwargs["reply_markup"].inline_keyboard) == 3
    invitation = transport.call_args.args[0]
    assert "**مریم**" in invitation and "**علی**" in invitation
    assert transport.call_args.kwargs["parse_mode"] == "Markdown"
    assert await db_session.scalar(select(func.count()).select_from(AppleDuelSlot)) == 2


async def test_deadline_rechecked_after_lock_wait(db_session, monkeypatch):
    import sib.services.apple_duel as domain

    duel, *_ = await setup_duel(db_session)
    clock = now_utc()
    duel.expires_at = clock + timedelta(seconds=1)
    await db_session.commit()
    calls = iter([clock, clock, clock + timedelta(seconds=2)])
    monkeypatch.setattr(domain, "now_utc", lambda: next(calls))
    await accept(db_session, duel)
    assert duel.status == "EXPIRED" and duel.battle_id is None


# Real FOR UPDATE tests must run on MySQL/MariaDB, not be "proved" by SQLite.


@requires_row_locks
@pytest.mark.parametrize(
    "race", ["duplicate_accept", "accept_expire", "overlapping_invite"]
)
async def test_mysql_concurrent_transitions(db_session, race):
    import asyncio

    from sib.database.connection import get_session

    duel, a, _, *_ = await setup_duel(db_session)
    if race == "overlapping_invite":
        c = await create_test_user(db_session, 703)
        await _card(db_session, c, "duel_c")
        await Service.act(db_session, duel.id, 702, -701, "reject")
        await db_session.commit()

    async def worker(operation):
        async with get_session() as session:
            if operation == "accept":
                await Service.act(session, duel.id, 702, -701, "accept")
            elif operation == "expire":
                await Service.expire(
                    session, duel.id, now=now_utc() + timedelta(minutes=5)
                )
            else:
                try:
                    await Service.create(
                        session, -701, a.id, 702 if operation == "invite_b" else 703
                    )
                except DuelError:
                    return False
        return True

    operations = {
        "duplicate_accept": ["accept", "accept"],
        "accept_expire": ["accept", "expire"],
        "overlapping_invite": ["invite_b", "invite_c"],
    }[race]
    outcomes = await asyncio.gather(*(worker(op) for op in operations))
    await db_session.refresh(duel)
    if race == "overlapping_invite":
        assert sum(outcomes) == 1
    else:
        assert duel.status in ("ACCEPTED", "EXPIRED")
        count = await db_session.scalar(select(func.count()).select_from(AppleBattle))
        assert count == (1 if duel.status == "ACCEPTED" else 0)
        awards = await db_session.scalar(
            select(func.count()).select_from(SeedlingAward)
        )
        assert awards == count


async def test_renderer_uses_registered_and_bundled_art(db_session, monkeypatch):
    import io

    from PIL import Image
    from sib.services.assets import AssetService
    from sib.services.character_art import resolve_character_photo

    # Compatibility regression: resolver previously called nonexistent get_file_id.
    monkeypatch.setattr(
        AssetService, "file_id", AsyncMock(return_value="registered-photo")
    )
    source = await resolve_character_photo(
        db_session, SimpleNamespace(code="sib_sorkh", artwork="custom")
    )
    assert source == ("registered-photo", None)
    assert AssetService.file_id.await_args.args[1] == "custom"
    monkeypatch.setattr(AssetService, "file_id", AsyncMock(return_value=None))
    file_id, path = await resolve_character_photo(
        db_session, SimpleNamespace(code="sib_sorkh", artwork="custom")
    )
    assert file_id is None and path.is_file()
    duel, *_ = await setup_duel(db_session)
    await accept(db_session, duel)
    snap = json.loads(duel.snapshot_json)
    for card, code in zip(snap["cards"], ("sib_sorkh", "sib_sabz")):
        card["code"] = code
    duel.snapshot_json = json.dumps(snap)
    await db_session.commit()
    photo = await Presentation._photo(SimpleNamespace(), duel, 0)
    assert photo.filename.endswith("-0.jpg")
    image = Image.open(io.BytesIO(photo.data))
    assert image.size == (1000, 650)
