# Sib Account Levels — Art Direction & Image-Generation Canon

**Status:** Approved production canon. **All 30 final illustrations are complete** in `assets/player_levels/level_01.jpg`–`level_30.jpg` as 1200 × 1500 px sRGB JPEGs.

**Scope:** One coherent, full-body Green Apple / Sib Sabz character illustration for each Account Level 1–30.

**Technical target:** 4:5 portrait, exactly **1200 × 1500 px** per final image.

**Authority:** This document is the visual source of truth for the 30-image set. The XP economy and Level meanings remain governed by [`FINAL_DESIGN.md`](FINAL_DESIGN.md); this brief does not change them.

---

## 1. Existing Art Audit & Decisions

### References inspected

- `assets/characters/sib_sabz.jpg` — current Green Apple card illustration: apple-green sage, stem and leaf, white eyebrows/moustache/beard, green hood and cream/green robes, gold trim, staff and scroll. The delivered card also includes an ornate frame, crest, and “GREEN APPLE” nameplate; those are card UI, not part of the character.
- `assets/apple_battle/opening_01_crimson_green.jpg` and `.agents/tasks/apple-battle-cinematic-narration/ART_DIRECTION.md` — the clearest current production lock for Green Apple in a shared scene: rounded ~2.5-head chibi scale, green elder with white beard, leaf, layered sage/cream robe, carved staff, soft dimensional painterly finish, warm orchard/castle light. This is the **rendering and chibi-style anchor** for the set.
- `assets/characters_raw/sib_sabz.jpg` — a simpler, softly painted apple-shaped hooded figure and useful reference for the friendly, round mascot silhouette. It differs from the current card/battle lock by omitting the beard and changing the pose/detail level.
- `assets/frames/card_frame_gold.jpg` and `scripts/frame_card.py` — establish the card’s separate decorative frame treatment; the frame must not be baked into Level art.
- `assets/banners/` and `services/banner.py` — establish Sib’s warm orchard world, honey-gold / cream / sage / apple-red palette, text-free illustration convention, and gouache-watercolor banner language. The banners are environmental references, not a reason to change the character rendering or portrait camera.
- `assets/preview/style_a_warm_storybook.jpg`, `style_b_painterly_sunset.jpg`, `card_apple_knight.jpg`, and `card_apple_wizard.jpg` — exploratory/prototype visuals; useful context for the broader storybook/fantasy range, but not a consistent Level-series identity lock. They do not supersede the current cast specification and approved Apple Battle anchor.
- `assets/apple_battle/manifest.json` — identifies the released cast and existing orchard-world visual vocabulary. The sibling characters’ crimson, gold, and moon palettes are not to be imported into Sib’s costume.
- `data/characters_seed.json` — describes Green Apple as a patient strategist and sage; the visual should feel thoughtful and capable, not royal or villainous.
- The current profile has a text progress bar and emoji badge, not a dedicated Level illustration. Level art must therefore remain clean standalone artwork, not a mock profile screen.

### Ambiguity resolved

The raw green concept omits facial hair, while the current framed card, Apple Battle cast description, and shipped battle artwork consistently define Green Apple as a **white-bearded elder sage**. The current production lock wins: the white brows, moustache, and beard remain visible at **every** Level, including Level 1. Use the raw image only as a secondary cue for the friendly, round apple silhouette and soft storybook warmth. Do not average the two designs by making the beard appear/disappear between Levels.

Use the character illustration inside `assets/characters/sib_sabz.jpg` for identity cues and `opening_01_crimson_green.jpg` for rendering/proportion cues. Ignore the card’s frame, crest, title, and layout. The XP badges in code are UI cues, not permission to put numbers, crowns, trophies, or interface elements into the artwork.

---

## 2. Style Bible — Permanent Rules for All 30 Images

### Format and composition

- Deliver **30 separate, full-bleed 1200 × 1500 px (4:5) portrait images**, sRGB, high-quality JPEG. No transparency is needed; use a painted background.
- Show Sib **head-to-toe**, including the head leaf/stem, both feet, complete staff top, and the full shield whenever carried. Do not crop equipment at the canvas edge.
- Keep the same near-frontal, gentle three-quarter view in every image: body turned slightly toward image-left, face still readable to the viewer, camera at character eye height or just above. Keep the horizon, ground contact, and camera distance stable.
- Center the character around the same vertical axis. The complete figure should occupy about **78–84% of canvas height**, with clear breathing room above the leaf/staff and at both sides for increasingly refined equipment. Feet sit on one consistent lower ground line. Keep the body scale fixed; Levels do not make Sib taller or larger.
- Keep the staff in Sib’s right hand (normally on image-left) and, from Level 5 onward, the buckler on his left arm (normally image-right). Never mirror the design between images; adjust pose only enough to keep both objects and the face legible.
- Maintain the recognizable **~2.5-head-tall** chibi proportion. The apple head is large and rounded; torso, arms, legs, hands, and feet remain short and sturdy. The head-to-body ratio must not drift with armor.

### Character identity and silhouette

- One character only: **Sib Sabz / Green Apple**, a small, sturdy, green apple-headed sage. His apple-shaped head, short brown stem, single green leaf, white facial hair, hood/cowl, and carved staff are the silhouette anchors.
- Keep the leaf visible above or beside the hood. Keep the face and beard open and unobstructed; use open-faced hoods and partial armor, never a full helmet, mask, visor, or face-covering scarf.
- Keep skin apple-green and gently glossy, with soft blush. Preserve the same rounded apple crown/cheeks, warm amber-brown eyes, small warm nose, expressive white brows, white moustache, and compact full beard. Facial proportions, beard shape/volume, perceived age, and eye placement are fixed. Only the expression changes subtly from uncertain to calm confidence.
- The hooded sage robe/cowl remains part of the design even when armor is added. Armor is layered over it; it never replaces the robe with an unrelated costume. The **carved wooden staff** remains Sib’s signature weapon through Level 30. A small round buckler may be added as a secondary tool at Level 5 and refined thereafter; never replace the staff with swords, bows, or a different weapon identity.
- Character identity is not Level. Do not make Sib younger, older, more human, more animal, or a different species at higher Levels. No transformation into a tree, dragon, giant, spirit, king, or celestial being.

### Rendering and materials

- Follow the polished, softly dimensional **painterly chibi-fantasy** rendering of `assets/apple_battle/opening_01_crimson_green.jpg`: clean readable edges, rounded forms, gentle surface highlights, and brush-like detail in cloth, wood, beard, and metal. Keep a warm storybook softness; do not switch to flat vector art, hard cel-shading, pixel art, photorealism, or a different 3D render style.
- Use the established palette by role: apple green `#6FA638`, deep forest `#2A3B24`, warm cream `#F6E3B8`, and muted gold `#E4B93C`. Brown wood/leather and restrained dark steel are supporting neutrals. Green remains the dominant character color at every Level; cream remains a major cloth counterpoint. Gold use grows through craftsmanship and trim, not large blocks of unrelated color.
- Distinguish materials consistently: softly glossy green skin; matte woven cloth; worn leather at low Levels, better-finished leather/metal in mid-levels, refined enamel/jade and brushed gold at high Levels; carved wood for the staff. Keep metals warm and hand-crafted, not chrome or sci-fi.
- Keep detail concentrated on Sib’s face, staff, shield, and the main armor seams. At thumbnail size, the apple face, beard, leaf, and staff must still read. Do not fill later images with micro-engraving, noise, or extra ornaments merely to signal rarity.

### Light, background, and effects

- Use a consistent fairytale orchard setting: softly blurred green canopy, a few apple leaves/blossoms, warm stone path or low stone footing, and a distant soft orchard/castle silhouette. The background supports Sib and never competes with the character. Do not change to a night sky, battlefield, throne room, cave, or different biome as the Level rises.
- Keep a warm golden-hour key from upper-right, gentle cool-green rim from image-left, and soft contact shadow under the feet. Use comparable exposure and contrast in every file; no dramatic color grading jumps.
- Effects are **earned and restrained**. Levels 1–9 have no visible magic. A few leaves/motes first appear near Level 10; Levels 16–24 may add a small localized glow in the staff’s stone or carvings; Levels 25–29 can have a few controlled leaf motes. Level 30 may add one soft leaf wreath and warm shaft of light. No persistent giant aura, explosions, lightning, floating runes, wings, transformation, or effect that hides the face/equipment.
- Any engraved magic mark is decorative leaf/root geometry, not readable letters, a number, or a faux interface symbol.

### Text-free asset contract

Do **not** bake in level numbers, words, labels, UI bars, badges, card frames, borders, ribbons, logos, watermarks, signatures, or mockups. No additional characters. The artwork is the illustration only; profile text and Level UI are rendered by Sib’s application.

---

## 3. Character Specification — Green Chibi Sib

**Identity:** Sib Sabz, the calm strategist and elder sage of the orchard. This is the same established Green Apple character at every Level; XP shows experience and kit quality, not a lore transformation.

**Never-drift features**

1. Round apple-green fruit head with a slight apple crown/shoulder shape; short brown stem and **one** bright green leaf, always visible.
2. Large warm amber-brown eyes; soft blush and small warm nose; thick white brows; white moustache connected to a compact, full white beard.
3. Gentle wise face. Low Levels may show uncertainty or concentration; later Levels may show calm determination or a small reassuring smile. No permanent scowl, rage, or smugness.
4. Small, sturdy chibi body, fixed ~2.5-head height; short green arms/legs and simple rounded hands/feet. The robe can hide much of the torso, but pose and limb proportions remain consistent.
5. Green hood/cowl and layered sage-green / cream robe throughout the set. The hood frames the face without hiding the leaf or beard.
6. One carved wooden staff with the same recognizable shaft/hand grip. Its workmanship, collars, carvings, and small stone improve by stage; its basic silhouette remains recognizable. From Level 5, a modest round buckler is a supporting secondary item, not a new primary weapon.

**Allowed to evolve:** garment condition and tailoring; leather protection; armor coverage and craftsmanship; shield construction/crest; staff carving and fittings; small green stones; restrained trim; belt tools, scroll case, cape length; stance confidence; localized magical motes.

**Not allowed to evolve:** skin hue, eye color/placement, apple-head silhouette, stem/leaf count, beard presence/color/shape, body scale, age, costume family, signature staff, camera angle, light direction, or visual medium.

---

## 4. Level 1–30 Progression Design

**Continuity rule:** each Level adds or refines one practical visual step from the previous image. Existing signature pieces remain visible; worn items are repaired/upgraded rather than replaced without explanation. Milestones at 5, 10, 15, 20, 25, and 30 may combine two related improvements. The upgrades communicate preparedness, craftsmanship, protection, and controlled skill—not a sudden costume swap or a pile of glow effects.

| Level | Theme | Clothing, weapon, and accessories | Pose / effects and visible step up from the previous Level |
|---:|---|---|---|
| **1** | Roadside novice | Threadbare sage hood and faded cream tunic with two obvious but tidy patches; rope belt, worn sandals, crooked branch staff bound with cloth, folded map. | Slightly hunched, tentative but kind gaze; no effects. **Baseline:** visibly poor, inexperienced, and under-equipped while still unmistakably the same bearded sage. |
| **2** | First trail | Same robe, patches neatly re-stitched; leather wrist wraps, small canvas satchel and water flask; staff grip re-wrapped. | Stands a little straighter and looks alert; no effects. **Upgrade:** first practical supplies and hand protection. |
| **3** | Orchard path scout | Add soft leather ankle boots and a short green shoulder scarf over the hooded robe; simple leather staff ferrule; map now tucked into the satchel. | Leans forward to study the path; no effects. **Upgrade:** safer travel kit and protected staff tip. |
| **4** | Apprentice | Add a quilted cream vest under the same robe, one plain forearm bracer, stitched belt pouch; straighten and smooth the wooden staff, carving one small leaf at the grip. | Holds the staff in a more balanced two-handed guard; no effects. **Upgrade:** torso padding and a dependable weapon. |
| **5** | First grove guard | Keep Levels 1–4 kit; add a small round plank buckler with a simple painted leaf, rawhide rim, and padded knee wraps. | Cautious but stable guard pose; no effects. **Upgrade:** first shield and basic lower-body protection. |
| **6** | Reliable traveler | Replace the rope belt with a plain leather belt; sturdier boots, matching wrist bracers, simple bronze hood clasp; add a small bronze cap to the staff’s lower end. | Balanced standing stance; no effects. **Upgrade:** the rough kit becomes a secure, wearable field set. |
| **7** | Orchard pathfinder | Add a short leather chest harness beneath the robe, one modest shoulder guard, and a slim scroll tube/compass at the belt; staff head gains a small natural fork. | Uses the free hand to indicate a route; no effects. **Upgrade:** scouting tools and first shoulder protection. |
| **8** | Warden’s apprentice | Add overlapping leather scales over the cream vest; reinforce the buckler strap and give it a narrow bronze rim; add a leather grip wrap to the staff. | Raises shield with a focused, composed expression; no effects. **Upgrade:** better torso defense and a shield that can withstand use. |
| **9** | Orchard sentinel | Add bronze knee/shin caps and a slightly longer sage mantle with cream lining; strengthen the buckler’s rim and keep the leaf mark. | Firm, upright stance with feet planted; no effects. **Upgrade:** complete basic limb protection and a more coherent silhouette. |
| **10** | Field Warden | Starter kit becomes a tailored sage brigandine over the same cream robe; matching bracers and knee guards; buckler receives a clean green leaf crest; staff gets bronze collars and a tiny, unlit green glass bead. | Calm ready stance; at most two faint leaves near the staff, no aura. **Upgrade:** first complete, serviceable adventurer set; all earlier signature items remain. |
| **11** | Professional warden | Keep the hood, robe, staff, and buckler; replace rough leather panels with fitted dark-green padded armor and neatly sewn gold-thread cuffs; add a matched pair of small bronze shoulder plates. | Relaxed professional posture; no effects. **Upgrade:** tailoring and matched protection replace apprentice-grade construction. |
| **12** | Veteran tactician | Add articulated leather-and-bronze lamellar across the torso, plus a secured scroll case and small compass medallion; polish the staff shaft. | Thoughtful, measured pose, staff held as a pointer; no effects. **Upgrade:** stronger torso coverage and purposeful field tools. |
| **13** | Root-grip adept | Add engraved bronze rings and a more precise carved fork to the same staff; reinforce the gloves/bracers and stitch a restrained leaf line into the robe hem. | Practiced staff guard; one drifting leaf at most, no glow. **Upgrade:** weapon handling and workmanship become visibly more advanced. |
| **14** | Road guardian | Extend protection to articulated knee/shin plates and add a short, weighted shoulder cape with cream lining; strengthen the buckler boss and inner strap. | Shield-forward stance, still open-faced and calm; no effects. **Upgrade:** full practical limb coverage and a more stable shield. |
| **15** | Grove captain | Milestone: fitted forest-green cuirass with a small antique-gold leaf insignia at the collar (not a crown); clean cream under-robe; leaf-matched buckler and a finely carved bough-shaped staff head. | Quietly confident, shoulders level; only a couple of faint motes. **Upgrade:** coordinated captain-quality kit and a clear leaf crest. |
| **16** | Runewood sage | Add a single small jade inset to the staff and shallow root/leaf grooves along its head; armor gains refined edging but keeps the same silhouette. | One hand rests on the staff while the other opens slightly; a narrow green glimmer stays inside the staff carving. **Upgrade:** first controlled enchantment, localized to the signature tool. |
| **17** | Elite grove guard | Upgrade to light articulated plates over shoulders, forearms, chest, and knees, always layered over visible robe; give the buckler a stronger double seam. | Balanced defensive stance; no aura. **Upgrade:** protection becomes more complete without hiding the chibi body or face. |
| **18** | High Warden | Tailor the hood seam with finer gold stitching; lengthen the mantle slightly and line it in cream; refine the staff top around its small green stone. | Staff held upright with two or three soft leaf motes; no broad glow. **Upgrade:** better cloth, fit, and a restrained high-quality staff fitting. |
| **19** | Canopy strategist | Add leaf-shaped detail to the vambraces, precise gold edging on existing plates, and a compact map tube with compass at the belt. | One hand makes a deliberate strategic gesture; a short, faint leaf arc follows the staff. **Upgrade:** refined functional detail and more assured control, not more bulk. |
| **20** | Master of the Orchard Gate | Milestone: complete professional armor-robe set in sage, forest green, and cream; polished but modest gold trim; matching buckler; carved staff with a fine jade point; scroll secured in an engraved tube. | Planted staff and serene, protective stance; a barely visible curved green shimmer behind the buckler only. **Upgrade:** full elite set and the first clear defensive magic cue. |
| **21** | Rootforged warden | Replace the ordinary plate finish with masterwork deep-green enamel/bronze and shallow root relief; staff becomes darker hardwood with a fitted small green gemstone. | Strong upright posture; no aura. **Upgrade:** endgame craftsmanship and material quality, not increased size. |
| **22** | Canopy sentinel | Refine the buckler with a precise double rim and engraved orchard-leaf crest; add a more substantial, finely woven cape edge. | Holds the shield at a practiced angle; gem catches the established warm light but emits no beam. **Upgrade:** shield and cloak reach masterwork finish. |
| **23** | Jade-thread adept | Add slim jade channels to existing staff grooves and cream armor panels; keep all insets narrow and green. | Controlled staff flourish; a few tiny green motes close to the tip. **Upgrade:** first visible active magic, limited to the staff and its immediate space. |
| **24** | Keeper of the grove relic | Staff head becomes a carefully carved twin-leaf fork around the same small stone; the buckler’s center echoes this motif; add fine leaf engraving to existing armor. | Serene, deliberate pose; soft stone glint only. **Upgrade:** a coherent matched heirloom motif across the existing kit. |
| **25** | Elder of the Deep Grove | Milestone: add a distinctive half-cape with cream lining and elegant leaf embroidery; raise armor finish to polished sage enamel and antique-gold seams; add one small leaf clasp, not royal regalia. | Calm strategist’s stance; a small handful of leaves orbit near shoulder/staff, not around the whole body. **Upgrade:** unmistakably rare, coordinated armor and cloak while preserving the base silhouette. |
| **26** | Rootway protector | Refine the masterwork plates at upper arm, hip, and knee into mobile, interlocking leaf-root forms; retain visible robe, beard, and hood; keep the scroll tube practical. | Staff grounded, watchful expression; no additional aura. **Upgrade:** more complete, articulated protection without becoming bulky. |
| **27** | Emerald strategist | Add precise gold-and-green inlay to staff grip and armor edges; refine the half-cape clasp with a small carved bough. | Open hand guides a short curl of three leaves near the staff; restrained localized glow. **Upgrade:** fine control and hand-finished detail, not a larger spell. |
| **28** | Legendary warden | Complete the matching deep-green/cream armor-robe; broad but still rounded leaf-form pauldrons; shield and staff share one restrained root-and-leaf engraving. Hood stays open around leaf and face. | Ceremonial poise without royal posture; a few tiny staff motes. **Upgrade:** unified relic-quality construction across the entire kit. |
| **29** | Guardian of the old orchard | Add the final precision details: finely engraved bracers, a refined hood clasp, immaculate staff carvings, and a subtly brighter green stone. No new weapon or bulky plate. | Steady, compassionate confidence; effects remain minimal so the capstone can read clearly. **Upgrade:** near-perfect finish and control, holding back the unique Level 30 focal detail. |
| **30** | Orchard Paragon — MAX | Ultimate normal form: the same sage robe/hood and cream layers beneath complete masterwork green armor; exquisitely restrained antique-gold leaf/root work; small leaf circlet integrated into the hood, never covering the stem; master-carved staff with one luminous green stone; matching buckler with an orchard-leaf crest. | Upright, generous, calm pose with staff grounded and free hand open; one soft wreath of leaves and warm orchard light around the staff/shoulders. **Upgrade:** complete, prestigious, perfectly coordinated regalia and controlled mastery—not a transformation, giant aura, or different character. |

---

## 5. Progression Review — Read the Whole Set as One Story

- **Levels 1–4:** survival and practical basics. The clothes stay visibly patched/worn while hands, feet, and staff become usable. The face and robe establish the character before armor exists.
- **Levels 5–10:** shield, leather protection, and bronze fittings arrive in small steps. Level 10 is a complete beginner adventurer kit, not an early legendary costume.
- **Levels 11–20:** rough components become tailored, articulated, and matched. The staff receives its first localized enchantment at 16; the shield gets a faint defensive cue only at 20. The character reads as a professional sage-warden.
- **Levels 21–29:** power is shown mainly through finer construction—enamel, jade, carved details, matched armor, and controlled craft. Effects do not scale up every Level. Level 29 remains complete but leaves one visual capstone: the integrated leaf circlet and soft leaf wreath belong to Level 30 only.
- **Level 30:** the most prestigious normal form, but still the same small green elder in hood, robe, beard, leaf, staff, and buckler. It is a culmination of the exact kit built across the preceding Levels, not a new form or a Legendary Rank transformation.

A contact sheet should read left-to-right as **patched novice → equipped learner → professional warden → masterwork sage → orchard paragon**. Reject any individual image that is attractive alone but breaks this sequence, changes Sib’s face/body, or makes a neighboring Level look weaker.

---

## 6. Batch Production Plan

**One image per Level, never a generated grid or collage.** The contact sheet is assembled separately for review. Use the same model/checkpoint, aspect ratio, prompt foundation, and original reference pair in all three batches. A previous generated Level may support continuity but must never become the only identity reference; otherwise small errors compound across batches.

### Batch 1 — Levels 1–10: Survival → Apprentice Field Warden

- Produce the under-equipped Level 1 first as the identity/scale proof; review its green apple face, beard, hood, leaf, staff, full-body framing, and background against the locks above before continuing.
- Build one practical improvement at a time through Level 9; reserve the first faint motes for Level 10. Level 5 introduces only the simple buckler milestone.
- Review Levels 1, 5, and 10 together before approving the batch. Lock the approved Level 1 as a continuity reference, not as a replacement for the original art references.

### Batch 2 — Levels 11–20: Professional Gear → Master of the Orchard Gate

- Keep the original identity references plus the approved Level 1 and Level 10 references available. Do not re-sample Sib’s face or body from a new costume image.
- Progress through tailored armor and practical accessories. Add localized staff magic at 16 and a faint shield cue at 20; do not jump directly to endgame glow.
- Review Levels 10–11 for a smooth tailoring transition and Levels 15/20 as milestone reads. Lock the approved Level 20 for the next batch.

### Batch 3 — Levels 21–30: Masterwork → MAX Orchard Paragon

- Use original identity/style anchors plus approved Levels 1, 10, and 20. Add rarity through workmanship, matched design, jade/green enamel, and restrained gold—not a different palette or body form.
- Keep Levels 21–29 controlled and mostly effect-free or localized; reserve the small leaf circlet and full soft leaf wreath for Level 30.
- Review Levels 20–21, 29–30, and the complete 1–30 contact sheet. Fix any abrupt gear reset, scale drift, color drift, or overpowered visual-effects jump before handing off the art set.

---

## 7. Image-Generation Rules for the Next Agent

### Fixed prompt foundation

Include this invariant in every per-Level prompt, then append only that Level’s row-specific gear/pose/effect block:

> Create a full-body portrait illustration of the **same established Sib Sabz / Green Apple** from the supplied Sib references: a small, sturdy ~2.5-head-tall apple-green elder sage with the same rounded apple head, short brown stem, one visible green leaf, warm amber-brown eyes, white eyebrows, white moustache and compact full white beard. Keep his face, age, body proportions, head/leaf silhouette, hooded sage robe, and carved wooden staff consistent with the references. Hold the staff in his right hand on image-left and (when specified) the buckler on his left arm on image-right; do not mirror the design. Use the approved softly dimensional painterly chibi-fantasy rendering, warm orchard palette, and established golden-hour light. Preserve the fixed three-quarter composition and show the complete character and all equipment. Change only the explicitly specified Level equipment, its condition/craftsmanship, pose confidence, and restrained effect.

### Reference use and batch consistency

- Primary identity reference: the character art in `assets/characters/sib_sabz.jpg` (ignore its frame, badge, and text). Primary style/proportion reference: `assets/apple_battle/opening_01_crimson_green.jpg` plus `.agents/tasks/apple-battle-cinematic-narration/ART_DIRECTION.md`.
- Secondary silhouette/warmth reference only: `assets/characters_raw/sib_sabz.jpg`. Do not use it to remove the official beard or change the fixed standing/full-body presentation.
- Include both original references in every Level generation where the tool permits. Add the previous tier’s milestone image only as a continuity aid. Keep the same model/checkpoint, 4:5 framing, style block, and relative character scale across batches.
- Generate Levels as separate assets; do not ask the model for multi-panel sheets, multiple Levels in one image, or a framed-card mockup. Review a separately assembled contact sheet at thumbnail size after each batch.

### Negative constraints and acceptance gate

Reject or regenerate an image if it contains any of the following:

- Missing/changed beard; a covered face; different eyes/skin/age; extra or missing leaf/stem; changed head/body ratio; taller/slimmer body; human-looking green-skinned adult rather than apple-headed chibi.
- New species/forms, wings, horns, tail, crown/royal robe, full-face helmet, sword/bow, extra staff, extra shield, extra limbs/fingers, duplicate character, or an unrelated costume theme.
- Large red/blue/purple costume blocks; neon green; chrome/sci-fi material; over-bright spell glow; effects that hide face, leaf, beard, staff, or armor construction.
- Different camera, crop, character scale, light direction, background world, color grade, or render style from adjacent Levels.
- Text, numbers, readable runes, labels, badges, UI, borders, frames, logos, watermark, signature, or baked profile elements.
- Cropped feet, leaf, staff, shield, or cape; a character too small to read at thumbnail size; an image that looks like an unrelated design when placed between its neighboring Levels.

Before delivery, verify each file is exactly 1200×1500, 4:5, sRGB JPEG; inspect at full size and at profile-thumbnail scale; confirm no crop, text, watermark, frame, anatomy error, or drift; and compare all 30 in one contact sheet. Keep the final artwork set separate from these design notes and record generation provenance according to the project asset workflow.

---

## 8. Proposed Asset Naming

Use one file per level, numerically padded so the profile/runtime can sort the set deterministically:

```text
assets/player_levels/level_01.jpg
assets/player_levels/level_02.jpg
...
assets/player_levels/level_30.jpg
```

This is a production naming recommendation only. This design phase creates no files in `assets/player_levels/` and makes no runtime or profile-code changes.
