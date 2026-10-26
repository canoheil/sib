"""Multi-source league scoring: matches, wars, the mine and duels.

Phase 1 of the rescue plan gave the weekly league four scoring sources, all
*derived* from tables that already exist. The tests here hold it to the
promises that make a multi-source table safe:

* **the breakdown is the truth** — ``match + war + mine + duel`` always equals
  ``points``, for every row, so the panel can never print a lie;
* **the caps hold** — no single source, and no single member, can carry a
  group: matches 45, wars 30, mine 10, duels 5 (theoretical max 90);
* **nothing farmable counts** — bot users, special apples, shifts outside a
  group and duels that never started score zero;
* **a duel is activity, not a result** — the duel happens inside ONE group,
  so it can never be worth a win;
* **everyone who earned points is paid** — the war attacker, the miner and
  the duellist are participants, not only the match players;
* **the table is config** — rebalancing ``league_points`` changes the score
  with no code change, and a zero step can never become infinite points.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from sib.database.models import (
    AppleDuel,
    AppleMineShift,
    Game,
    GroupCastle,
    GroupWar,
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    WarAttack,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.group_league import (
    DEFAULT_POINTS,
    DEFAULT_REWARDS,
    GroupLeagueService,
    LeagueRow,
    format_breakdown,
    format_panel,
    parse_points,
)
from sib.tests.test_parties import create_test_group, create_test_user

EVENING_UTC = datetime(2026, 10, 25, 15, 30, tzinfo=timezone.utc)
LAST_WEEK = "2026-10-17"
#: A moment inside ``LAST_WEEK`` (Tehran Saturday 2026-10-17 .. Friday 23).
IN_WEEK = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
async def clean_league(db_session):
    for table in (
        "group_league_awards",
        "group_league_weeks",
        "war_attacks",
        "group_wars",
        "apple_duels",
        "apple_duel_slots",
        "apple_mine_shift",
        "match_players",
        "matches",
        "apple_transactions",
        "apple_wallets",
        "player_cards",
        "users",
        "group_stats",
        "group_castles",
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
    game = Game(code="league_points_test", name_fa="بازی تست لیگ")
    db_session.add(game)
    await db_session.commit()
    await db_session.refresh(game)
    return game


async def _enable(db_session, **overrides) -> None:
    values = {
        "league_enabled": True,
        "league_rewards": DEFAULT_REWARDS,
        "league_min_points": 0,
        "league_first_week": "2026-10-01",
        **overrides,
    }
    for key, value in values.items():
        await ConfigService.set(db_session, key, value)
    await db_session.commit()


async def _castle(db_session, group):
    castle = GroupCastle(group_id=group.id)
    db_session.add(castle)
    await db_session.commit()
    return castle


async def _match(db_session, game, *, group1, group2=None, players1=(), winner="1"):
    match = Match(
        game_id=game.id,
        mode="solo",
        state=MatchState.FINISHED,
        match_type=MatchType.NORMAL,
        is_bot_match=False,
        result_applied=True,
        group1_id=group1.id,
        group2_id=group2.id if group2 is not None else None,
        winner_group_id=(
            None
            if winner == "draw"
            else (group1.id if winner == "1" else group2.id)
        ),
        winner_team=None if winner == "draw" else int(winner),
        is_draw=winner == "draw",
        finished_at=IN_WEEK,
    )
    db_session.add(match)
    await db_session.flush()
    for user in players1:
        db_session.add(
            MatchPlayer(match_id=match.id, user_id=user.id, group_id=group1.id, team=1)
        )
    await db_session.commit()
    return match


async def _war(db_session, *, attacker, defender, winner, attacks=()):
    """A finished war that ended inside the week.

    ``attacks`` is ``(user, damage)`` pairs the ATTACKER group dealt.
    """
    await _castle(db_session, attacker)
    await _castle(db_session, defender)
    war = GroupWar(
        attacker_id=attacker.id,
        defender_id=defender.id,
        status="finished",
        started_at=IN_WEEK - timedelta(days=1),
        ends_at=IN_WEEK,
        winner_id=(attacker.id if winner == "attacker" else defender.id),
    )
    db_session.add(war)
    await db_session.flush()
    for user, damage in attacks:
        db_session.add(
            WarAttack(
                war_id=war.id,
                user_id=user.id,
                source_group_id=attacker.id,
                target_group_id=defender.id,
                damage=damage,
                created_at=IN_WEEK,
            )
        )
    await db_session.commit()
    return war


async def _shift(db_session, *, user, group, apples, item="apple_normal", when=IN_WEEK):
    shift = AppleMineShift(
        user_id=user.id,
        group_id=group.id if group is not None else None,
        pickaxe_level=1,
        outcome="normal",
        item_code=item,
        amount=apples,
        worked_at=when,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


_DUEL_IDS = iter(range(1, 10_000))


async def _duel(db_session, *, group, inviter, recipient, status="ACCEPTED", battle_id=None):
    duel = AppleDuel(
        id=f"duel-{next(_DUEL_IDS)}-{inviter.id}",
        group_id=group.id,
        inviter_id=inviter.id,
        recipient_id=recipient.id,
        status=status,
        expires_at=IN_WEEK + timedelta(hours=1),
        # Every duel has its own battle row in production (unique column).
        battle_id=next(_DUEL_IDS) if battle_id is None else battle_id,
        created_at=IN_WEEK,
    )
    db_session.add(duel)
    await db_session.commit()
    return duel


def _row(table, group_id) -> LeagueRow:
    return next(row for row in table if row.group_id == group_id)


# --------------------------------------------------------------------------- #
# The config table
# --------------------------------------------------------------------------- #
def test_parse_points_defaults_and_normalisation():
    assert parse_points(None) == DEFAULT_POINTS
    # A JSON string (how system_config stores it) is accepted as-is.
    assert parse_points('{"match_win": 5}')["match_win"] == 5
    # Unknown keys, junk values and negatives are refused, defaults survive.
    parsed = parse_points(
        {"nonsense": 99, "war_win": "abc", "mine_cap": -4, "duel_member": "2"}
    )
    assert "nonsense" not in parsed
    assert parsed["war_win"] == DEFAULT_POINTS["war_win"]
    assert parsed["mine_cap"] == DEFAULT_POINTS["mine_cap"]  # negative refused
    assert parsed["duel_member"] == 2  # numeric strings are fine
    # A zero/negative division step would mean "points per no apples".
    assert parse_points({"mine_apples_step": 0})["mine_apples_step"] == 1
    assert parse_points({"war_damage_step": -20})["war_damage_step"] == 1
    assert parse_points("not json") == DEFAULT_POINTS


def test_the_caps_add_up_to_the_documented_ninety():
    assert (
        DEFAULT_POINTS["match_cap"]
        + DEFAULT_POINTS["war_cap"]
        + DEFAULT_POINTS["mine_cap"]
        + DEFAULT_POINTS["duel_cap"]
    ) == 90
    # Matches stay the backbone: the biggest single source, but under half.
    assert DEFAULT_POINTS["match_cap"] == 45
    assert DEFAULT_POINTS["war_cap"] == 30


# --------------------------------------------------------------------------- #
# Each source on its own
# --------------------------------------------------------------------------- #
async def test_a_won_war_is_worth_its_result_plus_damage(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "مهاجم")
    b = await create_test_group(db_session, "مدافع")
    attacker = await create_test_user(db_session, "جنگجو")
    await _war(
        db_session, attacker=a, defender=b, winner="attacker",
        attacks=((attacker, 450),),
    )

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)

    row = _row(table, a.id)
    # 10 (win) + 4 (450 damage // 100)
    assert row.war_points == 14
    assert row.wars == 1 and row.war_wins == 1
    assert row.match_points == 0
    assert row.points == row.war_points
    # The loser still earns its presence and any damage it dealt.
    loser = _row(table, b.id)
    assert loser.war_points == DEFAULT_POINTS["war_presence"]
    assert loser.war_wins == 0


async def test_war_points_are_capped(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "مهاجم")
    b = await create_test_group(db_session, "مدافع")
    fighter = await create_test_user(db_session, "دمیج‌زن")
    # 5000 damage = 50 damage points, well past the 30-point war cap.
    await _war(
        db_session, attacker=a, defender=b, winner="attacker",
        attacks=((fighter, 5000),),
    )

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)

    assert _row(table, a.id).war_points == DEFAULT_POINTS["war_cap"]


async def test_mine_counts_normal_apples_inside_a_group_only(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "معدن‌کارها")
    b = await create_test_group(db_session, "بی‌کار")
    miner = await create_test_user(db_session, "معدن‌کار")
    robot = await create_test_user(db_session, "ربات معدن")
    robot.is_bot = True
    await db_session.commit()

    await _shift(db_session, user=miner, group=a, apples=60)
    # Special apples are loot, not work; outside a group there is no group to
    # credit; a bot's shift is not a player's effort.
    await _shift(db_session, user=miner, group=a, apples=3, item="apple_gold")
    await _shift(db_session, user=miner, group=None, apples=500)
    await _shift(db_session, user=robot, group=a, apples=500)

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)

    row = _row(table, a.id)
    assert row.mine_apples == 60
    assert row.mine_points == 2  # 60 // 25
    assert all(other.group_id != b.id for other in table)


async def test_mine_points_are_capped(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "معدن‌کارها")
    miner = await create_test_user(db_session, "معدن‌کار")
    await _shift(db_session, user=miner, group=a, apples=10_000)

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)

    assert _row(table, a.id).mine_points == DEFAULT_POINTS["mine_cap"]


async def test_a_duel_is_activity_one_point_per_member_and_capped(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "دوئل‌بازها")
    p1 = await create_test_user(db_session, "اولی")
    p2 = await create_test_user(db_session, "دومی")
    p3 = await create_test_user(db_session, "سومی")

    await _duel(db_session, group=a, inviter=p1, recipient=p2)
    # A second duel between the SAME two people adds no extra member.
    await _duel(db_session, group=a, inviter=p2, recipient=p1, status="ACCEPTED")
    # A pending duel never happened.
    await _duel(db_session, group=a, inviter=p1, recipient=p3, status="PENDING", battle_id=0)

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)
    row = _row(table, a.id)
    assert row.duelists == 2
    assert row.duel_points == 2 * DEFAULT_POINTS["duel_member"]

    # Six distinct duelists still cannot beat the 5-point cap.
    for index in range(4):
        extra = await create_test_user(db_session, f"دوئل‌باز {index}")
        await _duel(db_session, group=a, inviter=p1, recipient=extra)
    capped = _row(await GroupLeagueService.standings(db_session, LAST_WEEK), a.id)
    assert capped.duelists >= 6
    assert capped.duel_points == DEFAULT_POINTS["duel_cap"]


# --------------------------------------------------------------------------- #
# The table as a whole
# --------------------------------------------------------------------------- #
async def test_the_breakdown_always_equals_the_total(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "همه‌کاره")
    b = await create_test_group(db_session, "حریف")
    player = await create_test_user(db_session, "بازیکن")
    miner = await create_test_user(db_session, "معدن‌کار")
    duelist = await create_test_user(db_session, "دوئل‌باز")

    await _match(db_session, game, group1=a, group2=b, players1=(player,))
    await _war(
        db_session, attacker=a, defender=b, winner="attacker",
        attacks=((player, 250),),
    )
    await _shift(db_session, user=miner, group=a, apples=75)
    await _duel(db_session, group=a, inviter=player, recipient=duelist)

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)

    row = _row(table, a.id)
    assert row.match_points == DEFAULT_POINTS["match_win"]
    assert row.war_points == DEFAULT_POINTS["war_win"] + 2  # 250 // 100
    assert row.mine_points == 3  # 75 // 25
    assert row.duel_points == 2 * DEFAULT_POINTS["duel_member"]
    assert row.points == (
        row.match_points + row.war_points + row.mine_points + row.duel_points
    )
    # And the group that only lost still appears, with its presence points.
    assert _row(table, b.id).points == DEFAULT_POINTS["war_presence"]


async def test_a_group_with_no_activity_this_week_is_not_ranked(db_session, game):
    await _enable(db_session)
    busy = await create_test_group(db_session, "فعال")
    idle = await create_test_group(db_session, "خاموش")
    player = await create_test_user(db_session, "بازیکن")
    await _match(db_session, game, group1=busy, group2=None, players1=(player,))
    await db_session.commit()

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)

    assert [row.group_id for row in table] == [busy.id]
    assert idle.id not in {row.group_id for row in table}


async def test_rebalancing_the_config_changes_the_table(db_session, game):
    a = await create_test_group(db_session, "قهرمان")
    b = await create_test_group(db_session, "حریف")
    player = await create_test_user(db_session, "بازیکن")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))

    await _enable(db_session, league_points={"match_win": 7, "match_draw": 2})
    table = await GroupLeagueService.standings(db_session, LAST_WEEK)
    assert _row(table, a.id).match_points == 7

    await _enable(db_session, league_points={"match_win": 0, "match_cap": 10})
    table = await GroupLeagueService.standings(db_session, LAST_WEEK)
    assert _row(table, a.id).match_points == 0
    assert _row(table, a.id).points == 0


async def test_the_floor_needs_four_wins_to_crown_a_champion(db_session, game):
    """``league_min_points`` (15) means one lucky match is not a championship."""
    a = await create_test_group(db_session, "یک‌برد")
    b = await create_test_group(db_session, "حریف")
    player = await create_test_user(db_session, "بازیکن")
    await _match(db_session, game, group1=a, group2=b, players1=(player,))
    await _enable(db_session, league_min_points=15)

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.finalized is True
    assert result.awards == ()
    assert await AppleWalletService.get_balance(db_session, player.id) == 0

    # A war win plus a match is over the floor and gets paid.
    await _war(
        db_session, attacker=a, defender=b, winner="attacker",
        attacks=((player, 500),),
    )
    await db_session.execute(text("DELETE FROM group_league_weeks"))
    await db_session.commit()
    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )
    assert [award.rank for award in result.awards] == [1]
    assert await AppleWalletService.get_balance(db_session, player.id) == DEFAULT_REWARDS[1]


# --------------------------------------------------------------------------- #
# Payout follows the work, not only the match sheet
# --------------------------------------------------------------------------- #
async def test_war_miners_and_duellists_are_paid_when_their_group_wins(db_session, game):
    await _enable(db_session, league_min_points=15)
    a = await create_test_group(db_session, "قهرمان")
    b = await create_test_group(db_session, "حریف")
    fighter = await create_test_user(db_session, "جنگجو")
    miner = await create_test_user(db_session, "معدن‌کار")
    duelist = await create_test_user(db_session, "دوئل‌باز")
    spectator = await create_test_user(db_session, "تماشاگر")

    # 10 (war win) + 5 (500 damage) + 1 (25 apples / 25) + 2 (two duelists)
    await _war(
        db_session, attacker=a, defender=b, winner="attacker",
        attacks=((fighter, 500),),
    )
    await _shift(db_session, user=miner, group=a, apples=25)
    await _duel(db_session, group=a, inviter=duelist, recipient=fighter)

    table = await GroupLeagueService.standings(db_session, LAST_WEEK)
    assert _row(table, a.id).points >= 15

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )
    assert result.awards[0].rank == 1

    for worker in (fighter, miner, duelist):
        assert (
            await AppleWalletService.get_balance(db_session, worker.id)
            == DEFAULT_REWARDS[1]
        )
    # Presence is still not participation.
    assert await AppleWalletService.get_balance(db_session, spectator.id) == 0


async def test_the_participant_list_covers_every_scoring_source(db_session, game):
    await _enable(db_session)
    a = await create_test_group(db_session, "گروه")
    b = await create_test_group(db_session, "حریف")
    fighter = await create_test_user(db_session, "جنگجو")
    miner = await create_test_user(db_session, "معدن‌کار")
    robot = await create_test_user(db_session, "ربات")
    robot.is_bot = True
    await db_session.commit()

    await _war(
        db_session, attacker=a, defender=b, winner="defender",
        attacks=((fighter, 100),),
    )
    await _shift(db_session, user=miner, group=a, apples=10)
    await _shift(db_session, user=robot, group=a, apples=10)

    players = await GroupLeagueService._participants(db_session, LAST_WEEK, a.id)

    assert fighter.id in players and miner.id in players
    assert robot.id not in players


# --------------------------------------------------------------------------- #
# The panel shows where the points came from
# --------------------------------------------------------------------------- #
def test_format_breakdown_lists_only_the_sources_that_scored():
    assert (
        format_breakdown(
            LeagueRow(
                rank=1, group_id=1, title="گروه", points=43,
                match_points=18, war_points=19, mine_points=2, duel_points=4,
                war_wins=1,
            )
        )
        == "🎮 نبرد ۱۸ · ⚔️ جنگ ۱۹ (۱ برد) · ⛏ معدن ۲ · 🥊 دوئل ۴"
    )
    assert (
        format_breakdown(
            LeagueRow(rank=1, group_id=1, title="گروه", points=13, match_points=13)
        )
        == "🎮 نبرد ۱۳"
    )
    assert "امتیازی" in format_breakdown(
        LeagueRow(rank=1, group_id=1, title="گروه", points=0)
    )


def test_league_table_hides_zero_records_and_shows_only_scoring_sources():
    from sib.services.group_league import format_table

    text = format_table(
        [
            LeagueRow(
                rank=1,
                group_id=1,
                title="قهرمان",
                points=20,
                war_points=20,
                wars=1,
                war_wins=1,
            ),
            LeagueRow(rank=2, group_id=2, title="گروه بی‌امتیاز", points=0),
        ]
    )

    assert "⚔️ جنگ ۲۰ (۱ برد)" in text
    assert "گروه بی‌امتیاز" in text and "**۰** امتیاز" in text
    assert "۰ برد" not in text
    assert "۰ مساوی" not in text
    assert "جنگ‌برد" not in text


def test_the_panel_prints_the_breakdown_for_our_group():
    rows = [
        LeagueRow(
            rank=1, group_id=1, title="قهرمان", points=43,
            wins=4, draws=1, matches=6,
            match_points=18, war_points=19, mine_points=2, duel_points=4,
            wars=1, war_wins=1,
        )
    ]
    panel = format_panel(
        now=EVENING_UTC,
        key="2026-10-24",
        rows=rows,
        own=rows[0],
        rewards=DEFAULT_REWARDS,
        min_points=15,
    )
    assert (
        "└ سهم امتیاز: 🎮 نبرد ۱۸ · ⚔️ جنگ ۱۹ (۱ برد) · "
        "⛏ معدن ۲ · 🥊 دوئل ۴"
    ) in panel
    assert "⚔️ جنگ ۱۹ (۱ برد)" in panel
