"""Transactional inv2 reward grants and idempotent region/group settlement."""
import random
from datetime import datetime, timezone

from sqlalchemy import func, select

from sib.database.models import Inv2Attack, Inv2GroupState, Inv2Invasion, Inv2Region, Inv2Reward
from sib.services.items import ItemService
from sib.services.lock_order import lock_user_rows_in_order
from sib.services.seedling import SeedlingService
from .enums import InvasionStatus, RewardType
from .rewards import roll_rewards

ITEM_KEYS = {
    RewardType.GOLDEN_APPLE: "apple_gold",
    RewardType.DIAMOND_APPLE: "apple_diamond",
    RewardType.OBSIDIAN_APPLE: "apple_obsidian",
}


async def grant_victory_rewards(session, invasion_id: int) -> None:
    """One transaction, one invasion-row mutex. Caller commits or rolls back.

    The guaranteed gold record is the completion marker per attacker; each
    user's rolls/inventory/ledger entries commit together with that marker.
    """
    invasion = await session.scalar(select(Inv2Invasion).where(
        Inv2Invasion.id == invasion_id).with_for_update().execution_options(populate_existing=True))
    if invasion is None or invasion.status != InvasionStatus.VICTORY.value:
        return
    attackers = list((await session.scalars(select(Inv2Attack.user_id).where(
        Inv2Attack.invasion_id == invasion_id).order_by(Inv2Attack.user_id))).all())
    await lock_user_rows_in_order(session, attackers)
    for user_id in attackers:
        recorded = await session.scalar(select(Inv2Reward.id).where(
            Inv2Reward.invasion_id == invasion_id, Inv2Reward.user_id == user_id).limit(1))
        if recorded is not None:
            continue
        for kind, amount in roll_rewards(random.Random()):
            if kind == RewardType.SEEDLING:
                await SeedlingService.award_invasion_victory(
                    session, user_id, amount, invasion_id=invasion_id)
            else:
                await ItemService.grant(session, user_id, ITEM_KEYS[kind], amount)
            session.add(Inv2Reward(invasion_id=invasion_id, user_id=user_id,
                reward_type=kind.value, reward_amount=amount, granted_at=datetime.now(timezone.utc)))
        await session.flush()


async def settle_outcome(session, invasion_id: int, status: str) -> list:
    """Derive counters from terminal invasions, never increment on a retry.

    Returns the list of garden-corruption results (G2) so the post-commit
    caller can DM the affected players; an empty list means no withering.
    """
    if status not in (
        InvasionStatus.VICTORY.value,
        InvasionStatus.DEFEAT.value,
        InvasionStatus.EXPIRED.value,
    ):
        return []
    inv = await session.scalar(select(Inv2Invasion).where(
        Inv2Invasion.id == invasion_id).with_for_update().execution_options(populate_existing=True))
    if inv is None or inv.status != status:
        return []
    corruption_results: list = []
    if status == InvasionStatus.VICTORY.value:
        await grant_victory_rewards(session, invasion_id)
    elif status == InvasionStatus.DEFEAT.value or (
        # G2: an EXPIRED invasion only corrupts gardens when it was actually
        # announced — players must have had a join button. Never-announced
        # PENDING->EXPIRED rows and startup-recovery expirations are exempt.
        status == InvasionStatus.EXPIRED.value and inv.started_at is not None
    ):
        from sib.services.garden import GardenService

        corruption_results = await GardenService.apply_invasion_corruption(
            session, inv
        )
    # Region flag and win/loss counters stay VICTORY/DEFEAT-only, exactly as
    # before G2: an announced EXPIRED invasion withers gardens but never moves
    # territory or the group counters.
    if status in (InvasionStatus.VICTORY.value, InvasionStatus.DEFEAT.value):
        region = await session.get(Inv2Region, inv.region_code)
        if region is not None and (region.last_invasion_id is None or region.last_invasion_id <= inv.id):
            region.is_lost_to_enemy = status == InvasionStatus.DEFEAT.value
            region.last_invasion_id = inv.id
        state = await session.scalar(select(Inv2GroupState).where(
            Inv2GroupState.group_id == inv.group_id).with_for_update())
        if state is None:
            state = Inv2GroupState(group_id=inv.group_id)
            session.add(state)
        # No new settlement table/column: terminal statuses are the durable source
        # of truth and count assignment is idempotent even for repeated calls.
        state.total_victories = int(await session.scalar(select(func.count()).select_from(Inv2Invasion).where(
            Inv2Invasion.group_id == inv.group_id,
            Inv2Invasion.status == InvasionStatus.VICTORY.value)))
        state.total_defeats = int(await session.scalar(select(func.count()).select_from(Inv2Invasion).where(
            Inv2Invasion.group_id == inv.group_id,
            Inv2Invasion.status == InvasionStatus.DEFEAT.value)))
    return corruption_results
