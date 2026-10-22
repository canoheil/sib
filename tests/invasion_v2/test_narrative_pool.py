"""Phase 8B: copy contracts, frozen API, safe formatting and recent selection."""
import inspect
import random
import re
from concurrent.futures import ThreadPoolExecutor
from string import Formatter

import pytest

from sib.services.invasion_v2 import narrative as narrative
from sib.services.invasion_v2 import narrative_pool as pool

MINIMUMS = {"ANNOUNCEMENT": 12, "JOIN_NORMAL": 22, "JOIN_CRIT": 16,
            "JOIN_BUFF": 6, "JOIN_KILL": 10, "VICTORY": 18,
            "DEFEAT": 12, "EXPIRED": 8}
POOLS = {name: getattr(pool, name) for name in MINIMUMS}
ALLOWED = {"user", "card", "region", "enemy_name", "damage", "hp_remaining", "n"}
TEMPLATES = [(name, i, text) for name, items in POOLS.items() for i, text in enumerate(items)]
COMMON = dict(username="داریوش", card_name="سیب سرخ", region_name="درّه ماه", enemy_name="شیاسیب سایه")


@pytest.fixture(autouse=True)
def clean_history():
    with pool._lock:
        pool._history.clear()
    yield
    with pool._lock:
        pool._history.clear()


def _render_category(category, **overrides):
    values = {**COMMON, **overrides}
    participant_count = values.pop("participant_count", None)
    rng = random.Random(17)
    if category.startswith("JOIN_"):
        return narrative.render_join(username=values["username"], card_name=values["card_name"],
            damage=123, is_crit=category == "JOIN_CRIT",
            enemy_hp_remaining=0 if category == "JOIN_KILL" else 456, rng=rng,
            is_buffed=category == "JOIN_BUFF")
    if category == "VICTORY":
        return narrative.render_victory(**values, rng=rng, participant_count=participant_count)
    return getattr(narrative, "render_" + category.lower())(**values, rng=rng)


def test_every_pool_has_minimum_count():
    assert sum(map(len, POOLS.values())) >= 80
    for name, minimum in MINIMUMS.items():
        assert isinstance(POOLS[name], list)
        assert len(POOLS[name]) >= minimum
        assert len(set(POOLS[name])) == len(POOLS[name])


@pytest.mark.parametrize("category", POOLS)
def test_no_template_exceeds_900_chars(category):
    assert all(0 < len(text) < 900 for text in POOLS[category])


@pytest.mark.parametrize("category", POOLS)
def test_only_allowed_placeholders(category):
    for text in POOLS[category]:
        for _, field, spec, conversion in Formatter().parse(text):
            if field is not None:
                assert field in ALLOWED
                assert not spec and conversion is None
                if field == "n":
                    assert category == "VICTORY"
        # Never invent data that cannot be provided by the frozen APIs.
        if category in ("JOIN_NORMAL", "JOIN_CRIT", "JOIN_BUFF"):
            assert "{region}" not in text and "{enemy_name}" not in text
        if category == "JOIN_KILL":
            assert {f for _, f, _, _ in Formatter().parse(text) if f} <= {"user", "card"}


@pytest.mark.parametrize("category", POOLS)
def test_short_modern_copy_and_opening_variety(category):
    texts = POOLS[category]
    assert all(0 < len(text) <= 140 for text in texts)
    assert all(1 <= len(re.findall(r"[.!؟]", text)) <= 2 for text in texts)
    assert all(text.count("**") >= 2 and text.count("**") % 2 == 0 for text in texts)
    assert all("\n" not in text and "—" not in text and "–" not in text for text in texts)
    assert not any(word in text for text in texts
                   for word in ("در هم کوبید", "نبرد عظیم", "دلاور", "پیش تاخت"))
    openings = [" ".join(text.split()[:3]) for text in texts]
    assert max(openings.count(opening) for opening in openings) <= 2


def test_pick_avoids_recent_repeats():
    selected = []
    for _ in range(300):
        text = pool.pick(pool.JOIN_NORMAL, key="join:123")
        assert text not in selected[-5:]
        selected.append(text)


@pytest.mark.parametrize("items", [["الف"], ["الف", "ب"], ["الف", "الف"], [str(i) for i in range(5)]])
def test_pick_small_pool(items):
    assert all(pool.pick(items, key="small") in items for _ in range(40))


def test_pick_zero_empty_and_negative():
    assert pool.pick(["الف"], key="zero", history_size=0) == "الف"
    with pytest.raises(ValueError):
        pool.pick([], key="empty")
    with pytest.raises(ValueError):
        pool.pick(pool.JOIN_NORMAL, key="negative", history_size=-1)


def test_pick_independent_keys_and_dynamic_history(monkeypatch):
    monkeypatch.setattr(pool.random, "choice", lambda available: available[0])
    assert pool.pick(pool.JOIN_NORMAL, key="first") == pool.JOIN_NORMAL[0]
    assert pool.pick(pool.JOIN_NORMAL, key="second") == pool.JOIN_NORMAL[0]
    selected = []
    for size in [1, 3, 7, 2, 10, 0, 5] * 8:
        text = pool.pick(pool.JOIN_NORMAL, key="changing", history_size=size)
        if size:
            assert text not in selected[-size:]
        selected.append(text)


def test_pick_thread_safe_and_compact_history(monkeypatch):
    ordered = []
    original = pool.random.choice
    def record(choices):
        value = original(choices)
        ordered.append(value)  # Called inside the picker's lock, in selection order.
        return value
    monkeypatch.setattr(pool.random, "choice", record)
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: pool.pick(pool.JOIN_NORMAL, key="parallel"), range(500)))
    for i, text in enumerate(ordered):
        assert text not in ordered[max(0, i-5):i]
    assert pool._history["parallel"].position == 500
    assert len(pool._history["parallel"].last_used) <= len(pool.JOIN_NORMAL)


@pytest.mark.parametrize("hp,critical", [(0, False), (0, True), (-5, False), (-5, True)])
def test_render_join_selects_kill_pool(monkeypatch, hp, critical):
    def pick(items, *, key, history_size=5):
        assert items is pool.JOIN_KILL and key.startswith("join:")
        return items[0]
    monkeypatch.setattr(pool, "pick", pick)
    text = narrative.render_join(username="داریوش", card_name="سیب سرخ", damage=123,
        is_crit=critical, enemy_hp_remaining=hp, rng=random.Random(0))
    assert text == pool.JOIN_KILL[0].format(user="داریوش", card="سیب سرخ")


def test_render_join_selects_crit_pool(monkeypatch):
    def pick(items, *, key, history_size=5):
        assert items is pool.JOIN_CRIT
        return items[0]
    monkeypatch.setattr(pool, "pick", pick)
    _render_category("JOIN_CRIT")


def test_render_join_selects_normal_pool(monkeypatch):
    def pick(items, *, key, history_size=5):
        assert items is pool.JOIN_NORMAL
        return items[0]
    monkeypatch.setattr(pool, "pick", pick)
    _render_category("JOIN_NORMAL")


@pytest.mark.parametrize("category,index,template", TEMPLATES, ids=[f"{n}-{i}" for n,i,_ in TEMPLATES])
def test_all_renders_stay_under_900_chars(monkeypatch, category, index, template):
    monkeypatch.setattr(pool, "pick", lambda *args, **kwargs: template)
    context = {"participant_count": 7} if "{n}" in template else {}
    text = _render_category(category, **context)
    assert 0 < len(text) < 900 and "{" not in text and "}" not in text
    long = _render_category(category, username="داریوش " * 1000,
        card_name="سیب " * 1000, region_name="درّه " * 1000, enemy_name="شیاسیب " * 1000, **context)
    assert 0 < len(long) < 900


@pytest.mark.parametrize("category", POOLS)
def test_no_english_letters(monkeypatch, category):
    injected = dict(username="GuardName", card_name="AppleCard", region_name="RegionName", enemy_name="EnemyName")
    for template in POOLS[category]:
        literal = "".join(part for part, _, _, _ in Formatter().parse(template))
        assert not re.search(r"[A-Za-z0-9]", literal)
        monkeypatch.setattr(pool, "pick", lambda *args, _template=template, **kwargs: _template)
        context = {"participant_count": 7} if "{n}" in template else {}
        text = _render_category(category, **injected, **context)
        for value in injected.values():
            text = text.replace(value, "")
        assert not re.search(r"[A-Za-z0-9]", text)


def test_numbers_are_persian_and_names_not_reformatted(monkeypatch):
    monkeypatch.setattr(pool, "pick", lambda *args, **kwargs: pool.JOIN_NORMAL[5])
    text = _render_category("JOIN_NORMAL", username="نگهبان {نام}")
    assert "۱۲۳" in text and "۴۵۶" in text
    assert "نگهبان {نام}" in text  # Injected braces are data, not a new format template.


def test_high_damage_and_finisher_compatibility(monkeypatch):
    selected = []
    def pick(items, *, key, history_size=5):
        selected.append(items)
        return items[0]
    monkeypatch.setattr(pool, "pick", pick)
    for hp in (1, 0):
        narrative.render_high_damage(username="داریوش", card_name="سیب سرخ", damage=99,
                                     enemy_hp_remaining=hp, rng=random.Random(1))
    narrative.render_finisher(**COMMON, rng=random.Random(1))
    assert selected[0] is pool.JOIN_CRIT
    assert selected[1] is pool.JOIN_KILL and selected[2] is pool.JOIN_KILL


def test_render_keys_do_not_depend_on_damage_or_rng(monkeypatch):
    keys = []
    def pick(items, *, key, history_size=5):
        keys.append(key)
        return items[0]
    monkeypatch.setattr(pool, "pick", pick)
    for damage, hp in [(20, 80), (21, 59), (59, 0)]:
        narrative.render_join(username="داریوش", card_name="سیب سرخ", damage=damage,
            is_crit=True, enemy_hp_remaining=hp, rng=random.Random(damage))
    narrative.render_finisher(**COMMON, rng=random.Random(1))
    assert len(set(keys)) == 1
    assert "داریوش" not in keys[0]  # History retains hashes, not display names.


def test_render_repetition_and_rng_scope():
    before = random.getstate()
    outputs = []
    for _ in range(30):
        text = _render_category("JOIN_NORMAL")  # Same seed every call, different recent history.
        assert text not in outputs[-5:]
        outputs.append(text)
    assert random.getstate() == before
    assert pool._rng.get() is None


def test_public_signatures_backwards_compatible():
    expected = {
        "render_join": ("username", "card_name", "damage", "is_crit", "enemy_hp_remaining", "rng"),
        "render_high_damage": ("username", "card_name", "damage", "enemy_hp_remaining", "rng"),
    }
    for name in ("announcement", "finisher", "victory", "defeat", "expired"):
        expected["render_"+name] = ("username", "card_name", "region_name", "enemy_name", "rng")
    public = {name for name, obj in vars(narrative).items() if not name.startswith("_") and inspect.isfunction(obj)}
    assert public == set(expected)
    for name, names in expected.items():
        sig = inspect.signature(getattr(narrative, name))
        optional = ("invasion_id", "participant_count") if name == "render_victory" else ("invasion_id",)
        if name == "render_join":
            optional += ("is_buffed",)
        assert tuple(sig.parameters) == names + optional and sig.return_annotation is str
        for parameter in sig.parameters.values():
            assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
            if parameter.name == "is_buffed":
                assert parameter.default is False and parameter.annotation is bool
                continue
            if parameter.name in optional:
                assert parameter.default is None and parameter.annotation == (int | None)
                continue
            assert parameter.default is inspect.Parameter.empty
            annotation = {"damage": int, "enemy_hp_remaining": int, "is_crit": bool, "rng": random.Random}.get(parameter.name, str)
            assert parameter.annotation is annotation


def test_added_copy_is_protective_not_graphic_or_tabular():
    forbidden = ("خون", "خونریزی", "تکه‌تکه", "قطع عضو", "فریاد درد", "نتیجه:", "آسیب ثبت شد")
    for items in POOLS.values():
        for text in items:
            assert not any(word in text for word in forbidden)
            assert not re.search(r"[🏁⚔️💢🗺]", text)
    for items in (pool.JOIN_NORMAL, pool.JOIN_CRIT, pool.JOIN_KILL):
        for text in items[3:]:
            assert "{user}" in text and "{card}" in text


# N2 reference samples: concise, conversational and bold by default.
def test_n2_announcement_and_join_samples():
    assert pool.ANNOUNCEMENT[0] == (
        "**{region}** زیر حمله‌ست! شیاسیب رسیده؛ برای دفاع آماده‌اید؟"
    )
    assert pool.JOIN_NORMAL[0] == (
        "**{user}** با **{card}** ضربه زد! شیاسیب یه قدم عقب رفت."
    )
