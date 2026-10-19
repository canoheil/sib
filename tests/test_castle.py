"""Castle domain tests on an isolated in-memory database, never production."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sib.database.models import (
    User,
    Group,
    GroupCastle,
    CastleDonation,
    CastleIncomeClaim,
    AppleWallet,
    AppleTransaction,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.castle_service import (
    CastleService,
    CastleError,
    THRESHOLDS,
    level_for_total,
    apply_castle_buff,
)
from sib.services.castle_art import castle_image


@pytest_asyncio.fixture
async def castle_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # Legacy ledger PK is BIGINT; SQLite test DDL needs INTEGER autoincrement.
        from sib.tests.conftest import _create_all_compatible

        await conn.run_sync(_create_all_compatible)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                User(id=1, first_name="سازنده"),
                Group(id=-1, title="قلعه"),
                Group(id=-2, title="دوم"),
            ]
        )
        await session.flush()
        await AppleWalletService.earn(session, 1, 10_000, reason="test")
        await session.commit()
        yield session
    await engine.dispose()


async def test_castle_donation_cap(castle_session):
    s = castle_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    await CastleService.donate(s, 1, -1, 100, now=now)
    await CastleService.donate(s, 1, -2, 400, now=now)
    with pytest.raises(CastleError):
        await CastleService.donate(s, 1, -1, 1, now=now)
    assert await AppleWalletService.get_balance(s, 1) == 9500
    assert await CastleService.donate(s, 1, -1, 500, now=now + timedelta(days=1)) == 500


@pytest.mark.parametrize("level,threshold", list(enumerate(THRESHOLDS, 1)))
def test_castle_level_up_calculation(level, threshold):
    assert level_for_total(threshold) == level
    if threshold:
        assert level_for_total(threshold - 1) == level - 1


async def test_donation_upgrades_and_replay(castle_session):
    s = castle_session
    castle = await CastleService.get_or_create_castle(s, -1)
    castle.total_donated = 4900
    await s.flush()
    assert await CastleService.donate(s, 1, -1, 100, request_key="castle:test:1") == 100
    assert castle.level == 2 and castle.total_donated == 5000
    assert await CastleService.donate(s, 1, -1, 100, request_key="castle:test:1") == 100
    assert castle.total_donated == 5000
    assert await s.scalar(select(func.count()).select_from(CastleDonation)) == 1
    with pytest.raises(CastleError):
        await CastleService.donate(s, 1, -1, 200, request_key="castle:test:1")


async def test_castle_daily_income_claim(castle_session):
    s = castle_session
    now = datetime(2026, 10, 18, 23, 59, tzinfo=timezone.utc)
    assert await CastleService.claim_daily_income(s, 1, -1, now=now) == 100
    with pytest.raises(CastleError):
        await CastleService.claim_daily_income(s, 1, -2, now=now)
    assert (
        await CastleService.claim_daily_income(s, 1, -1, now=now + timedelta(minutes=1))
        == 100
    )
    assert await AppleWalletService.get_balance(s, 1) == 10200


async def test_invasion_buff_applied_correctly(castle_session):
    s = castle_session
    assert await CastleService.get_invasion_buff(s, -1) == 0
    castle = await CastleService.get_or_create_castle(s, -1)
    castle.level = 10
    await s.flush()
    buff = await CastleService.get_invasion_buff(s, -1)
    assert buff == 0.5
    assert apply_castle_buff(101, buff) == 151
    assert apply_castle_buff(101, buff) * 2 == 302
    assert apply_castle_buff(0, buff) == 0


def test_castle_image_fallback_if_missing(tmp_path):
    (tmp_path / "level_1.jpg").write_bytes(b"test")
    (tmp_path / "level_3.jpg").write_bytes(b"test")
    assert castle_image(5, tmp_path).name == "level_3.jpg"
    assert castle_image(2, tmp_path).name == "level_1.jpg"
    assert castle_image(3, tmp_path).name == "level_3.jpg"
    (tmp_path / "level_1.jpg").unlink()
    assert castle_image(2, tmp_path) is None


async def test_max_and_max_level(castle_session):
    s = castle_session
    assert await CastleService.donate(s, 1, -1, None) == 500
    castle = await CastleService.get_or_create_castle(s, -2)
    castle.level, castle.total_donated = 10, THRESHOLDS[-1]
    await s.flush()
    with pytest.raises(CastleError):
        await CastleService.donate(s, 1, -2, None)


async def test_claim_failure_rolls_back(castle_session, monkeypatch):
    s = castle_session
    monkeypatch.setattr(
        AppleWalletService, "earn", AsyncMock(side_effect=RuntimeError("credit failed"))
    )
    with pytest.raises(RuntimeError):
        await CastleService.claim_daily_income(s, 1, -1)
    await s.rollback()
    assert await s.scalar(select(func.count()).select_from(CastleIncomeClaim)) == 0
    assert await AppleWalletService.get_balance(s, 1) == 10_000


def test_all_castle_banners_are_deployed():
    from PIL import Image
    from sib.services.castle_art import ASSET_DIR

    assert len(list(ASSET_DIR.glob("*.jpg"))) == 10
    for level in range(1, 11):
        path = castle_image(level)
        assert path.name == f"level_{level}.jpg"
        with Image.open(path) as image:
            assert image.size == (1280, 720)
            assert image.format == "JPEG"


def test_castle_migration_upgrade_downgrade():
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    migration = importlib.import_module(
        "sib.database.migrations.versions.049_apple_castles"
    )
    assert migration.down_revision == "048_apple_friendships"
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert set(inspect(connection).get_table_names()) == {
                "group_castles",
                "castle_donations",
                "castle_income_claims",
            }
            migration.downgrade()
            assert inspect(connection).get_table_names() == []
    engine.dispose()


async def test_callback_donation_and_replay(castle_session, monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import datetime
    from types import SimpleNamespace
    from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
    from sib.handlers import castle as handler

    @asynccontextmanager
    async def session():
        try:
            yield castle_session
            await castle_session.commit()
        except Exception:
            await castle_session.rollback()
            raise

    answer = AsyncMock()
    monkeypatch.setattr(handler, "get_session", session)
    monkeypatch.setattr(handler, "show_castle", AsyncMock())
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))
    )
    actor = TgUser(id=1, first_name="سازنده", is_bot=False)
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=-1, type="supergroup"),
    )
    cb = CallbackQuery(
        id="donate-once",
        from_user=actor,
        chat_instance="test",
        message=message,
        data="castle:100:-1",
    ).as_(bot)
    await handler.castle_callback(cb)
    await handler.castle_callback(cb)
    assert await AppleWalletService.get_balance(castle_session, 1) == 9900
    assert (
        await castle_session.scalar(select(func.count()).select_from(CastleDonation))
        == 1
    )
    assert answer.await_count == 2


async def test_callback_rejects_cross_group_and_nonmember(castle_session, monkeypatch):
    from types import SimpleNamespace
    from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
    from sib.handlers import castle as handler

    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    get_session = AsyncMock()
    monkeypatch.setattr(handler, "get_session", get_session)
    actor = TgUser(id=1, first_name="سازنده", is_bot=False)
    message = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=-1, type="group")
    )
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left"))
    )
    cb = CallbackQuery(
        id="bad",
        from_user=actor,
        chat_instance="test",
        message=message,
        data="castle:500:-2",
    ).as_(bot)
    await handler.castle_callback(cb)
    bot.get_chat_member.assert_not_awaited()
    cb = cb.model_copy(update={"data": "castle:500:-1"}).as_(bot)
    await handler.castle_callback(cb)
    get_session.assert_not_called()
    assert answer.await_count == 2


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "100"])
async def test_invalid_donation_amount(castle_session, amount):
    with pytest.raises(CastleError):
        await CastleService.donate(castle_session, 1, -1, amount)
    assert await AppleWalletService.get_balance(castle_session, 1) == 10_000


async def test_partial_max_at_lifetime_target(castle_session):
    s = castle_session
    castle = await CastleService.get_or_create_castle(s, -1)
    castle.total_donated, castle.level = THRESHOLDS[-1] - 37, 9
    await s.flush()
    assert await CastleService.donate(s, 1, -1, None) == 37
    assert castle.level == 10 and castle.total_donated == THRESHOLDS[-1]
    assert await AppleWalletService.get_balance(s, 1) == 10063


async def test_insufficient_funds_no_partial_donation(castle_session):
    s = castle_session
    await AppleWalletService.spend(s, 1, 9950, reason="test")
    with pytest.raises(CastleError):
        await CastleService.donate(s, 1, -1, 100)
    assert await s.scalar(select(func.count()).select_from(CastleDonation)) == 0
    assert await AppleWalletService.get_balance(s, 1) == 50
    assert await CastleService.donate(s, 1, -1, None) == 50


async def test_donation_failure_rolls_back(castle_session, monkeypatch):
    s = castle_session
    original = AppleWalletService.debit_once

    async def fail_after_debit(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("failure after debit")

    monkeypatch.setattr(AppleWalletService, "debit_once", fail_after_debit)
    with pytest.raises(RuntimeError):
        await CastleService.donate(s, 1, -1, 100)
    await s.rollback()
    assert await AppleWalletService.get_balance(s, 1) == 10_000
    assert await s.scalar(select(func.count()).select_from(CastleDonation)) == 0


def test_castle_buttons_have_the_expected_colors():
    from sib.handlers.castle import keyboard

    main_keyboard = keyboard(-123)
    donation_keyboard = keyboard(-123, donate=True)
    buttons = [
        button
        for markup in (main_keyboard, donation_keyboard)
        for row in markup.inline_keyboard
        for button in row
    ]
    by_callback = {button.callback_data: button for button in buttons}

    expected = {
        "castle:donate:-123": ("🍏 اهدای سیب", "success"),
        "castle:income:-123": ("🎁 دریافت درآمد", "primary"),
        "castle:top:-123": ("🏆 برترین اهداکنندگان", "primary"),
        "castle:100:-123": ("۱۰۰ 🍏", "success"),
        "castle:500:-123": ("۵۰۰ 🍏", "success"),
        "castle:max:-123": ("حداکثر", "success"),
        "castle:view:-123": ("🔙 بازگشت", "danger"),
    }
    assert set(by_callback) == set(expected)
    assert all(button.style in {"success", "primary", "danger"} for button in buttons)
    for callback_data, (text, style) in expected.items():
        assert by_callback[callback_data].text == text
        assert by_callback[callback_data].style == style


def test_castle_caption_is_localized_and_uses_persian_digits():
    from sib.handlers.castle import format_castle_caption

    caption = format_castle_caption(level=2, total_donated=12_345)
    assert caption == (
        "🏰 قلعه سیبی\n\n"
        "📊 سطح قلعه: ۲ از ۱۰\n\n"
        "🍏 سیب اهدا شده: ۱۲۳۴۵ / ۲۰۰۰۰\n\n"
        "⚔️ قدرت حمله در یورش: +۱۰٪\n\n"
        "🎁 درآمد روزانه: ۲۰۰ سیب\n\n"
        "⚠️ سقف اهدا: روزانه ۵۰۰ سیب (برای هر بازیکن)\n\n"
        "ℹ️ اهدای سیب بازگشت‌پذیر نیست. درآمد روزانه فقط از یک قلعه دریافت می‌شود."
    )
    assert "سطح قلعه" in caption
    for unwanted in ("Invasion", "دمیج", "لول", "UTC"):
        assert unwanted not in caption
    assert not any(digit in caption for digit in "0123456789")


@pytest.mark.parametrize("level", range(1, 11))
async def test_castle_daily_income_is_level_times_100(castle_session, level):
    s = castle_session
    castle = await CastleService.get_or_create_castle(s, -1)
    castle.level = level
    await s.flush()
    assert await CastleService.claim_daily_income(s, 1, -1) == level * 100
    claim = await s.scalar(select(CastleIncomeClaim))
    assert claim.amount == level * 100
    assert await AppleWalletService.get_balance(s, 1) == 10_000 + level * 100


@pytest.mark.parametrize("level", range(1, 11))
async def test_castle_invasion_buff_is_5_percent_per_level(castle_session, level):
    s = castle_session
    castle = await CastleService.get_or_create_castle(s, -1)
    castle.level = level
    await s.flush()
    assert await CastleService.get_invasion_buff(s, -1) == pytest.approx(level * 0.05)
    group = await s.get(Group, -1)
    group.is_active = False
    await s.flush()
    assert await CastleService.get_invasion_buff(s, -1) == 0


async def _seed_reward_donors(s, now):
    s.add_all([User(id=i, first_name="اهداکننده") for i in range(2, 7)])
    castle = await CastleService.get_or_create_castle(s, -1)
    await CastleService.get_or_create_castle(s, -2)
    castle.total_donated = 4900
    # One reward per unique user, including exact 24h boundary; no other
    # castle's donors, older donations or future-dated rows.
    for i, (user, group, age) in enumerate(
        [
            (2, -1, timedelta(hours=1)),
            (2, -1, timedelta(hours=2)),
            (3, -1, timedelta(hours=24)),
            (4, -1, timedelta(hours=24, microseconds=1)),
            (5, -2, timedelta(hours=1)),
            (6, -1, -timedelta(seconds=1)),
        ]
    ):
        s.add(
            CastleDonation(
                user_id=user,
                group_id=group,
                amount=100,
                donated_at=now - age,
                request_key=f"seed:{i}",
            )
        )
    await s.commit()


async def test_castle_level_up_rewards_recent_donors(castle_session):
    s = castle_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    await _seed_reward_donors(s, now)
    # No level-up means no reward.
    await CastleService.donate(s, 1, -1, 50, now=now, request_key="below-threshold")
    assert not (
        await s.scalars(
            select(AppleTransaction).where(
                AppleTransaction.reason == "castle_level_up_reward"
            )
        )
    ).all()
    await CastleService.donate(s, 1, -1, 50, now=now, request_key="level-up")
    rewards = (
        await s.scalars(
            select(AppleTransaction).where(
                AppleTransaction.reason == "castle_level_up_reward"
            )
        )
    ).all()
    assert sorted(tx.user_id for tx in rewards) == [1, 2, 3]
    assert all(tx.amount == 100 and tx.ref_type == "castle_donation" for tx in rewards)
    assert len({tx.ref_id for tx in rewards}) == 1
    assert await AppleWalletService.get_balance(s, 1) == 10_000
    for user in (2, 3):
        assert await AppleWalletService.get_balance(s, user) == 100
    for user in (4, 5, 6):
        assert await AppleWalletService.get_balance(s, user) == 0
    await s.commit()
    await CastleService.donate(s, 1, -1, 50, now=now, request_key="level-up")
    assert (
        await s.scalar(
            select(func.count())
            .select_from(AppleTransaction)
            .where(AppleTransaction.reason == "castle_level_up_reward")
        )
        == 3
    )
    assert (await s.get(GroupCastle, -1)).total_donated == 5000


async def test_castle_level_up_reward_failure_rolls_back(castle_session, monkeypatch):
    s = castle_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    await _seed_reward_donors(s, now)
    original = AppleWalletService.earn

    async def fail_second_reward(session, user_id, *args, **kwargs):
        if user_id == 2:
            raise RuntimeError("reward failure")
        return await original(session, user_id, *args, **kwargs)

    monkeypatch.setattr(AppleWalletService, "earn", fail_second_reward)
    with pytest.raises(RuntimeError, match="reward failure"):
        await CastleService.donate(s, 1, -1, 100, now=now, request_key="retry")
    await s.rollback()
    castle = await s.get(GroupCastle, -1)
    assert (castle.level, castle.total_donated) == (1, 4900)
    assert await AppleWalletService.get_balance(s, 1) == 10_000
    assert (
        await s.scalar(
            select(func.count())
            .select_from(AppleTransaction)
            .where(AppleTransaction.reason == "castle_level_up_reward")
        )
        == 0
    )
    assert (
        await s.scalar(
            select(CastleDonation).where(CastleDonation.request_key == "retry")
        )
        is None
    )
    monkeypatch.setattr(AppleWalletService, "earn", original)
    await CastleService.donate(s, 1, -1, 100, now=now, request_key="retry")
    assert (
        await s.scalar(
            select(func.count())
            .select_from(AppleTransaction)
            .where(AppleTransaction.reason == "castle_level_up_reward")
        )
        == 3
    )


async def test_castle_new_donor_during_lock_wait_requires_atomic_retry(
    castle_session, monkeypatch
):
    s = castle_session
    now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    await _seed_reward_donors(s, now)
    original = CastleService._recent_donors

    async def stale_snapshot(session, group_id, at, *, for_update=False):
        recipients = await original(session, group_id, at, for_update=for_update)
        # Simulate a donor committed after our initial consistent read.
        return recipients if for_update else recipients - {2}

    monkeypatch.setattr(CastleService, "_recent_donors", stale_snapshot)
    with pytest.raises(CastleError, match="دوباره تلاش کنید"):
        await CastleService.donate(s, 1, -1, 100, now=now, request_key="stale")
    await s.rollback()
    assert await AppleWalletService.get_balance(s, 1) == 10_000
    assert (await s.get(GroupCastle, -1)).total_donated == 4900
    assert (
        await s.scalar(
            select(func.count())
            .select_from(AppleTransaction)
            .where(AppleTransaction.reason == "castle_level_up_reward")
        )
        == 0
    )
    monkeypatch.setattr(CastleService, "_recent_donors", original)
    await CastleService.donate(s, 1, -1, 100, now=now, request_key="stale")
    assert (
        await s.scalar(
            select(func.count())
            .select_from(AppleTransaction)
            .where(AppleTransaction.reason == "castle_level_up_reward")
        )
        == 3
    )


def test_castle_caption_shows_an_active_peace_shield():
    """C2-6: an active shield is announced in the castle menu; an expired one
    is not, and the menu itself stays exactly as it was."""
    from sib.handlers.castle import format_castle_caption

    active = format_castle_caption(
        level=2, total_donated=12_345, shield_seconds=4 * 3600 + 12 * 60
    )
    assert "🛡 سپر صلح: فعال (تا ۴ ساعت و ۱۲ دقیقه دیگر)" in active
    # Only the shield line was added: the rest is the unchanged menu.
    assert active.replace("🛡 سپر صلح: فعال (تا ۴ ساعت و ۱۲ دقیقه دیگر)\n\n", "") == (
        format_castle_caption(level=2, total_donated=12_345)
    )
    assert not any(digit in active for digit in "0123456789")

    # Sub-minute shields still read as Persian text, never as a Latin digit.
    assert "کمتر از یک دقیقه" in format_castle_caption(2, 12_345, shield_seconds=10)
