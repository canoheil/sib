"""Narrative pool for the «سیب» wake-word claim (Phase N7).

Every claim now pays a RANDOM reward (see ``sib_claim.roll_claim_amount``)
and is answered with a fresh, short, modern Persian line from this pool.
Each template carries a single ``{amount}`` placeholder; it is always
filled with Persian digits via the shared ``fa_digits`` helper from
``sib_claim`` (imported lazily to avoid a module import cycle).
"""

import random

#: One short line each: an emoji, the bolded amount, a modern casual voice.
CLAIMS: list[str] = [
    "🍏 **{amount} سیب** به حسابت اضافه شد!",
    "🍎 یه سبد **{amount} تایی سیب** گرفتی!",
    "✨ درخت سیب برات **{amount} سیب** انداخت!",
    "🧺 **{amount} تا سیب** تازه توی سبدت نشست!",
    "🍏 **{amount} سیب** برداشت شد و به حسابت زده شد!",
    "🌟 امروز روز برداشته: **{amount} سیب** مال تو!",
    "🍎 سیب‌های امروز: **{amount} تا**، همه مال تو!",
    "✨ **{amount} سیب** تازه از درخت، مستقیم به حسابت!",
    "🧺 برداشت امروزت ثبت شد: **{amount} سیب**!",
    "🌳 درخت خوش‌برکت بود: **{amount} سیب** گرفتی!",
    "🍏 **{amount} سیب** به موجودی سیبات اضافه شد!",
    "🤝 **{amount} تا سیب** به مهربونیت تقدیم!",
]


def pick_claim_narrative(amount: int) -> str:
    """Pick ONE random claim line and fill its amount in Persian digits."""
    from sib.services.sib_claim import fa_digits  # lazy: avoid import cycle

    return random.choice(CLAIMS).format(amount=fa_digits(amount))
