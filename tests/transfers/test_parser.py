"""Unit tests for the transfer command parser (pure Python, no Telegram).

The parser returns the RAW amount: config bounds are validated later by the
service, so e.g. a 5-digit amount parses fine here.
"""

import pytest

from sib.services.transfers.parser import (
    ITEM_APPLE,
    ITEM_DIAMOND,
    ITEM_DISEASE_PLANT,
    ITEM_DISEASE_ANTIDOTE,
    ITEM_GOLD,
    ITEM_OBSIDIAN,
    MAX_RAW_AMOUNT,
    TransferCommand,
    parse_transfer_command,
)


# ---------------------------------------------------------------------------
# Accepted commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "item_type", "amount"),
    [
        # The four type phrases, Latin digits.
        ("انتقال سیب 100", ITEM_APPLE, 100),
        ("انتقال سیب طلایی 10", ITEM_GOLD, 10),
        ("انتقال سیب الماسی 69", ITEM_DIAMOND, 69),
        ("انتقال سیب ابسیدینی 5", ITEM_OBSIDIAN, 5),
        ("انتقال گیاه ویژه 1", ITEM_DISEASE_PLANT, 1),
        ("انتقال پادزهر آماده ۲", ITEM_DISEASE_ANTIDOTE, 2),
        # Persian digits (۰۱۲۳۴۵۶۷۸۹).
        ("انتقال سیب ۱۰۰", ITEM_APPLE, 100),
        ("انتقال سیب طلایی ۱۰", ITEM_GOLD, 10),
        ("انتقال سیب الماسی ۶۹", ITEM_DIAMOND, 69),
        ("انتقال سیب ابسیدینی ۵", ITEM_OBSIDIAN, 5),
        # Single-digit and multi-digit amounts.
        ("انتقال سیب 1", ITEM_APPLE, 1),
        ("انتقال سیب 9999", ITEM_APPLE, 9999),
    ],
)
def test_accepts_all_types_with_latin_and_persian_digits(text, item_type, amount):
    assert parse_transfer_command(text) == TransferCommand(
        item_type=item_type, amount=amount
    )


@pytest.mark.parametrize(
    ("text", "item_type", "amount"),
    [
        # Repeated spaces between every token.
        ("انتقال  سیب  100", ITEM_APPLE, 100),
        ("انتقال  سیب  ابسیدینی  5", ITEM_OBSIDIAN, 5),
        ("انتقال سیب طلایی   10", ITEM_GOLD, 10),
        ("  انتقال سیب ۱۰  ", ITEM_APPLE, 10),
        # Tabs/newlines count as separators too.
        ("انتقال\tسیب\t۲۰", ITEM_APPLE, 20),
        ("انتقال\nسیب الماسی\n7", ITEM_DIAMOND, 7),
    ],
)
def test_tolerates_extra_whitespace(text, item_type, amount):
    assert parse_transfer_command(text) == TransferCommand(
        item_type=item_type, amount=amount
    )


def test_raw_amount_is_not_limited_by_config_bounds():
    # Config default max is 10000 - the parser must not care.
    assert parse_transfer_command("انتقال سیب ۵۰۰۰۰").amount == 50000
    assert parse_transfer_command(f"انتقال سیب {MAX_RAW_AMOUNT}").amount == MAX_RAW_AMOUNT


# ---------------------------------------------------------------------------
# Rejected commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "انتقال سیب",  # missing number
        "انتقال",  # nothing at all
        "انتقال 100",  # missing type phrase
        "سیب 100",  # missing verb
        "انتقال سیب 0",  # zero
        "انتقال سیب ۰۰",  # zero, Persian digits
        "انتقال سیب -5",  # negative
        "انتقال سیب ۱۰۰-",  # trailing sign
        "انتقال سیب +5",  # explicit plus
        "انتقال سیب 10.5",  # non-integer
        "انتقال سیب ۱٫۵",  # Persian decimal separator
        "انتقال سیب 1,000",  # thousands separator
        "انتقال سیب 1e3",  # exponent notation
        "انتقال سیب صد",  # non-numeric word
        "انتقال سیب 100 به علی",  # trailing tokens
        "انتقال سیب طلایی 10 به علی",
        "انتقال سیب 100 200",  # second number
        "انتقال سیب 100 سیب",  # trailing type
        "انتقال سیب طلایی",  # missing number after long phrase
        "انتقال سیب طلایی ۱۰۰ به",
        "انتقال سیب قرمز 100",  # unknown type phrase
        "انتقال سیب طلاییx 10",
        "انتقال سیب طلایی الماسی 10",  # too many type words
        "انتقال سیب 100.",  # trailing punctuation
        "انتقال سیب 100\n10",  # extra line
        "انتقال سیب 10abc",
        "انتقال ۱۰۰ سیب",  # reversed order
        "انتقال سیب ۱ ۰",  # digits split by a space
    ],
)
def test_rejects_everything_else(text):
    assert parse_transfer_command(text) is None


def test_rejects_absurdly_large_amounts():
    # One above the raw ceiling, and a 5000-digit number (guarded before int()).
    assert parse_transfer_command(f"انتقال سیب {MAX_RAW_AMOUNT + 1}") is None
    assert parse_transfer_command("انتقال سیب " + "9" * 5000) is None


def test_rejects_non_string_input():
    assert parse_transfer_command(None) is None
    assert parse_transfer_command(123) is None


def test_type_phrases_are_case_and_spelling_sensitive():
    # Only the exact Persian phrases are accepted; Arabic ي / ك variants and
    # an English transliteration are not.
    assert parse_transfer_command("انتقال سیب علي 10") is None
    assert parse_transfer_command("انتقال apple 10") is None
    assert parse_transfer_command("انتقال سیب طلايی 10") is None
    assert parse_transfer_command("انتقال سیب طلائی 10") is None
