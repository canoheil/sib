"""Match lifecycle operations such as active-player forfeits."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from goje.core.errors import InvalidActionException, MatchNotFoundException
from goje.core.logging import Events, log_event
from goje.database.models import Match, MatchPlayer, MatchState
from goje.games.base import GameResult
from goje.services.rating import RatingService


class MatchLifecycleService:
    """Handle lifecycle transitions for active matches."""

    @classmethod
    async def forfeit_player(
        cls,
        session: AsyncSession,
        match_id: int,
        user_id: int,
    ) -> Match:
        """Forfeit an active match on behalf of one of its players."""

        stmt = (
            select(Match)
            .where(Match.id == match_id)
            .options(selectinload(Match.players))
            .with_for_update()
        )
        match = await session.scalar(stmt)

        if not match:
            raise MatchNotFoundException()

        if match.state != MatchState.ACTIVE:
            raise MatchNotFoundException()

        player = next(
            (p for p in match.players if p.user_id == user_id),
            None,
        )

        if not player:
            raise InvalidActionException(
                "شما بازیکن این مسابقه نیستید."
            )

        if not player.is_active:
            raise InvalidActionException(
                "شما قبلاً از این مسابقه خارج شده‌اید."
            )

        # Mark the leaving player inactive before applying the result.
        player.is_active = False

        winner_team = 2 if player.team == 1 else 1

        result = GameResult(
            winner_team=winner_team,
            is_draw=False,
            summary="بازیکن حریف از مسابقه خارج شد و تیم مقابل برنده شد! 🍅",
            details={
                "player_forfeit": True,
                "forfeited_player_id": user_id,
            },
        )

        await RatingService.apply_match_result(
            session,
            match,
            result,
        )

        await session.commit()


        return match
