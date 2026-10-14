# Phase 8A — static invasion imaging report

Date: 2026-10-14 UTC  
Branch: `arena/0e56f627-sib`  
Base: `8cc18ed5a9f93c631a3dcd21c49c0958e16b671d`

## Status / release limitation

**Static renderer implemented and tested; art acceptance partially blocked.**
All 14 requested file paths exist and are committed, but there are **10 independent
AI-generated source paintings plus 4 offline-derived layout plates**, not 14
independent generations. The image service rejected all four layout requests with
“Image generation limit of 10 reached for this turn.” No further generation was
attempted after that response. These layouts are provisional integration assets,
not final art approval. In particular:

- Victory is a gold/green-graded corrupted orchard, still too ominous; it does not
  fully deliver the requested lush restored foliage and floating petals.
- Defeat is a somber red/black ruined camp; the prominent shattered-apple motif
  and falling ash requested for a dedicated painting are not fully realized.
- Announcement and join have the required legibility/inset treatment, but are
  derived from the soldier/king paintings rather than separately generated art.

**Follow-up:** generate the four dedicated layouts in a subsequent turn, visually
review them, update manifest hashes/version, rerun tests/benchmarks and refresh
samples. Do not enable invasions or claim full visual completion on this basis.

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
| `layouts/announcement.jpg` | 1280×720 | 430,591 | RGB JPEG |
| `layouts/defeat.jpg` | 1280×720 | 368,210 | RGB JPEG |
| `layouts/join.jpg` | 1280×720 | 359,519 | RGB JPEG |
| `layouts/victory.jpg` | 1280×720 | 547,877 | RGB JPEG |
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
- Announcement derives from soldier: a soft right-side darkening and subtle
  static frame. Join derives from king: dark softly vignetted portrait inset.
  Victory derives from soldier with a brighter green/gold split grade; defeat
  derives from commander with a dark red/black grade. These changes happen offline
  and are baked into the committed JPEGs, not redrawn by the bot.
- `manifest.json` version `1`, generator `manual-ai`, uses the actual UTC
  preparation timestamp and SHA-256 calculated from each final file's bytes.
  Source scratch images and processing intermediates are not tracked.
- WCAG relative luminance computed over **every decoded JPEG pixel** in the
  announcement right third yields a minimum white-text contrast of **10.6904:1**.

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

The older `announcement_image`, `join_image`, `result_image` adapters are retained
inside `compose.py` for existing presentation/admin callers. They return PIL RGB
images and retain existing Persian literals, including the admin-only missing-card
placeholder. Production callers were not rewired. Consequently the older join
adapter has no HP argument and the old result adapter has no reward-line argument:
those overlays are available through the new API, while existing callers retain
HP/reward behavior in their existing captions. Announcement's existing faction
label and title likewise remain unchanged in the old adapter. This avoids changing
user-facing strings or gameplay handlers in an imaging-only phase.

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
| announcement | 11.31 | 12.29 | 38.00 |
| join-normal | 106.87 | 118.78 | 139.40 |
| join-crit | 125.01 | 161.52 | 121.80 |
| victory | 25.56 | 26.16 | 40.25 |
| defeat | 21.21 | 21.53 | 33.54 |

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

Each is a 1280×720 quality-88 JPEG. The benchmark regenerates them deliberately;
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
- Updated Phase 5's palette-key assertion to use `templates.ENEMY_TYPES`; old
  image-returning signature coverage remains intact.
- **183 passed, 0 failed**: all `tests/invasion_v2` plus
  `tests/test_invasion_feature_flag.py`.
- Broader optional character-art run: **190 passed, 4 failed** total. The same
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
- `narrative.py`, scheduler, resolver, join handler, circuit breaker, migrations,
  presentation and admin handlers are byte-for-byte unchanged from the base.
- No production or archived `_archive_inv1_*` tables were accessed. Existing
  migration tests operate solely on isolated disposable SQLite databases.
- No push, merge, bot startup, or feature activation was performed. The four
  requested commit chunks are on the session branch only.

## Remaining limitations

The four dedicated AI layout generations and final visual sign-off are the main
blocker. No request to regenerate unsatisfactory art could be completed after
the per-turn generation cap; the provisional result plates must be replaced,
not described as final high-quality fulfillment. Real Telegram preview and
MariaDB integration were not exercised. The unchanged raqm path was not executable
on this Pillow build. Existing card lettering and legacy caller data limitations
are described above; they are not altered silently in this phase.
