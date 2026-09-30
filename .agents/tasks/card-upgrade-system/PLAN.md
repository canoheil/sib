# PLAN — Card Upgrade System

Model: immutable base (`Character`) + owned progression (`player_cards.level`) + derived stats
(`base + 10*(level-1)` for each of the 3 attributes). Stored per-card stats are dropped (archived first).

1. `services/card_progression.py` — pure rules: MAX level 10, +10 step, `CardAttributes`, derivation.
2. Cost table in `CONFIG_DEFAULTS["card_upgrade_costs"]` (data-driven) + `services/card_upgrade.py`:
   cost lookup/validation, preview, atomic `upgrade` (card lock → items lock → wallet, CAS level,
   expected-level guard against double taps, ledger key `card_upgrade_v2:*`).
3. Model + migration 022: archive legacy allocation, reset level to 1, drop stored stats/boosts.
4. Remove old system (level_budget/upgrade_cost/upgrade/reallocate/card_up/allocation keyboard/old texts/config keys/tests).
5. UI: card detail (view-only, ⬆️ button) → Card Upgrade screen (art, before/after, cost, balance, missing) →
   do-upgrade callback; Level 10 state; nav back rows.
6. Art `assets/items/card_upgrade.jpg` (done) wired through AssetService.
7. Tests, docs, full regression, final report.
