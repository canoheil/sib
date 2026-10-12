"""Regression tests for verified transaction-safety defects.

Labels follow the transaction-safety audit (F-numbers) as used in the
implementation report:

* A (audit F2) - item purchase debited apples, then the grant failed and the
  handler committed the debit anyway.
* B (audit F3) - shop card purchase debited apples, then the card acquisition
  lost a race and the handler committed the debit anyway.
* C (audit F1) - a card bought, traded away and bought again replayed the
  permanent ``shop:{entry}:{user}`` key: the second purchase was free.
* D (audit F4) - a consumed wallet idempotency key silently "succeeded" for a
  new purchase, and a race inside the keyed debit rolled back the caller's
  whole transaction.
* E (audit F8) - a daily-chest claim retry rolled back the caller's session,
  discarding the user row the caller had just created.

Handler tests drive the real callbacks through ``get_session()`` so the
commit/rollback behaviour of the production path is what is being tested.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from sib.database.models import AppleTransaction, AppleWallet, PlayerCard, User
from sib.services.apple_wallet import AppleWalletService
from sib.services.items import ItemService
from sib.services.player_cards import DuplicateCardError, PlayerCardService
from sib.services.shop import ShopService
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters
from sib.tests.test_shop_sections import _run_handler, _sell_callback


async def _balance(session, user_id: int) -> int:
    """Fresh balance: re-read the row, never a stale identity-map copy."""
    wallet = await session.scalar(
        select(AppleWallet)
        .where(AppleWallet.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return wallet.balance if wallet else 0


async def _ledger_reasons(session, user_id: int) -> list[str]:
    rows = (
        await session.scalars(
            select(AppleTransaction)
            .where(AppleTransaction.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [row.reason for row in rows]


async def _owned_character_ids(session, user_id: int) -> list[int]:
    rows = (
        await session.scalars(
            select(PlayerCard)
            .where(PlayerCard.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    return [row.character_id for row in rows]


async def _first_entry(session, user_id: int):
    entries = await ShopService.list_entries(session, user_id=user_id)
    await session.commit()  # the rotation must be visible to handler sessions
    return entries[0]


# ---------------------------------------------------------------------------
# A (F2): item purchase must not keep a debit when the grant fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A_failed_item_grant_does_not_keep_the_debit(db_session, monkeypatch):
    from sib.handlers import shop as shop_handlers

    user = await create_test_user(db_session, "خریدار آیتم")
    await AppleWalletService.earn(db_session, user.id, 100, reason="seed")
    await db_session.commit()

    # Config prices a key the catalog does not know: the grant must refuse it.
    async def ghost_price(session, item_key):
        return 5 if item_key == "ghost_item" else None

    monkeypatch.setattr(ItemService, "buy_price", staticmethod(ghost_price))

    cb = _sell_callback(user.id, "shop_good_buy:ghost_item")
    await _run_handler(shop_handlers.handle_shop_good_buy, cb)

    assert await _balance(db_session, user.id) == 100
    assert "item_purchase" not in await _ledger_reasons(db_session, user.id)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


# ---------------------------------------------------------------------------
# B (F3): a lost card race must not keep the shop debit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B_duplicate_card_race_does_not_keep_the_shop_debit(db_session, monkeypatch):
    from sib.handlers import shop as shop_handlers

    await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "خریدار مسابقه")
    await AppleWalletService.earn(db_session, user.id, 500, reason="seed")
    await db_session.commit()
    entry, _character, _owned = await _first_entry(db_session, user.id)

    # Simulate a concurrent grant of the same character winning the race.
    monkeypatch.setattr(
        PlayerCardService,
        "acquire",
        AsyncMock(side_effect=DuplicateCardError("duplicate")),
    )

    cb = _sell_callback(user.id, f"shop_buy:{entry.id}:racenonce1")
    await _run_handler(shop_handlers.handle_shop_buy, cb)

    assert await _balance(db_session, user.id) == 500
    assert "shop_purchase" not in await _ledger_reasons(db_session, user.id)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


# ---------------------------------------------------------------------------
# C (F1): re-buying a traded card must cost apples; replays must not regrant
# ---------------------------------------------------------------------------


async def _trade_card_away(session, user_id: int, character_id: int) -> None:
    """Move the card to another owner, as an accepted trade does."""
    other = await create_test_user(session, "طرف معامله")
    await session.execute(
        update(PlayerCard)
        .where(PlayerCard.user_id == user_id, PlayerCard.character_id == character_id)
        .values(user_id=other.id)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_C_rebuying_a_traded_card_is_charged_at_full_price(db_session):
    from sib.handlers import shop as shop_handlers

    await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "خریدار مجدد")
    await AppleWalletService.earn(db_session, user.id, 1000, reason="seed")
    await db_session.commit()
    entry, character, _owned = await _first_entry(db_session, user.id)
    price = ShopService.card_price()

    await _run_handler(
        shop_handlers.handle_shop_buy,
        _sell_callback(user.id, f"shop_buy:{entry.id}:firstbuy01"),
    )
    assert await _balance(db_session, user.id) == 1000 - price

    await _trade_card_away(db_session, user.id, character.id)

    # A brand-new purchase (fresh nonce from a freshly rendered item page).
    await _run_handler(
        shop_handlers.handle_shop_buy,
        _sell_callback(user.id, f"shop_buy:{entry.id}:secondbuy2"),
    )

    assert await _balance(db_session, user.id) == 1000 - 2 * price
    assert await _owned_character_ids(db_session, user.id) == [character.id]


@pytest.mark.asyncio
async def test_C_consumed_nonce_never_authorizes_a_new_purchase(db_session):
    from sib.handlers import shop as shop_handlers

    await _make_characters(db_session, 1)
    user = await create_test_user(db_session, "دکمه کهنه")
    await AppleWalletService.earn(db_session, user.id, 1000, reason="seed")
    await db_session.commit()
    entry, character, _owned = await _first_entry(db_session, user.id)
    price = ShopService.card_price()

    await _run_handler(
        shop_handlers.handle_shop_buy,
        _sell_callback(user.id, f"shop_buy:{entry.id}:stalenonce1"),
    )
    await _trade_card_away(db_session, user.id, character.id)

    # The same (old) button pressed again must not charge or grant anything.
    cb = _sell_callback(user.id, f"shop_buy:{entry.id}:stalenonce1")
    await _run_handler(shop_handlers.handle_shop_buy, cb)

    assert await _balance(db_session, user.id) == 1000 - price
    assert await _owned_character_ids(db_session, user.id) == []


# ---------------------------------------------------------------------------
# D (F4): consumed keys never succeed silently; keyed races keep caller work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_D_keyed_spend_replay_is_refused_not_silently_accepted(db_session):
    from sib.services.apple_wallet import LedgerKeyReplayError

    user = await create_test_user(db_session, "کلید مصرف‌شده")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 50, reason="seed")
    await AppleWalletService.spend(
        db_session, uid, 10, reason="buy", idempotency_key="buy:d1"
    )
    await db_session.commit()

    with pytest.raises(LedgerKeyReplayError):
        await AppleWalletService.spend(
            db_session, uid, 10, reason="buy", idempotency_key="buy:d1"
        )
    await db_session.rollback()
    assert await _balance(db_session, uid) == 40


@pytest.mark.asyncio
async def test_D_consumed_garden_unlock_key_cannot_grant_a_free_plot(db_session):
    from sib.services.garden import GardenService

    user = await create_test_user(db_session, "باغبان")
    uid = user.id
    garden = await GardenService.get_or_create(db_session, uid)
    index = garden.capacity_plots
    price = await GardenService.next_unlock_price(db_session, garden)
    # The unlock for this plot index was already paid for and its key consumed
    # (same reason and reference as the real unlock).
    await AppleWalletService.earn(db_session, uid, price, reason="seed")
    await AppleWalletService.spend(
        db_session,
        uid,
        price,
        reason="garden_plot_unlock",
        ref_type="garden",
        ref_id=uid,
        idempotency_key=f"garden_unlock:{uid}:{index}",
    )
    await db_session.commit()

    # Give the garden back a lower capacity (as after a reset) and no apples.
    garden.capacity_plots = index
    await db_session.commit()
    assert await _balance(db_session, uid) == 0

    with pytest.raises(ValueError):
        await GardenService.unlock_plot(db_session, uid)
    await db_session.rollback()

    garden = await GardenService.get_or_create(db_session, uid)
    assert garden.capacity_plots == index


@pytest.mark.asyncio
async def test_D_keyed_debit_race_keeps_the_callers_transaction(db_session, monkeypatch):
    from sib.services import apple_wallet as wallet_module
    from sib.services.apple_wallet import AppleWalletService as Wallet
    from sib.services.apple_wallet import LedgerKeyReplayError

    user = await create_test_user(db_session, "رقابت کلید")
    uid = user.id
    await AppleWalletService.earn(db_session, uid, 50, reason="seed")
    await db_session.commit()

    # Another transaction commits the debit for this key first.
    await AppleWalletService.spend(
        db_session, uid, 10, reason="buy", idempotency_key="race:d3"
    )
    await db_session.commit()

    real_find = Wallet._find_ledger

    async def stale_precheck(session, key, *, lock=False):
        if not lock:
            return None  # our snapshot predates the other transaction's commit
        return await real_find(session, key, lock=True)

    monkeypatch.setattr(Wallet, "_find_ledger", staticmethod(stale_precheck))
    # The race branch uses a savepoint. Begin the transaction with a caller
    # write first (as every real caller does), so the savepoint is nested
    # correctly on the SQLite test engine too.
    monkeypatch.setattr(wallet_module, "savepoints_supported", lambda _session: True)

    user.first_name = "caller work survives"  # caller-owned write
    await db_session.flush()
    with pytest.raises(LedgerKeyReplayError):
        await AppleWalletService.spend(
            db_session, uid, 10, reason="buy", idempotency_key="race:d3"
        )
    await db_session.commit()

    await db_session.refresh(user)
    assert user.first_name == "caller work survives"
    assert await _balance(db_session, uid) == 40


# ---------------------------------------------------------------------------
# E (F8): a daily-chest claim retry must not discard the caller's user row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_E_daily_chest_retry_keeps_the_callers_user_row(db_session, monkeypatch):
    from sib.services.daily_chest import DailyChestService

    uid = 424242
    # Same shape as handle_chest_open: the caller creates the user row in its
    # session and only then claims. The row is flushed, not yet committed.
    db_session.add(User(id=uid, first_name="تازه‌وارد", username="newcomer_424242"))
    await db_session.flush()

    real_claim_once = DailyChestService._claim_once
    attempts = {"n": 0}

    async def flaky_claim_once(session, user_id, config, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise IntegrityError("injected race", {}, RuntimeError("lost race"))
        return await real_claim_once.__func__(
            DailyChestService, session, user_id, config, **kwargs
        )

    monkeypatch.setattr(DailyChestService, "_claim_once", staticmethod(flaky_claim_once))

    import random

    result = await DailyChestService.claim(
        db_session, uid, rng=random.Random(7)
    )
    await db_session.commit()

    assert attempts["n"] == 2
    assert result.status == "claimed"
    survivor = await db_session.get(User, uid)
    assert survivor is not None
    assert survivor.first_name == "تازه‌وارد"
