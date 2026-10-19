"""Apple Clash (Group War) tests covering energy, cooldown, items, declaration and UI styling."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sib.database.models import (
    Character,
    Group,
    GroupCastle,
    GroupWar,
    PlayerCard,
    User,
    WarAttack,
    WarParticipant,
)
from sib.handlers.castle import keyboard as castle_keyboard
from sib.handlers.war import war_keyboard
from sib.services.apple_wallet import AppleWalletService
from sib.services.castle_service import (
    CastleService,
    THRESHOLDS,
    level_floor,
    level_for_total,
)
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.player_cards import PlayerCardService
from sib.services.war_service import WarError, WarOutcome, WarService, war_loot


@pytest_asyncio.fixture
async def war_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        from sib.tests.conftest import _create_all_compatible

        await conn.run_sync(_create_all_compatible)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        # Create users
        u1 = User(id=1, first_name="مهاجم اول", language_code="fa")
        u2 = User(id=2, first_name="مدافع اول", language_code="fa")
        session.add_all([u1, u2])

        # Create groups
        g1 = Group(id=-1001, title="قبیله عقاب", is_active=True)
        g2 = Group(id=-1002, title="قبیله شیر", is_active=True)
        session.add_all([g1, g2])
        await session.flush()

        # Create castles
        c1 = await CastleService.get_or_create_castle(session, -1001)
        c2 = await CastleService.get_or_create_castle(session, -1002)
        c1.level = 3
        c2.level = 4
        await session.flush()

        # Create character and cards
        char1 = Character(
            id=1,
            code="apple_knight",
            name_fa="شوالیه سیب",
            power=50,
            speed=30,
            intelligence=20,
        )
        session.add(char1)
        await session.flush()

        card1 = PlayerCard(id=101, user_id=1, character_id=1, level=1, is_favorite=True)
        card2 = PlayerCard(id=102, user_id=2, character_id=1, level=1, is_favorite=True)
        session.add_all([card1, card2])
        await session.flush()

        # Wallets
        await AppleWalletService.earn(session, 1, 5000, reason="test")
        await AppleWalletService.earn(session, 2, 5000, reason="test")
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_healing_potion_added_to_shop(war_session):
    s = war_session
    assert "healing_potion" in ITEM_CATALOG
    assert ITEM_CATALOG["healing_potion"]["label"] == "معجون شفا"
    assert "۵۰ واحد از انرژی جنگی" in ITEM_CATALOG["healing_potion"]["description"]

    assert await ItemService.buy_price(s, "healing_potion") == 150

    # User buys healing potion for 150 apples
    assert await AppleWalletService.get_balance(s, 1) == 5000
    applied = await ItemService.buy(s, 1, "healing_potion", 1)
    assert applied == 1
    assert await AppleWalletService.get_balance(s, 1) == 4850
    assert await ItemService.get_total(s, 1, "healing_potion") == 1


@pytest.mark.asyncio
async def test_declaration_requires_admin(war_session, monkeypatch):
    from contextlib import asynccontextmanager
    from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
    from sib.handlers import castle as castle_handler

    @asynccontextmanager
    async def session_ctx():
        yield war_session

    monkeypatch.setattr(castle_handler, "get_session", session_ctx)
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)

    # 1. Non-admin user tries to start war
    bot_member = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
    )
    actor = TgUser(id=99, first_name="کاربر عادی", is_bot=False)
    msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-1001, type="supergroup"),
    )
    cb = CallbackQuery(
        id="cb-non-admin",
        from_user=actor,
        chat_instance="test",
        message=msg,
        data="castle:war_start:-1001",
    ).as_(bot_member)

    await castle_handler.castle_callback(cb)
    answer.assert_awaited()
    assert "تنها مدیران گروه" in str(answer.await_args)

    # 2. Admin user clicks war start
    answer.reset_mock()
    bot_admin = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator"))
    )
    cb_admin = CallbackQuery(
        id="cb-admin",
        from_user=actor,
        chat_instance="test",
        message=msg,
        data="castle:war_start:-1001",
    ).as_(bot_admin)

    await castle_handler.castle_callback(cb_admin)
    answer.assert_awaited()
    assert "تنها مدیران گروه" not in str(answer.await_args)


@pytest.mark.asyncio
async def test_war_duration_is_5_hours(war_session):
    s = war_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    # Both castles start not in war
    c1 = await s.scalar(select(GroupCastle).where(GroupCastle.group_id == -1001))
    c2 = await s.scalar(select(GroupCastle).where(GroupCastle.group_id == -1002))
    assert c1.in_war is False
    assert c2.in_war is False

    war = await WarService.declare_war(s, -1001, -1002, now=now)
    assert war.id is not None
    assert war.attacker_id == -1001
    assert war.defender_id == -1002
    assert war.status == "active"
    assert war.attacker_damage == 0
    assert war.defender_damage == 0
    assert war.ends_at == now + timedelta(hours=5)

    # in_war flags are updated
    assert c1.in_war is True
    assert c2.in_war is True

    # Declaring another war while in war fails
    with pytest.raises(WarError, match="در حال حاضر در نبرد است"):
        await WarService.declare_war(s, -1001, -1002, now=now)


@pytest.mark.asyncio
async def test_war_attack_costs_20_energy(war_session):
    s = war_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=now)

    # User 1 starts with 100 energy naturally
    damage, remaining_energy, card_name = await WarService.attack(
        s, war.id, 1, -1001, now=now
    )
    assert damage > 0
    assert remaining_energy == 80
    assert card_name == "شوالیه سیب"

    part = await s.scalar(
        select(WarParticipant).where(
            WarParticipant.war_id == war.id, WarParticipant.user_id == 1
        )
    )
    assert part.energy == 80
    assert part.total_damage == damage


@pytest.mark.asyncio
async def test_attack_fails_without_enough_energy(war_session):
    s = war_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=now)

    part = await WarService.get_or_create_participant(s, war.id, 1, -1001)
    part.energy = 19  # less than 20
    await s.flush()

    with pytest.raises(WarError, match="انرژی جنگی شما کافی نیست"):
        await WarService.attack(s, war.id, 1, -1001, now=now)


@pytest.mark.asyncio
async def test_war_attack_cooldown_is_30_seconds(war_session):
    s = war_session
    t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=t0)

    # First attack at t0
    await WarService.attack(s, war.id, 1, -1001, now=t0)

    # Immediate retries show Persian 30, and fractional seconds round UP.
    for seconds, displayed in [(0, "۳۰"), (29, "۱"), (29.999, "۱")]:
        with pytest.raises(WarError, match=f"{displayed} ثانیه دیگر صبر کنید"):
            await WarService.attack(
                s, war.id, 1, -1001, now=t0 + timedelta(seconds=seconds)
            )
    part = await WarService.get_or_create_participant(s, war.id, 1, -1001)
    assert part.energy == 80
    assert len((await s.scalars(select(WarAttack))).all()) == 1

    # Exactly 30 seconds succeeds (also exercises a naive MariaDB timestamp).
    part.last_attack_at = t0.replace(tzinfo=None)
    damage2, energy2, _ = await WarService.attack(
        s, war.id, 1, -1001, now=t0 + timedelta(seconds=30)
    )
    assert damage2 > 0
    assert energy2 == 60


@pytest.mark.asyncio
async def test_healing_potion_restores_50_energy(war_session):
    s = war_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=now)

    part = await WarService.get_or_create_participant(s, war.id, 1, -1001)
    part.energy = 30
    await s.flush()

    # User has no potion yet
    with pytest.raises(WarError, match="شما معجون شفا ندارید"):
        await WarService.use_healing_potion(s, war.id, 1, -1001)

    # Grant 2 potions
    await ItemService.grant(s, 1, "healing_potion", 2)

    # First drink restores 50: 30 + 50 = 80
    new_energy = await WarService.use_healing_potion(s, war.id, 1, -1001)
    assert new_energy == 80
    assert part.energy == 80
    assert await ItemService.get_total(s, 1, "healing_potion") == 1

    # Second drink caps at 100: min(100, 80 + 50) = 100
    new_energy2 = await WarService.use_healing_potion(s, war.id, 1, -1001)
    assert new_energy2 == 100
    assert part.energy == 100
    assert await ItemService.get_total(s, 1, "healing_potion") == 0

    # Trying to drink at 100 energy raises error
    await ItemService.grant(s, 1, "healing_potion", 1)
    with pytest.raises(WarError, match="انرژی جنگی شما پر است"):
        await WarService.use_healing_potion(s, war.id, 1, -1001)


@pytest.mark.asyncio
async def test_damage_goes_to_correct_pool(war_session):
    s = war_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=now)

    # Attacker attack (User 1 in group -1001)
    dmg_atk, _, _ = await WarService.attack(s, war.id, 1, -1001, now=now)
    assert war.attacker_damage == dmg_atk
    assert war.defender_damage == 0

    # Defender attack (User 2 in group -1002)
    dmg_def, _, _ = await WarService.attack(s, war.id, 2, -1002, now=now)
    assert war.attacker_damage == dmg_atk
    assert war.defender_damage == dmg_def


def test_war_buttons_have_correct_colors():
    """Verify that every button in war menus is styled with semantic glass colors."""
    # 1. Attacker view keyboard
    atk_kb = war_keyboard(war_id=1, group_id=-1001, is_attacker=True)
    atk_buttons = [btn for row in atk_kb.inline_keyboard for btn in row]
    atk_map = {b.callback_data: (b.text, b.style) for b in atk_buttons}

    expected_atk = {
        "war:atk:1:-1001:normal": ("⚔️ حمله به قلعه", "success"),
        "war:atk:1:-1001:buff": ("💥 حمله با سیب باف", "primary"),
        "war:potion:1:-1001": ("🧪 استفاده از معجون شفا", "primary"),
        "castle:view:-1001": ("🔙 بازگشت", "danger"),
    }
    assert atk_map == expected_atk
    assert all(b.style in {"success", "primary", "danger"} for b in atk_buttons)

    # 2. Defender view keyboard
    def_kb = war_keyboard(war_id=1, group_id=-1002, is_attacker=False)
    def_buttons = [btn for row in def_kb.inline_keyboard for btn in row]
    def_map = {b.callback_data: (b.text, b.style) for b in def_buttons}

    expected_def = {
        "war:atk:1:-1002:normal": ("🛡 دفاع از قلعه", "success"),
        "war:atk:1:-1002:buff": ("💥 دفاع با سیب باف", "primary"),
        "war:potion:1:-1002": ("🧪 استفاده از معجون شفا", "primary"),
        "castle:view:-1002": ("🔙 بازگشت", "danger"),
    }
    assert def_map == expected_def
    assert all(b.style in {"success", "primary", "danger"} for b in def_buttons)

    # 3. Castle menu with admin flag has green start war button
    admin_castle_kb = castle_keyboard(group_id=-1001, is_admin=True)
    admin_castle_buttons = [
        btn for row in admin_castle_kb.inline_keyboard for btn in row
    ]
    admin_castle_map = {
        b.callback_data: (b.text, b.style) for b in admin_castle_buttons
    }

    assert "castle:war_start:-1001" in admin_castle_map
    assert admin_castle_map["castle:war_start:-1001"] == ("⚔️ شروع حمله", "success")
    assert all(
        b.style in {"success", "primary", "danger"} for b in admin_castle_buttons
    )


def test_war_narrative_pools_have_variants():
    from sib.services.war.narrative import (
        WAR_ATTACK_ATTACKER,
        WAR_ATTACK_DEFENDER,
        render_attacker_narrative,
        render_defender_narrative,
    )

    assert len(WAR_ATTACK_ATTACKER) >= 5
    assert len(WAR_ATTACK_DEFENDER) >= 5

    text_atk = render_attacker_narrative(
        player="سهراب",
        card="شوالیه سیب",
        target_group="شیرها",
        damage=1250,
        is_buff=True,
    )
    assert "سهراب" in text_atk
    assert "شوالیه سیب" in text_atk
    assert "شیرها" in text_atk
    assert "سیب باف" in text_atk

    text_def = render_defender_narrative(
        player="رستم",
        card="سایه ایگریس",
        source_group="عقاب‌ها",
        damage=800,
        is_buff=False,
    )
    assert "رستم" in text_def
    assert "سایه ایگریس" in text_def
    assert "عقاب‌ها" in text_def
    assert "سیب باف" not in text_def


def test_compose_war_attack_image_returns_jpeg():
    from io import BytesIO
    from PIL import Image
    from sib.services.war.imaging import compose_war_attack_image

    dummy_card = BytesIO()
    Image.new("RGB", (100, 100), "blue").save(dummy_card, format="JPEG")
    card_bytes = dummy_card.getvalue()

    result = compose_war_attack_image(
        war_id=1,
        user_id=1,
        card_image_bytes=card_bytes,
        damage=2450,
        is_buff=True,
        perspective="attacker",
    )
    assert isinstance(result, bytes)
    assert result.startswith(b"\xff\xd8")

    with Image.open(BytesIO(result)) as img:
        assert img.format == "JPEG"
        assert img.size == (1280, 720)


@pytest.mark.asyncio
async def test_attack_sends_two_different_narratives(war_session, monkeypatch):
    from contextlib import asynccontextmanager
    from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
    from sib.handlers import war as war_handler

    @asynccontextmanager
    async def session_ctx():
        yield war_session

    monkeypatch.setattr(war_handler, "get_session", session_ctx)
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)

    now = datetime.now(timezone.utc)
    war = await WarService.declare_war(war_session, -1001, -1002, now=now)

    send_photo_mock = AsyncMock()
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        send_photo=send_photo_mock,
        send_message=AsyncMock(),
    )
    actor = TgUser(id=1, first_name="آرش کمانگیر", is_bot=False)
    msg = Message(
        message_id=10,
        date=now,
        chat=Chat(id=-1001, type="supergroup"),
    )
    cb = CallbackQuery(
        id="cb-atk",
        from_user=actor,
        chat_instance="test",
        message=msg,
        data=f"war:atk:{war.id}:-1001:normal",
    ).as_(bot)

    await war_handler.war_attack_callback(cb)

    # Dispatches sent to both attacker and defender chats
    assert send_photo_mock.await_count == 2
    call_args_list = send_photo_mock.await_args_list

    chat_ids = [call.kwargs.get("chat_id") for call in call_args_list]
    assert -1001 in chat_ids
    assert -1002 in chat_ids

    captions = {
        call.kwargs.get("chat_id"): call.kwargs.get("caption")
        for call in call_args_list
    }
    assert (
        captions[-1001] != captions[-1002]
    )  # Attacker and defender see different perspectives
    assert "tg://user?id=1" in captions[-1001]  # Attacker team sees teammate link
    assert (
        "tg://user" not in captions[-1002]
    )  # Defender team sees plain text with NO link or ID


def test_defender_narrative_has_no_tg_user_link():
    from sib.handlers.war import _mention
    from sib.services.war.narrative import (
        WAR_ATTACK_DEFENDER,
        render_defender_narrative,
    )

    # 1. Plain name test
    for _ in range(len(WAR_ATTACK_DEFENDER) * 2):
        text = render_defender_narrative(
            player="رستم",
            card="سایه ایگریس",
            source_group="شیرها",
            damage=1200,
            is_buff=True,
        )
        assert "tg://user" not in text
        assert "رستم" in text

    # 2. Even if passed a _mention with tg://user link, it must be stripped
    mention_str = _mention(88888, "سهراب پهلوان")
    assert "tg://user?id=88888" in mention_str

    for _ in range(len(WAR_ATTACK_DEFENDER) * 2):
        text = render_defender_narrative(
            player=mention_str,
            card="شوالیه سیب",
            source_group="شاهین‌ها",
            damage=950,
            is_buff=False,
        )
        assert "tg://user" not in text
        assert "88888" not in text
        assert "سهراب پهلوان" in text

    # 3. Hostile scheme / HTML link injection
    hostile_player = (
        "@attacker <a href='tg://user?id=99999'>دشمن</a> tg://user?id=77777"
    )
    for _ in range(len(WAR_ATTACK_DEFENDER) * 2):
        text = render_defender_narrative(
            player=hostile_player,
            card="کارت تاریک",
            source_group="گرگ‌ها",
            damage=600,
        )
        assert "tg://user" not in text
        assert "99999" not in text
        assert "77777" not in text
        assert "@attacker" not in text


def test_war_attack_image_perspective(monkeypatch):
    from io import BytesIO
    from sib.services.war import imaging

    opened_files = []
    real_open = imaging.Image.open

    def mock_open(fp, *args, **kwargs):
        opened_files.append(str(fp))
        return real_open(fp, *args, **kwargs)

    monkeypatch.setattr(imaging.Image, "open", mock_open)

    dummy_card = BytesIO()
    imaging.Image.new("RGB", (100, 100), "red").save(dummy_card, format="JPEG")
    card_bytes = dummy_card.getvalue()

    # 1. Attacker perspective loads war_bg_attacker.jpg
    opened_files.clear()
    imaging.compose_war_attack_image(
        war_id=1,
        user_id=1,
        card_image_bytes=card_bytes,
        damage=500,
        is_buff=False,
        perspective="attacker",
    )
    assert any("war_bg_attacker.jpg" in f for f in opened_files)

    # 2. Defender perspective loads war_bg_defender.jpg
    opened_files.clear()
    imaging.compose_war_attack_image(
        war_id=1,
        user_id=1,
        card_image_bytes=card_bytes,
        damage=500,
        is_buff=False,
        perspective="defender",
    )
    assert any("war_bg_defender.jpg" in f for f in opened_files)


@pytest.mark.parametrize("winner_id", [-1001, -1002])
async def test_war_peace_shield_is_12_hours(war_session, winner_id):
    s = war_session
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=start)
    war.attacker_damage = 200 if winner_id == -1001 else 100
    war.defender_damage = 100 if winner_id == -1001 else 200
    await s.commit()
    end = start + timedelta(hours=5)
    assert not await WarService.finish_war(
        s, war.id, now=end - timedelta(microseconds=1)
    )
    assert war.status == "active"
    assert await WarService.finish_expired_wars(s, now=end) == 1
    loser_id = -1002 if winner_id == -1001 else -1001
    loser = await s.get(GroupCastle, loser_id)
    winner = await s.get(GroupCastle, winner_id)
    assert war.status == "finished" and war.winner_id == winner_id
    assert not loser.in_war and not winner.in_war
    assert loser.shield_until == end + timedelta(hours=12)
    assert winner.shield_until is None
    await s.commit()
    shield_end = loser.shield_until
    # Repeated expiration never extends an already applied shield.
    assert await WarService.finish_expired_wars(s, now=end + timedelta(minutes=10)) == 0
    assert not await WarService.finish_war(s, war.id, now=end + timedelta(minutes=10))
    assert loser.shield_until.replace(tzinfo=timezone.utc) == shield_end.replace(
        tzinfo=timezone.utc
    )
    # Shielded castles cannot be attacked or initiate a war.
    for attacker, defender in [(winner_id, loser_id), (loser_id, winner_id)]:
        with pytest.raises(WarError, match="سپر صلح"):
            await WarService.declare_war(
                s,
                attacker,
                defender,
                now=end + timedelta(hours=12) - timedelta(microseconds=1),
            )
    assert (
        await WarService.find_target_groups(s, winner_id, now=end + timedelta(hours=11))
        == []
    )
    # Exactly 12 hours after settlement the peace shield has expired.
    assert (
        len(
            await WarService.find_target_groups(
                s, winner_id, now=end + timedelta(hours=12)
            )
        )
        == 1
    )
    new_war = await WarService.declare_war(
        s, winner_id, loser_id, now=end + timedelta(hours=12)
    )
    assert new_war.status == "active"


async def test_war_tie_has_no_loser_or_shield(war_session):
    s = war_session
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=start)
    assert await WarService.finish_war(s, war.id, now=start + timedelta(hours=5))
    assert war.status == "finished" and war.winner_id is None
    for group in [-1001, -1002]:
        castle = await s.get(GroupCastle, group)
        assert not castle.in_war and castle.shield_until is None


async def test_war_five_attacks_exhaust_energy(war_session):
    s = war_session
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=start)
    for i in range(5):
        _, energy, _ = await WarService.attack(
            s, war.id, 1, -1001, now=start + timedelta(seconds=i * 30)
        )
        assert energy == 100 - (i + 1) * 20
    with pytest.raises(WarError, match="۲۰ انرژی"):
        await WarService.attack(s, war.id, 1, -1001, now=start + timedelta(seconds=150))
    assert len((await s.scalars(select(WarAttack))).all()) == 5


async def test_war_attacks_stop_at_five_hour_deadline(war_session):
    s = war_session
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=start)
    with pytest.raises(WarError, match="زمان نبرد به پایان رسیده"):
        await WarService.attack(s, war.id, 1, -1001, now=start + timedelta(hours=5))
    assert not (await s.scalars(select(WarAttack))).all()


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (3 * 3600 + 15 * 60, "۳ ساعت و ۱۵ دقیقه"),
        (5 * 3600, "۵ ساعت و ۰ دقیقه"),
        (59, "۰ ساعت و ۰ دقیقه"),
        (-1, "۰ ساعت و ۰ دقیقه"),
    ],
)
def test_war_menu_shows_remaining_time_in_persian(seconds, expected):
    from sib.handlers.war import format_war_caption

    caption = format_war_caption("مهاجم", 120, "مدافع", 200, seconds, 80)
    assert f"⏳ زمان باقی‌مانده: {expected}" in caption
    assert "هر ضربه ۲۰ انرژی" in caption and "۳۰ ثانیه" in caption
    assert not any(digit in caption for digit in "0123456789")


async def test_war_expiration_runs_in_maintenance(war_session, monkeypatch):
    from contextlib import asynccontextmanager
    from sib.services.maintenance import service as maintenance
    from sib.services.timeout import TimeoutService
    from sib.services.session import SessionService
    from sib.services.trading import TradeService
    from sib.services.match_result import MatchResultService
    from sib.services.apple_duel_presentation import AppleDuelPresentation

    now = datetime.now(timezone.utc)
    war = await WarService.declare_war(
        war_session, -1001, -1002, now=now - timedelta(hours=6)
    )
    war.attacker_damage = 1
    await war_session.commit()

    @asynccontextmanager
    async def session_ctx():
        yield war_session
        await war_session.commit()

    monkeypatch.setattr(maintenance, "get_session", session_ctx)
    worker = maintenance.MaintenanceService
    for method in [
        "cleanup_zombie_parties",
        "process_expired_queue_entries",
        "run_marriage_love_cycle",
        "_resume_pending_bot_turns",
    ]:
        monkeypatch.setattr(worker, method, AsyncMock())
    monkeypatch.setattr(worker, "_invasion_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(SessionService, "reconcile_stale_state", AsyncMock())
    monkeypatch.setattr(TradeService, "expire_overdue", AsyncMock())
    monkeypatch.setattr(TimeoutService, "process_acceptance_timeouts", AsyncMock())
    monkeypatch.setattr(
        TimeoutService, "process_turn_timeouts", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        MatchResultService, "retry_pending_xp_awards", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(AppleDuelPresentation, "recover_and_publish", AsyncMock())
    await worker.run_once()
    assert war.status == "finished"
    loser = await war_session.get(GroupCastle, -1002)
    assert loser.shield_until is not None and not loser.in_war


async def test_expired_wars_are_finalized_in_maintenance(war_session, monkeypatch):
    """C2-4.5: the maintenance loop finalizes every DUE war - and only those.

    The zombie-party sweep is the first task of each cycle and was failing with
    the production ``CompileError``; war finalization must not depend on it.
    """
    from contextlib import asynccontextmanager
    from sqlalchemy.exc import CompileError
    from sib.services.maintenance import service as maintenance
    from sib.services.timeout import TimeoutService
    from sib.services.session import SessionService
    from sib.services.trading import TradeService
    from sib.services.match_result import MatchResultService
    from sib.services.apple_duel_presentation import AppleDuelPresentation

    def aware(moment):
        return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment

    s = war_session
    now = datetime.now(timezone.utc)
    # A second, unrelated pair of castles whose war is still running.
    s.add_all(
        [
            Group(id=-1003, title="قبیله گرگ", is_active=True),
            Group(id=-1004, title="قبیله ببر", is_active=True),
        ]
    )
    await s.flush()
    await CastleService.get_or_create_castle(s, -1003)
    await CastleService.get_or_create_castle(s, -1004)

    due = await WarService.declare_war(s, -1001, -1002, now=now - timedelta(hours=6))
    due.attacker_damage, due.defender_damage = 100, 250  # the DEFENDER wins
    running = await WarService.declare_war(s, -1003, -1004, now=now)  # ~5h left
    running.attacker_damage = 999
    await s.commit()
    due_id, running_id = due.id, running.id

    @asynccontextmanager
    async def session_ctx():
        yield s
        await s.commit()

    monkeypatch.setattr(maintenance, "get_session", session_ctx)
    worker = maintenance.MaintenanceService
    production_error = CompileError(
        'Dialect "mysql" does not support RETURNING with DELETE..USING; for '
        "synchronize_session='fetch', please add the additional execution "
        "option 'is_delete_using=True' to the statement"
    )
    monkeypatch.setattr(worker, "cleanup_zombie_parties", AsyncMock(side_effect=production_error))
    for method in [
        "process_expired_queue_entries",
        "run_marriage_love_cycle",
        "_resume_pending_bot_turns",
    ]:
        monkeypatch.setattr(worker, method, AsyncMock())
    monkeypatch.setattr(worker, "_invasion_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(SessionService, "reconcile_stale_state", AsyncMock())
    monkeypatch.setattr(TradeService, "expire_overdue", AsyncMock())
    monkeypatch.setattr(TimeoutService, "process_acceptance_timeouts", AsyncMock())
    monkeypatch.setattr(
        TimeoutService, "process_turn_timeouts", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        MatchResultService, "retry_pending_xp_awards", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(AppleDuelPresentation, "recover_and_publish", AsyncMock())

    # Re-read from the database (not the identity map) to see what was committed.
    async def war_row(war_id):
        return await s.scalar(
            select(GroupWar)
            .where(GroupWar.id == war_id)
            .execution_options(populate_existing=True)
        )

    async def castle_row(group_id):
        return await s.scalar(
            select(GroupCastle)
            .where(GroupCastle.group_id == group_id)
            .execution_options(populate_existing=True)
        )

    before = datetime.now(timezone.utc)
    await worker.run_once()  # the sweep above raises; finalization must still run
    after = datetime.now(timezone.utc)

    # The due war: finished, higher damage wins, loser (only) gets a 12h shield.
    war = await war_row(due_id)
    assert war.status == "finished"
    assert war.winner_id == -1002
    loser = await castle_row(-1001)
    winner = await castle_row(-1002)
    assert not loser.in_war and not winner.in_war
    assert before + timedelta(hours=12) <= aware(loser.shield_until) <= after + timedelta(hours=12)
    assert winner.shield_until is None

    # A war that has not reached its deadline is left strictly alone.
    still_running = await war_row(running_id)
    assert still_running.status == "active" and still_running.winner_id is None
    for group_id in (-1003, -1004):
        castle = await castle_row(group_id)
        assert castle.in_war and castle.shield_until is None

    # The loop repeats forever: later cycles must not touch a settled war.
    shield = aware(loser.shield_until)
    await worker.run_once()
    again = await castle_row(-1001)
    assert aware(again.shield_until) == shield
    assert (await war_row(due_id)).winner_id == -1002
    assert (await war_row(running_id)).status == "active"


async def test_war_expiration_failure_rolls_back_shield_and_flags(
    war_session, monkeypatch
):
    s = war_session
    start = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    war = await WarService.declare_war(s, -1001, -1002, now=start)
    war.attacker_damage = 100
    await s.commit()
    war_id = war.id
    original_flush = s.flush

    async def fail_flush(*args, **kwargs):
        await original_flush(*args, **kwargs)
        raise RuntimeError("settlement failure")

    monkeypatch.setattr(s, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="settlement failure"):
        await WarService.finish_war(s, war_id, now=start + timedelta(hours=5))
    await s.rollback()
    monkeypatch.setattr(s, "flush", original_flush)
    saved = await s.get(GroupWar, war_id)
    assert saved.status == "active" and saved.winner_id is None
    for group_id in (-1001, -1002):
        castle = await s.get(GroupCastle, group_id)
        assert castle.in_war and castle.shield_until is None
    assert await WarService.finish_war(s, war_id, now=start + timedelta(hours=5))


async def test_war_expiration_ignores_missing_war(war_session):
    assert not await WarService.finish_war(war_session, 999999)


# ---------------------------------------------------------------------------
# C2-5: loot
# ---------------------------------------------------------------------------

ATTACKER, DEFENDER = -1001, -1002
START = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
END = START + timedelta(hours=5)


async def _fresh_castle(s, group_id):
    """Re-read a castle from the database (not from the identity map)."""
    return await s.scalar(
        select(GroupCastle)
        .where(GroupCastle.group_id == group_id)
        .execution_options(populate_existing=True)
    )


async def _set_treasury(s, group_id, total):
    """Give a castle a treasury (and the level that goes with it)."""
    castle = await s.get(GroupCastle, group_id)
    castle.total_donated = total
    castle.level = level_for_total(total)
    await s.flush()


async def _declare(s, *, attacker_damage, defender_damage, now=START):
    war = await WarService.declare_war(s, ATTACKER, DEFENDER, now=now)
    war.attacker_damage, war.defender_damage = attacker_damage, defender_damage
    await s.commit()
    return war


@pytest.mark.parametrize("attacker_wins", [True, False], ids=["attacker-wins", "defender-wins"])
@pytest.mark.parametrize(
    "loser_total, winner_total, loot",
    [
        (10_000, 7_000, 1_500),  # exactly 15%
        (10_001, 0, 1_500),  # rounded DOWN (1500.15)
        (999, 123, 149),  # rounded DOWN (149.85)
        (1_200_000, 20_000, 180_000),  # level 9: 15% fits above the 880_000 floor
    ],
)
async def test_war_loot_transfers_15_percent(
    war_session, attacker_wins, loser_total, winner_total, loot
):
    """The loser's treasury drops by 15% and the winner's grows by the same amount.

    (Cases where the level cap does not bind; the capped ones are in
    ``test_war_loot_capped_to_prevent_level_drop``.)
    """
    s = war_session
    winner_id, loser_id = (ATTACKER, DEFENDER) if attacker_wins else (DEFENDER, ATTACKER)
    await _set_treasury(s, winner_id, winner_total)
    await _set_treasury(s, loser_id, loser_total)
    war = await _declare(
        s,
        attacker_damage=300 if attacker_wins else 100,
        defender_damage=100 if attacker_wins else 300,
    )
    assert loot == int(loser_total * 0.15)  # the specified formula, spelled out

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    loser = await _fresh_castle(s, loser_id)
    winner = await _fresh_castle(s, winner_id)
    assert loser.total_donated == loser_total - loot
    assert winner.total_donated == winner_total + loot
    # Apples only move: nothing is created or destroyed.
    assert loser.total_donated + winner.total_donated == loser_total + winner_total
    assert war.looted_apples == loot
    assert (outcome.winner_id, outcome.loser_id, outcome.loot) == (winner_id, loser_id, loot)

    # Settling again (next maintenance cycle) never loots a second time.
    assert await WarService.finish_war(s, war.id, now=END + timedelta(minutes=1)) is None
    assert await WarService.finish_expired_wars(s, now=END + timedelta(minutes=1)) == 0
    assert (await _fresh_castle(s, loser_id)).total_donated == loser_total - loot
    assert (await _fresh_castle(s, winner_id)).total_donated == winner_total + loot
    assert war.looted_apples == loot


def test_war_loot_formula_equals_int_of_total_times_015():
    """Integer maths, same numbers as ``int(total_donated * 0.15)`` - no float drift.

    Level 1 has a floor of 0, so the level cap can never bind there: what is
    left is the plain 15% rule.
    """
    probes = list(range(0, 60_000))
    probes += list(THRESHOLDS) + [t - 1 for t in THRESHOLDS[1:]]
    probes += [2**31 - 1, 2**31 - 2, 1_000_000_000]
    probes += [20 * k for k in range(10**6 - 100, 10**6 + 100)]  # 15% is a whole number
    for total in probes:
        assert war_loot(total, 1) == int(total * 0.15), total
    assert [war_loot(n, 1) for n in (0, 6, 7, 19, 20, 100, 10_000)] == [0, 0, 1, 2, 3, 15, 1_500]
    assert war_loot(-5, 1) == 0  # never negative


async def test_war_loot_is_taken_under_row_locks_from_current_values(war_session):
    """Loot - and its level cap - are computed under ``SELECT ... FOR UPDATE``.

    SQLite drops FOR UPDATE when it renders SQL, so the statements are
    inspected before they are compiled. A donation that committed after this
    session last read the castle must be seen: the new total AND the new level
    (the in-memory copy is stale).
    """
    from sqlalchemy import event, update
    from sqlalchemy.sql.dml import Update

    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)  # the loser: level 2 ...
    await _set_treasury(s, DEFENDER, 0)
    war = await _declare(s, attacker_damage=1, defender_damage=9)
    stale = await s.get(GroupCastle, ATTACKER)
    assert (stale.total_donated, stale.level) == (10_000, 2)

    # A donation of 10_100 commits right before the war ends and lifts the
    # loser into level 3 (written behind this session's back, like another
    # connection's committed donation: CastleService.donate sets both columns).
    await s.execute(
        update(GroupCastle)
        .where(GroupCastle.group_id == ATTACKER)
        .values(total_donated=20_100, level=3)
        .execution_options(synchronize_session=False)
    )
    assert (stale.total_donated, stale.level) == (10_000, 2)  # stale in memory

    seen = []

    def record(conn, clauseelement, multiparams, params, execution_options):
        if isinstance(clauseelement, Update):
            seen.append(("update", clauseelement.table.name, False))
        elif hasattr(clauseelement, "_for_update_arg"):
            tables = tuple(sorted(f.name for f in clauseelement.get_final_froms()))
            seen.append(("select", tables, clauseelement._for_update_arg is not None))

    event.listen(s.bind.sync_engine, "before_execute", record)
    try:
        outcome = await WarService.finish_war(s, war.id, now=END)
    finally:
        event.remove(s.bind.sync_engine, "before_execute", record)
    await s.commit()

    # 15% of the CURRENT 20_100 is 3_015, but only 100 apples are above the
    # floor of the CURRENT level 3. (The stale copy would take 1_500; the stale
    # level 2 with the fresh total would take 3_015 and demote the loser.)
    assert outcome.loot == 100
    loser = await _fresh_castle(s, ATTACKER)
    assert (loser.total_donated, loser.level) == (20_000, 3)
    assert (await _fresh_castle(s, DEFENDER)).total_donated == 100
    # Both castles are locked first (group-id order), then the war row ...
    locking = [tables for kind, tables, locked in seen if kind == "select" and locked]
    assert locking == [("group_castles",), ("group_wars",)]
    # ... and nothing is written before every lock is held.
    first_write = next(i for i, (kind, *_rest) in enumerate(seen) if kind == "update")
    last_lock = max(i for i, (kind, _t, locked) in enumerate(seen) if kind == "select" and locked)
    assert last_lock < first_write
    assert {t for kind, t, _l in seen if kind == "update"} == {"group_castles", "group_wars"}


async def test_war_loot_is_all_or_nothing(war_session, monkeypatch):
    """A failure anywhere in the settlement undoes the loot with everything else."""
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    await _set_treasury(s, DEFENDER, 4_000)
    war = await _declare(s, attacker_damage=1, defender_damage=2)
    war_id = war.id
    original_flush = s.flush

    async def fail_flush(*args, **kwargs):
        await original_flush(*args, **kwargs)  # the writes DID reach the transaction
        raise RuntimeError("settlement failure")

    monkeypatch.setattr(s, "flush", fail_flush)
    with pytest.raises(RuntimeError, match="settlement failure"):
        await WarService.finish_war(s, war_id, now=END)
    await s.rollback()
    monkeypatch.setattr(s, "flush", original_flush)

    saved = await s.scalar(
        select(GroupWar).where(GroupWar.id == war_id).execution_options(populate_existing=True)
    )
    assert saved.status == "active" and saved.winner_id is None and saved.looted_apples == 0
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 10_000
    assert (await _fresh_castle(s, DEFENDER)).total_donated == 4_000
    for group_id in (ATTACKER, DEFENDER):
        castle = await _fresh_castle(s, group_id)
        assert castle.in_war and castle.shield_until is None

    # The retry settles it - and loots exactly once.
    outcome = await WarService.finish_war(s, war_id, now=END)
    await s.commit()
    assert outcome.loot == 1_500
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 8_500
    assert (await _fresh_castle(s, DEFENDER)).total_donated == 5_500


async def test_war_tie_moves_no_loot(war_session):
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    await _set_treasury(s, DEFENDER, 20_000)
    war = await _declare(s, attacker_damage=250, defender_damage=250)

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    assert outcome.is_draw and outcome.loot == 0
    assert outcome.winner_id is None and outcome.loser_id is None
    assert war.looted_apples == 0 and war.winner_id is None
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 10_000
    assert (await _fresh_castle(s, DEFENDER)).total_donated == 20_000
    for group_id in (ATTACKER, DEFENDER):
        assert (await _fresh_castle(s, group_id)).shield_until is None


async def test_war_without_loot_leaves_castles_alone(war_session):
    """A treasury too small to be plundered (15% of 6 rounds down to 0)."""
    s = war_session
    await _set_treasury(s, ATTACKER, 6)
    await _set_treasury(s, DEFENDER, 50)
    # Levels that do not match the totals (the fixture made them up): with
    # nothing moved there is nothing to re-derive, so they must stay as they are.
    (await s.get(GroupCastle, ATTACKER)).level = 3
    (await s.get(GroupCastle, DEFENDER)).level = 4
    war = await _declare(s, attacker_damage=1, defender_damage=2)

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    assert outcome.loot == 0 and war.looted_apples == 0 and war.status == "finished"
    loser = await _fresh_castle(s, ATTACKER)
    winner = await _fresh_castle(s, DEFENDER)
    assert (loser.total_donated, winner.total_donated) == (6, 50)
    assert (loser.level, winner.level) == (3, 4)
    assert loser.shield_until is not None and winner.shield_until is None  # shield still applies


# ---------------------------------------------------------------------------
# C2-5.5: the loot never demotes the loser ("castle levels never downgrade")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loser_total, winner_total, loot",
    [
        # (loser treasury, winner treasury, loot actually taken)
        (21_000, 19_000, 1_000),  # level 3: 15% = 3_150 would leave 17_850 (level 2); only the 1_000 above the 20_000 floor may go
        (23_528, 0, 3_528),  # one apple short of where 15% fits: 3_529 would leave 19_999
        (1_000_000, 1_200_000, 120_000),  # level 9: 15% = 150_000, but only 120_000 are above the 880_000 floor
        (110_000, 5_000, 0),  # exactly on the floor of level 5: nothing may be taken
        (1_300_000, 880_000, 0),  # a maxed castle (donations stop at exactly 1_300_000): nothing above its floor
    ],
)
async def test_war_loot_capped_to_prevent_level_drop(
    war_session, loser_total, winner_total, loot
):
    """The 15% loot is capped at what the loser holds above its level's floor,
    so a lost war can never demote it."""
    s = war_session
    loser_level = level_for_total(loser_total)
    uncapped = int(loser_total * 0.15)
    # The point of the test: the plain 15% WOULD have dropped the loser a level.
    assert level_for_total(loser_total - uncapped) < loser_level
    assert loot == min(uncapped, loser_total - THRESHOLDS[loser_level - 1]) < uncapped

    await _set_treasury(s, ATTACKER, loser_total)
    await _set_treasury(s, DEFENDER, winner_total)
    war = await _declare(s, attacker_damage=1, defender_damage=2)  # the defender wins

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    loser = await _fresh_castle(s, ATTACKER)
    winner = await _fresh_castle(s, DEFENDER)
    assert outcome.loot == loot == war.looted_apples
    assert loser.total_donated == loser_total - loot
    assert winner.total_donated == winner_total + loot
    # The loser keeps its level, and still holds at least that level's floor.
    assert loser.level == loser_level
    assert loser.total_donated >= THRESHOLDS[loser_level - 1]
    assert level_for_total(loser.total_donated) == loser_level
    # The cap changes the amount only: the war is lost all the same.
    assert war.status == "finished" and war.winner_id == DEFENDER
    assert loser.shield_until is not None and winner.shield_until is None
    assert not loser.in_war and not winner.in_war


@pytest.mark.parametrize("attacker_wins", [True, False], ids=["attacker-wins", "defender-wins"])
@pytest.mark.parametrize(
    "loser_total, winner_total, loot",
    [
        (10_000, 7_000, 1_500),  # level 2: 15% fits comfortably above the 5_000 floor
        (23_529, 0, 3_529),  # the exact point where 15% just fits: the loser lands ON its 20_000 floor
        (1_200_000, 20_000, 180_000),  # level 9
        (1_600_000, 0, 240_000),  # level 10 above the donation cap (it won loot earlier): floor 1_300_000
        (999, 123, 149),  # level 1 has no floor to protect
    ],
)
async def test_war_loot_standard_case(
    war_session, attacker_wins, loser_total, winner_total, loot
):
    """When 15% does not cost the loser its level, the full 15% (rounded down) is taken."""
    s = war_session
    winner_id, loser_id = (ATTACKER, DEFENDER) if attacker_wins else (DEFENDER, ATTACKER)
    loser_level = level_for_total(loser_total)
    assert loot == int(loser_total * 0.15)  # the plain 15% ...
    assert loser_total - loot >= THRESHOLDS[loser_level - 1]  # ... fits above the floor

    await _set_treasury(s, winner_id, winner_total)
    await _set_treasury(s, loser_id, loser_total)
    war = await _declare(
        s,
        attacker_damage=300 if attacker_wins else 100,
        defender_damage=100 if attacker_wins else 300,
    )

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    loser = await _fresh_castle(s, loser_id)
    winner = await _fresh_castle(s, winner_id)
    assert outcome.loot == loot == war.looted_apples
    assert loser.total_donated == loser_total - loot
    assert winner.total_donated == winner_total + loot
    assert loser.level == loser_level
    assert winner.level == level_for_total(winner.total_donated)


def test_war_loot_cap_matches_the_spec_formula_for_every_level():
    """actual = min(total * 15 // 100, total - THRESHOLDS[level - 1]), at every
    level, densely just above its floor - which is where the cap bites."""
    for level, floor in enumerate(THRESHOLDS, start=1):
        ceiling = THRESHOLDS[level] if level < len(THRESHOLDS) else floor + 600_000
        totals = set(range(floor, min(ceiling, floor + 6_000)))
        totals |= set(range(floor, ceiling, max(1, (ceiling - floor) // 3_000)))
        totals.add(ceiling - 1)
        for total in totals:
            intended = total * 15 // 100
            loot = war_loot(total, level)
            assert loot == min(intended, total - floor), (level, total)
            assert 0 <= loot <= intended, (level, total)
            assert level_for_total(total - loot) == level, (level, total)  # level unchanged
            if intended <= total - floor:  # the cap does not bind: the plain 15%
                assert loot == int(total * 0.15), (level, total)

    # The boundary, spelled out: at level 3, 15% just fits from 23_529 apples on.
    assert [war_loot(t, 3) for t in (20_000, 20_001, 23_528, 23_529, 23_530)] == [
        0, 1, 3_528, 3_529, 3_529,
    ]
    assert war_loot(1_300_000, 10) == 0 and war_loot(1_300_001, 10) == 1  # a maxed castle
    assert war_loot(17_000, 3) == 0  # a level its total cannot support: never negative
    assert war_loot(100, 0) == war_loot(100, 1) == 15  # out-of-range levels are clamped
    assert war_loot(2_000_000, 99) == war_loot(2_000_000, 10) == 300_000
    assert war_loot(-5, 1) == 0


def test_level_floor_is_the_inverse_of_level_for_total():
    for level in range(1, 11):
        floor = level_floor(level)
        assert floor == THRESHOLDS[level - 1]
        assert level_for_total(floor) == level  # the floor itself holds the level ...
        if floor:
            assert level_for_total(floor - 1) == level - 1  # ... one apple less does not
    assert level_floor(0) == level_floor(-3) == level_floor(1) == 0  # clamped
    assert level_floor(11) == level_floor(99) == level_floor(10) == 1_300_000


async def test_war_loot_is_zero_when_the_total_cannot_support_the_level(war_session):
    """Loot is never negative: a loser whose stored level is above what its
    total supports (data the fixture made up) loses nothing."""
    s = war_session
    await _set_treasury(s, ATTACKER, 17_000)
    (await s.get(GroupCastle, ATTACKER)).level = 3  # floor 20_000: above its own total
    await _set_treasury(s, DEFENDER, 4_000)
    war = await _declare(s, attacker_damage=1, defender_damage=2)

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    assert outcome.loot == 0 and war.looted_apples == 0 and war.status == "finished"
    loser = await _fresh_castle(s, ATTACKER)
    winner = await _fresh_castle(s, DEFENDER)
    assert (loser.total_donated, winner.total_donated) == (17_000, 4_000)
    assert loser.level == 3 and loser.shield_until is not None


async def test_war_winner_may_level_up_through_loot(war_session):
    s = war_session
    await _set_treasury(s, ATTACKER, 100_000)  # loser: level 4 (floor 50_000), 15% fits
    await _set_treasury(s, DEFENDER, 19_000)  # winner: level 2
    war = await _declare(s, attacker_damage=1, defender_damage=2)

    outcome = await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    assert outcome.loot == 15_000
    loser = await _fresh_castle(s, ATTACKER)
    winner = await _fresh_castle(s, DEFENDER)
    assert (loser.total_donated, loser.level) == (85_000, 4)  # the loser keeps level 4
    assert (winner.total_donated, winner.level) == (34_000, 3)  # 19_000 -> 34_000: level 2 -> 3


async def test_war_never_lowers_the_level_of_a_winner_either(war_session):
    """A winner whose stored level is above what its total supports (data the
    fixture made up) keeps that level: a war only ever raises a level."""
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)  # loser: level 2, 15% = 1_500 fits
    await _set_treasury(s, DEFENDER, 10_000)
    (await s.get(GroupCastle, DEFENDER)).level = 3  # its total supports level 2 only
    war = await _declare(s, attacker_damage=1, defender_damage=2)

    await WarService.finish_war(s, war.id, now=END)
    await s.commit()

    winner = await _fresh_castle(s, DEFENDER)
    assert winner.total_donated == 11_500
    assert winner.level == 3  # not re-derived down to 2


# ---------------------------------------------------------------------------
# C2-5: the final message of a war
# ---------------------------------------------------------------------------

ZWNJ = "\u200c"


def victory_text(loot):
    """The winner's caption exactly as specified (two lines)."""
    return (
        "🏆 پیروزی در جدال سیبی!\n"
        f"دشمن را در هم کوبیدیم. ۱۵\u066a از خزانه{ZWNJ}ی آن{ZWNJ}ها غنیمت گرفتیم: "
        f"{loot} سیب به قلعه اضافه شد."
    )


def defeat_text(loot):
    """The loser's caption exactly as specified (two lines)."""
    return (
        "💔 شکست در جدال سیبی...\n"
        f"دشمن {loot} سیب از خزانه{ZWNJ}ی ما غارت کرد. "
        "اما ناامید نشوید، ۱۲ ساعت سپر صلح فعال شد."
    )


def _fake_bot():
    reply = SimpleNamespace(message_id=1)
    return SimpleNamespace(
        send_photo=AsyncMock(return_value=reply),
        send_message=AsyncMock(return_value=reply),
    )


def _prepare_cycle(monkeypatch, s, bot):
    """Everything in ``run_once`` except war finalization + announcement is stubbed."""
    from contextlib import asynccontextmanager
    from sib.services.apple_duel_presentation import AppleDuelPresentation
    from sib.services.maintenance import service as maintenance
    from sib.services.match_result import MatchResultService
    from sib.services.player_xp import PlayerXPService
    from sib.services.session import SessionService
    from sib.services.timeout import TimeoutService
    from sib.services.trading import TradeService

    @asynccontextmanager
    async def session_ctx():  # what get_session() does: commit, or roll back and raise
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise

    monkeypatch.setattr(maintenance, "get_session", session_ctx)
    worker = maintenance.MaintenanceService
    for method in [
        "cleanup_zombie_parties",
        "process_expired_queue_entries",
        "run_marriage_love_cycle",
        "_resume_pending_bot_turns",
    ]:
        monkeypatch.setattr(worker, method, AsyncMock())
    monkeypatch.setattr(worker, "_invasion_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(worker, "_bot", bot)
    monkeypatch.setattr(SessionService, "reconcile_stale_state", AsyncMock())
    monkeypatch.setattr(TradeService, "expire_overdue", AsyncMock())
    monkeypatch.setattr(TimeoutService, "process_acceptance_timeouts", AsyncMock())
    monkeypatch.setattr(TimeoutService, "process_turn_timeouts", AsyncMock(return_value=[]))
    monkeypatch.setattr(MatchResultService, "retry_pending_xp_awards", AsyncMock(return_value=0))
    monkeypatch.setattr(AppleDuelPresentation, "recover_and_publish", AsyncMock())
    monkeypatch.setattr(PlayerXPService, "deliver_pending_notifications", AsyncMock())
    return worker


async def _due_war(s, *, attacker_damage, defender_damage, hours_ago=6):
    war = await WarService.declare_war(
        s, ATTACKER, DEFENDER, now=datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    )
    war.attacker_damage, war.defender_damage = attacker_damage, defender_damage
    await s.commit()
    return war


async def test_war_finish_sends_messages_to_both_groups(war_session, monkeypatch):
    """When a war finishes, BOTH groups get a victory-banner message."""
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)  # the loser: 15% = 1_500 are plundered
    await _set_treasury(s, DEFENDER, 4_000)
    war = await _due_war(s, attacker_damage=100, defender_damage=250)  # the DEFENDER wins
    bot = _fake_bot()
    worker = _prepare_cycle(monkeypatch, s, bot)

    await worker.run_once()

    # One photo per group - and nothing else.
    assert bot.send_photo.await_count == 2
    bot.send_message.assert_not_awaited()
    sent = {call.args[0]: call for call in bot.send_photo.await_args_list}
    assert set(sent) == {ATTACKER, DEFENDER}
    assert sent[DEFENDER].kwargs["caption"] == victory_text("۱۵۰۰")  # winner
    assert sent[ATTACKER].kwargs["caption"] == defeat_text("۱۵۰۰")  # loser
    for call in sent.values():  # no defeat banner exists: both use the victory image
        assert Path(call.args[1].path).name == "war_bg_victory.jpg"
        assert Path(call.args[1].path).is_file()

    # What the messages say is what was committed.
    await s.refresh(war)
    assert war.status == "finished" and war.winner_id == DEFENDER and war.looted_apples == 1_500
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 8_500
    assert (await _fresh_castle(s, DEFENDER)).total_donated == 5_500

    # The next cycles must not announce the same war again.
    await worker.run_once()
    await worker.run_once()
    assert bot.send_photo.await_count == 2


async def test_war_messages_state_the_capped_loot(war_session, monkeypatch):
    """The announced amount is what was really taken after the level cap."""
    s = war_session
    await _set_treasury(s, ATTACKER, 21_000)  # 15% = 3_150, but only 1_000 are above its floor
    await _set_treasury(s, DEFENDER, 4_000)
    await _due_war(s, attacker_damage=100, defender_damage=250)  # the DEFENDER wins
    bot = _fake_bot()
    worker = _prepare_cycle(monkeypatch, s, bot)

    await worker.run_once()

    sent = {call.args[0]: call.kwargs["caption"] for call in bot.send_photo.await_args_list}
    assert sent == {DEFENDER: victory_text("۱۰۰۰"), ATTACKER: defeat_text("۱۰۰۰")}
    loser = await _fresh_castle(s, ATTACKER)
    assert (loser.total_donated, loser.level) == (20_000, 3)


async def test_war_result_is_sent_only_after_the_settlement_committed(
    war_session, monkeypatch
):
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    await _due_war(s, attacker_damage=1, defender_damage=2)
    bot = _fake_bot()
    worker = _prepare_cycle(monkeypatch, s, bot)
    events = []
    original_commit = s.commit

    async def logging_commit(*args, **kwargs):
        events.append("commit")
        return await original_commit(*args, **kwargs)

    async def logging_send(*args, **kwargs):
        events.append("send")

    monkeypatch.setattr(s, "commit", logging_commit)
    bot.send_photo.side_effect = logging_send

    await worker.run_once()

    assert events.count("send") == 2
    assert events.index("commit") < events.index("send")  # settled and saved first


async def test_war_result_is_not_announced_when_the_settlement_rolls_back(
    war_session, monkeypatch
):
    """Nothing is announced for a war whose settlement did not commit; the next
    cycle settles it and announces it exactly once."""
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    first = await _due_war(s, attacker_damage=1, defender_damage=2)
    s.add_all(
        [
            Group(id=-1003, title="قبیله گرگ", is_active=True),
            Group(id=-1004, title="قبیله ببر", is_active=True),
        ]
    )
    await s.flush()
    await CastleService.get_or_create_castle(s, -1003)
    await CastleService.get_or_create_castle(s, -1004)
    second = await WarService.declare_war(
        s, -1003, -1004, now=datetime.now(timezone.utc) - timedelta(hours=6)
    )
    second.attacker_damage = 5  # it has a winner too (a tie is announced as text)
    await s.commit()
    bot = _fake_bot()
    worker = _prepare_cycle(monkeypatch, s, bot)

    real_finish = WarService.finish_war
    first_id, second_id = first.id, second.id

    async def finish_then_crash(session, war_id, **kwargs):
        outcome = await real_finish(session, war_id, **kwargs)
        if war_id == second_id:
            raise RuntimeError("second war cannot be settled")
        return outcome

    monkeypatch.setattr(WarService, "finish_war", staticmethod(finish_then_crash))
    await worker.run_once()  # must not raise

    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    for war_id in (first_id, second_id):  # the whole batch was rolled back
        saved = await s.scalar(
            select(GroupWar).where(GroupWar.id == war_id).execution_options(populate_existing=True)
        )
        assert saved.status == "active" and saved.looted_apples == 0
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 10_000

    monkeypatch.setattr(WarService, "finish_war", staticmethod(real_finish))
    await worker.run_once()
    assert bot.send_photo.await_count == 4  # two wars x two groups, once each


async def test_failed_delivery_never_undoes_the_settlement(war_session, monkeypatch):
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    war = await _due_war(s, attacker_damage=1, defender_damage=2)
    bot = _fake_bot()
    bot.send_photo.side_effect = RuntimeError("telegram is down")
    bot.send_message.side_effect = RuntimeError("telegram is down")
    worker = _prepare_cycle(monkeypatch, s, bot)

    await worker.run_once()  # must not raise

    assert bot.send_photo.await_count == 2  # it tried both groups
    await s.refresh(war)
    assert war.status == "finished" and war.looted_apples == 1_500
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 8_500
    assert (await _fresh_castle(s, DEFENDER)).total_donated == 1_500


async def test_war_is_settled_but_not_announced_when_there_is_no_bot(war_session, monkeypatch):
    """Maintenance started without a bot (tests, tooling) still settles wars."""
    from sib.services import war_presentation

    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    war = await _due_war(s, attacker_damage=1, defender_damage=2)
    worker = _prepare_cycle(monkeypatch, s, None)
    announce = AsyncMock()
    monkeypatch.setattr(war_presentation, "announce_war_result", announce)

    await worker.run_once()

    announce.assert_not_awaited()
    await s.refresh(war)
    assert war.status == "finished" and war.looted_apples == 1_500


async def test_one_failing_war_announcement_does_not_skip_the_next_war(monkeypatch):
    from sib.services import war_presentation
    from sib.services.maintenance.service import MaintenanceService

    outcomes = [
        WarOutcome(1, -1, -2, -2, -1, 10),
        WarOutcome(2, -3, -4, -3, -4, 20),
    ]
    announced = []

    async def flaky(bot, outcome):
        if outcome.war_id == 1:
            raise RuntimeError("boom")
        announced.append(outcome.war_id)

    monkeypatch.setattr(war_presentation, "announce_war_result", flaky)
    await MaintenanceService._announce_war_results(object(), outcomes)  # must not raise
    assert announced == [2]


async def test_war_result_falls_back_to_text_when_the_photo_fails():
    from sib.services.war_presentation import announce_war_result

    bot = _fake_bot()
    bot.send_photo.side_effect = RuntimeError("photo upload rejected")

    reached = await announce_war_result(bot, WarOutcome(7, ATTACKER, DEFENDER, DEFENDER, ATTACKER, 1_500))

    assert reached == 2
    texts = {call.args[0]: call.args[1] for call in bot.send_message.await_args_list}
    assert texts == {DEFENDER: victory_text("۱۵۰۰"), ATTACKER: defeat_text("۱۵۰۰")}


async def test_one_unreachable_group_does_not_cost_the_other_its_message():
    from sib.services.war_presentation import announce_war_result

    bot = _fake_bot()

    async def photo(chat_id, *args, **kwargs):
        if chat_id == DEFENDER:  # e.g. the bot was removed from this group
            raise RuntimeError("Forbidden: bot was kicked")
        return SimpleNamespace(message_id=1)

    bot.send_photo.side_effect = photo
    bot.send_message.side_effect = RuntimeError("Forbidden: bot was kicked")

    reached = await announce_war_result(bot, WarOutcome(7, ATTACKER, DEFENDER, DEFENDER, ATTACKER, 1_500))

    assert reached == 1  # the loser still heard about it
    delivered = [call for call in bot.send_photo.await_args_list if call.args[0] == ATTACKER]
    assert len(delivered) == 1 and delivered[0].kwargs["caption"] == defeat_text("۱۵۰۰")


async def test_a_crash_while_sending_to_one_group_is_contained(monkeypatch):
    """Even an unexpected exception for one group cannot skip the other group."""
    from sib.services import war_presentation

    chats = []

    async def flaky_send(bot, chat_id, text, **kwargs):
        chats.append(chat_id)
        if chat_id == DEFENDER:
            raise RuntimeError("unexpected crash")
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(war_presentation, "safe_send_screen", flaky_send)

    reached = await war_presentation.announce_war_result(
        object(), WarOutcome(7, ATTACKER, DEFENDER, DEFENDER, ATTACKER, 1_500)
    )

    assert chats == [DEFENDER, ATTACKER]  # the winner crashed, the loser was still served
    assert reached == 1


async def test_war_result_without_the_victory_image_is_sent_as_text(monkeypatch, tmp_path):
    from sib.services import war_presentation

    monkeypatch.setattr(war_presentation, "VICTORY_IMAGE", tmp_path / "missing.jpg")
    bot = _fake_bot()

    reached = await war_presentation.announce_war_result(
        bot, WarOutcome(7, ATTACKER, DEFENDER, ATTACKER, DEFENDER, 0)
    )

    assert reached == 2
    bot.send_photo.assert_not_awaited()
    texts = {call.args[0]: call.args[1] for call in bot.send_message.await_args_list}
    assert texts == {ATTACKER: victory_text("۰"), DEFENDER: defeat_text("۰")}  # 0 loot is still stated


async def test_war_draw_sends_a_plain_notice_to_both_groups(war_session, monkeypatch):
    """The brief names a winner and a loser only; a tie gets a short text-only
    notice (no loot, no shield, no victory banner)."""
    s = war_session
    await _set_treasury(s, ATTACKER, 10_000)
    war = await _due_war(s, attacker_damage=200, defender_damage=200)
    bot = _fake_bot()
    worker = _prepare_cycle(monkeypatch, s, bot)

    await worker.run_once()

    bot.send_photo.assert_not_awaited()
    assert bot.send_message.await_count == 2
    assert {call.args[0] for call in bot.send_message.await_args_list} == {ATTACKER, DEFENDER}
    for call in bot.send_message.await_args_list:
        text = call.args[1]
        assert "مساوی" in text and not any(d in text for d in "0123456789")
    await s.refresh(war)
    assert war.status == "finished" and war.winner_id is None and war.looted_apples == 0
    assert (await _fresh_castle(s, ATTACKER)).total_donated == 10_000
