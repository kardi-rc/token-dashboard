# Project Overview

## Stack and Versions
- Python 3.8+, **stdlib only** — zero non-stdlib imports (verified 2026-10-03). No pip, no requirements.txt, no Makefile.
- Frontend: vanilla JS, no build step, no npm. ECharts vendored at `web/echarts.min.js`.
- Persistence: SQLite (`~/.claude/token-dashboard.db`).
- Tests: `unittest` only (no pytest, no external test libs). 213 tests, ~90s.

## Architecture
Flow: `cli.py` (170 lines) → `token_dashboard/scanner.py` (Claude JSONL, incremental by
mtime/byte-offset) OR `token_dashboard/opencode_source.py` (opencode SQLite, incremental by
timestamp) → `~/.claude/token-dashboard.db` (SQLite) → `token_dashboard/server.py`
(JSON APIs under `/api/*`, SSE at `/api/stream`, static files from `web/`).

Module sizes (the ~400-line convention matters):
- `db.py` 425 · `opencode_source.py` 399 · `pricing.py` 295 · `server.py` ~404 (cost-series
  handler pushed it to the edge of ~400 — extract cost-series helpers before growing it further) ·
  `scanner.py` 277 · `opencode_v2_source.py` 244 · `tips.py` 186 · `queries.py` 137 · `skills.py` 118.

## Design Decisions
- [2026-10-03] OpenCode v2 storage migration IN PROGRESS (spec `docs/specs/2026-10-03-opencode-v2-storage-and-pricing.md`, plan `docs/plans/2026-10-03-opencode-v2-storage-and-pricing.md`, 8 tasks). Tasks 1-4 done and reviewer-passed: `cost_usd` column + migration; `queries.py` cost-aware extraction (`db.py` 425 lines); `opencode_v2_source.py` v2 leg (`session_v2`/`session_message`, inline content, `part_id` namespaced `msg:call`, read-only `mode=ro`); dual-leg `import_opencode` orchestration (single watermark, single commit, auto-detect). Suite at 140 tests green. Pending: Tasks 5-8 (models.dev pricing refresh, CLI wiring, server cost display, docs). User decisions: `cost_usd` preferred only when > 0 (subscription 0.0 falls back to estimate); pricing refresh manual + 7-day TTL auto + first-run. Known deferred note: import summary `sessions` may double-count ids present in both `session` and `session_v2` (diagnostic only, no consumer breaks — docstring/KNOWN_LIMITATIONS clarification queued for Task 8). Also: project now has `verify.sh` at root (created from `~/.config/opencode/docs/verify-template.sh`; CHECKS: syntax python compileall default + full unittest `!slow` via `--full`; smoke test PASSALL 2026-10-03; verdict+exit-code cross-check contract).
  - [2026-10-04] **COMPLETED** — all 8 plan tasks + security hardening + devil-fix round passed (reviewer/security/devil gates all PASS). 198 tests green. Post-plan fixes: per-row import resilience (shape-invalid rows skipped, never stall ingestion), 10MB fetch cap, stored-XSS fix (settings.js escape + `_safe_model_id` ingest validation + five-key cache projection), negative-cost/rate rejection, POST /api/plan validation (400), watermark clamped to wall-clock (clock-skew data loss fixed), scan-loop stderr visibility, CLI status overflow guard.
  - [2026-10-04] GO-LIVE: committed b487e67 (migration) + 875d1c9 (User-Agent fix — models.dev 403-blocks the default Python-urllib UA; custom UA 'token-dashboard/0.1.0' gets 200), pushed to origin/main, live AC-D4 check PASSED (scan --backend opencode: 10,886 messages + 13,319 tool calls imported in 5s, post-freeze v2 data, no double-count), systemd token-dashboard restarted on port 8090 — /api/by-model serving v2 models with real models.dev rates (cache: 3,649 models, 459 KB at ~/.claude/pricing-cache.json; effective pricing 3,652 models at /api/plan). 199 tests green.
- [2026-10-03] AGENTS.md complements CLAUDE.md instead of duplicating it (references CLAUDE.md + docs/KNOWN_LIMITATIONS.md for detail).
- [2026-07-16] opencode adapter design (`docs/2026-07-16-opencode-support-design.md`) — adapter pattern, no UI changes. Implemented and merged in commit `7818a0e`.
- [2026-10-04] **Costs tab COMPLETE (all gates PASS).** New `#/costs` dashboard tab (`web/routes/costs.js` ~230 lines new; 1 ROUTES line in `web/app.js`) showing token/USD spend per model AND per provider: period selector (7d/30d default/90d/all/custom), per-provider KPI cards (cost, % share, monthly extrapolation avg/day×30), cost-share donut, daily stacked-bar trend, sortable per-model table (turns, tokens, cost, %, `est.` badge, null→`—` footer note). Backend: `queries.cost_series` (daily `(date,model)` merge-input rows, `date(timestamp,'localtime')` bucketing — intentionally differs from `/api/daily`'s UTC slice; `COALESCE(model,'unknown')`) + `GET /api/cost-series` handler in `server.py` with NEW `fromisoformat` since/until validation (400 invalid/`since>=until`, both `YYYY-MM-DD` and ISO; `_normalize_iso_param` canonicalizes to UTC before SQL lexicographic comparison). Handler does its OWN null-preserving stored-first merge (stored>0 wins whole bucket — preference NOT sum; unpriceable → `cost_usd` null). Tab consumes ONLY `/api/cost-series` (client-side aggregation); `/api/by-model` untouched. Tests: `CostSeriesTests` + `CostSeriesHandlerTests` (13 new, suite **213** green). `verify.sh` PASSALL.
  - Decisions: stored-preference merge (spec R2 — tab totals intentionally differ from `/api/by-model` additive sums for mixed stored+estimated groups); day bucketing localtime; until exclusive on the wire (custom range sends `end+1d`); frontend `deriveProvider` (literal `'unknown'` sentinel first, split `/`, prefix map incl. `o1`, aliases `deepseek-ai→deepseek`/`moonshotai→moonshot`); provider logic **frontend-only**. Artifacts: spec `docs/specs/2026-10-04-costs-tab.md` + plan `docs/plans/2026-10-04-costs-tab.md` (Tasks 1–5; only Task 5 Step 4 manual browser check left unchecked for the user).
  - Gate history: artifact-gate BLOCK → spec/plan revised (R1–R7); security BLOCK (chart-label XSS via unescaped provider/model names in the ECharts tooltip `innerHTML` sink; date normalization) → fixed (`esc()` at chart sinks in `costs.js`; `_normalize_iso_param`; ALSO fixed the same pre-existing sink in `overview.js` donut name); devil BLOCK (donut tooltip said `'tokens'` for USD; o1 bucket; est-flag pollution; TypeError; provider fragmentation) → fixed via backward-compatible `donutChart(el, data, opts)` optional `{unit:'usd'}` param in `web/charts.js` — ONE authorized deviation from the spec's `charts.js untouched` (default 2-arg path byte-identical, `overview.js` unaffected).
  - Ollama Cloud weekly quota was exhausted during this session — reviewer/security gates ran via agy (`gemini-3.6-flash-medium`) and security-fb (DeepSeek, Sunday off-peak).

## Conventions
- **Stdlib only.** Argue before adding any third-party dependency — deliberate project constraint.
- **SQL parameter binding ALWAYS.** `?` placeholders for any user-reachable value; f-strings only for internal identifiers.
- **Files ≤ ~400 lines**, one clear responsibility per module. `db.py` is the sole exception at 434 — extract queries, do not grow it further.
- **Dedup keys:** Claude scanner dedups on `(session_id, message_id)` (NOT uuid — see `scanner._evict_prior_snapshots`); opencode adapter uses `INSERT OR REPLACE` by message id.
- Tests pass paths explicitly via env vars/parameters instead of patching the environment (one exception: `test_skills_opencode` patches `Path.home`). Fixtures live in `tests/fixtures/`.

## Known Technical Debt
- `docs/plans/2026-07-16-opencode-support.md` is **STALE** — 21 unchecked checkboxes, but the work IS implemented and merged (commit `7818a0e`). Do NOT resume or "finish" it.
- `db.py` exceeds the 400-line limit (434 lines).
- [2026-10-04] Costs tab residuals: manual browser verification of `#/costs` pending (user, plan Task 5 Step 4); extrapolation divisors for 7d/30d presets are calendar-fixed (sparse-data under-extrapolation — spec-pinned trade-off); `charts.js` `donutChart` other callers unaffected by the `{unit:'usd'}` param, BUT the ECharts tooltip `innerHTML` sink class remains in `charts.js` itself — escaping is per-caller (a `charts.js`-level escape would be defense-in-depth); `server.py` ~404 lines (edge of the ~400 convention).
- [2026-10-04] Follow-ups (devil residuals, non-blocking): R1 `do_POST` exception net misses `RecursionError`/`UnicodeDecodeError` (widen the except tuple); R2 legacy negative `cost_usd` rows (one-line migration `UPDATE messages SET cost_usd=NULL WHERE cost_usd<0` — moot until a pre-fix import ran); R3 `settings.js` save handler ignores 400; Host/Origin/CSRF hardening + CSP headers for the localhost server (inherited posture); SSE single-queue fan-out (two tabs compete); `/tmp` tmpfs degraded with ~850 leaked test scratch dirs (needs user-approved cleanup); AGENTS.md test-count/module numbers go stale as the suite grows.

## External Dependencies and Integrations
- **None** — stdlib only, fully local, no telemetry.
- Reads Claude Code JSONL transcripts from `~/.claude/projects/`.
- Reads opencode SQLite from `~/.local/share/opencode/opencode.db`.
