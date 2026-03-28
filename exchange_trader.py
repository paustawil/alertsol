#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)
Wywoływany przez sol_alert.py co 15 minut.

Schemat działania:
  1. Nowy setup → 2 plan ordery przy W1 (każdy 50% pozycji):
       Plan A: preset TP=TP1 + preset SL
       Plan B: preset TP=TP2 + preset SL
  2. Entry wbity → Bitget aktywuje preset TP/SL na obu planach automatycznie
  3. TP1 wykonany → anuluj wszystkie aktywne pos_loss, postaw BE SL (sl_after_tp1)
  4. TP2 lub BE SL zamykają resztę

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


# ── Tryb pozycji i dźwignia ───────────────────────────────────────────────────

def _set_hedge_mode(client: BitgetClient):
    """Ustawia hedge mode — long i short mogą być otwarte równocześnie."""
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

def _place_plan_order(
    client: BitgetClient,
    s: dict,
    qty: float,
    preset_tp: float | None,
    preset_sl: float | None,
    label: str = "",
) -> str | None:
    """Plan order przy W1 z opcjonalnym preset TP i SL. Zwraca orderId."""
    direction = s["direction"]
    w1        = s["entries"][0]
    side      = "buy" if direction == "long" else "sell"

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
    if preset_tp is not None:
        params["presetStopSurplusPrice"] = _fmt_price(preset_tp)
    if preset_sl is not None:
        params["presetStopLossPrice"] = _fmt_price(preset_sl)

    try:
        resp = client.post("/api/v2/mix/order/place-plan-order", params)
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] Plan order {label}: {oid} | {side} {_fmt_qty(qty)} SOL @ {w1}"
                  f" | TP={preset_tp} SL={preset_sl}")
            return oid
        log.error(f"[exchange] place_plan_order {label}: {resp.get('msg')}")
    except Exception as e:
        log.error(f"[exchange] place_plan_order {label}: {e}")
    return None


def _place_two_plan_orders(client: BitgetClient, s: dict) -> tuple[str | None, str | None]:
    """
    Składa dwa plan ordery przy W1 — każdy po 50% pozycji:
      Plan A: preset TP=TP1 + preset SL
      Plan B: preset TP=TP2 + preset SL
    Zwraca (oid_tp1, oid_tp2).
    """
    w1       = s["entries"][0]
    full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    half_qty = _round_qty(full_qty / 2)
    tps      = s.get("tps", [])
    sl       = s.get("sl")
    tp1      = tps[0] if len(tps) > 0 else None
    tp2      = tps[1] if len(tps) > 1 else None

    oid_a = _place_plan_order(client, s, half_qty, tp1, sl, label="A(TP1)")
    oid_b = _place_plan_order(client, s, half_qty, tp2, sl, label="B(TP2)")
    return oid_a, oid_b



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


def _find_active_pos_loss_ids(client: BitgetClient) -> list[str]:
    """Zwraca ID wszystkich aktywnych zleceń pos_loss dla SOLUSDT."""
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "pos_loss",
        })
        if resp.get("code") == "00000":
            return [o["orderId"] for o in resp["data"].get("entrustedList", [])]
    except Exception as e:
        log.warning(f"[exchange] find_active_pos_loss: {e}")
    return []


def _modify_tpsl_order(client: BitgetClient, order_id: str, new_price: float):
    """Modyfikuje triggerPrice istniejącego zlecenia tpsl (pos_profit lub pos_loss)."""
    try:
        resp = client.post("/api/v2/mix/order/modify-tpsl-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginCoin":   MARGIN_COIN,
            "orderId":      order_id,
            "triggerPrice": _fmt_price(new_price),
        })
        if resp.get("code") == "00000":
            print(f"[exchange] TPSL order {order_id} zaktualizowany → {new_price}")
        else:
            log.warning(f"[exchange] modify_tpsl_order {order_id}: {resp.get('msg')}")
    except Exception as e:
        log.warning(f"[exchange] modify_tpsl_order {order_id}: {e}")


def _update_sl(client: BitgetClient, s: dict, new_sl: float):
    """Po TP1: modyfikuje aktywne preset SL Planu B na BE (sl_after_tp1)."""
    sl_ids = _find_active_pos_loss_ids(client)
    if sl_ids:
        for sl_id in sl_ids:
            _modify_tpsl_order(client, sl_id, new_sl)
        be_label = "BE" if abs(new_sl - s["entries"][0]) < 0.05 else f"@ {new_sl}"
        print(f"[exchange] SL przesunięty po TP1 → {new_sl} ({be_label})")
    else:
        log.warning("[exchange] _update_sl: brak aktywnych pos_loss do zaktualizowania.")


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



# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY:      składa 2 plan ordery (TP1 leg + TP2 leg, każdy 50%)
      - ANULOWANY: anuluje oba plan ordery
      - OCZEKUJĄCY: sprawdza czy entry wykonany → oznacza pozycję jako otwartą
      - OTWARTY:   wykrywa TP1 → anuluje aktywne SL, stawia BE SL
    """
    client = _client()
    if client is None:
        return

    _set_hedge_mode(client)
    _set_leverage(client)

    pending  = _load_pending()
    modified = False

    for s in pending:
        sid       = s.get("setup_id", "?")
        model     = s.get("model", "?")
        direction = s.get("direction", "?")
        shadow    = s.get("shadow", False)
        cancelled = bool(s.get("cancel_reason"))
        opened    = s.get("exchange_position_opened", False)
        entries   = s.get("entries", [])

        if not entries:
            continue

        label        = f"#{sid} [{model}] {direction.upper()}"
        order_id_tp1 = s.get("exchange_order_id_tp1")
        order_id_tp2 = s.get("exchange_order_id_tp2")
        any_order_id = order_id_tp1 or order_id_tp2

        # ── Anuluj gdy setup odrzucony ────────────────────────────────────────
        if (shadow or cancelled) and any_order_id and not opened:
            for oid in filter(None, [order_id_tp1, order_id_tp2]):
                print(f"[exchange] {label}: anulowany → cancel plan order {oid}")
                _cancel_tpsl_order(client, oid)
            s["exchange_order_id_tp1"] = None
            s["exchange_order_id_tp2"] = None
            modified = True
            continue

        # ── Nowy setup — złóż 2 plan ordery (TP1 leg + TP2 leg) ─────────────
        if not shadow and not cancelled and not any_order_id and s["entry_hit_at"] is None:
            w1       = entries[0]
            full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid_a, oid_b = _place_two_plan_orders(client, s)
            if oid_a or oid_b:
                s["exchange_order_id_tp1"]    = oid_a
                s["exchange_order_id_tp2"]    = oid_b
                s["exchange_position_opened"] = False
                s["exchange_qty"]             = _fmt_qty(full_qty)
                modified = True
                print(f"[exchange] {label}: 2 plan ordery złożone (TP1={oid_a}, TP2={oid_b})")
            continue

        # ── Oczekujący — sprawdź czy entry wykonany ──────────────────────────
        # Preset TP/SL aktywują się automatycznie — nie trzeba składać dodatkowych zleceń.
        if any_order_id and not opened:
            # Wystarczy że jeden z planów wykonany → pozycja otwarta
            statuses = {
                oid: _plan_order_status(client, oid)
                for oid in filter(None, [order_id_tp1, order_id_tp2])
            }
            if all(st == "cancelled" for st in statuses.values()):
                print(f"[exchange] {label}: oba plan ordery anulowane z zewnątrz")
                s["exchange_order_id_tp1"] = None
                s["exchange_order_id_tp2"] = None
                modified = True
                continue
            if any(st == "executed" for st in statuses.values()):
                print(f"[exchange] {label}: entry wykonany — pozycja otwarta z preset TP/SL.")
                s["exchange_position_opened"] = True
                modified = True
            continue

        # ── Pozycja otwarta — po TP1 przesuń SL na BE ────────────────────────
        if opened and not s.get("sl_adjusted"):
            # tp1_hit_at ustawiany przez check_pending w sol_alert.py
            if s.get("tp1_hit_at"):
                w1       = entries[0]
                sl_after = s.get("sl_after_tp1") or w1
                _update_sl(client, s, sl_after)
                s["sl_adjusted"] = True
                modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
