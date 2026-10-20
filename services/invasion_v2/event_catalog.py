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
            "از شکاف‌های زرهٔ شیاسیب، سنگ سیاه جوشید و صفحه‌ای براق روی پیکرش بست. تیغه‌های مدافعان بر آن پوست تازه لغزیدند و زخمی نگشودند؛ تنها جرقه بود که پرید.",
            "شیاسیب دست بر زمین کوبید. تاریکی از خاک بالا آمد و مانند پوسته‌ای دوم بر تنش نشست. هر ضربه اکنون بخشی از نیرویش را در این پوست سیاه گم می‌کند.",
            "سنگ‌های سیاه از دل زمین برخاستند و به هم جوش خوردند. شیاسیب در زرهٔ ابسیدینی فرو رفت و خنده‌اش از پشت آن شنیده شد؛ اما هر زرهی، در جایی درز دارد.",
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
            "شیاسیب عقب کشید و در سایهٔ خودش پنهان شد. وقتی بیرون آمد، زخم‌ها بسته شده بودند و از ضربه‌ها فقط خطی کمرنگ مانده بود. باید از نو بزنید.",
            "تاریکی از چهار گوشهٔ میدان به سوی او دوید و بر زخم‌هایش نشست. پیکر شیاسیب دوباره درخشید و نفسش بازگشت؛ چیزی از جانش را با خود آورد.",
            "سایه‌ها مانند دست‌هایی بر پیکر شیاسیب لغزیدند و شکاف‌ها را دوختند. او سر برداشت و به مدافعان نگاه کرد؛ نگاهش می‌گفت هنوز تمام نشده است.",
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
            "شیاسیب نفس کشید و باد سردی از سینه‌اش بیرون زد. تیغه‌ها کند شد و دست‌ها در سرما لرزید. تا دو دقیقه و نیم، ضربهٔ بحرانی در کار نیست.",
            "نفس شیاسیب آسمان را سفید کرد. شبنم روی زره‌ها یخ بست و مدافعان دیدند تیزی شمشیرهایشان در سرما خاکستری شده است.",
            "سرمای نفس او تا مغز استخوان رفت. میان این باد، هیچ ضربه‌ای نمی‌تواند به بحرانی‌ترین نقطهٔ پیکرش برسد.",
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
            "شیاسیب دایره‌ای بر خاک کشید و واژه‌ای خواند. هوا غلیظ شد و ضربه‌ها پیش از رسیدن به پیکرش، در میانهٔ راه کند شدند.",
            "طلسمی از دهان شیاسیب بیرون آمد و روی میدان نشست. زمان برای تیغه‌ها کندتر گذشت؛ تا دو دقیقه، هیچ ضربه‌ای بیش از سهمی معین از جانش را نمی‌برد.",
            "سنگ‌ها از جا کنده شدند و دور او چرخیدند. در این دایره، هر ضربه به دیواری از هوا می‌خورد و تنها بخشی از نیرویش را می‌رساند. صبر کنید تا طلسم بشکند.",
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
            "نوری از میان ابرها به میدان افتاد و بوی سیب تازه در هوا پیچید. دست‌های مدافعان سبک‌تر شد و تیغه‌ها درخشان‌تر.",
            "درّه لرزید و صدایی از آسمان آمد که نام مدافعان را می‌خواند. برکت سیب بر زره‌ها نشست و نیروی هر ضربه را افزون کرد.",
            "قطره‌ای نور روی سر نیزه‌ها چکید. از آن لحظه، شمشیرها سنگین‌تر و ضربه‌ها کارسازتر شدند. این برکت دیر نمی‌پاید؛ همین حالا بزنید.",
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
            "شاهینی از بالای برج پر کشید و بر شانهٔ مدافعان نشست. چشم‌ها تیز شدند و نقطه‌های ضعف شیاسیب، مانند شکاف‌های نور در تاریکی پیدا شدند.",
            "آسمان چشمی باز کرد. برای چند لحظه، هر مدافع جای دقیق زخم‌های شیاسیب را دید؛ جایی که زره باز است و تیغه می‌تواند به عمق برسد.",
            "نور سپیده بر تیغه‌ها افتاد و لبه‌هایشان را روشن کرد. حالا هر ضربه می‌تواند به بحرانی‌ترین نقطه برسد؛ فرصت را از دست ندهید.",
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
            "سپیده از پشت کوه‌ها سر زد و میدان را پر کرد. سایهٔ شیاسیب کوتاه شد و مدافعان، برای یک نفس، دو برابر نیرو گرفتند.",
            "خورشید از میان ابرها بیرون آمد و بر سر شیاسیب کوبید. هر ضربه‌ای که در این روشنایی زده شود، دو برابر کار می‌کند؛ اما سپیده به سرعت جای خود را به روز می‌دهد.",
            "نوری طلایی از دل زمین برخاست. دست مدافعان گرم شد و هر ضربه دو برابر شد. نود ثانیه؛ نه یک نفس بیشتر.",
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
            "آسمان ترک خورد و سنگی آتشین از دل آن بیرون آمد. شهاب‌سنگ بر پیکر شیاسیب نشست و زمین تا چند قدم لرزید. گرد و خاک که خوابید، بخشی از جانش رفته بود.",
            "ستاره‌ای از مدار خود بیرون افتاد و راهی میدان شد. برخورد، سکوت را شکست و شیاسیب زیر آتش و سنگ یک قدم عقب رفت.",
            "شبی که همه فکر می‌کردند آسمان آرام است، شهاب‌سنگ آمد. آتش بر شانهٔ شیاسیب نشست و زره‌اش را سفید کرد. این هدیهٔ آسمان را با ضربه‌های خود کامل کنید.",
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
            "نیزه‌ای نور از بالای ابرها فرود آمد و در شکاف زرهٔ شیاسیب نشست. زره ترک برداشت و درزهایش برای مدافعان باز شد.",
            "دستی از آسمان نیزه‌ای پرتاب کرد. نیزه بر سینهٔ شیاسیب نشست و صدای شکستن در تمام درّه پیچید. از آن شکاف، راهی به قلبش باز شد.",
            "نیزهٔ آسمانی از میان ابرها گذشت و بر شانهٔ شیاسیب نشست. تیغه‌ها اکنون جای شکستن را می‌بینند؛ از این درز استفاده کنید.",
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
            "صندوقچه‌ای زرین از دل خاک بیرون آمد و در میدان باز شد. سه مدافع می‌توانند پیش از آنکه سایه آن را ببلعد، سهم خود را بردارند.",
            "زمین ترک خورد و صندوقچه‌ای از دل آن بالا آمد. قفلش خودبه‌خود باز شد؛ سیب‌های طلایی در آن می‌درخشند و تنها سه دست به آن‌ها می‌رسد.",
            "صدای فلزی از زیر خاک آمد. صندوقچهٔ زرین، یادگاری از مدافعان قدیمی، در میدان ظاهر شد. عجله کنید؛ سایه در راه است.",
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
            "کیسهٔ آذوقه‌ای از دست سربازی افتاده بود و کنار دیوار ماند. کیسه باز شد و سیب‌های سرخ در خاک پخش شدند. سه مدافع می‌توانند سهم خود را بردارند.",
            "کاروانی که در تاریکی گم شده بود، کیسه‌ای سیب جا گذاشت. بوی سیب تازه در میدان پیچید و میان دود، جایی برای نفس کشیدن باز کرد.",
            "کیسهٔ سیب از پشت دیوار پیدا شد؛ یادگاری از کاروانی که هرگز نرسید. سیب‌ها سالم مانده‌اند. نخستین سه نفر، سهم خود را بردارند.",
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
            "صندوقچه‌ای کوچک با نوشته‌های نورانی میان سنگ‌ها پیدا شد. درونش پودری می‌درخشد که باغ‌های خفته را بیدار می‌کند؛ سه مدافع می‌توانند آن را بگیرند.",
            "درخششی آبی از زیر خاک بیرون زد. صندوقچهٔ جادو باز شد و پودری درخشان در هوا چرخید. سه نفر می‌توانند پیش از محو شدنش، سهم خود را بردارند.",
            "باد پودری درخشان را از دوردست آورد و در صندوقچه‌ای کهنه نشاند. این پودر، باغ‌های پژمرده را زنده می‌کند. نخستین نفرها، سهم خود را بردارند.",
        ),
        loot=LootSpec(reward_type="magic_powder", label_fa="یک پودر جادویی", default_amount=1),
    ),
)

BY_CODE: dict[str, EventTemplate] = {template.code: template for template in EVENTS}

CATEGORIES: tuple[str, ...] = (BOSS_BUFF, PLAYER_BUFF, INSTANT_DAMAGE, LOOT)


def fa_duration(seconds: int) -> str:
    """«۹۰ ثانیه» / «۳ دقیقه» / «۲ دقیقه و ۳۰ ثانیه» with Persian digits.

    Under two minutes the copy stays in seconds (the narratives say «نود
    ثانیه»), which also keeps short windows readable in a caption.
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
        return f"🛡 آسیب مدافعان ۳۵٪ کمتر شد؛ تا {duration}."
    if code == "boss_shadow_heal":
        return f"💚 شیاسیب ۱۰٪ از جانش را بازگرداند: {fa_digits(amount or 0)} جان."
    if code == "boss_cold_breath":
        return f"🥶 تا {duration}، ضربهٔ بحرانی ممکن نیست."
    if code == "boss_ward":
        return f"⛔ تا {duration}، هیچ ضربه‌ای بیش از ۱۵٪ جان شیاسیب را نمی‌برد."
    if code == "blessing_apple":
        return f"⚔️ آسیب همهٔ مدافعان ۵۰٪ بیشتر شد؛ تا {duration}."
    if code == "blessing_hawk":
        return f"🎯 شانس ضربهٔ بحرانی از ۱۰٪ به ۳۵٪ رسید؛ تا {duration}."
    if code == "blessing_dawn":
        return f"🌟 آسیب مدافعان دو برابر شد؛ تا {duration}."
    if code == "meteor":
        return f"☄️ شهاب‌سنگ {fa_digits(amount or 0)} آسیب زد؛ ۸٪ از جان شیاسیب کم شد."
    if code == "heavenly_spear":
        return (f"🗡 نیزهٔ آسمانی {fa_digits(amount or 0)} آسیب زد؛ "
                f"تا {duration} ضربهٔ بحرانی آسان‌تر شد.")
    if code == "loot_golden_chest":
        return f"🎁 {fa_digits(max_claims or DEFAULT_LOOT_MAX_CLAIMS)} جایزه برای نخستین نفرها؛ یک سیب طلایی برای هر نفر."
    if code == "loot_apple_sack":
        return (f"🍎 {fa_digits(max_claims or DEFAULT_LOOT_MAX_CLAIMS)} جایزه برای نخستین نفرها؛ "
                f"{fa_digits(loot_amount or 0)} سیب برای هر نفر.")
    if code == "loot_magic_chest":
        return f"✨ {fa_digits(max_claims or DEFAULT_LOOT_MAX_CLAIMS)} جایزه برای نخستین نفرها؛ یک پودر جادویی برای هر نفر."
    return ""


def render_loot_status(claimed: int, max_claims: int, winners: Sequence[str] = ()) -> str:
    """«🏅 برداشت‌ها: ۲ از ۳ · آرش، نیلوفر» or the empty/closed wording."""
    total = max(1, int(max_claims))
    claimed = max(0, int(claimed))
    if claimed >= total:
        return f"🏅 برداشت‌ها: {fa_digits(total)} از {fa_digits(total)} · صندوقچه خالی شد."
    line = f"🏅 برداشت‌ها: {fa_digits(claimed)} از {fa_digits(total)}"
    names = [name for name in winners if name]
    if names:
        line += " · " + "، ".join(names)
    return line
