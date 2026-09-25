"""Service for creating matches against internal game bots."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.core.config import settings
from sib.core.errors import InvalidActionException, PartyNotFoundException
from sib.database.models import Game, Match, MatchPlayer, MatchState, Party, PartyStatus, PartyMember
from sib.games.registry import get_game_engine
from sib.services.bot import BotService


class BotMatchService:
    """Creates and initializes matches against an internal bot."""

    @staticmethod
    async def create_bot_match(
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Match:
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .options(
                selectinload(Party.members).selectinload(PartyMember.user),
            )
            .with_for_update()
        )
        party = await session.scalar(stmt)

        if not party:
            raise PartyNotFoundException()

        if party.creator_user_id != user_id:
            raise InvalidActionException("فقط سازنده پارتی می‌تواند با Bot بازی کند.")

        if party.mode != "solo":
            raise InvalidActionException("بازی با Bot فقط برای پارتی تک‌نفره است.")

        if party.status != PartyStatus.FULL:
            raise InvalidActionException("پارتی هنوز آماده بازی نیست.")

        game = await session.scalar(
            select(Game).where(Game.id == party.game_id)
        )

        if not game or not game.supports_solo:
            raise InvalidActionException("این بازی از حالت تک‌نفره پشتیبانی نمی‌کند.")

        bot_user = await BotService.get_or_create_bot(session)

        human_member = next(
            (m for m in party.members if m.user_id == user_id),
            None,
        )
        if not human_member:
            raise InvalidActionException("شما عضو این پارتی نیستید.")

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
            started_at=datetime.now(timezone.utc),
        )
        session.add(match)
        await session.flush()

        human_player = MatchPlayer(
            match_id=match.id,
            user_id=human_member.user_id,
            group_id=party.group_id,
            team=1,
            turn_order=1,
            is_active=True,
            has_accepted=True,
        )
        human_player.user = human_member.user

        bot_player = MatchPlayer(
            match_id=match.id,
            user_id=bot_user.id,
            group_id=None,
            team=2,
            turn_order=1,
            is_active=True,
            has_accepted=True,
        )
        bot_player.user = bot_user

        session.add_all([human_player, bot_player])
        await session.flush()

        engine = get_game_engine(game.code)
        players = [human_player, bot_player]

        initial_state = engine.create_initial_state(players, party.mode)
        match.game_state = json.dumps(initial_state, ensure_ascii=False)

        current_player_id = initial_state.get("current_player_id")
        match.current_turn_player_id = current_player_id
        match.turn_deadline = (
            datetime.now(timezone.utc)
            + timedelta(seconds=engine.turn_timeout_seconds)
        )

        await session.flush()

        loaded_match = await session.scalar(
            select(Match)
            .where(Match.id == match.id)
            .options(
                selectinload(Match.players).selectinload(MatchPlayer.user),
                selectinload(Match.game),
            )
        )

        if not loaded_match:
            raise InvalidActionException("خطا در بارگذاری بازی.")

        return loaded_match
