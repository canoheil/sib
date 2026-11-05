"""Bidi (RTL/LTR) rendering: invisible marks keep Persian sentences correct.

An English (or stylized) name inside a Persian sentence changes what the
Unicode Bidirectional Algorithm guesses the *paragraph* direction to be. The
sentence

    «🫂 ᎶᎪmᎬ4ᎬᏉᎬᏒ از دوست سیبیش یه بغل گرم گرفت! ۲۰ سیب به هر کدوم رسید.»

is laid out left-to-right, so ``!``/``?``/``()`` and every emoji jump to the
wrong side of the line. Two zero-width controls fix it, and nothing else in
the text changes:

* ``RLM`` (U+200F, :func:`sib.core.bidi.fix_bidi`) pins the paragraph to RTL;
* ``LRM`` (U+200E, :func:`sib.core.bidi.wrap_ltr`) fences a name so it stays
  ONE left-to-right block inside the Persian run.

The helpers live in :mod:`sib.core.bidi`; ``safe_name`` /
``plain_display_name`` in :mod:`sib.core.text` apply the fence centrally, and
the hug/kiss, casino, theft, pet and transfer handlers pin the direction of
the strings they send. These tests assert the exact control characters and
then walk the real handlers to prove the group receives them.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import User as TGUser

from sib.core.bidi import (
    LRM,
    RLM,
    contains_rtl,
    fix_bidi,
    strip_controls,
    wrap_ltr,
)
from sib.core.text import plain_display_name, safe_name
from sib.services import marriage_art

# The exact broken sentence from the bug report, and the name inside it.
STYLIZED_NAME = "ᎶᎪmᎬ4ᎬᏉᎬᏒ"
BROKEN_HUG = (
    "🫂 {name} از دوست سیبیش 𝓢 یه بغل گرم گرفت! ۲۰ سیب به هر کدوم رسید."
)

# ---------------------------------------------------------------------------
# 1. The control characters themselves
# ---------------------------------------------------------------------------


def test_marks_are_the_unicode_bidi_controls():
    """RLM is U+200F and LRM is U+200E — the whole point of the module."""
    assert RLM == "\u200f"
    assert LRM == "\u200e"
    assert RLM != LRM
    assert ord(RLM) == 0x200F and ord(LRM) == 0x200E


def test_fix_bidi_prepends_rlm_for_persian_text():
    """A Persian sentence gets exactly one RLM at the very start."""
    fixed = fix_bidi("سلام دنیا")
    assert fixed.startswith(RLM)
    assert fixed == RLM + "سلام دنیا"
    # Only ONE RLM is added: the visible text is untouched.
    assert fixed.count(RLM) == 1
    assert strip_controls(fixed) == "سلام دنیا"


def test_fix_bidi_handles_a_sentence_that_opens_with_an_emoji_or_a_name():
    """The reported case: emoji + stylized English name + Persian prose."""
    broken = BROKEN_HUG.format(name=STYLIZED_NAME)
    fixed = fix_bidi(broken)
    assert fixed.startswith(RLM)
    assert strip_controls(fixed) == broken  # no visible character changed


def test_fix_bidi_leaves_pure_latin_text_alone():
    """English-only text is already LTR; adding an RLM would break it."""
    assert fix_bidi("hello world") == "hello world"
    assert fix_bidi("🎲 dice roll!") == "🎲 dice roll!"
    assert RLM not in fix_bidi("hello world")


def test_fix_bidi_is_idempotent_and_safe_on_empty_input():
    once = fix_bidi("🫂 علی آمد!")
    assert fix_bidi(once) == once
    assert fix_bidi("") == ""
    assert fix_bidi(None) == ""


def test_contains_rtl_recognizes_persian_and_arabic_ranges():
    assert contains_rtl("سیب")
    assert contains_rtl("علي")  # Arabic yeh, not Persian
    assert contains_rtl("۱۲۳")  # Arabic-Indic digits
    assert not contains_rtl("apple 123")
    assert not contains_rtl("")
    assert not contains_rtl(None)


def test_wrap_ltr_fences_the_name_with_lrm_on_both_sides():
    """``LRM`` on BOTH sides: the name is one LTR block, not a loose run."""
    wrapped = wrap_ltr("Ali")
    assert wrapped == LRM + "Ali" + LRM
    assert wrapped.startswith(LRM) and wrapped.endswith(LRM)
    assert wrapped.count(LRM) == 2
    assert strip_controls(wrapped) == "Ali"


def test_wrap_ltr_keeps_the_name_unchanged_and_is_idempotent():
    """Only invisible marks are added; escaping/characters stay the caller's."""
    assert strip_controls(wrap_ltr(STYLIZED_NAME)) == STYLIZED_NAME
    once = wrap_ltr("Ali")
    assert wrap_ltr(once) == once
    assert wrap_ltr("") == ""


@pytest.mark.parametrize("name", ["Ali", "ᎶᎪmᎬ4ᎬᏉᎬᏒ", "𝓢", "Soheil_Dev"])
def test_wrapped_names_survive_inside_a_persian_sentence(name):
    """Every wrapped name is one LTR block: exactly two LRM around it."""
    sentence = f"🤗 {wrap_ltr(name)} آمد!"
    assert sentence.count(LRM) == 2
    assert f"{LRM}{name}{LRM}" in sentence
    assert RLM not in sentence  # fix_bidi is the one that pins the paragraph


# ---------------------------------------------------------------------------
# 2. The central name formatters fence every name
# ---------------------------------------------------------------------------


def test_safe_name_wraps_the_name_with_lrm():
    assert safe_name("Ali") == LRM + "Ali" + LRM
    assert safe_name(STYLIZED_NAME) == LRM + STYLIZED_NAME + LRM
    # The fallback is fenced too, so a caller never has to special-case it.
    assert safe_name(None) == LRM + "بازیکن" + LRM


def test_safe_name_still_escapes_markdown_inside_the_fence():
    """Bidi must not undo the Markdown safety work of the same helper."""
    assert safe_name("Soheil_Dev") == LRM + "Soheil\\_Dev" + LRM
    assert safe_name("a*b") == LRM + "a\\*b" + LRM


def test_plain_display_name_wraps_the_name_with_lrm():
    assert plain_display_name("Ali", "بازیکن") == LRM + "Ali" + LRM
    # Sanitization still runs first (no @, no tag, no link) and the fence
    # is applied to what survived it.
    sanitized = plain_display_name("@sara <b>x</b> tg://user?id=1", "بازیکن")
    assert "@" not in sanitized and "<" not in sanitized and "tg://" not in sanitized
    assert sanitized.startswith(LRM) and sanitized.endswith(LRM)


# ---------------------------------------------------------------------------
# 3. The hug handler delivers LRM around the target's name
# ---------------------------------------------------------------------------


def _tg_user(uid: int, name: str, is_bot: bool = False) -> TGUser:
    return TGUser(id=uid, is_bot=is_bot, first_name=name, username=f"u{uid}")


def _group_hug_message(sender, text, *, target=None, message_id=950):
    msg = MagicMock()
    msg.chat = MagicMock()
    msg.chat.type = "supergroup"
    msg.chat.id = -9500
    msg.chat.title = "گروه بیدی"
    msg.chat.username = None
    msg.message_id = message_id
    msg.from_user = sender
    msg.text = text
    msg.reply = AsyncMock(return_value=MagicMock(message_id=message_id + 1))
    msg.reply_photo = AsyncMock(return_value=MagicMock(message_id=message_id + 2))
    if target is None:
        msg.reply_to_message = None
    else:
        msg.reply_to_message = MagicMock()
        msg.reply_to_message.from_user = target
    return msg


@pytest.mark.asyncio
async def test_hug_message_wraps_the_targets_ltr_name_with_lrm(
    db_session, monkeypatch, tmp_path
):
    """The group hug text has ``\\u200e`` on BOTH sides of the target's name."""
    from sib.handlers.sib_hug import handle_sib_hug

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)  # no hug artwork
    sender = _tg_user(951001, STYLIZED_NAME)
    target = _tg_user(951002, "Ali")

    msg = _group_hug_message(sender, "بغل سیبی", target=target)
    await handle_sib_hug(msg)

    text = msg.reply.call_args[0][0]
    assert msg.reply.call_args.kwargs.get("parse_mode") == "Markdown"
    # 1) The paragraph direction is pinned to RTL (the variant starts with
    #    an emoji, which the UBA would otherwise read as neutral).
    assert text.startswith(RLM)
    # 2) The target's name is fenced with LRM inside its mention link, so
    #    the Persian words and punctuation around it keep their order.
    assert f"[{LRM}Ali{LRM}](tg://user?id=951002)" in text
    assert f"[{LRM}{STYLIZED_NAME}{LRM}](tg://user?id=951001)" in text
    # 3) Nothing visible changed: dropping the controls yields the same line.
    visible = strip_controls(text)
    assert f"[Ali](tg://user?id=951002)" in visible
    assert STYLIZED_NAME in visible and "۲۰" not in visible


@pytest.mark.asyncio
async def test_kiss_message_wraps_the_targets_ltr_name_with_lrm(
    db_session, monkeypatch, tmp_path
):
    from sib.handlers.sib_kiss import handle_sib_kiss

    monkeypatch.setattr(marriage_art, "_ASSETS_DIR", tmp_path)
    sender = _tg_user(952001, "فرستنده بیدی")
    target = _tg_user(952002, "Ali")

    msg = _group_hug_message(sender, "بوس سیبی", target=target)
    await handle_sib_kiss(msg)

    text = msg.reply.call_args[0][0]
    assert text.startswith(RLM)
    assert f"[{LRM}Ali{LRM}](tg://user?id=952002)" in text
    assert strip_controls(text).count("Ali") == 1


# ---------------------------------------------------------------------------
# 4. Every other handler that interpolates a name
# ---------------------------------------------------------------------------


def test_casino_bet_prompt_pins_the_paragraph_and_fences_the_player():
    from types import SimpleNamespace

    from sib.handlers import casino

    player = SimpleNamespace(id=7, is_bot=False, first_name="Ali", full_name="Ali")
    prompt = casino.bet_prompt(player)

    assert prompt.startswith(RLM)  # the mention may open with a Latin name
    assert f"[{LRM}Ali{LRM}](tg://user?id=7)" in prompt
    assert strip_controls(prompt).startswith("[Ali](tg://user?id=7)")


def test_theft_captions_pin_the_direction_and_fence_the_names():
    from sib.handlers import theft

    caption = theft.WINDOW_FAIL_CAPTION
    assert "شکست" in caption  # Persian, so it must be pinned
    assert fix_bidi(caption).startswith(RLM)


def test_transfer_renders_fence_both_players():
    from sib.services.transfers.text import render_confirmation

    body = render_confirmation(
        from_display=safe_name("Ali"),
        to_display=safe_name("Sara"),
        item_type="apple",
        amount=100,
    )
    assert f"از: {LRM}Ali{LRM}" in body
    assert f"به: {LRM}Sara{LRM}" in body
    # Pinning the whole body keeps the «⏳ … منقضی می‌شود.» line on the right.
    assert fix_bidi(body).startswith(RLM)


def test_pet_display_name_fences_a_latin_pet_name():
    from types import SimpleNamespace

    from sib.core.bidi import wrap_ltr
    from sib.services import pet_service

    pet = SimpleNamespace(custom_name="Fluffy")
    assert pet_service.pet_display_name(pet) == wrap_ltr("Fluffy")
    assert pet_service.pet_display_name(SimpleNamespace(custom_name="")) == wrap_ltr(
        pet_service.UNNAMED_PET_FA
    )


# ---------------------------------------------------------------------------
# 5. Message-start pinning on the remaining name-bearing senders
# ---------------------------------------------------------------------------


def test_apple_battle_narration_pins_every_beat():
    """Narration beats start with an emoji and name both players."""
    from sib.services import apple_battle_narration as narration

    beat = "⚔️ **علی** با **شمشیر** (سطح **۴**)\n⚔️ **سارا** با **سپر**"
    assert fix_bidi(beat).startswith(RLM)
    # The sender applies the same helper before rendering/sending, so the
    # stored beat and the delivered message differ only by the invisible RLM.
    source = __import__("inspect").getsource(narration.AppleBattleNarration.send_beat)
    assert "fix_bidi(" in source


def test_apple_duel_scene_text_is_pinned_and_names_are_fenced():
    from types import SimpleNamespace

    from sib.services.apple_duel_presentation import scene_text

    snapshot = {
        "cards": [
            {
                "user_id": 1,
                "player_name": "Ali",
                "name": "سایه ایگریس",
                "level": 4,
            },
            {
                "user_id": 2,
                "player_name": "Sara",
                "name": "شمشیر سیب",
                "level": 3,
            },
        ]
    }
    duel = SimpleNamespace(snapshot_json=__import__("json").dumps(snapshot))
    battle = SimpleNamespace(winner_id=2)

    text = scene_text(duel, battle, 0)
    assert text.startswith(RLM)  # opens with «⚔️»
    assert f"**{LRM}Ali{LRM}**" in text
    assert f"**{LRM}Sara{LRM}**" in text


@pytest.mark.asyncio
async def test_friend_request_card_pins_direction_and_fences_both_names():
    from sib.handlers.friendship import _card_text

    text = _card_text("Ali", "Sara", 42)
    assert text.startswith(RLM)
    assert f"**{LRM}Ali{LRM}**" in text
    assert f"**[{LRM}Sara{LRM}](tg://user?id=42)**" in text
    assert strip_controls(text) == (
        "🤝 **درخواست دوستی سیبی**\n\n**Ali** به **Sara** درخواست رفاقت داد!\n"
        "**[Sara](tg://user?id=42)**، برای رفیق شدن «قبول» رو بزن."
    )


def test_admin_dashboard_fences_the_display_name_and_the_username():
    import inspect

    from sib.handlers import admin_users

    source = inspect.getsource(admin_users.dashboard)
    assert "fix_bidi(" in source
    assert "wrap_ltr(user.display_name)" in source
    assert "wrap_ltr(user.username or" in source
