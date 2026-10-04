# Project Memory: token-dashboard
Last updated: 2026-10-04

## Overview
- Stack: Python 3.8+, stdlib only (zero third-party imports); vanilla JS frontend (no build step, ECharts vendored); SQLite; unittest (no pytest).
- Architecture: `cli.py` → `scanner.py` (Claude JSONL) OR `opencode_source.py` (opencode SQLite) → `~/.claude/token-dashboard.db` → `server.py` (JSON `/api/*`, SSE `/api/stream`, static `web/`).
- Instruction sources for agents: AGENTS.md, CLAUDE.md, docs/KNOWN_LIMITATIONS.md
- See memoria.md for details

## Living Roadmap
- See roadmap.md

## Sub-memories (indexed by area)
- (none yet)
