# Project Overview

## Stack and Versions
- Python 3.8+, **stdlib only** — zero non-stdlib imports (verified 2026-10-03). No pip, no requirements.txt, no Makefile.
- Frontend: vanilla JS, no build step, no npm. ECharts vendored at `web/echarts.min.js`.
- Persistence: SQLite (`~/.claude/token-dashboard.db`).
- Tests: `unittest` only (no pytest, no external test libs). 103 tests, ~63s.

## Architecture
Flow: `cli.py` (170 lines) → `token_dashboard/scanner.py` (Claude JSONL, incremental by
mtime/byte-offset) OR `token_dashboard/opencode_source.py` (opencode SQLite, incremental by
timestamp) → `~/.claude/token-dashboard.db` (SQLite) → `token_dashboard/server.py`
(JSON APIs under `/api/*`, SSE at `/api/stream`, static files from `web/`).

Module sizes (the ~400-line convention matters):
- `db.py` 434 · `opencode_source.py` 316 · `scanner.py` 277 · `server.py` 250 ·
  `tips.py` 186 · `skills.py` 118 · `pricing.py` 66.

## Design Decisions
- [2026-10-03] AGENTS.md complements CLAUDE.md instead of duplicating it (references CLAUDE.md + docs/KNOWN_LIMITATIONS.md for detail).
- [2026-07-16] opencode adapter design (`docs/2026-07-16-opencode-support-design.md`) — adapter pattern, no UI changes. Implemented and merged in commit `7818a0e`.

## Conventions
- **Stdlib only.** Argue before adding any third-party dependency — deliberate project constraint.
- **SQL parameter binding ALWAYS.** `?` placeholders for any user-reachable value; f-strings only for internal identifiers.
- **Files ≤ ~400 lines**, one clear responsibility per module. `db.py` is the sole exception at 434 — extract queries, do not grow it further.
- **Dedup keys:** Claude scanner dedups on `(session_id, message_id)` (NOT uuid — see `scanner._evict_prior_snapshots`); opencode adapter uses `INSERT OR REPLACE` by message id.
- Tests pass paths explicitly via env vars/parameters instead of patching the environment (one exception: `test_skills_opencode` patches `Path.home`). Fixtures live in `tests/fixtures/`.

## Known Technical Debt
- `docs/plans/2026-07-16-opencode-support.md` is **STALE** — 21 unchecked checkboxes, but the work IS implemented and merged (commit `7818a0e`). Do NOT resume or "finish" it.
- `db.py` exceeds the 400-line limit (434 lines).

## External Dependencies and Integrations
- **None** — stdlib only, fully local, no telemetry.
- Reads Claude Code JSONL transcripts from `~/.claude/projects/`.
- Reads opencode SQLite from `~/.local/share/opencode/opencode.db`.
