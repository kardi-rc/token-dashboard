"""Tests for the source-column migration in the SQLite schema."""
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db


_SOURCE = "source"
_DEFAULT_SOURCE = "claude"


class SourceColumnMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

    def _columns(self, table):
        with sqlite3.connect(self.db_path) as c:
            return {row[1] for row in c.execute(f"PRAGMA table_info({table})")}

    def _table_has_source(self, table):
        return _SOURCE in self._columns(table)

    def test_fresh_database_has_source_column_in_messages(self):
        init_db(self.db_path)
        self.assertTrue(self._table_has_source("messages"))

    def test_fresh_database_has_source_column_in_tool_calls(self):
        init_db(self.db_path)
        self.assertTrue(self._table_has_source("tool_calls"))

    def test_fresh_database_messages_source_defaults_to_claude(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """INSERT INTO messages (uuid, session_id, project_slug, type, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                ("u1", "s1", "p1", "user", "2026-01-01T00:00:00Z"),
            )
            row = c.execute("SELECT source FROM messages WHERE uuid=?", ("u1",)).fetchone()
        self.assertEqual(row[0], _DEFAULT_SOURCE)

    def test_fresh_database_tool_calls_source_defaults_to_claude(self):
        init_db(self.db_path)
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """INSERT INTO messages (uuid, session_id, project_slug, type, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                ("u1", "s1", "p1", "user", "2026-01-01T00:00:00Z"),
            )
            c.execute(
                """INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                ("u1", "s1", "p1", "Read", "2026-01-01T00:00:00Z"),
            )
            row = c.execute("SELECT source FROM tool_calls WHERE id=1").fetchone()
        self.assertEqual(row[0], _DEFAULT_SOURCE)

    def test_migration_adds_source_column_to_existing_database(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """CREATE TABLE files (
                     path TEXT PRIMARY KEY,
                     mtime REAL NOT NULL,
                     bytes_read INTEGER NOT NULL,
                     scanned_at REAL NOT NULL
                   )"""
            )
            c.execute(
                """CREATE TABLE messages (
                     uuid TEXT PRIMARY KEY,
                     session_id TEXT NOT NULL,
                     project_slug TEXT NOT NULL,
                     type TEXT NOT NULL,
                     timestamp TEXT NOT NULL,
                     input_tokens INTEGER NOT NULL DEFAULT 0,
                     output_tokens INTEGER NOT NULL DEFAULT 0,
                     cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                     cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0,
                     cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0,
                     model TEXT
                   )"""
            )
            c.execute(
                """CREATE TABLE tool_calls (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     message_uuid TEXT NOT NULL,
                     session_id TEXT NOT NULL,
                     project_slug TEXT NOT NULL,
                     tool_name TEXT NOT NULL,
                     timestamp TEXT NOT NULL,
                     target TEXT,
                     result_tokens INTEGER,
                     is_error INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("u1", "s1", "p1", "user", "2026-01-01T00:00:00Z"),
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("u1", "s1", "p1", "Read", "2026-01-01T00:00:00Z"),
            )

        self.assertFalse(self._table_has_source("messages"))
        self.assertFalse(self._table_has_source("tool_calls"))

        init_db(self.db_path)

        self.assertTrue(self._table_has_source("messages"))
        self.assertTrue(self._table_has_source("tool_calls"))

        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("u2", "s1", "p1", "user", "2026-01-01T00:00:00Z"),
            )
            c.execute(
                "INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("u2", "s1", "p1", "Read", "2026-01-01T00:00:00Z"),
            )
            msg_source = c.execute("SELECT source FROM messages WHERE uuid=?", ("u2",)).fetchone()[0]
            tool_id = c.execute("SELECT id FROM tool_calls WHERE message_uuid=?", ("u2",)).fetchone()[0]
            tool_source = c.execute("SELECT source FROM tool_calls WHERE id=?", (tool_id,)).fetchone()[0]
        self.assertEqual(msg_source, _DEFAULT_SOURCE)
        self.assertEqual(tool_source, _DEFAULT_SOURCE)

    def test_migration_is_idempotent(self):
        init_db(self.db_path)
        init_db(self.db_path)
        self.assertTrue(self._table_has_source("messages"))
        self.assertTrue(self._table_has_source("tool_calls"))


_COST_USD = "cost_usd"

# Legacy shape: current schema MINUS cost_usd. message_id and source MUST be
# present, otherwise _migrate_add_message_id adds its column and DELETES all
# rows (documented rescan behavior), silently breaking the "rows keep their
# values" assertion, or _migrate_add_source takes its own backfill path.
_LEGACY_MESSAGES_DDL = """
CREATE TABLE messages (
  uuid                    TEXT PRIMARY KEY,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  type                    TEXT NOT NULL,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  message_id              TEXT,
  source                  TEXT    NOT NULL DEFAULT 'claude'
)
"""

_LEGACY_ROW = {
    "uuid": "legacy-1",
    "session_id": "s-legacy",
    "project_slug": "proj-legacy",
    "type": "assistant",
    "timestamp": "2026-01-01T00:00:00Z",
    "model": "claude-sonnet-4",
    "input_tokens": 11,
    "output_tokens": 22,
    "cache_read_tokens": 33,
    "cache_create_5m_tokens": 44,
    "cache_create_1h_tokens": 55,
    "message_id": "msg-legacy-1",
    "source": "claude",
}


class CostUsdMigrationTests(unittest.TestCase):
    """cost_usd: fresh schema column (AC-B1) and legacy-DB migration (AC-B2)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")

    def _cost_usd_columns(self):
        """Names of cost_usd columns in messages (one entry == present once)."""
        with sqlite3.connect(self.db_path) as c:
            return [row[1] for row in c.execute("PRAGMA table_info(messages)")
                    if row[1] == _COST_USD]

    def _create_legacy_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute(_LEGACY_MESSAGES_DDL)
            c.execute(
                """INSERT INTO messages (uuid, session_id, project_slug, type,
                       timestamp, model, input_tokens, output_tokens,
                       cache_read_tokens, cache_create_5m_tokens,
                       cache_create_1h_tokens, message_id, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(_LEGACY_ROW.values()),
            )

    def test_fresh_database_has_cost_usd_column(self):
        init_db(self.db_path)
        self.assertEqual(self._cost_usd_columns(), [_COST_USD])

    def test_migration_adds_cost_usd_to_existing_database(self):
        self._create_legacy_db()

        init_db(self.db_path)

        self.assertEqual(self._cost_usd_columns(), [_COST_USD])
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            row = dict(c.execute(
                "SELECT * FROM messages WHERE uuid = ?", (_LEGACY_ROW["uuid"],)
            ).fetchone())
        # Pre-existing row keeps ALL its original values...
        for field, value in _LEGACY_ROW.items():
            self.assertEqual(row[field], value)
        # ...and its stored cost starts as NULL (computed pricing still applies).
        self.assertIsNone(row[_COST_USD])
        # A newly inserted row on the migrated DB defaults to cost_usd NULL.
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """INSERT INTO messages (uuid, session_id, project_slug, type, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                ("new-1", "s-new", "p-new", "user", "2026-01-02T00:00:00Z"),
            )
            c.commit()
            cost = c.execute(
                "SELECT cost_usd FROM messages WHERE uuid = ?", ("new-1",)
            ).fetchone()[0]
        self.assertIsNone(cost)

    def test_migration_cost_usd_is_idempotent(self):
        self._create_legacy_db()
        init_db(self.db_path)
        init_db(self.db_path)  # second run must neither raise nor re-add
        self.assertEqual(self._cost_usd_columns(), [_COST_USD])
        with sqlite3.connect(self.db_path) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM messages WHERE uuid = ?",
                (_LEGACY_ROW["uuid"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
