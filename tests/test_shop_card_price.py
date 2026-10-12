"""Unified shop card pricing: EVERY card costs exactly 300 Apples.

Covers the whole rule end to end:

* the single price definition/accessor (:data:`SHOP_CARD_PRICE_APPLES`,
  ``ShopService.card_price()``) is 300 and takes no per-card input;
* every card in every rotation costs the same, including cards whose legacy
  ``characters.price`` / ``shop_entries.price_override`` columns still hold
  a historical value, and cards registered AFTER the rule was introduced;
* the debit, the ledger row and the screens all use that one value;
* the client cannot supply (or influence) a price;
* balance/ownership/idempotency validations are unchanged;
* the deprecated rarity column has no effect on pricing, screens or the
  purchase itself, and unrelated acquisition/economy paths are untouched.
"""

import inspect

import pytest
from sqlalchemy import select

from sib.database.models import (
    SHOP_CARD_PRICE_APPLES,
    AppleTransaction,
    Character,
    PlayerCard,
    ReleaseStatus,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService, render_character_card
from sib.services.player_cards import PlayerCardService
from sib.services.shop import ShopError, ShopService
from sib.tests.test_parties import create_test_user
from sib.tests.test_shop import _make_characters
from sib.tests.test_shop_sections import _callbacks, _labels, _run_handler, _sell_callback

EXPECTED_PRICE = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _buyer(db_session, apples: int = 10_000) -> int:
    user = await create_test_user(db_session, "خریدار قیمت یکسان")
    await AppleWalletService.earn(db_session, user.id, apples, reason="test_grant")
    await db_session.commit()
    return user.id


async def _set_legacy_prices(session, character_id: int, price: int) -> None:
    """Write a historical per-card price, exactly as an old row would hold it."""
    character = await session.get(Character, character_id)
    character.price = price
    await session.commit()


async def _shop_purchase_rows(session, user_id: int) -> list[AppleTransaction]:
    rows = await session.scalars(
        select(AppleTransaction)
        .where(
            AppleTransaction.user_id == user_id,
            AppleTransaction.reason == "shop_purchase",
        )
        .order_by(AppleTransaction.id.asc())
    )
    return list(rows.all())


async def _card_count(session, user_id: int, character_id: int) -> int:
    rows = await session.scalars(
        select(PlayerCard.id).where(
            PlayerCard.user_id == user_id, PlayerCard.character_id == character_id
        )
    )
    return len(list(rows.all()))


# ---------------------------------------------------------------------------
# 1. One definition, one value, no per-card input
# ---------------------------------------------------------------------------


def test_the_unified_card_price_is_exactly_300():
    assert SHOP_CARD_PRICE_APPLES == EXPECTED_PRICE
    assert ShopService.card_price() == EXPECTED_PRICE


def test_card_price_takes_no_entry_character_or_client_input():
    """The price cannot even *be* overridden: nothing is passed in."""
    params = inspect.signature(ShopService.card_price).parameters
    assert list(params) == []
    # The purchase API has no price/amount parameter either - it is derived
    # server-side from the single rule.
    purchase_params = inspect.signature(ShopService.purchase).parameters
    assert set(purchase_params) == {"session", "user_id", "entry_id", "nonce"}


def test_character_service_has_no_per_card_price_api():
    create_params = inspect.signature(CharacterService.create).parameters
    assert "price" not in create_params
    assert "rarity" not in create_params


async def test_new_cards_get_the_unified_price_without_configuration(db_session):
    character = await CharacterService.create(
        db_session, code="unified_new", name_fa="کارت تازه", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    # The legacy column is normalized for schema/backward compatibility...
    assert character.price == EXPECTED_PRICE
    # ...and a per-card price is not an editable field any more.
    from sib.services.characters import CharacterValidationError

    with pytest.raises(CharacterValidationError):
        await CharacterService.update(db_session, character.id, price=1)
    await db_session.rollback()


# ---------------------------------------------------------------------------
# 2. Every listed card costs 300 (identity/faction/stats/release date irrelevant)
# ---------------------------------------------------------------------------


async def test_every_card_in_the_rotation_costs_the_same_300(db_session):
    await _make_characters(db_session, 4)
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    assert entries
    for _entry, _character, _owned in entries:
        assert ShopService.card_price() == EXPECTED_PRICE


async def test_legacy_price_columns_cannot_change_the_price(db_session):
    """Same card sold at 3 different historical prices -> always 300."""
    chars = await _make_characters(db_session, 3)
    for legacy_price, character in zip((100, 120, 250), chars):
        await _set_legacy_prices(db_session, character.id, legacy_price)

    user_id = await _buyer(db_session)
    entries = await ShopService.list_entries(db_session, user_id=user_id)

    # A per-entry override (the other legacy pricing vector) must be ignored too.
    first_entry, _first_character, _ = entries[0]
    first_entry.price_override = 5
    await db_session.commit()

    for index, (entry, _character, _owned) in enumerate(entries):
        outcome = await ShopService.purchase(
            db_session, user_id, entry.id, nonce=f"legacyprices{index}"
        )
        await db_session.commit()
        assert outcome.replayed is False
        assert outcome.price == EXPECTED_PRICE

    assert await AppleWalletService.get_balance(db_session, user_id) == 10_000 - 3 * EXPECTED_PRICE
    ledger = await _shop_purchase_rows(db_session, user_id)
    assert [row.amount for row in ledger] == [-EXPECTED_PRICE] * 3


# ---------------------------------------------------------------------------
# 3. The debit is exactly 300 (and the ledger records the same number)
# ---------------------------------------------------------------------------


async def test_purchase_deducts_exactly_300_apples(db_session):
    await _make_characters(db_session, 1)
    user_id = await _buyer(db_session, apples=1000)
    entry, character, _owned = (await ShopService.list_entries(db_session))[0]

    outcome = await ShopService.purchase(db_session, user_id, entry.id, nonce="exactly30001")
    await db_session.commit()

    assert outcome.price == EXPECTED_PRICE
    assert await AppleWalletService.get_balance(db_session, user_id) == 1000 - EXPECTED_PRICE
    assert await _card_count(db_session, user_id, character.id) == 1

    (ledger_row,) = await _shop_purchase_rows(db_session, user_id)
    assert ledger_row.amount == -EXPECTED_PRICE
    assert ledger_row.balance_after == 1000 - EXPECTED_PRICE
    assert ledger_row.ref_type == "shop_entry"
    assert ledger_row.ref_id == entry.id


# ---------------------------------------------------------------------------
# 4. The shop UI shows 300 for every card (list and detail screens)
# ---------------------------------------------------------------------------


async def test_card_list_screen_shows_300_for_every_card(db_session):
    from sib.handlers.shop import _cards_view

    chars = await _make_characters(db_session, 3)
    for legacy_price, character in zip((100, 120, 500), chars):
        await _set_legacy_prices(db_session, character.id, legacy_price)
    user_id = await _buyer(db_session, apples=0)

    text, kb = await _cards_view(user_id)

    assert text.count(f"{EXPECTED_PRICE} سیب") == 3  # one line per card
    buy_labels = [label for label in _labels(kb) if label.startswith("🛒 خرید")]
    assert len(buy_labels) == 3
    assert all(f"({EXPECTED_PRICE} 🍎)" in label for label in buy_labels)


async def test_card_detail_screen_shows_300_even_with_a_legacy_override(db_session):
    from sib.handlers.shop import _shop_item_view

    chars = await _make_characters(db_session, 1)
    character = chars[0]
    await _set_legacy_prices(db_session, character.id, 7)
    user_id = await _buyer(db_session, apples=1000)
    entry, _character, _owned = (await ShopService.list_entries(db_session))[0]
    entry.price_override = 999
    await db_session.commit()

    text, kb, _photo = await _shop_item_view(user_id, entry.id)

    assert f"`{EXPECTED_PRICE}` سیب" in text  # card body (render_character_card)
    nonce_buttons = [c for c in _callbacks(kb) if c.startswith(f"shop_buy:{entry.id}:")]
    assert len(nonce_buttons) == 1
    assert f"({EXPECTED_PRICE} 🍎)" in next(
        label for label in _labels(kb) if label.startswith("✅ تأیید خرید")
    )


# ---------------------------------------------------------------------------
# 5. The client cannot supply or influence the price
# ---------------------------------------------------------------------------


async def test_forged_callback_with_a_price_segment_is_refused(db_session):
    """No callback format lets a client carry a price into the purchase."""
    from sib.handlers import shop as shop_handlers

    chars = await _make_characters(db_session, 1)
    user_id = await _buyer(db_session, apples=1000)
    entry_id = (await ShopService.list_entries(db_session))[0][0].id

    for forged in (
        f"shop_buy:{entry_id}:forgednonce1:1",      # extra (price-like) segment
        f"shop_buy:{entry_id}:,1",                  # nonce + injected price
        f"shop_buy:{entry_id}:1",                   # price instead of a nonce
        f"shop_buy:{entry_id}:forgednonce1:𝓅",      # unicode price
    ):
        cb = _sell_callback(user_id, forged)
        await _run_handler(shop_handlers.handle_shop_buy, cb)
        assert cb.answer.await_args.kwargs.get("show_alert") is True

    assert await AppleWalletService.get_balance(db_session, user_id) == 1000
    assert await _shop_purchase_rows(db_session, user_id) == []
    assert await _card_count(db_session, user_id, chars[0].id) == 0


async def test_a_purchase_callback_carries_only_an_entry_id_and_a_nonce(db_session):
    """The rendered buy button never contains a price the server might trust."""
    from sib.handlers.shop import _shop_item_view

    await _make_characters(db_session, 1)
    user_id = await _buyer(db_session, apples=1000)
    entry_id = (await ShopService.list_entries(db_session))[0][0].id
    await db_session.commit()  # the handler renders through its own session

    _text, kb, _photo = await _shop_item_view(user_id, entry_id)
    buy_callback = next(c for c in _callbacks(kb) if c.startswith("shop_buy:"))
    _head, callback_entry_id, nonce = buy_callback.split(":")
    assert callback_entry_id == str(entry_id)
    assert str(EXPECTED_PRICE) not in buy_callback
    assert nonce and not nonce.isdigit()


# ---------------------------------------------------------------------------
# 6. Existing validations keep working (balance, ownership, idempotency)
# ---------------------------------------------------------------------------


async def test_insufficient_balance_is_refused_before_anything_is_written(db_session):
    await _make_characters(db_session, 1)
    user_id = await _buyer(db_session, apples=EXPECTED_PRICE - 1)
    entry, character, _owned = (await ShopService.list_entries(db_session))[0]
    entry_id, character_id = entry.id, character.id
    await db_session.commit()

    with pytest.raises(ShopError) as err:
        await ShopService.purchase(db_session, user_id, entry_id, nonce="poorbuy00001")
    await db_session.rollback()

    assert str(EXPECTED_PRICE) in str(err.value)  # the refusal quotes the real price
    assert await AppleWalletService.get_balance(db_session, user_id) == EXPECTED_PRICE - 1
    assert await _card_count(db_session, user_id, character_id) == 0
    assert await _shop_purchase_rows(db_session, user_id) == []

    # Exactly enough Apples is enough.
    await AppleWalletService.earn(db_session, user_id, 1, reason="test_grant")
    await db_session.commit()
    outcome = await ShopService.purchase(db_session, user_id, entry_id, nonce="poorbuy00002")
    await db_session.commit()
    assert outcome.price == EXPECTED_PRICE
    assert await AppleWalletService.get_balance(db_session, user_id) == 0


async def test_duplicate_purchase_and_nonce_replay_are_unchanged(db_session):
    await _make_characters(db_session, 1)
    user_id = await _buyer(db_session, apples=2000)
    entry, character, _owned = (await ShopService.list_entries(db_session))[0]
    entry_id, character_id = entry.id, character.id

    first = await ShopService.purchase(db_session, user_id, entry_id, nonce="replayprice1")
    await db_session.commit()
    assert first.price == EXPECTED_PRICE

    # A new press cannot buy the same card twice at any price.
    with pytest.raises(ShopError):
        await ShopService.purchase(db_session, user_id, entry_id, nonce="replayprice2")
    await db_session.rollback()

    # Replaying the SAME press charges nothing and grants nothing.
    replay = await ShopService.purchase(db_session, user_id, entry_id, nonce="replayprice1")
    assert replay.replayed is True
    assert replay.price == EXPECTED_PRICE

    assert await AppleWalletService.get_balance(db_session, user_id) == 2000 - EXPECTED_PRICE
    assert await _card_count(db_session, user_id, character_id) == 1
    assert len(await _shop_purchase_rows(db_session, user_id)) == 1


# ---------------------------------------------------------------------------
# 7. Rarity: no effect on price, screens or the purchase itself
# ---------------------------------------------------------------------------


async def test_rarity_is_not_an_editable_field_any_more(db_session):
    from sib.services.characters import CharacterValidationError

    character = await CharacterService.create(
        db_session, code="no_rarity_edit", name_fa="بدون رتبه", power=20, speed=20, intelligence=20
    )
    await db_session.commit()

    with pytest.raises(CharacterValidationError):
        await CharacterService.update(db_session, character.id, rarity="LEGENDARY")
    await db_session.rollback()
    # Metadata edits (unrelated to rarity/pricing) still work.
    await CharacterService.update(db_session, character.id, name_en="No Rarity")
    await db_session.commit()
    assert character.name_en == "No Rarity"


def test_the_price_has_no_rarity_input_at_all():
    """There is no rarity tier to price by: the accessor takes no arguments
    and the model keeps the (deprecated, unread) column only as data."""
    assert list(inspect.signature(ShopService.card_price).parameters) == []
    from sib.database.models.shop import SHOP_CARD_PRICE_APPLES as FROM_MODELS

    assert FROM_MODELS == EXPECTED_PRICE


async def test_rarity_values_do_not_change_rendering_or_pricing(db_session):
    common = await CharacterService.create(
        db_session, code="rarity_common", name_fa="کارت مشترک", power=20, speed=20, intelligence=20
    )
    legendary = await CharacterService.create(
        db_session, code="rarity_legendary", name_fa="کارت مشترک", power=20, speed=20, intelligence=20
    )
    await db_session.commit()
    common.rarity = "COMMON"
    legendary.rarity = "LEGENDARY"
    await db_session.commit()

    # Identical cards -> identical presentation, including the price line.
    assert render_character_card(common) == render_character_card(legendary)
    assert f"`{EXPECTED_PRICE}` سیب" in render_character_card(legendary)
    for tier_label in ("کمیاب", "حماسی", "افسانه‌ای", "معمولی", "LEGENDARY", "COMMON"):
        assert tier_label not in render_character_card(legendary)


async def test_a_rare_flagged_card_is_still_bought_for_300_and_works(db_session):
    chars = await _make_characters(db_session, 1)
    character = chars[0]
    character.rarity = "LEGENDARY"
    await _set_legacy_prices(db_session, character.id, 5000)
    user_id = await _buyer(db_session, apples=1000)
    entry, _character, _owned = (await ShopService.list_entries(db_session))[0]

    outcome = await ShopService.purchase(db_session, user_id, entry.id, nonce="rarebuy00001")
    await db_session.commit()

    assert outcome.price == EXPECTED_PRICE
    assert await AppleWalletService.get_balance(db_session, user_id) == 1000 - EXPECTED_PRICE
    cards = await PlayerCardService.list_cards(db_session, user_id)
    assert [card.character_id for card in cards] == [character.id]
    assert cards[0].level == 1  # normal card functionality, untouched


# ---------------------------------------------------------------------------
# 8. Newly registered cards (future updates) follow the same rule
# ---------------------------------------------------------------------------


async def test_a_card_registered_after_the_rotation_opens_is_still_300(db_session):
    from datetime import datetime, timedelta, timezone

    await _make_characters(db_session, 1)
    await ShopService.current_rotation(db_session)
    await db_session.commit()

    # A brand-new card released later, with no price configuration anywhere.
    fresh = await CharacterService.create(
        db_session, code="future_card", name_fa="کارت آینده", power=20, speed=20, intelligence=20
    )
    await CharacterService.set_release_status(db_session, fresh.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    assert fresh.price == EXPECTED_PRICE

    # Next rotation offers it; the price rule needs no per-card setup.
    rotation = await ShopService.current_rotation(db_session)
    rotation.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    await ShopService.current_rotation(db_session)
    await db_session.commit()

    entries = await ShopService.list_entries(db_session)
    assert fresh.id in {character.id for _e, character, _o in entries}

    user_id = await _buyer(db_session, apples=1000)
    fresh_entry = next(
        entry for entry, character, _owned in entries if character.id == fresh.id
    )
    outcome = await ShopService.purchase(db_session, user_id, fresh_entry.id, nonce="futurecard1")
    await db_session.commit()

    assert outcome.price == EXPECTED_PRICE
    assert await AppleWalletService.get_balance(db_session, user_id) == 1000 - EXPECTED_PRICE


# ---------------------------------------------------------------------------
# 9. Unrelated economy / acquisition paths are untouched
# ---------------------------------------------------------------------------


async def test_free_acquisition_paths_are_unaffected(db_session):
    """Direct card acquisition (chests/rewards) still costs nothing."""
    chars = await _make_characters(db_session, 1)
    user_id = await _buyer(db_session, apples=0)

    card = await PlayerCardService.acquire(db_session, user_id, chars[0].id)
    await db_session.commit()

    assert card is not None
    assert await AppleWalletService.get_balance(db_session, user_id) == 0
    assert await _shop_purchase_rows(db_session, user_id) == []


async def test_other_shop_and_economy_prices_are_untouched():
    from sib.services.config_store import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS["item_buy_prices"]["magic_powder"] == 40
    assert CONFIG_DEFAULTS["item_buy_prices"]["wedding_ring"] == 1000
    assert CONFIG_DEFAULTS["item_buy_prices"]["flower"] == 30
    assert CONFIG_DEFAULTS["garden_plot_prices"] == [100, 250, 500, 1000, 2000]
    # The shop card price is a code rule, not a config key that could drift.
    assert "shop_card_price_apples" not in CONFIG_DEFAULTS


async def test_item_shop_still_uses_its_own_configured_prices(db_session):
    from sib.services.items import ItemService

    user_id = await _buyer(db_session, apples=100)

    total = await ItemService.buy(db_session, user_id, "magic_powder", 1)
    await db_session.commit()

    # Config-driven item price (40), unrelated to the unified card rule.
    assert total == 1
    assert await AppleWalletService.get_balance(db_session, user_id) == 60
    assert await ItemService.get_total(db_session, user_id, "magic_powder") == 1
