# Moon Descendants — V1 Art Direction

## Purpose

Build one cohesive, mature dark-fantasy art family for the Moon Descendants: a beautiful but intimidating lunar civilization whose ancient territory is being entered by outsiders. The set consists of seven faction/location illustrations and one square V1 Invasion announcement key art.

## Character identity and anchor

- `assets/invasion/moon_descendant_queen.jpg` is the selected visual anchor for the new faction family.
- The user's Moon Apple / Queen image supplies the character direction: pale moon-blue complexion, deep blue celestial clothing, crescent details, and an elegant lunar presence. The reference is inspiration, not a template to copy.
- `assets/characters_raw/sib_mahi.jpg` was used as an additional repository-resident identity cue during generation. Its original Sib art, along with all other existing Sib artwork, remains untouched.
- Moon Apple cues are expressed subtly through color, crescent/star adornments, moonstone seals, and celestial fabric patterns. The new set deliberately uses mature adult proportions and a more imposing visual language than the cute Sib art.

## Visual system

- **Mood:** regal, ancient, supernatural, mysterious, beautiful and intimidating; quiet authority rather than loud horror.
- **Palette:** midnight blue `#10182B`, near-black `#090B12`, moon-silver `#B9C6DA`, and restrained cold violet `#665A9D`.
- **Materials:** obsidian stone, dark silver, moonstone, crystalline inlays, embroidered midnight silk, and reflective water.
- **Lighting:** cold moonlight, controlled silver rim-light, deep but readable shadows, restrained violet crystal glow, and subtle atmospheric haze.
- **Motifs:** crescent moons, celestial filigree, gothic/ancient arches, lunar observatories, and old territorial markers.

## Composition and content rules

- Make characters clearly adult, poised, and individual; keep the Queen the visual focus in court and invasion scenes.
- Give the Knight, Oracle, Huntress, and Reaper distinct readable silhouettes and roles.
- Show the Moon Descendants as a disciplined civilization, not a group of generic monsters.
- Keep the Invasion key art focused on the Queen and her court, the breached boundary, the ancient temple, and the awakening lunar power. Anonymous intruders remain scale cues, not detailed new characters.
- Use fully painted, square, full-bleed artwork with clear focal points at small Telegram preview size.
- Keep every image free of text, readable runes, card frames, borders, logos, watermarks, signatures, and UI.
- Avoid childish/chibi proportions, comic styling, gore, graphic horror, and warm gold accents.

## Asset and runtime conventions

- Store the eight pre-generated JPEGs under `assets/invasion/` using the exact IDs as filename stems.
- Normalize the library to the established event-art convention: 1:1, 1024×1024, progressive JPEG, quality 85.
- `assets/invasion/manifest.json` is descriptive provenance only; it is not a new runtime asset framework.
- `moon_descendant_invasion.jpg` is the default Telegram announcement artwork. It is presentation-only; failed/missing photo delivery must continue to degrade to the existing text path without affecting Invasion state, transaction boundaries, retries, or message-ID persistence.
