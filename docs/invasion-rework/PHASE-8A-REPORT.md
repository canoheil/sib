# Phase 8A — static invasion imaging report

Date: 2026-10-14 UTC  
Branch: `arena/0e56f627-sib`  
Base: `8cc18ed5a9f93c631a3dcd21c49c0958e16b671d`

## Final status

**All 14 independent AI-generated source images are now present, reviewed and
committed. Static composition and the scoped regression checks are complete.**
Production message delivery and private admin preview are now wired to the byte
composition API and covered by real-composition delivery tests. The original ten
backgrounds/silhouettes are unchanged. Four dedicated layout
paintings now replace every provisional derivative. Manifest version is **2**.

The first turn reached the service limit of ten image generations, as previously
reported. Following the user's explicit continuation request, the four remaining
images were generated successfully in the next turn; no generation limit was hit
in that continuation. The earlier art-generation blocker is resolved.

- Announcement: empty atmospheric orchard clearing on the left, dark caption panel
  on the right, subtle botanical frame. An offline contrast pass ensures legibility.
- Join: painterly corrupted orchard shrine with a soft neutral central portrait
  inset; the 40%-opacity enemy reads clearly in the mist on the left.
- Victory: restored green apple grove, radiant golden light, blossoms and floating
  petals; an atmospheric painting without a border.
- Defeat: charcoal ruins, cold ash, crimson embers and a prominent shattered red
  apple; an atmospheric painting without a border.

All four raw plates and the refreshed final compositions were visually reviewed
for motifs, placement, absence of embedded text and legibility. This is local
asset/compositor validation, not a live Telegram deployment or a flag activation.

## Style reference and font inspection

Inspected the actual tracked `assets/characters/green_knight.jpg`,
`sage_king.jpg`, and `moon_lily.jpg` before generation. The first two have
1121×1403 portrait artwork with emerald foliage, sculpted gold leaf filigree,
tactile armor/fabric and warm cinematic highlights. Moon Lily is 912×1136, using
midnight sapphire, moon-silver/lavender, luminous particles and botanical details.
The shared style is highly detailed painterly/storybook fantasy with stylized
subjects, not flat icons or crude silhouettes. Existing cute proportions were
not copied into the merciless enemy faction; apple heads/stems, botanical
ornament, layered light and rich materials provide continuity instead.

The new environments use corrupted trees/fruit, thorn roots, dim amber, cold
blue, crimson, molten orange and sickly emerald. Enemy figures have distinct
readable apple-demon contours and matching subtle colored halos. Existing gold
card frames/English titles were not modified or copied into the new environment
plates. Card lettering remains naturally visible when the original card is pasted.

Inspected `.agents/skills/persian-writing/assets/fonts/Vazirmatn-Bold.ttf`:
123,036 bytes; Pillow reports family **Vazirmatn**, style **Bold**, and at 44 px
metrics ascent 46 / descent 24. The bundled font README identifies SIL OFL
licensing. Rendered Persian samples were visually checked for joined letterforms
and RTL order. This environment reports `raqm=False`, exercising the existing
`arabic_reshaper.reshape` → `bidi.algorithm.get_display` path. The existing raqm
branch is retained unchanged; no narrative or Persian copy polishing was done.

## Asset inventory

All paths below are relative to `assets/invasion_v2/`. All carry embedded sRGB ICC
profiles. Source JPEGs are quality 94, RGB, no alpha; silhouettes are transparent
RGBA PNGs. All five silhouettes have an entirely transparent final 60 rows.

| Path | Dimensions | Bytes | Format |
|---|---|---:|---|
| `backgrounds/brute.jpg` | 1280×720 | 385,932 | RGB JPEG |
| `backgrounds/commander.jpg` | 1280×720 | 392,587 | RGB JPEG |
| `backgrounds/king.jpg` | 1280×720 | 467,883 | RGB JPEG |
| `backgrounds/shadow.jpg` | 1280×720 | 433,278 | RGB JPEG |
| `backgrounds/soldier.jpg` | 1280×720 | 478,544 | RGB JPEG |
| `layouts/announcement.jpg` | 1280×720 | 296,459 | RGB JPEG |
| `layouts/defeat.jpg` | 1280×720 | 324,258 | RGB JPEG |
| `layouts/join.jpg` | 1280×720 | 330,404 | RGB JPEG |
| `layouts/victory.jpg` | 1280×720 | 442,548 | RGB JPEG |
| `silhouettes/brute.png` | 400×560 | 134,097 | RGBA PNG |
| `silhouettes/commander.png` | 400×560 | 117,284 | RGBA PNG |
| `silhouettes/king.png` | 400×560 | 113,877 | RGBA PNG |
| `silhouettes/shadow.png` | 400×560 | 146,324 | RGBA PNG |
| `silhouettes/soldier.png` | 400×560 | 105,032 | RGBA PNG |

### Offline preparation / provenance

- Five independently generated landscape sources were fitted to 1280×720 using
  Lanczos and saved as quality-94 JPEG (4:4:4 sampling).
- Five independently generated enemy figures used plain white extraction mattes.
  Offline processing removed the matte, decontaminated antialiased edge colors,
  fitted each figure within a 352×452 safe area on a 400×560 transparent canvas,
  and baked a faint type-colored halo into alpha. There is no white background or
  extraction step at runtime. Silhouettes were reviewed against a dark surface.
- Four dedicated layout paintings were generated separately, using the existing
  orchard/throne-room paintings and Green Knight's foliage/light as style
  references only. They are not crops or grades substituted for new artwork.
  Each was fitted to 1280×720 using Lanczos and saved as quality-94 JPEG (4:4:4).
- Announcement's decorative edge had bright pixels in the right third. A smooth
  offline-only darkening pass was baked into the right panel before final JPEG
  export. No runtime background drawing or extra asset is needed.
- `manifest.json` version **`2`**, generator `manual-ai`, uses the final preparation
  UTC timestamp and recomputed SHA-256 for all 14 final files. The original ten
  background/silhouette files retain exactly the same hashes as version 1.
  Source scratch images and processing intermediates are not tracked.
- WCAG relative luminance computed over **every decoded JPEG pixel** in the final
  announcement right third yields minimum white-text contrast **6.3315:1**,
  exceeding the required 4.5:1. The previous derivative's 10.69:1 measurement
  does not describe the replacement plate.

## Runtime implementation and compatibility

`templates.py` verifies every required image plus the manifest on import, reporting
all missing paths. Validated loaders return independent copies of bounded decoded
caches, keyed by path/version/file metadata; a warm cache cannot conceal a missing
file. The manifest is read for each version lookup, so version changes invalidate
both decoded assets and the existing content-addressed disk cache.

`backgrounds.py` contains only delegation to static loaders; all original gradient,
per-pixel scenery and enemy-shape drawing was deleted. Composition loads a static
plate, pastes a prepainted enemy and/or card, and draws Persian text. The only
geometric masks are the explicitly requested card round-corner/border/shadow and
critical-light UI effects; reusable masks are cached, not regenerated as artwork.

The public package exports only `compose_announcement_image`,
`compose_join_image`, `compose_result_image`, returning quality-88 JPEG bytes.
The cache also accepts bytes to avoid unnecessary JPEG decoding/re-encoding.
Atomic cache publication and 30-day pruning remain intact.

### Production and preview wiring (completed)

`services/invasion_v2/presentation.py` now calls the public byte-returning API:

- Announcement passes the stored `inv.enemy_name_fa`, not the controlling faction.
- Join reads the existing local card bytes off the event loop and passes username,
  stored damage/critical flag and `inv.enemy_hp_remaining`. The image uses the
  requested 96 px damage treatment and new critical badge, not the legacy overlay.
  HP is the persisted snapshot read for presentation; it is never recalculated or
  deducted here. Attack rows do not store a historical post-hit HP snapshot, so
  concurrent later attacks may already be reflected in the displayed value.
- Result maps terminal status to lowercase and passes region, enemy type and
  ordered reward lines. Victory lines aggregate only the existing granted reward
  rows using exactly the same labels/order/totals as the existing caption. Defeat
  passes no rewards, even if unrelated/stale reward rows exist. No rewards are
  granted by image composition or presentation.
- Captions/narrative, send-success activation and delivered-at semantics remain
  unchanged. A failed render cannot activate a pending announcement; a failed
  result send cannot mark a victory delivered. Circuit-breaker metering is intact.
- All image work remains in `asyncio.to_thread`. Worker arguments are captured
  plain values/bytes, not deferred ORM field reads.

`handlers/inv2_admin.py`'s private preview uses the same API. The existing
missing-main-card placeholder is reused through `load_card_image_bytes`; production
never enables that fallback. Preview-only HP is an explicit example (958 after a
42-point hit), and result previews use empty reward lines rather than inventing
DB rewards. Preview remains private, read-only, and usable while invasions are off.
The admin simulation function is unchanged.

Cache keys carry a `compose-v2` namespace to avoid reusing earlier adapter output
under the same asset version. They include visible input values (region/enemy
labels, username, damage, critical flag, remaining HP and reward lines), and card
**byte SHA-256**, not just its path. Exact replays hit cache; changed HP, username,
card contents, reward totals or announcement labels produce a different file.
Manifest versioning and 30-day pruning remain in effect. Asset version is still 2:
no artwork changed in this wiring step.

The older image-returning adapters remain only for backwards compatibility/tests;
production presentation and private preview no longer call them. Existing Persian
string literals in presentation, preview and composition were checked by AST
comparison against the pre-wiring files and are unchanged. The requested new
image fields/styles are selected by routing to the already-implemented new API,
not by rewriting narrative or caption copy.

Card aspect ratio is preserved inside 420×580 rather than cropped. The card is
centered horizontally, with slight upward placement for the 96 px damage footer.
Text shrink-to-fit prevents long supplied names overflowing their allotted area.

## Composition performance

Measured by `scripts/invasion_v2/benchmark_imaging.py`: **20 real compositions per
output after one warm-up**, including card decode/resize where applicable, text,
compositing, and JPEG serialization. This is not timing a final-image disk-cache
hit. Input card bytes were read before timing, as required by the new API.
Python 3.11.2; Pillow 12.3.0; Linux x86_64; `raqm=False`.

| Output | Mean ms (20 runs) | Max ms | First call ms* |
|---|---:|---:|---:|
| announcement | 10.85 | 12.28 | 35.99 |
| join-normal | 104.20 | 106.80 | 125.89 |
| join-crit | 117.02 | 130.13 | 123.34 |
| victory | 24.77 | 26.09 | 40.69 |
| defeat | 21.87 | 22.43 | 34.90 |

*First call for each case in a shared process; earlier cases can warm shared
fonts/effects. It is not five separate process-start benchmarks. All measured
renders stayed below 200 ms, comfortably under the 300 ms hard runtime target.
Timing depends on deployment hardware and card dimensions; it is not a guarantee
for arbitrarily large untrusted input images. The performance tests check 20 warm
normal and critical compositions against a 200 ms mean and 300 ms maximum.

Reproduce from the checkout root:

```bash
PYTHONPATH="$(dirname "$PWD"):$PWD" .venv/bin/python scripts/invasion_v2/benchmark_imaging.py
```

## Five composed samples (committed)

- `docs/invasion-rework/samples/phase-8a/announcement.jpg`
- `docs/invasion-rework/samples/phase-8a/join-normal.jpg`
- `docs/invasion-rework/samples/phase-8a/join-crit.jpg`
- `docs/invasion-rework/samples/phase-8a/victory.jpg`
- `docs/invasion-rework/samples/phase-8a/defeat.jpg`

Each is a 1280×720 quality-88 JPEG, regenerated with manifest v2's dedicated
layouts. The benchmark regenerates them deliberately;
normal bot execution does not write into the repository. Older Phase 5 samples
are retained as historical documentation.

## Tests and verification

- **37 new imaging cases**, covering manifest SHA-256 and exact inventory,
  dimensions/modes/sRGB, transparency/safe footer, all five announcement types,
  distinct normal/critical and victory/defeat output, HP/reward effects,
  manifest-version invalidation, missing-file rename even with a warm cache,
  import-time missing-path reporting, copy isolation, invalid names/status,
  minimum panel contrast, atomic byte cache/pruning, card aspect/placeholder,
  and normal/critical performance.
- Manifest version invalidation test now bumps relative to whatever version is
  deployed instead of hardcoding `2`, so later asset releases cannot make it a no-op.
- Updated Phase 5's palette-key assertion to use `templates.ENEMY_TYPES`; old
  image-returning signature coverage remains intact.
- **18 added delivery-integration cases** in `test_presentation_phase5.py` execute
  the actual byte composers and actual temporary disk cache, with SQLite and mocked
  Telegram only. They verify announcement enemy label, normal/critical join bytes
  and stored HP, victory reward aggregation/defeat suppression/no-reward victory,
  cache invalidation for HP/username/card bytes/reward totals/announcement labels,
  private preview with and without a main card, disabled-flag no-render behavior,
  failed result delivery, missing local card and failed announcement composition.
  Sent JPEG bytes are compared to the new compositor output. Render/cache work is
  asserted to run off the event-loop thread; reward/damage side effects are checked.
- **201 passed, 0 failed** after production/preview wiring (14.43 seconds): all
  `tests/invasion_v2` plus `tests/test_invasion_feature_flag.py`. This includes
  all 37 imaging cases and 18 added delivery cases. Earlier 183-pass results
  predated those new integration cases.
- Earlier broader optional character-art run (before layout replacement): **190 passed, 4 failed** total. The same
  four failures reproduced with the base commit's entire imaging package loaded
  in-memory instead of this implementation (**7 passed, 4 failed** for the
  character-art module alone). Existing failure reasons:
  1. `test_profile_shows_favorite_card_art`: a mocked `chat.id` is bound to SQLite.
  2. `test_all_character_cards_exist_with_same_dimensions`: old assertion expects
     896×1200, but tracked `sib_sorkh.jpg` is 1121×1403.
  3. `test_character_cards_share_one_frame_no_double_frame`: same existing frame/
     image-dimension mismatch.
  4. `test_character_cards_fill_the_opening_without_margins`: existing
     `sib_talayi` frame-lip coverage assertion fails.
  These unrelated tests/assets were not changed. The whole repository suite was
  not run; no claim of a globally green suite is made.

Tests require the checkout's two namespace roots and an isolated test DB:

```bash
PYTHONPATH="$(dirname "$PWD"):$PWD" \
  sib_TEST_DATABASE_URL="sqlite+aiosqlite:///$PWD/tmp/sibcom_sib_test.db" \
  .venv/bin/pytest tests/invasion_v2 tests/test_invasion_feature_flag.py -q
```

Local validation used a venv with repository requirements plus `aiosqlite` (tests)
and `numpy` (offline asset preparation only). Runtime dependency files are unchanged.

## Safety / flag status

- **`invasion_enabled` was not enabled or written.** The provided deployment state
  is false and this work leaves it unchanged. The tracked migration still seeds
  missing flags to `false`; feature-flag tests pass. No production DB connection
  or independent live-value query was performed, so this is preservation of the
  supplied state, not a claimed live DB audit.
- `narrative.py`, scheduler, resolver, join handler, circuit breaker and migrations
  remain byte-for-byte unchanged from the base. Only presentation's image wiring
  and the admin private-preview image wiring changed in this completion step;
  the admin simulation and Telegram exception/metering wrapper remain unchanged.
- No production or archived `_archive_inv1_*` tables were accessed. Existing
  migration tests operate solely on isolated disposable SQLite databases.
- No push, merge, bot startup, or feature activation was performed. The original four
  requested commit chunks plus the final-layout and presentation-wiring commits are on the
  session branch only.

## Remaining limitations

There is **no remaining image-generation blocker** and no provisional layout in
the runtime inventory. Final image quality and text placement were reviewed locally;
a real Telegram preview and MariaDB integration were not exercised. The unchanged
raqm path was not executable on this Pillow build. The earlier unrelated four
character-art test failures remain outside this phase; the whole repository suite
is not claimed green. Existing card lettering remains untouched. The legacy
caller-wiring limitation is resolved; the current-HP snapshot and preview-only
example inputs are explicitly documented above.

`invasion_enabled` stays untouched at the supplied false state; no live DB audit
was performed and enabling the feature is not part of this delivery.
