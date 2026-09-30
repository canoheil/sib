# Progression rules + cost table + upgrade service
Status: COMPLETED
## Sub tasks
1. [x] services/card_progression.py (MAX 10, +10, CARD_ATTRIBUTES, derive)
2. [x] CONFIG_DEFAULTS["card_upgrade_costs"] (+ card_base_attribute_budget; old budget/cost keys removed)
3. [x] services/card_upgrade.py (preview, atomic upgrade, errors)
4. [x] consumers moved to derived stats: apple_battle (+balance_lab), trading handler, bot_match
## NOTES
- Lock order: card -> item row -> wallet; CAS `UPDATE .. WHERE level=:from`; `expected_level` from the callback
  defeats double taps; ledger key `card_upgrade_v2:{card}:{to}`.
- wallet.get_balance / ItemService.get_total gained `for_update`.
