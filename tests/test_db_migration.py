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


if __name__ == "__main__":
    unittest.main()
