"""Short, modern Persian invasion prose with stable placeholders.

Pool sizes and each variant's placeholder set stay compatible with the frozen
render APIs. Join and finishing text use only their available values; counted
victory variants remain separate for participant-count selection.
"""

import random
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock


ANNOUNCEMENT: list[str] = [
    "**{region}** زیر حمله‌ست! شیاسیب رسیده؛ برای دفاع آماده‌اید؟",
    "شیاسیب به **{region}** رسید! بیاید دفاع، این یورش رو عقب بندازیم.",
    "زنگ خطر توی **{region}** به صدا دراومد! شیاسیب نزدیکه، جمع شوید.",
    "یورش به **{region}** رسید! مدافعا، سریع برید سراغ کارت‌هاتون.",
    "**{region}** به دفاع شما نیاز داره! شیاسیب رو قبل از ورودش نگه دارید.",
    "**{enemy_name}** راه افتاده به سمت **{region}**! برای یه دفاع حسابی جمع شوید.",
    "**{enemy_name}** به **{region}** حمله کرد! همه پای کار، نباید جلوتر بره.",
    "راه **{region}** رو ببندید! **{enemy_name}** داره نزدیک میشه.",
    "حمله به **{region}** شروع شد! با کارت‌هاتون بزنید و نگذارید شیاسیب رد بشه.",
    "**{enemy_name}** از راه برف به **{region}** رسید! مدافعا، نوبت شماست.",
    "آلارم یورش توی **{region}** روشنه! هر ضربه‌تون مهمه، بزنید.",
    "هنوز برای دفاع از **{region}** وقت داریم! شیاسیب رو با هم عقب بفرستیم.",
]

JOIN_NORMAL: list[str] = [
    "**{user}** با **{card}** ضربه زد! شیاسیب یه قدم عقب رفت.",
    "**{card}** دست **{user}** حرف زد! **{damage} آسیب** به شیاسیب رسید.",
    "**{user}** مستقیم رفت رو به شیاسیب و **{card}** رو به کار گرفت!",
    "دفاع رو **{user}** و **{card}** سفت کردن! شیاسیب از اینجا رد نمیشه.",
    "روی پل، **{user}** با **{card}** جلوی موج حمله رو گرفت!",
    "با **{card}**، **{user}** **{damage} آسیب** زد! جان باقی‌مانده شیاسیب: **{hp_remaining}**.",
    "یه ضربه تمیز از **{user}** و **{card}**! **{damage} آسیب** رفت حساب شیاسیب؛ **{hp_remaining}** جان مونده.",
    "مسیر رو **{user}** با **{card}** بست! شیاسیب مجبور شد برگرده.",
    "ضربه **{user}** با **{card}** دقیق به سپر شیاسیب خورد! حمله‌ش کند شد.",
    "**{card}** توی دست **{user}** برق زد! **{damage} آسیب** کافی بود برای عقب‌نشینی شیاسیب.",
    "بین شیاسیب و دروازه، **{user}** با **{card}** ایستاد! دیگه رد نمیشه.",
    "از بالای پله‌ها، **{user}** و **{card}** وارد عمل شدن! شیاسیب جایی برای رد شدن نداشت.",
    "یه فرصت کوچیک کافی بود! **{user}** با **{card}** ضربه رو دقیق زد.",
    "راه پناهگاه امن موند؛ **{user}** با **{card}** زد و **{damage} آسیب** رسوند!",
    "چراغ‌ها هنوز روشنه! **{user}** با **{card}** دفاع کرد؛ شیاسیب **{hp_remaining}** جان داره.",
    "کنار ارابه شکسته، **{user}** و **{card}** مسیر شیاسیب رو بستن!",
    "پرچم بالا موند! **{user}** با **{card}** شیاسیب رو عقب زد.",
    "نوبت **{user}** بود؛ **{card}** **{damage} آسیب** زد و **{hp_remaining}** جان برای شیاسیب موند!",
    "**{user}** و **{card}** حمله رو بردن به میدان باز! پشت سرشون همه امن موندن.",
    "ضربه محکم **{user}** با **{card}** رسید! شیاسیب دیگه نتونست جلو بیاد.",
    "شیاسیب از پهلو نزدیک شد، اما **{user}** با **{card}** جلوش رو گرفت و **{damage} آسیب** زد!",
    "**{user}** و **{card}** کنار هم موندن! حمله روی دفاعشون خورد و شیاسیب عقب موند.",
]

JOIN_CRIT: list[str] = [
    "**{user}** با **{card}** یه کریتیکال حسابی زد! کل صف شیاسیب لرزید.",
    "**{card}** رو **{user}** بلند کرد و کریتیکال زد! شیاسیب عقب رفت.",
    "انفجار کریتیکال از **{card}** و **{user}** بود! شیاسیب مستقیم رفت عقب.",
    "**{card}** توی دست **{user}** برق گرفت! یه کریتیکال و صف شیاسیب از دروازه عقب رفت.",
    "سپر دفاعی شیاسیب شکست! **{user}** با **{card}** کریتیکال زد و راه رو باز کرد.",
    "با **{card}**، **{user}** کریتیکال زد و **{damage} آسیب** رسوند! جان شیاسیب: **{hp_remaining}**.",
    "اول سنگ‌ها لرزیدن، بعد کریتیکال **{user}** با **{card}** اومد! دفاعمون نفس کشید.",
    "صدای کریتیکال توی تنگه پیچید! **{user}** با **{card}** صف شیاسیب رو شکافت.",
    "**{user}** و **{card}** یه کریتیکال سنگین زدن! **{damage} آسیب** رفت و **{hp_remaining}** جان موند.",
    "یه لحظه سکوت، بعد **{card}** توی دست **{user}** جرقه زد! کریتیکال دفاع شیاسیب رو باز کرد.",
    "زنگ برج هنوز می‌لرزید که **{user}** با **{card}** کریتیکال زد! دروازه نجات پیدا کرد.",
    "از روی خاکریز، **{user}** با **{card}** کریتیکال فرستاد! ضربه به جای دفاع ما، صف شیاسیب رو عقب برد.",
    "ضعف شیاسیب رو **{user}** با **{card}** پیدا کرد! کریتیکال با **{damage} آسیب** نشست.",
    "آخرین سپرها داشتن خم می‌شدن که **{user}** و **{card}** کریتیکال زدن! همه دوباره سر جاشون برگشتن.",
    "**{card}** کنار **{user}** نورانی شد و کریتیکال زد! فقط **{hp_remaining}** جان برای شیاسیب مونده.",
    "شیاسیب هنوز رد نشده بود که **{user}** با **{card}** کریتیکال زد! راه همون‌جا بسته موند.",
]

JOIN_BUFF: list[str] = [
    "💥 **{user}** با **سیب باف** و **{card}** زد! **۲ برابر** قدرت یعنی **{damage} آسیب**.",
    "💥 **سیب باف** **{card}** رو شارژ کرد! **{user}** **{damage} آسیب** زد، یعنی **۲ برابر** ضربه معمولی.",
    "💥 **{user}** **سیب باف** رو فعال کرد و **{card}** رو زد! آسیب **۲ برابر** شد: **{damage}**.",
    "💥 **سیب باف** توی دست **{user}** روشن شد! **{card}** ضربه **۲ برابر** زد و **{damage} آسیب** رسوند.",
    "💥 **{user}** و **{card}** با **سیب باف** وارد شدن! این ضربه **۲ برابر** خورد: **{damage} آسیب**.",
    "💥 سپر شیاسیب فایده نداشت! **{user}** با **سیب باف** و **{card}** **{damage} آسیب** زد؛ ضربه **۲ برابر** بود.",
]

JOIN_KILL: list[str] = [
    "**{user}** ضربه آخر رو با **{card}** زد! شیاسیب تموم کرد.",
    "آخرین ضربه مال **{user}** بود! شیاسیب افتاد و میدان آروم شد.",
    "**{user}** **{card}** رو محکم زد و کار شیاسیب رو تموم کرد!",
    "کنار دروازه، **{user}** با **{card}** آخرین حمله رو دفعه کرد! راه باز شد.",
    "فقط یه ضربه مونده بود! **{user}** با **{card}** زد و شیاسیب افتاد.",
    "**{user}** با **{card}** آخرین فشار شیاسیب رو برگردوند! این بار دیگه برنگشت.",
    "**{user}** و **{card}** رفتن سراغ آخرین صف! ضربه آخر اومد و یورش تموم شد.",
    "صدای **{card}** روی آخرین سپر پیچید! **{user}** کار شیاسیب رو همون‌جا تموم کرد.",
    "راه برگشت رو **{user}** با **{card}** بست و ضربه آخر رو زد! خطر تموم شد.",
    "این سکوت یعنی بردیم! **{user}** با **{card}** آخرین موج شیاسیب رو از بین برد.",
]

VICTORY: list[str] = [
    "شیاسیب عقب رفت و **{region}** موند! بردیم.",
    "یورش تموم شد و **{region}** سرپاست! حالا وقت جشنه.",
    "شیاسیب فرار کرد، ما موندیم! **{region}** رو نگه داشتیم.",
    "درهای **{region}** دوباره باز شد! مدافعا هنوز پای کارن.",
    "صبح رسید و **{region}** هنوز مال ماست! از شیاسیب خبری نیست.",
    "**{user}** و **{card}** کنار مردم **{region}** موندن! **{enemy_name}** شکست خورد و زندگی برگشت.",
    "**{region}** رو نگه داشتیم! شیاسیب عقب زد.",
    "**{user}** با **{card}** تا آخرین لحظه موند! **{region}** امنه و همه برگشتن.",
    "روی برج **{region}** پرچم خودمون بالاست! شیاسیب از افق محو شد.",
    "صدای چکش توی **{region}** برگشته! این بار برای تعمیر، نه نبرد.",
    "امشب چراغ‌های **{region}** روشن می‌مونه! **{user}** و **{card}** جزو کسایی بودن که نگذاشتن خاموش بشه.",
    "راه کاروان‌ها به **{region}** باز شد! شیاسیب تا همین‌جا اومد و جلوتر نرفت.",
    "**{n} مدافع** کنار هم ایستادن و **{region}** رو نگه داشتن! شیاسیب برگشت.",
    "تا صبح، **{n} نگهبان** از **{region}** دفاع کردن! حالا مردم از همون دروازه رد می‌شن.",
    "اسم **{n} مدافع** توی **{region}** می‌پیچه! اونا شیاسیب رو عقب فرستادن.",
    "بچه‌های **{region}** به خانه برگشتن! **{n} مدافع** تا آخرین موج جلوی دروازه موندن.",
    "درهای **{region}** باز شد و **{n} نگهبان** هنوز اونجا بودن! آخرین نفر رو هم رسوندن.",
    "زنگ پیروزی توی **{region}** به صدا دراومد! **{n} مدافع** نگذاشتن ترس وارد شهر بشه.",
]

DEFEAT: list[str] = [
    "**{region}** از دست رفت و شیاسیب اونجا موند! اواریا این شکست رو فراموش نمی‌کنه.",
    "**{region}** سقوط کرد! برمی‌گردیم، ولی این بار سخت‌تره.",
    "شیاسیب کنترل **{region}** رو گرفت! بازمانده‌ها رفتن کوه، ولی ما هنوز اینجاییم.",
    "پرچم **{region}** پایین اومد! مدافعا هنوز برای برگشتن آماده‌ان.",
    "امشب **{region}** توی دست شیاسیبه! اواریا اسمش رو فراموش نمی‌کنه.",
    "**{user}** و **{card}** مردم رو از **{region}** بیرون بردن! **{enemy_name}** شهر رو گرفت، ولی همه امن شدن.",
    "زنگ دیده‌بانی **{region}** خاموش شد! مدافعا بازمانده‌ها رو جمع کردن تا برن برای پس گرفتنش.",
    "دروازه دووم نیاورد! **{user}** و **{card}** مردم **{region}** رو از راه فرعی رسوندن بیرون.",
    "فقط برج‌های خاموش **{region}** دیده می‌شن! مدافعا هنوز نقشه برگشتن رو دارن.",
    "پرچم شیاسیب بالای **{region}**ه! توی پناهگاه‌ها همه دارن برای پس گرفتنش آماده می‌شن.",
    "مردم رو **{user}** و **{card}** از **{region}** به جاده کوه رسوندن! حالا نوبت آماده شدن برای برگشته.",
    "مردم **{region}** هنوز به برگشتن فکر می‌کنن! شیاسیب فقط شهر رو گرفته.",
]

EXPIRED: list[str] = [
    "یورش بی‌نتیجه تموم شد! سایه‌ها عقب رفتن، ولی **هیچ بردی ثبت نشد**.",
    "غبار نشست و شیاسیب غایب شد! **{region}** سرپاست، ولی این یورش **بی‌نتیجه** موند.",
    "شیاسیب ناپدید شد و نبرد **بی‌نتیجه** موند! این پیروزی حساب نمیشه.",
    "فراخوان به پایان رسید و نتیجه‌ای نگرفتیم! **یورش بی‌نتیجه موند**.",
    "مه کنار رفت و وقت نبرد تموم شد! **نه بردیم، نه باختیم**.",
    "وقت نبرد توی **{region}** تموم شد! **{user}** و **{card}** موندن، ولی **نتیجه‌ای ثبت نشد**.",
    "وقت نبرد با **{enemy_name}** توی **{region}** تموم شد! **نتیجه‌ای ثبت نشد**.",
    "فرصت پاسخ تموم شد و پایان نبرد مشخص نیست! **یورش بی‌نتیجه اعلام می‌شه**.",
]


# Recent selection is process-local. Store only the latest use of each distinct
# template, not an ever-growing list of picks. This also supports callers changing
# history_size: a template's last position answers any look-back window exactly.

@dataclass
class _History:
    position: int = 0
    last_used: dict[str, int] = field(default_factory=dict)


_history: dict[str, _History] = {}
_lock = Lock()
_rng: ContextVar[random.Random | None] = ContextVar("inv2_narrative_rng", default=None)


@contextmanager
def _using_rng(rng: random.Random):
    """Let the unchanged render API supply randomness without global seeding."""
    token = _rng.set(rng)
    try:
        yield
    finally:
        _rng.reset(token)


def pick(pool: list[str], *, key: str, history_size: int = 5) -> str:
    """Pick atomically, avoiding the last N selections for this key when possible.

    Repeated entries count as one variant. A pool with at most N distinct entries
    cannot sustain a strict N-pick exclusion, so that constraint is skipped for
    that call. Zero disables exclusion; negative sizes and empty pools are errors.
    History is shared across pools when the caller deliberately shares a key.
    """
    if history_size < 0:
        raise ValueError("history_size must be nonnegative")
    variants = list(dict.fromkeys(pool))
    if not variants:
        raise ValueError("Cannot pick from an empty narrative pool")
    with _lock:
        history = _history.setdefault(key, _History())
        choices = variants
        if len(variants) > history_size:
            cutoff = history.position - history_size
            choices = [text for text in variants
                       if text not in history.last_used or history.last_used[text] < cutoff]
        # More than N distinct variants guarantees at least one eligible entry.
        selected = (_rng.get() or random).choice(choices)
        history.last_used[selected] = history.position
        history.position += 1
        return selected
