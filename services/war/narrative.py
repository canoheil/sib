"""Persian narrative pools and rendering engine for Apple Clash battle dispatches."""

import random
import re
from typing import List, Optional

from sib.core.text import plain_display_name
from sib.services.sib_claim import fa_digits

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")

WAR_ATTACK_ATTACKER: List[str] = [
    "⚔️ {player} با تکیه بر نیروی افسانه‌ای کارت «{card}» به دیوارهای {target_group} یورش برد و {damage} آسیب سهمگین وارد کرد!{buff_label}",
    "🔥 غرش منجنیق‌های دلاور ما! {player} با هدایت «{card}» باروهای قلعهٔ {target_group} را به آتش کشید و {damage} خسارت سنگین ثبت نمود!{buff_label}",
    "🏹 ضربه‌ای مهلک به قلب خطوط دفاعی حریف! {player} به همراه کارت «{card}» بر سنگرهای {target_group} تاخت و {damage} امتیاز جنگی رقم زد!{buff_label}",
    "💥 لرزه بر اندام ارگ {target_group}! قهرمان غیور ما {player} با توان «{card}» بر دروازه‌های دشمن کوبید و {damage} صدمه بر جای گذاشت!{buff_label}",
    "⚡️ شکافی ویرانگر بر برج‌های دیده‌بانی حریف! {player} با تیغ بران «{card}» صفوف {target_group} را در هم شکست و {damage} آسیب تحمیل کرد!{buff_label}",
    "🗡 طوفان آتش و پولاد! دلاور ما {player} در رکاب «{card}» به باروهای مستحکم {target_group} تاخت و با وارد آوردن {damage} آسیب، حریف را به عقب راند!{buff_label}",
]

WAR_ATTACK_DEFENDER: List[str] = [
    "🚨 آژیر خطر در قلعه! {player} از گروه {source_group} با کارت «{card}» به باروهای ما حمله کرد و {damage} آسیب به استحکامات وارد نمود! آمادهٔ ضدحمله شوید!{buff_label}",
    "🛡 هشدار نفوذ دشمن! مهاجم {player} از قبیلهٔ {source_group} با قدرت «{card}» دیوارهای ما را هدف گرفت و {damage} خسارت وارد ساخت! صفوف دفاعی را محکم کنید!{buff_label}",
    "💥 باران سنگ و آتش بر سر قلعه! {player} از {source_group} با توان کارت «{card}» به دروازه‌های ما ضربه زد و {damage} آسیب به بار آورد! دلاوران ما پایداری می‌کنند!{buff_label}",
    "⚠️ ضربهٔ سنگین حریف به سنگرهای ما! {player} (از گروه {source_group}) با نیروی «{card}» شکافی به ارزش {damage} آسیب در مواضع ما پدید آورد! پاسخ کوبنده در راه است!{buff_label}",
    "🏹 تیرهای آتشین بر فراز باروها! {player} از قبایل {source_group} با همراهی کارت «{card}» مواضع ما را کوبید و {damage} آسیب به ثبت رساند! پرچم قلعه همچنان افراشته است!{buff_label}",
    "⚡️ هجوم بی‌امان به دیوارهای دفاعی! قهرمان مهاجم {player} از گروه {source_group} با تیغ «{card}» بر ما تاخت و {damage} صدمه زد! مدافعان آمادهٔ نبرد تن‌به‌تن شوید!{buff_label}",
]


def render_attacker_narrative(
    player: str,
    card: str,
    target_group: str,
    damage: int | str,
    *,
    is_buff: bool = False,
    rng: Optional[random.Random] = None,
) -> str:
    """Render a dynamic narrative dispatch for the attacking clan chat."""
    chosen_rng = rng or random.Random()
    template = chosen_rng.choice(WAR_ATTACK_ATTACKER)
    formatted_damage = fa_digits(damage) if isinstance(damage, int) else damage
    buff_label = " 💥 [با قدرت سیب باف]" if is_buff else ""
    return template.format(
        player=player,
        card=card,
        target_group=target_group,
        damage=formatted_damage,
        buff_label=buff_label,
    )


def render_defender_narrative(
    player: str,
    card: str,
    source_group: str,
    damage: int | str,
    *,
    is_buff: bool = False,
    rng: Optional[random.Random] = None,
) -> str:
    """Render a dynamic narrative dispatch for the defending clan chat.

    The attacker's name is sanitized to plain text without any clickable
    mentions or tg://user IDs to protect attacker privacy in enemy groups.
    """
    chosen_rng = rng or random.Random()
    template = chosen_rng.choice(WAR_ATTACK_DEFENDER)
    formatted_damage = fa_digits(damage) if isinstance(damage, int) else damage
    buff_label = " 💥 [با قدرت سیب باف]" if is_buff else ""

    # Sanitize attacker display name to plain text (strip markdown links/tg scheme)
    clean_player = _MD_LINK.sub(r"\1", str(player or ""))
    sanitized_player = plain_display_name(clean_player, fallback="مهاجم")

    return template.format(
        player=sanitized_player,
        card=card,
        source_group=source_group,
        damage=formatted_damage,
        buff_label=buff_label,
    )
