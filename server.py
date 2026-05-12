"""
engine-template server.

- Scheduler: every SCAN_INTERVAL_SEC, scan ACTIVE_UNIVERSE for squeeze fires.
- Position manager: every POSITION_CHECK_INTERVAL_SEC, check open trades for SL/TP/time-stop.
- HTTP API: state, signals, trades, halt control.
"""
from __future__ import annotations
import json
import os
import time
import traceback
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from engine.config import (
    HTTP_PORT, ACTIVE_UNIVERSE, BLOCKED_UNIVERSE, PRIMARY_UNIVERSE,
    SCAN_INTERVAL_SEC, POSITION_CHECK_INTERVAL_SEC, STRATEGY_PARAMS,
    HALT_STATE, ENGINE_NAME, ENGINE_VERSION, LIVE_TRADING, PAPER_MODE, DRY_RUN,
    PM_URL, RISK_PCT_PER_TRADE, LEVERAGE, MAX_NOTIONAL_PER_TRADE, MAX_OPEN_POSITIONS,
    HL_WALLET, HL_PRIVATE_KEY, USE_TESTNET,
    LIVE_MIN_ACCOUNT_VALUE, LIVE_SIZE_SCALE, LIVE_EXIT_SLIPPAGE, LIVE_MAKER_ONLY_ENTRIES,
)
from engine import persistence, hl_data, signal_detector, trader, pm_client


# ===== Halt-endpoint security =====
# HALT_TOKEN: required in X-Halt-Token header on POST /halt and /resume.
# DASHBOARD_ORIGIN: locks CORS Allow-Origin to a single origin.
HALT_TOKEN = os.environ.get("HALT_TOKEN", "").strip()
DASHBOARD_ORIGIN = os.environ.get("DASHBOARD_ORIGIN", "*").strip() or "*"
ALLOWED_ORIGINS = {DASHBOARD_ORIGIN} if DASHBOARD_ORIGIN != "*" else None


def _origin_for_request(handler) -> str:
    if ALLOWED_ORIGINS is None:
        return "*"
    req_origin = handler.headers.get("Origin", "")
    if req_origin in ALLOWED_ORIGINS:
        return req_origin
    return ""


# ===== Background workers =====
def scan_loop():
    """Cross-venue funding arb loop. Replaces standard signal_detector scan."""
    from engine import cross_venue_engine as cvf
    print(f"[cvf] cross-venue scan loop starting", flush=True)
    time.sleep(15)
    cvf_interval = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
    while True:
        try:
            if HALT_STATE.get("active"):
                print(f"[cvf] HALT active ({HALT_STATE.get('reason')}) — skipping", flush=True)
            else:
                cvf.tick()
        except Exception as e:
            print(f"[cvf] tick error: {e}\n{traceback.format_exc()}", flush=True)
        time.sleep(cvf_interval)


def _scan_once():
    n_signals = 0
    n_trades = 0
    n_skipped = 0

    for coin in ACTIVE_UNIVERSE:
        try:
            df = hl_data.fetch_candles(coin, interval="1h",
                                        n_bars=STRATEGY_PARAMS["candles_history"])
            if df is None or len(df) < 130:
                continue

            sig = signal_detector.evaluate_latest_bar(df)
            if sig is None:
                continue

            n_signals += 1
            print(f"[scan] SIGNAL fired {coin}: side={sig.get('trade_side')} "
                  f"ref={sig.get('ref_price', 0.0):.6f} "
                  f"sl={sig.get('sl_px', 0.0):.6f} tp={sig.get('tp_px', 0.0):.6f} "
                  f"atr={sig.get('atr', 0.0):.6f} "
                  f"reason={sig.get('fire_reason', 'n/a')}", flush=True)

            result = trader.attempt_trade(coin, sig)
            if result.get("status") == "opened":
                n_trades += 1
                print(f"[scan] OPENED {result['cloid']} {coin} {result['side']} "
                      f"size={result['size']:.4f} ntl=${result['notional']:.2f} "
                      f"sl={result['sl_px']:.4f} tp={result['tp_px']:.4f}", flush=True)
            elif result.get("status") == "dry_run_logged":
                # Logged signal, didn't execute
                persistence.insert_signal(coin, sig, traded=False, skip_reason="dry_run")
            else:
                # Track skip reason
                persistence.insert_signal(coin, sig, traded=False,
                                           skip_reason=result.get("reason"))
                n_skipped += 1
                print(f"[scan] skipped {coin}: {result.get('reason')}", flush=True)

        except Exception as e:
            print(f"[scan] error for {coin}: {e}", flush=True)

    if n_signals > 0:
        print(f"[scan] cycle done: {n_signals} signals, {n_trades} trades, {n_skipped} skipped", flush=True)


def position_loop():
    """Every POSITION_CHECK_INTERVAL_SEC: check open trades for exits.

    Lookahead-safe paper resolution: for each coin with open trades, fetch
    enough recent bars to cover the oldest trade's lifetime + buffer, then
    pass the bar list to manage_open_trades. The resolver scans only bars
    whose start_ms >= ts_open, eliminating the bug where a fresh trade
    inherits the current bar's pre-entry high/low and instantly TPs.
    """
    print(f"[positions] loop starting (interval={POSITION_CHECK_INTERVAL_SEC}s)", flush=True)
    time.sleep(20)

    while True:
        try:
            open_trades = persistence.get_open_trades()
            if not open_trades:
                time.sleep(POSITION_CHECK_INTERVAL_SEC)
                continue

            # Per-coin oldest trade ts_open → bars to fetch
            now_ms = int(time.time() * 1000)
            coin_oldest = {}
            for t in open_trades:
                c = t["coin"]; ts = t.get("ts_open", now_ms)
                coin_oldest[c] = min(coin_oldest.get(c, ts), ts)

            price_cache = {}
            for coin, oldest_ts in coin_oldest.items():
                # 1h bars; cover (now - oldest) hours + 2 buffer, capped at 48
                age_h = max(1, int((now_ms - oldest_ts) / 3_600_000) + 2)
                n_bars = min(48, age_h + 1)
                df = hl_data.fetch_candles(coin, interval="1h", n_bars=n_bars)
                if df is None or len(df) == 0:
                    continue
                last_bar = df.iloc[-1]
                bars = []
                for ts_val, row in df.iterrows():
                    # ts is the DataFrame index — pandas Timestamp (UTC).
                    # Convert to int ms.
                    try:
                        bar_ts = int(ts_val.timestamp() * 1000)
                    except Exception:
                        bar_ts = None
                    bars.append({
                        "t": bar_ts,
                        "o": float(row["open"]) if "open" in df.columns else None,
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                    })
                price_cache[coin] = (
                    float(last_bar["close"]),
                    float(last_bar["high"]),
                    float(last_bar["low"]),
                    bars,
                )

            def get_price(coin):
                return price_cache.get(coin)

            trader.manage_open_trades(get_price)
        except Exception as e:
            print(f"[positions] error: {e}\n{traceback.format_exc()}", flush=True)
        time.sleep(POSITION_CHECK_INTERVAL_SEC)


def reconcile_loop():
    """Live-mode only: every 30s, advance pending live orders to filled if HL says so."""
    print("[reconcile] loop starting (interval=30s, live-mode only)", flush=True)
    time.sleep(25)
    while True:
        try:
            if LIVE_TRADING and not HALT_STATE.get("active"):
                trader.reconcile_live_pending()
        except Exception as e:
            print(f"[reconcile] err: {e}\n{traceback.format_exc()}", flush=True)
        time.sleep(30)


# ===== HTTP API =====
def _json(handler, code, payload):
    body = json.dumps(payload, default=str).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    origin = _origin_for_request(handler)
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
        if origin != "*":
            handler.send_header("Vary", "Origin")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def do_OPTIONS(self):
        # CORS preflight handler. Origin-locked when DASHBOARD_ORIGIN env is set.
        origin = _origin_for_request(self)
        if not origin:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        if origin != "*":
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Halt-Token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                effective_mode = trader._effective_mode()
                _json(self, 200, {
                    "service": ENGINE_NAME,
                    "version": ENGINE_VERSION,
                    "mode_configured": "live" if LIVE_TRADING else ("dry_run" if DRY_RUN else "paper"),
                    "mode_effective": effective_mode,
                    "active_universe": ACTIVE_UNIVERSE,
                    "primary_universe": PRIMARY_UNIVERSE,
                    "blocked_universe": BLOCKED_UNIVERSE,
                    "halt": HALT_STATE,
                    "params": {
                        "strategy_params": STRATEGY_PARAMS,
                        "trade": {
                            "risk_pct_per_trade": RISK_PCT_PER_TRADE,
                            "leverage": LEVERAGE,
                            "max_notional": MAX_NOTIONAL_PER_TRADE,
                            "max_open_positions": MAX_OPEN_POSITIONS,
                        },
                        "live_safety": {
                            "min_account_value": LIVE_MIN_ACCOUNT_VALUE,
                            "size_scale": LIVE_SIZE_SCALE,
                            "exit_slippage": LIVE_EXIT_SLIPPAGE,
                            "maker_only_entries": LIVE_MAKER_ONLY_ENTRIES,
                            "use_testnet": USE_TESTNET,
                        },
                    },
                    "pm_url": PM_URL,
                    "endpoints": ["/health", "/state", "/signals", "/trades",
                                  "/closures", "/pnl", "/halt", "/scan", "/universe",
                                  "/live/status", "/live/events", "/live/pending"],
                })
            elif u.path == "/health":
                _json(self, 200, {"status": "ok", "ts": int(time.time() * 1000),
                                   "halted": HALT_STATE.get("active", False),
                                   "mode_effective": trader._effective_mode()})
            elif u.path == "/state":
                from engine import cross_venue_engine as _cvf
                _json(self, 200, {
                    "halt": HALT_STATE,
                    "mode_effective": trader._effective_mode(),
                    "open_trades": persistence.get_open_trades(),
                    "pending_live": persistence.get_pending_live_trades(),
                    "pnl": persistence.get_pnl_summary(),
                    "cross_venue": _cvf.get_state(),
                })
            elif u.path == "/cross_venue":
                from engine import cross_venue_engine as _cvf
                _json(self, 200, _cvf.get_state())
            elif u.path == "/cross_venue/scan":
                from engine import cross_venue_engine as _cvf
                _json(self, 200, {"opportunities": _cvf.scan_opportunities()})
            elif u.path == "/cross_venue/divergence":
                from engine import cross_venue_engine as _cvf
                _json(self, 200, {"divergences": _cvf.scan_price_divergence()})
            elif u.path == "/live/status":
                # Detailed live-mode introspection
                client = trader._get_hl_client()
                payload = {
                    "live_trading_flag": LIVE_TRADING,
                    "private_key_present": bool(HL_PRIVATE_KEY),
                    "expected_wallet": HL_WALLET,
                    "use_testnet": USE_TESTNET,
                    "effective_mode": trader._effective_mode(),
                }
                if client is not None:
                    payload["client_armed"] = bool(getattr(client, "armed", False))
                    payload["actual_wallet"] = getattr(client, "actual_wallet", None)
                    if client.armed:
                        try:
                            payload["account_value"] = client.get_account_value()
                        except Exception as e:
                            payload["account_value_error"] = str(e)
                else:
                    payload["client_armed"] = False
                _json(self, 200, payload)
            elif u.path == "/live/events":
                qs = parse_qs(u.query)
                limit = int(qs.get("limit", ["100"])[0])
                _json(self, 200, {"events": persistence.get_recent_live_events(limit)})
            elif u.path == "/live/pending":
                _json(self, 200, {"pending": persistence.get_pending_live_trades()})
            elif u.path == "/signals":
                qs = parse_qs(u.query)
                limit = int(qs.get("limit", ["50"])[0])
                _json(self, 200, {"signals": persistence.get_recent_signals(limit)})
            elif u.path == "/trades":
                _json(self, 200, {"trades": persistence.get_open_trades()})
            elif u.path == "/closures":
                qs = parse_qs(u.query)
                limit = int(qs.get("limit", ["50"])[0])
                _json(self, 200, {"closures": persistence.get_recent_closures(limit)})
            elif u.path == "/cells":
                from engine import cell_manager
                qs = parse_qs(u.query)
                only_active = qs.get("active", ["0"])[0] == "1"
                cells = cell_manager.list_cells(persistence._db_path(),
                                                  only_active=only_active)
                _json(self, 200, {"cells": cells, "count": len(cells)})
            elif u.path == "/pnl":
                _json(self, 200, persistence.get_pnl_summary())
            elif u.path == "/universe":
                _json(self, 200, {
                    "active": ACTIVE_UNIVERSE,
                    "primary": PRIMARY_UNIVERSE,
                    "blocked": BLOCKED_UNIVERSE,
                })
            elif u.path == "/halt":
                _json(self, 200, HALT_STATE)
            else:
                _json(self, 404, {"error": "not_found", "path": u.path})
        except Exception as e:
            _json(self, 500, {"error": str(e), "trace": traceback.format_exc()})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length > 0 else ""
            data = json.loads(body) if body else {}

            # Halt-endpoint authentication.
            if u.path in ("/halt", "/resume"):
                if HALT_TOKEN:
                    provided = self.headers.get("X-Halt-Token", "").strip()
                    import hmac
                    if not provided or not hmac.compare_digest(provided, HALT_TOKEN):
                        print(f"[halt] AUTH REJECTED on {u.path} (origin={self.headers.get('Origin','?')})", flush=True)
                        _json(self, 401, {"error": "unauthorized", "message": "X-Halt-Token required"})
                        return

            if u.path == "/halt":
                HALT_STATE["active"] = True
                HALT_STATE["reason"] = data.get("reason", "manual")
                HALT_STATE["ts"] = int(time.time() * 1000)
                print(f"[halt] ENGINE HALTED: {HALT_STATE['reason']}", flush=True)
                _json(self, 200, {"halted": True, **HALT_STATE})
            elif u.path == "/resume":
                HALT_STATE["active"] = False
                HALT_STATE["reason"] = None
                HALT_STATE["ts"] = int(time.time() * 1000)
                print("[halt] ENGINE RESUMED", flush=True)
                _json(self, 200, {"halted": False, **HALT_STATE})
            elif u.path == "/scan":
                # Manual trigger
                threading.Thread(target=_scan_once, daemon=True).start()
                _json(self, 200, {"status": "scan_triggered"})
            elif u.path == "/wipe_all_open":
                # Reconciliation: mark ALL open trades (paper + live) as wiped.
                # Used when wallet has 0 open positions but DB tracks phantoms.
                if HALT_TOKEN:
                    provided = self.headers.get("X-Halt-Token", "").strip()
                    import hmac as _hmac
                    if not provided or not _hmac.compare_digest(provided, HALT_TOKEN):
                        _json(self, 401, {"error": "unauthorized"})
                        return
                wiped = 0
                try:
                    with persistence.conn() as c:
                        rows = c.execute("SELECT cloid FROM trades WHERE status='open'").fetchall()
                        for r in rows:
                            c.execute("UPDATE trades SET status='wiped_phantom' WHERE cloid=?", (r[0],))
                            wiped += 1
                        c.commit()
                    _json(self, 200, {"wiped": wiped})
                    print(f"[wipe_all_open] wiped={wiped} phantom open trades", flush=True)
                except Exception as e:
                    _json(self, 500, {"error": str(e)})

            else:
                _json(self, 404, {"error": "not_found"})
        except Exception as e:
            _json(self, 500, {"error": str(e), "trace": traceback.format_exc()})


def main():
    print("=" * 60, flush=True)
    print(f"{ENGINE_NAME} v{ENGINE_VERSION} starting", flush=True)
    print(f"  configured:  {'LIVE' if LIVE_TRADING else ('DRY_RUN' if DRY_RUN else 'PAPER')}", flush=True)
    print(f"  effective:   {trader._effective_mode()}", flush=True)
    print(f"  universe:    {ACTIVE_UNIVERSE}", flush=True)
    print(f"  primary:     {PRIMARY_UNIVERSE}", flush=True)
    print(f"  blocked:     {BLOCKED_UNIVERSE}", flush=True)
    print(f"  pm_url:      {PM_URL}", flush=True)
    print(f"  port:        {HTTP_PORT}", flush=True)
    print(f"  halt_auth:   {'token required' if HALT_TOKEN else '⚠ OPEN (no token)'}", flush=True)
    print(f"  cors:        {'origin-locked → ' + DASHBOARD_ORIGIN if DASHBOARD_ORIGIN != '*' else '⚠ open (*)'}", flush=True)
    if not HALT_TOKEN:
        print("  ⚠ WARNING: HALT_TOKEN unset — halt endpoints are open", flush=True)
    if DASHBOARD_ORIGIN == "*":
        print("  ⚠ WARNING: DASHBOARD_ORIGIN unset — CORS is open", flush=True)
    if LIVE_TRADING:
        print(f"  testnet:     {USE_TESTNET}", flush=True)
        print(f"  size_scale:  {LIVE_SIZE_SCALE} (burn-in)", flush=True)
        print(f"  min_acct:    ${LIVE_MIN_ACCOUNT_VALUE}", flush=True)
        print(f"  maker_only:  {LIVE_MAKER_ONLY_ENTRIES}", flush=True)
    print("=" * 60, flush=True)

    persistence.init_db()
    threading.Thread(target=scan_loop, daemon=True, name="scan").start()
    threading.Thread(target=position_loop, daemon=True, name="positions").start()
    threading.Thread(target=reconcile_loop, daemon=True, name="reconcile").start()

    HTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
