"""
Trade execution: paper-mode logs trades to DB; live-mode places real HL orders.

Live mode (LIVE_TRADING=1 + valid HL_PRIVATE_KEY):
  Entry:  post-only limit at signal ref_price (Add-Liquidity-Only).
          If HL rejects (would cross spread) and LIVE_MAKER_ONLY_ENTRIES=1 → abort.
  Exit:   market_close on SL/TP/TIME (taker — speed > rebate for SL).
  Sizing: paper notional × LIVE_SIZE_SCALE (default 0.25 = 25% in burn-in).
  Fallback: if HL_PRIVATE_KEY missing or wallet mismatch → silent paper fallback.
"""
from __future__ import annotations
import time
import uuid
import threading
from typing import Optional

from . import blacklist  # consecutive-loss tracker + dynamic blacklist

from . import persistence
from . import pm_client
from .config import (
    PAPER_MODE, LIVE_TRADING, DRY_RUN, USE_TESTNET, CLOID_PREFIX,
    FIXED_NOTIONAL_USD,
    LEVERAGE, MAX_NOTIONAL_PER_TRADE, MAX_OPEN_POSITIONS, RISK_PCT_PER_TRADE,
    HALT_STATE, ACTIVE_UNIVERSE, BLOCKED_UNIVERSE,
    TRADE_PARAMS,
    HL_WALLET, HL_PRIVATE_KEY,
    LIVE_MIN_ACCOUNT_VALUE, LIVE_SIZE_SCALE, LIVE_EXIT_SLIPPAGE, LIVE_MAKER_ONLY_ENTRIES,
)


# ===== Lazy singleton HL client =====
_hl_client = None
_hl_client_lock = threading.Lock()


def _get_hl_client():
    """Lazy-init HL client. Returns None if live mode unarmed."""
    global _hl_client
    if _hl_client is not None:
        return _hl_client
    if not LIVE_TRADING:
        return None
    with _hl_client_lock:
        if _hl_client is not None:
            return _hl_client
        try:
            from .hl_exchange import HLClient
            _hl_client = HLClient(HL_PRIVATE_KEY, HL_WALLET, testnet=USE_TESTNET)
        except ImportError as e:
            print(f"[trader] HL SDK missing — cannot arm live: {e}", flush=True)
            return None
        except Exception as e:
            print(f"[trader] HL client init failed: {e}", flush=True)
            return None
    return _hl_client


def _effective_mode() -> str:
    """
    Determine effective mode based on flags + key validity.

    Returns 'live' only if LIVE_TRADING=1 AND key is valid AND wallet matches.
    Otherwise returns 'paper' (or 'dry_run').
    Auto-fallback prevents accidental order attempts when keys are misconfigured.
    """
    if DRY_RUN:
        return "dry_run"
    if not LIVE_TRADING:
        return "paper"
    if not HL_PRIVATE_KEY:
        return "paper"
    client = _get_hl_client()
    if client is None or not getattr(client, "armed", False):
        return "paper"
    return "live"


def gen_cloid() -> str:
    """Generate cloid: vsqf_<24hex uuid>."""
    return f"{CLOID_PREFIX}{uuid.uuid4().hex[:24]}"


def position_size(equity: float, ref_price: float, sl_distance_pct: float,
                  scale: float = 1.0) -> tuple[float, float]:
    """Compute (size_in_coin, notional_usd)."""
    if ref_price <= 0 or sl_distance_pct <= 0:
        return (0, 0)
    # Fixed-notional override (env: FIXED_NOTIONAL_USD). Bypasses risk-based
    # sizing — every trade is exactly this dollar notional. Capped only by
    # available leverage on the account.
    if FIXED_NOTIONAL_USD > 0:
        notional_cap_lev = equity * LEVERAGE
        notional = min(FIXED_NOTIONAL_USD * scale, notional_cap_lev)
        size = notional / ref_price
        return (size, notional)
    risk_usd = equity * RISK_PCT_PER_TRADE * scale
    notional_target = risk_usd / sl_distance_pct
    notional_cap_lev = equity * LEVERAGE
    notional_cap_max = MAX_NOTIONAL_PER_TRADE * scale
    notional = min(notional_target, notional_cap_max, notional_cap_lev)
    size = notional / ref_price
    return (size, notional)


def attempt_trade(coin: str, signal: dict) -> dict:
    """Try to open a trade based on a squeeze fade signal."""
    if HALT_STATE.get("active"):
        return {"status": "skipped", "reason": f"engine_halted: {HALT_STATE.get('reason')}"}

    if coin in BLOCKED_UNIVERSE:
        return {"status": "skipped", "reason": "blocked_universe"}
    is_long = bool(signal.get("is_long", signal.get("trade_side", "B") == "B"))
    from .config import BLOCKED_LONGS, BLOCKED_SHORTS
    if is_long and coin in BLOCKED_LONGS:
        return {"status": "skipped", "reason": f"blocked_longs[{coin}]"}
    if (not is_long) and coin in BLOCKED_SHORTS:
        return {"status": "skipped", "reason": f"blocked_shorts[{coin}]"}

    # Regime gate — skip if current regime label is in BLOCKED_REGIMES env list
    from . import regime as regime_module
    if regime_module.BLOCKED_REGIMES:
        try:
            # Use the candles available to the engine. The signal_detector was
            # called with a df slice — but here in trader we don't have it.
            # So we re-derive: fetch latest candles via hl_data and classify.
            from . import hl_data
            from .config import STRATEGY_PARAMS
            tf = STRATEGY_PARAMS.get("timeframe", "1h")
            regime_df = hl_data.fetch_candles(coin, tf, n_bars=250)
            if regime_df is not None and len(regime_df) >= 200:
                label = regime_module.classify_latest_bar(regime_df)
                if regime_module.is_blocked(label):
                    return {"status": "skipped",
                             "reason": f"blocked_regime[{label}]"}
        except Exception as e:
            # Fail open — don't kill engine just because regime check failed
            print(f"[regime] classifier error for {coin}: {e}", flush=True)
    # NB: ACTIVE_UNIVERSE check removed — scan loop already filters via
    # _get_active_universe() (dynamic full HL universe minus blacklist + BLOCKED).
    # The static config ACTIVE_UNIVERSE is now stale when USE_FULL_UNIVERSE=1.

    open_trades = persistence.get_open_trades()
    pending_trades = persistence.get_pending_live_trades()
    all_trades = open_trades + pending_trades
    if len(all_trades) >= MAX_OPEN_POSITIONS:
        return {"status": "skipped", "reason": f"max_open_positions ({MAX_OPEN_POSITIONS}) reached"}

    if any(t["coin"] == coin for t in all_trades):
        return {"status": "skipped", "reason": "already_open_on_coin"}

    equity = pm_client.get_equity()
    if equity is None:
        return {"status": "skipped", "reason": "pm_equity_fetch_failed"}
    if equity < 100:
        return {"status": "skipped", "reason": f"equity_too_low: ${equity:.2f}"}

    ref_px = signal["ref_price"]
    sl_px = signal["sl_px"]
    sl_distance_pct = abs(ref_px - sl_px) / ref_px
    if sl_distance_pct < 0.001 or sl_distance_pct > 0.20:
        return {"status": "skipped", "reason": f"sl_distance_out_of_range: {sl_distance_pct:.4f}"}

    cloid = gen_cloid()
    mode = _effective_mode()

    # Pull live size_fraction from PM (lifecycle stage). Paper engines get 0.0.
    # If PM is unreachable, get_size_fraction returns 0.0 (fail-closed).
    pm_size_fraction = pm_client.get_size_fraction()
    is_live_mode = (mode == "live")
    if is_live_mode:
        size_scale = pm_size_fraction
    else:
        # Paper engines: use full sizing for backtest realism; PM /check
        # still gates everything but with is_live=False
        size_scale = 1.0

    size, notional = position_size(equity, ref_px, sl_distance_pct, scale=size_scale)
    if notional < 10:
        return {"status": "skipped",
                "reason": f"notional_too_small: ${notional:.2f} (size_scale={size_scale:.3f})"}

    # Pre-trade gate AFTER computing notional, so PM has real numbers to check.
    pm_check = pm_client.check_pretrade(
        coin=coin,
        side=signal["trade_side"],
        notional=notional,
        sl_distance_pct=sl_distance_pct,
        is_live=is_live_mode,
    )
    if not pm_check.get("allow"):
        return {"status": "denied_by_pm", "reason": pm_check.get("reason"),
                "pm_check": pm_check}

    # ===== Paper / dry-run path =====
    if mode in ("paper", "dry_run"):
        signal_id = persistence.insert_signal(coin, signal, traded=True)
        if mode == "paper":
            persistence.insert_trade(
                cloid=cloid, signal_id=signal_id, coin=coin,
                side=signal["trade_side"], size=size, entry_px=ref_px,
                sl_px=sl_px, tp_px=signal["tp_px"], notional=notional,
                leverage=LEVERAGE, max_hold_bars=signal["max_hold_bars"],
                mode=mode, pm_check=pm_check, status="open",
            )
        return {
            "status": "opened" if mode == "paper" else "dry_run_logged",
            "cloid": cloid, "mode": mode, "coin": coin,
            "side": signal["trade_side"], "size": size, "entry_px": ref_px,
            "sl_px": sl_px, "tp_px": signal["tp_px"], "notional": notional,
        }

    # ===== Live path =====
    return _live_open_trade(cloid, coin, signal, size, notional, sl_distance_pct,
                              equity, pm_check)


def _live_open_trade(cloid, coin, signal, size, notional, sl_distance_pct,
                      equity, pm_check) -> dict:
    """Place a live post-only entry order on HL."""
    from .hl_exchange import pre_live_checks

    client = _get_hl_client()
    if client is None or not client.armed:
        persistence.log_live_event("client_unarmed_at_open", coin=coin, cloid=cloid,
                                    details={"reason": "client_not_available"})
        return {"status": "error", "reason": "live_client_not_available"}

    check = pre_live_checks(
        client=client,
        expected_wallet=HL_WALLET,
        target_coin=coin,
        min_account_value=LIVE_MIN_ACCOUNT_VALUE,
        require_no_existing_position=True,
    )
    if not check.passed:
        persistence.log_live_event("pre_live_check_fail", coin=coin, cloid=cloid,
                                    details=check.to_dict())
        return {"status": "skipped", "reason": "pre_live_check_failed",
                "failures": check.failures}

    size_rounded = client.round_size(coin, size)
    if size_rounded <= 0:
        return {"status": "skipped",
                "reason": f"size_rounded_to_zero (size={size}, sz_decimals={client.get_sz_decimals(coin)})"}
    notional_actual = size_rounded * signal["ref_price"]

    is_buy = signal["trade_side"] == "B"
    entry_px = signal["ref_price"]

    # Set leverage if needed (best-effort — non-fatal)
    try:
        max_lev = client.get_max_leverage(coin)
        target_lev = min(LEVERAGE, max_lev)
        client.update_leverage(coin, target_lev, is_cross=True)
    except Exception as e:
        print(f"[trader] update_leverage warning: {e}", flush=True)

    # Register cloid → engine attribution with PM BEFORE placing the order
    # so PM can match the eventual fill back to this engine. Best-effort.
    try:
        reg = pm_client.register_cloid(cloid=cloid, coin=coin)
        if not reg.get("ok"):
            print(f"[trader] pm.register_cloid failed (non-fatal): {reg.get('reason')}",
                  flush=True)
    except Exception as e:
        print(f"[trader] pm.register_cloid exception (non-fatal): {e}", flush=True)

    # Place post-only limit
    result = client.place_post_only_limit(
        coin=coin, is_buy=is_buy, size=size_rounded,
        limit_px=entry_px, internal_cloid=cloid,
        reduce_only=False,
    )

    if result.get("status") != "ok":
        persistence.log_live_event("order_rejected", coin=coin, cloid=cloid, details=result)
        if not LIVE_MAKER_ONLY_ENTRIES:
            print(f"[trader] post-only rejected for {coin}, falling back to market", flush=True)
            result = client.place_market_order(
                coin=coin, is_buy=is_buy, size=size_rounded,
                internal_cloid=cloid, slippage=0.05, reduce_only=False,
            )
            if result.get("status") != "ok":
                persistence.log_live_event("market_fallback_rejected", coin=coin,
                                            cloid=cloid, details=result)
                return {"status": "error", "reason": "market_fallback_failed",
                        "details": result}
        else:
            return {"status": "rejected", "reason": "post_only_rejected",
                    "details": result}

    exchange_cloid = result.get("exchange_cloid")
    oid = result.get("oid")
    is_filled = result.get("filled", False)

    signal_id = persistence.insert_signal(coin, signal, traded=True)
    if is_filled:
        actual_px = result.get("avg_px", entry_px)
        actual_sz = result.get("filled_sz", size_rounded)
        persistence.insert_trade(
            cloid=cloid, signal_id=signal_id, coin=coin,
            side=signal["trade_side"], size=actual_sz, entry_px=actual_px,
            sl_px=signal["sl_px"], tp_px=signal["tp_px"],
            notional=actual_sz * actual_px, leverage=LEVERAGE,
            max_hold_bars=signal["max_hold_bars"], mode="live",
            pm_check=pm_check, status="open",
            exchange_cloid=exchange_cloid, entry_oid=oid,
            live_filled=1, live_filled_px=actual_px, live_filled_sz=actual_sz,
        )
        persistence.log_live_event("order_placed_and_filled", coin=coin, cloid=cloid,
                                    details={"oid": oid, "fill_px": actual_px, "fill_sz": actual_sz})
        print(f"[trader] LIVE FILLED {cloid} {coin} {signal['trade_side']} "
              f"sz={actual_sz} @{actual_px} ntl=${actual_sz*actual_px:.2f}", flush=True)
        return {"status": "opened_filled", "cloid": cloid, "mode": "live",
                "coin": coin, "side": signal["trade_side"], "size": actual_sz,
                "entry_px": actual_px, "oid": oid, "exchange_cloid": exchange_cloid}
    else:
        persistence.insert_trade(
            cloid=cloid, signal_id=signal_id, coin=coin,
            side=signal["trade_side"], size=size_rounded, entry_px=entry_px,
            sl_px=signal["sl_px"], tp_px=signal["tp_px"], notional=notional_actual,
            leverage=LEVERAGE, max_hold_bars=signal["max_hold_bars"],
            mode="live", pm_check=pm_check, status="pending",
            exchange_cloid=exchange_cloid, entry_oid=oid, live_filled=0,
        )
        persistence.log_live_event("order_placed_resting", coin=coin, cloid=cloid,
                                    details={"oid": oid, "limit_px": entry_px, "sz": size_rounded})
        print(f"[trader] LIVE RESTING {cloid} {coin} {signal['trade_side']} "
              f"sz={size_rounded} @{entry_px} oid={oid}", flush=True)
        return {"status": "opened_resting", "cloid": cloid, "mode": "live",
                "coin": coin, "side": signal["trade_side"], "size": size_rounded,
                "entry_px": entry_px, "oid": oid, "exchange_cloid": exchange_cloid}


def reconcile_live_pending():
    """Promote filled pending → open, expire stale pending."""
    pending = persistence.get_pending_live_trades()
    if not pending:
        return
    client = _get_hl_client()
    if client is None or not client.armed:
        return

    for t in pending:
        coin = t["coin"]
        cloid = t["cloid"]
        try:
            pos = client.get_position(coin)
            if pos is not None:
                actual_px = pos.get("entry_px") or t["entry_px"]
                actual_sz = abs(pos["szi"])
                persistence.update_live_fill(cloid, actual_px, actual_sz)
                persistence.log_live_event("fill_detected", coin=coin, cloid=cloid,
                                            details={"actual_px": actual_px, "actual_sz": actual_sz})
                print(f"[reconcile] FILLED {cloid} {coin} @{actual_px} sz={actual_sz}", flush=True)
                continue

            age_ms = int(time.time() * 1000) - t["ts_open"]
            max_resting_ms = (t["max_hold_bars"] * 2) * 3600 * 1000
            if age_ms > max_resting_ms and t.get("entry_oid"):
                cancel_result = client.cancel_order(coin, t["entry_oid"])
                persistence.update_trade_status(cloid, "expired")
                persistence.log_live_event("resting_order_expired", coin=coin,
                                            cloid=cloid, details=cancel_result)
                print(f"[reconcile] EXPIRED {cloid} {coin} oid={t['entry_oid']}", flush=True)
        except Exception as e:
            print(f"[reconcile] err for {cloid} {coin}: {e}", flush=True)


def manage_open_trades(get_current_price_fn, get_current_atr_fn=None):
    """Iterate open trades, check for SL/TP/time-stop hits, close them.

    get_current_price_fn(coin) signature has TWO supported shapes:
      (a) returns (last_px, hi, lo)            — legacy 1-bar tuple
      (b) returns (last_px, hi, lo, bars)      — bars is list of dict with
          keys 't' (start_ms), 'o','h','l','c'. Used for lookahead-safe
          paper resolution: only bars with t >= ts_open are scanned for hits.

    Lookahead bug fix: shape (a) consumes the CURRENT bar's cumulative
    high/low which includes price action BEFORE the trade entered. That
    inflates paper TP/SL hits artificially. Shape (b) lets the resolver
    scan only post-entry bar history.
    """
    open_trades = persistence.get_open_trades()
    closed = []

    for t in open_trades:
        cloid = t["cloid"]
        coin = t["coin"]
        side = t["side"]
        entry_px = t["entry_px"]
        sl_px = t["sl_px"]
        tp_px = t["tp_px"]
        ts_open = t["ts_open"]
        max_hold_ms = t["max_hold_bars"] * 3600 * 1000
        mode = t.get("mode", "paper")

        try:
            current = get_current_price_fn(coin)
        except Exception as e:
            print(f"[trader] price fetch err for {coin}: {e}", flush=True)
            continue
        if current is None:
            continue
        # Support both legacy 3-tuple and new 4-tuple (with post-entry bars)
        bars = None
        if len(current) >= 4:
            last_px, hi, lo, bars = current[0], current[1], current[2], current[3]
        else:
            last_px, hi, lo = current

        outcome = None
        exit_px = None
        is_long = side == "B"

        # If we have bar history, scan only bars whose start_ms >= ts_open.
        # SL-first on same-bar SL+TP hits (conservative — matches live spec).
        if bars:
            for bar in bars:
                bar_ts = bar.get("t") or bar.get("ts")
                if bar_ts is None or bar_ts < ts_open:
                    continue
                bh = float(bar.get("h", bar.get("high", 0)))
                bl = float(bar.get("l", bar.get("low", 0)))
                if bh <= 0 or bl <= 0:
                    continue
                if is_long:
                    if bl <= sl_px:
                        outcome, exit_px = "SL", sl_px; break
                    if bh >= tp_px:
                        outcome, exit_px = "TP", tp_px; break
                else:
                    if bh >= sl_px:
                        outcome, exit_px = "SL", sl_px; break
                    if bl <= tp_px:
                        outcome, exit_px = "TP", tp_px; break
        else:
            # Legacy path — kept for back-compat. Live mode fills here too,
            # but paper mode should always pass bars going forward.
            if is_long:
                if lo <= sl_px: outcome, exit_px = "SL", sl_px
                elif hi >= tp_px: outcome, exit_px = "TP", tp_px
            else:
                if hi >= sl_px: outcome, exit_px = "SL", sl_px
                elif lo <= tp_px: outcome, exit_px = "TP", tp_px

        if outcome is None:
            now_ms = int(time.time() * 1000)
            if now_ms - ts_open >= max_hold_ms:
                outcome, exit_px = "TIME", last_px

        if outcome is None:
            continue

        size = t["size"]
        notional = t["notional"]
        direction_sign = 1 if is_long else -1
        bars_held = max(1, int((int(time.time() * 1000) - ts_open) / 3_600_000))

        if mode == "live":
            result = _live_close_trade(cloid, coin, outcome)
            if result.get("status") != "ok":
                persistence.log_live_event("close_failed", coin=coin, cloid=cloid,
                                            details=result)
                print(f"[trader] LIVE CLOSE FAILED {cloid} {coin}: {result.get('error')}", flush=True)
                continue

            actual_exit_px = result.get("avg_px") or exit_px
            actual_exit_sz = result.get("filled_sz") or size
            gross_pnl = (actual_exit_px - entry_px) * actual_exit_sz * direction_sign
            fee_bps = TRADE_PARAMS["fee_bps_maker"] + TRADE_PARAMS["fee_bps_taker"]
            fees = notional * (fee_bps / 1e4)

            persistence.close_trade(
                cloid=cloid, exit_px=actual_exit_px, outcome=outcome,
                gross_pnl=gross_pnl, fees=fees, bars_held=bars_held,
                ref_notional=notional,
                live_exit_oid=result.get("oid"),
                live_exit_cloid=result.get("exchange_cloid"),
            )
            # Update consecutive-loss tracker for this coin
            try:
                blacklist.record_outcome(coin, gross_pnl - fees, outcome=outcome)
            except Exception as _e:
                print(f"[trader] blacklist hook failed (live): {_e}", flush=True)
            closed.append({"cloid": cloid, "coin": coin, "outcome": outcome,
                           "entry_px": entry_px, "exit_px": actual_exit_px,
                           "gross_pnl": gross_pnl, "net_pnl": gross_pnl - fees,
                           "bars_held": bars_held, "mode": "live"})
            print(f"[trader] LIVE CLOSED {cloid} {coin} {side} {outcome} "
                  f"entry={entry_px:.4f} exit={actual_exit_px:.4f} "
                  f"gross=${gross_pnl:+.4f} bars={bars_held}", flush=True)
        else:
            gross_pnl = (exit_px - entry_px) * size * direction_sign
            fee_bps = TRADE_PARAMS["fee_bps_taker"] * 2
            fees = notional * (fee_bps / 1e4)
            persistence.close_trade(
                cloid=cloid, exit_px=exit_px, outcome=outcome,
                gross_pnl=gross_pnl, fees=fees, bars_held=bars_held,
                ref_notional=notional,
            )
            # Update consecutive-loss tracker for this coin
            try:
                blacklist.record_outcome(coin, gross_pnl - fees, outcome=outcome)
            except Exception as _e:
                print(f"[trader] blacklist hook failed (paper): {_e}", flush=True)
            closed.append({"cloid": cloid, "coin": coin, "outcome": outcome,
                           "entry_px": entry_px, "exit_px": exit_px,
                           "gross_pnl": gross_pnl, "net_pnl": gross_pnl - fees,
                           "bars_held": bars_held, "mode": "paper"})
            print(f"[trader] CLOSED {coin} {side} {outcome} entry={entry_px:.4f} "
                  f"exit={exit_px:.4f} gross=${gross_pnl:+.4f} bars={bars_held}", flush=True)

    return closed


def _live_close_trade(cloid: str, coin: str, outcome: str) -> dict:
    """Issue HL market_close for a live position. Generates fresh exit cloid."""
    client = _get_hl_client()
    if client is None or not client.armed:
        return {"status": "error", "error": "client_unarmed"}

    exit_cloid_internal = f"{CLOID_PREFIX}exit_{uuid.uuid4().hex[:18]}"

    pos = client.get_position(coin)
    if pos is None:
        persistence.log_live_event("close_skipped_no_position", coin=coin, cloid=cloid,
                                    details={"outcome": outcome})
        return {"status": "ok", "no_position": True, "avg_px": None, "filled_sz": 0,
                "exchange_cloid": None, "oid": None}

    result = client.market_close_position(
        coin=coin, internal_cloid=exit_cloid_internal,
        slippage=LIVE_EXIT_SLIPPAGE,
    )
    persistence.log_live_event("exit_placed", coin=coin, cloid=cloid,
                                details={"outcome": outcome, "exit_cloid": exit_cloid_internal,
                                         "result": result})
    return result
