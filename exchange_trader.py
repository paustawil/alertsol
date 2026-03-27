#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)
Wywoływany przez sol_alert.py co 15 minut.

Bitget używa "plan orders" jako conditional orderów — nie blokują margin
do momentu aktywacji (triggerPrice = W1).

Wymagane zmienne środowiskowe:
  BITGET_API_KEY       — klucz API
  BITGET_API_SECRET    — sekret API
  BITGET_PASSPHRASE    — hasło API (Bitget wymaga 3 składowych)
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
MARGIN_MODE   = "crossed"       # cross margin
LEVERAGE      = 20
TRADE_USDT    = float(os.getenv("BITGET_TRADE_USDT", "100.0"))  # kwota margin na jeden trade
QTY_STEP      = 0.1             # minimalny krok qty dla SOLUSDT
PRICE_DEC     = 2               # miejsca po przecinku ceny
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
            "ACCESS-KEY":       self.key,
            "ACCESS-SIGN":      self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":     "application/json",
            "locale":           "en-US",
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
        qs   = ""
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
    env_label = "DEMO" if demo else "PRODUKCJA"
    print(f"[exchange] Sesja Bitget {env_label} | {SYMBOL} | {LEVERAGE}x | {TRADE_USDT} USDT/trade")
    return BitgetClient(key, secret, passphrase, demo=demo)


# ── Dźwignia ──────────────────────────────────────────────────────────────────

def _set_leverage(client: BitgetClient):
    """Ustawia dźwignię dla long i short."""
    for hold_side in ("long", "short"):
        try:
            resp = client.post("/api/v2/mix/account/set-leverage", {
                "symbol":      SYMBOL,
                "productType": PRODUCT_TYPE,
                "marginCoin":  MARGIN_COIN,
                "leverage":    str(LEVERAGE),
                "holdSide":    hold_side,
            })
            if resp.get("code") not in ("00000", "40919"):  # 40919 = już ustawiona
                log.warning(f"[exchange] set_leverage {hold_side}: {resp.get('msg')}")
        except Exception as e:
            log.warning(f"[exchange] set_leverage {hold_side}: {e}")


# ── Składanie zleceń ───────────────────────────────────────────────────────────

def _place_plan_order(client: BitgetClient, s: dict) -> str | None:
    """
    Składa plan (conditional) order przy W1.
    Nie blokuje margin do momentu aktywacji.
    Bitget automatycznie ustala kierunek triggerowania na podstawie
    bieżącej ceny vs triggerPrice — nie trzeba go podawać.
    Zwraca orderId lub None.
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    qty       = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    side      = "buy" if direction == "long" else "sell"

    try:
        resp = client.post("/api/v2/mix/order/place-plan-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginMode":  MARGIN_MODE,
            "marginCoin":  MARGIN_COIN,
            "size":        _fmt_qty(qty),
            "price":       _fmt_price(w1),
            "triggerPrice": _fmt_price(w1),
            "triggerType": "mark_price",
            "side":        side,
            "tradeSide":   "open",
            "orderType":   "limit",
        })
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] Plan order złożony: {oid} | {side} {_fmt_qty(qty)} SOL @ {w1}")
            return oid
        log.error(f"[exchange] place_plan_order: {resp.get('msg')}")
        return None
    except Exception as e:
        log.error(f"[exchange] place_plan_order: {e}")
        return None


def _cancel_plan_order(client: BitgetClient, order_id: str):
    """Anuluje plan order."""
    try:
        resp = client.post("/api/v2/mix/order/cancel-plan-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "orderId":     order_id,
        })
        if resp.get("code") == "00000":
            print(f"[exchange] Plan order anulowany: {order_id}")
        else:
            log.warning(f"[exchange] cancel_plan_order {order_id}: {resp.get('msg')}")
    except Exception as e:
        log.warning(f"[exchange] cancel_plan_order {order_id}: {e}")


def _place_tp_order(client: BitgetClient, s: dict, tp_price: float, qty: float) -> str | None:
    """Składa limit order TP (reduce-only, close). Zwraca orderId."""
    direction = s["direction"]
    side      = "sell" if direction == "long" else "buy"

    try:
        resp = client.post("/api/v2/mix/order/place-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginMode":  MARGIN_MODE,
            "marginCoin":  MARGIN_COIN,
            "size":        _fmt_qty(qty),
            "price":       _fmt_price(tp_price),
            "side":        side,
            "tradeSide":   "close",
            "orderType":   "limit",
            "reduceOnly":  "YES",
        })
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] TP order: {oid} | {side} {_fmt_qty(qty)} SOL @ {tp_price}")
            return oid
        log.error(f"[exchange] place_tp_order @ {tp_price}: {resp.get('msg')}")
        return None
    except Exception as e:
        log.error(f"[exchange] place_tp_order: {e}")
        return None


def _place_sl_plan_order(client: BitgetClient, s: dict, sl_price: float, qty: float) -> str | None:
    """
    Składa plan order SL (stop-market, reduce-only, close).
    Zwraca orderId lub None.
    """
    direction = s["direction"]
    side      = "sell" if direction == "long" else "buy"

    try:
        resp = client.post("/api/v2/mix/order/place-plan-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginMode":   MARGIN_MODE,
            "marginCoin":   MARGIN_COIN,
            "size":         _fmt_qty(qty),
            "triggerPrice": _fmt_price(sl_price),
            "triggerType":  "mark_price",
            "side":         side,
            "tradeSide":    "close",
            "orderType":    "market",
            "reduceOnly":   "YES",
        })
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] SL plan order: {oid} | {side} {_fmt_qty(qty)} SOL stop @ {sl_price}")
            return oid
        log.error(f"[exchange] place_sl_plan_order @ {sl_price}: {resp.get('msg')}")
        return None
    except Exception as e:
        log.error(f"[exchange] place_sl_plan_order: {e}")
        return None


def _place_tp_sl_orders(client: BitgetClient, s: dict) -> tuple[str | None, str | None, str | None]:
    """Po wejściu w pozycję: TP1, TP2 (50%/50%) i SL. Zwraca (tp1_id, tp2_id, sl_id)."""
    w1       = s["entries"][0]
    full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    half_qty = _round_qty(full_qty / 2)
    tps      = s.get("tps", [])
    sl       = s["sl"]
    tp1      = tps[0] if tps else None
    tp2      = tps[1] if len(tps) > 1 else None

    tp1_id = _place_tp_order(client, s, tp1, half_qty) if tp1 else None
    tp2_id = _place_tp_order(client, s, tp2, half_qty) if tp2 else None
    sl_id  = _place_sl_plan_order(client, s, sl, full_qty)

    return tp1_id, tp2_id, sl_id


def _update_sl(client: BitgetClient, s: dict, new_sl: float):
    """Po TP1: anuluje stary SL, składa nowy na 50% pozycji."""
    old_sl_id = s.get("exchange_sl_order_id")
    if old_sl_id:
        _cancel_plan_order(client, old_sl_id)

    w1        = s["entries"][0]
    half_qty  = _round_qty(_round_qty((TRADE_USDT * LEVERAGE) / w1) / 2)
    new_sl_id = _place_sl_plan_order(client, s, new_sl, half_qty)

    if new_sl_id:
        s["exchange_sl_order_id"] = new_sl_id
        be_label = "BE" if abs(new_sl - w1) < 0.05 else f"+${abs(new_sl - w1):.2f}"
        print(f"[exchange] SL przesunięty po TP1 → {new_sl} ({be_label})")


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _plan_order_status(client: BitgetClient, order_id: str) -> str:
    """
    Sprawdza status plan order.
    Zwraca: 'live' | 'executed' | 'cancelled' | 'unknown'
    """
    try:
        # Sprawdź aktywne plan ordery
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
        })
        if resp.get("code") == "00000":
            live_ids = {o["orderId"] for o in resp["data"].get("entrustedList", [])}
            if order_id in live_ids:
                return "live"

        # Nie ma w aktywnych — sprawdź historię plan orderów
        resp = client.get("/api/v2/mix/order/orders-plan-history", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "startTime":   str(int((time.time() - 7 * 86400) * 1000)),  # ostatnie 7 dni
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


def _is_order_filled(client: BitgetClient, order_id: str) -> bool:
    """Sprawdza czy zwykły order (TP1/TP2) jest wypełniony."""
    try:
        resp = client.get("/api/v2/mix/order/detail", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "orderId":     order_id,
        })
        if resp.get("code") == "00000":
            return resp["data"].get("status") == "filled"
    except Exception as e:
        log.warning(f"[exchange] is_order_filled {order_id}: {e}")
    return False


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY:      składa plan order (conditional, nie blokuje margin)
      - ANULOWANY: anuluje plan order na Bitget
      - OCZEKUJĄCY: sprawdza czy plan order wykonany → otwiera TP1/TP2/SL
      - OTWARTY:   sprawdza TP1 → przesuwa SL
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

        # ── Anuluj gdy setup odrzucony przez Groka ───────────────────────────
        if (shadow or cancelled) and order_id and not opened:
            print(f"[exchange] {label}: anulowany → cancel plan order {order_id}")
            _cancel_plan_order(client, order_id)
            s["exchange_order_id"] = None
            modified = True
            continue

        # ── Nowy setup — złóż plan order (conditional entry) ─────────────────
        if not shadow and not cancelled and not order_id and s["entry_hit_at"] is None:
            w1  = entries[0]
            qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid = _place_plan_order(client, s)
            if oid:
                s["exchange_order_id"]        = oid
                s["exchange_position_opened"] = False
                s["exchange_tp1_order_id"]    = None
                s["exchange_tp2_order_id"]    = None
                s["exchange_sl_order_id"]     = None
                s["exchange_qty"]             = _fmt_qty(qty)
                modified = True
                print(f"[exchange] {label}: plan order → {oid} ({_fmt_qty(qty)} SOL @ {w1})")
            continue

        # ── Oczekujący — sprawdź czy plan order wykonany ─────────────────────
        if order_id and not opened:
            status = _plan_order_status(client, order_id)
            if status == "live":
                continue  # jeszcze czeka
            if status == "cancelled":
                print(f"[exchange] {label}: plan order anulowany z zewnątrz")
                s["exchange_order_id"] = None
                modified = True
                continue
            if status == "executed":
                print(f"[exchange] {label}: entry wykonany! Składam TP/SL.")
                tp1_id, tp2_id, sl_id = _place_tp_sl_orders(client, s)
                s["exchange_position_opened"] = True
                s["exchange_tp1_order_id"]    = tp1_id
                s["exchange_tp2_order_id"]    = tp2_id
                s["exchange_sl_order_id"]     = sl_id
                modified = True
            continue

        # ── Pozycja otwarta — sprawdź TP1, przesuń SL ───────────────────────
        if opened and not s.get("sl_adjusted"):
            tp1_oid = s.get("exchange_tp1_order_id")
            if tp1_oid and _is_order_filled(client, tp1_oid):
                w1       = entries[0]
                sl_after = s.get("sl_after_tp1") or w1  # null → W1 = breakeven
                _update_sl(client, s, sl_after)
                s["sl_adjusted"] = True
                s["tp1_hit_at"]  = s.get("tp1_hit_at") or int(time.time())
                modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
