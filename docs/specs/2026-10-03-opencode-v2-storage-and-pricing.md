# OpenCode v2 Storage Adapter + Live Pricing Refresh

- **Date:** 2026-10-03
- **Status:** Approved for planning (user decisions recorded 2026-10-03)
- **Scope:** opencode v2 SQLite import path (`session_v2` / `session_message`), native per-message cost storage, real pricing refresh from models.dev
- **Binding constraints:** stdlib only, SQL parameter binding, files ≤ ~400 lines, tests fully offline

---

## Objective

OpenCode v2 (deployed 2026-10-02) froze the legacy `session` / `message` / `part`
tables the dashboard adapter reads and now writes to `session_v2` /
`session_message` with inline content. The dashboard therefore shows no new
opencode data since the upgrade. This spec restores opencode ingestion by adding
a v2 import path alongside the legacy one, stores opencode's native per-message
USD cost instead of recomputing it, and adds a real pricing refresh from
https://models.dev/api.json (with a 7-day TTL cache and manual trigger) so
computed costs stay accurate for v1 and Claude Code messages.

---

## Context

### The problem

- Legacy `message` table `MAX(time_created)` = 2026-10-02 (the upgrade date).
  v2 writes only to new tables; the current adapter (`opencode_source.py`,
  316 lines) reads only legacy tables, so post-upgrade opencode activity never
  reaches the internal dashboard DB (`~/.claude/token-dashboard.db`).
- All pricing today is computed at query time from the bundled `pricing.json`
  (19 models + `tier_fallback` + `plans`) via `pricing.cost_for`. There is no
  refresh mechanism, and opencode v2 now records a real per-message cost that
  the dashboard ignores.

### Live v2 source schema (verified by direct DB inspection)

- `session_v2` (8,792 rows, ACTIVE) — columns include `id`, `directory`,
  `parent_id`, `title`, `agent`, `model`, `cost`, `tokens_*`, `time_created`,
  `time_updated`. `directory` and `parent_id` carry the same semantics as the
  v1 `session` table (cwd; non-null `parent_id` ⇒ sidechain).
- `session_message` (221k rows, ACTIVE) — columns: `id`, `session_id`, `type`,
  `seq`, `time_created`, `time_updated`, `data` (TEXT JSON). `seq` provides
  per-session ordering; `time_created` is epoch ms (same units as legacy).
- Legacy `session` (8,353 rows), `message` (205k), `part` (829k) — FROZEN at
  2026-10-02, v1 format, still present with historical data. v2 messages have
  **no** rows in `part` — content is inline in `session_message.data`.
- `session_message.type` values observed: `user`, `assistant`, `idle`
  (per API docs also AgentSelected, ModelSelected, LocationSwitched,
  Synthetic, System, Skill, Shell, Compaction). **Only `user` and
  `assistant` are imported; everything else is skipped.**

### v2 message JSON shapes (verified samples)

Assistant `data` top-level keys: `{agent, content, cost, finish, rawFinish,
model, snapshot, time, tokens}`:

- `time`: `{created, streamed, completed}` — epoch ms.
- `model`: **nested object** `{"id": "glm-5.3-flash", "providerID":
  "ollama-cloud", "variant": "default"}` — v1 had a flat `modelID` string.
- `tokens`: `{"input": 479, "output": 2332, "reasoning": 0, "cache":
  {"read": 59072, "write": 0}}` — SAME shape as v1.
- `cost`: REAL per-message USD cost — NEW, computed by opencode itself.
- `finish`: `"stop"` — stop reason, NEW.
- `content`: array of inline parts, each `{"type": "reasoning"|"text"|"tool", ...}`:
  - text: `{"type": "text", "text": "..."}`
  - reasoning: `{"type": "reasoning", "text": "...", "state": {...}, "time": {...}}`
  - tool: `{"type": "tool", "id": "call_o0hpy0wl", "name": "read",
    "executed": false, "state": {"status": "completed", "input": {...}, "output": ...}}`
- **No `parentID` key** in v2 assistant data (v1 had it; the internal
  `parent_uuid` must be derived — see Design Decisions §4).

User `data`: `{"time": {"created": ...}, "text": "..."}` — text INLINE
(v1 needed a `part` lookup).

### Current v1 adapter contract (opencode_source.py)

- Sessions: `SELECT id, directory, parent_id FROM session`.
- Messages: `SELECT id, session_id, time_created, data,
  json_extract(data,'$.role') FROM message WHERE
  json_extract(data,'$.time.created') > ? ORDER BY time_created`.
- User prompt text: `part WHERE message_id=? AND json_extract(data,'$.type')='text'`.
- Tool calls: `part WHERE json_extract(p.data,'$.type')='tool' AND p.time_created > ?`.
- v1 JSON paths: `data['role']`, `data['modelID']` (flat), `data['tokens']`,
  `data['agent']`, `data['parentID']`, part `state.input/output/status/error`,
  part `tool` (name).
- Upserts: messages by PK `uuid = message.id` (INSERT OR REPLACE);
  tool_calls by UNIQUE `(source, part_id)` (index
  `idx_tool_calls_source_part_id`).
- Watermark: kv row `opencode_last_import_ts` in the internal `plan` table
  (epoch ms), compared against `$.time.created`; `_latest_import_ts` takes
  MAX over both `messages` and `tool_calls` for the opencode source.
- The source connection is opened **read-write** today
  (`sqlite3.connect(path)`) — must become read-only (active 26 GB WAL DB).

### Internal schema and migration precedent (db.py, 434 lines)

- `messages` columns: `uuid` (PK), `parent_uuid`, `session_id`,
  `project_slug`, `cwd`, `type`, `is_sidechain`, `agent_id`, `timestamp`,
  `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_create_5m_tokens`, `cache_create_1h_tokens`, `prompt_text`,
  `prompt_chars`, `source`, plus always-NULL-for-opencode `git_branch`,
  `cc_version`, `entrypoint`, `stop_reason`, `prompt_id`, `tool_calls_json`,
  `message_id`. **No cost column** — cost is computed at query time.
- Migration precedent: `_migrate_add_message_id`, `_migrate_add_source`,
  `_migrate_add_tool_part_id` — each checks table existence via
  parameterized `sqlite_master` query, then `PRAGMA table_info` before
  `ALTER TABLE ... ADD COLUMN`. Migrations run inside `init_db` before
  `executescript(SCHEMA)`. Tested in `tests/test_db_migration.py`.
- `db.py` is already over the ~400-line limit (434). AGENTS.md: "Prefer
  extracting queries over growing it further."

### Pricing module contract (pricing.py, 66 lines)

- `load_pricing(path)` → parsed bundled `pricing.json`.
- `cost_for(model, usage, pricing)` → `{usd, estimated, breakdown}` using
  `pricing["models"][model]` or `tier_fallback`; `usd=None` when no match.
- `pricing.json` top level: `models` (per-model `{tier, input, output,
  cache_read, cache_create_5m, cache_create_1h, estimated?}`), `tier_fallback`
  (same rate shape keyed by tier), `plans`. Per-model rate keys are keyed by
  **bare model id** (e.g. `glm-5.2`, `kimi-k2.7-code`).
- Env vars are read **ONLY in `cli.py`** (hard project rule) — nothing in
  `token_dashboard/` reads the environment.

---

## Files

| Path | Action | Purpose |
|------|--------|---------|
| `token_dashboard/opencode_source.py` | Modify | Keep v1 leg; `import_opencode` becomes the orchestrator: table auto-detection, v1-then-v2 execution order, shared watermark; lazy-import the v2 leg |
| `token_dashboard/opencode_v2_source.py` | Create | v2 import leg: `session_v2` + `session_message`, inline content parsing, native cost, parent derivation |
| `token_dashboard/db.py` | Modify | Add `cost_usd REAL` to `SCHEMA` + `_migrate_add_cost_usd` migration; **no new query functions** (extract instead — see Design Decisions §9) |
| `token_dashboard/queries.py` | Create | Cost-preferring aggregation queries extracted from `db.py` (starting with `model_breakdown`) |
| `token_dashboard/pricing.py` | Modify | `refresh_pricing`, `maybe_refresh_pricing` (TTL), `load_effective_pricing` (bundled ⊕ cache merge), models.dev → internal rate transform |
| `cli.py` | Modify | New `pricing` subcommand (`--refresh`), auto-refresh hook in `cmd_dashboard`, `PRICING_URL` env read (cli.py only) |
| `token_dashboard/server.py` | Modify (conditional) | Only if a displayed cost endpoint needs the stored-first pattern; switch imports to `queries.py` where functions moved |
| `tests/test_opencode_source.py` | Modify | `FakeOpencodeV2Db` fixture (v2 shapes: nested model, inline content, cost, finish) + dual-import / auto-detect / watermark tests |
| `tests/test_pricing.py` | Modify | Refresh from `file://` URL, TTL logic, merge precedence, offline fail-open |
| `tests/test_db_migration.py` | Modify | `cost_usd` migration on a pre-existing legacy DB |
| `tests/fixtures/models-dev-sample.json` | Create | models.dev-shaped catalog fixture for offline refresh tests |
| `AGENTS.md`, `CLAUDE.md` | Modify (small, final task) | Note v2 tables, new modules, `pricing` subcommand, pricing cache |

---

## Design Decisions

User-approved 2026-10-03 — decisions 1–5 below are binding; 6–11 derive the
implementation details needed to satisfy them within repo constraints.

### 1. Dual import v1 + v2 with a single shared watermark (binding)

`import_opencode` runs BOTH legs when both table sets exist; auto-detection
selects what exists (fresh v2-only installs may lack legacy tables; upgraded
installs have both). One incremental watermark — the existing kv row
`opencode_last_import_ts`, epoch ms — continues to serve both legs.

**Execution order within one `import_opencode` call is fixed:**

1. Compute `since_ts` from `_latest_import_ts` (unchanged logic).
2. Run the **v1 leg** (legacy `message`/`part` import) with `since_ts` if
   legacy tables exist.
3. Recompute the watermark from the internal DB (MAX over opencode
   messages + tool_calls, as today) — call it `since_ts2`.
4. Run the **v2 leg** (`session_message` import) with `since_ts2` if v2
   tables exist.
5. Persist the final watermark once at the end.

This ordering makes the fresh-internal-DB-with-both-tables case safe: the v1
leg backfills history first and advances the watermark to the legacy freeze
point (~2026-10-02), so the v2 leg imports only rows newer than the legacy
history — no double-counted history even if v2 re-keyed message ids. The
upgraded-machine case (existing internal DB) is equally safe: the watermark is
already at/after the freeze point, the v1 leg finds nothing new, and the v2
leg picks up everything after it. UUID upserts (`INSERT OR REPLACE` on
`messages.uuid`) make any residual id overlap idempotent.

**Auto-detection** uses one parameterized `sqlite_master` query:
v1 leg requires tables `session` AND `message` (the v1 tool sub-leg requires
`part`; if `part` is absent, import v1 messages and skip v1 tools); v2 leg
requires `session_v2` AND `session_message`. No table-name interpolation into
SQL outside parameter binding.

The summary dict stays backward compatible: `sessions`, `messages`,
`tool_calls` remain TOTALS across both legs; add per-leg diagnostic keys
`v1_messages` / `v2_messages` / `v2_tool_calls` so `cmd_scan` output and
tests can distinguish legs.

### 2. Native cost preferred: `cost_usd` column (binding)

Add a nullable `cost_usd REAL` column to the internal `messages` table:

- New migration `_migrate_add_cost_usd` in `db.py`, following the
  `_migrate_add_tool_part_id` precedent exactly (table-exists check via
  parameterized `sqlite_master`, `PRAGMA table_info` column check,
  `ALTER TABLE messages ADD COLUMN cost_usd REAL`, commit). Existing rows
  are NOT cleared or rewritten — they keep `cost_usd NULL` and stay on the
  computed path.
- Add `cost_usd REAL` to the `SCHEMA` constant's `CREATE TABLE messages` so
  fresh DBs get it directly from `executescript`.
- The v2 leg stores `data['cost']` there when the value is numeric
  (int/float); anything else (missing, null, non-numeric) stores NULL.
- **Query-time preference (user decision, 2026-10-03):** queries prefer the
  stored `cost_usd` only when it is NOT NULL **AND > 0**. A stored
  `cost_usd = 0.0` (subscription/flat providers, e.g. `ollama-cloud` —
  verified: 148,482 of ~198.7k assistant rows) falls back to the computed
  estimate via pricing. The column still STORES 0.0 (zero is real data); the
  preference rule changes at query time — see §3.

### 3. Stored-cost-first query pattern

Every cost aggregation prefers stored `cost_usd` **per row** only when it is
NOT NULL **AND > 0** (user decision, 2026-10-03): a row with stored
`cost_usd > 0` contributes its stored cost; a row with NULL **or a stored
`cost_usd = 0.0`** contributes pricing-computed cost from its tokens
(subscription/flat providers such as `ollama-cloud` report 0 — verified:
148,482 of ~198.7k assistant rows). Because aggregation happens in SQL but
computation happens in Python, queries return BOTH parts:

- `stored_cost` = `COALESCE(SUM(cost_usd), 0)` over the group (zero-cost rows
  contribute 0, so the sum is unchanged);
- token sums restricted to rows with no usable stored cost —
  `cost_usd IS NULL OR cost_usd = 0` — e.g.
  `SUM(CASE WHEN cost_usd IS NULL OR cost_usd = 0 THEN input_tokens ELSE 0 END) AS null_input_tokens`
  (same for output / cache_read / cache_create_5m / cache_create_1h).

The caller computes `group_cost = stored_cost +
cost_for(model, null-row-tokens).usd_or_0`. `model_breakdown` is the primary
target (its docstring today says "Caller computes cost via pricing"); the
same pattern applies to any other endpoint that displays cost — the plan
enumerates them after reading `server.py`. Per-message displays
(`session_turns`) expose the effective cost alongside tokens — `cost_usd`
when > 0, the computed estimate when NULL or 0 (user decision, 2026-10-03).
v1 and Claude Code rows (always NULL) behave exactly as today.

### 4. v2 message mapping

One row per `session_message` row with `type IN ('user','assistant')` — the
type filter is in SQL, all other rows (`idle`, `Synthetic`, `System`, …) are
never read. Query:

```sql
SELECT id, session_id, seq, time_created, data, type
  FROM session_message
 WHERE time_created > ? AND type IN ('user','assistant')
 ORDER BY time_created
```

(Parameters bound; ordering by `time_created` keeps the watermark monotonic,
same contract as the v1 leg.) Field mapping into internal `messages`:

- `uuid` = `session_message.id` (PK upsert, unchanged mechanism).
- `session_id`, `project_slug`, `cwd` = from `session_v2` lookup
  (`SELECT id, directory, parent_id FROM session_v2`); `is_sidechain` =
  1 iff `session_v2.parent_id` non-null. Same `_encode_slug`/`_project_slug`
  helpers as v1.
- `type` = the `type` column (`user` / `assistant`) — internal semantics
  match (`overview_totals` counts `type='user'` as turns).
- `timestamp` = `_format_timestamp(data['time']['created'])` — same helper.
- `agent_id` = `data['agent']`.
- `model` = `data['model']['id']` (NESTED in v2; take the bare id, e.g.
  `"glm-5.3-flash"` — matches the bundled `pricing.json` key convention;
  `providerID` is discarded). Defensive fallback: if `data['model']` is a
  plain string (shape drift), use it as-is.
- `stop_reason` = `data['finish']` (NEW in v2; the column exists and was
  always NULL for opencode).
- `input_tokens` / `output_tokens` / `cache_read_tokens` /
  `cache_create_5m_tokens` = `data['tokens']` + nested
  `data['tokens']['cache']`, same extraction as v1; `cache_create_1h_tokens`
  = 0; `reasoning` tokens are not stored (unchanged).
- `cost_usd` = `data['cost']` when numeric, else NULL (§2).
- `prompt_text` / `prompt_chars` (user messages only) = inline
  `data['text']` — no `part` lookup; NULL when absent.
- `parent_uuid` — **derived**: v2 assistant data has no `parentID`. For
  each imported v2 message, run
  `SELECT id FROM session_message WHERE session_id=? AND seq<? AND type IN ('user','assistant') ORDER BY seq DESC LIMIT 1`
  (parameterized) and store the predecessor's id, or NULL when there is
  none. The `type IN ('user','assistant')` filter (approved 2026-10-03 —
  see EC-17) skips non-imported predecessor rows. This reproduces v1
  semantics (`expensive_prompts` joins
  `a.parent_uuid = u.uuid`) at a cost of one lookup per NEW message —
  negligible for incremental runs.
- `git_branch`, `cc_version`, `entrypoint`, `prompt_id`, `tool_calls_json`:
  NULL (unchanged).

### 5. v2 tool calls from inline content

For each imported v2 **assistant** message, parse `data['content']` (array)
and take items with `"type" == "tool"`:

- `part_id` = `f"{message_id}:{item['id']}"` — the call id namespaced by its
  parent message (e.g. `"<message_id>:call_o0hpy0wl"`), approved 2026-10-03
  (see EC-9); feeds the existing UNIQUE `(source, part_id)` upsert.
- `tool_name` = item `"name"` (**v2 key is `name`; v1 used `tool`**).
- `state` = item `"state"` (`input` / `output` / `status` / `error` — same
  shapes as v1 part state). Reuse `_extract_tool_target` +
  `_TOOL_TARGET_FIELDS` unchanged; `result_tokens = len(output) // 4`;
  `is_error = status != 'completed' or error is not None` (same rule).
- `timestamp` = the parent message's `time_created` (v2 tool items carry no
  independent timestamp).
- `executed` is ignored (it means agent-side execution, not success/failure).

### 6. Read-only source connection

`import_opencode` opens the opencode DB read-only:
`sqlite3.connect(f"file:{path}?mode=ro", uri=True)` — for BOTH legs. The
source is a live 26 GB WAL database owned by a running opencode; the adapter
must never be able to write it. Path interpolation here is internal (same
`path` value that today's `sqlite3.connect` receives), never user SQL input.

### 7. Pricing refresh — source, transport, cache (binding)

- **Source:** `https://models.dev/api.json` — the model catalog opencode
  itself uses; provider-keyed JSON with per-model cost (input, output, cache
  read/write) per MTok. Fetched with `urllib.request` (stdlib). No scraping,
  no HTML parsing.
- **Cache file:** `<internal_db_dir>/pricing-cache.json` — a sibling of the
  internal DB path, derived by `cli.py` (which already knows the DB path).
  Shape:
  `{"fetched_at": <unix seconds>, "source_url": "<url>", "models": {<model_id>: {<rate row>}}}`
  where each rate row matches the bundled per-model shape exactly
  (`input`, `output`, `cache_read`, `cache_create_5m`, `cache_create_1h`,
  optional `tier`, optional `estimated`).
- **Transform:** a defensive `models.dev → internal rate row` converter.
  Walk providers in sorted order; within each provider, walk models; map
  `cache_write → cache_create_5m` AND `cache_create_1h` (bundled convention
  sets both equal); skip any entry missing required numeric fields; model
  key = the catalog's bare model id. On id collisions across providers, the
  first provider in sorted order wins (deterministic). The transform NEVER
  raises on unexpected shapes — it skips what it cannot fully parse.
  Refreshed rows carry no `estimated` flag (catalog is authoritative);
  `cost_for` already defaults `estimated` to False.
- **Effective pricing = bundled ⊕ cache merge:** `load_effective_pricing(bundled_path, cache_path)`
  returns the bundled dict with `models` overlaid by cache models (cache wins
  per model id). `tier_fallback` and `plans` ALWAYS come from the bundled
  file (a bad fetched catalog must never break plan-aware formatting).
  Missing/corrupt cache → bundled only.
- **Env override:** `PRICING_URL` (default the https URL above), read ONLY in
  `cli.py` and passed as a parameter — the project rule that env vars are
  read only in `cli.py` is absolute. `urllib` supports `file://`, which
  makes the override double as the offline-test hook.

### 8. Refresh triggers (binding)

- **(a) MANUAL** — `python3 cli.py pricing --refresh`: force refresh now,
  then print cache status (fetched_at, source, model count). Plain
  `python3 cli.py pricing` prints status without refreshing. The subcommand
  accepts the shared common flags (`--db` etc. — parent parser) so the cache
  path derives correctly.
- **(b) AUTOMATIC, 7-day TTL** — at `cmd_dashboard` startup, before the
  server runs: if the cache exists and `fetched_at` is older than
  `604800` seconds, refresh.
- **(c) FIRST RUN** — when no cache file exists, refresh at first dashboard
  startup.
- All three funnel into one function, `maybe_refresh_pricing(url, cache_path,
  force=False)`. **Fail-open everywhere:** fetch timeout 10 s, any
  exception (network, HTTP error, JSON error, transform edge) is caught,
  a one-line note goes to stderr, and the pipeline continues with the
  existing cache (or bundled only). Refresh NEVER blocks, crashes, or
  delays dashboard startup beyond the 10 s timeout, and NEVER touches the
  bundled `pricing.json` file (the cache is a separate file; the bundle
  stays the offline floor).
- `PRICING_URL` pointing at a `file://` path is what tests use — zero
  network in unittest (binding).

### 9. Line-limit discipline (db.py 434 → must shrink, not grow)

- **db.py:** `_migrate_add_cost_usd` + `SCHEMA` line add ~10 lines of growth.
  Compensate by MOVING the cost-preferring aggregation functions out: the
  first extraction is `model_breakdown` → new module `token_dashboard/queries.py`
  (§3 pattern), with `server.py` (and any test) imports updated. Any further
  `db.py` query function that gains cost awareness moves to `queries.py` in
  the same commit — net effect: `db.py` ends ≤ 434 and ideally lower.
  `db.py` keeps schema, migrations, `connect`, slug/name helpers, and
  untouched queries.
- **opencode_source.py (316):** the v2 leg lives in the NEW module
  `token_dashboard/opencode_v2_source.py` (~150 lines), which imports the
  shared helpers (`_project_slug`, `_format_timestamp`,
  `_extract_tool_target`, `_TOOL_TARGET_FIELDS`, watermark helpers) FROM
  `opencode_source`. The orchestrator `import_opencode` stays in
  `opencode_source.py` and lazy-imports the v2 entry point inside the
  function body (precedent: `cli.py` lazy-imports `import_opencode`) to
  avoid a circular import. Both modules stay under ~400 lines.
- **pricing.py (66):** grows to ~170 with refresh/transform/merge — under
  the limit, no split needed.

### 10. Test strategy (offline, binding decision 5)

- `tests/test_opencode_source.py` gains a `FakeOpencodeV2Db` fixture mirroring
  the live v2 shapes exactly: `session_v2` + `session_message` tables,
  nested model object, inline content array (text/reasoning/tool), inline
  user text, `cost`, `finish`, `type` column with non-imported types
  (`idle`), and a both-table-sets layout for dual-import tests. Existing
  `FakeOpencodeDb` stays for the v1 leg.
- `tests/test_pricing.py` covers: refresh from a `file://` fixture
  (`tests/fixtures/models-dev-sample.json`), TTL fresh/stale boundaries,
  merge precedence (cache model wins, bundled-only models survive,
  `tier_fallback`/`plans` untouched), and fail-open (unreachable URL → no
  crash, bundled pricing used, no cache written).
- `tests/test_db_migration.py` gains a `cost_usd` migration case
  (pre-existing messages table without the column → column added, rows
  intact; rerun → no-op).
- All new tests pass explicit paths (temp dirs) — no env patching, no
  network.

### 11. Documentation (small, final task)

`AGENTS.md` + `CLAUDE.md` get short notes: v2 table names and frozen legacy
tables, the `pricing` subcommand and `PRICING_URL`, the pricing cache
location, and the new modules (`opencode_v2_source.py`, `queries.py`).
`docs/KNOWN_LIMITATIONS.md` is updated only if a § Edge Cases item is
accepted as a known limitation rather than fixed.

## Acceptance Criteria

The work is done when every statement below holds and is checked by an offline unittest, or, where noted, by a manual check.

### (a) v2 dual-schema import

- **AC-A1 — Auto-detection.** `import_opencode` finds tables with a single parameterized `sqlite_master` query (`type='table' AND name IN (?, ?, ...)`). It runs:
  - the v1 message leg only if `session` and `message` both exist;
  - the v1 tool sub-leg only if `part` also exists;
  - the v2 leg only if `session_v2` and `session_message` both exist.

  Table names are never interpolated into SQL. A source DB with none of these tables returns a summary of all zeros and raises nothing.
- **AC-A2 — Row selection.** The v2 leg reads `session_message` only through `WHERE time_created > ? AND type IN ('user','assistant') ORDER BY time_created`, with parameters bound. Each matching row produces exactly one internal `messages` row with `uuid = message_id = session_message.id` and `source = 'opencode'`. Rows of any other `type` never produce an internal row.
- **AC-A3 — Session fields.** `session_id`, `cwd`, `project_slug` and `is_sidechain` come from a `SELECT id, directory, parent_id FROM session_v2` lookup. `project_slug` uses `_project_slug`. `is_sidechain = 1` exactly when `parent_id` is non-null.
- **AC-A4 — Model.** `model = data['model']['id']` (for example `"glm-5.3-flash"`); `providerID` and `variant` are dropped. If `data['model']` is a plain string, it is stored as-is. If it is missing or another type, `model` is NULL.
- **AC-A5 — Tokens.** These come from `data['tokens']` and the nested `data['tokens']['cache']`:

  | Internal column | Source |
  |---|---|
  | `input_tokens` | `input` |
  | `output_tokens` | `output` |
  | `cache_read_tokens` | `cache.read` |
  | `cache_create_5m_tokens` | `cache.write` |
  | `cache_create_1h_tokens` | always `0` |

  `reasoning` is not stored. A missing key defaults to 0.
- **AC-A6 — Other columns.**
  - `stop_reason = data['finish']`, or NULL when absent.
  - `agent_id = data['agent']`.
  - `timestamp = _format_timestamp(data['time']['created'])`.
  - For user rows, `prompt_text = data['text']` and `prompt_chars = len(prompt_text)`, read inline with no `part` query.
  - `git_branch`, `cc_version`, `entrypoint`, `prompt_id` and `tool_calls_json` are NULL.
- **AC-A7 — Parent derivation.** `parent_uuid` is the `id` of the nearest earlier row in the same session (`seq < current.seq`, highest `seq` first), or NULL if there is none. The query is parameterized and uses the existing UNIQUE index `(session_id, seq)`. See Edge Case EC-17 for the type filter on the predecessor. A test confirms that `expensive_prompts` links a v2 user prompt to the assistant row that immediately follows it.
- **AC-A8 — Tool calls.**
  - For each imported **assistant** row, every `data['content']` item with `type == 'tool'` produces one `tool_calls` row:
    - `tool_name = item['name']`;
    - target comes from the unchanged `_extract_tool_target` / `_TOOL_TARGET_FIELDS`;
    - `result_tokens = len(output) // 4` when `output` is a string, else 0;
    - `is_error = 1` when `status != 'completed'` or `error is not None`;
    - `timestamp` is the parent message's `time_created`;
    - `message_uuid` is the parent message id;
    - `source = 'opencode'`.
  - `executed` is ignored.
  - `part_id` follows Edge Cases EC-9 and EC-10.
  - Reasoning and text items never produce tool rows. User rows are never scanned for tools.
- **AC-A9 — Single watermark and order.** One call runs these steps in order:
  1. `since_ts = _latest_import_ts(...)`.
  2. The v1 leg, using `since_ts`.
  3. `since_ts2 = _latest_import_ts(...)`, recomputed from the internal DB.
  4. The v2 leg, using `since_ts2`.
  5. The kv row `opencode_last_import_ts` is written once.
  6. A single commit.

  No other watermark key is created. If either leg raises, nothing from that call is committed and the watermark is not advanced.
- **AC-A10 — No double-count on a fresh internal DB.** Use a fixture with both table sets, where v2 contains migrated history using the same ids as legacy plus newer v2-only rows (this mirrors the live data, see EC-3). Importing into an empty internal DB gives:
  - each legacy message exactly once;
  - each newer v2 message exactly once;
  - `COUNT(*)` of opencode messages equal to the size of the union.

  Running the import a second time adds 0 rows and leaves token sums unchanged.
- **AC-A11 — Summary dict.** `sessions`, `messages` and `tool_calls` are totals across both legs. The diagnostic keys `v1_messages`, `v2_messages` and `v2_tool_calls` are present and correct for v2-only, legacy-only and dual fixtures. `cmd_scan` prints totals as it does today.
- **AC-A12 — Read-only source.** Both legs use one connection opened as `sqlite3.connect(<file URI>?mode=ro, uri=True)` (see EC-21 for URI escaping). A unit test proves that a write through the connection helper raises `sqlite3.OperationalError`. Running the import leaves the source fixture file's bytes unchanged (same hash before and after).

### (b) Native cost

- **AC-B1 — Schema.** `SCHEMA`'s `CREATE TABLE messages` includes `cost_usd REAL`, so a fresh `init_db` creates the column.
- **AC-B2 — Migration.** `_migrate_add_cost_usd` follows the `_migrate_add_tool_part_id` pattern:
  - a parameterized `sqlite_master` check that the table exists;
  - a `PRAGMA table_info` check for the column;
  - `ALTER TABLE messages ADD COLUMN cost_usd REAL`;
  - commit.

  It runs in `init_db` before `executescript(SCHEMA)`. In `tests/test_db_migration.py`, a messages table without the column gains it, existing rows keep their values with `cost_usd IS NULL`, and running it again does nothing and raises nothing.
- **AC-B3 — Storage.**
  - The v2 leg stores `data['cost']` as a float when it is a finite `int` or `float` (but not a `bool`). That includes `0` and `0.0`.
  - Missing, null, non-numeric, bool, NaN or infinite values are stored as NULL.
  - The v1 leg and the Claude scanner always leave `cost_usd` NULL.
- **AC-B4 — Stored-cost-first queries.**
  - `model_breakdown` moves to `token_dashboard/queries.py`.
  - For each group it returns `stored_cost = COALESCE(SUM(cost_usd), 0)` plus token sums limited to rows where the stored cost is not usable — `cost_usd IS NULL OR cost_usd = 0` (the `> 0` preference rule, user decision 2026-10-03) (`null_input_tokens`, `null_output_tokens`, `null_cache_read_tokens`, `null_cache_create_5m_tokens`, `null_cache_create_1h_tokens`).
  - The existing all-row token totals are still returned, so current token displays don't change.
- **AC-B5 — Endpoints.**
  - `/api/overview` and `/api/by-model` compute each group as `stored_cost + (cost_for(model, null-row tokens).usd or 0)`.
  - A group made only of NULL-cost rows (v1 or Claude) gives exactly the same `cost_usd` and `cost_estimated` as before the change.
  - A group with stored cost but a model that can't be priced reports the stored cost, not `None`.
  - `cost_estimated` reflects only the computed part. It is False when the group has no rows without usable stored cost (no NULL-cost and no zero-cost rows).
  - `/api/prompts` (a cache-read-only estimate, not a message total) keeps computed pricing.
  - `session_turns` exposes the effective cost on each row — `cost_usd` when > 0, the computed estimate when NULL or 0.
- **AC-B6 — Mixed fixture.** One model with one v2 row (`cost_usd = 0.42`) and one v1 row (NULL, priced through `pricing.json`) reports exactly `0.42 + computed(v1 row)`.
- **AC-B7 — Zero-cost fallback (user decision, 2026-10-03).** A v2 message with `cost_usd = 0.0` displays the computed estimate, not 0 (unit test).

### (c) Pricing refresh from models.dev

- **AC-C1 — Transport.** Fetching uses only `urllib.request` with a 10 s timeout. Schemes other than `http`, `https` and `file` are rejected and handled like any other refresh failure (fail-open).
- **AC-C2 — URL override.** `cli.py` reads `PRICING_URL` (an empty value falls back to `https://models.dev/api.json`) and passes it as a parameter. A grep shows no `os.environ` or `getenv` anywhere under `token_dashboard/`.
- **AC-C3 — Cache path.** The cache is `Path(<internal DB path>).parent / "pricing-cache.json"`, derived in `cli.py` from the same value `_db_path(args)` returns. Its shape is `{"fetched_at": <unix seconds>, "source_url": <url>, "models": {<id>: <rate row>}}`, and each rate row has numeric `input`, `output`, `cache_read`, `cache_create_5m` and `cache_create_1h`.
- **AC-C4 — Transform.**
  - The models.dev → rate-row converter walks providers in sorted order.
  - Rate fields map as follows:

    | Cache rate field | models.dev field | If missing |
    |---|---|---|
    | `input` | `cost.input` | entry skipped |
    | `output` | `cost.output` | entry skipped |
    | `cache_read` | `cost.cache_read` | `0.0` |
    | `cache_create_5m` | `cost.cache_write` | `0.0` |
    | `cache_create_1h` | `cost.cache_write` | `0.0` |

  - `input` and `output` must be finite non-bool numbers or the entry is skipped.
  - Unknown fields are ignored.
  - When the same model id appears under several providers, the first provider in sorted order wins.
  - The converter never raises; this is proven by tests that feed it non-dict providers, non-dict models, a non-dict `cost` and string prices.
- **AC-C5 — Writing the cache.**
  - A refresh writes the cache only if the transform produced at least one model.
  - The write is atomic: a temp file in the same directory, then `os.replace`.
  - A failed refresh leaves any existing cache byte-identical and never creates a partial file.
  - Refresh never writes the bundled `pricing.json` (its hash is unchanged after every refresh test).
- **AC-C6 — Merge.** `load_effective_pricing(bundled_path, cache_path)` returns the bundled dict with `models` overlaid by the cache, cache winning per model id:
  - models only in the bundle survive unchanged;
  - models only in the cache are added;
  - `tier_fallback` and `plans` always come from the bundle, even if the cache file contains those keys.

  A missing or corrupt cache gives exactly the bundled dict.
- **AC-C7 — Server.** The server builds its pricing with `load_effective_pricing`, using a cache path passed from `cli.py`. `/api/plan` returns the effective pricing.
- **AC-C8 — Manual refresh.**
  - `python3 cli.py pricing` prints `fetched_at`, `source_url` and model count, or "no cache — bundled pricing only". It exits 0 and never fetches.
  - `python3 cli.py pricing --refresh` refreshes regardless of TTL and then prints the status.
  - If a manual refresh fails, it prints a one-line stderr note and the unchanged status, exits 1, and prints no traceback.
  - The subcommand accepts the shared parent-parser flags (`--db`, ...).
- **AC-C9 — Automatic refresh.** `cmd_dashboard` calls `maybe_refresh_pricing(url, cache_path)` before `build_handler`, with or without `--no-scan`. It refreshes when:
  - the cache file is missing (first run);
  - the cache is corrupt;
  - `now - fetched_at >= 604800`;
  - `now - fetched_at < 0` (clock skew).

  Otherwise it does not fetch. `maybe_refresh_pricing` takes an injectable clock (keyword `now=None`) so the TTL boundary can be tested without patching.
- **AC-C10 — Fail-open.** With `PRICING_URL` set to a file that doesn't exist, an unroutable `http://` URL, or a file containing invalid JSON:
  - no exception escapes `maybe_refresh_pricing`;
  - exactly one stderr line is written;
  - the dashboard handler builds and serves `/api/overview` using bundled or existing cached pricing.

  Startup is delayed by at most the 10 s timeout.

### (d) Tests and regression

- **AC-D1.** `python3 -m unittest discover tests` passes fully offline (no network interface needed). Every new test uses temp dirs and explicit paths, and refresh tests use `Path(...).as_uri()` pointing at `tests/fixtures/models-dev-sample.json`.
- **AC-D2.** All 103 existing tests still pass. Their assertions are unchanged; the only edits allowed are import-path updates for functions moved to `queries.py`.
- **AC-D3.** Line counts:

  | File | Limit |
  |---|---|
  | `db.py` | ≤ 434 (no net growth) |
  | `opencode_source.py` | ≤ ~400 |
  | `opencode_v2_source.py` | ≤ ~400 (target ~150) |
  | `pricing.py` | ≤ ~400 (target ~170) |
  | `queries.py` | ≤ ~400 |

- **AC-D4 (manual).** Running `cli.py scan --backend opencode` against the live DB with a copy of the internal DB:
  - imports v2 rows newer than the legacy freeze point;
  - leaves the source DB's modification time unchanged.

  `/api/by-model` then shows v2 models (bare ids) with stored cost (zero-cost subscription rows show the computed estimate instead — user decision 2026-10-03).

## Conventions

- **Stdlib only.** No `pip install`, no `requirements.txt`. HTTP goes through `urllib.request` and JSON through `json`. Any third-party dependency needs an explicit argument before it is added.
- **Always bind SQL parameters.** Every value that comes from data or a user (timestamps, ids, `session_id`, `seq`, table names checked against `sqlite_master`, limits) uses `?` or named placeholders. f-strings are only allowed for internal identifiers and fixed SQL fragments, such as the existing `_range_clause` and the `order` pattern. The read-only URI is a connection string, not SQL. It is built from the internal path value and escaped (EC-21).
- **File size and responsibility.** Files stay at or below ~400 lines, each with one clear responsibility:
  - `db.py` must not grow (≤ 434). New cost-aware queries go in `queries.py`, and any `db.py` query that becomes cost-aware moves there in the same commit. `db.py` keeps the schema, migrations, `connect`, slug/name helpers and untouched queries.
  - `opencode_v2_source.py` holds the v2 leg and imports shared helpers from `opencode_source`.
  - `opencode_source.py` keeps the orchestrator and lazy-imports the v2 entry point inside `import_opencode`, to avoid circular imports.
  - `pricing.py` holds fetch, transform, merge and TTL logic.
- **Migrations follow the existing pattern.** Use the `_migrate_add_tool_part_id` structure exactly. Never clear or rewrite existing rows during a migration.
- **Idempotency.** Messages are upserted with `INSERT OR REPLACE` keyed on `messages.uuid`. Tool calls are upserted with `INSERT OR REPLACE` keyed on UNIQUE `(source, part_id)`. Re-running any import on the same data must not change row counts or sums.
- **Tests.** `unittest.TestCase` only; no pytest and no external test libraries. Tests pass paths explicitly (temp dirs, `file://` URIs, injected `now`). Don't monkeypatch `HOME` or the environment; the only exception remains the existing `test_skills_opencode` `Path.home` patch. Fixtures live in `tests/fixtures/`. Tests never touch the network.
- **Environment variables.** Read them only in `cli.py`, including the new `PRICING_URL`. Modules under `token_dashboard/` receive URLs and paths as parameters.
- **Fail-open for optional features.** Pricing refresh failures are caught at the `maybe_refresh_pricing` boundary and reported as one stderr line, with no traceback. Import errors on the source DB still propagate as they do today; there is no silent partial commit.
- **Language.** Code, identifiers, comments, docstrings, docs and commit messages are in English.

## Edge Cases

Items marked **VERIFIED** come from read-only queries run on 2026-10-03 against the live `~/.local/share/opencode/opencode.db` (SQLite 3.51.2).

1. **v2-only opencode DB (no `session`/`message`/`part`).** The v1 leg is skipped. On a fresh internal DB the v2 leg runs with `since_ts = 0` and imports all user and assistant rows. `v1_messages = 0`, and `sessions` counts `session_v2`.
2. **Legacy-only DB (a snapshot from before the upgrade, no `session_v2`).** The v2 leg is skipped. Behavior matches today's adapter exactly, apart from the read-only connection. `v2_messages = v2_tool_calls = 0`.
3. **Both table sets (the normal case).** **VERIFIED:** v2 holds migrated history.
   - The oldest `session_message.time_created` is 1781180491646 (about 2026-06-11).
   - The newest legacy `message.time_created` is 1790848475700 (the freeze, about 2026-10-02).
   - 201,334 `session_message.id` values also exist in `message`.

   The v1-then-v2 order (AC-A9) skips v2 rows at or before the freeze point. Any overlap left over (ids are preserved) is absorbed by the `uuid` upsert. Expected result: no double-counting.
4. **Message types other than `user` and `assistant`.** **VERIFIED** live values are `agent-switched` (38), `model-switched` (3), `location-switched` (1), `synthetic` (299), `system` (7,979), `idle` (743) and `compaction` (43). These are kebab-case and differ from the API-doc names (AgentSelected, ModelSelected, ...). `skill`, `shell` and any future type are handled the same way. The SQL `type IN ('user','assistant')` filter excludes all of them however they are spelled. They are never imported and never touch the watermark.
5. **Assistant message with an empty `content` array.** The message row is imported with tokens and cost. Zero tool rows are produced. If `content` is missing, null or not a list, treat it as empty. List items that aren't dicts are skipped, but they still take up their index position (EC-10).
6. **User message with no `text` key.** `prompt_text` and `prompt_chars` are NULL and the row is still imported (it still counts as a turn). An empty string `""` is also stored as NULL, matching v1's "falsy → NULL". **VERIFIED:** there are currently 0 such rows; this is defensive handling.
7. **Missing `tokens` or `cost` keys, or malformed `data` JSON.** Token columns default to 0 and `cost_usd` is NULL, so the row falls back to computed pricing. Malformed or non-object `data` is treated as `{}` (the same as `_parse_message_data`). The row is still imported, with `type` taken from the column and `timestamp` falling back to the `time_created` column. **VERIFIED:** 38 assistant rows have `cost` null.
8. **Cost present but `0` or `0.0`.** Store `0.0`, not NULL. Zero is real data. **VERIFIED:** 148,482 of about 198.7k assistant rows have `cost = 0`, most likely from subscription or free providers (`ollama-cloud`).
   - **Consequence (user decision, 2026-10-03):** the stored 0.0 is treated as "not usable" at query time — those rows fall back to the computed estimate, so opencode cost totals remain comparable to Claude Code sessions.
   - This follows binding Design Decision §2. Record it in `docs/KNOWN_LIMITATIONS.md` as "opencode v2 rows from subscription providers report cost 0; the dashboard shows the API-equivalent estimate for those rows, not the billed cost."
9. **Duplicate tool call ids across messages.** **VERIFIED:** 248,012 inline tool items carry only 181,003 distinct `id` values. Some providers reuse call ids. If `part_id` were the raw `item['id']` and were upserted on UNIQUE `(source, part_id)`, about 67k tool calls would silently overwrite each other.
   - **Expected behavior:** `part_id = f"{message_id}:{item['id']}"`, namespacing the call id by its parent message. It stays deterministic and idempotent, and v1 `prt_…` ids can't collide with it.
   - Approved 2026-10-03 (brain) — verified collision data (248,012 tool items, 181,003 distinct ids) makes namespacing mandatory.
   - If the same id appears twice in the *same* message, the later item replaces the earlier one. That is acceptable because it is the same call re-emitted.
   - A test covers two messages that share `call_0`, and expects two tool rows.
10. **Inline tool item with no usable `id`.** **VERIFIED:** there are currently 0 such items; this is defensive handling. If the id is missing, null, empty or not a string, use `part_id = f"{message_id}#{index}"`, where `index` is the item's zero-based position in the original `content` array. Using `#` instead of `:` keeps these ids from colliding with EC-9 ids. The same input always gives the same `part_id`, so re-imports stay idempotent.
11. **Tool state `running`, `streaming` or `error`.** **VERIFIED** counts: `completed` 243,018, `error` 4,995, `running` 4, `streaming` 2. The v1 rule applies unchanged: `is_error = 1` whenever `status != 'completed'` or `error is not None`.
    - In-flight calls (`running`, `streaming`) are therefore counted as errors.
    - With no `output`, or a non-string `output`, `result_tokens` is 0.
    - A missing `state` is treated as `{}`, so `is_error` is 1.

    Rows are keyed on `time_created`, which never changes, so a message imported mid-stream isn't revisited once the watermark has passed it (the v1 leg has the same limitation). Partial tokens, cost and tool state stay as captured. Record this in `KNOWN_LIMITATIONS.md` rather than changing the watermark design.
12. **models.dev unreachable, DNS failure or timeout.** Stop after 10 s at most and write one stderr line, for example `pricing refresh skipped: <reason>`. The existing cache is left untouched and the effective pricing is cache ⊕ bundled, or bundled only. The dashboard starts and serves normally, and there is no retry loop. The next startup tries again only if the TTL or no-cache condition still holds.
13. **Malformed models.dev response or schema drift.**
    - Unknown fields are ignored.
    - Entries without numeric `cost.input` or `cost.output` are skipped, including free or local models with no `cost`, which then fall back to bundled or `tier_fallback` pricing.
    - A JSON parse error, a non-dict top level, or a transform that yields 0 models counts as a failure. It is handled fail-open (EC-12) and the cache is not overwritten.
    - When a bare id is priced differently by several providers, the first provider in sorted order wins. This only affects computed rows (v1, Claude, and v2 rows whose stored cost is 0 — see the > 0 rule); v2 rows with stored cost > 0 use native cost.
14. **Corrupt or truncated cache file.** This covers invalid JSON, a non-dict top level, a missing or non-numeric `fetched_at`, or `models` that isn't a dict. Treat it as no cache:
    - `load_effective_pricing` returns the bundled pricing;
    - `maybe_refresh_pricing` takes the first-run path and refreshes;
    - a successful refresh replaces the file atomically.

    A single rate row that is malformed (missing any of the five numeric keys) is dropped on its own, and the rest of the cache still applies.
15. **Cache exactly 7 days old.** With `now - fetched_at == 604800`, refresh (the boundary is inclusive, `>=`). At 604799 there is no fetch. A negative age (`fetched_at` in the future) triggers a refresh, which corrects itself. Tests inject `now`.
16. **`PRICING_URL` pointing at a `file://` fixture.** `urllib` reads it like any URL, and `source_url` in the cache records that URI. Tests build it with `Path(fixture).resolve().as_uri()`. A `file://` path that doesn't exist is a normal fail-open failure (EC-12).
17. **v2 predecessor is a type that isn't imported.** If the previous row by `seq` is a `system`, `idle` or other skipped row, a `parent_uuid` pointing at it would reference a row missing from the internal DB and break the `expensive_prompts` join (`a.parent_uuid = u.uuid`).
    - **Expected behavior:** the predecessor lookup also filters `type IN ('user','assistant')`.
    - Approved 2026-10-03 (brain) — predecessor lookup must filter type IN ('user','assistant').
    - The `(session_id, seq)` index keeps the backward scan short.
    - The first imported row in a session gets NULL.
18. **26 GB source DB with an active WAL.**
    - The `mode=ro` connection is mandatory. Readers don't block opencode's writer, and the import never writes the source.
    - **VERIFIED:** `session_message_time_created_idx` serves the watermark filter, and the UNIQUE `session_message_session_seq_idx` serves the parent lookup, so the first full import does no per-row table scans.
    - A slow first import (minutes) is acceptable. Later incremental runs read only new rows.
    - Rows opencode commits during an import are picked up on the next run.
    - If the `-shm` file is missing and the directory isn't writable, SQLite raises `OperationalError`. It propagates as today, with no commit and no change to the watermark.
19. **Watermark gap after the internal DB is reset or deleted.** `since_ts` falls back to 0 (no rows and no kv entry). The v1 leg re-imports all legacy history, the watermark moves to the freeze point, and the v2 leg imports everything after it. `INSERT OR REPLACE` on `uuid` and `(source, part_id)` makes this safe to repeat. If only the kv row is lost, nothing changes, because `_latest_import_ts` derives from row maxima first.
20. **Watermark has second granularity.** `_latest_import_ts` truncates to whole seconds × 1000, so rows created within the same second as the watermark are read again on the next run. Those reads are idempotent upserts, with no duplicate rows and no lost rows.
21. **Opencode DB path with special characters (`?`, `#`, `%`, spaces) or a relative path.** Build the read-only URI as `Path(path).resolve().as_uri() + "?mode=ro"`, which percent-escapes the path. Don't use the raw `f"file:{path}?mode=ro"` form from §6, because a raw `?` or `#` in the path would truncate it or be misread. A test covers a temp path containing a space and `#`.
22. **Bundled `pricing.json` missing a model that the cache has, and vice versa.** The cache adds the model, and bundled-only models (for example custom `estimated: true` entries) stay. `cost_for` with a refreshed row returns `estimated = False`. Plan-aware formatting (`format_for_user`) is unaffected because `plans` always comes from the bundle.

confidence: MED — I checked the live DB (read-only) for types, cost distribution, history overlap, indexes and tool-id duplication. Not verified: the current models.dev `api.json` shape (whether the field names are `cost.input`/`cache_read`/`cache_write` and whether cache fields can be absent), whether v2 rows are written at stream start or at completion (EC-11), and whether the 67k duplicate tool ids come from providers reusing ids or from v2 storing the same message more than once. EC-9 and EC-17 were approved 2026-10-03 (brain) and are folded into Design Decisions §4 and §5 above.
