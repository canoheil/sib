"""Weekly group league: derived scoring, deterministic ranking, one payout.

The league is the answer to "why should a group keep the bot *this* week", so
the tests hold it to the two promises that make it trustworthy:

* **the score is honest** — it is derived from real, finished, normal,
  non-bot matches inside the Tehran week, nothing else, so no counter can
  drift and no result handler needed to change;
* **the prize is paid once** — to the players who actually played, with a
  wallet key per player per week, even if the finalize is retried.

Plus the operator-facing rails: the kill switch, the "a week that ended before
the league existed is never paid" guard, and a quiet week producing no
champion instead of a 0-point winner.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from sib.database.models import (
    AppleTransaction,
    Game,
    GroupLeagueAward,
    GroupLeagueWeek,
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.group_league import (
    DEFAULT_REWARDS,
    LEAGUE_REASON,
    POINTS_DRAW,
    POINTS_WIN,
    GroupLeagueService,
    LeagueRow,
    format_champion_announcement,
    format_panel,
    format_reward_ladder,
    format_table,
    gregorian_to_jalali,
    jalali_label,
    parse_rewards,
    shift_week,
    time_left,
    week_key,
    week_label,
    week_window,
)
from sib.tests.test_parties import create_test_group, create_test_user

# 19:00 Tehran == 15:30 UTC (Iran is UTC+3:30, no DST since 2022).
EVENING_UTC = datetime(2026, 10, 25, 15, 30, tzinfo=timezone.utc)
#: The week before ``EVENING_UTC``: Saturday 2026-10-17 .. Friday 2026-10-23.
LAST_WEEK = "2026-10-17"
CURRENT_WEEK = "2026-10-24"
#: A moment inside ``LAST_WEEK``.
IN_WEEK = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
async def clean_league(db_session):
    """Only THIS module's matches/groups/players exist, so counts are exact."""
    for table in (
        "group_league_awards",
        "group_league_weeks",
        "match_players",
        "matches",
        "apple_transactions",
        "apple_wallets",
        "users",
        "group_stats",
        "groups",
    ):
        await db_session.execute(text(f"DELETE FROM {table}"))
    await db_session.commit()
    yield


@pytest.fixture
async def game(db_session) -> Game:
    existing = await db_session.scalar(select(Game))
    if existing is not None:
        return existing
    game = Game(code="league_test", name_fa="بازی تست لیگ")
    db_session.add(game)
    await db_session.commit()
    await db_session.refresh(game)
    return game


async def _enable(db_session, **overrides) -> None:
    values = {
        "league_enabled": True,
        "league_rewards": DEFAULT_REWARDS,
        "league_min_points": 3,
        "league_first_week": "2026-10-01",
        **overrides,
    }
    for key, value in values.items():
        await ConfigService.set(db_session, key, value)
    await db_session.commit()


async def _match(
    db_session,
    game: Game,
    *,
    group1,
    group2,
    players1: tuple = (),
    players2: tuple = (),
    finished_at: datetime = IN_WEEK,
    winner: str = "1",
    is_bot: bool = False,
    match_type: str = MatchType.NORMAL,
    state: str = MatchState.FINISHED,
) -> Match:
    """A finished match with the given participants and outcome."""
    match = Match(
        game_id=game.id,
        mode="solo",
        state=state,
        match_type=match_type,
        is_bot_match=is_bot,
        result_applied=state == MatchState.FINISHED,
        group1_id=group1.id if group1 is not None else None,
        group2_id=group2.id if group2 is not None else None,
        winner_group_id=(
            None
            if winner == "draw"
            else (group1.id if winner == "1" else group2.id)
        ),
        winner_team=None if winner == "draw" else int(winner),
        is_draw=winner == "draw",
        finished_at=finished_at if state == MatchState.FINISHED else None,
    )
    db_session.add(match)
    await db_session.flush()
    for user in players1:
        db_session.add(
            MatchPlayer(
                match_id=match.id,
                user_id=user.id,
                group_id=group1.id if group1 is not None else None,
                team=1,
            )
        )
    for user in players2:
        db_session.add(
            MatchPlayer(
                match_id=match.id,
                user_id=user.id,
                group_id=group2.id if group2 is not None else None,
                team=2,
            )
        )
    await db_session.commit()
    await db_session.refresh(match)
    return match


def _bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


# --------------------------------------------------------------------------- #
# Calendar: the week is Tehran Saturday -> Friday
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "moment,expected",
    [
        # Sunday 19:00 Tehran belongs to the Saturday that started it.
        (EVENING_UTC, CURRENT_WEEK),
        # Saturday 00:30 Tehran is already the NEW week.
        (datetime(2026, 10, 23, 21, 0, tzinfo=timezone.utc), CURRENT_WEEK),
        # Friday 23:30 Tehran is still the OLD week (half-open boundary).
        (datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc), "2026-10-17"),
        # Saturday 00:00 Tehran exactly = the first instant of the new week.
        (datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc), CURRENT_WEEK),
    ],
)
def test_week_key_is_the_tehran_saturday(moment, expected):
    assert week_key(moment) == expected


def test_week_window_is_tehran_midnight_to_midnight():
    start, end = week_window("2026-10-24")

    assert start == datetime(2026, 10, 23, 20, 30, tzinfo=timezone.utc)
    assert end - start == timedelta(days=7)


def test_shift_week_moves_by_whole_weeks():
    assert shift_week("2026-10-24", -1) == "2026-10-17"
    assert shift_week("2026-10-24", 1) == "2026-10-31"
    assert shift_week("2026-10-24", -52) == "2025-10-25"


@pytest.mark.parametrize(
    "gy,gm,gd,expected",
    [
        (2026, 10, 24, (1405, 8, 2)),  # ۲ آبان ۱۴۰۵
        (2026, 3, 21, (1405, 1, 1)),  # نوروز ۱۴۰۵
        (2025, 3, 21, (1404, 1, 1)),
        (2024, 3, 20, (1403, 1, 1)),
        (2026, 9, 22, (1405, 6, 31)),  # last day of Shahrivar
    ],
)
def test_jalali_conversion_matches_the_known_anchors(gy, gm, gd, expected):
    assert gregorian_to_jalali(gy, gm, gd) == expected


def test_week_label_and_jalali_label_are_persian():
    assert jalali_label(date(2026, 10, 24)) == "۲ آبان"
    assert week_label("2026-10-24") == "۲ آبان تا ۸ آبان"
    # No ASCII digits leak into the user-facing label.
    assert not any(ch in "0123456789" for ch in week_label("2026-10-24"))


def test_time_left_counts_down_to_the_end_of_the_week():
    assert time_left(EVENING_UTC, CURRENT_WEEK) > timedelta(days=4)
    assert time_left(EVENING_UTC, LAST_WEEK) < timedelta(0)


# --------------------------------------------------------------------------- #
# Scoring is derived, never counted
# --------------------------------------------------------------------------- #
async def test_win_is_three_points_and_draw_is_one(db_session, game):
    a = await create_test_group(db_session, "گروه الف")
    b = await create_test_group(db_session, "گروه ب")
    await _match(db_session, game, group1=a, group2=b, winner="1")

    rows = await GroupLeagueService.standings(db_session, LAST_WEEK)

    assert [(row.group_id, row.points) for row in rows] == [
        (a.id, POINTS_WIN),
        (b.id, 0),
    ]
    assert rows[0].wins == 1 and rows[0].losses == 0
    assert rows[1].losses == 1 and rows[1].wins == 0
    assert rows[0].rank == 1 and rows[1].rank == 2


async def test_a_draw_pays_one_point_to_both_sides(db_session, game):
    a = await create_test_group(db_session, "گروه الف")
    b = await create_test_group(db_session, "گروه ب")
    await _match(db_session, game, group1=a, group2=b, winner="draw")

    rows = await GroupLeagueService.standings(db_session, LAST_WEEK)

    # Both sides get exactly one point; the id tie-break decides the order.
    assert {(row.group_id, row.points, row.draws) for row in rows} == {
        (a.id, POINTS_DRAW, 1),
        (b.id, POINTS_DRAW, 1),
    }
    assert [row.rank for row in rows] == [1, 2]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_bot": True},
        {"match_type": MatchType.FRIENDLY},
        {"state": MatchState.ACTIVE},
        {"finished_at": datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)},
    ],
    ids=["bot-match", "friendly", "unfinished", "other-week"],
)
async def test_matches_that_do_not_count(db_session, game, kwargs):
    a = await create_test_group(db_session, "گروه الف")
    b = await create_test_group(db_session, "گروه ب")
    await _match(db_session, game, group1=a, group2=b, winner="1", **kwargs)

    assert await GroupLeagueService.standings(db_session, LAST_WEEK) == []


async def test_ranking_is_deterministic_on_every_tie_break(db_session, game):
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    # Same points (2 draws each) and same draws: the group id decides, and the
    # order is stable across calls.
    await _match(db_session, game, group1=a, group2=b, winner="draw")
    await _match(db_session, game, group1=a, group2=b, winner="draw")

    first = await GroupLeagueService.standings(db_session, LAST_WEEK)
    second = await GroupLeagueService.standings(db_session, LAST_WEEK)

    assert [row.group_id for row in first] == [row.group_id for row in second]
    assert [row.group_id for row in first] == sorted([a.id, b.id])


async def test_more_wins_outrank_the_same_points(db_session, game):
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    c = await create_test_group(db_session, "ج")
    d = await create_test_group(db_session, "د")
    # A: 1 win (3 points). B: 3 draws (3 points).
    await _match(db_session, game, group1=a, group2=c, winner="1")
    for opponent in (c, d, d):
        await _match(db_session, game, group1=b, group2=opponent, winner="draw")

    rows = await GroupLeagueService.standings(db_session, LAST_WEEK)

    assert rows[0].group_id == a.id  # 3 points, 1 win
    assert rows[0].points == rows[1].points == 3
    assert rows[1].group_id == b.id


async def test_group_row_finds_only_its_own_line(db_session, game):
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    c = await create_test_group(db_session, "ج")
    await _match(db_session, game, group1=a, group2=b, winner="1")

    own = await GroupLeagueService.group_row(db_session, b.id, LAST_WEEK)
    other = await GroupLeagueService.group_row(db_session, c.id, LAST_WEEK)

    assert own is not None and own.rank == 2
    assert other is None


# --------------------------------------------------------------------------- #
# Finalize: once, only for finished weeks, only for players who played
# --------------------------------------------------------------------------- #
async def test_disabled_by_default_pays_nothing(db_session, game):
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    winner = await create_test_user(db_session, "سارا")
    await _match(
        db_session, game, group1=a, group2=b, players1=(winner,), winner="1"
    )

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.reason == "disabled"
    assert result.finalized is False
    assert await AppleWalletService.get_balance(db_session, winner.id) == 0
    assert await session_scalar_count(db_session, GroupLeagueAward) == 0


async def session_scalar_count(session, model) -> int:
    return int(await session.scalar(select(text("count(*)")).select_from(model.__table__)))


async def test_a_running_week_is_never_finalized(db_session, game):
    await _enable(db_session)
    result = await GroupLeagueService.finalize_week(
        db_session, CURRENT_WEEK, now=EVENING_UTC
    )

    assert result.reason == "week still running"
    assert await session_scalar_count(db_session, GroupLeagueWeek) == 0


async def test_a_week_that_ended_before_the_league_existed_is_never_paid(
    db_session, game
):
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))
    await _enable(db_session, league_first_week="2026-10-24")

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.reason == "before first week"
    assert await AppleWalletService.get_balance(db_session, player.id) == 0


async def test_finalize_pays_every_player_who_played_for_the_podium(db_session, game):
    # min_points=0: every podium rank is paid, including a runner-up that lost.
    await _enable(db_session, league_min_points=0)
    a = await create_test_group(db_session, "قهرمان")
    b = await create_test_group(db_session, "حریف")
    p1 = await create_test_user(db_session, "سارا")
    p2 = await create_test_user(db_session, "مهدی")
    rival = await create_test_user(db_session, "رقیب")
    await _match(
        db_session,
        game,
        group1=a,
        group2=b,
        players1=(p1, p2),
        players2=(rival,),
        winner="1",
    )

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.finalized is True
    assert [award.rank for award in result.awards] == [1, 2]
    assert result.awards[0].players_paid == 2
    assert result.awards[1].players_paid == 1
    assert result.apples_paid == 2 * DEFAULT_REWARDS[1] + DEFAULT_REWARDS[2]
    assert await AppleWalletService.get_balance(db_session, p1.id) == DEFAULT_REWARDS[1]
    assert await AppleWalletService.get_balance(db_session, p2.id) == DEFAULT_REWARDS[1]
    # Rank 2 also played, so it is paid too; nobody else is.
    assert (
        await AppleWalletService.get_balance(db_session, rival.id)
        == DEFAULT_REWARDS[2]
    )

    week = await db_session.get(GroupLeagueWeek, LAST_WEEK)
    await db_session.refresh(week)
    assert week.winner_group_id == a.id
    assert week.winner_points == POINTS_WIN
    assert week.finalized_at is not None


async def test_a_group_member_who_did_not_play_gets_nothing(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "بازی‌کن")
    spectator = await create_test_user(db_session, "تماشاگر")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))

    await GroupLeagueService.finalize_week(db_session, LAST_WEEK, now=EVENING_UTC)

    assert await AppleWalletService.get_balance(db_session, player.id) == 300
    assert await AppleWalletService.get_balance(db_session, spectator.id) == 0


async def test_bot_players_are_never_paid(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    human = await create_test_user(db_session, "انسان")
    robot = await create_test_user(db_session, "ربات")
    robot.is_bot = True
    await db_session.commit()
    await _match(db_session, game, group1=a, group2=b, players1=(human, robot))

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.awards[0].players_paid == 1
    assert await AppleWalletService.get_balance(db_session, human.id) == 300
    assert await AppleWalletService.get_balance(db_session, robot.id) == 0


async def test_finalize_is_idempotent(db_session, game):
    await _enable(db_session, league_min_points=0)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))

    first = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )
    await db_session.commit()
    second = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC + timedelta(hours=1)
    )

    assert first.finalized is True
    assert second.already is True
    assert second.finalized is False
    assert await AppleWalletService.get_balance(db_session, player.id) == 300
    # One award row per paid rank, written once.
    assert await session_scalar_count(db_session, GroupLeagueAward) == 2


async def test_a_crashed_finalize_can_be_retried_without_double_paying(
    db_session, game
):
    """The wallet key is the real safety net, not the week row."""
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))
    await GroupLeagueService.finalize_week(db_session, LAST_WEEK, now=EVENING_UTC)
    await db_session.commit()

    # Simulate a crash after the payouts but before the claim was committed:
    # the award rows and the "finalized" marker are gone, the ledger is not.
    for award in (
        await db_session.scalars(
            select(GroupLeagueAward).where(GroupLeagueAward.week_key == LAST_WEEK)
        )
    ).all():
        await db_session.delete(award)
    week = await db_session.get(GroupLeagueWeek, LAST_WEEK)
    await db_session.delete(week)
    await db_session.commit()

    retry = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert retry.finalized is True
    # Exactly one ledger row per player: `earn` replayed instead of crediting.
    rows = (
        await db_session.scalars(
            select(AppleTransaction).where(
                AppleTransaction.reason == LEAGUE_REASON
            )
        )
    ).all()
    assert len(rows) == 1  # one per participating player of the paid rank
    assert await AppleWalletService.get_balance(db_session, player.id) == 300


async def test_a_quiet_week_has_no_champion(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    # One draw = 2 points < league_min_points (3).
    await _match(
        db_session, game, group1=a, group2=b, players1=(player,), winner="draw"
    )

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.finalized is True
    assert result.awards == ()
    assert await AppleWalletService.get_balance(db_session, player.id) == 0
    week = await db_session.get(GroupLeagueWeek, LAST_WEEK)
    await db_session.refresh(week)
    assert week.winner_group_id is None
    assert week.finalized_at is not None


async def test_a_runner_up_without_points_is_not_paid(db_session, game):
    """The ladder pays ranks, but only groups that earned the minimum."""
    await _enable(db_session)  # league_min_points = 3
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    loser = await create_test_user(db_session, "بازنده")
    await _match(
        db_session, game, group1=a, group2=b, players2=(loser,), winner="1"
    )

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert [award.rank for award in result.awards] == [1]
    assert await AppleWalletService.get_balance(db_session, loser.id) == 0


async def test_rewards_are_config_driven(db_session, game):
    await _enable(
        db_session,
        league_rewards={1: 1000, 2: 0, 3: 0},
        league_min_points=0,
    )
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.awards[0].apples_per_player == 1000
    assert await AppleWalletService.get_balance(db_session, player.id) == 1000


# --------------------------------------------------------------------------- #
# The maintenance sweep + the announcement
# --------------------------------------------------------------------------- #
async def test_finalize_due_weeks_pays_the_week_that_just_ended(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))
    bot = _bot()

    results = await GroupLeagueService.finalize_due_weeks(bot, now=EVENING_UTC)

    # The sweep walks the bounded look-back (4 weeks, oldest first) and marks
    # every one of them settled; only the week with a match pays anything.
    assert [result.week_key for result in results] == [
        "2026-09-26",
        "2026-10-03",
        "2026-10-10",
        LAST_WEEK,
    ]
    # The oldest week of the look-back pre-dates the league (first_week
    # guard), so it is skipped instead of being settled retroactively.
    assert results[0].reason == "before first week"
    assert all(result.finalized for result in results[1:])
    assert [result.week_key for result in results if result.awards] == [LAST_WEEK]
    assert await AppleWalletService.get_balance(db_session, player.id) == 300
    # Only groups whose players were actually paid are told about it.
    assert [call.args[0] for call in bot.send_message.await_args_list] == [a.id]


async def test_the_announcement_is_not_repeated(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    await _match(db_session, game, group1=a, group2=b)
    bot = _bot()
    await GroupLeagueService.finalize_due_weeks(bot, now=EVENING_UTC)

    bot.send_message.reset_mock()
    results = await GroupLeagueService.finalize_due_weeks(
        bot, now=EVENING_UTC + timedelta(hours=1)
    )

    assert results[-1].already is True
    assert not any(result.finalized for result in results)
    bot.send_message.assert_not_awaited()


async def test_a_failed_announcement_never_raises(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    await _match(db_session, game, group1=a, group2=b)
    bot = _bot()
    bot.send_message.side_effect = RuntimeError("kicked from the group")

    results = await GroupLeagueService.finalize_due_weeks(bot, now=EVENING_UTC)

    assert results[-1].finalized is True


async def test_a_long_outage_settles_every_pending_week(db_session, game):
    """Two weeks of downtime must not silently drop one week's prize."""
    await _enable(db_session)
    a = await create_test_group(db_session, "الف")
    b = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "سارا")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))
    await _match(
        db_session,
        game,
        group1=a,
        group2=b,
        players1=(player,),
        finished_at=IN_WEEK - timedelta(days=7),
    )
    bot = _bot()

    # "Now" is a week after EVENING_UTC: both earlier weeks are pending.
    results = await GroupLeagueService.finalize_due_weeks(
        bot, now=EVENING_UTC + timedelta(days=7)
    )

    paid = [result.week_key for result in results if result.awards]
    assert paid == ["2026-10-10", LAST_WEEK]
    # One prize per week, never two: the same player is paid twice (once per
    # week) and the ledger key differs by week.
    assert await AppleWalletService.get_balance(db_session, player.id) == 600


async def test_the_sweep_is_silent_while_disabled(db_session, game):
    bot = _bot()
    results = await GroupLeagueService.finalize_due_weeks(bot, now=EVENING_UTC)

    assert results[0].reason == "disabled"
    bot.send_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Config parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, DEFAULT_REWARDS),
        ('{"1": 10, "2": 5}', {1: 10, 2: 5}),
        ({1: 10, "2": 5}, {1: 10, 2: 5}),
        ("not json", DEFAULT_REWARDS),
        ({"9": 10, "1": -5}, {}),  # out-of-range rank / negative amount
    ],
)
def test_parse_rewards_is_defensive(raw, expected):
    assert parse_rewards(raw) == expected


# --------------------------------------------------------------------------- #
# Persian text
# --------------------------------------------------------------------------- #
def _row(rank, group_id, title, points=3, wins=1, draws=0, losses=0, matches=1):
    return LeagueRow(
        rank=rank,
        group_id=group_id,
        title=title,
        points=points,
        wins=wins,
        draws=draws,
        losses=losses,
        matches=matches,
    )


def test_panel_shows_the_table_the_ladder_and_our_rank():
    rows = [_row(1, -100, "قهرمان"), _row(2, -200, "حریف")]
    text = format_panel(
        now=EVENING_UTC,
        key=CURRENT_WEEK,
        rows=rows,
        own=rows[1],
        rewards=DEFAULT_REWARDS,
    )

    assert "لیگ هفتگی گروه‌ها" in text
    assert "🥇" in text and "🥈" in text
    assert "رتبهٔ **۲**" in text
    assert "۳۰۰ سیب" in text  # the ladder
    assert "فقط نبردهای واقعیِ گروه با گروه" in text


def test_private_panel_has_no_own_group_line_but_the_table():
    rows = [_row(1, -100, "قهرمان")]
    text = format_panel(
        now=EVENING_UTC,
        key=CURRENT_WEEK,
        rows=rows,
        own=None,
        rewards=DEFAULT_REWARDS,
        is_group=False,
    )

    assert "گروه ما" not in text
    assert "قهرمان" in text


def test_an_empty_week_invites_the_first_match():
    assert "اولین نبرد" in format_table([])


def test_group_titles_cannot_break_the_parse_mode():
    hostile = "*bold* _under_ `tick` [link]"
    text = format_table([_row(1, -1, hostile)])

    assert "\\*bold\\*" in text
    assert "\\_under\\_" in text


def test_open_weeks_are_counted_down_in_persian():
    text = format_panel(
        now=EVENING_UTC,
        key=CURRENT_WEEK,
        rows=[],
        own=None,
        rewards={},
    )
    assert "تا پایان لیگ این هفته" in text
    assert not any(ch in "0123456789" for ch in text.split("\n")[2])


def test_no_rewards_config_hides_the_ladder_line():
    assert format_reward_ladder({}) == ""
    assert "سیب" in format_reward_ladder(DEFAULT_REWARDS)


def test_announcement_names_each_podium_group_and_its_prize():
    from sib.services.group_league import LeagueAwardResult, LeagueFinal

    result = LeagueFinal(
        week_key=LAST_WEEK,
        finalized=True,
        awards=(
            LeagueAwardResult(
                rank=1,
                group_id=-100,
                title="قهرمان",
                points=9,
                players_paid=3,
                apples_per_player=300,
            ),
        ),
        apples_paid=900,
    )
    text = format_champion_announcement(result)

    assert "قهرمان" in text
    assert "۳ بازیکن" in text
    assert "۳۰۰ سیب" in text
    assert "/league" in text


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
async def test_league_is_wired_into_the_maintenance_cycle():
    import inspect

    from sib.services.maintenance import service as maintenance

    source = inspect.getsource(maintenance.MaintenanceService.run_once)
    assert "GroupLeagueService.finalize_due_weeks" in source
    assert "Maintenance task failed: group league" in source


def test_league_command_is_published_to_players():
    from sib.services.bot_commands import PUBLIC_COMMANDS

    assert ("league", "لیگ هفتگی گروه‌ها") in PUBLIC_COMMANDS


def test_leaderboard_has_a_league_button():
    from sib.keyboards.leaderboard import get_leaderboard_keyboard

    buttons = [
        button
        for row in get_leaderboard_keyboard(is_group=True).inline_keyboard
        for button in row
    ]
    assert any(button.callback_data == "league_open" for button in buttons)


def test_league_keyboard_keeps_a_way_back():
    from sib.keyboards.league import get_league_keyboard

    for enabled in (True, False):
        buttons = [
            button
            for row in get_league_keyboard(enabled=enabled).inline_keyboard
            for button in row
        ]
        assert any(button.callback_data == "main_menu" for button in buttons)


async def test_the_panel_renders_for_a_group(db_session, game):
    from types import SimpleNamespace

    from sib.handlers.league import _render_panel

    await _enable(db_session)
    a = await create_test_group(db_session, "گروه ما")
    b = await create_test_group(db_session, "حریف")
    # The panel always renders the CURRENT week, so this match must be in it.
    await _match(
        db_session,
        game,
        group1=a,
        group2=b,
        winner="1",
        finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    text, keyboard = await _render_panel(
        SimpleNamespace(id=a.id, type="supergroup")
    )

    assert "گروه ما" in text
    assert "رتبهٔ **۱**" in text
    assert keyboard.inline_keyboard


async def test_the_league_command_replies_with_the_panel(db_session, game):
    from types import SimpleNamespace

    from sib.handlers.league import cmd_league

    await _enable(db_session)
    a = await create_test_group(db_session, "گروه ما")
    b = await create_test_group(db_session, "حریف")
    await _match(
        db_session,
        game,
        group1=a,
        group2=b,
        finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=a.id, type="supergroup"),
        message_id=1,
        photo=None,
        reply=AsyncMock(),
        reply_photo=AsyncMock(),
    )

    await cmd_league(message, AsyncMock())

    # There is no banner asset for the league, so the screen must degrade to a
    # text reply rather than fail.
    message.reply_photo.assert_not_awaited()
    reply = message.reply.await_args
    assert "لیگ هفتگی گروه‌ها" in reply.args[0]
    assert "گروه ما" in reply.args[0]
    assert reply.kwargs["reply_markup"].inline_keyboard


async def test_the_league_button_redraws_the_same_panel(db_session, game):
    from types import SimpleNamespace

    from sib.handlers.league import callback_league

    await _enable(db_session)
    a = await create_test_group(db_session, "گروه ما")
    b = await create_test_group(db_session, "حریف")
    await _match(
        db_session,
        game,
        group1=a,
        group2=b,
        finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    callback = SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=a.id, type="supergroup"),
            message_id=7,
            photo=None,
        ),
        answer=AsyncMock(),
    )
    bot = AsyncMock()

    await callback_league(callback, bot)

    callback.answer.assert_awaited()
    rendered = " ".join(
        str(part)
        for call in bot.method_calls
        for part in (*call.args, *call.kwargs.values())
    )
    assert "لیگ هفتگی گروه‌ها" in rendered
    # The real panel, not the "could not read the table" fallback.
    assert "هفتهٔ" in rendered


async def test_the_panel_says_so_when_the_league_is_off(db_session):
    from types import SimpleNamespace

    from sib.handlers.league import LEAGUE_DISABLED_TEXT, _render_panel

    text, keyboard = await _render_panel(SimpleNamespace(id=-1, type="group"))

    assert text == LEAGUE_DISABLED_TEXT
    # Still a way back, never a dead end.
    assert keyboard.inline_keyboard
