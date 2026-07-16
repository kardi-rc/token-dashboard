import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


if __name__ == "__main__":
    unittest.main()
