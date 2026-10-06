"""Account XP curve, ledger, cap, classification, and match integration tests."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aiogram.types import User as TelegramUser

from sib.database.models import (
    MatchType,
    PlayerProgression,
    PlayerXPGrant,
    User,
)
from sib.games.base import GameResult
from sib.services.match_result import MatchResultService
from sib.services.player_xp import (
    GLOBAL_DAILY_XP_CAP,
    MAX_LEVEL,
    MAX_TOTAL_XP,
    PlayerXPService,
)
from sib.tests.conftest import requires_row_locks
from sib.tests.test_group_wins import _create_match


CUMULATIVE_XP = (
    0,
    60,
    150,
    290,
    500,
    800,
    1_210,
    1_750,
    2_440,
    3_300,
    4_350,
    5_610,
    7_100,
    8_840,
    10_850,
    13_150,
    15_760,
    18_700,
    21_990,
    25_650,
    29_700,
    34_160,
    39_050,
    44_390,
    50_200,
    56_500,
    63_310,
    70_650,
    78_540,
    87_000,
)


async def _new_user(session, *, is_bot: bool = False) -> User:
    user_id = int(uuid4().int % 1_000_000_000) + 1
    user = User(
        id=user_id,
        first_name=f"XP {user_id}",
        language_code="fa",
        is_bot=is_bot,
    )
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_new_registered_account_starts_at_level_one_zero_xp(db_session):
    from sib.services.user import UserService

    tg_user = TelegramUser(
        id=int(uuid4().int % 1_000_000_000) + 1,
        is_bot=False,
        first_name="تازه‌وارد",
        language_code="fa",
    )
    await UserService.get_or_create_user(db_session, tg_user)
    await db_session.commit()

    row = await db_session.get(PlayerProgression, tg_user.id)
    assert row is not None and row.total_xp == 0
    assert await PlayerXPService.get_progress(db_session, tg_user.id) == PlayerXPService.get_level_progress(0)


@pytest.mark.parametrize("level,threshold", list(enumerate(CUMULATIVE_XP, start=1)))
def test_all_level_thresholds_and_boundaries(level, threshold):
    assert PlayerXPService.cumulative_xp_for_level(level) == threshold
    progress = PlayerXPService.get_level_progress(threshold)
    assert progress.level == level
    assert progress.total_xp == threshold

    if level < MAX_LEVEL:
        required = CUMULATIVE_XP[level] - threshold
        assert PlayerXPService.xp_required_for_next_level(level) == required
        assert progress.current_level_xp == 0
        assert progress.xp_to_next_level == required
    else:
        assert threshold == 87_000
        assert progress.is_max
        assert progress.xp_to_next_level is None
        assert progress.current_level_xp == 0


def test_level_curve_clamps_and_preserves_remainder():
    assert PlayerXPService.get_level_progress(-50).total_xp == 0
    assert PlayerXPService.get_level_progress(87_001).total_xp == MAX_TOTAL_XP

    progress = PlayerXPService.get_level_progress(150 + 37)
    assert progress.level == 3
    assert progress.current_level_xp == 37
    assert progress.xp_to_next_level == 140
    assert progress.remaining_xp == 103

    max_line = PlayerXPService.format_profile_progress(
        PlayerXPService.get_level_progress(MAX_TOTAL_XP)
    )
    assert "MAX" in max_line and "87,000 / 87,000" in max_line
    assert PlayerXPService.level_badge(30) == "👑"


@pytest.mark.asyncio
async def test_match_awards_win_bonus_and_is_idempotent(db_session):
    match, user_a, user_b = await _create_match(db_session)
    result = GameResult(
        winner_team=1,
        is_draw=False,
        summary="test result",
        details={},
    )

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    expected = {
        user_a.id: 125 if next(p for p in match.players if p.user_id == user_a.id).team == 1 else 100,
        user_b.id: 125 if next(p for p in match.players if p.user_id == user_b.id).team == 1 else 100,
    }
    for user_id, amount in expected.items():
        progress = await PlayerXPService.get_progress(db_session, user_id)
        assert progress.total_xp == amount
        grant = await db_session.scalar(
            select(PlayerXPGrant).where(
                PlayerXPGrant.user_id == user_id,
                PlayerXPGrant.source == "normal_match",
                PlayerXPGrant.event_key == f"match:{match.id}",
            )
        )
        assert grant is not None and grant.amount == amount

    # A duplicate settlement is stopped by the result guard and the XP key.
    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()
    assert await PlayerXPService.get_progress(db_session, user_a.id) == PlayerXPService.get_level_progress(
        expected[user_a.id]
    )


@pytest.mark.asyncio
async def test_normal_draw_awards_participation_without_win_bonus(db_session):
    match, user_a, user_b = await _create_match(db_session)
    result = GameResult(
        winner_team=None,
        is_draw=True,
        summary="draw",
        details={},
    )

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    for user_id in (user_a.id, user_b.id):
        assert (await PlayerXPService.get_progress(db_session, user_id)).total_xp == 100


@pytest.mark.asyncio
async def test_started_timeout_forfeit_uses_standard_match_xp(db_session):
    match, user_a, user_b = await _create_match(db_session)
    result = GameResult(
        winner_team=2,
        is_draw=False,
        summary="turn timeout forfeit",
        details={"turn_timeout": True, "forfeited_player_id": user_a.id},
    )

    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()

    for player in match.players:
        expected = 125 if player.team == 2 else 100
        assert (
            await PlayerXPService.get_progress(db_session, player.user_id)
        ).total_xp == expected


@pytest.mark.asyncio
async def test_xp_failure_preserves_finished_match_and_maintenance_retries(
    db_session, monkeypatch
):
    match, user_a, user_b = await _create_match(db_session)
    result = GameResult(
        winner_team=1,
        is_draw=False,
        summary="result survives XP failure",
        details={},
    )

    async def fail_award(_cls, _session, _match, *, started):
        assert started is True
        raise RuntimeError("transient XP failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            PlayerXPService, "award_match_results", classmethod(fail_award)
        )
        await MatchResultService.apply_match_result(db_session, match, result)

    assert match.result_applied is True
    assert match.state == "FINISHED"
    assert match.xp_awarded is False
    await db_session.commit()
    assert (await PlayerXPService.get_progress(db_session, user_a.id)).total_xp == 0

    assert await MatchResultService.retry_pending_xp_awards(db_session) == 1
    assert match.xp_awarded is True
    await db_session.commit()
    for player in match.players:
        expected = 125 if player.team == 1 else 100
        assert (
            await PlayerXPService.get_progress(db_session, player.user_id)
        ).total_xp == expected


@pytest.mark.asyncio
async def test_unstarted_match_and_bot_accounts_receive_no_normal_xp(db_session):
    match, user_a, user_b = await _create_match(db_session)
    match.state = "WAITING_FOR_ACCEPTANCE"
    result = GameResult(winner_team=1, is_draw=False, summary="not started", details={})
    await MatchResultService.apply_match_result(db_session, match, result)
    await db_session.commit()
    assert (await PlayerXPService.get_progress(db_session, user_a.id)).total_xp == 0
    assert (await PlayerXPService.get_progress(db_session, user_b.id)).total_xp == 0

    bot_user = await _new_user(db_session, is_bot=True)
    result = await PlayerXPService.award(
        db_session,
        user_id=bot_user.id,
        source="normal_match",
        event_key="match:bot-account",
        won=True,
    )
    await db_session.commit()
    assert result.granted_xp == 0
    assert await db_session.scalar(
        select(func.count(PlayerXPGrant.id)).where(PlayerXPGrant.user_id == bot_user.id)
    ) == 0


@pytest.mark.asyncio
async def test_match_classifier_uses_friendly_and_bot_buckets(db_session):
    friendly_a = await _new_user(db_session)
    friendly_b = await _new_user(db_session)
    bot = await _new_user(db_session, is_bot=True)
    friendly = SimpleNamespace(
        id=71_001,
        match_type=MatchType.FRIENDLY,
        is_bot_match=False,
        is_draw=False,
        winner_team=1,
        group1_id=-7001,
        group2_id=-7001,
        players=[
            SimpleNamespace(user_id=friendly_a.id, team=1, group_id=-7001),
            SimpleNamespace(user_id=friendly_b.id, team=2, group_id=-7001),
            SimpleNamespace(user_id=bot.id, team=2, group_id=None),
        ],
    )
    await PlayerXPService.award_match_results(db_session, friendly, started=True)

    assert (await PlayerXPService.get_progress(db_session, friendly_a.id)).total_xp == 35
    assert (await PlayerXPService.get_progress(db_session, friendly_b.id)).total_xp == 35
    assert await db_session.scalar(
        select(func.count(PlayerXPGrant.id)).where(PlayerXPGrant.user_id == bot.id)
    ) == 0

    practice_human = await _new_user(db_session)
    practice_bot = await _new_user(db_session, is_bot=True)
    practice = SimpleNamespace(
        id=71_002,
        match_type=MatchType.FRIENDLY,
        is_bot_match=False,
        is_draw=False,
        winner_team=1,
        group1_id=-7002,
        group2_id=-7002,
        players=[
            SimpleNamespace(user_id=practice_human.id, team=1, group_id=-7002),
            SimpleNamespace(user_id=practice_bot.id, team=2, group_id=None),
        ],
    )
    await PlayerXPService.award_match_results(db_session, practice, started=True)
    assert (await PlayerXPService.get_progress(db_session, practice_human.id)).total_xp == 20

    normal_bot_match_human = await _new_user(db_session)
    normal_bot_match = SimpleNamespace(
        id=71_003,
        match_type=MatchType.NORMAL,
        is_bot_match=True,
        is_draw=False,
        winner_team=1,
        group1_id=-7003,
        group2_id=-7004,
        players=[SimpleNamespace(user_id=normal_bot_match_human.id, team=1, group_id=-7003)],
    )
    await PlayerXPService.award_match_results(
        db_session, normal_bot_match, started=True
    )
    assert (await PlayerXPService.get_progress(db_session, normal_bot_match_human.id)).total_xp == 20


@pytest.mark.asyncio
async def test_every_repeatable_source_cap_is_applied_before_the_global_cap(db_session):
    day = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    cases = (
        ("normal_match", 8, 100),
        ("bot_match", 2, 20),
        ("friendly_match", 5, 35),
        ("garden_harvest", 5, 5),
        ("trade", 1, 10),
    )
    for source, daily_limit, expected_total in cases:
        user = await _new_user(db_session)
        for index in range(daily_limit):
            await PlayerXPService.award(
                db_session,
                user_id=user.id,
                source=source,
                event_key=f"{source}:cap:{index}",
                now=day,
            )
        capped = await PlayerXPService.award(
            db_session,
            user_id=user.id,
            source=source,
            event_key=f"{source}:cap:extra",
            won=(source == "normal_match"),
            now=day,
        )
        assert capped.granted_xp == 0, source
        assert (
            await PlayerXPService.get_progress(db_session, user.id)
        ).total_xp == daily_limit * expected_total, source


@pytest.mark.asyncio
async def test_one_time_and_repeatable_event_keys_are_idempotent(db_session):
    user = await _new_user(db_session)
    events = (
        ("card_collection", "card_collection:4001"),
        ("card_upgrade", "card_upgrade:4001:2"),
        ("garden_plant", "garden_plant:4001:0"),
        ("garden_unlock", "garden_unlock:4001:5"),
        ("garden_magic", "garden_magic:7001:1"),
        ("garden_harvest", "garden:7001:1"),
        ("trade", "trade:4001"),
        ("flower", "flower:4001"),
        ("first_marriage", "first_marriage"),
    )
    for source, event_key in events:
        first = await PlayerXPService.award(
            db_session,
            user_id=user.id,
            source=source,
            event_key=event_key,
        )
        duplicate = await PlayerXPService.award(
            db_session,
            user_id=user.id,
            source=source,
            event_key=event_key,
        )
        assert first.granted_xp > 0, (source, event_key)
        assert duplicate.duplicate is True, (source, event_key)
        assert duplicate.granted_xp == 0, (source, event_key)

    assert (await PlayerXPService.get_progress(db_session, user.id)).total_xp == 140


@pytest.mark.asyncio
async def test_daily_source_caps_global_partial_grant_and_utc_reset(db_session):
    user = await _new_user(db_session)
    day = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)

    # Eight normal match wins hit both the event-count limit and the 1,000-XP
    # global ceiling; a ninth eligible event is recorded with a zero grant.
    for index in range(8):
        await PlayerXPService.award(
            db_session,
            user_id=user.id,
            source="normal_match",
            event_key=f"match:cap-{index}",
            won=True,
            now=day,
        )
    ninth = await PlayerXPService.award(
        db_session,
        user_id=user.id,
        source="normal_match",
        event_key="match:cap-8",
        now=day,
    )
    assert ninth.granted_xp == 0
    assert (await PlayerXPService.get_progress(db_session, user.id)).total_xp == 1_000

    next_day = await PlayerXPService.award(
        db_session,
        user_id=user.id,
        source="garden_plant",
        event_key="garden_plant:reset-day",
        now=datetime(2026, 10, 7, 0, tzinfo=timezone.utc),
    )
    assert next_day.granted_xp == 10

    partial_user = await _new_user(db_session)
    for index in range(7):
        await PlayerXPService.award(
            db_session,
            user_id=partial_user.id,
            source="normal_match",
            event_key=f"match:partial-{index}",
            won=True,
            now=day,
        )
    for index in range(5):
        await PlayerXPService.award(
            db_session,
            user_id=partial_user.id,
            source="garden_harvest",
            event_key=f"garden:{index}:1",
            now=day,
        )
    for source, event_key in (
        ("card_collection", "card_collection:5001"),
        ("first_marriage", "first_marriage"),
        ("card_upgrade", "card_upgrade:5001:2"),
        ("trade", "trade:5001"),
    ):
        await PlayerXPService.award(
            db_session,
            user_id=partial_user.id,
            source=source,
            event_key=event_key,
            now=day,
        )
    clipped = await PlayerXPService.award(
        db_session,
        user_id=partial_user.id,
        source="garden_plant",
        event_key="garden_plant:partial",
        now=day,
    )
    assert clipped.requested_xp == 10
    assert clipped.granted_xp == 5
    assert (await PlayerXPService.get_progress(db_session, partial_user.id)).total_xp == GLOBAL_DAILY_XP_CAP

    flower_user = await _new_user(db_session)
    start = datetime(2026, 10, 6, 23, 30, tzinfo=timezone.utc)
    first_day = [
        await PlayerXPService.award(
            db_session,
            user_id=flower_user.id,
            source="flower",
            event_key=f"flower:{index}",
            now=start,
        )
        for index in range(3)
    ]
    assert [result.granted_xp for result in first_day] == [5, 5, 0]
    next_utc_day = await PlayerXPService.award(
        db_session,
        user_id=flower_user.id,
        source="flower",
        event_key="flower:next-day",
        now=datetime(2026, 10, 7, 0, tzinfo=timezone.utc),
    )
    assert next_utc_day.granted_xp == 5


@requires_row_locks
@pytest.mark.asyncio
async def test_concurrent_matches_cannot_exceed_source_or_daily_cap(db_session):
    user = await _new_user(db_session)
    day = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    for index in range(7):
        await PlayerXPService.award(
            db_session,
            user_id=user.id,
            source="normal_match",
            event_key=f"match:before-concurrency-{index}",
            won=True,
            now=day,
        )
    await db_session.commit()

    factory = async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, autoflush=False
    )

    async def award(key):
        async with factory() as session:
            result = await PlayerXPService.award(
                session,
                user_id=user.id,
                source="normal_match",
                event_key=key,
                won=True,
                now=day,
            )
            await session.commit()
            return result

    results = await asyncio.gather(
        award("match:concurrent-a"), award("match:concurrent-b")
    )
    assert sorted(result.granted_xp for result in results) == [0, 125]

    async with factory() as session:
        progress = await PlayerXPService.get_progress(session, user.id)
        assert progress.total_xp == 1_000
        assert await session.scalar(
            select(func.count(PlayerXPGrant.id)).where(
                PlayerXPGrant.user_id == user.id,
                PlayerXPGrant.source == "normal_match",
            )
        ) == 9


@pytest.mark.asyncio
async def test_level_30_lifetime_cap_and_first_marriage_one_time_key(db_session):
    user = await _new_user(db_session)
    db_session.add(PlayerProgression(user_id=user.id, total_xp=86_995))
    await db_session.flush()

    final = await PlayerXPService.award(
        db_session,
        user_id=user.id,
        source="garden_plant",
        event_key="garden_plant:capstone",
    )
    assert final.granted_xp == 5
    assert final.level_after == MAX_LEVEL

    overflow = await PlayerXPService.award(
        db_session,
        user_id=user.id,
        source="first_marriage",
        event_key="first_marriage",
    )
    assert overflow.granted_xp == 0
    assert (await PlayerXPService.get_progress(db_session, user.id)).total_xp == MAX_TOTAL_XP


@pytest.mark.asyncio
async def test_multilevel_transition_notification_is_post_commit_and_retryable(db_session):
    user = await _new_user(db_session)
    db_session.add(PlayerProgression(user_id=user.id, total_xp=50))
    await db_session.flush()

    result = await PlayerXPService.award(
        db_session,
        user_id=user.id,
        source="normal_match",
        event_key="match:level-up",
        won=True,
        notification_chat_id=-98765,
    )
    assert (result.level_before, result.level_after) == (1, 3)
    await db_session.commit()

    class BotStub:
        def __init__(self):
            self.calls = []

        async def send_message(self, *, chat_id, text, **kwargs):
            self.calls.append((chat_id, text, kwargs))
            if chat_id == user.id:
                raise RuntimeError("player has not started a private chat")

    bot = BotStub()
    assert await PlayerXPService.deliver_pending_notifications(bot) == 1
    assert [call[0] for call in bot.calls] == [user.id, -98765]
    assert "1 به سطح 3" in bot.calls[1][1]

    # A successful notice is not replayed by a later maintenance sweep.
    assert await PlayerXPService.deliver_pending_notifications(bot) == 0
    assert len(bot.calls) == 2

    grant = await db_session.scalar(
        select(PlayerXPGrant).where(PlayerXPGrant.event_key == "match:level-up")
    )
    assert grant.notification_sent_at is not None


@pytest.mark.asyncio
async def test_failed_level_up_notice_releases_claim_for_retry(db_session):
    user = await _new_user(db_session)
    db_session.add(PlayerProgression(user_id=user.id, total_xp=50))
    await db_session.flush()
    await PlayerXPService.award(
        db_session,
        user_id=user.id,
        source="garden_plant",
        event_key="garden_plant:notice-retry",
    )
    await db_session.commit()

    class FailingBot:
        async def send_message(self, *, chat_id, text, **kwargs):
            raise RuntimeError("Telegram temporarily unavailable")

    assert await PlayerXPService.deliver_pending_notifications(FailingBot()) == 0
    grant = await db_session.scalar(
        select(PlayerXPGrant).where(
            PlayerXPGrant.event_key == "garden_plant:notice-retry"
        )
    )
    await db_session.refresh(grant)
    assert grant.notification_claimed_at is None
    assert grant.notification_sent_at is None

    class RecoveredBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, *, chat_id, text, **kwargs):
            self.calls.append((chat_id, text))

    recovered = RecoveredBot()
    assert await PlayerXPService.deliver_pending_notifications(recovered) == 1
    assert len(recovered.calls) == 1 and recovered.calls[0][0] == user.id
    await db_session.refresh(grant)
    assert grant.notification_sent_at is not None
