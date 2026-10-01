"""Party service managing Competitive and Friendly group parties."""

from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sib.services.bot import BotService
from sib.core.errors import (
    ActiveSessionExistsException,
    InvalidActionException,
    PartyFullException,
    PartyNotFoundException,
)
from sib.core.logging import Events, log_event
from sib.database.models import (
    Game,
    MatchType,
    Party,
    PartyMember,
    PartyStatus,
    PlayerCard,
    User,
)
from sib.services.session import SessionService


class PartyService:
    """Manages party creation, atomic join/leave, bot filling, and lifecycle."""

    # Capacity of one side; Competitive parties retain these capacities.
    MODE_SLOTS = {"solo": 1, "2v2": 2, "4v4": 4}

    @classmethod
    def _team_capacity(cls, party: Party) -> int:
        try:
            return cls.MODE_SLOTS[party.mode]
        except KeyError as exc:
            raise InvalidActionException("حالت این پارتی معتبر نیست 🍏") from exc

    @staticmethod
    def _require_group(party: Party, group_id: Optional[int]) -> None:
        if group_id is not None and party.group_id != group_id:
            # Treat a button copied into another chat exactly like a stale
            # party reference; never reveal or mutate another group's party.
            raise PartyNotFoundException()

    @staticmethod
    async def _validate_card_selection(
        session: AsyncSession,
        *,
        game_code: str,
        match_type: str,
        user_id: int,
        card_id: Optional[int],
    ) -> Optional[int]:
        if game_code != "apple_battle" or match_type != MatchType.FRIENDLY:
            if card_id is not None:
                raise InvalidActionException("انتخاب کارت فقط برای نبرد سیبی دوستانه است 🍏")
            return None
        if card_id is None:
            raise InvalidActionException("برای نبرد سیبی دوستانه، اول کارت نبردت رو انتخاب کن 🍏")
        owned = await session.scalar(
            select(PlayerCard.id)
            .where(PlayerCard.id == card_id)
            .where(PlayerCard.user_id == user_id)
        )
        if owned is None:
            raise InvalidActionException("این کارت برای حساب شما نیست یا دیگر در دسترس نیست 🍏")
        return card_id

    @classmethod
    async def create_party(
        cls,
        session: AsyncSession,
        group_id: int,
        creator_user_id: int,
        game_code: str,
        mode: str = "solo",
        *,
        match_type: str = MatchType.COMPETITIVE,
        creator_team: int = 1,
        selected_card_id: Optional[int] = None,
    ) -> Party:
        """Create one Competitive side or a two-team Friendly party."""
        match_type = (match_type or MatchType.COMPETITIVE).upper()
        if match_type not in (MatchType.COMPETITIVE, MatchType.FRIENDLY):
            raise InvalidActionException("نوع مسابقه انتخاب‌شده معتبر نیست 🍏")
        if mode not in cls.MODE_SLOTS:
            raise InvalidActionException("حالت بازی انتخاب‌شده معتبر نیست 🍏")
        if creator_team not in (1, 2):
            raise InvalidActionException("تیم انتخاب‌شده معتبر نیست 🍏")
        if match_type == MatchType.COMPETITIVE and creator_team != 1:
            raise InvalidActionException("تیم رقابتی معتبر نیست 🍏")

        # A per-user row lock makes create/double-tap idempotent on the
        # existing one-active-session invariant.
        await SessionService.lock_user(session, creator_user_id)
        if await SessionService.has_active_session(session, creator_user_id):
            raise ActiveSessionExistsException()

        game = await session.scalar(
            select(Game).where(Game.code == game_code).where(Game.is_active == True)
        )
        if not game:
            raise InvalidActionException("بازی انتخاب شده معتبر نیست یا غیرفعال است.")
        if mode == "solo" and not game.supports_solo:
            raise InvalidActionException("این بازی از حالت انفرادی پشتیبانی نمی‌کنه.")
        if mode == "2v2" and not game.supports_2v2:
            raise InvalidActionException("این بازی از حالت تیمی ۲ به ۲ پشتیبانی نمی‌کنه.")
        if mode == "4v4" and not game.supports_4v4:
            raise InvalidActionException("این بازی از حالت تیمی ۴ به ۴ پشتیبانی نمی‌کنه.")

        selected_card_id = await cls._validate_card_selection(
            session,
            game_code=game_code,
            match_type=match_type,
            user_id=creator_user_id,
            card_id=selected_card_id,
        )

        side_capacity = cls.MODE_SLOTS[mode]
        max_players = side_capacity * (2 if match_type == MatchType.FRIENDLY else 1)
        initial_status = PartyStatus.FULL if max_players == 1 else PartyStatus.OPEN
        party = Party(
            group_id=group_id,
            game_id=game.id,
            mode=mode,
            match_type=match_type,
            max_players=max_players,
            current_players_count=1,
            status=initial_status,
            creator_user_id=creator_user_id,
        )
        session.add(party)
        await session.flush()

        session.add(
            PartyMember(
                party_id=party.id,
                user_id=creator_user_id,
                team=creator_team,
                selected_card_id=selected_card_id,
            )
        )
        await session.flush()
        log_event(
            Events.PARTY_CREATED,
            {
                "party_id": party.id,
                "group_id": group_id,
                "game": game_code,
                "mode": mode,
                "match_type": match_type,
                "creator_id": creator_user_id,
                "max_players": max_players,
                "status": party.status,
            },
        )
        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def join_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
        *,
        team: Optional[int] = None,
        group_id: Optional[int] = None,
        selected_card_id: Optional[int] = None,
    ) -> Party:
        """Atomically join an open party on the requested side."""
        import asyncio
        from sqlalchemy.exc import OperationalError

        await SessionService.lock_user(session, user_id)
        if await SessionService.has_active_session(session, user_id):
            raise ActiveSessionExistsException()

        party = None
        for attempt in range(2):
            try:
                party = await session.scalar(
                    select(Party)
                    .where(Party.id == party_id)
                    .options(selectinload(Party.game))
                    .with_for_update()
                )
                break
            except OperationalError:
                if attempt == 0:
                    await asyncio.sleep(0.05)
                    continue
                raise PartyFullException()
        if not party:
            raise PartyNotFoundException()
        cls._require_group(party, group_id)
        if party.status != PartyStatus.OPEN:
            if party.status == PartyStatus.FULL:
                raise PartyFullException()
            raise PartyNotFoundException()

        if await session.scalar(
            select(PartyMember.id)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        ):
            raise InvalidActionException("شما در حال حاضر عضو این پارتی هستید 🍏")

        if party.match_type == MatchType.FRIENDLY:
            if team not in (1, 2):
                raise InvalidActionException("برای پیوستن، یکی از دو تیم رو انتخاب کن 🍏")
            chosen_team = int(team)
        else:
            if team not in (None, 1):
                raise InvalidActionException("تیم انتخاب‌شده برای این پارتی معتبر نیست 🍏")
            chosen_team = 1

        side_capacity = cls._team_capacity(party)
        team_count = await session.scalar(
            select(func.count(PartyMember.id))
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.team == chosen_team)
        )
        if int(team_count or 0) >= side_capacity:
            raise PartyFullException()
        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL
            await session.flush()
            raise PartyFullException()

        selected_card_id = await cls._validate_card_selection(
            session,
            game_code=party.game.code,
            match_type=party.match_type,
            user_id=user_id,
            card_id=selected_card_id,
        )
        session.add(
            PartyMember(
                party_id=party.id,
                user_id=user_id,
                team=chosen_team,
                selected_card_id=selected_card_id,
            )
        )
        party.current_players_count += 1
        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL
        await session.flush()
        log_event(
            Events.PARTY_JOINED,
            {
                "party_id": party.id,
                "user_id": user_id,
                "team": chosen_team,
                "match_type": party.match_type,
                "current_players": party.current_players_count,
                "max_players": party.max_players,
                "status": party.status,
            },
        )
        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def fill_bot_slots(
        cls,
        session: AsyncSession,
        party_id: int,
        actor_user_id: int,
        *,
        group_id: Optional[int] = None,
    ) -> Party:
        """Fill every open Friendly slot with persisted, distinct bot users.

        The party row lock serializes this against human joins and another
        fill/start click. Only the active party creator may request it.
        """
        party = await session.scalar(
            select(Party)
            .where(Party.id == party_id)
            .options(
                selectinload(Party.members).selectinload(PartyMember.user),
                selectinload(Party.game),
            )
            .with_for_update()
        )
        if not party:
            raise PartyNotFoundException()
        cls._require_group(party, group_id)
        if party.match_type != MatchType.FRIENDLY:
            raise InvalidActionException("بات فقط برای پارتی دوستانه فعاله 🍏")
        if party.status != PartyStatus.OPEN:
            raise InvalidActionException("این پارتی جای خالی نداره یا دیگه باز نیست 🍏")
        if party.creator_user_id != actor_user_id:
            raise InvalidActionException("فقط سازنده‌ی پارتی می‌تونه جای خالی رو با بات پر کنه 🍏")
        if not any(m.user_id == actor_user_id for m in party.members):
            raise InvalidActionException("سازنده باید هنوز عضو پارتی باشه تا بات اضافه کنه 🍏")
        if party.game.code == "apple_battle":
            raise InvalidActionException("تکمیل باتِ نبرد سیبی از صفحه‌ی انتخاب کارت انجام می‌شه 🍏")

        side_capacity = cls._team_capacity(party)
        team_counts = {
            team: sum(1 for member in party.members if member.team == team)
            for team in (1, 2)
        }
        open_teams = [
            team
            for team in (1, 2)
            for _ in range(max(0, side_capacity - team_counts[team]))
        ]
        if not open_teams:
            raise PartyFullException()

        bot_rows = await BotService.create_party_bots(
            session,
            party_id=party.id,
            count=len(open_teams),
            mode=party.mode,
            game_code=party.game.code,
        )
        for team, (bot_user, profile) in zip(open_teams, bot_rows):
            session.add(
                PartyMember(
                    party_id=party.id,
                    user_id=bot_user.id,
                    team=team,
                    bot_profile=profile,
                )
            )
        party.current_players_count += len(bot_rows)
        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL
        await session.flush()
        log_event(
            "FRIENDLY_BOTS_FILLED",
            {
                "party_id": party.id,
                "filled_by": actor_user_id,
                "count": len(bot_rows),
                "team_counts": team_counts,
            },
        )
        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def leave_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
        *,
        group_id: Optional[int] = None,
    ) -> Tuple[Optional[Party], bool]:
        """Leave an open party. Returns ``(party, was_cancelled)``."""
        party = await session.scalar(
            select(Party).where(Party.id == party_id).with_for_update()
        )
        if not party or party.status in (
            PartyStatus.IN_QUEUE, PartyStatus.IN_MATCH, PartyStatus.CANCELLED
        ):
            raise PartyNotFoundException()
        cls._require_group(party, group_id)
        member = await session.scalar(
            select(PartyMember)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        if not member:
            raise InvalidActionException("شما عضو این پارتی نیستید 🍏")

        await session.delete(member)
        party.current_players_count = max(0, party.current_players_count - 1)
        was_cancelled = party.current_players_count == 0
        if was_cancelled:
            party.status = PartyStatus.CANCELLED
        else:
            party.status = PartyStatus.OPEN
        await session.flush()
        log_event(
            Events.PARTY_LEFT,
            {
                "party_id": party.id,
                "user_id": user_id,
                "remaining_players": party.current_players_count,
                "was_cancelled": was_cancelled,
            },
        )
        refreshed = await cls.get_party_by_id(session, party.id) if not was_cancelled else None
        return refreshed, was_cancelled

    @classmethod
    async def cancel_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
        *,
        group_id: Optional[int] = None,
    ) -> Party:
        """Cancel an open party; Friendly parties are creator-cancelled."""
        party = await session.scalar(
            select(Party).where(Party.id == party_id).with_for_update()
        )
        if not party:
            raise PartyNotFoundException()
        cls._require_group(party, group_id)

        if party.status not in (PartyStatus.OPEN, PartyStatus.FULL):
            # Queue cancellation has its own locked transition; an old party
            # button must never cancel/release a live match or queue entry.
            raise InvalidActionException("این پارتی در وضعیت فعلی قابل لغو نیست 🍏")
        if party.match_type == MatchType.FRIENDLY:
            if party.creator_user_id != user_id:
                raise InvalidActionException("فقط سازنده‌ی پارتی می‌تونه اون رو لغو کنه 🍏")
        else:
            member_id = await session.scalar(
                select(PartyMember.id)
                .where(PartyMember.party_id == party_id)
                .where(PartyMember.user_id == user_id)
            )
            if not member_id and party.creator_user_id != user_id:
                raise InvalidActionException("فقط اعضای این پارتی امکان لغو آن را دارند 🍏")

        party.status = PartyStatus.CANCELLED
        await session.flush()
        log_event("PARTY_CANCELLED", {"party_id": party.id, "cancelled_by": user_id})
        return party

    @classmethod
    async def get_party_by_id(cls, session: AsyncSession, party_id: int) -> Optional[Party]:
        """Fetch party with eager-loaded members, users, and game details."""
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .execution_options(populate_existing=True)
            .options(
                selectinload(Party.members).selectinload(PartyMember.user),
                selectinload(Party.game),
                selectinload(Party.creator),
            )
        )
        return await session.scalar(stmt)

    @classmethod
    async def get_active_parties_for_group(
        cls,
        session: AsyncSession,
        group_id: int,
    ) -> List[Party]:
        """Live parties of a group (OPEN, FULL, IN_QUEUE, IN_MATCH)."""
        return await SessionService.get_live_parties_for_group(session, group_id)
