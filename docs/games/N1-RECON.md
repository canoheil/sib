# N1-RECON — Narrative Text Overhaul

- **Phase:** N1 reconnaissance and copy design only
- **Date:** 2026-10-22
- **Branch:** `arena/a7e61107-sib`
- **Baseline:** `ffde68db206076a103869e30c6650c544f8e7334`

**N1 change scope:** this report only. No application code, tests, seed data, configuration, assets, or gameplay behavior are changed.

## 1. Executive findings

- The main readability problem is real and concentrated in **Invasion v2**: 104 templates in `narrative_pool.py` plus 36 random-event narratives in `event_catalog.py`. Many one-action captions contain three cinematic sentences before the separate damage, HP, or reward line.
- **War** has 12 attack dispatches with repeated epic openings and formal verbs. **Apple Battle** and **Apple Duel** add more staged, atmospheric narration.
- **Halloween Boss Attack is not currently long.** Its only attack result is the single alert in `handlers/halloween.py::handle_halloween_attack`. The example below rewrites that real string; it does not invent a missing narrative pool.
- **Mine Shift narratives are already short** (54–63 characters for the longest entry in each reward pool). Their remaining issues are tone, missing emphasis, and repetition between the narrative and the appended reward block.
- Garden writing is split between the handler and `CONFIG_DEFAULTS`; the latter contains the longest rarity-celebration captions.
- Social interactions, onboarding, and wiki lore also contain user-facing story or action copy. They are inventoried below so “all narrative” does not silently mean only files named `narrative.py`.

### Selection and inclusion method

The inventory covers runtime story, atmosphere, combat dispatch, result celebration, social action, onboarding-world, and lore text. Plain buttons, errors, numeric labels, admin text, help mechanics, asset-generation metadata, and historical documents are not mislabeled as narrative. The five “longest” examples were selected by comparing the unformatted template lengths in the relevant current pool or section.

The writing review follows `.agents/skills/persian-writing/SKILL.md`, especially `references/writing-style.md`, `references/orthography.md`, and `references/social-channels.md`.

## 2. Copy contract for later phases

- **One event, one or two short sentences.** Lead with the player, result, or required action. Remove scene-setting that does not change what the player understands or does next.
- **Use modern written colloquial Persian.** Prefer direct gamer language such as «ضربه زد»، «حمله کرد»، «گیرت اومد» and «بردی». Remove ceremonial or archaic phrasing such as «در رکاب»، «دلاور ما»، «باروی مستحکم» and «در هم کوبید».
- **Bold the information players scan for.** Player names, card names, damage, target/group names when important, and rewards use `**...**`. Do not bold whole paragraphs.
- **Preserve runtime meaning.** Critical, buff, finisher, remaining HP, cooldown, reward amount, participant count, and win/loss semantics must not be invented, dropped, or moved to a different event accidentally.
- **Preserve placeholder spelling.** Existing names such as `{user}`, `{card}`, `{damage}`, `{player}`, `{outcome.damage}`, and `{buff_label}` stay exactly as they are. A later phase may deliberately remove a redundant placeholder from a template, but must not rename it or silently invent unavailable data.
- **Avoid robotic variation.** Variants should change the action or sentence shape, not merely swap epic adjectives. Repeated openings, repeated damage suffixes, and identical “name + card + verb” chains need explicit review.
- **Follow Persian mechanics.** Use ZWNJ, Persian `ی` and `ک`, Persian digits at render time, Persian punctuation, and گیومه where needed. Do not use em/en dashes or three ASCII dots in new Persian prose.
- **Keep one owner for each fact.** Invasion currently appends a separate damage line after `render_join`, and Mine appends a separate reward line after its narrative. Later implementation must choose where each fact lives so the new short caption does not say it twice.

### Telegram formatting risk to resolve during implementation

The bot default is legacy `ParseMode.MARKDOWN`, while this brief explicitly requires `**` delimiters. Several surfaces also pass raw names or existing Markdown mention links. Before rollout, each changed surface needs a real rendering test for bold text, mention links, and names containing Markdown characters. Do not blindly flip every message to another parse mode or escape the bold delimiters themselves. Any formatting adapter must remain presentation-only and must preserve the defender-privacy rule in War.

## 3. Full runtime narrative inventory

The 26 files below are the complete set of runtime content files found for the narrative categories defined above. Function names are the actual current owners or assemblers of the player-facing copy.

### 3.1 Invasion v2

| File | Narrative pools, constants, and functions to revise |
|---|---|
| `services/invasion_v2/narrative_pool.py` | `ANNOUNCEMENT` (12), `JOIN_NORMAL` (22), `JOIN_CRIT` (16), `JOIN_BUFF` (6), `JOIN_KILL` (10), `VICTORY` (18), `DEFEAT` (12), `EXPIRED` (8). These feed `narrative.py::render_announcement`, `render_join`, `render_high_damage`, `render_finisher`, `render_victory`, `render_defeat`, and `render_expired`. |
| `services/invasion_v2/event_catalog.py` | `EVENTS[*].narratives`: 12 events with three variants each (36 total), including boss buffs, player buffs, instant damage, and loot. Also revise the player-facing event wording in `render_effect_line` and `render_loot_status` where it duplicates or sounds overly formal. |
| `services/invasion_v2/presentation.py` | `WITHER_NOTIFICATION_TEXT`, `WITHER_GROUP_HEADER`, `WITHER_GROUP_FOOTER`; narrative/status assembly in `build_announcement_caption`, `build_join_caption`, `build_event_caption`, `InvasionPresentation.announce`, and `InvasionPresentation.finalize`. This is the deduplication point for narrative plus HP, damage, and rewards. |

`services/invasion_v2/narrative.py` was inspected. Its public `render_*` functions select and format pools but contain no independent story templates. It only needs a later edit if placeholder escaping or fact ownership cannot be solved in the content/presentation files.

### 3.2 War and Castle

| File | Narrative functions and copy to revise |
|---|---|
| `services/war/narrative.py` | `WAR_ATTACK_ATTACKER` (6), `WAR_ATTACK_DEFENDER` (6), `render_attacker_narrative`, `render_defender_narrative`, and the `buff_label` fragment. Preserve clickable attacker mentions and plain, sanitized defender-side attacker names. |
| `services/war_presentation.py` | `victory_caption`, `defeat_caption`, `draw_caption`, and `_shield_hours_text`. These are the final result narratives and loot/shield messages. |
| `handlers/castle.py` | The war-declaration branch of `castle_callback` (`castle:war_confirm`): «طبل جنگ»، «دلاوران دو قبیله» and the loot/instruction block. The rest of this large function is mostly UI and is not blanket narrative scope. |
| `handlers/war.py` | Attack acknowledgement strings in `war_attack_callback`, including damage, remaining energy, and the buff variant. Keep these distinct from the two public dispatches to avoid a third differently worded version of the same hit. |

### 3.3 Halloween, Mine, Garden, and reward celebrations

| File | Narrative functions and copy to revise |
|---|---|
| `services/halloween.py` | `LOOT_DROP_VARIANTS` (3), `START_ANNOUNCEMENT_TEMPLATE`, `END_ANNOUNCEMENT`, `DAY7_WEAKENING_ANNOUNCEMENT`, `DEFEAT_ANNOUNCEMENT`; delivery is in `HalloweenService.publish_maintenance_notifications`. |
| `handlers/halloween.py` | `handle_halloween_attack` (the sole boss-attack result), `format_boss_menu_caption` (weakening and defeat states), and reward wording in `handle_halloween_loot`, `handle_halloween_claim_milestone`, and `handle_halloween_group_drop_claim`. |
| `services/apple_mine_narrative.py` | `NORMAL`, `GOLDEN`, `DIAMOND`, `OBSIDIAN` (6 shift variants each), `COOLDOWN` (6), `pick_narrative`, and `cooldown_message`. |
| `handlers/apple_mine.py` | Atmospheric and cooldown copy in `_panel_caption`; narrative/reward/level/next-shift assembly in `_result_caption`. |
| `handlers/garden.py` | Withering and restoration copy in `_render_garden`, `_tree_view`, and `handle_garden_fertilize`; harvest result wording in `_harvest_answer` and `handle_garden_harvest_all`. |
| `services/config_store.py` | `CONFIG_DEFAULTS["garden_drop_entries"]`: the normal/gold/diamond/obsidian `message` and `caption` values. The diamond and obsidian captions are the clearest non-combat examples of overly literary reward prose. |
| `services/daily_chest.py` | `format_reward_line`, `format_card_line`, and `format_open_result`, including jackpot and level-bonus presentation. |
| `handlers/daily_chest.py` | `CHEST_AVAILABLE_TEXT`, `GROUP_CHEST_TEXT`, and `_group_reward_notice`. These are short already; revise only for consistency and bold rewards, not for artificial expansion. |

`services/garden.py` was inspected separately. It contains game rules and short validation errors, not a hidden narrative pool; its text does not need a narrative rewrite.

### 3.4 Apple Battle and Apple Duel

| File | Narrative functions and copy to revise |
|---|---|
| `services/apple_battle.py` | `AppleBattleService._compose_plan`: three intro, nine round, and three ending variants, plus intro/finale assembly. `AppleBattleService.narrate`: the older duplicate opening/middle/ending renderer and score line. Result summaries in `build_match_state` and `extract_persisted_state` should use the same shorter result language. |
| `services/apple_duel_presentation.py` | `scene_text`: all six scenes (entry, size-up, first clash, turning point, climax, result/reward). Remove the meta-sentence explaining that a scene is “روایت رویارویی”; players need the event, not implementation commentary. |
| `handlers/apple_duel.py` | Invitation story/instruction text in `command_duel` and the “روایت در راه است” acceptance response in `callback_duel`. |

`services/apple_battle_narration.py`, `services/apple_battle_scenes.py`, and `games/apple_battle/engine.py` were inspected. They handle delivery, artwork selection, and precomputed-state rendering respectively; they do not own independent Persian narrative copy.

### 3.5 Social interactions

| File | Narrative functions and copy to revise |
|---|---|
| `services/sib_hug.py` | `HUG_MESSAGE_VARIANTS` (12) and `pick_hug_message`. Keep `{a}`, `{b}`, and `{n}` unchanged and bold the people/reward without breaking mention links. |
| `services/sib_kiss.py` | `KISS_MESSAGE_VARIANTS` (12) and `pick_kiss_message`, with the same placeholder and mention constraints. |
| `handlers/marriage.py` | `_proposal_text`, `_accept_card`, `_reject_card`, `_divorce_card`, the confirmation text in `handle_marriage_divorce_request`, `_flower_offer_card`, `_flower_accept_card`, `_flower_reject_card`, and the public/DM result text in the corresponding accept, reject, divorce-confirm, and flower callbacks. |
| `handlers/friendship.py` | `_card_text`, the unfriend result in `handle_unfriend`, and acceptance/rejection result text in `_resolve_friend_callback`. These currently use HTML, so N6 needs a deliberate presentation migration before applying the required `**` convention; raw delimiters must not be pasted into an HTML message. |

### 3.6 Onboarding, world text, and static lore

| File | Narrative content to revise |
|---|---|
| `handlers/start.py` | `_private_start_text` and the group-welcome branch of `handle_start`, including the Ovarya/Shiasib introduction and the “مدافعان اواریا” welcome. |
| `services/help_content.py` | The narrative opening of `_QUICK_START.body_fa` and the world story in `_WORLD.body_fa`. The remaining instructional sections are not part of the narrative rewrite. |
| `data/characters_seed.json` | All 18 `lore_fa` entries, including Amber. Preserve canonical relationships, factions, secrets, and character identity while reducing each entry to the new length and tone contract. |
| `data/factions_seed.json` | All three `lore_fa` entries. Preserve faction identity and story facts. |

`services/characters.py::render_character_card` and the Wiki handlers are consumers of the seed lore, not additional lore sources. Asset manifests and `docs/invasion-rework/PHASE-1-NARRATIVE.md` are likewise not runtime copy. `docs/card-lore.md` should only be synchronized later if the canonical meaning of a character or faction changes; shortening alone should not change that meaning.

## 4. Five before/after examples

“Current Text” is verbatim from the inspected baseline. “Proposed Text” keeps the existing placeholder tokens and applies the new tone, length, and emphasis rules.

### 4.1 Invasion Join

- **Section:** Invasion Join, longest join template (`JOIN_NORMAL[6]`, rendered by `render_join`)
- **Source:** `services/invasion_v2/narrative_pool.py:37`
- **Current Text:**

```text
هنگامی که سایهٔ دشمن بر دهانهٔ کوچه افتاد، {user} راه باریک را با {card} پوشاند. برخورد، غبار را تا بام‌ها برد و {damage} از نیروی مهاجم گرفت. با {hp_remaining} توان باقی‌مانده، شیاسیب ناچار شد مسیرش را دورتر از خانه‌ها بپیماید.
```

- **Proposed Text:**

```text
⚔️ **{user}** با **{card}** ضربه زد! **{damage} آسیب**؛ جان باقی‌مانده: **{hp_remaining}**.
```

### 4.2 Invasion Victory

- **Section:** Invasion Victory, longest victory template (`VICTORY[6]`, rendered by `render_victory`)
- **Source:** `services/invasion_v2/narrative_pool.py:103`
- **Current Text:**

```text
بر سنگ‌های میدان هنوز غبار نشسته بود، اما پنجره‌ها یکی‌یکی گشوده می‌شدند. {region} از یورش شیاسیب جان به در برده بود؛ مدافعان سپرها را پایین آوردند تا کودکان راه خانه را ببینند. پیروزی، همین گذر آرام از کنار بارو بود.
```

- **Proposed Text:**

```text
🏆 **{region}** رو نگه داشتیم؛ شیاسیب عقب زد!
```

### 4.3 War Attack

- **Section:** War Attack, longest template across attacker and defender pools (`WAR_ATTACK_DEFENDER[4]`, rendered by `render_defender_narrative`)
- **Source:** `services/war/narrative.py:26`
- **Current Text:**

```text
🏹 تیرهای آتشین بر فراز باروها! {player} از قبایل {source_group} با همراهی کارت «{card}» مواضع ما را کوبید و {damage} آسیب به ثبت رساند! پرچم قلعه همچنان افراشته است!{buff_label}
```

- **Proposed Text:**

```text
🏹 **{player}** از **{source_group}** با **{card}** حمله کرد؛ **{damage} آسیب** خوردیم!{buff_label}
```

### 4.4 Halloween Boss Attack

- **Section:** Halloween Boss Attack, the only current attack-result template
- **Source:** `handlers/halloween.py:319` in `handle_halloween_attack`
- **Current Text:**

```text
⚔️ شما {outcome.damage} آسیب وارد کردید! (کریتیکال: {crit_fa})
```

- **Proposed Text:**

```text
⚔️ **{outcome.damage} آسیب** به **سیبومورتیس** زدی! کریتیکال: **{crit_fa}**.
```

### 4.5 Mine Shift

- **Section:** Mine Shift, longest completed-shift template (`NORMAL[1]`; cooldown templates are a different event)
- **Source:** `services/apple_mine_narrative.py:8`
- **Current Text:**

```text
چکش به رگه‌ای روشن خورد و سیب‌های معمولی از میان خاک پیدا شدند.
```

- **Proposed Text:**

```text
⛏ کلنگ زدی و **سیب معمولی** گیرت اومد!
```

The five proposed samples were checked with the Persian cleanup tool and `fa_lint.py --check`; the proposal-only lint result was zero issues.

## 5. Replacement strategy

### Phase N2 — Invasion core and random events

- Replace all 104 core templates and 36 event narratives in `narrative_pool.py` and `event_catalog.py`.
- Update `presentation.py` so damage, HP, rewards, and withering instructions appear once, with the narrative carrying the action and the status line carrying only necessary mechanics.
- Keep pool selection, anti-repetition history, RNG behavior, public renderer signatures, combat rules, rewards, and event timing unchanged unless a separate approved task requires otherwise.
- Replace the old “cinematic length” test contract with the new readability contract.

### Phase N3 — War and Castle

- Rewrite both six-variant attack pools, war result captions, declaration text, and click acknowledgements.
- Test attacker and defender perspectives separately. The attacker-side mention may stay clickable; the defender-side attacker name must remain plain and free of `tg://user` links.
- Verify that `{buff_label}` remains attached correctly and that bold markup cannot leak or break group titles, card names, or player names.

### Phase N4 — Halloween, Mine, Garden, and chest rewards

- Modernize Halloween announcements/drops and the real boss-attack alert; do not add a new multi-message boss narration system as part of a copy overhaul.
- Rewrite all Mine shift/cooldown variants and remove duplication in `_result_caption`.
- Shorten Garden withering/restoration and rarity-celebration text, then align daily-chest reward wording and emphasis.
- Preserve reward types, quantities, cooldowns, item keys, and configuration-driven behavior.

### Phase N5 — Apple Battle and Apple Duel

- Rewrite `_compose_plan` and the legacy `narrate` path together so old and current flows cannot drift back into two different tones.
- Keep the existing beat count and delivery/recovery behavior in N5; this is a text replacement, not a timing redesign.
- Compress all six Duel scenes and remove meta-narration. Bold players, cards, winner, and reward where present.

### Phase N6 — Social narratives

- Rewrite Hug, Kiss, Marriage, and Friendship action/result messages with varied, natural phrasing and proper name/reward emphasis.
- Apply the required `**` convention consistently. HTML surfaces need an explicit presentation migration with escaping tests; Markdown and HTML must never be mixed in one message.

### Phase N7 — Onboarding and lore

- Shorten the two onboarding/world surfaces, then rewrite all character and faction lore without changing canon.
- Update the seed-synchronization and card-lore test expectations in the same phase. Do not change character stats, factions, release status, shop availability, or card ownership behavior.

### Phase N8 — Cross-game editorial and rendering audit

- Search again for archaic vocabulary, long multi-sentence narratives, repeated template openings, ASCII ellipses, em/en dashes, Arabic-character contamination, and unbolded key results.
- Render representative messages for every pool with ordinary names, long names, and Markdown-special characters. Check both photo captions and text fallback.
- Run the focused tests after each phase and the full suite after N7/N8. No phase should change damage, RNG, rewards, settlement, cooldowns, privacy, or database schema as a side effect of copy editing.

## 6. Test contracts that will intentionally need updates

- `tests/invasion_v2/test_narrative_pool.py::test_rhythm_and_added_openings` currently requires at least two templates with `180 <= len(text) < 900` in every core pool. That directly contradicts N2 and must be replaced, not worked around with padding.
- `tests/invasion_v2/test_narrative_pool.py::test_supplied_first_three_are_verbatim` freezes the old literary samples. The new reference set and orthography should replace it deliberately.
- `tests/test_halloween_event.py` currently looks for «آسیب وارد کردید! (کریتیکال:» and will need the new attack wording.
- `tests/apple_mine/test_apple_mine.py` pins the cooldown prefix and exact panel/result captions; update only the affected presentation expectations while retaining anti-repetition coverage.
- The `tests/test_card_lore*.py` family and roster/foundation tests contain exact lore strings and characteristic phrase assertions. N7 must update these from the approved rewritten canon rather than deleting coverage.
- War, Apple Battle, Duel, Garden, chest, and social suites should retain their behavioral checks while replacing exact-copy assertions with checks for required data, safe markup, concise structure, and variant diversity.

## 7. N1 completion state

- [x] Narrative pools and inline narrative captions traced to their actual renderers.
- [x] Runtime story/action/lore files inventoried, including files not named `narrative.py`.
- [x] Five real longest/current examples rewritten without renaming placeholders.
- [x] Persian writing rules applied to the proposals.
- [x] Phased replacement and test-migration strategy documented.
- [x] No application code written or changed in N1.
