"""Handlers package."""

from aiogram import Dispatcher
from sib.handlers.start import router as start_router
from sib.handlers.admin import router as admin_router
from sib.handlers.errors import router as errors_router
from sib.handlers.party import party_router
from sib.handlers.matchmaking import matchmaking_router
from sib.handlers.games import (
    rps_router,
    high_card_router,
    tic_tac_toe_router,
    connect_four_router,
    battleship_router,
    ads_router,
    game_common_router,
)
from sib.handlers.leaderboard import router as leaderboard_router


def register_all_handlers(dp: Dispatcher) -> None:
    """Register all modular routers to the main dispatcher."""
    dp.include_router(errors_router)
    dp.include_router(admin_router)
    dp.include_router(leaderboard_router)
    dp.include_router(matchmaking_router)
    dp.include_router(rps_router)
    dp.include_router(high_card_router)
    dp.include_router(tic_tac_toe_router)
    dp.include_router(connect_four_router)
    dp.include_router(battleship_router)
    dp.include_router(ads_router)
    dp.include_router(game_common_router)
    dp.include_router(party_router)
    dp.include_router(start_router)


__all__ = [
    "register_all_handlers",
    "start_router",
    "admin_router",
    "errors_router",
    "party_router",
    "matchmaking_router",
    "rps_router",
    "high_card_router",
    "tic_tac_toe_router",
    "connect_four_router",
    "battleship_router",
    "ads_router",
    "leaderboard_router",
]
