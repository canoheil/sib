# Apple Duel — Presentation Art Direction

Source of truth for the pre-generated duel scene library under
`assets/apple_duel/`. Mirrors the Apple Battle narration conventions
(`.agents/tasks/apple-battle-cinematic-narration/ART_DIRECTION.md`); the
runtime never generates images.

## Game frame

- Player fantasy: chibi apple warriors duelling in a fairytale orchard arena.
- Renderer: Telegram photo messages (Persian caption under a 20:13 image).
- Native viewport: 1000×650 (20:13), displayed full chat width in Telegram.
- The duel pair is dynamic (any two Main Cards), so every plate is
  **cast-free**: an environmental arena stage. The two duelists appear as
  tiny distant backlit silhouettes (or not at all), while the players' real
  framed card art is composited left/right at runtime by
  `services/apple_duel_presentation.compose_frame`.

## Visual system (shared with Apple Battle)

- Shape language: rounded chibi bodies (2.5 heads), chunky silhouettes,
  angular armor accents; readable at mobile width.
- Value structure: 3 bands — bright warm sky/arena light, mid-tone foliage,
  dark foreground framing; the action sits in the brightest band.
- Palette: crimson `#8E1F2B`, apple green `#6FA638`, gold `#E4B93C`,
  moon blue `#2E4B9C`, warm cream `#F6E3B8`, deep forest `#2A3B24`.
- Materials: glossy apple skin, brushed gold trim, dark steel plate,
  cream linen, glowing magic energy (soft additive).
- Lighting: warm golden-hour key from the upper right, cool rim from the
  left, volumetric god rays, dust motes and drifting apple blossoms.
- World landmarks: circular stone duel platform, blossom apple trees,
  distant fairytale castle with waterfall — same world as the card art.
- Motion character: weighty, cinematic, slightly over-the-top.
- Explicit exclusions: text, letters, numbers, logos, watermarks,
  signatures, UI chrome, frames/borders, gore, blood, realistic humans,
  foreground characters (cards are composited at runtime), panel seams or
  inset squares from outpainting.

## Scene set (one plate per narrative scene)

| Index | Category | Plate | Narrative beat (Persian caption) |
|---|---|---|---|
| 0 | entry | `scene_0_entry_castfree.jpg` | ورود دو مبارز؛ سکوت سنگین |
| 1 | sizeup | `scene_1_sizeup_castfree.jpg` | سنجش حریف؛ تنش آرام |
| 2 | clash | `scene_2_clash_castfree.jpg` | برخورد نخست؛ موج ضربه |
| 3 | turning | `scene_3_turning_castfree.jpg` | تغییر جریان؛ نور تازه |
| 4 | climax | `scene_4_climax_castfree.jpg` | لحظه‌ی تعیین‌کننده |
| 5 | aftermath | `scene_5_aftermath_castfree.jpg` | پایان و پاداش؛ جوانه‌ی نهال |

## Technical contract

- Dimensions/aspect: 1000×650 (20:13), progressive JPEG quality 85.
- ≤ ~400 KB per asset (actual: 76–132 KB).
- Background: fully painted scene, no transparency; left/right thirds kept
  soft-focus and character-free for card compositing.
- Naming: `assets/apple_duel/scene_<index>_<category>_castfree.jpg`.
- Manifest: `assets/apple_duel/manifest.json` (index, category, file,
  status, cast_free, prompt summary); missing/unreadable plates fall back
  to the procedural composition, never to a wrong-identity image.
