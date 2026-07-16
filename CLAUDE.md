# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

**Token Dashboard** — a local dashboard for tracking token usage, costs, and session history from **Claude Code** and/or **opencode**. This is a fork of [nateherkai/token-dashboard](https://github.com/nateherkai/token-dashboard) that adds an opencode adapter layer while maintaining full Claude Code compatibility. Reads Claude Code JSONL transcripts from `~/.claude/projects/` and/or opencode SQLite data from `~/.local/share/opencode/opencode.db`, turning them into per-prompt cost analytics, tool/file heatmaps, subagent attribution, cache analytics, project comparisons, and a rule-based tips engine.

Inspired by [phuryn/claude-usage](https://github.com/phuryn/claude-usage) but diverges in UI (vanilla JS + ECharts, dark theme, hash router, SSE refresh) and scope (expensive-prompt drill-down, skills view, tips engine, streaming-snapshot dedup, dual-backend support). See `docs/inspiration.md` for the original's feature set and known limitations.

## Status

Working codebase. 103 Python unit tests (`python3 -m unittest discover tests`). Seven UI tabs wired up (Overview, Prompts, Sessions, Projects, Skills, Tips, Settings). Runs on macOS, Windows, and Linux. Supports both Claude Code (JSONL) and opencode (SQLite) data sources with auto-detection.

## Architecture

- `cli.py` → `token_dashboard/scanner.py` (Claude JSONL) OR `token_dashboard/opencode_source.py` (opencode SQLite) → `~/.claude/token-dashboard.db` (SQLite)
- `token_dashboard/server.py` exposes JSON APIs (`/api/*`) + SSE stream (`/api/stream`) + static frontend (`web/`)
- `web/` is vanilla JS, no build step — hash router + ECharts

## Data sources

**Claude Code** writes one JSONL file per session to `~/.claude/projects/<project-slug>/<session-id>.jsonl`. Each line is a message record; usage fields live at `message.usage` and model identifier at `message.model`. The scanner is incremental — it tracks each file's mtime and byte offset in the `files` table and only reads new bytes on subsequent scans.

**opencode** stores all session data in a SQLite database at `~/.local/share/opencode/opencode.db`. The `opencode_source.py` adapter reads opencode's `session`, `message`, and `part` tables and upserts into the same internal `messages`/`tool_calls` schema, tagged with `source='opencode'`. Sync is incremental — it tracks the last imported timestamp and only reads new rows. opencode uses SQLite transactionally, so streaming-snapshot dedup is not needed (the adapter uses `INSERT OR REPLACE` upsert by message id instead).

## Conventions

- **Fully local.** No telemetry, no remote calls for user data. Tests run offline.
- **Stdlib only.** No `pip install`. If a new feature needs a third-party library, argue for it first — we're willing to pay ergonomics cost to keep install friction at zero.
- **SQLite parameter binding always.** Any f-string in a SQL statement must interpolate only internal, caller-controlled values (column names, placeholder lists). User-reachable values go through `?`.
- **Small files with clear responsibilities.** If a file grows past ~400 lines or accretes three distinct concerns, split it.
- **Streaming-snapshot dedup.** When adding scanner logic that joins the `messages` table, remember `(session_id, message_id)` is the dedup key, not `uuid`. See `scanner._evict_prior_snapshots` and the migration note in `db._migrate_add_message_id`.

## Customizing

Env vars: `PORT` (default 8080, 8090 for systemd), `HOST` (default 127.0.0.1, set to `dual` for IPv4+IPv6 loopback), `CLAUDE_PROJECTS_DIR`, `TOKEN_DASHBOARD_DB`, `OPENCODE_DB` (opencode SQLite path), `DASHBOARD_BACKEND` (`auto`/`claude`/`opencode`). Pricing lives in `pricing.json`. See README.md § Environment variables for details.

## Known limitations

See `docs/KNOWN_LIMITATIONS.md`. Current summary: Skills `tokens_per_call` is populated only for skills installed under the scanned roots — Claude Code (`~/.claude/skills/`, `~/.claude/scheduled-tasks/`, `~/.claude/plugins/`) and opencode (`~/.config/opencode/skill/`, `~/.agents/skills/`); project-local skills and subagent-dispatched skills show invocation counts but blank token counts.

## Verifying changes

```bash
python3 -m unittest discover tests        # all tests (103)
python3 cli.py dashboard --no-open        # start the server (port 8080)
python3 cli.py scan --backend opencode    # import opencode data
curl http://127.0.0.1:8080/api/overview   # sanity-check an endpoint
```

For the systemd service (port 8090, dual-stack):

```bash
systemctl --user status token-dashboard
curl http://localhost:8090/api/overview   # works on both IPv4 and IPv6
```
