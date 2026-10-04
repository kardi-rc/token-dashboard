"""Tests for the opencode -> token_dashboard adapter."""
from __future__ import annotations

import hashlib
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


class FakeOpencodeV2Db:
    """In-memory opencode v2 (session_v2 / session_message) schema for tests.

    Mirrors the live v2 tables the import leg reads, including the UNIQUE
    (session_id, seq) index the predecessor lookup relies on (EC-17). Takes a
    caller-supplied path so Task 4 tests can layer this fixture on top of the
    legacy FakeOpencodeDb in ONE file. ``data`` is JSON-encoded from a dict,
    or inserted raw when a string is passed (for malformed-row tests, EC-7).
    """

    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE session_v2 (
                id          TEXT PRIMARY KEY,
                directory   TEXT,
                parent_id   TEXT
            );
            CREATE TABLE session_message (
                id            TEXT PRIMARY KEY,
                session_id    TEXT,
                type          TEXT,
                seq           INTEGER,
                time_created  INTEGER,
                time_updated  INTEGER,
                data          TEXT
            );
            CREATE UNIQUE INDEX session_message_session_seq_idx
                ON session_message (session_id, seq);
        """)

    def add_session(
        self,
        session_id: str,
        directory: str = "/home/user/projects/foo-bar",
        parent_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO session_v2 (id, directory, parent_id) VALUES (?, ?, ?)",
            (session_id, directory, parent_id),
        )
        self.conn.commit()

    def add_message(
        self,
        msg_id: str,
        session_id: str,
        msg_type: str,
        seq: int,
        time_created: int,
        data,
    ) -> None:
        payload = data if isinstance(data, str) else json.dumps(data)
        self.conn.execute(
            "INSERT INTO session_message "
            "(id, session_id, type, seq, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, msg_type, seq, time_created, time_created, payload),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


class OpencodeV2TestBase(unittest.TestCase):
    """Shared setUp + v2 import harness for the v2-leg test classes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opencode_path = Path(self.tmp.name) / "opencode.db"
        self.internal_path = Path(self.tmp.name) / "token-dashboard.db"
        self.oc = FakeOpencodeV2Db(self.opencode_path)
        self.addCleanup(self.oc.close)

    def import_v2(self, since_ts: int = 0, opencode_path: Path | None = None) -> dict:
        """Run the v2 leg over a read-only source conn + an init_db internal DB.

        The v2 module is imported lazily so Step 1's red phase fails per-test
        (ModuleNotFoundError) without breaking collection of the v1 tests.
        """
        from token_dashboard import opencode_v2_source

        db_mod.init_db(self.internal_path)
        oc_conn = opencode_source._connect_readonly(
            self.opencode_path if opencode_path is None else opencode_path
        )
        try:
            internal_conn = sqlite3.connect(self.internal_path)
            internal_conn.row_factory = sqlite3.Row
            try:
                result = opencode_v2_source.import_opencode_v2(
                    oc_conn, internal_conn, since_ts
                )
                internal_conn.commit()
                return result
            finally:
                internal_conn.close()
        finally:
            oc_conn.close()

    def _messages(self) -> dict:
        db_mod.init_db(self.internal_path)
        conn = sqlite3.connect(self.internal_path)
        conn.row_factory = sqlite3.Row
        try:
            return {r["uuid"]: dict(r) for r in conn.execute("SELECT * FROM messages")}
        finally:
            conn.close()

    def _tool_rows(self) -> dict:
        db_mod.init_db(self.internal_path)
        conn = sqlite3.connect(self.internal_path)
        conn.row_factory = sqlite3.Row
        try:
            return {r["part_id"]: dict(r) for r in conn.execute("SELECT * FROM tool_calls")}
        finally:
            conn.close()

    @staticmethod
    def assistant_data(**over) -> dict:
        """Full-shape v2 assistant payload; keyword overrides replace top-level keys."""
        data = {
            "agent": "build",
            "finish": "stop",
            "model": {"id": "glm-5.3-flash", "providerID": "ollama-cloud", "variant": "default"},
            "tokens": {"input": 10, "output": 20, "reasoning": 3, "cache": {"read": 30, "write": 40}},
            "cost": 0.0025,
            "time": {"created": 1_700_000_002_000},
            "content": [],
        }
        data.update(over)
        return data


class TestOpencodeV2Source(OpencodeV2TestBase):
    def test_a2_only_user_and_assistant_rows_imported(self):
        self.oc.add_session("sess_1", directory="/home/user/projects/my-app")
        self.oc.add_message(
            "m_user", "sess_1", "user", 1, 1_700_000_001_000,
            {"text": "hi", "time": {"created": 1_700_000_001_000}},
        )
        self.oc.add_message(
            "m_assistant", "sess_1", "assistant", 2, 1_700_000_002_000,
            self.assistant_data(),
        )
        self.oc.add_message("m_idle", "sess_1", "idle", 3, 1_700_000_003_000, {})
        self.oc.add_message("m_system", "sess_1", "system", 4, 1_700_000_004_000, {})

        result = self.import_v2()

        self.assertEqual(
            set(result), {"sessions", "messages", "tool_calls", "skipped_rows"}
        )
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["tool_calls"], 0)
        rows = self._messages()
        self.assertEqual(set(rows), {"m_user", "m_assistant"})
        for mid, row in rows.items():
            self.assertEqual(row["uuid"], mid)
            self.assertEqual(row["message_id"], mid)
            self.assertEqual(row["source"], "opencode")

    def test_a3_session_fields_project_slug_cwd_sidechain(self):
        self.oc.add_session("s_main", directory="/home/user/projects/my-app")
        self.oc.add_session("s_child", directory="/home/user/projects/other-dir", parent_id="s_main")
        self.oc.add_message("u1", "s_main", "user", 1, 1_700_000_001_000, {"time": {"created": 1_700_000_001_000}})
        self.oc.add_message("u2", "s_child", "user", 1, 1_700_000_002_000, {"time": {"created": 1_700_000_002_000}})

        self.import_v2()

        rows = self._messages()
        self.assertEqual(rows["u1"]["session_id"], "s_main")
        self.assertEqual(rows["u1"]["project_slug"], db_mod._encode_slug("/home/user/projects/my-app"))
        self.assertEqual(rows["u1"]["cwd"], "/home/user/projects/my-app")
        self.assertEqual(rows["u1"]["is_sidechain"], 0)
        self.assertEqual(rows["u2"]["cwd"], "/home/user/projects/other-dir")
        self.assertEqual(rows["u2"]["is_sidechain"], 1)

    def test_a4_nested_plain_and_missing_model_shapes(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("m_nested", "s1", "assistant", 1, b + 1, self.assistant_data(
            model={"id": "glm-5.3-flash", "providerID": "ollama-cloud", "variant": "default"}))
        self.oc.add_message("m_plain", "s1", "assistant", 2, b + 2, self.assistant_data(model="plain-model"))
        self.oc.add_message("m_missing", "s1", "assistant", 3, b + 3, self.assistant_data(model=None))
        self.oc.add_message("m_num", "s1", "assistant", 4, b + 4, self.assistant_data(model=42))
        self.oc.add_message("m_dict_noid", "s1", "assistant", 5, b + 5, self.assistant_data(model={"providerID": "x"}))

        self.import_v2()

        rows = self._messages()
        self.assertEqual(rows["m_nested"]["model"], "glm-5.3-flash")
        self.assertEqual(rows["m_plain"]["model"], "plain-model")
        self.assertIsNone(rows["m_missing"]["model"])
        self.assertIsNone(rows["m_num"]["model"])
        self.assertIsNone(rows["m_dict_noid"]["model"])

    def test_a5_token_mapping_defaults_and_1h_always_zero(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("m_full", "s1", "assistant", 1, b + 1, self.assistant_data(
            tokens={"input": 479, "output": 2332, "reasoning": 5, "cache": {"read": 59072, "write": 7}}))
        self.oc.add_message("m_notokens", "s1", "assistant", 2, b + 2, {"time": {"created": b + 2}})
        self.oc.add_message("m_partial", "s1", "assistant", 3, b + 3, self.assistant_data(
            tokens={"cache": {"read": 5}}))

        self.import_v2()

        rows = self._messages()
        full = rows["m_full"]
        self.assertEqual(full["input_tokens"], 479)
        self.assertEqual(full["output_tokens"], 2332)
        self.assertEqual(full["cache_read_tokens"], 59072)
        self.assertEqual(full["cache_create_5m_tokens"], 7)
        self.assertEqual(full["cache_create_1h_tokens"], 0)
        for mid in ("m_notokens", "m_partial"):
            self.assertEqual(rows[mid]["input_tokens"], 0)
            self.assertEqual(rows[mid]["output_tokens"], 0)
            self.assertEqual(rows[mid]["cache_create_1h_tokens"], 0)
        self.assertEqual(rows["m_partial"]["cache_read_tokens"], 5)
        self.assertEqual(rows["m_partial"]["cache_create_5m_tokens"], 0)
        self.assertEqual(rows["m_notokens"]["cache_read_tokens"], 0)
        self.assertIsNone(rows["m_notokens"]["stop_reason"])
        self.assertIsNone(rows["m_notokens"]["agent_id"])

    def test_a6_stop_reason_agent_timestamp_and_user_text(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("u_full", "s1", "user", 1, b + 1, {"text": "hello world", "time": {"created": b + 1}})
        self.oc.add_message("u_notext", "s1", "user", 2, b + 2, {"time": {"created": b + 2}})
        self.oc.add_message("u_empty", "s1", "user", 3, b + 3, {"text": "", "time": {"created": b + 3}})
        self.oc.add_message("a1", "s1", "assistant", 4, b + 4, self.assistant_data(
            finish="tool-calls", agent="explore", time={"created": b + 4},
            text="inline key is ignored on assistants"))

        self.import_v2()

        rows = self._messages()
        self.assertEqual(len(rows), 4)  # EC-6: no-text / empty-text rows are still imported
        a = rows["a1"]
        self.assertEqual(a["stop_reason"], "tool-calls")
        self.assertEqual(a["agent_id"], "explore")
        self.assertEqual(a["timestamp"], opencode_source._format_timestamp(b + 4))
        self.assertEqual(
            rows["u_full"]["timestamp"], opencode_source._format_timestamp(b + 1)
        )
        self.assertIsNone(a["prompt_text"])
        self.assertIsNone(a["prompt_chars"])
        self.assertEqual(rows["u_full"]["prompt_text"], "hello world")
        self.assertEqual(rows["u_full"]["prompt_chars"], 11)
        self.assertIsNone(rows["u_notext"]["prompt_text"])
        self.assertIsNone(rows["u_notext"]["prompt_chars"])
        self.assertIsNone(rows["u_empty"]["prompt_text"])
        self.assertIsNone(rows["u_empty"]["prompt_chars"])
        for col in ("git_branch", "cc_version", "entrypoint", "prompt_id", "tool_calls_json"):
            self.assertIsNone(a[col], col)

    def test_a7_ec17_parent_derivation_and_expensive_prompts(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("u1", "s1", "user", 1, b + 1, {"text": "prompt one", "time": {"created": b + 1}})
        self.oc.add_message("syst", "s1", "system", 2, b + 2, {})
        self.oc.add_message("idle1", "s1", "idle", 3, b + 3, {})
        self.oc.add_message("a1", "s1", "assistant", 4, b + 4, self.assistant_data(time={"created": b + 4}))
        self.oc.add_message("a2", "s1", "assistant", 5, b + 5, self.assistant_data(time={"created": b + 5}))

        self.import_v2()

        rows = self._messages()
        self.assertIsNone(rows["u1"]["parent_uuid"])       # first imported row in the session
        self.assertEqual(rows["a1"]["parent_uuid"], "u1")  # system/idle predecessors are skipped
        self.assertEqual(rows["a2"]["parent_uuid"], "a1")

        prompts = db_mod.expensive_prompts(self.internal_path)
        links = {(p["user_uuid"], p["assistant_uuid"]) for p in prompts}
        self.assertIn(("u1", "a1"), links)

    def test_b3_cost_storage_rules(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        cases = [
            ("c_pos", 0.42),
            ("c_int_zero", 0),
            ("c_float_zero", 0.0),
            ("c_missing", "__omit__"),
            ("c_null", None),
            ("c_str", "0.42"),
            ("c_true", True),
            ("c_false", False),
            ("c_nan", float("nan")),
            ("c_inf", float("inf")),
        ]
        for i, (mid, cost) in enumerate(cases):
            data = self.assistant_data(time={"created": b + i})
            if cost == "__omit__":
                del data["cost"]
            else:
                data["cost"] = cost
            self.oc.add_message(mid, "s1", "assistant", i + 1, b + i, data)

        self.import_v2()

        rows = self._messages()
        self.assertEqual(rows["c_pos"]["cost_usd"], 0.42)
        self.assertIsInstance(rows["c_pos"]["cost_usd"], float)
        self.assertEqual(rows["c_int_zero"]["cost_usd"], 0.0)
        self.assertIsInstance(rows["c_int_zero"]["cost_usd"], float)
        self.assertEqual(rows["c_float_zero"]["cost_usd"], 0.0)
        for mid in ("c_missing", "c_null", "c_str", "c_true", "c_false", "c_nan", "c_inf"):
            self.assertIsNone(rows[mid]["cost_usd"], mid)

    def test_o1_negative_cost_stores_none_not_a_negative(self):
        """O1 predicate consistency: a stored cost must be >= 0. A negative
        value falls back to NULL (the computed estimate), so the SQL predicate
        (``cost_usd IS NULL OR cost_usd = 0``), the ``> 0`` preference in
        ``effective_message_cost`` and ``merge_model_group_cost`` all agree —
        a negative can never be silently preferred over the estimate."""
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("c_neg", "s1", "assistant", 1, b + 1, self.assistant_data(cost=-0.42))
        self.oc.add_message("c_neg_int", "s1", "assistant", 2, b + 2, self.assistant_data(cost=-1))
        self.oc.add_message("c_pos", "s1", "assistant", 3, b + 3, self.assistant_data(cost=0.42))
        self.oc.add_message("c_zero", "s1", "assistant", 4, b + 4, self.assistant_data(cost=0))

        result = self.import_v2()

        self.assertEqual(result["messages"], 4)
        self.assertEqual(result["skipped_rows"], 0)
        rows = self._messages()
        self.assertIsNone(rows["c_neg"]["cost_usd"])
        self.assertIsNone(rows["c_neg_int"]["cost_usd"])
        self.assertEqual(rows["c_pos"]["cost_usd"], 0.42)
        self.assertEqual(rows["c_zero"]["cost_usd"], 0.0)

    def test_ec7_malformed_data_row_still_imports(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("bad", "s1", "assistant", 1, b + 1, "{this is ] not json")
        self.oc.add_message("nullish", "s1", "user", 2, b + 2, "null")
        self.oc.add_message("strtime", "s1", "assistant", 3, b + 3, {"time": {"created": "soon"}, "cost": 1.0})

        result = self.import_v2()

        self.assertEqual(result["messages"], 3)
        rows = self._messages()
        bad = rows["bad"]
        self.assertEqual(bad["type"], "assistant")  # type comes from the column
        self.assertEqual(bad["timestamp"], opencode_source._format_timestamp(b + 1))
        self.assertEqual(bad["input_tokens"], 0)
        self.assertEqual(bad["output_tokens"], 0)
        self.assertEqual(bad["cache_read_tokens"], 0)
        self.assertIsNone(bad["cost_usd"])
        self.assertIsNone(bad["model"])
        self.assertIsNone(bad["stop_reason"])
        self.assertIsNone(rows["nullish"]["prompt_text"])
        # Non-numeric time.created falls back to the row's time_created column.
        self.assertEqual(rows["strtime"]["timestamp"], opencode_source._format_timestamp(b + 3))
        self.assertEqual(rows["strtime"]["cost_usd"], 1.0)

    def test_a8_ec9_two_messages_same_call_id_are_namespaced(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000

        def tool_item():
            return {"type": "tool", "id": "call_0", "name": "read",
                    "state": {"status": "completed", "input": {"file_path": "/tmp/a.py"}, "output": "data"}}

        self.oc.add_message("msgA", "s1", "assistant", 1, b + 1, self.assistant_data(time={"created": b + 1}, content=[tool_item()]))
        self.oc.add_message("msgB", "s1", "assistant", 2, b + 2, self.assistant_data(time={"created": b + 2}, content=[tool_item()]))

        result = self.import_v2()

        self.assertEqual(result["tool_calls"], 2)
        rows = self._tool_rows()
        self.assertEqual(set(rows), {"msgA:call_0", "msgB:call_0"})

    def test_a8_tool_field_mapping(self):
        self.oc.add_session("s1", directory="/tmp/foo")
        b = 1_700_000_000_000
        item = {"type": "tool", "id": "call_x", "tool": "decoy-v1-key-ignored", "name": "bash",
                "executed": True,
                "state": {"status": "completed", "input": {"command": "ls -la"}, "output": "total 0\n"}}
        self.oc.add_message("a1", "s1", "assistant", 1, b + 1, self.assistant_data(time={"created": b + 1}, content=[item]))

        self.import_v2()

        rows = self._tool_rows()
        row = rows["a1:call_x"]
        self.assertEqual(row["tool_name"], "bash")  # v2 key is name, not tool
        self.assertEqual(row["target"], "ls -la")
        self.assertEqual(row["result_tokens"], len("total 0\n") // 4)
        self.assertEqual(row["is_error"], 0)  # executed is ignored entirely (AC-A8)
        self.assertEqual(row["message_uuid"], "a1")
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["project_slug"], db_mod._encode_slug("/tmp/foo"))
        self.assertEqual(row["timestamp"], opencode_source._format_timestamp(b + 1))
        self.assertEqual(row["source"], "opencode")

    def test_a8_ec11_is_error_and_result_tokens_rules(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000

        def item(tool_id, state):
            it = {"type": "tool", "id": tool_id}
            if state is not None:
                it["state"] = state
            return it

        content = [
            item("run", {"status": "running", "output": "partial"}),
            item("err", {"status": "completed", "output": "", "error": "boom"}),
            item("nostate", None),
            item("dictout", {"status": "completed", "output": {"ok": True}}),
        ]
        self.oc.add_message("a1", "s1", "assistant", 1, b + 1, self.assistant_data(time={"created": b + 1}, content=content))

        self.import_v2()

        rows = self._tool_rows()
        self.assertEqual(set(rows), {"a1:run", "a1:err", "a1:nostate", "a1:dictout"})
        self.assertEqual(rows["a1:run"]["is_error"], 1)          # in-flight counted as error
        self.assertEqual(rows["a1:err"]["is_error"], 1)          # error present
        self.assertEqual(rows["a1:nostate"]["is_error"], 1)      # missing state -> {} -> error
        self.assertEqual(rows["a1:nostate"]["tool_name"], "unknown")
        self.assertEqual(rows["a1:dictout"]["is_error"], 0)
        self.assertEqual(rows["a1:dictout"]["result_tokens"], 0)  # non-str output
        self.assertEqual(rows["a1:run"]["result_tokens"], len("partial") // 4)

    def test_ec10_no_id_fallback_keeps_original_index_slots(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        content = [
            42,
            "junk string",
            {"type": "tool", "id": "", "name": "glob",
             "state": {"status": "completed", "input": {"pattern": "*.py"}}},
            {"type": "text", "text": "not a tool"},
            {"type": "tool", "name": "grep",
             "state": {"status": "completed", "input": {"pattern": "needle"}}},
        ]
        self.oc.add_message("a1", "s1", "assistant", 1, b + 1, self.assistant_data(time={"created": b + 1}, content=content))

        result = self.import_v2()

        self.assertEqual(result["tool_calls"], 2)
        rows = self._tool_rows()
        # Zero-based positions in the ORIGINAL array: 2 and 4 (junk keeps its slot).
        self.assertEqual(set(rows), {"a1#2", "a1#4"})
        self.assertEqual(rows["a1#2"]["target"], "*.py")
        self.assertEqual(rows["a1#4"]["tool_name"], "grep")

    def test_ec5_and_user_rows_never_produce_tool_rows(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        tool = {"type": "tool", "id": "c1", "name": "bash", "state": {"status": "completed", "output": "x"}}
        self.oc.add_message("a_nocontent", "s1", "assistant", 1, b + 1, {"time": {"created": b + 1}})
        self.oc.add_message("a_none", "s1", "assistant", 2, b + 2, self.assistant_data(time={"created": b + 2}, content=None))
        self.oc.add_message("a_str", "s1", "assistant", 3, b + 3, self.assistant_data(time={"created": b + 3}, content="not a list"))
        self.oc.add_message("a_empty", "s1", "assistant", 4, b + 4, self.assistant_data(time={"created": b + 4}, content=[]))
        self.oc.add_message("u_tool", "s1", "user", 5, b + 5, {"text": "hi", "time": {"created": b + 5}, "content": [tool]})

        result = self.import_v2()

        self.assertEqual(result["messages"], 5)
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(self._tool_rows(), {})

    def test_since_ts_filter_and_idempotent_reimport(self):
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("u1", "s1", "user", 1, b + 1, {"text": "one", "time": {"created": b + 1}})
        self.oc.add_message("a1", "s1", "assistant", 2, b + 2, self.assistant_data(time={"created": b + 2}))
        self.oc.add_message("u2", "s1", "user", 3, b + 3, {"text": "two", "time": {"created": b + 3}})

        first = self.import_v2(since_ts=0)
        self.assertEqual(first["messages"], 3)
        second = self.import_v2(since_ts=b + 3)
        self.assertEqual(second["messages"], 0)
        third = self.import_v2(since_ts=b + 1)  # re-reads a1/u2: upserts, no duplicates
        self.assertEqual(third["messages"], 2)
        self.assertEqual(set(self._messages()), {"u1", "a1", "u2"})

    # (f) robustness: nested-shape garbage must never crash or stall the leg ---

    def test_robustness_nested_shape_garbage_parses_with_defaults(self):
        """A dict ``data`` whose NESTED values violate expected shapes parses
        with defaults via the _parse_v2_data guards — never raises, the row is
        IMPORTED (not skipped): tokens→0, fallback timestamp, non-str text→NULL."""
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("u_ok", "s1", "user", 1, b + 1,
                            {"text": "hi", "time": {"created": b + 1}})
        self.oc.add_message("u_garbage", "s1", "user", 2, b + 2, {
            "tokens": "garbage",   # non-dict tokens/cache shapes
            "time": 5,             # non-dict time -> fallback timestamp
            "text": ["not", "a", "string"],  # truthy non-str text -> NULL
            "model": ["x"],        # non-dict/non-str model -> NULL
            "cost": {"a": 1},      # non-numeric cost -> None
        })

        result = self.import_v2()

        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["skipped_rows"], 0)
        rows = self._messages()
        g = rows["u_garbage"]
        for col in ("input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_create_5m_tokens", "cache_create_1h_tokens"):
            self.assertEqual(g[col], 0, col)
        self.assertEqual(g["timestamp"], opencode_source._format_timestamp(b + 2))
        self.assertIsNone(g["prompt_text"])
        self.assertIsNone(g["prompt_chars"])
        self.assertIsNone(g["model"])
        self.assertIsNone(g["cost_usd"])

    def test_robustness_unbindable_row_skipped_not_stalled(self):
        """A row whose remaining shapes still defeat the INSERT binding (dict
        agent / list finish -> sqlite3.InterfaceError) is SKIPPED by the
        per-row net; the import completes and the other rows persist."""
        self.oc.add_session("s1")
        b = 1_700_000_000_000
        self.oc.add_message("a_bad", "s1", "assistant", 1, b + 1,
                            self.assistant_data(time={"created": b + 1},
                                                agent={"deep": 1}, finish=["x"]))
        self.oc.add_message("u_ok", "s1", "user", 2, b + 2,
                            {"text": "after", "time": {"created": b + 2}})

        result = self.import_v2()

        self.assertEqual(result["skipped_rows"], 1)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["tool_calls"], 0)
        self.assertEqual(set(self._messages()), {"u_ok"})


class TestOpencodeV2ReadOnlyConnection(OpencodeV2TestBase):
    def test_a12_write_through_readonly_connection_raises(self):
        oc_conn = opencode_source._connect_readonly(self.opencode_path)
        self.addCleanup(oc_conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            oc_conn.execute("CREATE TABLE probe (id INTEGER)")

    def test_ec21_path_with_space_and_hash_imports(self):
        weird_dir = Path(self.tmp.name) / "proj with space#hash"
        weird_dir.mkdir()
        weird_path = weird_dir / "opencode v2.db"
        oc = FakeOpencodeV2Db(weird_path)
        self.addCleanup(oc.close)
        oc.add_session("s1", directory="/home/user/projects/x")
        oc.add_message("u1", "s1", "user", 1, 1_700_000_001_000, {"text": "hi", "time": {"created": 1_700_000_001_000}})

        result = self.import_v2(opencode_path=weird_path)

        self.assertEqual(result["messages"], 1)
        self.assertEqual(self._messages()["u1"]["prompt_text"], "hi")


class TestOpencodeDualImportOrchestration(unittest.TestCase):
    """Task 4: dual-leg orchestration in import_opencode.

    Auto-detection (AC-A1/EC-1/EC-2/EC-3), single shared watermark (AC-A9),
    no double-count on a fresh internal DB (AC-A10), watermark recovery from
    row maxima (EC-19) and read-only source bytes (AC-A12). Dual sources are
    built by layering the legacy FakeOpencodeDb DDL and the FakeOpencodeV2Db
    DDL on ONE temp file (spec §10).
    """

    B = 1_700_000_000_000  # epoch ms base; all test times are whole seconds

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opencode_path = Path(self.tmp.name) / "opencode.db"
        self.internal_path = Path(self.tmp.name) / "token-dashboard.db"

    # -- fixture helpers ----------------------------------------------------

    def _legacy(self) -> FakeOpencodeDb:
        oc = FakeOpencodeDb(self.opencode_path)
        self.addCleanup(oc.close)
        return oc

    def _v2(self) -> FakeOpencodeV2Db:
        oc = FakeOpencodeV2Db(self.opencode_path)
        self.addCleanup(oc.close)
        return oc

    def _raw_legacy_conn(self, session_id_nullable: bool = False,
                         with_part: bool = True):
        """Legacy DDL copy for shapes FakeOpencodeDb cannot express:
        no ``part`` table, or a nullable ``message.session_id`` (AC-A9
        atomicity — the row then violates the INTERNAL NOT NULL)."""
        conn = sqlite3.connect(self.opencode_path)
        msg_session = "TEXT" if session_id_nullable else "TEXT NOT NULL"
        part_ddl = (
            "CREATE TABLE part ("
            " id TEXT PRIMARY KEY, message_id TEXT NOT NULL,"
            " session_id TEXT NOT NULL, time_created INTEGER,"
            " time_updated INTEGER, data TEXT);"
            if with_part else ""
        )
        conn.executescript(
            "CREATE TABLE session ("
            " id TEXT PRIMARY KEY, directory TEXT, parent_id TEXT,"
            " time_created INTEGER, time_updated INTEGER);"
            " CREATE TABLE message ("
            f" id TEXT PRIMARY KEY, session_id {msg_session},"
            " time_created INTEGER, time_updated INTEGER, data TEXT);"
            + part_ddl
        )
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    @staticmethod
    def _legacy_data(role: str, time_created: int) -> str:
        return json.dumps({"role": role, "time": {"created": time_created}})

    @staticmethod
    def _v2_assistant(time_created: int) -> dict:
        return {
            "model": {"id": "glm-5.2"},
            "tokens": {"input": 5, "output": 6},
            "cost": 0.01,
            "finish": "stop",
            "time": {"created": time_created},
            "content": [],
        }

    # -- internal-DB probes --------------------------------------------------

    def _internal_conn(self):
        db_mod.init_db(self.internal_path)
        conn = sqlite3.connect(self.internal_path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def _opencode_rows(self):
        with self._internal_conn() as conn:
            return {r["uuid"]: dict(r) for r in conn.execute(
                "SELECT * FROM messages WHERE source = 'opencode'"
            )}

    def _token_sums(self):
        with self._internal_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n,"
                " COALESCE(SUM(input_tokens), 0) AS i,"
                " COALESCE(SUM(output_tokens), 0) AS o"
                " FROM messages WHERE source = 'opencode'"
            ).fetchone()
            return (row["n"], row["i"], row["o"])

    # (a) AC-A1 auto-detection -----------------------------------------------

    def test_a1_both_table_sets_run_both_legs(self):
        legacy = self._legacy()
        v2 = self._v2()
        legacy.add_session("sess_v1", directory="/home/user/projects/a")
        legacy.add_message("m_v1", "sess_v1", "user", self.B + 1_000)
        v2.add_session("sess_v2x", directory="/home/user/projects/b")
        v2.add_message(
            "m_v2", "sess_v2x", "user", 1, self.B + 9_000_000,
            {"text": "hello", "time": {"created": self.B + 9_000_000}},
        )

        summary = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertGreater(summary["v1_messages"], 0)
        self.assertGreater(summary["v2_messages"], 0)
        self.assertEqual(
            summary["messages"], summary["v1_messages"] + summary["v2_messages"]
        )
        self.assertEqual(summary["sessions"], 2)

    def test_ec1_v2_only_skips_v1_and_sessions_count_session_v2(self):
        v2 = self._v2()
        v2.add_session("s1", directory="/home/user/projects/a")
        v2.add_session("s2", directory="/home/user/projects/b", parent_id="s1")
        v2.add_message(
            "u1", "s1", "user", 1, self.B + 1_000,
            {"text": "one", "time": {"created": self.B + 1_000}},
        )

        summary = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(summary["v1_messages"], 0)
        self.assertEqual(summary["sessions"], 2)
        self.assertEqual(summary["messages"], 1)
        self.assertEqual(summary["v2_messages"], 1)

    def test_ec2_legacy_only_zeroes_v2_counters(self):
        legacy = self._legacy()
        legacy.add_session("s1", directory="/home/user/projects/a")
        legacy.add_message("m1", "s1", "assistant", self.B + 1_000)
        legacy.add_part(
            "prt_1", "m1", "s1", "tool",
            data={"type": "tool", "tool": "bash",
                  "state": {"status": "completed",
                            "input": {"command": "ls"}, "output": "ok"}},
            time_created=self.B + 1_001,
        )

        summary = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(summary["v2_messages"], 0)
        self.assertEqual(summary["v2_tool_calls"], 0)
        self.assertEqual(summary["messages"], summary["v1_messages"])
        self.assertEqual(summary["tool_calls"], 1)

    def test_ac_a1_no_relevant_tables_returns_all_zeros(self):
        conn = sqlite3.connect(self.opencode_path)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()

        summary = opencode_source.import_opencode(  # must NOT raise
            self.opencode_path, self.internal_path
        )

        self.assertEqual(summary["sessions"], 0)
        self.assertEqual(summary["messages"], 0)
        self.assertEqual(summary["tool_calls"], 0)
        self.assertEqual(summary["v1_messages"], 0)
        self.assertEqual(summary["v2_messages"], 0)
        self.assertEqual(summary["v2_tool_calls"], 0)

    def test_ac_a1_legacy_without_part_skips_v1_tools(self):
        conn = self._raw_legacy_conn(with_part=False)
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated)"
            " VALUES ('s1', '/home/user/projects/a', ?, ?)",
            (self.B, self.B),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)"
            " VALUES ('m1', 's1', ?, ?, ?)",
            (self.B + 1_000, self.B + 1_000,
             self._legacy_data("assistant", self.B + 1_000)),
        )
        conn.commit()

        summary = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(summary["v1_messages"], 1)
        self.assertEqual(summary["tool_calls"], 0)
        self.assertEqual(self._opencode_rows().keys(), {"m1"})

    # (b) AC-A10 no double-count on a fresh internal DB -----------------------

    def test_a10_no_double_count_with_reused_ids_then_idempotent(self):
        legacy = self._legacy()
        v2 = self._v2()
        legacy.add_session("s1", directory="/home/user/projects/x")
        legacy.add_message("L1", "s1", "user", self.B + 1_000)
        legacy.add_message("L2", "s1", "assistant", self.B + 2_000)
        # EC-3 shape: v2 history re-uses a legacy id at the same time_created,
        # plus newer v2-only rows.
        v2.add_session("s1", directory="/home/user/projects/x")
        v2.add_message(
            "L1", "s1", "user", 1, self.B + 1_000,
            {"text": "dup", "time": {"created": self.B + 1_000}},
        )
        v2.add_message("V1", "s1", "assistant", 2, self.B + 5_000,
                       self._v2_assistant(self.B + 5_000))

        first = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(first["v1_messages"], 2)
        self.assertEqual(first["v2_messages"], 1)  # only the newer V1
        rows = self._opencode_rows()
        # Union size 3: each legacy message exactly once, each newer v2 once.
        self.assertEqual(set(rows), {"L1", "L2", "V1"})
        self.assertEqual(rows["L1"]["prompt_text"], None)  # v1 copy, not v2's "dup"
        sums_before = self._token_sums()

        second = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(second["messages"], 0)
        self.assertEqual(self._token_sums(), sums_before)

    # (c) AC-A9 single watermark + atomicity -----------------------------------

    def test_a9_dual_import_writes_exactly_one_watermark_key(self):
        legacy = self._legacy()
        v2 = self._v2()
        legacy.add_session("s1", directory="/home/user/projects/a")
        legacy.add_message("m1", "s1", "user", self.B + 1_000)
        v2.add_session("s2", directory="/home/user/projects/b")
        v2.add_message("m2", "s2", "user", 1, self.B + 9_000_000,
                       {"text": "x", "time": {"created": self.B + 9_000_000}})

        opencode_source.import_opencode(self.opencode_path, self.internal_path)

        with self._internal_conn() as conn:
            keys = [r["k"] for r in conn.execute(
                "SELECT k FROM plan WHERE k LIKE 'opencode%'"
            )]
        self.assertEqual(keys, ["opencode_last_import_ts"])

    def test_a9_leg_failure_rolls_back_everything_including_watermark(self):
        # session_id_nullable: the bad legacy row passes the SOURCE DDL but
        # violates the INTERNAL messages.session_id NOT NULL (AC-A9 atomicity).
        conn = self._raw_legacy_conn(session_id_nullable=True)
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated)"
            " VALUES ('s1', '/home/user/projects/a', ?, ?)",
            (self.B, self.B),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)"
            " VALUES ('ok', 's1', ?, ?, ?)",
            (self.B + 1_000, self.B + 1_000,
             self._legacy_data("user", self.B + 1_000)),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)"
            " VALUES ('bad', NULL, ?, ?, ?)",
            (self.B + 2_000, self.B + 2_000,
             self._legacy_data("user", self.B + 2_000)),
        )
        conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            opencode_source.import_opencode(
                self.opencode_path, self.internal_path
            )

        self.assertEqual(self._opencode_rows(), {})
        with self._internal_conn() as ic:
            watermark = ic.execute(
                "SELECT v FROM plan WHERE k = 'opencode_last_import_ts'"
            ).fetchone()
        self.assertIsNone(watermark)

    # (d) EC-19 watermark derives from row maxima -------------------------------

    def test_ec19_missing_kv_row_re_derives_from_row_maxima(self):
        legacy = self._legacy()
        legacy.add_session("s1", directory="/home/user/projects/a")
        legacy.add_message("m1", "s1", "user", self.B + 1_000)
        first = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )
        self.assertEqual(first["messages"], 1)

        with sqlite3.connect(self.internal_path) as conn:
            conn.execute("DELETE FROM plan WHERE k = 'opencode_last_import_ts'")
            conn.commit()

        second = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(second["messages"], 0)  # watermark came from rows, not kv
        self.assertEqual(len(self._opencode_rows()), 1)

    # (e) AC-A12 source bytes unchanged -----------------------------------------

    def test_a12_source_file_unchanged_after_full_import(self):
        legacy = self._legacy()
        v2 = self._v2()
        legacy.add_session("s1", directory="/home/user/projects/a")
        legacy.add_message("m1", "s1", "assistant", self.B + 1_000)
        legacy.add_part(
            "prt_1", "m1", "s1", "tool",
            data={"type": "tool", "tool": "bash",
                  "state": {"status": "completed",
                            "input": {"command": "ls"}, "output": "ok"}},
            time_created=self.B + 1_001,
        )
        v2.add_session("s2", directory="/home/user/projects/b")
        v2.add_message("m2", "s2", "assistant", 1, self.B + 9_000_000,
                       self._v2_assistant(self.B + 9_000_000))

        digest_before = hashlib.sha256(
            self.opencode_path.read_bytes()
        ).hexdigest()
        summary = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )
        digest_after = hashlib.sha256(
            self.opencode_path.read_bytes()
        ).hexdigest()

        self.assertEqual(summary["v1_messages"], 1)
        self.assertEqual(summary["v2_messages"], 1)
        self.assertEqual(digest_before, digest_after)

    # (f) robustness: one malformed row never stalls the whole import ----------

    def test_robustness_v2_bad_row_skipped_watermark_advances(self):
        """A v2 row the guards cannot rescue is skipped; the import still
        COMMITS, the watermark persists past it (no permanent re-read/stall)
        and the one stderr summary line is printed."""
        import io
        from contextlib import redirect_stderr

        v2 = self._v2()
        v2.add_session("s1", directory="/home/user/projects/a")
        v2.add_message("bad", "s1", "assistant", 1, self.B + 1_000, {
            "tokens": "garbage", "time": 5, "agent": {"x": 1}, "content": [],
        })
        v2.add_message("u1", "s1", "user", 2, self.B + 2_000,
                       {"text": "hi", "time": {"created": self.B + 2_000}})

        err = io.StringIO()
        with redirect_stderr(err):
            first = opencode_source.import_opencode(
                self.opencode_path, self.internal_path
            )

        self.assertEqual(first["skipped_rows"], 1)
        self.assertEqual(first["messages"], 1)
        self.assertIn("opencode import skipped 1 malformed rows", err.getvalue())
        with self._internal_conn() as conn:
            wm = conn.execute(
                "SELECT v FROM plan WHERE k = 'opencode_last_import_ts'"
            ).fetchone()
        self.assertIsNotNone(wm)  # single commit happened despite the bad row

        second = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )
        self.assertEqual(second["messages"], 0)  # watermark covers the rows
        self.assertEqual(set(self._opencode_rows()), {"u1"})

    def test_robustness_v1_bad_row_skipped_import_completes(self):
        """A legacy v1 message row with nested-shape garbage (str ``tokens``)
        makes _parse_message_data raise AttributeError — the per-row net skips
        it and the import completes instead of crashing every scan."""
        conn = self._raw_legacy_conn()
        conn.execute(
            "INSERT INTO session (id, directory, time_created, time_updated)"
            " VALUES ('s1', '/home/user/projects/a', ?, ?)",
            (self.B, self.B),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)"
            " VALUES ('bad', 's1', ?, ?, ?)",
            (self.B + 1_000, self.B + 1_000,
             json.dumps({"role": "user", "tokens": "garbage",
                         "time": {"created": self.B + 1_000}})),
        )
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)"
            " VALUES ('ok', 's1', ?, ?, ?)",
            (self.B + 2_000, self.B + 2_000,
             self._legacy_data("user", self.B + 2_000)),
        )
        conn.commit()

        first = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )

        self.assertEqual(first["skipped_rows"], 1)
        self.assertEqual(first["v1_messages"], 1)
        self.assertEqual(set(self._opencode_rows()), {"ok"})

        second = opencode_source.import_opencode(
            self.opencode_path, self.internal_path
        )
        self.assertEqual(second["messages"], 0)
        self.assertEqual(second["skipped_rows"], 0)  # bad row behind watermark

    # (g) O3: watermark clock-skew clamp ---------------------------------------

    def test_o3_future_stamped_watermark_clamped_and_reread(self):
        """O3: a source row stamped in the FUTURE (clock skew) must not strand
        the import. The persisted watermark is clamped to wall-clock now, and
        the derived watermark reads the same way, so after the clock corrects
        the row is re-read IDEMPOTENTLY (INSERT OR REPLACE keeps exactly one
        row, token sums never double) instead of being silently lost forever."""
        import time

        future_ms = int(time.time() * 1000) + 2 * 3_600_000  # 2h ahead of the clock
        v2 = self._v2()
        v2.add_session("s1", directory="/home/user/projects/a")
        v2.add_message("fut", "s1", "assistant", 1, future_ms,
                       self._v2_assistant(future_ms))

        first = opencode_source.import_opencode(self.opencode_path, self.internal_path)
        self.assertEqual(first["v2_messages"], 1)
        after_import_ms = int(time.time() * 1000)

        with self._internal_conn() as conn:
            watermark = int(conn.execute(
                "SELECT v FROM plan WHERE k = 'opencode_last_import_ts'"
            ).fetchone()["v"])
        self.assertLess(watermark, future_ms, "watermark must not chase a skewed row")
        self.assertLessEqual(watermark, after_import_ms)

        # "Clock correction": the same fixture re-imported must re-read the row.
        second = opencode_source.import_opencode(self.opencode_path, self.internal_path)
        self.assertEqual(second["v2_messages"], 1)

        rows = self._opencode_rows()
        self.assertEqual(set(rows), {"fut"})  # INSERT OR REPLACE: still exactly one
        self.assertEqual(self._token_sums()[0], 1)


if __name__ == "__main__":
    unittest.main()
