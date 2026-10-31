"""Party service managing group party creation, joining, and leaving.

A party has a MODE (solo / 2v2 / 4v4 = one team's size) and a MATCH TYPE:

- ``normal``: the party is ONE team; it queues for an opponent party.
- ``friendly`` ("بازی با اعضای گروه"): the party holds BOTH teams of one
  group-internal match (up to ``2 x MODE_SLOTS[mode]`` human players) and the
  free slots can be filled with bots before the match is started directly.

Bot filling therefore lives here (party/team logic) and never inside a game.
"""

from typing import List, Optional, Tuple
from sqlalchemy import func, select, and_, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sib.core.errors import (
    ActiveSessionExistsException,
    PartyFullException,
    PartyNotFoundException,
    InvalidActionException,
)
from sib.core.logging import log_event, Events
from sib.database.models import MatchType, Party, PartyMember, PartyStatus, Game, User
from sib.services.bot import BotService, choose_bot_display_names
from sib.services.session import SessionService


class PartyService:
    """Manages party creation, atomic join/leave operations, and lifecycle."""

    MODE_SLOTS = {
        "solo": 1,
        "2v2": 2,
        "4v4": 4,
    }

    @classmethod
    def team_size_for_mode(cls, mode: str) -> int:
        """Slots per team for a game mode (solo=1, 2v2=2, 4v4=4)."""
        return cls.MODE_SLOTS.get(mode, 1)

    @classmethod
    def friendly_slots_for_mode(cls, mode: str) -> int:
        """A friendly party holds BOTH teams: 2 x team size."""
        return 2 * cls.team_size_for_mode(mode)

    @classmethod
    async def _validate_game_mode(
        cls, session: AsyncSession, game_code: str, mode: str
    ) -> Game:
        """Resolve the game and reject a mode the game does not support."""
        game_stmt = select(Game).where(Game.code == game_code).where(Game.is_active == True)
        game = await session.scalar(game_stmt)
        if not game:
            raise InvalidActionException("بازی انتخاب شده معتبر نیست یا غیرفعال است.")

        if mode == "solo" and not game.supports_solo:
            raise InvalidActionException("این بازی از حالت انفرادی پشتیبانی نمی‌کنه.")
        if mode == "2v2" and not game.supports_2v2:
            raise InvalidActionException("این بازی از حالت تیمی ۲ به ۲ پشتیبانی نمی‌کنه.")
        if mode == "4v4" and not game.supports_4v4:
            raise InvalidActionException("این بازی از حالت تیمی ۴ به ۴ پشتیبانی نمی‌کنه.")
        if mode not in cls.MODE_SLOTS:
            raise InvalidActionException("حالت بازی انتخاب شده معتبر نیست.")
        return game

    @classmethod
    async def _remove_other_memberships(cls, session: AsyncSession, user_id: int, keep_party_id=None) -> None:
        """Remove a user's memberships in other parties under their user lock."""
        from sib.database.models import MatchmakingQueue

        stmt = select(PartyMember, Party).join(Party, Party.id == PartyMember.party_id).where(
            PartyMember.user_id == user_id
        ).with_for_update()
        if keep_party_id is not None:
            stmt = stmt.where(PartyMember.party_id != keep_party_id)
        rows = (await session.execute(stmt)).all()
        for member, party in rows:
            # A live match owns its participants until settlement; never detach
            # a player from a game already in progress.
            from sib.database.models import Match, MatchState
            active_match = await session.scalar(
                select(Match.id).where(
                    Match.state == MatchState.ACTIVE,
                    ((Match.party1_id == party.id) | (Match.party2_id == party.id)),
                ).limit(1)
            )
            if active_match:
                continue
            await session.delete(member)
            party.current_players_count = max(0, party.current_players_count - 1)
            if party.current_players_count == 0:
                party.status = PartyStatus.CANCELLED
            elif party.status in (PartyStatus.OPEN, PartyStatus.FULL):
                party.status = PartyStatus.OPEN
            await session.execute(
                update(MatchmakingQueue).where(MatchmakingQueue.party_id == party.id)
                .values(is_active=False)
            )

    @classmethod
    async def create_party(
        cls,
        session: AsyncSession,
        group_id: int,
        creator_user_id: int,
        game_code: str,
        mode: str = "solo",
        match_type: str = MatchType.NORMAL,
    ) -> Party:
        """Create a new party in a group with the creator as first member.

        ``match_type=FRIENDLY`` creates the group-internal party that holds
        both teams (creator starts on team 1); its slots stay OPEN until the
        group fills them or the host fills them with bots.
        """
        # One-active-session rule. The per-user row lock serializes two
        # concurrent creations by the same user (double tap / retry), so
        # only one can observe "no active session".
        await SessionService.lock_user(session, creator_user_id)
        await cls._remove_other_memberships(session, creator_user_id)
        if await SessionService.has_active_session(session, creator_user_id):
            raise ActiveSessionExistsException()

        game = await cls._validate_game_mode(session, game_code, mode)

        if match_type == MatchType.FRIENDLY:
            max_players = cls.friendly_slots_for_mode(mode)
            initial_status = PartyStatus.OPEN
        else:
            max_players = cls.team_size_for_mode(mode)
            # Status: If solo, team is immediately full! If team mode, it starts OPEN
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

        member = PartyMember(
            party_id=party.id,
            user_id=creator_user_id,
            team=1,
        )
        session.add(member)
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
    async def create_friendly_party(
        cls,
        session: AsyncSession,
        group_id: int,
        creator_user_id: int,
        game_code: str,
        mode: str = "solo",
    ) -> Party:
        """Create a friendly ("بازی با اعضای گروه") party for a game mode."""
        return await cls.create_party(
            session,
            group_id,
            creator_user_id,
            game_code,
            mode,
            match_type=MatchType.FRIENDLY,
        )

    @classmethod
    async def _team_counts(cls, session: AsyncSession, party_id: int) -> dict:
        """Members per team for one party (no lazy loads)."""
        rows = await session.execute(
            select(PartyMember.team, func.count(PartyMember.id))
            .where(PartyMember.party_id == party_id)
            .group_by(PartyMember.team)
        )
        return {int(team): int(count) for team, count in rows.all()}

    @classmethod
    async def free_slots(cls, session: AsyncSession, party: Party) -> dict:
        """Free slots per team for a friendly party."""
        counts = await cls._team_counts(session, party.id)
        team_size = party.team_size
        return {
            team: max(0, team_size - counts.get(team, 0))
            for team in (1, 2)
        }

    @classmethod
    async def join_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
        team: Optional[int] = None,
    ) -> Party:
        """Atomically join an open party using row-level locking.

        Friendly parties take up to ``2 x team size`` players: an explicit
        ``team`` (1 or 2) is honoured when that team still has a free slot,
        otherwise the joiner is balanced automatically onto the emptier team.
        Every slot limit is enforced under the same party row lock, so
        concurrent joins can never overfill a team or the party.
        """
        import asyncio
        from sqlalchemy.exc import OperationalError

        # 1. Check one-active-session rule (per-user lock: same user can
        #    never pass this check twice concurrently).
        await SessionService.lock_user(session, user_id)
        await cls._remove_other_memberships(session, user_id, keep_party_id=party_id)
        if await SessionService.has_active_session(session, user_id):
            raise ActiveSessionExistsException()

        # 2. Lock party row with FOR UPDATE (retry once if concurrent modification occurs)
        party = None
        for attempt in range(2):
            try:
                stmt = (
                    select(Party)
                    .where(Party.id == party_id)
                    .with_for_update()
                )
                party = await session.scalar(stmt)
                break
            except OperationalError:
                if attempt == 0:
                    await asyncio.sleep(0.05)
                    continue
                raise PartyFullException()

        if not party:
            raise PartyNotFoundException()

        if party.status != PartyStatus.OPEN:
            if party.status == PartyStatus.FULL:
                raise PartyFullException()
            raise PartyNotFoundException()

        # Check if user is already in this party
        is_member_stmt = (
            select(PartyMember.id)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        if await session.scalar(is_member_stmt):
            raise InvalidActionException("شما در حال حاضر عضو این پارتی هستید 🍏")

        # Check capacity
        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL
            await session.flush()
            raise PartyFullException()

        # Team assignment: normal parties are ONE team (team 1); friendly
        # parties carry both teams and enforce the per-team slot cap.
        if party.is_friendly:
            free = await cls.free_slots(session, party)
            if team is not None and team not in (1, 2):
                raise InvalidActionException("تیم انتخاب شده معتبر نیست 🍏")
            if team is None:
                # Balance the teams (ties prefer team 1, the host's side).
                team = 2 if free.get(2, 0) > free.get(1, 0) else 1
            if free.get(team, 0) <= 0:
                raise PartyFullException()
        else:
            team = 1

        # User lock serializes membership transitions across all parties.
        await cls._remove_other_memberships(session, user_id, keep_party_id=party.id)

        # Add member
        member = PartyMember(party_id=party.id, user_id=user_id, team=team)
        session.add(member)
        party.current_players_count += 1

        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL

        await session.flush()

        log_event(
            Events.PARTY_JOINED,
            {
                "party_id": party.id,
                "user_id": user_id,
                "team": team,
                "current_players": party.current_players_count,
                "max_players": party.max_players,
                "status": party.status,
            },
        )

        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def leave_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Tuple[Optional[Party], bool]:
        """
        User leaves party.
        If the user is the leader (creator_user_id) and other human members
        remain, leadership transfers to the oldest remaining member; if no
        human member remains, the party is disbanded (CANCELLED).
        Returns (party, was_cancelled).
        """
        from sib.database.models import MatchmakingQueue

        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .with_for_update()
        )
        party = await session.scalar(stmt)

        if not party or party.status in (PartyStatus.IN_MATCH, PartyStatus.CANCELLED):
            raise PartyNotFoundException()

        # Check user membership
        member_stmt = (
            select(PartyMember)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        member = await session.scalar(member_stmt)
        if not member:
            raise InvalidActionException("شما عضو این پارتی نیستید 🍏")

        was_leader = party.creator_user_id == user_id

        await session.delete(member)
        party.current_players_count = max(0, party.current_players_count - 1)
        await session.flush()

        next_leader_id = await session.scalar(
            select(PartyMember.user_id)
            .join(User, User.id == PartyMember.user_id)
            .where(PartyMember.party_id == party_id)
            .where(User.is_bot == False)  # noqa: E712
            .order_by(PartyMember.joined_at.asc(), PartyMember.id.asc())
            .limit(1)
        )

        was_cancelled = False
        if party.current_players_count == 0 or next_leader_id is None:
            if party.current_players_count > 0:
                bot_members = list(
                    (
                        await session.scalars(
                            select(PartyMember).where(PartyMember.party_id == party_id)
                        )
                    ).all()
                )
                for bot_member in bot_members:
                    await session.delete(bot_member)
                party.current_players_count = 0
            party.status = PartyStatus.CANCELLED
            was_cancelled = True
        else:
            if was_leader:
                party.creator_user_id = next_leader_id
            party.status = PartyStatus.OPEN

        await session.execute(
            update(MatchmakingQueue)
            .where(
                MatchmakingQueue.party_id == party.id,
                MatchmakingQueue.is_active == True,  # noqa: E712
            )
            .values(is_active=False)
        )

        await session.flush()

        log_event(
            Events.PARTY_LEFT,
            {
                "party_id": party.id,
                "user_id": user_id,
                "remaining_players": party.current_players_count,
                "was_cancelled": was_cancelled,
                "new_leader_id": (
                    party.creator_user_id if (was_leader and not was_cancelled) else None
                ),
            },
        )

        refreshed_party = await cls.get_party_by_id(session, party.id) if not was_cancelled else None
        return refreshed_party, was_cancelled

    @classmethod
    async def leave_all_parties(
        cls,
        session: AsyncSession,
        user_id: int,
    ) -> List[dict]:
        """Leave all active parties the user is currently a member of.

        Reuses ``leave_party`` so membership removal, leadership transfer,
        party disbanding, and matchmaking queue cleanup share one code path.
        """
        from sib.database.models import Match

        await SessionService.lock_user(session, user_id)

        stmt = (
            select(Party)
            .join(PartyMember, PartyMember.party_id == Party.id)
            .where(PartyMember.user_id == user_id)
            .where(Party.status.in_(SessionService.ACTIVE_PARTY_STATUSES))
            .order_by(Party.id.asc())
            .with_for_update()
        )
        active_parties = list((await session.scalars(stmt)).all())

        outcomes: List[dict] = []
        for party in active_parties:
            if party.status == PartyStatus.IN_MATCH:
                live_match_id = await session.scalar(
                    select(Match.id)
                    .where(
                        ((Match.party1_id == party.id) | (Match.party2_id == party.id))
                        & (Match.state.in_(SessionService.ACTIVE_MATCH_STATES))
                    )
                    .limit(1)
                )
                if live_match_id is not None:
                    continue
                await SessionService._restore_party(party)
                await session.flush()

            was_leader = party.creator_user_id == user_id
            updated_party, was_cancelled = await cls.leave_party(
                session, party.id, user_id
            )
            outcomes.append(
                {
                    "party_id": party.id,
                    "was_leader": was_leader,
                    "was_cancelled": was_cancelled,
                    "new_leader_id": (
                        updated_party.creator_user_id
                        if (was_leader and not was_cancelled and updated_party is not None)
                        else None
                    ),
                }
            )

        return outcomes

    @classmethod
    async def cancel_party(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Party:
        """Cancel a party (allowed by creator or group member)."""
        stmt = (
            select(Party)
            .where(Party.id == party_id)
            .with_for_update()
        )
        party = await session.scalar(stmt)
        if not party:
            raise PartyNotFoundException()

        # Check membership
        member_stmt = (
            select(PartyMember.id)
            .where(PartyMember.party_id == party_id)
            .where(PartyMember.user_id == user_id)
        )
        is_member = await session.scalar(member_stmt)
        if not is_member and party.creator_user_id != user_id:
            raise InvalidActionException("فقط اعضای این پارتی امکان لغو آن را دارند 🍏")

        party.status = PartyStatus.CANCELLED
        await session.flush()

        log_event("PARTY_CANCELLED", {"party_id": party.id, "cancelled_by": user_id})
        return party

    @classmethod
    async def enable_friendly(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Party:
        """Turn the creator's own solo party into a friendly 1v1 party.

        This is the «بازی با اعضای گروه» action on a solo party card: the SAME
        party row is reused (so the One-Active-Session rule stays intact) and
        re-opened with two slots - team 1 is the host, team 2 is any other
        group member (or a bot). Only an untouched solo party can be converted.
        """
        party = await session.scalar(
            select(Party).where(Party.id == party_id).with_for_update()
        )
        if not party or party.status == PartyStatus.CANCELLED:
            raise PartyNotFoundException()

        if party.creator_user_id != user_id:
            raise InvalidActionException("فقط سازندهٔ این پارتی می‌تونه بازی رفیقانه بسازه 🍏")
        if party.status not in (PartyStatus.OPEN, PartyStatus.FULL):
            raise InvalidActionException("این پارتی الان توی صف یا مسابقه است؛ اول از اون خارج شو 🍏")
        if party.is_friendly:
            raise InvalidActionException("این پارتی از قبل رفیقانه است 🍏")
        if party.mode != "solo" or party.current_players_count != 1:
            raise InvalidActionException(
                "بازی با اعضای گروه برای حالت انفرادی از همین کارت پارتی شروع می‌شه 🍏"
            )

        party.match_type = MatchType.FRIENDLY
        party.max_players = cls.friendly_slots_for_mode(party.mode)
        party.status = PartyStatus.OPEN
        await session.flush()

        log_event(
            Events.PARTY_FRIENDLY_ENABLED,
            {"party_id": party.id, "group_id": party.group_id, "mode": party.mode},
        )
        return await cls.get_party_by_id(session, party.id)

    @classmethod
    async def fill_empty_slots_with_bots(
        cls,
        session: AsyncSession,
        party_id: int,
        user_id: int,
    ) -> Party:
        """Fill every empty friendly-party slot with a bot (host action).

        The bots are REAL ``User(is_bot=True)`` participants added through the
        normal ``PartyMember`` rows. Their shared randomized name pool and
        persisted profiles work across every supported game.
        Idempotent under the party row lock: once the party is full a second
        press changes nothing and reports a friendly error.
        """
        party = await session.scalar(
            select(Party).where(Party.id == party_id).with_for_update()
        )
        if not party or party.status == PartyStatus.CANCELLED:
            raise PartyNotFoundException()
        if not party.is_friendly:
            raise InvalidActionException("پر کردن با بات فقط توی پارتی رفیقانه ممکنه 🍏")
        if party.status not in (PartyStatus.OPEN, PartyStatus.FULL):
            raise InvalidActionException("این پارتی الان توی صف یا مسابقه است 🍏")
        if party.creator_user_id != user_id:
            raise InvalidActionException("فقط سازندهٔ پارتی می‌تونه جای خالی رو با بات پر کنه 🍏")
        if party.is_full:
            raise InvalidActionException("جای خالی نمونده؛ تیم کامله 🍏")

        game = await session.get(Game, party.game_id)
        game_code = game.code if game else ""

        free = await cls.free_slots(session, party)
        existing_bot_names = list(
            (
                await session.scalars(
                    select(User.first_name)
                    .join(PartyMember, PartyMember.user_id == User.id)
                    .where(
                        PartyMember.party_id == party.id,
                        User.is_bot == True,  # noqa: E712
                    )
                )
            ).all()
        )
        total_bots = sum(free.values())
        bot_names = choose_bot_display_names(
            total_bots,
            existing_names=existing_bot_names,
        )
        ordinal_offset = 0
        name_offset = 0
        for team in (1, 2):
            count = free.get(team, 0)
            if count <= 0:
                continue
            bots = await BotService.create_party_bots(
                session,
                party_id=party.id,
                count=count,
                game_code=game_code,
                mode=party.mode,
                team=team,
                ordinal_offset=ordinal_offset,
                display_names=bot_names[name_offset:name_offset + count],
            )
            for bot_user, profile, bot_team in bots:
                session.add(
                    PartyMember(
                        party_id=party.id,
                        user_id=bot_user.id,
                        team=bot_team,
                        bot_profile=profile,
                    )
                )
                party.current_players_count += 1
            ordinal_offset += count
            name_offset += count

        if party.current_players_count >= party.max_players:
            party.status = PartyStatus.FULL
        await session.flush()

        log_event(
            Events.PARTY_BOTS_FILLED,
            {
                "party_id": party.id,
                "group_id": party.group_id,
                "players": party.current_players_count,
                "max_players": party.max_players,
            },
        )
        return await cls.get_party_by_id(session, party.id)

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
        """Live parties of a group (OPEN, FULL, IN_QUEUE, IN_MATCH).

        Delegates to ``SessionService`` so the Active Games screen lists
        exactly the parties that block the One-Active-Session guard - a
        party waiting in the queue or playing a match is live too and must
        never be invisible (the "ghost active game" bug).
        """
        return await SessionService.get_live_parties_for_group(session, group_id)
