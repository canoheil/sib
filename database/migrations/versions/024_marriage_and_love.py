"""Add marriages, marriage_proposals, and flower_gifts tables for Sib Marriage and Sib Love.

Revision ID: 024_marriage_and_love
Revises: 023_friendly_parties
Create Date: 2026-10-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "024_marriage_and_love"
down_revision: Union[str, None] = "023_friendly_parties"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PK_TYPE = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())

    if "marriage_proposals" not in existing_tables:
        op.create_table(
            "marriage_proposals",
            sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
            sa.Column(
                "proposer_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "group_id",
                sa.BigInteger(),
                sa.ForeignKey("groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="PENDING",
            ),
            sa.Column("invalid_reason", sa.String(length=64), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_marriage_proposals_proposer_id",
            "marriage_proposals",
            ["proposer_id"],
        )
        op.create_index(
            "ix_marriage_proposals_target_id",
            "marriage_proposals",
            ["target_id"],
        )
        op.create_index(
            "ix_marriage_proposals_group_id",
            "marriage_proposals",
            ["group_id"],
        )
        op.create_index(
            "ix_marriage_proposals_status",
            "marriage_proposals",
            ["status"],
        )
        op.create_index(
            "ix_marriage_proposals_expires_at",
            "marriage_proposals",
            ["expires_at"],
        )
        op.create_index(
            "ix_marriage_proposals_proposer_status",
            "marriage_proposals",
            ["proposer_id", "status"],
        )
        op.create_index(
            "ix_marriage_proposals_target_status",
            "marriage_proposals",
            ["target_id", "status"],
        )
        op.create_index(
            "ix_marriage_proposals_status_expires_at",
            "marriage_proposals",
            ["status", "expires_at"],
        )

    if "marriages" not in existing_tables:
        op.create_table(
            "marriages",
            sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
            sa.Column(
                "user1_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user2_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "proposer_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "accepter_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "origin_group_id",
                sa.BigInteger(),
                sa.ForeignKey("groups.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "proposal_id",
                sa.BigInteger(),
                sa.ForeignKey("marriage_proposals.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="ACTIVE",
            ),
            sa.Column(
                "love_score",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "last_flower_accepted_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "last_flower_accepted_date",
                sa.String(length=10),
                nullable=True,
            ),
            sa.Column(
                "last_decay_date",
                sa.String(length=10),
                nullable=True,
            ),
            sa.Column(
                "married_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "divorced_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "divorced_by_user_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "cooldown_until",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_marriages_user1_id", "marriages", ["user1_id"])
        op.create_index("ix_marriages_user2_id", "marriages", ["user2_id"])
        op.create_index("ix_marriages_proposer_id", "marriages", ["proposer_id"])
        op.create_index("ix_marriages_accepter_id", "marriages", ["accepter_id"])
        op.create_index("ix_marriages_origin_group_id", "marriages", ["origin_group_id"])
        op.create_index("ix_marriages_proposal_id", "marriages", ["proposal_id"])
        op.create_index("ix_marriages_status", "marriages", ["status"])
        op.create_index("ix_marriages_last_decay_date", "marriages", ["last_decay_date"])
        op.create_index("ix_marriages_cooldown_until", "marriages", ["cooldown_until"])
        op.create_index(
            "ix_marriages_user1_status",
            "marriages",
            ["user1_id", "status"],
        )
        op.create_index(
            "ix_marriages_user2_status",
            "marriages",
            ["user2_id", "status"],
        )
        op.create_index(
            "ix_marriages_status_last_decay_date",
            "marriages",
            ["status", "last_decay_date"],
        )

    if "flower_gifts" not in existing_tables:
        op.create_table(
            "flower_gifts",
            sa.Column("id", _PK_TYPE, primary_key=True, autoincrement=True),
            sa.Column(
                "marriage_id",
                sa.BigInteger(),
                sa.ForeignKey("marriages.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "sender_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "receiver_id",
                sa.BigInteger(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("message_id", sa.BigInteger(), nullable=True),
            sa.Column("gift_date", sa.String(length=10), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="PENDING",
            ),
            sa.Column(
                "love_delta",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("ix_flower_gifts_marriage_id", "flower_gifts", ["marriage_id"])
        op.create_index("ix_flower_gifts_sender_id", "flower_gifts", ["sender_id"])
        op.create_index("ix_flower_gifts_receiver_id", "flower_gifts", ["receiver_id"])
        op.create_index("ix_flower_gifts_gift_date", "flower_gifts", ["gift_date"])
        op.create_index("ix_flower_gifts_status", "flower_gifts", ["status"])
        op.create_index(
            "ix_flower_gifts_marriage_date_sender_status",
            "flower_gifts",
            ["marriage_id", "gift_date", "sender_id", "status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = set(insp.get_table_names())

    if "flower_gifts" in existing_tables:
        op.drop_table("flower_gifts")
    if "marriages" in existing_tables:
        op.drop_table("marriages")
    if "marriage_proposals" in existing_tables:
        op.drop_table("marriage_proposals")
