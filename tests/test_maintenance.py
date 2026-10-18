"""Phase C2-4.5: maintenance crash fix and per-task failure isolation.

Production log that started this phase::

    [ERROR] [sib] Zombie party cleanup failed
    sqlalchemy.exc.CompileError: Dialect "mysql" does not support RETURNING
    with DELETE..USING; for synchronize_session='fetch', please add the
    additional execution option 'is_delete_using=True' ...

Covered here:

* ``cleanup_zombie_parties`` never asks SQLAlchemy for RETURNING on its
  multi-table DELETE (``synchronize_session=False``). The statement it builds
  is executed through the REAL ORM on the REAL MySQL/MariaDB dialect (a fake
  DBAPI connection stands in for the server, so no database is required): a
  control test reproduces the original ``CompileError`` and the fixed
  statement runs clean.
* Every ``MaintenanceService.run_once`` task is isolated: one failing task is
  logged and all the others still run, a failed task never rolls back what
  another task committed, and the worker loop survives a crashing cycle.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import CompileError
from sqlalchemy.orm import Session

from sib.database.models import Game, Party, PartyMember, PartyStatus
from sib.services.maintenance import service as maintenance_module
from sib.services.maintenance.service import MaintenanceService
from sib.tests.test_parties import create_test_group, create_test_user


# ---------------------------------------------------------------------------
# A real MySQL/MariaDB *dialect* without a server
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Just enough DBAPI cursor to record statements and report a rowcount."""

    description = None
    lastrowid = None

    def __init__(self, connection):
        self._connection = connection
        self.rowcount = -1

    def execute(self, statement, parameters=None):
        self._connection.statements.append(statement)
        self.rowcount = 4 if statement.lstrip().upper().startswith("DELETE") else -1
        return self.rowcount

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeDBAPIConnection:
    def __init__(self, statements):
        self.statements = statements

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


# (is MariaDB, server version). MariaDB reports itself through the "mysql"
# dialect, which is why production shows ``Dialect "mysql"``. Only MariaDB has
# DELETE..RETURNING, hence only MariaDB hits the multi-table restriction.
_SERVER_FLAVOURS = {
    "mariadb-10.11": (True, (10, 11, 6)),
    "mariadb-11.4": (True, (11, 4, 2)),
    "mysql-8.0": (False, (8, 0, 36)),
}


@pytest.fixture
def server_session():
    """Factory for ``(ORM session, executed SQL)`` on the real MySQL dialect."""
    engines = []
    sessions = []

    def make(flavour: str):
        is_mariadb, version = _SERVER_FLAVOURS[flavour]
        statements: list[str] = []
        engine = create_engine(
            "mysql+pymysql://user:secret@localhost/sib",
            creator=lambda: _FakeDBAPIConnection(statements),
            _initialize=False,  # skip the first-connect handshake queries
        )
        dialect = engine.dialect
        if is_mariadb:
            dialect._set_mariadb(True, version)  # what first connect does
        dialect.server_version_info = version
        dialect.default_schema_name = "sib"
        dialect._connection_charset = "utf8mb4"
        dialect.delete_returning = is_mariadb
        engines.append(engine)
        session = Session(engine)
        sessions.append(session)
        return session, statements

    yield make
    for session in sessions:
        session.close()
    for engine in engines:
        engine.dispose()


def _effective_options(execute_mock) -> dict:
    """Execution options in force for the awaited ``session.execute`` call.

    Looked up on the statement AND on the call, so the test does not care
    which of the two places a future refactor declares the option in.
    """
    call = execute_mock.await_args
    options = dict(call.args[0].get_execution_options())
    options.update(call.kwargs.get("execution_options") or {})
    return options


def _normalised(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.parametrize("flavour", list(_SERVER_FLAVOURS))
async def test_zombie_party_cleanup_does_not_crash_on_mysql(
    monkeypatch, server_session, flavour
):
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=3)),
        commit=AsyncMock(),
    )

    # Branch 1: the caller supplies the session.
    assert await MaintenanceService.cleanup_zombie_parties(session) == 3
    options = _effective_options(session.execute)
    assert options.get("synchronize_session") is False or options.get(
        "is_delete_using"
    ) is True, f"unsafe execution options for a multi-table DELETE: {options}"
    session.commit.assert_awaited_once()
    stmt = session.execute.await_args.args[0]

    # Branch 2: run_once() path - the sweep opens its own session.
    own_session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=3)),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def fake_get_session():
        yield own_session

    monkeypatch.setattr(maintenance_module, "get_session", fake_get_session)
    assert await MaintenanceService.cleanup_zombie_parties() == 3
    own_options = _effective_options(own_session.execute)
    assert own_options.get("synchronize_session") is False or own_options.get(
        "is_delete_using"
    ) is True
    own_session.commit.assert_awaited_once()

    # The statement the service really built must compile AND execute through
    # the real ORM on the MySQL/MariaDB dialect: no CompileError (that was the
    # crash), a single DELETE..USING, no RETURNING and no pre-SELECT.
    orm_session, executed = server_session(flavour)
    result = orm_session.execute(stmt)
    assert result.rowcount == 4
    assert len(executed) == 1
    sql = _normalised(executed[0])
    assert sql.startswith("DELETE FROM party_members USING party_members, parties")
    assert "RETURNING" not in sql.upper()


@pytest.mark.parametrize("flavour", ["mariadb-10.11", "mariadb-11.4"])
async def test_zombie_party_cleanup_control_reproduces_the_production_error(
    monkeypatch, server_session, flavour
):
    """Control: without the option the SAME statement raises the logged error.

    Guards the harness above - if it ever stopped reproducing the original
    crash, the "does not crash" test would pass for the wrong reason.
    """
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=0)), commit=AsyncMock()
    )
    await MaintenanceService.cleanup_zombie_parties(session)
    stmt = session.execute.await_args.args[0]
    unfixed = stmt.execution_options(synchronize_session="auto")  # the old default

    orm_session, executed = server_session(flavour)
    with pytest.raises(CompileError, match=r"does not support RETURNING with DELETE\.\.USING"):
        orm_session.execute(unfixed)
    assert executed == []


# ---------------------------------------------------------------------------
# run_once(): every task isolated
# ---------------------------------------------------------------------------

# Every task of one maintenance cycle, in execution order, as the
# (owner, attribute) pair the cycle calls.
def _task_table():
    from sib.services.apple_duel_presentation import AppleDuelPresentation
    from sib.services.invasion_v2.scheduler import InvasionScheduler
    from sib.services.match_result import MatchResultService
    from sib.services.player_xp import PlayerXPService
    from sib.services.session import SessionService
    from sib.services.timeout import TimeoutService
    from sib.services.trading import TradeService
    from sib.services.war_service import WarService

    return {
        "cleanup_zombie_parties": (MaintenanceService, "cleanup_zombie_parties"),
        "reconcile_stale_state": (SessionService, "reconcile_stale_state"),
        "expire_overdue_trades": (TradeService, "expire_overdue"),
        "process_acceptance_timeouts": (TimeoutService, "process_acceptance_timeouts"),
        "process_turn_timeouts": (TimeoutService, "process_turn_timeouts"),
        "process_expired_queue_entries": (
            MaintenanceService,
            "process_expired_queue_entries",
        ),
        "finish_expired_wars": (WarService, "finish_expired_wars"),
        "retry_pending_xp_awards": (MatchResultService, "retry_pending_xp_awards"),
        "run_marriage_love_cycle": (MaintenanceService, "run_marriage_love_cycle"),
        "invasion_tick": (InvasionScheduler, "tick"),
        "resume_pending_bot_turns": (MaintenanceService, "_resume_pending_bot_turns"),
        "apple_duel_recovery": (AppleDuelPresentation, "recover_and_publish"),
        "present_bot_fallbacks": (MaintenanceService, "_present_bot_fallbacks"),
        "sync_resolved_matches": (MaintenanceService, "_sync_resolved_matches"),
        "deliver_xp_notifications": (PlayerXPService, "deliver_pending_notifications"),
    }


# Tasks that only have work when an earlier task produced it: if that earlier
# task is the one failing there is nothing to present, so they correctly skip.
_NO_INPUT_WHEN_FAILING = {
    "process_expired_queue_entries": {"present_bot_fallbacks"},
    "process_turn_timeouts": {"sync_resolved_matches"},
}


@pytest.fixture
def maintenance_tasks(monkeypatch):
    """Replace every task of ``run_once`` with an AsyncMock, keyed by name.

    Everything is switched ON (bot present, invasion enabled, fallback match
    and resolved match available) so each task runs in a healthy cycle.
    """
    from sib.services.invasion_v2 import admin_runtime
    from sib.services.invasion_v2.presentation import InvasionPresentation

    @asynccontextmanager
    async def fake_get_session():
        yield SimpleNamespace()

    async def expire_queue_entries(session, fallbacks):
        fallbacks.append({"match_id": 1})

    mocks = {name: AsyncMock(return_value=None) for name in _task_table()}
    mocks["process_turn_timeouts"].return_value = [7]
    mocks["process_expired_queue_entries"].side_effect = expire_queue_entries
    mocks["finish_expired_wars"].return_value = 0
    mocks["retry_pending_xp_awards"].return_value = 0
    mocks["invasion_tick"].return_value = []

    for name, (owner, attribute) in _task_table().items():
        monkeypatch.setattr(owner, attribute, mocks[name])

    monkeypatch.setattr(maintenance_module, "get_session", fake_get_session)
    monkeypatch.setattr(MaintenanceService, "_bot", object())
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(InvasionPresentation, "publish_pending", AsyncMock())
    # A failing tick records itself in process-wide state: keep it hermetic.
    monkeypatch.setattr(admin_runtime, "last_error", None)
    monkeypatch.setattr(admin_runtime, "last_successful_tick", None)
    return mocks


async def test_maintenance_runs_every_task_once_when_healthy(maintenance_tasks):
    """Sanity check of the fixture the isolation test relies on."""
    await MaintenanceService.run_once()

    assert {name: mock.await_count for name, mock in maintenance_tasks.items()} == {
        name: 1 for name in maintenance_tasks
    }


@pytest.mark.parametrize("failing", list(_task_table()))
async def test_maintenance_isolates_task_failures(maintenance_tasks, caplog, failing):
    """Any single task may blow up - every other task still runs."""
    maintenance_tasks[failing].side_effect = RuntimeError(f"boom in {failing}")

    with caplog.at_level(logging.ERROR, logger="sib"):
        await MaintenanceService.run_once()  # must not raise

    skipped = _NO_INPUT_WHEN_FAILING.get(failing, set())
    for name, mock in maintenance_tasks.items():
        expected = 0 if name in skipped else 1
        assert mock.await_count == expected, (
            f"{name} ran {mock.await_count}x (expected {expected}) "
            f"after {failing} failed"
        )
    # The failure is logged (with its traceback), not swallowed silently.
    assert any(
        record.levelno >= logging.ERROR
        and record.exc_info
        and f"boom in {failing}" in str(record.exc_info[1])
        for record in caplog.records
    ), f"no logged traceback for the failing task {failing}"


async def test_maintenance_survives_every_task_failing_at_once(maintenance_tasks):
    """Worst case: nothing works - the cycle still completes and returns."""
    for mock in maintenance_tasks.values():
        mock.side_effect = RuntimeError("everything is down")

    await MaintenanceService.run_once()

    for name, mock in maintenance_tasks.items():
        if name in {"present_bot_fallbacks", "sync_resolved_matches"}:
            continue  # no committed fallback / resolved match to present
        assert mock.await_count == 1, name


async def test_failed_task_does_not_roll_back_work_committed_by_earlier_tasks(
    db_session, monkeypatch
):
    """Each DB task has its own transaction.

    Stale-state reconciliation releases a party that is stuck IN_MATCH
    without a match. The matchmaking-expiry task that runs AFTER it then
    crashes: that failure must not undo the reconciliation (it used to,
    because every DB task shared one transaction).
    """
    group = await create_test_group(db_session, "گروه ایزوله")
    user = await create_test_user(db_session, "بازیکن ایزوله")
    game = await db_session.scalar(select(Game).where(Game.code == "rock_paper_scissors"))
    party = Party(
        group_id=group.id,
        game_id=game.id,
        mode="solo",
        max_players=1,
        current_players_count=1,
        status=PartyStatus.IN_MATCH,  # but no match exists: a zombie
        creator_user_id=user.id,
    )
    db_session.add(party)
    await db_session.flush()
    db_session.add(PartyMember(party_id=party.id, user_id=user.id))
    await db_session.commit()
    party_id = party.id

    monkeypatch.setattr(
        MaintenanceService,
        "process_expired_queue_entries",
        AsyncMock(side_effect=RuntimeError("queue task crashed")),
    )
    await MaintenanceService.run_once()

    db_session.expunge_all()
    refreshed = await db_session.get(Party, party_id)
    assert refreshed.status == PartyStatus.FULL


# ---------------------------------------------------------------------------
# run_forever(): the worker loop itself
# ---------------------------------------------------------------------------


async def test_run_forever_survives_a_crashing_cycle(monkeypatch, caplog):
    """bot.py never awaits the worker task, so one escaped error used to end
    ALL maintenance (war finalization included) until the next deploy."""
    stop = asyncio.Event()
    cycles = 0

    async def flaky_cycle():
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            raise RuntimeError("escaped every per-task guard")
        stop.set()

    monkeypatch.setattr(MaintenanceService, "run_once", AsyncMock(side_effect=flaky_cycle))
    monkeypatch.setattr(MaintenanceService, "_invasion_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(MaintenanceService, "_bot", None)  # run_forever assigns it
    monkeypatch.setattr(MaintenanceService, "INTERVAL_SECONDS", 0.01)

    with caplog.at_level(logging.ERROR, logger="sib"):
        await asyncio.wait_for(MaintenanceService.run_forever(stop), timeout=5)

    assert cycles == 2  # the loop kept going after the crash
    assert "Maintenance cycle crashed" in caplog.text
