"""Rebalance invasion v2 defaults and add the Sib-Buff shop price.

Revision ID: 047_inv2_rebalance_sib_buff
Revises: 046_inv2_min_players_with_card

Only known previous defaults are updated. Both the repository's 3600-second
interval and the deployed 7200-second interval become 14400. Custom values,
the feature flag and active invasion snapshots are never changed. Missing
rows remain missing and use the new ConfigService defaults.

An existing item_buy_prices JSON object is extended, not replaced. Its other
prices and any pre-existing sib_buff price remain authoritative. Downgrade
restores the repository's old defaults (interval 3600) and removes only a
sib_buff price still equal to 50; it cannot distinguish an explicit admin
choice equal to a default. Player inventories and reward history are untouched.
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "047_inv2_rebalance_sib_buff"
down_revision = "046_inv2_min_players_with_card"
branch_labels = None
depends_on = None

# Frozen values: migrations must not import mutable application defaults.
_DEFAULTS = (
    ("invasion_difficulty_multiplier", ("2.5",), "2.0"),
    ("invasion_join_window_seconds", ("300",), "1800"),
    ("invasion_interval_seconds", ("3600", "7200"), "14400"),
)


def _update_defaults(connection, *, reverse: bool = False) -> None:
    statement = sa.text(
        "UPDATE system_config SET `value` = :new "
        "WHERE `key` = :key AND `value` IN :previous"
    ).bindparams(sa.bindparam("previous", expanding=True))
    for key, previous, new in _DEFAULTS:
        connection.execute(statement, {
            "key": key,
            "previous": (new,) if reverse else previous,
            "new": previous[0] if reverse else new,
        })


def _update_shop_price(connection, *, reverse: bool = False) -> None:
    raw = connection.execute(sa.text(
        "SELECT `value` FROM system_config WHERE `key` = 'item_buy_prices'"
    )).scalar_one_or_none()
    if raw is None:
        return  # The complete built-in price map already includes sib_buff.
    prices = json.loads(raw)
    if not isinstance(prices, dict):
        raise ValueError("item_buy_prices must be a JSON object; refusing to overwrite it")
    if reverse:
        if prices.get("sib_buff") != 50:
            return
        del prices["sib_buff"]
    else:
        if "sib_buff" in prices:
            return
        prices["sib_buff"] = 50
    connection.execute(sa.text(
        "UPDATE system_config SET `value` = :value WHERE `key` = 'item_buy_prices'"
    ), {"value": json.dumps(prices, ensure_ascii=False)})


def upgrade() -> None:
    connection = op.get_bind()
    _update_defaults(connection)
    _update_shop_price(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _update_defaults(connection, reverse=True)
    _update_shop_price(connection, reverse=True)
