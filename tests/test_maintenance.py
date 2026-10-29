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

Phase C2-5 (safe sweep): the sweep deletes the members of a party only when the
party is older than 2 hours AND not queued AND not in an active match. Those
tests run the REAL statement against real rows (it is a plain single-table
DELETE now, so SQLite can execute it as well).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import CompileError
from sqlalchemy.orm import Session

from sib.database.models import (
    Game,
    Match,
    MatchmakingQueue,
    MatchState,
    Party,
    PartyMember,
    PartyStatus,
)
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
    # crash), ONE plain single-table DELETE (its condition is a sub-SELECT, so
    # there is no multi-table DELETE..USING any more), no RETURNING and no
    # pre-SELECT.
    orm_session, executed = server_session(flavour)
    result = orm_session.execute(stmt)
    assert result.rowcount == 4
    assert len(executed) == 1
    sql = _normalised(executed[0])
    assert sql.startswith(
        "DELETE FROM party_members WHERE party_members.party_id IN "
        "(SELECT parties.id FROM parties WHERE"
    )
    assert " USING " not in sql
    assert "RETURNING" not in sql.upper()


@pytest.mark.parametrize("flavour", ["mariadb-10.11", "mariadb-11.4"])
async def test_zombie_party_cleanup_control_reproduces_the_production_error(
    server_session, flavour
):
    """Control: the multi-table DELETE the sweep USED to be raises the logged error.

    Guards the harness above - if it ever stopped reproducing the original
    crash, the "does not crash" test would pass for the wrong reason. The
    legacy statement is rebuilt here (a WHERE clause that also references
    ``parties`` makes ORM DML a multi-table ``DELETE ... USING``).
    """
    from sqlalchemy import delete, exists

    from sib.database.models import Match

    legacy = delete(PartyMember).where(
        PartyMember.party_id == Party.id,
        ~exists(select(Match.id).where(Match.party1_id == Party.id)),
    )

    orm_session, executed = server_session(flavour)
    with pytest.raises(CompileError, match=r"does not support RETURNING with DELETE\.\.USING"):
        orm_session.execute(legacy)  # ORM DML default: synchronize_session="auto"
    assert executed == []

    # ... and the C2-4.5 fix of that shape (no synchronisation) did compile.
    orm_session.execute(legacy.execution_options(synchronize_session=False))
    assert _normalised(executed[0]).startswith("DELETE FROM party_members USING party_members, parties")


# ---------------------------------------------------------------------------
# C2-5: the sweep only touches OLD parties that are idle
# ---------------------------------------------------------------------------


async def _make_party(
    db_session, *, age, status=PartyStatus.OPEN, members=1, now=None
) -> Party:
    """A real party created ``age`` ago, with ``members`` real members."""
    now = now or datetime.now(timezone.utc)
    group = await create_test_group(db_session, "گروه پاکسازی")
    creator = await create_test_user(db_session, "سازنده")
    game = await db_session.scalar(select(Game).where(Game.code == "rock_paper_scissors"))
    party = Party(
        group_id=group.id,
        game_id=game.id,
        mode="2v2",
        max_players=max(2, members),
        current_players_count=members,
        status=status,
        creator_user_id=creator.id,
        created_at=now - age,
    )
    db_session.add(party)
    await db_session.flush()
    db_session.add(PartyMember(party_id=party.id, user_id=creator.id))
    for index in range(members - 1):
        extra = await create_test_user(db_session, f"عضو {index}")
        db_session.add(PartyMember(party_id=party.id, user_id=extra.id))
    await db_session.commit()
    return party


async def _member_count(db_session, party_id: int) -> int:
    count = await db_session.scalar(
        select(func.count()).select_from(PartyMember).where(PartyMember.party_id == party_id)
    )
    await db_session.commit()  # end the read transaction: the sweep writes next
    return count


async def _attach(db_session, party: Party, kind: str) -> None:
    """Give ``party`` a queue entry or a match: ``queue:<active|inactive>`` or
    ``match:<STATE>:<1|2>`` (which side of the match the party is on)."""
    if kind.startswith("queue:"):
        db_session.add(
            MatchmakingQueue(
                party_id=party.id,
                group_id=party.group_id,
                game_id=party.game_id,
                mode=party.mode,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                is_active=kind == "queue:active",
            )
        )
    else:
        _, state, side = kind.split(":")
        other = await _make_party(db_session, age=timedelta(minutes=1))
        mine, theirs = (party, other) if side == "1" else (other, party)
        db_session.add(
            Match(
                game_id=party.game_id,
                mode=party.mode,
                state=getattr(MatchState, state),
                group1_id=mine.group_id,
                group2_id=theirs.group_id,
                party1_id=mine.id,
                party2_id=theirs.id,
            )
        )
    await db_session.commit()


async def test_zombie_cleanup_skips_new_open_parties(db_session):
    """A brand-new lobby keeps its members, however idle it looks.

    Before C2-5 the sweep also removed the members of ANY party that was not
    queued yet - i.e. every lobby still waiting for friends.
    """
    fresh = {
        "open, 10 minutes old": await _make_party(db_session, age=timedelta(minutes=10)),
        "full, 10 minutes old": await _make_party(
            db_session, age=timedelta(minutes=10), status=PartyStatus.FULL, members=2
        ),
        "open, 1 minute old": await _make_party(db_session, age=timedelta(minutes=1)),
        "open, just under the 2 hour limit": await _make_party(
            db_session, age=timedelta(hours=1, minutes=59)
        ),
    }
    before = {name: await _member_count(db_session, p.id) for name, p in fresh.items()}
    assert all(before.values()), "fixture: every party starts with members"

    deleted = await MaintenanceService.cleanup_zombie_parties(db_session)

    assert deleted == 0
    after = {name: await _member_count(db_session, p.id) for name, p in fresh.items()}
    assert after == before


async def test_zombie_cleanup_deletes_old_parties(db_session):
    """An abandoned party (3 hours old, not queued, not in a match) is swept."""
    old_open = await _make_party(db_session, age=timedelta(hours=3), members=2)
    old_full = await _make_party(
        db_session, age=timedelta(hours=5), status=PartyStatus.FULL, members=2
    )
    old_cancelled = await _make_party(
        db_session, age=timedelta(hours=3), status=PartyStatus.CANCELLED
    )
    new_open = await _make_party(db_session, age=timedelta(minutes=10), members=2)

    # No session passed: this is the path run_once() takes (its own session).
    deleted = await MaintenanceService.cleanup_zombie_parties()

    assert deleted == 5  # 2 + 2 + 1 members; the young lobby's 2 stay
    for party in (old_open, old_full, old_cancelled):
        assert await _member_count(db_session, party.id) == 0
    assert await _member_count(db_session, new_open.id) == 2
    # Only memberships are removed; the party rows themselves stay.
    assert await db_session.get(Party, old_open.id) is not None


@pytest.mark.parametrize(
    "kind, swept",
    [
        # Protected however old the party is:
        ("queue:active", False),
        ("match:ACTIVE:1", False),
        ("match:ACTIVE:2", False),
        # The window right after a match was found: the queue entry is already
        # inactive and the match is not ACTIVE yet, but the party is mid-game.
        ("match:WAITING_FOR_ACCEPTANCE:1", False),
        ("match:WAITING_FOR_ACCEPTANCE:2", False),
        ("match:OPPONENT_FOUND:1", False),
        ("match:WAITING_FOR_OPPONENT:2", False),
        # Nothing live is left behind these: the party is a zombie.
        ("queue:inactive", True),
        ("match:FINISHED:1", True),
        ("match:CANCELLED:2", True),
    ],
)
async def test_zombie_cleanup_keeps_old_parties_that_are_queued_or_in_a_match(
    db_session, kind, swept
):
    old = await _make_party(db_session, age=timedelta(hours=3), members=2)
    await _attach(db_session, old, kind)

    deleted = await MaintenanceService.cleanup_zombie_parties(db_session)

    assert await _member_count(db_session, old.id) == (0 if swept else 2)
    assert deleted == (2 if swept else 0)


async def test_zombie_cleanup_age_limit_is_exactly_two_hours(db_session):
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    on_the_limit = await _make_party(db_session, age=timedelta(hours=2), now=now)
    just_over = await _make_party(db_session, age=timedelta(hours=2, seconds=1), now=now)

    deleted = await MaintenanceService.cleanup_zombie_parties(db_session, now=now)

    assert deleted == 1
    assert await _member_count(db_session, on_the_limit.id) == 1  # not older than 2h yet
    assert await _member_count(db_session, just_over.id) == 0


async def test_maintenance_cycle_sweeps_old_parties_but_keeps_new_lobbies(db_session):
    """The sweep is wired into run_once() and really executes (on SQLite it used
    to fail inside the cycle, so no cycle test could ever see it delete)."""
    old = await _make_party(db_session, age=timedelta(hours=3))
    new = await _make_party(db_session, age=timedelta(minutes=10), status=PartyStatus.FULL)

    await MaintenanceService.run_once()

    assert await _member_count(db_session, old.id) == 0
    assert await _member_count(db_session, new.id) == 1


# ---------------------------------------------------------------------------
# run_once(): every task isolated
# ---------------------------------------------------------------------------

# Every task of one maintenance cycle, in execution order, as the
# (owner, attribute) pair the cycle calls.
def _task_table():
    from sib.services.apple_duel_presentation import AppleDuelPresentation
    from sib.services.golden_hour_announcement import GoldenHourGroupAnnouncementService
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
        "run_disease_cycle": (MaintenanceService, "run_disease_cycle"),
        "invasion_tick": (InvasionScheduler, "tick"),
        "golden_hour_group_notices": (
            GoldenHourGroupAnnouncementService,
            "run_due",
        ),
        "resume_pending_bot_turns": (MaintenanceService, "_resume_pending_bot_turns"),
        "apple_duel_recovery": (AppleDuelPresentation, "recover_and_publish"),
        "present_bot_fallbacks": (MaintenanceService, "_present_bot_fallbacks"),
        "announce_war_results": (MaintenanceService, "_announce_war_results"),
        "sync_resolved_matches": (MaintenanceService, "_sync_resolved_matches"),
        "deliver_xp_notifications": (PlayerXPService, "deliver_pending_notifications"),
    }


# Tasks that only have work when an earlier task produced it: if that earlier
# task is the one failing there is nothing to present, so they correctly skip.
_NO_INPUT_WHEN_FAILING = {
    "process_expired_queue_entries": {"present_bot_fallbacks"},
    "process_turn_timeouts": {"sync_resolved_matches"},
    # A war whose settlement failed was rolled back: it must not be announced.
    "finish_expired_wars": {"announce_war_results"},
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

    async def finish_due_wars(session, *, outcomes=None, **kwargs):
        from sib.services.war_service import WarOutcome

        if outcomes is not None:
            outcomes.append(WarOutcome(1, -1001, -1002, -1002, -1001, 150))
        return 1

    mocks = {name: AsyncMock(return_value=None) for name in _task_table()}
    mocks["process_turn_timeouts"].return_value = [7]
    mocks["process_expired_queue_entries"].side_effect = expire_queue_entries
    mocks["finish_expired_wars"].side_effect = finish_due_wars
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
        if name in {"present_bot_fallbacks", "announce_war_results", "sync_resolved_matches"}:
            continue  # no committed fallback / settled war / resolved match to present
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
