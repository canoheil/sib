"""Focused bot naming and delayed-turn regressions."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from sib.bots import runner
from sib.bots.runner import run_bot_turns
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


def _bot_turn_fixture():
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
        group_id=-102,
        user=SimpleNamespace(is_bot=True, display_name="🤖 سیب کوچولو"),
        bot_profile=None,
    )
    engine = get_game_engine("connect_four")
    state = engine.create_initial_state([human, bot], "solo")
    state = engine.apply_action(state, human.user_id, {"col": 3})
    match = SimpleNamespace(
        id=404,
        game=SimpleNamespace(code="connect_four"),
        players=[human, bot],
        is_bot_match=True,
        state=MatchState.ACTIVE,
        current_turn_player_id=bot.user_id,
        game_state=None,
        turn_deadline=None,
    )
    return match, state, human, bot


def _simultaneous_bot_turn_fixture():
    human = SimpleNamespace(
        user_id=12001,
        team=1,
        group_id=-201,
        user=SimpleNamespace(is_bot=False, display_name="بازیکن واقعی"),
        bot_profile=None,
    )
    bot = SimpleNamespace(
        user_id=12002,
        team=2,
        group_id=-202,
        user=SimpleNamespace(is_bot=True, display_name="🤖 سیب خندان"),
        bot_profile=None,
    )
    engine = get_game_engine("rock_paper_scissors")
    state = engine.create_initial_state([human, bot], "solo")
    state = engine.apply_action(state, human.user_id, {"choice": "rock"})
    match = SimpleNamespace(
        id=405,
        game=SimpleNamespace(code="rock_paper_scissors"),
        players=[human, bot],
        is_bot_match=True,
        state=MatchState.ACTIVE,
        current_turn_player_id=None,
        game_state=None,
        turn_deadline=None,
    )
    return match, state, human, bot


@pytest.mark.asyncio
async def test_simultaneous_bot_choice_is_delayed_once_per_round():
    match, state, _human, bot = _simultaneous_bot_turn_fixture()
    delays = []

    async def no_wait(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    updated = await run_bot_turns(
        match,
        state,
        sleep=no_wait,
        random_delay=lambda minimum, maximum: (minimum + maximum) / 2,
    )

    bot_state = updated["players"][str(bot.user_id)]
    # Both choices resolved round one and reset for round two. The bot is not
    # allowed to make a second hidden choice during the same runner call.
    assert updated["round"] == 2
    assert bot_state["locked"] is False
    assert len(delays) == 1
    assert runner.BOT_MOVE_DELAY_MIN_SECONDS <= delays[0] <= runner.BOT_MOVE_DELAY_MAX_SECONDS


@pytest.mark.asyncio
async def test_bot_move_waits_random_async_delay_and_remains_non_blocking():
    match, state, human, bot = _bot_turn_fixture()
    delays = []
    ranges = []
    entered_wait = asyncio.Event()
    release_wait = asyncio.Event()

    def pick_delay(minimum, maximum):
        ranges.append((minimum, maximum))
        return 1.37

    async def controlled_sleep(delay):
        delays.append(delay)
        entered_wait.set()
        await release_wait.wait()

    task = asyncio.create_task(
        run_bot_turns(
            match,
            state,
            sleep=controlled_sleep,
            random_delay=pick_delay,
        )
    )
    await asyncio.wait_for(entered_wait.wait(), timeout=0.5)

    # The bot has not moved yet, and an unrelated coroutine can run while its
    # asynchronous delay is pending.
    assert state["moves_count"] == 1
    loop_responsive = asyncio.Event()
    asyncio.get_running_loop().call_soon(loop_responsive.set)
    await asyncio.wait_for(loop_responsive.wait(), timeout=0.5)
    assert ranges == [(runner.BOT_MOVE_DELAY_MIN_SECONDS, runner.BOT_MOVE_DELAY_MAX_SECONDS)]
    assert runner.BOT_MOVE_DELAY_MIN_SECONDS <= delays[0] <= runner.BOT_MOVE_DELAY_MAX_SECONDS

    release_wait.set()
    updated = await task
    assert updated["moves_count"] == 2
    assert updated["current_player_id"] == human.user_id
    assert match.current_turn_player_id == human.user_id
    assert bot.user.is_bot is True


@pytest.mark.asyncio
async def test_cancelling_during_delay_prevents_the_bot_move():
    match, state, _human, _bot = _bot_turn_fixture()
    entered_wait = asyncio.Event()
    never_release = asyncio.Event()

    async def controlled_sleep(_delay):
        entered_wait.set()
        await never_release.wait()

    task = asyncio.create_task(
        run_bot_turns(
            match,
            state,
            sleep=controlled_sleep,
            random_delay=lambda _minimum, _maximum: 1.0,
        )
    )
    await asyncio.wait_for(entered_wait.wait(), timeout=0.5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state["moves_count"] == 1
    assert state["current_player_id"] == match.current_turn_player_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidator",
    ["turn_changed", "match_cancelled", "match_finished", "game_finished"],
)
async def test_wake_up_rechecks_turn_and_match_before_moving(invalidator):
    match, state, human, bot = _bot_turn_fixture()

    async def invalidate_during_wait(_delay):
        if invalidator == "turn_changed":
            state["current_player_id"] = human.user_id
            match.current_turn_player_id = human.user_id
        elif invalidator == "match_cancelled":
            match.state = MatchState.CANCELLED
        elif invalidator == "match_finished":
            match.state = MatchState.FINISHED
        elif invalidator == "game_finished":
            state["finished"] = True

    updated = await run_bot_turns(
        match,
        state,
        sleep=invalidate_during_wait,
        random_delay=lambda _minimum, _maximum: 1.0,
    )

    assert updated["moves_count"] == 1
    assert bot.user.is_bot is True


@pytest.mark.asyncio
async def test_bot_turn_metadata_prevents_out_of_turn_and_duplicate_moves():
    match, state, human, bot = _bot_turn_fixture()
    sleep_calls = []

    async def no_sleep(delay):
        sleep_calls.append(delay)

    # A stale match turn cannot authorize the bot, even when the game state
    # still points at it.
    match.current_turn_player_id = human.user_id
    unchanged = await run_bot_turns(
        match,
        state,
        sleep=no_sleep,
        random_delay=lambda _minimum, _maximum: 1.0,
    )
    assert unchanged["moves_count"] == 1
    assert sleep_calls == []

    # Restore the legitimate bot turn and let it move once.
    match.current_turn_player_id = bot.user_id
    updated = await run_bot_turns(
        match,
        state,
        sleep=no_sleep,
        random_delay=lambda _minimum, _maximum: 1.0,
    )
    assert updated["moves_count"] == 2
    after_first_move = copy.deepcopy(updated)

    # Replaying the runner on the now-human turn cannot make another bot move.
    replayed = await run_bot_turns(
        match,
        updated,
        sleep=no_sleep,
        random_delay=lambda _minimum, _maximum: 1.0,
    )
    assert replayed == after_first_move
    assert len(sleep_calls) == 1


@pytest.mark.asyncio
async def test_human_vs_human_match_skips_bot_delay():
    match, state, _human, _bot = _bot_turn_fixture()
    match.is_bot_match = False

    async def unexpected_sleep(_delay):
        raise AssertionError("human-vs-human matches must not wait for a bot")

    unchanged = await run_bot_turns(
        match,
        state,
        sleep=unexpected_sleep,
        random_delay=lambda _minimum, _maximum: 1.0,
    )
    assert unchanged["moves_count"] == 1
