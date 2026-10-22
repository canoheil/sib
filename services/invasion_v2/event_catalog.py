"""Catalogue of the twelve Invasion v2 random events.

Everything user-facing lives here: the Persian name, the three narrative
variants, the effect line and the image file name. Effects are expressed as a
fraction of the *total* boss HP wherever possible, so one definition is fair
for a three-player group and for a twenty-player group.

The module is pure data plus pure helpers: no database, no Telegram and no
global mutable state, so the scheduler and the tests can share it.
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from sib.services.sib_claim import fa_digits

# Effect kinds persisted in ``inv2_event_effects.kind``.
DAMAGE_MULTIPLIER = "damage_multiplier"
CRIT_BONUS = "crit_bonus"
DAMAGE_CAP_PCT = "damage_cap_pct"
BOSS_HEAL_PCT = "boss_heal_pct"
INSTANT_DAMAGE_PCT = "instant_damage_pct"

# Timed kinds are read by the join path; one-shot kinds only document HP.
TIMED_KINDS = (DAMAGE_MULTIPLIER, CRIT_BONUS, DAMAGE_CAP_PCT)

# Categories.
BOSS_BUFF = "boss_buff"
PLAYER_BUFF = "player_buff"
INSTANT_DAMAGE = "instant_damage"
LOOT = "loot"

# A chest is limited to its first three winners unless configured otherwise.
DEFAULT_LOOT_MAX_CLAIMS = 3


@dataclass(frozen=True)
class LootSpec:
    """What a chest grants, and through which canonical service."""

    reward_type: str          # wallet ``apples`` or an ``ITEM_CATALOG`` key
    label_fa: str             # «یک سیب طلایی»
    default_amount: int
    amount_config_key: Optional[str] = None

    @property
    def is_wallet(self) -> bool:
        return self.reward_type == "apples"


@dataclass(frozen=True)
class EventTemplate:
    """One of the twelve events, independent of any battle instance."""

    code: str
    name_fa: str
    category: str
    weight: float
    duration_seconds: int
    effects: tuple[tuple[str, float], ...]
    narratives: tuple[str, ...]
    loot: Optional[LootSpec] = None

    @property
    def image_name(self) -> str:
        return f"{self.code}.jpg"

    @property
    def is_one_shot(self) -> bool:
        return self.duration_seconds <= 0

    def effect_value(self, kind: str) -> Optional[float]:
        for effect_kind, value in self.effects:
            if effect_kind == kind:
                return value
        return None

    def eligible(self, *, hp_remaining: int, total_hp: int) -> bool:
        """A heal cannot be planned on a boss already at full HP."""
        if hp_remaining <= 0:
            return False
        if self.effect_value(BOSS_HEAL_PCT) is not None:
            return hp_remaining < total_hp
        return True


EVENTS: tuple[EventTemplate, ...] = (
    EventTemplate(
        code="boss_obsidian_armor",
        name_fa="زرهٔ ابسیدینی",
        category=BOSS_BUFF,
        weight=1.0,
        duration_seconds=180,
        effects=((DAMAGE_MULTIPLIER, 0.65),),
        narratives=(
            "🛡 شیاسیب **زره ابسیدینی** پوشید! ضربه‌زدن سخت‌تر شد.",
            "🛡 **زره ابسیدینی** رفت تن شیاسیب! حالا آسیب کمتری می‌خوره.",
            "🛡 سنگ سیاه دور شیاسیب جمع شد! **زره جدیدش** رو جدی بگیرید.",
        ),
    ),
    EventTemplate(
        code="boss_shadow_heal",
        name_fa="بازسازی سایه",
        category=BOSS_BUFF,
        weight=0.8,
        duration_seconds=0,
        effects=((BOSS_HEAL_PCT, 0.10),),
        narratives=(
            "💚 شیاسیب توی سایه **جانش رو برگردوند**! دوباره باید بزنیم.",
            "💚 سایه به شیاسیب رسید و **زخم‌هاش رو ترمیم کرد**! هنوز تموم نشده.",
            "💚 شیاسیب **جان گرفت**! راحتش نذارید، ادامه بدید.",
        ),
    ),
    EventTemplate(
        code="boss_cold_breath",
        name_fa="نفس سرد",
        category=BOSS_BUFF,
        weight=1.0,
        duration_seconds=150,
        effects=((CRIT_BONUS, -0.10),),
        narratives=(
            "🥶 **نفس سرد** شیاسیب اومد! کریتیکال‌ها موقتاً یخ زدن.",
            "🥶 هوا یخ زد و **کریتیکال** از کار افتاد! باید تا تموم شدنش صبر کنیم.",
            "🥶 شیاسیب سرما فرستاد! **ضربه‌های کریتیکال** موقتاً تعطیله.",
        ),
    ),
    EventTemplate(
        code="boss_ward",
        name_fa="طلسم سکون",
        category=BOSS_BUFF,
        weight=0.9,
        duration_seconds=120,
        effects=((DAMAGE_CAP_PCT, 0.15),),
        narratives=(
            "⛔ **طلسم سکون** فعال شد! ضربه‌ها دیگه آسیب کامل نمی‌زنن.",
            "⛔ شیاسیب دور خودش **سقف آسیب** گذاشت! ضربه‌های سنگین هم محدود می‌شن.",
            "⛔ سنگ‌ها دور شیاسیب چرخیدن! تا طلسم هست، **آسیب سقف داره**.",
        ),
    ),
    EventTemplate(
        code="blessing_apple",
        name_fa="برکت سیب",
        category=PLAYER_BUFF,
        weight=1.2,
        duration_seconds=180,
        effects=((DAMAGE_MULTIPLIER, 1.5),),
        narratives=(
            "🍏 **برکت سیب** رسید! حالا ضربه‌هاتون آسیب بیشتری می‌زنه.",
            "🍏 نور سیب روی کارت‌ها نشست! **آسیب همه** زیاد شد.",
            "🍏 **باف آسیب** فعاله! همین حالا بزنید.",
        ),
    ),
    EventTemplate(
        code="blessing_hawk",
        name_fa="چشم شاهین",
        category=PLAYER_BUFF,
        weight=1.0,
        duration_seconds=150,
        effects=((CRIT_BONUS, 0.25),),
        narratives=(
            "🦅 **چشم شاهین** فعال شد! نقاط ضعف شیاسیب پیداست.",
            "🦅 چشم‌هاتون تیز شد! **کریتیکال** زدن از همیشه راحت‌تره.",
            "🦅 شاهین هدف رو گرفت! شانس **کریتیکال** رفت بالا.",
        ),
    ),
    EventTemplate(
        code="blessing_dawn",
        name_fa="نور سپیده",
        category=PLAYER_BUFF,
        weight=0.8,
        duration_seconds=90,
        effects=((DAMAGE_MULTIPLIER, 2.0),),
        narratives=(
            "🌟 **نور سپیده** رسید! ضربه‌هاتون دو برابر می‌زنه.",
            "🌟 شیاسیب توی نور موند! **آسیب دو برابر** رو از دست ندید.",
            "🌟 **باف آسیب** روشنه و آسیب ضربه‌ها **۲ برابر**ه! سریع بزنید، فرصت کمیه.",
        ),
    ),
    EventTemplate(
        code="meteor",
        name_fa="شهاب‌سنگ",
        category=INSTANT_DAMAGE,
        weight=1.0,
        duration_seconds=0,
        effects=((INSTANT_DAMAGE_PCT, 0.08),),
        narratives=(
            "☄️ **شهاب‌سنگ** خورد به شیاسیب! یه ضربه حسابی از آسمان اومد.",
            "☄️ آسمان شیاسیب رو هدف گرفت! **شهاب‌سنگ** دقیق نشست.",
            "☄️ **شهاب‌سنگ** رسید و زمین لرزید! حالا نوبت ضربه‌های شماست.",
        ),
    ),
    EventTemplate(
        code="heavenly_spear",
        name_fa="نیزهٔ آسمانی",
        category=INSTANT_DAMAGE,
        weight=1.0,
        duration_seconds=90,
        effects=((INSTANT_DAMAGE_PCT, 0.06), (CRIT_BONUS, 0.10)),
        narratives=(
            "🗡 **نیزه آسمانی** به زره شیاسیب خورد! شکافش بازه.",
            "🗡 نیزه از آسمان اومد و **زره شیاسیب** رو شکست! بزنید.",
            "🗡 **نیزه آسمانی** دقیق نشست! حالا کریتیکال زدن راحت‌تره.",
        ),
    ),
    EventTemplate(
        code="loot_golden_chest",
        name_fa="صندوقچهٔ زرین",
        category=LOOT,
        weight=1.2,
        duration_seconds=120,
        effects=(),
        narratives=(
            "🎁 **صندوقچه زرین** اومد! **سیب طلایی** منتظر سریع‌ترین‌هاست.",
            "🎁 قفل **صندوقچه زرین** باز شد! برای **سیب طلایی** عجله کنید.",
            "🎁 زمین یه **صندوقچه زرین** بیرون داد! سهم خودتون رو بردارید.",
        ),
        loot=LootSpec(reward_type="apple_gold", label_fa="یک سیب طلایی", default_amount=1),
    ),
    EventTemplate(
        code="loot_apple_sack",
        name_fa="کیسهٔ سیب",
        category=LOOT,
        weight=1.2,
        duration_seconds=120,
        effects=(),
        narratives=(
            "🍎 یه **کیسه سیب** پیدا شد! سهمتون رو قبل از بسته شدنش بردارید.",
            "🍎 **سیب تازه** توی میدانه! کیسه رو باز کنید و بردارید.",
            "🍎 کیسه کاروان اینجا مونده! **سیب‌ها** سالم‌ان و منتظر شما.",
        ),
        loot=LootSpec(reward_type="apples", label_fa="سیب", default_amount=40,
                      amount_config_key="inv2_event_loot_apples"),
    ),
    EventTemplate(
        code="loot_magic_chest",
        name_fa="صندوقچهٔ جادو",
        category=LOOT,
        weight=1.0,
        duration_seconds=120,
        effects=(),
        narratives=(
            "✨ **صندوقچه جادو** باز شد! **پودر جادویی** برای بیدار کردن باغ منتظر شماست.",
            "✨ نور آبی از **صندوقچه جادو** اومد! **پودر جادویی** رو بردارید.",
            "✨ **پودر جادویی** پیدا شد! باغ پژمرده رو باهاش برگردونید.",
        ),
        loot=LootSpec(reward_type="magic_powder", label_fa="یک پودر جادویی", default_amount=1),
    ),
)

BY_CODE: dict[str, EventTemplate] = {template.code: template for template in EVENTS}

CATEGORIES: tuple[str, ...] = (BOSS_BUFF, PLAYER_BUFF, INSTANT_DAMAGE, LOOT)


def fa_duration(seconds: int) -> str:
    """«۹۰ ثانیه» / «۳ دقیقه» / «۲ دقیقه و ۳۰ ثانیه» with Persian digits.

    Under two minutes the copy stays in seconds to keep short windows
    readable in a caption.
    """
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{fa_digits(seconds)} ثانیه"
    minutes, remainder = divmod(seconds, 60)
    if remainder:
        return f"{fa_digits(minutes)} دقیقه و {fa_digits(remainder)} ثانیه"
    return f"{fa_digits(minutes)} دقیقه"


def _percent(value: float) -> str:
    """0.35 -> «۳۵٪» (never a Latin ``%`` in Persian prose)."""
    text = f"{value * 100:.0f}"
    return f"{fa_digits(text)}٪"


def render_effect_line(
    template: EventTemplate,
    *,
    amount: Optional[int] = None,
    loot_amount: Optional[int] = None,
    max_claims: Optional[int] = None,
) -> str:
    """The status line appended after the narrative.

    ``amount`` is the real HP moved by a one-shot event. ``loot_amount`` and
    ``max_claims`` let the chest copy follow configuration.
    """
    code = template.code
    duration = fa_duration(template.duration_seconds)
    if code == "boss_obsidian_armor":
        return f"🛡 آسیب مدافعان **۳۵٪** کمتر شد؛ تا **{duration}**."
    if code == "boss_shadow_heal":
        return f"💚 شیاسیب **{fa_digits(amount or 0)} جان** برگردوند."
    if code == "boss_cold_breath":
        return f"🥶 تا **{duration}**، کریتیکال ممکن نیست."
    if code == "boss_ward":
        return f"⛔ تا **{duration}**، هر ضربه حداکثر **۱۵٪** جان شیاسیب رو کم می‌کنه."
    if code == "blessing_apple":
        return f"⚔️ آسیب همه مدافعان **۵۰٪** بیشتر شد؛ تا **{duration}**."
    if code == "blessing_hawk":
        return f"🎯 شانس کریتیکال از **۱۰٪** به **۳۵٪** رسید؛ تا **{duration}**."
    if code == "blessing_dawn":
        return f"🌟 آسیب مدافعان **۲ برابر** شد؛ تا **{duration}**."
    if code == "meteor":
        return f"☄️ شهاب‌سنگ **{fa_digits(amount or 0)} آسیب** زد."
    if code == "heavenly_spear":
        return (f"🗡 نیزه آسمانی **{fa_digits(amount or 0)} آسیب** زد؛ "
                f"تا **{duration}** شانس کریتیکال بیشتره.")
    if code == "loot_golden_chest":
        return f"🎁 **{fa_digits(max_claims or DEFAULT_LOOT_MAX_CLAIMS)} جایزه** برای نخستین نفرها؛ **یک سیب طلایی** برای هر نفر."
    if code == "loot_apple_sack":
        return (f"🍎 **{fa_digits(max_claims or DEFAULT_LOOT_MAX_CLAIMS)} جایزه** برای نخستین نفرها؛ "
                f"**{fa_digits(loot_amount or 0)} سیب** برای هر نفر.")
    if code == "loot_magic_chest":
        return f"✨ **{fa_digits(max_claims or DEFAULT_LOOT_MAX_CLAIMS)} جایزه** برای نخستین نفرها؛ **یک پودر جادویی** برای هر نفر."
    return ""


def render_loot_status(claimed: int, max_claims: int, winners: Sequence[str] = ()) -> str:
    """«🏅 برداشت‌ها: ۲ از ۳ · آرش، نیلوفر» or the empty/closed wording."""
    total = max(1, int(max_claims))
    claimed = max(0, int(claimed))
    if claimed >= total:
        return f"🏅 برداشت‌ها: **{fa_digits(total)} از {fa_digits(total)}** · صندوقچه خالی شد."
    line = f"🏅 برداشت‌ها: **{fa_digits(claimed)} از {fa_digits(total)}**"
    names = [name for name in winners if name]
    if names:
        line += " · **" + "، ".join(names) + "**"
    return line
