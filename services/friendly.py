"""Friendly matches ("بازی با اعضای گروه") — a MATCH TYPE, not a game mode.

The hierarchy is Game -> Game Mode -> Match Type:

    Battleship -> 4v4 -> «پیدا کردن حریف»        (normal, ranked matchmaking)
    Battleship -> 4v4 -> «بازی با اعضای گروه»    (friendly, group-internal)

A friendly party holds BOTH teams of one group-internal match (up to
``2 x MODE_SLOTS[mode]`` human players; bots may fill the free slots) and
starts immediately - no queue and no acceptance step. Friendly matches:

- never touch rank/ELO/MMR, group wins/leaderboards or seedlings,
- pay a configurable Apple reward instead (``FriendlyRewardService``),
- reuse the exact same game engines, Match model and bot infrastructure,
- can be rematched after they finish.

This module owns friendly match assembly, the Apple reward rule and the
single-board rendering helpers. Party/team mechanics (slots, teams, bot
filling) live in ``sib.services.party.service``.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.errors import (
    ActiveSessionExistsException,
    InvalidActionException,
    MatchNotFoundException,
    PartyNotFoundException,
)
from sib.core.logging import log_event, Events
from sib.database.models import (
    AppleTransaction,
    Game,
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    Party,
    PartyMember,
    PartyStatus,
    User,
)
from sib.games.registry import get_game_engine
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.party import PartyService
from sib.services.session import SessionService

# Ledger reason of the friendly Apple reward (auditable + countable per day).
FRIENDLY_REWARD_REASON = "friendly_match_reward"

# Team identities stored in the engine state of every friendly match.
FRIENDLY_TEAM_NAMES = {"1": "🍎 تیم سرخ", "2": "🍏 تیم سبز"}

# Engine outcome lines that are relative to ONE side's board. A friendly match
# shares a single group board between both teams, so the final message must
# name the winning team instead of saying "you" to half of the group.
_RELATIVE_OUTCOME_LINES = (
    "🎉 **گروه شما پیروز شد!** 🍏🏆",
    "💔 **گروه حریف پیروز شد!** 🍏",
    "🎉 **گروه شما پیروز شد!** 🍏🚢",
    "🎉 **تیم شما پیروز شد!** 🍏🏆",
    "💔 **تیم حریف پیروز شد!** 🍏",
    "🎉 **برنده شدید!** 🍏🏆",
    "💔 **بازی را واگذار کردید!** 🍏",
)


def is_friendly(match: Any) -> bool:
    """True when this Match (or Party) is a friendly one."""
    return getattr(match, "match_type", MatchType.NORMAL) == MatchType.FRIENDLY


# ---------------------------------------------------------------------------
# Shared board rendering
# ---------------------------------------------------------------------------


def friendly_audiences(group_id: int) -> Dict[int, int]:
    """Render audiences for a friendly match played inside ONE group.

    Engine renders resolve "my side" from the serialized players' ``group_id``.
    Both friendly teams live in the same Telegram group, so team 1 keeps the
    real (negative) group id while team 2 gets a synthetic positive marker -
    distinct audiences, correct per-team radars/labels, no DB change.
    """
    group_id = int(group_id)
    return {1: group_id, 2: abs(group_id) * 1000 + 2}


def apply_friendly_state(state: Dict[str, Any], match: Match, group_id: int) -> Dict[str, Any]:
    """Rewrite an engine state for a friendly match (audiences + team names)."""
    audiences = friendly_audiences(group_id)
    players = state.get("players", {})
    for player in match.players:
        info = players.get(str(player.user_id))
        if info is None:
            continue
        info["group_id"] = audiences.get(int(player.team), audiences[1])
    state["match_type"] = MatchType.FRIENDLY
    state["friendly"] = True
    state["team_names"] = dict(FRIENDLY_TEAM_NAMES)
    return state


def viewer_audience(
    match: Match, state: Optional[Dict[str, Any]], viewer_player_id: Optional[int]
) -> Optional[int]:
    """Render audience of a friendly viewer's team, or None for normal matches.

    Call sites keep their existing ``for_group_id`` for normal matches and use
    this for friendly ones, so the shared board always shows the acting
    player's own team (radar/cells/keyboard stay consistent on every turn).
    """
    if not is_friendly(match) or not state or viewer_player_id is None:
        return None
    info = (state.get("players") or {}).get(str(viewer_player_id))
    if not info:
        return None
    value = info.get("group_id")
    return int(value) if value is not None else None


def state_viewer_id(
    state: Optional[Dict[str, Any]],
    human_ids: Any,
    actor_id: Optional[int],
) -> Optional[int]:
    """Player whose perspective a friendly shared board should show.

    The acting player must always get a live keyboard, so the current player
    wins; for simultaneous games (no current player) the player who just acted
    keeps their own view. Pure state helper (works without a Match instance).
    """
    human_ids = {int(pid) for pid in human_ids}
    if not human_ids:
        return actor_id
    current = (state or {}).get("current_player_id")
    if current is not None and int(current) in human_ids:
        return int(current)
    if actor_id in human_ids:
        return actor_id
    return min(human_ids)


def shared_board_viewer(
    match: Match, state: Optional[Dict[str, Any]], actor_id: Optional[int]
) -> Optional[int]:
    """Match-based wrapper of :func:`state_viewer_id` (None for normal matches)."""
    if not is_friendly(match):
        return None
    human_ids = {p.user_id for p in match.players if p.group_id is not None}
    return state_viewer_id(state, human_ids, actor_id)


class FriendlyOutcomeView:
    """Minimal match-like view used to shape a shared final board.

    The presentation phase of game actions works on primitive snapshots, but
    the friendly final-text/keyboard helpers only need the match id, the
    outcome and the "this is friendly" marker.
    """

    def __init__(self, match_id: int, is_draw: bool, winner_team: Optional[int]):
        self.id = match_id
        self.match_type = MatchType.FRIENDLY
        self.is_draw = bool(is_draw)
        self.winner_team = winner_team


def friendly_result_line(match: Match, state: Optional[Dict[str, Any]]) -> str:
    """Neutral, team-named result line for the shared friendly board."""
    if match.is_draw:
        return "🤝 **نتیجه: مساوی!**"
    team_name = ((state or {}).get("team_names") or {}).get(str(match.winner_team))
    if not team_name:
        team_name = f"تیم {match.winner_team}"
    return f"🏆 **برندهٔ بازی رفیقانه: {team_name}**"


def shape_friendly_final_text(
    text: str, match: Match, state: Optional[Dict[str, Any]]
) -> str:
    """Replace side-relative outcome text with the neutral friendly result."""
    replaced = False
    for pattern in _RELATIVE_OUTCOME_LINES:
        if pattern in text:
            text = text.replace(pattern, "")
            replaced = True
    if replaced:
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
    line = friendly_result_line(match, state)
    text = text.rstrip()
    if line in text:
        return text
    return f"{text}\n\n{line}" if text else line


# ---------------------------------------------------------------------------
# Friendly match assembly
# ---------------------------------------------------------------------------


class FriendlyMatchService:
    """Creates and restarts group-internal friendly matches."""

    @classmethod
    async def load_match(cls, session: AsyncSession, match_id: int) -> Optional[Match]:
        stmt = (
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.game),
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.group1),
                selectinload(Match.group2),
            )
        )
        return await session.scalar(stmt)

    @classmethod
    async def _load_party_locked(cls, session: AsyncSession, party_id: int) -> Party:
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Party.members).selectinload(PartyMember.user),
                selectinload(Party.game),
                selectinload(Party.group),
            )
            .with_for_update()
        )
        party = await session.scalar(stmt)
        if not party or party.status == PartyStatus.CANCELLED:
            raise PartyNotFoundException()
        return party

    @classmethod
    async def start_friendly_match(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Match:
        """Start a full friendly party as an ACTIVE match (idempotency-guarded).

        The party row lock serializes double taps / two members pressing start
        at once: exactly one request can move OPEN|FULL -> IN_MATCH and create
        the match, every later request sees IN_MATCH and is rejected.
        """
        party = await cls._load_party_locked(session, party_id)

        if not party.is_friendly:
            raise InvalidActionException(
                "این پارتی رفیقانه نیست؛ برای بازی رقابتی از «پیدا کردن حریف» استفاده کن 🍏"
            )
        if party.status == PartyStatus.IN_MATCH:
            raise InvalidActionException("این بازی رفیقانه شروع شده و در جریانه 🍏")
        if party.status == PartyStatus.IN_QUEUE:
            raise InvalidActionException("این پارتی توی صف حریف‌یابیه 🍏")
        if not any(member.user_id == user_id for member in party.members):
            raise InvalidActionException("فقط اعضای این پارتی می‌تونن بازی رو شروع کنن 🍏")
        if not party.is_full:
            raise InvalidActionException(
                f"هنوز {party.remaining_slots} جای خالی داریم؛ یا عضو جدید بیار یا جای خالی رو با بات پر کن 🤖🍏"
            )

        game = party.game
        if not game:
            raise InvalidActionException("بازی این پارتی دیگه در دسترس نیست.")

        now = datetime.now(timezone.utc)
        members = sorted(
            party.members,
            key=lambda member: (int(member.team or 1), member.joined_at, member.id),
        )
        has_bots = any(member.user is not None and member.user.is_bot for member in members)

        match = Match(
            game_id=party.game_id,
            mode=party.mode,
            match_type=MatchType.FRIENDLY,
            state=MatchState.ACTIVE,
            group1_id=party.group_id,
            group2_id=party.group_id,
            party1_id=party.id,
            party2_id=None,
            group1_accepted=True,
            group2_accepted=True,
            is_bot_match=has_bots,
            started_at=now,
        )
        # Mark the relationship loaded before the first flush so later
        # collection assignment never triggers an async lazy-load.
        match.players = []
        session.add(match)
        await session.flush()

        turn_orders: Dict[int, int] = {1: 0, 2: 0}
        for member in members:
            team = int(member.team or 1)
            if team not in (1, 2):
                team = 1
            turn_orders[team] = turn_orders.get(team, 0) + 1
            is_bot_member = member.user is not None and member.user.is_bot
            session.add(
                MatchPlayer(
                    match_id=match.id,
                    user_id=member.user_id,
                    group_id=None if is_bot_member else party.group_id,
                    team=team,
                    turn_order=turn_orders[team],
                    is_active=True,
                    has_accepted=True,
                    bot_profile=member.bot_profile,
                )
            )
        await session.flush()

        players = list(
            (
                await session.scalars(
                    select(MatchPlayer)
                    .where(MatchPlayer.match_id == match.id)
                    .options(selectinload(MatchPlayer.user))
                    .order_by(MatchPlayer.team, MatchPlayer.turn_order)
                )
            ).all()
        )
        match.players = players
        match.game = game
        match.group1 = party.group
        match.group2 = party.group

        engine = get_game_engine(game.code)
        state = engine.create_initial_state(match.players, party.mode)
        apply_friendly_state(state, match, party.group_id)
        match.current_turn_player_id = state.get("current_player_id")

        # Bots may need to act before the first human (simultaneous games lock
        # their hidden choice; turn-based games may start on a bot turn).
        from sib.bots.runner import run_bot_turns

        state = await run_bot_turns(match, state)

        match.game_state = json.dumps(state, ensure_ascii=False)
        match.current_turn_player_id = state.get("current_player_id")
        match.turn_deadline = (
            None
            if engine.is_finished(state)
            else now + timedelta(seconds=engine.turn_timeout_seconds)
        )
        party.status = PartyStatus.IN_MATCH
        await session.flush()

        if engine.is_finished(state):
            # A bot-only lineup can finish the whole game during the opening
            # bot turns (e.g. 4v4 with one human). The match must never stay
            # ACTIVE: apply the result/reward through the one shared choke
            # point exactly like a human move would.
            from sib.services.match_result import MatchResultService

            await MatchResultService.apply_match_result(
                session, match, engine.get_result(state)
            )
            match.game_state = json.dumps(state, ensure_ascii=False)

        log_event(
            Events.FRIENDLY_MATCH_STARTED,
            {
                "match_id": match.id,
                "party_id": party.id,
                "group_id": party.group_id,
                "game": game.code,
                "mode": party.mode,
                "humans": sum(1 for p in players if not p.user.is_bot),
                "bots": sum(1 for p in players if p.user.is_bot),
                "started_by": user_id,
            },
        )
        return await cls.load_match(session, match.id)

    @classmethod
    async def rematch(
        cls,
        session: AsyncSession,
        match_id: int,
        user_id: int,
    ) -> Match:
        """Recreate a finished friendly match with the same game/mode/lineup.

        The new match is ALWAYS friendly (never ranked). Every human of the
        finished match who is still free joins with their old team; busy
        players and empty slots are filled with bots, so the rematch starts
        immediately. Reward eligibility is re-evaluated per player by
        ``FriendlyRewardService`` (daily limits still apply).
        """
        old = await session.scalar(
            select(Match)
            .where(Match.id == match_id)
            .execution_options(populate_existing=True)
            .options(selectinload(Match.players).selectinload(MatchPlayer.user))
            .with_for_update()
        )
        if not old:
            raise MatchNotFoundException()
        if not is_friendly(old):
            raise InvalidActionException("بازی مجدد فقط برای بازی‌های رفیقانه فعاله 🍏")
        if old.state != MatchState.FINISHED:
            raise InvalidActionException("این مسابقه هنوز تموم نشده 🍏")
        presser = next(
            (p for p in old.players if p.user_id == user_id and p.user is not None and not p.user.is_bot),
            None,
        )
        if not presser:
            raise InvalidActionException("فقط بازیکن‌های همین مسابقه می‌تونن بازی مجدد بسازن 🍏")
        if await SessionService.has_active_session(session, user_id):
            raise ActiveSessionExistsException()
        if old.group1_id is None:
            raise InvalidActionException("گروه این مسابقه دیگه در دسترس نیست 🍏")

        party = Party(
            group_id=old.group1_id,
            game_id=old.game_id,
            mode=old.mode,
            match_type=MatchType.FRIENDLY,
            max_players=PartyService.friendly_slots_for_mode(old.mode),
            current_players_count=0,
            status=PartyStatus.OPEN,
            creator_user_id=user_id,
        )
        session.add(party)
        await session.flush()

        team_size = party.team_size
        team_counts: Dict[int, int] = {1: 0, 2: 0}
        humans = [p for p in old.players if p.user is not None and not p.user.is_bot]
        ordered = [presser] + [p for p in humans if p.user_id != presser.user_id]
        for player in ordered:
            team = int(player.team or 1)
            if team not in (1, 2) or team_counts.get(team, 0) >= team_size:
                team = 2 if team_counts.get(1, 0) >= team_size else 1
            if team_counts.get(team, 0) >= team_size:
                continue
            if player.user_id != user_id and await SessionService.has_active_session(
                session, player.user_id
            ):
                # Busy player: the slot is filled with a bot instead.
                continue
            session.add(
                PartyMember(party_id=party.id, user_id=player.user_id, team=team)
            )
            team_counts[team] = team_counts.get(team, 0) + 1
            party.current_players_count += 1
        await session.flush()

        if not party.is_full:
            await PartyService.fill_empty_slots_with_bots(session, party.id, user_id)

        match = await cls.start_friendly_match(session, party.id, user_id)
        log_event(
            Events.FRIENDLY_REMATCH,
            {
                "previous_match_id": old.id,
                "match_id": match.id,
                "party_id": party.id,
                "user_id": user_id,
            },
        )
        return match


# ---------------------------------------------------------------------------
# Rewards
# ---------------------------------------------------------------------------


class FriendlyRewardService:
    """Apple rewards for completed friendly matches (never rank/seedlings).

    Rule (all three values are `system_config` tunables):

    - at most ``friendly_reward_max_matches_per_day`` reward-eligible matches
      per player per day,
    - ``friendly_reward_apples_per_match`` Apples per eligible match,
    - at most ``friendly_reward_daily_apple_cap`` Apples per player per day.

    Every payout is one auditable wallet ledger entry whose unique idempotency
    key is ``friendly_reward:{match_id}:{user_id}`` - duplicate callbacks,
    retries or a second result application can never pay the same match twice.
    """

    @classmethod
    async def usage_today(
        cls, session: AsyncSession, user_id: int
    ) -> Tuple[int, int]:
        """(rewarded matches today, apples earned today) for one player."""
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = (
            await session.execute(
                select(
                    func.count(AppleTransaction.id),
                    func.coalesce(func.sum(AppleTransaction.amount), 0),
                ).where(
                    AppleTransaction.user_id == user_id,
                    AppleTransaction.reason == FRIENDLY_REWARD_REASON,
                    AppleTransaction.created_at >= day_start,
                )
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    @classmethod
    async def reward_for_player(
        cls, session: AsyncSession, user_id: int
    ) -> int:
        """Apples this player still earns on one more friendly match today."""
        max_matches = int(
            await ConfigService.get(session, "friendly_reward_max_matches_per_day")
        )
        per_match = int(await ConfigService.get(session, "friendly_reward_apples_per_match"))
        daily_cap = int(await ConfigService.get(session, "friendly_reward_daily_apple_cap"))
        used_matches, earned_today = await cls.usage_today(session, user_id)
        if used_matches >= max_matches:
            return 0
        return max(0, min(per_match, daily_cap - earned_today))

    @classmethod
    async def award_match_rewards(
        cls, session: AsyncSession, match: Match
    ) -> Dict[int, int]:
        """Pay every human participant of a finished FRIENDLY match.

        Returns ``{user_id: apples}`` for the players who were paid in this
        call (0/absent for capped players and for bots). Safe to call more
        than once: the ledger keys make it a no-op for already-paid players.
        """
        if not is_friendly(match):
            return {}

        participant_ids = [player.user_id for player in match.players]
        if not participant_ids:
            return {}

        # Bot identities are resolved with ONE query: never touch a lazily
        # loaded relationship here (safe for async sessions and partial loads).
        bot_ids = set(
            (
                await session.scalars(
                    select(User.id).where(
                        User.id.in_(participant_ids),
                        User.is_bot == True,  # noqa: E712
                    )
                )
            ).all()
        )
        human_ids = [user_id for user_id in participant_ids if user_id not in bot_ids]
        if not human_ids:
            return {}

        paid: Dict[int, int] = {}
        for user_id in human_ids:
            amount = await cls.reward_for_player(session, user_id)
            if amount <= 0:
                log_event(
                    Events.FRIENDLY_REWARD_CAPPED,
                    {"match_id": match.id, "user_id": user_id},
                )
                continue
            await AppleWalletService.earn(
                session,
                user_id=user_id,
                amount=amount,
                reason=FRIENDLY_REWARD_REASON,
                ref_type="match",
                ref_id=match.id,
                idempotency_key=f"friendly_reward:{match.id}:{user_id}",
            )
            paid[user_id] = amount
            log_event(
                Events.FRIENDLY_REWARD_PAID,
                {
                    "match_id": match.id,
                    "user_id": user_id,
                    "amount": amount,
                },
            )
        return paid
