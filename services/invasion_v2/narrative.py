"""Persian cinematic narration with process-local recent-template avoidance.

Existing arguments are preserved; optional invasion context adds scoped history. Supplied RNGs still drive selection; replay is
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


def _key(event: str, *context: str, invasion_id: int | None = None,
         fallback_event: str | None = None) -> str:
    """Prefer an event/invasion key; legacy callers keep their context-based key."""
    if invasion_id is not None:
        return f"{event}:{invasion_id}"
    digest = hashlib.sha256(json.dumps(context, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"{fallback_event or event}:{digest}"


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
    enemy_hp_remaining: int, rng: random.Random, invasion_id: int | None = None,
) -> str:
    """Finishing hit wins over critical; describe stored outcomes, never decide them."""
    if enemy_hp_remaining <= 0:
        templates = _pool.JOIN_KILL
    elif is_crit:
        templates = _pool.JOIN_CRIT
    else:
        templates = _pool.JOIN_NORMAL
    event = "join_kill" if templates is _pool.JOIN_KILL else "join_crit" if templates is _pool.JOIN_CRIT else "join_normal"
    return _render(templates, key=_key(event, username, card_name,
                   invasion_id=invasion_id, fallback_event="join"), rng=rng,
                   user=username, card=card_name, damage=_fa(damage),
                   hp_remaining=_fa(enemy_hp_remaining))


def render_high_damage(
    *, username: str, card_name: str, damage: int,
    enemy_hp_remaining: int, rng: random.Random, invasion_id: int | None = None,
) -> str:
    """Keep the explicit legacy entry point; strong-impact prose shares the crit pool."""
    templates = _pool.JOIN_KILL if enemy_hp_remaining <= 0 else _pool.JOIN_CRIT
    event = "join_kill" if templates is _pool.JOIN_KILL else "join_crit" if templates is _pool.JOIN_CRIT else "join_normal"
    return _render(templates, key=_key(event, username, card_name,
                   invasion_id=invasion_id, fallback_event="join"), rng=rng,
                   user=username, card=card_name, damage=_fa(damage),
                   hp_remaining=_fa(enemy_hp_remaining))


def render_announcement(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random, invasion_id: int | None = None,
) -> str:
    return _render(_pool.ANNOUNCEMENT, key=_key("announcement", region_name, enemy_name,
                   invasion_id=invasion_id, fallback_event="announce"), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_finisher(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random, invasion_id: int | None = None,
) -> str:
    return _render(_pool.JOIN_KILL, key=_key("join_kill", username, card_name,
                   invasion_id=invasion_id, fallback_event="join"), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_victory(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random, invasion_id: int | None = None,
    participant_count: int | None = None,
) -> str:
    with_n = [text for text in _pool.VICTORY if "{n}" in text]
    without_n = [text for text in _pool.VICTORY if "{n}" not in text]
    values = dict(user=username, card=card_name, region=region_name, enemy_name=enemy_name)
    if participant_count is not None:
        values["n"] = _fa(participant_count)
    return _render(with_n if participant_count is not None else without_n,
                   key=_key("victory", region_name, enemy_name, invasion_id=invasion_id),
                   rng=rng, **values)


def render_defeat(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random, invasion_id: int | None = None,
) -> str:
    return _render(_pool.DEFEAT, key=_key("defeat", region_name, enemy_name, invasion_id=invasion_id), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)


def render_expired(
    *, username: str, card_name: str, region_name: str,
    enemy_name: str, rng: random.Random, invasion_id: int | None = None,
) -> str:
    return _render(_pool.EXPIRED, key=_key("expired", region_name, enemy_name, invasion_id=invasion_id), rng=rng,
                   user=username, card=card_name, region=region_name, enemy_name=enemy_name)
