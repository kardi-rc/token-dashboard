# Design Spec: opencode Support for token-dashboard

**Date:** 2026-07-16  
**Author:** token-dashboard maintainers  
**Status:** Draft ready for implementation  
**Scope:** Backend adapter only; no UI changes.

## 1. Goal

Extend [token-dashboard](https://github.com/nateherkai/token-dashboard) so it can also ingest session data from [opencode](https://opencode.ai), while keeping the existing Claude Code/JSONL pipeline intact. The result should be a clean fork that opencode users can run without configuration changes, and that existing Claude Code users can keep using exactly as before.

## 2. Non-goals

- No new UI tabs, widgets, or backend selector in the web interface.
- No real-time watching of `opencode.db` (reuse the existing periodic scan).
- No opencode event-stream / SSE integration.
- No multi-user or remote/cloud data support.

## 3. Guiding principle: adapter pattern

Keep `scanner.py` untouched as the Claude Code adapter. Add a second adapter, `opencode_source.py`, that reads from `~/.local/share/opencode/opencode.db` and writes the same internal rows (`messages`, `tool_calls`) that the UI and query layer already expect. Everything above the adapter — `db.py`, `server.py`, `web/` — stays unchanged.

```
cli.py
  ├── --backend auto|claude|opencode  (default: auto)
  ├── [claude]  scanner.py  →  reads JSONL from ~/.claude/projects/
  │                                ↓ populates messages + tool_calls
  └── [opencode] opencode_source.py  →  reads ~/.local/share/opencode/opencode.db
                                       ↓ populates messages + tool_calls
  db.py (queries) → server.py (/api/*) → web/ (UI)  [UNCHANGED]
```

## 4. opencode.db source schema

Verified from a real opencode installation.

### 4.1 `session` table

| Column | Type | Example / notes |
|--------|------|-----------------|
| `id` | TEXT | `ses_094a2a201ffe6HRd4E79xMvuCz` |
| `project_id` | TEXT | hash identifying the project |
| `parent_id` | TEXT | parent session for subagents |
| `slug` | TEXT | session slug |
| `directory` | TEXT | `/home/user/projects/myapp` |
| `title` | TEXT | session title |
| `version` | TEXT | opencode version |
| `agent` | TEXT | `build`, `coder`, `reviewer`, `security`, `devil`, etc. |
| `model` | TEXT | JSON string: `{"id":"glm-5.2","providerID":"ollama-cloud"}` |
| `cost` | REAL | always `0.0` — cost not calculated by opencode |
| `tokens_input` | INTEGER | aggregated session total |
| `tokens_output` | INTEGER | aggregated session total |
| `tokens_reasoning` | INTEGER | aggregated session total |
| `tokens_cache_read` | INTEGER | aggregated session total |
| `tokens_cache_write` | INTEGER | aggregated session total |
| `time_created` | INTEGER | epoch ms |
| `time_updated` | INTEGER | epoch ms |
| `workspace_id` | TEXT | workspace identifier |
| `path` | TEXT | session path |

### 4.2 `message` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT | `msg_f6b5e0965001gGSBatghIZcnxR` |
| `session_id` | TEXT | FK to `session.id` |
| `time_created` | INTEGER | epoch ms |
| `time_updated` | INTEGER | epoch ms |
| `data` | TEXT | JSON blob; see below |

`message.data` JSON shape:

```json
{
  "parentID": "msg_xxx",
  "role": "user" | "assistant",
  "mode": "build",
  "agent": "build",
  "path": {"cwd": "/home/user/project", "root": "/home/user/project"},
  "cost": 0,
  "tokens": {
    "input": 55600,
    "output": 201,
    "reasoning": 0,
    "total": 55801,
    "cache": {"read": 0, "write": 0}
  },
  "modelID": "glm-5.2",
  "providerID": "ollama-cloud",
  "time": {"created": 1784212760466}
}
```

Important: many assistant messages in `opencode.db` have all-zero token fields because they are streaming snapshots. The non-zero row for a given assistant response is the final snapshot. Session-level totals are reliable; message-level tokens should be imported as-is and deduplicated by taking the latest `time_updated`.

### 4.3 `part` table

| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT | `prt_f6b5e6536001hdKg7hhEvHP3DF` |
| `message_id` | TEXT | FK to `message.id` |
| `session_id` | TEXT | FK to `session.id` |
| `time_created` | INTEGER | epoch ms |
| `time_updated` | INTEGER | epoch ms |
| `data` | TEXT | JSON blob; see below |

Verified `part.data` types:

| `type` | Purpose |
|--------|---------|
| `text` | User prompt text: `{"type":"text", "text":"...", "time":{"start":...}}` |
| `tool` | Tool invocation: `{"type":"tool", "tool":"bash", "callID":"call_xxx", "state":{"status":"running\|completed", "input":{...}, "output":"...", "time":{"start":...}}}` |
| `reasoning` | Model reasoning block |
| `step-start` | Step boundary start |
| `step-finish` | Step boundary end |
| `patch` | File diffs/edits |
| `file` | File reference: `{"type":"file", "mime":"text/plain", "filename":"...", "url":"file://...", "source":{...}}` |
| `compaction` | Compaction record |

### 4.4 Other tables

| Table | Relevance |
|-------|-----------|
| `project` | `id`, `name`, `worktree`, `vcs`, `time_created` — may be used to improve project name mapping in the future; not required for v1. |
| `session_message` | `id`, `session_id`, `type` (`model-switched`, `agent-switched`), `seq` — not required for v1. |

### 4.5 Verified scale from a real installation

| Entity | Count |
|--------|-------|
| sessions | 1841 |
| messages | 55964 (52037 assistant, 4014 user) |
| parts | 234900 |
| projects | 13 |
| events | 780326 |

### 4.6 Verified distinct values

**Models:** `glm-5.2`, `deepseek-v4-pro`, `deepseek-v4-flash`, `kimi-k2.7-code`, `mistral-large-3:675b`, `qwen3.5:397b`, `qwen3-coder:480b`, `gemma4:31b`, `gpt-oss:120b`, `nemotron-3-ultra`, `sabia-4`, `gemini-3-flash-preview`, `gemini-3.1-pro-preview`, `gemini-3.5-flash`, `devstral-2:123b`, `fugu`, `fugu-ultra`, `auto`.

**Agents:** `build`, `coder`, `coder-fb`, `coder-fb2`, `reviewer`, `reviewer-fb`, `security`, `security-fb`, `devil`, `devil-fb`, `devil-fb2`, `designer`, `designer-fb`, `docwriter`, `explore`, `explorer`, `general`, `juridico`, `vision`, `auditor`, `auditor-fb`, `plan`, `compaction`.

**Tools:** `bash`, `edit`, `glob`, `grep`, `question`, `read`, `skill`, `task`, `todowrite`, `webfetch`, `write`, `invalid`.

## 5. Data mapping

### 5.1 `messages` table

| Internal column | opencode source | Notes |
|-----------------|-----------------|-------|
| `uuid` | `message.id` | direct |
| `parent_uuid` | `message.data.parentID` | JSON |
| `session_id` | `session.id` | direct |
| `project_slug` | derived from `session.directory` | hash or path slug; must be stable |
| `cwd` | `session.directory` | direct |
| `git_branch` | `NULL` | opencode does not track this |
| `cc_version` | `session.version` | opencode version |
| `entrypoint` | `NULL` | not applicable |
| `type` | `message.data.role` | `user` or `assistant` |
| `is_sidechain` | `1` if `session.parent_id IS NOT NULL` | subagent sessions |
| `agent_id` | `session.agent` | direct |
| `timestamp` | `message.time_created` | epoch ms |
| `model` | `message.data.modelID` | JSON |
| `stop_reason` | `NULL` | not stored by opencode |
| `prompt_id` | `NULL` | not used by opencode |
| `message_id` | `message.id` | same as `uuid` |
| `input_tokens` | `message.data.tokens.input` | JSON; may be 0 for streaming snapshots |
| `output_tokens` | `message.data.tokens.output` | JSON |
| `cache_read_tokens` | `message.data.tokens.cache.read` | JSON |
| `cache_create_5m_tokens` | `message.data.tokens.cache.write` | JSON; opencode does not distinguish 5m/1h |
| `cache_create_1h_tokens` | `0` | opencode does not distinguish 5m/1h |
| `prompt_text` | `part.text` where `part.type='text'` AND `message.data.role='user'` | JOIN `part` on `message_id` |
| `prompt_chars` | `LENGTH(prompt_text)` | computed |
| `tool_calls_json` | derived from `part` where `type='tool'` | compact JSON |
| `source` (new) | `'opencode'` | migration adds column; default `'claude'` |

### 5.2 `tool_calls` table

| Internal column | opencode source | Notes |
|-----------------|-----------------|-------|
| `message_uuid` | `part.message_id` | FK |
| `session_id` | `part.session_id` | direct |
| `project_slug` | derived from `session.directory` | same derivation as messages |
| `tool_name` | `part.data.tool` | JSON |
| `target` | extracted from `part.data.state.input` | depends on tool; see §5.3 |
| `result_tokens` | `LENGTH(part.data.state.output) // 4` | rough estimate |
| `is_error` | `1` if `state.status` is not `completed` OR `state.error` key exists in `part.data.state` | deterministic; opencode stores `state.error` on failed tool calls |
| `timestamp` | `part.time_created` | epoch ms |
| `source` (new) | `'opencode'` | migration adds column; default `'claude'` |

### 5.3 Tool target extraction

| Tool | `target` value | Source field |
|------|----------------|--------------|
| `bash` | command string | `state.input.command` or joined argv |
| `read`, `edit`, `write`, `glob` | file path / glob | `state.input.file_path` or `path` |
| `grep` | search pattern | `state.input.pattern` |
| `task` | subagent type | `state.input.agent` or `type` |
| `skill` | skill name | `state.input.skill` or `name` |
| `todowrite` | `NULL` | no meaningful target |
| `webfetch` | URL | `state.input.url` |
| `question` | question header / first line | `state.input.header` or truncated text |
| `invalid` | `NULL` | fallback |

Target extraction should be defensive: if the expected field is missing, fall back to `NULL` rather than crash.

## 6. Backend selection and auto-detection

### 6.1 CLI flag

Add `--backend {auto|claude|opencode}` to both `dashboard` and `scan` subcommands. Default is `auto`.

### 6.2 Env vars

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | Path to the opencode SQLite database |
| `DASHBOARD_BACKEND` | `auto` | Same values as `--backend`; CLI flag wins if both are set |

### 6.3 Detection logic

```
auto-detect:
  1. Check if ~/.claude/projects/ exists and has *.jsonl files → claude=True
  2. Check if ~/.local/share/opencode/opencode.db exists → opencode=True
  3. If both True → run both backends (data merged in the same internal DB)
  4. If only one True → run that backend
  5. If neither → error: "No data sources found"
```

When both sources are present the two adapters run independently and write into the same internal tables. Because `project_slug` generation is backend-specific, collisions are unlikely; if a user points both tools at the same project directory the slug function must still produce the same value from the same path.

## 7. Incremental sync strategy for opencode

Reuse the existing `files` table to track progress. Repurpose columns as follows:

| `files` column | Value for opencode sync |
|----------------|-------------------------|
| `path` | absolute path to `opencode.db` |
| `mtime` | file mtime of `opencode.db` |
| `bytes_read` | `last_synced_timestamp` — max `message.time_created` imported so far (epoch ms) |
| `scanned_at` | last sync timestamp |

Sync steps:

1. Read `last_synced_timestamp` from `files` where `path = opencode_db_path`.
2. Query `opencode.db` for messages and parts with `time_created > last_synced_timestamp`.
3. Transform and import new sessions, messages, and parts.
4. Update `last_synced_timestamp` to `MAX(message.time_created)` of imported rows.
5. For rows that already exist but have a newer `time_updated`, upsert: keep the row with the highest `time_updated` per `message.id`.

Unlike the Claude scanner, opencode does not need `_evict_prior_snapshots`. SQLite guarantees transactional updates, and the import should use `INSERT ... ON CONFLICT(message_id) DO UPDATE` keyed on `message.id`, only replacing when the incoming `time_updated` is greater.

## 8. File-by-file changes

### 8.1 NEW: `token_dashboard/opencode_source.py` (~200–250 lines)

Responsibilities:

- `import_opencode(opencode_db_path, internal_conn)` — reads from the opencode SQLite DB, transforms records, and populates the internal `messages` and `tool_calls` tables.
- Incremental sync via the `files` table.
- Upsert logic so the latest `time_updated` wins per message.
- Extract `prompt_text` from `part` rows of type `text` attached to user messages.
- Extract tool invocations from `part` rows of type `tool`.
- Stable `project_slug` derivation from `session.directory`.
- Tool target extraction as described in §5.3.

Public interface (tentative):

```python
def import_opencode(opencode_db_path: str | Path, internal_conn: sqlite3.Connection) -> None:
    """Import or incrementally sync opencode data into the internal dashboard DB."""
```

### 8.2 `token_dashboard/db.py` (minor)

- Add `source TEXT DEFAULT 'claude'` to the `messages` table.
- Add `source TEXT DEFAULT 'claude'` to the `tool_calls` table.
- `init_db()` must run migrations for existing dashboards: add the columns if missing.
- All query functions remain unchanged; the new column is informational only.

### 8.3 `token_dashboard/skills.py` (minor)

Extend `_DEFAULT_ROOTS` with:

- `Path.home() / ".config" / "opencode" / "skill"`
- `Path.home() / ".agents" / "skills"`

`_slugs_for()` already walks arbitrary paths, so no other logic changes.

### 8.4 `token_dashboard/server.py` (minor)

- `_scan_loop`: call `import_opencode()` when the opencode backend is active, in addition to or instead of `scan_dir()`.
- `/api/scan` route: dispatch to the correct backend(s) based on detection or the `--backend` flag.
- SSE events: unchanged.

### 8.5 `cli.py` (minor)

- Add `--backend auto|claude|opencode` to `dashboard` and `scan`.
- Add env vars: `OPENCODE_DB`, `DASHBOARD_BACKEND`.
- `scan` and `dashboard` subcommands dispatch to `scan_dir()`, `import_opencode()`, or both based on the resolved backend.

### 8.6 `pricing.json` (extend)

Add opencode model entries. All should be marked `"estimated": true` initially because actual cost depends on the serving provider (Ollama Cloud, DeepSeek API, etc.).

| Model | Notes |
|-------|-------|
| `glm-5.2` | ZhipuAI pricing estimate |
| `deepseek-v4-pro` | DeepSeek pricing |
| `deepseek-v4-flash` | DeepSeek pricing, cheaper tier |
| `kimi-k2.7-code` | Moonshot pricing |
| `mistral-large-3:675b` | Mistral pricing |
| `qwen3.5:397b` | Alibaba pricing |
| `qwen3-coder:480b` | Alibaba pricing |
| `gemma4:31b` | Google pricing |
| `gpt-oss:120b` | OpenAI pricing |
| `nemotron-3-ultra` | NVIDIA pricing |
| `sabia-4` | Maritaca pricing |
| `gemini-3-flash-preview`, `gemini-3.1-pro-preview`, `gemini-3.5-flash` | Google pricing |
| `devstral-2:123b` | Mistral pricing |
| `auto` | `null` — cannot price |
| `fugu`, `fugu-ultra` | `null` — unknown models |

Users can edit `pricing.json` to fill in real values as providers publish them.

### 8.7 `web/` (no changes)

The UI remains backend-agnostic. All tabs work unchanged because `/api/*` JSON shapes do not change.

### 8.8 `README.md` (update)

Add:

- A short "opencode support" section.
- `--backend` flag documentation.
- `OPENCODE_DB` env var documentation.
- A note that opencode is an alternative data source, not a replacement.

Keep all existing Claude Code documentation intact.

## 9. Test plan

### 9.1 New test files

#### `tests/test_opencode_source.py`

- Data transformation: opencode.db rows → internal schema columns.
- Incremental sync: only messages newer than `last_synced_timestamp` are imported.
- Dedup: when a message is re-imported with a newer `time_updated`, the newer row wins.
- `prompt_text` extraction from `part` rows of type `text` on user messages.
- `tool_calls_json` extraction from `part` rows of type `tool`.
- Target extraction per tool type.

#### `tests/test_skills_opencode.py`

- opencode skill directories are scanned.
- Both Claude and opencode skills appear in the catalog.

### 9.2 Regression

Run the full suite:

```bash
python3 -m unittest discover tests
```

All existing tests must continue to pass.

## 10. Acceptance criteria

1. `python3 -m unittest discover tests` passes, including new tests.
2. `python3 cli.py scan --backend opencode` populates the dashboard DB from `opencode.db`.
3. `python3 cli.py dashboard` auto-detects opencode and serves the UI with opencode data.
4. `python3 cli.py dashboard --backend claude` behaves exactly as before for Claude Code users.
5. `python3 cli.py dashboard --backend auto` detects and imports both sources when both are present.
6. The Skills tab shows skills from `~/.claude/`, `~/.config/opencode/skill/`, and `~/.agents/skills/`.
7. All seven UI tabs work with opencode data: Overview, Prompts, Sessions, Projects, Skills, Tips, Settings.
8. No breaking changes for existing Claude Code users.

## 11. Backward compatibility

The following files and behaviors stay unchanged:

- `scanner.py` — completely unchanged.
- `db.py` query functions — unchanged; the new `source` column is nullable with a default.
- `server.py` API endpoints and JSON shapes — unchanged.
- `pricing.py` — unchanged; only `pricing.json` grows.
- `tips.py` — unchanged.
- `web/` — completely unchanged.
- All existing tests — still pass.

## 12. Follow-up documentation

After implementation, write `docs/OPENCODE_ADAPTER.md` as developer-facing documentation covering:

- Adapter internals (`opencode_source.py`).
- How incremental sync works.
- How `project_slug` is derived.
- How to add new tool target extractors.
- How to extend pricing for new opencode models.
