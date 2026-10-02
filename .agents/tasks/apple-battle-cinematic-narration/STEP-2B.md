# STEP-2B — Apple Battle Narration Visual Library + Integration

Status: **COMPLETE**
Branch: `arena/01a0fd14-sib` · Base: `8896891` (main)
Commits: `d0d5f3a` (runtime + library + artwork + tests + skills),
`<report commit>` (this document)

---

## 1. What changed

Phase 2A delivered live narration as **separate temporary messages** (one photo +
caption per beat) using the fighter's card art. Phase 2B adds a real, reusable
library of **pre-generated cinematic plates** and wires it into that same flow.

```
battle beat → AppleBattleSceneDirector (pure, seeded)
            → pre-generated plate (assets/apple_battle/*.jpg)
            → safe_send_screen(photo + beat caption)   ← separate message
… plan finished → delete temporary messages → settlement → UNCHANGED final report
```

The battle formula, RNG seed, winner, rewards, matchmaking, bot difficulty and
the final report were not touched. No database migration was needed.

## 2. Files

| File | Change |
|---|---|
| `services/apple_battle_narration.py` | beat delivery picker + restart-safe message-id bookkeeping + cleanup (Phase 2A); `resolve()` now prefers the scene library |
| `services/apple_battle_scenes.py` **(new)** | `AppleBattleSceneLibrary` (manifest access) + `AppleBattleSceneDirector` (deterministic selection, `plan_sequence`) |
| `services/apple_battle.py` | narration loop (separate messages, cursor-before-send, cleanup-before-settlement); persisted `cast` key per fighter |
| `services/recovery/service.py` | cleans narration messages for battles finalized during startup recovery |
| `services/maintenance/service.py` | cleans narration messages for battles resolved by the turn-timeout backstop |
| `games/apple_battle/engine.py`, `handlers/apple_battle.py` | docstrings only |
| `assets/apple_battle/` **(new)** | 25 plates + `manifest.json` |
| `tests/test_apple_battle_scene_library.py` **(new)** | Phase 2B suite (17 tests) |
| `tests/test_apple_battle_narration.py` **(new)** | Phase 2A suite (14 tests) |
| `tests/test_apple_battle.py` | loop test updated to the separate-message contract |
| `.agents/skills/create-game-assets`, `.agents/skills/procedural-gen` | GameDev skills installed + tracked |
| `.agents/tasks/apple-battle-cinematic-narration/` | art direction, stage records, library preview |

## 3. Generated narration assets

* **Location:** `assets/apple_battle/`
* **Count:** **25 plates** (all 1024×1024, 1:1 JPEG, 126–216 KB, total ≈ 4.1 MB)
* **Catalog:** 25 scene entries in `manifest.json` across 10 categories, all `ready`

| Category | Ready variants | Category | Ready variants |
|---|---|---|---|
| opening | 3 | turning_point | 2 |
| attack | 4 | final_attack | 2 |
| counterattack | 2 | victory | 3 |
| dodge | 2 | power_up | 2 |
| impact | 3 | critical_moment | 2 |

**Identity-free (cast-free) plates:** 6 — `opening_03_silhouettes`,
`impact_02_silhouettes`, `attack_04_silhouettes`, `impact_03_silhouettes`,
`victory_02_champion_silhouette`, `victory_03_silhouettes`.

Casts in the library: **crimson (Red Apple)**, **green (Green Apple)**,
**gold (Golden Apple)**, **moon (Moon Apple)** — matching the four released
characters, so every real battle can be pictured with the correct fighters.

## 4. How image selection works

1. **Beat → families** (`BEAT_FAMILIES`):
   `intro→opening`, `round1→attack|counterattack`, `round2→impact|attack`,
   `round3→turning_point|critical_moment|impact`,
   `finale→victory|final_attack`, each extended with meaning-preserving
   fallbacks (a finishing blow *is* an attack, so `victory→final_attack→attack`).
2. **Eligibility filter:** a plate is used only when its cast set is **exactly
   this battle's pair** or it is **identity-free**. A caption naming two
   fighters can never sit on a picture of two different fighters; when no
   compatible plate exists the resolver falls back to Phase 2A card art (and
   then to the bundled placeholder / text-only) rather than showing a wrong image.
3. **Deterministic pick:** `random.Random(f"scene:{seed}:{beat}:{chat}:pick")`
   over the ordered eligible pool — a **pure function of the persisted battle
   seed, beat index and chat**. No clock, no global RNG, no call-order
   dependence. Restart during narration re-derives exactly the same plate.
4. **Variety:** `plan_sequence()` computes the whole battle's plate sequence,
   where each step avoids the plate used by the previous beat (consecutive
   narration messages never show the same picture). Verified over 150
   sequences: zero consecutive repeats.
5. **Outcome safety:** nothing in this path writes gameplay state; the result
   is computed and persisted before narration (Phase 1 invariant).

## 5. Artwork provenance & constraints

* Generated **ahead of time**, one batch per turn (the generator allows 10
  images per turn), using `assets/apple_battle/opening_01_crimson_green.jpg`
  as the style reference for the whole family so palette, arena, lighting,
  camera language and costume locks hold.
* Art direction brief: `.agents/tasks/apple-battle-cinematic-narration/ART_DIRECTION.md`.
* **Nothing is generated at runtime** — asserted by a test that greps the
  narration modules for any HTTP/image-service usage and verifies every
  resolved plate is an existing file inside the repository asset folder.
* Contact sheet: `.agents/tasks/apple-battle-cinematic-narration/library_preview.jpg`.

## 6. Tests

Command (isolated SQLite test database, SQLite file name enforced by conftest):

```
rm -f /tmp/sibcom_sib_test.db && PYTHONPATH=/home/user \
  DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" \
  .venv/bin/python -m pytest -q
```

| Run | Result |
|---|---|
| `tests/test_apple_battle_scene_library.py` | **17 passed** |
| `tests/test_apple_battle_narration.py` | **14 passed** |
| `tests/test_apple_battle.py` (Phase 1 core, recovery, timeout, races) | **30 passed** |
| Apple Battle focused total | **61 passed** |
| Full suite | **693 passed, 8 skipped, 3 failed** |
| Same 3 failures at the BASE commit (`git stash` check) | **3 failed** — pre-existing, unrelated (`test_character_art.py` frame geometry) |

Phase 2B coverage: library integrity (1:1 / 1024² / JPEG / on-disk /
Telegram-sized), multi-variant families, every beat family has ready artwork,
beat↔plate meaning match, wrong-identity guards, unbounded-cast guard,
determinism (incl. cache reset + global-RNG noise), variety across seeds,
no-consecutive-repeat sequences, `mode(plan) == mode(single beat)`,
identity-free variety for unpaired matchups, restart re-uses the same plates,
cleanup deletes every plate message, settlement + final report unchanged,
missing-manifest degradation, and "no runtime image generation".

## 7. Verification of the artwork

* All 25 files verified programmatically: `PIL.verify()`, size exactly
  1024×1024, JPEG, ≤ 512 KB (mostly ~160 KB).
* Visually inspected (contact sheet + individual plates): consistent style,
  no watermarks/signatures/borders/text, no duplicate near-identical images,
  each plate clearly depicts its event (opening standoff, attack, parry &
  riposte, dodge with afterimage, clash shockwave, power-up gathering,
  turning point, finishing blow, victory).

## 8. Confirmations

* **Images are pre-generated, never created at runtime** — the runtime only
  reads files from `assets/apple_battle/`; enforced by test.
* **Final report unchanged** — same `render_for_group` output, same winner
  card art (test asserts the report uses `assets/characters/`, never a
  narration plate), same final keyboard, same board message ids.
* **Phase 1 / Phase 2A recovery intact** — persisted cursor is the single
  source of truth, ids live in `match.game_state["narration"]`, cleanup is
  best-effort and cannot break settlement; recovery tests (resume mid-narration,
  idempotent finalization, timeout backstop, worker races) all pass.
* **No push to any remote**; commits stay on `arena/01a0fd14-sib`.

## 9. Notes / limitations

* Sandbox resets during this stage discarded the git history twice (the
  workspace re-clones the repo between sessions but preserves file content).
  All work was re-verified and re-committed; the content is complete.
* The remaining gold/moon variants and identity-free plates were generated in
  follow-up batches; the manifest marks a plate `ready` only once its file
  exists on disk, and the loader ignores any non-ready entry.
* Selection intentionally never mixes a *different* fighter pair into a
  caption; a future 2v2 or new character only needs new plates plus a cast
  entry in the manifest — no code change in the message flow.
