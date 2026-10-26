"""Weekly group league: a reason for a whole group to play *this* week.

The diagnosis was blunt: «سیب» had no reason for a group admin to keep the bot
in the chat, and no reason for a group to play on a Tuesday. The global
leaderboard is all-time wins — a group that arrives late can never catch up,
so it stops trying. This module adds the cheapest fix that changes that: a
**weekly** competition with a small prize, resetting every Saturday.

Nothing here writes on the hot path
----------------------------------
Points are *derived* from ``matches`` — normal (never friendly), non-bot,
finished matches inside the week — so no result handler changed, no counter
can drift, and the league is correct for every match already in the database.
The two tables (``group_league_weeks``, ``group_league_awards``) store only
what cannot be derived: the atomic "this week is paid" claim and the frozen
podium.

Scoring and ranking
-------------------
The table is **multi-source** (Phase 1 of the rescue plan): a group is paid
for the week it actually had, not only for the matches it played.

============================  =============================  =============
source                        points                         week cap
============================  =============================  =============
group matches                 win 3, draw 1, loss 0           45
group wars                    win 10, presence 3,            30
                              1 per 100 damage dealt
apple mine (inside a group)   1 per 25 normal apples          10
apple duels (inside a group)  1 per distinct member           5
============================  =============================  =============

Every number is one ``league_points`` config row, so the whole league can be
rebalanced without a deploy. The caps are what stop a single member from
carrying a group and what stop any one source from owning the table: matches
stay the backbone (50% of the theoretical 90), wars are worth real effort
(33%), and the two daily loops are a small bonus.

* match win = 3 points, draw = 1, loss = 0 (classic football scoring);
* bot matches and friendly matches **never** count: farming bots must not
  lift a group, and friendly games are the same group on both sides;
* bot users never count anywhere (wars, mine, duels), and only *normal*
  apples dug inside a group count — special apples are rare loot, not work;
* an **apple duel is always inside one group** (``apple_duels.group_id`` is
  the group both players belong to), so scoring its win would reward a group
  for beating itself. It is scored as *activity*: one point per member who
  finished a duel, hard cap 5 — never a win;
* tie-breaks are deterministic: points, then wins, then draws, then group id.
  ``GROUP BY`` ordering is never relied on for a public ranking.

Payout
------
Every player who actually played for a top-3 group that week is paid (not every
member — presence is not participation). The credit is an auditable wallet
transaction with the idempotency key
``league:<week>:<group>:<user>``, so a crashed or doubled finalize can never
pay twice. ``league_first_week`` (seeded by migration 064) stops the first
deploy from paying a retroactive prize for a week that ended before the league
existed.

The champion card
----------------
Rank 1 also hands out the **champion card** (``league_champion_card_code``,
``hero`` by default) to every player who actually played for the winning
group. The card is a trophy: it is not in the shop, the daily chest never
drops it, and the starter pack can never hand it out — the league is the
only way in. Ownership is checked before the grant, so winning three weeks
in a row still leaves one card (the reward for repeat wins stays the Apples).

Everything is config-driven: ``league_enabled`` (kill switch),
``league_rewards`` (``{"1": 300, "2": 150, "3": 100}`` per participating
player), ``league_min_points`` (a quiet week has no champion),
``league_champion_card_code`` (the trophy card, empty to disable).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.core.logging import Events, log_event, logger
from sib.core.text import escape_markdown
from sib.database.models import (
    AppleDuel,
    AppleMineShift,
    Character,
    Group,
    GroupWar,
    GroupLeagueAward,
    GroupLeagueWeek,
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    PlayerCard,
    ReleaseStatus,
    User,
    WarAttack,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.sib_claim import fa_digits

TEHRAN = ZoneInfo("Asia/Tehran")

#: Saturday (``date.weekday()``: Monday == 0). The Persian week starts on
#: Saturday, so the league resets at Tehran midnight on Friday->Saturday.
WEEK_START_WEEKDAY = 5

POINTS_WIN = 3
POINTS_DRAW = 1

LEAGUE_REASON = "group_league_reward"
LEAGUE_KEY_FORMAT = "league:{week}:{group}:{user}"

#: Apples per participating player, by rank. Small enough to be a bonus, big
#: enough to matter next to a 300-Apple card.
DEFAULT_REWARDS: dict[int, int] = {1: 300, 2: 150, 3: 100}

#: The card only the league can hand out (``shop_enabled`` is false for it, so
#: neither the shop, the daily chest nor the starter pack can grant it).
DEFAULT_CHAMPION_CARD_CODE = "hero"

#: The whole scoring table, in one place. Migration 065 seeds the same values
#: into ``system_config.league_points``; an operator can rebalance any of them
#: at runtime and nothing else has to change.
#:
#: * ``match_*`` — classic football scoring, capped so one group cannot run
#:   away with the table in a single evening of matches;
#: * ``war_*`` — a won war is worth more than three match wins, and simply
#:   showing up is worth a draw; ``war_damage_step`` turns dealt damage into
#:   points so the losing side still earns its effort;
#: * ``mine_*`` — the daily loop, deliberately small and capped;
#: * ``duel_*`` — activity only (see the module docstring), per distinct member.
DEFAULT_POINTS: dict[str, int] = {
    "match_win": 3,
    "match_draw": 1,
    "match_cap": 45,
    "war_win": 10,
    "war_presence": 3,
    "war_damage_step": 100,
    "war_cap": 30,
    "mine_apples_step": 25,
    "mine_cap": 10,
    "duel_member": 1,
    "duel_cap": 5,
}

#: Guards against a typo in config turning the league into a farm: a step of 0
#: would mean "one point per zero damage", so the parser refuses it.
_MIN_STEP = 1

#: Rows shown on the panel (the podium plus the chasing pack).
LEAGUE_TABLE_LIMIT = 10

#: How many past weeks one sweep may settle. A restart is not a reason to lose
#: a prize, but the look-back must stay bounded: 4 weeks covers a long outage
#: and still costs one primary-key lookup per week when nothing is pending.
LEAGUE_LOOKBACK_WEEKS = 4

_PERSIAN_MONTHS = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

MERIT_BADGE = "🏆"


# ---------------------------------------------------------------------------
# Calendar helpers (pure functions, unit-tested against known anchors)
# ---------------------------------------------------------------------------
def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to the Solar Hijri (Jalali) date.

    A straight port of the well-known ``jalaali-js`` algorithm; no dependency
    and no lookup tables to maintain. Used ONLY for display text.
    """
    g_days_in_month = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2, gm2, gd2 = gy - 1600, gm - 1, gd - 1
    g_day_no = (
        365 * gy2
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
    )
    g_day_no += g_days_in_month[gm2] + gd2
    if gm > 2 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    if j_day_no < 186:
        jm = 1 + j_day_no // 31
        jd = 1 + j_day_no % 31
    else:
        jm = 7 + (j_day_no - 186) // 30
        jd = 1 + (j_day_no - 186) % 30
    return jy, jm, jd


def jalali_label(moment: date) -> str:
    """«۲ آبان» — a Persian day label for user-facing text."""
    jy, jm, jd = gregorian_to_jalali(moment.year, moment.month, moment.day)
    return f"{fa_digits(jd)} {_PERSIAN_MONTHS[jm - 1]}"


def week_key(moment: datetime) -> str:
    """The league week a moment belongs to (Tehran Saturday, ``YYYY-MM-DD``)."""
    local = moment.astimezone(TEHRAN)
    days_since_start = (local.weekday() - WEEK_START_WEEKDAY) % 7
    return (local - timedelta(days=days_since_start)).date().isoformat()


def week_window(key: str) -> tuple[datetime, datetime]:
    """``[start, end)`` of a league week in UTC (Tehran midnight boundaries)."""
    start_local = datetime.fromisoformat(key).replace(tzinfo=TEHRAN)
    end_local = start_local + timedelta(days=7)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def shift_week(key: str, weeks: int) -> str:
    """The week ``weeks`` before/after ``key`` (``key`` is already a Saturday)."""
    return (date.fromisoformat(key) + timedelta(weeks=weeks)).isoformat()


def week_label(key: str) -> str:
    """«۲ تا ۸ آبان» — the human label of a week."""
    start = date.fromisoformat(key)
    return f"{jalali_label(start)} تا {jalali_label(start + timedelta(days=6))}"


def time_left(now: datetime, key: str) -> timedelta:
    """How much of the week is still ahead (negative once it is over)."""
    _start, end = week_window(key)
    return end - now


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LeagueRow:
    """One group's line in a week's table."""

    rank: int
    group_id: int
    title: str
    points: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    matches: int = 0
    #: Where the points came from (derived, never stored). The four fields add
    #: up to ``points`` exactly, which is what the panel prints.
    match_points: int = 0
    war_points: int = 0
    mine_points: int = 0
    duel_points: int = 0
    wars: int = 0
    war_wins: int = 0
    mine_apples: int = 0
    duelists: int = 0


@dataclass(frozen=True)
class LeagueAwardResult:
    rank: int
    group_id: int
    title: str
    points: int
    players_paid: int
    apples_per_player: int
    #: Champion cards actually handed out (rank 1 only, owners excluded).
    cards_paid: int = 0

    @property
    def apples_total(self) -> int:
        return self.players_paid * self.apples_per_player


@dataclass(frozen=True)
class LeagueFinal:
    """What one finalize attempt did (logged, and asserted in the tests)."""

    week_key: str
    finalized: bool = False
    already: bool = False
    reason: str = ""
    awards: tuple[LeagueAwardResult, ...] = ()
    apples_paid: int = 0

    @property
    def ran(self) -> bool:
        return not self.reason


@dataclass(frozen=True)
class PodiumRow:
    """A frozen award row joined with the group title (for history text)."""

    week_key: str
    rank: int
    group_id: int
    title: str
    points: int
    players_paid: int
    apples_per_player: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class GroupLeagueService:
    """Derive weekly standings from ``matches`` and pay the podium once."""

    # -- configuration ---------------------------------------------------
    @classmethod
    async def settings(cls, session: AsyncSession) -> dict:
        rewards_raw = await ConfigService.get(session, "league_rewards")
        if rewards_raw is None:
            rewards_raw = DEFAULT_REWARDS
        return {
            "enabled": bool(await ConfigService.get(session, "league_enabled")),
            "rewards": parse_rewards(rewards_raw),
            "min_points": max(
                0, int(await ConfigService.get(session, "league_min_points") or 0)
            ),
            "first_week": str(
                await ConfigService.get(session, "league_first_week") or ""
            ),
            "champion_card": str(
                await ConfigService.get(session, "league_champion_card_code")
                or ""
            ),
            "points": parse_points(
                await ConfigService.get(session, "league_points")
            ),
        }

    # -- standings -------------------------------------------------------
    @classmethod
    async def _week_matches(
        cls, session: AsyncSession, key: str
    ) -> list[Match]:
        start, end = week_window(key)
        rows = await session.scalars(
            select(Match)
            .where(Match.state == MatchState.FINISHED)
            .where(Match.match_type == MatchType.NORMAL)
            .where(Match.is_bot_match.is_(False))
            .where(Match.finished_at.is_not(None))
            .where(Match.finished_at >= start)
            .where(Match.finished_at < end)
        )
        return list(rows.all())

    @classmethod
    async def _week_wars(
        cls, session: AsyncSession, key: str
    ) -> list[GroupWar]:
        """Wars that ENDED inside the week (an unfinished war can wait)."""
        start, end = week_window(key)
        rows = await session.scalars(
            select(GroupWar)
            .where(GroupWar.status == "finished")
            .where(GroupWar.ends_at >= start)
            .where(GroupWar.ends_at < end)
        )
        return list(rows.all())

    @classmethod
    async def _week_war_damage(
        cls, session: AsyncSession, key: str
    ) -> dict[int, int]:
        """Damage each group DEALT with its own attacks this week."""
        start, end = week_window(key)
        rows = (
            await session.execute(
                select(WarAttack.source_group_id, func.sum(WarAttack.damage))
                .where(WarAttack.created_at >= start)
                .where(WarAttack.created_at < end)
                .group_by(WarAttack.source_group_id)
            )
        ).all()
        return {int(group_id): int(damage or 0) for group_id, damage in rows}

    @classmethod
    async def _week_mine_apples(
        cls, session: AsyncSession, key: str
    ) -> dict[int, int]:
        """NORMAL apples dug during a shift that happened inside a group.

        Special apples are rare loot, not work, so they never score; shifts
        outside a group have no group to credit.
        """
        start, end = week_window(key)
        rows = (
            await session.execute(
                select(AppleMineShift.group_id, func.sum(AppleMineShift.amount))
                .join(User, User.id == AppleMineShift.user_id)
                .where(AppleMineShift.group_id.is_not(None))
                .where(AppleMineShift.item_code == "apple_normal")
                .where(AppleMineShift.worked_at >= start)
                .where(AppleMineShift.worked_at < end)
                .where(User.is_bot.is_(False))
                .group_by(AppleMineShift.group_id)
            )
        ).all()
        return {
            int(group_id): int(apples or 0) for group_id, apples in rows
        }

    @classmethod
    async def _week_duelists(
        cls, session: AsyncSession, key: str
    ) -> dict[int, set[int]]:
        """Members of each group who finished an apple duel this week."""
        start, end = week_window(key)
        rows = (
            await session.execute(
                select(AppleDuel.group_id, AppleDuel.inviter_id, AppleDuel.recipient_id)
                .join(User, User.id == AppleDuel.inviter_id)
                .where(AppleDuel.status == "ACCEPTED")
                .where(AppleDuel.battle_id.is_not(None))
                .where(AppleDuel.created_at >= start)
                .where(AppleDuel.created_at < end)
                .where(User.is_bot.is_(False))
            )
        ).all()
        duelists: dict[int, set[int]] = {}
        for group_id, inviter_id, recipient_id in rows:
            bucket = duelists.setdefault(int(group_id), set())
            bucket.add(int(inviter_id))
            bucket.add(int(recipient_id))
        return duelists

    @classmethod
    async def standings(
        cls,
        session: AsyncSession,
        key: str,
        points: Optional[dict[str, int]] = None,
    ) -> list[LeagueRow]:
        """Every group that earned anything this week, best first.

        All four sources are *derived* from existing tables inside the Tehran
        week, so no counter can drift and no result handler had to change.
        ``points`` is the ``league_points`` table; it is read from config when
        the caller does not already have it.
        """
        conf = points if points is not None else parse_points(
            await ConfigService.get(session, "league_points")
        )

        tally: dict[int, dict[str, int]] = {}

        def row_for(group_id: int) -> dict[str, int]:
            return tally.setdefault(
                int(group_id),
                {
                    "points": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "matches": 0,
                    "match_points": 0,
                    "war_points": 0,
                    "mine_points": 0,
                    "duel_points": 0,
                    "wars": 0,
                    "war_wins": 0,
                    "mine_apples": 0,
                    "duelists": 0,
                },
            )

        # --- 1. group matches -------------------------------------------
        for match in await cls._week_matches(session, key):
            groups = {gid for gid in (match.group1_id, match.group2_id) if gid}
            if not groups:
                continue
            for group_id in groups:
                tally_row = row_for(group_id)
                tally_row["matches"] += 1
                if match.is_draw:
                    tally_row["match_points"] += conf["match_draw"]
                    tally_row["draws"] += 1
                elif match.winner_group_id == group_id:
                    tally_row["match_points"] += conf["match_win"]
                    tally_row["wins"] += 1
                elif match.winner_group_id is not None:
                    tally_row["losses"] += 1
                else:
                    # Neither a draw nor a winner: a corrupt result must not
                    # silently hand out points.
                    logger.warning(
                        "league: match %s has no winner and is not a draw",
                        match.id,
                    )

        # --- 2. group wars (result + damage dealt) ----------------------
        for war in await cls._week_wars(session, key):
            sides = {gid for gid in (war.attacker_id, war.defender_id) if gid}
            for group_id in sides:
                tally_row = row_for(group_id)
                tally_row["wars"] += 1
                if war.winner_id == group_id:
                    tally_row["war_points"] += conf["war_win"]
                    tally_row["war_wins"] += 1
                else:
                    # Showing up is worth a draw: the losing (or drawn) side
                    # still spent energy and items on the war.
                    tally_row["war_points"] += conf["war_presence"]
        step = conf["war_damage_step"]
        for group_id, damage in (await cls._week_war_damage(session, key)).items():
            row_for(group_id)["war_points"] += damage // step

        # --- 3. apple mine (normal apples only) -------------------------
        for group_id, apples in (await cls._week_mine_apples(session, key)).items():
            tally_row = row_for(group_id)
            tally_row["mine_apples"] = apples
            tally_row["mine_points"] = apples // conf["mine_apples_step"]

        # --- 4. apple duels (activity, one point per member) ------------
        for group_id, members in (await cls._week_duelists(session, key)).items():
            tally_row = row_for(group_id)
            tally_row["duelists"] = len(members)
            tally_row["duel_points"] = len(members) * conf["duel_member"]

        if not tally:
            return []

        # --- 5. caps, totals, deterministic ranking ---------------------
        for tally_row in tally.values():
            tally_row["match_points"] = min(
                tally_row["match_points"], conf["match_cap"]
            )
            tally_row["war_points"] = min(
                tally_row["war_points"], conf["war_cap"]
            )
            tally_row["mine_points"] = min(
                tally_row["mine_points"], conf["mine_cap"]
            )
            tally_row["duel_points"] = min(
                tally_row["duel_points"], conf["duel_cap"]
            )
            tally_row["points"] = (
                tally_row["match_points"]
                + tally_row["war_points"]
                + tally_row["mine_points"]
                + tally_row["duel_points"]
            )

        titles = dict(
            (
                await session.execute(
                    select(Group.id, Group.title).where(
                        Group.id.in_(list(tally))
                    )
                )
            ).all()
        )

        ordered = sorted(
            tally.items(),
            key=lambda item: (
                -item[1]["points"],
                -item[1]["wins"],
                -item[1]["draws"],
                item[0],
            ),
        )
        return [
            LeagueRow(
                rank=index,
                group_id=group_id,
                title=titles.get(group_id, f"گروه {group_id}"),
                **row,
            )
            for index, (group_id, row) in enumerate(ordered, start=1)
        ]

    @classmethod
    async def group_row(
        cls,
        session: AsyncSession,
        group_id: int,
        key: str,
        points: Optional[dict[str, int]] = None,
    ) -> Optional[LeagueRow]:
        for row in await cls.standings(session, key, points=points):
            if row.group_id == group_id:
                return row
        return None

    # -- history ---------------------------------------------------------
    @classmethod
    async def finalized_week(
        cls, session: AsyncSession, key: str
    ) -> Optional[GroupLeagueWeek]:
        return await session.get(GroupLeagueWeek, key)

    @classmethod
    async def recent_podium(
        cls, session: AsyncSession, *, weeks: int = 1
    ) -> list[PodiumRow]:
        """The podium of the most recent finalized week(s), rank order."""
        keys = list(
            (
                await session.scalars(
                    select(GroupLeagueWeek.week_key)
                    .where(GroupLeagueWeek.finalized_at.is_not(None))
                    .order_by(GroupLeagueWeek.week_key.desc())
                    .limit(weeks)
                )
            ).all()
        )
        if not keys:
            return []
        rows = (
            await session.execute(
                select(GroupLeagueAward, Group.title)
                .outerjoin(Group, Group.id == GroupLeagueAward.group_id)
                .where(GroupLeagueAward.week_key.in_(keys))
                .order_by(
                    GroupLeagueAward.week_key.desc(), GroupLeagueAward.rank.asc()
                )
            )
        ).all()
        return [
            PodiumRow(
                week_key=award.week_key,
                rank=award.rank,
                group_id=award.group_id,
                title=title or f"گروه {award.group_id}",
                points=award.points,
                players_paid=award.players_paid,
                apples_per_player=award.apples_per_player,
            )
            for award, title in rows
        ]

    @classmethod
    async def crown_holder(
        cls, session: AsyncSession
    ) -> Optional[PodiumRow]:
        """The most recent champion (drives the «قهرمان هفتهٔ گذشته» line)."""
        podium = await cls.recent_podium(session, weeks=1)
        return podium[0] if podium else None

    # -- finalization ----------------------------------------------------
    @classmethod
    async def finalize_week(
        cls,
        session: AsyncSession,
        key: str,
        *,
        now: Optional[datetime] = None,
        settings: Optional[dict] = None,
    ) -> LeagueFinal:
        """Pay a finished week's podium exactly once.

        Safe to call repeatedly: the ``group_league_weeks`` primary key is the
        claim, and every wallet credit carries an idempotency key, so a crash
        between "claim" and "commit" is repaired by simply running again.
        """
        moment = now or datetime.now(timezone.utc)
        conf = settings if settings is not None else await cls.settings(session)

        if not conf["enabled"]:
            return LeagueFinal(week_key=key, reason="disabled")
        first_week = conf["first_week"]
        if first_week and key < first_week:
            # Never pay a prize for a week that ended before the league existed.
            return LeagueFinal(week_key=key, reason="before first week")
        if week_window(key)[1] > moment:
            return LeagueFinal(week_key=key, reason="week still running")

        claim = await session.scalar(
            select(GroupLeagueWeek)
            .where(GroupLeagueWeek.week_key == key)
            .with_for_update()
        )
        if claim is not None and claim.finalized_at is not None:
            return LeagueFinal(week_key=key, already=True)

        if claim is None:
            try:
                async with session.begin_nested():
                    claim = GroupLeagueWeek(week_key=key)
                    session.add(claim)
                    await session.flush()
            except IntegrityError:
                # Another worker claimed it between our SELECT and INSERT.
                claim = await session.scalar(
                    select(GroupLeagueWeek)
                    .where(GroupLeagueWeek.week_key == key)
                    .with_for_update()
                )
                if claim is None or claim.finalized_at is not None:
                    return LeagueFinal(week_key=key, already=True)

        table = await cls.standings(session, key, points=conf["points"])
        podium = [
            row for row in table[:3] if row.points >= conf["min_points"]
        ]

        champion_card = await cls.champion_card(session, conf["champion_card"])

        awards: list[LeagueAwardResult] = []
        apples_paid = 0
        for row in podium:
            per_player = conf["rewards"].get(row.rank, 0)
            players = await cls._participants(session, key, row.group_id)
            paid = 0
            cards_paid = 0
            if row.rank == 1 and champion_card is not None:
                cards_paid = await cls._grant_champion_card(
                    session, players, champion_card
                )
            if per_player > 0:
                for user_id in players:
                    await AppleWalletService.earn(
                        session,
                        user_id,
                        per_player,
                        reason=LEAGUE_REASON,
                        ref_type="group_league",
                        ref_id=None,
                        idempotency_key=LEAGUE_KEY_FORMAT.format(
                            week=key, group=row.group_id, user=user_id
                        ),
                    )
                    paid += 1
                apples_paid += paid * per_player

            session.add(
                GroupLeagueAward(
                    week_key=key,
                    rank=row.rank,
                    group_id=row.group_id,
                    points=row.points,
                    wins=row.wins,
                    draws=row.draws,
                    losses=row.losses,
                    matches=row.matches,
                    players_paid=paid,
                    apples_per_player=per_player,
                )
            )
            awards.append(
                LeagueAwardResult(
                    rank=row.rank,
                    group_id=row.group_id,
                    title=row.title,
                    points=row.points,
                    players_paid=paid,
                    apples_per_player=per_player,
                    cards_paid=cards_paid,
                )
            )

        claim.finalized_at = moment
        claim.winner_group_id = podium[0].group_id if podium else None
        claim.winner_points = podium[0].points if podium else None
        claim.apples_paid = apples_paid
        await session.flush()

        log_event(
            Events.LEAGUE_FINALIZED,
            {
                "action": "group_league",
                "week": key,
                "groups": len(awards),
                "apples_paid": apples_paid,
                "winner_group_id": claim.winner_group_id,
            },
        )
        return LeagueFinal(
            week_key=key,
            finalized=True,
            awards=tuple(awards),
            apples_paid=apples_paid,
        )

    @classmethod
    async def _participants(
        cls, session: AsyncSession, key: str, group_id: int
    ) -> list[int]:
        """Humans who actually did something for ``group_id`` that week.

        Every scoring source counts, so a group that won its week at war (or
        kept mining every day) still pays the people who did the work — not
        only the ones who happened to play a match. Presence is still not
        participation: a member who did nothing gets nothing.
        """
        start, end = week_window(key)
        players: set[int] = set()

        # Matches: the player must be on THIS group's side of the match.
        match_ids: dict[int, int] = {}
        for match in await cls._week_matches(session, key):
            if match.group1_id == group_id:
                match_ids[match.id] = 1
            elif match.group2_id == group_id:
                match_ids[match.id] = 2
        if match_ids:
            rows = (
                await session.execute(
                    select(
                        MatchPlayer.match_id, MatchPlayer.user_id, MatchPlayer.team
                    )
                    .join(User, User.id == MatchPlayer.user_id)
                    .where(MatchPlayer.match_id.in_(list(match_ids)))
                    .where(User.is_bot.is_(False))
                )
            ).all()
            for match_id, user_id, team in rows:
                if team == match_ids[match_id]:
                    players.add(int(user_id))

        # Wars: whoever attacked for this group during the week.
        players.update(
            int(user_id)
            for user_id in (
                await session.scalars(
                    select(WarAttack.user_id)
                    .join(User, User.id == WarAttack.user_id)
                    .where(WarAttack.source_group_id == group_id)
                    .where(WarAttack.created_at >= start)
                    .where(WarAttack.created_at < end)
                    .where(User.is_bot.is_(False))
                )
            ).all()
        )

        # Apple mine: whoever worked a shift inside this group.
        players.update(
            int(user_id)
            for user_id in (
                await session.scalars(
                    select(AppleMineShift.user_id)
                    .join(User, User.id == AppleMineShift.user_id)
                    .where(AppleMineShift.group_id == group_id)
                    .where(AppleMineShift.worked_at >= start)
                    .where(AppleMineShift.worked_at < end)
                    .where(User.is_bot.is_(False))
                )
            ).all()
        )

        # Duels inside this group: both sides played.
        for inviter_id, recipient_id in (
            await session.execute(
                select(AppleDuel.inviter_id, AppleDuel.recipient_id)
                .join(User, User.id == AppleDuel.inviter_id)
                .where(AppleDuel.group_id == group_id)
                .where(AppleDuel.status == "ACCEPTED")
                .where(AppleDuel.battle_id.is_not(None))
                .where(AppleDuel.created_at >= start)
                .where(AppleDuel.created_at < end)
                .where(User.is_bot.is_(False))
            )
        ).all():
            players.add(int(inviter_id))
            players.add(int(recipient_id))

        return sorted(players)

    @classmethod
    async def champion_card(
        cls, session: AsyncSession, code: str
    ) -> Optional[Character]:
        """The trophy card for rank 1, or ``None`` when it cannot be granted.

        A missing, inactive or unreleased card is not an error: the Apple
        prizes must still be paid on a deployment that has not seeded the
        card yet.
        """
        if not code:
            return None
        character = await session.scalar(
            select(Character).where(Character.code == code)
        )
        if (
            character is None
            or not character.is_active
            or character.release_status != ReleaseStatus.RELEASED
        ):
            return None
        return character

    @classmethod
    async def _grant_champion_card(
        cls,
        session: AsyncSession,
        players: Sequence[int],
        character: Character,
    ) -> int:
        """Hand the trophy card to each winner who does not own it yet.

        One card per character per player is the game's oldest card rule, so
        this is a query-then-insert (not a blind insert) and repeats are
        skipped silently — a player who already won keeps their single card.
        """
        if not players:
            return 0
        owned = set(
            (
                await session.scalars(
                    select(PlayerCard.user_id).where(
                        PlayerCard.character_id == character.id,
                        PlayerCard.user_id.in_(list(players)),
                    )
                )
            ).all()
        )
        granted = 0
        for user_id in players:
            if user_id in owned:
                continue
            session.add(
                PlayerCard(
                    user_id=user_id, character_id=character.id, level=1
                )
            )
            granted += 1
        if granted:
            await session.flush()
            log_event(
                Events.CHARACTER_EVENT,
                {
                    "action": "champion_card_granted",
                    "character": character.code,
                    "count": granted,
                },
            )
        return granted

    @classmethod
    async def finalize_due_weeks(
        cls,
        bot,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[LeagueFinal, ...]:
        """Finalize the week that just ended and tell the podium's groups.

        Called from the maintenance cycle behind its own try/except: a league
        problem can never affect gameplay. Announcements are best-effort (the
        bot may have been removed from a group), and are only attempted for a
        week this call actually finalized.
        """
        from sib.database.connection import get_session

        moment = now or datetime.now(timezone.utc)
        current = week_key(moment)
        keys = [
            shift_week(current, -offset)
            for offset in range(LEAGUE_LOOKBACK_WEEKS, 0, -1)
        ]

        results: list[LeagueFinal] = []
        try:
            async with get_session() as session:
                conf = await cls.settings(session)
                if not conf["enabled"]:
                    return (LeagueFinal(week_key=current, reason="disabled"),)
                for key in keys:
                    results.append(
                        await cls.finalize_week(
                            session, key, now=moment, settings=conf
                        )
                    )
        except Exception:
            logger.exception(
                "group league finalize failed weeks=%s", ",".join(keys)
            )
            return (LeagueFinal(week_key=current, reason="failed"),)

        # Announce only after the payouts are committed, and only to groups
        # whose players were actually paid.
        if bot is not None:
            for result in results:
                if not result.finalized or not result.awards:
                    continue
                text = format_champion_announcement(result)
                for award in result.awards:
                    if award.players_paid <= 0:
                        continue
                    try:
                        await bot.send_message(
                            award.group_id, text, parse_mode=None
                        )
                    except Exception:  # pragma: no cover - depends on Telegram
                        logger.warning(
                            "could not announce league result week=%s group=%s",
                            result.week_key,
                            award.group_id,
                            exc_info=True,
                        )
        return tuple(results)


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------
def parse_rewards(raw) -> dict[int, int]:
    """Normalise ``league_rewards`` (dict, JSON string, or ``None``)."""
    if raw is None:
        return dict(DEFAULT_REWARDS)
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("invalid league_rewards config: %r", raw)
            return dict(DEFAULT_REWARDS)
    if not isinstance(raw, dict):
        return dict(DEFAULT_REWARDS)
    rewards: dict[int, int] = {}
    for key, value in raw.items():
        try:
            rank = int(key)
            apples = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= rank <= 3 and apples >= 0:
            rewards[rank] = apples
    return rewards


def parse_points(raw) -> dict[str, int]:
    """Normalise ``league_points`` (dict, JSON string, or ``None``).

    Unknown keys are ignored, negative values are refused, and a division
    step (``war_damage_step`` / ``mine_apples_step``) is clamped to at least
    one so a typo can never turn "points per 100 damage" into infinity.
    """
    points = dict(DEFAULT_POINTS)
    if raw is None:
        return points
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("invalid league_points config: %r", raw)
            return points
    if not isinstance(raw, dict):
        return points
    steps = ("war_damage_step", "mine_apples_step")
    for key, value in raw.items():
        if key not in DEFAULT_POINTS:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if key in steps:
            points[key] = max(_MIN_STEP, number)
        elif number >= 0:
            points[key] = number
    return points


# ---------------------------------------------------------------------------
# Persian text
# ---------------------------------------------------------------------------
PANEL_TITLE = f"{MERIT_BADGE} **لیگ هفتگی گروه‌ها**"


def format_countdown(now: datetime, key: str) -> str:
    remaining = time_left(now, key)
    if remaining.total_seconds() <= 0:
        return "لیگ این هفته تموم شده؛ نتایج به‌زودی نهایی می‌شه."
    days = remaining.days
    hours = remaining.seconds // 3600
    if days:
        return f"⏳ {fa_digits(days)} روز و {fa_digits(hours)} ساعت تا پایان لیگ این هفته."
    return f"⏳ {fa_digits(hours)} ساعت تا پایان لیگ این هفته."


def format_table(rows: Sequence[LeagueRow], *, limit: int = LEAGUE_TABLE_LIMIT) -> str:
    if not rows:
        return "این هفته هنوز هیچ بازی واقعی‌ای ثبت نشده. اولین نبرد رو شروع کن!"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for row in rows[:limit]:
        badge = medals.get(row.rank, f"**{fa_digits(row.rank)}.**")
        record = f"{fa_digits(row.wins)} برد، {fa_digits(row.draws)} مساوی"
        if row.wars:
            record += f"، {fa_digits(row.war_wins)} جنگ‌برد"
        lines.append(
            f"{badge} **{escape_markdown(row.title)}** — "
            f"{fa_digits(row.points)} امتیاز ({record})"
        )
    return "\n".join(lines)


def format_breakdown(row: LeagueRow) -> str:
    """«🎮 بازی ۱۸ + ⚔️ جنگ ۱۹ + ⛏ معدن ۲ + 🥊 دوئل ۴» — where points came from.

    Only the sources that actually scored are shown, so a pure-match week
    stays one short line instead of four empty ones.
    """
    parts = []
    if row.match_points:
        parts.append(f"🎮 بازی {fa_digits(row.match_points)}")
    if row.war_points:
        parts.append(f"⚔️ جنگ {fa_digits(row.war_points)}")
    if row.mine_points:
        parts.append(f"⛏ معدن {fa_digits(row.mine_points)}")
    if row.duel_points:
        parts.append(f"🥊 دوئل {fa_digits(row.duel_points)}")
    if not parts:
        return "هنوز امتیازی جمع نشده"
    return " + ".join(parts)


def format_reward_ladder(rewards: dict[int, int], champion_card_name: str = "") -> str:
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    parts = [
        f"{medals[rank]} {fa_digits(apples)} سیب"
        for rank, apples in sorted(rewards.items())
        if apples > 0
    ]
    if not parts and not champion_card_name:
        return ""
    lines = []
    if parts:
        lines.append(
            "🎁 جایزهٔ هر بازیکنِ بازی‌کردهٔ گروه‌های برتر: " + " | ".join(parts)
        )
    if champion_card_name:
        lines.append(
            f"🏆 کارت قهرمان (**{escape_markdown(champion_card_name)}**) هم "
            "فقط برای بازیکن‌های گروه اول — یک‌بار برای همیشه."
        )
    return "\n".join(lines)


def format_panel(
    *,
    now: datetime,
    key: str,
    rows: Sequence[LeagueRow],
    own: Optional[LeagueRow],
    rewards: dict[int, int],
    last_week: Optional[str] = None,
    last_awards: Sequence[PodiumRow] = (),
    is_group: bool = True,
    min_points: int = 0,
    champion_card_name: str = "",
) -> str:
    """The whole «لیگ هفته» panel text."""
    lines = [PANEL_TITLE, f"🗓 هفتهٔ {week_label(key)}", format_countdown(now, key), ""]

    if is_group:
        if own is not None:
            lines.append(
                f"📍 گروه ما: رتبهٔ **{fa_digits(own.rank)}** با "
                f"**{fa_digits(own.points)}** امتیاز "
                f"({fa_digits(own.matches)} بازی)"
            )
            lines.append(f"└ {format_breakdown(own)}")
        else:
            lines.append("📍 گروه ما این هفته هنوز بازی‌ای نکرده.")
        lines.append("")

    lines.append("**جدول این هفته**")
    lines.append(format_table(rows))

    ladder = format_reward_ladder(rewards, champion_card_name)
    if ladder:
        lines.extend(["", ladder])

    if last_awards:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        label = week_label(last_week) if last_week else "هفتهٔ گذشته"
        lines.append("")
        lines.append(f"**قهرمان‌های {label}**")
        for award in last_awards:
            lines.append(
                f"{medals.get(award.rank, '•')} "
                f"**{escape_markdown(award.title)}** — "
                f"{fa_digits(award.points)} امتیاز، "
                f"{fa_digits(award.players_paid)} بازیکن × "
                f"{fa_digits(award.apples_per_player)} سیب"
            )

    lines.extend(
        [
            "",
            "ℹ️ امتیاز از چهار جا می‌آد: نبرد گروهی (برد ۳، مساوی ۱)، جنگ "
            "گروه‌ها (برد ۱۰ + دمیج)، معدن سیب داخل گروه، و دوئل اعضا. "
            "برد از بات و بازی دوستانه حساب نمی‌شه.",
        ]
    )
    if min_points > 0:
        lines.append(
            f"ℹ️ برای گرفتن جایزه، گروه باید حداقل {fa_digits(min_points)} "
            "امتیاز در همین هفته بگیره."
        )
    return "\n".join(lines)


def format_champion_announcement(result: LeagueFinal) -> str:
    """The message posted in each podium group once the week is finalized."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [
        f"{MERIT_BADGE} **لیگ هفتهٔ {week_label(result.week_key)} تموم شد!**",
        "",
    ]
    for award in result.awards:
        medal = medals.get(award.rank, "•")
        lines.append(
            f"{medal} {award.title} — {fa_digits(award.points)} امتیاز | "
            f"{fa_digits(award.players_paid)} بازیکن × "
            f"{fa_digits(award.apples_per_player)} سیب"
        )
    cards = sum(award.cards_paid for award in result.awards)
    lines.append("")
    lines.append("🍎 جایزهٔ بازیکن‌های هر گروه مستقیم به کیف سیبشون واریز شد.")
    if cards:
        lines.append(
            f"🏆 و {fa_digits(cards)} بازیکنِ تیم اول کارت قهرمان رو گرفتن — "
            "این کارت فقط از راه لیگ به دست می‌آد."
        )
    lines.append("لیگ جدید از همین حالا شروع شده — /league")
    return "\n".join(lines)
