"""Parser for the user-to-user transfer command (pure, no Telegram).

Accepted grammar (Persian, case-sensitive phrases; Latin or Persian digits):

    انتقال <type phrase> <amount>

    انتقال سیب 100
    انتقال سیب طلایی 10
    انتقال سیب الماسی ۶۹
    انتقال  سیب  ابسیدینی  5
    انتقال گیاه ویژه 1
    انتقال پادزهر آماده ۱

* ``انتقال`` must be the first token; the amount must be the LAST token.
  Anything after it (``انتقال سیب 100 به علی``) is rejected - no trailing
  tokens, no captions, no reply-target syntax.
* Any run of spaces/tabs/newlines between tokens is treated as one separator.
* ``amount`` is a plain positive integer. Signs, decimals, thousands
  separators, exponent notation and non-digits are rejected, as are zero and
  absurdly large values (``MAX_RAW_AMOUNT``).

The parser deliberately knows NOTHING about the config limits
(``transfer_min_amount`` / ``transfer_max_amount``): it returns the raw
amount and the service validates it. Phase 2 formats all user-facing text;
this module contains no messages.
"""

import re
from dataclasses import dataclass
from typing import Optional

# Canonical ``item_type`` values (the keys stored in transfer_log.item_type).
# ``apple`` is the normal Apple CURRENCY (apple_wallets.balance); the others
# are real inventory items (user_items.item_key).
ITEM_APPLE = "apple"
ITEM_GOLD = "apple_gold"
ITEM_DIAMOND = "apple_diamond"
ITEM_OBSIDIAN = "apple_obsidian"
ITEM_DISEASE_PLANT = "disease_plant"
ITEM_DISEASE_ANTIDOTE = "disease_antidote"

TRANSFER_ITEM_TYPES: tuple[str, ...] = (
    ITEM_APPLE,
    ITEM_GOLD,
    ITEM_DIAMOND,
    ITEM_OBSIDIAN,
    ITEM_DISEASE_PLANT,
    ITEM_DISEASE_ANTIDOTE,
)

# Persian (Extended Arabic-Indic) digits -> Latin. Only the Persian set is
# translated: these are the digits Persian keyboards produce.
PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
LATIN_DIGITS = "0123456789"
_DIGIT_TRANSLATION = str.maketrans(PERSIAN_DIGITS, LATIN_DIGITS)

# Type phrase (already whitespace-normalized) -> item_type. Longest phrases
# first only for readability; the regex alternation below handles matching.
TYPE_PHRASES: dict[str, str] = {
    "سیب": ITEM_APPLE,
    "سیب طلایی": ITEM_GOLD,
    "سیب الماسی": ITEM_DIAMOND,
    "سیب ابسیدینی": ITEM_OBSIDIAN,
    "گیاه ویژه": ITEM_DISEASE_PLANT,
    "پادزهر آماده": ITEM_DISEASE_ANTIDOTE,
}

# The whole command, anchored on both ends. ``[0-9]+`` (post-translation)
# excludes signs, decimals and separators. ``\s+`` allows single or repeated
# spaces between every token.
_TRANSFER_RE = re.compile(
    r"^انتقال\s+(?P<phrase>سیب(?:\s+(?:ابسیدینی|الماسی|طلایی))?|گیاه\s+ویژه|پادزهر\s+آماده)"
    r"\s+(?P<amount>[0-9]+)$"
)

# Sanity ceiling for the RAW amount, so a 5000-digit message cannot create a
# huge Python int. Real limits live in config (transfer_max_amount).
MAX_RAW_AMOUNT = 1_000_000_000
_MAX_AMOUNT_DIGITS = len(str(MAX_RAW_AMOUNT))


@dataclass(frozen=True)
class TransferCommand:
    """A successfully parsed transfer command (raw, unvalidated amount)."""

    item_type: str
    amount: int


def _normalize(text: str) -> str:
    """Latin digits + collapse every whitespace run to a single space."""
    translated = text.translate(_DIGIT_TRANSLATION)
    return " ".join(translated.split())


def parse_transfer_command(text: str) -> Optional[TransferCommand]:
    """Parse ``انتقال <type> <amount>``; ``None`` for anything else.

    Pure and side-effect free: no config, no database, no Telegram.
    """
    if not isinstance(text, str):
        return None

    match = _TRANSFER_RE.match(_normalize(text))
    if match is None:
        return None

    item_type = TYPE_PHRASES.get(match.group("phrase"))
    if item_type is None:  # defensive; the regex only matches known phrases
        return None

    digits = match.group("amount")
    if len(digits) > _MAX_AMOUNT_DIGITS:
        return None

    amount = int(digits)
    if amount <= 0 or amount > MAX_RAW_AMOUNT:
        return None

    return TransferCommand(item_type=item_type, amount=amount)
