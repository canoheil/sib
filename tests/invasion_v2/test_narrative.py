"""Pure Persian prose: rendering, seeded replay, and event variety."""

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
    assert text.count("داریوش1") == 1
    assert text.count("سیب سرخ1") == 1
    # Repeated sentences/clauses in one render are never deliberate variety.
    clauses = [part.strip() for part in re.split(r"[.!؟؛]", text) if part.strip()]
    assert len(clauses) == len(set(clauses))
    if event.startswith("join"):
        assert "۱۲۳" in text and "۴۵۶" in text
    else:
        assert text.count("درّه ماه") == 1
        assert text.count("شیاسیب سایه") == 1


@pytest.mark.parametrize("event", EVENTS)
def test_narrative_is_deterministic_given_seed(event):
    assert render_event(event, 3, 123) == render_event(event, 3, 123)
    assert render_event(event, 3, 123) != render_event(event, 4, 123)


@pytest.mark.parametrize("event", EVENTS)
def test_twenty_varied_inputs_never_repeat_an_output(event):
    outputs = [render_event(event, index, 500 + index) for index in range(20)]
    assert len(outputs) == len(set(outputs))


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
