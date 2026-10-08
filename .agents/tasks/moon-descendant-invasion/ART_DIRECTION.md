# Moon Descendants — V1 Art Direction

## Purpose

Build one cohesive, mature dark-fantasy art family for the Moon Descendants: a beautiful but intimidating lunar civilization whose ancient territory is being entered by outsiders. The set consists of seven faction/location illustrations and one square V1 Invasion announcement key art.

## Character identity and anchor

- `assets/invasion/moon_descendant_invasion.jpg` is the approved visual anchor for the Invasion art family. It establishes how Sib's apple-character style is treated in this frightening atmosphere.
- Sib's core identity is preserved: Moon Descendant characters are chibi apple characters, with apple-shaped bodies, stems, leaves, and faces integrated into the apple skin. They are not humans wearing apple costumes.
- Invasion's atmosphere is more frightening than the rest of Sib. It is achieved through dark, cold palettes, high-contrast moonlit lighting, gothic environments, and menacing expressions. This is an atmospheric treatment of the existing apple-character style, not a replacement for it.
- `assets/characters_raw/sib_mahi.jpg` was used as an additional repository-resident identity cue during generation. Its original Sib art, along with all other existing Sib artwork, remains untouched.
- Moon Apple cues are expressed through blue apple skin, crescent/star adornments, moonstone seals, and celestial fabric patterns.

## Visual system

- **Mood:** regal, ancient, supernatural, mysterious, beautiful and intimidating; quiet authority rather than loud horror.
- **Palette:** midnight blue `#10182B`, near-black `#090B12`, moon-silver `#B9C6DA`, and restrained cold violet `#665A9D`.
- **Materials:** obsidian stone, dark silver, moonstone, crystalline inlays, embroidered midnight silk, and reflective water.
- **Lighting:** cold moonlight, controlled silver rim-light, deep but readable shadows, restrained violet crystal glow, and subtle atmospheric haze.
- **Motifs:** crescent moons, celestial filigree, gothic/ancient arches, lunar observatories, and old territorial markers.

## Composition and content rules

- Make characters poised, mature in bearing, and individual while keeping their apple-character construction; keep the Queen the visual focus in court and invasion scenes.
- Give the Knight, Oracle, Huntress, and Reaper distinct readable silhouettes and roles.
- Show the Moon Descendants as a disciplined civilization, not a group of generic monsters.
- Keep the Invasion key art focused on the Queen and her court, the breached boundary, the ancient temple, and the awakening lunar power. Anonymous intruders remain scale cues, not detailed new characters.
- Use fully painted, square, full-bleed artwork with clear focal points at small Telegram preview size.
- Keep every image free of text, readable runes, card frames, borders, logos, watermarks, signatures, and UI.
- Avoid childish or comic presentation (cheerful expressions, playful poses, silly exaggeration), gore, graphic horror, and warm gold accents. Chibi apple proportions remain the baseline for Sib characters and are not forbidden.

## Asset and runtime conventions

- Store the eight pre-generated JPEGs under `assets/invasion/` using the exact IDs as filename stems.
- Normalize the library to the established event-art convention: 1:1, 1024×1024, progressive JPEG, quality 85.
- `assets/invasion/manifest.json` is descriptive provenance only; it is not a new runtime asset framework.
- `moon_descendant_invasion.jpg` is the default Telegram announcement artwork. It is presentation-only; failed/missing photo delivery must continue to degrade to the existing text path without affecting Invasion state, transaction boundaries, retries, or message-ID persistence.
