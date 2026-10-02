# Apple Battle — Narration Art Direction (Phase 2B)

Source of truth for the pre-generated narration scene library under
`assets/apple_battle/`. Generated with the `create-game-assets` +
`flux-image-best-practices` skills; the runtime never generates images.

## Game frame

- Player fantasy: chibi apple warriors duelling in a fairytale orchard arena.
- Core verbs: attack, counter-attack, dodge, brace, unleash, win, lose.
- Renderer: Telegram photo messages (square captions under a 1:1 image).
- Native viewport: 1024×1024 (1:1), displayed ~600px wide in Telegram.
- Typical asset size on screen: full chat width, animated by scene changes.

## Visual system

- Shape language: rounded chibi bodies (2.5 heads), chunky silhouettes,
  angular armor accents; readable at 600px.
- Silhouette priorities: every scene must show at least one clear
  apple-warrior silhouette plus the action beat (blade arc, shockwave,
  dodge streak) — readable with the caption hidden.
- Value structure: 3 bands — bright warm sky/arena light, mid-tone foliage,
  dark foreground framing; the action sits in the brightest band.
- Palette roles and swatches:
  - crimson (Sib Red) `#8E1F2B`, apple green `#6FA638`, gold `#E4B93C`,
    moon blue `#2E4B9C`, warm cream light `#F6E3B8`, deep forest `#2A3B24`.
- Materials: glossy apple skin (slight subsurface blush), brushed gold trim,
  dark steel plate, cream linen, glowing magic energy (soft additive).
- Edge treatment: clean rendered edges with painterly bloom; no outlines.
- Lighting: warm golden-hour key from the upper right, cool rim from the
  left, volumetric god rays, dust motes and drifting apple blossoms.
- Detail density: high in the fighter and the action; background softened
  with depth of field so captions stay readable.
- Motion character: weighty, cinematic, slightly over-the-top. Motion blur
  streaks on the moving limb/weapon, petals displaced by the shockwave.
- Explicit exclusions: text, letters, numbers, logos, watermarks, signatures,
  UI chrome, frames/borders, gore, blood, realistic humans, extra limbs,
  duplicated fighters beyond the two duellists.

## Character roster (identity locks)

| Cast | Character | Locked appearance |
|---|---|---|
| crimson | Red Apple (sib_sorkh) | glossy deep-red apple skin, amber eyes, green leaf on head, crimson hooded cloak with gold leaf embroidery, dark steel + gold armor, slim glowing longsword |
| green | Green Apple (sib_sabz) | glossy apple-green skin, white beard, leaf on head, layered green + cream robes, gold trim, carved wooden staff, scroll |
| gold | Golden Apple (sib_talayi) | glossy golden apple skin, bright joyful eyes, golden laurel crown, white + gold layered robes, radiant staff with a glowing apple orb |
| moon | Moon Apple (sib_mahi) | glossy pale-blue apple skin, silver crescent ornaments, dark navy gown with star embroidery, glowing moon orb |
| neutral | — | backlit silhouette with the face hidden; used for identity-agnostic shots |

## Technical contract

- Dimensions/aspect: 1024×1024 (1:1), JPEG, quality 85.
- Background: fully painted scene, no transparency.
- Filtering/compression: progressive JPEG, ≤ ~400 KB per asset.
- Naming: `assets/apple_battle/<category>_<NN>_<casts>.jpg`
  (e.g. `attack_01_crimson_green.jpg`).
- Manifest: `assets/apple_battle/manifest.json` (category, casts, file,
  provenance, prompt summary).

## Visual target

- Approved style anchor: `opening_01_crimson_green.jpg`.
- All other library assets are produced FROM that anchor as a visual
  reference so palette, world, camera language and costume locks hold.
