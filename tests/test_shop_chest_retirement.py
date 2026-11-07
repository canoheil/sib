"""Retire sales without deleting definitions, owned rewards or replayable history."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from sib.database.connection import seed_characters
from sib.database.models import ShopChest, ShopChestOpening, ShopChestPity, UserItem
from sib.handlers import shop
from sib.services.apple_wallet import AppleWalletService
from sib.services.items import ItemService
from sib.services.shop_chests import (
    RETIRED_CHEST_CODES,
    ShopChestInactiveError,
    ShopChestService,
)
from sib.tests.test_shop_chests import FixedRng, _chest, _ledger, _user_with_apples

LEGACY_CHESTS = {
    "sages_chest": "صندوق فرزانگان",
    "empire_chest": "صندوق امپراتوری",
    "moonlight_chest": "صندوق مهتاب",
    "green_apple_chest": "صندوق سیبی",
}


def test_only_the_four_requested_legacy_codes_are_retired():
    assert RETIRED_CHEST_CODES == set(LEGACY_CHESTS)


async def test_menu_hides_active_legacy_rows_but_keeps_tiers_and_the_garden(db_session):
    await seed_characters(db_session)
    await ShopChestService.seed_tiers(db_session)
    legacy = [await _chest(db_session, code=code, name=name)
              for code, name in LEGACY_CHESTS.items()]
    garden = await _chest(db_session, code="enchanted_garden_chest", name="صندوق باغ افسون")
    user_id = await _user_with_apples(db_session, 20_000)

    visible = await ShopChestService.list_active(db_session)
    assert {chest.code for chest in visible} == {
        "bronze_chest", "silver_chest", "gold_chest", "diamond_chest", garden.code,
    }
    text, keyboard = await shop._chests_view(user_id)
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    for chest in legacy:
        assert chest.name not in text
        assert f"shop_chest:{chest.id}" not in callbacks
        assert await shop._chest_detail_view(user_id, chest.id) is None
        assert chest.is_active  # Stored rows/settings were not deleted or rewritten.
    for chest in visible:
        assert chest.name in text and f"shop_chest:{chest.id}" in callbacks
    assert await db_session.scalar(select(func.count(ShopChest.id))) == 9


@pytest.mark.parametrize("code", LEGACY_CHESTS)
async def test_new_opens_and_old_buttons_refuse_without_touching_owned_inventory(
    db_session, monkeypatch, code
):
    user_id = await _user_with_apples(db_session, 20_000)
    chest = await _chest(db_session, code=code, name=LEGACY_CHESTS[code])
    db_session.add(UserItem(user_id=user_id, item_key=code, quantity=2))
    db_session.add(ShopChestPity(user_id=user_id, pity_counter=9))
    await db_session.commit()
    ledger_before = await _ledger(db_session, user_id)
    rewards_before = await ShopChestService.load_rewards(db_session, chest.id)

    # Even a caller swallowing the refusal and committing must persist no sale.
    with pytest.raises(ShopChestInactiveError):
        await ShopChestService.open_chest(
            db_session, user_id, chest.id, "retired-api-0001",
            expected_version=chest.version, rng=FixedRng([]),
        )
    await db_session.commit()

    answer, edit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(shop, "_shop_actor", AsyncMock(return_value=user_id))
    monkeypatch.setattr(shop, "safe_answer_callback", answer)
    monkeypatch.setattr(shop, "safe_edit_callback_message", edit)
    callback = SimpleNamespace(data=f"shop_chest_open:{chest.id}:{chest.version}:retired-press-01")
    await shop.handle_shop_chest_open(callback, AsyncMock())
    assert answer.await_args.kwargs["show_alert"] is True
    assert "برای فروش نیست" in answer.await_args.args[1]
    edit.assert_not_awaited()

    assert await AppleWalletService.get_balance(db_session, user_id) == 20_000
    assert await _ledger(db_session, user_id) == ledger_before
    assert await ShopChestService.pity_counter(db_session, user_id) == 9
    assert await ItemService.get_total(db_session, user_id, code) == 2
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 0
    assert await ShopChestService.load_rewards(db_session, chest.id) == rewards_before
    assert (await ShopChestService.get_by_code(db_session, code)).is_active


@pytest.mark.parametrize("code", LEGACY_CHESTS)
async def test_completed_legacy_opening_still_replays_after_retirement(db_session, code):
    user_id = await _user_with_apples(db_session, 20_000)
    # Simulate an opening committed before its definition became retired.
    chest = await _chest(db_session, code="previously_available_chest")
    original = await ShopChestService.open_chest_reliably(
        user_id, chest.id, "historical-open-01", rng=FixedRng([0]),
    )
    chest.code, chest.name = code, LEGACY_CHESTS[code]
    await db_session.commit()
    ledger_before = await _ledger(db_session, user_id)
    counter_before = await ShopChestService.pity_counter(db_session, user_id)
    await db_session.commit()

    replay = await ShopChestService.open_chest_reliably(
        user_id, chest.id, original.operation_key, rng=FixedRng([]),
    )
    assert replay.replayed and replay.opening_id == original.opening_id
    assert replay.chest_code == code and replay.balance_after == original.balance_after
    assert replay.apples_awarded == original.apples_awarded
    assert await _ledger(db_session, user_id) == ledger_before
    assert await ShopChestService.pity_counter(db_session, user_id) == counter_before
    assert await db_session.scalar(select(func.count(ShopChestOpening.id))) == 1
    with pytest.raises(ShopChestInactiveError):
        await ShopChestService.open_chest_reliably(
            user_id, chest.id, "fresh-retired-01", rng=FixedRng([]),
        )
