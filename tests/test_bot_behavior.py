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
    # Clean Apple-themed names: no "🤖" prefix/suffix (the robot emoji was
    # removed from user-facing bot names; identity stays internal).
    assert first == list(BOT_DISPLAY_NAME_POOL[:3])
    assert last != first
    assert all("🤖" not in name for name in first + last)
    assert all(name in BOT_DISPLAY_NAME_POOL for name in first + last)


def test_bot_names_avoid_existing_names_and_overflow_without_duplicates():
    # A legacy stored row may still carry the old "🤖 " prefix; dedup must
    # treat it as the same base name even though new names are unprefixed.
    existing = f"{BOT_DISPLAY_NAME_PREFIX}{BOT_DISPLAY_NAME_POOL[0]}"
    names = choose_bot_display_names(
        len(BOT_DISPLAY_NAME_POOL) + 2,
        existing_names=[existing],
        rng=FirstNamePicker(),
    )

    assert len(names) == len(BOT_DISPLAY_NAME_POOL) + 2
    assert len(set(names)) == len(names)
    assert BOT_DISPLAY_NAME_POOL[0] not in names
    assert all("🤖" not in name for name in names)
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
        0.6,
        1.8,
    )


def test_default_delay_picker_stays_random_inside_the_window():
    """The delay is randomized in [0.6, 1.8]s - never fixed, never larger."""
    delays = [
        runner._random_delay(
            runner.BOT_MOVE_DELAY_MIN_SECONDS, runner.BOT_MOVE_DELAY_MAX_SECONDS
        )
        for _ in range(500)
    ]

    assert all(
        runner.BOT_MOVE_DELAY_MIN_SECONDS <= delay <= runner.BOT_MOVE_DELAY_MAX_SECONDS
        for delay in delays
    )
    # Randomized (not fixed): the sample spans most of the window and a
    # 20-30s stall can never come from the humanization delay itself.
    assert max(delays) - min(delays) > 0.5
    assert max(delays) < 5.0


# ---------------------------------------------------------------------------
# "🤖" is gone from user-facing bot names (display formatting only).
# ---------------------------------------------------------------------------


def test_legacy_prefixed_names_are_stripped_for_display():
    from sib.services.bot import strip_bot_display_prefix

    assert strip_bot_display_prefix("🤖 جنگجوی سیبی") == "جنگجوی سیبی"
    assert strip_bot_display_prefix("جنگجوی سیبی") == "جنگجوی سیبی"
    assert strip_bot_display_prefix("🤖  سیب کوچولو ") == "سیب کوچولو"


def test_party_card_member_lines_have_no_robot_emoji():
    from sib.handlers.party.renderer import _member_line

    legacy_bot = SimpleNamespace(
        user=SimpleNamespace(
            is_bot=True, display_name="🤖 جنگجوی سیبی", first_name="🤖 جنگجوی سیبی"
        )
    )
    assert _member_line(legacy_bot) == "جنگجوی سیبی"

    fresh_bot = SimpleNamespace(
        user=SimpleNamespace(is_bot=True, display_name="سیب کوچولو")
    )
    assert _member_line(fresh_bot) == "سیب کوچولو"

    # Real players are completely untouched - even one who literally chose a
    # robot emoji as the start of their own Telegram name.
    human = SimpleNamespace(
        user=SimpleNamespace(is_bot=False, display_name="🤖 علی")
    )
    assert _member_line(human) == "👤 🤖 علی"


def test_generated_bot_names_are_unprefixed_but_identity_still_internal():
    """Names are clean, while ``is_bot`` + profile still identify bots."""
    names = choose_bot_display_names(5)
    assert len(names) == 5
    assert all(name in BOT_DISPLAY_NAME_POOL for name in names)
    assert not any("🤖" in name for name in names)
