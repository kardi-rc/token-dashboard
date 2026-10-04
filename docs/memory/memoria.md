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
- [2026-10-03] OpenCode v2 storage migration IN PROGRESS (spec `docs/specs/2026-10-03-opencode-v2-storage-and-pricing.md`, plan `docs/plans/2026-10-03-opencode-v2-storage-and-pricing.md`, 8 tasks). Tasks 1-4 done and reviewer-passed: `cost_usd` column + migration; `queries.py` cost-aware extraction (`db.py` 425 lines); `opencode_v2_source.py` v2 leg (`session_v2`/`session_message`, inline content, `part_id` namespaced `msg:call`, read-only `mode=ro`); dual-leg `import_opencode` orchestration (single watermark, single commit, auto-detect). Suite at 140 tests green. Pending: Tasks 5-8 (models.dev pricing refresh, CLI wiring, server cost display, docs). User decisions: `cost_usd` preferred only when > 0 (subscription 0.0 falls back to estimate); pricing refresh manual + 7-day TTL auto + first-run. Known deferred note: import summary `sessions` may double-count ids present in both `session` and `session_v2` (diagnostic only, no consumer breaks — docstring/KNOWN_LIMITATIONS clarification queued for Task 8). Also: project now has `verify.sh` at root (created from `~/.config/opencode/docs/verify-template.sh`; CHECKS: syntax python compileall default + full unittest `!slow` via `--full`; smoke test PASSALL 2026-10-03; verdict+exit-code cross-check contract).
  - [2026-10-04] **COMPLETED** — all 8 plan tasks + security hardening + devil-fix round passed (reviewer/security/devil gates all PASS). 198 tests green. Post-plan fixes: per-row import resilience (shape-invalid rows skipped, never stall ingestion), 10MB fetch cap, stored-XSS fix (settings.js escape + `_safe_model_id` ingest validation + five-key cache projection), negative-cost/rate rejection, POST /api/plan validation (400), watermark clamped to wall-clock (clock-skew data loss fixed), scan-loop stderr visibility, CLI status overflow guard.
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
- [2026-10-04] Follow-ups (devil residuals, non-blocking): R1 `do_POST` exception net misses `RecursionError`/`UnicodeDecodeError` (widen the except tuple); R2 legacy negative `cost_usd` rows (one-line migration `UPDATE messages SET cost_usd=NULL WHERE cost_usd<0` — moot until a pre-fix import ran); R3 `settings.js` save handler ignores 400; Host/Origin/CSRF hardening + CSP headers for the localhost server (inherited posture); SSE single-queue fan-out (two tabs compete); `/tmp` tmpfs degraded with ~850 leaked test scratch dirs (needs user-approved cleanup); AGENTS.md test-count/module numbers go stale as the suite grows.

## External Dependencies and Integrations
- **None** — stdlib only, fully local, no telemetry.
- Reads Claude Code JSONL transcripts from `~/.claude/projects/`.
- Reads opencode SQLite from `~/.local/share/opencode/opencode.db`.
