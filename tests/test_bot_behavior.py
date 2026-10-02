"""Bot identity + bot-turn decision regressions.

Delayed bot turn execution itself is covered by
``tests/test_bot_delay_scheduling.py`` (human path immediacy, per-bot delays,
lock boundaries, cancellation and duplicate protection). This module keeps
the bot naming pool tests and pins the pure "who must act next" decision used
by the scheduler.
"""

from types import SimpleNamespace

from sib.bots import runner
from sib.database.models import MatchState
from sib.services.bot import (
    BOT_DISPLAY_NAME_POOL,
    BOT_DISPLAY_NAME_PREFIX,
    choose_bot_display_names,
)
from sib.games.registry import get_game_engine


class FirstNamePicker:
    def sample(self, population, count):
        return list(population)[:count]

    def choice(self, population):
        return population[0]


class LastNamePicker:
    def sample(self, population, count):
        return list(reversed(population))[:count]

    def choice(self, population):
        return population[-1]


def test_bot_names_are_randomly_selected_from_one_central_pool():
    first = choose_bot_display_names(3, rng=FirstNamePicker())
    last = choose_bot_display_names(3, rng=LastNamePicker())

    assert len(BOT_DISPLAY_NAME_POOL) >= 12
    assert len(set(BOT_DISPLAY_NAME_POOL)) == len(BOT_DISPLAY_NAME_POOL)
    assert first == [f"{BOT_DISPLAY_NAME_PREFIX}{name}" for name in BOT_DISPLAY_NAME_POOL[:3]]
    assert last != first
    assert all(name.startswith(BOT_DISPLAY_NAME_PREFIX) for name in first + last)
    assert all(
        name.removeprefix(BOT_DISPLAY_NAME_PREFIX) in BOT_DISPLAY_NAME_POOL
        for name in first + last
    )


def test_bot_names_avoid_existing_names_and_overflow_without_duplicates():
    existing = f"{BOT_DISPLAY_NAME_PREFIX}{BOT_DISPLAY_NAME_POOL[0]}"
    names = choose_bot_display_names(
        len(BOT_DISPLAY_NAME_POOL) + 2,
        existing_names=[existing],
        rng=FirstNamePicker(),
    )

    assert len(names) == len(BOT_DISPLAY_NAME_POOL) + 2
    assert len(set(names)) == len(names)
    assert existing not in names
    assert all(name.startswith(BOT_DISPLAY_NAME_PREFIX) for name in names)
    assert any("۲" in name or "۳" in name for name in names)


def _turn_fixture(game_code="connect_four", *, simultaneous=False):
    human = SimpleNamespace(
        user_id=11001,
        team=1,
        group_id=-101,
        user=SimpleNamespace(is_bot=False, display_name="بازیکن واقعی"),
        bot_profile=None,
    )
    bot = SimpleNamespace(
        user_id=11002,
        team=2,
        group_id=None,
        user=SimpleNamespace(is_bot=True, display_name="🤖 سیب کوچولو"),
        bot_profile=None,
    )
    engine = get_game_engine(game_code)
    state = engine.create_initial_state([human, bot], "solo")
    state = engine.apply_action(
        state, human.user_id, {"choice": "rock"} if simultaneous else {"col": 3}
    )
    match = SimpleNamespace(
        id=404,
        game=SimpleNamespace(code=game_code),
        players=[human, bot],
        is_bot_match=True,
        state=MatchState.ACTIVE,
        current_turn_player_id=None if simultaneous else bot.user_id,
        game_state=None,
        turn_deadline=None,
    )
    return match, state, human, bot


def test_turn_based_decision_requires_the_matching_match_metadata():
    match, state, human, bot = _turn_fixture()

    assert runner.next_bot_turn(match, state) == (bot.user_id, False)
    assert runner.has_pending_bot_turn(match, state) is True

    # A stale Match metadata turn cannot authorize the bot on its own.
    match.current_turn_player_id = human.user_id
    assert runner.next_bot_turn(match, state) is None

    # A human turn is never a bot turn.
    match.current_turn_player_id = human.user_id
    state["current_player_id"] = human.user_id
    assert runner.next_bot_turn(match, state) is None


def test_simultaneous_decision_uses_the_unlocked_hidden_choice():
    match, state, human, bot = _turn_fixture("rock_paper_scissors", simultaneous=True)

    assert runner.next_bot_turn(match, state) == (bot.user_id, True)

    # Once the bot's hidden choice is locked there is no pending bot turn.
    state["players"][str(bot.user_id)]["locked"] = True
    assert runner.next_bot_turn(match, state) is None


def test_inactive_finished_or_human_matches_never_produce_bot_turns():
    match, state, human, bot = _turn_fixture()

    match.is_bot_match = False
    assert runner.next_bot_turn(match, state) is None

    match.is_bot_match = True
    match.state = MatchState.CANCELLED
    assert runner.next_bot_turn(match, state) is None

    match.state = MatchState.ACTIVE
    state["finished"] = True
    assert runner.next_bot_turn(match, state) is None


def test_games_with_their_own_auto_flow_are_never_scheduled():
    """Apple Battle's narration is not a bot turn (no registered strategy)."""
    match, state, human, bot = _turn_fixture()
    match.game = SimpleNamespace(code="apple_battle")
    # Apple Battle player states carry no hidden-choice ``locked`` flag.
    state["players"][str(bot.user_id)].pop("locked", None)
    state["current_player_id"] = None

    assert runner.next_bot_turn(match, state) is None


def test_delay_constants_stay_in_the_intended_humanization_window():
    assert (runner.BOT_MOVE_DELAY_MIN_SECONDS, runner.BOT_MOVE_DELAY_MAX_SECONDS) == (
        0.8,
        2.0,
    )
