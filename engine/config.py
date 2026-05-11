"""
engine-template — generic engine config.

What forks edit:
  - ENGINE_NAME              (must match a key in PM's STRATEGY_REGISTRY)
  - CLOID_PREFIX             (6-char prefix for trade attribution at HL)
  - PRIMARY_UNIVERSE         (the coins your strategy targets)
  - STRATEGY_PARAMS          (your strategy's tuned numbers — replace verbatim)
  - TRADE_PARAMS             (SL/TP multipliers, hold time, etc.)

What forks DO NOT edit (handled by env vars on Render):
  - LIVE_TRADING / DRY_RUN / USE_TESTNET
  - HL_PRIVATE_KEY / HL_WALLET
  - PM_URL / PM_CHECK_ENABLED / PM_AUTH_TOKEN
  - Capital sizing knobs

All sizing knobs read from env so the same image runs paper/canary/small/full
without code changes — PM dials capital_fraction at the broker, engine just
respects MAX_NOTIONAL_PER_TRADE × pm_client.get_size_fraction().
"""
import os

# ─── IDENTITY ─────────────────────────────────────────────────────────
# REQUIRED: forks override.
ENGINE_NAME      = os.environ.get("ENGINE_NAME", "engine-template")
CLOID_PREFIX     = os.environ.get("CLOID_PREFIX", "tmpl_")
ENGINE_VERSION   = os.environ.get("ENGINE_VERSION", "0.1.0")

# ─── MODE ─────────────────────────────────────────────────────────────
LIVE_TRADING     = os.environ.get("LIVE_TRADING", "0") == "1"
PAPER_MODE       = not LIVE_TRADING
DRY_RUN          = os.environ.get("DRY_RUN", "0") == "1"
USE_TESTNET      = os.environ.get("USE_TESTNET", "0") == "1"

# ─── LIVE-MODE GUARDRAILS ─────────────────────────────────────────────
LIVE_MIN_ACCOUNT_VALUE  = float(os.environ.get("LIVE_MIN_ACCOUNT_VALUE", "200"))
LIVE_SIZE_SCALE         = float(os.environ.get("LIVE_SIZE_SCALE", "0.25"))
LIVE_EXIT_SLIPPAGE      = float(os.environ.get("LIVE_EXIT_SLIPPAGE", "0.05"))
LIVE_MAKER_ONLY_ENTRIES = os.environ.get("LIVE_MAKER_ONLY_ENTRIES", "1") == "1"

# ─── HL ENDPOINTS / WALLET ────────────────────────────────────────────
HL_REST       = "https://api.hyperliquid.xyz/info"
HL_EXCHANGE   = "https://api.hyperliquid.xyz/exchange"
HL_WALLET     = os.environ.get("HL_WALLET", "")
HL_PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")

# ─── PM CONTRACT ──────────────────────────────────────────────────────
PM_URL              = os.environ.get("PM_URL", "https://portfolio-manager-7df2.onrender.com")
PM_CHECK_ENABLED    = os.environ.get("PM_CHECK_ENABLED", "0") == "1"
PM_AUTH_TOKEN       = os.environ.get("PM_AUTH_TOKEN", "").strip()

# ─── UNIVERSE ─────────────────────────────────────────────────────────
# Forks override. Keep splits so capacity scales without renaming.
PRIMARY_UNIVERSE   = (os.environ.get("PRIMARY_UNIVERSE", "BTC,ETH,SOL,LINK")
                     .split(","))
SECONDARY_UNIVERSE = (os.environ.get("SECONDARY_UNIVERSE", "AVAX,DOGE,BNB,XRP")
                     .split(","))
BLOCKED_UNIVERSE   = [c for c in os.environ.get("BLOCKED_UNIVERSE", "").split(",") if c]
# Per-direction blacklists — coins this engine won't take long / short on.
# Lets us filter by historical per-coin / per-direction edge data.
BLOCKED_LONGS      = [c for c in os.environ.get("BLOCKED_LONGS", "").split(",") if c]
BLOCKED_SHORTS     = [c for c in os.environ.get("BLOCKED_SHORTS", "").split(",") if c]
# Regimes this engine refuses to trade in. Local classifier in engine/regime.py.
# Labels: trend_up, trend_down, range, chop
BLOCKED_REGIMES    = [r for r in os.environ.get("BLOCKED_REGIMES", "").split(",") if r]
ACTIVE_UNIVERSE    = [c for c in (PRIMARY_UNIVERSE + SECONDARY_UNIVERSE)
                      if c and c not in BLOCKED_UNIVERSE]

# ─── STRATEGY PARAMS ─────────────────────────────────────────────────
# REQUIRED: forks override. Put your tuned numbers here verbatim.
STRATEGY_PARAMS = {
    "timeframe":       os.environ.get("STRATEGY_TIMEFRAME", "1h"),
    "candles_history": int(os.environ.get("CANDLES_HISTORY", "200")),
    # ... fork adds its own keys here
}

# ─── TRADE PARAMS ────────────────────────────────────────────────────
TRADE_PARAMS = {
    "sl_atr_mult":    float(os.environ.get("SL_ATR_MULT", "2.0")),
    "tp_atr_mult":    float(os.environ.get("TP_ATR_MULT", "4.0")),
    "max_hold_bars":  int(os.environ.get("MAX_HOLD_BARS", "48")),
    "atr_period":     int(os.environ.get("ATR_PERIOD", "14")),
}

# ─── SIZING / RISK ───────────────────────────────────────────────────
RISK_PCT_PER_TRADE    = float(os.environ.get("RISK_PCT_PER_TRADE", "0.01"))
LEVERAGE              = float(os.environ.get("LEVERAGE", "5"))
MAX_NOTIONAL_PER_TRADE = float(os.environ.get("MAX_NOTIONAL_PER_TRADE", "100"))
FIXED_NOTIONAL_USD    = float(os.environ.get("FIXED_NOTIONAL_USD", "50"))
MAX_OPEN_POSITIONS    = int(os.environ.get("MAX_OPEN_POSITIONS", "4"))

# ─── SCHEDULING ──────────────────────────────────────────────────────
HTTP_PORT                    = int(os.environ.get("PORT", "10000"))
SCAN_INTERVAL_SEC            = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
POSITION_CHECK_INTERVAL_SEC  = int(os.environ.get("POSITION_CHECK_INTERVAL_SEC", "60"))

# ─── STORAGE ─────────────────────────────────────────────────────────
STATE_DIR = os.environ.get("STATE_DIR", "/var/data")
DB_FILE   = f"{ENGINE_NAME.replace('-', '_')}.db"

# ─── HALT STATE (in-memory) ──────────────────────────────────────────
HALT_STATE = {"active": False, "reason": None, "ts": None}
