"""Persian prose: pool rendering, history-aware replay, and event variety."""

import re
from random import Random

import pytest

from sib.services.invasion_v2 import (
    render_announcement,
    render_defeat,
    render_expired,
    render_finisher,
    render_high_damage,
    render_join,
    render_victory,
)


from sib.services.invasion_v2 import narrative_pool as pool


@pytest.fixture(autouse=True)
def clean_history():
    with pool._lock:
        pool._history.clear()
    yield
    with pool._lock:
        pool._history.clear()


EVENTS = (
    "announcement", "join_normal", "join_critical", "join_high",
    "finisher", "victory", "defeat", "expired",
)


def render_event(event: str, index: int, seed: int = 30) -> str:
    username = f"داریوش{index}"
    card_name = f"سیب سرخ{index}"
    rng = Random(seed)
    if event in ("join_normal", "join_critical", "join_high"):
        values = dict(username=username, card_name=card_name, damage=123,
                      enemy_hp_remaining=456, rng=rng)
        if event == "join_high":
            return render_high_damage(**values)
        return render_join(is_crit=event == "join_critical", **values)
    values = dict(username=username, card_name=card_name,
                  region_name="درّه ماه", enemy_name="شیاسیب سایه", rng=rng)
    return {
        "announcement": render_announcement,
        "finisher": render_finisher,
        "victory": render_victory,
        "defeat": render_defeat,
        "expired": render_expired,
    }[event](**values)


@pytest.mark.parametrize("event", EVENTS)
def test_narrative_has_no_placeholders_repetitions_or_english(event):
    text = render_event(event, 1)
    assert "{" not in text and "}" not in text
    assert not re.search(r"[A-Za-z]", text)
    assert re.search(r"[\u0600-\u06ff]", text)
    # Supplied reference paragraphs sometimes omit names or combat numbers.
    # Values must be injected exactly when their selected template asks for them.
    pools = {"announcement": pool.ANNOUNCEMENT, "join_normal": pool.JOIN_NORMAL,
             "join_critical": pool.JOIN_CRIT, "join_high": pool.JOIN_CRIT,
             "finisher": pool.JOIN_KILL, "victory": [text for text in pool.VICTORY if "{n}" not in text],
             "defeat": pool.DEFEAT, "expired": pool.EXPIRED}
    values = dict(user="داریوش1", card="سیب سرخ1", damage="۱۲۳", hp_remaining="۴۵۶",
                  region="درّه ماه", enemy_name="شیاسیب سایه")
    assert text in {template.format(**values) for template in pools[event]}
    # Repeated sentences/clauses in one render are never deliberate variety.
    clauses = [part.strip() for part in re.split(r"[.!؟؛]", text) if part.strip()]
    assert len(clauses) == len(set(clauses))


@pytest.mark.parametrize("event", EVENTS)
def test_narrative_is_deterministic_given_seed_and_history(event):
    first = [render_event(event, 3, seed) for seed in range(20)]
    with pool._lock:
        pool._history.clear()
    assert first == [render_event(event, 3, seed) for seed in range(20)]


@pytest.mark.parametrize("event", EVENTS)
def test_twenty_picks_in_one_context_avoid_recent_repeats(event):
    outputs = []
    for index in range(20):
        text = render_event(event, 1, 500 + index)
        assert text not in outputs[-5:]
        outputs.append(text)


@pytest.mark.parametrize("event", EVENTS)
def test_same_inputs_have_multiple_cinematic_alternatives(event):
    outputs = {render_event(event, 1, seed) for seed in range(20)}
    assert len(outputs) >= 3


def test_join_crit_and_normal_are_distinct_given_same_values():
    common = dict(username="ناهید", card_name="سیب ماهی", damage=24,
                  enemy_hp_remaining=12)
    assert render_join(**common, is_crit=False, rng=Random(19)) != render_join(
        **common, is_crit=True, rng=Random(19)
    )
