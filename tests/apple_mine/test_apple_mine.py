"""Apple Mine pickaxe rework: service, handler, schema and copy tests."""

import dataclasses
import re
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from sib.core.text import format_html
from sib.handlers import apple_mine as mine_handler
from sib.services import apple_mine as mine_service
from sib.services import apple_mine_narrative as narrative
from sib.services import telegram_ui
from sib.services.apple_mine import (
    CooldownError,
    PickaxeInsufficientFunds,
    PickaxeMaxLevelError,
    RewardRow,
    ShiftResult,
    UpgradeResult,
)
from sib.services.apple_wallet import AppleWalletService
from sib.database.models import AppleMineShift, AppleTransaction, UserPickaxe


NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _session_context(session):
    @asynccontextmanager
    async def _context():
        yield session

    return _context


def _load_module(path: Path, name: str):
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _default_reward_rows() -> list[RewardRow]:
    return mine_service._parse_reward_table(
        mine_service.ConfigService.default("pickaxe_rewards")
    )


def _view(level=2, balance=350, cooldown=0) -> mine_handler.MineView:
    rows = _default_reward_rows()
    costs = mine_service._parse_cost_table(
        mine_service.ConfigService.default("pickaxe_upgrade_costs")
    )
    below_max = level < mine_service.MAX_PICKAXE_LEVEL
    return mine_handler.MineView(
        level=level,
        reward=rows[level - 1],
        next_reward=rows[level] if below_max else None,
        # index i = cost to enter level i+1; the next step of level L sits at i = L
        upgrade_cost=costs[level] if below_max else None,
        balance=balance,
        cooldown=cooldown,
    )


class _ShiftSession:
    """Small fake session for service logic that is independently unit-tested."""

    def __init__(self, last_worked_at=None):
        self.last_worked_at = last_worked_at
        self.shifts = []
        self.flush = AsyncMock(side_effect=self._flush)
        self.scalar = AsyncMock(return_value=None)  # no previous shift

    def add(self, row):
        if isinstance(row, AppleMineShift):
            row.id = len(self.shifts) + 1
            self.shifts.append(row)

    async def _flush(self):
        if self.shifts:
            self.last_worked_at = self.shifts[-1].worked_at


def _shift_fixtures(monkeypatch, *, level: int, roll: str) -> None:
    """Pin the primary reward independently of the optional plant drop."""
    monkeypatch.setattr(mine_service, "_plant_drop_chance", AsyncMock(return_value=0))
    monkeypatch.setattr(mine_service, "lock_user_row", AsyncMock())
    monkeypatch.setattr(mine_service, "_database_now", AsyncMock(return_value=NOW))
    monkeypatch.setattr(
        mine_service, "get_pickaxe_level", AsyncMock(return_value=level)
    )
    monkeypatch.setattr(
        mine_service,
        "reward_table_for_level",
        AsyncMock(return_value=_default_reward_rows()[level - 1]),
    )
    if roll == "normal":
        # Never hit the special bands; normal roll always returns the max.
        monkeypatch.setattr(mine_service.random, "random", lambda: 0.9999)
        monkeypatch.setattr(mine_service.random, "randint", lambda a, b: b)
    else:
        # Hit the first special band (gold) on every roll.
        monkeypatch.setattr(mine_service.random, "random", lambda: 0.0001)


# ---------------------------------------------------------------------------
# Configuration tables
# ---------------------------------------------------------------------------


def test_parse_reward_table_accepts_defaults_and_rejects_bad_rows():
    rows = _default_reward_rows()
    assert [row.level for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0].min_apples == 10 and rows[0].max_apples == 15
    assert rows[4].min_apples == 45 and rows[4].max_apples == 60
    for row in rows:
        assert 0 <= row.special_pct <= 100

    bad = [
        [],  # wrong length
        [{"min": 1, "max": 2, "gold": 0, "diamond": 0, "obsidian": 0}] * 4,
        [{"min": 20, "max": 10, "gold": 1, "diamond": 1, "obsidian": 1}] * 5,
        [{"min": 0, "max": 10, "gold": 1, "diamond": 1, "obsidian": 1}] * 5,
        [{"min": 1, "max": 10, "gold": 100, "diamond": 1, "obsidian": 0}] * 5,
        [{"min": 1, "max": 10, "gold": 50, "diamond": 50, "obsidian": 1}] * 5,
        [{"min": 1, "max": 10, "gold": -1, "diamond": 0, "obsidian": 0}] * 5,
        [{"max": 10, "gold": 0, "diamond": 0, "obsidian": 0}] * 5,
    ]
    for entry in bad:
        with pytest.raises(ValueError):
            mine_service._parse_reward_table(entry)


def test_parse_cost_table_accepts_defaults_and_rejects_bad_values():
    assert mine_service._parse_cost_table(
        mine_service.ConfigService.default("pickaxe_upgrade_costs")
    ) == [0, 300, 600, 1000, 1500]

    bad = [
        [300, 600, 1000, 1500, 2000],  # level 1 must be free
        [0, -1, 600, 1000, 1500],
        [0, "300", 600, 1000, 1500],
        [0, True, 600, 1000, 1500],
        [0, 300, 600, 1000],  # wrong length
    ]
    for entry in bad:
        with pytest.raises(ValueError):
            mine_service._parse_cost_table(entry)


@pytest.mark.asyncio
async def test_invalid_config_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(
        mine_service.ConfigService, "get", AsyncMock(return_value={"broken": True})
    )
    table = await mine_service.reward_table(object())
    assert table == _default_reward_rows()
    costs = await mine_service.upgrade_costs(object())
    assert costs == [0, 300, 600, 1000, 1500]


@pytest.mark.asyncio
async def test_upgrade_cost_for_level_is_none_at_max(monkeypatch):
    monkeypatch.setattr(
        mine_service.ConfigService,
        "get",
        AsyncMock(return_value=mine_service.ConfigService.default("pickaxe_upgrade_costs")),
    )
    assert await mine_service.upgrade_cost_for_level(object(), 4) == 1500
    assert await mine_service.upgrade_cost_for_level(object(), 5) is None


# ---------------------------------------------------------------------------
# Shift roll: distribution and scaling with pickaxe level
# ---------------------------------------------------------------------------


def test_special_chance_distribution_within_tolerance():
    for row in (_default_reward_rows()[0], _default_reward_rows()[4]):
        counts: Counter = Counter()
        amounts = []
        for _ in range(10_000):
            code, _item, amount = mine_service._roll_shift(row)
            counts[code] += 1
            if code == "normal":
                assert row.min_apples <= amount <= row.max_apples
                amounts.append(amount)
        for code, pct in (
            ("golden", row.gold_pct),
            ("diamond", row.diamond_pct),
            ("obsidian", row.obsidian_pct),
        ):
            expected = 10_000 * pct / 100
            assert abs(counts[code] - expected) <= expected * 0.20 + 5, (code, row.level)
        expected_normal = 10_000 * (100 - row.special_pct) / 100
        assert abs(counts["normal"] - expected_normal) <= expected_normal * 0.05
        # The range is actually sampled, not pinned.
        assert min(amounts) > row.min_apples - 1
        assert max(amounts) < row.max_apples + 1
        assert len(set(amounts)) > 1


@pytest.mark.asyncio
async def test_work_rewards_scale_with_pickaxe_level(db_session, monkeypatch):
    from sib.database.models import AppleWallet, User

    monkeypatch.setattr(mine_service.random, "random", lambda: 0.9999)
    monkeypatch.setattr(mine_service.random, "randint", lambda a, b: b)

    cases = [(9201, 1, 15), (9202, 5, 60)]
    for user_id, level, expected_amount in cases:
        db_session.add(User(id=user_id, first_name="تست"))
        db_session.add(UserPickaxe(user_id=user_id, level=level))
        await db_session.flush()
        result = await mine_service.perform_shift(
            db_session, user_id=user_id, group_id=None
        )
        assert result.pickaxe_level == level
        assert result.amount == expected_amount

    shifts = (
        (
            await db_session.execute(
                sa.select(AppleMineShift).where(
                    AppleMineShift.user_id.in_([case[0] for case in cases])
                )
            )
        )
        .scalars()
        .all()
    )
    by_user = {shift.user_id: shift for shift in shifts}
    assert by_user[9201].amount == 15
    assert by_user[9201].pickaxe_level == 1
    assert by_user[9201].card_id is None
    assert by_user[9202].amount == 60
    assert by_user[9202].pickaxe_level == 5
    assert await AppleWalletService.get_balance(db_session, 9202) == 60


# ---------------------------------------------------------------------------
# Pickaxe progression (real SQLite session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pickaxe_starts_at_level_1(db_session):
    from sib.database.models import User

    db_session.add(User(id=9101, first_name="تست"))
    await db_session.flush()

    assert await mine_service.get_pickaxe_level(db_session, user_id=9101) == 1
    row = await db_session.get(UserPickaxe, 9101)
    assert row is not None
    assert row.level == 1


@pytest.mark.asyncio
async def test_upgrade_deducts_apples_and_increases_level(db_session):
    from sib.database.models import AppleWallet, User

    db_session.add(User(id=9102, first_name="تست"))
    db_session.add(AppleWallet(user_id=9102, balance=1000))
    await db_session.flush()

    result = await mine_service.upgrade_pickaxe(db_session, user_id=9102)

    assert result.level == 2
    assert result.cost == 300
    assert result.balance_after == 700
    assert await AppleWalletService.get_balance(db_session, 9102) == 700
    assert (await db_session.get(UserPickaxe, 9102)).level == 2
    ledger = (
        (
            await db_session.execute(
                sa.select(AppleTransaction).where(AppleTransaction.user_id == 9102)
            )
        )
        .scalars()
        .all()
    )
    assert any(
        entry.reason == "pickaxe_upgrade" and entry.amount == -300
        for entry in ledger
    )


@pytest.mark.asyncio
async def test_upgrade_requires_sequential_steps(db_session):
    from sib.database.models import AppleWallet, User

    db_session.add(User(id=9105, first_name="تست"))
    db_session.add(AppleWallet(user_id=9105, balance=10000))
    await db_session.flush()

    first = await mine_service.upgrade_pickaxe(db_session, user_id=9105)
    second = await mine_service.upgrade_pickaxe(db_session, user_id=9105)

    assert first.level == 2 and first.cost == 300
    assert second.level == 3 and second.cost == 600
    assert await AppleWalletService.get_balance(db_session, 9105) == 9100


@pytest.mark.asyncio
async def test_cannot_upgrade_past_level_5(db_session):
    from sib.database.models import AppleWallet, User

    db_session.add(User(id=9103, first_name="تست"))
    db_session.add(AppleWallet(user_id=9103, balance=10_000))
    db_session.add(UserPickaxe(user_id=9103, level=5))
    await db_session.flush()

    with pytest.raises(PickaxeMaxLevelError):
        await mine_service.upgrade_pickaxe(db_session, user_id=9103)

    assert await AppleWalletService.get_balance(db_session, 9103) == 10_000
    assert (await db_session.get(UserPickaxe, 9103)).level == 5
    ledger = (
        (
            await db_session.execute(
                sa.select(AppleTransaction).where(AppleTransaction.user_id == 9103)
            )
        )
        .scalars()
        .all()
    )
    assert ledger == []


@pytest.mark.asyncio
async def test_upgrade_insufficient_funds_reports_shortfall(db_session):
    from sib.database.models import AppleWallet, User

    db_session.add(User(id=9104, first_name="تست"))
    db_session.add(AppleWallet(user_id=9104, balance=250))
    await db_session.flush()

    with pytest.raises(PickaxeInsufficientFunds) as exc_info:
        await mine_service.upgrade_pickaxe(db_session, user_id=9104)

    assert exc_info.value.cost == 300
    assert exc_info.value.missing == 50
    assert (await db_session.get(UserPickaxe, 9104)).level == 1
    assert await AppleWalletService.get_balance(db_session, 9104) == 250


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_mine_cooldown_is_15_minutes():
    assert mine_service.WORK_COOLDOWN_SECONDS == 900


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("elapsed_seconds", "remaining_seconds"), [(0, 900), (600, 300), (899, 1)]
)
async def test_cooldown_blocks_second_shift_within_15_min(
    monkeypatch, elapsed_seconds, remaining_seconds
):
    session = _ShiftSession()
    _shift_fixtures(monkeypatch, level=1, roll="normal")
    monkeypatch.setattr(
        mine_service,
        "_last_worked_at",
        AsyncMock(return_value=NOW - timedelta(seconds=elapsed_seconds)),
    )

    with pytest.raises(CooldownError) as error:
        await mine_service.perform_shift(session, user_id=102, group_id=None)

    assert error.value.remaining_seconds == remaining_seconds
    assert session.shifts == []
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed_seconds", [900, 901])
async def test_cooldown_expires_after_15_min(elapsed_seconds):
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[NOW, NOW - timedelta(seconds=elapsed_seconds)])
    )
    assert await mine_service.remaining_cooldown(session, user_id=103) == 0


@pytest.mark.asyncio
async def test_shift_allowed_at_15_minutes(monkeypatch):
    session = _ShiftSession()
    _shift_fixtures(monkeypatch, level=1, roll="normal")
    monkeypatch.setattr(
        mine_service,
        "_last_worked_at",
        AsyncMock(return_value=NOW - timedelta(minutes=15)),
    )
    earn = AsyncMock(return_value=15)
    monkeypatch.setattr(mine_service.AppleWalletService, "earn", earn)

    result = await mine_service.perform_shift(session, user_id=103, group_id=None)

    assert result.item_code == "apple_normal"
    assert len(session.shifts) == 1
    earn.assert_awaited_once()


@pytest.mark.asyncio
async def test_cooldown_remaining_seconds_round_up():
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[NOW, NOW - timedelta(seconds=899.2)])
    )
    assert await mine_service.remaining_cooldown(session, user_id=104) == 1


def test_cooldown_message_uses_persian_digits():
    message = narrative.cooldown_message(125, user_id=105)
    assert "**۲ دقیقه و ۵ ثانیه**" in message
    assert re.search(r"[0-9]", message) is None


# ---------------------------------------------------------------------------
# Shift grants (fake session)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_called_once_per_shift(monkeypatch):
    session = _ShiftSession()
    _shift_fixtures(monkeypatch, level=2, roll="golden")
    grant = AsyncMock(return_value=3)
    monkeypatch.setattr(mine_service.ItemService, "grant", grant)

    result = await mine_service.perform_shift(session, user_id=101, group_id=None)

    grant.assert_awaited_once_with(session, 101, "apple_gold", quantity=3)
    assert result.outcome_code == "golden"
    assert result.pickaxe_level == 2
    assert session.shifts[0].pickaxe_level == 2
    assert session.shifts[0].card_id is None
    assert len(session.shifts) == 1


@pytest.mark.asyncio
async def test_normal_currency_uses_wallet_credit_once(monkeypatch):
    session = _ShiftSession()
    _shift_fixtures(monkeypatch, level=1, roll="normal")
    earn = AsyncMock(return_value=15)
    monkeypatch.setattr(mine_service.AppleWalletService, "earn", earn)

    result = await mine_service.perform_shift(session, user_id=109, group_id=None)

    assert result.item_code == "apple_normal"
    assert result.amount == 15
    earn.assert_awaited_once_with(
        session,
        user_id=109,
        amount=15,
        reason="apple_mine",
        ref_type="apple_mine_shift",
        ref_id=1,
        idempotency_key="apple_mine:1",
        reward_multiplier=1,
        occurred_at=NOW,
    )


@pytest.mark.asyncio
@pytest.mark.golden_hour
async def test_normal_mine_reward_doubles_and_reports_paid_amount(monkeypatch):
    session = _ShiftSession()
    _shift_fixtures(monkeypatch, level=1, roll="normal")
    golden_at = datetime(2026, 10, 26, 16, 45, tzinfo=timezone.utc)
    monkeypatch.setattr(
        mine_service, "_database_now", AsyncMock(return_value=golden_at)
    )
    earn = AsyncMock(return_value=30)
    monkeypatch.setattr(mine_service.AppleWalletService, "earn", earn)

    result = await mine_service.perform_shift(session, user_id=110, group_id=None)

    assert result.amount == 30
    assert session.shifts[0].amount == 30
    earn.assert_awaited_once_with(
        session,
        user_id=110,
        amount=15,
        reason="apple_mine",
        ref_type="apple_mine_shift",
        ref_id=1,
        idempotency_key="apple_mine:1",
        reward_multiplier=2,
        occurred_at=golden_at,
    )


@pytest.mark.asyncio
async def test_double_tap_safety(monkeypatch):
    session = _ShiftSession()
    _shift_fixtures(monkeypatch, level=3, roll="golden")

    async def last_worked(_session, _user_id):
        return session.last_worked_at

    monkeypatch.setattr(mine_service, "_last_worked_at", last_worked)
    grant = AsyncMock(return_value=3)
    monkeypatch.setattr(mine_service.ItemService, "grant", grant)

    first = await mine_service.perform_shift(session, user_id=108, group_id=None)
    assert first.outcome_code == "golden"
    with pytest.raises(CooldownError):
        await mine_service.perform_shift(session, user_id=108, group_id=None)

    assert len(session.shifts) == 1
    grant.assert_awaited_once()


# ---------------------------------------------------------------------------
# Handler: main menu, work flow, pickaxe screens
# ---------------------------------------------------------------------------


def test_no_card_selection_required_for_mine():
    import inspect

    kb = mine_handler._main_keyboard().inline_keyboard
    assert len(kb) == 2 and all(len(row) == 1 for row in kb)
    callbacks = {button.callback_data for row in kb for button in row}
    assert callbacks == {"mine:work", "mine:pickaxe"}

    # The M1 card-list screen is gone: no route, no service, no callback.
    source = inspect.getsource(mine_handler)
    assert "mine:card:" not in source
    assert "PlayerCardService" not in source
    assert "handle_mine_card" not in source


@pytest.mark.asyncio
async def test_work_performs_shift_directly(monkeypatch):
    session = object()
    monkeypatch.setattr(mine_handler, "get_session", _session_context(session))
    monkeypatch.setattr(
        mine_handler, "_register_user_and_group", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        mine_handler, "remaining_cooldown", AsyncMock(return_value=0)
    )
    result = ShiftResult(
        outcome_code="normal",
        item_code="apple_normal",
        amount=18,
        narrative="⛏ **رگهٔ سیب** رو پیدا کردی.",
        pickaxe_level=2,
    )
    perform = AsyncMock(return_value=result)
    monkeypatch.setattr(mine_handler, "perform_shift", perform)
    edit_caption = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "safe_edit_message_caption", edit_caption)

    photo = [object()]
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-501),
        message_id=77,
        photo=photo,
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        data="mine:work",
        from_user=SimpleNamespace(id=107),
        message=message,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace()

    await mine_handler.handle_mine_work(callback, bot)

    perform.assert_awaited_once()
    args, kwargs = perform.await_args
    assert args == (session,)
    assert kwargs == {"user_id": 107, "group_id": None}
    assert "card_id" not in kwargs
    edit_caption.assert_awaited_once()
    edit_args, edit_kwargs = edit_caption.await_args
    assert edit_args[:3] == (bot, -501, 77)
    assert edit_kwargs["reply_markup"].inline_keyboard == []
    assert edit_kwargs["parse_mode"] == "HTML"
    caption = edit_args[3]
    assert caption == format_html(
        "⛏ **رگهٔ سیب** رو پیدا کردی.\n"
        "⛏ **۱۸ سیب معمولی** گیرت اومد!\n"
        "🔨 سطح کلنگ: **۲**؛ شیفت بعدی: **۱۵ دقیقه دیگه**"
    )
    assert message.photo is photo


@pytest.mark.asyncio
@pytest.mark.parametrize("cooldown_source", ["precheck", "locked_shift"])
@pytest.mark.parametrize(
    ("remaining_seconds", "expected_duration"),
    [
        (900, "۱۵ دقیقه و ۰ ثانیه"),
        (899, "۱۴ دقیقه و ۵۹ ثانیه"),
        (600, "۱۰ دقیقه و ۰ ثانیه"),
    ],
)
async def test_work_cooldown_alerts(
    monkeypatch, cooldown_source, remaining_seconds, expected_duration
):
    monkeypatch.setattr(mine_handler, "get_session", _session_context(object()))
    monkeypatch.setattr(
        mine_handler, "_register_user_and_group", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        mine_handler,
        "remaining_cooldown",
        AsyncMock(return_value=remaining_seconds if cooldown_source == "precheck" else 0),
    )
    perform = AsyncMock(side_effect=CooldownError(remaining_seconds))
    monkeypatch.setattr(mine_handler, "perform_shift", perform)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-504), message_id=80, photo=[object()]
    )
    callback = SimpleNamespace(
        data="mine:work",
        from_user=SimpleNamespace(id=111),
        message=message,
        answer=AsyncMock(),
    )

    await mine_handler.handle_mine_work(callback, SimpleNamespace())

    if cooldown_source == "precheck":
        perform.assert_not_awaited()
    else:
        perform.assert_awaited_once()
    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.await_args
    assert kwargs.get("show_alert") is True
    assert expected_duration in args[0]
    assert re.search(r"[0-9a-zA-Z]", args[0]) is None


@pytest.mark.asyncio
async def test_pickaxe_screen_swaps_photo_in_place(monkeypatch, tmp_path):
    session = object()
    monkeypatch.setattr(mine_handler, "get_session", _session_context(session))
    monkeypatch.setattr(
        mine_handler, "_register_user_and_group", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        mine_handler, "_read_mine_view", AsyncMock(return_value=_view(level=2))
    )
    media = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "safe_edit_message_media", media)
    fake_asset = tmp_path / "pickaxe_2.jpg"
    fake_asset.write_bytes(b"fake-jpeg")
    monkeypatch.setattr(
        mine_handler, "_pickaxe_image_path", lambda level: fake_asset
    )

    photo = [object()]
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-502), message_id=78, photo=photo
    )
    callback = SimpleNamespace(
        data="mine:pickaxe",
        from_user=SimpleNamespace(id=112),
        message=message,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace()

    await mine_handler.handle_mine_pickaxe(callback, bot)

    media.assert_awaited_once()
    args, kwargs = media.await_args
    assert args[:3] == (bot, -502, 78)
    assert str(getattr(kwargs["photo"], "path", kwargs["photo"])).endswith(
        "pickaxe_2.jpg"
    )
    assert format_html("📊 سطح فعلی: **۲ از ۵**") in kwargs["text"]
    assert format_html("💰 هزینه ارتقا: **۶۰۰ سیب**") in kwargs["text"]
    assert "**" not in kwargs["text"]
    assert kwargs["parse_mode"] == "HTML"
    kb = kwargs["reply_markup"].inline_keyboard
    assert {b.callback_data for row in kb for b in row} == {
        "mine:pickaxe:upgrade",
        "mine:pickaxe:back",
    }


@pytest.mark.asyncio
async def test_upgrade_success_edits_to_new_level(monkeypatch, tmp_path):
    monkeypatch.setattr(mine_handler, "get_session", _session_context(object()))
    monkeypatch.setattr(
        mine_handler,
        "upgrade_pickaxe",
        AsyncMock(return_value=UpgradeResult(level=3, cost=600, balance_after=750)),
    )
    monkeypatch.setattr(
        mine_handler,
        "_read_mine_view",
        AsyncMock(return_value=_view(level=3, balance=750)),
    )
    media = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "safe_edit_message_media", media)
    fake_asset = tmp_path / "pickaxe_3.jpg"
    fake_asset.write_bytes(b"fake-jpeg")
    monkeypatch.setattr(
        mine_handler, "_pickaxe_image_path", lambda level: fake_asset
    )

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-505), message_id=81, photo=[object()]
    )
    callback = SimpleNamespace(
        data="mine:pickaxe:upgrade",
        from_user=SimpleNamespace(id=113),
        message=message,
        answer=AsyncMock(),
    )

    await mine_handler.handle_mine_pickaxe_upgrade(callback, SimpleNamespace())

    args, kwargs = media.await_args
    assert str(getattr(kwargs["photo"], "path", kwargs["photo"])).endswith(
        "pickaxe_3.jpg"
    )
    assert format_html("🔨 **ارتقای کلنگ**") in kwargs["text"]
    assert format_html("📊 سطح فعلی: **۳ از ۵**") in kwargs["text"]
    assert format_html("🍎 موجودی شما: **۷۵۰ سیب**") in kwargs["text"]
    assert "**" not in kwargs["text"]
    assert "🎉" not in kwargs["text"]
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_upgrade_insufficient_shows_shortfall_alert(monkeypatch):
    monkeypatch.setattr(mine_handler, "get_session", _session_context(object()))
    monkeypatch.setattr(
        mine_handler,
        "upgrade_pickaxe",
        AsyncMock(side_effect=PickaxeInsufficientFunds(cost=600, missing=250)),
    )

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-506), message_id=82, photo=[object()]
    )
    callback = SimpleNamespace(
        data="mine:pickaxe:upgrade",
        from_user=SimpleNamespace(id=114),
        message=message,
        answer=AsyncMock(),
    )

    await mine_handler.handle_mine_pickaxe_upgrade(callback, SimpleNamespace())

    args, kwargs = callback.answer.await_args
    assert kwargs.get("show_alert") is True
    assert "۲۵۰" in args[0]


@pytest.mark.asyncio
async def test_upgrade_stale_max_level_renders_max_screen(monkeypatch, tmp_path):
    monkeypatch.setattr(mine_handler, "get_session", _session_context(object()))
    monkeypatch.setattr(
        mine_handler,
        "upgrade_pickaxe",
        AsyncMock(side_effect=PickaxeMaxLevelError(5)),
    )
    monkeypatch.setattr(
        mine_handler,
        "_read_mine_view",
        AsyncMock(return_value=_view(level=5, balance=999)),
    )
    media = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "safe_edit_message_media", media)
    fake_asset = tmp_path / "pickaxe_5.jpg"
    fake_asset.write_bytes(b"fake-jpeg")
    monkeypatch.setattr(
        mine_handler, "_pickaxe_image_path", lambda level: fake_asset
    )

    message = SimpleNamespace(
        chat=SimpleNamespace(id=-507), message_id=83, photo=[object()]
    )
    callback = SimpleNamespace(
        data="mine:pickaxe:upgrade",
        from_user=SimpleNamespace(id=115),
        message=message,
        answer=AsyncMock(),
    )

    await mine_handler.handle_mine_pickaxe_upgrade(callback, SimpleNamespace())

    args, kwargs = media.await_args
    assert str(getattr(kwargs["photo"], "path", kwargs["photo"])).endswith(
        "pickaxe_5.jpg"
    )
    assert format_html("🏆 **کلنگ به سطح ۵ رسید!**") in kwargs["text"]
    assert "🎉" not in kwargs["text"]
    kb = kwargs["reply_markup"].inline_keyboard
    assert {b.callback_data for row in kb for b in row} == {"mine:pickaxe:back"}


# ---------------------------------------------------------------------------
# Captions and keyboards
# ---------------------------------------------------------------------------


def test_panel_caption_shows_pickaxe_level_and_ready_state():
    caption = mine_handler._panel_caption(_view(level=1, cooldown=0), 7)
    assert caption == (
        "⛏ **معدن سیبی**\n"
        "🔨 کلنگ: **سطح ۱**\n"
        "🎁 هر شیفت: **۱۰ تا ۱۵ سیب**\n"
        "✨ شانس سیب ویژه: **۱۰٪**\n"
        "🕗 ساعت طلایی تهران (۲۰:۰۰ تا پیش از ۲۱:۰۰): سیب معمولی دوبرابر می‌شود.\n\n"
        "✅ **معدن آماده‌ست! کلنگ بزن.**"
    )
    assert re.search(r"[0-9]", caption) is None


@pytest.mark.parametrize(
    ("remaining_seconds", "expected_duration"),
    [(900, "۱۵ دقیقه و ۰ ثانیه"), (125, "۲ دقیقه و ۵ ثانیه")],
)
def test_panel_caption_shows_cooldown_line(remaining_seconds, expected_duration):
    caption = mine_handler._panel_caption(_view(cooldown=remaining_seconds), 7)
    assert caption.endswith(f"⏳ شیفت بعدی: **{expected_duration} دیگه**.")
    assert re.search(r"[0-9]", caption) is None


def test_pickaxe_caption_shows_preview_and_cost():
    caption = mine_handler._pickaxe_caption(_view(level=2, balance=350))
    assert caption == (
        "🔨 **ارتقای کلنگ**\n\n"
        "📊 سطح فعلی: **۲ از ۵**\n"
        "⛏ استخراج: **۱۵ تا ۲۲ سیب**\n"
        "✨ شانس سیب ویژه: **۱۴٪**\n\n"
        "⬆️ **سطح ۳:**\n"
        "⛏ استخراج: **۲۱ تا ۳۰ سیب**\n"
        "✨ شانس سیب ویژه: **۱۹٪**\n\n"
        "💰 هزینه ارتقا: **۶۰۰ سیب**\n"
        "🍎 موجودی شما: **۳۵۰ سیب**\n\n"
        "🕗 ساعت طلایی تهران (۲۰:۰۰ تا پیش از ۲۱:۰۰): سیب معمولی دوبرابر می‌شود."
    )
    assert re.search(r"[0-9]", caption) is None


def test_pickaxe_caption_max_level():
    caption = mine_handler._pickaxe_caption(_view(level=5, balance=999))
    assert "🏆 **کلنگ به سطح ۵ رسید!**" in caption
    assert "📊 سطح فعلی: **۵ از ۵**" in caption
    assert "🍎 موجودی شما: **۹۹۹ سیب**" in caption
    assert "هزینه ارتقا" not in caption
    assert re.search(r"[0-9]", caption) is None


def test_pickaxe_caption_after_upgrade_uses_current_level_format():
    caption = mine_handler._pickaxe_caption(_view(level=3, balance=750))
    assert "🔨 **ارتقای کلنگ**" in caption
    assert "📊 سطح فعلی: **۳ از ۵**" in caption
    assert "🍎 موجودی شما: **۷۵۰ سیب**" in caption
    assert "🎉" not in caption
    assert re.search(r"[0-9]", caption) is None


def test_result_caption_formats_special_reward_with_persian_digits():
    result = ShiftResult(
        "golden", "apple_gold", 3, "🌕 **سیب طلایی** زیر دستت درخشید.", pickaxe_level=4
    )
    caption = mine_handler._result_caption(result)
    assert caption == (
        "🌕 **سیب طلایی** زیر دستت درخشید.\n"
        "⛏ **۳ سیب طلایی** گیرت اومد!\n"
        "🔨 سطح کلنگ: **۴**؛ شیفت بعدی: **۱۵ دقیقه دیگه**"
    )
    assert re.search(r"[0-9]", caption) is None


def test_result_caption_uses_cooldown_constant(monkeypatch):
    monkeypatch.setattr(mine_handler, "WORK_COOLDOWN_SECONDS", 7 * 60)
    result = ShiftResult(
        "golden", "apple_gold", 3, "سیب طلایی از دل سنگ پیدا شد.", pickaxe_level=4
    )
    assert mine_handler._result_caption(result).endswith(
        "شیفت بعدی: **۷ دقیقه دیگه**"
    )


def test_main_menu_keyboard_layout():
    kb = mine_handler._main_keyboard().inline_keyboard
    assert len(kb) == 2 and all(len(row) == 1 for row in kb)
    work, pickaxe = kb[0][0], kb[1][0]
    assert work.callback_data == "mine:work"
    if mine_handler.APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE:
        assert work.text == "⛏ کار کردن در معدن"
        assert work.style == "success"
    else:
        assert work.text == "🟢 ⛏ کار کردن در معدن"
        assert work.style is None
    assert pickaxe.callback_data == "mine:pickaxe"
    assert pickaxe.text == "🔨 ارتقا کلنگ"
    assert pickaxe.style == "primary"


def test_pickaxe_keyboard_upgrade_label_and_back():
    view = _view(level=4, balance=0)
    assert view.balance < view.upgrade_cost
    kb = mine_handler._pickaxe_keyboard(view).inline_keyboard
    assert len(kb) == 2
    upgrade, back = kb[0][0], kb[1][0]
    assert upgrade.callback_data == "mine:pickaxe:upgrade"
    assert upgrade.text == "🍏 ارتقا به سطح ۵"
    assert upgrade.style == "success"
    assert "۱۵۰۰" not in upgrade.text
    assert back.callback_data == "mine:pickaxe:back"
    assert back.text == "🔙 بازگشت"


def test_pickaxe_keyboard_max_level_has_back_only():
    kb = mine_handler._pickaxe_keyboard(_view(level=5)).inline_keyboard
    assert len(kb) == 1
    assert kb[0][0].callback_data == "mine:pickaxe:back"


def test_supported_work_button_uses_success_style():
    button = telegram_ui.work_button_kb(supports_style=True).inline_keyboard[0][0]
    assert button.text == "⛏ کار کردن در معدن"
    assert button.callback_data == "mine:work"
    assert button.style == "success"


def test_fallback_work_button_label():
    button = telegram_ui.work_button_kb(supports_style=False).inline_keyboard[0][0]
    assert button.text == "🟢 ⛏ کار کردن در معدن"
    assert button.callback_data == "mine:work"
    assert button.style is None


def test_style_type_error_falls_back(monkeypatch):
    original_button = telegram_ui.InlineKeyboardButton

    def button_factory(*args, **kwargs):
        if "style" in kwargs:
            raise TypeError("old aiogram does not accept style")
        return original_button(*args, **kwargs)

    monkeypatch.setattr(telegram_ui, "InlineKeyboardButton", button_factory)
    monkeypatch.setattr(telegram_ui, "_WORK_BUTTON_PATH_LOGGED", False)

    button = telegram_ui.work_button_kb(supports_style=True).inline_keyboard[0][0]
    assert button.text == "🟢 ⛏ کار کردن در معدن"
    assert button.style is None


def test_no_english_letters_in_user_text():
    from sib.services.apple_mine_narrative import (
        COOLDOWN,
        DIAMOND,
        GOLDEN,
        NORMAL,
        OBSIDIAN,
    )

    view = _view()
    result = ShiftResult(
        "normal", "apple_normal", 15, "چند سیب از دل سنگ بیرون آمد.", pickaxe_level=2
    )
    user_texts = [
        mine_handler._panel_caption(view, 1),
        mine_handler._panel_caption(dataclasses.replace(view, cooldown=25), 1),
        mine_handler._pickaxe_caption(view),
        mine_handler._pickaxe_caption(view),
        mine_handler._pickaxe_caption(_view(level=5)),
        mine_handler._result_caption(result),
        mine_handler._insufficient_text(
            PickaxeInsufficientFunds(cost=600, missing=250)
        ),
        mine_handler.MINE_FAILURE_TEXT,
        *NORMAL,
        *GOLDEN,
        *DIAMOND,
        *OBSIDIAN,
        *[line.replace("{mm}", "۲").replace("{ss}", "۵") for line in COOLDOWN],
        *[b.text for row in mine_handler._main_keyboard().inline_keyboard for b in row],
        *[b.text for row in mine_handler._pickaxe_keyboard(view).inline_keyboard for b in row],
        telegram_ui.work_button_kb(supports_style=False).inline_keyboard[0][0].text,
    ]
    for text in user_texts:
        assert re.search(r"[A-Za-z]", text) is None, text


def test_narrative_pools_are_short_modern_and_bold():
    from string import Formatter

    for pool in (narrative.NORMAL, narrative.GOLDEN, narrative.DIAMOND, narrative.OBSIDIAN, narrative.COOLDOWN):
        assert len(pool) == 6 and len(set(pool)) == 6
        for text in pool:
            assert 0 < len(text) <= 120
            assert 1 <= len(re.findall(r"[.!؟]", text)) <= 2
            assert text.count("**") >= 2 and text.count("**") % 2 == 0
            assert not any(mark in text for mark in ("—", "–", "...", "\n"))
            assert not any(char in text for char in "\u064a\u0643\u0664\u0665\u0666")
    for text in narrative.COOLDOWN:
        fields = {field for _, field, _, _ in Formatter().parse(text) if field}
        assert fields == {"mm", "ss"}


def test_narratives_avoid_repeating_last_five_for_one_user():
    picked = [narrative.pick_narrative("normal", user_id=110) for _ in range(6)]
    assert len(set(picked)) == 6


# ---------------------------------------------------------------------------
# Schema and migration
# ---------------------------------------------------------------------------


def test_shift_model_has_required_indexes_and_nullable_foreign_keys():
    table = AppleMineShift.__table__
    indexes = {index.name: tuple(column.name for column in index.columns) for index in table.indexes}
    assert indexes["ix_apple_mine_shift_user_worked_at"] == ("user_id", "worked_at")
    assert indexes["ix_apple_mine_shift_user_id"] == ("user_id",)
    assert indexes["ix_apple_mine_shift_worked_at"] == ("worked_at",)
    assert table.c.user_id.nullable is False
    assert table.c.group_id.nullable is True
    assert table.c.card_id.nullable is True
    assert table.c.pickaxe_level.nullable is True


def test_user_pickaxe_model_shape():
    table = UserPickaxe.__table__
    assert set(table.c.keys()) == {"user_id", "level", "created_at", "updated_at"}
    assert set(table.primary_key.columns.keys()) == {"user_id"}
    assert table.c.level.nullable is False
    foreign_keys = list(table.c.user_id.foreign_keys)
    assert foreign_keys and foreign_keys[0].target_fullname == "users.id"


def test_migration_053_upgrade_and_downgrade_are_reversible():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    versions = (
        Path(__file__).resolve().parents[2]
        / "database"
        / "migrations"
        / "versions"
    )
    migration_043 = _load_module(versions / "043_apple_mine_shift.py", "m043")
    migration_053 = _load_module(versions / "053_apple_pickaxes.py", "m053")

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id BIGINT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE groups (id BIGINT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE player_cards (id BIGINT PRIMARY KEY)")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration_043.upgrade()
            migration_053.upgrade()
        inspector = inspect(connection)
        assert "user_pickaxes" in inspector.get_table_names()
        assert {
            column["name"] for column in inspector.get_columns("user_pickaxes")
        } == {"user_id", "level", "created_at", "updated_at"}
        assert "pickaxe_level" in {
            column["name"] for column in inspector.get_columns("apple_mine_shift")
        }
        with Operations.context(context):
            migration_053.downgrade()
        inspector = inspect(connection)
        assert "user_pickaxes" not in inspector.get_table_names()
        assert "pickaxe_level" not in {
            column["name"] for column in inspector.get_columns("apple_mine_shift")
        }
        with Operations.context(context):
            migration_043.downgrade()
    engine.dispose()
