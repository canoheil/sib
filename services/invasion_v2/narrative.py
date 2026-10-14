"""Persian cinematic narration with process-local recent-template avoidance.

Public signatures are preserved. Supplied RNGs still drive selection; replay is
reproducible given the same RNG *and history*, not the seed alone. Rendering never
changes combat state, rewards, feature flags or Telegram messages.
"""

import hashlib
import json
import random

from . import narrative_pool as _pool


_MAX_VALUE_LENGTH = 96
_MAX_CAPTION_LENGTH = 899


def _fa(number: int) -> str:
    """Use Persian numerals in user-facing text without mutable tables."""
    return "".join("۰۱۲۳۴۵۶۷۸۹"[ord(char) - 48] if "0" <= char <= "9" else char for char in str(number))


def _key(event: str, *context: str) -> str:
    """No invasion ID exists in the frozen API; scope to available context.

    Hash structured values to avoid delimiter collisions and retaining names in
    history keys. Do not key by damage/HP/RNG state: every hit would get fresh history.
    """
    digest = hashlib.sha256(json.dumps(context, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"{event}:{digest}"


def _value(value: object) -> str:
    """Keep injected display values inline and bounded; never translate names."""
    text = " ".join(str(value).split())
    if len(text) > _MAX_VALUE_LENGTH:
        return text[:_MAX_VALUE_LENGTH-1] + "…"
    return text


def _render(templates: list[str], *, key: str, rng: random.Random, **values: object) -> str:
    with _pool._using_rng(rng):
        template = _pool.pick(templates, key=key)
    text = template.format(**{name: _value(value) for name, value in values.items()})
    # Current templates plus bounded values are comfortably below this limit.
    # Retain a final guard so later copy additions cannot overflow Telegram captions.
    if len(text) > _MAX_CAPTION_LENGTH:
        text = text[:_MAX_CAPTION_LENGTH-1].rstrip() + "…"
    return text


def render_join(
    *, username: str, card_name: str, damage: int, is_crit: bool,
    enemy_hp_remaining: int, rng: random.Random,
) -> str:
    """Finishing hit wins over critical; describe stored outcomes, never decide them."""
    if enemy_hp_remaining <= 0:
        templates = _pool.JOIN_KILL
    elif is_crit:
        templates = _pool.JOIN_CRIT
    else:
        templates = _pool.JOIN_NORMAL
    return _render(templates, key=_key("join", username, card_name), rng=rng,
                   user=username, card=card_name, damage=_fa(damage),
                   hp_remaining=_fa(enemy_hp_remaining))


def render_high_damage(
    *, username: str, card_name: str, damage: int,
    enemy_hp_remaining: int, rng: random.Random,
) -> str:
    """Keep the explicit legacy entry point; strong-impact prose shares the crit pool."""
    templates = _pool.JOIN_KILL if enemy_hp_remaining <= 0 else _pool.JOIN_CRIT
    return _render(templates, key=_key("join", username, card_name), rng=rng,
                   user=username, card=card_name, damage=_fa(damage),
                   hp_remaining=_fa(enemy_hp_remaining))


def render_announcement(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_pool.ANNOUNCEMENT, key=_key("announce", region_name, enemy_name), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_finisher(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_pool.JOIN_KILL, key=_key("join", username, card_name), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_victory(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_pool.VICTORY, key=_key("victory", region_name, enemy_name), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_defeat(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_pool.DEFEAT, key=_key("defeat", region_name, enemy_name), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_expired(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random,
) -> str:
    return _render(_pool.EXPIRED, key=_key("expired", region_name, enemy_name), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)
