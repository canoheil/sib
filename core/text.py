"""Utilities for building safe Telegram message text.

Two rendering targets exist and they need different treatment:

* **Messages and captions** are sent with ``parse_mode="HTML"``. Narrative
  sources are written with the lightweight ``**bold**`` marker, so they go
  through :func:`format_html`, which HTML-escapes the raw text first (so a
  ``<`` or ``&`` typed by a player can never produce an invalid entity and
  crash the send with ``can't parse entities``) and only then turns
  ``**bold**`` into ``<b>bold</b>``.
* **Callback alerts** (``callback.answer``) support NO parse mode at all:
  Telegram shows the text verbatim, so any ``**`` would appear literally.
  Those go through :func:`strip_bold`, which deletes the markers.

The legacy-Markdown helpers (:func:`escape_markdown`, :func:`safe_name`)
are still used by text that has not been migrated yet. Markdown escaping
is *inert* under HTML, so :func:`format_html` also removes that escaping
layer (``\\_`` renders as ``_``, not as a stray backslash) before applying
the HTML escaping.

Bidi: both name helpers fence the name with :func:`sib.core.bidi.wrap_ltr`,
so a Latin/stylized name inside a Persian sentence cannot reorder the words
or their punctuation around it. The paragraph direction of a message that
opens with an emoji or an English word is pinned separately, at the point
where the final string is sent, with :func:`sib.core.bidi.fix_bidi`.
"""

import re
from typing import Optional

from sib.core.bidi import wrap_ltr

# Characters that are active in Telegram's legacy Markdown parse mode.
_MARKDOWN_SPECIALS = ("\\", "_", "*", "`", "[")


def escape_markdown(text: Optional[str]) -> str:
    """Escape legacy-Markdown special characters in untrusted text.

    A backslash is escaped first so the remaining escapes stay literal.
    ``None`` is rendered as an empty string.
    """
    if not text:
        return ""
    result = text.replace("\\", "\\\\")
    for ch in _MARKDOWN_SPECIALS[1:]:
        result = result.replace(ch, f"\\{ch}")
    return result


def safe_name(name: Optional[str], fallback: str = "بازیکن") -> str:
    """Return a Markdown-safe display name with a friendly fallback.

    The name is fenced with ``LRM`` (see :func:`sib.core.bidi.wrap_ltr`):
    ``Ali`` must read as one left-to-right block inside the Persian
    sentence, otherwise the ``!``/``()`` around it jump to the wrong side.
    The marks are invisible and inert in both Markdown and HTML.

    >>> safe_name("Ali")
    '\u200eAli\u200e'
    """
    cleaned = (name or "").strip()
    if not cleaned:
        cleaned = fallback
    return escape_markdown(wrap_ltr(cleaned))


# HTML tags, link schemes and Telegram @handles that must never reach a
# public, player-facing name (see :func:`plain_display_name`).
_HTML_TAG = re.compile(r"<[^>]*>")
_LINK_SCHEME = re.compile(r"(?i)(?:tg://|https?://|t\.me/|www\.)\S*")
_AT_SIGNS = ("@", "\uff20")  # ASCII and full-width commercial at


def plain_display_name(
    name: Optional[str], fallback: str, max_length: int = 32
) -> str:
    """A player-facing name that is plain text and can never link anyone.

    Used wherever ANOTHER player's name is shown (spouse line, public
    profile). Telegram handles, HTML tags and link schemes are removed,
    whitespace is collapsed to one line, the name is capped, and the result
    is Markdown-escaped so it cannot break the message. The output never
    contains ``@``, ``tg://`` or an HTML tag, so it can't become a clickable
    DM link. Like :func:`safe_name`, the name is fenced with ``LRM`` so it
    stays one LTR block inside a Persian sentence.
    """
    cleaned = _HTML_TAG.sub("", name or "")
    cleaned = _LINK_SCHEME.sub("", cleaned)
    for sign in _AT_SIGNS:
        cleaned = cleaned.replace(sign, "")
    cleaned = " ".join(cleaned.split())[:max_length].strip()
    if not cleaned:
        cleaned = fallback
    return escape_markdown(wrap_ltr(cleaned))


# ---------------------------------------------------------------------------
# HTML rendering (parse_mode="HTML")
# ---------------------------------------------------------------------------
# Order matters and is the whole point of this module:
#   1. drop the Markdown escaping layer (it only exists for the Markdown
#      parser and would show up as stray backslashes under HTML),
#   2. escape raw HTML characters - BEFORE any tag is inserted, so the tags
#      this helper adds are the only markup Telegram ever sees,
#   3. rebuild the two constructs the bot itself generates: ``tg://user``
#      mentions and ``**bold**``.
# Everything else stays plain text, which is exactly what makes the result
# safe: Telegram can never reject it for "can't parse entities".

_HTML_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

# ``**bold**`` -> ``<b>bold</b>``. Non-greedy so two markers on one line
# stay two separate bold runs.
_BOLD_PAIR = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

# Only the ``tg://user?id=<id>`` mention the bot generates itself becomes a
# real link. Arbitrary user text can never introduce a URL.
_TG_MENTION = re.compile(r"\[([^\[\]<>\n]+)\]\((tg://user\?id=\d+)\)")

# A backslash-escaped Markdown special (``\_``, ``\*``, ...).
_MARKDOWN_ESCAPE = re.compile(r"\\([\\_*`\[\]])")

# A tag or character entity left dangling by truncation.
_TRAILING_TAG = re.compile(r"<[^<>]*$")
_TRAILING_ENTITY = re.compile(r"&(?:[A-Za-z0-9]+|#\d+)?$")

# Telegram counts caption length in UTF-16 code units, not characters.
CAPTION_LIMIT = 1024


def escape_html(text: Optional[str]) -> str:
    """Escape the three characters that are active in Telegram HTML mode."""
    result = "" if text is None else str(text)
    for char, replacement in _HTML_ESCAPES:
        result = result.replace(char, replacement)
    return result


def unescape_markdown(text: Optional[str]) -> str:
    """Remove the backslashes :func:`escape_markdown` added.

    Under HTML those backslashes are literal text, so ``Ali\\_Reza`` would
    be shown as ``Ali\\_Reza``. Dropping the layer first keeps the rendered
    name identical to the legacy Markdown rendering.
    """
    if not text:
        return ""
    return _MARKDOWN_ESCAPE.sub(r"\1", str(text))


def format_html(text: Optional[str]) -> str:
    """Render ``**bold**`` markup as safe Telegram HTML.

    Raw ``&``, ``<`` and ``>`` are escaped first, so the only markup in the
    result is the ``<b>`` (and optional ``<a>``) this function inserts - a
    message can never be rejected for unparseable entities. ``tg://user``
    mentions keep working, and any ``**`` left over (an unbalanced marker,
    e.g. one cut in half by caption truncation) is removed instead of being
    shown to the group.

    >>> format_html("**چشم شاهین** فعال شد!")
    '<b>چشم شاهین</b> فعال شد!'
    >>> format_html("a < b && c")
    'a &lt; b &amp;&amp; c'
    """
    if not text:
        return ""
    safe = escape_html(unescape_markdown(text))
    safe = _TG_MENTION.sub(r'<a href="\2">\1</a>', safe)
    safe = _BOLD_PAIR.sub(r"<b>\1</b>", safe)
    # An unbalanced marker is never displayed: no group should ever see "**".
    return safe.replace("**", "")


def strip_bold(text: Optional[str]) -> str:
    """Delete every ``**`` marker (Telegram callback alerts have no markup).

    ``callback.answer`` ignores ``parse_mode`` entirely, so an alert must be
    plain text: the markers are removed and the words are kept.

    >>> strip_bold("**چشم شاهین** فعال شد!")
    'چشم شاهین فعال شد!'
    """
    if not text:
        return ""
    return str(text).replace("**", "")


def plain_text(text: Optional[str]) -> str:
    """Render for a target with NO parse mode at all (plain-text fallbacks).

    Neither the ``**`` markers nor the HTML tags mean anything there, so the
    Markdown escaping layer and the markers are both dropped.

    >>> plain_text("**سیب باف** \\_ready\\_")
    'سیب باف _ready_'
    """
    return strip_bold(unescape_markdown(text))


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _slice_utf16(text: str, units: int) -> str:
    """Take a prefix of ``units`` UTF-16 code units, never splitting a pair."""
    taken = 0
    out = []
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if taken + size > units:
            break
        out.append(char)
        taken += size
    return "".join(out)


def _clean_truncated(text: str) -> str:
    """Drop a tag or entity a cut left dangling at the end."""
    return _TRAILING_ENTITY.sub("", _TRAILING_TAG.sub("", text))


def truncate_html(text: str, limit: int = CAPTION_LIMIT) -> str:
    """Shorten HTML to ``limit`` UTF-16 units without breaking the markup.

    Truncating a rendered caption naively can cut a ``<b>`` in half, and
    Telegram rejects the whole caption for that. A dangling tag or entity is
    trimmed and every still-open ``<b>`` is closed, so the result is always
    well-formed HTML.
    """
    if _utf16_units(text) <= limit:
        return text
    cut = _clean_truncated(_slice_utf16(text, limit))
    while True:
        open_tags = cut.count("<b>") - cut.count("</b>")
        if open_tags <= 0:
            return cut
        if _utf16_units(cut) + 4 * open_tags <= limit:
            # The closers fit inside the budget.
            return cut + "</b>" * open_tags
        # No room for the closing tags: give up four more units and retry.
        # Each pass drops at least one character, so this always terminates.
        cut = _clean_truncated(_slice_utf16(cut, max(_utf16_units(cut) - 4, 0)))


def format_html_caption(text: Optional[str], limit: int = CAPTION_LIMIT) -> str:
    """``format_html`` that also fits Telegram's caption limit.

    Photo captions are capped at 1024 UTF-16 units; HTML escaping and the
    ``<b>`` tags add a few units on top of the source text, so the fit is
    applied *after* rendering (see :func:`truncate_html`).
    """
    return truncate_html(format_html(text), limit)
