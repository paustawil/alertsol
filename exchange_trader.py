#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)
Wywoływany przez sol_alert.py co 15 minut.

Bitget używa "plan orders" jako conditional orderów — nie blokują margin
do momentu aktywacji (triggerPrice = W1).

Przy wejściu składane są DWA plan ordery (50%/50%), każdy z własnym
presetTakeProfitPrice i presetStopLossPrice. Bitget automatycznie tworzy
powiązane OCO (TP + SL) po wykonaniu każdego planu.

Po TP1: anulujemy pozostały SL (dla połowy TP2) i stawiamy nowy na BE.
Resztą zarządza Bitget.

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
TRADE_USDT    = float(os.getenv("BITGET_TRADE_USDT") or "100.0")  # kwota margin na jeden trade
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

def _place_entry_plan_orders(client: BitgetClient, s: dict) -> tuple[str | None, str | None]:
    """
    Dwa plan ordery przy W1 — każdy na 50% pozycji z presetTakeProfitPrice i
    presetStopLossPrice. Bitget automatycznie tworzy powiązane OCO (TP+SL)
    po wykonaniu każdego planu.
    Zwraca (order1_id, order2_id).
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    tps       = s.get("tps", [])
    sl        = s["sl"]
    tp1       = tps[0] if len(tps) > 0 else None
    tp2       = tps[1] if len(tps) > 1 else None
    half_qty  = _round_qty(_round_qty((TRADE_USDT * LEVERAGE) / w1) / 2)
    side      = "buy" if direction == "long" else "sell"

    def _place_one(tp_price: float | None, label: str) -> str | None:
        params = {
            "symbol":              SYMBOL,
            "productType":         PRODUCT_TYPE,
            "marginMode":          MARGIN_MODE,
            "marginCoin":          MARGIN_COIN,
            "planType":            "normal_plan",
            "size":                _fmt_qty(half_qty),
            "price":               _fmt_price(w1),
            "triggerPrice":        _fmt_price(w1),
            "triggerType":         "mark_price",
            "side":                side,
            "posSide":             direction,
            "orderType":           "limit",
            "presetStopLossPrice": _fmt_price(sl),
        }
        if tp_price:
            params["presetTakeProfitPrice"] = _fmt_price(tp_price)
        try:
            resp = client.post("/api/v2/mix/order/place-plan-order", params)
            if resp.get("code") == "00000":
                oid = resp["data"]["orderId"]
                tp_info = f"TP={tp_price}" if tp_price else "bez TP"
                print(f"[exchange] Plan order {label}: {oid} | {side} {_fmt_qty(half_qty)} SOL @ {w1} | {tp_info} SL={sl}")
                return oid
            log.error(f"[exchange] place_entry_plan_order {label}: {resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] place_entry_plan_order {label}: {e}")
        return None

    oid1 = _place_one(tp1, "1/TP1")
    oid2 = _place_one(tp2, "2/TP2")
    return oid1, oid2


def _cancel_plan_order(client: BitgetClient, order_id: str):
    """Anuluje plan order (normal_plan lub tpsl)."""
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


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _plan_order_status(client: BitgetClient, order_id: str) -> str:
    """
    Sprawdza status plan order (normal_plan).
    Zwraca: 'live' | 'executed' | 'cancelled' | 'unknown'
    """
    try:
        # Sprawdź aktywne plan ordery
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "normal_plan",
        })
        if resp.get("code") == "00000":
            live_ids = {o["orderId"] for o in resp["data"].get("entrustedList", [])}
            if order_id in live_ids:
                return "live"

        # Nie ma w aktywnych — sprawdź historię
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


def _is_tp1_executed(client: BitgetClient, tp1_price: float, direction: str) -> bool:
    """
    Sprawdza czy tpsl TP1 (pos_profit) przy cenie tp1_price został wykonany.
    Najpierw sprawdza czy nie ma go w pending — jeśli nie ma, szuka w historii.
    """
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "pos_profit",
        })
        if resp.get("code") == "00000":
            for o in resp["data"].get("entrustedList", []):
                if (o.get("posSide") == direction
                        and abs(float(o.get("triggerPrice", 0)) - tp1_price) < 0.01):
                    return False  # Nadal aktywny — jeszcze nie wykonany
    except Exception as e:
        log.warning(f"[exchange] is_tp1_executed pending: {e}")

    try:
        resp = client.get("/api/v2/mix/order/orders-plan-history", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "pos_profit",
            "startTime":   str(int((time.time() - 7 * 86400) * 1000)),
            "endTime":     str(int(time.time() * 1000)),
        })
        if resp.get("code") == "00000":
            for o in resp["data"].get("entrustedList", []):
                if (o.get("posSide") == direction
                        and abs(float(o.get("triggerPrice", 0)) - tp1_price) < 0.01
                        and o.get("planStatus") == "executed"):
                    return True
    except Exception as e:
        log.warning(f"[exchange] is_tp1_executed history: {e}")
    return False


def _update_sl_after_tp1(client: BitgetClient, s: dict, new_sl: float):
    """
    Po TP1: anuluje pozostały SL (pos_loss) dla danego kierunku
    i stawia nowy na podanym poziomie (BE lub wyżej) dla 50% pozycji.
    """
    direction = s["direction"]

    # Anuluj pozostałe pos_loss ordery dla tego kierunku
    # (Bitget powinien już auto-anulować SL połowy TP1, zostaje jeden — dla TP2)
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "pos_loss",
        })
        if resp.get("code") == "00000":
            for o in resp["data"].get("entrustedList", []):
                if o.get("posSide") == direction:
                    _cancel_plan_order(client, o["orderId"])
    except Exception as e:
        log.warning(f"[exchange] update_sl_after_tp1 find: {e}")

    # Nowy SL dla pozostałej 50% pozycji
    w1       = s["entries"][0]
    half_qty = _round_qty(_round_qty((TRADE_USDT * LEVERAGE) / w1) / 2)
    side     = "sell" if direction == "long" else "buy"

    try:
        resp = client.post("/api/v2/mix/order/place-tpsl-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginMode":   MARGIN_MODE,
            "marginCoin":   MARGIN_COIN,
            "planType":     "pos_loss",
            "size":         _fmt_qty(half_qty),
            "triggerPrice": _fmt_price(new_sl),
            "triggerType":  "mark_price",
            "side":         side,
            "posSide":      direction,
        })
        if resp.get("code") == "00000":
            be_label = "BE" if abs(new_sl - w1) < 0.05 else f"@ {new_sl}"
            print(f"[exchange] SL przesunięty po TP1 → {new_sl} ({be_label})")
        else:
            log.error(f"[exchange] update_sl_after_tp1 place: {resp.get('msg')}")
    except Exception as e:
        log.error(f"[exchange] update_sl_after_tp1 place: {e}")


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY:      składa 2x plan order (50%/50%) z preset TP/SL
      - ANULOWANY: anuluje oba plan ordery na Bitget
      - OCZEKUJĄCY: sprawdza czy plan order wykonany
      - OTWARTY:   sprawdza TP1 → przesuwa SL drugiej połowy na BE
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
        order1_id = s.get("exchange_order1_id")
        order2_id = s.get("exchange_order2_id")
        opened    = s.get("exchange_position_opened", False)
        entries   = s.get("entries", [])

        if not entries:
            continue

        label = f"#{sid} [{model}] {direction.upper()}"

        # ── Anuluj gdy setup odrzucony przez Groka ───────────────────────────
        if (shadow or cancelled) and (order1_id or order2_id) and not opened:
            print(f"[exchange] {label}: anulowany → cancel plan orders")
            if order1_id:
                _cancel_plan_order(client, order1_id)
                s["exchange_order1_id"] = None
            if order2_id:
                _cancel_plan_order(client, order2_id)
                s["exchange_order2_id"] = None
            modified = True
            continue

        # ── Nowy setup — złóż 2x plan order z preset TP/SL ──────────────────
        if not shadow and not cancelled and not order1_id and s["entry_hit_at"] is None:
            w1 = entries[0]
            oid1, oid2 = _place_entry_plan_orders(client, s)
            if oid1 and oid2:
                s["exchange_order1_id"]       = oid1
                s["exchange_order2_id"]       = oid2
                s["exchange_position_opened"] = False
                modified = True
                print(f"[exchange] {label}: 2x plan order złożone @ {w1}")
            continue

        # ── Oczekujący — sprawdź czy plan order wykonany ─────────────────────
        if order1_id and not opened:
            status = _plan_order_status(client, order1_id)
            if status == "live":
                continue
            if status == "cancelled":
                print(f"[exchange] {label}: plan order anulowany z zewnątrz")
                if order2_id:
                    _cancel_plan_order(client, order2_id)
                s["exchange_order1_id"] = None
                s["exchange_order2_id"] = None
                modified = True
                continue
            if status == "executed":
                print(f"[exchange] {label}: entry wykonany! TP/SL zarządzane przez Bitget.")
                s["exchange_position_opened"] = True
                modified = True
            continue

        # ── Pozycja otwarta — sprawdź TP1, przesuń SL drugiej połowy ────────
        if opened and not s.get("sl_adjusted"):
            tps = s.get("tps", [])
            tp1 = tps[0] if tps else None
            if tp1 and _is_tp1_executed(client, tp1, direction):
                w1       = entries[0]
                sl_after = s.get("sl_after_tp1") or w1  # null → W1 = breakeven
                _update_sl_after_tp1(client, s, sl_after)
                s["sl_adjusted"] = True
                s["tp1_hit_at"]  = s.get("tp1_hit_at") or int(time.time())
                modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
