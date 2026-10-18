"""Apple Clash (Group War) tests covering energy, cooldown, items, declaration and UI styling."""

from datetime import datetime, timedelta, timezone
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
from sib.services.castle_service import CastleService
from sib.services.items import ITEM_CATALOG, ItemService
from sib.services.player_cards import PlayerCardService
from sib.services.war_service import WarError, WarService


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
