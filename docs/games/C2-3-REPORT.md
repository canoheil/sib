# C2-3-REPORT — Apple Clash: Narrative & Pillow Composition

**Feature:** جدال سیبی (Apple Clash) — Phase C2-3 Implementation  
**Date:** 2026-10-18  
**Branch:** `arena/94815b63-sib`

---

## 1. Overview

Phase C2-3 implements the battle narrative engine and dynamic Pillow visual compositor for Apple Clash (جدال سیبی). When a warrior executes an attack (either standard or powered by Sib-Buff), the system:
1. Dynamically generates a high-definition battle visual (`1280×720` JPEG) matching the Invasion v2 design language.
2. Selects vivid Persian combat narratives tailored to both attacker and defender perspectives.
3. Simultaneously dispatches the visual and customized perspective captions to both competing group chats without pinning.

---

## 2. Narrative Pools (`services/war/narrative.py`)

Two distinct narrative pools were authored with rich Persian martial vocabulary, matching the epic mythological tone of the Sib universe:

### Attacker Perspective (`WAR_ATTACK_ATTACKER`) — 6 Variants
- **Focus:** Valor, conquest, triumph, inflicting damage on enemy defenses.
- **Tokens Supported:** `{player}`, `{card}`, `{target_group}`, `{damage}`, `{buff_indicator}`.
- **Sib-Buff Tag:** `✨ با قدرت اعجازانگیز سیب باف` (empty string for standard attacks).
- **Count:** 6 variants (surpassing the minimum requirement of 5).

### Defender Perspective (`WAR_ATTACK_DEFENDER`) — 6 Variants
- **Focus:** Incoming assault alert, castle breach impact, rallying the defense.
- **Tokens Supported:** `{player}`, `{card}`, `{source_group}`, `{damage}`, `{buff_indicator}`.
- **Sib-Buff Tag:** `💥 تقویت‌شده با نیروی مهیب سیب باف!` (empty string for standard attacks).
- **Count:** 6 variants (surpassing the minimum requirement of 5).

### Helper Functions
- `render_attacker_narrative(player, card, target_group, damage, is_buff)`
- `render_defender_narrative(player, card, source_group, damage, is_buff)`
- Numbers formatted with Persian numerals via `fa_digits()`.

---

## 3. Pillow Battle Composition (`services/war/imaging.py`)

Function: `compose_war_attack_image(war_id, user_id, card_image_bytes, damage, is_buff=False, perspective="attacker") -> bytes`

### Composition Pipeline:
1. **Background Loading:**
   - Attacker perspective: loads `assets/apple_clash/war_bg_attacker.jpg`.
   - Defender perspective: loads `assets/apple_clash/war_bg_defender.jpg`.
   - Fallback: dark slate battle canvas (`#0F172A`) if asset file is missing.
2. **Card Framing & Presentation:**
   - Card dimensions: `390×530` px centered horizontally (`x=445, y=55`).
   - Rounded corners with smooth anti-aliased alpha mask (`radius=22px`).
   - Soft drop shadow (`offset=(0, 16)`, `radius=28`, `#000000D0`).
   - Dual border: Outer golden/fire border (`#D4AF37` / `#FF7043`, 4px) and inner dark border (`#0D1117`, 2px).
3. **Typography & Glow:**
   - Font loading: Prioritizes Persian/Arabic fonts (`Vazirmatn-Bold.ttf`, `DejaVuSans-Bold.ttf`, `FreeSansBold.ttf`).
   - Persian damage label and formatted digits (`fa_digits(damage) + " خسارت"`).
   - Bottom damage banner bar with gradient pill background.
   - Text drop shadow & radial outer glow:
     - Standard attack: Cyan/Gold glow (`#FACC15`).
     - Buff attack: Fiery orange glow (`#FF7043`).
4. **Fiery Sib-Buff Overlay:**
   - When `is_buff=True`, renders dynamic fiery orange/red particles, radial sparks, and an ambient warm gradient over the lower section of the card and damage bar.
5. **Output Format:**
   - Returns optimized JPEG bytes (`quality=92`, `format="JPEG"`).

---

## 4. Handler Integration (`handlers/war.py`)

- **Main Card Resolution:** `get_player_card_bytes(session, user_id)` fetches the player's active deck card artwork, falling back to a clean dark card canvas if unassigned.
- **Dispatch Delivery:** `_send_dispatch(bot, chat_id, photo_bytes, caption)` delivers photo dispatches to both the attacking castle group and defending castle group, falling back to text if photo delivery fails.
- **Loud Alerts:** Dispatches are delivered normally without chat pinning, featuring loud emojis (`🚨`, `⚔️`, `💥`).

---

## 5. Verification & Tests (`tests/test_war.py`)

The test suite covers:
- `test_war_narrative_pools_have_variants`: Validates pool counts (>= 5), Persian string formatting, and buff indicator toggles.
- `test_compose_war_attack_image_returns_jpeg`: Verifies output is valid JPEG bytes with exact `1280×720` dimensions.
- `test_attack_sends_two_different_narratives`: Verifies battle dispatches are sent to both attacker (`-1001`) and defender (`-1002`) chat IDs with different perspective texts.
- `test_war_attack_image_perspective`: Verifies correct perspective background loading (`war_bg_attacker.jpg` for attacker, `war_bg_defender.jpg` for defender).

**Results:**
```text
tests/test_war.py ............. [100%]
13 passed in 4.59s
```

---

## 6. Commit

- Commit message: `[c2-3] implement war narratives and pillow composition`
