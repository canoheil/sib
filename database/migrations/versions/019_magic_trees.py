"""Magic tree progression, garden capacity redesign, item economy config.

Revision ID: 019_magic_trees
Revises: 018_apple_battle_match
Create Date: 2026-09-30

Additive schema change plus two data normalizations required by the new
economy model:

1. ``garden_plots.magic_level`` (INT, default 0): per-tree magical level.
2. Legacy ``apple_normal`` inventory rows (created by the old flat drop
   table) are converted into real wallet balance — one auditable ledger
   row per user (reason ``item_normalization``, idempotency key
   ``mig019:normalize:<user_id>``). Normal Apples are the currency and
   must never be an inventory item.
3. Obsolete balancing keys are removed from ``system_config`` so the new
   code defaults take effect:
   - ``garden_harvest_drops``       → replaced by ``garden_drop_entries`` /
                                      ``garden_drop_weights`` (per magic level)
   - ``garden_expansion_step``      → replaced by per-plot unlocks
   - ``garden_expansion_cost_apples`` → replaced by ``garden_plot_prices``
   - ``garden_max_plots``           → new code default is 10

Gardens that already exceed the new 10-plot cap are GRANDFATHERED: their
plots and trees are never deleted or truncated; the cap only gates NEW
plot unlocks.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "019_magic_trees"
down_revision: Union[str, None] = "018_apple_battle_match"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# system_config keys made obsolete by this revision.
_OBSOLETE_CONFIG_KEYS = (
    "garden_harvest_drops",
    "garden_expansion_step",
    "garden_expansion_cost_apples",
    "garden_max_plots",
)


def _normalize_normal_apple_items(conn) -> None:
    """Convert legacy ``apple_normal`` inventory rows into wallet balance.

    Portable plain SQL (MariaDB + SQLite friendly: no multi-table UPDATE,
    no dialect-specific upsert). One ledger row per user keeps the change
    auditable; the unique idempotency key makes a re-run harmless.
    """
    rows = conn.execute(
        sa.text(
            "SELECT ui.id, ui.user_id, ui.quantity FROM user_items ui "
            "WHERE ui.item_key = 'apple_normal' AND ui.quantity > 0"
        )
    ).fetchall()

    for item_id, user_id, quantity in rows:
        wallet = conn.execute(
            sa.text("SELECT balance FROM apple_wallets WHERE user_id = :u"),
            {"u": user_id},
        ).fetchone()
        old_balance = int(wallet[0]) if wallet else 0
        new_balance = old_balance + int(quantity)

        if wallet is None:
            conn.execute(
                sa.text(
                    "INSERT INTO apple_wallets (user_id, balance) "
                    "VALUES (:u, :b)"
                ),
                {"u": user_id, "b": new_balance},
            )
        else:
            conn.execute(
                sa.text(
                    "UPDATE apple_wallets SET balance = :b WHERE user_id = :u"
                ),
                {"u": user_id, "b": new_balance},
            )
        conn.execute(
            sa.text(
                "INSERT INTO apple_transactions "
                "(user_id, amount, reason, ref_type, ref_id, idempotency_key, balance_after) "
                "VALUES (:u, :a, 'item_normalization', 'user_item', :ref, :k, :ba)"
            ),
            {
                "u": user_id,
                "a": int(quantity),
                "ref": int(item_id),
                "k": f"mig019:normalize:{user_id}",
                "ba": new_balance,
            },
        )

    if rows:
        conn.execute(
            sa.text("DELETE FROM user_items WHERE item_key = 'apple_normal'")
        )


def upgrade() -> None:
    # 1. Per-tree magical level (additive, safe default 0).
    op.add_column(
        "garden_plots",
        sa.Column("magic_level", sa.Integer(), nullable=False, server_default="0"),
    )

    conn = op.get_bind()

    # 2. Normal Apples are currency, not inventory items.
    _normalize_normal_apple_items(conn)

    # 3. Obsolete balancing keys -> new code defaults apply.
    for key in _OBSOLETE_CONFIG_KEYS:
        conn.execute(
            sa.text("DELETE FROM system_config WHERE `key` = :k"), {"k": key}
        )


def downgrade() -> None:
    op.drop_column("garden_plots", "magic_level")

    # The apple_normal item normalization is intentionally NOT reversed:
    # converted Apples are spendable currency and reverting would rewrite
    # player history. Obsolete config keys are restored with their legacy
    # defaults so the previous code version keeps working.
    conn = op.get_bind()
    legacy = {
        "garden_max_plots": "25",
        "garden_expansion_step": "5",
        "garden_expansion_cost_apples": "50",
    }
    for key, value in legacy.items():
        conn.execute(
            sa.text(
                "INSERT INTO system_config (`key`, `value`) VALUES (:k, :v) "
                "ON DUPLICATE KEY UPDATE `value` = :v"
            ),
            {"k": key, "v": value},
        )
