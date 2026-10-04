import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from token_dashboard.pricing import load_pricing, cost_for, format_for_user

PRICING = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pricing.json"))
FIXTURE = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "models-dev-sample.json"))

RATE_KEYS = ("input", "output", "cache_read", "cache_create_5m", "cache_create_1h")


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run_capturing_stderr(fn, *args, **kw):
    """Call fn while capturing stderr; return (result, stderr_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        result = fn(*args, **kw)
    return result, buf.getvalue()


class CostTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def _u(self, **kw):
        base = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        }
        base.update(kw)
        return base

    def test_known_opus_input_cost(self):
        c = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_sonnet_output_cost(self):
        c = cost_for("claude-sonnet-4-6", self._u(output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)

    def test_unknown_opus_falls_back(self):
        c = cost_for("claude-opus-9-9-experimental", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)
        self.assertTrue(c["estimated"])

    def test_unknown_unparseable_returns_none(self):
        c = cost_for("custom-local-model", self._u(input_tokens=9999), self.p)
        self.assertIsNone(c["usd"])

    def test_cache_read_cheaper_than_input(self):
        c_in = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        c_cr = cost_for("claude-opus-4-7", self._u(cache_read_tokens=1_000_000), self.p)
        self.assertLess(c_cr["usd"], c_in["usd"])

    def test_glm_5_2_has_cost(self):
        c = cost_for("glm-5.2", self._u(input_tokens=1_000_000), self.p)
        self.assertIsNotNone(c["usd"])
        self.assertGreater(c["usd"], 0)

    def test_deepseek_v4_pro_has_cost(self):
        c = cost_for("deepseek-v4-pro", self._u(input_tokens=1_000_000), self.p)
        self.assertIsNotNone(c["usd"])
        self.assertGreater(c["usd"], 0)

    def test_kimi_k27_code_has_cost(self):
        c = cost_for("kimi-k2.7-code", self._u(input_tokens=1_000_000), self.p)
        self.assertIsNotNone(c["usd"])
        self.assertGreater(c["usd"], 0)

    def test_unknown_model_returns_null(self):
        c = cost_for("auto", self._u(input_tokens=9999), self.p)
        self.assertIsNone(c["usd"])

    def test_opencode_model_marked_estimated(self):
        c = cost_for("glm-5.2", self._u(input_tokens=1_000_000), self.p)
        self.assertTrue(c["estimated"])


class PlanFormatTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def test_api_plan_returns_raw(self):
        out = format_for_user(12.34, "api", self.p)
        self.assertEqual(out["display_usd"], 12.34)
        self.assertIsNone(out["subscription_usd"])

    def test_pro_plan_returns_subscription_subtitle(self):
        out = format_for_user(12.34, "pro", self.p)
        self.assertEqual(out["subscription_usd"], 20)
        self.assertIn("Pro", out["subtitle"])


class TransformTests(unittest.TestCase):
    """Task 5 (a): models.dev -> rate-row converter (AC-C4).

    New names are imported lazily in setUp so the TDD red phase raises
    ImportError only in the new classes; CostTests/PlanFormatTests keep
    passing (plan Task 5, Step 2).
    """

    def setUp(self):
        from token_dashboard.pricing import transform_models_dev
        self.transform = transform_models_dev
        with open(FIXTURE, encoding="utf-8") as f:
            self.catalog = json.load(f)

    def test_bare_model_ids_as_keys(self):
        out = self.transform(self.catalog)
        self.assertEqual(
            set(out), {"dup-model", "full-model", "no-cache-cost", "other-model"}
        )

    def test_full_cost_row_mapping(self):
        row = self.transform(self.catalog)["full-model"]
        self.assertEqual(row, {
            "input": 3.0, "output": 15.0, "cache_read": 0.3,
            "cache_create_5m": 3.75, "cache_create_1h": 3.75,
        })

    def test_missing_cache_rates_map_to_zero(self):
        row = self.transform(self.catalog)["no-cache-cost"]
        self.assertEqual(row["cache_read"], 0.0)
        self.assertEqual(row["cache_create_5m"], 0.0)
        self.assertEqual(row["cache_create_1h"], 0.0)

    def test_skipped_entries_absent(self):
        out = self.transform(self.catalog)
        for bad in ("string-prices", "no-cost", "bad-model-value", "bad-cost"):
            self.assertNotIn(bad, out)

    def test_duplicate_id_first_provider_in_sorted_order_wins(self):
        # AC-C4: "aaa-vendor" sorts before "zzz-vendor" -> aaa price wins.
        row = self.transform(self.catalog)["dup-model"]
        self.assertEqual(row["input"], 1.0)
        self.assertEqual(row["output"], 2.0)
        self.assertEqual(row["cache_read"], 0.3)
        self.assertEqual(row["cache_create_5m"], 0.4)
        self.assertEqual(row["cache_create_1h"], 0.4)

    def test_rows_carry_no_estimated_or_tier_key(self):
        for row in self.transform(self.catalog).values():
            self.assertNotIn("estimated", row)
            self.assertNotIn("tier", row)

    def test_flat_provider_shape_walked_directly(self):
        # Dual-shape walk (defensive): no "models" key -> walk provider dict.
        cat = {"vendor": {"name": "Acme", "m1": {"cost": {"input": 1.0, "output": 2.0}}}}
        self.assertEqual(self.transform(cat)["m1"]["input"], 1.0)

    def test_non_dict_models_key_falls_back_to_provider_dict(self):
        cat = {"vendor": {"models": "oops", "m2": {"cost": {"input": 1.0, "output": 2.0}}}}
        self.assertIn("m2", self.transform(cat))

    def test_inline_garbage_never_raises(self):
        # AC-C4: converter never raises — non-dict top, non-dict provider/
        # model/cost, string prices, bools, NaN and inf are all just skipped.
        self.assertEqual(self.transform(None), {})
        self.assertEqual(self.transform([]), {})
        self.assertEqual(self.transform("nope"), {})
        self.assertEqual(self.transform({"p": "not-a-dict"}), {})
        self.assertEqual(self.transform({"p": {"m": "not-a-dict"}}), {})
        self.assertEqual(self.transform({"p": {"m": {"cost": "not-a-dict"}}}), {})
        self.assertEqual(self.transform({"p": {"m": {"cost": {"input": "1", "output": "2"}}}}), {})
        self.assertEqual(self.transform({"p": {"m": {"cost": {"input": True, "output": 1.0}}}}), {})
        self.assertEqual(
            self.transform({"p": {"m": {"cost": {"input": float("nan"), "output": 1.0}}}}), {})
        self.assertEqual(
            self.transform({"p": {"m": {"cost": {"input": 1.0, "output": float("inf")}}}}), {})


class RefreshTests(unittest.TestCase):
    """Task 5 (b): refresh_pricing over file:// URLs (AC-C1/C3/C5/C10, EC-12/13/16)."""

    def setUp(self):
        from token_dashboard.pricing import (
            refresh_pricing, transform_models_dev, _fetch_catalog,
        )
        self.refresh = refresh_pricing
        self.transform = transform_models_dev
        self.fetch = _fetch_catalog
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "pricing-cache.json")
        self.url = Path(FIXTURE).resolve().as_uri()  # EC-16
        self.bundled_hash = self._sha256_bundled()

    def tearDown(self):
        # AC-C5: refresh must never touch the bundled pricing.json.
        self.assertEqual(self._sha256_bundled(), self.bundled_hash)

    @staticmethod
    def _sha256_bundled():
        return hashlib.sha256(open(PRICING, "rb").read()).hexdigest()

    def _sentinel_cache(self):
        sentinel = b'{"fetched_at": 1, "source_url": "sentinel", "models": {}}'
        with open(self.cache, "wb") as f:
            f.write(sentinel)
        return sentinel

    def _write_bad_json(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        return Path(bad).resolve().as_uri()

    def test_refresh_writes_exact_cache_shape(self):
        # AC-C3: {"fetched_at", "source_url", "models"} and nothing else.
        ok = self.refresh(self.url, self.cache, now=1727950000)
        self.assertTrue(ok)
        with open(self.cache, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(set(data), {"fetched_at", "source_url", "models"})
        self.assertEqual(data["fetched_at"], 1727950000)
        self.assertEqual(data["source_url"], self.url)
        with open(FIXTURE, encoding="utf-8") as f:
            expected_models = self.transform(json.load(f))
        self.assertEqual(data["models"], expected_models)

    def test_atomic_write_leaves_no_temp_residue(self):
        self.refresh(self.url, self.cache, now=1727950000)
        self.assertEqual(os.listdir(self.tmp.name), ["pricing-cache.json"])

    def test_nonexistent_file_url_one_stderr_line_and_false(self):
        # EC-12: exactly one "pricing refresh skipped:" line, no exception.
        missing = Path(self.tmp.name, "nope.json").resolve().as_uri()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.refresh(missing, self.cache, now=1)
        self.assertFalse(ok)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("pricing refresh skipped:"))
        self.assertFalse(os.path.exists(self.cache))

    def test_failed_refresh_leaves_existing_cache_byte_identical(self):
        # AC-C5/EC-12: failed refresh never rewrites or deletes the cache.
        sentinel = self._sentinel_cache()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.refresh(self._write_bad_json(), self.cache, now=2)
        self.assertFalse(ok)
        self.assertEqual(len(buf.getvalue().splitlines()), 1)
        with open(self.cache, "rb") as f:
            self.assertEqual(f.read(), sentinel)
        self.assertEqual(
            [f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")], [])

    def test_invalid_json_is_a_normal_failure(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.refresh(self._write_bad_json(), self.cache, now=3)
        self.assertFalse(ok)
        self.assertEqual(len(buf.getvalue().splitlines()), 1)

    def test_zero_model_catalog_is_a_failure_and_writes_nothing(self):
        # EC-13: transform yielding 0 models counts as refresh failure.
        empty = os.path.join(self.tmp.name, "empty.json")
        with open(empty, "w", encoding="utf-8") as f:
            f.write("{}")
        sentinel = self._sentinel_cache()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.refresh(Path(empty).resolve().as_uri(), self.cache, now=4)
        self.assertFalse(ok)
        self.assertEqual(len(buf.getvalue().splitlines()), 1)
        with open(self.cache, "rb") as f:
            self.assertEqual(f.read(), sentinel)

    def test_ftp_scheme_rejected_before_any_socket(self):
        # AC-C1: scheme gate raises ValueError inside _fetch_catalog...
        with self.assertRaises(ValueError):
            self.fetch("ftp://models.invalid/api.json")
        # ...and surfaces as a normal one-line refresh failure.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.refresh("ftp://models.invalid/api.json", self.cache, now=5)
        self.assertFalse(ok)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("pricing refresh skipped:"))
        self.assertFalse(os.path.exists(self.cache))


class MaybeRefreshTests(unittest.TestCase):
    """Task 5 (c): TTL via injectable clock (AC-C9, EC-14/15)."""

    BASE = 1727950000

    def setUp(self):
        from token_dashboard.pricing import (
            DEFAULT_PRICING_URL, PRICING_TTL_SECONDS,
            maybe_refresh_pricing, read_pricing_cache, refresh_pricing,
        )
        self.ttl = PRICING_TTL_SECONDS
        self.default_url = DEFAULT_PRICING_URL
        self.maybe = maybe_refresh_pricing
        self.read = read_pricing_cache
        self.refresh = refresh_pricing
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "pricing-cache.json")
        self.url = Path(FIXTURE).resolve().as_uri()
        self.bundled_hash = hashlib.sha256(open(PRICING, "rb").read()).hexdigest()

    def tearDown(self):
        self.assertEqual(
            hashlib.sha256(open(PRICING, "rb").read()).hexdigest(), self.bundled_hash)

    def _seed_cache(self, fetched_at=None):
        # Build the initial cache through the real refresh path (plan Step 1c).
        ok = self.refresh(self.url, self.cache, now=self.BASE if fetched_at is None else fetched_at)
        self.assertTrue(ok)

    def _fetched_at(self):
        return self.read(self.cache)["fetched_at"]

    def test_constants(self):
        self.assertEqual(self.default_url, "https://models.dev/api.json")
        self.assertEqual(self.ttl, 604800)

    def test_no_cache_refreshes(self):
        # EC-14/first run: missing cache -> refresh.
        ok = self.maybe(self.url, self.cache, now=self.BASE)
        self.assertTrue(ok)
        self.assertEqual(self._fetched_at(), self.BASE)

    def test_fresh_cache_no_fetch(self):
        self._seed_cache()
        before = open(self.cache, "rb").read()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.maybe(self.url, self.cache, now=self.BASE + 604799)
        self.assertFalse(ok)
        self.assertEqual(open(self.cache, "rb").read(), before)
        self.assertEqual(buf.getvalue(), "")

    def test_age_exactly_ttl_refreshes(self):
        # EC-15: boundary is inclusive (>=).
        self._seed_cache()
        ok = self.maybe(self.url, self.cache, now=self.BASE + 604800)
        self.assertTrue(ok)
        self.assertEqual(self._fetched_at(), self.BASE + 604800)

    def test_negative_age_refreshes(self):
        # Clock skew: fetched_at in the future -> refresh (EC-15).
        self._seed_cache(fetched_at=self.BASE + 5000)
        ok = self.maybe(self.url, self.cache, now=self.BASE)
        self.assertTrue(ok)
        self.assertEqual(self._fetched_at(), self.BASE)

    def test_corrupt_cache_refreshes(self):
        # EC-14: corrupt cache is treated as no cache -> refresh.
        with open(self.cache, "w", encoding="utf-8") as f:
            f.write("{{{ not json")
        ok = self.maybe(self.url, self.cache, now=self.BASE + 10)
        self.assertTrue(ok)
        self.assertEqual(self._fetched_at(), self.BASE + 10)

    def test_force_always_refreshes(self):
        self._seed_cache()
        ok = self.maybe(self.url, self.cache, force=True, now=self.BASE + 5)
        self.assertTrue(ok)
        self.assertEqual(self._fetched_at(), self.BASE + 5)

    def test_failure_is_swallowed_and_returns_false(self):
        # AC-C10: no exception escapes maybe_refresh_pricing.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ok = self.maybe("ftp://models.invalid/api.json", self.cache, now=self.BASE)
        self.assertFalse(ok)
        self.assertEqual(len(buf.getvalue().splitlines()), 1)


class EffectivePricingTests(unittest.TestCase):
    """Task 5 (d): bundled + cache merge (AC-C5/C6, EC-14/22)."""

    def setUp(self):
        from token_dashboard.pricing import load_effective_pricing, read_pricing_cache
        self.effective = load_effective_pricing
        self.read = read_pricing_cache
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "pricing-cache.json")
        self.bundle = load_pricing(PRICING)
        self.snapshot = json.dumps(self.bundle, sort_keys=True)

    def _write_cache(self, payload):
        with open(self.cache, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _row(self, n):
        return {"input": n, "output": n, "cache_read": n,
                "cache_create_5m": n, "cache_create_1h": n}

    def test_no_cache_returns_bundled(self):
        self.assertEqual(self.effective(PRICING, self.cache), self.bundle)
        self.assertEqual(self.effective(PRICING, None), self.bundle)

    def test_cache_wins_per_model_id_and_both_sides_survive(self):
        # EC-22 / AC-C6: cache overlay per id, bundled-only stays, cache-only added.
        cache_row = self._row(9.9)
        self._write_cache({
            "fetched_at": 1727950000,
            "source_url": "https://models.dev/api.json",
            "models": {"claude-opus-4-7": cache_row, "cache-only-model": self._row(1.0)},
        })
        eff = self.effective(PRICING, self.cache)
        self.assertEqual(eff["models"]["claude-opus-4-7"], cache_row)
        self.assertEqual(
            eff["models"]["claude-sonnet-4-6"], self.bundle["models"]["claude-sonnet-4-6"])
        self.assertEqual(eff["models"]["cache-only-model"], self._row(1.0))

    def test_tier_fallback_and_plans_always_from_bundled(self):
        # AC-C6: a poisoned cache can never override tier_fallback/plans.
        self._write_cache({
            "fetched_at": 1727950000,
            "source_url": "x",
            "models": {"m": self._row(1.0)},
            "tier_fallback": {"opus": self._row(999.0)},
            "plans": {"api": {"label": "POISON", "monthly": 999}},
        })
        eff = self.effective(PRICING, self.cache)
        self.assertEqual(eff["tier_fallback"], self.bundle["tier_fallback"])
        self.assertEqual(eff["plans"], self.bundle["plans"])

    def test_malformed_cache_rows_dropped_individually(self):
        # EC-14: one bad row must not sink the whole cache.
        good = self._row(2.5)
        bad_missing = {"input": 1, "output": 1, "cache_read": 1, "cache_create_5m": 1}
        bad_string = dict(self._row(1), output="x")
        self._write_cache({
            "fetched_at": 1727950000, "source_url": "x",
            "models": {"good-model": good, "bad-missing": bad_missing,
                       "bad-string": bad_string, "non-dict-row": "oops"},
        })
        eff = self.effective(PRICING, self.cache)
        self.assertEqual(eff["models"]["good-model"], good)
        for bad in ("bad-missing", "bad-string", "non-dict-row"):
            self.assertNotIn(bad, eff["models"])

    def test_corrupt_cache_files_give_exactly_bundled(self):
        # EC-14: invalid JSON / non-dict top / bad fetched_at / non-dict models.
        cases = [
            "{invalid json",
            json.dumps([1, 2, 3]),
            json.dumps({"source_url": "x", "models": {}}),                      # no fetched_at
            json.dumps({"fetched_at": "abc", "models": {}}),                     # non-numeric
            json.dumps({"fetched_at": 1727950000, "models": "nope"}),            # models not dict
        ]
        for raw in cases:
            with open(self.cache, "w", encoding="utf-8") as f:
                f.write(raw)
            self.assertIsNone(self.read(self.cache), msg=raw)
            self.assertEqual(self.effective(PRICING, self.cache), self.bundle, msg=raw)
            os.remove(self.cache)

    def test_bundled_never_mutated(self):
        # AC-C5: returned dict is a NEW object; bundled copy stays pristine.
        self._write_cache({
            "fetched_at": 1727950000, "source_url": "x",
            "models": {"claude-opus-4-7": self._row(9.9), "added": self._row(1.0)},
        })
        eff = self.effective(PRICING, self.cache)
        self.assertIsNot(eff, self.bundle)
        self.assertEqual(json.dumps(self.bundle, sort_keys=True), self.snapshot)
        self.assertNotEqual(eff, self.bundle)


class IngestValidationTests(unittest.TestCase):
    """Devil-gate K1b/O1b: model ids and rates coming from an EXTERNAL origin
    (the models.dev catalog / the refresh cache) are validated at ingest, not
    only at render. Unsafe ids and negative rates never reach the cache or the
    effective pricing merge."""

    def setUp(self):
        from token_dashboard.pricing import load_effective_pricing, transform_models_dev
        self.transform = transform_models_dev
        self.effective = load_effective_pricing
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "pricing-cache.json")

    def _row(self, **over):
        row = {"input": 1.0, "output": 2.0, "cache_read": 0.3,
               "cache_create_5m": 0.4, "cache_create_1h": 0.4}
        row.update(over)
        return row

    def test_transform_rejects_html_active_model_id(self):
        # K1b: exactly the payload web/routes/settings.js renders must not be
        # cached (the JS side also escapes — defense in depth, not the only net).
        evil = "<img src=x onerror=alert(1)>"
        out = self.transform({"v": {"models": {
            evil: {"cost": {"input": 1.0, "output": 2.0}},
            "ok-model": {"cost": {"input": 1.0, "output": 2.0}},
        }}})
        self.assertNotIn(evil, out)
        self.assertEqual(set(out), {"ok-model"})

    def test_transform_rejects_quotes_ampersand_control_chars_and_empty(self):
        evil_ids = ['a"b', "a'b", "a&b", "a<b", "a>b", "a\tb", "a\nb", "", "x\x00y"]
        for evil in evil_ids:
            self.assertEqual(
                self.transform({"v": {"models": {evil: {"cost": {"input": 1.0, "output": 2.0}}}}}),
                {}, msg=repr(evil))

    def test_transform_skips_negative_rates(self):
        # O1b: a negative rate would produce a cost < 0, which none of the
        # consumer predicates (cost_usd = 0 / > 0) account for.
        base = {"input": 1.0, "output": 2.0, "cache_read": 0.3, "cache_write": 0.4}
        for key in ("input", "output", "cache_read", "cache_write"):
            cost = dict(base)
            cost[key] = -1.0
            self.assertEqual(self.transform({"v": {"models": {"m": {"cost": cost}}}}),
                             {}, msg=key)

    def _write_cache(self, models):
        with open(self.cache, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": 1727950000, "source_url": "x", "models": models}, f)

    def test_effective_merge_strips_non_rate_keys(self):
        # K1: only the five rate keys may cross the ingest boundary, so a
        # poisoned cache can never smuggle a render field (tier/estimated)
        # into /api/plan — the settings badge would interpolate it.
        self._write_cache({"m": dict(self._row(),
                                     tier='<img src=x onerror=alert(1)>',
                                     estimated="poison")})
        eff = self.effective(PRICING, self.cache)
        self.assertEqual(set(eff["models"]["m"]), set(RATE_KEYS))

    def test_effective_merge_drops_unsafe_ids_and_negative_rates(self):
        evil = "<svg onload=alert(1)>"
        self._write_cache({
            evil: self._row(),
            "neg-model": self._row(output=-3.0),
            "good-model": self._row(input=7.0),
        })
        bundle = load_pricing(PRICING)
        eff = self.effective(PRICING, self.cache)
        self.assertNotIn(evil, eff["models"])
        self.assertNotIn("neg-model", eff["models"])
        self.assertEqual(eff["models"]["good-model"], self._row(input=7.0))
        self.assertEqual(eff["models"]["claude-opus-4-7"], bundle["models"]["claude-opus-4-7"])


if __name__ == "__main__":
    unittest.main()
