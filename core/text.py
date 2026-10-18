"""Utilities for building safe Telegram message text.

Telegram renders bot messages with legacy ``Markdown`` parse mode. Any
unescaped user-provided character such as ``_``, ``*``, `` ` `` or ``[``
can make the whole message invalid (``can't parse entities``) which in
turn silently breaks board refreshes. All user-controlled text (display
names, group titles, character names injected by admins, ...) must pass
through :func:`escape_markdown` before being interpolated into Markdown
message bodies.
"""

import re
from typing import Optional

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
    """Return a Markdown-safe display name with a friendly fallback."""
    cleaned = (name or "").strip()
    if not cleaned:
        cleaned = fallback
    return escape_markdown(cleaned)


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
    DM link.
    """
    cleaned = _HTML_TAG.sub("", name or "")
    cleaned = _LINK_SCHEME.sub("", cleaned)
    for sign in _AT_SIGNS:
        cleaned = cleaned.replace(sign, "")
    cleaned = " ".join(cleaned.split())[:max_length].strip()
    if not cleaned:
        cleaned = fallback
    return escape_markdown(cleaned)
