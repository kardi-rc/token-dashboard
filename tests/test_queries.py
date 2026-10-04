import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from token_dashboard import queries
from token_dashboard.db import (
    init_db, connect,
    overview_totals, expensive_prompts, project_summary,
    tool_token_breakdown, recent_sessions,
    daily_token_breakdown, project_name_for,
    skill_breakdown,
)
from token_dashboard.pricing import cost_for
from token_dashboard.queries import model_breakdown, session_turns


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "q.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, parent_uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens,
              prompt_text, prompt_chars)
            VALUES
              ('u1',NULL,'s1','projA','user','2026-04-10T00:00:00Z',NULL,0,0,0,0,0,'big prompt',10),
              ('a1','u1','s1','projA','assistant','2026-04-10T00:00:01Z','claude-opus-4-7',100,200,300,0,0,NULL,NULL),
              ('u2',NULL,'s2','projB','user','2026-04-11T00:00:00Z',NULL,0,0,0,0,0,'small',5),
              ('a2','u2','s2','projB','assistant','2026-04-11T00:00:01Z','claude-sonnet-4-6',5,5,0,0,0,NULL,NULL);
            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error)
            VALUES ('a1','s1','projA','Read','foo.py','2026-04-10T00:00:01Z',0),
                   ('a1','s1','projA','Bash','npm test','2026-04-10T00:00:01Z',0);
            """)
            c.commit()

    def test_overview_totals(self):
        t = overview_totals(self.db, since=None, until=None)
        self.assertEqual(t["sessions"], 2)
        self.assertEqual(t["turns"], 2)
        self.assertEqual(t["input_tokens"], 105)
        self.assertEqual(t["output_tokens"], 205)

    def test_expensive_prompts_orders_by_tokens(self):
        rows = expensive_prompts(self.db, limit=10)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["prompt_text"], "big prompt")

    def test_expensive_prompts_sort_recent(self):
        rows = expensive_prompts(self.db, limit=10, sort="recent")
        self.assertEqual(rows[0]["prompt_text"], "small")
        self.assertEqual(rows[1]["prompt_text"], "big prompt")

    def test_project_summary_groups(self):
        rows = project_summary(self.db)
        slugs = {r["project_slug"]: r for r in rows}
        self.assertIn("projA", slugs)
        self.assertEqual(slugs["projA"]["turns"], 1)

    def test_tool_breakdown(self):
        rows = tool_token_breakdown(self.db)
        names = {r["tool_name"]: r for r in rows}
        self.assertIn("Read", names)
        self.assertIn("Bash", names)

    def test_recent_sessions(self):
        rows = recent_sessions(self.db, limit=5)
        self.assertEqual(rows[0]["session_id"], "s2")

    def test_session_turns(self):
        rows = session_turns(self.db, "s1")
        self.assertEqual(len(rows), 2)

    def test_daily_token_breakdown_groups_by_day(self):
        rows = daily_token_breakdown(self.db)
        days = {r["day"]: r for r in rows}
        self.assertIn("2026-04-10", days)
        self.assertIn("2026-04-11", days)
        self.assertEqual(days["2026-04-10"]["input_tokens"], 100)
        self.assertEqual(days["2026-04-10"]["output_tokens"], 200)
        self.assertEqual(days["2026-04-10"]["cache_read_tokens"], 300)

    def test_daily_token_breakdown_respects_since(self):
        rows = daily_token_breakdown(self.db, since="2026-04-11T00:00:00Z")
        days = [r["day"] for r in rows]
        self.assertEqual(days, ["2026-04-11"])

    def test_model_breakdown_respects_since_and_groups(self):
        rows = model_breakdown(self.db)
        models = {r["model"]: r for r in rows}
        self.assertIn("claude-opus-4-7", models)
        self.assertIn("claude-sonnet-4-6", models)
        self.assertEqual(models["claude-opus-4-7"]["input_tokens"], 100)

        filtered = model_breakdown(self.db, since="2026-04-11T00:00:00Z")
        names = [r["model"] for r in filtered]
        self.assertEqual(names, ["claude-sonnet-4-6"])


class SkillBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "s.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, session_id, project_slug, type, timestamp)
            VALUES
              ('u1','s1','pA','user','2026-04-10T00:00:00Z'),
              ('a1','s1','pA','assistant','2026-04-10T00:00:01Z'),
              ('u2','s2','pA','user','2026-04-11T00:00:00Z'),
              ('a2','s2','pA','assistant','2026-04-11T00:00:01Z');

            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, result_tokens, timestamp, is_error)
            VALUES
              ('a1','s1','pA','Skill','brainstorming',NULL,'2026-04-10T00:00:01Z',0),
              ('u1','s1','pA','_tool_result','use-123',500,'2026-04-10T00:00:05Z',0),
              ('a1','s1','pA','Skill','brainstorming',NULL,'2026-04-10T00:00:30Z',0),
              ('u1','s1','pA','_tool_result','use-124',800,'2026-04-10T00:00:32Z',0),
              ('a2','s2','pA','Skill','create-skill',NULL,'2026-04-11T00:00:01Z',0),
              ('u2','s2','pA','_tool_result','use-125',1200,'2026-04-11T00:00:02Z',0);
            """)
            c.commit()

    def test_groups_by_skill(self):
        rows = skill_breakdown(self.db)
        by_name = {r["skill"]: r for r in rows}
        self.assertEqual(by_name["brainstorming"]["invocations"], 2)
        self.assertEqual(by_name["brainstorming"]["sessions"], 1)
        self.assertEqual(by_name["create-skill"]["invocations"], 1)

    def test_orders_by_invocations(self):
        rows = skill_breakdown(self.db)
        self.assertEqual(rows[0]["skill"], "brainstorming")

    def test_respects_since(self):
        rows = skill_breakdown(self.db, since="2026-04-11T00:00:00Z")
        names = [r["skill"] for r in rows]
        self.assertEqual(names, ["create-skill"])


class ProjectNameTests(unittest.TestCase):
    def test_basename_of_posix_cwd(self):
        self.assertEqual(project_name_for("/Users/x/foo", "slug"), "foo")

    def test_basename_of_windows_cwd(self):
        self.assertEqual(
            project_name_for(r"C:\Users\alice\projects\Token Dashboard", "anything"),
            "Token Dashboard",
        )

    def test_trailing_slash_stripped(self):
        self.assertEqual(project_name_for("/a/b/c/", "slug"), "c")

    def test_fallback_uses_last_dash_segment(self):
        self.assertEqual(
            project_name_for(None, "C--Users-x-Foo-Bar"),
            "Bar",
        )

    def test_fallback_single_segment(self):
        self.assertEqual(project_name_for(None, "projA"), "projA")

    def test_empty(self):
        self.assertEqual(project_name_for(None, ""), "")

    def test_walks_up_cwd_to_project_root(self):
        # cwd is a subfolder; slug matches the parent → return the parent's basename
        self.assertEqual(
            project_name_for(
                r"C:\Users\alice\projects\MyProject\subdir",
                "C--Users-alice-projects-MyProject",
            ),
            "MyProject",
        )

    def test_walks_up_preserves_spaces(self):
        self.assertEqual(
            project_name_for(
                r"C:\Users\alice\projects\Token Dashboard\src\subdir",
                "C--Users-alice-projects-Token-Dashboard",
            ),
            "Token Dashboard",
        )


class ProjectNameInQueriesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "n.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, session_id, project_slug, cwd, type, timestamp,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)
            VALUES
              ('u1','s1','C--Users-x-My-Repo','/Users/x/My Repo','user','2026-04-10T00:00:00Z',0,0,0,0,0),
              ('a1','s1','C--Users-x-My-Repo','/Users/x/My Repo','assistant','2026-04-10T00:00:01Z',10,20,0,0,0),
              ('u2','s2','slugOnly',NULL,'user','2026-04-11T00:00:00Z',0,0,0,0,0),
              ('a2','s2','slugOnly',NULL,'assistant','2026-04-11T00:00:01Z',5,5,0,0,0);
            """)
            c.commit()

    def test_project_summary_uses_cwd_basename(self):
        rows = project_summary(self.db)
        names = {r["project_slug"]: r["project_name"] for r in rows}
        self.assertEqual(names["C--Users-x-My-Repo"], "My Repo")
        self.assertEqual(names["slugOnly"], "slugOnly")

    def test_recent_sessions_has_project_name(self):
        rows = recent_sessions(self.db)
        by_sid = {r["session_id"]: r for r in rows}
        self.assertEqual(by_sid["s1"]["project_name"], "My Repo")
        self.assertEqual(by_sid["s2"]["project_name"], "slugOnly")


class CostAwareQueriesTests(unittest.TestCase):
    """Cost-aware queries (spec Design Decisions §3): AC-B4..AC-B7.

    Fixture is a temp DB with explicit-path inserts mixing stored-cost,
    zero-cost and NULL-cost rows on several models. Pricing is an injected
    dict (no file reads, no env) so expected values are hand-computable.
    """

    PRICING = {
        "models": {
            "glm-5.2": {"tier": "pro", "input": 3.0, "output": 15.0,
                        "cache_read": 0.3, "cache_create_5m": 3.75,
                        "cache_create_1h": 6.0},
            "zero-mod": {"tier": "pro", "input": 2.0, "output": 8.0,
                         "cache_read": 0.0, "cache_create_5m": 0.0,
                         "cache_create_1h": 0.0},
            "null-only": {"tier": "pro", "input": 1.0, "output": 4.0,
                          "cache_read": 0.5, "cache_create_5m": 1.0,
                          "cache_create_1h": 2.0},
            "stored-only": {"tier": "pro", "input": 1.0, "output": 1.0,
                            "cache_read": 0.0, "cache_create_5m": 0.0,
                            "cache_create_1h": 0.0},
        },
        "tier_fallback": {},
        "plans": {"api": {"label": "API", "monthly": 0}},
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "cost.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens,
              cache_create_5m_tokens, cache_create_1h_tokens, cost_usd)
            VALUES
              -- AC-B6 mixed group: stored 0.42 row + NULL row on one model
              ('m1','sc','pc','assistant','2026-04-10T00:00:01Z','glm-5.2',100,200,0,0,0,0.42),
              ('m2','sc','pc','assistant','2026-04-10T00:00:02Z','glm-5.2',1000000,0,0,0,0,NULL),
              -- AC-B7 zero-cost row (flat/subscription provider): stored 0.0
              ('m3','sc','pc','assistant','2026-04-10T00:00:03Z','zero-mod',500000,100000,0,0,0,0.0),
              -- AC-B5 all-NULL group (v1/Claude shape)
              ('m4','sc','pc','assistant','2026-04-10T00:00:04Z','null-only',200000,30000,0,0,0,NULL),
              ('m5','sc','pc','assistant','2026-04-10T00:00:05Z','null-only',40000,5000,0,0,0,NULL),
              -- AC-B5 all-stored group (no NULL/0 rows)
              ('m6','sc','pc','assistant','2026-04-10T00:00:06Z','stored-only',10,20,0,0,0,0.05),
              ('m7','sc','pc','assistant','2026-04-10T00:00:07Z','stored-only',1,2,0,0,0,0.07),
              -- unpriceable model: stored 0.25 row + NULL row
              ('m8','sc','pc','assistant','2026-04-10T00:00:08Z','no-such-model-xyz',5,6,0,0,0,0.25),
              ('m9','sc','pc','assistant','2026-04-10T00:00:09Z','no-such-model-xyz',7,8,0,0,0,NULL),
              -- user row (turn), always NULL cost
              ('u1','sc','pc','user','2026-04-10T00:00:00Z',NULL,0,0,0,0,0,NULL);
            """)
            c.commit()
        self.by_model = {r["model"]: r for r in queries.model_breakdown(self.db)}

    def test_model_breakdown_field_list_ac_b4(self):
        row = self.by_model["glm-5.2"]
        for key in ("model", "turns", "input_tokens", "output_tokens",
                    "cache_read_tokens", "cache_create_5m_tokens",
                    "cache_create_1h_tokens",
                    "stored_cost", "null_cost_rows",
                    "null_input_tokens", "null_output_tokens",
                    "null_cache_read_tokens", "null_cache_create_5m_tokens",
                    "null_cache_create_1h_tokens"):
            self.assertIn(key, row)
        # Existing keys unchanged: all-row totals still span every row.
        self.assertEqual(row["turns"], 2)
        self.assertEqual(row["input_tokens"], 100 + 1000000)
        self.assertEqual(row["output_tokens"], 200)

    def test_mixed_group_ac_b6(self):
        row = self.by_model["glm-5.2"]
        self.assertAlmostEqual(row["stored_cost"], 0.42)
        self.assertEqual(row["null_cost_rows"], 1)
        self.assertEqual(row["null_input_tokens"], 1000000)
        self.assertEqual(row["null_output_tokens"], 0)
        self.assertEqual(row["null_cache_read_tokens"], 0)
        self.assertEqual(row["null_cache_create_5m_tokens"], 0)
        self.assertEqual(row["null_cache_create_1h_tokens"], 0)
        usage = {"input_tokens": 1000000, "output_tokens": 0,
                 "cache_read_tokens": 0, "cache_create_5m_tokens": 0,
                 "cache_create_1h_tokens": 0}
        computed = cost_for("glm-5.2", usage, self.PRICING)
        self.assertAlmostEqual(computed["usd"], 3.0, places=9)
        merged = queries.merge_model_group_cost(row, self.PRICING)
        self.assertAlmostEqual(merged["usd"], 0.42 + computed["usd"], places=9)
        self.assertEqual(merged["estimated"], computed["estimated"])

    def test_zero_cost_row_ac_b7(self):
        row = self.by_model["zero-mod"]
        self.assertEqual(row["null_cost_rows"], 1)
        self.assertAlmostEqual(row["stored_cost"], 0.0)
        self.assertEqual(row["null_input_tokens"], 500000)
        self.assertEqual(row["null_output_tokens"], 100000)
        merged = queries.merge_model_group_cost(row, self.PRICING)
        # 500000*2/1e6 + 100000*8/1e6 = 1.0 + 0.8: estimate, NOT 0.
        self.assertGreater(merged["usd"], 0.0)
        self.assertAlmostEqual(merged["usd"], 1.8, places=9)

    def test_all_null_group_matches_plain_cost_for_ac_b5(self):
        row = self.by_model["null-only"]
        self.assertEqual(row["null_cost_rows"], row["turns"])
        self.assertAlmostEqual(row["stored_cost"], 0.0)
        plain = cost_for("null-only", row, self.PRICING)
        merged = queries.merge_model_group_cost(row, self.PRICING)
        self.assertEqual(merged["usd"], plain["usd"])
        self.assertEqual(merged["estimated"], plain["estimated"])

    def test_all_stored_group_ac_b5(self):
        row = self.by_model["stored-only"]
        self.assertEqual(row["null_cost_rows"], 0)
        merged = queries.merge_model_group_cost(row, self.PRICING)
        self.assertEqual(merged["usd"], row["stored_cost"])
        self.assertAlmostEqual(merged["usd"], 0.12, places=9)
        self.assertEqual(merged["estimated"], False)

    def test_unpriceable_model_with_stored_rows_ac_b5(self):
        row = self.by_model["no-such-model-xyz"]
        self.assertEqual(row["null_cost_rows"], 1)
        merged = queries.merge_model_group_cost(row, self.PRICING)
        self.assertIsNotNone(merged["usd"])
        self.assertAlmostEqual(merged["usd"], 0.25, places=9)

    def test_session_turns_exposes_raw_cost_usd(self):
        rows = queries.session_turns(self.db, "sc")
        self.assertEqual(len(rows), 10)
        by_uuid = {r["uuid"]: r for r in rows}
        self.assertIn("cost_usd", by_uuid["m1"])
        self.assertAlmostEqual(by_uuid["m1"]["cost_usd"], 0.42)
        self.assertIsNone(by_uuid["m2"]["cost_usd"])
        self.assertAlmostEqual(by_uuid["m3"]["cost_usd"], 0.0)
        self.assertIsNone(by_uuid["u1"]["cost_usd"])

    def test_effective_message_cost(self):
        usage = {"input_tokens": 1000, "output_tokens": 2000,
                 "cache_read_tokens": 0, "cache_create_5m_tokens": 0,
                 "cache_create_1h_tokens": 0}
        stored = queries.effective_message_cost(0.42, "glm-5.2", usage, self.PRICING)
        self.assertAlmostEqual(stored["usd"], 0.42)
        self.assertEqual(stored["estimated"], False)
        plain = cost_for("glm-5.2", usage, self.PRICING)
        null_eff = queries.effective_message_cost(None, "glm-5.2", usage, self.PRICING)
        self.assertEqual(null_eff["usd"], plain["usd"])
        self.assertEqual(null_eff["estimated"], plain["estimated"])
        zero_eff = queries.effective_message_cost(0.0, "glm-5.2", usage, self.PRICING)
        self.assertEqual(zero_eff["usd"], plain["usd"])
        self.assertEqual(zero_eff["estimated"], plain["estimated"])


class CostSeriesTests(unittest.TestCase):
    """cost_series (Costs-tab plan Task 1): daily (date, model) cost series.

    Rows carry the merge-input keys — stored_cost + the five null_* token
    sums — mirroring model_breakdown(), with NO pricing and NO merging (the
    /api/cost-series handler merges later). Timestamps are built from LOCAL
    wall-clock times so bucketing assertions are timezone-agnostic.
    """

    @staticmethod
    def _local_ts(y, m, d, hh=12, mm=0, ss=0):
        """UTC 'Z' ISO string for a LOCAL wall-clock time (tz-agnostic)."""
        naive = datetime(y, m, d, hh, mm, ss)
        return naive.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "cs.db")
        init_db(self.db)

    def _insert(self, uuid, timestamp, model="glm-5.2",
                tokens=(0, 0, 0, 0, 0), cost=None, msg_type="assistant"):
        with connect(self.db) as c:
            c.execute(
                """INSERT INTO messages
                       (uuid, session_id, project_slug, type, timestamp, model,
                        input_tokens, output_tokens, cache_read_tokens,
                        cache_create_5m_tokens, cache_create_1h_tokens, cost_usd)
                   VALUES (?, 'cs', 'cs', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid, msg_type, timestamp, model,
                 tokens[0], tokens[1], tokens[2], tokens[3], tokens[4], cost))
            c.commit()

    def test_empty_period_returns_no_rows(self):
        self._insert("a1", self._local_ts(2026, 3, 10))
        rows = queries.cost_series(self.db, since="2027-01-01T00:00:00Z",
                                   until="2027-02-01T00:00:00Z")
        self.assertEqual(rows, [])

    def test_mixed_bucket_stored_and_null_keys(self):
        # (b) same local date + same model: one 0.42 stored row + one NULL row.
        self._insert("a1", self._local_ts(2026, 3, 10, 10),
                     tokens=(100, 20, 3, 4, 5), cost=0.42)
        self._insert("a2", self._local_ts(2026, 3, 10, 14),
                     tokens=(1000, 200, 30, 40, 50), cost=None)
        rows = queries.cost_series(self.db)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["date"], "2026-03-10")
        self.assertEqual(r["model"], "glm-5.2")
        self.assertEqual(r["turns"], 2)
        # all-row token sums span every row (model_breakdown shape).
        self.assertEqual(r["input_tokens"], 1100)
        self.assertEqual(r["output_tokens"], 220)
        self.assertAlmostEqual(r["stored_cost"], 0.42)
        # null_* sums cover ONLY the NULL-cost row.
        self.assertEqual(r["null_input_tokens"], 1000)
        self.assertEqual(r["null_output_tokens"], 200)
        self.assertEqual(r["null_cache_read_tokens"], 30)
        self.assertEqual(r["null_cache_create_5m_tokens"], 40)
        self.assertEqual(r["null_cache_create_1h_tokens"], 50)

    def test_null_model_groups_under_unknown_string(self):
        # (c) NULL model → keyed under the STRING 'unknown', never dropped.
        self._insert("a1", self._local_ts(2026, 3, 10), model=None,
                     tokens=(7, 8, 0, 0, 0))
        rows = queries.cost_series(self.db)
        self.assertEqual(len(rows), 1)
        self.assertIsInstance(rows[0]["model"], str)
        self.assertEqual(rows[0]["model"], "unknown")
        self.assertEqual(rows[0]["input_tokens"], 7)
        self.assertEqual(rows[0]["output_tokens"], 8)

    def test_local_midnight_bucketing(self):
        # (d) two messages on opposite sides of LOCAL midnight → two dates.
        midnight = datetime(2026, 3, 10)  # local wall-clock midnight
        before = (midnight - timedelta(seconds=1)).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        after = (midnight + timedelta(seconds=1)).astimezone(timezone.utc) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        self._insert("a1", before, tokens=(10, 1, 0, 0, 0))
        self._insert("a2", after, tokens=(20, 2, 0, 0, 0))
        rows = queries.cost_series(self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["date"] for r in rows], ["2026-03-09", "2026-03-10"])
        by_date = {r["date"]: r for r in rows}
        self.assertEqual(by_date["2026-03-09"]["input_tokens"], 10)
        self.assertEqual(by_date["2026-03-10"]["input_tokens"], 20)

    def test_same_day_same_model_one_row_summed(self):
        # (e) two same-model messages, same local day → ONE row, summed.
        self._insert("a1", self._local_ts(2026, 3, 10, 9),
                     tokens=(100, 10, 1, 2, 3), cost=0.10)
        self._insert("a2", self._local_ts(2026, 3, 10, 15),
                     tokens=(200, 20, 2, 4, 6), cost=0.20)
        rows = queries.cost_series(self.db)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["date"], "2026-03-10")
        self.assertEqual(r["model"], "glm-5.2")
        self.assertEqual(r["turns"], 2)
        self.assertEqual(r["input_tokens"], 300)
        self.assertEqual(r["output_tokens"], 30)
        self.assertEqual(r["cache_read_tokens"], 3)
        self.assertEqual(r["cache_create_5m_tokens"], 6)
        self.assertEqual(r["cache_create_1h_tokens"], 9)
        self.assertAlmostEqual(r["stored_cost"], 0.30)
        # both rows carry a usable stored cost → null_* sums are zero.
        self.assertEqual(r["null_input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
