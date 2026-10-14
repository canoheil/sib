"""Backfill group activity from historical match participants (no power guesses).

From the repository parent (after reviewing the target URL and applying 037):
  python -m sib.scripts.backfill_inv2_group_members --dry-run
  python -m sib.scripts.backfill_inv2_group_members --confirm-database NAME

Historical match timestamps, not today's date, determine active membership.
"""

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, inspect, select
from sqlalchemy.dialects import mysql, sqlite

from sib.database.connection import get_session
from sib.database.models import Inv2GroupMember, MatchPlayer, User


@dataclass
class BackfillCounts:
    source_available: bool = True
    scanned: int = 0
    inserted: int = 0
    advanced: int = 0
    unchanged: int = 0


def _upsert(dialect: str, user_id: int, group_id: int, seen_at: datetime):
    values = dict(user_id=user_id, group_id=group_id, last_seen_at=seen_at)
    if dialect in ("mysql", "mariadb"):
        stmt = mysql.insert(Inv2GroupMember).values(**values)
        return stmt.on_duplicate_key_update(
            last_seen_at=func.greatest(Inv2GroupMember.last_seen_at, stmt.inserted.last_seen_at)
        )
    if dialect == "sqlite":
        stmt = sqlite.insert(Inv2GroupMember).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=["user_id", "group_id"],
            set_={"last_seen_at": func.max(Inv2GroupMember.last_seen_at, stmt.excluded.last_seen_at)},
        )
    raise RuntimeError(f"Unsupported backfill dialect: {dialect}")


async def backfill(session, *, dry_run: bool = True) -> BackfillCounts:
    """Only backfill verified user/group pairs; repeat runs never rewind visits.

    The caller controls the transaction. Dry-run executes no writes at all.
    """
    counts = BackfillCounts()
    conn = await session.connection()
    has_source = await conn.run_sync(lambda sync: inspect(sync).has_table("match_players"))
    if not has_source:
        counts.source_available = False
        return counts

    # A match participant's group_id is an FK to groups.id, user_id an FK to
    # users.id. Exclude simulated players. Preserve historical UTC timestamps.
    latest_of_row = case(
        (MatchPlayer.updated_at >= MatchPlayer.created_at, MatchPlayer.updated_at),
        else_=MatchPlayer.created_at,
    )
    source = (await session.execute(
        select(MatchPlayer.user_id, MatchPlayer.group_id, func.max(latest_of_row))
        .join(User, User.id == MatchPlayer.user_id)
        .where(MatchPlayer.group_id.is_not(None), User.is_bot.is_(False))
        .group_by(MatchPlayer.user_id, MatchPlayer.group_id)
        .order_by(MatchPlayer.user_id, MatchPlayer.group_id)
    )).all()
    if not source:
        return counts
    existing = {
        (user_id, group_id): seen_at
        for user_id, group_id, seen_at in (await session.execute(
            select(Inv2GroupMember.user_id, Inv2GroupMember.group_id, Inv2GroupMember.last_seen_at)
        )).all()
    }
    dialect = session.get_bind().dialect.name
    for user_id, group_id, seen_at in source:
        counts.scanned += 1
        old = existing.get((user_id, group_id))
        if old is not None and old >= seen_at:
            counts.unchanged += 1
        else:
            if old is None:
                counts.inserted += 1
            else:
                counts.advanced += 1
            if not dry_run:
                await session.execute(_upsert(dialect, user_id, group_id, seen_at))
    return counts


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect only; no writes")
    parser.add_argument("--confirm-database", metavar="NAME", help="required for writes; exact target database name")
    args = parser.parse_args()

    async with get_session() as session:
        url = session.get_bind().url
        print(f"target: {url.render_as_string(hide_password=True)}")
        if not args.dry_run and (not url.database or args.confirm_database != url.database):
            print("refused: use --dry-run or --confirm-database with the exact target database name")
            return 2
        counts = await backfill(session, dry_run=args.dry_run)
        if not counts.source_available:
            print("no match_players source table; nothing to backfill")
            return 0
        mode = "dry-run" if args.dry_run else "committed"
        print(f"{mode}: scanned={counts.scanned} inserted={counts.inserted} "
              f"advanced={counts.advanced} unchanged={counts.unchanged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
