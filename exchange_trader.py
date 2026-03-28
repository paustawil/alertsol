#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)
Wywoływany przez sol_alert.py co 15 minut.

Schemat działania:
  1. Nowy setup    → plan order z preset TP1 i SL (aktywują się automatycznie przy wejściu)
  2. TP1 wykonany  → anuluj stary SL, postaw nowy SL na BE (sl_after_tp1)
  3. Resztą zarządza Bitget (tpsl powiązane z pozycją)

Wymagane zmienne środowiskowe:
  BITGET_API_KEY       — klucz API
  BITGET_API_SECRET    — sekret API
  BITGET_PASSPHRASE    — hasło API
  BITGET_DEMO          — "true" = demo trading (domyślnie true)
"""

import os
import json
import math
import time
import hmac
import hashlib
import base64
import logging
import requests

log = logging.getLogger("exchange")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Konfiguracja ───────────────────────────────────────────────────────────────
SYMBOL        = "SOLUSDT"
PRODUCT_TYPE  = "USDT-FUTURES"
MARGIN_COIN   = "USDT"
MARGIN_MODE   = "crossed"
LEVERAGE      = 20
TRADE_USDT    = float(os.getenv("BITGET_TRADE_USDT") or "100.0")
QTY_STEP      = 0.1
PRICE_DEC     = 2
BASE_URL      = "https://api.bitget.com"
PENDING_FILE  = "pending_setups.json"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _round_qty(qty: float) -> float:
    return max(math.floor(qty / QTY_STEP) * QTY_STEP, QTY_STEP)

def _fmt_qty(qty: float) -> str:
    return f"{qty:.1f}"

def _fmt_price(p: float) -> str:
    return f"{p:.{PRICE_DEC}f}"

def _load_pending() -> list[dict]:
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE) as f:
        return json.load(f)

def _save_pending(pending: list[dict]):
    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)


# ── Klient Bitget REST API ─────────────────────────────────────────────────────

class BitgetClient:
    def __init__(self, key: str, secret: str, passphrase: str, demo: bool = True):
        self.key        = key
        self.secret     = secret
        self.passphrase = passphrase
        self.demo       = demo

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        msg = ts + method.upper() + path + (body or "")
        mac = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        h  = {
            "ACCESS-KEY":        self.key,
            "ACCESS-SIGN":       self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":      "application/json",
            "locale":            "en-US",
        }
        if self.demo:
            h["paptrading"] = "1"
        return h

    def post(self, path: str, params: dict) -> dict:
        body = json.dumps(params)
        resp = requests.post(
            BASE_URL + path,
            headers=self._headers("POST", path, body),
            data=body,
            timeout=10,
        )
        if not resp.ok:
            try:
                err = resp.json()
                raise requests.HTTPError(
                    f"{resp.status_code} {resp.reason} — code={err.get('code')} msg={err.get('msg')} | url={resp.url}",
                    response=resp,
                )
            except (ValueError, KeyError):
                resp.raise_for_status()
        return resp.json()

    def get(self, path: str, params: dict | None = None) -> dict:
        qs = ""
        if params:
            qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = requests.get(
            BASE_URL + path + qs,
            headers=self._headers("GET", path + qs),
            timeout=10,
        )
        if not resp.ok:
            try:
                err = resp.json()
                raise requests.HTTPError(
                    f"{resp.status_code} {resp.reason} — code={err.get('code')} msg={err.get('msg')} | url={resp.url}",
                    response=resp,
                )
            except (ValueError, KeyError):
                resp.raise_for_status()
        return resp.json()


def _client() -> BitgetClient | None:
    key        = os.getenv("BITGET_API_KEY", "")
    secret     = os.getenv("BITGET_API_SECRET", "")
    passphrase = os.getenv("BITGET_PASSPHRASE", "")
    if not key or not secret or not passphrase:
        print("[exchange] Brak BITGET_API_KEY/BITGET_API_SECRET/BITGET_PASSPHRASE — pomijam.")
        return None
    demo = os.getenv("BITGET_DEMO", "true").lower() != "false"
    print(f"[exchange] Sesja Bitget {'DEMO' if demo else 'PRODUKCJA'} | {SYMBOL} | {LEVERAGE}x | {TRADE_USDT} USDT/trade")
    return BitgetClient(key, secret, passphrase, demo=demo)


# ── Dźwignia ──────────────────────────────────────────────────────────────────

def _set_leverage(client: BitgetClient):
    for hold_side in ("long", "short"):
        try:
            resp = client.post("/api/v2/mix/account/set-leverage", {
                "symbol":      SYMBOL,
                "productType": PRODUCT_TYPE,
                "marginCoin":  MARGIN_COIN,
                "leverage":    str(LEVERAGE),
                "holdSide":    hold_side,
            })
            if resp.get("code") not in ("00000", "40919"):
                log.warning(f"[exchange] set_leverage {hold_side}: {resp.get('msg')}")
        except Exception as e:
            log.warning(f"[exchange] set_leverage {hold_side}: {e}")


# ── Składanie zleceń ───────────────────────────────────────────────────────────

def _place_plan_order(client: BitgetClient, s: dict) -> str | None:
    """Plan order (conditional entry przy W1) z preset TP i SL. Zwraca orderId."""
    direction = s["direction"]
    w1        = s["entries"][0]
    qty       = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    side      = "buy" if direction == "long" else "sell"
    tps       = s.get("tps", [])
    sl        = s.get("sl")
    tp1       = tps[0] if tps else None

    params = {
        "symbol":       SYMBOL,
        "productType":  PRODUCT_TYPE,
        "marginMode":   MARGIN_MODE,
        "marginCoin":   MARGIN_COIN,
        "planType":     "normal_plan",
        "size":         _fmt_qty(qty),
        "price":        _fmt_price(w1),
        "triggerPrice": _fmt_price(w1),
        "triggerType":  "mark_price",
        "side":         side,
        "posSide":      direction,
        "orderType":    "limit",
    }
    if tp1 is not None:
        params["presetStopSurplusPrice"] = _fmt_price(tp1)
    if sl is not None:
        params["presetStopLossPrice"] = _fmt_price(sl)

    try:
        resp = client.post("/api/v2/mix/order/place-plan-order", params)
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            tp_info = f" | preset TP={tp1}" if tp1 else ""
            sl_info = f" | preset SL={sl}" if sl else ""
            print(f"[exchange] Plan order: {oid} | {side} {_fmt_qty(qty)} SOL @ {w1}{tp_info}{sl_info}")
            return oid
        log.error(f"[exchange] place_plan_order: {resp.get('msg')}")
    except Exception as e:
        log.error(f"[exchange] place_plan_order: {e}")
    return None


def _place_tpsl_order(
    client: BitgetClient,
    s: dict,
    plan_type: str,       # "pos_profit" | "pos_loss"
    trigger_price: float,
    qty: float,
) -> str | None:
    """
    Składa zlecenie TPSL powiązane z pozycją (pos_profit lub pos_loss).
    Bitget automatycznie anuluje je gdy pozycja zostaje zamknięta.
    Zwraca orderId.
    """
    direction = s["direction"]
    side      = "sell" if direction == "long" else "buy"
    try:
        resp = client.post("/api/v2/mix/order/place-tpsl-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginMode":   MARGIN_MODE,
            "marginCoin":   MARGIN_COIN,
            "planType":     plan_type,
            "size":         _fmt_qty(qty),
            "triggerPrice": _fmt_price(trigger_price),
            "triggerType":  "mark_price",
            "side":         side,
            "posSide":      direction,
        })
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] {plan_type}: {oid} | {side} {_fmt_qty(qty)} SOL trigger @ {trigger_price}")
            return oid
        log.error(f"[exchange] place_tpsl_order {plan_type} @ {trigger_price}: {resp.get('msg')}")
    except Exception as e:
        log.error(f"[exchange] place_tpsl_order: {e}")
    return None


def _place_tp_sl_orders(client: BitgetClient, s: dict) -> tuple[str | None, str | None, str | None]:
    """Po wejściu: TP1 (50%), TP2 (50%), SL (100%) przez place-tpsl-order."""
    w1       = s["entries"][0]
    full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    half_qty = _round_qty(full_qty / 2)
    tps      = s.get("tps", [])
    sl       = s["sl"]
    tp1      = tps[0] if len(tps) > 0 else None
    tp2      = tps[1] if len(tps) > 1 else None

    tp1_id = _place_tpsl_order(client, s, "pos_profit", tp1, half_qty) if tp1 else None
    tp2_id = _place_tpsl_order(client, s, "pos_profit", tp2, half_qty) if tp2 else None
    sl_id  = _place_tpsl_order(client, s, "pos_loss",   sl,  full_qty)
    return tp1_id, tp2_id, sl_id


def _cancel_tpsl_order(client: BitgetClient, order_id: str):
    """Anuluje zlecenie tpsl (lub plan order)."""
    try:
        resp = client.post("/api/v2/mix/order/cancel-plan-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "orderId":     order_id,
        })
        if resp.get("code") == "00000":
            print(f"[exchange] Order anulowany: {order_id}")
        else:
            log.warning(f"[exchange] cancel_tpsl_order {order_id}: {resp.get('msg')}")
    except Exception as e:
        log.warning(f"[exchange] cancel_tpsl_order {order_id}: {e}")


def _update_sl(client: BitgetClient, s: dict, new_sl: float):
    """Po TP1: anuluje stary SL, stawia nowy na BE dla 50% pozycji."""
    old_sl_id = s.get("exchange_sl_order_id")
    if old_sl_id:
        _cancel_tpsl_order(client, old_sl_id)

    w1       = s["entries"][0]
    half_qty = _round_qty(_round_qty((TRADE_USDT * LEVERAGE) / w1) / 2)
    new_sl_id = _place_tpsl_order(client, s, "pos_loss", new_sl, half_qty)

    if new_sl_id:
        s["exchange_sl_order_id"] = new_sl_id
        be_label = "BE" if abs(new_sl - w1) < 0.05 else f"@ {new_sl}"
        print(f"[exchange] SL przesunięty po TP1 → {new_sl} ({be_label})")


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _plan_order_status(client: BitgetClient, order_id: str) -> str:
    """
    Sprawdza status plan order (normal_plan).
    Zwraca: 'live' | 'executed' | 'cancelled' | 'unknown'
    """
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "normal_plan",
        })
        if resp.get("code") == "00000":
            live_ids = {o["orderId"] for o in resp["data"].get("entrustedList", [])}
            if order_id in live_ids:
                return "live"

        resp = client.get("/api/v2/mix/order/orders-plan-history", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "normal_plan",
            "startTime":   str(int((time.time() - 7 * 86400) * 1000)),
            "endTime":     str(int(time.time() * 1000)),
        })
        if resp.get("code") == "00000":
            for o in resp["data"].get("entrustedList", []):
                if o["orderId"] == order_id:
                    status = o.get("planStatus", "")
                    if status == "executed":
                        return "executed"
                    if status in ("cancelled", "expired"):
                        return "cancelled"
    except Exception as e:
        log.warning(f"[exchange] plan_order_status {order_id}: {e}")
    return "unknown"


def _tpsl_order_status(client: BitgetClient, order_id: str, plan_type: str) -> str:
    """
    Sprawdza status zlecenia tpsl (pos_profit lub pos_loss).
    Zwraca: 'live' | 'executed' | 'cancelled' | 'unknown'
    """
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    plan_type,
        })
        if resp.get("code") == "00000":
            live_ids = {o["orderId"] for o in resp["data"].get("entrustedList", [])}
            if order_id in live_ids:
                return "live"

        resp = client.get("/api/v2/mix/order/orders-plan-history", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    plan_type,
            "startTime":   str(int((time.time() - 7 * 86400) * 1000)),
            "endTime":     str(int(time.time() * 1000)),
        })
        if resp.get("code") == "00000":
            for o in resp["data"].get("entrustedList", []):
                if o["orderId"] == order_id:
                    status = o.get("planStatus", "")
                    if status == "executed":
                        return "executed"
                    if status in ("cancelled", "expired"):
                        return "cancelled"
    except Exception as e:
        log.warning(f"[exchange] tpsl_order_status {order_id}: {e}")
    return "unknown"


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY:      składa plan order z preset TP1+SL
      - ANULOWANY: anuluje plan order
      - OCZEKUJĄCY: sprawdza czy entry wykonany → oznacza pozycję jako otwartą
      - OTWARTY:   sprawdza TP1 → przesuwa SL na BE
    """
    client = _client()
    if client is None:
        return

    _set_leverage(client)

    pending  = _load_pending()
    modified = False

    for s in pending:
        sid       = s.get("setup_id", "?")
        model     = s.get("model", "?")
        direction = s.get("direction", "?")
        shadow    = s.get("shadow", False)
        cancelled = bool(s.get("cancel_reason"))
        order_id  = s.get("exchange_order_id")
        opened    = s.get("exchange_position_opened", False)
        entries   = s.get("entries", [])

        if not entries:
            continue

        label = f"#{sid} [{model}] {direction.upper()}"

        # ── Anuluj gdy setup odrzucony ───────────────────────────────────────
        if (shadow or cancelled) and order_id and not opened:
            print(f"[exchange] {label}: anulowany → cancel plan order {order_id}")
            _cancel_tpsl_order(client, order_id)
            s["exchange_order_id"] = None
            modified = True
            continue

        # ── Nowy setup — złóż plan order z preset TP+SL ─────────────────────
        if not shadow and not cancelled and not order_id and s["entry_hit_at"] is None:
            w1  = entries[0]
            qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid = _place_plan_order(client, s)
            if oid:
                s["exchange_order_id"]        = oid
                s["exchange_position_opened"] = False
                s["exchange_qty"]             = _fmt_qty(qty)
                modified = True
                print(f"[exchange] {label}: plan order → {oid} ({_fmt_qty(qty)} SOL @ {w1})")
            continue

        # ── Oczekujący — sprawdź czy entry wykonany ──────────────────────────
        # Preset TP/SL zostały ustawione na planie — nie trzeba składać dodatkowych zleceń.
        if order_id and not opened:
            status = _plan_order_status(client, order_id)
            if status == "live":
                continue
            if status == "cancelled":
                print(f"[exchange] {label}: plan order anulowany z zewnątrz")
                s["exchange_order_id"] = None
                modified = True
                continue
            if status == "executed":
                print(f"[exchange] {label}: entry wykonany — pozycja otwarta z preset TP/SL.")
                s["exchange_position_opened"] = True
                modified = True
            continue

        # ── Pozycja otwarta — sprawdź TP1 przez cenę (preset nie zwraca ID) ─
        if opened and not s.get("sl_adjusted"):
            # Wykryj TP1 przez sol_alert.py (tp1_hit_at ustawiane przez check_pending)
            if s.get("tp1_hit_at"):
                w1       = entries[0]
                sl_after = s.get("sl_after_tp1") or w1
                _update_sl(client, s, sl_after)
                s["sl_adjusted"] = True
                s["tp1_hit_at"]  = s.get("tp1_hit_at") or int(time.time())
                modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
