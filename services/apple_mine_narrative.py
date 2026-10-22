"""Persian Apple Mine prose with per-user anti-repetition."""

from sib.services.invasion_v2.narrative_pool import pick
from sib.services.sib_claim import fa_digits

NORMAL: list[str] = [
    "⛏ کلنگ زدی و **رگهٔ سیب** رو پیدا کردی.",
    "⛏ **رگهٔ تازه** باز کردی.",
    "🍎 سنگ شکست و **سیب معمولی** در اومد.",
    "⛏ یه **ضربهٔ دقیق** به رگه زدی.",
    "🍎 **رگهٔ سرخ** از زیر خاک بیرون اومد.",
    "⛏ **کلنگ** این بار درست به هدف خورد.",
]

GOLDEN: list[str] = [
    "✨ یه **درخشش طلایی** توی رگه دیدی.",
    "⛏ کلنگ به **رگهٔ طلایی** خورد.",
    "🌕 معدن یه لحظه **طلایی** شد.",
    "✨ **نور طلایی** از شکاف سنگ اومد.",
    "⛏ این ضربه **رگهٔ طلایی** رو باز کرد.",
    "🌕 **سیب طلایی** زیر دستت درخشید.",
]

DIAMOND: list[str] = [
    "💎 یه **درخشش بلورین** دیدی.",
    "⛏ کلنگ به **بلور** خورد.",
    "💎 **رگهٔ الماسی** باز شد.",
    "✨ معدن با **نور الماس** روشن شد.",
    "⛏ این ضربه **بلور پنهان** رو پیدا کرد.",
    "💎 **سیب الماسی** از دل سنگ برق زد.",
]

OBSIDIAN: list[str] = [
    "🌑 **رگهٔ سیاه** باز شد.",
    "⛏ کلنگ به **رگهٔ ابسیدینی** خورد.",
    "🌑 **سیب ابسیدینی** از تاریکی بیرون اومد.",
    "⚡ یه ضربه، **رگهٔ سیاه** رو شکست.",
    "⛏ **کلنگ** این بار رگهٔ کمیاب رو پیدا کرد.",
    "🌑 معدن **سیب ابسیدینی** رو نشونت داد.",
]

COOLDOWN: list[str] = [
    "⛏ شیفت بعدی: **{mm} دقیقه و {ss} ثانیه** دیگه.",
    "⏳ معدن هنوز آماده نیست؛ **{mm} دقیقه و {ss} ثانیه** صبر کن.",
    "⛏ کلنگ هنوز آماده نیست؛ **{mm} دقیقه و {ss} ثانیه** دیگه بیا.",
    "⏳ معدن هنوز شارژ نشده؛ **{mm} دقیقه و {ss} ثانیه** صبر کن.",
    "⛏ بین هر شیفت یه وقفه‌ست؛ **{mm} دقیقه و {ss} ثانیه** دیگه برگرد.",
    "⏳ **{mm} دقیقه و {ss} ثانیه** دیگه، کلنگ آماده‌ست.",
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
