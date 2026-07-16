import http.server
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from token_dashboard.db import init_db
from token_dashboard.server import build_handler, _scan_loop


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


if __name__ == "__main__":
    unittest.main()
