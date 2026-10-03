# AGENTS.md — Token Dashboard

Ramp-up map for OpenCode coding agents. Complements `CLAUDE.md` at the repo root
(overlapping guidance — prefer referencing it over duplicating) and
`docs/KNOWN_LIMITATIONS.md` (current and accurate). Read those for depth.

## Project Overview

Local-only dashboard tracking token usage, costs, and sessions from Claude Code
(JSONL transcripts) and opencode (SQLite). Fork of nateherkai/token-dashboard.
Python 3.8+, **stdlib only** (zero non-stdlib imports — verified). Frontend is
vanilla JS with ECharts vendored at `web/echarts.min.js` — no build step, no npm.
Fully local: no telemetry, tests run offline.

## Commands

```bash
# Full suite — 103 tests, ~63s. test_cli spawns REAL subprocesses; use
# generous timeouts (90s+) before assuming a hang.
python3 -m unittest discover tests

# Single module
python3 -m unittest tests.test_scanner_dedup

# Single case
python3 -m unittest tests.test_scanner_dedup.ClassName.test_method

# Run the dashboard server (default 127.0.0.1:8080; auto-scans, SSE re-scan every 30s)
python3 cli.py dashboard --no-open
python3 cli.py dashboard --no-scan --no-open  # skip initial scan (systemd service uses this)

# Import opencode data only
python3 cli.py scan --backend opencode

# Sanity check the running server
curl http://127.0.0.1:8080/api/overview
```

Other subcommands: `today`, `stats`, `tips`. No pytest, no external test libs —
unittest.TestCase only. There is no Makefile and no requirements.txt.

## Architecture

Flow: `cli.py` → `token_dashboard/scanner.py` (Claude JSONL, incremental by
mtime/byte-offset) OR `token_dashboard/opencode_source.py` (opencode SQLite,
incremental by timestamp) → `~/.claude/token-dashboard.db` (SQLite) →
`token_dashboard/server.py` (JSON APIs under `/api/*`, SSE at `/api/stream`,
static files from `web/`).

Module sizes — the ~400-line limit matters (see Conventions):
- `db.py` 434 lines — the ONLY file over the limit. Prefer extracting queries
  over growing it further.
- `opencode_source.py` 316 · `scanner.py` 277 · `server.py` 250 ·
  `tips.py` 186 · `skills.py` 118 · `pricing.py` 66.

## Conventions (hard rules)

- **Stdlib only.** No pip install, no requirements.txt. Argue before adding
  any third-party dependency — deliberate project constraint.
- **SQL parameter binding ALWAYS.** `?` placeholders for any user-reachable
  value; f-strings only for internal identifiers.
- **Files ≤ ~400 lines**, one clear responsibility per module.
- **Dedup keys:** Claude scanner dedups on `(session_id, message_id)` — NOT
  uuid (see `scanner._evict_prior_snapshots`). The opencode adapter uses
  `INSERT OR REPLACE` by message id.
- **Tests pass paths explicitly** via env vars/parameters instead of patching
  the environment — the one exception: `test_skills_opencode` patches
  `Path.home` to redirect skill roots. Fixtures live in `tests/fixtures/`.
- Frontend has no build step — edit `web/` files directly and reload.

## Environment Variables

All env vars are read **ONLY in `cli.py`** — nothing in `token_dashboard/`
reads the environment. Defaults in parentheses:

- `TOKEN_DASHBOARD_DB` (`~/.claude/token-dashboard.db`) — SQLite store.
- `CLAUDE_PROJECTS_DIR` (`~/.claude/projects`) — Claude JSONL location.
- `OPENCODE_DB` (`~/.local/share/opencode/opencode.db`) — opencode source.
- `DASHBOARD_BACKEND` (`auto`/`claude`/`opencode`) — the CLI `--backend`
  flag wins over this env var.
- `HOST` (`127.0.0.1`; the value `dual` = IPv4+IPv6 loopback; `0.0.0.0` is
  the documented cross-device workaround but exposes the server — see
  `docs/KNOWN_LIMITATIONS.md`).
- `PORT` (`8080`; the systemd service uses `8090`).

## Gotchas

- **No CI, no pre-commit hooks.** Local `python3 -m unittest discover tests`
  is the only verification gate — run it before declaring work done.
- The common flags `--db`, `--projects-dir`, `--backend`, `--opencode-db` are
  shared by ALL subcommands (argparse parent parser) — not dashboard-only.
- `pricing.json` at the repo root (19 models + `tier_fallback` + plans) is
  consumed by `server.py` via a path relative to the repo: edit the file and
  restart the server to change prices. There is NO API endpoint that updates it.
- Backend `auto` detection checks for `*.jsonl` in the projects dir AND for
  `opencode.db`; if neither exists the CLI exits with "no data sources found".
- The systemd unit at `docs/token-dashboard.service` has author-specific
  hardcoded paths — README instructs editing it for other machines.
- `docs/plans/2026-07-16-opencode-support.md` has 21 unchecked checkboxes, but
  the work IS implemented and merged (commit 7818a0e). The plan file is stale —
  do NOT resume or "finish" it.
- For known-broken/unsupported behavior, check `docs/KNOWN_LIMITATIONS.md`
  before inventing a fix — it is current and accurate.
