"""Adapter: import opencode SQLite transcript data into token-dashboard.

Transforms opencode's session/message/part schema into the same internal
``messages`` and ``tool_calls`` tables used by the Claude Code JSONL scanner.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .db import _encode_slug, init_db


def default_opencode_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def _connect_readonly(path) -> sqlite3.Connection:
    """Open a SQLite source database strictly read-only via a file URI.

    Spec §6 / EC-21: build the URI with ``Path(path).resolve().as_uri()``,
    which percent-escapes ``?``, ``#`` and spaces — NEVER build the raw
    ``f"file:{path}?mode=ro"`` form, because a raw ``?`` or ``#`` in the path
    would truncate or corrupt the URI. ``mode=ro`` guarantees the adapter can
    never write the live opencode database (active WAL source, AC-A12). The
    connection string is not SQL; the path value is the same internal one
    ``sqlite3.connect`` already receives today.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def _import_sessions(oc_conn) -> int:
    """Return the number of opencode sessions seen (used for diagnostics)."""
    count = oc_conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    return int(count)


def _latest_import_ts(internal_conn) -> int:
    """Return the last imported opencode timestamp (epoch ms).

    Considers both opencode messages and tool_calls so tool parts created
    after the newest message are still picked up on the next import. O3 clock
    skew: capped at wall-clock now, so a future-stamped row never strands the
    import — it is re-read (the upsert keeps it single) once the clock corrects.
    """
    now_ms = int(time.time() * 1000)
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
        return min(int(row[0]), now_ms)
    # Fallback for the first import before any rows exist.
    plan_row = internal_conn.execute(
        "SELECT v FROM plan WHERE k=?", ("opencode_last_import_ts",)
    ).fetchone()
    return min(int(plan_row[0]), now_ms) if plan_row and plan_row[0] is not None else 0


def _import_messages(oc_conn, internal_conn, since_ts: int) -> tuple:
    """Import legacy v1 messages; returns ``(inserted, skipped)``. Net-skipped
    rows never stall the import; NOT NULL raises still propagate (AC-A9)."""
    sessions = {
        row["id"]: {
            "directory": row["directory"],
            "parent_id": row["parent_id"],
        }
        for row in oc_conn.execute("SELECT id, directory, parent_id FROM session")
    }

    inserted = 0
    skipped = 0
    for row in oc_conn.execute(
        "SELECT id, session_id, data, "
        "       json_extract(data, '$.role') AS role "
        "FROM message "
        "WHERE json_extract(data, '$.time.created') > ? ORDER BY time_created",
        (since_ts,),
    ):
        msg_id = row["id"]
        session_id = row["session_id"]
        try:
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
        except (ValueError, TypeError, AttributeError, sqlite3.InterfaceError, sqlite3.ProgrammingError):
            skipped += 1  # malformed shape (incl. non-scalar bind): skip, keep importing

    return inserted, skipped


INSERT_OPENCODE_TOOL = """
INSERT OR REPLACE INTO tool_calls (
    part_id, message_uuid, session_id, project_slug, tool_name, target,
    result_tokens, is_error, timestamp, source
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _import_tool_calls(oc_conn, internal_conn, since_ts: int) -> tuple:
    """Import tool parts after ``since_ts``; returns ``(inserted, skipped)``.
    Parse-shape rows are net-skipped; AC-A9 raises still propagate."""
    sessions = {
        row["id"]: {
            "directory": row["directory"],
            "parent_id": row["parent_id"],
        }
        for row in oc_conn.execute("SELECT id, directory, parent_id FROM session")
    }

    inserted = 0
    skipped = 0
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
        except (ValueError, TypeError, AttributeError, sqlite3.InterfaceError, sqlite3.ProgrammingError):
            skipped += 1  # malformed shape (incl. non-scalar bind): skip, keep importing

    return inserted, skipped


def _persist_import_ts(internal_conn, new_since: int) -> None:
    internal_conn.execute(
        "INSERT OR REPLACE INTO plan (k, v) VALUES (?, ?)",
        ("opencode_last_import_ts", str(new_since)),
    )


_OPENCODE_TABLE_NAMES = ("session", "message", "part", "session_v2", "session_message")


def _detect_tables(oc_conn) -> set:
    """Return the source tables the import legs can read (AC-A1).

    ONE parameterized probe of the source table catalog over the five known
    table names; the names are bound values, never interpolated into any
    other SQL. The leg gates compare the returned set with ``<=``.
    """
    rows = oc_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?)",
        _OPENCODE_TABLE_NAMES,
    ).fetchall()
    return {row["name"] for row in rows}


def import_opencode(opencode_db_path, internal_db_path) -> dict:
    """Import opencode data — dual legs (v1 legacy + v2) with one watermark.

    Fixed order (spec §DD1 / AC-A9): open ONE read-only source connection
    for both legs (AC-A12); auto-detect tables (AC-A1); run the v1 leg with
    ``since_ts`` if ``session``+``message`` exist (tool sub-leg only if
    ``part`` exists too — EC-2 skips it otherwise); recompute the watermark
    from the internal DB; run the v2 leg with the recomputed value if
    ``session_v2``+``session_message`` exist; persist the watermark ONCE and
    commit once, so a raise in either leg leaves nothing committed (AC-A9).

    Returns a summary whose ``sessions``/``messages``/``tool_calls`` are
    totals across both legs (backward compatible, AC-A11) plus the per-leg
    diagnostic keys ``v1_messages``/``v2_messages``/``v2_tool_calls`` plus
    ``skipped_rows`` (robustness-net skips — the import still commits, no stall).
    ``sessions`` counts source-table rows per leg, so an id present in both
    ``session`` and ``session_v2`` is counted twice — diagnostic only; no
    consumer relies on its uniqueness.
    """
    init_db(internal_db_path)
    oc_conn = _connect_readonly(opencode_db_path)
    try:
        internal_conn = sqlite3.connect(internal_db_path)
        internal_conn.row_factory = sqlite3.Row
        try:
            internal_conn.execute("PRAGMA foreign_keys = ON")
            tables = _detect_tables(oc_conn)
            since_ts = _latest_import_ts(internal_conn)

            v1_sessions = v1_messages = v1_tools = 0
            v1_msg_skipped = v1_tool_skipped = skipped_rows = 0
            if {"session", "message"} <= tables:
                v1_sessions = _import_sessions(oc_conn)
                v1_messages, v1_msg_skipped = _import_messages(oc_conn, internal_conn, since_ts)
                if "part" in tables:
                    v1_tools, v1_tool_skipped = _import_tool_calls(oc_conn, internal_conn, since_ts)
            skipped_rows = v1_msg_skipped + v1_tool_skipped

            v2 = {"sessions": 0, "messages": 0, "tool_calls": 0, "skipped_rows": 0}
            since_ts2 = _latest_import_ts(internal_conn)
            if {"session_v2", "session_message"} <= tables:
                # Lazy import: opencode_v2_source imports helpers from THIS
                # module — a top-level import would be circular (spec §9;
                # precedent: cli.py lazy-imports import_opencode).
                from .opencode_v2_source import import_opencode_v2
                v2 = import_opencode_v2(oc_conn, internal_conn, since_ts2)

            # O3: clamp the persisted watermark to wall-clock now. A source row
            # stamped in the future (clock skew) is then re-read idempotently
            # after the clock corrects, instead of being silently lost forever.
            new_since = max(since_ts, min(_latest_import_ts(internal_conn), int(time.time() * 1000)))
            _persist_import_ts(internal_conn, new_since)
            internal_conn.commit()
        finally:
            internal_conn.close()
    finally:
        oc_conn.close()
    skipped_rows += v2.get("skipped_rows", 0)
    if skipped_rows:
        print(f"opencode import skipped {skipped_rows} malformed rows", file=sys.stderr)
    return {
        "sessions": v1_sessions + v2["sessions"],
        "messages": v1_messages + v2["messages"],
        "tool_calls": v1_tools + v2["tool_calls"],
        "v1_messages": v1_messages,
        "v2_messages": v2["messages"],
        "v2_tool_calls": v2["tool_calls"],
        "skipped_rows": skipped_rows,
    }
