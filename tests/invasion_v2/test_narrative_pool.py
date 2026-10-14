"""Phase 8B: copy contracts, frozen API, safe formatting and recent selection."""
import inspect
import random
import re
from concurrent.futures import ThreadPoolExecutor
from string import Formatter

import pytest

from sib.services.invasion_v2 import narrative as narrative
from sib.services.invasion_v2 import narrative_pool as pool

MINIMUMS = {"ANNOUNCEMENT": 12, "JOIN_NORMAL": 20, "JOIN_CRIT": 15,
            "JOIN_KILL": 8, "VICTORY": 10, "DEFEAT": 10, "EXPIRED": 6}
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
            enemy_hp_remaining=0 if category == "JOIN_KILL" else 456, rng=rng)
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
        if category in ("JOIN_NORMAL", "JOIN_CRIT"):
            assert "{region}" not in text and "{enemy_name}" not in text
        if category == "JOIN_KILL":
            assert {f for _, f, _, _ in Formatter().parse(text) if f} <= {"user", "card"}


@pytest.mark.parametrize("category", POOLS)
def test_rhythm_and_added_openings(category):
    texts = POOLS[category]
    assert sum(len(t) < 100 for t in texts) >= 2
    assert sum(100 <= len(t) < 180 for t in texts) >= 2
    assert sum(180 <= len(t) < 900 for t in texts) >= 2
    # Mandatory reference text has repeated openings; assess original additions.
    openings = [" ".join(t.split()[:2]) for t in texts[3:]]
    assert len(openings) == len(set(openings))
    assert all("\n" not in t for t in texts)


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
        assert tuple(sig.parameters) == names + optional and sig.return_annotation is str
        for parameter in sig.parameters.values():
            assert parameter.kind == inspect.Parameter.KEYWORD_ONLY
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


# Supplied samples are frozen, including spelling, punctuation, perspective and
# missing placeholders. Only the prompt's presentation line wrapping is removed.
REFERENCE_FIRST_THREE = {
    "ANNOUNCEMENT": [
        "ای مدافعان اواریا! سایه‌ی شیاسیب روی {region} افتاده و باد، بوی یورش می‌دهد. اگر امروز نایستید، فردا در خانه‌های خودتان خواهند ایستاد. برخیزید.",
        "صدای طبل از دور می‌آید. سایه‌ها بلندتر شده‌اند و زمین زیر قدم‌های سنگین می‌لرزد. شیاسیب به {region} رسیده است. این یورش باید همین امروز متوقف شود.",
        "آسمان {region} سنگین شد. گرد و خاک از دوردست بلند می‌شود و هر قدمی که نزدیک‌تر می‌آید، هوای شهر تیره‌تر می‌شود. کسانی که دستشان به سلاح می‌رسد، اکنون زمانش است.",
    ],
    "JOIN_NORMAL": [
        "{user} پیش رفت. یک قدم، دو قدم. {card} بالا آمد و روی زره‌ی شیاسیب نشست. صدای برخورد پیچید و دشمن برای لحظه‌ای از پا بازماند.",
        "{card} در دست {user} سوت کشید و به پهلوی دشمن نشست. {damage} آسیب بر پیکر شیاسیب وارد شد و برای لحظه‌ای، یورشش کُند شد.",
        "{user} از میان گرد و خاک بیرون آمد و {card} را بر سینه‌ی مهاجم کوبید. شیاسیب یک قدم عقب کشید و نفسش را حبس کرد.",
    ],
    "JOIN_CRIT": [
        "آسمان ترکید! {user} با {card} چنان ضربه‌ای زد که پیکر شیاسیب به لرزه افتاد. گردان‌های دشمن از جای خود پرت شدند و زمین زیر پایشان دهان باز کرد.",
        "{user}، {card} را چرخاند و ضربه‌ای زد که پیکر مهاجم به عقب خم شد. لحظه‌ای سکوت شد — بعد گردان دشمن لرزید.",
        "یک انفجار از برخورد {card} در دست {user} با زره‌ی شیاسیب بلند شد. شعله‌های سبز از شکاف زره بیرون زد و دشمن به عقب پرتاب شد. زمین تا چند قدم سوخت.",
    ],
    "JOIN_KILL": [
        "{user} ضربه‌ی آخر را زد. {card} در هوا چرخید و بر شانه‌ی شیاسیب نشست. دشمن نفسش را در سینه حبس کرد و در تاریکی محو شد.",
        "آخرین ضربه از {user} بود. چشمان شیاسیب برای لحظه‌ای درخشید، بعد خاموش شد — و میدان ساکت. زمین زیر پایشان، با آرامشی غریب، نفس کشید.",
        "{user} پیش رفت، {card} را دو دستی گرفت و بر سینه‌ی شیاسیب کوبید. دشمن مثل سنگ به زمین افتاد و پیکرش در گرد و خاک گم شد.",
    ],
    "VICTORY": [
        "شیاسیب عقب نشست. {region} نفس کشید. باد پرچم‌های ما را در کوچه‌ها چرخاند و هر دیوار، نام قهرمانانش را زیر لب تکرار می‌کند.",
        "آخرین سایه محو شد. {region} از زیر خاکستر بیرون آمد، زخمی اما ایستاده. امشب در خانه‌ها نان می‌پزند و در میدان، سرود پیروزی خوانده می‌شود.",
        "دشمن گریخت. ما ماندیم. {region} هنوز ایستاده و آسمانش دوباره آبی شده. اما یادتان باشد: این پایان نیست — این فقط یک نفس کوتاه است.",
    ],
    "DEFEAT": [
        "دیگر صدایی از {region} نمی‌آید. شیاسیب در آن نشسته و تاریکی از هر خانه بیرون می‌زند. اواریا یک تکه از خودش را از دست داد.",
        "{region} سقوط کرد. دودی از آن بلند می‌شود که تا هفت آسمان می‌رود. هنوز می‌توان بازگشت، اما این بار گران تمام خواهد شد.",
        "شیاسیب بر {region} مسلط شد. کسانی که جان به در بردند، به کوه‌ها گریختند. دشمن ایستاده — اما ما هنوز زنده‌ایم و باز خواهیم گشت.",
    ],
    "EXPIRED": [
        "نبرد بی‌نتیجه ماند. سایه‌ها آرام آرام عقب کشیدند و در تاریکی گم شدند — گویی هیچ‌وقت آنجا نبودند.",
        "غبار فرو نشست و میدان خالی ماند. شیاسیب ناپدید شد، بدون آنکه کسی بداند کجا رفت. {region} هنوز ایستاده، اما زخم خورده.",
        "هیچ ضربه‌ای به هدف نرسید. دشمن در تاریکی غیب شد. اما این یک پیروزی نیست — فقط وقفه‌ای پیش از یورش بعدی.",
    ],
}


@pytest.mark.parametrize("category", POOLS)
def test_supplied_first_three_are_verbatim(category):
    assert POOLS[category][:3] == REFERENCE_FIRST_THREE[category]
