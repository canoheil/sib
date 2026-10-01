"""Direct same-group match creation for Friendly parties."""

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.errors import InvalidActionException, PartyNotFoundException
from sib.core.logging import Events, log_event
from sib.database.models import (
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    Party,
    PartyMember,
    PartyStatus,
)
from sib.games.registry import get_game_engine
from sib.services.party import PartyService


class FriendlyMatchService:
    """Build and start one shared match from a full Friendly party."""

    @classmethod
    async def start_party(
        cls,
        session: AsyncSession,
        party_id: int,
        actor_user_id: int,
        *,
        group_id: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Match:
        """Atomically transition a full party into one ACTIVE Friendly Match."""
        now = now or datetime.now(timezone.utc)
        party = await session.scalar(
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
        if not party:
            raise PartyNotFoundException()
        PartyService._require_group(party, group_id)
        if party.match_type != MatchType.FRIENDLY:
            raise InvalidActionException("این دکمه فقط برای پارتی دوستانه است 🍏")
        if party.status != PartyStatus.FULL:
            raise InvalidActionException("برای شروع بازی، اول همه‌ی جای خالی‌ها رو پر کن 🍏")
        if not any(member.user_id == actor_user_id for member in party.members):
            raise InvalidActionException("فقط اعضای پارتی می‌تونن بازی رو شروع کنن 🍏")
        if not party.game:
            raise InvalidActionException("بازی این پارتی دیگر در دسترس نیست 🍏")
        if party.game.code == "apple_battle":
            raise InvalidActionException("شروع نبرد سیبی دوستانه از مسیر انتخاب کارت انجام می‌شه 🍏")

        team_capacity = PartyService._team_capacity(party)
        team_counts = {1: 0, 2: 0}
        for member in party.members:
            if member.team not in team_counts:
                raise InvalidActionException("ترکیب تیم‌های این پارتی معتبر نیست 🍏")
            team_counts[member.team] += 1
        if (
            party.current_players_count != party.max_players
            or len(party.members) != party.max_players
            or team_counts != {1: team_capacity, 2: team_capacity}
        ):
            raise InvalidActionException("تعداد بازیکنان دو تیم برای شروع کافی نیست 🍏")
        if party.max_players != team_capacity * 2:
            raise InvalidActionException("ظرفیت این پارتی با حالت دوستانه هماهنگ نیست 🍏")

        try:
            engine = get_game_engine(party.game.code)
        except ValueError as exc:
            raise InvalidActionException("موتور این بازی هنوز آماده نیست 🍏") from exc

        party.status = PartyStatus.IN_MATCH
        match = Match(
            game_id=party.game_id,
            mode=party.mode,
            match_type=MatchType.FRIENDLY,
            state=MatchState.ACTIVE,
            group1_id=party.group_id,
            group2_id=None,
            party1_id=party.id,
            party2_id=None,
            group1_accepted=True,
            group2_accepted=True,
            is_bot_match=any(
                bool(member.user and member.user.is_bot) for member in party.members
            ),
            started_at=now,
        )
        match.players = []
        session.add(match)
        await session.flush()

        sorted_members = sorted(
            party.members,
            key=lambda member: (member.team, member.joined_at, member.id),
        )
        team_turn_order = {1: 0, 2: 0}
        for member in sorted_members:
            team_turn_order[member.team] += 1
            player = MatchPlayer(
                match_id=match.id,
                user_id=member.user_id,
                group_id=party.group_id,
                team=member.team,
                turn_order=team_turn_order[member.team],
                is_active=True,
                has_accepted=True,
                bot_profile=member.bot_profile,
            )
            if member.user is not None:
                player.user = member.user
            session.add(player)
        await session.flush()

        match.players = list(
            (
                await session.scalars(
                    select(MatchPlayer)
                    .where(MatchPlayer.match_id == match.id)
                    .options(selectinload(MatchPlayer.user))
                    .order_by(MatchPlayer.team, MatchPlayer.turn_order)
                )
            ).all()
        )
        match.game = party.game
        match.group1 = party.group
        match.group2 = None

        state = engine.create_initial_state(match.players, party.mode)
        state["match_type"] = MatchType.FRIENDLY
        state["team_names"] = {"1": "تیم اول", "2": "تیم دوم"}

        if match.is_bot_match:
            from sib.bots.runner import run_bot_turns

            state = await run_bot_turns(match, state)

        match.game_state = json.dumps(state, ensure_ascii=False)
        current_player_id = state.get("current_player_id")
        match.current_turn_player_id = current_player_id
        match.turn_deadline = (
            None
            if engine.is_finished(state)
            else now + timedelta(seconds=engine.turn_timeout_seconds)
        )
        await session.flush()

        log_event(
            Events.MATCH_STARTED,
            {
                "match_id": match.id,
                "party_id": party.id,
                "group_id": party.group_id,
                "match_type": MatchType.FRIENDLY,
                "mode": party.mode,
                "is_bot_match": match.is_bot_match,
                "team_counts": team_counts,
            },
        )

        return await session.scalar(
            select(Match)
            .where(Match.id == match.id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.game),
                selectinload(Match.group1),
            )
        )
