import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# EC-16: every pricing URL under test is a file:// URI of the offline fixture —
# the suite NEVER touches a live URL.
FIXTURE_URI = (Path(ROOT) / "tests" / "fixtures" / "models-dev-sample.json").resolve().as_uri()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.proj, "demo"))
        with open(os.path.join(self.proj, "demo", "s.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"type":"user","uuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:00Z","isSidechain":false,"message":{"role":"user","content":"hi"}}\n')
            f.write('{"type":"assistant","uuid":"a1","parentUuid":"u1","sessionId":"s1","timestamp":"2026-04-19T00:00:01Z","isSidechain":false,"message":{"model":"claude-haiku-4-5","usage":{"input_tokens":1,"output_tokens":1}}}\n')
        self.db = os.path.join(self.tmp, "t.db")

    def _run(self, *args):
        env = {**os.environ, "TOKEN_DASHBOARD_DB": self.db}
        return subprocess.run(
            [sys.executable, "cli.py", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )

    def test_scan_then_today(self):
        r1 = self._run("scan", "--projects-dir", self.proj)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("scanned", r1.stdout)
        r2 = self._run("today")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("Token Dashboard", r2.stdout)

    def test_stats(self):
        self._run("scan", "--projects-dir", self.proj)
        r = self._run("stats")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all time", r.stdout)

    def test_tips_runs_without_data(self):
        r = self._run("tips")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no suggestions", r.stdout)


class CliBackendTests(unittest.TestCase):
    def test_detect_backends_auto_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "projects"
            odb = Path(tmp) / "opencode.db"
            pdir.mkdir()
            (pdir / "proj").mkdir()
            (pdir / "proj" / "sess.jsonl").write_text("{}")
            odb.write_text("")
            self.assertEqual(
                cli._detect_backends("auto", str(pdir), str(odb)),
                {"claude", "opencode"},
            )

    def test_detect_backends_auto_only_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "projects"
            pdir.mkdir()
            (pdir / "proj").mkdir()
            (pdir / "proj" / "sess.jsonl").write_text("{}")
            self.assertEqual(
                cli._detect_backends("auto", str(pdir), str(Path(tmp) / "nonexistent.db")),
                {"claude"},
            )

    def test_detect_backends_auto_only_opencode(self):
        with tempfile.TemporaryDirectory() as tmp:
            odb = Path(tmp) / "opencode.db"
            odb.write_text("")
            self.assertEqual(
                cli._detect_backends("auto", str(Path(tmp) / "noproj"), str(odb)),
                {"opencode"},
            )

    def test_detect_backends_explicit_opencode(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                cli._detect_backends("opencode", str(Path(tmp) / "p"), str(Path(tmp) / "o.db")),
                {"opencode"},
            )

    def test_detect_backends_explicit_claude(self):
        self.assertEqual(
            cli._detect_backends("claude", "/nonexistent", "/nonexistent.db"),
            {"claude"},
        )

    def test_cmd_scan_opencode_backend(self):
        with patch("token_dashboard.opencode_source.import_opencode") as mock_import, \
             patch.object(cli, "init_db"), \
             patch.object(cli, "scan_dir") as mock_scan, \
             tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dash.db"
            odb = Path(tmp) / "opencode.db"
            odb.write_text("")
            mock_import.return_value = {"sessions": 0, "messages": 0, "tool_calls": 0}
            args = argparse.Namespace(
                db=str(db), projects_dir=None, backend="opencode", opencode_db=str(odb),
            )
            cli.cmd_scan(args)
            mock_import.assert_called_once_with(str(odb), str(db))
            mock_scan.assert_not_called()


class CliPricingTests(unittest.TestCase):
    """`cli.py pricing` subprocess tests (Task 6 / AC-C8, EC-16).

    Real subprocesses like CliTests._run; env vars are the CLI's parameter
    channel (AGENTS.md). Every run gets a temp TOKEN_DASHBOARD_DB and an
    explicit PRICING_URL (default: the offline fixture URI)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.cache = Path(self.tmp) / "pricing-cache.json"

    def _run(self, *args, pricing_url=FIXTURE_URI):
        env = {**os.environ, "TOKEN_DASHBOARD_DB": self.db, "PRICING_URL": pricing_url}
        return subprocess.run(
            [sys.executable, "cli.py", *args],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )

    def test_pricing_status_without_cache_never_fetches(self):
        r = self._run("pricing")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no cache — bundled pricing only", r.stdout)
        self.assertFalse(self.cache.exists())

    def test_pricing_status_with_cache_is_readonly(self):
        payload = {
            "fetched_at": 1777000000,
            "source_url": FIXTURE_URI,
            "models": {
                "model-a": {"input": 1.0, "output": 2.0, "cache_read": 0.1,
                            "cache_create_5m": 0.2, "cache_create_1h": 0.3},
                "model-b": {"input": 0.5, "output": 1.5, "cache_read": 0.05,
                            "cache_create_5m": 0.1, "cache_create_1h": 0.1},
            },
        }
        self.cache.write_text(json.dumps(payload), encoding="utf-8")
        before = self.cache.read_bytes()
        r = self._run("pricing")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(FIXTURE_URI, r.stdout)
        self.assertIn("models: 2", r.stdout)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_pricing_refresh_creates_cache(self):
        r = self._run("pricing", "--refresh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.cache.exists())
        data = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(data["source_url"], FIXTURE_URI)
        self.assertTrue(data["models"])

    def test_pricing_refresh_failure_exits_1_cleanly(self):
        missing = (Path(self.tmp) / "no-catalog.json").resolve().as_uri()
        r = self._run("pricing", "--refresh", pricing_url=missing)
        self.assertEqual(r.returncode, 1, r.stderr)
        lines = r.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("pricing refresh skipped:"), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.cache.exists())


class CliDashboardPricingTests(unittest.TestCase):
    """Dashboard auto-refresh hook (Task 6 / AC-C9 + AC-C10): real subprocess,
    free port via bind-to-0-then-release, bounded poll of 30 x 0.5 s."""

    def _serve(self, pricing_url):
        """Start `cli.py dashboard --backend claude --no-scan --no-open`, poll
        PORT until it answers; returns (proc, tmp, port). Fails (after cleanup)
        if the port never answers — the hook must never block startup."""
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "t.db")
        proj = os.path.join(tmp, "projects")
        os.makedirs(proj)
        Path(proj, "fake.jsonl").touch()  # hermetic; --backend claude short-circuits _detect_backends
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = {**os.environ,
               "TOKEN_DASHBOARD_DB": db,
               "CLAUDE_PROJECTS_DIR": proj,
               "PRICING_URL": pricing_url,
               "HOST": "127.0.0.1",
               "PORT": str(port)}
        proc = subprocess.Popen(
            [sys.executable, "cli.py", "dashboard", "--backend", "claude",
             "--no-scan", "--no-open"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        ready = False
        for _ in range(30):
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    ready = True
                    break
            except OSError:
                time.sleep(0.5)
        if not ready:
            proc.terminate()
            try:
                _, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, err = proc.communicate(timeout=10)
            self.fail(f"dashboard did not answer on port {port} "
                      f"(rc={proc.returncode})\nstderr:\n{err}")
        return proc, tmp, port

    @staticmethod
    def _stop(proc):
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()

    def test_dashboard_first_run_writes_pricing_cache(self):
        proc = tmp = port = None
        try:
            proc, tmp, port = self._serve(FIXTURE_URI)
            self.assertTrue((Path(tmp) / "pricing-cache.json").exists())
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/overview", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            self._stop(proc)

    def test_dashboard_survives_pricing_refresh_failure(self):
        missing = (Path(tempfile.mkdtemp()) / "no-catalog.json").resolve().as_uri()
        proc = tmp = port = None
        try:
            proc, tmp, port = self._serve(missing)
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/overview", timeout=5
            ) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            self._stop(proc)


if __name__ == "__main__":
    unittest.main()
