"""Pricing table + plan-aware cost formatting.

Two pricing sources (spec 2026-10-03 §DD7/§DD8):
  * bundled ``pricing.json`` — offline floor, NEVER written by this module;
  * refresh cache — models.dev catalog fetched on a 7-day TTL, cached as
    ``pricing-cache.json`` next to the internal DB (path owned by cli.py).
``load_effective_pricing`` merges them (cache wins per model id;
``tier_fallback``/``plans`` always come from the bundle).
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional, Union

from .db import connect


def load_pricing(path: Union[str, Path]) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tier_from_name(model: str) -> Optional[str]:
    m = (model or "").lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in m:
            return tier
    return None


def cost_for(model: str, usage: dict, pricing: dict) -> dict:
    """Return {usd, estimated, breakdown}. usd=None when no tier match."""
    rates = pricing["models"].get(model)
    estimated = False
    if rates is not None:
        estimated = bool(rates.get("estimated", False))
    else:
        tier = _tier_from_name(model or "")
        if tier and tier in pricing["tier_fallback"]:
            rates = pricing["tier_fallback"][tier]
            estimated = True
        else:
            return {"usd": None, "estimated": True, "breakdown": {}}
    bd = {
        "input":           usage["input_tokens"]            * rates["input"]           / 1_000_000,
        "output":          usage["output_tokens"]           * rates["output"]          / 1_000_000,
        "cache_read":      usage["cache_read_tokens"]       * rates["cache_read"]      / 1_000_000,
        "cache_create_5m": usage["cache_create_5m_tokens"]  * rates["cache_create_5m"] / 1_000_000,
        "cache_create_1h": usage["cache_create_1h_tokens"]  * rates["cache_create_1h"] / 1_000_000,
    }
    return {"usd": round(sum(bd.values()), 6), "estimated": estimated, "breakdown": bd}


def get_plan(db_path: Union[str, Path], default: str = "api") -> str:
    with connect(db_path) as c:
        row = c.execute("SELECT v FROM plan WHERE k='plan'").fetchone()
    return row["v"] if row else default


def set_plan(db_path: Union[str, Path], plan: str) -> None:
    with connect(db_path) as c:
        c.execute("INSERT OR REPLACE INTO plan (k, v) VALUES ('plan', ?)", (plan,))
        c.commit()


def format_for_user(api_cost_usd: float, plan: str, pricing: dict) -> dict:
    p = pricing["plans"].get(plan, pricing["plans"]["api"])
    if plan == "api" or p["monthly"] == 0:
        return {"display_usd": api_cost_usd, "subtitle": None, "subscription_usd": None}
    return {
        "display_usd":      api_cost_usd,
        "subtitle":         f"You pay ${p['monthly']}/mo on {p['label']}",
        "subscription_usd": p["monthly"],
    }


# ---------------------------------------------------------------------------
# Pricing refresh: fetch, transform, cache, TTL, merge (spec §DD7/§DD8).
# Fail-open everywhere — refresh never raises, never blocks past the fetch
# timeout, and never touches the bundled pricing.json (AC-C5).
# ---------------------------------------------------------------------------

DEFAULT_PRICING_URL = "https://models.dev/api.json"
PRICING_TTL_SECONDS = 604800
FETCH_TIMEOUT_SECONDS = 10
_ALLOWED_SCHEMES = ("http", "https", "file")
_RATE_KEYS = ("input", "output", "cache_read", "cache_create_5m", "cache_create_1h")


def _is_num(v) -> bool:
    """Finite non-bool number — shared by transform and cache validation."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


_UNSAFE_ID_CHARS = frozenset("<>&\"'")


def _safe_model_id(mid) -> bool:
    """Allowlist for EXTERNAL-origin model ids (K1b — ingest-side XSS net).

    Accepts only a plain, non-empty, printable string with no HTML-significant
    character: non-str, empty, ``""``/``''``/``<``/``>``/``&`` or any control
    character (``isprintable()`` is False for newline, tab, NUL, ...) is
    rejected. Rejected ids are skipped, exactly like a malformed rate row.
    The frontend escapes on render as well — this is the second layer of the
    same defense (defense in depth), so a poisoned cache never even reaches
    ``/api/plan``.
    """
    return (isinstance(mid, str) and bool(mid) and mid.isprintable()
            and not (_UNSAFE_ID_CHARS & frozenset(mid)))


def _non_negative(*values) -> bool:
    """O1b: rates must be >= 0 — a negative rate would yield a cost < 0, which
    no consumer predicate (``cost_usd = 0`` / ``> 0``) accounts for."""
    return all(v >= 0 for v in values)


def transform_models_dev(catalog) -> dict:
    """models.dev catalog -> {model_id: five-key rate row}. Never raises (AC-C4).

    Providers are walked in sorted order; on id collisions the first provider
    wins (deterministic). cache_write maps to BOTH cache_create_5m and
    cache_create_1h (bundled convention); missing/non-numeric cache rates
    default to 0.0. Entries without finite numeric input+output, entries with a
    NEGATIVE rate (O1b) and entries whose id fails the ``_safe_model_id``
    allowlist (K1b) are skipped. Refreshed rows carry no ``estimated``/``tier``
    key — the catalog is authoritative and ``cost_for`` defaults ``estimated``
    to False.
    """
    out: dict = {}
    if not isinstance(catalog, dict):
        return out
    for provider in sorted(catalog):
        prov = catalog[provider]
        if not isinstance(prov, dict):
            continue
        # Dual-shape walk (defensive): prefer a nested "models" dict when
        # present, else walk the provider dict itself. No-op on the expected
        # shape; never a raise when the live shape drifts (EC-13).
        nested = prov.get("models")
        models = nested if isinstance(nested, dict) else prov
        # key=str keeps the never-raises contract for hand-built dicts with
        # non-str keys (JSON catalogs always have str keys); such ids are then
        # rejected by _safe_model_id below rather than reaching the output.
        for mid in sorted(models, key=str):
            if not _safe_model_id(mid):
                continue  # K1b: ids are rendered into HTML by the web routes
            entry = models[mid]
            if not isinstance(entry, dict):
                continue
            cost = entry.get("cost")
            if not isinstance(cost, dict):
                continue
            inp, outp = cost.get("input"), cost.get("output")
            if not (_is_num(inp) and _is_num(outp)):
                continue
            cache_read = cost.get("cache_read")
            cache_write = cost.get("cache_write")
            cr = cache_read if _is_num(cache_read) else 0.0
            cw = cache_write if _is_num(cache_write) else 0.0
            if not _non_negative(inp, outp, cr, cw):
                continue  # O1b: negative rate -> whole entry skipped
            if mid in out:
                continue  # first provider in sorted order wins (AC-C4)
            out[mid] = {
                "input": inp,
                "output": outp,
                "cache_read": cr,
                "cache_create_5m": cw,
                "cache_create_1h": cw,
            }
    return out


def _fetch_catalog(url: str, timeout: int = FETCH_TIMEOUT_SECONDS):
    """Fetch + parse the catalog. Scheme gate FIRST: non http/https/file
    raises ValueError before any socket is opened (AC-C1)."""
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported pricing URL scheme: {scheme or url!r}")
    # models.dev's bot filter answers 403 to urllib's default UA
    # (Python-urllib/x.y); a custom UA gets 200. urlopen accepts a Request
    # for any scheme the opener supports, so file:// URLs keep working —
    # the headers are simply unused there (no HTTP request is made).
    request = urllib.request.Request(
        url, headers={"User-Agent": "token-dashboard/0.1.0",
                      "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # Cap the read at 10 MB: a pathological source must not balloon memory
        # (the models.dev catalog is ~1-2 MB); a truncated read raises
        # JSONDecodeError -> the existing fail-open path (cache kept).
        return json.loads(response.read(10 * 1024 * 1024))


def read_pricing_cache(cache_path: Optional[Union[str, Path]]) -> Optional[dict]:
    """Return {"fetched_at", "source_url", "models"} after top-level
    validation; None on missing/corrupt/None path (EC-14)."""
    if cache_path is None:
        return None
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not _is_num(data.get("fetched_at")):
        return None
    if not isinstance(data.get("models"), dict):
        return None
    return {"fetched_at": data["fetched_at"],
            "source_url": data.get("source_url"),
            "models": data["models"]}


def _write_cache_atomic(cache_path: Union[str, Path], payload: dict) -> None:
    """Temp file in the cache's parent dir -> close -> os.replace (AC-C5:
    atomic; no partial file left behind on failure)."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=str(path.parent), mode="w",
                                      suffix=".tmp", delete=False, encoding="utf-8")
    try:
        with tmp:
            json.dump(payload, tmp)
        os.replace(tmp.name, path)
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def refresh_pricing(url: str, cache_path: Union[str, Path],
                    now: Optional[float] = None) -> bool:
    """Fetch -> transform -> atomic cache write. True on success.

    Any fetch/parse failure or a 0-model transform (EC-13) prints EXACTLY one
    stderr line and returns False, leaving any existing cache untouched
    (EC-12) — fail-open, no exception escapes."""
    try:
        catalog = _fetch_catalog(url)
        models = transform_models_dev(catalog)
        if not models:
            print("pricing refresh skipped: catalog yielded 0 models",
                  file=sys.stderr)
            return False
        fetched_at = int(now if now is not None else time.time())
        _write_cache_atomic(cache_path, {
            "fetched_at": fetched_at,
            "source_url": url,
            "models": models,
        })
        return True
    except Exception as exc:  # fail-open (AC-C10): one line, then carry on
        print(f"pricing refresh skipped: {exc}", file=sys.stderr)
        return False


def maybe_refresh_pricing(url: str, cache_path: Union[str, Path],
                          force: bool = False,
                          now: Optional[float] = None) -> bool:
    """Refresh iff forced, cache missing/corrupt (EC-14), or age >= TTL /
    negative (EC-15 inclusive boundary + clock skew). Injectable clock."""
    ts = now if now is not None else time.time()
    cache = read_pricing_cache(cache_path)
    if not force and cache is not None:
        age = ts - cache["fetched_at"]
        if 0 <= age < PRICING_TTL_SECONDS:
            return False
    return refresh_pricing(url, cache_path, now=now)


def load_effective_pricing(bundled_path: Union[str, Path],
                           cache_path: Optional[Union[str, Path]]) -> dict:
    """Bundled ⊕ cache merge (AC-C6): cache wins per model id; bundled-only
    models survive; cache-only models are added (EC-22). tier_fallback and
    plans ALWAYS come from the bundle. Malformed cache rows are dropped
    individually (EC-14), as are rows whose id fails the ``_safe_model_id``
    allowlist (K1b) or carries a negative rate (O1b). Surviving rows are
    PROJECTED onto the five rate keys only — a cache can never smuggle an
    extra render field (``tier``/``estimated``) into ``/api/plan`` (K1).
    Missing/corrupt cache -> bundled unchanged. Never mutates the bundled dict."""
    bundled = load_pricing(bundled_path)
    cache = read_pricing_cache(cache_path)
    if cache is None:
        return bundled
    valid = {mid: {k: row[k] for k in _RATE_KEYS}
             for mid, row in cache["models"].items()
             if _safe_model_id(mid) and isinstance(row, dict)
             and all(_is_num(row.get(k)) for k in _RATE_KEYS)
             and _non_negative(*(row[k] for k in _RATE_KEYS))}
    effective = dict(bundled)
    effective["models"] = {**bundled.get("models", {}), **valid}
    return effective
