"""Apple Battle cinematic narration delivery (Phase 2A).

Before Phase 2A the live narration was appended into the SAME group board
message by repeatedly editing it (``_edit_boards`` on every beat). Phase 2A
delivers every narration beat as its OWN Telegram message — a photo plus a
short cinematic caption — and removes those temporary messages when the
battle ends, before the (unchanged) final Apple Battle report is rendered.

Teardown runs while the match is still ACTIVE, so a crash mid-cleanup is
recovered on restart (recovery finalizes the battle and deletes whatever is
left); settlement follows cleanup and is therefore never broken by a
Telegram delete failure.

Restart safety (Phase 1 recovery architecture)
----------------------------------------------
* The plan cursor in ``match.game_state`` remains the ONE source of truth for
  narration progress. It is advanced and persisted BEFORE the Telegram send,
  so a restart can never re-send an already claimed beat: delivery is
  deliberately AT-MOST-ONCE (a crash between claim and send can skip one
  beat, but can never produce duplicate narration events).
* The Telegram message ids of the temporary narration messages are persisted
  inside ``match.game_state`` (``state["narration"]["messages"]``) — the same
  restart-safe state Phase 1 recovery already reads/resumes from, so no
  in-memory list is required and no schema migration is involved.
* Recording a message id rewrites ONLY the ``narration`` sub-object of the
  persisted state, so it can never roll back a concurrently persisted cursor.
* ``cleanup`` never raises: a Telegram delete failure is logged and left for a
  later pass; it can never break settlement or the final report.

Artwork (Phase 2A → Phase 2B)
-----------------------------
``AppleBattleNarrationArt`` is the single place that maps a beat to a photo.
Phase 2B resolves it from the PRE-GENERATED scene library
(``assets/apple_battle/``, see ``sib.services.apple_battle_scenes``); the
fighter's own card art and then an existing placeholder remain as graceful
fallbacks. Nothing is ever generated at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select

from sib.core.bidi import fix_bidi
from sib.core.logging import Events, log_event, logger
from sib.database.connection import get_session
from sib.database.models import Character, Match, PlayerCard

# Key inside ``match.game_state`` holding the persisted narration bookkeeping.
NARRATION_STATE_KEY = "narration"
NARRATION_STATE_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Existing bundled project image used as the Phase 2A placeholder when a
# fighter's card art is unavailable. Never generated at runtime.
GENERIC_PLACEHOLDER = _REPO_ROOT / "assets" / "banners" / "games.jpg"

# Telegram answers these when a message is already gone: nothing left to clean.
_ALREADY_GONE_MARKERS = (
    "message to delete not found",
    "message_id_invalid",
    "message not found",
)


def _parse_state(raw: Optional[str]) -> Dict[str, Any]:
    """Best-effort parse of ``match.game_state`` into a dict."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class AppleBattleNarrationState:
    """Read helpers for the persisted narration block inside ``game_state``."""

    @staticmethod
    def block(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(state, dict):
            return {}
        block = state.get(NARRATION_STATE_KEY)
        return block if isinstance(block, dict) else {}

    @classmethod
    def messages(cls, state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Recorded temporary narration messages, in send order."""
        block = cls.block(state)
        messages = block.get("messages")
        if not isinstance(messages, list):
            return []
        return [m for m in messages if isinstance(m, dict)]

    @classmethod
    def delivered_beats(cls, state: Optional[Dict[str, Any]]) -> set[int]:
        """Beat indexes that were already delivered as narration messages."""
        beats: set[int] = set()
        for record in cls.messages(state):
            try:
                beats.add(int(record.get("beat")))
            except (TypeError, ValueError):
                continue
        return beats

    @classmethod
    def is_cleaned(cls, state: Optional[Dict[str, Any]]) -> bool:
        return bool(cls.block(state).get("cleaned"))

    @staticmethod
    def ensure(state: Dict[str, Any]) -> Dict[str, Any]:
        """Return the mutable narration block, creating it when missing."""
        block = state.get(NARRATION_STATE_KEY)
        if not isinstance(block, dict):
            block = {}
        block.setdefault("version", NARRATION_STATE_VERSION)
        if not isinstance(block.get("messages"), list):
            block["messages"] = []
        block.setdefault("cleaned", False)
        state[NARRATION_STATE_KEY] = block
        return block


class AppleBattleNarrationArt:
    """Beat→photo resolution.

    Resolution order (Phase 2B):
      1. the pre-generated scene library — a deterministic, meaning-compatible
         plate whose cast matches this battle (or is identity-free);
      2. Phase 2A fallback: the focus fighter's own card art;
      3. the bundled generic placeholder;
      4. ``None`` → text-only message (a narration event is never lost).

    Every branch only ever references existing repository files.
    """

    @staticmethod
    def _ordered_user_ids(state: Dict[str, Any]) -> List[int]:
        players = state.get("players") or {}
        if not isinstance(players, dict):
            return []
        ids: List[int] = []
        for key in players:
            try:
                ids.append(int(key))
            except (TypeError, ValueError):
                continue
        ids.sort(key=lambda uid: (int(players.get(str(uid), {}).get("team", 0) or 0), uid))
        return ids

    @staticmethod
    def _group_player_id(state: Dict[str, Any], chat_id: int) -> Optional[int]:
        players = state.get("players") or {}
        if not isinstance(players, dict):
            return None
        for key, info in players.items():
            if not isinstance(info, dict):
                continue
            try:
                if int(info.get("group_id")) == int(chat_id):
                    return int(key)
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def focus_user_id(
        cls, state: Dict[str, Any], beat_index: int, chat_id: int
    ) -> Optional[int]:
        """The fighter whose card art represents this beat in this chat."""
        plan = state.get("plan") or []
        beat = plan[beat_index] if 0 <= beat_index < len(plan) and isinstance(plan[beat_index], dict) else {}
        ids = cls._ordered_user_ids(state)
        mine = cls._group_player_id(state, chat_id)
        phase = beat.get("phase")

        if phase == "finale":
            winner = (state.get("battle") or {}).get("winner_id")
            try:
                return int(winner)
            except (TypeError, ValueError):
                return mine or (ids[0] if ids else None)
        if phase == "intro":
            return mine or (ids[0] if ids else None)
        if not ids:
            return None
        try:
            round_no = int(beat.get("round") or 1)
        except (TypeError, ValueError):
            round_no = 1
        return ids[(round_no - 1) % len(ids)]

    @classmethod
    async def _card_photo(cls, session, state: Dict[str, Any], user_id: Optional[int]):
        if user_id is None:
            return None
        cards = state.get("cards") or {}
        info = cards.get(str(user_id)) if isinstance(cards, dict) else None
        card_id = info.get("card_id") if isinstance(info, dict) else None
        if not card_id:
            return None
        card = await session.get(PlayerCard, int(card_id))
        character = await session.get(Character, card.character_id) if card else None
        if character is None:
            return None
        from sib.services.character_art import photo_arg, resolve_character_photo

        file_id, path = await resolve_character_photo(session, character)
        return photo_arg(file_id, path)

    @classmethod
    def placeholder(cls):
        """Existing bundled placeholder image, or ``None`` when absent."""
        if not GENERIC_PLACEHOLDER.exists():
            return None
        from aiogram.types import FSInputFile

        return FSInputFile(GENERIC_PLACEHOLDER)

    # ------------------------------------------------------------------
    # Phase 2B — pre-generated scene library
    # ------------------------------------------------------------------
    @staticmethod
    def _scene_photo(scene):
        """Telegram photo argument for a scene plate, or None when missing."""
        try:
            if not scene.path.exists():
                return None
            from aiogram.types import FSInputFile

            return FSInputFile(scene.path)
        except Exception:  # pragma: no cover - defensive
            return None

    @classmethod
    async def battle_cast_keys(cls, session, state: Dict[str, Any]) -> List[str]:
        """Manifest cast keys of this battle's fighters (order: team order).

        New battles carry the key in the persisted state; for states written
        before Phase 2B the key is recovered from the card's character code —
        no recomputation of the battle, no extra persisted state required.
        """
        from sib.services.apple_battle_scenes import AppleBattleSceneLibrary

        cards = state.get("cards") or {}
        if not isinstance(cards, dict):
            return []

        keys: List[str] = []
        for uid in cls._ordered_user_ids(state):
            info = cards.get(str(uid))
            cast_key = info.get("cast") if isinstance(info, dict) else None
            if not cast_key and isinstance(info, dict) and info.get("card_id"):
                try:
                    card = await session.get(PlayerCard, int(info["card_id"]))
                    character = (
                        await session.get(Character, card.character_id) if card else None
                    )
                    cast_key = AppleBattleSceneLibrary.cast_key_for_code(
                        getattr(character, "code", None)
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(f"Could not resolve battle cast for user {uid}: {exc}")
                    cast_key = None
            if cast_key and cast_key not in keys:
                keys.append(str(cast_key))
        return keys

    @classmethod
    def scene_plate(
        cls,
        state: Dict[str, Any],
        beat_index: int,
        chat_id: int,
        cast_keys: Iterable[Optional[str]],
    ):
        """Deterministically chosen pre-generated plate for this beat, or None."""
        from sib.services.apple_battle_scenes import AppleBattleSceneDirector

        plan = state.get("plan") or []
        beat = (
            plan[beat_index]
            if isinstance(plan, list) and 0 <= beat_index < len(plan)
            else None
        )
        seed = (state.get("battle") or {}).get("seed")
        return AppleBattleSceneDirector.select_scene(
            beat=beat,
            beat_index=beat_index,
            chat_id=chat_id,
            cast_keys=cast_keys,
            seed=seed,
            plan=plan if isinstance(plan, list) else None,
        )

    @classmethod
    async def resolve(cls, session, state: Dict[str, Any], beat_index: int, chat_id: int):
        """Return a Telegram photo argument for one narration beat, or None."""
        # 1) PRE-GENERATED scene library (deterministic, meaning-compatible).
        try:
            cast_keys = await cls.battle_cast_keys(session, state)
            scene = cls.scene_plate(state, beat_index, chat_id, cast_keys)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Scene library lookup failed (chat {chat_id}): {exc}")
            scene = None
        if scene is not None:
            photo = cls._scene_photo(scene)
            if photo is not None:
                return photo

        # 2) Phase 2A fallback: the focus fighter's own card art.
        ordered = cls._ordered_user_ids(state)
        preferred = cls.focus_user_id(state, beat_index, chat_id)

        candidates: List[int] = []
        for uid in ([preferred] if preferred is not None else []) + ordered:
            if uid not in candidates:
                candidates.append(uid)

        for uid in candidates:
            photo = await cls._card_photo(session, state, uid)
            if photo is not None:
                return photo
        # 3) Generic placeholder, 4) text-only.
        return cls.placeholder()


class AppleBattleNarration:
    """Delivery, persistence and cleanup of temporary narration messages."""

    # ------------------------------------------------------------------
    # Persisted bookkeeping (only touches state["narration"])
    # ------------------------------------------------------------------
    @classmethod
    async def record_message(
        cls,
        match_id: int,
        *,
        beat: int,
        chat_id: int,
        message_id: int,
        group: Optional[int] = None,
    ) -> bool:
        """Persist one temporary narration message id (idempotent per beat+chat)."""
        async with get_session() as session:
            match = await session.scalar(
                select(Match).where(Match.id == match_id).with_for_update()
            )
            if match is None:
                return False
            state = _parse_state(match.game_state)
            block = AppleBattleNarrationState.ensure(state)
            for record in block["messages"]:
                try:
                    same = (
                        int(record.get("beat")) == int(beat)
                        and int(record.get("chat_id")) == int(chat_id)
                    )
                except (TypeError, ValueError):
                    same = False
                if same:
                    return False  # already recorded — never duplicate
            block["messages"].append(
                {
                    "beat": int(beat),
                    "chat_id": int(chat_id),
                    "message_id": int(message_id),
                    "group": int(group) if group is not None else None,
                    "deleted": False,
                }
            )
            block["cleaned"] = False
            match.game_state = json.dumps(state, ensure_ascii=False)
            await session.flush()
        return True

    @classmethod
    async def _mark(
        cls,
        match_id: int,
        *,
        deleted: Sequence[Tuple[int, int]] = (),
        cleaned: Optional[bool] = None,
    ) -> None:
        """Best-effort bookkeeping update of the persisted narration block.

        Never invents a block for matches that never had temporary narration
        messages (legacy or non-narrated battles stay byte-identical).
        """
        if not deleted and cleaned is None:
            return
        try:
            async with get_session() as session:
                match = await session.scalar(
                    select(Match).where(Match.id == match_id).with_for_update()
                )
                if match is None:
                    return
                state = _parse_state(match.game_state)
                existing = state.get(NARRATION_STATE_KEY)
                if not isinstance(existing, dict):
                    return  # nothing to bookkeep — never invent a block
                block = AppleBattleNarrationState.ensure(state)
                deleted_set = {(int(c), int(m)) for c, m in deleted}
                for record in block["messages"]:
                    try:
                        key = (int(record.get("chat_id")), int(record.get("message_id")))
                    except (TypeError, ValueError):
                        continue
                    if key in deleted_set:
                        record["deleted"] = True
                if cleaned is not None:
                    block["cleaned"] = bool(cleaned)
                match.game_state = json.dumps(state, ensure_ascii=False)
                await session.flush()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"Could not persist narration bookkeeping for match {match_id}: {exc}"
            )

    @classmethod
    async def pending_messages(cls, match_id: int) -> List[Dict[str, Any]]:
        """Recorded narration messages that were not cleaned up yet."""
        try:
            async with get_session() as session:
                match = await session.get(Match, match_id)
                if match is None:
                    return []
                records = AppleBattleNarrationState.messages(
                    _parse_state(match.game_state)
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not load narration messages for match {match_id}: {exc}")
            return []
        return [r for r in records if not r.get("deleted")]

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    @classmethod
    async def send_beat(
        cls,
        bot,
        *,
        match_id: int,
        state: Dict[str, Any],
        beat_index: int,
        targets: Iterable[Tuple[int, int]],
    ) -> List[Dict[str, Any]]:
        """Send ONE narration beat as a separate photo message per target chat.

        Returns the recorded message payloads. Delivery failures are logged
        and skipped; narration never blocks settlement or recovery.
        """
        plan = state.get("plan") or []
        if not isinstance(plan, list) or not (0 <= beat_index < len(plan)):
            return []
        beat = plan[beat_index] if isinstance(plan[beat_index], dict) else {}
        # A beat opens with an emoji and names both players, so the paragraph
        # direction is pinned to RTL before it is rendered/sent.
        text = fix_bidi(str(beat.get("text") or ""))
        if not text:
            return []
        if bot is None:
            logger.warning(
                f"Apple battle narration beat {beat_index} of match {match_id} "
                "has no bot instance to deliver with"
            )
            return []

        from sib.services.telegram_ui import safe_send_screen

        records: List[Dict[str, Any]] = []
        for chat_id, group_no in targets:
            photo = None
            try:
                async with get_session() as session:
                    photo = await AppleBattleNarrationArt.resolve(
                        session, state, beat_index, chat_id
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    f"Could not resolve narration art for match {match_id} "
                    f"beat {beat_index} chat {chat_id}: {exc}"
                )
            try:
                sent = await safe_send_screen(
                    bot, chat_id, text, photo=photo, parse_mode="Markdown"
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    f"Could not send narration beat {beat_index} of match "
                    f"{match_id} to chat {chat_id}: {exc}"
                )
                sent = None

            message_id = getattr(sent, "message_id", None)
            if not isinstance(message_id, int):
                logger.warning(
                    f"Apple battle narration beat {beat_index} was not delivered "
                    f"to chat {chat_id} (match {match_id})"
                )
                continue
            try:
                await cls.record_message(
                    match_id,
                    beat=beat_index,
                    chat_id=chat_id,
                    message_id=message_id,
                    group=group_no,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    f"Could not persist narration message id for match "
                    f"{match_id} beat {beat_index}: {exc}"
                )
            records.append(
                {
                    "beat": int(beat_index),
                    "chat_id": int(chat_id),
                    "message_id": int(message_id),
                    "group": group_no,
                }
            )
        if records:
            log_event(
                Events.APPLE_BATTLE_EVENT,
                {
                    "action": "narration_beat_sent",
                    "match_id": match_id,
                    "beat": beat_index,
                    "messages": len(records),
                },
            )
        return records

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    @classmethod
    async def cleanup(cls, bot, match_id: int) -> int:
        """Delete every recorded temporary narration message (never raises).

        A message that is already gone counts as cleaned. Delete failures are
        logged and left recorded, so a later pass can retry; they never affect
        settlement, the persisted result or the final report.
        """
        records = await cls.pending_messages(match_id)

        if not records:
            if bot is not None:
                await cls._mark(match_id, cleaned=True)
            return 0

        if bot is None:
            # No bot handle (e.g. recovery without a bot): keep the records and
            # let a later pass clean them up. Settlement is untouched.
            return 0

        deleted: List[Tuple[int, int]] = []
        failed = 0
        for record in records:
            chat_id = record.get("chat_id")
            message_id = record.get("message_id")
            if chat_id is None or message_id is None:
                continue
            try:
                await bot.delete_message(int(chat_id), int(message_id))
                deleted.append((int(chat_id), int(message_id)))
            except Exception as exc:
                if any(marker in str(exc).lower() for marker in _ALREADY_GONE_MARKERS):
                    # Already deleted (manually or by Telegram): nothing to do.
                    deleted.append((int(chat_id), int(message_id)))
                    continue
                failed += 1
                logger.warning(
                    f"Could not delete narration message {message_id} in chat "
                    f"{chat_id} for match {match_id}: {exc}"
                )

        if deleted:
            await cls._mark(match_id, deleted=deleted)
        if not failed:
            await cls._mark(match_id, cleaned=True)

        log_event(
            Events.APPLE_BATTLE_EVENT,
            {
                "action": "narration_cleanup",
                "match_id": match_id,
                "deleted": len(deleted),
                "failed": failed,
            },
        )
        return len(deleted)
