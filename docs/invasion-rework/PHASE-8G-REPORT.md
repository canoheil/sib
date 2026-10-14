# Phase 8G — the victory word was misspelled and the result text sat on black plates

Status: fixed. Rendering only (`services/invasion_v2/imaging/compose.py`), plus
two cache-key prefix bumps so already-rendered images are not served again. No
asset, migration, config key, scheduler, resolver or narrative change, and
`invasion_enabled` was not read, written or seeded by this phase.

## Reported symptoms

1. The victory image printed **«پیوروزی»** instead of **«پیروزی»**.
2. The region name and the reward lines on the result image sat on **solid
   black rectangles** that disfigured the painted background.

## Cause

**Spelling.** `_STATUS_WORDS["victory"]` in `compose.py` literally held the
string `"پیوروزی"` — seven characters, with an extra `و` (U+0648) after the
first `ی`. The shaping pipeline (`arabic_reshaper` + `python-bidi`, or libraqm
where available) was never at fault; it faithfully rendered a misspelled
source string. The same misspelling was hard-coded in
`tests/invasion_v2/test_imaging.py::test_victory_fill_is_deep_gold`, so the
test suite locked the typo in place.

**Black plates.** `_pill_backdrop()` drew the backdrop with
`ImageDraw.Draw(image).rounded_rectangle(..., fill=(0, 0, 0, 140))`. On an
RGBA canvas Pillow's draw operations *replace* pixel values instead of
alpha-compositing them, so the intended "55% dark pill" became a region of
`(0, 0, 0, alpha=140)` pixels; `_jpeg()` then flattened the canvas to RGB and
discarded the alpha channel, leaving a 100% opaque black box. The pill was a
bug, not a design decision — it never looked the way Phase 8E documented.

## Fix

| File | Change |
|---|---|
| `imaging/compose.py` | `"پیوروزی"` → `"پیروزی"` (status-word tuple otherwise untouched: fill `#C8A02E`, stroke `#3A2408` w8, glow `#E8C766`, warm vignette, center (640, 325), 200px) |
| `imaging/compose.py` | `_pill_backdrop()` and `_PILL_FILL` removed; both call sites in `_result()` deleted |
| `imaging/compose.py` | Legibility without a plate: region name stroke w3 → w4, reward lines stroke w1 → w2, both with `_RESULT_SHADOW` = offset (0, 4), blur 10, opacity 0.85 (the Phase 8E default is offset (3, 3), blur 6, opacity 0.7) |
| `imaging/compose.py` | Legacy-adapter typo «اسیب» → «آسیب» (join fallback text, not used by delivery) |
| `presentation.py` | Result/join/announcement cache prefix `compose-v3` → `compose-v4` |
| `handlers/inv2_admin.py` | Admin preview cache prefix `compose-v2` → `compose-v3` |

The admin preview keys are fully deterministic (`enemy` + `region`), so without
the prefix bump `~/.cache/inv2` would have kept serving the old misspelled,
black-boxed JPEGs after deployment. Production result keys contain the
invasion id, so only re-sends of an already-rendered invasion were affected —
bumped anyway for safety.

Unchanged on purpose: the `_shaped()` / `templates.label()` shaping pipeline,
every font size (88 / 200 / 54) and position (region y=70, status word
(640, 325), reward lines bottom-anchored at y=672, spacing 86), the status-word
glow and vignettes, the announcement layout (its dark right-hand panel is part
of `assets/invasion_v2/layouts/announcement.jpg`, not drawn text backdrop), the
join image, all assets and the manifest version.

## Tests

`tests/invasion_v2/test_imaging.py` — one test replaced, three added:

- `test_region_name_backdrop_present` → **`test_result_text_has_no_dark_backdrop`**:
  asserts no `rounded_rectangle` is drawn at all while the region name and a
  reward line still reach the canvas.
- **`test_result_art_stays_visible_around_region_name`** (victory + defeat):
  samples the band the old 20px-padded pill covered, clear of the ink and its
  shadow, and requires real artwork there. Against the Phase 8E sample those
  same probe points sum to 0–16 (black plate); after the fix they sum to
  218–734 and are not a flat fill.
- **`test_victory_word_is_spelled_correctly`**: the shaped «پیروزی» is drawn and
  `_STATUS_WORDS["victory"][0] == "پیروزی"`.
- `test_victory_fill_is_deep_gold` now measures the correctly spelled word.

Run: `tests/invasion_v2/test_imaging.py` — 51 passed (48 before the change;
the three new cases are the delta).

## Samples (regenerated from the fixed code)

| Variant | Full | 360px |
|---|---|---|
| Victory (3 reward lines) | `docs/invasion-rework/samples/phase-8g/victory.jpg` | `.../victory-360.jpg` |
| Defeat | `docs/invasion-rework/samples/phase-8g/defeat.jpg` | `.../defeat-360.jpg` |

The Phase 8E samples are kept as the "before" reference; the pill rows in
`PHASE-8E-REPORT.md` describe behaviour this phase removed.
