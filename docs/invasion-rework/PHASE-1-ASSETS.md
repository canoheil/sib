# Invasion v2 asset inventory — Phase 8A supersedes the Phase 1 plan

The historical Phase 1 square-canvas proposal and Phase 5 procedural art are
superseded. Runtime now loads committed static artwork from
`assets/invasion_v2/`; it does not generate backgrounds or enemy shapes.

**Phase 8A inventory complete:** all 14 images now have independently generated
AI source artwork. Four dedicated layouts replaced the provisional derivatives
in the continuation turn after the initial generation cap. Manifest version **2**
invalidates old cached compositions. See [PHASE-8A-REPORT.md](PHASE-8A-REPORT.md)
for provenance, exact sizes, current timings and validation limitations.
This does not enable invasions or change the feature flag.

## Current runtime inventory (14 images)

All paths below are relative to `assets/invasion_v2/`. All files carry an sRGB
ICC profile. JPEG source plates are quality 94, RGB, with no alpha. PNG enemy
sprites are RGBA with baked colored halos and a fully transparent bottom 60 px.

| Path | Dimensions | Content / provenance |
|---|---|---|
| `backgrounds/soldier.jpg` | 1280×720 | AI-painted corrupted dusk orchard, amber/brown, quiet dark right third |
| `backgrounds/shadow.jpg` | 1280×720 | AI-painted moonlit blue/purple glade, mist and spores |
| `backgrounds/commander.jpg` | 1280×720 | AI-painted ruined crimson/ember war camp, torn banners and smoke |
| `backgrounds/brute.jpg` | 1280×720 | AI-painted black cavern and molten orange veins |
| `backgrounds/king.jpg` | 1280×720 | AI-painted green/gold corrupted throne room |
| `silhouettes/soldier.png` | 400×560 | AI-painted upright apple demon, claws, amber halo |
| `silhouettes/shadow.png` | 400×560 | AI-painted hunched wraith, blue eyes and halo |
| `silhouettes/commander.png` | 400×560 | AI-painted horned armored commander, red halo |
| `silhouettes/brute.png` | 400×560 | AI-painted broad spiked brute, orange halo |
| `silhouettes/king.png` | 400×560 | AI-painted skeletal crowned apple king, green halo |
| `layouts/announcement.jpg` | 1280×720 | Dedicated AI orchard clearing, dark right panel and subtle frame |
| `layouts/join.jpg` | 1280×720 | Dedicated AI orchard shrine, soft dark central portrait inset |
| `layouts/victory.jpg` | 1280×720 | Dedicated AI restored grove, golden light, green foliage and floating petals |
| `layouts/defeat.jpg` | 1280×720 | Dedicated AI somber ruins, falling ash and shattered crimson apple |

`manifest.json` records version, UTC timestamp, generator, dimensions and SHA-256
of every final image's actual file bytes. Bump its version and recalculate hashes
when replacing artwork. No generation tool or preparation script runs in the bot.

## Composition contract

- 1280×720 output JPEGs, quality 88.
- Announcement: static announcement plate, natural-size sprite at (80, 80),
  right-aligned region/enemy text. The plate's right third has a measured minimum
  **6.33:1** contrast against pure white (requirement: 4.5:1).
- Join: static join plate, 40%-opacity enemy, centered local card contained within
  420×580 without aspect distortion/cropping; rounded 16 px corners, 4 px dark
  border, shadow. Username, damage, optional critical treatment and remaining HP.
- Result: static victory/defeat plate, centered 1.4× enemy at 35% opacity; region,
  status, victory-only reward lines. No nonterminal-expiry image is introduced.
- Existing player cards at `assets/characters/{code}.jpg` are not altered.
  Their baked English lettering remains visible inside the card; invasion source
  plates and sprites themselves contain no text.
- Persian uses the existing Vazirmatn Bold and unchanged raqm-or-
  arabic-reshaper/python-bidi shaping decision. Long input can shrink to fit.
- Production presentation and private admin preview use the byte-returning
  `compose_*` API: enemy name on announcement, stored remaining HP and the new
  critical treatment on join, aggregated granted-reward lines on victory only.
  Existing captions and narrative strings stay unchanged. Preview retains its
  explicit private-only placeholder; production never substitutes missing art.
- Legacy image-returning adapters remain for compatibility/tests only. Render
  cache keys include a `compose-v2` namespace, visible text/HP/reward inputs and
  the card's byte hash, in addition to manifest versioning.
- Decoded image copies and UI masks are bounded in-memory caches. Content-addressed
  disk cache remains `~/.cache/inv2`, includes the manifest version and retains its
  30-day pruning policy.

## References and samples

Visual references for Phase 8A were `green_knight.jpg`, `sage_king.jpg` and
`moon_lily.jpg`: rich botanical detail, gold ornament, painterly materials and
cinematic jewel-toned light. Shiyasib shifts these toward rotten fruit, thorns,
charcoal, amber, cold blue, crimson and sickly green; no cute human faces.

Current five samples are under `docs/invasion-rework/samples/phase-8a/`:
`announcement.jpg`, `join-normal.jpg`, `join-crit.jpg`, `victory.jpg`, `defeat.jpg`.
The older Phase 5 samples remain historical examples, not current output.
