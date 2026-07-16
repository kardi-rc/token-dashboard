# Token Dashboard

A local dashboard that reads session transcripts from **Claude Code** and/or **opencode** and turns them into per-prompt cost analytics, tool/file heatmaps, subagent attribution, cache analytics, project comparisons, and a rule-based tips engine.

> **Fork notice.** This is a fork of [nateherkai/token-dashboard](https://github.com/nateherkai/token-dashboard), originally built for Claude Code. This fork adds a **opencode** adapter layer that reads from opencode's SQLite database (`~/.local/share/opencode/opencode.db`) while maintaining 100% backward compatibility with Claude Code. Both data sources can be used simultaneously — data is merged into a single dashboard.

**Everything runs locally.** No data leaves your machine — no telemetry, no API calls for your data, no login.

![Overview tab — totals and daily charts](docs/images/dashboard-overview-top.jpg)

![Overview tab — per-project, per-model, top tools, recent sessions](docs/images/dashboard-overview-bottom.jpg)

## What this is useful for

- Seeing which of your prompts are expensive (surprise: they usually involve large tool results).
- Comparing token usage across projects you've worked on.
- Spotting wasteful patterns — the same file read twenty times in a session, a tool call returning 80k tokens.
- Understanding what a "cache hit" actually saves you.
- If you're on Pro or Max, confirming you're getting your money's worth in API-equivalent dollars.

## Prerequisites

- **Python 3.8 or newer** — already installed on macOS and most Linux. On Windows: `winget install Python.Python.3.12` or download from python.org.
- **Claude Code** and/or **opencode** — at least one must be installed with at least one session run. The dashboard auto-detects which is available.
- **A web browser.** Any modern one.

No `pip install`. No Node.js. No build step.

## Quickstart

```bash
git clone https://github.com/nateherkai/token-dashboard.git
cd token-dashboard
python3 cli.py dashboard
```

> On Windows, if `python3` isn't on your PATH, substitute `py -3` for `python3` in every command below.

The command:
1. Auto-detects available data sources (Claude Code JSONL and/or opencode SQLite).
2. Scans them (first run can take 20–60 seconds on a heavy user's machine).
3. Starts a local server at http://127.0.0.1:8080.
4. Opens your default browser to that URL.

Leave it running; it re-scans every 30 seconds and pushes updates live. Stop with `Ctrl+C`.

To use only opencode as data source:

```bash
python3 cli.py dashboard --backend opencode
```

## opencode support

This dashboard also reads session data from [opencode](https://opencode.ai), which stores sessions in a SQLite database at `~/.local/share/opencode/opencode.db`.

### Auto-detection

By default the dashboard auto-detects which data sources are available:

- **Claude Code** — JSONL files in `~/.claude/projects/`
- **opencode** — SQLite database at `~/.local/share/opencode/opencode.db`
- **Both** — if both sources are present, data from both is merged into the same dashboard

### Forcing a backend

```bash
python3 cli.py dashboard --backend opencode      # only opencode
python3 cli.py dashboard --backend claude        # only Claude Code
python3 cli.py dashboard --backend auto          # auto-detect (default)
```

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | Path to opencode SQLite database |
| `DASHBOARD_BACKEND` | `auto` | Same as `--backend`; CLI flag wins if both set |

## Running as a systemd service

To keep the dashboard always available as a background service on Linux, use the included systemd user service. It runs on **port 8090** with **dual-stack IPv4+IPv6 loopback** to avoid conflicts with ad-hoc runs on 8080.

### Setup

1. Copy the service file to your systemd user directory:

```bash
mkdir -p ~/.config/systemd/user
cp docs/token-dashboard.service ~/.config/systemd/user/
```

2. Edit the service file to match your paths. Key settings to customize:

```ini
WorkingDirectory=/path/to/token-dashboard          # where you cloned the repo
Environment=PORT=8090                                # port (8090 avoids conflicts)
Environment=HOST=dual                                # dual-stack IPv4+IPv6 loopback
Environment=DASHBOARD_BACKEND=opencode              # auto | claude | opencode
Environment=OPENCODE_DB=/home/you/.local/share/opencode/opencode.db
Environment=TOKEN_DASHBOARD_DB=/home/you/.claude/token-dashboard.db
ExecStart=/usr/bin/python3 /path/to/token-dashboard/cli.py dashboard --no-scan --no-open
```

3. Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable token-dashboard    # start on login
systemctl --user start token-dashboard     # start now
```

### Manage

```bash
systemctl --user status token-dashboard    # check status
systemctl --user restart token-dashboard   # restart (picks up code changes)
systemctl --user stop token-dashboard      # stop
journalctl --user -u token-dashboard -f    # follow logs
```

### How it works

- `--no-scan`: the service doesn't block startup with a full scan; the background scan loop populates data every 30 seconds.
- `--no-open`: no browser is opened (systemd has no display).
- `HOST=dual`: binds to both `127.0.0.1` (IPv4) and `::1` (IPv6), so `http://localhost:8090/` works regardless of which IP family the browser resolves first.
- `Restart=always`: the service restarts automatically if the process dies.
- `WantedBy=default.target`: the service starts when you log in.

### Troubleshooting

```bash
# Service won't start: check the logs
journalctl --user -u token-dashboard --no-pager -n 50

# Port 8090 already in use: change PORT in the service file
# IPv6 not available: the service falls back to IPv4 only (check logs for "IPv6 binding skipped")

# Service file not found after editing
systemctl --user daemon-reload
```

## Where the data comes from

### Claude Code

Claude Code writes one JSONL file per session here:

| OS | Path |
|---|---|
| macOS / Linux | `~/.claude/projects/<project-slug>/<session-id>.jsonl` |
| Windows | `C:\Users\<you>\.claude\projects\<project-slug>\<session-id>.jsonl` |

### opencode

opencode stores all session data in a single SQLite database:

| OS | Path |
|---|---|
| Linux | `~/.local/share/opencode/opencode.db` |
| macOS | `~/Library/Application Support/opencode/opencode.db` |
| Windows | `%LOCALAPPDATA%\opencode\opencode.db` |

The dashboard reads this database via an adapter (`token_dashboard/opencode_source.py`) that transforms opencode's `session`, `message`, and `part` tables into the same internal schema used by the Claude Code scanner. Both data sources populate the same `messages` and `tool_calls` tables, tagged with a `source` column (`'claude'` or `'opencode'`).

The dashboard never modifies the source files — it only reads them and keeps a local SQLite cache at `~/.claude/token-dashboard.db`.

To point at a different location:

```bash
python3 cli.py dashboard --projects-dir /path/to/projects --db /path/to/cache.db
```

### Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `8080` (CLI) / `8090` (systemd) | Port the local web server listens on |
| `HOST` | `127.0.0.1` | Bind address. Set to `dual` for IPv4+IPv6 loopback. Keep the default. Setting `0.0.0.0` exposes your entire prompt history to anyone on your local network — don't do this on any network you don't fully control. |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Where to scan for Claude Code session JSONL files |
| `TOKEN_DASHBOARD_DB` | `~/.claude/token-dashboard.db` | SQLite cache location |
| `OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | Path to opencode SQLite database |
| `DASHBOARD_BACKEND` | `auto` | `auto` (detect both), `claude` (JSONL only), or `opencode` (SQLite only). `--backend` CLI flag wins if both set. |

Pricing lives in [`pricing.json`](pricing.json). Edit it directly if model prices change or to add a new plan.

## CLI reference

```bash
python3 cli.py scan          # populate / refresh the local DB, then exit
python3 cli.py today         # today's totals (terminal)
python3 cli.py stats         # all-time totals (terminal)
python3 cli.py tips          # active suggestions (terminal)
python3 cli.py dashboard     # scan + serve the UI at http://localhost:8080

# backend selection
python3 cli.py dashboard --backend opencode      # only opencode
python3 cli.py dashboard --backend claude        # only Claude Code
python3 cli.py dashboard --backend auto          # auto-detect both (default)

# dashboard flags
python3 cli.py dashboard --no-open   # don't auto-open the browser
python3 cli.py dashboard --no-scan   # skip the initial scan (use cached DB only)
python3 cli.py dashboard --opencode-db /path/to/opencode.db  # custom opencode DB path
```

Change the port: `PORT=9000 python3 cli.py dashboard`.

Dual-stack loopback (IPv4 + IPv6): `HOST=dual PORT=8090 python3 cli.py dashboard`.

## The 7 tabs

The dashboard is a single page with a hash-router tab bar across the top. Each tab is backed by its own JSON API under `/api/`:

- **Overview** — all-time input/output/cache tokens, sessions, turns, estimated cost on your chosen plan, daily work and cache-read charts, tokens-by-project, token share by model, top tools by call count, and recent sessions. This is the landing tab.
- **Prompts** — your most expensive user prompts ranked by tokens. Click any row to see the assistant response, tool calls made, and the size of each tool result.
- **Sessions** — turn-by-turn view of any single session, with per-turn tokens and tool calls.
- **Projects** — per-project comparison: tokens, session counts, and which files were touched most.
- **Skills** — which skills you invoke most often, and (where we can measure them) their token cost. See [limitations](docs/KNOWN_LIMITATIONS.md#skills-token-counts-are-partial).
- **Tips** — rule-based suggestions for reducing token usage (repeated file reads, oversized tool results, low cache-hit rate, etc.).
- **Settings** — switch pricing between API / Pro / Max / Max-20x so cost figures everywhere else reflect your actual plan.

The Overview tab also has a built-in "What do these numbers mean?" panel that explains input/output/cache tokens in plain English.

## Troubleshooting

**"No data" or empty charts.** Run `python3 cli.py scan` once to populate the DB, then reload.

**Port 8080 already in use.** `PORT=9000 python3 cli.py dashboard`.

**Numbers look wrong / stuck.** The DB lives at `~/.claude/token-dashboard.db`. Delete it and re-run `python3 cli.py scan` to rebuild from scratch.

**Running the dashboard twice at the same time.** Don't — both processes will fight over the SQLite DB. Stop all instances before starting a new one.

## Accuracy note

Claude Code writes each assistant response 2–3 times to disk while it streams (the same API message gets snapshotted as output grows). The dashboard dedupes these by `message.id` so the final tally matches what the API actually billed. If you compare against another tool that sums every JSONL row, expect this dashboard's numbers to be lower — and closer to reality.

opencode stores data in SQLite transactionally, so streaming-snapshot dedup is not needed for opencode data. The adapter uses `INSERT OR REPLACE` (upsert by message id) so re-imports are idempotent without evicting prior snapshots.

## Privacy

Nothing leaves your machine. No telemetry. No remote calls for your data. The browser fetches its JSON from `127.0.0.1`, and all JS/CSS/fonts are served from that same local server — ECharts is vendored into `web/`, and the UI falls back to system fonts rather than pulling from a font CDN. If you want to verify: `grep -r "https://" token_dashboard/ web/` — you'll find nothing.

## Tech stack

Python 3 (stdlib only) for the CLI, scanner, and HTTP server. SQLite for the local cache. Vanilla JS + ECharts for the UI, no build step. Dark theme, hash-based router, server-sent events for live refresh.

Data flow: `cli.py` → `token_dashboard/scanner.py` (Claude JSONL) and/or `token_dashboard/opencode_source.py` (opencode SQLite) → SQLite DB; `token_dashboard/server.py` exposes `/api/*` JSON routes and serves `web/`.

## Further reading

- [`CLAUDE.md`](CLAUDE.md) — conventions and architecture overview (also picked up automatically by Claude Code)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to develop and test
- [`docs/KNOWN_LIMITATIONS.md`](docs/KNOWN_LIMITATIONS.md) — rough edges
- [`docs/inspiration.md`](docs/inspiration.md) — prior art and how this project diverges
- [`docs/2026-07-16-opencode-support-design.md`](docs/2026-07-16-opencode-support-design.md) — design spec for the opencode adapter
- [`docs/plans/2026-07-16-opencode-support.md`](docs/plans/2026-07-16-opencode-support.md) — implementation plan with 7 tasks

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Short version: fork, `python3 -m unittest discover tests` before opening a PR, keep it stdlib-only. Contributions for both Claude Code and opencode support are welcome.

## License

[MIT](LICENSE).
