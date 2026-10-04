"""Cost-aware aggregation queries extracted from db.py.

Implements the stored-cost-first pattern (spec Design Decisions §3,
docs/specs/2026-10-03-opencode-v2-storage-and-pricing.md): SQL aggregates
the stored per-message ``cost_usd`` per group, plus token sums limited to
rows without a usable stored cost (``cost_usd IS NULL OR cost_usd = 0``);
the caller adds the pricing-computed estimate for those rows via
``merge_model_group_cost``. A stored cost is preferred only when it is NOT
NULL **and > 0**, so flat/subscription rows that report 0.0 fall back to
the computed estimate (AC-B7). v1 and Claude Code rows (always NULL)
behave exactly as before the extraction.
"""
from __future__ import annotations

from .db import connect, _range_clause
from .pricing import cost_for


def model_breakdown(db_path, since=None, until=None) -> list:
    """Per-model token totals + turn count + stored/NULL-cost aggregates.

    Beyond the original display keys (``model``, ``turns`` and the five
    all-row token sums — unchanged so current token displays don't move),
    each group carries (AC-B4):

    - ``stored_cost``: ``COALESCE(SUM(cost_usd), 0)`` over the group. NULL
      rows add nothing and 0-cost rows add 0, so the "> 0" preference rule
      lives in the next predicate, not here.
    - the five ``null_*`` token sums: all-row tokens limited to rows
      without a usable stored cost (``cost_usd IS NULL OR cost_usd = 0``),
      the usage dict to feed ``cost_for`` from Python.
    - ``null_cost_rows``: count of rows without a usable stored cost. This
      is an additive diagnostic beyond AC-B4's field list, required to
      implement AC-B5's ``cost_estimated`` rule exactly (False when the
      group has no NULL-cost and no zero-cost rows).

    The caller computes the merged cost via merge_model_group_cost().
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens,
             COALESCE(SUM(cost_usd), 0) AS stored_cost,
             SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0 THEN 1 ELSE 0 END)
               AS null_cost_rows,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN input_tokens ELSE 0 END), 0) AS null_input_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN output_tokens ELSE 0 END), 0) AS null_output_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN cache_read_tokens ELSE 0 END), 0) AS null_cache_read_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN cache_create_5m_tokens ELSE 0 END), 0) AS null_cache_create_5m_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN cache_create_1h_tokens ELSE 0 END), 0) AS null_cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY model
       ORDER BY (input_tokens + output_tokens + cache_create_5m_tokens + cache_create_1h_tokens) DESC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def cost_series(db_path, since=None, until=None) -> list:
    """Daily (date, model) cost-series rows for the /api/cost-series endpoint.

    One row per (local calendar date, model) pair, carrying exactly the
    merge-input keys of ``model_breakdown`` — ``stored_cost`` plus the five
    ``null_*`` token sums (rows without a usable stored cost) — so the
    handler can price each bucket later. NO pricing argument and NO merging
    here.

    Bucketing (pinned by spec): ``date(timestamp, 'localtime')`` — local
    midnight, not substr — and ``COALESCE(model, 'unknown')`` so NULL-model
    rows group under the string ``'unknown'`` and are never dropped nor
    keyed by SQL NULL. Same FROM/WHERE and ``_range_clause`` reuse as
    ``model_breakdown`` (since/until bound with ``?``; until exclusive).
    Ordered by date, then model (time-series friendly).
    """
    rng, args = _range_clause(since, until)
    sql = f"""
      SELECT date(timestamp, 'localtime') AS date,
             COALESCE(model, 'unknown') AS model,
             COUNT(*) AS turns,
             COALESCE(SUM(input_tokens),0)            AS input_tokens,
             COALESCE(SUM(output_tokens),0)           AS output_tokens,
             COALESCE(SUM(cache_read_tokens),0)       AS cache_read_tokens,
             COALESCE(SUM(cache_create_5m_tokens),0)  AS cache_create_5m_tokens,
             COALESCE(SUM(cache_create_1h_tokens),0)  AS cache_create_1h_tokens,
             COALESCE(SUM(cost_usd), 0) AS stored_cost,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN input_tokens ELSE 0 END), 0) AS null_input_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN output_tokens ELSE 0 END), 0) AS null_output_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN cache_read_tokens ELSE 0 END), 0) AS null_cache_read_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN cache_create_5m_tokens ELSE 0 END), 0) AS null_cache_create_5m_tokens,
             COALESCE(SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0
                               THEN cache_create_1h_tokens ELSE 0 END), 0) AS null_cache_create_1h_tokens
        FROM messages
       WHERE type = 'assistant' {rng}
       GROUP BY date(timestamp, 'localtime'), COALESCE(model, 'unknown')
       ORDER BY date, model
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def session_turns(db_path, session_id: str) -> list:
    """Ordered rows of one session, with the raw stored cost per row.

    Returns the same columns as before the extraction plus the raw
    ``cost_usd`` (no pricing here — the caller computes the effective
    per-message cost via effective_message_cost()). ``session_id`` is a
    bound parameter.
    """
    sql = """
      SELECT uuid, parent_uuid, type, timestamp, model, is_sidechain, agent_id,
             input_tokens, output_tokens, cache_read_tokens,
             cache_create_5m_tokens, cache_create_1h_tokens,
             prompt_text, prompt_chars, tool_calls_json, project_slug, cwd,
             cost_usd
        FROM messages
       WHERE session_id = ?
       ORDER BY timestamp ASC
    """
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id,))]


def merge_model_group_cost(row, pricing: dict) -> dict:
    """Merged cost for one model_breakdown group (spec §3, AC-B5/AC-B6).

    ``group_cost = stored_cost + cost_for(model, null-row-tokens).usd_or_0``.
    Rules:

    - ``null_cost_rows == 0`` (no NULL-cost and no zero-cost rows) →
      ``{"usd": stored_cost, "estimated": False}`` — the group is entirely
      stored-cost, nothing was computed.
    - otherwise the computed estimate covers exactly the rows without a
      usable stored cost; when the model cannot be priced
      (``cost_for().usd is None``) its contribution is 0, so a group with
      stored cost still reports the stored cost, not None (AC-B5).
    - an all-NULL group of a priceable model has ``stored_cost == 0`` and
      ``null_*`` sums equal to the all-row totals, so the merge is exactly
      the plain ``cost_for`` result — byte-identical to pre-extraction
      behavior (AC-B5 regression guarantee).

    ``estimated`` reflects only the computed part (AC-B5).
    """
    stored = row["stored_cost"]
    if row["null_cost_rows"] == 0:
        return {"usd": stored, "estimated": False}
    usage = {
        "input_tokens": row["null_input_tokens"],
        "output_tokens": row["null_output_tokens"],
        "cache_read_tokens": row["null_cache_read_tokens"],
        "cache_create_5m_tokens": row["null_cache_create_5m_tokens"],
        "cache_create_1h_tokens": row["null_cache_create_1h_tokens"],
    }
    computed = cost_for(row["model"], usage, pricing)
    return {"usd": stored + (computed["usd"] or 0), "estimated": computed["estimated"]}


def effective_message_cost(cost_usd, model, usage: dict, pricing: dict) -> dict:
    """Effective cost of one message row (AC-B5 session_turns bullet).

    The stored value wins only when it is NOT NULL and > 0 →
    ``{"usd": float(cost_usd), "estimated": False}``. A NULL or 0.0 stored
    cost returns cost_for(model, usage, pricing) unchanged — its ``usd``
    may be None for unpriceable models, which the server renders as it
    does today.
    """
    if cost_usd is not None and cost_usd > 0:
        return {"usd": float(cost_usd), "estimated": False}
    return cost_for(model, usage, pricing)
