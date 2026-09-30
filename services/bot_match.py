"""Create normal active Matches with independent bot participants."""

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.errors import InvalidActionException, PartyNotFoundException
from sib.database.models import (
    AppleBattleQueueEntry,
    Character,
    Match,
    MatchPlayer,
    MatchState,
    MatchmakingQueue,
    Party,
    PartyMember,
    PartyStatus,
    PlayerCard,
    ReleaseStatus,
)
from sib.games.registry import get_game_engine
from sib.services.bot import BotService
from sib.services.match_setup import attach_team_names


class BotMatchService:
    """Builds fallback matches through the same Match/MatchPlayer model as PvP."""

    @staticmethod
    def _is_expired(value, now: datetime) -> bool:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= now

    @classmethod
    async def create_fallback_from_queue(
        cls,
        session: AsyncSession,
        queue_entry_id: int,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[Match]:
        """Atomically claim an expired queue and create its bot-fallback Match.

        Matchmaking and timeout workers lock the same queue/party rows. Thus a
        real opponent can either claim the queue first, or timeout can claim it,
        but never both.
        """
        now = now or datetime.now(timezone.utc)
        queue = await session.scalar(
            select(MatchmakingQueue)
            .where(MatchmakingQueue.id == queue_entry_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            not queue
            or not queue.is_active
            or not cls._is_expired(queue.expires_at, now)
        ):
            return None

        party = await session.scalar(
            select(Party)
            .where(Party.id == queue.party_id)
            .options(
                selectinload(Party.members).selectinload(PartyMember.user),
                selectinload(Party.game),
                selectinload(Party.group),
            )
            .with_for_update()
        )
        if not party or party.status != PartyStatus.IN_QUEUE:
            return None

        if not party.is_full or len(party.members) != party.max_players:
            # Do not fabricate missing human players. Release this stale queue.
            queue.is_active = False
            party.status = PartyStatus.FULL if party.current_players_count else PartyStatus.CANCELLED
            await session.flush()
            return None

        game = party.game
        if not game:
            raise InvalidActionException("بازی این صف دیگر در دسترس نیست.")
        try:
            engine = get_game_engine(game.code)
            if game.code != "apple_battle":
                from sib.bots.registry import get_bot_strategy

                get_bot_strategy(game.code)
        except ValueError as exc:
            raise InvalidActionException("برای این بازی هنوز حریف سیبی آماده نیست.") from exc

        queue.is_active = False
        party.status = PartyStatus.IN_MATCH
        match = Match(
            game_id=party.game_id,
            mode=party.mode,
            state=MatchState.ACTIVE,
            group1_id=party.group_id,
            group2_id=None,
            party1_id=party.id,
            party2_id=None,
            group1_accepted=True,
            group2_accepted=True,
            is_bot_match=True,
            started_at=now,
        )
        # Mark the relationship loaded before the first flush so later
        # collection assignment never triggers an async lazy-load.
        match.players = []
        session.add(match)
        await session.flush()

        humans = sorted(party.members, key=lambda member: member.joined_at)
        for index, member in enumerate(humans, start=1):
            session.add(
                MatchPlayer(
                    match_id=match.id,
                    user_id=member.user_id,
                    group_id=party.group_id,
                    team=1,
                    turn_order=index,
                    is_active=True,
                    has_accepted=True,
                )
            )

        bot_count = party.max_players
        bots = await BotService.create_match_bots(
            session,
            match_id=match.id,
            count=bot_count,
            mode=party.mode,
            game_code=game.code,
        )
        bot_players = []
        for index, (bot_user, profile) in enumerate(bots, start=1):
            bot_player = MatchPlayer(
                match_id=match.id,
                user_id=bot_user.id,
                group_id=None,
                team=2,
                turn_order=index,
                is_active=True,
                has_accepted=True,
                bot_profile=profile,
            )
            bot_player.user = bot_user
            bot_players.append(bot_player)
        session.add_all(bot_players)
        await session.flush()

        players = await session.scalars(
            select(MatchPlayer)
            .where(MatchPlayer.match_id == match.id)
            .options(selectinload(MatchPlayer.user))
            .order_by(MatchPlayer.team, MatchPlayer.turn_order)
        )
        match.players = list(players.all())
        match.game = game
        match.group1 = party.group
        match.group2 = None

        if game.code == "apple_battle":
            await cls._attach_bot_apple_battle_loadout(session, bots)
            from sib.services.apple_battle import AppleBattleService

            state = await AppleBattleService.build_match_state(session, match)
            if state is None:
                raise InvalidActionException("نبرد سیبی بدون کارت آماده قابل شروع نیست.")
        else:
            state = engine.create_initial_state(match.players, party.mode)
            attach_team_names(state, match)
            # Simultaneous games need the bot's hidden choice locked before the
            # board is first rendered. Turn-based modes start with the humans.
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

        return await session.scalar(
            select(Match)
            .where(Match.id == match.id)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.game),
                selectinload(Match.group1),
                selectinload(Match.group2),
            )
        )

    @staticmethod
    async def _attach_bot_apple_battle_loadout(session, bots) -> None:
        """Give each Apple Battle bot a real, independently-owned card/loadout."""
        characters = list(
            (
                await session.scalars(
                    select(Character)
                    .where(Character.is_active == True)
                    .where(Character.release_status == ReleaseStatus.RELEASED)
                    .order_by(Character.id.asc())
                )
            ).all()
        )
        if not characters:
            raise InvalidActionException("برای نبرد سیبی هنوز شخصیت آماده‌ای وجود ندارد.")

        for index, (bot_user, _profile) in enumerate(bots):
            character = characters[index % len(characters)]
            card = PlayerCard(
                user_id=bot_user.id,
                character_id=character.id,
                level=1,
            )
            session.add(card)
            await session.flush()
            session.add(
                AppleBattleQueueEntry(
                    user_id=bot_user.id,
                    card_id=card.id,
                    level=1,
                    queued_at=datetime.now(timezone.utc),
                )
            )
        await session.flush()
