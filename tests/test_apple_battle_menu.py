"""AB1: hide the battle leaderboard only on the card-selection screen."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sib.handlers import apple_battle


@pytest.mark.parametrize("card_count", [1, 3, 11])
@pytest.mark.parametrize("origin", [None, "group_games_panel"])
async def test_apple_battle_menu_has_no_battle_leaderboard_button(
    monkeypatch, card_count, origin
):
    cards = [
        SimpleNamespace(id=i, character_id=i, level=i + 1)
        for i in range(1, card_count + 1)
    ]
    session = AsyncMock()
    session.scalar.return_value = 7
    context = MagicMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(apple_battle, "get_session", lambda: context)
    monkeypatch.setattr(
        apple_battle.PlayerCardService, "list_cards", AsyncMock(return_value=cards)
    )
    monkeypatch.setattr(
        apple_battle, "_card_names",
        AsyncMock(return_value={card.id: f"قهرمان {card.id}" for card in cards}),
    )

    text, keyboard = await apple_battle._select_view(123, origin=origin)

    assert "🍎 **نبرد سیبی**" in text
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert all(button.text != "🏆 لیدربورد نبرد" for button in buttons)
    assert all(button.callback_data != "ab_board" for button in buttons)
    expected = [
        [(f"⚔️ قهرمان {card.id} (سطح {card.level})", f"ab_pick:{card.id}")]
        for card in cards[:10]
    ]
    expected += [
        [("🔄 بروزرسانی", "ab_refresh:group_games_panel" if origin else "ab_refresh")],
        [("↩️ بازگشت به بازی‌ها", "group_games_panel")]
        if origin else [("↩️ بازگشت به منوی اصلی", "main_menu")],
    ]
    assert [
        [(button.text, button.callback_data) for button in row]
        for row in keyboard.inline_keyboard
    ] == expected


async def test_apple_battle_ready_menu_keeps_battle_leaderboard_button(monkeypatch):
    """The other menu must retain its separate battle leaderboard entry."""
    card = SimpleNamespace(character_id=10, level=2)
    session = AsyncMock()
    session.get.side_effect = [card, SimpleNamespace(name_fa="قهرمان")]
    context = MagicMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(apple_battle, "get_session", lambda: context)
    monkeypatch.setattr(
        apple_battle.AppleBattleService, "get_loadout",
        AsyncMock(return_value=SimpleNamespace(card_id=1)),
    )
    monkeypatch.setattr(
        apple_battle.AppleBattleService, "_card_stats",
        lambda *_: SimpleNamespace(power=20, speed=18, intelligence=16),
    )

    _, keyboard = await apple_battle._ready_view(123, origin="group_games_panel")

    assert [
        [button.callback_data for button in row]
        for row in keyboard.inline_keyboard
    ] == [["ab_find"], ["ab_change:group_games_panel"], ["ab_board"], ["group_games_panel"]]
    assert keyboard.inline_keyboard[2][0].text == "🏆 لیدربورد نبرد"
