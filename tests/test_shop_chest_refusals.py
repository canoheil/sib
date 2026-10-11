"""Chest openings: refusals must leave nothing behind, even if the caller commits.

The defect this pins: the opening row used to be flushed as the idempotency
gate BEFORE the funds check and the debit. A caller that caught the refusal and
committed its outer transaction therefore kept a burned opening row (no reward,
no charge) for that operation key.

Two groups:

* Pre-write refusals (funds, card race): decided before the gate is written.
  These run for real on SQLite.
* Post-write refusal (debit refused after the gate): needs the savepoint branch
  that MariaDB uses. It is forced on here with a monkeypatch, so it checks the
  wiring on SQLite only. It is NOT evidence of MariaDB savepoint behaviour.
"""

import pytest
from sqlalchemy import func, select

from sib.database.connection import get_async_sessionmaker
from sib.database.models import AppleTransaction, ShopChestOpening
from sib.services import shop_chests as chests_module
from sib.services.apple_wallet import AppleWalletService, InsufficientFundsException
from sib.services.player_cards import CardError
from sib.services.shop_chests import (
    RewardSpec,
    ShopChestError,
    ShopChestInsufficientFundsError,
    ShopChestService,
)
from sib.tests.test_shop import _make_characters
from sib.tests.test_shop_chests import FixedRng, _chest, _user_with_apples

KEY = "chest-refuse-0001"


async def _state(user_id: int) -> dict:
    async with get_async_sessionmaker()() as session:
        openings = await session.scalar(
            select(func.count(ShopChestOpening.id)).where(ShopChestOpening.user_id == user_id)
        )
        charges = await session.scalar(
            select(func.count(AppleTransaction.id)).where(
                AppleTransaction.user_id == user_id,
                AppleTransaction.reason == "shop_chest_purchase",
            )
        )
        return {
            "openings": openings,
            "charges": charges,
            "balance": await AppleWalletService.get_balance(session, user_id),
        }


@pytest.mark.asyncio
async def test_funds_refusal_then_commit_leaves_no_opening_row(db_session):
    user_id = await _user_with_apples(db_session, 10)
    chest = await _chest(db_session, code="refuse_funds", price=50)

    async with get_async_sessionmaker()() as session:
        with pytest.raises(ShopChestInsufficientFundsError):
            await ShopChestService.open_chest(session, user_id, chest.id, KEY, rng=FixedRng([0]))
        # The caller swallows the refusal and commits its own transaction.
        await session.commit()

    assert await _state(user_id) == {"openings": 0, "charges": 0, "balance": 10}

    # The key was never burned: once funds exist, the same key opens normally.
    async with get_async_sessionmaker()() as session:
        await AppleWalletService.earn(session, user_id, 100, reason="test_topup")
        await session.commit()
    result = await ShopChestService.open_chest_reliably(user_id, chest.id, KEY, rng=FixedRng([0]))
    assert result.replayed is False
    assert (await _state(user_id))["openings"] == 1


@pytest.mark.asyncio
async def test_card_race_refusal_then_commit_leaves_no_opening_row(db_session, monkeypatch):
    from sib.services.player_cards import PlayerCardService

    chars = await _make_characters(db_session, 1)
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(
        db_session,
        code="refuse_card",
        price=20,
        rewards=[RewardSpec(kind="card", weight=1, character_id=chars[0].id)],
    )

    async def lost_race(*_args, **_kwargs):
        raise CardError("شخصیت دیگر در دسترس نیست.")

    monkeypatch.setattr(PlayerCardService, "acquire", lost_race)

    async with get_async_sessionmaker()() as session:
        with pytest.raises(ShopChestError):
            await ShopChestService.open_chest(session, user_id, chest.id, KEY, rng=FixedRng([0]))
        await session.commit()

    assert await _state(user_id) == {"openings": 0, "charges": 0, "balance": 100}


@pytest.mark.asyncio
async def test_post_write_refusal_then_commit_undoes_gate_on_savepoint_branch(db_session, monkeypatch):
    """Savepoint branch forced on (logic check on SQLite, not MariaDB evidence).

    The gate row is flushed inside the savepoint, then the debit is refused. The
    savepoint must roll the gate back, so a commit after the catch leaves nothing.
    """
    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, code="refuse_debit", price=20, rewards=[RewardSpec(kind="apples", weight=1, apples=7)])

    monkeypatch.setattr(chests_module, "savepoints_supported", lambda _session: True)

    async def refuse(*_args, **_kwargs):
        raise InsufficientFundsException()

    monkeypatch.setattr(AppleWalletService, "debit_once", refuse)

    async with get_async_sessionmaker()() as session:
        with pytest.raises(ShopChestInsufficientFundsError):
            await ShopChestService.open_chest(session, user_id, chest.id, KEY, rng=FixedRng([0]))
        await session.commit()

    assert await _state(user_id) == {"openings": 0, "charges": 0, "balance": 100}


@pytest.mark.asyncio
async def test_unexpected_error_gets_neutral_alert_not_success(db_session, monkeypatch):
    """An unexpected failure must not say the chest opened."""
    from sib.handlers import shop as shop_handlers
    from sib.tests.test_shop_sections import _run_handler, _sell_callback

    user_id = await _user_with_apples(db_session, 100)
    chest = await _chest(db_session, code="boom_chest", price=20)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(ShopChestService, "open_chest_reliably", boom)

    cb = _sell_callback(user_id, f"shop_chest_open:{chest.id}:{chest.version}:chest-boom-0001")
    edit, _banner = await _run_handler(shop_handlers.handle_shop_chest_open, cb)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.answer.await_args
    assert args[0] == shop_handlers.CHEST_UNKNOWN_OUTCOME_TEXT
    assert "باز شد" not in args[0]
    assert kwargs.get("show_alert") is True
    edit.assert_not_awaited()  # no opened-chest screen
    assert await _state(user_id) == {"openings": 0, "charges": 0, "balance": 100}
