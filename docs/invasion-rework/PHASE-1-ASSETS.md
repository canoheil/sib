# Phase 1 — Invasion v2 asset plan

**Plan only.** Generate nothing in this phase. In Phase 5, use Pillow for compositing, crop/resize, gradients, masks, Persian text overlays and export; any painted source artwork would have to be supplied or approved separately. Do not reuse/alter Apple Duel assets or change existing invasion art. This plan uses the Sib apple-character silhouette rather than generic human soldiers.

## Ten source assets to prepare

One **background** and one **transparent silhouette** for each enemy type. Proposed file stems are inventory labels only, not new runtime paths or a migration. Backgrounds: 1024×1024 RGB source art (matching the existing square event-art convention); silhouettes: 1024×1024 RGBA PNG, transparent outside the figure, with a single focal figure so Pillow can layer it over the chosen background. Keep room for captions and avoid embedded text, UI, logos, or gore.

| Enemy type | Background (one each; 5 total) | Silhouette (one each; 5 total) |
|---|---|---|
| `soldier` — شیاسیب پیاده | `soldier_background`: broken stone gate at moonrise, a long road through Ovarya | `soldier_silhouette`: corrupted apple foot soldier, stem, leaf, chipped dark armor, grounded stance |
| `shadow` — شیاسیب سایه | `shadow_background`: cold mist, doubled reflections at a dark lake | `shadow_silhouette`: thin apple-shaped spectral form, ragged edge, recognizable stem/leaf |
| `commander` — شیاسیب سردار | `commander_background`: abandoned battlements with a distant enemy standard | `commander_silhouette`: commanding apple figure, angular cloak/crown outline, one raised arm |
| `brute` — شیاسیب سنگین | `brute_background`: crushed rampart and fractured stone under a storm | `brute_silhouette`: broad, weighty apple-shaped body, heavy shoulder silhouette, no human anatomy |
| `king` — شیاسیب پادشاه | `king_background`: eclipse above a ruined citadel, maximum visual gravity | `king_silhouette`: crowned corrupted apple ruler, distinct stem, tattered mantle, unmistakable crown |

Keep silhouettes high-contrast at Telegram thumbnail size; preserve type distinction even if viewed in grayscale. Asset filenames and placement are proposals for Phase 5, not files created here.

## Join visual template (Pillow overlay specification)

- **Canvas:** 1024×1024 px, square, with a 64 px outer safe margin. Composite the corresponding background full bleed; put the matching silhouette in the central frame, approximately `x=256..768, y=220..760`, while preserving its aspect ratio and transparent edges.
- **Legibility layer:** subtle near-black vertical gradient across the lower `y=690..1024`, with a dark top vignette. Keep the face and stem out of the darkest part; use a narrow silver rim light, not a flat black fill.
- **Top overlay:** Persian region + enemy name at `x=80..944, y=74..175`, right-aligned; a short status/scene line at `x=80..944, y=174..248`. Do not place type codes or English text in the image.
- **Lower overlay:** player's display name at `x=80..944, y=742..816`; main-card name at `x=80..944, y=826..887`; short action/damage line (when applicable) at `x=80..944, y=897..960`. Fit RTL text without colliding with artwork; wrap or reduce font size conservatively for long names.
- **Accessibility:** keep text at least 64 px from every edge, minimum 4.5:1 contrast against the darkened area; test at small Telegram preview size and provide the full Persian narrative in the message/caption rather than relying only on pixels. The eventual mechanics must decide whether “join” also deals the attack before rendering a damage line.

## Result screen

- **Canvas:** 1024×1024 px, square. Compose the same enemy/region art (no changes to original source files). Apply a subdued silver dawn for victory, cold ash haze for defeat, or neutral moonlit haze for unresolved expiry; do not assume expiry means defeat.
- **Top:** region and enemy labels within `x=80..944, y=72..185` (right-aligned). **Focal area:** enemy silhouette or a cleared horizon within `x=214..810, y=195..680`.
- **Bottom:** one short cinematic Persian result sentence at `x=80..944, y=736..860` and, if appropriate, a concise reward cue at `x=80..944, y=871..958`. Longer stories/reward details belong in the Persian caption, not a tabular image. Do not reproduce the old «نتیجه نهایی تهاجم» table treatment.
- Export target: quality-controlled progressive JPEG for full-frame art, or PNG where transparency is required. Verify byte size and thumbnail readability before deployment; no runtime asset pipeline is specified in Phase 1.

## Existing art and proposed palette

Inspected `assets/invasion/moon_descendant_invasion.jpg`, `assets/invasion/manifest.json`, `assets/apple_battle/opening_01_crimson_green.jpg`, and `assets/frames/card_frame_gold.jpg`. The invasion key art is a square, moonlit gothic gate with chibi apple characters, crescent rim light and dark mist; Apple Battle uses painterly red/green apple characters and warm-gold scenery, while the card frame has ornate gold on parchment. **These are references, not assets to modify or merge with Apple Duel.** Existing invasion art direction explicitly anchors midnight blue `#10182B`, near-black `#090B12`, moon-silver `#B9C6DA`, and cold violet `#665A9D` (see `assets/invasion/manifest.json` and `.agents/tasks/moon-descendant-invasion/ART_DIRECTION.md`).

| Use | Hex | Notes |
|---|---|---|
| Background shadow | `#090B12` | Established invasion near-black; gradient and legible caption base |
| Night atmosphere | `#10182B` | Established invasion midnight blue |
| Moon highlight / text | `#B9C6DA` | Established invasion moon-silver; test contrast |
| Arcane edge light | `#665A9D` | Established cold violet, restrained glow |
| Corruption accent (suggested) | `#8B273D` | A little dark crimson links enemy to Sib's red apples without turning scenes cheerful |
| Reward-only accent (suggested) | `#C7A65A` | Small warm-gold glint for apples/rewards; not the primary moonlit palette |

Avoid neon saturation, cute comedy, generic humanoid monsters, graphic horror, English lettering, and large parchment/gold frames that would conflict with the dark invasion family. Keep motifs of leaves, stems, old stone, silver filigree and distant moons; use richer accent only where it distinguishes enemy threat or rewards.
