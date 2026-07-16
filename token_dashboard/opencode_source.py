"""Adapter: import opencode SQLite transcript data into token-dashboard.

Transforms opencode's session/message/part schema into the same internal
``messages`` and ``tool_calls`` tables used by the Claude Code JSONL scanner.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .db import _encode_slug, init_db


def default_opencode_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _project_slug(directory: Optional[str]) -> str:
    return _encode_slug(directory or "unknown")


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
    field = _TOOL_TARGET_FIELDS.get(tool_name)
    if field and isinstance(state_input, dict):
        v = state_input.get(field)
        if isinstance(v, str):
            return v[:500]
    return None


def _format_timestamp(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


def _parse_message_data(data_json: str) -> dict:
    """Extract token-dashboard fields from opencode message.data JSON.

    Malformed rows are treated as empty so one corrupt message doesn't
    abort a full import.
    """
    try:
        data = json.loads(data_json) if isinstance(data_json, str) else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    tokens = data.get("tokens") or {}
    cache = tokens.get("cache") or {}
    time = data.get("time") or {}
    return {
        "type": data.get("role"),
        "parent_uuid": data.get("parentID"),
        "agent_id": data.get("agent"),
        "model": data.get("modelID"),
        "timestamp": _format_timestamp(time.get("created", 0)),
        "input_tokens": int(tokens.get("input") or 0),
        "output_tokens": int(tokens.get("output") or 0),
        "cache_read_tokens": int(cache.get("read") or 0),
        "cache_create_5m_tokens": int(cache.get("write") or 0),
        "cache_create_1h_tokens": 0,
    }


def _fetch_prompt_text(oc_conn, message_id: str, role: str) -> tuple[Optional[str], Optional[int]]:
    if role != "user" or not message_id:
        return None, None
    rows = oc_conn.execute(
        "SELECT data FROM part WHERE message_id=? AND json_extract(data, '$.type') = 'text'",
        (message_id,),
    ).fetchall()
    parts = []
    for (payload,) in rows:
        part = json.loads(payload) if payload else {}
        text = part.get("text")
        if text:
            parts.append(text)
    text = "".join(parts) if parts else None
    return (text, len(text)) if text else (None, None)


def _import_sessions(oc_conn, internal_conn) -> int:
    """Return the number of opencode sessions seen (used for diagnostics)."""
    count = oc_conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    return int(count)


def _latest_import_ts(internal_conn) -> int:
    """Return the last imported opencode timestamp (epoch ms).

    Considers both opencode messages and tool_calls so tool parts created
    after the newest message are still picked up on the next import.
    """
    row = internal_conn.execute(
        "SELECT MAX(ts) FROM ("
        "  SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) * 1000 AS ts "
        "  FROM messages WHERE source = 'opencode' "
        "  UNION ALL "
        "  SELECT MAX(CAST(strftime('%s', timestamp) AS INTEGER)) * 1000 AS ts "
        "  FROM tool_calls WHERE source = 'opencode'"
        ")"
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    # Fallback for the first import before any rows exist.
    plan_row = internal_conn.execute(
        "SELECT v FROM plan WHERE k=?", ("opencode_last_import_ts",)
    ).fetchone()
    return int(plan_row[0]) if plan_row and plan_row[0] is not None else 0


def _import_messages(oc_conn, internal_conn, since_ts: int) -> int:
    sessions = {
        row["id"]: {
            "directory": row["directory"],
            "parent_id": row["parent_id"],
        }
        for row in oc_conn.execute("SELECT id, directory, parent_id FROM session")
    }

    new_since = since_ts
    inserted = 0
    for row in oc_conn.execute(
        "SELECT id, session_id, time_created, data, "
        "       json_extract(data, '$.role') AS role "
        "FROM message "
        "WHERE json_extract(data, '$.time.created') > ? ORDER BY time_created",
        (since_ts,),
    ):
        msg_id = row["id"]
        session_id = row["session_id"]
        time_created = row["time_created"]
        session = sessions.get(session_id) or {}
        parsed = _parse_message_data(row["data"])
        # Prefer the role extracted by SQLite so prompts are fetched with the
        # same json_extract expression the adapter uses for role queries.
        role = row["role"] or parsed["type"]
        prompt_text, prompt_chars = _fetch_prompt_text(oc_conn, msg_id, role)
        directory = session.get("directory")
        internal_conn.execute(
            """
            INSERT OR REPLACE INTO messages (
                uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
                type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
                input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
                prompt_text, prompt_chars, tool_calls_json, source
            ) VALUES (
                :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
                :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
                :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
                :prompt_text, :prompt_chars, :tool_calls_json, :source
            )
            """,
            {
                "uuid": msg_id,
                "parent_uuid": parsed["parent_uuid"],
                "session_id": session_id,
                "project_slug": _project_slug(directory),
                "cwd": directory,
                "git_branch": None,
                "cc_version": None,
                "entrypoint": None,
                "type": parsed["type"],
                "is_sidechain": 1 if session.get("parent_id") else 0,
                "agent_id": parsed["agent_id"],
                "timestamp": parsed["timestamp"],
                "model": parsed["model"],
                "stop_reason": None,
                "prompt_id": None,
                "message_id": msg_id,
                "input_tokens": parsed["input_tokens"],
                "output_tokens": parsed["output_tokens"],
                "cache_read_tokens": parsed["cache_read_tokens"],
                "cache_create_5m_tokens": parsed["cache_create_5m_tokens"],
                "cache_create_1h_tokens": parsed["cache_create_1h_tokens"],
                "prompt_text": prompt_text,
                "prompt_chars": prompt_chars,
                "tool_calls_json": None,
                "source": "opencode",
            },
        )
        inserted += 1
        if time_created > new_since:
            new_since = time_created

    return inserted


INSERT_OPENCODE_TOOL = """
INSERT OR REPLACE INTO tool_calls (
    part_id, message_uuid, session_id, project_slug, tool_name, target,
    result_tokens, is_error, timestamp, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _import_tool_calls(oc_conn, internal_conn, since_ts: int) -> int:
    """Import opencode tool parts created after ``since_ts`` into internal ``tool_calls``."""
    sessions = {
        row["id"]: {
            "directory": row["directory"],
            "parent_id": row["parent_id"],
        }
        for row in oc_conn.execute("SELECT id, directory, parent_id FROM session")
    }

    new_since = since_ts
    inserted = 0
    for row in oc_conn.execute(
        "SELECT p.id, p.message_id, p.session_id, p.time_created, p.data "
        "FROM part p "
        "WHERE json_extract(p.data, '$.type') = 'tool' AND p.time_created > ? "
        "ORDER BY p.time_created",
        (since_ts,),
    ):
        part_id = row["id"]
        message_id = row["message_id"]
        session_id = row["session_id"]
        time_created = row["time_created"]
        try:
            data = json.loads(row["data"]) if row["data"] else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}

        state = data.get("state") or {}
        state_input = state.get("input") or {}
        tool_name = data.get("tool") or "unknown"
        target = _extract_tool_target(tool_name, state_input)
        output = state.get("output") or ""
        status = state.get("status") or ""
        error = state.get("error")
        result_tokens = len(output) // 4 if isinstance(output, str) else 0
        is_error = 1 if (status != "completed" or error is not None) else 0

        session = sessions.get(session_id) or {}
        directory = session.get("directory")

        internal_conn.execute(
            INSERT_OPENCODE_TOOL,
            (
                part_id,
                message_id,
                session_id,
                _project_slug(directory),
                tool_name,
                target,
                result_tokens,
                is_error,
                _format_timestamp(time_created),
                "opencode",
            ),
        )
        inserted += 1
        if time_created > new_since:
            new_since = time_created

    return inserted


def _persist_import_ts(internal_conn, new_since: int) -> None:
    internal_conn.execute(
        "INSERT OR REPLACE INTO plan (k, v) VALUES (?, ?)",
        ("opencode_last_import_ts", str(new_since)),
    )


def import_opencode(opencode_db_path, internal_db_path) -> dict:
    """Import opencode session/message/tool data into the internal dashboard DB.

    Returns a small summary dict with ``sessions``, ``messages`` and
    ``tool_calls`` counts.
    """
    init_db(internal_db_path)
    oc_conn = sqlite3.connect(opencode_db_path)
    oc_conn.row_factory = sqlite3.Row
    try:
        internal_conn = sqlite3.connect(internal_db_path)
        internal_conn.row_factory = sqlite3.Row
        try:
            internal_conn.execute("PRAGMA foreign_keys = ON")
            since_ts = _latest_import_ts(internal_conn)
            sessions = _import_sessions(oc_conn, internal_conn)
            messages = _import_messages(oc_conn, internal_conn, since_ts)
            tools = _import_tool_calls(oc_conn, internal_conn, since_ts)
            new_since = max(
                since_ts,
                _latest_import_ts(internal_conn),
            )
            _persist_import_ts(internal_conn, new_since)
            internal_conn.commit()
        finally:
            internal_conn.close()
    finally:
        oc_conn.close()
    return {"sessions": sessions, "messages": messages, "tool_calls": tools}
