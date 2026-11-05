"""Unicode bidi (RTL/LTR) helpers for mixed Persian/English messages.

Every message is laid out by the Unicode Bidirectional Algorithm (UBA).
The *paragraph* direction is guessed from the first strong character, so a
sentence that starts with an emoji or a Latin name and continues in Persian

    «🫂 ᎶᎪmᎬ4ᎬᏉᎬᏒ از دوست سیبیش یه بغل گرم گرفت! ۲۰ سیب به هر کدوم رسید.»

is laid out left-to-right: the Persian run stays RTL but every neutral
character at a direction boundary (``!``, ``?``, ``(``, ``)``, ``.``) and
every emoji follows the LTR paragraph, so they jump to the wrong side of
the sentence.

Two zero-width controls fix this without touching a single visible
character:

* :data:`RLM` (U+200F, RIGHT-TO-LEFT MARK) at the start of the text pins
  the paragraph direction to right-to-left - :func:`fix_bidi`.
* :data:`LRM` (U+200E, LEFT-TO-RIGHT MARK) on both sides of a name isolates
  it as ONE left-to-right block, so Latin/Cyrillic/stylized names and
  usernames cannot reorder the Persian words or their punctuation around
  them - :func:`wrap_ltr`.

Both helpers are idempotent and safe to apply twice (a name goes through
``safe_name`` and then a whole rendered message through ``fix_bidi``).
"""

import re
from typing import Optional

#: U+200F RIGHT-TO-LEFT MARK: a strong RTL character, used to force the
#: paragraph direction of a sentence that would otherwise be guessed LTR.
RLM = "\u200f"

#: U+200E LEFT-TO-RIGHT MARK: a strong LTR character, used on both sides of
#: an LTR name to keep it one isolated block inside a Persian sentence.
LRM = "\u200e"

#: Arabic / Persian letters and digits: the Arabic block, Arabic Supplement,
#: Arabic Extended-A and both Arabic Presentation-Forms blocks (Persian
#: letters such as ی/ک/گ live in the first one; presentation forms show up
#: in text pasted from PDFs and in generated art strings).
_RTL_RE = re.compile(
    "[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufefc]"
)


def contains_rtl(text: Optional[str]) -> bool:
    """True when ``text`` has at least one Persian/Arabic (RTL) character."""
    return bool(text) and _RTL_RE.search(str(text)) is not None


def fix_bidi(text: str) -> str:
    """Force the paragraph direction of a Persian sentence to RTL.

    A right-to-left mark is prepended when the text contains Persian or
    Arabic characters, so the *first* strong character the renderer sees is
    RTL no matter whether the message opens with an emoji, a Latin name or
    an English word. Text without Persian/Arabic (and already-marked text)
    is returned unchanged, which makes the helper idempotent.

    >>> fix_bidi("🫂 علی آمد!") == RLM + "🫂 علی آمد!"
    True
    >>> fix_bidi("hello")
    'hello'
    """
    if not text:
        return ""
    value = str(text)
    if value.startswith(RLM) or not contains_rtl(value):
        return value
    return RLM + value


def wrap_ltr(name: str) -> str:
    """Isolate an LTR name so it cannot reorder the Persian text around it.

    The name is fenced with :data:`LRM` on both sides, which makes the
    renderer treat it as one left-to-right block inside the RTL sentence:
    ``Ali`` keeps reading ``Ali`` and the neighbouring ``!``/``()`` stay
    where the Persian grammar puts them. Idempotent, and an empty name is
    returned untouched (no invisible characters for "no name").

    >>> wrap_ltr("Ali") == LRM + "Ali" + LRM
    True
    """
    if not name:
        return "" if name is None else name
    value = str(name).strip()
    if not value:
        return ""
    # Already fenced (``safe_name`` -> ``wrap_ltr`` -> caller) or genuinely a
    # one-character name that happens to be the mark itself: never double it.
    if value.startswith(LRM) and value.endswith(LRM) and len(value) > 2 * len(LRM):
        return value
    return f"{LRM}{value}{LRM}"


def strip_controls(text: Optional[str]) -> str:
    """Remove every bidi control from ``text`` (used by tests and logs only).

    The controls are invisible, so debugging a broken layout means looking
    at the same string with them filtered out.
    """
    if not text:
        return ""
    return str(text).replace(RLM, "").replace(LRM, "")
