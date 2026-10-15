"""Persian Apple Mine prose with per-user anti-repetition."""

from sib.services.invasion_v2.narrative_pool import pick
from sib.services.sib_claim import fa_digits

NORMAL: list[str] = [
    "چند سیب معمولی از دل سنگ بیرون آمد؛ دست خالی برنگشتی.",
    "چکش به رگه‌ای روشن خورد و سیب‌های معمولی از میان خاک پیدا شدند.",
    "زیر غبار سنگ، سیب‌های سرخ برق زدند و راه بازگشتت روشن شد.",
    "رگه‌ای پنهان شکافت؛ سهم امروزت از معدن پیدا شد.",
    "صدای چکش در تونل پیچید و سیب‌های معمولی از سنگ بیرون لغزیدند.",
    "فانوس لرزید و چند سیب معمولی کنار چرخ‌دستی نمایان شدند.",
]

GOLDEN: list[str] = [
    "نوری طلایی از شکاف سنگ تابید؛ سیب طلایی درخشید.",
    "رگه‌ای کهربایی گشوده شد و سیب‌های طلایی پدیدار شدند.",
    "از دل تاریکی، سیب‌های طلایی چون اخگر روشن شدند.",
    "صدای چکش خاموش شد؛ سیب‌های طلایی در سنگ می‌درخشیدند.",
    "خاک کنار رفت و سیب‌های طلایی، گرم و درخشان، پیدا شدند.",
    "فانوس‌ها در برابر درخشش سیب‌های طلایی کم‌نور شدند.",
]

DIAMOND: list[str] = [
    "سیبی الماسی، سرد و درخشان، از معدن بیرون آمد.",
    "در دل سنگ، سیب الماسی چون ستاره‌ای خاموش می‌درخشید.",
    "چکش به بلور پنهان رسید؛ سیب الماسی نور فانوس را بازتاب داد.",
    "غبار کنار رفت و سیبی الماسی، شفاف و سرد، پیدا شد.",
    "رگهٔ بلورین شکافت و سیب الماسی در کف دستت درخشید.",
    "نور آبی از ژرفای معدن برخاست؛ سیب الماسی آنجا بود.",
]

OBSIDIAN: list[str] = [
    "تاریکی سنگ شکست و سیبی ابسیدینی بیرون افتاد.",
    "از رگه‌ای سیاه، سیب ابسیدینی با درخششی آرام نمایان شد.",
    "سنگ تیره کنار رفت؛ سیب ابسیدینی زیر نور کهربایی برق زد.",
    "در ژرفای معدن، سیب ابسیدینی چون شبی پرستاره می‌درخشید.",
    "چکش به سنگ سیاه خورد و سیب ابسیدینی آشکار شد.",
    "سکوت تونل شکست؛ سیب ابسیدینی از دل سنگ پیدا شد.",
]

COOLDOWN: list[str] = [
    "⛏ دیوارهای معدن هنوز خاموش‌اند. {mm} دقیقه و {ss} ثانیه دیگر برگرد.",
    "⛏ دیوارهای معدن هنوز خاموش‌اند. {mm} دقیقه و {ss} ثانیه دیگر برگرد؛ صدای چکش هنوز دور است.",
    "⛏ دیوارهای معدن هنوز خاموش‌اند. {mm} دقیقه و {ss} ثانیه دیگر برگرد؛ تونل‌ها به سکوت نیاز دارند.",
    "⛏ دیوارهای معدن هنوز خاموش‌اند. {mm} دقیقه و {ss} ثانیه دیگر برگرد؛ بگذار فانوس‌ها دوباره روشن شوند.",
    "⛏ دیوارهای معدن هنوز خاموش‌اند. {mm} دقیقه و {ss} ثانیه دیگر برگرد؛ رگه‌ها هنوز آرام نگرفته‌اند.",
    "⛏ دیوارهای معدن هنوز خاموش‌اند. {mm} دقیقه و {ss} ثانیه دیگر برگرد؛ کارت بعدی را در انتظار بگذار.",
]

_POOLS = {
    "normal": NORMAL,
    "golden": GOLDEN,
    "diamond": DIAMOND,
    "obsidian": OBSIDIAN,
    "cooldown": COOLDOWN,
}


def pick_narrative(pool_name: str, user_id: int) -> str:
    """Pick prose without repeating the last five lines for this user."""
    try:
        pool = _POOLS[pool_name]
    except KeyError as exc:
        raise ValueError(f"unknown mine narrative pool: {pool_name}") from exc
    return pick(pool, key=f"mine:{user_id}", history_size=5)


def cooldown_message(remaining_seconds: int, user_id: int) -> str:
    """Format a picked Persian cooldown line using Persian digits."""
    minutes, seconds = divmod(max(0, int(remaining_seconds)), 60)
    return pick_narrative("cooldown", user_id).format(
        mm=fa_digits(minutes),
        ss=fa_digits(seconds),
    )
