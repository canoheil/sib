"""Shop-chest admin CLI: target display, confirmation, optimistic concurrency.

Runs on the SQLite test database. Row locks (``SELECT ... FOR UPDATE``) are not
exercised here: the lock test below only checks that the lock is REQUESTED.
The actual two-writer race needs MariaDB and is not verified by this suite.
"""

import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from sib.database.connection import get_async_sessionmaker
from sib.database.models import ShopChest
from sib.scripts import shop_chests as cli
from sib.services.shop_chests import ShopChestConfigError, ShopChestService
from sib.tests.test_shop_chests import _chest, _config_entry


def _db_name() -> str:
    return make_url(os.environ["sib_TEST_DATABASE_URL"]).database


async def _version(code: str):
    async with get_async_sessionmaker()() as session:
        chest = await ShopChestService.get_by_code(session, code)
        return None if chest is None else (chest.version, chest.is_active, chest.name)


async def _count() -> int:
    async with get_async_sessionmaker()() as session:
        return await session.scalar(select(func.count(ShopChest.id)))


def _write(tmp_path, entries) -> object:
    path = tmp_path / "chests.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Target display: the password is never printed
# ---------------------------------------------------------------------------


def test_target_description_hides_the_password():
    url = make_url("mysql+aiomysql://admin:s3cret-pass@db.example:3306/sibcom_sib_test")
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(url=url))
    shown = cli.target_description(session)
    assert "s3cret-pass" not in shown
    assert "db.example" in shown and "sibcom_sib_test" in shown


# ---------------------------------------------------------------------------
# Confirmation before non-dry-run writes
# ---------------------------------------------------------------------------


class _Session:
    def __init__(self, name):
        # MariaDB-style URL: the database name is the confirmation name.
        self._url = make_url(f"mysql+aiomysql://tester:pw-hidden@localhost:3306/{name}")

    def get_bind(self):
        return SimpleNamespace(url=self._url)


@pytest.mark.asyncio
async def test_authorize_refuses_without_confirmation_when_not_interactive(capsys):
    ok = await cli.authorize_write(_Session("sibcom_sib_test"), None, interactive=False)
    assert ok is False
    assert "--confirm-database sibcom_sib_test" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_authorize_refuses_a_mismatched_confirmation(capsys):
    ok = await cli.authorize_write(_Session("sibcom_sib_test"), "sib", interactive=False)
    assert ok is False
    assert "does not match" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_authorize_accepts_a_matching_confirmation(capsys):
    ok = await cli.authorize_write(_Session("sibcom_sib_test"), "sibcom_sib_test", interactive=False)
    assert ok is True
    assert "target:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_authorize_prompt_requires_the_typed_name():
    prompts = []

    def answer(text):
        prompts.append(text)
        return "sibcom_sib_test"

    assert await cli.authorize_write(
        _Session("sibcom_sib_test"), None, interactive=True, input_fn=answer
    )
    assert prompts and "sibcom_sib_test" in prompts[0]

    assert not await cli.authorize_write(
        _Session("sibcom_sib_test"), None, interactive=True, input_fn=lambda _t: "yes"
    )


@pytest.mark.asyncio
async def test_apply_without_confirmation_writes_nothing(db_session, tmp_path):
    path = _write(tmp_path, [_config_entry("unconfirmed_chest")])
    assert await cli._cmd_apply(path, False) == 1
    assert await _version("unconfirmed_chest") is None


@pytest.mark.asyncio
async def test_apply_dry_run_needs_no_confirmation_and_writes_nothing(db_session, tmp_path, capsys):
    path = _write(tmp_path, [_config_entry("dry_chest")])
    assert await cli._cmd_apply(path, True) == 0
    assert await _version("dry_chest") is None
    out = capsys.readouterr().out
    assert "target:" in out and "dry run ok" in out


@pytest.mark.asyncio
async def test_set_active_without_confirmation_changes_nothing(db_session, tmp_path):
    chest = await _chest(db_session, code="guarded_chest", active=False)
    assert await cli._cmd_set_active("guarded_chest", True, None) == 1
    assert (await _version("guarded_chest"))[1] is False


# ---------------------------------------------------------------------------
# Optimistic concurrency on apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_creates_a_new_chest_with_expected_version_zero(db_session, tmp_path):
    path = _write(tmp_path, [_config_entry("occ_new")])
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 0
    assert (await _version("occ_new"))[0] == 1


@pytest.mark.asyncio
async def test_apply_with_the_read_version_updates_and_bumps_it(db_session, tmp_path):
    await _chest(db_session, code="occ_match", price=25)
    entry = _config_entry("occ_match", price=40)
    entry["expected_version"] = 1
    path = _write(tmp_path, [entry])
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 0
    assert (await _version("occ_match"))[0] == 2


@pytest.mark.asyncio
async def test_stale_version_refuses_the_whole_file(db_session, tmp_path):
    await _chest(db_session, code="occ_stale")
    # Someone else already moved this chest to version 2.
    async with get_async_sessionmaker()() as session:
        await ShopChestService.save_chest(
            session, code="occ_stale", name="changed by someone else", price_apples=25,
            duplicate_apples=0, is_active=False, rewards=None, expected_version=1,
        )
        await session.commit()

    fresh = _config_entry("occ_stale", price=99)
    fresh["expected_version"] = 1  # the operator's copy is stale
    other = _config_entry("occ_untouched")
    other["expected_version"] = 0
    path = _write(tmp_path, [other, fresh])

    before = await _version("occ_stale")
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 1
    assert await _version("occ_stale") == before
    assert await _version("occ_untouched") is None  # the file is all-or-nothing


@pytest.mark.asyncio
async def test_expected_version_zero_for_an_existing_chest_is_refused(db_session, tmp_path):
    await _chest(db_session, code="occ_exists")
    entry = _config_entry("occ_exists")
    entry["expected_version"] = 0
    path = _write(tmp_path, [entry])
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 1
    assert (await _version("occ_exists"))[0] == 1


@pytest.mark.asyncio
async def test_expected_version_for_a_missing_chest_is_refused(db_session, tmp_path):
    entry = _config_entry("occ_ghost")
    entry["expected_version"] = 3
    path = _write(tmp_path, [entry])
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 1
    assert await _version("occ_ghost") is None


@pytest.mark.asyncio
async def test_missing_expected_version_is_refused(db_session, tmp_path):
    entry = _config_entry("occ_missing_field")
    del entry["expected_version"]
    path = _write(tmp_path, [entry])
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 1
    assert await _version("occ_missing_field") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-1, True, "1", 1.5])
async def test_malformed_expected_version_is_refused(db_session, bad):
    with pytest.raises(ShopChestConfigError):
        await ShopChestService.save_chest(
            db_session, code="occ_bad_type", name="x", price_apples=25,
            duplicate_apples=0, is_active=False, rewards=None, expected_version=bad,
        )


@pytest.mark.asyncio
async def test_malformed_json_is_rejected_without_writing(db_session, tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("[{not json", encoding="utf-8")
    assert await cli._cmd_apply(path, False, confirm_database=_db_name()) == 1
    assert "cannot read" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_lock_is_requested_only_when_a_version_is_given(db_session, monkeypatch):
    """Logic check only: SQLite has no row locks, so the race itself is untested here."""
    seen = []
    original = ShopChestService.get_by_code

    async def spy(session, code, *, lock=False):
        seen.append(lock)
        return await original(session, code, lock=lock)

    monkeypatch.setattr(ShopChestService, "get_by_code", staticmethod(spy))
    await ShopChestService.save_chest(
        db_session, code="occ_lock_a", name="a", price_apples=25,
        duplicate_apples=0, is_active=False, rewards=None,
    )
    await ShopChestService.save_chest(
        db_session, code="occ_lock_b", name="b", price_apples=25,
        duplicate_apples=0, is_active=False, rewards=None, expected_version=0,
    )
    assert seen == [False, True]


# ---------------------------------------------------------------------------
# Schema errors: a clear hint, no migration, nothing changed
# ---------------------------------------------------------------------------


def test_missing_schema_prints_the_migration_hint(monkeypatch, capsys):
    async def broken():
        raise ProgrammingError("SELECT 1", {}, Exception("no such table: shop_chests"))

    monkeypatch.setattr(cli, "_cmd_list", broken)
    assert cli.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "alembic upgrade head" in err and "Nothing was changed" in err
