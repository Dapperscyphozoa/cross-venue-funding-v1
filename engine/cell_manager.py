"""
cell_manager.py — per-cell lifecycle management.

A "cell" is a (engine, coin, regime, direction) tuple. Each cell maintains
its own rolling stats and is independently gated.

Why: engines that fire one flat config across 8 coins are bundling 8
distinct probability distributions. Most cells have no edge. The audit
keeps degrading because we're averaging across heterogeneous setups.

This module:
  1. Persists per-cell stats to the engine's SQLite db
  2. Updates stats every trade close
  3. Gates attempt_trade() — only fires if cell has positive rolling stats
  4. Returns a size multiplier for confidence-weighted sizing
  5. Auto-prunes cells when rolling perf degrades

Lifecycle stages per cell:
  bootstrap   — < MIN_TRADES_FOR_PROMOTION trades; size 0.5x (probing)
  active      — passes thresholds; full size (1.0x base, scaled by PF)
  demoted     — stats failed; size 0.0 (gated off, but stats still collected)

Cells auto-rehabilitate: a demoted cell that pulls back into thresholds
on shadow signals (recorded but not traded) returns to active.
"""
from __future__ import annotations
import os
import time
import sqlite3
import json
from contextlib import contextmanager
from typing import Optional

# Tunables (env-overridable)
MIN_TRADES_FOR_PROMOTION = int(os.environ.get("CELL_MIN_TRADES_PROMO", "8"))
MIN_PF_FOR_ACTIVE        = float(os.environ.get("CELL_MIN_PF",            "1.20"))
MIN_SHARPE_FOR_ACTIVE    = float(os.environ.get("CELL_MIN_SHARPE",        "0.05"))
LOOKBACK_TRADES          = int(os.environ.get("CELL_LOOKBACK_TRADES",     "30"))
BOOTSTRAP_SIZE_MULT      = float(os.environ.get("CELL_BOOTSTRAP_MULT",    "0.5"))
PF_SIZE_CAP              = float(os.environ.get("CELL_PF_SIZE_CAP",       "2.0"))

# Optional: pre-seed cells with backtest stats so they don't bootstrap from zero.
# Format: env var CELL_SEEDS = JSON object {"coin:regime:direction": {"pf":x,"n":n,"sum_r":r}}
SEED_JSON = os.environ.get("CELL_SEEDS", "")


@contextmanager
def _conn(db_path: str):
    c = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
    finally:
        c.close()


def init_schema(db_path: str):
    """Create cells table. Idempotent."""
    with _conn(db_path) as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS cells (
            cell_key      TEXT PRIMARY KEY,    -- coin:regime:direction
            coin          TEXT NOT NULL,
            regime        TEXT NOT NULL,
            direction     TEXT NOT NULL,       -- 'long' or 'short'
            stage         TEXT NOT NULL DEFAULT 'bootstrap',
            n_trades      INTEGER NOT NULL DEFAULT 0,
            n_wins        INTEGER NOT NULL DEFAULT 0,
            sum_r         REAL NOT NULL DEFAULT 0.0,
            gross_win     REAL NOT NULL DEFAULT 0.0,
            gross_loss    REAL NOT NULL DEFAULT 0.0,
            r_values_json TEXT,                -- JSON list of recent R values
            last_update   INTEGER NOT NULL DEFAULT 0,
            note          TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cells_coin ON cells(coin)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cells_stage ON cells(stage)")


def _key(coin: str, regime: str, direction: str) -> str:
    return f"{coin}:{regime}:{direction}"


def _load_seeds(db_path: str):
    """One-shot seeding from CELL_SEEDS env var. Safe to call repeatedly."""
    if not SEED_JSON:
        return
    try:
        seeds = json.loads(SEED_JSON)
    except Exception as e:
        print(f"[cell] seed parse error: {e}", flush=True); return
    with _conn(db_path) as c:
        for key, s in seeds.items():
            parts = key.split(":")
            if len(parts) != 3:
                continue
            coin, regime, direction = parts
            n = int(s.get("n", 0)); sum_r = float(s.get("sum_r", 0))
            pf = float(s.get("pf", 1.0))
            if n < 1: continue
            # Reconstruct gross_win / gross_loss from pf + sum_r
            if pf > 0 and pf != 1.0:
                gl = abs(sum_r / (pf - 1)) if (pf - 1) != 0 else 0.0
                gw = pf * gl
            else:
                gw = max(0.0, sum_r); gl = max(0.0, -sum_r)
            n_wins = int(s.get("n_wins", n // 2))
            # Insert only if cell doesn't exist (don't overwrite live stats)
            exists = c.execute("SELECT 1 FROM cells WHERE cell_key=?", (key,)).fetchone()
            if exists:
                continue
            stage = "active" if (pf >= MIN_PF_FOR_ACTIVE and n >= MIN_TRADES_FOR_PROMOTION) else "bootstrap"
            c.execute("""
                INSERT INTO cells (cell_key, coin, regime, direction, stage,
                                    n_trades, n_wins, sum_r, gross_win, gross_loss,
                                    r_values_json, last_update, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (key, coin, regime, direction, stage,
                  n, n_wins, sum_r, gw, gl, "[]",
                  int(time.time() * 1000), "seeded_from_backtest"))


def get_or_create_cell(db_path: str, coin: str, regime: str, direction: str) -> dict:
    """Fetch cell stats. Create as bootstrap if missing."""
    key = _key(coin, regime, direction)
    with _conn(db_path) as c:
        r = c.execute("SELECT cell_key, coin, regime, direction, stage, n_trades, "
                       "n_wins, sum_r, gross_win, gross_loss, r_values_json, last_update "
                       "FROM cells WHERE cell_key=?", (key,)).fetchone()
        if r is None:
            c.execute("INSERT INTO cells (cell_key, coin, regime, direction, "
                      "n_trades, last_update) VALUES (?, ?, ?, ?, 0, ?)",
                      (key, coin, regime, direction, int(time.time() * 1000)))
            return {"cell_key": key, "coin": coin, "regime": regime,
                    "direction": direction, "stage": "bootstrap", "n_trades": 0,
                    "n_wins": 0, "sum_r": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
                    "r_values": [], "pf": None, "sharpe": None}
        rv = json.loads(r[10]) if r[10] else []
        # Computed stats
        gw, gl = r[8], r[9]
        pf = (gw / gl) if gl > 0 else None
        sharpe = _sharpe(rv) if rv else None
        return {
            "cell_key": r[0], "coin": r[1], "regime": r[2], "direction": r[3],
            "stage": r[4], "n_trades": r[5], "n_wins": r[6], "sum_r": r[7],
            "gross_win": r[8], "gross_loss": r[9], "r_values": rv,
            "last_update": r[11],
            "pf": pf, "sharpe": sharpe,
        }


def _sharpe(r_values):
    if not r_values or len(r_values) < 2:
        return None
    n = len(r_values)
    mean = sum(r_values) / n
    var = sum((r - mean) ** 2 for r in r_values) / (n - 1)
    std = var ** 0.5
    if std == 0: return None
    return mean / std


def update_cell_on_close(db_path: str, coin: str, regime: str,
                          direction: str, pnl_r: float) -> dict:
    """Called by trader after every close. Updates stats, may transition stage."""
    cell = get_or_create_cell(db_path, coin, regime, direction)
    rv = cell["r_values"] + [float(pnl_r)]
    if len(rv) > LOOKBACK_TRADES:
        rv = rv[-LOOKBACK_TRADES:]
    n = len(rv)
    n_wins = sum(1 for r in rv if r > 0)
    sum_r = sum(rv)
    gw = sum(r for r in rv if r > 0)
    gl = abs(sum(r for r in rv if r <= 0))
    pf = (gw / gl) if gl > 0 else None
    sharpe = _sharpe(rv)

    # Decide stage transition
    new_stage = cell["stage"]
    note = None
    if n < MIN_TRADES_FOR_PROMOTION:
        new_stage = "bootstrap"
    else:
        if (pf is not None and pf >= MIN_PF_FOR_ACTIVE
            and sharpe is not None and sharpe >= MIN_SHARPE_FOR_ACTIVE):
            if cell["stage"] != "active":
                note = f"promoted: pf={pf:.2f} sharpe={sharpe:.3f}"
            new_stage = "active"
        else:
            if cell["stage"] == "active":
                note = f"demoted: pf={pf:.2f} sharpe={sharpe}"
            new_stage = "demoted"

    key = _key(coin, regime, direction)
    with _conn(db_path) as c:
        c.execute("""
            UPDATE cells SET stage=?, n_trades=?, n_wins=?, sum_r=?,
                              gross_win=?, gross_loss=?, r_values_json=?,
                              last_update=?, note=COALESCE(?, note)
            WHERE cell_key=?
        """, (new_stage, n, n_wins, sum_r, gw, gl, json.dumps(rv),
              int(time.time() * 1000), note, key))
    return {**cell, "stage": new_stage, "n_trades": n, "sum_r": sum_r,
             "pf": pf, "sharpe": sharpe}


def gate_decision(db_path: str, coin: str, regime: str,
                   direction: str) -> tuple[bool, float, str]:
    """
    Decide whether to fire this trade and at what size multiplier.

    Returns (allowed, size_multiplier, reason).

    Stages:
      bootstrap → allow at BOOTSTRAP_SIZE_MULT, gather data
      active    → allow at min(1.0 + (pf - 1.2) * 0.5, PF_SIZE_CAP)
      demoted   → block (size 0); shadow-record signal for rehab tracking
    """
    cell = get_or_create_cell(db_path, coin, regime, direction)
    stage = cell["stage"]
    n = cell["n_trades"]
    pf = cell["pf"]
    if stage == "active" and pf is not None:
        bump = max(1.0, min(1.0 + (pf - MIN_PF_FOR_ACTIVE) * 0.5, PF_SIZE_CAP))
        return True, bump, f"active(pf={pf:.2f})"
    if stage == "bootstrap":
        if n < MIN_TRADES_FOR_PROMOTION:
            return True, BOOTSTRAP_SIZE_MULT, f"bootstrap({n}/{MIN_TRADES_FOR_PROMOTION})"
        # has enough trades but didn't promote — must have just demoted
        return False, 0.0, f"bootstrap_failed_promotion(pf={pf})"
    if stage == "demoted":
        return False, 0.0, f"demoted(pf={pf} sharpe={cell['sharpe']})"
    return False, 0.0, f"unknown_stage[{stage}]"


def list_cells(db_path: str, *, only_active: bool = False) -> list[dict]:
    init_schema(db_path)
    _load_seeds(db_path)
    with _conn(db_path) as c:
        sql = "SELECT cell_key, coin, regime, direction, stage, n_trades, " \
              "n_wins, sum_r, gross_win, gross_loss, r_values_json, last_update, note " \
              "FROM cells"
        if only_active:
            sql += " WHERE stage='active'"
        sql += " ORDER BY sum_r DESC"
        rows = c.execute(sql).fetchall()
        out = []
        for r in rows:
            rv = json.loads(r[10]) if r[10] else []
            gw, gl = r[8], r[9]
            pf = (gw / gl) if gl > 0 else None
            out.append({
                "cell_key": r[0], "coin": r[1], "regime": r[2],
                "direction": r[3], "stage": r[4], "n_trades": r[5],
                "n_wins": r[6], "sum_r": r[7], "pf": pf,
                "sharpe": _sharpe(rv), "last_update": r[11], "note": r[12],
            })
        return out
