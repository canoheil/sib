"""The champion card («قهرمان»): a trophy only the league can hand out.

The card is *not* a shop item — ``shop_enabled`` is false in the seed — so the
tests here hold the three promises that make it worth winning:

* **the league is the only source** — the shop, the daily chest and the
  starter pack all filter on ``shop_enabled``, so the trophy can never appear
  there by accident;
* **it is granted to the players who actually played for the winning group**
  (presence is not participation) and nobody else;
* **winning twice never duplicates it** — one card per character per player is
  the game's oldest rule, and the repeat prize stays the Apples.

The Apple payout must also survive a deployment that has not seeded the card
yet: a missing trophy never blocks the money.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from sib.database.models import (
    Character,
    Match,
    MatchPlayer,
    MatchState,
    MatchType,
    Game,
    PlayerCard,
    ReleaseStatus,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.group_league import (
    DEFAULT_CHAMPION_CARD_CODE,
    DEFAULT_REWARDS,
    GroupLeagueService,
    format_reward_ladder,
)
from sib.tests.test_parties import create_test_group, create_test_user

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "characters_seed.json"
ART_PATH = Path(__file__).resolve().parents[1] / "assets" / "characters" / "hero.jpg"

EVENING_UTC = datetime(2026, 10, 25, 15, 30, tzinfo=timezone.utc)
LAST_WEEK = "2026-10-17"
IN_WEEK = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures: a clean league, and the trophy card in the roster
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
async def clean_league(db_session):
    for table in (
        "group_league_awards",
        "group_league_weeks",
        "match_players",
        "matches",
        "apple_transactions",
        "apple_wallets",
        "player_cards",
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


@pytest.fixture
async def hero(db_session) -> Character:
    """The trophy, seeded exactly like the real roster row."""
    from sib.database.connection import seed_characters

    await seed_characters(db_session)
    await db_session.commit()
    character = await db_session.scalar(
        select(Character).where(Character.code == DEFAULT_CHAMPION_CARD_CODE)
    )
    assert character is not None, "the champion card must be in the roster seed"
    return character


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
    db_session, game, *, group1, group2, players1=(), players2=(), finished_at=IN_WEEK
) -> Match:
    match = Match(
        game_id=game.id,
        mode="solo",
        state=MatchState.FINISHED,
        match_type=MatchType.NORMAL,
        is_bot_match=False,
        result_applied=True,
        group1_id=group1.id,
        group2_id=group2.id,
        winner_group_id=group1.id,
        winner_team=1,
        is_draw=False,
        finished_at=finished_at,
    )
    db_session.add(match)
    await db_session.flush()
    for user in players1:
        db_session.add(
            MatchPlayer(match_id=match.id, user_id=user.id, group_id=group1.id, team=1)
        )
    for user in players2:
        db_session.add(
            MatchPlayer(match_id=match.id, user_id=user.id, group_id=group2.id, team=2)
        )
    await db_session.commit()
    await db_session.refresh(match)
    return match


async def _cards_of(db_session, user_id: int, character_id: int) -> int:
    rows = (
        await db_session.scalars(
            select(PlayerCard.id).where(
                PlayerCard.user_id == user_id,
                PlayerCard.character_id == character_id,
            )
        )
    ).all()
    return len(rows)


# --------------------------------------------------------------------------- #
# The roster row is a trophy, not a shop card
# --------------------------------------------------------------------------- #
def test_champion_seed_row_is_a_well_formed_card():
    entry = next(
        row
        for row in json.loads(SEED_PATH.read_text(encoding="utf-8"))
        if row["code"] == DEFAULT_CHAMPION_CARD_CODE
    )
    assert entry["name_fa"] == "هیرو"
    assert entry["name_en"] == "Hero"
    assert entry["power"] + entry["speed"] + entry["intelligence"] == 60
    # «آجیلیتی» — speed is the card's dominant stat.
    assert entry["speed"] > max(entry["power"], entry["intelligence"])
    assert entry["artwork"] is None  # art resolves to assets/characters/hero.jpg
    assert entry["release_status"] == "RELEASED"
    assert entry["shop_enabled"] is False
    # The wolf is part of the story and must be named there.
    assert "سپیدچنگ" in entry["lore_fa"]


def test_the_trophy_cannot_be_bought_or_drawn_anywhere(hero):
    """Every card source filters on ``shop_enabled``; the trophy fails it."""
    from sib.services.player_cards import is_shop_eligible

    assert hero.shop_enabled is False
    assert hero.release_status == ReleaseStatus.RELEASED
    assert hero.is_active is True
    assert is_shop_eligible(hero) is False
    # The starter pack prefers a shop-eligible card, so the trophy is out.
    assert is_shop_eligible(hero) is False


async def test_the_trophy_is_not_in_the_weekly_shop_rotation(db_session, hero):
    from sib.services.shop import ShopService

    entries = await ShopService.list_entries(db_session, user_id=424242)
    await db_session.commit()
    offered = {character.code for _entry, character, _owned in entries}
    assert DEFAULT_CHAMPION_CARD_CODE not in offered


async def test_the_trophy_is_not_in_the_daily_chest_pool(db_session, hero):
    from sib.services.daily_chest import DailyChestService

    pool = await DailyChestService._eligible_card_characters(db_session, 424243)
    assert DEFAULT_CHAMPION_CARD_CODE not in {character.code for character in pool}


async def test_the_trophy_is_visible_in_the_wiki(db_session, hero):
    """A trophy nobody can see is not worth winning."""
    from sib.services.wiki import WikiService

    listed = await WikiService.list_wiki(db_session)
    assert DEFAULT_CHAMPION_CARD_CODE in {character.code for character in listed}
    page = await WikiService.wiki_page(db_session, hero.id)
    assert page is not None and "هیرو" in page


# --------------------------------------------------------------------------- #
# The grant
# --------------------------------------------------------------------------- #
async def test_rank_one_players_receive_the_champion_card(db_session, game, hero):
    await _enable(db_session)
    winners = await create_test_group(db_session, "قهرمان")
    losers = await create_test_group(db_session, "حریف")
    p1 = await create_test_user(db_session, "سارا")
    p2 = await create_test_user(db_session, "مهدی")
    rival = await create_test_user(db_session, "رقیب")
    await _match(
        db_session,
        game,
        group1=winners,
        group2=losers,
        players1=(p1, p2),
        players2=(rival,),
    )

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.finalized is True
    assert result.awards[0].rank == 1
    assert result.awards[0].cards_paid == 2
    assert await _cards_of(db_session, p1.id, hero.id) == 1
    assert await _cards_of(db_session, p2.id, hero.id) == 1
    # Rank 2 played, but only rank 1 wears the crown.
    assert await _cards_of(db_session, rival.id, hero.id) == 0
    # The Apple prize is untouched by the card.
    assert await AppleWalletService.get_balance(db_session, p1.id) == DEFAULT_REWARDS[1]


async def test_a_bench_member_who_did_not_play_gets_no_card(db_session, game, hero):
    await _enable(db_session)
    winners = await create_test_group(db_session, "الف")
    losers = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "بازی‌کن")
    spectator = await create_test_user(db_session, "تماشاگر")
    await _match(db_session, game, group1=winners, group2=losers, players1=(player,))

    await GroupLeagueService.finalize_week(db_session, LAST_WEEK, now=EVENING_UTC)

    assert await _cards_of(db_session, player.id, hero.id) == 1
    assert await _cards_of(db_session, spectator.id, hero.id) == 0


async def test_winning_twice_never_duplicates_the_card(db_session, game, hero):
    await _enable(db_session)
    winners = await create_test_group(db_session, "الف")
    losers = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "دوبار قهرمان")

    await _match(db_session, game, group1=winners, group2=losers, players1=(player,))
    first = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )
    assert first.awards[0].cards_paid == 1

    # The next week: the same group wins again.
    await db_session.execute(text("DELETE FROM group_league_weeks"))
    await db_session.execute(text("DELETE FROM matches"))
    await db_session.commit()
    await _match(
        db_session,
        game,
        group1=winners,
        group2=losers,
        players1=(player,),
        finished_at=datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc),
    )
    second = await GroupLeagueService.finalize_week(
        db_session, "2026-10-24", now=datetime(2026, 11, 1, 15, 30, tzinfo=timezone.utc)
    )
    assert second.finalized is True
    assert second.awards[0].cards_paid == 0  # already owned
    assert await _cards_of(db_session, player.id, hero.id) == 1
    # Repeat wins still pay the Apples every week.
    assert (
        await AppleWalletService.get_balance(db_session, player.id)
        == DEFAULT_REWARDS[1] * 2
    )


async def test_a_missing_trophy_never_blocks_the_apple_prize(db_session, game):
    """A deployment that has not seeded the card still pays the money."""
    await _enable(db_session)
    winners = await create_test_group(db_session, "الف")
    losers = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "بازیکن")
    await _match(db_session, game, group1=winners, group2=losers, players1=(player,))

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.finalized is True
    assert result.awards[0].cards_paid == 0
    assert await AppleWalletService.get_balance(db_session, player.id) == DEFAULT_REWARDS[1]


async def test_the_card_can_be_switched_off_from_config(db_session, game, hero):
    await _enable(db_session, league_champion_card_code="")
    winners = await create_test_group(db_session, "الف")
    losers = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "بازیکن")
    await _match(db_session, game, group1=winners, group2=losers, players1=(player,))

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.awards[0].cards_paid == 0
    assert await _cards_of(db_session, player.id, hero.id) == 0
    assert await AppleWalletService.get_balance(db_session, player.id) == DEFAULT_REWARDS[1]


async def test_an_unreleased_card_is_not_granted(db_session, game, hero):
    await _enable(db_session)
    hero.release_status = ReleaseStatus.DRAFT
    await db_session.commit()
    winners = await create_test_group(db_session, "الف")
    losers = await create_test_group(db_session, "ب")
    player = await create_test_user(db_session, "بازیکن")
    await _match(db_session, game, group1=winners, group2=losers, players1=(player,))

    result = await GroupLeagueService.finalize_week(
        db_session, LAST_WEEK, now=EVENING_UTC
    )

    assert result.awards[0].cards_paid == 0
    assert await _cards_of(db_session, player.id, hero.id) == 0


# --------------------------------------------------------------------------- #
# The panel tells players the trophy exists
# --------------------------------------------------------------------------- #
def test_the_panel_ladder_announces_the_champion_card():
    ladder = format_reward_ladder({1: 300}, champion_card_name="هیرو")
    assert "هیرو" in ladder
    assert "300" in ladder.replace("۳۰۰", "300")
    # Without the card (or the name) the ladder stays an Apple ladder only.
    assert "قهرمان" not in format_reward_ladder({1: 300})
    assert format_reward_ladder({}, "") == ""
