"""Tests for database migrations.

The migration DDL itself targets MariaDB (ALTER TABLE constraints),
which SQLite cannot execute; the chain is therefore validated against a
standalone stamped database instead of a live `upgrade head` run.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(*args: str, db_url: str) -> subprocess.CompletedProcess:
    """Run an alembic command against ``db_url`` (never the real database)."""
    env = os.environ.copy()
    env["DATABASE_SYNC_URL"] = db_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def test_migration_chain_has_single_head():
    """There must be exactly one head revision (no forked history)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'heads_check.db'}"
        result = _run_alembic("heads", db_url=db_url)
    assert result.returncode == 0
    heads = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1
    assert "(head)" in heads[0]


def test_alembic_current_revision():
    """A freshly stamped database reports the chain's head revision."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{Path(tmp) / 'current_check.db'}"
        stamp = _run_alembic("stamp", "head", db_url=db_url)
        assert stamp.returncode == 0

        current = _run_alembic("current", db_url=db_url)
    assert current.returncode == 0
    assert "head" in current.stdout
