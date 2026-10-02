"""Apple Battle narration scene library + selection director (Phase 2B).

Phase 2A delivered one temporary Telegram message per narration beat and let
the beat's photo come from the fighter's card art. Phase 2B replaces that with
a real, reusable library of PRE-GENERATED cinematic scene plates
(``assets/apple_battle/``) wired into the exact same message flow.

Design rules
------------
* **Pre-generated only.** Every plate is a static repository asset described by
  ``assets/apple_battle/manifest.json``. Nothing is ever generated at runtime —
  the runtime path is: battle event → pick a pre-generated plate → send photo
  with the beat caption.
* **Deterministic.** Selection is a pure function of the PERSISTED battle seed,
  the beat index and the target chat. Nothing else feeds the choice (no clock,
  no global RNG, no call order), so a battle replays identically and a restart
  during narration re-picks the very same plate.
* **Truthful.** A plate is only used when it cannot contradict the caption:
  the plate must belong to a family that fits the beat's meaning, and its cast
  must either be exactly this battle's two fighters or be identity-free
  (silhouette/reaction plates). Otherwise the caller falls back to Phase 2A
  behaviour (the fighter's own card art), never to a wrong-identity plate.
* **Data-driven.** Categories, cast registry and scenes live in the manifest;
  adding new artwork never requires touching the message flow.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sib.core.logging import logger

SCENES_DIR = Path(__file__).resolve().parent.parent / "assets" / "apple_battle"
MANIFEST_PATH = SCENES_DIR / "manifest.json"

STATUS_READY = "ready"
STATUS_PLANNED = "planned"


@dataclass(frozen=True)
class AppleBattleScene:
    """One pre-generated narration plate."""

    id: str
    category: str
    file: str
    casts: Tuple[str, ...] = ()
    cast_free: bool = False
    action: str = ""
    status: str = STATUS_READY
    prompt_summary: str = ""

    @property
    def path(self) -> Path:
        return SCENES_DIR / self.file


class AppleBattleSceneLibrary:
    """Read-only access to the scene manifest (cached per process)."""

    _scenes: Optional[List[AppleBattleScene]] = None
    _cast_by_code: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> List[AppleBattleScene]:
        """Parse the manifest once; never raises (an empty library is valid)."""
        if cls._scenes is not None:
            return cls._scenes

        scenes: List[AppleBattleScene] = []
        cast_by_code: Dict[str, str] = {}
        try:
            raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            for key, info in (raw.get("casts") or {}).items():
                code = (info or {}).get("code")
                if code:
                    cast_by_code[str(code)] = str(key)
            for entry in raw.get("scenes") or []:
                if not isinstance(entry, dict) or not entry.get("id"):
                    continue
                scenes.append(
                    AppleBattleScene(
                        id=str(entry["id"]),
                        category=str(entry.get("category") or ""),
                        file=str(entry.get("file") or ""),
                        casts=tuple(str(c) for c in (entry.get("casts") or ())),
                        cast_free=bool(entry.get("cast_free")),
                        action=str(entry.get("action") or ""),
                        status=str(entry.get("status") or STATUS_READY),
                        prompt_summary=str(entry.get("prompt_summary") or ""),
                    )
                )
        except FileNotFoundError:
            logger.warning(f"Apple battle scene manifest missing at {MANIFEST_PATH}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not read apple battle scene manifest: {exc}")

        cls._scenes = scenes
        cls._cast_by_code = cast_by_code
        return scenes

    @classmethod
    def reset_cache(cls) -> None:
        """Drop the cache (tests after adding/removing artwork)."""
        cls._scenes = None
        cls._cast_by_code = None

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    @classmethod
    def available(cls) -> List[AppleBattleScene]:
        """Scenes that are shipped AND present on disk."""
        return [
            scene
            for scene in cls.load()
            if scene.status == STATUS_READY and scene.file and scene.path.exists()
        ]

    @classmethod
    def all_scenes(cls) -> List[AppleBattleScene]:
        """Every manifest entry, including the planned (not yet generated) ones."""
        return list(cls.load())

    @classmethod
    def by_category(cls, category: str) -> List[AppleBattleScene]:
        return [scene for scene in cls.available() if scene.category == category]

    @classmethod
    def categories(cls) -> Dict[str, int]:
        """Ready-scene count per category (major families must stay > 1)."""
        counts: Dict[str, int] = {}
        for scene in cls.available():
            counts[scene.category] = counts.get(scene.category, 0) + 1
        return counts

    @classmethod
    def cast_key_for_code(cls, code: Optional[str]) -> Optional[str]:
        """Map a character code (``sib_sorkh``) to a manifest cast key."""
        cls.load()
        if not code:
            return None
        return (cls._cast_by_code or {}).get(str(code))


class AppleBattleSceneDirector:
    """Chooses the plate for a narration beat — deterministically and safely."""

    #: Beat phase/round → the families whose meaning matches the caption.
    BEAT_FAMILIES: Dict[Tuple[str, Optional[int]], Tuple[str, ...]] = {
        ("intro", None): ("opening",),
        ("round", 1): ("attack", "counterattack"),
        ("round", 2): ("impact", "attack"),
        ("round", 3): ("turning_point", "critical_moment", "impact"),
        ("finale", None): ("victory", "final_attack"),
    }

    #: Meaning-preserving fallbacks when a family has no compatible plate.
    #: A fallback is only listed when the caption would still be true of the
    #: picture (e.g. a finishing blow IS an attack, so ``victory`` may fall
    #: back to ``final_attack`` and then to ``attack``).
    FAMILY_FALLBACKS: Dict[str, Tuple[str, ...]] = {
        "opening": (),
        "attack": ("impact",),
        "counterattack": ("attack", "impact"),
        "dodge": ("attack",),
        "impact": ("attack",),
        "power_up": ("attack", "impact"),
        "critical_moment": ("impact", "turning_point"),
        "turning_point": ("critical_moment", "impact"),
        "final_attack": ("impact", "attack"),
        "victory": ("final_attack", "attack", "impact"),
    }

    # ------------------------------------------------------------------
    # Beat semantics
    # ------------------------------------------------------------------
    @classmethod
    def families_for_beat(cls, beat: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
        """Families compatible with a narration beat (never contradicts it)."""
        if not isinstance(beat, dict):
            return ()
        phase = str(beat.get("phase") or "")
        round_no: Optional[int]
        try:
            round_no = int(beat["round"]) if beat.get("round") is not None else None
        except (TypeError, ValueError):
            round_no = None

        families = cls.BEAT_FAMILIES.get((phase, round_no))
        if families is None:
            families = cls.BEAT_FAMILIES.get((phase, None))
        if families is None:
            return ()

        expanded: List[str] = []
        for family in families:
            for candidate in (family,) + cls.FAMILY_FALLBACKS.get(family, ()):
                if candidate not in expanded:
                    expanded.append(candidate)
        return tuple(expanded)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    @classmethod
    def _ordered_pool(
        cls,
        families: Sequence[str],
        cast_keys: Iterable[Optional[str]],
    ) -> List[AppleBattleScene]:
        """Eligible plates in a stable order (exact-cast first, then cast-free).

        Only plates whose cast is exactly this battle's pair and identity-free
        plates are eligible; a plate showing a different duo is never used for
        a named caption.
        """
        wanted = {str(key) for key in cast_keys if key}
        ordered_families: List[str] = []
        for family in families:
            if family not in ordered_families:
                ordered_families.append(family)

        exact: List[AppleBattleScene] = []
        identity_free: List[AppleBattleScene] = []
        seen: set[str] = set()
        for family in ordered_families:
            for scene in AppleBattleSceneLibrary.by_category(family):
                if scene.id in seen:
                    continue
                seen.add(scene.id)
                cast_set = set(scene.casts)
                if scene.cast_free and not cast_set:
                    identity_free.append(scene)
                elif cast_set and wanted and cast_set == wanted:
                    exact.append(scene)
        # Exact-cast plates lead (they show the real fighters); identity-free
        # plates remain available as cinematic cuts that are always truthful.
        return exact + identity_free

    @classmethod
    def compatible_pool(
        cls,
        families: Sequence[str],
        cast_keys: Iterable[Optional[str]],
    ) -> List[AppleBattleScene]:
        """Public view of the eligible pool (see ``_ordered_pool``)."""
        return cls._ordered_pool(families, cast_keys)

    @classmethod
    def _raw_pick_index(
        cls,
        families: Sequence[str],
        cast_keys: Iterable[Optional[str]],
        beat_index: int,
        chat_id: int,
        seed: Optional[str],
    ) -> Optional[Tuple[List[AppleBattleScene], int]]:
        """Seeded pick without the anti-repeat rule: the pool plus an index."""
        pool = cls._ordered_pool(families, cast_keys)
        if not pool:
            return None
        key = f"scene:{seed or 'no-seed'}:{int(beat_index)}:{int(chat_id)}"
        # Separate, dedicated RNG streams: the outcome cannot depend on how
        # many other random calls happened before (procedural-gen rule).
        chooser = random.Random(f"{key}:pick")
        return pool, chooser.randrange(len(pool))

    @classmethod
    def plan_sequence(
        cls,
        plan: Sequence[Dict[str, Any]],
        *,
        upto: int,
        chat_id: int,
        cast_keys: Iterable[Optional[str]],
        seed: Optional[str],
    ) -> List[Optional[AppleBattleScene]]:
        """Deterministic plate sequence for the first ``upto`` beats.

        Each step applies the anti-repeat rule against the FINAL choice of the
        previous beat, so two consecutive narration messages never show the
        same picture while an alternative exists. The whole sequence is a pure
        function of the persisted plan/seed/chat — no state, no clock — so a
        restart reproduces it exactly.
        """
        picks: List[Optional[AppleBattleScene]] = []
        for index in range(max(0, int(upto))):
            if index >= len(plan):
                break
            picked = cls._raw_pick_index(
                cls.families_for_beat(plan[index]), cast_keys, index, chat_id, seed
            )
            if picked is None:
                picks.append(None)
                continue
            pool, choice = picked
            previous = picks[-1] if picks else None
            if previous is not None and len(pool) > 1 and pool[choice].id == previous.id:
                choice = (choice + 1) % len(pool)
            picks.append(pool[choice])
        return picks

    @classmethod
    def select_scene(
        cls,
        *,
        beat: Optional[Dict[str, Any]],
        beat_index: int,
        chat_id: int,
        cast_keys: Iterable[Optional[str]],
        seed: Optional[str],
        plan: Optional[Sequence[Dict[str, Any]]] = None,
        previous_beat: Optional[Dict[str, Any]] = None,
    ) -> Optional[AppleBattleScene]:
        """Pick the beat's plate. Pure function of the persisted inputs.

        With ``plan`` the pick is taken from the deterministic sequence (which
        also enforces the no-immediate-repeat rule); without it, the single
        beat is picked on its own. Neither path uses in-memory state, so a
        restart during narration re-picks exactly the same plate.
        """
        if plan is not None and 0 <= beat_index < len(plan):
            sequence = cls.plan_sequence(
                plan, upto=beat_index + 1, chat_id=chat_id, cast_keys=cast_keys, seed=seed
            )
            if beat_index < len(sequence):
                return sequence[beat_index]
            return None

        families = cls.families_for_beat(beat)
        if not families:
            return None
        picked = cls._raw_pick_index(families, cast_keys, beat_index, chat_id, seed)
        if picked is None:
            return None
        pool, index = picked

        if previous_beat is not None and beat_index > 0 and len(pool) > 1:
            prev_families = cls.families_for_beat(previous_beat)
            prev = (
                cls._raw_pick_index(
                    prev_families, cast_keys, beat_index - 1, chat_id, seed
                )
                if prev_families
                else None
            )
            if prev is not None and pool[index].id == prev[0][prev[1]].id:
                index = (index + 1) % len(pool)
        return pool[index]

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        """Diagnostics: ready scenes per category and the planned backlog."""
        library = AppleBattleSceneLibrary
        ready = library.categories()
        planned = [
            scene.id
            for scene in library.all_scenes()
            if scene.status != STATUS_READY or not scene.path.exists()
        ]
        return {"ready": ready, "planned": planned, "total": len(library.all_scenes())}
