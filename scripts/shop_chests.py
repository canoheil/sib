"""Manage shop chests (admin tool). Game-balance values are DATA, not defaults.

Usage (from the repository parent, with the bot's environment configured):

    python -m sib.scripts.shop_chests list
    python -m sib.scripts.shop_chests show <code>
    python -m sib.scripts.shop_chests apply <file.json> [--dry-run] [--confirm-database NAME]
    python -m sib.scripts.shop_chests set-active <code> on|off [--expected-version N] [--confirm-database NAME]

Safety rules:

* Every command prints the TARGET database (driver, host, port, database name;
  the password is never printed).
* Non-dry-run writes (``apply`` and ``set-active``) need explicit confirmation
  BEFORE anything is written: ``--confirm-database NAME`` must equal the target
  database name, or, in an interactive terminal, the name must be typed at the
  prompt. Anything else cancels with nothing written.
* ``apply --dry-run`` validates and prints the plan, then rolls back. It needs no
  confirmation.
* Optimistic concurrency: every chest in the file carries ``expected_version``,
  the version read with ``list`` or ``show`` (``0`` for a new chest). A stale
  version refuses the whole file. ``set-active`` takes an optional
  ``--expected-version``.
* Migrations are NOT run here. If the schema is missing, the command fails with a
  hint to run ``alembic upgrade head`` as a separate, reviewed step.

JSON format (a list of chests):

    [
      {
        "code": "starter_chest",
        "name": "Starter Chest",
        "price_apples": 50,
        "duplicate_apples": 10,
        "image_key": null,
        "sort_order": 1,
        "is_active": false,
        "expected_version": 0,
        "rewards": [
          {"kind": "apples", "weight": 60, "apples": 5},
          {"kind": "item", "weight": 30, "item_key": "magic_powder"},
          {"kind": "card", "weight": 10, "character_id": 12}
        ]
      }
    ]

``apply`` upserts every chest in the JSON file in ONE transaction. Validation
runs on save (see services/shop_chests.py); a single invalid or stale chest
rolls back the whole file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from sib.database.connection import get_async_sessionmaker
from sib.services.shop_chests import (
    ShopChestConfigError,
    ShopChestError,
    ShopChestService,
    apply_chest_configs,
    format_probability,
)

SCHEMA_HINT = (
    "the shop chest schema is missing or out of date. Run `alembic upgrade head` as a "
    "separate, reviewed step, then retry. Nothing was changed."
)


def target_description(session: AsyncSession) -> str:
    """The database the session writes to, with the password hidden."""
    return session.get_bind().url.render_as_string(hide_password=True)


async def authorize_write(
    session: AsyncSession,
    confirm_database: Optional[str],
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Show the target and decide whether a write may proceed. Writes nothing."""
    url = session.get_bind().url
    name = url.database or ""
    print(f"target: {target_description(session)}")
    if not name:
        print("refused: the target database has no name; nothing written", file=sys.stderr)
        return False
    if confirm_database is not None:
        if confirm_database == name:
            return True
        print(
            f"refused: --confirm-database {confirm_database!r} does not match the target "
            f"database {name!r}; nothing written",
            file=sys.stderr,
        )
        return False
    if not interactive:
        print(
            "refused: writes need confirmation. Pass --confirm-database "
            f"{name} (or run interactively); nothing written",
            file=sys.stderr,
        )
        return False
    typed = input_fn(f"Type the database name ({name}) to write, anything else cancels: ").strip()
    if typed == name:
        return True
    print("cancelled; nothing written", file=sys.stderr)
    return False


async def _cmd_list() -> int:
    async with get_async_sessionmaker()() as session:
        from sqlalchemy import select

        from sib.database.models import ShopChest

        print(f"target: {target_description(session)}")
        chests = (
            await session.scalars(select(ShopChest).order_by(ShopChest.sort_order, ShopChest.id))
        ).all()
        if not chests:
            print("no shop chests configured")
        for chest in chests:
            try:
                await ShopChestService.validate_chest(session, chest)
                state = "valid"
            except ShopChestConfigError as exc:
                state = f"INVALID ({exc})"
            print(
                f"{chest.code}\tactive={chest.is_active}\tprice={chest.price_apples}"
                f"\tduplicate={chest.duplicate_apples}\tversion={chest.version}\t{state}"
            )
    return 0


async def _cmd_show(code: str) -> int:
    async with get_async_sessionmaker()() as session:
        print(f"target: {target_description(session)}")
        chest = await ShopChestService.get_by_code(session, code)
        if chest is None:
            print(f"no chest with code {code!r}", file=sys.stderr)
            return 2
        rows = await ShopChestService.describe(session, chest)
        print(f"{chest.code}: {chest.name} price={chest.price_apples} active={chest.is_active} version={chest.version}")
        for spec, probability, label in rows:
            print(f"  {format_probability(probability):>9}  {label}  (weight {spec.weight})")
    return 0


async def _cmd_apply(
    path: Path,
    dry_run: bool,
    *,
    confirm_database: Optional[str] = None,
    interactive: bool = False,
    input_fn: Callable[[str], str] = input,
) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"rejected, nothing written: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, list):
        print("the file must contain a JSON list of chests", file=sys.stderr)
        return 2
    async with get_async_sessionmaker()() as session:
        if not dry_run:
            if not await authorize_write(
                session, confirm_database, interactive=interactive, input_fn=input_fn
            ):
                return 1
        else:
            print(f"target: {target_description(session)}")
        try:
            codes = await apply_chest_configs(session, data)
        except (ShopChestError, KeyError, TypeError, ValueError, IntegrityError) as exc:
            await session.rollback()
            print(f"rejected, nothing written: {exc}", file=sys.stderr)
            return 1
        if dry_run:
            await session.rollback()
            print(f"dry run ok: {len(codes)} chest(s) would be written: {', '.join(codes)}")
        else:
            await session.commit()
            print(f"written: {len(codes)} chest(s): {', '.join(codes)}")
    return 0


async def _cmd_set_active(
    code: str,
    active: bool,
    expected_version: int | None,
    *,
    confirm_database: Optional[str] = None,
    interactive: bool = False,
    input_fn: Callable[[str], str] = input,
) -> int:
    async with get_async_sessionmaker()() as session:
        if not await authorize_write(
            session, confirm_database, interactive=interactive, input_fn=input_fn
        ):
            return 1
        chest = await ShopChestService.get_by_code(
            session, code, lock=expected_version is not None
        )
        if chest is None:
            print(f"no chest with code {code!r}", file=sys.stderr)
            return 2
        try:
            chest = await ShopChestService.set_active(
                session, chest.id, active, expected_version=expected_version
            )
        except ShopChestError as exc:
            await session.rollback()
            print(f"rejected, nothing changed: {exc}", file=sys.stderr)
            return 1
        await session.commit()
        print(f"{chest.code}: active={chest.is_active} version={chest.version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage shop chests.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("code")
    apply_ = sub.add_parser("apply")
    apply_.add_argument("file", type=Path)
    apply_.add_argument("--dry-run", action="store_true")
    apply_.add_argument("--confirm-database", default=None, metavar="NAME")
    active = sub.add_parser("set-active")
    active.add_argument("code")
    active.add_argument("state", choices=["on", "off"])
    active.add_argument("--expected-version", type=int, default=None)
    active.add_argument("--confirm-database", default=None, metavar="NAME")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interactive = sys.stdin.isatty()
    try:
        if args.command == "list":
            return asyncio.run(_cmd_list())
        if args.command == "show":
            return asyncio.run(_cmd_show(args.code))
        if args.command == "apply":
            return asyncio.run(
                _cmd_apply(
                    args.file,
                    args.dry_run,
                    confirm_database=args.confirm_database,
                    interactive=interactive,
                )
            )
        if args.command == "set-active":
            return asyncio.run(
                _cmd_set_active(
                    args.code,
                    args.state == "on",
                    args.expected_version,
                    confirm_database=args.confirm_database,
                    interactive=interactive,
                )
            )
    except (OperationalError, ProgrammingError) as exc:
        print(f"database error: {SCHEMA_HINT} ({exc.__class__.__name__})", file=sys.stderr)
        return 1
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
