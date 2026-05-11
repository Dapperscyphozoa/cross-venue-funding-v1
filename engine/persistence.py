"""
SQLite persistence for vol-squeeze-fade.

Tables:
  signals        — every squeeze fire we observe (whether we trade or not)
  trades         — actual trades placed (paper or live)
  closures       — trade closures with PnL
"""
from __future__ import annotations
import os
import sqlite3
import time
import threading
import json
from contextlib import contextmanager
from typing import Optional

from .config import STATE_DIR, DB_FILE


_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _db_path() -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, DB_FILE)


def init_db():
    global _conn
    with _lock:
        if _conn is not None: return
        _conn = sqlite3.connect(_db_path(), check_same_thread=False, isolation_level=None)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _create_schema(_conn)
        _migrate_schema(_conn)


def _migrate_schema(c: sqlite3.Connection):
    """Migration shim: ensure cell_* columns exist on trades."""
    def _try(sql):
        try: c.execute(sql)
        except Exception: pass
    _try("ALTER TABLE trades ADD COLUMN cell_regime TEXT")
    _try("ALTER TABLE trades ADD COLUMN cell_direction TEXT")
    _try("ALTER TABLE trades ADD COLUMN cell_size_mult REAL")

    # Legacy column migrations (each wrapped — re-runs safe)
    def _add_col(table: str, col_def: str):
        col_name = col_def.split()[0]
        existing = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if col_name not in existing:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
            except sqlite3.OperationalError:
                pass

    # Live-trading columns on trades table (Package 5)
    _add_col("trades", "exchange_cloid TEXT")
    _add_col("trades", "entry_oid INTEGER")
    _add_col("trades", "live_filled INTEGER DEFAULT 0")
    _add_col("trades", "live_filled_px REAL")
    _add_col("trades", "live_filled_sz REAL")
    # Live exit columns on closures table
    _add_col("closures", "live_exit_oid INTEGER")
    _add_col("closures", "live_exit_cloid TEXT")
    # Indexes on new columns
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_exchange_cloid ON trades(exchange_cloid)")
    except sqlite3.OperationalError:
        pass


def _create_schema(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              INTEGER NOT NULL,
        coin            TEXT NOT NULL,
        ref_price       REAL NOT NULL,
        atr             REAL NOT NULL,
        raw_direction   TEXT,
        fade_direction  TEXT,
        bw_percentile   REAL,
        vol_spike       REAL,
        momentum        REAL,
        sl_px           REAL,
        tp_px           REAL,
        traded          INTEGER DEFAULT 0,
        skip_reason     TEXT,
        details_json    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
    CREATE INDEX IF NOT EXISTS idx_signals_coin ON signals(coin);

    CREATE TABLE IF NOT EXISTS trades (
        cloid           TEXT PRIMARY KEY,
        signal_id       INTEGER,
        ts_open         INTEGER NOT NULL,
        coin            TEXT NOT NULL,
        side            TEXT NOT NULL,             -- 'B' (long) | 'A' (short)
        size            REAL NOT NULL,
        entry_px        REAL NOT NULL,
        sl_px           REAL NOT NULL,
        tp_px           REAL NOT NULL,
        notional        REAL NOT NULL,
        leverage        INTEGER NOT NULL,
        max_hold_bars   INTEGER NOT NULL,
        mode            TEXT NOT NULL,             -- 'paper' | 'live' | 'dry_run'
        status          TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'closed_tp' | 'closed_sl' | 'closed_time' | 'halted' | 'manual' | 'pending'
        pm_check_result TEXT,
        exchange_cloid  TEXT,                       -- HL on-chain cloid (live only)
        entry_oid       INTEGER,                    -- HL order id of entry (live only)
        live_filled     INTEGER DEFAULT 0,          -- 0=not yet filled (resting), 1=filled
        live_filled_px  REAL,                       -- actual fill price from HL (live only)
        live_filled_sz  REAL,                       -- actual fill size from HL (live only)
        FOREIGN KEY(signal_id) REFERENCES signals(id)
    );
    CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_coin ON trades(coin);

    CREATE TABLE IF NOT EXISTS closures (
        cloid           TEXT PRIMARY KEY,
        ts_close        INTEGER NOT NULL,
        exit_px         REAL NOT NULL,
        outcome         TEXT NOT NULL,             -- 'TP' | 'SL' | 'TIME' | 'MANUAL' | 'HALT'
        gross_pnl       REAL NOT NULL,
        fees            REAL NOT NULL DEFAULT 0,
        net_pnl         REAL NOT NULL,
        bps_return      REAL NOT NULL,
        bars_held       INTEGER NOT NULL,
        live_exit_oid   INTEGER,                    -- HL order id of exit (live only)
        live_exit_cloid TEXT,                       -- HL on-chain cloid for exit (live only)
        FOREIGN KEY(cloid) REFERENCES trades(cloid)
    );

    CREATE TABLE IF NOT EXISTS live_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              INTEGER NOT NULL,
        cloid           TEXT,
        event_type      TEXT NOT NULL,             -- 'order_placed' | 'order_rejected' | 'fill_detected' | 'exit_placed' | 'pre_live_check_fail'
        coin            TEXT,
        details_json    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_live_events_ts ON live_events(ts);
    CREATE INDEX IF NOT EXISTS idx_live_events_cloid ON live_events(cloid);
    """)


@contextmanager
def conn():
    if _conn is None: init_db()
    with _lock:
        yield _conn


def insert_signal(coin: str, sig: dict, traded: bool = False, skip_reason: Optional[str] = None) -> int:
    with conn() as c:
        cursor = c.execute("""
            INSERT INTO signals (ts, coin, ref_price, atr, raw_direction, fade_direction,
                bw_percentile, vol_spike, momentum, sl_px, tp_px, traded, skip_reason, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            int(time.time() * 1000), coin, sig["ref_price"], sig["atr"],
            sig.get("raw_direction"), sig.get("fade_direction"),
            sig.get("bw_percentile"), sig.get("vol_spike"), sig.get("momentum"),
            sig.get("sl_px"), sig.get("tp_px"),
            1 if traded else 0, skip_reason,
            json.dumps({
                "bb_upper": sig.get("bb_upper"),
                "bb_lower": sig.get("bb_lower"),
                "bb_mid": sig.get("bb_mid"),
            }),
        ))
        return cursor.lastrowid


def insert_trade(cloid: str, signal_id: int, coin: str, side: str, size: float,
                  entry_px: float, sl_px: float, tp_px: float, notional: float,
                  leverage: int, max_hold_bars: int, mode: str,
                  pm_check: Optional[dict] = None,
                  status: str = "open",
                  exchange_cloid: Optional[str] = None,
                  entry_oid: Optional[int] = None,
                  live_filled: int = 0,
                  live_filled_px: Optional[float] = None,
                  live_filled_sz: Optional[float] = None,
                  cell_regime: Optional[str] = None,
                  cell_direction: Optional[str] = None,
                  cell_size_mult: Optional[float] = None):
    with conn() as c:
        c.execute("""
            INSERT INTO trades (cloid, signal_id, ts_open, coin, side, size, entry_px,
                sl_px, tp_px, notional, leverage, max_hold_bars, mode, status, pm_check_result,
                exchange_cloid, entry_oid, live_filled, live_filled_px, live_filled_sz,
                cell_regime, cell_direction, cell_size_mult)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cloid, signal_id, int(time.time() * 1000), coin, side, size, entry_px,
            sl_px, tp_px, notional, leverage, max_hold_bars, mode, status,
            json.dumps(pm_check) if pm_check else None,
            exchange_cloid, entry_oid, live_filled, live_filled_px, live_filled_sz,
            cell_regime, cell_direction, cell_size_mult,
        ))


def update_trade_status(cloid: str, status: str):
    with conn() as c:
        c.execute("UPDATE trades SET status = ? WHERE cloid = ?", (status, cloid))


def update_live_fill(cloid: str, filled_px: float, filled_sz: float):
    """When HL confirms a resting limit order has filled, persist actuals."""
    with conn() as c:
        c.execute("""
            UPDATE trades SET live_filled = 1, live_filled_px = ?, live_filled_sz = ?,
                              entry_px = ?, status = 'open'
            WHERE cloid = ?
        """, (filled_px, filled_sz, filled_px, cloid))


def get_pending_live_trades() -> list[dict]:
    """Live trades whose limit order is still resting (not yet filled)."""
    with conn() as c:
        rows = c.execute("""
            SELECT cloid, ts_open, coin, side, size, entry_px, sl_px, tp_px,
                   notional, leverage, max_hold_bars, mode, exchange_cloid, entry_oid
            FROM trades WHERE mode = 'live' AND status = 'pending'
            ORDER BY ts_open ASC
        """).fetchall()
        return [{
            "cloid": r[0], "ts_open": r[1], "coin": r[2], "side": r[3],
            "size": r[4], "entry_px": r[5], "sl_px": r[6], "tp_px": r[7],
            "notional": r[8], "leverage": r[9], "max_hold_bars": r[10], "mode": r[11],
            "exchange_cloid": r[12], "entry_oid": r[13],
        } for r in rows]


def get_trade(cloid: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("""
            SELECT cloid, ts_open, coin, side, size, entry_px, sl_px, tp_px,
                   notional, leverage, max_hold_bars, mode, status,
                   exchange_cloid, entry_oid, live_filled, live_filled_px, live_filled_sz
            FROM trades WHERE cloid = ?
        """, (cloid,)).fetchone()
        if not row: return None
        cols = ["cloid", "ts_open", "coin", "side", "size", "entry_px", "sl_px", "tp_px",
                "notional", "leverage", "max_hold_bars", "mode", "status",
                "exchange_cloid", "entry_oid", "live_filled", "live_filled_px", "live_filled_sz"]
        return dict(zip(cols, row))


def log_live_event(event_type: str, coin: Optional[str] = None,
                   cloid: Optional[str] = None, details: Optional[dict] = None):
    """Append a live-mode lifecycle event (order placed, rejected, filled, etc.)."""
    with conn() as c:
        c.execute("""
            INSERT INTO live_events (ts, cloid, event_type, coin, details_json)
            VALUES (?, ?, ?, ?, ?)
        """, (int(time.time() * 1000), cloid, event_type, coin,
              json.dumps(details) if details else None))


def get_recent_live_events(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT id, ts, cloid, event_type, coin, details_json
            FROM live_events ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        out = []
        for r in rows:
            d = {"id": r[0], "ts": r[1], "cloid": r[2],
                 "event_type": r[3], "coin": r[4]}
            if r[5]:
                try: d["details"] = json.loads(r[5])
                except Exception: d["details"] = r[5]
            out.append(d)
        return out


def close_trade(cloid: str, exit_px: float, outcome: str, gross_pnl: float,
                fees: float, bars_held: int, ref_notional: float,
                live_exit_oid: Optional[int] = None,
                live_exit_cloid: Optional[str] = None):
    net = gross_pnl - fees
    bps = (net / ref_notional * 1e4) if ref_notional > 0 else 0
    with conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO closures (cloid, ts_close, exit_px, outcome,
                gross_pnl, fees, net_pnl, bps_return, bars_held,
                live_exit_oid, live_exit_cloid)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cloid, int(time.time() * 1000), exit_px, outcome,
              gross_pnl, fees, net, bps, bars_held,
              live_exit_oid, live_exit_cloid))
        status_map = {"TP": "closed_tp", "SL": "closed_sl", "TIME": "closed_time",
                      "MANUAL": "manual", "HALT": "halted"}
        c.execute("UPDATE trades SET status = ? WHERE cloid = ?",
                  (status_map.get(outcome, "closed"), cloid))


def get_open_trades() -> list[dict]:
    """Returns only fully-open trades (not pending fill)."""
    with conn() as c:
        rows = c.execute("""
            SELECT cloid, ts_open, coin, side, size, entry_px, sl_px, tp_px,
                   notional, leverage, max_hold_bars, mode, status,
                   exchange_cloid, entry_oid, live_filled, live_filled_px, live_filled_sz,
                   cell_regime, cell_direction, cell_size_mult
            FROM trades WHERE status = 'open' ORDER BY ts_open ASC
        """).fetchall()
        return [{
            "cloid": r[0], "ts_open": r[1], "coin": r[2], "side": r[3],
            "size": r[4], "entry_px": r[5], "sl_px": r[6], "tp_px": r[7],
            "notional": r[8], "leverage": r[9], "max_hold_bars": r[10], "mode": r[11],
            "status": r[12], "exchange_cloid": r[13], "entry_oid": r[14],
            "live_filled": r[15], "live_filled_px": r[16], "live_filled_sz": r[17],
            "cell_regime": r[18], "cell_direction": r[19], "cell_size_mult": r[20],
        } for r in rows]


def get_recent_signals(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT id, ts, coin, ref_price, atr, raw_direction, fade_direction,
                   bw_percentile, vol_spike, momentum, sl_px, tp_px, traded, skip_reason
            FROM signals ORDER BY ts DESC LIMIT ?
        """, (limit,)).fetchall()
        return [{
            "id": r[0], "ts": r[1], "coin": r[2], "ref_price": r[3], "atr": r[4],
            "raw_direction": r[5], "fade_direction": r[6],
            "bw_percentile": r[7], "vol_spike": r[8], "momentum": r[9],
            "sl_px": r[10], "tp_px": r[11], "traded": r[12], "skip_reason": r[13],
        } for r in rows]


def get_recent_closures(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute("""
            SELECT cl.cloid, cl.ts_close, cl.exit_px, cl.outcome, cl.gross_pnl,
                   cl.fees, cl.net_pnl, cl.bps_return, cl.bars_held,
                   t.coin, t.side, t.entry_px, t.notional, t.mode
            FROM closures cl LEFT JOIN trades t ON cl.cloid = t.cloid
            ORDER BY cl.ts_close DESC LIMIT ?
        """, (limit,)).fetchall()
        return [{
            "cloid": r[0], "ts_close": r[1], "exit_px": r[2], "outcome": r[3],
            "gross_pnl": r[4], "fees": r[5], "net_pnl": r[6], "bps_return": r[7],
            "bars_held": r[8], "coin": r[9], "side": r[10], "entry_px": r[11],
            "notional": r[12], "mode": r[13],
        } for r in rows]


def get_pnl_summary() -> dict:
    with conn() as c:
        row = c.execute("""
            SELECT COUNT(*), SUM(net_pnl), SUM(fees), SUM(bps_return),
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN outcome = 'TP' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN outcome = 'SL' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN outcome = 'TIME' THEN 1 ELSE 0 END)
            FROM closures
        """).fetchone()
        n = row[0] or 0
        wins = row[4] or 0
        return {
            "n_closed": n,
            "n_wins": wins,
            "wr_pct": (wins / n * 100) if n > 0 else 0,
            "total_net_pnl": row[1] or 0,
            "total_fees": row[2] or 0,
            "total_bps": row[3] or 0,
            "avg_bps": ((row[3] or 0) / n) if n > 0 else 0,
            "tp_count": row[5] or 0,
            "sl_count": row[6] or 0,
            "time_count": row[7] or 0,
        }
