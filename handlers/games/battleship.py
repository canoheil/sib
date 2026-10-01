"""Telegram gameplay handlers for Battleship."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action, answer_invalid_game_callback

router = Router(name="game_battleship")


@router.callback_query(F.data.startswith("bs_action:"))
async def handle_bs_action(callback: CallbackQuery, bot: Bot):
    """Handle a player firing a shot in Battleship."""
    parts = (callback.data or "").split(":")
    try:
        if len(parts) != 4:
            raise ValueError
        match_id = int(parts[1])
        r = int(parts[2])
        c = int(parts[3])
    except (IndexError, ValueError):
        await answer_invalid_game_callback(callback)
        return

    await process_game_action(callback, bot, match_id, {"r": r, "c": c})
