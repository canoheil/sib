"""Telegram gameplay handlers for Connect Four."""

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery
from sib.handlers.games.common import process_game_action, answer_invalid_game_callback

router = Router(name="game_connect_four")


@router.callback_query(F.data.startswith("c4_action:"))
async def handle_c4_action(callback: CallbackQuery, bot: Bot):
    """Handle a player dropping a piece in Connect Four."""
    parts = (callback.data or "").split(":")
    try:
        if len(parts) != 3:
            raise ValueError
        match_id = int(parts[1])
        col = int(parts[2])
    except (IndexError, ValueError):
        await answer_invalid_game_callback(callback)
        return

    await process_game_action(callback, bot, match_id, {"col": col})
