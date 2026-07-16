"""Tests for the opencode -> token_dashboard adapter."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from token_dashboard import db as db_mod
from token_dashboard import opencode_source


class FakeOpencodeDb:
    """In-memory opencode.db-like schema for tests.

    Mirrors the real opencode.db column names (snake_case) and stores message
    and part payloads as JSON in ``data`` columns.
    """

    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE session (
                id            TEXT PRIMARY KEY,
                directory     TEXT,
                parent_id     TEXT,
                title         TEXT,
                agent         TEXT,
                model         TEXT,
                cost          REAL,
                tokens_input  INTEGER,
                tokens_output INTEGER,
                tokens_reasoning INTEGER,
                tokens_cache_read INTEGER,
                tokens_cache_write INTEGER,
                time_created  INTEGER,
                time_updated  INTEGER
            );
            CREATE TABLE message (
                id            TEXT PRIMARY KEY,
                session_id    TEXT NOT NULL,
                time_created  INTEGER,
                time_updated  INTEGER,
                data          TEXT
            );
            CREATE TABLE part (
                id            TEXT PRIMARY KEY,
                message_id    TEXT NOT NULL,
                session_id    TEXT NOT NULL,
                time_created  INTEGER,
                time_updated  INTEGER,
                data          TEXT
            );
        """)

    def add_session(
        self,
        session_id: str,
        directory: str = "/home/user/projects/foo-bar",
        parent_id: str | None = None,
        time_created: int = 1_700_000_000_000,
    ) -> None:
        self.conn.execute(
            "INSERT INTO session (id, directory, parent_id, time_created, time_updated) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, directory, parent_id, time_created, time_created),
        )
        self.conn.commit()

    def add_message(
        self,
        msg_id: str,
        session_id: str,
        role: str,
        time_created: int,
        parent_id: str | None = None,
        agent: str | None = "build",
        model_id: str = "glm-5.2",
        provider_id: str = "ollama-cloud",
        tokens: dict | None = None,
    ) -> None:
        data = {
            "role": role,
            "modelID": model_id,
            "providerID": provider_id,
            "agent": agent,
            "parentID": parent_id,
            "tokens": tokens or {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
            "time": {"created": time_created},
        }
        self.conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
            (msg_id, session_id, time_created, time_created, json.dumps(data)),
        )
        self.conn.commit()

    def add_part(
        self,
        part_id: str,
        message_id: str,
        session_id: str,
        part_type: str,
        text: str | None = None,
        data: dict | None = None,
        time_created: int = 1_700_000_000_000,
    ) -> None:
        payload = {"type": part_type}
        if text is not None:
            payload["text"] = text
        if data:
            payload.update(data)
        self.conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (part_id, message_id, session_id, time_created, time_created, json.dumps(payload)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


class TestOpencodeSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opencode_path = Path(self.tmp.name) / "opencode.db"
        self.internal_path = Path(self.tmp.name) / "token-dashboard.db"
        self.oc = FakeOpencodeDb(self.opencode_path)
        self.addCleanup(self.oc.close)

    def test_default_opencode_db_path(self):
        expected = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        self.assertEqual(opencode_source.default_opencode_db_path(), expected)

    def test_basic_import_creates_messages_with_source_opencode(self):
        self.oc.add_session("sess_1", directory="/home/user/projects/my-app")
        self.oc.add_message(
            "msg_user_1", "sess_1", "user", 1_700_000_001_000,
            parent_id=None, agent="build",
        )
        self.oc.add_message(
            "msg_assistant_1", "sess_1", "assistant", 1_700_000_002_000,
            parent_id="msg_user_1", agent="build",
            tokens={"input": 10, "output": 20, "reasoning": 5, "cache": {"read": 1, "write": 2}},
        )

        rows_before = self._internal_messages()
        self.assertEqual(len(rows_before), 0)

        opencode_source.import_opencode(self.opencode_path, self.internal_path)

        rows = self._internal_messages()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["source"] == "opencode" for r in rows))

        user = [r for r in rows if r["type"] == "user"][0]
        self.assertEqual(user["uuid"], "msg_user_1")
        self.assertEqual(user["session_id"], "sess_1")
        self.assertEqual(user["project_slug"], db_mod._encode_slug("/home/user/projects/my-app"))
        self.assertEqual(user["is_sidechain"], 0)
        self.assertEqual(user["agent_id"], "build")
        self.assertEqual(user["timestamp"], "2023-11-14T22:13:21+00:00")

        assistant = [r for r in rows if r["type"] == "assistant"][0]
        self.assertEqual(assistant["uuid"], "msg_assistant_1")
        self.assertEqual(assistant["parent_uuid"], "msg_user_1")
        self.assertEqual(assistant["input_tokens"], 10)
        self.assertEqual(assistant["output_tokens"], 20)
        self.assertEqual(assistant["cache_read_tokens"], 1)
        self.assertEqual(assistant["cache_create_5m_tokens"], 2)
        self.assertEqual(assistant["cache_create_1h_tokens"], 0)
        self.assertEqual(assistant["model"], "glm-5.2")

    def test_sidechain_detection(self):
        self.oc.add_session("parent_sess", directory="/home/user/projects/parent")
        self.oc.add_session("child_sess", directory="/home/user/projects/parent", parent_id="parent_sess")
        self.oc.add_message("msg_1", "child_sess", "user", 1_700_000_000_000)

        opencode_source.import_opencode(self.opencode_path, self.internal_path)

        row = self._internal_messages()[0]
        self.assertEqual(row["is_sidechain"], 1)

    def test_prompt_text_extracted_from_parts(self):
        self.oc.add_session("sess_1", directory="/home/user/projects/parent")
        self.oc.add_message("msg_user_1", "sess_1", "user", 1_700_000_001_000)
        self.oc.add_part("part_1", "msg_user_1", "sess_1", "text", "hello world")
        self.oc.add_part("part_2", "msg_user_1", "sess_1", "reasoning", "should be ignored")

        opencode_source.import_opencode(self.opencode_path, self.internal_path)

        row = self._internal_messages()[0]
        self.assertEqual(row["type"], "user")
        self.assertEqual(row["prompt_text"], "hello world")
        self.assertEqual(row["prompt_chars"], 11)

    def test_incremental_sync(self):
        self.oc.add_session("sess_1", directory="/home/user/projects/parent")
        self.oc.add_message("msg_old", "sess_1", "user", 1_700_000_000_000)
        opencode_source.import_opencode(self.opencode_path, self.internal_path)
        first = self._internal_messages()
        self.assertEqual(len(first), 1)

        self.oc.add_message("msg_new", "sess_1", "user", 1_800_000_000_000)
        opencode_source.import_opencode(self.opencode_path, self.internal_path)
        second = self._internal_messages()
        self.assertEqual(len(second), 2)
        self.assertEqual({r["uuid"] for r in second}, {"msg_old", "msg_new"})

    def _internal_messages(self):
        db_mod.init_db(self.internal_path)
        conn = sqlite3.connect(self.internal_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY timestamp")]
        finally:
            conn.close()

    def _internal_tool_calls(self):
        db_mod.init_db(self.internal_path)
        conn = sqlite3.connect(self.internal_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM tool_calls ORDER BY timestamp")]
        finally:
            conn.close()

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
        self.oc.add_session("sess_1", directory="/tmp/foo")
        self.oc.add_message("msg_1", "sess_1", "assistant", 1_700_000_001_000)
        self.oc.add_part(
            "part_tool_1",
            "msg_1",
            "sess_1",
            "tool",
            data={
                "type": "tool",
                "tool": "bash",
                "callID": "call_abc",
                "state": {
                    "status": "completed",
                    "input": {"command": "ls -la"},
                    "output": "total 0\n",
                    "time": {"start": 1_700_000_001_272},
                },
            },
            time_created=1_700_000_001_001,
        )

        result = opencode_source.import_opencode(self.opencode_path, self.internal_path)
        self.assertEqual(result["tool_calls"], 1)

        rows = self._internal_tool_calls()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["message_uuid"], "msg_1")
        self.assertEqual(row["session_id"], "sess_1")
        self.assertEqual(row["project_slug"], db_mod._encode_slug("/tmp/foo"))
        self.assertEqual(row["tool_name"], "bash")
        self.assertEqual(row["target"], "ls -la")
        self.assertEqual(row["is_error"], 0)
        self.assertEqual(row["source"], "opencode")

    def test_tool_call_error_status(self):
        self.oc.add_session("sess_1", directory="/tmp/foo")
        self.oc.add_message("msg_1", "sess_1", "assistant", 1_700_000_001_000)
        self.oc.add_part(
            "part_tool_1",
            "msg_1",
            "sess_1",
            "tool",
            data={
                "type": "tool",
                "tool": "bash",
                "callID": "call_err",
                "state": {
                    "status": "failed",
                    "input": {"command": "exit 1"},
                    "output": "",
                    "error": "command failed",
                    "time": {"start": 1_700_000_001_272},
                },
            },
            time_created=1_700_000_001_001,
        )

        opencode_source.import_opencode(self.opencode_path, self.internal_path)
        rows = self._internal_tool_calls()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["is_error"], 1)

    def test_result_tokens_estimation(self):
        self.oc.add_session("sess_1", directory="/tmp/foo")
        self.oc.add_message("msg_1", "sess_1", "assistant", 1_700_000_001_000)
        self.oc.add_part(
            "part_tool_1",
            "msg_1",
            "sess_1",
            "tool",
            data={
                "type": "tool",
                "tool": "bash",
                "callID": "call_big",
                "state": {
                    "status": "completed",
                    "input": {"command": "python -c 'print(\"x\"*400)'"},
                    "output": "x" * 400,
                    "time": {"start": 1_700_000_001_272},
                },
            },
            time_created=1_700_000_001_001,
        )

        opencode_source.import_opencode(self.opencode_path, self.internal_path)
        rows = self._internal_tool_calls()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
