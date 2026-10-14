"""Read-only, deterministic Monte Carlo estimate of group Main Card attacks."""
import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sib.database.models import Character, Inv2GroupMember
from sib.services.card_progression import MAX_CARD_LEVEL, card_attributes
from sib.services.config_store import ConfigService, DEFAULT_INVASION_DIFFICULTY_MULTIPLIER
from sib.services.player_cards import PlayerCardService
from .damage import compute_damage
from .enemy import choose_enemy_type, compute_enemy_hp, enemy_spec
from .enums import EnemyType
from .power_resolver import get_power_resolver
from .power_resolver_impl import DbGroupPowerResolver

TRIALS = 2000
JOINERS = (1, 3, 5, 10)


async def simulate(session, group_raw: str, enemy_raw: str | None = None) -> str:
    try:
        group_id = int(group_raw)
        if not -(2**63) <= group_id < 2**63 or group_id == 0:
            raise ValueError("group")
        enemy = EnemyType(enemy_raw) if enemy_raw else choose_enemy_type(random.Random(group_id))
    except (ValueError, TypeError):
        return "نمونه: /inv2_simulate -100 soldier؛ گونهٔ دشمن اختیاری است."
    resolver = get_power_resolver() or DbGroupPowerResolver()
    average = await resolver.average_main_card_power(group_id=group_id, session=session)
    if average is None or not math.isfinite(average) or average <= 0:
        return "توان کارت‌های اصلی این گروه نامشخص است؛ شبیه‌سازی ایمن ممکن نیست."
    days = await ConfigService.get(session, "inv2_member_active_window_days")
    if type(days) not in (int, str) or not str(days).isdigit() or int(days) <= 0:
        return "پنجرهٔ فعالیت گروه معتبر نیست."
    members = (await session.scalars(select(Inv2GroupMember.user_id).where(
        Inv2GroupMember.group_id == group_id,
        Inv2GroupMember.last_seen_at >= datetime.now(timezone.utc) - timedelta(days=int(days))
    ).order_by(Inv2GroupMember.user_id))).all()
    if not members:
        return "عضو فعالِ ثبت‌شده‌ای در گروه نیست."
    powers = []
    for user_id in members:
        card = await PlayerCardService.get_main_card(session, user_id)
        if card is None or type(card.level) is not int or not 1 <= card.level <= MAX_CARD_LEVEL:
            return "توان یکی از کارت‌های اصلی نامشخص است."
        char = await session.get(Character, card.character_id)
        if char is None or any(type(getattr(char, key)) is not int or getattr(char, key) < 0
                               for key in ("power", "speed", "intelligence")):
            return "توان یکی از کارت‌های اصلی نامشخص است."
        powers.append(card_attributes(card, char).power)
    raw_difficulty = await ConfigService.get(session, "invasion_difficulty_multiplier")
    difficulty = float(DEFAULT_INVASION_DIFFICULTY_MULTIPLIER if raw_difficulty is None else raw_difficulty)
    if not math.isfinite(difficulty) or difficulty <= 0:
        return "سختی نبرد معتبر نیست."
    hp = compute_enemy_hp(average, enemy, difficulty)
    if hp <= 0:
        return "جان دشمن معتبر نیست."
    rng = random.Random(f"inv2sim:{group_id}:{enemy.value}:{','.join(map(str, powers))}:{hp}")
    lines = [f"⚔️ شبیه‌سازی گروه {group_id} | {enemy_spec(enemy).name_fa}",
             f"جان دشمن: {hp} | میانگین کارت: {average:.1f} | اعضای مشاهده‌شده: {len(powers)}"]
    for count in JOINERS:
        wins = total_damage = 0
        for _ in range(TRIALS):
            damage = sum(compute_damage(rng.choice(powers), rng)[0] for _ in range(count))
            total_damage += damage
            wins += damage >= hp
        lines.append(f"{count} نگهبان: آسیب موردانتظار {round(total_damage / TRIALS)} | بخت پیروزی {wins / TRIALS:.1%}")
    lines.append("برآورد با نمونه‌گیری مجدد از کارت‌های مشاهده‌شده؛ تضمین پیروزی نیست.")
    return "\n".join(lines)
