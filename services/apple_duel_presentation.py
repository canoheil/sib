"""Truthful Persian scenes, dynamic existing card art, DB-driven delivery."""

import asyncio
import io
import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from aiogram.types import BufferedInputFile
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from sib.core.logging import logger
from sib.core.text import safe_name
from sib.database.connection import get_session
from sib.database.models import AppleBattle, AppleDuel
from sib.services.apple_duel import AppleDuelService, now_utc
from sib.services.character_art import resolve_character_photo
from sib.services.sib_claim import fa_digits
from sqlalchemy import select, update

DUEL_SCENE_DELAY_SECONDS = 3.0
_play_locks: dict[int, asyncio.Lock] = {}

# Pre-generated cast-free arena plates (see assets/apple_duel/manifest.json
# and .agents/tasks/apple-duel-cinematic-presentation/ART_DIRECTION.md).
# The runtime never generates images; unreadable/missing plates fall back to
# the procedural composition below, never to a wrong-identity picture.
DUEL_SCENES_DIR = Path(__file__).resolve().parent.parent / "assets" / "apple_duel"
DUEL_MANIFEST_PATH = DUEL_SCENES_DIR / "manifest.json"


@lru_cache(maxsize=1)
def _plate_files():
    """index -> plate path from the manifest; an empty mapping is valid."""
    try:
        raw = json.loads(DUEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Apple Duel plate manifest unreadable; procedural fallback")
        return {}
    plates = {}
    for scene in raw.get("scenes", []):
        if scene.get("status") == "ready" and scene.get("cast_free"):
            plates[scene["index"]] = DUEL_SCENES_DIR / scene["file"]
    return plates


@lru_cache(maxsize=8)
def scene_plate_bytes(index):
    """Pre-generated arena plate bytes for a scene, or None for fallback."""
    path = _plate_files().get(index)
    if path is None or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except Exception:
        logger.exception("Apple Duel plate unreadable %s", path)
        return None


def scene_text(duel, battle, index):
    """Persian caption of one indexed scene (0-5).

    Player-facing narration only: internal identifiers (the duel UUID,
    the battle row id, the RNG seed) are never rendered into the group
    message. Scene 5 is the terminal result - winner plus the reward lines,
    and nothing else.
    """
    cards = json.loads(duel.snapshot_json)["cards"]
    for card in cards:
        card["player_name"] = safe_name(card["player_name"])
        card["name"] = safe_name(card["name"])
    a, b = cards
    winner = next(c for c in cards if c["user_id"] == battle.winner_id)
    scenes = [
        f"⚔️ **دوئل سیبی**\n\n"
        f"**{a['player_name']}** با **{a['name']}** (سطح **{fa_digits(a['level'])}**)\n"
        f"**{b['player_name']}** با **{b['name']}** (سطح **{fa_digits(b['level'])}**)\n\n"
        "🔥 دو کارت، یه میدان. نبرد شروع شد!",
        "👀 **سنجش حریف**\n\nدو نگاه قفل شد؛ هیچ‌کدوم کوتاه نمیان!",
        f"⚔️ **برخورد اول**\n\n**{a['player_name']}** جلو رفت؛ **{b['player_name']}** محکم وایساد!",
        f"💥 **تغییر جریان**\n\n**{a['name']}** و **{b['name']}** برگشتن وسط قاب! هنوز همه‌چیز بازه!",
        f"⚡ **ضربهٔ آخر**\n\n**{winner['name']}** جلو افتاد! حالا وقت اعلام برنده‌ست!",
        f"🏆 **پایان دوئل سیبی**\n\nبرنده: **{winner['player_name']}** با **{winner['name']}**\n"
        "🎁 **۱ نهال** و **۵ سیب** برای برنده؛ در ساعت طلایی، پاداش سیب دوبرابر می‌شه. "
        "بازنده چیزی از دست نداد.",
    ]
    return scenes[index]


def compose_frame(arts, stage, background=None):
    """Facing-card composition over a pre-generated plate (or procedural).

    ``background`` is a cast-free arena plate from ``assets/apple_duel``;
    without it the original procedural backdrop keeps every scene sendable.
    """
    configs = [(90, 10), (115, 6), (140, -8), (150, 10), (165, 16), (175, 0)]
    offset, angle = configs[stage]
    if background:
        with Image.open(io.BytesIO(background)) as plate:
            frame = ImageOps.cover(plate.convert("RGB"), (1000, 650))
    else:
        colors = [
            (28, 36, 62),
            (36, 30, 52),
            (62, 25, 42),
            (52, 38, 24),
            (68, 43, 16),
            (20, 62, 48),
        ]
        frame = Image.new("RGB", (1000, 650), colors[stage])
        glow = Image.new("RGB", frame.size, (0, 0, 0))
        draw = ImageDraw.Draw(glow)
        draw.ellipse((350 - stage * 15, 0, 650 + stage * 15, 650), fill=(115, 88, 43))
        glow = glow.filter(ImageFilter.GaussianBlur(70))
        from PIL import ImageChops

        frame = ImageChops.add(frame, glow)
        draw = ImageDraw.Draw(frame)
        # Light trails emphasize the facing composition without suggesting mechanics.
        for n in range(8):
            y = 75 + n * 70
            draw.line(
                (0, y, 500, 325 + (n - 4) * (stage + 2), 1000, 650 - y),
                fill=(80 + stage * 18, 64 + stage * 10, 45),
                width=2,
            )
    for i, raw in enumerate(arts):
        if raw:
            with Image.open(io.BytesIO(raw)) as image:
                card = ImageOps.contain(image.convert("RGB"), (330, 490)).convert(
                    "RGBA"
                )
        else:
            # Honest missing-art placeholder; never show a wrong character.
            card = Image.new("RGBA", (320, 470), (32, 36, 46, 255))
            ImageDraw.Draw(card).rounded_rectangle(
                (10, 10, 310, 460), 22, outline=(185, 145, 65), width=4
            )
        card = card.rotate(
            angle if i == 0 else -angle, resample=Image.Resampling.BICUBIC, expand=True
        )
        x = offset if i == 0 else 1000 - offset - card.width
        frame.paste(card, (x, (650 - card.height) // 2), card)
    if not background:
        ImageDraw.Draw(frame).line(
            (500, 45, 500, 605), fill=(224, 184, 92), width=2 + stage
        )
    result = io.BytesIO()
    frame.save(result, "JPEG", quality=88)
    return result.getvalue()


class AppleDuelPresentation:
    @staticmethod
    async def _photo(bot, duel, index):
        cards = json.loads(duel.snapshot_json)["cards"]
        sources = []
        async with get_session() as session:
            for card in cards:
                sources.append(
                    await resolve_character_photo(
                        session,
                        SimpleNamespace(code=card["code"], artwork=card.get("artwork")),
                    )
                )
        arts = []
        for file_id, path in sources:
            if file_id:
                buf = io.BytesIO()
                await bot.download(file_id, destination=buf)
                arts.append(buf.getvalue())
            elif path:
                arts.append(await asyncio.to_thread(path.read_bytes))
            else:
                arts.append(None)
        background = await asyncio.to_thread(scene_plate_bytes, index)
        raw = await asyncio.to_thread(compose_frame, arts, index, background)
        return BufferedInputFile(raw, filename=f"duel-{duel.id}-{index}.jpg")

    @classmethod
    async def tick(cls, bot, duel_id):
        if bot is None:
            return
        async with get_session() as session:
            index = await AppleDuelService.claim_scene(session, duel_id)
        async with get_session() as session:
            duel = await session.get(AppleDuel, duel_id)
            if not duel:
                return
            battle = (
                await session.get(AppleBattle, duel.battle_id)
                if duel.battle_id
                else None
            )
        # Claim transaction has committed; even total Telegram failure cannot
        # roll back result, reward or re-run the engine.
        if index is not None:
            text = scene_text(duel, battle, index)
            try:
                try:
                    photo = await cls._photo(bot, duel, index)
                except Exception:
                    logger.exception(
                        "Apple Duel artwork unavailable %s/%s; use text", duel_id, index
                    )
                    photo = None
                if photo:
                    try:
                        await bot.send_photo(
                            duel.group_id, photo, caption=text, parse_mode="Markdown"
                        )
                    except Exception:
                        await bot.send_message(
                            duel.group_id, text, parse_mode="Markdown"
                        )
                else:
                    await bot.send_message(duel.group_id, text, parse_mode="Markdown")
            except Exception:
                logger.exception(
                    "Apple Duel scene delivery failed %s/%s", duel_id, index
                )
                # Transport failure (not a process crash): return the scene to
                # the durable queue so the next sweep retries the same scene
                # instead of silently skipping it. Result/reward untouched.
                async with get_session() as session:
                    if await AppleDuelService.release_scene(session, duel_id, index):
                        logger.warning(
                            "Apple Duel scene %s/%s queued for retry", duel_id, index
                        )
        if (
            duel.status in ("COMPLETED", "REJECTED", "EXPIRED")
            and not duel.terminal_presented
        ):
            await cls.publish_terminal(bot, duel, battle)

    @classmethod
    async def play(cls, bot, duel_id: int) -> None:
        """Play the durable scene queue, one scene at a time, for this duel."""
        lock = _play_locks.setdefault(duel_id, asyncio.Lock())
        async with lock:
            try:
                while True:
                    # Remember the cursor so a transport failure (which tick
                    # releases back to the same cursor) stops this worker.
                    async with get_session() as session:
                        before = await session.get(AppleDuel, duel_id)
                        if before is None:
                            logger.info(f"Apple Duel player exited {duel_id}: missing")
                            return
                        before_cursor = before.cursor
                        before_status = before.status
                        before_terminal_presented = before.terminal_presented

                    if before_status in ("COMPLETED", "REJECTED", "EXPIRED"):
                        if before_terminal_presented:
                            logger.info(
                                f"Apple Duel player exited {duel_id}: terminal presented"
                            )
                            return
                        # A terminal board may have failed transport previously;
                        # tick retries that durable presentation without claiming
                        # another scene.
                    elif before_status != "ACCEPTED" or before_cursor >= 6:
                        logger.info(f"Apple Duel player exited {duel_id}: no scenes")
                        return

                    await cls.tick(bot, duel_id)

                    async with get_session() as session:
                        duel = await session.get(AppleDuel, duel_id)
                        if duel is None:
                            logger.info(f"Apple Duel player exited {duel_id}: missing")
                            return
                        status = duel.status
                        cursor = duel.cursor
                        terminal_presented = duel.terminal_presented

                    if status in ("COMPLETED", "REJECTED", "EXPIRED"):
                        logger.info(
                            f"Apple Duel player exited {duel_id}: terminal "
                            f"(presented={terminal_presented})"
                        )
                        return
                    if status != "ACCEPTED" or cursor >= 6:
                        logger.info(f"Apple Duel player exited {duel_id}: no scenes")
                        return
                    if cursor <= before_cursor:
                        # tick releases failed scenes durably; maintenance will
                        # retry them on a later recovery sweep.
                        logger.info(f"Apple Duel player exited {duel_id}: no progress")
                        return

                    # claim_scene retains its durable retry deadline (normally
                    # four seconds). Once delivery advanced the cursor, make
                    # the next scene immediately claimable; this task's 1.5s
                    # sleep is the presentation pacing, and recovery can still
                    # resume immediately after a process restart.
                    async with get_session() as session:
                        await session.execute(
                            update(AppleDuel)
                            .where(
                                AppleDuel.id == duel_id,
                                AppleDuel.status == "ACCEPTED",
                                AppleDuel.cursor == cursor,
                            )
                            .values(next_scene_at=now_utc())
                        )

                    await asyncio.sleep(DUEL_SCENE_DELAY_SECONDS)
            except Exception:
                logger.warning(
                    f"Apple Duel player failed {duel_id}", exc_info=True
                )

    @staticmethod
    async def publish_terminal(bot, duel, battle):
        from aiogram.exceptions import TelegramBadRequest

        text = (
            scene_text(duel, battle, 5)
            if battle
            else "❌ دعوت دوئل رد شد."
            if duel.status == "REJECTED"
            else "⌛ وقت دعوت دوئل تموم شد."
        )
        try:
            if duel.message_id:
                try:
                    await bot.edit_message_text(
                        text,
                        chat_id=duel.group_id,
                        message_id=duel.message_id,
                        reply_markup=None,
                        parse_mode="Markdown",
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        await bot.send_message(
                            duel.group_id, text, parse_mode="Markdown"
                        )
                except Exception:
                    await bot.send_message(duel.group_id, text, parse_mode="Markdown")
            elif battle:
                await bot.send_message(duel.group_id, text, parse_mode="Markdown")
            async with get_session() as session:
                stored = await session.get(AppleDuel, duel.id)
                stored.terminal_presented = True
        except Exception:
            logger.exception(
                "Apple Duel final board delivery failed %s; retry next sweep", duel.id
            )

    @classmethod
    async def recover_and_publish(cls, bot):
        """Existing maintenance calls this; restart needs no process-local task."""
        now = now_utc()
        async with get_session() as session:
            expired = list(
                (
                    await session.scalars(
                        select(AppleDuel.id).where(
                            AppleDuel.status == "PENDING", AppleDuel.expires_at <= now
                        )
                    )
                ).all()
            )
            for duel_id in expired:
                await AppleDuelService.expire(session, duel_id, now=now)
        if bot is None:
            return
        async with get_session() as session:
            ids = list(
                (
                    await session.scalars(
                        select(AppleDuel.id).where(
                            (
                                (AppleDuel.status == "ACCEPTED")
                                & (AppleDuel.next_scene_at <= now)
                            )
                            | (
                                AppleDuel.status.in_(
                                    ("COMPLETED", "REJECTED", "EXPIRED")
                                )
                                & AppleDuel.terminal_presented.is_(False)
                            )
                        )
                    )
                ).all()
            )
        for duel_id in ids:
            try:
                schedule_play(bot, duel_id)
            except Exception:
                logger.exception("Could not schedule Apple Duel recovery %s", duel_id)


_play_tasks: dict[int, asyncio.Task] = {}


def schedule_play(bot, duel_id: int) -> asyncio.Task:
    """Schedule a retained, deduplicated background player for one duel."""
    existing = _play_tasks.get(duel_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(AppleDuelPresentation.play(bot, duel_id))
    _play_tasks[duel_id] = task

    def _cleanup(done_task: asyncio.Task) -> None:
        if _play_tasks.get(duel_id) is done_task:
            _play_tasks.pop(duel_id, None)

    task.add_done_callback(_cleanup)
    return task
