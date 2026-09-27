"""Guard against tests/migration-check scripts targeting the main DB by
accident (docs/CLAUDE_CODE_INSTRUCTIONS.md §2, incident logged in
docs/PROGRESS.md M0 audit)."""
from __future__ import annotations

import pytest

from app.core.db_safety import MainDatabaseGuardError, db_name, refuse_if_main_db
from tests.conftest import _test_db_error


def test_db_name_extracts_path_component_only():
    assert (
        db_name("postgresql+asyncpg://u:p@host:5432/agenthub_test")
        == "agenthub_test"
    )
    assert db_name("postgresql+asyncpg://u:p@host:5432/AgentHub_DB") == "AgentHub_DB"
    assert db_name("") == ""


def test_refuse_if_main_db_allows_different_names(capsys):
    refuse_if_main_db(
        "postgresql+asyncpg://u:p@host:5432/agenthub_test",
        "postgresql+asyncpg://u:p@host:5432/AgentHub_DB",
        label="test",
    )
    out = capsys.readouterr().out
    assert "agenthub_test" in out
    assert "u:p" not in out  # never prints credentials


def test_refuse_if_main_db_blocks_same_database_name():
    with pytest.raises(MainDatabaseGuardError):
        refuse_if_main_db(
            "postgresql+asyncpg://u:p@host:5432/AgentHub_DB",
            "postgresql+asyncpg://different:creds@otherhost:5433/AgentHub_DB",
            label="test",
        )


def test_conftest_guard_rejects_test_db_equal_to_main():
    """The exact misconfiguration that would make pytest's TRUNCATE wipe the
    main database: TEST_DATABASE_URL pointed at the same DB as DATABASE_URL."""
    same = "postgresql+asyncpg://u:p@h:5432/same_db"
    error = _test_db_error(same, same)
    assert error is not None
    assert "same_db" in error


def test_conftest_guard_allows_distinct_test_db():
    error = _test_db_error(
        "postgresql+asyncpg://u:p@h:5432/agenthub_test",
        "postgresql+asyncpg://u:p@h:5432/AgentHub_DB",
    )
    assert error is None


def test_conftest_guard_rejects_non_postgres_url():
    error = _test_db_error("sqlite:///./test.db", "postgresql+asyncpg://u:p@h:5432/AgentHub_DB")
    assert error is not None
    assert "postgresql+asyncpg" in error
