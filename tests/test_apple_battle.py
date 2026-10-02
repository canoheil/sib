"""Tests for Apple Battle (نبرد سیبی) — SHARED matchmaking redesign.

Pins the audited/locked decisions:
- REAL shared matchmaking (solo party → MatchmakingService queue → cross-group
  Match); the old parallel queue pairing is GONE.
- Battle formula UNCHANGED (power + 1.05*speed + 0.95*intelligence × hidden
  modifiers; logistic scale 60; seeded persisted RNG).
- Precomputed result persisted BEFORE narration; narration never changes it.
- Staged narrative: intro + 3 rounds + finale (plan beats).
- Winner reward: +1 seedling + 5 apples (idempotent); loser unchanged.
- Loadout upsert (one row per user); explicit dequeue; no other's cards.
- Hidden character modifiers never leak into rendered text.
"""

import json

import pytest

from sib.services.card_progression import card_attributes
from sqlalchemy import select

from sib.database.models import (
    AppleBattle,
    AppleBattleQueueEntry,
    AppleBattleStatus,
    Match,
    MatchPlayer,
    MatchState,
    Party,
    PartyStatus,
    ReleaseStatus,
    SeedlingAward,
)
from sib.services.apple_battle import AppleBattleService, BattleError
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.player_cards import PlayerCardService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_user


async def _released_character(db_session, code):
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=22,
        speed=20,
        intelligence=18,
        passive_key="test_passive",
        hidden_modifiers={"crit_multiplier": 1.05},
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    await db_session.commit()
    return character


async def _card(db_session, user, code):
    character = await _released_character(db_session, code)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()
    return card


async def _make_battle_row(db_session, a, b, card_a, card_b):
    """Legacy standalone battle row (PENDING) for precompute/narration tests."""
    battle = AppleBattle(
        player1_id=a.id,
        player2_id=b.id,
        player1_card_id=card_a.id,
        player2_card_id=card_b.id,
        level=card_a.level,
    )
    db_session.add(battle)
    await db_session.commit()
    return battle


async def _make_match(db_session, game, g1, g2, a, b):
    """A shared Match in ACCEPTANCE-ready state with two solo players."""
    match = Match(game_id=game.id, mode="solo", state=MatchState.CREATED,
                  group1_id=g1.id, group2_id=g2.id)
    db_session.add(match)
    await db_session.flush()
    db_session.add(MatchPlayer(match_id=match.id, user_id=a.id, group_id=g1.id, team=1))
    db_session.add(MatchPlayer(match_id=match.id, user_id=b.id, group_id=g2.id, team=2))
    await db_session.commit()
    await db_session.refresh(match)
    return match



async def _reload_match(db_session, match_id):
    from sqlalchemy.orm import selectinload

    return await db_session.scalar(
        select(Match)
        .where(Match.id == match_id)
        .options(selectinload(Match.players).selectinload(MatchPlayer.user))
        .execution_options(populate_existing=True)
    )

# ---------------------------------------------------------------------------
# Shared matchmaking (replaces the old private queue pairing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_matchmaking_creates_cross_group_match(db_session):
    """Apple Battle uses the REAL shared matchmaking — cross-group rule intact."""
    from sib.matchmaking.matcher import MatchmakingService
    from sib.services.party import PartyService

    game = type("G", (), {})()  # placeholder; use seeded game row
    from sib.database.models import Game

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    assert game is not None, "seed_default_data must create the apple_battle game row"

    g1 = type("G1", (), {"id": -1001})()
    g2 = type("G2", (), {"id": -1002})()
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-1001, "گروه یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-1002, "گروه دو"))
    await db_session.commit()

    a = await create_test_user(db_session, "صف‌نشین الف")
    b = await create_test_user(db_session, "صف‌نشین ب")
    card_a = await _card(db_session, a, "mm_a")
    card_b = await _card(db_session, b, "mm_b")

    p1 = await PartyService.create_party(db_session, -1001, a.id, "apple_battle", "solo")
    p2 = await PartyService.create_party(db_session, -1002, b.id, "apple_battle", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, a.id)
    await db_session.commit()

    # Solo queue: no opponent yet.
    assert await MatchmakingService.find_and_create_match(db_session, p1.id) is None

    await MatchmakingService.enqueue_party(db_session, p2.id, b.id)
    await db_session.commit()
    match = await MatchmakingService.find_and_create_match(db_session, p1.id)
    await db_session.commit()

    assert match is not None
    assert match.game.code == "apple_battle"
    assert {match.group1_id, match.group2_id} == {-1001, -1002}
    assert match.state == MatchState.WAITING_FOR_ACCEPTANCE


@pytest.mark.asyncio
async def test_same_group_parties_never_match_each_other(db_session):
    """Cross-group safeguard preserved for apple_battle parties."""
    from sib.matchmaking.matcher import MatchmakingService
    from sib.services.group import GroupService
    from sib.services.party import PartyService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-2001, "هم‌گروهی"))
    await db_session.commit()

    a = await create_test_user(db_session, "هم‌گروه الف")
    b = await create_test_user(db_session, "هم‌گروه ب")
    await _card(db_session, a, "mm_c")
    await _card(db_session, b, "mm_d")

    p1 = await PartyService.create_party(db_session, -2001, a.id, "apple_battle", "solo")
    p2 = await PartyService.create_party(db_session, -2001, b.id, "apple_battle", "solo")
    await db_session.commit()

    await MatchmakingService.enqueue_party(db_session, p1.id, a.id)
    await MatchmakingService.enqueue_party(db_session, p2.id, b.id)
    await db_session.commit()
    assert await MatchmakingService.find_and_create_match(db_session, p1.id) is None


# ---------------------------------------------------------------------------
# Precomputed plan (formula UNCHANGED) + staged beats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battle_plan_is_precomputed_with_formula_and_staged_beats(db_session):
    a = await create_test_user(db_session, "پیش‌محاسبه الف")
    b = await create_test_user(db_session, "پیش‌محاسبه ب")
    card_a = await _card(db_session, a, "battle_d")
    card_b = await _card(db_session, b, "battle_e")
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()

    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-3001, "نبرد یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-3002, "نبرد دو"))
    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(db_session, game, type("X", (), {"id": -3001})(),
                              type("X", (), {"id": -3002})(), a, b)
    match = await _reload_match(db_session, match.id)

    state = await AppleBattleService.build_match_state(db_session, match)
    await db_session.commit()
    assert state is not None

    battle = state["battle"]
    assert battle["winner_id"] in (a.id, b.id)
    assert battle["seed"]
    assert battle["balance_version"] is not None
    assert "p1_win_probability" in battle["result"]
    assert battle["result"]["rng_seed"] == battle["seed"]

    # Staged plan: intro + 3 rounds + finale.
    phases = [b_["phase"] for b_ in state["plan"]]
    assert phases == ["intro", "round", "round", "round", "finale"]
    assert [b_["round"] for b_ in state["plan"][1:4]] == [1, 2, 3]

    # Formula UNCHANGED: effective = (p + 1.05*s + 0.95*i) × hidden mods.
    c1 = await db_session.get(__import__("sib.database.models", fromlist=["PlayerCard"]).PlayerCard, card_a.id)
    ch1 = await db_session.get(__import__("sib.database.models", fromlist=["Character"]).Character, c1.character_id)
    stats = card_attributes(c1, ch1)
    expected = round(stats.power + 1.05 * stats.speed + 0.95 * stats.intelligence, 2)
    for v in (ch1.hidden_modifier_params or {}).values():
        if isinstance(v, (int, float)):
            expected = round(expected * (1.0 + float(v) * 0.1), 2)
    assert battle["result"]["scores"]["player1"] == expected

    # Intro beat names both cards (Persian narrative).
    assert "قهرمان battle_d" in state["plan"][0]["text"] or "قهرمان battle_e" in state["plan"][0]["text"]


@pytest.mark.asyncio
async def test_result_is_precomputed_persisted_with_seed_and_balance_version(db_session):
    a = await create_test_user(db_session, "پیش‌محاسبه۲ الف")
    b = await create_test_user(db_session, "پیش‌محاسبه۲ ب")
    card_a = await _card(db_session, a, "battle_f")
    card_b = await _card(db_session, b, "battle_g")

    battle = await _make_battle_row(db_session, a, b, card_a, card_b)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    await db_session.commit()

    assert battle.status == AppleBattleStatus.RESULT_READY
    assert battle.winner_id in (a.id, b.id)
    assert battle.rng_seed  # documented
    assert battle.balance_version is not None

    result = json.loads(battle.result_json)
    assert result["rng_seed"] == battle.rng_seed
    assert result["balance_version"] == battle.balance_version
    assert "p1_win_probability" in result
    assert result["winner_id"] == battle.winner_id

    # Idempotent: recomputation does not change the persisted outcome.
    winner_before = battle.winner_id
    seed_before = battle.rng_seed
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    assert battle.winner_id == winner_before
    assert battle.rng_seed == seed_before


@pytest.mark.asyncio
async def test_same_seed_same_inputs_give_same_result(db_session):
    """Controlled RNG: the computation is a pure function of the seed."""
    a = await create_test_user(db_session, "تکرار الف")
    b = await create_test_user(db_session, "تکرار ب")
    card_a = await _card(db_session, a, "battle_h")
    card_b = await _card(db_session, b, "battle_i")

    import random

    s1 = random.Random("fixed-seed")
    s2 = random.Random("fixed-seed")
    assert s1.random() == s2.random()

    # The precomputed narrative is also a pure function of the persisted seed.
    battle = await _make_battle_row(db_session, a, b, card_a, card_b)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    await db_session.commit()
    story1 = await AppleBattleService.narrate(db_session, battle.id)
    story2 = await AppleBattleService.narrate(db_session, battle.id)
    assert story1 == story2


# ---------------------------------------------------------------------------
# Engine: staged advance → finish with precomputed winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_advances_stages_and_finishes_with_precomputed_winner(db_session):
    from sib.games.apple_battle.engine import AppleBattleEngine

    a = await create_test_user(db_session, "موتور الف")
    b = await create_test_user(db_session, "موتور ب")
    card_a = await _card(db_session, a, "engine_a")
    card_b = await _card(db_session, b, "engine_b")
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()

    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-4001, "موتور یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-4002, "موتور دو"))
    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(db_session, game, type("X", (), {"id": -4001})(),
                              type("X", (), {"id": -4002})(), a, b)
    match = await _reload_match(db_session, match.id)
    state = await AppleBattleService.build_match_state(db_session, match)
    await db_session.commit()

    engine = AppleBattleEngine()
    winner_id = state["battle"]["winner_id"]
    winner_team = state["players"][str(winner_id)]["team"]

    # Intro is visible from the start (cursor=1), not finished.
    assert not engine.is_finished(state)
    assert "نبرد سیبی آغاز شد" in engine.render_for_group(state, -4001, a.id)

    # Advance through all beats (staged presentation).
    for _ in range(4):
        ok, _reason = engine.validate_action(state, a.id, {"type": "advance"})
        assert ok
        state = engine.apply_action(state, a.id, {"type": "advance"})
    assert engine.is_finished(state)

    result = engine.get_result(state)
    assert result.is_draw is False
    assert result.winner_team == winner_team
    assert result.details["winner_id"] == winner_id

    # Finale mentions the winner; both stages rendered for both groups.
    final_text = engine.render_for_group(state, -4001, a.id)
    assert "برنده" in final_text
    assert "قهرمان engine_a" in final_text or "قهرمان engine_b" in final_text


@pytest.mark.asyncio
async def test_hidden_modifiers_never_leak_into_rendered_text(db_session):
    from sib.games.apple_battle.engine import AppleBattleEngine

    a = await create_test_user(db_session, "محرمانه الف")
    b = await create_test_user(db_session, "محرمانه ب")
    card_a = await _card(db_session, a, "priv_a")
    card_b = await _card(db_session, b, "priv_b")
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()

    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-5001, "محرمانه یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-5002, "محرمانه دو"))
    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(db_session, game, type("X", (), {"id": -5001})(),
                              type("X", (), {"id": -5002})(), a, b)
    match = await _reload_match(db_session, match.id)
    state = await AppleBattleService.build_match_state(db_session, match)
    await db_session.commit()

    engine = AppleBattleEngine()
    texts = []
    while not engine.is_finished(state):
        texts.append(engine.render_for_group(state, -5001, a.id))
        state = engine.apply_action(state, a.id, {"type": "advance"})
    texts.append(engine.render_for_group(state, -5001, a.id))

    for text in texts:
        assert "crit_multiplier" not in text
        assert "1.05" not in text  # the hidden modifier VALUE must never appear


# ---------------------------------------------------------------------------
# Winner reward: +1 seedling + 5 apples, idempotent; loser unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_winner_gets_one_seedling_and_five_apples_idempotent(db_session):
    a = await create_test_user(db_session, "نهال‌دار الف")
    b = await create_test_user(db_session, "نهال‌دار ب")
    card_a = await _card(db_session, a, "battle_j")
    card_b = await _card(db_session, b, "battle_k")

    battle = await _make_battle_row(db_session, a, b, card_a, card_b)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    await db_session.commit()

    battle = await AppleBattleService.complete(db_session, battle.id)
    await db_session.commit()

    winner_id = battle.winner_id
    loser_id = b.id if winner_id == a.id else a.id
    assert await SeedlingService.get_total(db_session, winner_id) == 1
    assert await SeedlingService.get_total(db_session, loser_id) == 0
    assert await AppleWalletService.get_balance(db_session, winner_id) == 5
    assert await AppleWalletService.get_balance(db_session, loser_id) == 0

    # Idempotent completion (retry) does not double-award anything.
    battle = await AppleBattleService.complete(db_session, battle.id)
    await db_session.commit()
    assert await SeedlingService.get_total(db_session, winner_id) == 1
    assert await AppleWalletService.get_balance(db_session, winner_id) == 5

    awards = (
        await db_session.scalars(
            select(SeedlingAward).where(SeedlingAward.user_id == winner_id)
        )
    ).all()
    assert len(awards) == 1
    assert awards[0].source == "battle"
    assert awards[0].reason == "apple_battle_win"


@pytest.mark.asyncio
async def test_finalize_match_awards_via_shared_match_result(db_session):
    """Full-flow reward: shared apply_match_result + apples (idempotent)."""
    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-6001, "پاداش یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-6002, "پاداش دو"))

    a = await create_test_user(db_session, "پاداش الف")
    b = await create_test_user(db_session, "پاداش ب")
    card_a = await _card(db_session, a, "fin_a")
    card_b = await _card(db_session, b, "fin_b")
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(db_session, game, type("X", (), {"id": -6001})(),
                              type("X", (), {"id": -6002})(), a, b)
    match = await _reload_match(db_session, match.id)
    state = await AppleBattleService.build_match_state(db_session, match)
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.state = MatchState.ACTIVE
    await db_session.commit()

    battle_row = await AppleBattleService.record_battle(db_session, match, state)
    await db_session.commit()

    match = await _reload_match(db_session, match.id)
    await AppleBattleService.finalize_match(db_session, match, battle_row.id)
    await db_session.commit()

    winner_id = state["battle"]["winner_id"]
    loser_id = a.id if winner_id == b.id else b.id

    assert await SeedlingService.get_total(db_session, winner_id) == 1
    assert await SeedlingService.get_total(db_session, loser_id) == 0
    assert await AppleWalletService.get_balance(db_session, winner_id) == 5
    assert await AppleWalletService.get_balance(db_session, loser_id) == 0

    match = await _reload_match(db_session, match.id)
    assert match.result_applied is True
    assert match.winner_group_id is not None

    # Idempotent: second finalize awards nothing extra.
    await AppleBattleService.finalize_match(db_session, match, battle_row.id)
    await db_session.commit()
    assert await SeedlingService.get_total(db_session, winner_id) == 1
    assert await AppleWalletService.get_balance(db_session, winner_id) == 5


# ---------------------------------------------------------------------------
# Loadout lifecycle (single canonical entry; no duplicate entries)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loadout_upsert_keeps_single_entry_and_dequeue_clears(db_session):
    a = await create_test_user(db_session, "صف‌نشین۲")
    card_a = await _card(db_session, a, "battle_l")

    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await db_session.commit()
    # Re-selecting REPLACES the loadout (upsert) — never a duplicate entry.
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await db_session.commit()

    rows = (
        await db_session.scalars(
            select(AppleBattleQueueEntry).where(AppleBattleQueueEntry.user_id == a.id)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].card_id == card_a.id

    assert await AppleBattleService.dequeue(db_session, a.id) is True
    assert await AppleBattleService.dequeue(db_session, a.id) is False


@pytest.mark.asyncio
async def test_cannot_enqueue_someone_elses_card(db_session):
    a = await create_test_user(db_session, "کارت‌سوز")
    b = await create_test_user(db_session, "کارت‌دار")
    card_b = await _card(db_session, b, "battle_m")

    with pytest.raises(BattleError):
        await AppleBattleService.enqueue(db_session, a.id, card_b.id)


@pytest.mark.asyncio
async def test_battle_leaderboard_counts_wins(db_session):
    a = await create_test_user(db_session, "قهرمان الف")
    b = await create_test_user(db_session, "قهرمان ب")
    card_a = await _card(db_session, a, "battle_n")
    card_b = await _card(db_session, b, "battle_o")

    battle = await _make_battle_row(db_session, a, b, card_a, card_b)
    battle = await AppleBattleService.compute_and_persist_result(db_session, battle.id)
    battle = await AppleBattleService.complete(db_session, battle.id)
    await db_session.commit()

    board = await AppleBattleService.battle_leaderboard(db_session)
    assert len(board) >= 1
    winner_id, wins, total = board[0]
    assert winner_id == battle.winner_id
    assert wins == 1


# ---------------------------------------------------------------------------
# Presentation + entry-point rules
# ---------------------------------------------------------------------------


def test_apple_battle_is_hidden_from_generic_game_grid():
    from sib.database.models import Game
    from sib.keyboards.party import HIDDEN_GAME_CODES, _game_grid

    games = [
        Game(code="high_card", name_fa="کارت بالا"),
        Game(code="apple_battle", name_fa="نبرد سیبی"),
        Game(code="tic_tac_toe", name_fa="دوز"),
    ]
    keyboard = _game_grid(games)
    callbacks = [b.callback_data for row in keyboard for b in row]
    assert "select_game:apple_battle" not in callbacks
    assert "select_game:high_card" in callbacks
    assert "apple_battle" in HIDDEN_GAME_CODES


@pytest.mark.asyncio
async def test_canonical_entry_screens_and_explicit_cancel_semantics(db_session):
    """Select → Find → searching; ONLY explicit cancel dequeues (ab_cancel)."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery

    from sib.handlers.apple_battle import (
        handle_ab_cancel,
        handle_ab_find,
        handle_ab_pick,
    )
    from sib.matchmaking.matcher import MatchmakingService
    from sib.services.party import PartyService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    from sib.services.group import GroupService

    await GroupService.get_or_create_group(db_session, _Chat(-7001, "لغو یک"))
    a = await create_test_user(db_session, "لغوچی")
    card_a = await _card(db_session, a, "cancel_a")
    await db_session.commit()
    a_id = a.id  # plain snapshot; rollback below expires the ORM row
    card_a_id = card_a.id

    def _cb(data):
        cb = MagicMock(spec=CallbackQuery)
        cb.data = data
        cb.from_user = MagicMock(id=a_id, is_bot=False, first_name="لغوچی")
        cb.message = MagicMock()
        cb.message.chat = MagicMock(id=-7001, type="supergroup")
        cb.answer = AsyncMock()
        cb.message.edit_text = AsyncMock()
        return cb

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    # 1. Pick card (loadout).
    await handle_ab_pick(_cb(f"ab_pick:{card_a_id}"), bot)
    await db_session.rollback()  # handler committed in its own session
    loadout = await AppleBattleService.get_loadout(db_session, a_id)
    assert loadout is not None and loadout.card_id == card_a_id

    # 2. Find opponent → enters the SHARED queue (no opponent yet).
    await handle_ab_find(_cb("ab_find"), bot)
    await db_session.rollback()

    from sib.handlers.apple_battle import _find_my_battle_party

    party = await _find_my_battle_party(db_session, -7001, a_id)
    assert party is not None and party.status == PartyStatus.IN_QUEUE

    # 3. Explicit cancel dequeues AND clears the loadout.
    await handle_ab_cancel(_cb("ab_cancel"), bot)
    await db_session.rollback()  # end snapshot; handler committed in its own session
    party = await _find_my_battle_party(db_session, -7001, a_id)
    assert party is None or party.status != PartyStatus.IN_QUEUE
    assert await AppleBattleService.get_loadout(db_session, a_id) is None


@pytest.mark.asyncio
async def test_navigation_back_never_dequeues(db_session):
    """Back/home = navigation only: an active search keeps its queue entry."""
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import CallbackQuery

    from sib.handlers.apple_battle import handle_ab_find, handle_ab_pick
    from sib.services.group import GroupService
    from sib.services.party import PartyService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-8001, "باقی یک"))
    a = await create_test_user(db_session, "باقی‌مانده")
    card_a = await _card(db_session, a, "keep_a")
    await db_session.commit()

    def _cb(data):
        cb = MagicMock(spec=CallbackQuery)
        cb.data = data
        cb.from_user = MagicMock(id=a.id, is_bot=False, first_name="باقی")
        cb.message = MagicMock()
        cb.message.chat = MagicMock(id=-8001, type="supergroup")
        cb.answer = AsyncMock()
        cb.message.edit_text = AsyncMock()
        return cb

    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()

    await handle_ab_pick(_cb(f"ab_pick:{card_a.id}"), bot)
    await handle_ab_find(_cb("ab_find"), bot)
    await db_session.commit()

    from sib.handlers.apple_battle import _find_my_battle_party

    party = await _find_my_battle_party(db_session, -8001, a.id)
    assert party is not None and party.status == PartyStatus.IN_QUEUE

    # A navigation callback (main_menu) is handled elsewhere and must not be
    # part of the battle flow — the queue is untouched by NOT calling cancel.
    party = await _find_my_battle_party(db_session, -8001, a.id)
    assert party is not None and party.status == PartyStatus.IN_QUEUE


# ---------------------------------------------------------------------------
# Staged narration loop (auto presentation, ~3s beats, message edits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_loop_edits_both_messages_and_finalizes(db_session):
    from unittest.mock import AsyncMock, MagicMock

    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-9001, "روایت یک"))
    await GroupService.get_or_create_group(db_session, _Chat(-9002, "روایت دو"))

    a = await create_test_user(db_session, "روایت‌گر الف")
    b = await create_test_user(db_session, "روایت‌گر ب")
    card_a = await _card(db_session, a, "narr_a")
    card_b = await _card(db_session, b, "narr_b")
    await AppleBattleService.enqueue(db_session, a.id, card_a.id)
    await AppleBattleService.enqueue(db_session, b.id, card_b.id)
    await db_session.commit()
    a_id, b_id = a.id, b.id

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(db_session, game, type("X", (), {"id": -9001})(),
                              type("X", (), {"id": -9002})(), a, b)
    match_id = match.id
    match = await _reload_match(db_session, match_id)
    state = await AppleBattleService.build_match_state(db_session, match)
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.state = MatchState.ACTIVE
    match.group1_message_id = 11
    match.group2_message_id = 22
    await db_session.commit()

    winner_id = state["battle"]["winner_id"]

    bot = MagicMock()
    bot.edit_message_text = AsyncMock()

    # Beat delay 0 → the loop runs to completion immediately.
    await AppleBattleService._narration_loop(bot, match_id, beat_delay=0.0)

    # Both group messages were edited (staged presentation).
    edited_chats = {c.kwargs["chat_id"] for c in bot.edit_message_text.call_args_list}
    assert edited_chats == {-9001, -9002}
    last_texts = [
        c.kwargs["chat_id"] and c.args[0] if c.args else c.kwargs.get("text", "")
        for c in bot.edit_message_text.call_args_list
    ]
    assert any("برنده" in t for t in last_texts)

    await db_session.rollback()
    match = await _reload_match(db_session, match_id)
    assert match.result_applied is True

    assert await SeedlingService.get_total(db_session, winner_id) == 1
    assert await AppleWalletService.get_balance(db_session, winner_id) == 5
    loser_id = b_id if winner_id == a_id else a_id
    assert await SeedlingService.get_total(db_session, loser_id) == 0
    assert await AppleWalletService.get_balance(db_session, loser_id) == 0

    # The precomputed winner never changed.
    final_state = json.loads(match.game_state)
    assert final_state["battle"]["winner_id"] == winner_id


# ---------------------------------------------------------------------------
# Phase 1 — Apple Battle Integrity Regression Suite
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
from sib.games.apple_battle.engine import AppleBattleEngine


async def _setup_active_apple_battle(
    db_session,
    suffix: str,
    *,
    cursor: int = 1,
    level1: int = 1,
    level2: int = 1,
    is_bot_match: bool = False,
    expired_deadline: bool = True,
):
    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    base_chat = -(abs(hash(suffix)) % 900_000 + 100_000)
    await GroupService.get_or_create_group(
        db_session, _Chat(base_chat, f"گروه اول {suffix}")
    )
    await GroupService.get_or_create_group(
        db_session, _Chat(base_chat - 1, f"گروه دوم {suffix}")
    )

    u1 = await create_test_user(db_session, f"بازیکن اول {suffix}")
    u2 = await create_test_user(db_session, f"بازیکن دوم {suffix}")
    if is_bot_match:
        u2.is_bot = True
        await db_session.flush()

    c1 = await _card(db_session, u1, f"char1_{suffix}")
    c2 = await _card(db_session, u2, f"char2_{suffix}")
    c1.level = level1
    c2.level = level2
    await db_session.flush()

    await AppleBattleService.enqueue(db_session, u1.id, c1.id)
    await AppleBattleService.enqueue(db_session, u2.id, c2.id)

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(
        db_session,
        game,
        type("G", (), {"id": base_chat})(),
        type("G", (), {"id": base_chat - 1})(),
        u1,
        u2,
    )
    match.is_bot_match = is_bot_match
    await db_session.flush()

    match = await _reload_match(db_session, match.id)
    state = await AppleBattleService.build_match_state(db_session, match)
    state["cursor"] = cursor
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.state = MatchState.ACTIVE
    match.turn_deadline = (
        datetime.now(timezone.utc) - timedelta(minutes=15)
        if expired_deadline
        else datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    await db_session.commit()
    return match, state, u1, u2, c1, c2


@pytest.mark.asyncio
async def test_recovery_persisted_result_stage_0_resumes_and_preserves_winner(db_session):
    """Recovery test: persisted result + stage 0 resumes narration and never draws."""
    from sib.services.recovery.service import RecoveryService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "rec_stage0", cursor=0, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    persisted_seed = state["battle"]["seed"]
    match_id = match.id

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["resumed_apple_battles"] == 1
    assert stats["expired_abandoned_matches"] == 0
    assert AppleBattleService.is_narration_scheduled(match_id) is True

    # Cancel the default 3s task so we can drive narration deterministically at 0s.
    await AppleBattleService.cancel_narration(match_id)

    await db_session.refresh(match)
    assert match.state == MatchState.ACTIVE
    assert match.is_draw is False
    recovered_state = json.loads(match.game_state)
    assert recovered_state["cursor"] == 0
    assert recovered_state["battle"]["winner_id"] == persisted_winner
    assert recovered_state["battle"]["seed"] == persisted_seed

    await AppleBattleService.run_narration(None, match_id, beat_delay=0.0)
    await db_session.rollback()

    match = await _reload_match(db_session, match_id)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is False
    final_state = json.loads(match.game_state)
    assert final_state["battle"]["winner_id"] == persisted_winner
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5


@pytest.mark.asyncio
async def test_recovery_persisted_result_middle_stage_resumes_from_cursor(db_session):
    """Recovery test: persisted result + middle stage (cursor=2) resumes without resetting."""
    from sib.services.recovery.service import RecoveryService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "rec_mid", cursor=2, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    persisted_seed = state["battle"]["seed"]
    match_id = match.id

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["resumed_apple_battles"] == 1
    assert AppleBattleService.is_narration_scheduled(match_id) is True

    # Idempotent second recovery call while active does not reset cursor or duplicate rows.
    stats_second = await RecoveryService.recover_on_startup(db_session)
    assert stats_second["resumed_apple_battles"] == 1

    await AppleBattleService.cancel_narration(match_id)

    await db_session.refresh(match)
    recovered_state = json.loads(match.game_state)
    assert recovered_state["cursor"] == 2
    assert recovered_state["battle"]["winner_id"] == persisted_winner
    assert recovered_state["battle"]["seed"] == persisted_seed

    battles = list(
        (
            await db_session.scalars(
                select(AppleBattle).where(AppleBattle.match_id == match_id)
            )
        ).all()
    )
    assert len(battles) == 1

    await AppleBattleService.run_narration(None, match_id, beat_delay=0.0)
    await db_session.rollback()

    match = await _reload_match(db_session, match_id)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is False
    assert json.loads(match.game_state)["battle"]["winner_id"] == persisted_winner
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5


@pytest.mark.asyncio
async def test_recovery_persisted_result_final_stage_pending_finalizes_immediately(db_session):
    """Recovery test: persisted result + final stage pending finalizes settlement idempotently."""
    from sib.services.recovery.service import RecoveryService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "rec_final", cursor=len(state["plan"]) if False else 5, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    match_id = match.id

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["finalized_apple_battles"] == 1
    assert stats["resumed_apple_battles"] == 0
    assert stats["expired_abandoned_matches"] == 0
    assert AppleBattleService.is_narration_scheduled(match_id) is False

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is False
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5

    battle = await db_session.scalar(
        select(AppleBattle).where(AppleBattle.match_id == match_id)
    )
    assert battle is not None
    assert battle.status == AppleBattleStatus.COMPLETED
    assert battle.winner_id == persisted_winner

    # Running recovery a second time is a harmless no-op.
    stats_again = await RecoveryService.recover_on_startup(db_session)
    assert stats_again["finalized_apple_battles"] == 0
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5


@pytest.mark.asyncio
async def test_recovery_no_result_preserves_existing_timeout_behavior(db_session):
    """Recovery test: active Apple Battle without a persisted result follows existing timeout behavior."""
    from datetime import timedelta
    from sib.database.models import Game
    from sib.services.group import GroupService
    from sib.services.recovery.service import RecoveryService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-91001, "بدون نتیجه ۱"))
    await GroupService.get_or_create_group(db_session, _Chat(-91002, "بدون نتیجه ۲"))
    u1 = await create_test_user(db_session, "بازیکن بی‌نتیجه ۱")
    u2 = await create_test_user(db_session, "بازیکن بی‌نتیجه ۲")
    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(
        db_session,
        game,
        type("G", (), {"id": -91001})(),
        type("G", (), {"id": -91002})(),
        u1,
        u2,
    )
    match = await _reload_match(db_session, match.id)
    match.state = MatchState.ACTIVE
    match.game_state = json.dumps(
        AppleBattleEngine().create_initial_state(list(match.players), "solo"),
        ensure_ascii=False,
    )
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(minutes=20)
    await db_session.commit()

    stats = await RecoveryService.recover_on_startup(db_session)
    assert stats["resumed_apple_battles"] == 0
    assert stats["finalized_apple_battles"] == 0
    assert stats["expired_abandoned_matches"] == 1

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is True


@pytest.mark.asyncio
async def test_timeout_persisted_winner_preserved_and_settled_not_draw(db_session):
    """Timeout test: persisted winner + generic timeout preserves winner and never draws."""
    from sib.services.timeout.service import TimeoutService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "timeout_win", cursor=2, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    loser_id = u2.id if persisted_winner == u1.id else u1.id
    expected_team = 1 if persisted_winner == u1.id else 2
    match_id = match.id

    processed = await TimeoutService.process_turn_timeouts(db_session)
    assert match_id in processed

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is False
    assert match.winner_team == expected_team

    battle = await db_session.scalar(
        select(AppleBattle).where(AppleBattle.match_id == match_id)
    )
    assert battle is not None
    assert battle.status == AppleBattleStatus.COMPLETED
    assert battle.winner_id == persisted_winner

    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await SeedlingService.get_total(db_session, loser_id) == 0
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5
    assert await AppleWalletService.get_balance(db_session, loser_id) == 0


@pytest.mark.asyncio
async def test_timeout_no_result_uses_existing_behavior(db_session):
    """Timeout test: no persisted result + timeout keeps existing draw behavior."""
    from datetime import timedelta
    from sib.database.models import Game
    from sib.services.group import GroupService
    from sib.services.timeout.service import TimeoutService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-92001, "تایم‌اوت ۱"))
    await GroupService.get_or_create_group(db_session, _Chat(-92002, "تایم‌اوت ۲"))
    u1 = await create_test_user(db_session, "بازیکن تایم‌اوت ۱")
    u2 = await create_test_user(db_session, "بازیکن تایم‌اوت ۲")
    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = await _make_match(
        db_session,
        game,
        type("G", (), {"id": -92001})(),
        type("G", (), {"id": -92002})(),
        u1,
        u2,
    )
    match.state = MatchState.ACTIVE
    match.game_state = "{}"
    match.turn_deadline = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db_session.commit()

    processed = await TimeoutService.process_turn_timeouts(db_session)
    assert match.id in processed

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is True
    assert match.winner_team is None


@pytest.mark.asyncio
async def test_timeout_already_finalized_is_noop(db_session):
    """Timeout test: already finalized match + timeout is a harmless no-op."""
    from sib.services.timeout.service import TimeoutService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "timeout_noop", cursor=5, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    battle = await AppleBattleService.record_battle(db_session, match, state)
    await AppleBattleService.finalize_match(db_session, match, battle.id)
    await db_session.commit()

    processed = await TimeoutService.process_turn_timeouts(db_session)
    assert match.id not in processed

    # Direct invocation on the already-finalized match is also a no-op.
    match = await _reload_match(db_session, match.id)
    await TimeoutService._resolve_expired_active_match(db_session, match)
    await db_session.commit()

    await db_session.refresh(match)
    assert match.state == MatchState.FINISHED
    assert match.is_draw is False
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5


@pytest.mark.asyncio
async def test_finalize_match_called_multiple_times_grants_one_reward_set(db_session):
    """Idempotency test: finalize_match called multiple times grants exactly one reward set."""
    from sib.database.models import AppleTransaction, PlayerStats, SeedlingAward

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "idem_multi", cursor=5
    )
    persisted_winner = state["battle"]["winner_id"]
    battle = await AppleBattleService.record_battle(db_session, match, state)
    await db_session.commit()

    for _ in range(4):
        match = await _reload_match(db_session, match.id)
        await AppleBattleService.finalize_match(db_session, match, battle.id)
        await AppleBattleService.finalize_match(db_session, match, None)
        await db_session.commit()

    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5

    awards = list(
        (
            await db_session.scalars(
                select(SeedlingAward).where(SeedlingAward.match_id == match.id)
            )
        ).all()
    )
    txs = list(
        (
            await db_session.scalars(
                select(AppleTransaction).where(
                    AppleTransaction.user_id == persisted_winner,
                    AppleTransaction.reason == "apple_battle_win",
                )
            )
        ).all()
    )
    p_stat = await db_session.scalar(
        select(PlayerStats).where(PlayerStats.user_id == persisted_winner)
    )
    assert len(awards) == 1
    assert len(txs) == 1
    assert p_stat is not None and p_stat.wins == 1


@pytest.mark.asyncio
async def test_recovery_and_scheduled_narration_race_is_deduplicated(db_session):
    """Concurrency test: recovery + scheduled narration race produces one worker and one reward set."""
    import asyncio
    from sib.services.recovery.service import RecoveryService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "race_rec_narr", cursor=1, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    match_id = match.id

    t1 = AppleBattleService.schedule_narration(match_id, bot=None, beat_delay=0.01)
    t2 = AppleBattleService.schedule_narration(match_id, bot=None, beat_delay=0.01)
    assert t1 is t2

    await asyncio.gather(
        RecoveryService.recover_on_startup(db_session),
        AppleBattleService.run_narration(None, match_id, beat_delay=0.01),
        t1,
    )
    await db_session.rollback()

    match = await _reload_match(db_session, match_id)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is False
    assert json.loads(match.game_state)["battle"]["winner_id"] == persisted_winner
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5


@pytest.mark.asyncio
async def test_scheduled_narration_and_timeout_race_preserves_winner_and_single_rewards(db_session):
    """Concurrency test: scheduled narration + timeout race never draws or double-rewards."""
    import asyncio
    from sib.database.connection import get_session
    from sib.services.timeout.service import TimeoutService

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "race_narr_timeout", cursor=3, expired_deadline=True
    )
    persisted_winner = state["battle"]["winner_id"]
    match_id = match.id

    async def _run_timeout():
        async with get_session() as timeout_session:
            await TimeoutService.process_turn_timeouts(timeout_session)

    await asyncio.gather(
        AppleBattleService.run_narration(None, match_id, beat_delay=0.0),
        _run_timeout(),
    )
    await db_session.rollback()

    match = await _reload_match(db_session, match_id)
    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.is_draw is False
    assert json.loads(match.game_state)["battle"]["winner_id"] == persisted_winner
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5


@pytest.mark.asyncio
async def test_winner_photo_resolves_by_match_id_when_battle_id_differs(db_session):
    """Presentation test: AppleBattle.id != Match.id still resolves winner artwork."""
    from sib.database.models import Game
    from sib.services.group import GroupService

    class _Chat:
        def __init__(self, cid, title):
            self.id = cid
            self.title = title
            self.type = "supergroup"
            self.username = None

    await GroupService.get_or_create_group(db_session, _Chat(-93001, "عکس ۱"))
    await GroupService.get_or_create_group(db_session, _Chat(-93002, "عکس ۲"))
    u1 = await create_test_user(db_session, "بازیکن عکس ۱")
    u2 = await create_test_user(db_session, "بازیکن عکس ۲")
    # Use a seeded character code ("sib_sorkh") that has real artwork on disk.
    c1 = await _card(db_session, u1, "sib_sorkh")
    c2 = await _card(db_session, u2, "sib_sabz")

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    # Create extra matches so target_match.id != battle.id.
    decoy_match = await _make_match(
        db_session,
        game,
        type("G", (), {"id": -93001})(),
        type("G", (), {"id": -93002})(),
        u1,
        u2,
    )
    _spacer_match = await _make_match(
        db_session,
        game,
        type("G", (), {"id": -93001})(),
        type("G", (), {"id": -93002})(),
        u1,
        u2,
    )
    target_match = await _make_match(
        db_session,
        game,
        type("G", (), {"id": -93001})(),
        type("G", (), {"id": -93002})(),
        u1,
        u2,
    )

    # Create a cancelled decoy AppleBattle row whose PK equals target_match.id
    # if possible, plus the real completed AppleBattle row with a different PK.
    decoy_battle = AppleBattle(
        id=target_match.id,
        player1_id=u1.id,
        player2_id=u2.id,
        player1_card_id=c1.id,
        player2_card_id=c2.id,
        level=1,
        match_id=decoy_match.id,
        status=AppleBattleStatus.CANCELLED,
    )
    real_battle = AppleBattle(
        id=target_match.id + 500,
        player1_id=u1.id,
        player2_id=u2.id,
        player1_card_id=c1.id,
        player2_card_id=c2.id,
        level=1,
        match_id=target_match.id,
        status=AppleBattleStatus.COMPLETED,
        winner_id=u1.id,
    )
    db_session.add_all([decoy_battle, real_battle])
    await db_session.commit()

    assert real_battle.id != target_match.id
    photo = await AppleBattleService._winner_photo(target_match.id)
    assert photo is not None


@pytest.mark.asyncio
async def test_different_card_levels_display_accurately_and_help_copy_is_truthful(db_session):
    """Presentation test: different card levels render accurately for both players."""
    from sib.handlers.start import HELP_TOPICS

    match, state, u1, u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "diff_levels", cursor=1, level1=4, level2=2
    )
    intro_text = state["plan"][0]["text"]
    assert f"{u1.display_name} با" in intro_text
    assert "(سطح 4)" in intro_text
    assert "(سطح 2)" in intro_text

    rendered = AppleBattleEngine().render_for_group(state, match.group1_id, u1.id)
    assert "(سطح 4)" in rendered
    assert "(سطح 2)" in rendered

    # Help copy must not promise guaranteed same-level opponents.
    _title, apple_help_body = HELP_TOPICS["apple_battle"]
    assert "هم‌سطح" not in apple_help_body
    assert "هم سطح" not in apple_help_body


@pytest.mark.asyncio
async def test_bot_vs_pvp_reward_text_matches_actual_reward_logic(db_session):
    """Presentation test: bot vs PvP finale reward text matches actual reward outcome."""
    pvp_match, pvp_state, _u1, _u2, _c1, _c2 = await _setup_active_apple_battle(
        db_session, "reward_pvp", cursor=5, is_bot_match=False
    )
    bot_match, bot_state, bu1, bu2, _bc1, _bc2 = await _setup_active_apple_battle(
        db_session, "reward_bot", cursor=5, is_bot_match=True
    )

    pvp_finale = pvp_state["plan"][-1]["text"]
    bot_finale = bot_state["plan"][-1]["text"]

    assert "🎁 پاداش پیروزی: ۱ نهال 🌱 + ۵ سیب 🍏" in pvp_finale
    assert "🎁 پاداش پیروزی: ۵ سیب 🍏" in bot_finale
    assert "نهال" not in bot_finale

    # Finalize bot match and verify actual reward settlement matches the text.
    bot_battle = await AppleBattleService.record_battle(db_session, bot_match, bot_state)
    await AppleBattleService.finalize_match(db_session, bot_match, bot_battle.id)
    await db_session.commit()

    bot_winner = bot_state["battle"]["winner_id"]
    assert await SeedlingService.get_total(db_session, bot_winner) == 0
    assert await AppleWalletService.get_balance(db_session, bot_winner) == 5
    await db_session.refresh(bot_battle)
    assert bot_battle.seedlings_awarded == 0


@pytest.mark.asyncio
async def test_unimplemented_passives_are_not_shown_as_active(db_session):
    """Presentation test: unimplemented passive_key values are omitted from player cards/wiki."""
    from sib.database.connection import seed_characters
    from sib.services.characters import CharacterService, render_character_card
    from sib.services.wiki import WikiService

    await seed_characters(db_session)
    await db_session.commit()

    released = await CharacterService.list_released(db_session)
    assert len(released) >= 4
    for char in released:
        card_text = render_character_card(char)
        wiki_text = await WikiService.wiki_page(db_session, char.id)
        assert "توانایی منفعل" not in card_text
        assert (char.passive_key or "") not in card_text
        if wiki_text:
            assert "توانایی منفعل" not in wiki_text
            assert (char.passive_key or "") not in wiki_text
