"""Regression coverage for shared PvP matchmaking and bot fallback Matches."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from sib.database.models import (
    AppleBattleQueueEntry,
    Character,
    GroupStats,
    Match,
    MatchPlayer,
    MatchState,
    MatchmakingQueue,
    PartyStatus,
    PlayerCard,
    PlayerStats,
    ReleaseStatus,
    SeedlingInventory,
)
from sib.games.base import GameResult
from sib.games.registry import get_game_engine
from sib.handlers.party.renderer import render_party_card
from sib.keyboards.party import get_party_action_keyboard
from sib.matchmaking.matcher import MatchmakingService
from sib.services.bot_match import BotMatchService
from sib.services.leaderboard import LeaderboardService
from sib.services.maintenance import MaintenanceService
from sib.services.match_result import MatchResultService
from sib.services.panel import is_owner_scoped_callback, PanelOwnershipService
from sib.services.party import PartyService
from sib.tests.test_parties import create_test_group, create_test_user


async def _team_party(session, group, users, mode, game="connect_four"):
    party = await PartyService.create_party(session, group.id, users[0].id, game, mode)
    for user in users[1:]:
        party = await PartyService.join_party(session, party.id, user.id)
    return party


async def _expire(session, party_id):
    entry = await session.scalar(
        select(MatchmakingQueue).where(MatchmakingQueue.party_id == party_id)
    )
    entry.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await session.flush()
    return entry


@pytest.mark.asyncio
async def test_solo_real_opponent_creates_normal_pvp_match(db_session):
    group_a = await create_test_group(db_session, "گروه انفرادی الف")
    group_b = await create_test_group(db_session, "گروه انفرادی ب")
    user_a = await create_test_user(db_session, "بازیکن الف")
    user_b = await create_test_user(db_session, "بازیکن ب")
    party_a = await PartyService.create_party(db_session, group_a.id, user_a.id, "connect_four", "solo")
    party_b = await PartyService.create_party(db_session, group_b.id, user_b.id, "connect_four", "solo")

    await MatchmakingService.enqueue_party(db_session, party_a.id, user_a.id)
    await MatchmakingService.enqueue_party(db_session, party_b.id, user_b.id)
    match = await MatchmakingService.find_and_create_match(db_session, party_b.id)

    assert match is not None and not match.is_bot_match
    assert match.state == MatchState.WAITING_FOR_ACCEPTANCE
    assert len(match.players) == 2
    assert all(not p.user.is_bot for p in match.players)


@pytest.mark.asyncio
async def test_solo_timeout_creates_one_independent_bot_in_normal_match(db_session):
    group = await create_test_group(db_session, "گروه تنهای سیب")
    human = await create_test_user(db_session, "بازیکن تنها")
    party = await PartyService.create_party(db_session, group.id, human.id, "connect_four", "solo")
    queue = await MatchmakingService.enqueue_party(db_session, party.id, human.id)
    queue.message_id = 9123
    await _expire(db_session, party.id)
    created = []

    assert await MaintenanceService.process_expired_queue_entries(db_session, created) == 1
    assert len(created) == 1 and created[0]["queue_message_id"] == 9123
    match = await db_session.scalar(
        select(Match).where(Match.id == created[0]["match_id"])
    )

    assert match.is_bot_match and match.state == MatchState.ACTIVE
    assert len([p for p in match.players if not p.user.is_bot]) == 1
    assert len([p for p in match.players if p.user.is_bot]) == 1
    bot_player = next(p for p in match.players if p.user.is_bot)
    assert bot_player.user.display_name == "سیب کوچولو"
    assert bot_player.group_id is None and bot_player.bot_profile
    assert party.status == PartyStatus.IN_MATCH


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,size", [("2v2", 2), ("4v4", 4)])
async def test_full_team_parties_match_real_opponent(mode, size, db_session):
    group_a = await create_test_group(db_session, f"تیم واقعی الف {mode}")
    group_b = await create_test_group(db_session, f"تیم واقعی ب {mode}")
    users_a = [await create_test_user(db_session, f"الف {mode} {i}") for i in range(size)]
    users_b = [await create_test_user(db_session, f"ب {mode} {i}") for i in range(size)]
    party_a = await _team_party(db_session, group_a, users_a, mode)
    party_b = await _team_party(db_session, group_b, users_b, mode)

    await MatchmakingService.enqueue_party(db_session, party_a.id, users_a[0].id)
    await MatchmakingService.enqueue_party(db_session, party_b.id, users_b[0].id)
    match = await MatchmakingService.find_and_create_match(db_session, party_b.id)

    assert match is not None and not match.is_bot_match
    assert match.state == MatchState.WAITING_FOR_ACCEPTANCE
    assert sum(p.team == 1 and not p.user.is_bot for p in match.players) == size
    assert sum(p.team == 2 and not p.user.is_bot for p in match.players) == size


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,size", [("2v2", 2), ("4v4", 4)])
async def test_team_timeout_creates_exact_bot_roster_and_preserves_team_names(mode, size, db_session):
    group = await create_test_group(db_session, f"تمپلارهای {mode}")
    users = [await create_test_user(db_session, f"بازیکن {mode} {i}") for i in range(size)]
    party = await _team_party(db_session, group, users, mode)
    await MatchmakingService.enqueue_party(db_session, party.id, users[0].id)
    await _expire(db_session, party.id)
    created = []

    assert await MaintenanceService.process_expired_queue_entries(db_session, created) == 1
    match = await db_session.scalar(select(Match).where(Match.id == created[0]["match_id"]))
    humans = [p for p in match.players if not p.user.is_bot]
    bots = [p for p in match.players if p.user.is_bot]
    assert len(humans) == size and all(p.team == 1 for p in humans)
    assert len(bots) == size and all(p.team == 2 and p.group_id is None for p in bots)
    assert len({p.user_id for p in bots}) == size

    state = __import__("json").loads(match.game_state)
    assert state["team_names"] == {"1": group.title, "2": "🍎 تیم سیب کوچولو"}
    rendered = get_game_engine("connect_four").render_for_group(state, group.id, users[0].id)
    assert group.title in rendered and "تیم سیب کوچولو" in rendered
    for user in users:
        assert user.display_name in rendered
    for bot_player in bots:
        assert bot_player.user.display_name in rendered


@pytest.mark.asyncio
async def test_late_opponent_timeout_race_creates_only_one_match(db_session):
    group_a = await create_test_group(db_session, "صف نزدیک به پایان")
    group_b = await create_test_group(db_session, "حریف دیررس")
    user_a = await create_test_user(db_session, "در انتظار")
    user_b = await create_test_user(db_session, "دیر رسید")
    party_a = await PartyService.create_party(db_session, group_a.id, user_a.id, "connect_four", "solo")
    party_b = await PartyService.create_party(db_session, group_b.id, user_b.id, "connect_four", "solo")
    await MatchmakingService.enqueue_party(db_session, party_a.id, user_a.id)
    queue_a = await _expire(db_session, party_a.id)

    # A matcher that wakes at the deadline cannot use the expired row.
    assert await MatchmakingService.find_and_create_match(db_session, party_a.id) is None
    await MatchmakingService.enqueue_party(db_session, party_b.id, user_b.id)
    fallbacks = []
    assert await MaintenanceService.process_expired_queue_entries(db_session, fallbacks) == 1

    # The late human search can no longer claim the deactivated queue entry.
    assert await MatchmakingService.find_and_create_match(db_session, party_b.id) is None
    matches = list((await db_session.scalars(select(Match))).all())
    assert len(matches) == 1 and matches[0].is_bot_match
    assert queue_a.is_active is False


@pytest.mark.asyncio
async def test_bot_results_have_separate_stats_and_no_seedlings_or_group_rank(db_session):
    group = await create_test_group(db_session, "گروه تمرینی")
    human = await create_test_user(db_session, "بازیکن تمرینی")
    party = await PartyService.create_party(db_session, group.id, human.id, "connect_four", "solo")
    await MatchmakingService.enqueue_party(db_session, party.id, human.id)
    await _expire(db_session, party.id)
    fallbacks = []
    await MaintenanceService.process_expired_queue_entries(db_session, fallbacks)
    match = await db_session.scalar(select(Match).where(Match.id == fallbacks[0]["match_id"]))

    await MatchResultService.apply_match_result(
        db_session,
        match,
        GameResult(winner_team=1, is_draw=False, summary="تست", details={}),
    )
    await db_session.flush()
    stats = await db_session.get(PlayerStats, human.id)
    seedlings = await db_session.scalar(
        select(SeedlingInventory).where(SeedlingInventory.user_id == human.id)
    )
    group_stats = await db_session.get(GroupStats, group.id)
    public_stats = await LeaderboardService.get_player_stats(db_session, human.id)

    assert stats.matches_played == stats.wins == stats.losses == 0
    assert stats.bot_matches_played == 1 and stats.bot_wins == 1
    assert public_stats["bot_wins"] == 1 and public_stats["wins"] == 0
    assert seedlings is None or seedlings.total == 0
    assert group_stats is not None
    assert (group_stats.matches_played, group_stats.wins, group_stats.losses) == (0, 0, 0)


@pytest.mark.asyncio
async def test_pvp_and_bot_statistics_remain_logically_separate(db_session):
    user = await create_test_user(db_session, "آمار جدا")
    stats = PlayerStats(user_id=user.id, matches_played=2, wins=1, losses=1, bot_matches_played=3, bot_wins=2, bot_losses=1)
    db_session.add(stats)
    await db_session.flush()
    result = await LeaderboardService.get_player_stats(db_session, user.id)
    assert (result["matches_played"], result["wins"], result["losses"]) == (2, 1, 1)
    assert (result["bot_matches_played"], result["bot_wins"], result["bot_losses"]) == (3, 2, 1)


@pytest.mark.asyncio
async def test_solo_bot_turn_runs_through_independent_match_player(db_session):
    group = await create_test_group(db_session, "بازی با سیب")
    human = await create_test_user(db_session, "بازیکن سیب")
    party = await PartyService.create_party(db_session, group.id, human.id, "connect_four", "solo")
    await MatchmakingService.enqueue_party(db_session, party.id, human.id)
    await _expire(db_session, party.id)
    fallbacks = []
    await MaintenanceService.process_expired_queue_entries(db_session, fallbacks)
    match = await db_session.scalar(select(Match).where(Match.id == fallbacks[0]["match_id"]))
    engine = get_game_engine("connect_four")
    state = __import__("json").loads(match.game_state)

    state = engine.apply_action(state, human.id, {"col": 3})
    match.current_turn_player_id = state["current_player_id"]
    from sib.bots.runner import run_bot_turns
    state = await run_bot_turns(match, state)

    assert sum(cell is not None for row in state["board"] for cell in row) == 2
    assert state["current_player_id"] == human.id


@pytest.mark.asyncio
async def test_find_opponent_keyboard_removes_direct_bot_button_and_keeps_navigation(db_session):
    group = await create_test_group(db_session, "گروه دکمه‌ها")
    user = await create_test_user(db_session, "بازیکن دکمه‌ها")
    party = await PartyService.create_party(db_session, group.id, user.id, "connect_four", "solo")
    keyboard = get_party_action_keyboard(party, is_member=True)
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    labels = {button.text for row in keyboard.inline_keyboard for button in row}

    assert f"find_match:{party.id}" in callbacks
    assert not any(data and data.startswith("play_bot:") for data in callbacks)
    assert not any("بازی با Bot" in label for label in labels)
    assert any(data == "group_active_games" for data in callbacks)
    assert f"view_party:{party.id}" in callbacks  # Refresh
    assert is_owner_scoped_callback(f"find_match:{party.id}") is False
    assert is_owner_scoped_callback(f"cancel_matchmaking:{party.id}") is False


@pytest.mark.asyncio
async def test_existing_solo_bot_match_pipeline_still_starts_playable_board(db_session):
    group = await create_test_group(db_session, "گروه بازی")
    user = await create_test_user(db_session, "بازیکن بازی")
    party = await PartyService.create_party(db_session, group.id, user.id, "connect_four", "solo")
    await MatchmakingService.enqueue_party(db_session, party.id, user.id)
    await _expire(db_session, party.id)
    fallbacks = []
    await MaintenanceService.process_expired_queue_entries(db_session, fallbacks)
    match = await db_session.scalar(select(Match).where(Match.id == fallbacks[0]["match_id"]))
    state = __import__("json").loads(match.game_state)

    assert match.state == MatchState.ACTIVE
    assert state["board"] and state["current_player_id"] == user.id
    assert get_game_engine("connect_four").get_keyboard(state, match.id, user.id)
    assert render_party_card(party)


@pytest.mark.asyncio
async def test_queue_deadlines_are_mode_specific(db_session):
    group = await create_test_group(db_session, "گروه تایم‌اوت")
    solo_user = await create_test_user(db_session, "انفرادی")
    solo = await PartyService.create_party(db_session, group.id, solo_user.id, "connect_four", "solo")
    solo_queue = await MatchmakingService.enqueue_party(db_session, solo.id, solo_user.id)
    solo_seconds = (solo_queue.expires_at - solo_queue.entered_at).total_seconds()
    assert 14 <= solo_seconds <= 16

    team_group = await create_test_group(db_session, "گروه تیمی تایم‌اوت")
    team_users = [await create_test_user(db_session, f"تیمی {i}") for i in range(2)]
    team = await _team_party(db_session, team_group, team_users, "2v2")
    team_queue = await MatchmakingService.enqueue_party(db_session, team.id, team_users[0].id)
    team_seconds = (team_queue.expires_at - team_queue.entered_at).total_seconds()
    assert 19 <= team_seconds <= 21


@pytest.mark.asyncio
async def test_timeout_edits_queue_reference_and_releases_public_panel_owner(db_session, monkeypatch):
    group = await create_test_group(db_session, "گروه پنل صف")
    user = await create_test_user(db_session, "صاحب صف")
    party = await PartyService.create_party(db_session, group.id, user.id, "connect_four", "solo")
    queue = await MatchmakingService.enqueue_party(db_session, party.id, user.id)
    queue.message_id = 8811
    await PanelOwnershipService.claim(db_session, group.id, queue.message_id, user.id, "party")
    await _expire(db_session, party.id)
    fallbacks = []
    await MaintenanceService.process_expired_queue_entries(db_session, fallbacks)

    import sib.services.telegram_ui as telegram_ui
    edits = []

    async def fake_edit(_bot, chat_id, message_id, text, reply_markup=None, **_kwargs):
        edits.append((chat_id, message_id, text, reply_markup))
        return message_id + 100

    monkeypatch.setattr(telegram_ui, "safe_edit_screen", fake_edit)
    monkeypatch.setattr(telegram_ui, "safe_send_screen", AsyncMock())
    await MaintenanceService._present_bot_fallbacks(object(), fallbacks)

    assert len(edits) == 1
    chat_id, old_message_id, text, _keyboard = edits[0]
    assert (chat_id, old_message_id) == (group.id, 8811)
    assert "سیب کوچولو" in text and "چهار در یک خط" in text
    match = await db_session.scalar(select(Match).where(Match.id == fallbacks[0]["match_id"]))
    await db_session.refresh(match)
    assert match.group1_message_id == 8911
    assert await PanelOwnershipService.get_owner(db_session, group.id, 8811) is None


@pytest.mark.asyncio
async def test_apple_battle_solo_timeout_uses_a_bot_card_and_normal_match(db_session):
    group = await create_test_group(db_session, "گروه نبرد سیبی")
    human = await create_test_user(db_session, "جنگجوی منتظر")
    character = Character(
        code="bot_fallback_test_character",
        name_fa="سیب‌آور",
        release_status=ReleaseStatus.RELEASED,
        is_active=True,
    )
    db_session.add(character)
    await db_session.flush()
    card = PlayerCard(
        user_id=human.id,
        character_id=character.id,
        level=1,
        power=20,
        speed=20,
        intelligence=20,
    )
    db_session.add(card)
    await db_session.flush()
    db_session.add(
        AppleBattleQueueEntry(
            user_id=human.id,
            card_id=card.id,
            level=1,
            queued_at=datetime.now(timezone.utc),
        )
    )
    party = await PartyService.create_party(db_session, group.id, human.id, "apple_battle", "solo")
    await MatchmakingService.enqueue_party(db_session, party.id, human.id)
    await _expire(db_session, party.id)
    fallbacks = []

    assert await MaintenanceService.process_expired_queue_entries(db_session, fallbacks) == 1
    match = await db_session.scalar(select(Match).where(Match.id == fallbacks[0]["match_id"]))
    state = __import__("json").loads(match.game_state)
    bot = next(p for p in match.players if p.user.is_bot)

    assert match.is_bot_match and match.state == MatchState.ACTIVE
    assert bot.bot_profile and state["cards"][str(bot.user_id)]["card_id"]
    assert state["battle"]["winner_id"] in {human.id, bot.user_id}
