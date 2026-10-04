"""Focused tests for the Sib Marriage and Sib Love database/domain foundation."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from sib.database import (
    FlowerGift,
    FlowerGiftStatus,
    Group,
    Marriage,
    MarriageProposal,
    MarriageStatus,
    ProposalStatus,
    User,
    canonical_spouse_ids,
)
from sib.services.config_store import CONFIG_DEFAULTS, ConfigService
from sib.services.items import (
    ITEM_CATALOG,
    ItemService,
    item_description,
    item_emoji,
    item_label,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


async def _ensure_user(session, user_id: int, first_name: str = "کاربر") -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=f"{first_name}{user_id}", language_code="fa")
        session.add(user)
        await session.flush()
    return user


async def _ensure_group(session, group_id: int = -1001234567890) -> Group:
    group = await session.get(Group, group_id)
    if group is None:
        group = Group(id=group_id, title="گروه تست سیب")
        session.add(group)
        await session.flush()
    return group


# ---------------------------------------------------------------------------
# Canonical spouse pair & status enum definitions
# ---------------------------------------------------------------------------


def test_canonical_spouse_ids_orders_ascending_and_rejects_self():
    assert canonical_spouse_ids(100, 200) == (100, 200)
    assert canonical_spouse_ids(200, 100) == (100, 200)
    with pytest.raises(ValueError):
        canonical_spouse_ids(100, 100)


def test_status_constants_cover_required_lifecycles():
    assert set(MarriageStatus.ALL) == {"ACTIVE", "DIVORCED"}
    assert set(ProposalStatus.ALL) == {
        "PENDING",
        "ACCEPTED",
        "REJECTED",
        "EXPIRED",
        "INVALIDATED",
        "FAILED",
    }
    assert ProposalStatus.PENDING not in ProposalStatus.TERMINAL
    assert set(FlowerGiftStatus.ALL) == {
        "PENDING",
        "ACCEPTED",
        "REJECTED",
        "INVALIDATED",
        "EXPIRED",
    }
    assert FlowerGiftStatus.PENDING not in FlowerGiftStatus.TERMINAL


# ---------------------------------------------------------------------------
# Models: MarriageProposal, Marriage, FlowerGift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marriage_proposal_persistence_and_statuses(db_session):
    await _ensure_user(db_session, 101)
    await _ensure_user(db_session, 202)
    await _ensure_group(db_session, -100500)

    now = datetime.now(timezone.utc)
    proposal = MarriageProposal(
        proposer_id=202,
        target_id=101,
        group_id=-100500,
        message_id=999,
        expires_at=now + timedelta(seconds=300),
    )
    db_session.add(proposal)
    await db_session.commit()

    assert proposal.id is not None
    assert proposal.status == ProposalStatus.PENDING
    assert proposal.is_pending is True

    for next_status in (
        ProposalStatus.REJECTED,
        ProposalStatus.EXPIRED,
        ProposalStatus.INVALIDATED,
        ProposalStatus.FAILED,
        ProposalStatus.ACCEPTED,
    ):
        proposal.status = next_status
        proposal.resolved_at = now
        await db_session.commit()
        fetched = await db_session.get(MarriageProposal, proposal.id)
        assert fetched is not None
        assert fetched.status == next_status
        assert fetched.is_pending is False


@pytest.mark.asyncio
async def test_marriage_enforces_canonical_order_and_preserves_divorce_history(db_session):
    await _ensure_user(db_session, 50)
    await _ensure_user(db_session, 10)
    await _ensure_group(db_session, -100777)

    now = datetime.now(timezone.utc)
    proposal = MarriageProposal(
        proposer_id=50,
        target_id=10,
        group_id=-100777,
        message_id=1234,
        status=ProposalStatus.ACCEPTED,
        expires_at=now + timedelta(seconds=300),
        resolved_at=now,
    )
    db_session.add(proposal)
    await db_session.flush()

    # Proposer (50) > Accepter (10): Marriage must canonicalize user1_id=10, user2_id=50
    marriage = Marriage(
        proposer_id=50,
        accepter_id=10,
        origin_group_id=-100777,
        proposal_id=proposal.id,
    )
    db_session.add(marriage)
    await db_session.commit()

    assert marriage.user1_id == 10
    assert marriage.user2_id == 50
    assert marriage.proposer_id == 50
    assert marriage.accepter_id == 10
    assert marriage.status == MarriageStatus.ACTIVE
    assert marriage.love_score == 0
    assert marriage.is_active is True
    assert marriage.is_divorced is False
    assert marriage.includes_user(10) is True
    assert marriage.includes_user(50) is True
    assert marriage.includes_user(99) is False
    assert marriage.spouse_of(10) == 50
    assert marriage.spouse_of(50) == 10
    assert marriage.spouse_of(99) is None

    # Simulate Love growth then divorce: record is preserved, Love resets to 0, cooldown set
    marriage.love_score = 45
    await db_session.commit()

    divorce_time = now + timedelta(days=3)
    marriage.status = MarriageStatus.DIVORCED
    marriage.love_score = 0
    marriage.divorced_at = divorce_time
    marriage.divorced_by_user_id = 50
    marriage.cooldown_until = divorce_time + timedelta(seconds=86400)
    await db_session.commit()

    # Historical divorced record remains in DB while a later remarriage can be created
    remarriage = Marriage(
        user1_id=50,
        user2_id=10,
        proposer_id=10,
        accepter_id=50,
        origin_group_id=-100777,
    )
    db_session.add(remarriage)
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(Marriage)
            .where(Marriage.user1_id == 10, Marriage.user2_id == 50)
            .order_by(Marriage.id.asc())
        )
    ).scalars().all()
    assert len(rows) == 2
    assert rows[0].status == MarriageStatus.DIVORCED
    assert rows[0].love_score == 0
    assert rows[0].divorced_by_user_id == 50
    assert rows[1].status == MarriageStatus.ACTIVE


@pytest.mark.asyncio
async def test_flower_gift_directional_audit_and_decay_fields(db_session):
    await _ensure_user(db_session, 11)
    await _ensure_user(db_session, 22)
    await _ensure_group(db_session, -100888)

    marriage = Marriage(
        proposer_id=11,
        accepter_id=22,
        origin_group_id=-100888,
    )
    db_session.add(marriage)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    today = "2026-10-03"

    # A -> B accepted gift
    gift_ab = FlowerGift(
        marriage_id=marriage.id,
        sender_id=11,
        receiver_id=22,
        chat_id=-100888,
        message_id=5001,
        gift_date=today,
        status=FlowerGiftStatus.ACCEPTED,
        love_delta=5,
        resolved_at=now,
    )
    # B -> A rejected gift on the same UTC date
    gift_ba = FlowerGift(
        marriage_id=marriage.id,
        sender_id=22,
        receiver_id=11,
        chat_id=11,
        message_id=5002,
        gift_date=today,
        status=FlowerGiftStatus.REJECTED,
        love_delta=-1,
        resolved_at=now,
    )
    marriage.love_score = 4
    marriage.last_flower_accepted_at = now
    marriage.last_flower_accepted_date = today
    marriage.last_decay_date = today

    db_session.add_all([gift_ab, gift_ba])
    await db_session.commit()

    ab_rows = (
        await db_session.execute(
            select(FlowerGift).where(
                FlowerGift.marriage_id == marriage.id,
                FlowerGift.gift_date == today,
                FlowerGift.sender_id == 11,
            )
        )
    ).scalars().all()
    ba_rows = (
        await db_session.execute(
            select(FlowerGift).where(
                FlowerGift.marriage_id == marriage.id,
                FlowerGift.gift_date == today,
                FlowerGift.sender_id == 22,
            )
        )
    ).scalars().all()

    assert len(ab_rows) == 1 and ab_rows[0].status == FlowerGiftStatus.ACCEPTED
    assert len(ba_rows) == 1 and ba_rows[0].status == FlowerGiftStatus.REJECTED
    assert marriage.last_flower_accepted_date == today
    assert marriage.last_decay_date == today


# ---------------------------------------------------------------------------
# Migration 024 upgrade / downgrade validation
# ---------------------------------------------------------------------------


def test_migration_024_upgrade_and_downgrade_are_reversible():
    path = REPO_ROOT / "database/migrations/versions/024_marriage_and_love.py"
    spec = importlib.util.spec_from_file_location("migration_024", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "024_marriage_and_love"
    assert migration.down_revision == "023_friendly_parties"

    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", sa.BigInteger, primary_key=True))
    sa.Table("groups", metadata, sa.Column("id", sa.BigInteger, primary_key=True))
    metadata.create_all(engine)

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            # Idempotent second upgrade check
            migration.upgrade()

        insp = sa.inspect(connection)
        tables = set(insp.get_table_names())
        assert {"marriage_proposals", "marriages", "flower_gifts"}.issubset(tables)

        marriage_indexes = {idx["name"] for idx in insp.get_indexes("marriages")}
        assert {
            "ix_marriages_user1_status",
            "ix_marriages_user2_status",
            "ix_marriages_status",
            "ix_marriages_status_last_decay_date",
        }.issubset(marriage_indexes)

        proposal_indexes = {idx["name"] for idx in insp.get_indexes("marriage_proposals")}
        assert {
            "ix_marriage_proposals_proposer_status",
            "ix_marriage_proposals_target_status",
            "ix_marriage_proposals_status_expires_at",
        }.issubset(proposal_indexes)

        flower_indexes = {idx["name"] for idx in insp.get_indexes("flower_gifts")}
        assert "ix_flower_gifts_marriage_date_sender_status" in flower_indexes

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            # Idempotent second downgrade check
            migration.downgrade()

        remaining_tables = set(sa.inspect(connection).get_table_names())
        assert "marriage_proposals" not in remaining_tables
        assert "marriages" not in remaining_tables
        assert "flower_gifts" not in remaining_tables


# ---------------------------------------------------------------------------
# Configuration & Item Catalog foundation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_marriage_and_love_config_defaults(db_session):
    assert await ConfigService.get(db_session, "marriage_proposal_timeout_seconds") == 300
    assert await ConfigService.get(db_session, "marriage_divorce_cooldown_seconds") == 86400
    assert await ConfigService.get(db_session, "love_min") == 0
    assert await ConfigService.get(db_session, "love_max") == 100
    assert await ConfigService.get(db_session, "love_initial") == 0
    assert await ConfigService.get(db_session, "flower_reject_love_penalty") == 1
    assert await ConfigService.get(db_session, "love_daily_decay") == 1
    assert await ConfigService.get(db_session, "flower_daily_limit_per_direction") == 1
    assert await ConfigService.get(db_session, "item_sell_price_ratio") == 0.5

    # Economy-dependent keys exist in CONFIG_DEFAULTS and are runtime-configurable.
    # flower_love_gain now carries the value chosen by the economy review; the
    # two per-item price keys stay unset (the price maps are the one source).
    assert CONFIG_DEFAULTS["flower_love_gain"] == 2
    for key in ("wedding_ring_buy_price", "flower_buy_price"):
        assert key in CONFIG_DEFAULTS
        assert CONFIG_DEFAULTS[key] is None

    await ConfigService.set(db_session, "flower_love_gain", 5)
    assert await ConfigService.get(db_session, "flower_love_gain") == 5


@pytest.mark.asyncio
async def test_wedding_ring_and_flower_catalog_and_inventory(db_session):
    for key in ("wedding_ring", "flower"):
        assert key in ITEM_CATALOG
        assert item_emoji(key)
        assert item_label(key) != key
        assert item_description(key)

    # Magic Powder behavior and default shop prices remain untouched.
    # (The map itself gains the Ring/Flower entries in the economy phase, so
    # assert on Magic Powder's price rather than on the whole dict.)
    assert CONFIG_DEFAULTS["item_buy_prices"]["magic_powder"] == 40
    assert "magic_powder" not in CONFIG_DEFAULTS["item_sell_prices"]

    await _ensure_user(db_session, 303)
    await ItemService.grant(db_session, 303, "wedding_ring", 1)
    await ItemService.grant(db_session, 303, "flower", 3)
    await db_session.commit()

    assert await ItemService.get_total(db_session, 303, "wedding_ring") == 1
    assert await ItemService.get_total(db_session, 303, "flower") == 3

    remaining_rings = await ItemService.consume(db_session, 303, "wedding_ring", 1)
    assert remaining_rings == 0
    assert await ItemService.get_total(db_session, 303, "wedding_ring") == 0
