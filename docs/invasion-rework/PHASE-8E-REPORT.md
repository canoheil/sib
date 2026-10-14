# Phase 8E Report — Phone-legible typography, shadows, glow, dramatic status words

> **Superseded in part by Phase 8G** (`PHASE-8G-REPORT.md`): the dark "pill"
> backdrops behind the result region name and reward lines were removed (they
> rendered as solid black boxes), and the victory word — written «پیوروزی» in
> the Phase 8E code and samples below — is now spelled «پیروزی». Everything
> else in this report still describes the current renderer.

Scope: `services/invasion_v2/imaging/compose.py` (rendering only), 11 new tests,
cache-key bump in `presentation.py` (v2 → v3 so old images are never reused),
`numpy` added to `requirements.txt` (needed by the radial vignette gradient).
No narrative/scheduler/resolver/circuit-breaker changes. `invasion_enabled` is
still `false` — nothing live changed.

## Font sizes (1280×720 canvas)

| Element | Before | After | Notes |
|---|---|---|---|
| Announcement region name | 44 | 88 | panel-centered, fits to 380px |
| Announcement enemy label | 28 | 52 | centered under the decorative line |
| Join username | 32 | 60 | fits to 350px |
| Join damage number | 96 | 160 | moved up (y 682 → 648) for breathing room |
| Join HP remaining | 28 | 44 | bottom-right, always visible |
| Result region name | 44 | 88 | own dark pill, y=70, auto-shrinks |
| Result status word | 120 | 200 | vignette + glow + heavy stroke |
| Result reward lines | 32 (shrunk to fit spacing) | 54 | fixed size, each line on its own pill |

Legacy preview adapters (the 32/44/28px fallbacks used when the caller passes
`legacy=True`) keep their original sizes — they exist for backwards-compatible
previews, not for delivery.

## Colors

| Element | Before | After |
|---|---|---|
| Status word "پیروزی" (victory) | #FCECD8, stroke #3A1A08 w4 | fill #C8A02E (deep gold), stroke #3A2408 w8, glow #E8C766 @60% blur 20, warm halo vignette (60,40,10) 900×360 @55% |
| Status word "شکست" (defeat) | #efa7a0 (pastel pink!), stroke #3A1A08 w4 | fill #7A0E0E (deep blood red), stroke #1A0202 w10, glow #8B0000 @70% blur 24, dark vignette 900×400 @75% |
| Damage number (normal) | #FCECD8 | #FCECD8 + glow blur 12 @40%, stroke #2A1408 w1 |
| Damage number (crit) | #FCECD8 | #FFD24A + glow blur 16 @60%, stroke #2A1408 w1 |
| Damage number (killing blow) | #FCECD8 | #B8E986 + glow blur 16 @60%, stroke #2A1408 w1 |
| Region name (result) | #FCECD8 | #FCECD8, stroke #2A1408 w3, on rgba(0,0,0,0.55) pill (radius 20) |
| Reward lines | #FCECD8 | #FCECD8, stroke #2A1408 w1, each on its own rgba(0,0,0,0.55) pill |
| Every other Persian text | no shadow | blurred black drop shadow (offset 3,3, blur 6, 70% opacity) |

Killing-blow detection is `enemy_hp_remaining is not None and enemy_hp_remaining <= 0`
inside `compose_join_image` — no signature change, kill takes precedence over crit.

## New helpers (compose.py)

- `_shaped(text)` — central arabic_reshaper + python-bidi (raqm when available)
  pipeline; every Persian string in the file now goes through it.
- `_fit_size(text, nominal, max_width, min_size, warn_key)` — whole-point shrink
  loop; logs a WARNING when `warn_key` is set and a shrink happens (used by the
  result region name: 88 → min 56, 90% canvas limit).
- `_draw_text_with_shadow(img, xy, text, font, fill, stroke_fill, stroke_width,
  shadow_offset=(3,3), shadow_blur=6, shadow_opacity=0.7, anchor="mm")` —
  blurred black silhouette first, stroked text on top. Used for all text.
- `_glow(image, xy, text, font, color, opacity, blur, anchor)` — dilated
  (MaxFilter + dilate) + Gaussian-blurred copy composited behind the text.
- `_draw_radial_vignette(img, center_xy, radius_x, radius_y, inner_alpha,
  outer_alpha=0.0, color=(0,0,0))` — numpy radial gradient pasted as RGBA.
- `_text_ink_box(...)` — measured ink bbox for anchor math.
- `_pill_backdrop(...)` — dark rounded-rect pill behind a text ink box.
- `_decorative_line(...)` / `_draw_apple_icon(...)` — announcement panel art
  (160×3 #6E3511 line with soft glow; red circle + green leaf icon right of the
  enemy label).

Announcement panel: region name now centered at (1070, 216) — panel-centered
horizontally and ~30% down the right-third panel — with the decorative line at
ink-bottom + 36 and the enemy label centered below it.

## Layout notes (measured, not guessed)

The 200px status word at (640, 325) spans y≈262–440. Reward pills (54px ink,
±20px padding, 86px spacing, bottom line at y=672) leave a 14px gap below the
word and stay ≥2px inside the 720px canvas edge for 1–3 reward lines.
Victory/defeat words were verified by rendering, not by metrics alone — the
first pass at y=350/spacing 88 collided the top reward pill with the "پیروزی"
descender, which is why the final constants are the measured ones.

## Samples (this phase, regenerated from the final code)

| Variant | Full | 360px |
|---|---|---|
| Announcement | `docs/invasion-rework/samples/phase-8e/announcement.jpg` | `.../announcement-360.jpg` |
| Join (normal hit) | `.../join-normal.jpg` | `.../join-normal-360.jpg` |
| Join (crit) | `.../join-crit.jpg` | `.../join-crit-360.jpg` |
| Join (killing blow) | `.../join-kill.jpg` | `.../join-kill-360.jpg` |
| Victory | `.../victory.jpg` | `.../victory-360.jpg` |
| Defeat | `.../defeat.jpg` | `.../defeat-360.jpg` |

360px check: at 360px width the status word renders ~56px tall — "شکست" reads
as a heavy dark-red slab with near-black outline (Dark Souls "YOU DIED"
register), "پیروزی" reads as gold on a warm halo. All usernames, damage
numbers, region names and reward lines stay legible.

## Tests

`tests/invasion_v2/test_imaging.py` — 11 new tests:

1. `test_announcement_region_font_size` — 88px requested for the region name
2. `test_join_damage_font_size` — 160px requested for the damage number
3. `test_result_status_font_size` — 200px requested for the status word
4. `test_region_name_font_size` — 88px requested for the result region name
5. `test_defeat_fill_is_deep_red` — word drawn with #7A0E0E + #8B0000 glow
6. `test_victory_fill_is_deep_gold` — word drawn with #C8A02E + #E8C766 glow
7. `test_text_helper_adds_shadow` — blur before colored text; pixels below the
   ink are actually darkened
8. `test_damage_glow_present_for_crit_and_kill` — blur 12 (normal), 16 (crit),
   16 (kill); kill fill #B8E986 wins over crit
9. `test_vignette_produces_alpha_gradient` — alpha fades monotonically from
   inner to outer
10. `test_region_name_backdrop_present` — pill (0,0,0,140) r20 drawn before the
    region text
11. `test_long_region_name_auto_shrinks` — long region name shrinks 88 → 56
    and logs a WARNING

Result: `tests/invasion_v2/` full suite — see command output (433 passed).

## Commit sequence (branch `arena/258bd7ec-sib`)

```
[phase-8e] tests and phase 8e report
[phase-8e] region name backdrop and reward line styling
[phase-8e] dramatic victory and defeat status words
[phase-8e] announcement panel rebalance
[phase-8e] damage number glow for crit and kill
[phase-8e] add drop shadow helper for all text
[phase-8e] increase font sizes across invasion images
```

## Limitations / honest notes

- The card silhouette shadow (behind the character portrait) and the text drop
  shadow both use GaussianBlur; on very low-end rendering boxes the 1280×720
  cost is dominated by the two 200px/160px blurs — still well inside the 200ms
  perf budget (measured in `test_compose_join_fast`).
- `_fit_size` shrinks in whole points; a 1pt step can overshoot the 90% limit
  by a fraction of a point in the worst case (visually irrelevant).
- The vignette is a per-pixel numpy gradient (400×300 region ≈ 4ms). If PIL ever
  needs to run without numpy, `_draw_radial_vignette` is the one function that
  would need a fallback.
- Status word colors are tuned for the dark templates; on a lighter template
  variant the gold could read brighter than intended (templates are fixed in
  this phase — `assets/invasion_v2/` was not touched).
