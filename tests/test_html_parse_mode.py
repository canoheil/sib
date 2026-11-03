"""HW3 — no group ever reads a raw ``**`` again.

Narrative sources keep the lightweight ``**bold**`` marker. Messages and
captions are delivered with ``parse_mode="HTML"`` and rendered by
:func:`sib.core.text.format_html`; callback alerts have no parse mode at
all, so they are rendered by :func:`sib.core.text.strip_bold`.

These tests cover the helper contract (TASK 1 / TASK 3) and then walk the
real delivery paths of the Invasion, War, Halloween and Mine narratives
(TASK 2) to prove Telegram receives HTML and never an asterisk.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sib.core.text import (
    CAPTION_LIMIT,
    escape_html,
    format_html,
    format_html_caption,
    plain_text,
    strip_bold,
)


# ---------------------------------------------------------------------------
# TASK 1 — format_html
# ---------------------------------------------------------------------------


def test_format_html_converts_bold_and_escapes_html():
    """**bold** becomes <b>bold</b>; raw HTML is escaped BEFORE the tags."""
    assert format_html("**چشم شاهین** فعال شد!") == "<b>چشم شاهین</b> فعال شد!"
    # Several runs on one line stay separate runs.
    assert format_html("**الف** و **ب**") == "<b>الف</b> و <b>ب</b>"
    # The three characters Telegram treats as markup are escaped, so a
    # player-typed "<" or "&" can never break the entity parse.
    assert escape_html("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"
    assert format_html("a < b && c") == "a &lt; b &amp;&amp; c"
    # Escaping happens BEFORE the bold conversion: an injected tag is text,
    # it can never become real markup the bot did not write itself.
    rendered = format_html("**<script>bold</script>**")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert rendered == "<b>&lt;script&gt;bold&lt;/script&gt;</b>"
    # Empty / None input is safe.
    assert format_html("") == "" and format_html(None) == ""


def test_format_html_never_leaves_an_asterisk():
    """An unbalanced marker (e.g. one cut in half by truncation) is dropped."""
    assert format_html("ضربهٔ **سنگین") == "ضربهٔ سنگین"
    assert format_html("****") == ""
    assert "**" not in format_html("شروع **وسط پایان")
    # The bot's own tg://user mention survives the migration as a real link.
    assert format_html("[آرش](tg://user?id=42) زد") == (
        '<a href="tg://user?id=42">آرش</a> زد'
    )
    # A Markdown-escaped name renders as the name, not with a stray backslash.
    assert format_html("آرش\\_کمانگیر") == "آرش_کمانگیر"


def test_format_html_caption_fits_telegram_and_keeps_valid_tags():
    """A rendered caption stays inside 1024 UTF-16 units, tags intact."""
    long_one = " ".join(f"واژه{index}" for index in range(400))
    caption = format_html_caption(f"**{long_one}**\n\n**پایان**")
    assert len(caption.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert "**" not in caption
    # Every <b> that was opened is closed: Telegram would reject the whole
    # caption for a single dangling tag.
    assert caption.count("<b>") == caption.count("</b>")
    # A short caption is never touched.
    short = format_html_caption("**سلام**")
    assert short == "<b>سلام</b>"


# ---------------------------------------------------------------------------
# TASK 3 — callback alerts (no parse mode is supported)
# ---------------------------------------------------------------------------


def test_callback_alerts_strip_bold_markers():
    """strip_bold removes every marker and keeps the words."""
    assert strip_bold("**چشم شاهین** فعال شد!") == "چشم شاهین فعال شد!"
    assert strip_bold("**الف** و **ب**") == "الف و ب"
    assert strip_bold("بدون نشانه") == "بدون نشانه"
    assert strip_bold("") == "" and strip_bold(None) == ""


def test_callback_alerts_of_every_narrative_are_plain():
    """The alert strings the handlers really build contain no marker."""
    from sib.handlers import halloween as halloween_handler
    from sib.services import apple_mine_narrative
    from sib.services.halloween import milestone_reward_line

    alerts = [
        f"🎃 **{3} دونه کدو** گرفتی!",
        f"🎁 جایزه **{75}٪**: {milestone_reward_line(75)}!",
        "⏳ **مهلت این صندوق تموم شد.**",
        halloween_handler.ATTACK_CRIT_LINE,
        halloween_handler.ATTACK_DEFEAT_LINE,
        apple_mine_narrative.cooldown_message(95, user_id=1),
    ]
    for alert in alerts:
        assert "**" in alert, alert  # the source keeps the marker ...
        stripped = strip_bold(alert)
        assert "**" not in stripped, alert  # ... the alert must not show it
        assert stripped  # and the words survive


@pytest.mark.asyncio
async def test_mine_shortfall_alert_is_answered_without_markers(monkeypatch):
    """End-to-end through the real handler: callback.answer gets plain text."""
    from contextlib import asynccontextmanager

    from sib.handlers import apple_mine as mine_handler
    from sib.services.apple_mine import PickaxeInsufficientFunds

    @asynccontextmanager
    async def session_ctx():
        yield object()

    monkeypatch.setattr(mine_handler, "get_session", session_ctx)
    monkeypatch.setattr(
        mine_handler,
        "upgrade_pickaxe",
        AsyncMock(side_effect=PickaxeInsufficientFunds(cost=600, missing=250)),
    )
    message = SimpleNamespace(chat=SimpleNamespace(id=-506), message_id=82,
                              photo=[object()])
    callback = SimpleNamespace(data="mine:pickaxe:upgrade:114",
                               from_user=SimpleNamespace(id=114),
                               message=message, answer=AsyncMock())

    await mine_handler.handle_mine_pickaxe_upgrade(callback, SimpleNamespace())

    args, kwargs = callback.answer.await_args
    assert kwargs.get("show_alert") is True
    assert "**" not in args[0]
    assert "<b>" not in args[0]
    assert "۲۵۰" in args[0]


# ---------------------------------------------------------------------------
# TASK 2 — the delivered narrative is HTML on every path
# ---------------------------------------------------------------------------


def test_invasion_event_catalog_renders_without_markers():
    """The event catalog prose the group reads is bold, not asterisks."""
    from sib.services.invasion_v2 import event_catalog as catalog

    for template in catalog.EVENTS:
        for line in template.narratives:
            rendered = format_html(line)
            assert "**" not in rendered
            assert "<b>" in rendered, line
        effect = format_html(
            catalog.render_effect_line(template, amount=12, loot_amount=3)
        )
        assert "**" not in effect, effect


def test_invasion_hp_and_damage_lines_render_as_html():
    from sib.services.invasion_v2.presentation import (
        announcement_hp_line,
        build_join_caption,
    )
    from sib.services.sib_claim import fa_digits

    hp = format_html(announcement_hp_line(159, 200))
    assert hp == format_html(f"\n\n⚔️ جان شیاسیب: **{fa_digits(159)}** / **{fa_digits(200)}**")
    assert "**" not in hp and "<b>" in hp

    join = format_html_caption(build_join_caption("ضربهٔ سنگین", 41,
                                                  is_crit=True, is_kill=False))
    assert "**" not in join
    assert join.endswith(format_html(f"\n\n💥 آسیب وارده به دشمن: **{fa_digits(41)}**"))


def test_war_narratives_render_as_html():
    from sib.services.war.narrative import (
        render_attacker_narrative,
        render_defender_narrative,
    )
    from sib.services.war_presentation import defeat_caption, victory_caption

    attacker = format_html(render_attacker_narrative(
        player="[آرش](tg://user?id=7)", card="سایه ایگریس",
        target_group="شیرها", damage=1200, is_buff=True))
    assert "**" not in attacker
    # The teammate mention stays a real link under HTML.
    assert '<a href="tg://user?id=7">آرش</a>' in attacker

    defender = format_html(render_defender_narrative(
        player="رستم", card="شوالیه سیب", source_group="شاهین‌ها",
        damage=950, is_buff=False))
    assert "**" not in defender and "<b>" in defender

    for caption in (victory_caption(1500, shield_hours=2), defeat_caption(1500)):
        rendered = format_html(caption)
        assert "**" not in rendered and "<b>" in rendered


def test_halloween_narratives_render_as_html():
    from sib.handlers.halloween import BOSS_MENU_TITLE
    from sib.services.halloween import (
        DEFEAT_ANNOUNCEMENT,
        START_ANNOUNCEMENT,
        format_duration_fa,
    )
    from sib.services.sib_claim import fa_digits

    assert "<b>" in format_html(START_ANNOUNCEMENT)
    assert "**" not in format_html(DEFEAT_ANNOUNCEMENT)
    assert "<b>" in format_html(BOSS_MENU_TITLE)

    alert = f"🎃 **{fa_digits(4)} دونه کدو** گرفتی! غنیمت بعدی: {format_duration_fa(90)}"
    assert "**" not in strip_bold(alert)


def test_plain_text_fallback_drops_every_layer():
    """A send without any parse mode shows neither ** nor <b>."""
    assert plain_text("**سیب باف** \\_آماده\\_") == "سیب باف _آماده_"
    assert plain_text("**الف**") == "الف"
    assert plain_text(None) == ""
