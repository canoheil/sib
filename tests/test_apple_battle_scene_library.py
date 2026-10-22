"""Phase 2B — Apple Battle narration scene library + deterministic selection.

Covers:
* the pre-generated library is real, valid and Telegram-friendly
  (1:1 JPEGs, existing files, no runtime generation);
* every major narration family has multiple scene variations in the catalog;
* the plate chosen for a beat always matches the beat's meaning
  (the caption can never contradict the picture);
* selection is deterministic and seeded from the PERSISTED battle seed, so a
  restart re-picks the same plate and different battles still differ;
* wrong-identity plates are never used for a named caption;
* the Phase 2A message flow (separate messages → cleanup → unchanged final
  report) still works with the real artwork wired in.
"""

import json
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from sib.database.models import Game, Match, MatchPlayer, MatchState, ReleaseStatus
from sib.games.apple_battle.engine import AppleBattleEngine
from sib.services.apple_battle import AppleBattleService
from sib.services.apple_battle_scenes import (
    MANIFEST_PATH,
    STATUS_READY,
    SCENES_DIR,
    AppleBattleSceneDirector,
    AppleBattleSceneLibrary,
)
from sib.services.characters import CharacterService
from sib.services.group import GroupService
from sib.services.player_cards import PlayerCardService
from sib.services.recovery.service import RecoveryService
from sib.services.telegram_ui import photo_caption
from sib.tests.test_parties import create_test_user

#: Families whose captions appear in EVERY battle — they must stay varied.
MAJOR_CATEGORIES = (
    "opening",
    "attack",
    "counterattack",
    "impact",
    "turning_point",
    "final_attack",
    "victory",
)

#: Families the caption of each beat can truthfully be illustrated by: the
#: primary family plus the meaning-preserving fallbacks shipped with the
#: director. Kept explicit here so a change in the mapping is caught.
BEAT_EXPECTATIONS = {
    0: ("opening",),
    1: ("attack", "impact", "counterattack"),
    2: ("impact", "attack"),
    3: ("turning_point", "critical_moment", "impact", "attack"),
    4: ("victory", "final_attack", "attack", "impact"),
}


def _fake_bot():
    """Bot double: numbers every sent message; records call order."""
    counter = {"next": 2000}
    order = []

    def _next_message(chat_id):
        message_id = counter["next"]
        counter["next"] += 1
        return SimpleNamespace(message_id=message_id, chat_id=chat_id)

    async def _send_photo(chat_id, photo, **kwargs):
        order.append(("send_photo", chat_id, kwargs.get("caption"), photo))
        return _next_message(chat_id)

    async def _send_message(chat_id, text, **kwargs):
        order.append(("send_message", chat_id, text, None))
        return _next_message(chat_id)

    async def _edit_media(**kwargs):
        order.append(
            ("edit_media", kwargs.get("chat_id"), getattr(kwargs.get("media"), "caption", None),
             kwargs.get("media"))
        )
        return _next_message(kwargs.get("chat_id"))

    async def _edit_text(text, **kwargs):
        order.append(("edit_text", kwargs.get("chat_id"), text, None))
        return _next_message(kwargs.get("chat_id"))

    async def _delete_message(chat_id, message_id, **kwargs):
        order.append(("delete", chat_id, message_id, None))
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


async def _setup_battle(db_session, base_chat, suffix, *, code1="sib_sorkh", code2="sib_sabz"):
    """Active Apple Battle with a persisted plan (real cast codes by default)."""
    await GroupService.get_or_create_group(
        db_session,
        type("C", (), {"id": base_chat, "title": f"گروه {suffix}", "type": "supergroup", "username": None})(),
    )
    await GroupService.get_or_create_group(
        db_session,
        type("C", (), {"id": base_chat - 1, "title": f"گروه {suffix} ۲", "type": "supergroup", "username": None})(),
    )
    u1 = await create_test_user(db_session, f"بازیکن {suffix} ۱")
    u2 = await create_test_user(db_session, f"بازیکن {suffix} ۲")
    c1 = await _card(db_session, u1, code1)
    c2 = await _card(db_session, u2, code2)
    await AppleBattleService.enqueue(db_session, u1.id, c1.id)
    await AppleBattleService.enqueue(db_session, u2.id, c2.id)

    game = await db_session.scalar(select(Game).where(Game.code == "apple_battle"))
    match = Match(
        game_id=game.id, mode="solo", state=MatchState.CREATED,
        group1_id=base_chat, group2_id=base_chat - 1,
    )
    db_session.add(match)
    await db_session.flush()
    db_session.add(MatchPlayer(match_id=match.id, user_id=u1.id, group_id=base_chat, team=1))
    db_session.add(MatchPlayer(match_id=match.id, user_id=u2.id, group_id=base_chat - 1, team=2))
    await db_session.commit()

    from sqlalchemy.orm import selectinload

    match = await db_session.scalar(
        select(Match)
        .where(Match.id == match.id)
        .options(selectinload(Match.players).selectinload(MatchPlayer.user))
        .execution_options(populate_existing=True)
    )
    state = await AppleBattleService.build_match_state(db_session, match)
    match.game_state = json.dumps(state, ensure_ascii=False)
    match.state = MatchState.ACTIVE
    match.group1_message_id = 11
    match.group2_message_id = 22
    await db_session.commit()
    return {
        "match_id": match.id,
        "state": state,
        "chat1": base_chat,
        "chat2": base_chat - 1,
        "plan": state["plan"],
        "seed": state["battle"]["seed"],
        "winner_id": state["battle"]["winner_id"],
        "u1": u1,
        "u2": u2,
    }


# ---------------------------------------------------------------------------
# 1. Library integrity (pre-generated, valid, Telegram-friendly)
# ---------------------------------------------------------------------------


def test_manifest_library_is_valid_and_pre_generated():
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert raw["generation"]["mode"] == "pre-generated"
    assert raw["generation"]["runtime_generation"] is False

    ids = [entry["id"] for entry in raw["scenes"]]
    assert len(ids) == len(set(ids)), "scene ids must be unique"

    known_casts = set(raw["casts"])
    for entry in raw["scenes"]:
        assert set(entry.get("casts") or ()) <= known_casts
        assert entry["category"] in raw["categories"], entry["id"]
        if entry["status"] != STATUS_READY:
            continue
        path = SCENES_DIR / entry["file"]
        assert path.exists(), f"{entry['id']} is marked ready but {path} is missing"
        assert path.stat().st_size > 0

    from PIL import Image  # local import: image checks only when Pillow exists

    for scene in AppleBattleSceneLibrary.available():
        with Image.open(scene.path) as image:
            assert image.size[0] == image.size[1], f"{scene.file} is not 1:1"
            assert image.size == (1024, 1024), f"{scene.file} must be 1024x1024"
            assert image.format == "JPEG"
        assert scene.path.stat().st_size <= 512 * 1024, "keep plates Telegram-friendly"


def test_every_catalog_family_has_multiple_variations():
    """Design invariant: no major narration family relies on a single plate."""
    catalog = AppleBattleSceneLibrary.all_scenes()
    counts: dict[str, int] = {}
    for scene in catalog:
        counts[scene.category] = counts.get(scene.category, 0) + 1

    for category in MAJOR_CATEGORIES:
        assert counts.get(category, 0) >= 2, f"{category} needs more scene variations"
    for category, count in counts.items():
        assert count >= 1

    # Ready plates are a subset of the catalog and always resolvable.
    ready = AppleBattleSceneLibrary.categories()
    for category, count in ready.items():
        assert count <= counts[category]
    assert AppleBattleSceneLibrary.available(), "no ready plates on disk"
    assert all(scene.status == STATUS_READY for scene in AppleBattleSceneLibrary.available())


def test_ready_plates_cover_every_beat_family():
    """Every family a beat can ask for has shipped artwork on disk."""
    ready = AppleBattleSceneLibrary.categories()
    required = set()
    for families in AppleBattleSceneDirector.BEAT_FAMILIES.values():
        required.update(families)
    for family in sorted(required):
        assert ready.get(family, 0) >= 1, f"{family} has no ready plate"

    # Families that appear in EVERY battle are already multi-variant.
    for family in ("opening", "attack", "counterattack", "impact", "victory"):
        assert ready.get(family, 0) >= 2, f"{family} needs a second ready variant"

    # Identity-free plates exist for the beats whose cast cannot be named.
    cast_free_categories = {
        scene.category for scene in AppleBattleSceneLibrary.available() if scene.cast_free
    }
    assert {"opening", "impact", "victory"} <= cast_free_categories


def test_narration_sequence_never_repeats_a_plate_back_to_back():
    """Presentation variety: consecutive beats never show the same picture."""
    plan = [
        {"phase": "intro", "text": "..."},
        {"phase": "round", "round": 1, "text": "..."},
        {"phase": "round", "round": 2, "text": "..."},
        {"phase": "round", "round": 3, "text": "..."},
        {"phase": "finale", "text": "..."},
    ]
    for casts in (
        ("crimson", "green"),
        ("gold", "moon"),
        ("crimson", "gold"),
        ("green", "moon"),
    ):
        for index in range(20):
            sequence = AppleBattleSceneDirector.plan_sequence(
                plan,
                upto=len(plan),
                chat_id=-100,
                cast_keys=casts,
                seed=f"seq-{index}",
            )
            ids = [scene.id if scene else None for scene in sequence]
            assert len(ids) == len(plan)
            for position in range(1, len(ids)):
                if ids[position] is None or ids[position - 1] is None:
                    continue
                assert ids[position] != ids[position - 1], (
                    f"{casts} seed seq-{index} repeated {ids[position]}"
                )
            # Every plate still belongs to a family the caption allows.
            for position, scene in enumerate(sequence):
                if scene is not None:
                    assert scene.category in BEAT_EXPECTATIONS[position]


def test_plan_sequence_is_deterministic_and_matches_single_beat_picks():
    plan = [
        {"phase": "intro", "text": "..."},
        {"phase": "round", "round": 1, "text": "..."},
        {"phase": "round", "round": 2, "text": "..."},
    ]
    casts = ("crimson", "green")
    first = [
        scene.id
        for scene in AppleBattleSceneDirector.plan_sequence(
            plan, upto=3, chat_id=-55, cast_keys=casts, seed="stable"
        )
    ]
    AppleBattleSceneLibrary.reset_cache()
    second = [
        scene.id
        for scene in AppleBattleSceneDirector.plan_sequence(
            plan, upto=3, chat_id=-55, cast_keys=casts, seed="stable"
        )
    ]
    assert first == second

    # select_scene(plan=...) is the same function used by the narration loop.
    for index in range(3):
        scene = AppleBattleSceneDirector.select_scene(
            beat=plan[index],
            beat_index=index,
            chat_id=-55,
            cast_keys=casts,
            seed="stable",
            plan=plan,
        )
        assert scene.id == first[index]


def test_identity_free_pool_is_varied_for_unpaired_matchups():
    """Matchups without dedicated plates still get several distinct pictures."""
    plan = [
        {"phase": "intro", "text": "..."},
        {"phase": "round", "round": 1, "text": "..."},
        {"phase": "round", "round": 2, "text": "..."},
        {"phase": "round", "round": 3, "text": "..."},
        {"phase": "finale", "text": "..."},
    ]
    seen = set()
    for index in range(40):
        seen.update(
            scene.id
            for scene in AppleBattleSceneDirector.plan_sequence(
                plan,
                upto=len(plan),
                chat_id=-9,
                cast_keys=("crimson", "gold"),
                seed=f"unpaired-{index}",
            )
            if scene is not None
        )
    assert len(seen) >= 4, f"unpaired matchups repeat too few plates: {sorted(seen)}"
    # Every plate used is identity-free (cast-free) — never a wrong duo.
    cast_free = {scene.id for scene in AppleBattleSceneLibrary.available() if scene.cast_free}
    assert seen <= cast_free


def test_runtime_never_generates_images():
    """The narration modules only reference existing repo files."""
    import inspect

    from sib.services import apple_battle_narration, apple_battle_scenes

    for module in (apple_battle_narration, apple_battle_scenes):
        source = inspect.getsource(module).lower()
        for forbidden in ("requests.", "httpx", "aiohttp", "urllib", "openai", "imagegen"):
            assert forbidden not in source, f"{module.__name__} must not call an image service"
    # And the plates resolve to real files inside the repo asset folder.
    scenes = AppleBattleSceneLibrary.available()
    assert scenes and all(scene.path.exists() for scene in scenes)
    assert all(SCENES_DIR in scene.path.parents for scene in scenes)


# ---------------------------------------------------------------------------
# 2. Selection matches the narration event
# ---------------------------------------------------------------------------


def test_selected_plate_matches_each_narration_beat():
    plan = [
        {"phase": "intro", "text": "..."},
        {"phase": "round", "round": 1, "text": "..."},
        {"phase": "round", "round": 2, "text": "..."},
        {"phase": "round", "round": 3, "text": "..."},
        {"phase": "finale", "text": "..."},
    ]
    for index, beat in enumerate(plan):
        families = AppleBattleSceneDirector.families_for_beat(beat)
        assert families, f"beat {index} has no family"
        scene = AppleBattleSceneDirector.select_scene(
            beat=beat,
            beat_index=index,
            chat_id=-100,
            cast_keys=("crimson", "green"),
            seed="a" * 32,
        )
        assert scene is not None, f"no compatible plate for beat {index}"
        assert scene.category in families
        assert scene.category in BEAT_EXPECTATIONS[index]
        assert scene.path.exists()


def test_wrong_identity_plates_are_never_used():
    """A crimson/gold battle must not show the crimson/green or gold/moon duo."""
    plan = [
        {"phase": "intro", "text": "..."},
        {"phase": "round", "round": 1, "text": "..."},
        {"phase": "round", "round": 2, "text": "..."},
        {"phase": "round", "round": 3, "text": "..."},
        {"phase": "finale", "text": "..."},
    ]
    battle_casts = {"crimson", "gold"}
    for index, beat in enumerate(plan):
        for seed in ("s1", "s2", "s3", "s4", "s5"):
            scene = AppleBattleSceneDirector.select_scene(
                beat=beat,
                beat_index=index,
                chat_id=-100,
                cast_keys=battle_casts,
                seed=seed,
            )
            if scene is None:
                continue  # caller falls back to card art (identity-correct)
            assert scene.cast_free or set(scene.casts) == battle_casts
            assert not (set(scene.casts) - battle_casts)


def test_unknown_cast_battle_uses_identity_free_plates_only():
    scene = AppleBattleSceneDirector.select_scene(
        beat={"phase": "intro", "text": "..."},
        beat_index=0,
        chat_id=-100,
        cast_keys=(),
        seed="seed-x",
    )
    assert scene is None or scene.cast_free


# ---------------------------------------------------------------------------
# 3. Determinism + variety
# ---------------------------------------------------------------------------


def test_selection_is_stable_for_the_same_battle_seed():
    beat = {"phase": "round", "round": 1, "text": "..."}
    args = dict(beat=beat, beat_index=1, chat_id=-100, cast_keys=("crimson", "green"), seed="f" * 32)
    first = AppleBattleSceneDirector.select_scene(**args)
    # Global RNG state and call order must not influence the outcome.
    random.seed(1)
    second = AppleBattleSceneDirector.select_scene(**args)
    random.random()
    AppleBattleSceneLibrary.reset_cache()
    third = AppleBattleSceneDirector.select_scene(**args)
    assert first is not None
    assert first.id == second.id == third.id


def test_different_seeds_can_pick_different_variants():
    """Variety: different battles get different plates, all still compatible."""
    beat = {"phase": "round", "round": 1, "text": "..."}
    picked = {
        AppleBattleSceneDirector.select_scene(
            beat=beat,
            beat_index=1,
            chat_id=-100,
            cast_keys=("crimson", "green"),
            seed=f"seed-{index}",
        ).id
        for index in range(24)
    }
    assert len(picked) >= 2, "selection collapsed to a single plate"
    for scene_id in picked:
        scene = next(s for s in AppleBattleSceneLibrary.available() if s.id == scene_id)
        assert scene.category in BEAT_EXPECTATIONS[1]


def test_both_groups_receive_valid_plates_and_beats_vary():
    plan = [
        {"phase": "intro", "text": "..."},
        {"phase": "round", "round": 1, "text": "..."},
        {"phase": "round", "round": 2, "text": "..."},
        {"phase": "round", "round": 3, "text": "..."},
        {"phase": "finale", "text": "..."},
    ]
    beats = {
        (index, chat): AppleBattleSceneDirector.select_scene(
            beat=beat,
            beat_index=index,
            chat_id=chat,
            cast_keys=("crimson", "green"),
            seed="variety-seed",
        )
        for index, beat in enumerate(plan)
        for chat in (-100, -200)
    }
    # Every beat/chat pair resolves to a compatible plate.
    assert len(beats) == 10
    assert all(scene is not None for scene in beats.values())
    for (index, _chat), scene in beats.items():
        assert scene.category in BEAT_EXPECTATIONS[index]
    # The three rounds do not repeat a single picture across the battle.
    assert len({beats[(i, -100)].id for i in (1, 2, 3)}) >= 2, "rounds repeat one plate"


# ---------------------------------------------------------------------------
# 4. Integration with the Phase 2A flow (real plates, mocked bot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narration_loop_sends_real_scene_plates(db_session):
    ctx = await _setup_battle(db_session, -71001, "plates")
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    photos = [event[3] for event in bot.order if event[0] == "send_photo"]
    assert len(photos) == len(ctx["plan"]) * 2
    for photo in photos:
        path = str(getattr(photo, "path", ""))
        assert "assets/apple_battle/" in path, "narration must use pre-generated plates"

    # The plate for each beat matches its family (caption can never contradict).
    per_beat: dict[int, list[str]] = {}
    for _kind, _chat, caption, photo in [e for e in bot.order if e[0] == "send_photo"]:
        index = [beat["text"] for beat in ctx["plan"]].index(caption)
        per_beat.setdefault(index, []).append(Path(getattr(photo, "path")).name)
    for index, files in per_beat.items():
        families = AppleBattleSceneDirector.families_for_beat(ctx["plan"][index])
        allowed = {
            scene.file
            for family in families
            for scene in AppleBattleSceneLibrary.by_category(family)
        }
        assert set(files) <= allowed, f"beat {index} used a mismatched plate"


@pytest.mark.asyncio
async def test_restart_during_narration_reuses_the_same_plates(db_session):
    """Determinism across a restart: the same beats yield the same plates."""
    ctx = await _setup_battle(db_session, -71011, "restartplates")
    plan = ctx["plan"]

    async def _plates_for(chat_id):
        plates = {}
        for index in range(len(plan)):
            plates[index] = AppleBattleSceneDirector.select_scene(
                beat=plan[index],
                beat_index=index,
                chat_id=chat_id,
                cast_keys=("crimson", "green"),
                seed=ctx["seed"],
                plan=plan,
            )
        return plates

    expected = await _plates_for(ctx["chat1"])

    # Simulate a mid-narration restart: recovery re-arms, then the loop runs.
    stats = await RecoveryService.recover_on_startup(db_session, bot=None)
    assert stats["resumed_apple_battles"] == 1
    await AppleBattleService.cancel_narration(ctx["match_id"])

    bot = _fake_bot()
    await AppleBattleService.run_narration(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    sent = [
        (event[2], Path(getattr(event[3], "path")).name)
        for event in bot.order
        if event[0] == "send_photo" and event[1] == ctx["chat1"]
    ]
    assert len(sent) == len(plan)
    for caption, filename in sent:
        index = [beat["text"] for beat in plan].index(caption)
        assert filename == expected[index].file, "restart changed the chosen plate"

    # Cleanup + settlement are untouched by the real artwork.
    state = json.loads((await db_session.get(Match, ctx["match_id"])).game_state)
    assert state["narration"]["cleaned"] is True
    assert all(record["deleted"] for record in state["narration"]["messages"])
    assert len(bot.delete_message.call_args_list) == len(plan) * 2


@pytest.mark.asyncio
async def test_final_report_is_unchanged_after_real_plates(db_session):
    ctx = await _setup_battle(db_session, -71021, "reportplates")
    bot = _fake_bot()

    await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
    await db_session.rollback()

    match = await db_session.scalar(select(Match).where(Match.id == ctx["match_id"]))
    state = json.loads(match.game_state)
    engine = AppleBattleEngine()

    report_edits = [event for event in bot.order if event[0] in ("edit_media", "edit_text")]
    assert len(report_edits) == 2
    for _kind, chat_id, caption, media in report_edits:
        expected_report = engine.render_for_group(state, chat_id, None)
        assert caption == photo_caption(expected_report)
        assert "✅ این نبرد به پایان رسید." in caption
        # The final report still shows the WINNER'S CARD ART (Phase 2A path),
        # never a narration plate: the report is untouched by Phase 2B.
        media_path = str(getattr(getattr(media, "media", None), "path", ""))
        assert "assets/characters/" in media_path
        assert "assets/apple_battle/" not in media_path

    # And the narration plates were temporary: every one was deleted.
    deleted = {event[2] for event in bot.order if event[0] == "delete"}
    sent_ids = {
        record["message_id"] for record in state["narration"]["messages"]
    }
    assert deleted == sent_ids


@pytest.mark.asyncio
async def test_scene_director_survives_missing_manifest(db_session, monkeypatch):
    """A missing/corrupt manifest degrades to the Phase 2A fallback, never crashes."""
    import sib.services.apple_battle_scenes as scenes_module

    monkeypatch.setattr(scenes_module, "MANIFEST_PATH", Path("/nonexistent/manifest.json"))
    AppleBattleSceneLibrary.reset_cache()
    try:
        assert AppleBattleSceneLibrary.available() == []
        assert AppleBattleSceneDirector.describe()["total"] == 0
        ctx = await _setup_battle(db_session, -71031, "nomanifest")
        bot = _fake_bot()
        await AppleBattleService._narration_loop(bot, ctx["match_id"], beat_delay=0.0)
        await db_session.rollback()
        # The narration still happened (card art / placeholder / text only).
        # Direct level-up notices are separate from those narrative beats.
        narration_texts = {beat["text"] for beat in ctx["plan"]}
        narration_sends = [
            call.kwargs.get("caption")
            for call in bot.send_photo.call_args_list
            if call.kwargs.get("caption") in narration_texts
        ]
        for call in bot.send_message.call_args_list:
            text = call.kwargs.get("text")
            if text is None and len(call.args) > 1:
                text = call.args[1]
            if text in narration_texts:
                narration_sends.append(text)
        assert len(narration_sends) == len(ctx["plan"]) * 2
        assert bot.send_message.await_count == 2  # committed Level-up notices
        match = await db_session.scalar(select(Match).where(Match.id == ctx["match_id"]))
        assert match.result_applied is True
    finally:
        AppleBattleSceneLibrary.reset_cache()
