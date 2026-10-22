"""Persian narrative pools and rendering engine for Apple Clash battle dispatches."""

import random
import re
from typing import List, Optional

from sib.core.text import plain_display_name
from sib.services.sib_claim import fa_digits

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")

WAR_ATTACK_ATTACKER: List[str] = [
    "⚔️ **{player}** با **{card}** به **{target_group}** حمله کرد؛ **{damage} آسیب** زدیم!{buff_label}",
    "🔥 ضربه **{player}** با **{card}** رسید! **{target_group}** **{damage} آسیب** خورد.{buff_label}",
    "🏹 **{target_group}** رو هدف گرفتیم! **{player}** با **{card}** **{damage} آسیب** زد.{buff_label}",
    "💥 **{player}** و **{card}** وارد میدان شدن! **{damage} آسیب** به **{target_group}** رسید.{buff_label}",
    "⚡️ حمله به **{target_group}**! **{player}** با **{card}** ضربه زد و **{damage} آسیب** رسوند.{buff_label}",
    "🗡 **{player}** با **{card}** حمله رو ادامه داد؛ **{target_group}** **{damage} آسیب** خورد!{buff_label}",
]

WAR_ATTACK_DEFENDER: List[str] = [
    "🏹 **{player}** از **{source_group}** با **{card}** حمله کرد؛ **{damage} آسیب** خوردیم!{buff_label}",
    "🚨 حمله به قلعه ما! **{player}** از **{source_group}** با **{card}** زد و **{damage} آسیب** رسوند.{buff_label}",
    "🛡 **{player}** از **{source_group}** با **{card}** ضربه زد! **{damage} آسیب** به قلعه ما رسید.{buff_label}",
    "💥 **{card}** توی دست **{player}** از **{source_group}** حرف زد! **{damage} آسیب** خوردیم.{buff_label}",
    "⚠️ **{source_group}** حمله کرد! **{player}** با **{card}** **{damage} آسیب** به ما زد.{buff_label}",
    "⚡️ ضربه از **{source_group}** رسید! **{player}** با **{card}** **{damage} آسیب** زد.{buff_label}",
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
    buff_label = " 💥 **سیب باف**" if is_buff else ""
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
    buff_label = " 💥 **سیب باف**" if is_buff else ""

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
