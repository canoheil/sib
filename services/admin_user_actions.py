"""Transactional admin mutations against the canonical inventories."""
import json
from datetime import datetime, timezone
from sqlalchemy import select
from sib.admin.auth import is_admin
from sib.database.models import User, AdminLog, AppleWallet, SeedlingInventory, PlayerCard, Character
from sib.database.models.player_xp import PlayerProgression
from sib.services.apple_wallet import AppleWalletService
from sib.services.seedling import SeedlingService
from sib.services.items import ItemService
from sib.services.player_cards import PlayerCardService
from sib.services.player_xp import PlayerXPService

FIELDS = {"apples": "🍏 سیب معمولی", "apple_gold": "🥇 طلایی", "apple_diamond": "💎 الماسی",
          "apple_obsidian": "🖤 ابسیدینی", "seedling": "🌱 نهال", "level": "⭐ سطح", "xp": "✨ تجربه"}

async def snapshot(session, uid):
    user = await session.get(User, uid)
    if not user:
        raise ValueError("کاربر پیدا نشد.")
    wallet = await session.get(AppleWallet, uid)
    seed = await session.get(SeedlingInventory, uid)
    progression = await session.get(PlayerProgression, uid)
    xp = progression.total_xp if progression else 0
    items = await ItemService.list_items(session, uid)
    cards = list((await session.scalars(select(PlayerCard).where(PlayerCard.user_id == uid))).all())
    values = {"apples": wallet.balance if wallet else 0, "seedling": seed.total if seed else 0,
              "xp": xp, "level": PlayerXPService.get_level_progress(xp).level}
    values.update({k: items.get(k, 0) for k in ("apple_gold", "apple_diamond", "apple_obsidian")})
    return user, values, cards

async def mutate(session, admin_id, uid, field, mode, value=None, *, confirmed=False, admin_confirmed=False):
    if not is_admin(admin_id):
        raise ValueError("فقط مدیران اجازه دسترسی دارند.")
    if is_admin(uid) and uid != admin_id and not admin_confirmed:
        raise ValueError("تأیید دوم برای ویرایش حساب مدیر لازم است.")
    user = await session.scalar(select(User).where(User.id == uid).with_for_update())
    if not user:
        raise ValueError("کاربر پیدا نشد.")
    if (mode == "sub" or field in ("ban", "mine", "claim")) and not confirmed:
        raise ValueError("ابتدا عملیات را تأیید کنید.")
    _, values, cards = await snapshot(session, uid)
    note = ""
    if field in FIELDS:
        if mode not in ("add", "sub") or not isinstance(value, int) or not 0 < value <= 2_000_000_000:
            raise ValueError("مقدار مثبت معتبر وارد کنید.")
        old = values[field]
        new = old + (value if mode == "add" else -value)
        if new < (1 if field == "level" else 0) or new > 2_000_000_000:
            raise ValueError("موجودی یا سطح نمی‌تواند منفی شود.")
        if field == "apples":
            fn = AppleWalletService.earn if mode == "add" else AppleWalletService.spend
            await fn(session, uid, value, "admin_adjust")
        elif field == "seedling":
            await SeedlingService.admin_adjust(session, uid, new-old)
        elif field.startswith("apple_"):
            fn = ItemService.grant if mode == "add" else ItemService.consume
            await fn(session, uid, field, value)
        else:
            xp = PlayerXPService.cumulative_xp_for_level(new) if field == "level" else new
            if xp > 87000:
                raise ValueError("تجربه از سقف مجاز بیشتر است.")
            row = await session.get(PlayerProgression, uid)
            if row is None:
                row = PlayerProgression(user_id=uid, total_xp=0)
                session.add(row)
            row.total_xp = xp
    elif field == "card":
        character = await session.scalar(select(Character).where(Character.code == str(value)))
        if not character:
            raise ValueError("کد کارت پیدا نشد.")
        card = next((c for c in cards if c.character_id == character.id), None)
        old = [{"code_id": c.character_id, "main": c.is_favorite} for c in cards]
        if mode == "add":
            await PlayerCardService.acquire(session, uid, character.id)
        elif mode == "set" and card:
            await PlayerCardService.set_favorite(session, uid, card.id)
        elif mode == "sub" and card:
            # Trading/party references need a dedicated removal policy: fail closed.
            raise ValueError("حذف کارت تا بررسی ارجاع‌های بازی و معامله غیرفعال است.")
        else:
            raise ValueError("کارت متعلق به کاربر نیست.")
        await session.flush()
        new = [{"code_id": c.character_id, "main": c.is_favorite} for c in
               (await session.scalars(select(PlayerCard).where(PlayerCard.user_id == uid))).all()]
    elif field == "ban":
        old = {"is_banned": user.is_banned, "reason": user.ban_reason}
        user.is_banned = bool(value)
        user.banned_at = datetime.now(timezone.utc).replace(tzinfo=None) if value else None
        user.banned_by = admin_id if value else None
        user.ban_reason = "مسدودسازی توسط مدیر" if value else None
        new = {"is_banned": user.is_banned, "reason": user.ban_reason}
        if value:
            note = "بررسی عضویت و حذف از پارتی به تعویق افتاد؛ مسدودیت فعلاً فقط در پنل ثبت می‌شود."
    elif field in ("mine", "claim"):
        raise ValueError("کول‌داون از تاریخچه پاداش محاسبه می‌شود؛ ریست بدون تغییر تاریخچه فعلاً پشتیبانی نمی‌شود.")
    else:
        raise ValueError("فیلد پشتیبانی نمی‌شود.")
    session.add(AdminLog(admin_id=admin_id, action="user_edit", details=json.dumps({
        "target_user_id": uid, "field": field, "old_value": old, "new_value": new,
        "note": note}, ensure_ascii=False)))
    await session.flush()
    return note
