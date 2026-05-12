"""
Blofin REST client with HMAC-SHA256 auth.

Required env:
  BLOFIN_API_KEY
  BLOFIN_API_SECRET
  BLOFIN_PASSPHRASE

Public endpoints work without auth (used for funding rate scanning).
Private endpoints (balances, positions, orders) require all three.

Rate limits (Blofin enforced):
  500 req/min per IP (public)
  30 req/10s per UserId (trading)
"""
from __future__ import annotations
import os
import time
import hmac
import json
import hashlib
import base64
import urllib.request
import urllib.parse
import urllib.error
import uuid
from typing import Optional, Any, Dict

BLOFIN_BASE = "https://openapi.blofin.com"

API_KEY = os.environ.get("BLOFIN_API_KEY", "").strip()
API_SECRET = os.environ.get("BLOFIN_API_SECRET", "").strip()
PASSPHRASE = os.environ.get("BLOFIN_PASSPHRASE", "").strip()


def _sign(timestamp: str, method: str, path: str, body: str, nonce: str) -> str:
    """Blofin HMAC-SHA256 signature.

    pre-hash = path + method + timestamp + nonce + body
    signature = base64(hex(HMAC-SHA256(secret, pre-hash)))
    """
    pre_hash = f"{path}{method}{timestamp}{nonce}{body}"
    hex_sig = hmac.new(
        API_SECRET.encode(),
        pre_hash.encode(),
        hashlib.sha256,
    ).hexdigest()
    return base64.b64encode(hex_sig.encode()).decode()


def _request(method: str, path: str, params: Optional[dict] = None,
              body: Optional[dict] = None, auth: bool = False,
              timeout: int = 10) -> Optional[dict]:
    """Make an HTTP request. Returns parsed JSON or None on error."""
    url = BLOFIN_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
        path += "?" + urllib.parse.urlencode(params, doseq=True)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; PrecogBot/1.0)",
        "Accept": "application/json",
    }
    data = b""
    body_str = ""
    if body is not None:
        body_str = json.dumps(body, separators=(",", ":"))
        data = body_str.encode()

    if auth:
        if not API_KEY or not API_SECRET or not PASSPHRASE:
            return {"error": "blofin_creds_missing"}
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        signature = _sign(timestamp, method, path, body_str, nonce)
        headers.update({
            "ACCESS-KEY": API_KEY,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": PASSPHRASE,
        })

    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()
            return {"error": f"http_{e.code}", "body": err_body[:300]}
        except Exception:
            return {"error": f"http_{e.code}"}
    except Exception as e:
        return {"error": str(e)[:200]}


# ─── Public endpoints (no auth required) ─────────────────────────────────────

def get_funding_rate(inst_id: str) -> Optional[float]:
    """Current funding rate for a coin. inst_id like "BTC-USDT"."""
    r = _request("GET", "/api/v1/market/funding-rate",
                  params={"instId": inst_id})
    if not r or r.get("error"): return None
    data = r.get("data") or []
    if not data: return None
    try:
        return float(data[0].get("fundingRate", 0))
    except Exception:
        return None


def get_funding_rate_history(inst_id: str, limit: int = 100) -> list:
    """Historical funding rates."""
    r = _request("GET", "/api/v1/market/funding-rate-history",
                  params={"instId": inst_id, "limit": str(limit)})
    if not r or r.get("error"): return []
    return r.get("data") or []


def get_instruments(inst_type: str = "SWAP") -> list:
    """List all instruments. inst_type: SWAP / SPOT."""
    r = _request("GET", "/api/v1/market/instruments",
                  params={"instType": inst_type})
    if not r or r.get("error"): return []
    return r.get("data") or []


def get_mark_price(inst_id: str) -> Optional[float]:
    r = _request("GET", "/api/v1/market/mark-price",
                  params={"instId": inst_id})
    if not r or r.get("error"): return None
    data = r.get("data") or []
    if not data: return None
    try:
        return float(data[0].get("markPx", 0))
    except Exception:
        return None


# ─── Private endpoints (auth required) ──────────────────────────────────────

def get_balance(account_type: str = "futures") -> Optional[dict]:
    """account_type: futures / funding / copy_trading."""
    r = _request("GET", "/api/v1/asset/balances",
                  params={"accountType": account_type}, auth=True)
    if not r or r.get("error"): return r
    return r


def get_positions(inst_id: Optional[str] = None) -> list:
    params = {}
    if inst_id: params["instId"] = inst_id
    r = _request("GET", "/api/v1/account/positions", params=params, auth=True)
    if not r or r.get("error"): return []
    return r.get("data") or []


def place_order(inst_id: str, side: str, size: str,
                 order_type: str = "market",
                 position_side: str = "net",
                 price: Optional[str] = None,
                 reduce_only: bool = False,
                 client_order_id: Optional[str] = None,
                 leverage: Optional[str] = None) -> Optional[dict]:
    """Place a futures order.

    side: buy / sell
    size: contract count as string
    order_type: market / limit / post_only
    position_side: net / long / short (hedge mode)
    """
    body = {
        "instId": inst_id,
        "marginMode": "cross",
        "positionSide": position_side,
        "side": side,
        "orderType": order_type,
        "size": str(size),
        "reduceOnly": str(reduce_only).lower(),
    }
    if price is not None: body["price"] = str(price)
    if client_order_id: body["clientOrderId"] = client_order_id
    if leverage: body["leverage"] = str(leverage)
    return _request("POST", "/api/v1/trade/order", body=body, auth=True)


def cancel_order(inst_id: str, order_id: Optional[str] = None,
                  client_order_id: Optional[str] = None) -> Optional[dict]:
    body = {"instId": inst_id}
    if order_id: body["orderId"] = order_id
    if client_order_id: body["clientOrderId"] = client_order_id
    return _request("POST", "/api/v1/trade/cancel-order", body=body, auth=True)


def close_position(inst_id: str, position_side: str = "net") -> Optional[dict]:
    """Close entire position on a contract."""
    return _request("POST", "/api/v1/trade/close-position",
                     body={"instId": inst_id, "marginMode": "cross",
                           "positionSide": position_side}, auth=True)


def transfer(currency: str, amount: str, from_account: str,
              to_account: str) -> Optional[dict]:
    """Move funds between accounts (futures/funding/copy_trading).
    Requires API key with TRANSFER permission."""
    return _request("POST", "/api/v1/asset/transfer",
                     body={
                         "currency": currency,
                         "amount": str(amount),
                         "fromAccount": from_account,
                         "toAccount": to_account,
                         "clientId": uuid.uuid4().hex[:16],
                     }, auth=True)


# ─── Health / smoke test ─────────────────────────────────────────────────────

def health_check() -> dict:
    """Returns {public_ok, auth_ok, has_creds, balance_usdt}."""
    out = {
        "has_creds": bool(API_KEY and API_SECRET and PASSPHRASE),
        "public_ok": False,
        "auth_ok": False,
        "balance_usdt": None,
        "error": None,
    }
    # Public check
    fr = get_funding_rate("BTC-USDT")
    out["public_ok"] = fr is not None
    if not out["has_creds"]:
        out["error"] = "BLOFIN_API_KEY/SECRET/PASSPHRASE not set"
        return out
    # Auth check
    bal = get_balance("futures")
    if bal and not bal.get("error"):
        out["auth_ok"] = True
        # Find USDT balance
        for entry in (bal.get("data") or []):
            if entry.get("currency") == "USDT":
                try:
                    out["balance_usdt"] = float(entry.get("balance", 0))
                except Exception:
                    pass
                break
    elif bal:
        out["error"] = bal.get("error") or "auth_unknown"
    return out
