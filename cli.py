"""Token Dashboard CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_dashboard.db import init_db, default_db_path, overview_totals
from token_dashboard.scanner import scan_dir
from token_dashboard.tips import all_tips


def _db_path(args) -> str:
    return args.db or os.environ.get("TOKEN_DASHBOARD_DB") or str(default_db_path())


def _projects(args) -> str:
    return (
        args.projects_dir
        or os.environ.get("CLAUDE_PROJECTS_DIR")
        or str(Path.home() / ".claude" / "projects")
    )


def _backend(args) -> str:
    return args.backend or os.environ.get("DASHBOARD_BACKEND") or "auto"


def _opencode_db(args) -> str:
    return (
        args.opencode_db
        or os.environ.get("OPENCODE_DB")
        or str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
    )


def _detect_backends(backend_choice: str, projects_dir: str, opencode_db: str) -> set:
    if backend_choice == "claude":
        return {"claude"}
    if backend_choice == "opencode":
        return {"opencode"}
    result = set()
    pdir = Path(projects_dir)
    if pdir.is_dir() and any(pdir.rglob("*.jsonl")):
        result.add("claude")
    if Path(opencode_db).is_file():
        result.add("opencode")
    if not result:
        raise SystemExit(
            "Token Dashboard: no data sources found (no Claude JSONL and no opencode.db)"
        )
    return result


def _today_range():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return start, end


def cmd_scan(args):
    db = _db_path(args)
    init_db(db)
    projects = _projects(args)
    opencode_db = _opencode_db(args)
    backends = _detect_backends(_backend(args), projects, opencode_db)
    total = {"files": 0, "messages": 0, "tools": 0}
    if "claude" in backends:
        r = scan_dir(projects, db)
        total["files"] += r["files"]
        total["messages"] += r["messages"]
        total["tools"] += r["tools"]
    if "opencode" in backends:
        from token_dashboard.opencode_source import import_opencode

        r = import_opencode(opencode_db, db)
        total["messages"] += r.get("messages", 0)
        total["tools"] += r.get("tool_calls", 0)
    print(f"Token Dashboard: scanned {total['files']} files, {total['messages']} messages, {total['tools']} tool calls")


def cmd_today(args):
    db = _db_path(args)
    init_db(db)
    s, e = _today_range()
    t = overview_totals(db, since=s, until=e)
    print("Token Dashboard — today")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")
    print(f"  cache rd: {t['cache_read_tokens']:>12,}    cache cr: {t['cache_create_5m_tokens']+t['cache_create_1h_tokens']:>12,}")


def cmd_stats(args):
    db = _db_path(args)
    init_db(db)
    t = overview_totals(db)
    print("Token Dashboard — all time")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")


def cmd_tips(args):
    db = _db_path(args)
    init_db(db)
    tips = all_tips(db)
    if not tips:
        print("Token Dashboard: no suggestions")
        return
    for tip in tips:
        print(f"[{tip['category']}] {tip['title']}")
        print(f"  {tip['body']}\n")


def cmd_dashboard(args):
    db = _db_path(args)
    init_db(db)
    projects = _projects(args)
    opencode_db = _opencode_db(args)
    backends = _detect_backends(_backend(args), projects, opencode_db)
    if not args.no_scan:
        if "claude" in backends:
            scan_dir(projects, db)
        if "opencode" in backends:
            from token_dashboard.opencode_source import import_opencode

            import_opencode(opencode_db, db)
    from token_dashboard.server import run

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    display_host = "localhost" if host == "dual" else host
    url = f"http://{display_host}:{port}/"
    if not args.no_open:
        webbrowser.open(url)
    run(host, port, db, projects, backends, opencode_db)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="SQLite path (default ~/.claude/token-dashboard.db)")
    common.add_argument("--projects-dir", help="JSONL root (default ~/.claude/projects)")
    common.add_argument(
        "--backend",
        choices=["auto", "claude", "opencode"],
        help="Data backend: auto, claude, or opencode",
    )
    common.add_argument(
        "--opencode-db",
        help="Path to opencode.db (default ~/.local/share/opencode/opencode.db)",
    )

    p = argparse.ArgumentParser(prog="token-dashboard", description="Local Claude Code usage dashboard", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan",  parents=[common]).set_defaults(func=cmd_scan)
    sub.add_parser("today", parents=[common]).set_defaults(func=cmd_today)
    sub.add_parser("stats", parents=[common]).set_defaults(func=cmd_stats)
    sub.add_parser("tips",  parents=[common]).set_defaults(func=cmd_tips)
    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("--no-scan", action="store_true")
    d.add_argument("--no-open", action="store_true")
    d.set_defaults(func=cmd_dashboard)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
