"""Friendly matches ("بازی با اعضای گروه") — a MATCH TYPE, not a game mode.

Covers: friendly party creation/join/team limits, 2v2 + 4v4 friendly matches,
an 8-player friendly 4v4 party, bot filling, starting (and never starting the
same party twice), the Apple reward rule with its daily caps, duplicate-reward
prevention, rematch, "never ranked" guarantees, and navigation.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat as TelegramChat,
    Message,
    User as TelegramUser,
)
from sqlalchemy import func, select

from sib.core.errors import (
    ActiveSessionExistsException,
    InvalidActionException,
    PartyFullException,
)
from sib.database.models import (
    AppleTransaction,
    Game,
    GroupStats,
    Match,
    MatchState,
    MatchType,
    PartyMember,
    PartyStatus,
    SeedlingAward,
    SeedlingInventory,
    User,
)
from sib.games.base import GameResult
from sib.handlers.friendly import (
    handle_create_friendly_party,
    handle_friendly_fill_bots,
    handle_friendly_start,
    render_friendly_board,
)
from sib.handlers.games.connect_four import handle_c4_action
from sib.handlers.party.handlers import (
    handle_join_party,
    handle_select_mode,
    handle_view_party,
)
from sib.handlers.party.renderer import render_party_card
from sib.keyboards.party import (
    get_friendly_party_keyboard,
    get_match_type_keyboard,
    get_party_action_keyboard,
    get_shared_friendly_party_keyboard,
)
from sib.matchmaking.matcher import MatchmakingService
from sib.services.apple_wallet import AppleWalletService
from sib.services.config_store import ConfigService
from sib.services.friendly import (
    FRIENDLY_REWARD_REASON,
    FriendlyMatchService,
    FriendlyRewardService,
    friendly_audiences,
    is_friendly,
    shape_friendly_final_text,
    viewer_audience,
)
from sib.services.match_result import MatchResultService
from sib.services.party import PartyService
from sib.services.seedling import SeedlingService
from sib.tests.test_parties import create_test_group, create_test_user


async def _friendly_party(session, group, users, mode, game="connect_four"):
    party = await PartyService.create_friendly_party(
        session, group.id, users[0].id, game, mode
    )
    for user in users[1:]:
        party = await PartyService.join_party(session, party.id, user.id)
    return party


async def _finish(session, match, winner_team=1, is_draw=False):
    result = GameResult(
        winner_team=None if is_draw else winner_team,
        is_draw=is_draw,
        summary="تست",
        details={},
    )
    await MatchResultService.apply_match_result(session, match, result)
    await session.commit()
    return await FriendlyMatchService.load_match(session, match.id)


async def _bot_members(session, party_id):
    rows = await session.execute(
        select(PartyMember, User)
        .join(User, User.id == PartyMember.user_id)
        .where(PartyMember.party_id == party_id, User.is_bot == True)  # noqa: E712
    )
    return rows.all()


# ---------------------------------------------------------------------------
# Party creation / join / team rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_friendly_solo_party(db_session):
    """A friendly solo party is a 1v1 party: 2 slots, creator on team 1."""
    user = await create_test_user(db_session, "میزبان")
    group = await create_test_group(db_session, "گروه رفیقانه")

    party = await PartyService.create_friendly_party(
        db_session, group.id, user.id, "tic_tac_toe", "solo"
    )
    await db_session.commit()

    assert party.match_type == MatchType.FRIENDLY
    assert party.is_friendly is True
    assert party.mode == "solo"
    assert party.max_players == 2
    assert party.team_size == 1
    assert party.status == PartyStatus.OPEN
    assert party.current_players_count == 1
    assert [m.team for m in party.members] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "game,mode,team_size,human_count",
    [
        ("connect_four", "2v2", 2, 4),
        ("connect_four", "4v4", 4, 8),
        ("battleship", "4v4", 4, 8),
    ],
)
async def test_friendly_team_party_supports_full_roster(
    game, mode, team_size, human_count, db_session
):
    """Friendly 2v2 has 4 slots and friendly 4v4 has 8 slots (4 per team)."""
    group = await create_test_group(db_session, f"گروه {game} {mode}")
    users = [await create_test_user(db_session, f"رفیق {mode} {i}") for i in range(human_count)]

    party = await _friendly_party(db_session, group, users, mode, game=game)
    await db_session.commit()

    assert party.match_type == MatchType.FRIENDLY
    assert party.max_players == 2 * team_size == human_count
    assert party.current_players_count == human_count
    assert party.status == PartyStatus.FULL
    assert party.is_full is True

    counts = await PartyService._team_counts(db_session, party.id)
    assert counts[1] == team_size
    assert counts[2] == team_size


@pytest.mark.asyncio
async def test_friendly_respects_each_game_supported_modes(db_session):
    """Hard game/mode compatibility applies to friendly parties too."""
    group = await create_test_group(db_session, "گروه سازگاری حالت")
    user = await create_test_user(db_session, "سازگاری")

    # high_card is solo-only: a friendly 4v4 must be rejected.
    with pytest.raises(InvalidActionException):
        await PartyService.create_friendly_party(
            db_session, group.id, user.id, "high_card", "4v4"
        )

    # ...while a friendly solo party works and has exactly two slots.
    party = await PartyService.create_friendly_party(
        db_session, group.id, user.id, "high_card", "solo"
    )
    await db_session.commit()
    assert party.max_players == 2 and party.is_friendly


@pytest.mark.asyncio
async def test_friendly_join_team_choice_and_auto_balance(db_session):
    """Team slots can be chosen explicitly; auto-joining balances the teams."""
    group = await create_test_group(db_session, "گروه تیم‌بندی")
    host = await create_test_user(db_session, "میزبان تیم")
    chosen = await create_test_user(db_session, "انتخاب‌کننده")
    balanced = await create_test_user(db_session, "متعادل")

    party = await PartyService.create_friendly_party(
        db_session, group.id, host.id, "connect_four", "2v2"
    )
    party = await PartyService.join_party(db_session, party.id, chosen.id, team=2)
    await db_session.commit()
    assert [m.team for m in party.members if m.user_id == chosen.id] == [2]

    # Auto balancing prefers the emptier team (team 1: 1 member, team 2: 1 member)
    party = await PartyService.join_party(db_session, party.id, balanced.id)
    await db_session.commit()
    assert [m.team for m in party.members if m.user_id == balanced.id] == [1]
    counts = await PartyService._team_counts(db_session, party.id)
    assert counts == {1: 2, 2: 1} and party.status == PartyStatus.OPEN


@pytest.mark.asyncio
async def test_friendly_team_slot_limit_and_duplicate_join(db_session):
    """Overfilling one team (and joining twice) is rejected."""
    group = await create_test_group(db_session, "گروه سقف تیم")
    u1 = await create_test_user(db_session, "one")
    u2 = await create_test_user(db_session, "two")
    u3 = await create_test_user(db_session, "three")

    party = await PartyService.create_friendly_party(
        db_session, group.id, u1.id, "connect_four", "2v2"
    )
    party = await PartyService.join_party(db_session, party.id, u2.id, team=1)
    await db_session.commit()
    assert party.status == PartyStatus.OPEN  # 2v2 = 4 slots; only team 1 is full
    assert (await PartyService._team_counts(db_session, party.id))[1] == 2

    # team 1 is full (u1, u2) - u3 cannot squeeze in, even explicitly
    with pytest.raises(PartyFullException):
        await PartyService.join_party(db_session, party.id, u3.id, team=1)

    # Joining twice is rejected by the one-active-session rule (u2's own
    # active party is this very party).
    with pytest.raises(ActiveSessionExistsException):
        await PartyService.join_party(db_session, party.id, u2.id)

    with pytest.raises(InvalidActionException):
        await PartyService.join_party(db_session, party.id, u3.id, team=7)


@pytest.mark.asyncio
async def test_friendly_solo_party_join_blocks_second_team_one_player(db_session):
    """1v1 friendly: exactly one player per side; the second joiner is team 2."""
    group = await create_test_group(db_session, "گروه تک‌نفره رفیقانه")
    host = await create_test_user(db_session, "میزبان تک")
    guest = await create_test_user(db_session, "مهمان تک")

    party = await PartyService.create_friendly_party(
        db_session, group.id, host.id, "rock_paper_scissors", "solo"
    )
    party = await PartyService.join_party(db_session, party.id, guest.id)
    await db_session.commit()
    assert party.status == PartyStatus.FULL
    teams = {m.user_id: m.team for m in party.members}
    assert teams == {host.id: 1, guest.id: 2}


@pytest.mark.asyncio
async def test_enable_friendly_reuses_the_solo_party(db_session):
    """The solo party card action converts the SAME party into a friendly one."""
    group = await create_test_group(db_session, "گروه تبدیل")
    host = await create_test_user(db_session, "میزبان تبدیل")
    guest = await create_test_user(db_session, "مهمان تبدیل")

    party = await PartyService.create_party(
        db_session, group.id, host.id, "tic_tac_toe", "solo"
    )
    await db_session.commit()
    assert party.status == PartyStatus.FULL and party.max_players == 1
    original_party_id = party.id

    party = await PartyService.enable_friendly(db_session, party.id, host.id)
    await db_session.commit()
    assert party.match_type == MatchType.FRIENDLY
    assert party.max_players == 2 and party.status == PartyStatus.OPEN
    # The SAME row is reused, so the one-active-session rule stays intact.
    assert party.id == original_party_id

    party = await PartyService.join_party(db_session, party.id, guest.id)
    await db_session.commit()
    assert party.is_full


# ---------------------------------------------------------------------------
# Bot filling (party/team system, not per game)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,humans,bots", [("2v2", 2, 2), ("4v4", 2, 6)])
async def test_fill_empty_slots_with_bots(mode, humans, bots, db_session):
    """The host fills every free team slot with clearly-marked bots."""
    group = await create_test_group(db_session, f"گروه بات {mode}")
    users = [await create_test_user(db_session, f"بات‌باز {mode} {i}") for i in range(humans)]

    party = await _friendly_party(db_session, group, users, mode)
    await db_session.commit()
    assert not party.is_full

    party = await PartyService.fill_empty_slots_with_bots(db_session, party.id, users[0].id)
    await db_session.commit()

    assert party.is_full and party.status == PartyStatus.FULL
    assert party.current_players_count == humans + bots
    bot_members = await _bot_members(db_session, party.id)
    assert len(bot_members) == bots
    for member, user in bot_members:
        assert user.is_bot is True
        assert user.first_name.startswith("🤖")
        assert member.bot_profile
        assert member.team in (1, 2)

    counts = await PartyService._team_counts(db_session, party.id)
    assert counts[1] == party.team_size and counts[2] == party.team_size

    # Idempotency: a second press cannot duplicate bots.
    with pytest.raises(InvalidActionException):
        await PartyService.fill_empty_slots_with_bots(db_session, party.id, users[0].id)


@pytest.mark.asyncio
async def test_bot_fill_is_host_only_and_friendly_only(db_session):
    """Non-hosts cannot fill, and normal (ranked) parties never get bots."""
    group = await create_test_group(db_session, "گروه قوانین بات")
    host = await create_test_user(db_session, "میزبان بات")
    other = await create_test_user(db_session, "غیرمیزبان")
    ranked = await create_test_user(db_session, "رقابتی")  # free of any party

    friendly = await PartyService.create_friendly_party(
        db_session, group.id, host.id, "connect_four", "2v2"
    )
    friendly = await PartyService.join_party(db_session, friendly.id, other.id)
    await db_session.commit()

    with pytest.raises(InvalidActionException):
        await PartyService.fill_empty_slots_with_bots(db_session, friendly.id, other.id)

    normal = await PartyService.create_party(
        db_session, group.id, ranked.id, "connect_four", "2v2"
    )
    await db_session.commit()
    with pytest.raises(InvalidActionException):
        await PartyService.fill_empty_slots_with_bots(db_session, normal.id, ranked.id)
    assert (await PartyService._team_counts(db_session, normal.id))[1] == 1


# ---------------------------------------------------------------------------
# Starting the friendly match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_friendly_match_two_teams_and_no_double_start(db_session):
    """A full friendly party starts ONE ACTIVE friendly match (never twice)."""
    group = await create_test_group(db_session, "گروه شروع")
    users = [await create_test_user(db_session, f"شروع {i}") for i in range(4)]
    party = await _friendly_party(db_session, group, users, "2v2")
    await db_session.commit()

    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()

    assert match.match_type == MatchType.FRIENDLY
    assert match.state == MatchState.ACTIVE
    assert match.mode == "2v2"
    assert match.is_bot_match is False
    assert match.group1_id == match.group2_id == group.id
    assert match.group1_accepted and match.group2_accepted
    assert sorted(p.team for p in match.players) == [1, 1, 2, 2]
    assert all(not p.user.is_bot for p in match.players)
    assert match.game_state and match.turn_deadline is not None
    assert party.status == PartyStatus.IN_MATCH

    # Second start (double tap / another member) must not create a second match.
    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.start_friendly_match(db_session, party.id, users[1].id)

    count = await db_session.scalar(
        select(func.count(Match.id)).where(Match.party1_id == party.id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_start_requires_a_full_party(db_session):
    """A friendly party with free slots cannot start (fill them first)."""
    group = await create_test_group(db_session, "گروه ناقص")
    users = [await create_test_user(db_session, f"ناقص {i}") for i in range(2)]
    party = await _friendly_party(db_session, group, users, "4v4")
    await db_session.commit()

    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)

    # non-members can never start someone else's friendly party
    outsider = await create_test_user(db_session, "غریبه")
    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.start_friendly_match(db_session, party.id, outsider.id)


@pytest.mark.asyncio
async def test_eight_player_friendly_4v4_match(db_session):
    """A friendly 4v4 party takes 8 humans and starts with 4v4 players."""
    group = await create_test_group(db_session, "گروه هشت‌نفره")
    users = [await create_test_user(db_session, f"هشت {i}") for i in range(8)]
    party = await _friendly_party(db_session, group, users, "4v4")
    await db_session.commit()

    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[3].id)
    await db_session.commit()

    assert len(match.players) == 8
    assert sorted({p.user_id for p in match.players}) == sorted(u.id for u in users)
    counts = {team: sum(1 for p in match.players if p.team == team) for team in (1, 2)}
    assert counts == {1: 4, 2: 4}
    assert match.is_bot_match is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,humans,bots", [("2v2", 2, 2), ("4v4", 2, 6)])
async def test_friendly_match_with_bots_starts_and_keeps_teams(mode, humans, bots, db_session):
    """Bot-filled friendly matches start and reuse the real bot abstraction."""
    group = await create_test_group(db_session, f"گروه بات‌شروع {mode}")
    users = [await create_test_user(db_session, f"شروع‌بات {mode} {i}") for i in range(humans)]
    party = await _friendly_party(db_session, group, users, mode)
    party = await PartyService.fill_empty_slots_with_bots(db_session, party.id, users[0].id)
    await db_session.commit()

    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()

    assert match.is_bot_match is True
    assert len(match.players) == humans + bots
    bot_players = [p for p in match.players if p.user.is_bot]
    assert len(bot_players) == bots
    assert all(p.group_id is None and p.bot_profile for p in bot_players)
    human_players = [p for p in match.players if not p.user.is_bot]
    assert sorted(p.team for p in human_players) == [1, 2]
    assert all(isinstance(p.turn_order, int) and p.turn_order >= 1 for p in match.players)
    assert match.game_state


@pytest.mark.asyncio
async def test_friendly_party_can_never_enter_the_ranked_queue(db_session):
    """Regression: a friendly party must never become a ranked match."""
    group_a = await create_test_group(db_session, "گروه رفیقانه صف A")
    group_b = await create_test_group(db_session, "گروه رفیقانه صف B")
    users_a = [await create_test_user(db_session, f"رفیق A {i}") for i in range(2)]
    users_b = [await create_test_user(db_session, f"بازیکن B {i}") for i in range(2)]

    friendly = await _friendly_party(db_session, group_a, users_a, "2v2")
    normal = await PartyService.create_party(
        db_session, group_b.id, users_b[0].id, "battleship", "2v2"
    )
    normal = await PartyService.join_party(db_session, normal.id, users_b[1].id)
    await db_session.commit()
    assert normal.match_type == MatchType.NORMAL

    with pytest.raises(InvalidActionException):
        await MatchmakingService.enqueue_party(db_session, friendly.id, users_a[0].id)
    await db_session.commit()
    assert friendly.status == PartyStatus.OPEN  # never queued, never ranked

    # Real matchmaking keeps working exactly as before.
    await MatchmakingService.enqueue_party(db_session, normal.id, users_b[0].id)
    assert normal.status == PartyStatus.IN_QUEUE


# ---------------------------------------------------------------------------
# Rewards: apples, never rank/seedlings, daily caps, duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,humans", [("solo", 2), ("2v2", 4), ("4v4", 8)])
async def test_friendly_result_pays_apples_and_never_rank(mode, humans, db_session):
    """Friendly results: +10 apples per human, no group wins, no seedlings."""
    group = await create_test_group(db_session, f"گروه پاداش {mode}")
    users = [await create_test_user(db_session, f"پاداش {mode} {i}") for i in range(humans)]
    party = await _friendly_party(db_session, group, users, mode)
    await db_session.commit()
    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()

    # An unfinished match never pays.
    assert await AppleWalletService.get_balance(db_session, users[0].id) == 0

    match = await _finish(db_session, match, winner_team=2)
    await db_session.commit()

    assert match.state == MatchState.FINISHED
    assert match.result_applied is True
    assert match.winner_team == 2
    assert party.status == PartyStatus.CANCELLED

    for user in users:
        assert await AppleWalletService.get_balance(db_session, user.id) == 10
        assert await SeedlingService.get_total(db_session, user.id) == 0

    stats = await db_session.scalar(
        select(GroupStats).where(GroupStats.group_id == group.id)
    )
    assert stats is None or (stats.wins == 0 and stats.matches_played == 0 and stats.losses == 0)
    assert await db_session.scalar(select(func.count(SeedlingAward.id))) == 0
    assert await db_session.scalar(select(func.count(SeedlingInventory.user_id))) == 0
    assert await db_session.scalar(select(func.count(AppleTransaction.id))) == humans


@pytest.mark.asyncio
async def test_friendly_reward_daily_caps_and_duplicate_prevention(db_session):
    """≤N matches/day, ≤cap apples/day, and never the same match twice."""
    await ConfigService.set(db_session, "friendly_reward_max_matches_per_day", 3)
    await ConfigService.set(db_session, "friendly_reward_apples_per_match", 20)
    await ConfigService.set(db_session, "friendly_reward_daily_apple_cap", 50)
    group = await create_test_group(db_session, "گروه سقف پاداش")
    users = [await create_test_user(db_session, f"سقف {i}") for i in range(2)]

    balances = []
    for index in range(4):
        party = await _friendly_party(db_session, group, users, "solo")
        await db_session.commit()
        match = await FriendlyMatchService.start_friendly_match(
            db_session, party.id, users[0].id
        )
        await db_session.commit()
        match = await _finish(db_session, match, winner_team=1)
        await db_session.commit()
        balances.append(await AppleWalletService.get_balance(db_session, users[0].id))

    # 20 + 20 + 10 (daily cap 50) and NO payout for the 4th match (limit 3).
    assert balances == [20, 40, 50, 50]

    # Re-applying the SAME match result (callback retry / double finish) is a
    # no-op: the idempotency key and result_applied guard both hold.
    match = await FriendlyMatchService.load_match(db_session, match.id)
    paid = await FriendlyRewardService.award_match_rewards(db_session, match)
    await db_session.commit()
    assert paid == {}
    assert await AppleWalletService.get_balance(db_session, users[0].id) == 50

    ledger_rows = await db_session.scalar(
        select(func.count(AppleTransaction.id)).where(
            AppleTransaction.user_id == users[0].id,
            AppleTransaction.reason == FRIENDLY_REWARD_REASON,
        )
    )
    assert ledger_rows == 3


@pytest.mark.asyncio
async def test_reward_config_is_used_for_a_fresh_player(db_session):
    """A player who has not hit the cap still earns the configured amount."""
    await ConfigService.set(db_session, "friendly_reward_apples_per_match", 7)
    assert await FriendlyRewardService.reward_for_player(db_session, 424242) == 7


# ---------------------------------------------------------------------------
# Rematch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rematch_recreates_friendly_match_and_fills_with_bots(db_session):
    """Rematch = same game/mode, still friendly, bots fill departing players."""
    group = await create_test_group(db_session, "گروه بازی مجدد")
    users = [await create_test_user(db_session, f"مجدد {i}") for i in range(2)]
    party = await _friendly_party(db_session, group, users, "2v2")
    party = await PartyService.fill_empty_slots_with_bots(
        db_session, party.id, users[0].id
    )
    await db_session.commit()
    first = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()
    first = await _finish(db_session, first, winner_team=1)
    await db_session.commit()
    assert len(first.players) == 4  # 2 humans + 2 bots

    # Both humans are free again, but the rematch still owns every slot of the
    # same mode: the two free slots are filled with bots, exactly like the
    # first match, and the new match is ALWAYS friendly.
    second = await FriendlyMatchService.rematch(db_session, first.id, users[1].id)
    await db_session.commit()

    assert second.id != first.id
    assert is_friendly(second)
    assert second.state == MatchState.ACTIVE
    assert second.mode == first.mode and second.game_id == first.game_id
    assert second.is_bot_match is True  # the two empty 2v2 slots became bots
    assert len(second.players) == 4
    humans = [p for p in second.players if not p.user.is_bot]
    assert sorted(p.user_id for p in humans) == sorted(u.id for u in users)
    assert len([p for p in second.players if p.user.is_bot]) == 2

    # Rematching a friendly match never touches group rank.
    stats = await db_session.scalar(
        select(GroupStats).where(GroupStats.group_id == group.id)
    )
    assert stats is None or stats.wins == 0


@pytest.mark.asyncio
async def test_rematch_guards(db_session):
    """Rematch is friendly-only, participant-only and finished-only."""
    group = await create_test_group(db_session, "گروه گارد مجدد")
    users = [await create_test_user(db_session, f"گارد {i}") for i in range(2)]
    outsider = await create_test_user(db_session, "غریبهٔ مجدد")

    party = await _friendly_party(db_session, group, users, "solo")
    await db_session.commit()
    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()

    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.rematch(db_session, match.id, users[0].id)

    match = await _finish(db_session, match, winner_team=1)
    await db_session.commit()

    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.rematch(db_session, match.id, outsider.id)

    # A busy player cannot be pulled into a rematch.
    other_party = await PartyService.create_friendly_party(
        db_session,
        (await create_test_group(db_session, "گروه اشغال")).id,
        users[0].id,
        "tic_tac_toe",
        "solo",
    )
    await db_session.commit()
    with pytest.raises(ActiveSessionExistsException):
        await FriendlyMatchService.rematch(db_session, match.id, users[0].id)

    # A normal (ranked) match never offers a rematch.
    game = await db_session.scalar(select(Game).where(Game.code == "tic_tac_toe"))
    fake = Match(
        game_id=game.id,
        mode="solo",
        match_type=MatchType.NORMAL,
        state=MatchState.FINISHED,
        group1_id=group.id,
        group2_id=group.id,
    )
    db_session.add(fake)
    await db_session.commit()
    with pytest.raises(InvalidActionException):
        await FriendlyMatchService.rematch(db_session, fake.id, users[0].id)


@pytest.mark.asyncio
async def test_rematch_respects_daily_reward_limit(db_session):
    """Rematch never bypasses the daily reward rule."""
    await ConfigService.set(db_session, "friendly_reward_max_matches_per_day", 2)
    group = await create_test_group(db_session, "گروه مجدد سقف")
    users = [await create_test_user(db_session, "مجدد سقف ۱"), await create_test_user(db_session, "مجدد سقف ۲")]

    party = await _friendly_party(db_session, group, users, "solo")
    await db_session.commit()
    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()
    match = await _finish(db_session, match, winner_team=1)
    await db_session.commit()

    # 2nd eligible match through rematch: still paid.
    match = await FriendlyMatchService.rematch(db_session, match.id, users[0].id)
    await db_session.commit()
    match = await _finish(db_session, match, winner_team=2)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, users[0].id) == 20

    # 3rd: the daily match limit is reached - the rematch still starts but pays 0.
    match = await FriendlyMatchService.rematch(db_session, match.id, users[0].id)
    await db_session.commit()
    match = await _finish(db_session, match, winner_team=1)
    await db_session.commit()
    assert await AppleWalletService.get_balance(db_session, users[0].id) == 20


# ---------------------------------------------------------------------------
# Rendering helpers (single shared board in one group)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_friendly_audiences_are_distinct_per_team(db_session):
    """Both friendly teams share the group, so renders use per-team audiences."""
    group = await create_test_group(db_session, "گروه تماشا")
    users = [await create_test_user(db_session, f"تماشا {i}") for i in range(2)]
    party = await _friendly_party(db_session, group, users, "solo", game="battleship")
    await db_session.commit()
    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()

    import json

    state = json.loads(match.game_state)
    audiences = friendly_audiences(group.id)
    assert audiences[1] != audiences[2]
    assert audiences[2] > 0

    team1 = next(p for p in match.players if p.team == 1)
    team2 = next(p for p in match.players if p.team == 2)
    assert viewer_audience(match, state, team1.user_id) == audiences[1]
    assert viewer_audience(match, state, team2.user_id) == audiences[2]


def test_shape_friendly_final_text_names_the_winner():
    """The shared board never tells both teams "you won"."""
    class _Match:
        is_draw = False
        winner_team = 2

    text = "🚢 **پایان**\n\nboard\n\n💔 **گروه حریف پیروز شد!** 🍏\n\n📝 summary"
    shaped = shape_friendly_final_text(text, _Match(), {"team_names": {"2": "🍏 تیم سبز"}})
    assert "گروه حریف" not in shaped
    assert "🍏 تیم سبز" in shaped


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_friendly_party_card_and_keyboard_navigation(db_session):
    """Friendly party screen: join/start/cancel/refresh/back (back never cancels)."""
    group = await create_test_group(db_session, "گروه ناوبری")
    host = await create_test_user(db_session, "ناوبر رفیق")
    guest = await create_test_user(db_session, "ناوبر مهمان")

    party = await PartyService.create_friendly_party(
        db_session, group.id, host.id, "connect_four", "solo"
    )
    await db_session.commit()

    text = render_party_card(party)
    assert "رفیقانه" in text

    host_kb = get_friendly_party_keyboard(party, is_member=True, is_host=True)
    host_cbs = [b.callback_data for row in host_kb.inline_keyboard for b in row]
    assert f"friendly_fill_bots:{party.id}" in host_cbs
    assert f"cancel_party:{party.id}" in host_cbs
    assert f"view_party:{party.id}" in host_cbs
    assert "group_active_games" in host_cbs
    assert f"friendly_start:{party.id}" not in host_cbs  # not full yet

    guest_view = get_friendly_party_keyboard(party, is_member=False)
    guest_cbs = [b.callback_data for row in guest_view.inline_keyboard for b in row]
    assert f"join_party:{party.id}:1" in guest_cbs
    assert f"join_party:{party.id}:2" in guest_cbs
    assert f"friendly_start:{party.id}" not in guest_cbs
    # A non-member never sees the (host-only) bot filling action.
    assert f"friendly_fill_bots:{party.id}" not in guest_cbs

    # After the guest joins the solo friendly party is FULL; any member may
    # start and the (host-only) bot filling action is gone.
    party = await PartyService.join_party(db_session, party.id, guest.id)
    await db_session.commit()
    member_kb = get_friendly_party_keyboard(party, is_member=True)
    member_cbs = [b.callback_data for row in member_kb.inline_keyboard for b in row]
    assert f"friendly_start:{party.id}" in member_cbs
    assert f"friendly_fill_bots:{party.id}" not in member_cbs
    # A non-member can never start someone else's full friendly party.
    outsider_kb = get_friendly_party_keyboard(party, is_member=False)
    outsider_cbs = [b.callback_data for row in outsider_kb.inline_keyboard for b in row]
    assert f"friendly_start:{party.id}" not in outsider_cbs
    assert f"leave_party:{party.id}" not in outsider_cbs
    # A non-host member sees no bot-filling action either.
    guest_member_kb = get_friendly_party_keyboard(party, is_member=True, is_host=False)
    guest_member_cbs = [
        b.callback_data for row in guest_member_kb.inline_keyboard for b in row
    ]
    assert f"friendly_fill_bots:{party.id}" not in guest_member_cbs

    # Back on a full friendly party must never cancel it.
    back_cbs = [
        b.callback_data
        for row in member_kb.inline_keyboard
        for b in row
        if b.callback_data == "group_active_games"
    ]
    assert back_cbs == ["group_active_games"]
    assert party.status == PartyStatus.FULL


def test_match_type_keyboard_keeps_normal_flow_and_adds_friendly():
    """Game -> Mode -> Match Type: both actions are offered for a mode."""
    from types import SimpleNamespace

    game = SimpleNamespace(code="battleship", name_fa="کشتی جنگی", supports_solo=True,
                           supports_2v2=True, supports_4v4=True)
    kb = get_match_type_keyboard(game, "4v4", back_to="group_games_panel")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "create_party:battleship:4v4" in cbs
    assert "create_friendly_party:battleship:4v4" in cbs
    # Back returns to the mode list of the same game (never to a cancel path).
    assert "select_game:battleship:group_games_panel" in cbs


@pytest.mark.asyncio
async def test_solo_party_card_offers_both_match_types(db_session):
    """Solo menu: Find Opponent + Play With Group Members + Cancel/Refresh/Back."""
    group = await create_test_group(db_session, "گروه منوی انفرادی")
    host = await create_test_user(db_session, "منوی انفرادی")

    party = await PartyService.create_party(
        db_session, group.id, host.id, "high_card", "solo"
    )
    await db_session.commit()

    kb = get_party_action_keyboard(party, is_member=True)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"find_match:{party.id}" in cbs
    assert f"make_friendly:{party.id}" in cbs
    assert f"cancel_party:{party.id}" in cbs
    assert f"view_party:{party.id}" in cbs
    assert "group_active_games" in cbs

    # The conversion action itself is safe and idempotent-guarded.
    party = await PartyService.enable_friendly(db_session, party.id, host.id)
    await db_session.commit()
    assert party.match_type == MatchType.FRIENDLY
    with pytest.raises(InvalidActionException):
        await PartyService.enable_friendly(db_session, party.id, host.id)


# ---------------------------------------------------------------------------
# Handlers + shared-board presentation (Game -> Mode -> Match Type -> board)
# ---------------------------------------------------------------------------


def _callback(data: str, user_id: int, group_id: int, message_id: int = 700):
    """Mock the callback query exactly like the existing handler tests do."""
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="بازیکن")
    cb.message = MagicMock(spec=Message)
    cb.message.chat = TelegramChat(id=group_id, type="supergroup", title="گروه")
    cb.message.message_id = message_id
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_select_mode_screen_offers_both_match_types(db_session):
    """The mode screen keeps «پیدا کردن حریف» and adds «بازی با اعضای گروه»."""
    group = await create_test_group(db_session, "گروه صفحهٔ نوع مسابقه")
    user = await create_test_user(db_session, "بازدیدکننده")

    mock_bot = AsyncMock()
    callback = _callback(
        "select_mode:connect_four:2v2:group_games_panel", user.id, group.id
    )
    await handle_select_mode(callback, mock_bot)

    assert mock_bot.edit_message_text.called
    kb = mock_bot.edit_message_text.call_args[1]["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "create_party:connect_four:2v2" in cbs
    assert "create_friendly_party:connect_four:2v2" in cbs
    assert "select_game:connect_four:group_games_panel" in cbs


@pytest.mark.asyncio
async def test_friendly_ui_callbacks_create_fill_and_start(db_session):
    """create_friendly_party -> fill bots -> start, through the real handlers."""
    group = await create_test_group(db_session, "گروه مسیر رفیقانه")
    host = await create_test_user(db_session, "میزبان مسیر")
    guest = await create_test_user(db_session, "مهمان مسیر")
    group_id, host_id, guest_id = group.id, host.id, guest.id  # survive rollbacks
    mock_bot = AsyncMock()

    # 1) create (2v2 = 4 slots, both teams inside the group)
    callback = _callback("create_friendly_party:connect_four:2v2", host_id, group_id)
    await handle_create_friendly_party(callback, mock_bot)
    text = mock_bot.edit_message_text.call_args[0][0]
    assert "رفیقانه" in text

    await db_session.rollback()
    active = await PartyService.get_active_parties_for_group(db_session, group_id)
    assert len(active) == 1
    party_id = active[0].id
    assert active[0].is_friendly and active[0].max_players == 4

    # 2) a second group member joins a SPECIFIC team (join_party:{id}:{team})
    callback = _callback(f"join_party:{party_id}:2", guest_id, group_id)
    await handle_join_party(callback, mock_bot)

    await db_session.rollback()
    joined = await PartyService.get_party_by_id(db_session, party_id)
    assert [m.team for m in joined.members if m.user_id == guest_id] == [2]

    # 3) the host fills the remaining free slots with bots
    callback = _callback(f"friendly_fill_bots:{party_id}", host_id, group_id)
    await handle_friendly_fill_bots(callback, mock_bot)

    await db_session.rollback()
    refreshed = await PartyService.get_party_by_id(db_session, party_id)
    assert refreshed.is_full and refreshed.current_players_count == 4
    assert len(await _bot_members(db_session, party_id)) == 2

    # 4) start: the party card message becomes the ONE shared board
    callback = _callback(f"friendly_start:{party_id}", host_id, group_id, message_id=701)
    await handle_friendly_start(callback, mock_bot)

    await db_session.rollback()
    matches = list(
        (await db_session.scalars(select(Match).where(Match.party1_id == party_id))).all()
    )
    assert len(matches) == 1
    match = await FriendlyMatchService.load_match(db_session, matches[0].id)
    assert match.state == MatchState.ACTIVE
    assert match.match_type == MatchType.FRIENDLY
    assert match.group1_id == group_id and match.group2_id == group_id
    assert match.group1_message_id == 701 and match.group2_message_id == 701

    final_kb = mock_bot.edit_message_text.call_args[1]["reply_markup"]
    cbs = [b.callback_data for row in final_kb.inline_keyboard for b in row]
    assert f"game_refresh:{match.id}" in cbs


def _keyboard_callbacks(kb) -> list:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_friendly_party_initial_card_offers_team_joins_immediately(db_session):
    """REGRESSION: the FIRST party message already carries the join buttons.

    A friendly party is ONE shared group message, so the card every other
    member sees is the one rendered when the creator creates the party. That
    render used to be tailored to the host, which left every other member
    without the Team 🍎/🍏 join buttons until somebody pressed Refresh.
    """
    group = await create_test_group(db_session, "گروه نمای اولیه")
    host = await create_test_user(db_session, "میزبان نمای اولیه")
    guest = await create_test_user(db_session, "مهمان نمای اولیه")
    group_id, host_id, guest_id = group.id, host.id, guest.id
    mock_bot = AsyncMock()

    # 1) the creator creates the friendly party (2v2 -> both teams open)
    callback = _callback("create_friendly_party:connect_four:2v2", host_id, group_id)
    await handle_create_friendly_party(callback, mock_bot)

    await db_session.rollback()
    party_id = (await PartyService.get_active_parties_for_group(db_session, group_id))[0].id

    initial_kb = mock_bot.edit_message_text.call_args[1]["reply_markup"]
    initial_cbs = _keyboard_callbacks(initial_kb)

    # 2+3) another eligible group member sees BOTH join buttons right there
    assert f"join_party:{party_id}:1" in initial_cbs
    assert f"join_party:{party_id}:2" in initial_cbs
    # 4) and no Refresh was required: the create render is the only render.
    assert mock_bot.edit_message_text.call_count == 1

    # The creator keeps their normal party controls on the same card.
    assert f"friendly_fill_bots:{party_id}" in initial_cbs
    assert f"cancel_party:{party_id}" in initial_cbs
    assert f"view_party:{party_id}" in initial_cbs
    assert "group_active_games" in initial_cbs

    # ...and a member's own view of the party (Active Games / Refresh) keeps
    # the join buttons - the shared card does not flip per viewer.
    refresh_bot = AsyncMock()
    refresh_callback = _callback(
        f"view_party:{party_id}", guest_id, group_id, message_id=701
    )
    await handle_view_party(refresh_callback, refresh_bot)
    refreshed_cbs = _keyboard_callbacks(
        refresh_bot.edit_message_text.call_args[1]["reply_markup"]
    )
    assert f"join_party:{party_id}:1" in refreshed_cbs
    assert f"join_party:{party_id}:2" in refreshed_cbs

    # Joining a team re-renders the same shared card: the still-open teams
    # remain joinable for the next member (again without any Refresh).
    join_bot = AsyncMock()
    join_callback = _callback(f"join_party:{party_id}:2", guest_id, group_id)
    await handle_join_party(join_callback, join_bot)
    joined_cbs = _keyboard_callbacks(
        join_bot.edit_message_text.call_args[1]["reply_markup"]
    )
    assert f"join_party:{party_id}:1" in joined_cbs  # team 🍎 is still open
    assert f"join_party:{party_id}:2" in joined_cbs  # team 🍏 has one free slot


@pytest.mark.asyncio
async def test_friendly_party_card_hides_join_button_of_a_full_team(db_session):
    """A full team's Join button disappears; a full party offers Start."""
    group = await create_test_group(db_session, "گروه تیم پر")
    host = await create_test_user(db_session, "میزبان تیم پر")
    mate = await create_test_user(db_session, "هم‌تیمی تیم پر")
    guest = await create_test_user(db_session, "مهمان تیم پر")
    group_id, host_id, guest_id = group.id, host.id, guest.id

    party = await PartyService.create_friendly_party(
        db_session, group_id, host_id, "connect_four", "2v2"
    )
    party = await PartyService.join_party(db_session, party.id, mate.id, team=1)
    party_id = party.id
    await db_session.commit()

    # Team 🍎 (team 1) is full, team 🍏 (team 2) is still open.
    kb = get_shared_friendly_party_keyboard(party)
    cbs = _keyboard_callbacks(kb)
    assert f"join_party:{party_id}:1" not in cbs
    assert f"join_party:{party_id}:2" in cbs

    # The same rule applies to the render another member gets from the card.
    mock_bot = AsyncMock()
    callback = _callback(f"view_party:{party_id}", guest_id, group_id)
    await handle_view_party(callback, mock_bot)
    cbs = _keyboard_callbacks(mock_bot.edit_message_text.call_args[1]["reply_markup"])
    assert f"join_party:{party_id}:1" not in cbs
    assert f"join_party:{party_id}:2" in cbs

    # Once the whole party is full the join buttons give way to Start.
    filler = await create_test_user(db_session, "پرکنندهٔ تیم پر")
    party = await PartyService.join_party(db_session, party_id, filler.id)
    last = await create_test_user(db_session, "آخرین تیم پر")
    party = await PartyService.join_party(db_session, party_id, last.id)
    await db_session.commit()
    assert party.is_full
    cbs = _keyboard_callbacks(get_shared_friendly_party_keyboard(party))
    assert not any(c.startswith("join_party:") for c in cbs)
    assert f"friendly_start:{party_id}" in cbs
    assert f"friendly_fill_bots:{party_id}" not in cbs


@pytest.mark.asyncio
async def test_friendly_party_shared_card_keeps_bot_fill_host_only(db_session):
    """The shared card carries the host's Bot Fill, but only the host may use it."""
    group = await create_test_group(db_session, "گروه محافظت بات")
    host = await create_test_user(db_session, "میزبان محافظت")
    guest = await create_test_user(db_session, "مهمان محافظت")
    group_id, host_id, guest_id = group.id, host.id, guest.id

    party = await PartyService.create_friendly_party(
        db_session, group_id, host_id, "connect_four", "2v2"
    )
    party = await PartyService.join_party(db_session, party.id, guest_id, team=2)
    party_id = party.id
    await db_session.commit()

    # The non-host presser is refused with a friendly alert, no bots appear
    # and the card is not re-rendered.
    mock_bot = AsyncMock()
    callback = _callback(f"friendly_fill_bots:{party_id}", guest_id, group_id)
    await handle_friendly_fill_bots(callback, mock_bot)

    await db_session.rollback()
    refreshed = await PartyService.get_party_by_id(db_session, party_id)
    assert not refreshed.is_full
    assert refreshed.current_players_count == 2
    assert await _bot_members(db_session, party_id) == []
    assert not mock_bot.edit_message_text.called
    assert callback.answer.await_count == 1


@pytest.mark.asyncio
async def test_friendly_game_action_edits_one_shared_board(db_session):
    """A friendly move re-renders ONE group board (never one per team)."""
    group = await create_test_group(db_session, "گروه یک‌صفحه")
    users = [await create_test_user(db_session, f"یک‌صفحه {i}") for i in range(2)]
    party = await _friendly_party(db_session, group, users, "solo")
    await db_session.commit()
    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()

    match = await FriendlyMatchService.load_match(db_session, match.id)
    match.group1_message_id = 900
    match.group2_message_id = 900
    await db_session.commit()

    state = json.loads(match.game_state)
    current_player = int(state["current_player_id"])
    mock_bot = AsyncMock()
    callback = _callback(
        f"c4_action:{match.id}:3", current_player, group.id, message_id=900
    )

    await handle_c4_action(callback, mock_bot)

    assert mock_bot.edit_message_text.call_count == 1
    _, kwargs = mock_bot.edit_message_text.call_args
    assert kwargs["chat_id"] == group.id and kwargs["message_id"] == 900
    cbs = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert f"game_refresh:{match.id}" in cbs


@pytest.mark.asyncio
async def test_friendly_final_board_is_neutral_and_offers_rematch(db_session):
    """The shared final board names the winning TEAM and offers Rematch."""
    group = await create_test_group(db_session, "گروه پایان رفیقانه")
    users = [await create_test_user(db_session, f"پایان‌رفیق {i}") for i in range(2)]
    party = await _friendly_party(db_session, group, users, "solo")
    await db_session.commit()
    match = await FriendlyMatchService.start_friendly_match(db_session, party.id, users[0].id)
    await db_session.commit()
    match = await _finish(db_session, match, winner_team=2)
    await db_session.commit()

    mock_bot = AsyncMock()
    callback = _callback(f"view_party:{party.id}", users[0].id, group.id, message_id=910)
    assert await render_friendly_board(callback, mock_bot, match, users[0].id) is True

    text = mock_bot.edit_message_text.call_args[0][0]
    assert "گروه حریف" not in text and "تیم شما" not in text
    assert "بازی رفیقانه" in text  # the neutral, team-named result line
    kb = mock_bot.edit_message_text.call_args[1]["reply_markup"]
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"friendly_rematch:{match.id}" in cbs
    assert "group_active_games" in cbs
