"""A calm, once-a-day official-channel snapshot of real group-game activity.

The maintenance worker checks the configured Asia/Tehran hour (20:00 by
default). At most one short post is attempted for a Tehran calendar day, and
only if at least one real league source was active that day. Quiet days are
recorded as skipped instead of being filled with generic promotional copy.
All scores and activity are read from their source tables; this service never
writes a score or pays a reward.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from sib.core.config import settings as app_settings
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import (
    AppleDuel,
    AppleMineShift,
    DailyChannelPost,
    Group,
    GroupWar,
    Match,
    MatchState,
    MatchType,
    User,
    WarAttack,
)
from sib.services.config_store import ConfigService
from sib.services.group_league import (
    GroupLeagueService,
    jalali_label,
    week_key,
)
from sib.services.sib_claim import fa_digits

TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT_POST_HOUR = 20
_PERSIAN_WEEKDAYS = (
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنجشنبه",
    "جمعه",
    "شنبه",
    "یکشنبه",
)


@dataclass(frozen=True)
class DailyDigestSettings:
    enabled: bool
    hour: int
    channel_id: str


@dataclass(frozen=True)
class DailyActivity:
    group_matches: int = 0
    finished_wars: int = 0
    war_damage: int = 0
    normal_mine_apples: int = 0
    completed_duels: int = 0
    moment: str = ""

    @property
    def has_activity(self) -> bool:
        return bool(
            self.group_matches
            or self.finished_wars
            or self.war_damage
            or self.normal_mine_apples
            or self.completed_duels
        )


@dataclass(frozen=True)
class DigestPreview:
    day: date
    text: str = ""
    reason: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True)
class DigestRunResult:
    status: str
    day: date
    reason: str = ""
    message_id: Optional[int] = None
    preview: Optional[DigestPreview] = None


class DailyChannelDigestService:
    """Compose, schedule, and deliver the daily channel digest."""

    @staticmethod
    def _now_utc(now: Optional[datetime] = None) -> datetime:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            # Naive datetimes in tests/tools are interpreted as UTC, matching
            # the rest of the maintenance service's clock contract.
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            folded = value.strip().lower()
            if folded in {"true", "1", "yes", "on"}:
                return True
            if folded in {"false", "0", "no", "off", ""}:
                return False
        return default

    @classmethod
    async def load_settings(cls, session) -> DailyDigestSettings:
        enabled = cls._as_bool(
            await ConfigService.get(session, "daily_digest_enabled"), False
        )
        try:
            hour = int(await ConfigService.get(session, "daily_digest_hour"))
        except (TypeError, ValueError):
            hour = DEFAULT_POST_HOUR
        hour = min(23, max(0, hour))
        raw_channel = str(
            await ConfigService.get(session, "daily_digest_channel_id") or ""
        ).strip()
        channel_id = raw_channel or str(app_settings.OFFICIAL_CHANNEL_ID).strip()
        return DailyDigestSettings(
            enabled=enabled,
            hour=hour,
            channel_id=channel_id,
        )

    @staticmethod
    def _day_window(now: datetime) -> tuple[date, datetime, datetime]:
        local = now.astimezone(TEHRAN)
        local_start = datetime.combine(local.date(), time.min, tzinfo=TEHRAN)
        start = local_start.astimezone(timezone.utc)
        end = now.astimezone(timezone.utc)
        return local.date(), start, end

    @staticmethod
    def _clean_title(value: object, *, fallback: str = "گروه") -> str:
        title = " ".join(str(value or "").replace("\n", " ").split())
        if not title:
            return fallback
        return title[:48].rstrip()

    @classmethod
    async def _daily_activity(
        cls, session, *, start: datetime, end: datetime
    ) -> DailyActivity:
        """Count only the same real, group-scoped activity used by the league."""
        if end <= start:
            return DailyActivity()

        matches = list(
            (
                await session.execute(
                    select(
                        Match.id,
                        Match.group1_id,
                        Match.group2_id,
                        Match.winner_group_id,
                        Match.is_draw,
                    )
                    .where(Match.state == MatchState.FINISHED)
                    .where(Match.match_type == MatchType.NORMAL)
                    .where(Match.is_bot_match.is_(False))
                    .where(Match.finished_at >= start)
                    .where(Match.finished_at < end)
                    .where(
                        (Match.group1_id.is_not(None))
                        | (Match.group2_id.is_not(None))
                    )
                )
            ).all()
        )

        wars = list(
            (
                await session.execute(
                    select(
                        GroupWar.id,
                        GroupWar.attacker_id,
                        GroupWar.defender_id,
                        GroupWar.winner_id,
                    )
                    .where(GroupWar.status == "finished")
                    .where(GroupWar.ends_at >= start)
                    .where(GroupWar.ends_at < end)
                    .order_by(GroupWar.ends_at.desc(), GroupWar.id.desc())
                )
            ).all()
        )

        war_damage = await session.scalar(
            select(func.coalesce(func.sum(WarAttack.damage), 0))
            .join(User, User.id == WarAttack.user_id)
            .where(WarAttack.created_at >= start)
            .where(WarAttack.created_at < end)
            .where(User.is_bot.is_(False))
        )

        mine_apples = await session.scalar(
            select(func.coalesce(func.sum(AppleMineShift.amount), 0))
            .join(User, User.id == AppleMineShift.user_id)
            .where(AppleMineShift.group_id.is_not(None))
            .where(AppleMineShift.item_code == "apple_normal")
            .where(AppleMineShift.worked_at >= start)
            .where(AppleMineShift.worked_at < end)
            .where(User.is_bot.is_(False))
        )

        inviter = aliased(User)
        recipient = aliased(User)
        duel_count = await session.scalar(
            select(func.count(AppleDuel.id))
            .join(inviter, inviter.id == AppleDuel.inviter_id)
            .join(recipient, recipient.id == AppleDuel.recipient_id)
            .where(AppleDuel.status == "ACCEPTED")
            .where(AppleDuel.battle_id.is_not(None))
            .where(AppleDuel.created_at >= start)
            .where(AppleDuel.created_at < end)
            .where(inviter.is_bot.is_(False))
            .where(recipient.is_bot.is_(False))
        )

        group_ids: set[int] = set()
        wins: dict[int, int] = {}
        for _match_id, group1_id, group2_id, winner_id, is_draw in matches:
            for group_id in (group1_id, group2_id):
                if group_id is not None:
                    group_ids.add(int(group_id))
            if (
                not is_draw
                and winner_id is not None
                and winner_id in (group1_id, group2_id)
            ):
                wins[int(winner_id)] = wins.get(int(winner_id), 0) + 1
        for _war_id, attacker_id, defender_id, winner_id in wars:
            group_ids.update((int(attacker_id), int(defender_id)))
            if winner_id in (attacker_id, defender_id):
                group_ids.add(int(winner_id))

        titles = {}
        if group_ids:
            titles = dict(
                (
                    await session.execute(
                        select(Group.id, Group.title).where(Group.id.in_(group_ids))
                    )
                ).all()
            )

        moment = ""
        if (
            wars
            and wars[0][3] is not None
            and wars[0][3] in (wars[0][1], wars[0][2])
        ):
            _war_id, _attacker, _defender, winner_id = wars[0]
            winner = cls._clean_title(
                titles.get(int(winner_id)), fallback=f"گروه {winner_id}"
            )
            moment = (
                f"در آخرین جنگ امروز، «{winner}» پیروز شد."
            )
        elif wins:
            winner_id, win_count = min(
                wins.items(), key=lambda item: (-item[1], item[0])
            )
            winner = cls._clean_title(
                titles.get(winner_id), fallback=f"گروه {winner_id}"
            )
            moment = (
                f"«{winner}» با {fa_digits(win_count)} برد، "
                "پر‌بردترین گروه در بازی‌های امروز بود."
            )

        return DailyActivity(
            group_matches=len(matches),
            finished_wars=len(wars),
            war_damage=max(0, int(war_damage or 0)),
            normal_mine_apples=max(0, int(mine_apples or 0)),
            completed_duels=max(0, int(duel_count or 0)),
            moment=moment,
        )

    @classmethod
    async def build_preview(
        cls, session, *, now: Optional[datetime] = None
    ) -> DigestPreview:
        """Build the exact post text without sending or recording a decision."""
        moment = cls._now_utc(now)
        day, start, end = cls._day_window(moment)
        league = await GroupLeagueService.settings(session)
        if not league["enabled"]:
            return DigestPreview(day=day, reason="league_disabled")

        activity = await cls._daily_activity(session, start=start, end=end)
        if not activity.has_activity:
            return DigestPreview(day=day, reason="quiet_day")

        rows = await GroupLeagueService.standings(
            session, week_key(moment), points=league["points"]
        )
        if not rows:
            return DigestPreview(day=day, reason="no_league_score")

        local = moment.astimezone(TEHRAN)
        activity_parts = []
        if activity.group_matches:
            activity_parts.append(
                f"🎮 {fa_digits(activity.group_matches)} بازی گروهی"
            )
        if activity.finished_wars:
            activity_parts.append(
                f"⚔️ {fa_digits(activity.finished_wars)} جنگ تمام‌شده"
            )
        if activity.war_damage:
            activity_parts.append(
                f"🛡️ {fa_digits(activity.war_damage)} دمیج در جنگ‌ها"
            )
        if activity.normal_mine_apples:
            activity_parts.append(
                f"⛏ {fa_digits(activity.normal_mine_apples)} سیب معمولی از معدن"
            )
        if activity.completed_duels:
            activity_parts.append(
                f"🥊 {fa_digits(activity.completed_duels)} دوئل گروهی"
            )

        lines = [
            "🍏 نبض روزانهٔ لیگ سیب",
            f"{_PERSIAN_WEEKDAYS[day.weekday()]} {jalali_label(day)} · گزارش تا ساعت "
            f"{fa_digits(f'{local.hour:02d}:{local.minute:02d}')} تهران",
            "",
        ]
        if activity.moment:
            lines.extend([f"✨ {activity.moment}", ""])
        lines.append("امروز: " + " · ".join(activity_parts))
        lines.extend(["", "🏆 جدول زندهٔ این هفته"])

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for row in rows[:3]:
            title = cls._clean_title(row.title, fallback=f"گروه {row.group_id}")
            lines.append(
                f"{medals.get(row.rank, '•')} {title} — "
                f"{fa_digits(row.points)} امتیاز"
            )

        return DigestPreview(day=day, text="\n".join(lines))

    @classmethod
    def _join_keyboard(cls) -> InlineKeyboardMarkup:
        username = str(app_settings.BOT_USERNAME or "sib_game_bot").lstrip("@").strip()
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🎮 آوردن سیب به گروه",
                        url=f"https://t.me/{username}?startgroup=true",
                        style="primary",
                    )
                ]
            ]
        )

    @classmethod
    async def _record_skipped(
        cls,
        day: date,
        channel_id: str,
        now: datetime,
        reason: str,
    ) -> bool:
        try:
            async with get_session() as session:
                session.add(
                    DailyChannelPost(
                        post_day=day,
                        status="skipped",
                        channel_id=channel_id,
                        attempt_count=0,
                        attempted_at=now,
                        skip_reason=reason[:120],
                    )
                )
                await session.commit()
            return True
        except IntegrityError:
            # Another process already made today's durable decision.
            return False

    @classmethod
    async def _claim_send(
        cls,
        day: date,
        channel_id: str,
        now: datetime,
        content_hash: str,
        *,
        retry_failed: bool = False,
    ) -> tuple[bool, str]:
        """Atomically claim this day's only send, or claim an admin retry."""
        async with get_session() as session:
            existing = await session.get(DailyChannelPost, day)
            if existing is not None:
                if not retry_failed or existing.status != "failed":
                    return False, existing.status
                result = await session.execute(
                    update(DailyChannelPost)
                    .where(
                        DailyChannelPost.post_day == day,
                        DailyChannelPost.status == "failed",
                    )
                    .values(
                        status="sending",
                        channel_id=channel_id,
                        attempt_count=DailyChannelPost.attempt_count + 1,
                        attempted_at=now,
                        sent_at=None,
                        message_id=None,
                        content_hash=content_hash,
                        skip_reason=None,
                        error=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                return result.rowcount == 1, "sending" if result.rowcount == 1 else "claimed"

            session.add(
                DailyChannelPost(
                    post_day=day,
                    status="sending",
                    channel_id=channel_id,
                    attempt_count=1,
                    attempted_at=now,
                    content_hash=content_hash,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False, "claimed"
            return True, "sending"

    @classmethod
    async def _mark_result(
        cls,
        day: date,
        *,
        status: str,
        now: datetime,
        message_id: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        async with get_session() as session:
            result = await session.execute(
                update(DailyChannelPost)
                .where(
                    DailyChannelPost.post_day == day,
                    DailyChannelPost.status == "sending",
                )
                .values(
                    status=status,
                    sent_at=now if status == "sent" else None,
                    message_id=message_id,
                    error=error[:1000] if error else None,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if result.rowcount != 1:
                logger.error(
                    "daily channel digest delivery state was not updated day=%s status=%s",
                    day,
                    status,
                )

    @classmethod
    async def _record_status(cls, day: date) -> Optional[DailyChannelPost]:
        async with get_session() as session:
            return await session.get(DailyChannelPost, day)

    @classmethod
    async def _publish(
        cls,
        bot,
        *,
        now: datetime,
        config: DailyDigestSettings,
        retry_failed: bool = False,
    ) -> DigestRunResult:
        day = now.astimezone(TEHRAN).date()
        existing = await cls._record_status(day)
        if existing is not None and not (retry_failed and existing.status == "failed"):
            return DigestRunResult(
                status=existing.status,
                day=day,
                reason=existing.skip_reason or existing.error or "already_decided",
                message_id=existing.message_id,
            )

        async with get_session() as session:
            preview = await cls.build_preview(session, now=now)

        if not preview.ready:
            if existing is not None and retry_failed:
                # Do not keep retrying a failed post if the underlying content
                # disappeared (e.g. an operator disabled the league meanwhile).
                return DigestRunResult(
                    status=existing.status,
                    day=day,
                    reason=preview.reason,
                    preview=preview,
                )
            await cls._record_skipped(
                day, config.channel_id, now, preview.reason or "no_content"
            )
            return DigestRunResult(
                status="skipped",
                day=day,
                reason=preview.reason or "no_content",
                preview=preview,
            )

        digest_hash = hashlib.sha256(preview.text.encode("utf-8")).hexdigest()
        claimed, claim_status = await cls._claim_send(
            day,
            config.channel_id,
            now,
            digest_hash,
            retry_failed=retry_failed,
        )
        if not claimed:
            return DigestRunResult(
                status=claim_status,
                day=day,
                reason="already_claimed",
                preview=preview,
            )

        try:
            sent = await bot.send_message(
                chat_id=config.channel_id,
                text=preview.text,
                reply_markup=cls._join_keyboard(),
                parse_mode=None,
            )
        except Exception as exc:
            await cls._mark_result(
                day,
                status="failed",
                now=now,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.warning(
                "daily channel digest delivery failed day=%s channel=%s",
                day,
                config.channel_id,
                exc_info=True,
            )
            return DigestRunResult(
                status="failed",
                day=day,
                reason=str(exc)[:240],
                preview=preview,
            )

        message_id = getattr(sent, "message_id", None)
        await cls._mark_result(
            day, status="sent", now=now, message_id=message_id
        )
        logger.info(
            "daily channel digest sent day=%s channel=%s message_id=%s",
            day,
            config.channel_id,
            message_id,
        )
        return DigestRunResult(
            status="sent",
            day=day,
            message_id=message_id,
            preview=preview,
        )

    @classmethod
    async def run_scheduled(
        cls, bot, *, now: Optional[datetime] = None
    ) -> DigestRunResult:
        """Attempt today's post after the configured Tehran hour, at most once."""
        moment = cls._now_utc(now)
        day = moment.astimezone(TEHRAN).date()
        async with get_session() as session:
            config = await cls.load_settings(session)
        if not config.enabled:
            return DigestRunResult(status="disabled", day=day, reason="disabled")
        local = moment.astimezone(TEHRAN)
        if local.hour < config.hour:
            return DigestRunResult(status="not_due", day=day, reason="not_due")
        if not config.channel_id:
            return DigestRunResult(status="misconfigured", day=day, reason="no_channel")
        return await cls._publish(bot, now=moment, config=config)

    @classmethod
    async def retry_failed_today(
        cls, bot, *, now: Optional[datetime] = None
    ) -> DigestRunResult:
        """Admin-only retry; never retries a post with an uncertain send state."""
        moment = cls._now_utc(now)
        day = moment.astimezone(TEHRAN).date()
        async with get_session() as session:
            config = await cls.load_settings(session)
        if not config.enabled:
            return DigestRunResult(status="disabled", day=day, reason="disabled")
        return await cls._publish(
            bot, now=moment, config=config, retry_failed=True
        )

    @classmethod
    async def control_state(
        cls, *, now: Optional[datetime] = None
    ) -> tuple[DailyDigestSettings, Optional[DailyChannelPost]]:
        moment = cls._now_utc(now)
        day = moment.astimezone(TEHRAN).date()
        async with get_session() as session:
            config = await cls.load_settings(session)
            record = await session.get(DailyChannelPost, day)
        return config, record


def preview_reason_fa(reason: str) -> str:
    return {
        "league_disabled": "لیگ هفتگی خاموش است؛ برای همین پست ساخته نمی‌شود.",
        "quiet_day": "امروز فعالیت گروهیِ قابل گزارش ثبت نشده؛ پست خالی نمی‌گذاریم.",
        "no_league_score": "امروز فعالیتی ثبت شده، اما هنوز جدول قابل‌اتکایی برای نمایش نیست.",
        "not_due": "زمان انتشار امروز هنوز نرسیده است.",
        "already_decided": "تصمیم امروز قبلاً ثبت شده است.",
        "no_channel": "شناسهٔ کانال مقصد تنظیم نشده است.",
        "disabled": "انتشار خودکار خاموش است.",
    }.get(reason, reason or "محتوای امروز آماده نیست.")
