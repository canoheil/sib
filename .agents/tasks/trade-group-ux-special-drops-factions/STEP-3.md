# STEP 3 — Special apple drop captions

Status: DONE (weights and images untouched; `tests/test_special_apple_captions.py` 7 tests).

- `services/config_store.py`: `CONFIG_DEFAULTS["garden_drop_entries"]` per-apple
  `caption` (escalating tone: golden «طلایی»/«شانس», diamond «الماسی»/«کمیاب»,
  obsidian «ابسیدینی»/«افسانه»/«هزار»). Weights stay 70/20/9/1.
- `services/garden.py`: `HarvestOutcome.caption = entry.get("caption") or entry.get("message")`.
- `handlers/garden.py`: `_send_asset(callback, bot, asset_key, caption=None)` →
  `AssetService.send(..., caption=caption)`; single harvest uses
  `outcome.caption`; harvest-all sends one celebration per distinct special asset.
- No new localization layer: captions live in the existing config/defaults.
