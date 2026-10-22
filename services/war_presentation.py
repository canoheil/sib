"""Apple Clash result announcements: the final message of a finished war.

Presentation only. Everything comes from a committed ``WarOutcome``; a delivery
failure is logged and can never touch the settlement that already committed
(winner, loot, peace shield). Each group is served on its own, so one group
that cannot be reached (bot removed, chat migrated, Telegram error) never
costs the other group its message.
"""

from pathlib import Path
from typing import Optional

from aiogram.types import FSInputFile

from sib.core.logging import logger
from sib.services.sib_claim import fa_digits
from sib.services.telegram_ui import safe_send_screen
from sib.services.war_service import PEACE_SHIELD_DURATION, WarOutcome

VICTORY_IMAGE = (
    Path(__file__).resolve().parent.parent / "assets" / "apple_clash" / "war_bg_victory.jpg"
)


def _shield_hours_text(hours: int) -> str:
    return f"سپر صلح **{fa_digits(hours)} ساعته** فعال شد."


def victory_caption(loot: int, *, shield_hours: Optional[int] = None) -> str:
    """Final message for the winning group.

    States the amount ACTUALLY taken: the loot is capped so the loser keeps its
    level (it can be less than 15%, or 0), so no percentage is promised.

    ``shield_hours`` is passed only when this winner is the DEFENDER (C2-6: the
    defender is always shielded, the attacker never is).
    """
    text = (
        "🏆 **پیروزی در جدال!**\n"
        f"دشمن رو شکست دادیم. غنیمت: **{fa_digits(loot)} سیب**."
    )
    if shield_hours:
        text += f"\n🛡 {_shield_hours_text(shield_hours)}"
    return text


def defeat_caption(loot: int, *, shield_hours: Optional[int] = None) -> str:
    """Final message for the losing group.

    C2-6: the shield belongs to the DEFENDER, so a losing defender is told
    about its peace shield (C2-9: 2 hours), while a losing attacker is told it gets
    none (it can be attacked again immediately).
    """
    text = (
        "💔 **شکست در جدال.**\n"
        f"دشمن **{fa_digits(loot)} سیب** از خزانه ما برداشت."
    )
    if shield_hours:
        text += f"\n🛡 {_shield_hours_text(shield_hours)}"
    else:
        text += "\n⚔️ مهاجم **سپر صلح** نمی‌گیره."
    return text


def draw_caption(*, shield_hours: Optional[int] = None) -> str:
    """Final message of a tie: no loot, but the defender is still shielded."""
    text = (
        "🤝 **جدال مساوی شد!**\n"
        "غنیمتی جابه‌جا نشد."
    )
    if shield_hours:
        text += f"\n🛡 {_shield_hours_text(shield_hours)}"
    else:
        text += "\n⚔️ مهاجم **سپر صلح** نمی‌گیره."
    return text


async def _deliver(
    bot, war_id: int, chat_id: int, text: str, image: Optional[Path]
) -> bool:
    """Send one group its message; True when Telegram accepted it. Never raises."""
    try:
        photo = None
        if image is not None:
            if image.is_file():
                # A fresh handle per message: an upload stream is read only once.
                photo = FSInputFile(str(image))
            else:
                logger.warning("Apple Clash result image is missing: %s", image)
        # Plain text: the bot's default parse mode is Markdown. A failed photo
        # upload degrades to the same text instead of losing the message.
        sent = await safe_send_screen(bot, chat_id, text, photo=photo, parse_mode=None)
    except Exception:
        logger.exception(
            "Apple Clash war %s: result delivery to group %s crashed", war_id, chat_id
        )
        return False
    if sent is None:
        logger.warning(
            "Apple Clash war %s: result could not be delivered to group %s", war_id, chat_id
        )
        return False
    return True


async def announce_war_result(bot, outcome: WarOutcome) -> int:
    """Send the final message of one finished war to BOTH groups.

    The winner and the loser both get the victory banner with their own text;
    a draw gets a plain text notice. C2-6: only the DEFENDER is told about the
    peace shield (C2-9: 2 hours; one win, lose or draw); the ATTACKER is never
    shielded. Returns how many groups were reached.
    """
    shield_hours = int(PEACE_SHIELD_DURATION.total_seconds() // 3600)
    if outcome.is_draw:
        deliveries = [
            (outcome.defender_id, draw_caption(shield_hours=shield_hours), None),
            (outcome.attacker_id, draw_caption(), None),
        ]
    else:
        winner_is_defender = outcome.winner_id == outcome.defender_id
        deliveries = [
            (
                outcome.winner_id,
                victory_caption(
                    outcome.loot,
                    shield_hours=shield_hours if winner_is_defender else None,
                ),
                VICTORY_IMAGE,
            ),
            (
                outcome.loser_id,
                defeat_caption(
                    outcome.loot,
                    shield_hours=None if winner_is_defender else shield_hours,
                ),
                VICTORY_IMAGE,
            ),
        ]
    reached = 0
    for chat_id, text, image in deliveries:
        if await _deliver(bot, outcome.war_id, chat_id, text, image):
            reached += 1
    return reached
