# KNOWLEDGE — card-upgrade-system
- Test command: `/tmp/runtests.sh -q -p no:cacheprovider` =
  `cd /home/user/sib && PYTHONPATH=/home/user:/home/user/sib:. sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test.db /home/user/.venv-sib/bin/python -m pytest ...`
  (venv at /home/user/.venv-sib with requirements + greenlet aiosqlite pillow). Baseline: 2 failed
  (test_navigation::test_shop_and_battle_..., test_stale_and_duplicate_callbacks::test_non_existent_match_callback — MySQL-only), 491 passed, 6 skipped.
- Skills dir is `.agents/skills/`.
- "Red Apple" in the brief == wallet currency (normal 🍎 apple). Never treat the sib_sorkh character as an item.
- Legacy ledger idempotency keys `card_upgrade:{id}:{lvl}` exist; new keys MUST differ (`card_upgrade_v2:`).
- `apple_wallet.spend/earn` call `session.rollback()` on duplicate-key IntegrityError.
- `character_art.resolve_character_photo` calls `AssetService.get_file_id` which does not exist (only hit when
  `characters.artwork` is set; currently null everywhere). Pre-existing, out of scope.

## Findings while testing (step 2)
- `handlers/shop.py` `handle_shop_sell_all` ends with dead code referencing undefined `render_banner/text/kb` (pre-existing at HEAD, out of scope, flagged in the report).
- SQLite cannot interleave two writers: the CAS race test mutates through the SAME session instead of a second `get_session()`.
- Retired ledger keys `card_upgrade:<card>:<level>` may still exist: new key is `card_upgrade_v2:...`.
- Full suite after the feature: 2 known MySQL-only failures, 556 passed, 8 skipped.
