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


class TestFetchLedgerMap:
    def _ingest(self, source, mtime, size):
        with db.cursor() as c:
            c.execute(
                """INSERT OR REPLACE INTO parsed_files
                   (source, path, mtime, size, ingested_at, rows_ingested, duration_s)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, 0, 0.0)""",
                [source, '/fake/' + source, mtime, size],
            )

    def test_empty_input_returns_empty_dict(self):
        assert db.fetch_ledger_map([]) == {}

    def test_unknown_sources_omitted(self):
        self._ingest('seen.log', 1000.0, 512)
        m = db.fetch_ledger_map(['seen.log', 'never.log'])
        assert set(m.keys()) == {'seen.log'}
        assert m['seen.log'][0] == 1000.0
        assert m['seen.log'][1] == 512

    def test_multiple_hits_returned_together(self):
        self._ingest('a.log', 100.0, 1)
        self._ingest('b.log', 200.0, 2)
        self._ingest('c.log', 300.0, 3)
        m = db.fetch_ledger_map(['a.log', 'b.log', 'c.log'])
        assert {s: (mt, sz) for s, (mt, sz, _) in m.items()} == {
            'a.log': (100.0, 1), 'b.log': (200.0, 2), 'c.log': (300.0, 3),
        }

    def test_matches_per_file_decision(self):
        """fetch_ledger_map + _ledger_means_current must match is_file_current."""
        db._REPARSE_COOLDOWN_S = 0
        self._ingest('match.log', 1000.0, 100)
        m = db.fetch_ledger_map(['match.log', 'missing.log'])
        assert db._ledger_means_current(m.get('match.log'), 1000.0, 100) is True
        assert db._ledger_means_current(m.get('match.log'), 1001.0, 200) is False
        assert db._ledger_means_current(m.get('missing.log'), 1.0, 1) is False
