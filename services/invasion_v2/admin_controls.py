"""Admin-only v2 database operations; caller authenticates and audits."""
import math
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select

from sib.database.models import Group, Inv2GroupState, Inv2Invasion, Inv2Region, SystemConfig
from sib.services.config_store import (
    CONFIG_DEFAULTS, ConfigService, DEFAULT_INVASION_DIFFICULTY_MULTIPLIER,
    DEFAULT_INVASION_JOIN_WINDOW_SECONDS, DEFAULT_INVASION_SPAWN_HOURS,
    DEFAULT_INVASION_SLOT_WINDOW_MINUTES,
)
from . import admin_runtime
from .circuit_breaker import is_group_available
from .enemy import choose_enemy_type, compute_enemy_hp, enemy_spec
from .farsi import fa_number
from .enums import EnemyType, InvasionStatus
from .power_resolver import get_power_resolver
from .scheduler import InvasionScheduler
from .rewards_service import settle_outcome

LIVE = (InvasionStatus.PENDING.value, InvasionStatus.ACTIVE.value)
NOW = lambda: datetime.now(timezone.utc)


def _date(value) -> str:
    if value is None:
        return "—"
    return value.isoformat(timespec="seconds")


def _integer(raw: str, *, minimum: int = 1, maximum: int = 10**9) -> int:
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError("خارج از بازهٔ مجاز")
    return value


def _group_id(raw: str) -> int:
    value = int(raw)
    if not -(2**63) <= value < 2**63 or value == 0:
        raise ValueError("شناسهٔ گروه نامعتبر است")
    return value


def _spawn_schedule_display(raw_hours, raw_window) -> tuple[str, int]:
    """Render the fixed Asia/Tehran spawn schedule for /inv2_status.

    Never raises: a missing, empty or malformed ``invasion_spawn_hours`` /
    ``invasion_slot_window_minutes`` config row falls back to the same
    defaults the scheduler itself uses (services/invasion_v2/scheduler.py).
    """
    hours_raw = str(raw_hours) if raw_hours else DEFAULT_INVASION_SPAWN_HOURS
    try:
        hours = sorted({int(part.strip()) for part in hours_raw.split(",") if part.strip() != ""})
    except ValueError:
        hours = []
    if not hours:
        hours = sorted(int(part.strip()) for part in DEFAULT_INVASION_SPAWN_HOURS.split(","))
    hours_text = "، ".join(f"{hour:02d}:00" for hour in hours)
    try:
        window = int(DEFAULT_INVASION_SLOT_WINDOW_MINUTES if not raw_window else raw_window)
    except (TypeError, ValueError):
        window = int(DEFAULT_INVASION_SLOT_WINDOW_MINUTES)
    return hours_text, window


async def status(session) -> str:
    flag = await ConfigService.get(session, "invasion_enabled")
    join = await ConfigService.get(session, "invasion_join_window_seconds")
    difficulty = await ConfigService.get(session, "invasion_difficulty_multiplier")
    raw_spawn_hours = await ConfigService.get(session, "invasion_spawn_hours")
    raw_slot_window = await ConfigService.get(session, "invasion_slot_window_minutes")
    hours_text, window_minutes = _spawn_schedule_display(raw_spawn_hours, raw_slot_window)
    active = await session.scalar(select(func.count()).select_from(Inv2Invasion).where(Inv2Invasion.status == "ACTIVE"))
    muted = await session.scalar(select(func.count()).select_from(Inv2GroupState).where(Inv2GroupState.muted_until > NOW()))
    lost = await session.scalar(select(func.count()).select_from(Inv2Region).where(Inv2Region.is_lost_to_enemy.is_(True)))
    return ("⚔️ وضعیت یورش\n"
            f"فعال: {'بله' if flag is True or str(flag).lower() == 'true' else 'خیر'}"
            f" · پنجرهٔ پیوستن: {fa_number(join if join is not None else DEFAULT_INVASION_JOIN_WINDOW_SECONDS)} ثانیه\n"
            f"ساعت‌های اسپان (تهران): {hours_text}\n"
            f"پنجره هر اسلات: {window_minutes} دقیقه"
            f" · سختی: {fa_number(difficulty if difficulty is not None else DEFAULT_INVASION_DIFFICULTY_MULTIPLIER)}\n"
            f"یورش فعال: {fa_number(active)} · گروه خاموش: {fa_number(muted)}"
            f" · ناحیهٔ از‌دست‌رفته: {fa_number(lost)}\n"
            f"آخرین تیک موفق: {_date(admin_runtime.last_successful_tick)}")


async def toggle(session, raw: str) -> str:
    if raw not in ("on", "off"):
        return "نمونه: /inv2_toggle on یا /inv2_toggle off"
    await ConfigService.set(session, "invasion_enabled", raw == "on")
    return f"⚔️ یورش {'فعال' if raw == 'on' else 'غیرفعال'} شد."


async def force_spawn(session, group_raw: str, region_code: str | None = None, enemy_raw: str | None = None) -> str:
    group_id = _group_id(group_raw)
    if not await is_group_available(group_id, session=session):
        return "گروه غیرفعال، خاموش یا سامانهٔ یورش غیرفعال است."
    group = await session.scalar(select(Group).where(Group.id == group_id).with_for_update().execution_options(populate_existing=True))
    if group is None or not group.is_active:
        return "گروه فعال پیدا نشد."
    max_active = _integer(str(await ConfigService.get(session, "invasion_max_active_per_group") or 1), maximum=100)
    live = await session.scalar(select(func.count()).select_from(Inv2Invasion).where(
        Inv2Invasion.group_id == group_id, Inv2Invasion.status.in_(LIVE)))
    if live >= max_active:
        return "سقف یورش‌های هم‌زمان این گروه پر است."
    # The group row is the per-group mutex shared with circuit-breaker writes.
    # Re-read the state under that lock, not from an earlier identity-map hit.
    state = await session.scalar(select(Inv2GroupState).where(
        Inv2GroupState.group_id == group_id).with_for_update().execution_options(populate_existing=True))
    if state is not None and state.muted_until is not None:
        muted = state.muted_until.replace(tzinfo=timezone.utc) if state.muted_until.tzinfo is None else state.muted_until
        if muted >= NOW():
            return "گروه خاموش شده است."
    resolver = get_power_resolver()
    if resolver is None:
        return "توان کارت‌های گروه نامشخص است؛ حل‌گر ثبت نشده است."
    average = await resolver.average_main_card_power(group_id=group_id, session=session)
    if average is None or not math.isfinite(average) or average <= 0:
        return "توان کارت‌های اصلی گروه نامشخص است؛ یورش ساخته نشد."
    if region_code:
        region = await session.get(Inv2Region, region_code)
        if region is None:
            return "کد ناحیه پیدا نشد."
    else:
        regions = list((await session.scalars(select(Inv2Region).order_by(Inv2Region.code))).all())
        if not regions:
            return "ناحیه‌ای ثبت نشده است."
        previous = await session.scalar(select(Inv2Invasion.region_code).where(
            Inv2Invasion.group_id == group_id).order_by(
            Inv2Invasion.scheduled_at.desc(), Inv2Invasion.id.desc()).limit(1))
        region = InvasionScheduler.choose_region(regions, previous, random.Random())
    try:
        enemy = EnemyType(enemy_raw) if enemy_raw else choose_enemy_type(random.Random())
    except ValueError:
        return "گونهٔ دشمن نامعتبر است."
    difficulty = float(await ConfigService.get(session, "invasion_difficulty_multiplier") or DEFAULT_INVASION_DIFFICULTY_MULTIPLIER)
    if not math.isfinite(difficulty) or difficulty <= 0:
        return "سختی یورش نامعتبر است."
    hp = compute_enemy_hp(average, enemy, difficulty)
    if hp <= 0:
        return "جان دشمن معتبر نیست."
    inv = Inv2Invasion(group_id=group_id, region_code=region.code, enemy_type=enemy.value,
        enemy_name_fa=enemy_spec(enemy).name_fa, enemy_hp=hp, enemy_hp_remaining=hp,
        difficulty_multiplier=difficulty, status=InvasionStatus.PENDING.value,
        scheduled_at=NOW(), occurrence_key=f"inv2:admin:{uuid.uuid4().hex}")
    session.add(inv)
    await session.flush()
    region.last_invasion_id = inv.id
    return f"⚔️ یورش شمارهٔ {inv.id} برای گروه {group_id} در {region.name_fa} آماده شد."


async def force_end(session, inv_raw: str, result_raw: str) -> str:
    if result_raw not in ("victory", "defeat"):
        return "نمونه: /inv2_force_end ۱۲ victory یا defeat"
    invasion_id = _integer(inv_raw)
    status_value = InvasionStatus.VICTORY.value if result_raw == "victory" else InvasionStatus.DEFEAT.value
    inv = await session.scalar(select(Inv2Invasion).where(Inv2Invasion.id == invasion_id).with_for_update())
    if inv is None:
        return "یورش پیدا نشد."
    if inv.status == status_value:
        await settle_outcome(session, inv.id, status_value)  # retry incomplete settlement
        return f"یورش شمارهٔ {inv.id} پیش‌تر با همین نتیجه پایان یافته است."
    if inv.status != InvasionStatus.ACTIVE.value:
        return "تنها یورش فعال را می‌توان پایان داد."
    inv.status = status_value
    inv.finalized_at = NOW()
    await session.flush()
    await settle_outcome(session, inv.id, status_value)
    return f"⚔️ یورش شمارهٔ {inv.id} با {'پیروزی' if result_raw == 'victory' else 'شکست'} پایان یافت."


async def regions(session) -> str:
    rows = (await session.scalars(select(Inv2Region).order_by(Inv2Region.code))).all()
    return "🗺️ نواحی اواریا\n" + ("\n".join(
        f"{r.code} · {r.name_fa} · {r.controlling_faction} · {'از‌دست‌رفته' if r.is_lost_to_enemy else 'آزاد'} · آخرین: {r.last_invasion_id or '—'}"
        for r in rows) or "ناحیه‌ای ثبت نشده است.")


async def reset_region(session, code: str) -> str:
    region = await session.scalar(select(Inv2Region).where(Inv2Region.code == code).with_for_update())
    if region is None:
        return "ناحیه پیدا نشد."
    region.is_lost_to_enemy = False
    return f"🗺️ ناحیهٔ {region.name_fa} آزاد شد."


async def mute(session, group_raw: str, hours_raw: str) -> str:
    group_id = _group_id(group_raw)
    hours = _integer(hours_raw, maximum=24 * 365)
    group = await session.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None:
        return "گروه پیدا نشد."
    state = await session.get(Inv2GroupState, group_id)
    if state is None:
        state = Inv2GroupState(group_id=group_id)
        session.add(state)
    state.muted_until = NOW() + timedelta(hours=hours)
    return f"🔕 گروه {group_id} برای {fa_number(hours)} ساعت خاموش شد."


async def unmute(session, group_raw: str) -> str:
    group_id = _group_id(group_raw)
    group = await session.scalar(select(Group).where(Group.id == group_id).with_for_update())
    if group is None:
        return "گروه پیدا نشد."
    state = await session.get(Inv2GroupState, group_id)
    if state is None:
        return "گروه از پیش خاموش نیست."
    state.muted_until = None
    state.consecutive_failures = 0
    return f"🔔 گروه {group_id} از خاموشی بیرون آمد."


async def group_info(session, group_raw: str) -> str:
    group_id = _group_id(group_raw)
    group = await session.get(Group, group_id)
    if group is None:
        return "گروه پیدا نشد."
    state = await session.get(Inv2GroupState, group_id)
    inv = await session.scalar(select(Inv2Invasion).where(
        Inv2Invasion.group_id == group_id, Inv2Invasion.status.in_(LIVE)).order_by(Inv2Invasion.id.desc()).limit(1))
    return (f"⚔️ گروه {group_id} · {'فعال' if group.is_active else 'غیرفعال'}\n"
            f"خطاهای ارسال: {fa_number(state.consecutive_failures if state else 0)}"
            f" · خاموش تا: {_date(state.muted_until if state else None)}\n"
            f"پیروزی: {fa_number(state.total_victories if state else 0)}"
            f" · شکست: {fa_number(state.total_defeats if state else 0)}\n"
            f"یورش جاری: {inv.id if inv else '—'}")


async def invasions(session, status_filter: str | None = None) -> str:
    if status_filter and status_filter not in {v.value for v in InvasionStatus}:
        return ("وضعیت نامعتبر است؛ یکی از این‌ها را بنویسید: PENDING (در انتظار)،"
                " ACTIVE (فعال)، VICTORY (پیروزی)، DEFEAT (شکست)، EXPIRED (منقضی).")
    query = select(Inv2Invasion).order_by(Inv2Invasion.id.desc()).limit(20)
    if status_filter:
        query = query.where(Inv2Invasion.status == status_filter)
    rows = (await session.scalars(query)).all()
    return "⚔️ یورش‌های اخیر\n" + ("\n".join(
        f"#{r.id} گروه {r.group_id} · {r.region_code} · {r.enemy_type} · {r.status}"
        f" · جان: {fa_number(r.enemy_hp_remaining)}/{fa_number(r.enemy_hp)}"
        f" · آغاز: {_date(r.started_at)} · پایان: {_date(r.ends_at)}"
        for r in rows) or "یورشی ثبت نشده است.")


async def config(session) -> str:
    rows = (await session.scalars(select(SystemConfig).where(
        or_(SystemConfig.key.like("inv2_%"), SystemConfig.key.like("invasion_%"))).order_by(SystemConfig.key))).all()
    return "⚙️ تنظیمات یورش\n" + ("\n".join(f"{r.key}: {r.value}" for r in rows) or "تنظیمی ثبت نشده است.")


async def set_config(session, key: str, value_raw: str) -> str:
    # Only implemented inv2 keys, never arbitrary config/flag keys.
    allowed = {key for key in CONFIG_DEFAULTS if key.startswith("inv2_")}
    if key not in allowed:
        return "کلید مجاز نیست؛ تنها کلیدهای شناخته‌شدهٔ inv2_* پذیرفته‌اند."
    value = _integer(value_raw, maximum=3650)
    await ConfigService.set(session, key, value)
    return f"⚙️ {key} برابر {value} ثبت شد."


async def health(session) -> str:
    flag = await ConfigService.get(session, "invasion_enabled")
    active = await session.scalar(select(func.count()).select_from(Inv2Invasion).where(Inv2Invasion.status == "ACTIVE"))
    muted = await session.scalar(select(func.count()).select_from(Inv2GroupState).where(Inv2GroupState.muted_until > NOW()))
    lost = await session.scalar(select(func.count()).select_from(Inv2Region).where(Inv2Region.is_lost_to_enemy.is_(True)))
    duration = admin_runtime.last_tick_duration_ms
    return (f"🩺 سلامت یورش · {'فعال' if flag is True or str(flag).lower() == 'true' else 'غیرفعال'}\n"
            f"یورش: {fa_number(active)} · گروه خاموش: {fa_number(muted)} · ناحیهٔ از‌دست‌رفته: {fa_number(lost)}\n"
            f"آخرین تیک موفق: {_date(admin_runtime.last_successful_tick)}\n"
            f"آخرین اجرای تیک: {_date(admin_runtime.last_tick_at)} · مدت: {fa_number(duration) if duration is not None else '—'} میلی‌ثانیه\n"
            f"زمان آخرین خطا: {_date(admin_runtime.last_error_at)}\n"
            f"آخرین خطای این فرایند: {admin_runtime.last_error_message or admin_runtime.last_error or '—'}")
