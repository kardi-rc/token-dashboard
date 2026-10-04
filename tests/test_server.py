import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from token_dashboard.db import init_db
from token_dashboard.pricing import cost_for, load_pricing
from token_dashboard.server import build_handler, _scan_loop, run, IPv6HTTPServer, PRICING_JSON


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens, prompt_text, prompt_chars) VALUES ('u',NULL,'s','p','user','2026-04-19T00:00:00Z',NULL,0,0,0,0,0,'hi',2)")
            c.execute("INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model, input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens) VALUES ('a','u','s','p','assistant','2026-04-19T00:00:01Z','claude-haiku-4-5',1,1,0,0,0)")
            c.commit()
        self.port = _free_port()
        H = build_handler(self.db, projects_dir="/nonexistent", backends={"claude"}, opencode_db="/nonexistent/oc.db")
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()

    def _get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}").read()

    def test_index_html(self):
        body = self._get("/")
        self.assertIn(b"Token Dashboard", body)

    def test_overview_json(self):
        body = json.loads(self._get("/api/overview"))
        self.assertIn("sessions", body)
        self.assertEqual(body["sessions"], 1)

    def test_prompts_json(self):
        body = json.loads(self._get("/api/prompts?limit=10"))
        self.assertIsInstance(body, list)

    def test_projects_json(self):
        body = json.loads(self._get("/api/projects"))
        self.assertIsInstance(body, list)
        self.assertEqual(body[0]["project_slug"], "p")

    def test_plan_json(self):
        body = json.loads(self._get("/api/plan"))
        self.assertIn("plan", body)
        self.assertIn("pricing", body)

    def test_head_returns_200_not_501(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_head_api_endpoint(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/overview", method="HEAD")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_post_plan_rejects_non_string_value(self):
        # O2: a dict reached the sqlite bind -> ProgrammingError, the handler
        # thread died, the client got no response. Now: clean 400 JSON.
        status, body = self._post("/api/plan", {"plan": {"nested": 1}})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        # The server is still serving — the bad payload did not kill anything.
        self.assertEqual(json.loads(self._get("/api/overview"))["sessions"], 1)

    def test_post_plan_rejects_unknown_key(self):
        # Allowlist validation: only keys present in pricing["plans"] are stored.
        status, body = self._post("/api/plan", {"plan": "no-such-plan"})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_post_plan_accepts_a_known_key(self):
        plans = list(json.loads(self._get("/api/plan"))["pricing"]["plans"])
        chosen = plans[-1]
        status, body = self._post("/api/plan", {"plan": chosen})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(json.loads(self._get("/api/plan"))["plan"], chosen)

    def test_post_tip_dismiss_rejects_non_string_key(self):
        status, body = self._post("/api/tips/dismiss", {"key": ["not", "a", "str"]})
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class ServerBackendTests(unittest.TestCase):
    def test_scan_loop_calls_opencode_only(self):
        with patch("token_dashboard.server.scan_dir") as mock_scan, \
             patch("token_dashboard.server.import_opencode") as mock_oc:
            mock_scan.return_value = {"files": 0, "messages": 0, "tools": 0}
            mock_oc.return_value = {"sessions": 1, "messages": 5, "tool_calls": 2}
            with patch("token_dashboard.server.time.sleep", side_effect=Exception("stop")):
                with self.assertRaises(Exception):
                    _scan_loop(":memory:", "/tmp/projects", {"opencode"}, "/tmp/oc.db", interval=1.0)
            mock_scan.assert_not_called()
            mock_oc.assert_called_once_with("/tmp/oc.db", ":memory:")

    def test_scan_loop_calls_both_backends(self):
        with patch("token_dashboard.server.scan_dir") as mock_scan, \
             patch("token_dashboard.server.import_opencode") as mock_oc:
            mock_scan.return_value = {"files": 1, "messages": 10, "tools": 3}
            mock_oc.return_value = {"sessions": 1, "messages": 5, "tool_calls": 2}
            with patch("token_dashboard.server.time.sleep", side_effect=Exception("stop")):
                with self.assertRaises(Exception):
                    _scan_loop(":memory:", "/tmp/projects", {"claude", "opencode"}, "/tmp/oc.db", interval=1.0)
            mock_scan.assert_called_once_with("/tmp/projects", ":memory:")
            mock_oc.assert_called_once_with("/tmp/oc.db", ":memory:")

    def test_scan_loop_prints_one_stderr_line_on_failure(self):
        # O4: a scan failure used to be invisible (SSE error event only) — now
        # exactly one "scan failed:" stderr line per failure, loop keeps going.
        import io
        from contextlib import redirect_stderr

        with patch("token_dashboard.server.scan_dir", side_effect=RuntimeError("boom disk")), \
             patch("token_dashboard.server.import_opencode"), \
             patch("token_dashboard.server.time.sleep", side_effect=Exception("stop")):
            buf = io.StringIO()
            with redirect_stderr(buf):
                with self.assertRaises(Exception):
                    _scan_loop(":memory:", "/tmp/projects", {"claude"}, "/tmp/oc.db", interval=1.0)
        lines = [ln for ln in buf.getvalue().splitlines() if "scan failed" in ln]
        self.assertEqual(len(lines), 1, buf.getvalue())
        self.assertIn("boom disk", lines[0])


class DualStackTests(unittest.TestCase):
    def test_dual_stack_creates_two_servers(self):
        with patch("token_dashboard.server.threading.Thread") as mock_thread, \
             patch("token_dashboard.server.http.server.ThreadingHTTPServer") as mock_httpd, \
             patch("token_dashboard.server.IPv6HTTPServer") as mock_ipv6:
            mock_httpd.return_value = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
            mock_ipv6.return_value = http.server.ThreadingHTTPServer(("::1", 0), http.server.BaseHTTPRequestHandler)
            with patch("token_dashboard.server.time.sleep", side_effect=KeyboardInterrupt):
                run("dual", 8090, ":memory:", "/tmp/projects", {"claude"}, "/tmp/oc.db")
            v4_calls = [c for c in mock_httpd.call_args_list if c.args[0] == ("127.0.0.1", 8090)]
            self.assertEqual(len(v4_calls), 1, f"expected one IPv4 server call, got {mock_httpd.call_args_list}")
            mock_ipv6.assert_called_once_with(("::1", 8090), unittest.mock.ANY)

    def test_dual_stack_ipv6_failure_fallback(self):
        with patch("token_dashboard.server.threading.Thread") as mock_thread, \
             patch("token_dashboard.server.http.server.ThreadingHTTPServer") as mock_httpd, \
             patch("token_dashboard.server.IPv6HTTPServer", side_effect=OSError("IPv6 unavailable")) as mock_ipv6:
            mock_httpd.return_value = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
            with patch("token_dashboard.server.time.sleep", side_effect=KeyboardInterrupt):
                run("dual", 8090, ":memory:", "/tmp/projects", {"claude"}, "/tmp/oc.db")
            v4_calls = [c for c in mock_httpd.call_args_list if c.args[0] == ("127.0.0.1", 8090)]
            self.assertEqual(len(v4_calls), 1, f"expected one IPv4 server call, got {mock_httpd.call_args_list}")
            mock_ipv6.assert_called_once_with(("::1", 8090), unittest.mock.ANY)


class ServerCostTests(unittest.TestCase):
    """Endpoint-level cost display (Task 7: AC-B5 stored-cost-first overview
    wiring + AC-C7 effective pricing). Same pattern as ServerTests: temp
    init_db DB + direct INSERTs + real HTTP through build_handler; the
    pricing_cache keyword rides the handler-build path per the Step 1
    inventory (build_handler owns the pricing closure)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        init_db(self.db)
        self.port = _free_port()

    def tearDown(self):
        if getattr(self, "httpd", None):
            self.httpd.shutdown()

    def _insert(self, uuid, model, cost_usd, inp=0, outp=0):
        with sqlite3.connect(self.db) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, "
                "timestamp, model, input_tokens, output_tokens, cache_read_tokens, "
                "cache_create_5m_tokens, cache_create_1h_tokens, cost_usd) "
                "VALUES (?, ?, ?, 'assistant', ?, ?, ?, ?, 0, 0, 0, ?)",
                (uuid, "s", "p", "2026-04-19T00:00:00Z", model, inp, outp, cost_usd),
            )
            c.commit()

    def _serve(self, pricing_cache=None):
        H = build_handler(self.db, projects_dir="/nonexistent",
                          backends={"claude"}, opencode_db="/nonexistent/oc.db",
                          pricing_cache=pricing_cache)
        self.httpd = http.server.HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read())

    def _group(self, model):
        rows = self._get("/api/by-model")[1]
        return next(r for r in rows if r["model"] == model)

    def test_by_model_mixed_group_stored_plus_estimate(self):
        # AC-B5 + AC-B7 end-to-end: stored 0.42 wins, the 0.0 row is estimated
        # from its null-row tokens (bundled glm-5.2 rates, estimated: true).
        self._insert("c1", "glm-5.2", 0.42, inp=100, outp=50)
        self._insert("c2", "glm-5.2", 0.0, inp=1000, outp=700)
        self._serve()
        usage = {"input_tokens": 1000, "output_tokens": 700,
                 "cache_read_tokens": 0, "cache_create_5m_tokens": 0,
                 "cache_create_1h_tokens": 0}
        expected = 0.42 + cost_for("glm-5.2", usage, load_pricing(PRICING_JSON))["usd"]
        g = self._group("glm-5.2")
        self.assertAlmostEqual(g["cost_usd"], expected, places=9)
        self.assertTrue(g["cost_estimated"])
        # /api/overview routes through the same model_breakdown merge (AC-B5):
        # cost = sum of merged groups, cost_estimated = True for a computed part.
        status, totals = self._get("/api/overview")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(totals["cost_usd"], round(expected, 4), places=9)
        self.assertTrue(totals["cost_estimated"])

    def test_by_model_stored_only_group_not_estimated(self):
        self._insert("s1", "kimi-k2.7-code", 0.10)
        self._insert("s2", "kimi-k2.7-code", 0.20)
        self._serve()
        g = self._group("kimi-k2.7-code")
        self.assertAlmostEqual(g["cost_usd"], 0.30, places=9)
        self.assertFalse(g["cost_estimated"])
        status, totals = self._get("/api/overview")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(totals["cost_usd"], 0.30, places=9)
        self.assertFalse(totals["cost_estimated"])

    def test_pricing_cache_overrides_bundled_rates(self):
        # AC-C7: pricing_cache reaches load_effective_pricing — the cache's
        # glm-5.2 row (input 1.0, rest 0) wins over the bundled rates, so the
        # 0.0-row's computed part uses the CACHE rate, not the bundle's.
        self._insert("e1", "glm-5.2", 0.42, inp=100, outp=50)
        self._insert("e2", "glm-5.2", 0.0, inp=1000, outp=700)
        cache = os.path.join(self.tmp, "pricing-cache.json")
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": int(time.time()), "source_url": "file:///fixture",
                       "models": {"glm-5.2": {"input": 1.0, "output": 0.0,
                                              "cache_read": 0.0, "cache_create_5m": 0.0,
                                              "cache_create_1h": 0.0}}}, f)
        self._serve(pricing_cache=cache)
        g = self._group("glm-5.2")
        cache_expected = 0.42 + 1000 * 1.0 / 1_000_000
        bundled_expected = 0.42 + cost_for(
            "glm-5.2",
            {"input_tokens": 1000, "output_tokens": 700, "cache_read_tokens": 0,
             "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0},
            load_pricing(PRICING_JSON))["usd"]
        self.assertAlmostEqual(g["cost_usd"], cache_expected, places=9)
        self.assertNotAlmostEqual(g["cost_usd"], bundled_expected, places=9)
        # Cache rows carry no "estimated" key — the catalog is authoritative
        # (cost_for defaults estimated=False), so the computed part is not flagged.
        self.assertFalse(g["cost_estimated"])

    def test_missing_cache_file_fails_open_to_bundled(self):
        # Fail-open floor (EC-14/AC-C10): missing cache path -> bundled-only
        # pricing still served, /api/overview answers 200.
        self._insert("f1", "glm-5.2", 0.0, inp=1000, outp=700)
        self._serve(pricing_cache=os.path.join(self.tmp, "no-such-cache.json"))
        usage = {"input_tokens": 1000, "output_tokens": 700,
                 "cache_read_tokens": 0, "cache_create_5m_tokens": 0,
                 "cache_create_1h_tokens": 0}
        g = self._group("glm-5.2")
        self.assertAlmostEqual(
            g["cost_usd"],
            cost_for("glm-5.2", usage, load_pricing(PRICING_JSON))["usd"],
            places=9)
        self.assertTrue(g["cost_estimated"])
        status, totals = self._get("/api/overview")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
