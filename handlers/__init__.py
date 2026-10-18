"""Handlers package."""

from aiogram import Dispatcher
from sib.handlers.start import router as start_router
from sib.handlers.group_lifecycle import router as group_lifecycle_router
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
from sib.handlers.garden import router as garden_router
from sib.handlers.wiki import router as wiki_router
from sib.handlers.shop import router as shop_router
from sib.handlers.cards import router as cards_router
from sib.handlers.trading import router as trading_router
from sib.handlers.apple_duel import router as apple_duel_router
from sib.handlers.apple_battle import router as apple_battle_router
from sib.handlers.admin_meta import router as admin_meta_router
from sib.handlers.assets import router as assets_router
from sib.handlers.admin_center import router as admin_center_router
from sib.handlers.friendly import router as friendly_router
from sib.handlers.marriage import router as marriage_router
from sib.handlers.friendship import router as friendship_router
from sib.handlers.inv2_join import router as inv2_join_router
from sib.handlers.inv2_admin import router as inv2_admin_router
from sib.handlers.inv2_controls import router as inv2_controls_router
from sib.handlers.inv2_dashboard import router as inv2_dashboard_router
from sib.handlers.sib_hug import router as sib_hug_router
from sib.handlers.sib_kiss import router as sib_kiss_router
from sib.handlers.castle import router as castle_router
from sib.handlers.daily_chest import router as daily_chest_router
from sib.handlers.apple_mine import router as apple_mine_router


def register_all_handlers(dp: Dispatcher) -> None:
    """Register all modular routers to the main dispatcher."""
    dp.include_router(errors_router)
    from sib.handlers.admin_broadcast import router as broadcast_router
    from sib.handlers.admin_users import router as users_admin_router
    dp.include_router(users_admin_router)
    dp.include_router(broadcast_router)
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
    dp.include_router(garden_router)
    dp.include_router(wiki_router)
    dp.include_router(shop_router)
    dp.include_router(cards_router)
    dp.include_router(trading_router)
    dp.include_router(apple_duel_router)
    dp.include_router(apple_battle_router)
    dp.include_router(admin_meta_router)
    dp.include_router(assets_router)
    dp.include_router(admin_center_router)
    dp.include_router(party_router)
    dp.include_router(friendly_router)
    dp.include_router(marriage_router)
    dp.include_router(friendship_router)
    dp.include_router(sib_hug_router)
    dp.include_router(sib_kiss_router)
    dp.include_router(daily_chest_router)
    dp.include_router(apple_mine_router)
    dp.include_router(inv2_controls_router)
    dp.include_router(inv2_dashboard_router)
    dp.include_router(inv2_admin_router)
    dp.include_router(inv2_join_router)
    dp.include_router(group_lifecycle_router)
    dp.include_router(castle_router)
    dp.include_router(start_router)


__all__ = [
    "register_all_handlers",
    "start_router",
    "group_lifecycle_router",
    "admin_router",
    "errors_router",
    "party_router",
    "friendly_router",
    "marriage_router",
    "friendship_router",
    "matchmaking_router",
    "rps_router",
    "high_card_router",
    "tic_tac_toe_router",
    "connect_four_router",
    "battleship_router",
    "ads_router",
    "leaderboard_router",
    "apple_mine_router",
]
