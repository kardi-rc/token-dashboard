"""v2 import leg: opencode ``session_v2`` / ``session_message`` tables.

Handles the post-2026-10-02 opencode storage schema: inline message content
(no ``part`` rows), nested ``model`` objects, native per-message USD ``cost``,
``finish`` reasons and seq-derived parent links (spec §DD4/§DD5). Shared
helpers are imported from ``opencode_source``; the orchestrator
``import_opencode`` lazy-imports ``import_opencode_v2`` from this module to
avoid a circular import (spec §9).
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Optional

from .opencode_source import (
    INSERT_OPENCODE_TOOL,
    _TOOL_TARGET_FIELDS,  # noqa: F401 — re-export: spec §9 shared-helper surface
    _extract_tool_target,
    _format_timestamp,
    _project_slug,
)


def _is_num(value) -> bool:
    """Finite non-bool number (AC-B3): bools, NaN and infinities are rejected."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _v2_model(value) -> Optional[str]:
    """Model per AC-A4: nested ``{"id": ...}`` (bare id), plain string as-is,
    anything else (missing/other type/empty) → NULL. providerID/variant dropped."""
    if isinstance(value, dict):
        mid = value.get("id")
        return mid if isinstance(mid, str) and mid else None
    if isinstance(value, str) and value:
        return value
    return None


def _int_or_zero(value) -> int:
    """int() coercion that never raises: non-numeric shapes become 0."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _parse_v2_data(data_json, fallback_time_created) -> dict:
    """Extract token-dashboard fields from a v2 ``session_message.data`` JSON row.

    Malformed or non-object JSON is treated as ``{}`` (same contract as
    ``_parse_message_data``); non-dict NESTED values (``tokens``/``cache``/
    ``time``) and non-numeric token values are coerced to defaults so one
    corrupt row never aborts the import (EC-7 + robustness guards).
    ``timestamp`` uses ``data['time']['created']`` when it is a non-bool number,
    else falls back to the row's ``time_created`` column (EC-7).
    """
    try:
        data = json.loads(data_json) if isinstance(data_json, str) else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw_tokens = data.get("tokens")
    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    raw_cache = tokens.get("cache")
    cache = raw_cache if isinstance(raw_cache, dict) else {}
    raw_time = data.get("time")
    time = raw_time if isinstance(raw_time, dict) else {}
    created = time.get("created")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        timestamp = _format_timestamp(created)
    else:
        timestamp = _format_timestamp(fallback_time_created)
    cost = data.get("cost")
    text = data.get("text")
    content = data.get("content")
    return {
        "agent_id": data.get("agent"),
        "model": _v2_model(data.get("model")),
        "stop_reason": data.get("finish"),
        "timestamp": timestamp,
        "input_tokens": _int_or_zero(tokens.get("input") or 0),
        "output_tokens": _int_or_zero(tokens.get("output") or 0),
        "cache_read_tokens": _int_or_zero(cache.get("read") or 0),
        "cache_create_5m_tokens": _int_or_zero(cache.get("write") or 0),
        "cache_create_1h_tokens": 0,  # v2 carries no 1h split (AC-A5)
        # AC-B3, EC-8: 0 stores 0.0. O1: a NEGATIVE stored cost is rejected
        # (None) so every stored value is >= 0 — then the SQL predicate
        # (cost_usd IS NULL OR cost_usd = 0), the "> 0" preference in
        # effective_message_cost and merge_model_group_cost agree, and a
        # negative row falls back to the computed estimate.
        "cost_usd": float(cost) if (_is_num(cost) and cost >= 0) else None,
        "prompt_text": text if isinstance(text, str) and text else None,  # EC-6 + shape guard
        "content": content if isinstance(content, list) else [],  # EC-5
    }


INSERT_V2_MESSAGE = """
INSERT OR REPLACE INTO messages (
    uuid, parent_uuid, session_id, project_slug, cwd, git_branch, cc_version, entrypoint,
    type, is_sidechain, agent_id, timestamp, model, stop_reason, prompt_id, message_id,
    input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
    prompt_text, prompt_chars, tool_calls_json, cost_usd, source
) VALUES (
    :uuid, :parent_uuid, :session_id, :project_slug, :cwd, :git_branch, :cc_version, :entrypoint,
    :type, :is_sidechain, :agent_id, :timestamp, :model, :stop_reason, :prompt_id, :message_id,
    :input_tokens, :output_tokens, :cache_read_tokens, :cache_create_5m_tokens, :cache_create_1h_tokens,
    :prompt_text, :prompt_chars, :tool_calls_json, :cost_usd, :source
)
"""


def _parent_uuid(oc_conn, session_id, seq) -> Optional[str]:
    """Nearest earlier imported row in the session by seq, or NULL (AC-A7).

    The predecessor lookup filters ``type IN ('user','assistant')`` so skipped
    rows (system/idle/...) never leak a dangling parent (EC-17); served by the
    UNIQUE (session_id, seq) index.
    """
    row = oc_conn.execute(
        "SELECT id FROM session_message "
        "WHERE session_id=? AND seq<? AND type IN ('user','assistant') "
        "ORDER BY seq DESC LIMIT 1",
        (session_id, seq),
    ).fetchone()
    return row["id"] if row else None


def import_opencode_v2(oc_conn, internal_conn, since_ts: int) -> dict:
    """Import v2 user/assistant messages and their inline tool calls.

    Reads ``session_v2``/``session_message`` through caller-owned connections
    (``oc_conn`` must be read-only, AC-A12); does NOT commit — the single
    watermark/commit boundary belongs to the orchestrator (AC-A9). Returns
    ``{"sessions", "messages", "tool_calls", "skipped_rows"}`` counts.
    """
    sessions = {
        row["id"]: {"directory": row["directory"], "parent_id": row["parent_id"]}
        for row in oc_conn.execute("SELECT id, directory, parent_id FROM session_v2")
    }
    session_count = int(oc_conn.execute("SELECT COUNT(*) FROM session_v2").fetchone()[0])

    messages = 0
    tool_calls = 0
    skipped = 0
    for row in oc_conn.execute(
        "SELECT id, session_id, seq, time_created, data, type "
        "FROM session_message "
        "WHERE time_created > ? AND type IN ('user','assistant') "
        "ORDER BY time_created",
        (since_ts,),
    ):
        msg_id = row["id"]
        session_id = row["session_id"]
        time_created = row["time_created"]
        msg_type = row["type"]
        try:
            session = sessions.get(session_id) or {}
            directory = session.get("directory")
            parsed = _parse_v2_data(row["data"], time_created)
            prompt_text = parsed["prompt_text"] if msg_type == "user" else None
            internal_conn.execute(
                INSERT_V2_MESSAGE,
                {
                    "uuid": msg_id,
                    "parent_uuid": _parent_uuid(oc_conn, session_id, row["seq"]),
                    "session_id": session_id,
                    "project_slug": _project_slug(directory),
                    "cwd": directory,
                    "git_branch": None,
                    "cc_version": None,
                    "entrypoint": None,
                    "type": msg_type,
                    "is_sidechain": 1 if session.get("parent_id") else 0,
                    "agent_id": parsed["agent_id"],
                    "timestamp": parsed["timestamp"],
                    "model": parsed["model"],
                    "stop_reason": parsed["stop_reason"],
                    "prompt_id": None,
                    "message_id": msg_id,
                    "input_tokens": parsed["input_tokens"],
                    "output_tokens": parsed["output_tokens"],
                    "cache_read_tokens": parsed["cache_read_tokens"],
                    "cache_create_5m_tokens": parsed["cache_create_5m_tokens"],
                    "cache_create_1h_tokens": parsed["cache_create_1h_tokens"],
                    "prompt_text": prompt_text,
                    "prompt_chars": len(prompt_text) if prompt_text else None,
                    "tool_calls_json": None,
                    "cost_usd": parsed["cost_usd"],
                    "source": "opencode",
                },
            )
            messages += 1

            if msg_type != "assistant":
                continue  # user rows are never scanned for tools (AC-A8)
            for index, item in enumerate(parsed["content"]):
                if not isinstance(item, dict) or item.get("type") != "tool":
                    continue  # non-dict items are skipped but keep their index slot (EC-10)
                item_id = item.get("id")
                part_id = (
                    f"{msg_id}:{item_id}"
                    if isinstance(item_id, str) and item_id
                    else f"{msg_id}#{index}"  # EC-9/EC-10: namespacing + fallback
                )
                tool_name = item.get("name") or "unknown"
                state = item.get("state") or {}
                target = _extract_tool_target(tool_name, state.get("input") or {})
                output = state.get("output") or ""
                result_tokens = len(output) // 4 if isinstance(output, str) else 0
                is_error = 1 if (state.get("status") != "completed" or state.get("error") is not None) else 0
                internal_conn.execute(
                    INSERT_OPENCODE_TOOL,
                    (
                        part_id,
                        msg_id,
                        session_id,
                        _project_slug(directory),
                        tool_name,
                        target,
                        result_tokens,
                        is_error,
                        _format_timestamp(time_created),  # parent's time_created (AC-A8)
                        "opencode",
                    ),
                )
                tool_calls += 1
        except (ValueError, TypeError, AttributeError, sqlite3.InterfaceError, sqlite3.ProgrammingError):
            # Robustness net: a row whose shape still defeats the parse guards
            # (e.g. non-scalar agent/finish at INSERT binding — Python raises
            # ProgrammingError/InterfaceError for unsupported types) is SKIPPED,
            # so one malformed row can never re-crash and stall the whole import.
            # IntegrityError/OperationalError still propagate (AC-A9 atomicity).
            skipped += 1

    return {
        "sessions": session_count,
        "messages": messages,
        "tool_calls": tool_calls,
        "skipped_rows": skipped,
    }
