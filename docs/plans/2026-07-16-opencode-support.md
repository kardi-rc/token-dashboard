# opencode Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opencode SQLite backend support to token-dashboard while maintaining full Claude Code backward compatibility.

**Architecture:** Adapter pattern — keep `scanner.py` (Claude JSONL) untouched, add `opencode_source.py` (opencode SQLite) that populates the same internal `messages`/`tool_calls` tables. Auto-detection determines which backend(s) to run.

**Tech Stack:** Python 3.8+ stdlib only, SQLite, unittest, vanilla JS frontend (unchanged).

---

### Task 1: DB migration — add `source` column

**Files:**
- Modify: `token_dashboard/db.py`
- Test: `tests/test_db_migration.py` (new)

We need to tag every internal row as coming from `claude` or `opencode` so the dashboard can show/hide/filter sources and so future multi-source imports can update only their own rows. The safest way is a `source TEXT DEFAULT 'claude'` column on `messages` and `tool_calls`, plus a migration that backfills existing databases.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_migration.py`:

```python
import os
import sqlite3
import tempfile
import unittest

from token_dashboard.db import init_db


class TestSourceMigration(unittest.TestCase):
    def test_fresh_db_has_source_columns_defaulting_to_claude(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "fresh.db")
        conn = sqlite3.connect(db_path)
        init_db(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("source", cols)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
        self.assertIn("source", cols)

        conn.execute(
            """
            INSERT INTO messages (
                uuid, parent_uuid, session_id, project_slug, cwd, git_branch,
                cc_version, entrypoint, type, is_sidechain, agent_id, timestamp,
                model, stop_reason, prompt_id, message_id, input_tokens,
                output_tokens, cache_read_tokens, cache_create_5m_tokens,
                cache_create_1h_tokens, prompt_text, prompt_chars, tool_calls_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "msg-1", None, "sess-1", "slug", "/tmp", "main", "0.1",
                "cli", "assistant", 0, "agent-1", "2026-07-16T12:00:00.000Z",
                "claude-sonnet-4", "end_turn", "p1", "m1", 100, 50,
                0, 0, 0, "hello", 5, None,
            ),
        )
        conn.commit()
        source = conn.execute("SELECT source FROM messages").fetchone()[0]
        self.assertEqual(source, "claude")

    def test_existing_db_without_source_column_is_migrated(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "legacy.db")
        conn = sqlite3.connect(db_path)

        # Simulate an old schema without `source`
        conn.executescript(
            """
            CREATE TABLE messages (
                uuid TEXT PRIMARY KEY,
                parent_uuid TEXT,
                session_id TEXT,
                project_slug TEXT,
                cwd TEXT,
                git_branch TEXT,
                cc_version TEXT,
                entrypoint TEXT,
                type TEXT,
                is_sidechain INTEGER,
                agent_id TEXT,
                timestamp TEXT,
                model TEXT,
                stop_reason TEXT,
                prompt_id TEXT,
                message_id TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_create_5m_tokens INTEGER,
                cache_create_1h_tokens INTEGER,
                prompt_text TEXT,
                prompt_chars INTEGER,
                tool_calls_json TEXT
            );
            CREATE TABLE tool_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_uuid TEXT,
                session_id TEXT,
                project_slug TEXT,
                tool_name TEXT,
                target TEXT,
                result_tokens INTEGER,
                is_error INTEGER,
                timestamp TEXT
            );
            INSERT INTO messages (uuid, session_id, project_slug, timestamp, model)
            VALUES ('msg-old', 'sess-old', 'slug', '2026-07-15T00:00:00Z', 'claude');
            """
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db_path)
        init_db(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        self.assertIn("source", cols)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
        self.assertIn("source", cols)

        rows = conn.execute("SELECT source FROM messages").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "claude")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing test**

```bash
python3 -m unittest tests.test_db_migration -v
```

Expected: `FAIL` with `AssertionError: 'source' not found in column set` (or `OperationalError` if init_db re-creates tables and the test reads before the migration).

- [ ] **Step 3: Add the migration function to `token_dashboard/db.py`**

Insert this helper near the existing `_migrate_add_message_id` function:

```python
def _migrate_add_source(conn: sqlite3.Connection) -> None:
    """Add source column to messages and tool_calls if missing."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source TEXT DEFAULT 'claude'")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE tool_calls ADD COLUMN source TEXT DEFAULT 'claude'")
```

- [ ] **Step 4: Update the `SCHEMA` constant in `token_dashboard/db.py`**

Add `source TEXT DEFAULT 'claude'` near the end of the `messages` and `tool_calls` CREATE TABLE statements. For example, in the `messages` table add it after `tool_calls_json`, and in `tool_calls` add it after `timestamp`.

The `messages` table creation line should end:

```sql
                tool_calls_json TEXT,
                source TEXT DEFAULT 'claude'
```

The `tool_calls` table creation line should end:

```sql
                timestamp TEXT,
                source TEXT DEFAULT 'claude'
```

- [ ] **Step 5: Wire the migration into `init_db`**

Update `init_db` in `token_dashboard/db.py` to call `_migrate_add_source` after `_migrate_add_message_id`:

```python
def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and run idempotent migrations."""
    _migrate_add_message_id(conn)
    _migrate_add_source(conn)
    conn.executescript(SCHEMA)
    conn.commit()
```

- [ ] **Step 6: Run the migration test**

```bash
python3 -m unittest tests.test_db_migration -v
```

Expected: two tests, both PASS.

- [ ] **Step 7: Run the full test suite**

```bash
python3 -m unittest discover tests -v
```

Expected: all 68+ existing tests still pass (the new `source` default should not affect scanner tests).

- [ ] **Step 8: Commit**

```bash
git add token_dashboard/db.py tests/test_db_migration.py
git commit -m "feat(db): add source column to messages and tool_calls with migration"
```

---

### Task 2: opencode_source.py — core adapter (session + message import)

**Files:**
- Create: `token_dashboard/opencode_source.py`
- Test: `tests/test_opencode_source.py` (new)

This module is the adapter between the opencode SQLite database and the internal token-dashboard schema. It reads opencode's `session`, `message`, and `part` tables and upserts rows into `messages` with `source='opencode'`. Keeping this in its own file preserves the boundary with `scanner.py`, which stays Claude-specific.

- [ ] **Step 1: Write the failing test fixture**

Create `tests/test_opencode_source.py` with a helper that builds a fake opencode.db:

```python
import json
import os
import sqlite3
import tempfile
import unittest

from token_dashboard import opencode_source


class FakeOpencodeDb:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                directory TEXT,
                parent_session_id TEXT,
                time_created INTEGER,
                data TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                sessionID TEXT,
                parentID TEXT,
                agentID TEXT,
                role TEXT,
                time_created INTEGER,
                data TEXT,
                FOREIGN KEY (sessionID) REFERENCES session(id)
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                messageID TEXT,
                type TEXT,
                time_created INTEGER,
                data TEXT,
                FOREIGN KEY (messageID) REFERENCES message(id)
            );
            """
        )
        self.conn.commit()

    def add_session(self, session_id, directory, time_created, parent=None, data=None):
        self.conn.execute(
            "INSERT INTO session (id, directory, parent_session_id, time_created, data) VALUES (?, ?, ?, ?, ?)",
            (session_id, directory, parent, time_created, json.dumps(data or {})),
        )

    def add_message(self, msg_id, session_id, role, time_created, parent=None, agent=None, data=None):
        self.conn.execute(
            "INSERT INTO message (id, sessionID, parentID, agentID, role, time_created, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, parent, agent, role, time_created, json.dumps(data or {})),
        )

    def add_part(self, part_id, message_id, part_type, time_created, data):
        self.conn.execute(
            "INSERT INTO part (id, messageID, type, time_created, data) VALUES (?, ?, ?, ?, ?)",
            (part_id, message_id, part_type, time_created, json.dumps(data)),
        )

    def close(self):
        self.conn.commit()
        self.conn.close()


class TestOpencodeSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.oc_path = os.path.join(self.tmp, "opencode.db")
        self.internal_path = os.path.join(self.tmp, "internal.db")

    def test_import_opencode_basic(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/home/user/proj", 1784212776000)
        fake.add_message(
            "msg-1", "sess-1", "user", 1784212776000,
            data={"tokens": 123},
        )
        fake.add_part(
            "part-1", "msg-1", "text", 1784212776000,
            data={"text": "explain this code"},
        )
        fake.close()

        result = opencode_source.import_opencode(self.oc_path, self.internal_path)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["messages"], 1)

        internal = sqlite3.connect(self.internal_path)
        row = internal.execute("SELECT * FROM messages").fetchone()
        self.assertIsNotNone(row)

        # source should be opencode
        source = internal.execute("SELECT source FROM messages").fetchone()[0]
        self.assertEqual(source, "opencode")

        # project_slug derived from directory
        slug = internal.execute("SELECT project_slug FROM messages").fetchone()[0]
        self.assertEqual(slug, "home-user-proj")

    def test_import_opencode_is_incremental(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/home/user/proj", 1784212776000)
        fake.add_message(
            "msg-1", "sess-1", "user", 1784212776000,
            data={"tokens": 123},
        )
        fake.add_part(
            "part-1", "msg-1", "text", 1784212776000,
            data={"text": "first"},
        )
        fake.close()

        result1 = opencode_source.import_opencode(self.oc_path, self.internal_path)
        self.assertEqual(result1["messages"], 1)

        # Add a newer message directly to the same opencode db
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_message(
            "msg-2", "sess-1", "assistant", 1784212777000,
            data={"tokens": 456},
        )
        fake.add_part(
            "part-2", "msg-2", "text", 1784212777000,
            data={"text": "second"},
        )
        fake.close()

        result2 = opencode_source.import_opencode(self.oc_path, self.internal_path)
        self.assertEqual(result2["messages"], 1)
        internal = sqlite3.connect(self.internal_path)
        count = internal.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(count, 2)

    def test_prompt_text_extracted_from_text_part(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/tmp/foo", 1784212776000)
        fake.add_message(
            "msg-1", "sess-1", "user", 1784212776000,
        )
        fake.add_part(
            "part-1", "msg-1", "text", 1784212776000,
            data={"text": "what is recursion?"},
        )
        fake.close()

        opencode_source.import_opencode(self.oc_path, self.internal_path)
        internal = sqlite3.connect(self.internal_path)
        prompt_text = internal.execute("SELECT prompt_text FROM messages").fetchone()[0]
        self.assertEqual(prompt_text, "what is recursion?")

    def test_token_extraction_from_assistant_message(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/tmp/foo", 1784212776000)
        fake.add_message(
            "msg-1", "sess-1", "assistant", 1784212776000,
            data={
                "tokens": {
                    "input": 100,
                    "output": 50,
                    "cacheRead": 10,
                    "cacheCreate5m": 20,
                    "cacheCreate1h": 30,
                },
                "modelID": "claude-sonnet-4-20250514",
                "parentID": "msg-0",
                "agent": {"id": "agent-7"},
            },
        )
        fake.add_part(
            "part-1", "msg-1", "text", 1784212776000,
            data={"text": "recursion is..."},
        )
        fake.close()

        opencode_source.import_opencode(self.oc_path, self.internal_path)
        internal = sqlite3.connect(self.internal_path)
        row = internal.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, "
            "cache_create_5m_tokens, cache_create_1h_tokens, model, agent_id, "
            "parent_uuid, prompt_text FROM messages"
        ).fetchone()
        self.assertEqual(row[0], 100)
        self.assertEqual(row[1], 50)
        self.assertEqual(row[2], 10)
        self.assertEqual(row[3], 20)
        self.assertEqual(row[4], 30)
        self.assertEqual(row[5], "claude-sonnet-4-20250514")
        self.assertEqual(row[6], "agent-7")
        self.assertEqual(row[7], "msg-0")
        self.assertEqual(row[8], "recursion is...")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the failing test**

```bash
python3 -m unittest tests.test_opencode_source -v
```

Expected: `FAIL` / `ModuleNotFoundError: No module named 'token_dashboard.opencode_source'` (or `AttributeError` once the module exists but functions are empty).

- [ ] **Step 3: Create `token_dashboard/opencode_source.py`**

```python
"""Adapter that imports opencode SQLite data into token-dashboard's internal schema."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from token_dashboard.db import _encode_slug, init_db


def default_opencode_db_path() -> Path:
    """Return the default opencode SQLite database path."""
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _project_slug(directory: str) -> str:
    """Encode a directory path into the same slug format used for Claude projects."""
    return _encode_slug(directory)


def _parse_message_data(data_json: str) -> Dict[str, Any]:
    """Extract role, tokens, modelID, parentID, and agent from message.data JSON."""
    if not data_json:
        return {}
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return {}

    parsed: Dict[str, Any] = {}
    if "tokens" in data:
        tokens = data["tokens"]
        if isinstance(tokens, dict):
            parsed["input_tokens"] = tokens.get("input") or 0
            parsed["output_tokens"] = tokens.get("output") or 0
            parsed["cache_read_tokens"] = tokens.get("cacheRead") or 0
            parsed["cache_create_5m_tokens"] = tokens.get("cacheCreate5m") or 0
            parsed["cache_create_1h_tokens"] = tokens.get("cacheCreate1h") or 0
        else:
            parsed["input_tokens"] = 0
            parsed["output_tokens"] = 0
            parsed["cache_read_tokens"] = 0
            parsed["cache_create_5m_tokens"] = 0
            parsed["cache_create_1h_tokens"] = 0
    parsed["model"] = data.get("modelID", "")
    parsed["parent_uuid"] = data.get("parentID", "")
    parsed["agent_id"] = (data.get("agent") or {}).get("id", "")
    return parsed


def _format_timestamp(epoch_ms: int) -> str:
    """Convert opencode epoch milliseconds to an ISO 8601 UTC string."""
    dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat()


INSERT_OPENCODE_MSG = """
INSERT OR REPLACE INTO messages (
    uuid, parent_uuid, session_id, project_slug, cwd, git_branch,
    cc_version, entrypoint, type, is_sidechain, agent_id, timestamp,
    model, stop_reason, prompt_id, message_id, input_tokens,
    output_tokens, cache_read_tokens, cache_create_5m_tokens,
    cache_create_1h_tokens, prompt_text, prompt_chars, tool_calls_json,
    source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _import_sessions(oc_conn: sqlite3.Connection, internal_conn: sqlite3.Connection) -> int:
    """Read all sessions from opencode.db and return the count."""
    sessions = oc_conn.execute(
        "SELECT id, directory, parent_session_id, time_created, data FROM session"
    ).fetchall()
    return len(sessions)


def _fetch_prompt_text(oc_conn: sqlite3.Connection, message_id: str, role: str) -> str:
    """For user messages, return the concatenated text part content."""
    if role != "user":
        return ""
    rows = oc_conn.execute(
        """
        SELECT p.data
        FROM part p
        WHERE p.messageID = ? AND p.type = 'text'
        ORDER BY p.time_created ASC
        """,
        (message_id,),
    ).fetchall()
    parts = []
    for (raw,) in rows:
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        text = data.get("text", "")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _import_messages(
    oc_conn: sqlite3.Connection,
    internal_conn: sqlite3.Connection,
    since_ts: int,
) -> Dict[str, int]:
    """Import messages created after `since_ts` and upsert into internal messages table."""
    rows = oc_conn.execute(
        """
        SELECT m.id, m.sessionID, m.parentID, m.agentID, m.role, m.time_created, m.data,
               s.directory
        FROM message m
        JOIN session s ON m.sessionID = s.id
        WHERE m.time_created > ?
        ORDER BY m.time_created ASC
        """,
        (since_ts,),
    ).fetchall()

    imported = 0
    for row in rows:
        (
            msg_id,
            session_id,
            parent_id,
            agent_id,
            role,
            time_created,
            data_json,
            directory,
        ) = row

        parsed = _parse_message_data(data_json)
        prompt_text = _fetch_prompt_text(oc_conn, msg_id, role)
        prompt_chars = len(prompt_text) if prompt_text else 0
        slug = _project_slug(directory) if directory else ""

        values = (
            msg_id,
            parsed.get("parent_uuid") or parent_id or "",
            session_id,
            slug,
            directory or "",
            "",  # git_branch
            "",  # cc_version
            "",  # entrypoint
            role,
            0,   # is_sidechain
            parsed.get("agent_id") or agent_id or "",
            _format_timestamp(time_created),
            parsed.get("model", ""),
            "",  # stop_reason
            "",  # prompt_id
            msg_id,  # message_id
            parsed.get("input_tokens", 0),
            parsed.get("output_tokens", 0),
            parsed.get("cache_read_tokens", 0),
            parsed.get("cache_create_5m_tokens", 0),
            parsed.get("cache_create_1h_tokens", 0),
            prompt_text,
            prompt_chars,
            None,  # tool_calls_json
            "opencode",
        )
        internal_conn.execute(INSERT_OPENCODE_MSG, values)
        imported += 1

    return {"messages": imported}


def _latest_message_ts(internal_conn: sqlite3.Connection) -> int:
    """Return the largest imported opencode message timestamp (epoch ms), or 0."""
    row = internal_conn.execute(
        "SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) FROM messages WHERE source = 'opencode'"
    ).fetchone()
    if row and row[0]:
        return row[0] * 1000
    return 0


def import_opencode(opencode_db_path: str, internal_db_path: str) -> Dict[str, int]:
    """Main entry point: import opencode sessions/messages into the internal db."""
    oc_conn = sqlite3.connect(opencode_db_path)
    internal_conn = sqlite3.connect(internal_db_path)
    try:
        init_db(internal_conn)
        since_ts = _latest_message_ts(internal_conn)
        session_count = _import_sessions(oc_conn, internal_conn)
        msg_result = _import_messages(oc_conn, internal_conn, since_ts)
        internal_conn.commit()
        return {"sessions": session_count, **msg_result}
    finally:
        oc_conn.close()
        internal_conn.close()
```

- [ ] **Step 4: Run the tests**

```bash
python3 -m unittest tests.test_opencode_source -v
```

Expected: all four tests PASS.

- [ ] **Step 5: Run the full test suite**

```bash
python3 -m unittest discover tests -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add token_dashboard/opencode_source.py tests/test_opencode_source.py
git commit -m "feat(opencode): adapter for session and message import"
```

---

### Task 3: opencode_source.py — tool calls extraction

**Files:**
- Modify: `token_dashboard/opencode_source.py`
- Test: `tests/test_opencode_source.py` (append tests)

opencode stores tool invocations as `part` rows with `type='tool'`. We extract the tool name, target (file path, command, pattern, etc.), whether it errored, and an estimated token count from the output length so the dashboard heatmaps and cost drill-downs work for opencode sessions the same way they do for Claude sessions.

- [ ] **Step 1: Append failing tests to `tests/test_opencode_source.py`**

Add these test methods to the `TestOpencodeSource` class (before `if __name__ == '__main__'`):

```python
    def test_extract_tool_target(self):
        self.assertEqual(
            opencode_source._extract_tool_target("bash", {"command": "ls -la"}),
            "ls -la",
        )
        self.assertEqual(
            opencode_source._extract_tool_target("read", {"file_path": "/tmp/foo.py"}),
            "/tmp/foo.py",
        )
        self.assertEqual(
            opencode_source._extract_tool_target("task", {"subagent_type": "coder"}),
            "coder",
        )
        self.assertIsNone(
            opencode_source._extract_tool_target("todowrite", {}),
        )
        self.assertEqual(
            opencode_source._extract_tool_target("question", {"header": "Continue?"}),
            "Continue?",
        )

    def test_import_tool_calls_basic(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/tmp/foo", 1784212776000)
        fake.add_message("msg-1", "sess-1", "assistant", 1784212776000)
        fake.add_part(
            "part-tool-1", "msg-1", "tool", 1784212776001,
            data={
                "type": "tool",
                "tool": "bash",
                "callID": "call_abc",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls -la"},
                    "output": "total 0\n",
                    "time": {"start": 1784212776272},
                },
            },
        )
        fake.close()

        opencode_source.import_opencode(self.oc_path, self.internal_path)
        internal = sqlite3.connect(self.internal_path)
        row = internal.execute(
            "SELECT message_uuid, session_id, project_slug, tool_name, target, "
            "result_tokens, is_error, source FROM tool_calls"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "msg-1")
        self.assertEqual(row[1], "sess-1")
        self.assertEqual(row[2], "tmp-foo")
        self.assertEqual(row[3], "bash")
        self.assertEqual(row[4], "ls -la")
        self.assertEqual(row[6], 0)
        self.assertEqual(row[7], "opencode")

    def test_tool_call_error_status(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/tmp/foo", 1784212776000)
        fake.add_message("msg-1", "sess-1", "assistant", 1784212776000)
        fake.add_part(
            "part-tool-1", "msg-1", "tool", 1784212776001,
            data={
                "type": "tool",
                "tool": "bash",
                "callID": "call_err",
                "state": {
                    "status": "failed",
                    "input": {"command": "exit 1"},
                    "output": "",
                    "error": "command failed",
                    "time": {"start": 1784212776272},
                },
            },
        )
        fake.close()

        opencode_source.import_opencode(self.oc_path, self.internal_path)
        internal = sqlite3.connect(self.internal_path)
        is_error = internal.execute("SELECT is_error FROM tool_calls").fetchone()[0]
        self.assertEqual(is_error, 1)

    def test_result_tokens_estimation(self):
        fake = FakeOpencodeDb(self.oc_path)
        fake.add_session("sess-1", "/tmp/foo", 1784212776000)
        fake.add_message("msg-1", "sess-1", "assistant", 1784212776000)
        fake.add_part(
            "part-tool-1", "msg-1", "tool", 1784212776001,
            data={
                "type": "tool",
                "tool": "bash",
                "callID": "call_big",
                "state": {
                    "status": "completed",
                    "input": {"command": "python -c 'print(\"x\"*400)'"},
                    "output": "x" * 400,
                    "time": {"start": 1784212776272},
                },
            },
        )
        fake.close()

        opencode_source.import_opencode(self.oc_path, self.internal_path)
        internal = sqlite3.connect(self.internal_path)
        result_tokens = internal.execute("SELECT result_tokens FROM tool_calls").fetchone()[0]
        self.assertEqual(result_tokens, 100)
```

- [ ] **Step 2: Run the failing tests**

```bash
python3 -m unittest tests.test_opencode_source -v
```

Expected: `AttributeError: module 'token_dashboard.opencode_source' has no attribute '_extract_tool_target'` and related failures.

- [ ] **Step 3: Implement tool extraction in `token_dashboard/opencode_source.py`**

Add these imports at the top of the file (if not already present):

```python
from typing import Any, Dict, Optional
```

Append these functions and update `_import_messages` / `import_opencode` to call `_import_tool_calls`.

Add helper:

```python
_TOOL_TARGET_FIELDS = {
    "bash": "command",
    "read": "file_path",
    "edit": "file_path",
    "write": "file_path",
    "glob": "pattern",
    "grep": "pattern",
    "task": "subagent_type",
    "skill": "name",
    "webfetch": "url",
    "question": "header",
}


def _extract_tool_target(tool_name: str, state_input: Dict[str, Any]) -> Optional[str]:
    """Return the human-readable target for a tool call based on its name and input."""
    if tool_name == "todowrite":
        return None
    field = _TOOL_TARGET_FIELDS.get(tool_name, "")
    if field:
        return state_input.get(field)
    return None
```

Add `INSERT_OPENCODE_TOOL` near `INSERT_OPENCODE_MSG`:

```python
INSERT_OPENCODE_TOOL = """
INSERT OR REPLACE INTO tool_calls (
    id, message_uuid, session_id, project_slug, tool_name, target,
    result_tokens, is_error, timestamp, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
```

Add `_import_tool_calls`:

```python
def _import_tool_calls(
    oc_conn: sqlite3.Connection,
    internal_conn: sqlite3.Connection,
    since_ts: int,
) -> Dict[str, int]:
    """Import tool parts created after `since_ts` into internal tool_calls table."""
    rows = oc_conn.execute(
        """
        SELECT p.id, p.messageID, p.data, m.sessionID, s.directory, p.time_created
        FROM part p
        JOIN message m ON p.messageID = m.id
        JOIN session s ON m.sessionID = s.id
        WHERE p.type = 'tool' AND p.time_created > ?
        ORDER BY p.time_created ASC
        """,
        (since_ts,),
    ).fetchall()

    imported = 0
    for part_id, message_id, data_json, session_id, directory, time_created in rows:
        try:
            data = json.loads(data_json) if data_json else {}
        except json.JSONDecodeError:
            data = {}

        tool_name = data.get("tool", "")
        state = data.get("state") or {}
        state_input = state.get("input") or {}
        output = state.get("output") or ""
        status = state.get("status", "")
        error = state.get("error")

        target = _extract_tool_target(tool_name, state_input)
        result_tokens = len(output) // 4 if output else 0
        is_error = 1 if (status != "completed" or error) else 0

        slug = _project_slug(directory) if directory else ""

        internal_conn.execute(
            INSERT_OPENCODE_TOOL,
            (
                part_id,
                message_id,
                session_id,
                slug,
                tool_name,
                target,
                result_tokens,
                is_error,
                _format_timestamp(time_created),
                "opencode",
            ),
        )
        imported += 1

    return {"tool_calls": imported}
```

Update `_latest_message_ts` to `_latest_import_ts` so it also considers tool parts:

```python
def _latest_import_ts(internal_conn: sqlite3.Connection) -> int:
    """Return the largest imported opencode timestamp (epoch ms), or 0."""
    row = internal_conn.execute(
        "SELECT MAX(ts) FROM ("
        "  SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) * 1000 AS ts "
        "  FROM messages WHERE source = 'opencode' "
        "  UNION ALL "
        "  SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) * 1000 AS ts "
        "  FROM tool_calls WHERE source = 'opencode'"
        ")"
    ).fetchone()
    if row and row[0]:
        return int(row[0])
    return 0
```

Replace the old `_latest_message_ts` usage with `_latest_import_ts`.

Update `import_opencode`:

```python
def import_opencode(opencode_db_path: str, internal_db_path: str) -> Dict[str, int]:
    """Main entry point: import opencode sessions/messages/tools into the internal db."""
    oc_conn = sqlite3.connect(opencode_db_path)
    internal_conn = sqlite3.connect(internal_db_path)
    try:
        init_db(internal_conn)
        since_ts = _latest_import_ts(internal_conn)
        session_count = _import_sessions(oc_conn, internal_conn)
        msg_result = _import_messages(oc_conn, internal_conn, since_ts)
        tool_result = _import_tool_calls(oc_conn, internal_conn, since_ts)
        internal_conn.commit()
        return {
            "sessions": session_count,
            "messages": msg_result.get("messages", 0),
            "tool_calls": tool_result.get("tool_calls", 0),
        }
    finally:
        oc_conn.close()
        internal_conn.close()
```

- [ ] **Step 4: Run the tests**

```bash
python3 -m unittest tests.test_opencode_source -v
```

Expected: all eight tests PASS.

- [ ] **Step 5: Run the full test suite**

```bash
python3 -m unittest discover tests -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add token_dashboard/opencode_source.py tests/test_opencode_source.py
git commit -m "feat(opencode): extract tool calls and result tokens from opencode parts"
```

---

## Self-Review

**Spec coverage:**
- DB `source` column with default `'claude'` → Task 1.
- opencode adapter module with project slug reuse, timestamp conversion, message import, prompt text extraction, token mapping → Task 2.
- Tool call target extraction and result token estimation → Task 3.
- Stdlib-only Python, unittest, exact file paths, exact commands, checkbox syntax → covered throughout.

**Placeholder scan:** No TODOs, TBDs, or hand-wavy steps. Every step shows exact code or exact commands.

**Type consistency:** `_encode_slug` is imported from `token_dashboard.db` and reused in `_project_slug`. `_parse_message_data` consistently returns a dict with the same token keys. `_import_tool_calls` and `_import_messages` both use `_format_timestamp`. `import_opencode` returns a dict with keys `sessions`, `messages`, `tool_calls`.

---

### Task 4: Skills catalog — add opencode roots

**Files:**
- Modify: `token_dashboard/skills.py` (add opencode roots to `_DEFAULT_ROOTS`)
- Test: `tests/test_skills_opencode.py` (new)

Current `_DEFAULT_ROOTS` in `skills.py`:

```python
_DEFAULT_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "scheduled-tasks",
    Path.home() / ".claude" / "plugins",
]
```

Add two new roots:

```python
    Path.home() / ".config" / "opencode" / "skill",
    Path.home() / ".agents" / "skills",
```

### Step 4.1 — Write failing test

Create `tests/test_skills_opencode.py`:

```python
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from token_dashboard.skills import scan_catalog, _DEFAULT_ROOTS


class OpencodeSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_opencode_config_skill_root_scanned(self):
        # Create skill under the opencode config path
        skill = self.tmp / ".config" / "opencode" / "skill" / "my-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("x" * 400, encoding="utf-8")
        # Monkeypatch Path.home so _DEFAULT_ROOTS resolves to temp
        with patch.object(Path, "home", return_value=self.tmp):
            cat = scan_catalog()
        self.assertIn("my-skill", cat)
        self.assertEqual(cat["my-skill"]["chars"], 400)

    def test_agents_skills_root_scanned(self):
        skill = self.tmp / ".agents" / "skills" / "another-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("y" * 200, encoding="utf-8")
        with patch.object(Path, "home", return_value=self.tmp):
            cat = scan_catalog()
        self.assertIn("another-skill", cat)
        self.assertEqual(cat["another-skill"]["tokens"], 50)
```

```bash
python3 -m unittest tests.test_skills_opencode -v
```

Expected: tests fail (skills not found because opencode roots are not yet in `_DEFAULT_ROOTS`).

### Step 4.2 — Modify `_DEFAULT_ROOTS`

Edit `token_dashboard/skills.py`:

```python
_DEFAULT_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".claude" / "scheduled-tasks",
    Path.home() / ".claude" / "plugins",
    Path.home() / ".config" / "opencode" / "skill",
    Path.home() / ".agents" / "skills",
]
```

### Step 4.3 — Run new tests

```bash
python3 -m unittest tests.test_skills_opencode -v
```

Expected: both tests pass.

### Step 4.4 — Run full test suite

```bash
python3 -m unittest discover tests -v
```

Expected: all tests pass.

### Step 4.5 — Commit

```bash
git add token_dashboard/skills.py tests/test_skills_opencode.py
git commit -m "feat(skills): scan opencode skill roots (.config/opencode/skill, .agents/skills)"
```

---

### Task 5: CLI — add `--backend` flag

**Files:**
- Modify: `cli.py` (add `--backend` flag and backend dispatch logic)
- Test: `tests/test_cli.py` (modify to test backend flag)

### Step 5.1 — Write failing tests

Append to `tests/test_cli.py`:

```python
class CliBackendTests(unittest.TestCase):
    def test_detect_backends_auto_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "projects"
            db = Path(tmp) / "opencode.db"
            pdir.mkdir()
            (pdir / "proj").mkdir()
            (pdir / "proj" / "sess.jsonl").write_text("{}")
            db.write_text("")
            self.assertEqual(
                cli._detect_backends("auto", str(pdir), str(db)),
                {"claude", "opencode"},
            )

    def test_detect_backends_auto_only_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "projects"
            db = Path(tmp) / "opencode.db"
            pdir.mkdir()
            (pdir / "proj").mkdir()
            (pdir / "proj" / "sess.jsonl").write_text("{}")
            self.assertEqual(
                cli._detect_backends("auto", str(pdir), str(db)),
                {"claude"},
            )

    def test_detect_backends_explicit_opencode(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "projects"
            db = Path(tmp) / "opencode.db"
            self.assertEqual(
                cli._detect_backends("opencode", str(pdir), str(db)),
                {"opencode"},
            )

    def test_cmd_scan_opencode_backend(self):
        with patch.object(cli, "import_opencode") as mock_import, \
             tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dash.db"
            odb = Path(tmp) / "opencode.db"
            odb.write_text("")
            args = argparse.Namespace(
                db=str(db),
                projects_dir=None,
                backend="opencode",
                opencode_db=str(odb),
            )
            cli.cmd_scan(args)
            mock_import.assert_called_once_with(str(odb), str(db))
```

```bash
python3 -m unittest tests.test_cli.CliBackendTests -v
```

Expected: tests fail because `_detect_backends`, `_backend`, `_opencode_db`, and `cmd_scan` changes do not exist yet.

### Step 5.2 — Implement CLI changes

Edit `cli.py`.

Add near the top, after imports:

```python
def _backend(args):
    return args.backend or os.environ.get("DASHBOARD_BACKEND") or "auto"


def _opencode_db(args):
    return args.opencode_db or os.environ.get("OPENCODE_DB") or str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")


def _detect_backends(backend_choice: str, projects_dir: str, opencode_db: str) -> set:
    if backend_choice == "claude":
        return {"claude"}
    if backend_choice == "opencode":
        return {"opencode"}
    # auto
    result = set()
    if Path(projects_dir).is_dir() and any(Path(projects_dir).rglob("*.jsonl")):
        result.add("claude")
    if Path(opencode_db).is_file():
        result.add("opencode")
    if not result:
        raise SystemExit("Token Dashboard: no data sources found (no Claude JSONL and no opencode.db)")
    return result
```

Modify `cmd_scan`:

```python
def cmd_scan(args):
    db_path = _db_path(args)
    projects_dir = _projects(args)
    opencode_db = _opencode_db(args)
    backend_choice = _backend(args)
    backends = _detect_backends(backend_choice, projects_dir, opencode_db)
    total = {"files": 0, "messages": 0, "tools": 0}
    if "claude" in backends:
        r = scan_dir(projects_dir, db_path)
        total["messages"] += r["messages"]
        total["tools"] += r["tools"]
        total["files"] += r["files"]
    if "opencode" in backends:
        from token_dashboard.opencode_source import import_opencode
        r = import_opencode(opencode_db, db_path)
        total["messages"] += r.get("messages", 0)
        total["tools"] += r.get("tools", 0)
        total["files"] += r.get("files", 0)
    print(f"Imported {total['messages']} messages, {total['tools']} tools from {total['files']} files")
```

Modify `cmd_dashboard`:

```python
def cmd_dashboard(args):
    db_path = _db_path(args)
    projects_dir = _projects(args)
    opencode_db = _opencode_db(args)
    backend_choice = _backend(args)
    backends = _detect_backends(backend_choice, projects_dir, opencode_db)
    from token_dashboard import server
    server.run(args.host, args.port, db_path, projects_dir, backends, opencode_db)
```

Modify the common parser in `main()`:

```python
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=os.environ.get("TOKEN_DASHBOARD_DB"))
    common.add_argument("--projects-dir", default=os.environ.get("CLAUDE_PROJECTS_DIR"))
    common.add_argument("--backend", choices=["auto", "claude", "opencode"], default=None)
    common.add_argument("--opencode-db", default=os.environ.get("OPENCODE_DB"))
```

### Step 5.3 — Run new tests

```bash
python3 -m unittest tests.test_cli.CliBackendTests -v
```

Expected: all tests pass.

### Step 5.4 — Run full test suite

```bash
python3 -m unittest discover tests -v
```

Expected: all tests pass.

### Step 5.5 — Commit

```bash
git add cli.py tests/test_cli.py
git commit -m "feat(cli): add --backend and --opencode-db flags for multi-backend scans"
```

---

### Task 6: Server — multi-backend scan loop

**Files:**
- Modify: `token_dashboard/server.py` (adapt scan loop and `/api/scan` for dual backend)
- Test: `tests/test_server.py` (add backend dispatch test)

### Step 6.1 — Write failing tests

Append to `tests/test_server.py`:

```python
class ServerBackendTests(unittest.TestCase):
    def test_scan_loop_calls_opencode_backend(self):
        with patch("token_dashboard.server.scan_dir") as mock_scan, \
             patch("token_dashboard.server.import_opencode") as mock_opencode:
            mock_scan.return_value = {"files": 0, "messages": 0, "tools": 0}
            mock_opencode.return_value = {"sessions": 1, "messages": 2, "tools": 1}
            # Kill the loop after one iteration by having the sleep raise
            with patch("token_dashboard.server.time.sleep", side_effect=Exception("stop")):
                with self.assertRaises(Exception):
                    server._scan_loop(":memory:", "/tmp/projects", {"opencode"}, "/tmp/opencode.db", interval=1.0)
            mock_scan.assert_not_called()
            mock_opencode.assert_called_once_with("/tmp/opencode.db", ":memory:")

    def test_api_scan_combines_backends(self):
        handler = server.build_handler(":memory:", "/tmp/projects", {"claude", "opencode"}, "/tmp/opencode.db")
        with patch("token_dashboard.server.scan_dir") as mock_scan, \
             patch("token_dashboard.server.import_opencode") as mock_opencode:
            mock_scan.return_value = {"files": 1, "messages": 10, "tools": 3}
            mock_opencode.return_value = {"files": 0, "messages": 5, "tools": 2}
            req = _request("POST", "/api/scan")
            resp = handler(req)
            body = json.loads(resp.read().decode())
            self.assertEqual(body["messages"], 15)
            self.assertEqual(body["tools"], 5)
            self.assertEqual(body["files"], 1)
```

```bash
python3 -m unittest tests.test_server.ServerBackendTests -v
```

Expected: tests fail because `build_handler`, `_scan_loop`, `/api/scan`, and `run` signatures do not yet accept backend parameters.

### Step 6.2 — Implement server changes

Edit `token_dashboard/server.py`.

Change `build_handler` signature:

```python
def build_handler(db_path: str, projects_dir: str, backends: set, opencode_db: str):
```

Change `_scan_loop`:

```python
def _scan_loop(db_path: str, projects_dir: str, backends: set, opencode_db: str, interval: float = 30.0):
    while True:
        try:
            n = {"files": 0, "messages": 0, "tools": 0}
            if "claude" in backends:
                r = scan_dir(projects_dir, db_path)
                n["messages"] += r["messages"]
                n["tools"] += r["tools"]
                n["files"] += r["files"]
            if "opencode" in backends:
                from .opencode_source import import_opencode
                r = import_opencode(opencode_db, db_path)
                n["messages"] += r.get("messages", 0)
                n["tools"] += r.get("tools", 0)
                n["files"] += r.get("files", 0)
            if n["messages"] > 0:
                EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
        except Exception as e:
            EVENTS.put({"type": "error", "message": str(e)})
        time.sleep(interval)
```

Change `/api/scan` route:

```python
        if self.path == "/api/scan":
            n = {"files": 0, "messages": 0, "tools": 0}
            if "claude" in backends:
                r = scan_dir(projects_dir, db_path)
                n["messages"] += r["messages"]
                n["tools"] += r["tools"]
                n["files"] += r["files"]
            if "opencode" in backends:
                from .opencode_source import import_opencode
                r = import_opencode(opencode_db, db_path)
                n["messages"] += r.get("messages", 0)
                n["tools"] += r.get("tools", 0)
                n["files"] += r.get("files", 0)
            EVENTS.put({"type": "scan", "n": n, "ts": time.time()})
            return _json(200, n)
```

Change `run` signature and thread start:

```python
def run(host: str, port: int, db_path: str, projects_dir: str, backends: set, opencode_db: str):
    threading.Thread(
        target=_scan_loop,
        args=(db_path, projects_dir, backends, opencode_db),
        daemon=True,
    ).start()
    httpd = HTTPServer((host, port), build_handler(db_path, projects_dir, backends, opencode_db))
    ...
```

### Step 6.3 — Run new tests

```bash
python3 -m unittest tests.test_server.ServerBackendTests -v
```

Expected: all tests pass.

### Step 6.4 — Run full test suite

```bash
python3 -m unittest discover tests -v
```

Expected: all tests pass.

### Step 6.5 — Commit

```bash
git add token_dashboard/server.py tests/test_server.py
git commit -m "feat(server): multi-backend scan loop and /api/scan support opencode + claude"
```

---

### Task 7: `pricing.json` — add opencode models + README update

**Files:**
- Modify: `pricing.json` (add opencode models)
- Modify: `README.md` (add opencode support section)
- Modify: `CLAUDE.md` (update architecture description)
- Test: `tests/test_pricing.py` (add test for opencode model pricing)

### Step 7.1 — Write failing tests

Append to `tests/test_pricing.py`:

```python
class OpencodePricingTests(unittest.TestCase):
    def test_glm_5_2_has_cost(self):
        usd = cost_for("glm-5.2", input_tokens=1000000, output_tokens=1000000)
        self.assertIsNotNone(usd)
        self.assertGreater(usd, 0)

    def test_deepseek_v4_pro_has_cost(self):
        usd = cost_for("deepseek-v4-pro", input_tokens=1000000, output_tokens=1000000)
        self.assertIsNotNone(usd)
        self.assertGreater(usd, 0)

    def test_auto_returns_none(self):
        usd = cost_for("auto", input_tokens=1000000, output_tokens=1000000)
        self.assertIsNone(usd)
```

```bash
python3 -m unittest tests.test_pricing.OpencodePricingTests -v
```

Expected: first two tests fail (models not in `pricing.json`), third passes.

### Step 7.2 — Update `pricing.json`

Add to the `"models"` section (order does not matter, but keep the same format):

```json
"glm-5.2":               { "tier": "glm", "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"deepseek-v4-pro":       { "tier": "deepseek", "input": 2.70, "output": 11.00, "cache_read": 0.27, "cache_create_5m": 3.24, "cache_create_1h": 3.24, "estimated": true },
"deepseek-v4-flash":     { "tier": "deepseek", "input": 0.27, "output": 1.10, "cache_read": 0.03, "cache_create_5m": 0.32, "cache_create_1h": 0.32, "estimated": true },
"kimi-k2.7-code":        { "tier": "kimi", "input": 0.60, "output": 2.50, "cache_read": 0.06, "cache_create_5m": 0.72, "cache_create_1h": 0.72, "estimated": true },
"mistral-large-3:675b":  { "tier": "mistral", "input": 2.00, "output": 6.00, "cache_read": 0.20, "cache_create_5m": 2.40, "cache_create_1h": 2.40, "estimated": true },
"qwen3.5:397b":          { "tier": "qwen", "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"qwen3-coder:480b":     { "tier": "qwen", "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"gemma4:31b":            { "tier": "gemma", "input": 0.20, "output": 0.80, "cache_read": 0.02, "cache_create_5m": 0.24, "cache_create_1h": 0.24, "estimated": true },
"gpt-oss:120b":          { "tier": "gpt-oss", "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"nemotron-3-ultra":     { "tier": "nemotron", "input": 0.40, "output": 1.60, "cache_read": 0.04, "cache_create_5m": 0.48, "cache_create_1h": 0.48, "estimated": true },
"sabia-4":               { "tier": "sabia", "input": 0.70, "output": 2.00, "cache_read": 0.07, "cache_create_5m": 0.84, "cache_create_1h": 0.84, "estimated": true },
"gemini-3-flash-preview":     { "tier": "gemini", "input": 0.15, "output": 0.60, "cache_read": 0.015, "cache_create_5m": 0.18, "cache_create_1h": 0.18, "estimated": true },
"gemini-3.1-pro-preview":     { "tier": "gemini", "input": 1.25, "output": 5.00, "cache_read": 0.125, "cache_create_5m": 1.50, "cache_create_1h": 1.50, "estimated": true },
"gemini-3.5-flash":          { "tier": "gemini", "input": 0.15, "output": 0.60, "cache_read": 0.015, "cache_create_5m": 0.18, "cache_create_1h": 0.18, "estimated": true },
"devstral-2:123b":      { "tier": "mistral", "input": 0.40, "output": 1.20, "cache_read": 0.04, "cache_create_5m": 0.48, "cache_create_1h": 0.48, "estimated": true }
```

Add to the `"tier_fallback"` section:

```json
"glm":       { "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"deepseek":  { "input": 2.70, "output": 11.00, "cache_read": 0.27, "cache_create_5m": 3.24, "cache_create_1h": 3.24, "estimated": true },
"kimi":      { "input": 0.60, "output": 2.50, "cache_read": 0.06, "cache_create_5m": 0.72, "cache_create_1h": 0.72, "estimated": true },
"mistral":   { "input": 2.00, "output": 6.00, "cache_read": 0.20, "cache_create_5m": 2.40, "cache_create_1h": 2.40, "estimated": true },
"qwen":      { "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"gemma":     { "input": 0.20, "output": 0.80, "cache_read": 0.02, "cache_create_5m": 0.24, "cache_create_1h": 0.24, "estimated": true },
"gpt-oss":   { "input": 0.50, "output": 2.00, "cache_read": 0.05, "cache_create_5m": 0.60, "cache_create_1h": 0.60, "estimated": true },
"nemotron":  { "input": 0.40, "output": 1.60, "cache_read": 0.04, "cache_create_5m": 0.48, "cache_create_1h": 0.48, "estimated": true },
"sabia":     { "input": 0.70, "output": 2.00, "cache_read": 0.07, "cache_create_5m": 0.84, "cache_create_1h": 0.84, "estimated": true },
"gemini":    { "input": 0.15, "output": 0.60, "cache_read": 0.015, "cache_create_5m": 0.18, "cache_create_1h": 0.18, "estimated": true }
```

### Step 7.3 — Run new tests

```bash
python3 -m unittest tests.test_pricing.OpencodePricingTests -v
```

Expected: all three tests pass.

### Step 7.4 — Update `README.md`

Insert after the Quickstart section:

```markdown
## opencode support

This dashboard also reads session data from [opencode](https://opencode.ai), which stores sessions in a SQLite database at `~/.local/share/opencode/opencode.db`.

### Auto-detection

By default the dashboard auto-detects which data sources are available:

- **Claude Code** — JSONL files in `~/.claude/projects/`
- **opencode** — SQLite database at `~/.local/share/opencode/opencode.db`
- **Both** — if both sources are present, data from both is merged into the same dashboard

### Forcing a backend

```bash
python3 cli.py dashboard --backend opencode      # only opencode
python3 cli.py dashboard --backend claude        # only Claude Code
python3 cli.py dashboard --backend auto          # auto-detect (default)
```

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | Path to opencode SQLite database |
| `DASHBOARD_BACKEND` | `auto` | Same as `--backend`; CLI flag wins if both set |
```

### Step 7.5 — Update `CLAUDE.md`

Edit the Architecture section in `CLAUDE.md`:

```
- `cli.py` → `token_dashboard/scanner.py` (Claude JSONL) OR `token_dashboard/opencode_source.py` (opencode SQLite) → `~/.claude/token-dashboard.db` (SQLite)
```

### Step 7.6 — Run full test suite

```bash
python3 -m unittest discover tests -v
```

Expected: all tests pass.

### Step 7.7 — Commit

```bash
git add pricing.json tests/test_pricing.py README.md CLAUDE.md
git commit -m "feat(pricing): add estimated pricing for opencode models and document dual-backend support"
```

---

### Self-Review (after Tasks 4-7)

**Spec coverage:**
- opencode skill roots added to `_DEFAULT_ROOTS` → Task 4.
- CLI `--backend` and `--opencode-db` flags with env var fallbacks and backend dispatch → Task 5.
- Server scan loop and `/api/scan` dispatch to both backends → Task 6.
- Estimated pricing for opencode models + dual-backend documentation in README and CLAUDE.md → Task 7.
- Stdlib-only Python, unittest, exact file paths, exact commands, checkbox syntax → covered throughout.

**Placeholder scan:** No TODOs, TBDs, or hand-wavy steps. Every step shows exact code or exact commands.

**Type consistency:** `_detect_backends` returns a `set` of backend strings; `server._scan_loop` and `/api/scan` consistently sum the same `messages`/`tools`/`files` keys and emit `n` in scan events.

