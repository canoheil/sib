"""Classify character rarities and retire pre-rarity shop rotations.

Revision ID: 081_card_rarity
Revises: 080_group_doctors

012 already created characters.rarity as VARCHAR(20) NOT NULL DEFAULT
'COMMON'. Reactivate that column rather than adding or dropping it again.
Use the shipped roster's actual codes (e.g. mmung, goje and moon_lily), not
the design list's English aliases. Unlisted/custom cards default to COMMON.

Expire current rotations so the next shop visit selects five cards under
the new policy. Historical entries, ownership, wallets and balance history
are untouched. Downgrade keeps rarity data (080 does not read it) and the
pre-existing column, and expires rotations for the old policy to take over.
"""

import sqlalchemy as sa
from alembic import op

revision = "081_card_rarity"
down_revision = "080_group_doctors"
branch_labels = None
depends_on = None

# Frozen migration data; never import mutable application seed/config here.
RARITY_CODES = {
    "LEGENDARY": ("amber", "hero", "igris"),
    "EPIC": ("tifun", "mmung", "goje", "zarush", "filo"),
    "RARE": ("nekro", "wizard", "dr_daxibe", "moon_lily", "treyni", "mahmud"),
    "COMMON": (
        "sib_sorkh", "sib_talayi", "sib_sabz", "green_knight",
        "sage_king", "sib_mahi", "moon_knight",
    ),
}


def _expire_rotations() -> None:
    rotations = sa.table("shop_rotations", sa.column("is_active", sa.Boolean()))
    op.execute(rotations.update().where(rotations.c.is_active == True).values(is_active=False))


def upgrade() -> None:
    characters = sa.table(
        "characters", sa.column("code", sa.String(64)), sa.column("rarity", sa.String(20))
    )
    op.execute(characters.update().values(rarity="COMMON"))
    for rarity, codes in RARITY_CODES.items():
        op.execute(characters.update().where(characters.c.code.in_(codes)).values(rarity=rarity))
    _expire_rotations()


def downgrade() -> None:
    # The column belongs to 012; never remove it or invent old rarity values.
    _expire_rotations()
