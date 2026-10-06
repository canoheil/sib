"""Phase 2A — Apple Battle cinematic narration architecture.

Pins the NEW delivery contract (mocks only, no real Telegram calls):

* live narration is sent as SEPARATE messages (photo + short caption) — never
  appended by repeatedly editing one board message;
* the Telegram message ids of those temporary messages are persisted inside
  ``match.game_state`` (restart-safe, same state Phase 1 recovery resumes from);
* cleanup deletes every temporary narration message, and a cleanup failure
  never breaks settlement or the final report;
* a restart during narration resumes from the persisted cursor, does not
  recompute the battle, does not duplicate narration events and never runs a
  second narration worker;
* the existing final Apple Battle report is rendered unchanged after cleanup.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from sib.database.models import (
    AppleBattle,
    AppleBattleStatus,
    Game,
    Match,
    MatchPlayer,
    MatchState,
    ReleaseStatus,
)
from sib.games.apple_battle.engine import AppleBattleEngine
from sib.services.apple_battle import AppleBattleService
from sib.services.apple_battle_narration import (
    GENERIC_PLACEHOLDER,
    AppleBattleNarration,
    AppleBattleNarrationArt,
    AppleBattleNarrationState,
)
from sib.services.apple_wallet import AppleWalletService
from sib.services.characters import CharacterService
from sib.services.group import GroupService
from sib.services.player_cards import PlayerCardService
from sib.services.recovery.service import RecoveryService
from sib.services.seedling import SeedlingService
from sib.services.telegram_ui import photo_caption
from sib.tests.test_parties import create_test_user


class _Chat:
    def __init__(self, cid, title):
        self.id = cid
        self.title = title
        self.type = "supergroup"
        self.username = None


def _fake_bot(delete_error=None):
    """Bot double: numbers every sent message; records call order."""
    counter = {"next": 1000}
    order = []

    def _next_message(chat_id):
        message_id = counter["next"]
        counter["next"] += 1
        return SimpleNamespace(message_id=message_id, chat_id=chat_id)

    async def _send_photo(chat_id, photo, **kwargs):
        order.append(("send_photo", chat_id, kwargs.get("caption")))
        return _next_message(chat_id)

    async def _send_message(chat_id, text, **kwargs):
        order.append(("send_message", chat_id, text))
        return _next_message(chat_id)

    async def _edit_media(**kwargs):
        order.append(
            ("edit_media", kwargs.get("chat_id"), getattr(kwargs.get("media"), "caption", None))
        )
        return _next_message(kwargs.get("chat_id"))

    async def _edit_text(text, **kwargs):
        order.append(("edit_text", kwargs.get("chat_id"), text))
        return _next_message(kwargs.get("chat_id"))

    async def _delete_message(chat_id, message_id, **kwargs):
        order.append(("delete", chat_id, message_id))
        if delete_error is not None:
            raise delete_error
        return True

    bot = MagicMock()
    bot.send_photo = AsyncMock(side_effect=_send_photo)
    bot.send_message = AsyncMock(side_effect=_send_message)
    bot.edit_message_media = AsyncMock(side_effect=_edit_media)
    bot.edit_message_text = AsyncMock(side_effect=_edit_text)
    bot.delete_message = AsyncMock(side_effect=_delete_message)
    bot.order = order
    return bot


async def _card(db_session, user, code):
    character = await CharacterService.create(
        db_session,
        code=code,
        name_fa=f"قهرمان {code}",
        power=22,
        speed=20,
        intelligence=18,
        passive_key="test_passive",
        hidden_modifiers={"crit_multiplier": 1.05},
    )
    await CharacterService.set_release_status(db_session, character.id, ReleaseStatus.RELEASED)
    card = await PlayerCardService.acquire(db_session, user.id, character.id)
    await db_session.commit()
    return card


async def _setup_battle(
    db_session,
    base_chat: int,
    suffix: str,
    *,
    cursor: int = 0,
    already_sent: int = 0,
    art: bool = False,
    expired_deadline: bool = False,
    board_ids=(11, 22),
):
    """Active Apple Battle with a persisted plan, ready for narration.

    ``already_sent`` beats are pre-recorded in ``state["narration"]`` with fake
    Telegram message ids, simulating a process that died mid-narration.
    """
    await GroupService.get_or_create_group(db_session, _Chat(base_chat, f"گروه یک {suffix}"))
    await GroupService.get_or_create_group(db_session, _Chat(base_chat - 1, f"گروه دو {suffix}"))

    u1 = await create_test_user(db_session, f"بازیکن یک {suffix}")
    u2 = await create_test_user(db_session, f"بازیکن دو {suffix}")
    c1 = await _card(db_session, u1, "sib_sorkh" if art else f"art1_{suffix}")
    c2 = await _card(db_session, u2, "sib_sabz" if art else f"art2_{suffix}")

    await AppleBattleService.enqueue(db_session, u1.id, c1.id)
    await AppleBattleService.enqueue(db_session, u2.id, c2.id)

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = Match(
        game_id=game.id,
        mode="solo",
        state=MatchState.CREATED,
        group1_id=base_chat,
        group2_id=base_chat - 1,
    )
    db_session.add(match)
    await db_session.flush()
    db_session.add(MatchPlayer(match_id=match.id, user_id=u1.id, group_id=base_chat, team=1))
    db_session.add(
        MatchPlayer(match_id=match.id, user_id=u2.id, group_id=base_chat - 1, team=2)
    )
    await db_session.commit()

    from sqlalchemy.orm import selectinload

    match = await db_session.scalar(
        select(Match)
        .where(Match.id == match.id)
        .options(selectinload(Match.players).selectinload(MatchPlayer.user))
        .execution_options(populate_existing=True)
    )
    state = await AppleBattleService.build_match_state(db_session, match)
    state["cursor"] = cursor

    if already_sent:
        block = AppleBattleNarrationState.ensure(state)
        for beat in range(already_sent):
            for offset, chat_id in ((0, base_chat), (1, base_chat - 1)):
                block["messages"].append(
                    {
                        "beat": beat,
                        "chat_id": chat_id,
                        "message_id": 5000 + beat * 2 + offset,
                        "group": offset + 1,
                        "deleted": False,
                    }
                )

    match.game_state = json.dumps(state, ensure_ascii=False)
    match.state = MatchState.ACTIVE
    match.group1_message_id = board_ids[0]
    match.group2_message_id = board_ids[1]
    from datetime import datetime, timedelta, timezone

    match.turn_deadline = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
        if expired_deadline
        else datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    await db_session.commit()

    return {
        "match_id": match.id,
        "state": state,
        "u1": u1,
        "u2": u2,
        "chat1": base_chat,
        "chat2": base_chat - 1,
        "plan": state["plan"],
        "winner_id": state["battle"]["winner_id"],
    }


async def _reload_match(db_session, match_id):
    from sqlalchemy.orm import selectinload

    return await db_session.scalar(
        select(Match)
        .where(Match.id == match_id)
        .options(selectinload(Match.players).selectinload(MatchPlayer.user))
        .execution_options(populate_existing=True)
    )


async def _persisted_state(db_session, match_id):
    match = await _reload_match(db_session, match_id)
    return json.loads(match.game_state)


# ---------------------------------------------------------------------------
# 1. Separate messages — never one appended/edited message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_is_sent_as_separate_photo_messages(db_session):
    ctx = await _setup_battle(db_session, -70001, "sep", cursor=0, art=True)
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)

    assert bot.send_photo.await_count == len(ctx["plan"]) * 2
    # Every narration message is a PHOTO + short cinematic caption.
    for call in bot.send_photo.call_args_list:
        assert call.args[1] is not None, "narration messages must carry a photo"
        assert call.kwargs["caption"] in [beat["text"] for beat in ctx["plan"]]

    # Each beat appears exactly once per group, as its own send.
    captions = [c.kwargs["caption"] for c in bot.send_photo.call_args_list]
    for beat in ctx["plan"]:
        assert captions.count(beat["text"]) == 2

    # Narration is NEVER appended into a repeatedly edited board message:
    # the only edits are the two final reports.
    assert bot.edit_message_text.await_count + bot.edit_message_media.await_count == 2
    assert bot.order[-2][0] in ("edit_media", "edit_text")


# ---------------------------------------------------------------------------
# 2. Message ids are persisted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_message_ids_are_persisted(db_session):
    ctx = await _setup_battle(db_session, -70011, "ids", cursor=0, art=True)
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    state = await _persisted_state(db_session, ctx["match_id"])
    records = AppleBattleNarrationState.messages(state)
    assert len(records) == len(ctx["plan"]) * 2
    assert {(r["beat"], r["chat_id"]) for r in records} == {
        (beat, chat)
        for beat in range(len(ctx["plan"]))
        for chat in (ctx["chat1"], ctx["chat2"])
    }
    sent_ids = {c.kwargs["caption"]: None for c in bot.send_photo.call_args_list}
    assert all(isinstance(r["message_id"], int) for r in records)
    assert len({r["message_id"] for r in records}) == len(records)
    assert {r["message_id"] for r in records} == set(range(1000, 1000 + len(records)))
    assert AppleBattleNarrationState.delivered_beats(state) == set(range(len(ctx["plan"])))
    del sent_ids


# ---------------------------------------------------------------------------
# 3. Cleanup deletes the narration messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_deletes_all_narration_messages(db_session):
    ctx = await _setup_battle(db_session, -70021, "clean", cursor=0, art=True)
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    state = await _persisted_state(db_session, ctx["match_id"])
    records = AppleBattleNarrationState.messages(state)
    deleted_pairs = [(c.args[0], c.args[1]) for c in bot.delete_message.call_args_list]
    assert len(deleted_pairs) == len(records)
    assert set(deleted_pairs) == {(r["chat_id"], r["message_id"]) for r in records}
    assert AppleBattleNarrationState.is_cleaned(state) is True
    assert all(r["deleted"] is True for r in records)
    assert await AppleBattleNarration.pending_messages(ctx["match_id"]) == []


@pytest.mark.asyncio
async def test_cleanup_treats_already_deleted_messages_as_clean(db_session):
    from aiogram.exceptions import TelegramBadRequest

    ctx = await _setup_battle(db_session, -70031, "gone", cursor=0, already_sent=1)
    bot = _fake_bot(
        delete_error=TelegramBadRequest(method=None, message="message to delete not found")
    )

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    state = await _persisted_state(db_session, ctx["match_id"])
    assert AppleBattleNarrationState.is_cleaned(state) is True
    assert all(r["deleted"] is True for r in AppleBattleNarrationState.messages(state))


# ---------------------------------------------------------------------------
# 4. Cleanup failure never breaks finalization or the final report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_break_finalization_or_report(db_session):
    ctx = await _setup_battle(db_session, -70041, "fail", cursor=0, art=True)
    bot = _fake_bot(delete_error=RuntimeError("telegram unavailable"))

    # Must never raise.
    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    match = await _reload_match(db_session, ctx["match_id"])
    state = json.loads(match.game_state)

    # Settlement fully applied.
    assert match.result_applied is True
    assert match.state == MatchState.FINISHED
    assert state["battle"]["winner_id"] == ctx["winner_id"]
    assert await SeedlingService.get_total(db_session, ctx["winner_id"]) == 1
    assert await AppleWalletService.get_balance(db_session, ctx["winner_id"]) == 5

    # The unchanged final report was still shown after the failed cleanup.
    report_edits = [e for e in bot.order if e[0] in ("edit_media", "edit_text")]
    assert len(report_edits) == 2
    engine = AppleBattleEngine()
    for kind, chat_id, text in report_edits:
        expected = photo_caption(engine.render_for_group(state, chat_id, None))
        assert text == expected
        assert "✅ این نبرد به پایان رسید." in text
        assert kind == "edit_media"

    # Cleanup genuinely failed → records stay for a later retry, never lost.
    assert len(await AppleBattleNarration.pending_messages(ctx["match_id"])) == len(
        ctx["plan"]
    ) * 2
    assert AppleBattleNarrationState.is_cleaned(state) is False


@pytest.mark.asyncio
async def test_cleanup_helper_never_raises_on_delete_failure(db_session):
    ctx = await _setup_battle(db_session, -70051, "helper", cursor=0, already_sent=2)
    bot = _fake_bot(delete_error=RuntimeError("boom"))

    deleted = await AppleBattleNarration.cleanup(bot, ctx["match_id"])
    assert deleted == 0
    assert len(await AppleBattleNarration.pending_messages(ctx["match_id"])) == 4


# ---------------------------------------------------------------------------
# 5. Restart during narration: resume, no recompute, no duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_resumes_mid_narration_without_duplicates(db_session):
    ctx = await _setup_battle(
        db_session, -70061, "resume", cursor=2, already_sent=2, art=True, expired_deadline=True
    )
    persisted_winner = ctx["winner_id"]
    persisted_seed = ctx["state"]["battle"]["seed"]
    match_id = ctx["match_id"]

    # Simulated restart: Phase 1 recovery resumes (does NOT finalize/recompute).
    stats = await RecoveryService.recover_on_startup(db_session, bot=None)
    assert stats["resumed_apple_battles"] == 1
    assert stats["finalized_apple_battles"] == 0

    # No duplicate worker: scheduling again returns the same live task.
    first = AppleBattleService.schedule_narration(match_id, bot=None)
    second = AppleBattleService.schedule_narration(match_id, bot=None)
    assert first is second
    await AppleBattleService.cancel_narration(match_id)

    bot = _fake_bot()
    await AppleBattleService.run_narration(bot, match_id, beat_delay=0.0)
    await db_session.rollback()

    # Only the UNSENT beats (2, 3, 4) were delivered — once per group.
    captions = [c.kwargs["caption"] for c in bot.send_photo.call_args_list]
    assert len(captions) == 3 * 2
    assert all(text not in captions for text in [b["text"] for b in ctx["plan"][:2]])
    for beat in ctx["plan"][2:]:
        assert captions.count(beat["text"]) == 2

    state = await _persisted_state(db_session, match_id)
    records = AppleBattleNarrationState.messages(state)
    # Exactly one record per (beat, chat): no duplicated narration events.
    assert len(records) == len(ctx["plan"]) * 2
    assert len({(r["beat"], r["chat_id"]) for r in records}) == len(records)
    # Preserved pre-restart message ids were kept (no second sequence).
    assert 5000 in {r["message_id"] for r in records}
    assert 5003 in {r["message_id"] for r in records}

    match = await _reload_match(db_session, match_id)
    assert match.result_applied is True
    assert state["battle"]["winner_id"] == persisted_winner
    assert state["battle"]["seed"] == persisted_seed
    assert await SeedlingService.get_total(db_session, persisted_winner) == 1
    assert await AppleWalletService.get_balance(db_session, persisted_winner) == 5
    assert AppleBattleService.is_narration_scheduled(match_id) is False


@pytest.mark.asyncio
async def test_concurrent_narration_workers_never_duplicate_events(db_session):
    """Two racing narration workers: one sequence, no duplicate messages."""
    import asyncio

    ctx = await _setup_battle(db_session, -70065, "race", cursor=0, art=True)
    bot_a = _fake_bot()
    bot_b = _fake_bot()

    await asyncio.gather(
        AppleBattleService.run_narration(bot_a, ctx["match_id"], beat_delay=0.0),
        AppleBattleService.run_narration(bot_b, ctx["match_id"], beat_delay=0.0),
    )
    await db_session.rollback()

    all_captions = [
        c.kwargs["caption"]
        for bot in (bot_a, bot_b)
        for c in bot.send_photo.call_args_list
    ]
    assert len(all_captions) == len(ctx["plan"]) * 2
    for beat in ctx["plan"]:
        assert all_captions.count(beat["text"]) == 2

    state = await _persisted_state(db_session, ctx["match_id"])
    records = AppleBattleNarrationState.messages(state)
    assert len({(r["beat"], r["chat_id"]) for r in records}) == len(records)
    match = await _reload_match(db_session, ctx["match_id"])
    assert match.result_applied is True
    assert AppleBattleService.is_narration_scheduled(ctx["match_id"]) is False


@pytest.mark.asyncio
async def test_recovery_after_crash_during_cleanup_finishes_teardown(db_session):
    """Crash between cleanup and settlement: recovery finalizes + cleans up."""
    ctx = await _setup_battle(
        db_session, -70069, "partial", cursor=5, already_sent=5, art=True, expired_deadline=True
    )
    # Simulate a partially finished teardown: two messages already deleted
    # (recorded), the rest still live.
    state = ctx["state"]
    block = AppleBattleNarrationState.ensure(state)
    for record in block["messages"][:2]:
        record["deleted"] = True
    match = await _reload_match(db_session, ctx["match_id"])
    match.game_state = json.dumps(state, ensure_ascii=False)
    await db_session.commit()

    bot = _fake_bot()
    stats = await RecoveryService.recover_on_startup(db_session, bot=bot)
    assert stats["finalized_apple_battles"] == 1
    await db_session.rollback()

    # Only the still-pending messages were deleted; nothing was re-sent.
    deleted = {(c.args[0], c.args[1]) for c in bot.delete_message.call_args_list}
    assert len(deleted) == 8
    assert bot.send_photo.await_count == 0
    final = await _persisted_state(db_session, ctx["match_id"])
    assert AppleBattleNarrationState.is_cleaned(final) is True
    battle = await db_session.scalar(
        select(AppleBattle).where(AppleBattle.match_id == ctx["match_id"])
    )
    assert battle is not None and battle.status == AppleBattleStatus.COMPLETED


@pytest.mark.asyncio
async def test_legacy_state_without_narration_records_resumes_safely(db_session):
    """Pre-Phase-2A persisted state (no narration block) resumes without duplicating."""
    ctx = await _setup_battle(db_session, -70071, "legacy", cursor=2, art=True)
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    captions = [c.kwargs["caption"] for c in bot.send_photo.call_args_list]
    assert len(captions) == 3 * 2
    for beat in ctx["plan"][2:]:
        assert captions.count(beat["text"]) == 2
    for beat in ctx["plan"][:2]:
        assert beat["text"] not in captions


# ---------------------------------------------------------------------------
# 6. The final report is unchanged after cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_report_unchanged_and_shown_after_cleanup(db_session):
    ctx = await _setup_battle(db_session, -70081, "report", cursor=0, art=True)
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    state = await _persisted_state(db_session, ctx["match_id"])
    engine = AppleBattleEngine()

    report_edits = [e for e in bot.order if e[0] in ("edit_media", "edit_text")]
    assert len(report_edits) == 2
    # Cleanup happens BEFORE the report is shown (and nothing is sent after).
    first_report_index = next(
        i for i, event in enumerate(bot.order) if event[0] in ("edit_media", "edit_text")
    )
    delete_indexes = [i for i, event in enumerate(bot.order) if event[0] == "delete"]
    assert delete_indexes and max(delete_indexes) < first_report_index
    assert all(event[0] != "send_photo" for event in bot.order[first_report_index:])

    for kind, chat_id, caption in report_edits:
        expected_report = engine.render_for_group(state, chat_id, None)
        assert caption == photo_caption(expected_report)
        # Exact same content/layout as before Phase 2A: every beat + footer.
        for beat in ctx["plan"]:
            assert beat["text"] in caption
        assert "✅ این نبرد به پایان رسید." in caption
        assert kind == "edit_media"

    # Report lands on the existing board message ids (not on narration ones).
    edited_board_ids = {
        (c.kwargs["chat_id"], c.kwargs["message_id"])
        for c in bot.edit_message_media.call_args_list
    }
    assert edited_board_ids == {(ctx["chat1"], 11), (ctx["chat2"], 22)}


# ---------------------------------------------------------------------------
# Artwork: existing bundled placeholders only, swappable architecture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_art_prefers_pregenerated_scene_library(db_session):
    """Phase 2B: a compatible pre-generated plate wins over card art."""
    from sib.database.connection import get_session
    from sib.services.apple_battle_scenes import AppleBattleSceneLibrary

    ctx = await _setup_battle(db_session, -70091, "artlib", cursor=0, art=True)
    state = ctx["state"]

    async with get_session() as session:
        cast_keys = await AppleBattleNarrationArt.battle_cast_keys(session, state)
    assert cast_keys == ["crimson", "green"]  # team order, from persisted state

    async with get_session() as session:
        intro_photo = await AppleBattleNarrationArt.resolve(
            session, state, 0, ctx["chat1"]
        )
        finale_photo = await AppleBattleNarrationArt.resolve(
            session, state, len(ctx["plan"]) - 1, ctx["chat1"]
        )

    intro_path = str(getattr(intro_photo, "path", ""))
    finale_path = str(getattr(finale_photo, "path", ""))
    assert AppleBattleSceneLibrary.available(), "scene library must ship real plates"
    # Compatible pre-generated plates are preferred over card art.
    exact_pair_files = {
        scene.file
        for scene in AppleBattleSceneLibrary.available()
        if set(scene.casts) == {"crimson", "green"} or scene.cast_free
    }
    for path in (intro_path, finale_path):
        assert "assets/apple_battle/" in path
        assert path.split("/")[-1] in exact_pair_files


@pytest.mark.asyncio
async def test_narration_art_falls_back_to_card_art_and_placeholder(db_session, monkeypatch):
    """When the library has no compatible plate, Phase 2A behaviour still holds."""
    import sib.services.apple_battle_scenes as scenes_module
    from sib.database.connection import get_session
    from sib.services.apple_battle_scenes import AppleBattleSceneLibrary

    monkeypatch.setattr(scenes_module, "MANIFEST_PATH", Path("/nonexistent/manifest.json"))
    AppleBattleSceneLibrary.reset_cache()
    try:
        # Bundled card art exists → the fighter's own card is used.
        ctx = await _setup_battle(db_session, -70101, "artok", cursor=0, art=True)
        async with get_session() as session:
            photo = await AppleBattleNarrationArt.resolve(
                session, ctx["state"], 0, ctx["chat1"]
            )
        assert "assets/characters/sib_sorkh.jpg" in str(getattr(photo, "path", ""))

        # No card art either → the existing bundled placeholder.
        ctx2 = await _setup_battle(db_session, -70102, "artnone", cursor=0, art=False)
        async with get_session() as session:
            fallback = await AppleBattleNarrationArt.resolve(
                session, ctx2["state"], 0, ctx2["chat1"]
            )
        assert GENERIC_PLACEHOLDER.exists(), "placeholder must be an existing repo image"
        assert str(GENERIC_PLACEHOLDER) in str(getattr(fallback, "path", ""))
    finally:
        AppleBattleSceneLibrary.reset_cache()


@pytest.mark.asyncio
async def test_narration_continues_as_text_when_no_photo_is_available(db_session):
    """A missing image never loses a narration event (text-only fallback)."""
    ctx = await _setup_battle(db_session, -70111, "noart", cursor=0, art=False)
    bot = _fake_bot()
    # Force the photo path to fail entirely: send_photo raises, placeholder
    # resolution returns None.
    bot.send_photo = AsyncMock(side_effect=RuntimeError("photo rejected"))
    import sib.services.apple_battle_narration as narration_module

    original_placeholder = narration_module.AppleBattleNarrationArt.placeholder
    narration_module.AppleBattleNarrationArt.placeholder = classmethod(lambda cls: None)
    try:
        await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    finally:
        narration_module.AppleBattleNarrationArt.placeholder = original_placeholder
    await db_session.rollback()

    # Narration still reaches both groups as separate messages. The two
    # additional direct messages are the new, post-settlement Level-up notices.
    texts = []
    for call in bot.send_message.call_args_list:
        text = call.kwargs.get("text")
        if text is None and len(call.args) > 1:
            text = call.args[1]
        texts.append(text)
    narration_texts = {beat["text"] for beat in ctx["plan"]}
    narration_sends = [text for text in texts if text in narration_texts]
    level_up_notices = [text for text in texts if text not in narration_texts]
    assert len(narration_sends) == len(ctx["plan"]) * 2
    for beat in ctx["plan"]:
        assert narration_sends.count(beat["text"]) == 2
    assert len(level_up_notices) == 2
    assert all("تبریک" in text and "سطح" in text for text in level_up_notices)
    assert bot.edit_message_text.await_count == 2  # final report, text-only


# ---------------------------------------------------------------------------
# Cleanup safety net: recovery-finalized battles drop their messages too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_cleanup_for_finalized_battle(db_session):
    """A completed-by-recovery battle still cleans its temporary messages."""
    ctx = await _setup_battle(
        db_session, -70121, "recclean", cursor=5, already_sent=5, art=True, expired_deadline=True
    )
    bot = _fake_bot()

    stats = await RecoveryService.recover_on_startup(db_session, bot=bot)
    assert stats["finalized_apple_battles"] == 1
    await db_session.rollback()

    deleted = [(c.args[0], c.args[1]) for c in bot.delete_message.call_args_list]
    state = await _persisted_state(db_session, ctx["match_id"])
    records = AppleBattleNarrationState.messages(state)
    assert set(deleted) == {(r["chat_id"], r["message_id"]) for r in records}
    assert AppleBattleNarrationState.is_cleaned(state) is True

    battle = await db_session.scalar(
        select(AppleBattle).where(AppleBattle.match_id == ctx["match_id"])
    )
    assert battle is not None
    assert battle.status == AppleBattleStatus.COMPLETED
    assert battle.winner_id == ctx["winner_id"]
    match = await _reload_match(db_session, ctx["match_id"])
    assert match.state == MatchState.FINISHED
