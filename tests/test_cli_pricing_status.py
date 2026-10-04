"""L1: ``pricing`` status printing survives a poisoned ``fetched_at``.

Unit-level (no subprocess): ``cmd_pricing`` is called directly with a stub
args namespace and a patched cache path, so the whole check stays offline and
inside ~1s. A cache written by a hostile/huge ``fetched_at`` used to raise
OverflowError out of ``datetime.fromtimestamp`` — a traceback instead of the
command's clean-exit convention.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli  # noqa: E402  (repo-root module, path set above)


class PricingStatusOverflowTests(unittest.TestCase):
    def _run(self, fetched_at):
        tmp = tempfile.mkdtemp()
        cache = os.path.join(tmp, "pricing-cache.json")
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": fetched_at, "source_url": "file:///fixture",
                       "models": {}}, f)
        out = io.StringIO()
        with patch("cli._pricing_cache_path", return_value=Path(cache)):
            with contextlib.redirect_stdout(out):
                cli.cmd_pricing(SimpleNamespace(db=os.path.join(tmp, "t.db"),
                                                refresh=False))
        return out.getvalue()

    def test_huge_fetched_at_prints_raw_value_instead_of_traceback(self):
        printed = self._run(1e18)  # finite number, unconvertible to a datetime
        self.assertIn("fetched_at: 1e+18", printed)
        self.assertIn("source_url: file:///fixture", printed)
        self.assertIn("models: 0", printed)

    def test_normal_fetched_at_still_prints_isoformat(self):
        printed = self._run(1727950000)
        line = [ln for ln in printed.splitlines() if ln.startswith("fetched_at: ")][0]
        self.assertRegex(line, r"^fetched_at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


if __name__ == "__main__":
    unittest.main()
