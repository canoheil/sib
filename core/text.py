"""Utilities for building safe Telegram message text.

Telegram renders bot messages with legacy ``Markdown`` parse mode. Any
unescaped user-provided character such as ``_``, ``*``, `` ` `` or ``[``
can make the whole message invalid (``can't parse entities``) which in
turn silently breaks board refreshes. All user-controlled text (display
names, group titles, character names injected by admins, ...) must pass
through :func:`escape_markdown` before being interpolated into Markdown
message bodies.
"""

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
