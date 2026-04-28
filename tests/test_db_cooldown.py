"""Unit tests for is_file_current reparse-cooldown logic."""
import os
import time
import datetime
import tempfile
import shutil
import pytest

import app.db as db


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db_path = str(tmp_path / 'test.duckdb')
    db.connect(db_path)
    yield db_path
    db.close()


@pytest.fixture(autouse=True)
def default_cooldown():
    original = db._REPARSE_COOLDOWN_S
    db._REPARSE_COOLDOWN_S = 300
    yield
    db._REPARSE_COOLDOWN_S = original


class TestIsFileCurrent:
    def _ingest(self, source, mtime, size):
        with db.cursor() as c:
            c.execute(
                """INSERT OR REPLACE INTO parsed_files
                   (source, path, mtime, size, ingested_at, rows_ingested, duration_s)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 0, 0.0)""",
                [source, '/fake/' + source, mtime, size],
            )

    def test_unknown_file_is_not_current(self):
        assert db.is_file_current('never_seen.log', 1000.0, 512) is False

    def test_exact_mtime_size_match_is_current(self):
        self._ingest('exact.log', 1234567890.0, 4096)
        assert db.is_file_current('exact.log', 1234567890.0, 4096) is True

    def test_changed_size_without_cooldown_is_not_current(self):
        db._REPARSE_COOLDOWN_S = 0
        self._ingest('grown.log', 1000.0, 100)
        assert db.is_file_current('grown.log', 1000.0, 200) is False

    def test_changed_mtime_within_cooldown_is_treated_as_current(self):
        db._REPARSE_COOLDOWN_S = 300
        self._ingest('active.log', 1000.0, 100)
        # mtime advanced, size grew — but was ingested just now, so cooldown applies
        assert db.is_file_current('active.log', 1001.0, 200) is True

    def test_changed_file_after_cooldown_expires_is_not_current(self):
        db._REPARSE_COOLDOWN_S = 300
        self._ingest('old.log', 1000.0, 100)
        # Back-date ingested_at by 10 minutes so cooldown (300 s) has clearly expired
        old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).strftime(
            '%Y-%m-%d %H:%M:%S'
        )
        with db.cursor() as c:
            c.execute(
                "UPDATE parsed_files SET ingested_at = ? WHERE source = 'old.log'",
                [old_ts],
            )
        assert db.is_file_current('old.log', 1001.0, 200) is False

    def test_cooldown_zero_disables_protection(self):
        db._REPARSE_COOLDOWN_S = 0
        self._ingest('live.log', 1000.0, 100)
        assert db.is_file_current('live.log', 1001.0, 200) is False
