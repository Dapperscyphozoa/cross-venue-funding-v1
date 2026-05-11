"""
regime.py — local price-action regime classifier.

Mirrors KIROSHI's logic but runs locally per-coin per-bar. Avoids dependency
on KIROSHI (which only tracks BTC/ETH/SOL).

Labels: trend_up / trend_down / range / chop

Rules:
  - SMA200 anchor: above = trending bias up, below = trending bias down
  - 20-bar slope: > +1% = up momentum, < -1% = down momentum
  - ADX(14): > 20 = trending, < 15 = chop, else range

Usage:
    from engine.regime import classify_latest_bar
    label = classify_latest_bar(df)  # df with OHLCV, latest bar
    # returns 'trend_up' / 'trend_down' / 'range' / 'chop' / None
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd
from typing import Optional, Tuple


# Per-engine env-configurable
BLOCKED_REGIMES = [r for r in os.environ.get("BLOCKED_REGIMES", "").split(",") if r]


def _adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
          period: int = 14) -> np.ndarray:
    if len(highs) < 2:
        return np.full(len(highs), np.nan)
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    plus_dm = np.where((highs[1:] - highs[:-1]) > (lows[:-1] - lows[1:]),
                        np.maximum(highs[1:] - highs[:-1], 0), 0)
    minus_dm = np.where((lows[:-1] - lows[1:]) > (highs[1:] - highs[:-1]),
                         np.maximum(lows[:-1] - lows[1:], 0), 0)
    atr = pd.Series(tr).ewm(span=period).mean()
    plus_di = 100 * pd.Series(plus_dm).ewm(span=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm).ewm(span=period).mean() / atr
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, 1)
    adx = dx.ewm(span=period).mean()
    return np.concatenate(([np.nan], adx.values))


def classify_latest_bar(df: pd.DataFrame) -> Optional[str]:
    """Return regime label for latest bar in df, or None if insufficient data."""
    if df is None or len(df) < 200:
        return None
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values

    sma200 = float(pd.Series(closes).rolling(200).mean().iloc[-1])
    if pd.isna(sma200):
        return None
    slope_20 = float(pd.Series(closes).pct_change(20).iloc[-1])
    if pd.isna(slope_20):
        return None
    adx_arr = _adx(highs, lows, closes, 14)
    adx = float(adx_arr[-1])
    if pd.isna(adx):
        return None

    last_close = float(closes[-1])
    above_sma = last_close > sma200
    trending = adx > 20

    if trending and above_sma and slope_20 > 0.01:
        return "trend_up"
    elif trending and (not above_sma) and slope_20 < -0.01:
        return "trend_down"
    elif adx < 15:
        return "chop"
    else:
        return "range"


def is_blocked(label: Optional[str]) -> bool:
    """Whether the engine should skip trading in this regime."""
    if label is None:
        return False   # don't block when classifier insufficient (e.g. early bars)
    return label in BLOCKED_REGIMES
