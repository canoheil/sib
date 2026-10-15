"""Apple Mine reward, cooldown, and handler regression tests."""

from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sib.handlers import apple_mine as mine_handler
from sib.services import apple_mine as mine_service
from sib.services import apple_mine_narrative as narrative
from sib.services import telegram_ui
from sib.services.apple_mine import CooldownError, ShiftResult
from sib.database.models import AppleMineShift


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _session_context(session):
    @asynccontextmanager
    async def _context():
        yield session

    return _context


class _ShiftSession:
    """Small fake session for service logic that is independently unit-tested."""

    def __init__(self, last_worked_at=None, *, card_id=None):
        self.last_worked_at = last_worked_at
        self.card_id = card_id
        self.shifts = []
        self.flush = AsyncMock(side_effect=self._flush)
        self.scalar = AsyncMock(return_value=card_id)

    def add(self, row):
        if isinstance(row, AppleMineShift):
            row.id = len(self.shifts) + 1
            self.shifts.append(row)

    async def _flush(self):
        if self.shifts:
            self.last_worked_at = self.shifts[-1].worked_at


@pytest.fixture
def deterministic_normal_roll(monkeypatch):
    monkeypatch.setattr(
        mine_service.random,
        "choices",
        lambda population, weights, k: [("normal", 60, "apple_normal", 15)],
    )


def test_weights_sum_to_100():
    assert sum(weight for _code, weight, _item, _amount in mine_service.OUTCOMES) == 100


def test_outcome_distribution_within_tolerance():
    rng = random.Random(20261015)
    outcomes = rng.choices(
        mine_service.OUTCOMES,
        weights=[entry[1] for entry in mine_service.OUTCOMES],
        k=10_000,
    )
    counts = Counter(outcome[0] for outcome in outcomes)
    for code, weight, _item_code, _amount in mine_service.OUTCOMES:
        expected = 10_000 * weight / 100
        assert abs(counts[code] - expected) <= expected * 0.20


@pytest.mark.asyncio
async def test_grant_called_once_per_shift(monkeypatch):
    session = _ShiftSession(card_id=51)
    monkeypatch.setattr(mine_service, "lock_user_row", AsyncMock())
    monkeypatch.setattr(mine_service, "_database_now", AsyncMock(return_value=NOW))
    monkeypatch.setattr(mine_service, "_last_worked_at", AsyncMock(return_value=None))
    monkeypatch.setattr(
        mine_service.random,
        "choices",
        lambda population, weights, k: [("golden", 25, "apple_gold", 3)],
    )
    grant = AsyncMock(return_value=3)
    monkeypatch.setattr(mine_service.ItemService, "grant", grant)

    result = await mine_service.perform_shift(
        session, user_id=101, group_id=None, card_id=51
    )

    grant.assert_awaited_once_with(session, 101, "apple_gold", quantity=3)
    assert result.outcome_code == "golden"
    assert len(session.shifts) == 1


@pytest.mark.asyncio
async def test_cooldown_blocks_second_shift_within_30_min(monkeypatch):
    session = _ShiftSession()
    monkeypatch.setattr(mine_service, "lock_user_row", AsyncMock())
    monkeypatch.setattr(mine_service, "_database_now", AsyncMock(return_value=NOW))
    monkeypatch.setattr(
        mine_service,
        "_last_worked_at",
        AsyncMock(return_value=NOW - timedelta(seconds=1799)),
    )

    with pytest.raises(CooldownError) as error:
        await mine_service.perform_shift(
            session, user_id=102, group_id=None, card_id=None
        )

    assert error.value.remaining_seconds == 1
    assert session.shifts == []


@pytest.mark.asyncio
async def test_cooldown_expires_after_30_min(monkeypatch):
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[NOW, NOW - timedelta(seconds=1801)]))
    assert await mine_service.remaining_cooldown(session, user_id=103) == 0


@pytest.mark.asyncio
async def test_cooldown_remaining_seconds_round_up(monkeypatch):
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[NOW, NOW - timedelta(seconds=1799.2)])
    )
    assert await mine_service.remaining_cooldown(session, user_id=104) == 1


def test_cooldown_message_uses_persian_digits():
    message = narrative.cooldown_message(125, user_id=105)
    assert message.startswith("⛏ دیوارهای معدن هنوز خاموش‌اند.")
    assert "۲ دقیقه و ۵ ثانیه" in message
    assert re.search(r"[0-9]", message) is None


@pytest.mark.asyncio
async def test_no_cards_flow(monkeypatch):
    session = object()
    monkeypatch.setattr(mine_handler, "get_session", _session_context(session))
    monkeypatch.setattr(
        mine_handler, "_register_user_and_group", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(mine_handler, "remaining_cooldown", AsyncMock(return_value=0))
    monkeypatch.setattr(
        mine_handler.PlayerCardService, "list_cards", AsyncMock(return_value=[])
    )

    user = SimpleNamespace(id=106)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=501, type="private"),
        answer=AsyncMock(),
    )
    callback = SimpleNamespace(
        from_user=user,
        message=message,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())

    await mine_handler.handle_mine_work(callback, bot)

    callback.answer.assert_awaited_once()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == mine_handler.NO_CARDS_TEXT


@pytest.mark.asyncio
async def test_panel_edit_after_success(monkeypatch):
    session = object()
    monkeypatch.setattr(mine_handler, "get_session", _session_context(session))
    register = AsyncMock(return_value=None)
    monkeypatch.setattr(mine_handler, "_register_user_and_group", register)
    result = ShiftResult(
        outcome_code="golden",
        item_code="apple_gold",
        amount=3,
        narrative="نوری طلایی از شکاف سنگ تابید؛ سیب طلایی درخشید.",
    )
    monkeypatch.setattr(mine_handler, "perform_shift", AsyncMock(return_value=result))
    edit_caption = AsyncMock(return_value=True)
    monkeypatch.setattr(mine_handler, "safe_edit_message_caption", edit_caption)

    photo = [object()]
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-501),
        message_id=77,
        photo=photo,
    )
    callback = SimpleNamespace(
        data="mine:card:51",
        from_user=SimpleNamespace(id=107),
        message=message,
        answer=AsyncMock(),
    )
    bot = SimpleNamespace()

    await mine_handler.handle_mine_card(callback, bot)

    register.assert_not_awaited()
    callback.answer.assert_awaited_once()
    edit_caption.assert_awaited_once()
    args, kwargs = edit_caption.await_args
    assert args[:3] == (bot, -501, 77)
    assert kwargs["reply_markup"].inline_keyboard == []
    assert kwargs["parse_mode"] is None
    assert "🌕 +۳ سیب طلایی" in args[3]
    assert message.photo is photo


@pytest.mark.asyncio
async def test_double_tap_safety(monkeypatch):
    session = _ShiftSession()
    monkeypatch.setattr(mine_service, "lock_user_row", AsyncMock())
    monkeypatch.setattr(mine_service, "_database_now", AsyncMock(return_value=NOW))

    async def last_worked(_session, _user_id):
        return session.last_worked_at

    monkeypatch.setattr(mine_service, "_last_worked_at", last_worked)
    monkeypatch.setattr(
        mine_service.random,
        "choices",
        lambda population, weights, k: [("golden", 25, "apple_gold", 3)],
    )
    grant = AsyncMock(return_value=3)
    monkeypatch.setattr(mine_service.ItemService, "grant", grant)

    first = await mine_service.perform_shift(
        session, user_id=108, group_id=None, card_id=None
    )
    assert first.outcome_code == "golden"
    with pytest.raises(CooldownError):
        await mine_service.perform_shift(
            session, user_id=108, group_id=None, card_id=None
        )

    assert len(session.shifts) == 1
    grant.assert_awaited_once()


def test_no_english_letters_in_user_text():
    from sib.services.apple_mine_narrative import (
        COOLDOWN,
        DIAMOND,
        GOLDEN,
        NORMAL,
        OBSIDIAN,
    )

    user_texts = [
        mine_handler.PANEL_CAPTION,
        mine_handler.CARD_PICK_CAPTION,
        mine_handler.NO_CARDS_TEXT,
        mine_handler.MINE_FAILURE_TEXT,
        mine_handler.CARD_NOT_OWNED_TEXT,
        mine_handler.CARD_LIST_FAILURE_TEXT,
        *NORMAL,
        *GOLDEN,
        *DIAMOND,
        *OBSIDIAN,
        *[line.replace("{mm}", "۲").replace("{ss}", "۵") for line in COOLDOWN],
        mine_handler._result_caption(
            ShiftResult("normal", "apple_normal", 15, "چند سیب از دل سنگ بیرون آمد.")
        ),
        telegram_ui.work_button_kb(supports_style=False).inline_keyboard[0][0].text,
    ]
    for text in user_texts:
        assert re.search(r"[A-Za-z]", text) is None, text


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


@pytest.mark.asyncio
async def test_normal_currency_uses_wallet_credit_once(monkeypatch):
    session = _ShiftSession()
    monkeypatch.setattr(mine_service, "lock_user_row", AsyncMock())
    monkeypatch.setattr(mine_service, "_database_now", AsyncMock(return_value=NOW))
    monkeypatch.setattr(mine_service, "_last_worked_at", AsyncMock(return_value=None))
    monkeypatch.setattr(
        mine_service.random,
        "choices",
        lambda population, weights, k: [("normal", 60, "apple_normal", 15)],
    )
    earn = AsyncMock(return_value=15)
    monkeypatch.setattr(mine_service.AppleWalletService, "earn", earn)

    result = await mine_service.perform_shift(
        session, user_id=109, group_id=None, card_id=None
    )

    assert result.item_code == "apple_normal"
    earn.assert_awaited_once_with(
        session,
        user_id=109,
        amount=15,
        reason="apple_mine",
        ref_type="apple_mine_shift",
        ref_id=1,
        idempotency_key="apple_mine:1",
    )


def test_narratives_avoid_repeating_last_five_for_one_user():
    picked = [narrative.pick_narrative("normal", user_id=110) for _ in range(6)]
    assert len(set(picked)) == 6


def test_shift_model_has_required_indexes_and_nullable_foreign_keys():
    table = AppleMineShift.__table__
    indexes = {index.name: tuple(column.name for column in index.columns) for index in table.indexes}
    assert indexes["ix_apple_mine_shift_user_worked_at"] == ("user_id", "worked_at")
    assert indexes["ix_apple_mine_shift_user_id"] == ("user_id",)
    assert indexes["ix_apple_mine_shift_worked_at"] == ("worked_at",)
    assert table.c.user_id.nullable is False
    assert table.c.group_id.nullable is True
    assert table.c.card_id.nullable is True


def test_migration_upgrade_and_downgrade_are_reversible():
    from importlib.util import module_from_spec, spec_from_file_location

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    migration_path = (
        Path(__file__).resolve().parents[2]
        / "database"
        / "migrations"
        / "versions"
        / "043_apple_mine_shift.py"
    )
    spec = spec_from_file_location("apple_mine_migration", migration_path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id BIGINT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE groups (id BIGINT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE player_cards (id BIGINT PRIMARY KEY)")
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
        inspector = inspect(connection)
        assert "apple_mine_shift" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("apple_mine_shift")} == {
            "id",
            "user_id",
            "group_id",
            "card_id",
            "outcome",
            "item_code",
            "amount",
            "worked_at",
            "created_at",
        }
        with Operations.context(context):
            migration.downgrade()
        assert "apple_mine_shift" not in inspect(connection).get_table_names()
    engine.dispose()
