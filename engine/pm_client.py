"""
pm_client — canonical Python client for the portfolio-manager service.

Three calls every engine should use:
  1. get_size_fraction(engine)   → call before computing order size; multiply
                                    your default notional by this. Returns 0.0
                                    for paper engines (no live trades). Cached
                                    per CACHE_TTL.
  2. check_pretrade(...)         → call before sending an order. Returns
                                    {allow: bool, reason: str, capital_remaining}.
                                    FAIL-CLOSED if PM unreachable (allow=False).
  3. get_equity()                → optional helper for sizing math.

Design:
  - All functions tolerate PM downtime; safe defaults applied.
  - All functions take a timeout; never block more than PM_TIMEOUT_SEC.
  - check_pretrade always passes is_live correctly so paper engines testing
    the gate against PM don't get auto-denied with paper_stage_no_live.
  - Auth: if PM_AUTH_TOKEN is set in env, X-PM-Auth header is included.

Required env vars:
  PM_URL              — base URL, e.g. https://portfolio-manager-7df2.onrender.com
  ENGINE_NAME         — must match a key in PM's STRATEGY_REGISTRY
  PM_CHECK_ENABLED    — "1" to actually call /check (otherwise default-allow)
  PM_AUTH_TOKEN       — optional shared secret if PM has auth enabled
  PM_TIMEOUT_SEC      — optional, default 5
"""
from __future__ import annotations
import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional


PM_URL          = os.environ.get("PM_URL", "https://portfolio-manager-7df2.onrender.com").rstrip("/")
ENGINE_NAME     = os.environ.get("ENGINE_NAME", "")
PM_CHECK_ENABLED = os.environ.get("PM_CHECK_ENABLED", "0") == "1"
PM_AUTH_TOKEN   = os.environ.get("PM_AUTH_TOKEN", "").strip()
PM_TIMEOUT_SEC  = int(os.environ.get("PM_TIMEOUT_SEC", "5"))

# Cache size_fraction so we don't hammer PM
SIZE_CACHE_TTL_SEC = int(os.environ.get("PM_SIZE_CACHE_TTL", "60"))
_size_cache: dict[str, tuple[float, float]] = {}  # engine -> (size_frac, expiry_ts)


def _request(method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
    url = f"{PM_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if PM_AUTH_TOKEN:
        headers["X-PM-Auth"] = PM_AUTH_TOKEN
    data = json.dumps(body).encode() if body is not None else None
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=PM_TIMEOUT_SEC) as r:
            payload = r.read().decode()
            if r.status == 429:
                # rate limited — caller should back off
                return {"_rate_limited": True, **(json.loads(payload) if payload else {})}
            if 200 <= r.status < 300:
                return json.loads(payload) if payload else {}
            return {"_http_error": r.status, "_body": payload}
    except urllib.error.HTTPError as e:
        try:
            return {"_http_error": e.code, "_body": e.read().decode()}
        except Exception:
            return {"_http_error": e.code}
    except Exception as e:
        return {"_unreachable": True, "_error": str(e)}


def get_equity() -> Optional[float]:
    r = _request("GET", "/equity")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    av = r.get("account_value")
    return float(av) if av is not None else None


def get_regime(coin: str) -> Optional[dict]:
    r = _request("GET", f"/regime/{coin}")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    return r


def fetch_macro_state() -> Optional[dict]:
    """Cross-asset macro regime sensor.

    Returns macro state with per-(coin_class, direction) confluence multipliers.
    Used by trader.attempt_trade() to scale cell_size_mult by macro alignment.

    Returns None on PM unreachable — fail-open (proceed with 1.0× multiplier).
    """
    r = _request("GET", "/macro_state")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    return r


def fetch_confluence(coin: str, direction: str, window_min: int = 60) -> Optional[dict]:
    """Cross-engine ensemble voting.

    Returns how many engines independently fired the same (coin, direction)
    within window_min minutes. Higher count = stronger structural signal.

    Used by trader.attempt_trade() to scale cell_size_mult — engines that
    fire alone get baseline size; engines firing in chorus get sized up.
    """
    r = _request("GET", f"/confluence/{coin}/{direction}?window_min={window_min}")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    return r


def fetch_macro_state() -> Optional[dict]:
    """Cross-asset macro regime + per-direction confluence multipliers.

    Returns {regime_summary, confluence: {long_alt, short_alt, long_btc, short_btc}, ...}
    None on PM unreachable — caller fails open (proceeds with multiplier 1.0).
    """
    r = _request("GET", "/macro_state")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    return r


def fetch_confluence(coin: str, direction: str, window_min: int = 60) -> Optional[dict]:
    """Cross-engine ensemble: how many other engines fired (coin, direction)
    in the last window_min minutes?

    Returns {n_engines_fired, engines: [...], confluence_mult: 1.0|1.3|1.6|2.0}
    """
    r = _request("GET", f"/confluence/{coin}/{direction}?window_min={window_min}")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    return r


def fetch_net_position(coin: str) -> Optional[dict]:
    """Cross-engine portfolio netting helper.

    Returns the aggregate exposure to `coin` across all engines, e.g.:
      {"coin": "BTC", "net_size": 0.05, "net_notional": 5000,
       "net_direction": "long", "n_positions": 2,
       "by_engine": {"wyckoff-v1": {...}, "funding-div-v1": {...}}}

    Returns None on PM unreachable — caller should fail-open (proceed with trade).
    """
    r = _request("GET", f"/net_position/{coin}")
    if not r or r.get("_unreachable") or r.get("_http_error"):
        return None
    return r


def get_size_fraction(engine: str = ENGINE_NAME) -> float:
    """
    Returns the size_fraction this engine should apply to its default notional.
    
    paper   → 0.0  (don't place live orders)
    canary  → 0.05
    small   → 0.25
    full    → 1.00
    demoted → 0.0
    
    Cached for SIZE_CACHE_TTL_SEC. Returns 0.0 if PM unreachable (fail-closed).
    """
    now = time.time()
    cached = _size_cache.get(engine)
    if cached and cached[1] > now:
        return cached[0]
    
    r = _request("GET", f"/size/{engine}")
    if not r or r.get("_unreachable"):
        # PM down → fail-closed: don't trade
        return 0.0
    if r.get("_http_error") == 404:
        # Engine not registered — refuse
        return 0.0
    if r.get("halted"):
        return 0.0
    sf = float(r.get("size_fraction", 0.0))
    _size_cache[engine] = (sf, now + SIZE_CACHE_TTL_SEC)
    return sf


def check_pretrade(coin: str, side: str, notional: float,
                    sl_distance_pct: Optional[float] = None,
                    is_live: bool = True,
                    engine: str = ENGINE_NAME) -> dict:
    """
    Pre-trade gate. Pass is_live=False for paper-mode trades so PM doesn't
    auto-deny with paper_stage_no_live (the gate's other rules still apply).
    
    Fail-closed: PM unreachable → allow=False reason='pm_unreachable'.
    Rate-limited (429) → allow=False reason='rate_limited' with retry_after.
    """
    if not PM_CHECK_ENABLED:
        return {"allow": True, "reason": "pm_check_disabled",
                "engine": engine, "coin": coin}
    
    if not engine:
        return {"allow": False, "reason": "ENGINE_NAME_unset"}
    
    if notional <= 0:
        return {"allow": False, "reason": "notional_must_be_positive"}
    
    body = {
        "engine": engine,
        "coin": coin,
        "side": side,
        "notional": float(notional),
        "is_live": is_live,
    }
    if sl_distance_pct is not None:
        body["sl_distance_pct"] = sl_distance_pct
    
    r = _request("POST", "/check", body=body)
    
    if r is None:
        return {"allow": False, "reason": "pm_unreachable"}
    
    if r.get("_unreachable"):
        return {"allow": False, "reason": "pm_unreachable",
                "error": r.get("_error")}
    
    if r.get("_rate_limited"):
        return {"allow": False, "reason": "rate_limited",
                "retry_after_seconds": r.get("retry_after_seconds")}
    
    if r.get("_http_error"):
        return {"allow": False, "reason": f"pm_error_{r['_http_error']}",
                "body": r.get("_body")}
    
    return r


def is_pm_live() -> bool:
    """Quick health check; useful for boot probes."""
    r = _request("GET", "/health")
    return bool(r and r.get("status") == "ok")


def register_cloid(cloid: str, coin: Optional[str] = None,
                    engine: str = ENGINE_NAME) -> dict:
    """
    Register a cloid → engine mapping with PM BEFORE placing the order.
    Solves attribution for engines whose cloids are hashed at the exchange.
    
    Best-effort: PM unreachable → returns {ok: False, reason: 'pm_unreachable'}.
    Caller should NOT block trade placement on this; just log failures.
    """
    if not engine:
        return {"ok": False, "reason": "ENGINE_NAME_unset"}
    if not cloid:
        return {"ok": False, "reason": "cloid_empty"}
    body = {"cloid": str(cloid), "engine": str(engine)}
    if coin:
        body["coin"] = str(coin)
    r = _request("POST", "/register_cloid", body=body)
    if not r:
        return {"ok": False, "reason": "pm_unreachable"}
    if r.get("_unreachable"):
        return {"ok": False, "reason": "pm_unreachable", "error": r.get("_error")}
    if r.get("_http_error"):
        return {"ok": False, "reason": f"pm_error_{r['_http_error']}"}
    return r
