"""Database models package."""

from sib.database.base import Base, TimestampMixin
from sib.database.models.user import User
from sib.database.models.group import Group
from sib.database.models.game import Game
from sib.database.models.match import Match, MatchState, MatchType
from sib.database.models.match_player import MatchPlayer
from sib.database.models.group_stats import GroupStats
from sib.database.models.sponsor_channel import SponsorChannel, ChannelType
from sib.database.models.party import Party, PartyMember, PartyStatus
from sib.database.models.matchmaking import MatchmakingQueue
from sib.database.models.player_stats import PlayerStats
from sib.database.models.player_xp import PlayerProgression, PlayerXPGrant
from sib.database.models.ad_campaign import AdCampaign, FrequencyMode
from sib.database.models.panel import PanelOwner
from sib.database.models.seedling import SeedlingInventory, SeedlingAward
from sib.database.models.asset import BotAsset, UserItem
from sib.database.models.apple import AppleWallet, AppleTransaction
from sib.database.models.garden import (
    Garden,
    GardenPlot,
    GardenWitherEvent,
    PlotState,
)
from sib.database.models.system_config import SystemConfig
from sib.database.models.player_card import PlayerCard, PlayerCardLegacyStats
from sib.database.models.shop import (
    SHOP_CARD_PRICE_APPLES,
    ShopEntry,
    ShopEntryType,
    ShopRotation,
)
from sib.database.models.trade import Trade, TradeStatus
from sib.database.models.apple_battle import (
    AppleBattle,
    AppleBattleQueueEntry,
    AppleBattleStatus,
)
from sib.database.models.faction import Faction
from sib.database.models.character import (
    Character,
    CharacterBalanceVersion,
    ReleaseStatus,
)
from sib.database.models.marriage import (
    FlowerGift,
    FlowerGiftStatus,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    canonical_spouse_ids,
)
from sib.database.models.friendship import AppleFriendship, FriendshipStatus
from sib.database.models.daily_chest import DailyChestClaim
from sib.database.models.daily_channel_post import DailyChannelPost
from sib.database.models.golden_hour_announcement import GoldenHourGroupAnnouncement
from sib.database.models.transfer import (
    PendingTransfer,
    PendingTransferState,
    TransferLog,
)
from sib.database.models.shop_chest import (
    ShopChest,
    ShopChestOpening,
    ShopChestReward,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "User",
    "Group",
    "Game",
    "Match",
    "MatchState",
    "MatchType",
    "MatchPlayer",
    "GroupStats",
    "SponsorChannel",
    "ChannelType",
    "Party",
    "PartyMember",
    "PartyStatus",
    "MatchmakingQueue",
    "PlayerStats",
    "PlayerProgression",
    "PlayerXPGrant",
    "AdCampaign",
    "FrequencyMode",
    "PanelOwner",
    "SeedlingInventory",
    "SeedlingAward",
    "BotAsset",
    "UserItem",
    "AppleWallet",
    "AppleTransaction",
    "Garden",
    "GardenPlot",
    "GardenWitherEvent",
    "PlotState",
    "SystemConfig",
    "Faction",
    "Character",
    "CharacterBalanceVersion",
    "ReleaseStatus",
    "PlayerCard",
    "PlayerCardLegacyStats",
    "ShopEntry",
    "ShopEntryType",
    "ShopRotation",
    "Trade",
    "TradeStatus",
    "AppleBattle",
    "AppleBattleQueueEntry",
    "AppleBattleStatus",
    "Marriage",
    "MarriageStatus",
    "MarriageProposal",
    "ProposalStatus",
    "FlowerGift",
    "FlowerGiftStatus",
    "canonical_spouse_ids",
    "AppleFriendship",
    "FriendshipStatus",
    "DailyChestClaim",
    "DailyChannelPost",
    "GoldenHourGroupAnnouncement",
    "TransferLog",
    "PendingTransfer",
    "PendingTransferState",
    "ShopChest",
    "ShopChestReward",
    "ShopChestOpening",
]

from sib.database.models.apple_duel import AppleDuel, AppleDuelSlot

__all__ += ["AppleDuel", "AppleDuelSlot"]

from sib.database.models.invasion_v2 import (
    Inv2Region,
    Inv2Invasion,
    Inv2Attack,
    Inv2Reward,
    Inv2GroupState,
)

__all__ += ["Inv2Region", "Inv2Invasion", "Inv2Attack", "Inv2Reward", "Inv2GroupState"]

from sib.database.models.invasion_v2_group_member import Inv2GroupMember

__all__ += ["Inv2GroupMember"]

from sib.database.models.invasion_v2_events import (
    Inv2EventClaim,
    Inv2EventEffect,
    Inv2RandomEvent,
)

__all__ += ["Inv2RandomEvent", "Inv2EventEffect", "Inv2EventClaim"]

from sib.database.models.admin_log import AdminLog
__all__ += ["AdminLog"]

from sib.database.models.apple_mine import AppleMineShift, UserPickaxe
__all__ += ["AppleMineShift", "UserPickaxe"]

from sib.database.models.castle import GroupCastle, CastleDonation, CastleIncomeClaim
__all__ += ["GroupCastle", "CastleDonation", "CastleIncomeClaim"]

from sib.database.models.war import GroupWar, WarParticipant, WarAttack
__all__ += ["GroupWar", "WarParticipant", "WarAttack"]

from sib.database.models.halloween import (
    HalloweenAttack,
    HalloweenEventState,
    HalloweenLootDrop,
    HalloweenLootDropClaim,
    HalloweenRewardClaim,
    HalloweenSeedCooldown,
)
__all__ += [
    "HalloweenEventState",
    "HalloweenAttack",
    "HalloweenRewardClaim",
    "HalloweenSeedCooldown",
    "HalloweenLootDrop",
    "HalloweenLootDropClaim",
]

from sib.database.models.reminder import UserReminderState
__all__ += ["UserReminderState"]

from sib.database.models.league import GroupLeagueWeek, GroupLeagueAward
__all__ += ["GroupLeagueWeek", "GroupLeagueAward"]
