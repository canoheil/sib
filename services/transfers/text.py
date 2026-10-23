"""All Persian, user-facing text for user-to-user transfers (Phase 2).

This is the single source of truth for the transfer UX strings: reason
messages, item-type display names/emojis, the confirmation body, success,
cancel, expiry and the receiver notification. Handlers must never inline a
Persian string; they call the ``render_*`` helpers here.

Numbers are rendered with the project's existing Persian-digit helper
(``services.sib_claim.fa_digits``), exposed here as
:func:`to_persian_digits`. The codebase has no thousands-separator
convention, so plain Persian digits are used.
"""

from sib.core.text import safe_name
from sib.services.sib_claim import fa_digits

#: Seconds a confirmation stays valid (the pending row's TTL).
PENDING_TTL_SECONDS = 60

#: ``item_type`` -> (Persian name, emoji). The four transferable currencies;
#: ``apple`` is the regular-Apple wallet currency, the rest are inventory
#: items (``user_items.item_key``).
TYPE_DISPLAY: dict[str, tuple[str, str]] = {
    "apple": ("سیب", "🍎"),
    "apple_gold": ("سیب طلایی", "🌟"),
    "apple_diamond": ("سیب الماسی", "💎"),
    "apple_obsidian": ("سیب ابسیدینی", "⬛"),
}

#: Service reason code -> Persian message. ``amount_out_of_bounds`` carries
#: the configured bounds as ``{min}`` / ``{max}`` placeholders.
REASON_MESSAGES: dict[str, str] = {
    "disabled": "انتقال موقتاً غیرفعال است.",
    "amount_out_of_bounds": "مقدار باید بین {min} و {max} باشد.",
    "self": "نمی‌توانید به خودتان انتقال دهید.",
    "user_not_found": "کاربر مقصد پیدا نشد.",
    "banned": "یکی از طرفین مسدود است.",
    "bot": "انتقال به بات ممکن نیست.",
    "invalid_item_type": "نوع سیب نامعتبر است.",
    "invalid_amount": "مقدار باید یک عدد صحیح مثبت باشد.",
    "cooldown": "لطفاً کمی صبر کنید و دوباره تلاش کنید.",
    "daily_limit": "سقف انتقال روزانه شما تکمیل شده است.",
    "insufficient": "موجودی کافی نیست.",
    "unknown": "انتقال انجام نشد. لطفاً بعداً تلاش کنید.",
}

CONFIRM_TITLE = "⚠️ تأیید انتقال"
CONFIRM_BUTTON_CONFIRM = "✅ بله، انتقال بده"
CONFIRM_BUTTON_CANCEL = "❌ انصراف"

SUCCESS_TOAST = "✅ انتقال انجام شد."
CANCELLED_TEXT = "❌ انتقال لغو شد."
CANCELLED_TOAST = "لغو شد."
EXPIRED_TEXT = "⏳ این تأیید منقضی شده است."
ALREADY_PROCESSED_TEXT = "قبلاً پردازش شده است."
NOT_YOUR_BUTTON_TEXT = "این دکمه برای شما نیست."
INVALID_REQUEST_TEXT = "این درخواست معتبر نیست."


def to_persian_digits(value: object) -> str:
    """Latin digits -> Persian digits (the project's existing helper)."""
    return fa_digits(value)


def display_name(first_name: object, username: object = None, fallback: str = "بازیکن") -> str:
    """Markdown-safe player display: first name + optional ``@username``."""
    name = safe_name(first_name if isinstance(first_name, str) else None, fallback)
    if isinstance(username, str) and username.strip():
        return f"{name} (@{safe_name(username.strip().lstrip('@'), '')})"
    return name


def render_type(item_type: str) -> tuple[str, str]:
    """``(Persian name, emoji)`` for an item type; unknown keys show the key."""
    return TYPE_DISPLAY.get(item_type, (item_type, "🍎"))


def render_reason(
    code: str | None,
    *,
    min_amount: int | None = None,
    max_amount: int | None = None,
) -> str:
    """Persian text for a service reason code (``unknown`` for anything odd)."""
    template = REASON_MESSAGES.get(code or "", REASON_MESSAGES["unknown"])
    if "{min}" in template or "{max}" in template:
        return template.format(
            min=to_persian_digits(min_amount if min_amount is not None else 1),
            max=to_persian_digits(max_amount if max_amount is not None else 1),
        )
    return template


def render_confirmation(
    *,
    from_display: str,
    to_display: str,
    item_type: str,
    amount: int,
    fee: int = 0,
    ttl_seconds: int = PENDING_TTL_SECONDS,
) -> str:
    """The confirmation body shown to the sender before the transfer."""
    name, emoji = render_type(item_type)
    lines = [
        CONFIRM_TITLE,
        f"از: {from_display}",
        f"به: {to_display}",
        f"مقدار: {to_persian_digits(amount)} {emoji} {name}",
    ]
    if fee > 0:
        lines.append(f"کارمزد: {to_persian_digits(fee)} {name}")
        lines.append(
            f"مجموع کسر از شما: {to_persian_digits(amount + fee)} {name}"
        )
    lines.append(
        f"⏳ این تأیید تا {to_persian_digits(ttl_seconds)} ثانیه دیگر منقضی می‌شود."
    )
    return "\n".join(lines)


def render_success(
    *,
    to_display: str,
    item_type: str,
    amount: int,
    fee: int = 0,
) -> str:
    """The edit shown on the confirmation message after a successful transfer."""
    name, _emoji = render_type(item_type)
    text = f"✅ {to_persian_digits(amount)} {name} به {to_display} منتقل شد."
    if fee > 0:
        text += f"\nکارمزد: {to_persian_digits(fee)} {name}"
    return text


def render_failure(reason: str | None, *, min_amount: int | None = None, max_amount: int | None = None) -> str:
    """The edit shown on the confirmation message after a refused transfer."""
    return f"❌ {render_reason(reason, min_amount=min_amount, max_amount=max_amount)}"


def render_expired(original_text: str) -> str:
    """The confirmation body, marked as expired."""
    return f"{original_text}\n\n{EXPIRED_TEXT}"


def render_receiver_notification(
    *,
    from_display: str,
    item_type: str,
    amount: int,
    to_mention: str | None = None,
) -> str:
    """The message the receiver gets in the group after a successful transfer.

    ``to_mention`` is a ready-made ``[name](tg://user?id=...)`` link. The
    notification cannot be a *reply to the receiver's own message* (that id
    is not persisted - see the phase report), so the mention is what makes
    Telegram notify them.
    """
    name, _emoji = render_type(item_type)
    text = (
        f"📥 {from_display} مبلغ {to_persian_digits(amount)} {name} "
        "به شما منتقل کرد."
    )
    return f"{to_mention} {text}" if to_mention else text
