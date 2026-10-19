"""Hard cap counter on war participants (C2-8.5).

Adds ``war_participants.attack_count`` so the war attack handler can enforce
the 10-attacks-per-player-per-war limit under a row lock, independently of
the energy/potion economy.

The column starts at 0 for new rows: ``attack_count`` counts attacks ALREADY
spent in the war, so ``attack_count < 10`` admits exactly ten hits. (A server
default of 1 would silently cost every existing/fresh participant one of
those ten attacks.)

Rows of wars that are already running are backfilled from ``war_attacks``, so
a player who exploited the missing cap before this migration is capped
immediately instead of receiving ten extra hits. The correlated subquery uses
``ix_war_attacks_war_user(war_id, user_id)`` and is valid on both MariaDB and
the SQLite migration tests.
"""

from alembic import op
import sqlalchemy as sa

revision = "052_war_attack_count"
down_revision = "051_referrals"
branch_labels = None
depends_on = None


def upgrade():
    # batch mode keeps the ALTER portable between MariaDB (production) and the
    # isolated SQLite migration tests.
    with op.batch_alter_table("war_participants") as batch:
        batch.add_column(
            sa.Column(
                "attack_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
    op.execute(
        """
        UPDATE war_participants
        SET attack_count = (
            SELECT COUNT(*)
            FROM war_attacks
            WHERE war_attacks.war_id = war_participants.war_id
              AND war_attacks.user_id = war_participants.user_id
        )
        """
    )


def downgrade():
    with op.batch_alter_table("war_participants") as batch:
        batch.drop_column("attack_count")
