"""
cross-venue-funding-v1 — delta-neutral funding arbitrage between HL and Blofin.

THESIS
======
Both HL and Blofin run perp markets on the same coins (BTC, ETH, SOL, etc).
Each charges/pays funding rates independently. When the rates diverge:
  - long the side that pays MORE (gets funding from longs)
  - short the side that pays LESS (gets funding from shorts)

The two legs are delta-neutral (price changes cancel). The funding spread
becomes pure yield, capped only by execution costs and venue risk.

EXAMPLE (current live):
  JUP: HL pays 0.0013%/hr, Blofin pays -0.0042%/hr
       → LONG HL JUP (collect 0.0013%/hr from HL shorts)
       → SHORT Blofin JUP (collect 0.0042%/hr from Blofin longs)
       Net: 0.55bp/hr → ~47% APR delta-neutral

CONDITIONS TO OPEN:
  - Spread > MIN_SPREAD_BP (default 0.3bp/hr ≈ 26% APR threshold)
  - Both venues have liquidity (min_size respected)
  - Capital available on both venues

HOLDING:
  - Position size set so each leg is delta-neutral in USD terms
  - Hold until spread compresses below CLOSE_SPREAD_BP (default 0.1bp)
  - OR funding flips (one side stops paying)
  - OR price diverges between venues by >TRACKING_ERROR_PCT (rare but possible)

EXECUTION:
  - Open: market orders on both venues simultaneously (race condition risk)
  - Close: market orders on both venues simultaneously
  - Track total funding collected per position cycle
"""
from __future__ import annotations
import os
import time
import json
import threading
import urllib.request
from typing import Dict, Optional, Any

from . import blofin_client as bf
from . import persistence

# Fallback record_event if persistence module doesn't have it
if not hasattr(persistence, "record_event"):
    def _record_event(d):
        print(f"[cvf:event] {json.dumps(d, default=str)}", flush=True)
    persistence.record_event = _record_event

# ───── Config ────────────────────────────────────────────────────────────────
MIN_SPREAD_BP_PER_HR = float(os.environ.get("MIN_SPREAD_BP_PER_HR", "0.3"))
CLOSE_SPREAD_BP_PER_HR = float(os.environ.get("CLOSE_SPREAD_BP_PER_HR", "0.1"))
TRACKING_ERROR_PCT = float(os.environ.get("TRACKING_ERROR_PCT", "0.005"))
POSITION_NOTIONAL_USD = float(os.environ.get("POSITION_NOTIONAL_USD", "500"))
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
MIN_CAPITAL_BLOFIN = float(os.environ.get("MIN_CAPITAL_BLOFIN", "200"))
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

# Coins to scan
COINS = os.environ.get("CROSS_VENUE_COINS",
    "BTC,ETH,SOL,DOGE,XRP,BNB,AVAX,LINK,JUP,ATOM,HYPE,SUI,NEAR,INJ").split(",")


# ───── Helpers ───────────────────────────────────────────────────────────────

def _fetch_hl_funding() -> Dict[str, float]:
    """Fetch HL funding rates for all coins (per-hour rate)."""
    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        out = {}
        for i, u in enumerate(data[0].get("universe", [])):
            if i < len(data[1]):
                out[u.get("name", "")] = float(data[1][i].get("funding", 0))
        return out
    except Exception as e:
        print(f"[cvf] HL funding fetch failed: {e}", flush=True)
        return {}


def _fetch_hl_mark_price(coin: str) -> Optional[float]:
    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "allMids"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return float(data.get(coin, 0)) or None
    except Exception:
        return None


def scan_opportunities() -> list:
    """Parallel funding-spread scan across COINS.

    Returns list of (coin, spread_bp_per_hr, direction).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    hl_funding = _fetch_hl_funding()

    def _check_coin(coin):
        coin = coin.strip().upper()
        if not coin: return None
        hl_h = hl_funding.get(coin)
        if hl_h is None: return None
        bf_8h = bf.get_funding_rate(f"{coin}-USDT")
        if bf_8h is None: return None
        bf_h = bf_8h / 8
        spread_h = hl_h - bf_h
        spread_bp = spread_h * 10000
        if abs(spread_bp) < MIN_SPREAD_BP_PER_HR:
            return None
        direction = "long_hl_short_bf" if spread_bp > 0 else "short_hl_long_bf"
        return {
            "coin": coin,
            "hl_funding_pct_hr": hl_h * 100,
            "bf_funding_pct_hr": bf_h * 100,
            "spread_bp_hr": spread_bp,
            "abs_spread_bp_hr": abs(spread_bp),
            "annual_pct": abs(spread_bp) * 24 * 365 / 100,
            "direction": direction,
        }

    opportunities = []
    # 12 workers — Blofin rate limit is 500/min/IP, so 12 parallel × ~1s/call = safe
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in as_completed([ex.submit(_check_coin, c) for c in COINS]):
            result = r.result()
            if result: opportunities.append(result)

    opportunities.sort(key=lambda x: -x["abs_spread_bp_hr"])
    return opportunities


# ───── Position tracker ──────────────────────────────────────────────────────
from .github_state import GithubState

# GitHub-backed persistence for DRY_RUN paper positions (survives Render restarts)
_GH_STATE = GithubState(
    owner="Dapperscyphozoa", repo="multica",
    path=f"engine_state/cross-venue-funding-v1/positions.json",
)

_positions: Dict[str, Dict[str, Any]] = {}   # coin -> {opened_ts, direction, hl_size, bf_size, ...}
_positions_lock = threading.Lock()

# Persistence: dump positions to disk so they survive restarts.
# In DRY_RUN mode this preserves accrued spread/time. In live mode it's
# essential — restart without state = orphaned positions on both venues.
_state_dir = os.environ.get("STATE_DIR", "/tmp/cvf-state")
_state_file = os.path.join(_state_dir, "cvf_positions.json")


def _persist_positions():
    try:
        os.makedirs(_state_dir, exist_ok=True)
        with _positions_lock:
            snapshot = dict(_positions)
        with open(_state_file, "w") as f:
            json.dump(snapshot, f, default=str)
    except Exception as e:
        print(f"[cvf] persist error: {e}", flush=True)


def _load_positions():
    """Load _positions from GitHub (or /tmp fallback) on boot."""
    global _positions
    try:
        # Try GitHub first
        snapshot = _GH_STATE.load(default=None)
        if not snapshot:
            # Fallback to /tmp file
            local_path = os.path.join(STATE_DIR, "cvf_positions.json")
            if os.path.exists(local_path):
                with open(local_path) as f:
                    snapshot = json.load(f)
        if snapshot and isinstance(snapshot, dict):
            saved_positions = snapshot.get("positions", {})
            if isinstance(saved_positions, dict):
                _positions.update(saved_positions)
                print(f"[cvf] restored {len(saved_positions)} positions from GitHub state", flush=True)
    except Exception as e:
        print(f"[cvf] load err: {e}", flush=True)




_load_positions()


def can_open_more() -> bool:
    with _positions_lock:
        return len(_positions) < MAX_OPEN_POSITIONS


def open_paired_position(opp: dict) -> Optional[dict]:
    """Open delta-neutral paired position. Returns position record or None."""
    coin = opp["coin"]
    direction = opp["direction"]

    mark_px = _fetch_hl_mark_price(coin)
    if not mark_px:
        return None

    # Size = POSITION_NOTIONAL_USD / mark_price (in coin units)
    size_coin = POSITION_NOTIONAL_USD / mark_px

    if DRY_RUN:
        print(f"[cvf:DRY_RUN] would open {direction} on {coin}: "
              f"size={size_coin:.4f} @ ${mark_px:.2f} "
              f"(spread {opp['spread_bp_hr']:+.2f}bp/hr, "
              f"~{opp['annual_pct']:.0f}% APR)", flush=True)
        pos = {
            "coin": coin,
            "direction": direction,
            "opened_ts": int(time.time() * 1000),
            "entry_px": mark_px,
            "size_coin": size_coin,
            "entry_spread_bp_hr": opp["spread_bp_hr"],
            "hl_filled": True,    # simulated
            "bf_filled": True,
            "dry_run": True,
        }
        with _positions_lock:
            _positions[coin] = pos
        _persist_positions()
        return pos

    # ── LIVE EXECUTION ──
    # Direction:
    #   long_hl_short_bf: BUY HL perp + SELL Blofin perp
    #   short_hl_long_bf: SELL HL perp + BUY Blofin perp
    bf_health = bf.health_check()
    if not bf_health.get("auth_ok"):
        print(f"[cvf] LIVE blocked — Blofin auth not ready: {bf_health.get('error')}", flush=True)
        return None

    from . import hl_exchange
    try:
        hl = hl_exchange.HLExchange()
        if not getattr(hl, "armed", False):
            print(f"[cvf] LIVE blocked — HL exchange not armed", flush=True)
            return None
    except Exception as e:
        print(f"[cvf] LIVE blocked — HL init failed: {e}", flush=True)
        return None

    # Compute HL contract size (in coin units, rounded to sz_decimals)
    hl_sz_decimals = hl.get_sz_decimals(coin)
    hl_size = round(size_coin, hl_sz_decimals)
    if hl_size <= 0:
        print(f"[cvf] LIVE blocked — HL size rounded to 0", flush=True)
        return None

    # Blofin contract size: convert coin-units to contract count via instrument spec
    bf_inst = f"{coin}-USDT"
    bf_size_str = bf.coin_size_to_contracts(coin, hl_size)
    if bf_size_str is None:
        print(f"[cvf] Blofin size conversion failed for {coin} — instrument unsupported?", flush=True)
        return None

    hl_is_buy = direction == "long_hl_short_bf"
    bf_side = "sell" if hl_is_buy else "buy"

    internal_cloid = f"cvf_{coin}_{int(time.time())}"

    # Step 1: Place HL market order
    print(f"[cvf] LIVE opening {direction} on {coin}: HL {'BUY' if hl_is_buy else 'SELL'} {hl_size} @ market", flush=True)
    hl_result = hl.place_market_order(coin, hl_is_buy, hl_size, internal_cloid)
    if hl_result.get("status") != "ok":
        print(f"[cvf] HL leg failed: {hl_result}", flush=True)
        return None
    hl_fill_px = hl_result.get("avg_px") or mark_px

    # Step 2: Place Blofin market order
    print(f"[cvf] LIVE opening Blofin {bf_side} {bf_size_str} on {bf_inst}", flush=True)
    bf_result = bf.place_order(
        inst_id=bf_inst,
        side=bf_side,
        size=bf_size_str,
        order_type="market",
        position_side="net",
        client_order_id=internal_cloid,
        leverage="1",
    )

    # Step 3: If Blofin failed, immediately close HL leg (delta-neutral broken)
    if not bf_result or bf_result.get("code") not in ("0", 0):
        print(f"[cvf] Blofin leg failed: {bf_result} — reversing HL leg", flush=True)
        try:
            hl.market_close_position(coin, internal_cloid)
        except Exception as e:
            print(f"[cvf] CRITICAL: HL reverse failed: {e} — manual intervention required", flush=True)
        return None

    bf_order_id = (bf_result.get("data") or [{}])[0].get("orderId")
    print(f"[cvf] LIVE both legs filled: HL={internal_cloid} BF={bf_order_id}", flush=True)

    pos = {
        "coin": coin,
        "direction": direction,
        "opened_ts": int(time.time() * 1000),
        "entry_px": hl_fill_px,
        "size_coin": hl_size,
        "entry_spread_bp_hr": opp["spread_bp_hr"],
        "hl_internal_cloid": internal_cloid,
        "hl_filled": True,
        "bf_order_id": bf_order_id,
        "bf_filled": True,
        "bf_inst_id": bf_inst,
        "live": True,
    }
    with _positions_lock:
        _positions[coin] = pos
    return pos


def close_paired_position(coin: str) -> Optional[dict]:
    with _positions_lock:
        pos = _positions.pop(coin, None)
    if not pos:
        return None
    _persist_positions()

    held_hours = (int(time.time() * 1000) - pos["opened_ts"]) / 3600_000
    accrued_bp = pos["entry_spread_bp_hr"] * held_hours
    accrued_usd = (accrued_bp / 10000) * POSITION_NOTIONAL_USD
    pos["closed_ts"] = int(time.time() * 1000)
    pos["held_hours"] = held_hours
    pos["accrued_bp"] = accrued_bp
    pos["accrued_usd"] = accrued_usd

    if pos.get("dry_run"):
        print(f"[cvf:DRY_RUN] closed {pos['direction']} on {coin} "
              f"after {held_hours:.1f}h, accrued ~{accrued_bp:.2f}bp "
              f"(~${accrued_usd:.2f})", flush=True)
        return pos

    if pos.get("live"):
        # Close HL leg first
        from . import hl_exchange
        coin = pos["coin"]
        hl_cloid = pos.get("hl_internal_cloid")
        bf_inst = pos.get("bf_inst_id")
        try:
            hl = hl_exchange.HLExchange()
            hl_close = hl.market_close_position(coin, hl_cloid)
            print(f"[cvf:LIVE] HL close {coin}: {hl_close.get('status')}", flush=True)
        except Exception as e:
            print(f"[cvf:LIVE] HL close failed: {e}", flush=True)

        # Close Blofin leg
        try:
            bf_close = bf.close_position(bf_inst, "net")
            print(f"[cvf:LIVE] Blofin close {bf_inst}: {bf_close}", flush=True)
        except Exception as e:
            print(f"[cvf:LIVE] Blofin close failed: {e}", flush=True)

        print(f"[cvf:LIVE] {pos['direction']} closed on {coin} "
              f"after {held_hours:.1f}h, accrued ~{accrued_bp:.2f}bp "
              f"(~${accrued_usd:.2f})", flush=True)
        return pos

    return pos


def check_closing_conditions(coin: str, current_spread_bp_hr: float) -> Optional[str]:
    """Returns close reason if should close, else None."""
    with _positions_lock:
        pos = _positions.get(coin)
    if not pos:
        return None
    # Spread compressed
    if abs(current_spread_bp_hr) < CLOSE_SPREAD_BP_PER_HR:
        return "spread_compressed"
    # Spread flipped sign (now paying instead of collecting)
    if (pos["entry_spread_bp_hr"] > 0) != (current_spread_bp_hr > 0):
        return "spread_flipped"
    return None


# ───── Main loop ─────────────────────────────────────────────────────────────

def tick():
    """One scan cycle."""
    opps = scan_opportunities()
    print(f"[cvf] scanned {len(COINS)} coins, found {len(opps)} opportunities "
          f"(threshold {MIN_SPREAD_BP_PER_HR}bp/hr)", flush=True)

    # Check existing positions for close conditions
    with _positions_lock:
        open_coins = list(_positions.keys())
    for coin in open_coins:
        # Find current spread for this coin
        match = next((o for o in opps if o["coin"] == coin), None)
        if match:
            reason = check_closing_conditions(coin, match["spread_bp_hr"])
        else:
            # No longer above MIN_SPREAD threshold → spread compressed
            reason = "spread_below_min"
        if reason:
            closed = close_paired_position(coin)
            if closed:
                try:
                    persistence.record_event({
                        "event": "cross_venue_close",
                        "coin": coin,
                        "reason": reason,
                        **closed,
                    })
                except Exception as e:
                    print(f"[cvf] persist close failed: {e}", flush=True)

    # Open new positions for top opportunities (within MAX_OPEN_POSITIONS)
    for opp in opps[:MAX_OPEN_POSITIONS]:
        if not can_open_more(): break
        with _positions_lock:
            if opp["coin"] in _positions: continue
        result = open_paired_position(opp)
        if result:
            try:
                persistence.record_event({
                    "event": "cross_venue_open",
                    **result,
                    "entry_spread_bp_hr": opp["spread_bp_hr"],
                    "annual_pct": opp["annual_pct"],
                })
            except Exception as e:
                print(f"[cvf] persist open failed: {e}", flush=True)


def run_forever():
    print(f"[cvf] starting. MIN_SPREAD={MIN_SPREAD_BP_PER_HR}bp/hr "
          f"NOTIONAL=${POSITION_NOTIONAL_USD} MAX_OPEN={MAX_OPEN_POSITIONS} "
          f"DRY_RUN={DRY_RUN}", flush=True)
    bf_health = bf.health_check()
    print(f"[cvf] Blofin health: {bf_health}", flush=True)
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[cvf] tick error: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SEC)


def get_state() -> dict:
    """For HTTP /state endpoint."""
    with _positions_lock:
        pos_snapshot = dict(_positions)
    return {
        "config": {
            "min_spread_bp_hr": MIN_SPREAD_BP_PER_HR,
            "close_spread_bp_hr": CLOSE_SPREAD_BP_PER_HR,
            "notional_usd": POSITION_NOTIONAL_USD,
            "max_open": MAX_OPEN_POSITIONS,
            "scan_interval_sec": SCAN_INTERVAL_SEC,
            "coins": COINS,
            "dry_run": DRY_RUN,
        },
        "n_open_positions": len(pos_snapshot),
        "positions": pos_snapshot,
        "blofin_health": bf.health_check(),
    }



# ─── Cross-venue price divergence detector ──────────────────────────────────

def scan_price_divergence() -> list:
    """Parallel price-divergence scan between HL and Blofin."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    MIN_PCT = float(os.environ.get("PRICE_DIVERGENCE_MIN_PCT", "0.002"))

    # Fetch all HL mids in one call (much faster than per-coin)
    try:
        import json as _j, urllib.request as _ur
        req = _ur.Request("https://api.hyperliquid.xyz/info",
            data=_j.dumps({"type": "allMids"}).encode(),
            headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=10) as r:
            hl_mids = {k: float(v) for k, v in _j.loads(r.read()).items()}
    except Exception:
        hl_mids = {}

    def _check_coin(coin):
        coin = coin.strip().upper()
        if not coin: return None
        hl_px = hl_mids.get(coin)
        if hl_px is None: return None
        bf_px = bf.get_mark_price(f"{coin}-USDT")
        if bf_px is None or hl_px <= 0 or bf_px <= 0: return None
        diff_pct = (hl_px - bf_px) / ((hl_px + bf_px) / 2)
        if abs(diff_pct) < MIN_PCT: return None
        action = "short_hl_long_bf" if diff_pct > 0 else "long_hl_short_bf"
        return {
            "coin": coin, "hl_price": hl_px, "bf_price": bf_px,
            "diff_pct": diff_pct * 100, "diff_bp": diff_pct * 10000,
            "action": action,
        }

    divergences = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in as_completed([ex.submit(_check_coin, c) for c in COINS]):
            result = r.result()
            if result: divergences.append(result)

    divergences.sort(key=lambda x: -abs(x["diff_pct"]))
    return divergences



# ─── HL oracle/mark premium scanner ────────────────────────────────────────

def scan_hl_premium() -> list:
    """Detect significant premium between HL mark price and oracle.

    HL exposes both:
      - oraclePx: TWAP from external spot venues
      - markPx: HL perp orderbook-derived
      - premium: (mark - oracle) / oracle

    When |premium| > threshold, mark typically converges to oracle quickly
    (seconds to minutes). The arb is:
      premium > 0 (mark > oracle): SHORT mark, wait for catch down
      premium < 0 (mark < oracle): LONG mark, wait for catch up
    """
    MIN_PREMIUM_BP = float(os.environ.get("HL_PREMIUM_MIN_BP", "5"))   # 5bp

    try:
        req = urllib.request.Request("https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
    except Exception:
        return []

    universe = data[0].get("universe", [])
    ctxs = data[1]
    opportunities = []

    for i, u in enumerate(universe):
        if i >= len(ctxs): continue
        coin = u.get("name", "")
        ctx = ctxs[i]
        try:
            oracle = float(ctx.get("oraclePx", 0))
            mark = float(ctx.get("markPx", 0))
            premium = float(ctx.get("premium", 0))
        except Exception:
            continue

        if oracle <= 0: continue
        premium_bp = premium * 10000   # premium in bp
        if abs(premium_bp) < MIN_PREMIUM_BP: continue

        action = "short" if premium > 0 else "long"
        opportunities.append({
            "coin": coin,
            "oracle_px": oracle,
            "mark_px": mark,
            "premium_bp": premium_bp,
            "abs_premium_bp": abs(premium_bp),
            "action": action,
            "expected_target_px": oracle,
        })

    opportunities.sort(key=lambda x: -x["abs_premium_bp"])
    return opportunities



def recover_positions_from_exchanges() -> int:
    """On boot (live mode only), query HL + Blofin for existing positions
    and rebuild the _positions dict so the engine survives Render restarts.

    Returns count of recovered positions."""
    if DRY_RUN:
        return 0
    bf_health = bf.health_check()
    if not bf_health.get("auth_ok"):
        print("[cvf:recover] Blofin auth not ready — skipping recovery", flush=True)
        return 0

    try:
        from . import hl_exchange
        hl = hl_exchange.HLExchange()
    except Exception as e:
        print(f"[cvf:recover] HL init failed: {e}", flush=True)
        return 0

    # Get HL positions
    try:
        hl_state = hl.get_account_state()
        hl_positions = {p["coin"]: p for p in hl_state.get("assetPositions", [])
                        if abs(float(p.get("szi", 0))) > 0}
    except Exception as e:
        print(f"[cvf:recover] HL fetch failed: {e}", flush=True)
        hl_positions = {}

    # Get Blofin positions
    try:
        bf_positions_raw = bf.get_positions("SWAP") or []
        bf_positions = {p["instId"].replace("-USDT", ""): p
                        for p in bf_positions_raw
                        if abs(float(p.get("positions", 0))) > 0}
    except Exception as e:
        print(f"[cvf:recover] Blofin fetch failed: {e}", flush=True)
        bf_positions = {}

    # Cross-reference: any coin appearing in BOTH = a paired position we should track
    recovered = 0
    with _positions_lock:
        for coin in set(hl_positions.keys()) & set(bf_positions.keys()):
            hl_pos = hl_positions[coin]
            bf_pos = bf_positions[coin]
            hl_size = abs(float(hl_pos.get("szi", 0)))
            hl_is_long = float(hl_pos.get("szi", 0)) > 0
            bf_is_long = float(bf_pos.get("positions", 0)) > 0
            # Check delta-neutral structure
            if hl_is_long == bf_is_long:
                print(f"[cvf:recover] {coin}: legs same direction (not cvf), skip", flush=True)
                continue
            direction = "long_hl_short_bf" if hl_is_long else "short_hl_long_bf"
            entry_px = float(hl_pos.get("entryPx", 0)) or _fetch_hl_mark_price(coin) or 0
            _positions[coin] = {
                "coin": coin,
                "direction": direction,
                "opened_ts": int(time.time() * 1000),   # approximate; can't recover exact
                "entry_px": entry_px,
                "size_coin": hl_size,
                "entry_spread_bp_hr": 0,   # unknown; will be updated on close
                "hl_filled": True,
                "bf_filled": True,
                "bf_inst_id": f"{coin}-USDT",
                "live": True,
                "recovered": True,
            }
            recovered += 1
            print(f"[cvf:recover] recovered {direction} on {coin}: size={hl_size}", flush=True)

    return recovered
