"""Adapter: import opencode SQLite transcript data into token-dashboard.

Transforms opencode's session/message/part schema into the same internal
``messages`` table used by the Claude Code JSONL scanner.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import _encode_slug, init_db


def default_opencode_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _project_slug(directory: Optional[str]) -> str:
    return _encode_slug(directory or "unknown")


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
    """Return the last imported opencode message time_created (epoch ms)."""
    row = internal_conn.execute(
        "SELECT v FROM plan WHERE k=?", ("opencode_last_import_ts",)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


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

    internal_conn.execute(
        "INSERT OR REPLACE INTO plan (k, v) VALUES (?, ?)",
        ("opencode_last_import_ts", str(new_since)),
    )
    return inserted


def import_opencode(opencode_db_path, internal_db_path) -> dict:
    """Import opencode session/message data into the internal dashboard DB.

    Returns a small summary dict with ``sessions`` and ``messages`` counts.
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
            internal_conn.commit()
        finally:
            internal_conn.close()
    finally:
        oc_conn.close()
    return {"sessions": sessions, "messages": messages}
