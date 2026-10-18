"""Apple Clash group wars, attacks, participants and peace shields."""

from alembic import op
import sqlalchemy as sa

revision = "050_apple_clash"
down_revision = "049_apple_castles"
branch_labels = None
depends_on = None


def upgrade():
    # Update group_castles: shield_until and in_war
    op.add_column(
        "group_castles",
        sa.Column("shield_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "group_castles",
        sa.Column(
            "in_war",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )

    # Create group_wars
    op.create_table(
        "group_wars",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "attacker_id",
            sa.BigInteger(),
            sa.ForeignKey("group_castles.group_id"),
            nullable=False,
        ),
        sa.Column(
            "defender_id",
            sa.BigInteger(),
            sa.ForeignKey("group_castles.group_id"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "attacker_damage",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "defender_damage",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "ends_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "winner_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id"),
            nullable=True,
        ),
        sa.Column(
            "looted_apples",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'finished')", name="ck_group_wars_status"
        ),
    )
    op.create_index(
        "ix_group_wars_status_ends_at", "group_wars", ["status", "ends_at"]
    )
    op.create_index(
        "ix_group_wars_attacker_status", "group_wars", ["attacker_id", "status"]
    )
    op.create_index(
        "ix_group_wars_defender_status", "group_wars", ["defender_id", "status"]
    )

    # Create war_participants
    op.create_table(
        "war_participants",
        sa.Column(
            "war_id",
            sa.BigInteger(),
            sa.ForeignKey("group_wars.id"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            primary_key=True,
        ),
        sa.Column(
            "group_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id"),
            nullable=False,
        ),
        sa.Column(
            "energy",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
        sa.Column(
            "total_damage",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "last_attack_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_war_participants_war_group", "war_participants", ["war_id", "group_id"]
    )

    # Create war_attacks
    op.create_table(
        "war_attacks",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "war_id",
            sa.BigInteger(),
            sa.ForeignKey("group_wars.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "source_group_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id"),
            nullable=False,
        ),
        sa.Column(
            "target_group_id",
            sa.BigInteger(),
            sa.ForeignKey("groups.id"),
            nullable=False,
        ),
        sa.Column("damage", sa.Integer(), nullable=False),
        sa.Column(
            "is_buff_used",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_war_attacks_war_user", "war_attacks", ["war_id", "user_id"]
    )
    op.create_index(
        "ix_war_attacks_war_source_group", "war_attacks", ["war_id", "source_group_id"]
    )


def downgrade():
    op.drop_table("war_attacks")
    op.drop_table("war_participants")
    op.drop_table("group_wars")
    op.drop_column("group_castles", "in_war")
    op.drop_column("group_castles", "shield_until")
