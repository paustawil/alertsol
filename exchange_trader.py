#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)

Flow dla każdego setupu:
  1. Nowy setup → 1 plan order przy W1 (pełna pozycja, bez presetów TP/SL)
  2. Plan order wykonany → 3 TPSL ordery:
       TP1: profit_plan, half_qty SOL, trigger=TP1
       TP2: profit_plan, half_qty SOL, trigger=TP2
       SL:  loss_plan,   full_qty SOL, trigger=SL
  3. Monitoring TPSL:
       SL wykonany  → anuluj TP1 i TP2
       TP1 wykonany → zmodyfikuj SL: size=half_qty, trigger=SLpoTP1
       TP2 wykonany → anuluj SL

Wymagane zmienne środowiskowe:
  BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
  BITGET_DEMO       — "true" = demo (domyślnie true)
  BITGET_TRADE_USDT — rozmiar pozycji w USDT (domyślnie 100)
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
SYMBOL       = "SOLUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN  = "USDT"
MARGIN_MODE  = "crossed"
LEVERAGE     = 20
TRADE_USDT   = float(os.getenv("BITGET_TRADE_USDT") or "100.0")
QTY_STEP     = 0.1
PRICE_DEC    = 2
BASE_URL     = "https://api.bitget.com"
PENDING_FILE = "pending_setups.json"


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


# ── Tryb pozycji i dźwignia ────────────────────────────────────────────────────

def _set_hedge_mode(client: BitgetClient):
    try:
        resp = client.post("/api/v2/mix/account/set-position-mode", {
            "productType": PRODUCT_TYPE,
            "posMode":     "hedge_mode",
        })
        if resp.get("code") not in ("00000", "40919"):
            log.warning(f"[exchange] set_position_mode: {resp.get('msg')}")
        else:
            print("[exchange] Hedge mode aktywny.")
    except Exception as e:
        log.warning(f"[exchange] set_position_mode: {e}")


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

def _place_entry_plan_order(client: BitgetClient, s: dict, full_qty: float) -> str | None:
    """
    Składa jeden plan order przy W1 — pełna pozycja, bez presetów TP/SL.
    Zwraca orderId lub None.
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    side      = "buy" if direction == "long" else "sell"

    params = {
        "symbol":       SYMBOL,
        "productType":  PRODUCT_TYPE,
        "marginMode":   MARGIN_MODE,
        "marginCoin":   MARGIN_COIN,
        "planType":     "normal_plan",
        "size":         _fmt_qty(full_qty),
        "price":        _fmt_price(w1),
        "triggerPrice": _fmt_price(w1),
        "triggerType":  "mark_price",
        "side":         side,
        "tradeSide":    "open",
        "posSide":      direction,
        "orderType":    "limit",
    }

    try:
        resp = client.post("/api/v2/mix/order/place-plan-order", params)
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] Plan order złożony: {oid} | {side} {_fmt_qty(full_qty)} SOL @ trigger {w1}")
            return oid
        log.error(f"[exchange] place_entry_plan_order: code={resp.get('code')} msg={resp.get('msg')}")
    except Exception as e:
        log.error(f"[exchange] place_entry_plan_order: {e}")
    return None


def _place_tpsl_orders(
    client: BitgetClient,
    s: dict,
    full_qty: float,
    half_qty: float,
) -> tuple[str | None, str | None, str | None]:
    """
    Po wejściu w pozycję składa 3 oddzielne TPSL ordery:
      TP1: profit_plan, half_qty SOL, trigger=TP1
      TP2: profit_plan, half_qty SOL, trigger=TP2
      SL:  loss_plan,   full_qty SOL, trigger=SL
    Zwraca (tp1_id, tp2_id, sl_id).
    """
    direction = s["direction"]
    hold_side = direction  # "long" lub "short"
    tps       = s.get("tps", [])
    tp1       = tps[0] if len(tps) > 0 else None
    tp2       = tps[1] if len(tps) > 1 else None
    sl        = s.get("sl")
    sid       = s.get("setup_id", "?")

    tp1_id = tp2_id = sl_id = None

    if tp1 is not None:
        try:
            resp = client.post("/api/v2/mix/order/place-tpsl-order", {
                "symbol":       SYMBOL,
                "productType":  PRODUCT_TYPE,
                "marginCoin":   MARGIN_COIN,
                "planType":     "profit_plan",
                "triggerPrice": _fmt_price(tp1),
                "triggerType":  "mark_price",
                "executePrice": "0",
                "holdSide":     hold_side,
                "size":         _fmt_qty(half_qty),
            })
            if resp.get("code") == "00000":
                tp1_id = resp["data"]["orderId"]
                print(f"[exchange] #{sid} TP1 order: {tp1_id} | {_fmt_qty(half_qty)} SOL @ {tp1}")
            else:
                log.error(f"[exchange] #{sid} place TP1: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place TP1: {e}")

    if tp2 is not None:
        try:
            resp = client.post("/api/v2/mix/order/place-tpsl-order", {
                "symbol":       SYMBOL,
                "productType":  PRODUCT_TYPE,
                "marginCoin":   MARGIN_COIN,
                "planType":     "profit_plan",
                "triggerPrice": _fmt_price(tp2),
                "triggerType":  "mark_price",
                "executePrice": "0",
                "holdSide":     hold_side,
                "size":         _fmt_qty(half_qty),
            })
            if resp.get("code") == "00000":
                tp2_id = resp["data"]["orderId"]
                print(f"[exchange] #{sid} TP2 order: {tp2_id} | {_fmt_qty(half_qty)} SOL @ {tp2}")
            else:
                log.error(f"[exchange] #{sid} place TP2: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place TP2: {e}")

    if sl is not None:
        try:
            resp = client.post("/api/v2/mix/order/place-tpsl-order", {
                "symbol":       SYMBOL,
                "productType":  PRODUCT_TYPE,
                "marginCoin":   MARGIN_COIN,
                "planType":     "loss_plan",
                "triggerPrice": _fmt_price(sl),
                "triggerType":  "mark_price",
                "executePrice": "0",
                "holdSide":     hold_side,
                "size":         _fmt_qty(full_qty),
            })
            if resp.get("code") == "00000":
                sl_id = resp["data"]["orderId"]
                print(f"[exchange] #{sid} SL order:  {sl_id} | {_fmt_qty(full_qty)} SOL @ {sl}")
            else:
                log.error(f"[exchange] #{sid} place SL: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place SL: {e}")

    return tp1_id, tp2_id, sl_id


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _plan_order_status(client: BitgetClient, order_id: str) -> str:
    """
    Status plan ordera (normal_plan): 'live' | 'executed' | 'cancelled' | 'unknown'
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


def _tpsl_order_status(client: BitgetClient, order_id: str) -> str:
    """
    Status zlecenia TPSL (profit_plan lub loss_plan): 'live' | 'executed' | 'cancelled' | 'unknown'
    Bitget używa planType='profit_loss' do odpytywania obu typów razem.
    """
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "profit_loss",
        })
        if resp.get("code") == "00000":
            live_ids = {o["orderId"] for o in resp["data"].get("entrustedList", [])}
            if order_id in live_ids:
                return "live"

        resp = client.get("/api/v2/mix/order/orders-plan-history", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "profit_loss",
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


# ── Anulowanie i modyfikacja zleceń ───────────────────────────────────────────

def _cancel_order(client: BitgetClient, order_id: str, plan_type: str):
    """Anuluje zlecenie plan/tpsl po orderId."""
    try:
        resp = client.post("/api/v2/mix/order/cancel-plan-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "orderId":     order_id,
            "planType":    plan_type,
        })
        if resp.get("code") == "00000":
            print(f"[exchange] Anulowano {plan_type} order {order_id}")
        else:
            log.warning(f"[exchange] cancel {order_id}: code={resp.get('code')} msg={resp.get('msg')}")
    except Exception as e:
        log.warning(f"[exchange] cancel {order_id}: {e}")


def _modify_sl(client: BitgetClient, sl_order_id: str, new_price: float, new_qty: float):
    """
    Modyfikuje istniejący SL order: nowa cena triggera i nowy rozmiar.
    Używane po TP1 — przesunięcie SL na SLpoTP1 i zmniejszenie size do half_qty.
    """
    try:
        resp = client.post("/api/v2/mix/order/modify-tpsl-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginCoin":   MARGIN_COIN,
            "orderId":      sl_order_id,
            "triggerPrice": _fmt_price(new_price),
            "triggerType":  "mark_price",
            "size":         _fmt_qty(new_qty),
        })
        if resp.get("code") == "00000":
            print(f"[exchange] SL {sl_order_id} zmodyfikowany → {new_price}, size={_fmt_qty(new_qty)} SOL")
        else:
            log.warning(f"[exchange] modify_sl {sl_order_id}: code={resp.get('code')} msg={resp.get('msg')}")
    except Exception as e:
        log.warning(f"[exchange] modify_sl {sl_order_id}: {e}")


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana przez exchange_sync.yml co 5 minut (lub ręcznie).

    Stany setupu:
      NOWY            → składa plan order przy W1
      PLAN ZŁOŻONY    → sprawdza czy entry wykonany → składa TPSL
      TPSL ZŁOŻONE    → monitoruje:
                          SL executed  → anuluj TP1, TP2
                          TP1 executed → zmodyfikuj SL (size=half, trigger=SLpoTP1)
                          TP2 executed → anuluj SL
      ANULOWANY       → anuluje plan order jeśli jeszcze nie wykonany
    """
    client = _client()
    if client is None:
        return

    _set_hedge_mode(client)
    _set_leverage(client)

    pending  = _load_pending()
    modified = False

    # Guard: tylko jedna pozycja na raz w Bitget.
    # Jeśli jakikolwiek setup ma już plan order lub otwartą pozycję → blokuj nowe.
    exchange_slot_taken = any(
        (s.get("exchange_plan_oid") or s.get("exchange_position_opened"))
        and not s.get("exchange_done", False)
        for s in pending
    )
    if exchange_slot_taken:
        active = next(
            s for s in pending
            if (s.get("exchange_plan_oid") or s.get("exchange_position_opened"))
            and not s.get("exchange_done", False)
        )
        print(f"[exchange] Slot zajęty przez setup #{active.get('setup_id','?')} — nowe pozycje wstrzymane.")

    for s in pending:
        sid       = s.get("setup_id", "?")
        direction = s.get("direction", "?")
        model     = s.get("model", "?")
        entries   = s.get("entries", [])
        shadow    = s.get("shadow", False)
        cancelled = bool(s.get("cancel_reason"))
        label     = f"#{sid} [{model}] {direction.upper()}"

        if not entries:
            continue

        # Pola stanu exchange (nowa architektura)
        plan_oid  = s.get("exchange_plan_oid")      # entry plan order
        tp1_oid   = s.get("exchange_tp1_oid")        # TP1 tpsl order
        tp2_oid   = s.get("exchange_tp2_oid")        # TP2 tpsl order
        sl_oid    = s.get("exchange_sl_oid")         # SL tpsl order
        pos_open  = s.get("exchange_position_opened", False)
        tp1_done  = s.get("exchange_tp1_done", False)
        ex_done   = s.get("exchange_done", False)

        if ex_done:
            continue

        # ── Anuluj gdy setup odrzucony przed wejściem ─────────────────────────
        if (shadow or cancelled) and plan_oid and not pos_open:
            print(f"[exchange] {label}: anulowany → cancel plan order {plan_oid}")
            _cancel_order(client, plan_oid, "normal_plan")
            s["exchange_plan_oid"] = None
            s["exchange_done"]     = True
            modified = True
            continue

        # ── NOWY setup — złóż plan order przy W1 ─────────────────────────────
        if not shadow and not cancelled and not plan_oid and s.get("entry_hit_at") is None:
            if exchange_slot_taken:
                print(f"[exchange] {label}: pominięty — slot zajęty (tryb jedna pozycja na raz)")
                continue
            w1       = entries[0]
            full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid      = _place_entry_plan_order(client, s, full_qty)
            if oid:
                s["exchange_plan_oid"]        = oid
                s["exchange_qty_full"]        = _fmt_qty(full_qty)
                s["exchange_qty_half"]        = _fmt_qty(_round_qty(full_qty / 2))
                s["exchange_position_opened"] = False
                modified = True
                print(f"[exchange] {label}: plan order złożony ({_fmt_qty(full_qty)} SOL @ W1={w1})")
            continue

        # ── Plan order złożony, pozycja jeszcze nie otwarta ───────────────────
        if plan_oid and not pos_open:
            status = _plan_order_status(client, plan_oid)
            print(f"[exchange] {label}: plan order status = {status}")

            if status == "cancelled":
                print(f"[exchange] {label}: plan order anulowany z zewnątrz")
                s["exchange_plan_oid"] = None
                modified = True

            elif status == "executed":
                # Entry hit — składaj TPSL ordery
                full_qty = float(s.get("exchange_qty_full", "0").replace(",", ".") or "0")
                half_qty = float(s.get("exchange_qty_half", "0").replace(",", ".") or "0")
                if full_qty <= 0:
                    w1       = entries[0]
                    full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
                    half_qty = _round_qty(full_qty / 2)

                tp1_id, tp2_id, sl_id = _place_tpsl_orders(client, s, full_qty, half_qty)
                s["exchange_position_opened"] = True
                s["exchange_tp1_oid"]         = tp1_id
                s["exchange_tp2_oid"]         = tp2_id
                s["exchange_sl_oid"]          = sl_id
                modified = True
                print(f"[exchange] {label}: pozycja otwarta, TPSL złożone "
                      f"(TP1={tp1_id} TP2={tp2_id} SL={sl_id})")
            continue

        # ── Pozycja otwarta — monitoruj TPSL ─────────────────────────────────
        if pos_open and (tp1_oid or tp2_oid or sl_oid):

            # Sprawdź SL jako pierwsze — ma priorytet
            if sl_oid:
                sl_status = _tpsl_order_status(client, sl_oid)
                print(f"[exchange] {label}: SL status = {sl_status}")

                if sl_status == "executed":
                    print(f"[exchange] {label}: SL wykonany — anuluj TP1 i TP2")
                    for oid in filter(None, [tp1_oid, tp2_oid]):
                        _cancel_order(client, oid, "profit_plan")
                    s["exchange_sl_oid"]  = None
                    s["exchange_tp1_oid"] = None
                    s["exchange_tp2_oid"] = None
                    s["exchange_done"]    = True
                    modified = True
                    continue

            # Sprawdź TP1 (jeśli jeszcze nie wykonany)
            if tp1_oid and not tp1_done:
                tp1_status = _tpsl_order_status(client, tp1_oid)
                print(f"[exchange] {label}: TP1 status = {tp1_status}")

                if tp1_status == "executed":
                    # Przesuń SL na SLpoTP1 i zmniejsz size do half_qty
                    new_sl    = s.get("sl_after_tp1") or s.get("entries", [0])[0]
                    half_qty  = float(s.get("exchange_qty_half", "0").replace(",", ".") or "0")
                    if sl_oid and half_qty > 0:
                        _modify_sl(client, sl_oid, new_sl, half_qty)
                    s["exchange_tp1_oid"]    = None
                    s["exchange_tp1_done"]   = True
                    modified = True
                    print(f"[exchange] {label}: TP1 wykonany — SL przesunięty na {new_sl}")

            # Sprawdź TP2
            if tp2_oid:
                tp2_status = _tpsl_order_status(client, tp2_oid)
                print(f"[exchange] {label}: TP2 status = {tp2_status}")

                if tp2_status == "executed":
                    print(f"[exchange] {label}: TP2 wykonany — anuluj SL")
                    if sl_oid:
                        _cancel_order(client, sl_oid, "loss_plan")
                    s["exchange_tp2_oid"] = None
                    s["exchange_sl_oid"]  = None
                    s["exchange_done"]    = True
                    modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
