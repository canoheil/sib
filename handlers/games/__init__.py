"""Games handlers package."""

from sib.handlers.games.rps import router as rps_router
from sib.handlers.games.high_card import router as high_card_router
from sib.handlers.games.tic_tac_toe import router as tic_tac_toe_router
from sib.handlers.games.connect_four import router as connect_four_router
from sib.handlers.games.battleship import router as battleship_router
from sib.handlers.games.ads import router as ads_router

__all__ = [
    "rps_router",
    "high_card_router",
    "tic_tac_toe_router",
    "connect_four_router",
    "battleship_router",
    "ads_router",
]
