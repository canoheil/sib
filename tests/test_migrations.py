"""Tests for database migrations."""

import subprocess
from pathlib import Path


def test_alembic_current_revision():
    """Verify that Alembic migrations are at 'head'."""
    result = subprocess.run(
        ["alembic", "current"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "head" in result.stdout
