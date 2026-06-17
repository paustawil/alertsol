#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)

Flow dla każdego setupu:
  1a. Aggressive setup → natychmiastowy market order (full_qty), potem TPSL ręcznie
  1b. Pozostałe → 2 plan ordery przy W1, każdy half_qty z preset TP i SL:
       Plan 1: half_qty, trigger=W1, preset TP=TP1, preset SL=SL
       Plan 2: half_qty, trigger=W1, preset TP=TP2, preset SL=SL
  2. Oba plan ordery wykonane → Bitget tworzy 4 TPSL ordery:
       TP1: profit_plan, half_qty, trigger=TP1  → exchange_tp1_oid
       TP2: profit_plan, half_qty, trigger=TP2  → exchange_tp2_oid
       SL1: loss_plan,   half_qty, trigger=SL   → exchange_sl_oid   (dla pary TP1)
       SL2: loss_plan,   half_qty, trigger=SL   → exchange_sl2_oid  (dla pary TP2)
  3. Monitoring TPSL:
       SL1 lub SL2 wykonany → anuluj TP1, TP2; SL2/SL1 auto-zamyka resztę
       TP1 wykonany → anuluj SL1; zmodyfikuj SL2: trigger=entry (BE)
       TP2 wykonany (po TP1) → anuluj SL2 (BE); resolve TP1+TP2
       SL2-BE wykonany (po TP1) → resolve TP1+BE

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
import threading
import requests
import db

log = logging.getLogger("exchange")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Konfiguracja ───────────────────────────────────────────────────────────────
SYMBOL       = "SOLUSDT"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN  = "USDT"
MARGIN_MODE  = "crossed"
LEVERAGE     = int(os.getenv("BITGET_LEVERAGE", "20"))
TRADE_USDT    = float(os.getenv("BITGET_TRADE_USDT") or "100.0")
MAX_POSITIONS = int(os.getenv("BITGET_MAX_POSITIONS") or "5")
QTY_STEP     = 0.1
PRICE_DEC    = 2
BASE_URL     = "https://api.bitget.com"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_effective_trade_params(setup_type: str, variant: str | None) -> tuple[float, int, bool, str | None]:
    """Zwraca (trade_usdt, leverage, enabled, tp_strategy) dla danego type+variant.
    tp_strategy=None oznacza 'użyj wartości z setupu (domyślna algorytmu)'."""
    try:
        settings = db.get_app_settings()
        base_usdt = float(settings.get("trade_usdt") or TRADE_USDT)
        base_lev  = int(settings.get("leverage") or LEVERAGE)
        key = f"{setup_type}__{variant or 'baseline'}"
        cfg = (settings.get("type_configs") or {}).get(key, {})
        enabled     = cfg.get("enabled", False)
        eff_usdt    = float(cfg["trade_usdt"]) if cfg.get("trade_usdt") else base_usdt
        eff_lev     = int(cfg["leverage"])     if cfg.get("leverage")   else base_lev
        tp_strategy = cfg.get("tp_strategy") or None
        return eff_usdt, eff_lev, bool(enabled), tp_strategy
    except Exception as e:
        log.warning(f"[exchange] _get_effective_trade_params błąd: {e}")
        return TRADE_USDT, LEVERAGE, True, None


def _round_qty(qty: float) -> float:
    return max(math.floor(qty / QTY_STEP) * QTY_STEP, QTY_STEP)

def _fmt_qty(qty: float) -> str:
    return f"{qty:.1f}"

def _fmt_price(p: float) -> str:
    return f"{p:.{PRICE_DEC}f}"

def _load_pending() -> list[dict]:
    return db.load_pending()

def _save_pending(pending: list[dict]):
    db.save_pending_list(pending)


# ── Pobieranie aktualnej pozycji ──────────────────────────────────────────────

def _get_open_position_size(client: "BitgetClient", hold_side: str) -> float:
    """
    Zwraca rzeczywisty rozmiar otwartej pozycji dla danego hold_side ('long'/'short').
    Odpytuje Bitget bezpośrednio — służy do weryfikacji po wykonaniu plan ordera.
    Zwraca 0.0 jeśli brak pozycji lub błąd.
    """
    try:
        resp = client.get("/api/v2/mix/position/all-position", {
            "productType": PRODUCT_TYPE,
            "marginCoin":  MARGIN_COIN,
        })
        if resp.get("code") == "00000":
            for pos in (resp.get("data") or []):
                if (pos.get("symbol") == SYMBOL
                        and pos.get("holdSide") == hold_side):
                    return float(pos.get("total") or 0)
    except Exception as e:
        log.warning(f"[exchange] get_position_size({hold_side}): {e}")
    return 0.0


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


def transfer_futures_to_spot(amount: float) -> dict:
    """Przelewa `amount` USDT z konta Futures (mix_usdt) na Spot.
    Zwraca {'ok': True} lub {'ok': False, 'error': str}."""
    client = _client()
    if client is None:
        return {"ok": False, "error": "brak klucza API"}
    if amount <= 0:
        return {"ok": False, "error": f"kwota musi być > 0 (otrzymano {amount})"}
    try:
        resp = client.post("/api/v2/spot/wallet/transfer", {
            "fromType":  "mix_usdt",
            "toType":    "spot",
            "amount":    str(round(amount, 2)),
            "coin":      MARGIN_COIN,
            "clientOid": f"weekly_transfer_{int(time.time())}",
        })
        if resp.get("code") == "00000":
            log.info(f"[transfer] Przelano {amount:.2f} USDT Futures→Spot. transferId={resp.get('data', {}).get('transferId')}")
            return {"ok": True, "transfer_id": resp.get("data", {}).get("transferId")}
        else:
            msg = resp.get("msg") or str(resp)
            log.warning(f"[transfer] Błąd Bitget: {msg}")
            return {"ok": False, "error": msg}
    except Exception as e:
        log.exception(f"[transfer] Wyjątek: {e}")
        return {"ok": False, "error": str(e)}


def get_account_balance() -> float | None:
    """Zwraca equity konta futures USDT (całkowita wartość konta, bez odjęcia marginu).
    Używamy equity, nie available — bo committed_db już odejmuje zaangażowany kapitał."""
    client = _client()
    if client is None:
        return None
    try:
        resp = client.get("/api/v2/mix/account/account", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginCoin":  MARGIN_COIN,
        })
        if resp.get("code") == "00000":
            data = resp.get("data") or {}
            equity = data.get("equity") or data.get("usdtEquity") or data.get("available")
            if equity is not None:
                return float(equity)
    except Exception as e:
        log.warning(f"[exchange] get_account_balance: {e}")
    return None


# ── Składanie zleceń ───────────────────────────────────────────────────────────

def _place_entry_plan_orders(
    client: BitgetClient, s: dict, half_qty: float
) -> tuple[str | None, str | None]:
    """
    Składa plan ordery przy W1:
    - tp1_only: JEDEN order full_qty z preset TP=TP1 i SL
    - tp1_tp2 (domyślnie): DWA ordery half_qty — Plan1@TP1, Plan2@TP2

    Po triggerze każdego plan order Bitget automatycznie tworzy odpowiednie
    TPSL ordery (profit_plan + loss_plan) dla tej połowy pozycji.

    Zwraca (plan1_oid, plan2_oid) lub (None, None) przy błędzie.
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    side      = "buy" if direction == "long" else "sell"
    tps       = s.get("tps", [])
    tp1       = tps[0] if len(tps) > 0 else None
    tp2       = tps[1] if len(tps) > 1 else None
    sl        = s.get("sl")
    sid       = s.get("setup_id", "?")

    def _place_one(tp: float | None, label: str) -> str | None:
        params = {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginMode":   MARGIN_MODE,
            "marginCoin":   MARGIN_COIN,
            "planType":     "normal_plan",
            "size":         _fmt_qty(half_qty),
            "triggerPrice": _fmt_price(w1),
            "triggerType":  "mark_price",
            "side":         side,
            "tradeSide":    "open",
            "posSide":      direction,
            "orderType":    "market",
        }
        if tp is not None:
            params["stopSurplusTriggerPrice"] = _fmt_price(tp)
            params["stopSurplusTriggerType"]  = "mark_price"
        if sl is not None:
            params["stopLossTriggerPrice"] = _fmt_price(sl)
            params["stopLossTriggerType"]  = "mark_price"
        try:
            resp = client.post("/api/v2/mix/order/place-plan-order", params)
            if resp.get("code") == "00000":
                oid = resp["data"]["orderId"]
                print(f"[exchange] #{sid} Plan {label}: {oid} | {side} {_fmt_qty(half_qty)} SOL"
                      f" @ W1={w1} | TP={tp} SL={sl}")
                return oid
            log.error(f"[exchange] #{sid} place plan {label}: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place plan {label}: {e}")
        return None

    if s.get("tp_strategy") == "tp1_only":
        # Jeden order full_qty na TP1 — brak podziału na połowy
        full_qty_single = half_qty * 2
        params_full = {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginMode":   MARGIN_MODE,
            "marginCoin":   MARGIN_COIN,
            "planType":     "normal_plan",
            "size":         _fmt_qty(full_qty_single),
            "triggerPrice": _fmt_price(s["entries"][0]),
            "triggerType":  "mark_price",
            "side":         "buy" if s["direction"] == "long" else "sell",
            "tradeSide":    "open",
            "posSide":      s["direction"],
            "orderType":    "market",
        }
        if tp1 is not None:
            params_full["stopSurplusTriggerPrice"] = _fmt_price(tp1)
            params_full["stopSurplusTriggerType"]  = "mark_price"
        sl = s.get("sl")
        if sl is not None:
            params_full["stopLossTriggerPrice"] = _fmt_price(sl)
            params_full["stopLossTriggerType"]  = "mark_price"
        try:
            resp = client.post("/api/v2/mix/order/place-plan-order", params_full)
            if resp.get("code") == "00000":
                oid = resp["data"]["orderId"]
                print(f"[exchange] #{sid} Plan 1(TP1-only): {oid} | {_fmt_qty(full_qty_single)} SOL @ TP1={tp1}")
                return oid, None
            log.error(f"[exchange] #{sid} place plan tp1_only: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place plan tp1_only: {e}")
        return None, None

    plan1_oid = _place_one(tp1, "1(TP1)")
    if not plan1_oid:
        return None, None

    plan2_oid = _place_one(tp2, "2(TP2)")
    if not plan2_oid:
        # Cofnij plan1 żeby nie zostawić samotnego half-qty plan order
        log.error(f"[exchange] #{sid} plan2 nieudany — anuluję plan1 {plan1_oid}")
        _cancel_order(client, plan1_oid, "normal_plan")
        return None, None

    return plan1_oid, plan2_oid


def _place_market_entry(
    client: "BitgetClient",
    s: dict,
    trade_usdt: float | None = None,
    leverage: int | None = None,
) -> bool:
    """Market order open + natychmiastowe TPSL dla aggressive setups.
    Zwraca True przy sukcesie i ustawia pola exchange_* na s."""
    direction = s["direction"]
    entries   = s.get("entries", [])
    w1        = entries[0] if entries else 0
    side      = "buy" if direction == "long" else "sell"
    sid       = s.get("setup_id", "?")

    eff_usdt = trade_usdt if trade_usdt is not None else TRADE_USDT
    eff_lev  = leverage   if leverage   is not None else LEVERAGE
    full_qty = _round_qty((eff_usdt * eff_lev) / w1)
    half_qty = _round_qty(full_qty / 2)

    try:
        resp = client.post("/api/v2/mix/order/place-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginCoin":  MARGIN_COIN,
            "side":        side,
            "tradeSide":   "open",
            "posSide":     direction,
            "orderType":   "market",
            "size":        _fmt_qty(full_qty),
        })
    except Exception as e:
        log.error(f"[exchange] #{sid} market entry: {e}")
        return False

    if resp.get("code") != "00000":
        log.error(f"[exchange] #{sid} market entry: code={resp.get('code')} msg={resp.get('msg')}")
        return False

    print(f"[exchange] #{sid} market entry: {_fmt_qty(full_qty)} SOL {side.upper()} (aggressive)")

    actual_qty = _get_open_position_size(client, direction)
    if actual_qty <= 0:
        log.error(f"[exchange] #{sid} market entry: pozycja=0 po market order")
        return False

    tp1_id, tp2_id, sl1_id, sl2_id = _place_tpsl_orders_split(client, s, half_qty)

    s["exchange_position_opened"] = True
    s["exchange_plan_oid"]        = None
    s["exchange_qty_full"]        = _fmt_qty(full_qty)
    s["exchange_qty_half"]        = _fmt_qty(half_qty)
    s["exchange_tp1_oid"]         = tp1_id
    s["exchange_tp2_oid"]         = tp2_id
    s["exchange_sl_oid"]          = sl1_id
    s["exchange_sl2_oid"]         = sl2_id
    return True


def _place_tpsl_orders_split(
    client: BitgetClient,
    s: dict,
    half_qty: float,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Fallback: ręcznie składa 4 TPSL ordery gdy preset z plan order nie zadziałał.
      TP1: profit_plan, half_qty, trigger=TP1  → tp1_id
      TP2: profit_plan, half_qty, trigger=TP2  → tp2_id
      SL1: loss_plan,   half_qty, trigger=SL   → sl1_id  (para z TP1)
      SL2: loss_plan,   half_qty, trigger=SL   → sl2_id  (para z TP2, zmodyfikowany na BE po TP1)
    Zwraca (tp1_id, tp2_id, sl1_id, sl2_id).
    """
    direction = s["direction"]
    hold_side = direction
    tps       = s.get("tps", [])
    tp1       = tps[0] if len(tps) > 0 else None
    tp2       = tps[1] if len(tps) > 1 else None
    sl        = s.get("sl")
    sid       = s.get("setup_id", "?")

    tp1_id = tp2_id = sl1_id = sl2_id = None

    def _place_tp(price, label):
        if price is None:
            return None
        try:
            resp = client.post("/api/v2/mix/order/place-tpsl-order", {
                "symbol":       SYMBOL,
                "productType":  PRODUCT_TYPE,
                "marginCoin":   MARGIN_COIN,
                "planType":     "profit_plan",
                "triggerPrice": _fmt_price(price),
                "triggerType":  "mark_price",
                "executePrice": "0",
                "holdSide":     hold_side,
                "size":         _fmt_qty(half_qty),
            })
            if resp.get("code") == "00000":
                oid = resp["data"]["orderId"]
                print(f"[exchange] #{sid} {label}: {oid} | {_fmt_qty(half_qty)} SOL @ {price}")
                return oid
            log.error(f"[exchange] #{sid} place {label}: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place {label}: {e}")
        return None

    def _place_sl(label):
        if sl is None:
            return None
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
                "size":         _fmt_qty(half_qty),
            })
            if resp.get("code") == "00000":
                oid = resp["data"]["orderId"]
                print(f"[exchange] #{sid} {label}: {oid} | {_fmt_qty(half_qty)} SOL @ {sl}")
                return oid
            log.error(f"[exchange] #{sid} place {label}: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place {label}: {e}")
        return None

    if s.get("tp_strategy") == "tp1_only":
        # Jeden TP i jeden SL na full_qty zamiast split
        full_qty_single = half_qty * 2

        def _place_tp_full(price, label):
            if price is None:
                return None
            try:
                resp = client.post("/api/v2/mix/order/place-tpsl-order", {
                    "symbol":       SYMBOL,
                    "productType":  PRODUCT_TYPE,
                    "marginCoin":   MARGIN_COIN,
                    "planType":     "profit_plan",
                    "triggerPrice": _fmt_price(price),
                    "triggerType":  "mark_price",
                    "executePrice": "0",
                    "holdSide":     hold_side,
                    "size":         _fmt_qty(full_qty_single),
                })
                if resp.get("code") == "00000":
                    oid = resp["data"]["orderId"]
                    print(f"[exchange] #{sid} {label}(full): {oid} | {_fmt_qty(full_qty_single)} SOL @ {price}")
                    return oid
                log.error(f"[exchange] #{sid} place {label}: code={resp.get('code')} msg={resp.get('msg')}")
            except Exception as e:
                log.error(f"[exchange] #{sid} place {label}: {e}")
            return None

        def _place_sl_full(label):
            if sl is None:
                return None
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
                    "size":         _fmt_qty(full_qty_single),
                })
                if resp.get("code") == "00000":
                    oid = resp["data"]["orderId"]
                    print(f"[exchange] #{sid} {label}(full): {oid} | {_fmt_qty(full_qty_single)} SOL @ {sl}")
                    return oid
                log.error(f"[exchange] #{sid} place {label}: code={resp.get('code')} msg={resp.get('msg')}")
            except Exception as e:
                log.error(f"[exchange] #{sid} place {label}: {e}")
            return None

        return _place_tp_full(tp1, "TP1"), None, _place_sl_full("SL1"), None

    tp1_id = _place_tp(tp1, "TP1")
    tp2_id = _place_tp(tp2, "TP2")
    sl1_id = _place_sl("SL1")
    sl2_id = _place_sl("SL2")
    return tp1_id, tp2_id, sl1_id, sl2_id


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
            live_ids = {o["orderId"] for o in (resp["data"].get("entrustedList") or [])}
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
            for o in (resp["data"].get("entrustedList") or []):
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
            live_ids = {o["orderId"] for o in (resp["data"].get("entrustedList") or [])}
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
            for o in (resp["data"].get("entrustedList") or []):
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


def _modify_plan_order_size(client: BitgetClient, order_id: str, new_qty: float) -> bool:
    """Modyfikuje rozmiar istniejącego plan order (przed aktywacją)."""
    try:
        resp = client.post("/api/v2/mix/order/modify-plan-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "orderId":      order_id,
            "newSize":      _fmt_qty(new_qty),
        })
        if resp.get("code") == "00000":
            print(f"[exchange] Plan order {order_id} resized → {_fmt_qty(new_qty)} SOL")
            return True
        log.warning(f"[exchange] modify plan {order_id}: code={resp.get('code')} msg={resp.get('msg')}")
        return False
    except Exception as e:
        log.warning(f"[exchange] modify plan {order_id}: {e}")
        return False


def _resize_pending_plan_orders(client: BitgetClient, pending: list[dict], account_balance: float | None) -> bool:
    """Po zamknięciu pozycji: przelicza i aktualizuje rozmiar wszystkich oczekujących plan orderów."""
    if account_balance is None:
        return False
    modified = False
    for s in pending:
        if s.get("exchange_done", False) or s.get("shadow", False):
            continue
        plan_oid = s.get("exchange_plan_oid")
        if not plan_oid or plan_oid == "PENDING":
            continue
        if s.get("exchange_position_opened", False):
            continue
        sid = s.get("setup_id", "?")
        entries = s.get("entries", [])
        if not entries:
            continue
        w1 = entries[0]
        new_usdt = round(max(account_balance, 1.0), 2)
        settings = db.get_app_settings()
        key = f"{s.get('type', '')}__{ s.get('variant') or 'baseline'}"
        cfg = (settings.get("type_configs") or {}).get(key, {})
        eff_lev = int(cfg["leverage"]) if cfg.get("leverage") else int(settings.get("leverage") or LEVERAGE)

        new_full = _round_qty((new_usdt * eff_lev) / w1)
        new_half = _round_qty(new_full / 2)

        old_half_str = s.get("exchange_qty_half", "0").replace(",", ".")
        old_half = float(old_half_str) if old_half_str else 0
        if abs(new_half - old_half) < QTY_STEP:
            continue

        label = f"#{sid} [{s.get('model','?')}] {s.get('direction','?').upper()}"
        print(f"[exchange] {label}: resize plan orders {old_half} → {_fmt_qty(new_half)} SOL (trade_usdt {s.get('trade_usdt')} → {new_usdt})")

        ok1 = _modify_plan_order_size(client, plan_oid, new_half)
        plan2_oid = s.get("exchange_plan2_oid")
        ok2 = True
        if plan2_oid:
            ok2 = _modify_plan_order_size(client, plan2_oid, new_half)

        if ok1 and ok2:
            s["exchange_qty_full"] = _fmt_qty(new_full)
            s["exchange_qty_half"] = _fmt_qty(new_half)
            s["trade_usdt"] = new_usdt
            db.update_setup(s["setup_id"], trade_usdt=new_usdt)
            modified = True
    return modified


def _find_preset_tpsl_pair(
    client: BitgetClient,
    hold_side: str,
    tp1_price: float,
    tp2_price: float | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Szuka 4 TPSL orderów (z presetów obu plan orderów) dla danej strony pozycji.
    Rozróżnia TP1 od TP2 po cenie triggera; SL1/SL2 mają tę samą cenę — bierze pierwsze dwa.

    Zwraca (tp1_oid, tp2_oid, sl1_oid, sl2_oid).
    """
    tp1_id = tp2_id = sl1_id = sl2_id = None
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "profit_loss",
        })
        if resp.get("code") == "00000":
            for o in (resp["data"].get("entrustedList") or []):
                if o.get("posSide", o.get("holdSide", "")) != hold_side:
                    continue
                plan_type = o.get("planType", "")
                try:
                    trig = float(o.get("triggerPrice") or 0)
                except (ValueError, TypeError):
                    trig = 0.0
                if plan_type == "profit_plan":
                    if tp1_price and abs(trig - tp1_price) < tp1_price * 0.001:
                        tp1_id = o["orderId"]
                    elif tp2_price and abs(trig - tp2_price) < tp2_price * 0.001:
                        tp2_id = o["orderId"]
                elif plan_type == "loss_plan":
                    if sl1_id is None:
                        sl1_id = o["orderId"]
                    elif sl2_id is None:
                        sl2_id = o["orderId"]
    except Exception as e:
        log.warning(f"[exchange] _find_preset_tpsl_pair: {e}")
    return tp1_id, tp2_id, sl1_id, sl2_id


def _modify_sl(client: BitgetClient, sl_order_id: str, new_price: float, new_qty: float) -> bool:
    """
    Modyfikuje istniejący SL order: nowa cena triggera i nowy rozmiar.
    Używane po TP1 — przesunięcie SL na SLpoTP1 i zmniejszenie size do half_qty.
    Zwraca True jeśli modyfikacja się powiodła.
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
            return True
        else:
            log.warning(f"[exchange] modify_sl {sl_order_id}: code={resp.get('code')} msg={resp.get('msg')}")
            return False
    except Exception as e:
        log.warning(f"[exchange] modify_sl {sl_order_id}: {e}")
        return False


def _place_new_sl(
    client: BitgetClient,
    direction: str,
    price: float,
    qty: float,
    sid,
) -> str | None:
    """
    Składa nowy loss_plan TPSL order po TP1.
    Używane gdy _modify_sl się nie powiodło — np. Bitget auto-anulował stary SL2
    w momencie gdy TP1 zamknął pierwszą połowę pozycji.
    Zwraca orderId lub None przy błędzie.
    """
    try:
        resp = client.post("/api/v2/mix/order/place-tpsl-order", {
            "symbol":       SYMBOL,
            "productType":  PRODUCT_TYPE,
            "marginCoin":   MARGIN_COIN,
            "planType":     "loss_plan",
            "triggerPrice": _fmt_price(price),
            "triggerType":  "mark_price",
            "executePrice": "0",
            "holdSide":     direction,
            "size":         _fmt_qty(qty),
        })
        if resp.get("code") == "00000":
            oid = resp["data"]["orderId"]
            print(f"[exchange] #{sid} nowy SL2: {oid} | {_fmt_qty(qty)} SOL @ {price}")
            return oid
        log.error(f"[exchange] #{sid} place new SL2: code={resp.get('code')} msg={resp.get('msg')}")
    except Exception as e:
        log.error(f"[exchange] #{sid} place new SL2: {e}")
    return None


# ── Inwalidacja otwartych pozycji (wywoływana z sol_alert.py) ─────────────────

def close_open_position(setup_id: int) -> bool:
    """
    Zamknij otwartą pozycję market orderem i anuluj wszystkie zlecenia TPSL.
    Wywoływana przy inwalidacji otwartego setupu (zmiana reżimu / timeout).
    Zwraca True jeśli market close powiódł się, False przy błędzie.
    """
    client = _client()
    if client is None:
        log.warning(f"[exchange] close_open_position #{setup_id}: brak klienta")
        return False

    setups = db.get_active_setups()
    s = next((x for x in setups if x["setup_id"] == setup_id), None)
    if s is None:
        log.warning(f"[exchange] close_open_position #{setup_id}: nie znaleziono setupu")
        return False

    direction    = s.get("direction", "")
    tp1_oid      = s.get("exchange_tp1_oid")
    tp2_oid      = s.get("exchange_tp2_oid")
    sl_oid       = s.get("exchange_sl_oid")
    sl2_oid      = s.get("exchange_sl2_oid")
    full_qty_str = (s.get("exchange_qty_full") or "0").replace(",", ".")
    full_qty     = float(full_qty_str)

    if not direction or full_qty <= 0:
        log.warning(f"[exchange] close_open_position #{setup_id}: brak kierunku lub qty=0")
        return False

    # Anuluj wszystkie TPSL przed zamknięciem
    for oid, plan_type in [
        (tp1_oid, "profit_plan"), (tp2_oid, "profit_plan"),
        (sl_oid, "loss_plan"),    (sl2_oid, "loss_plan"),
    ]:
        if oid:
            _cancel_order(client, oid, plan_type)

    close_side = "sell" if direction == "long" else "buy"
    try:
        resp = client.post("/api/v2/mix/order/place-order", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "marginCoin":  MARGIN_COIN,
            "side":        close_side,
            "tradeSide":   "close",
            "posSide":     direction,
            "orderType":   "market",
            "size":        _fmt_qty(full_qty),
        })
        if resp.get("code") == "00000":
            print(f"[exchange] #{setup_id}: zamknięto pozycję market ({direction.upper()}, {_fmt_qty(full_qty)} SOL)")
            db.update_setup(setup_id,
                            exchange_done=True,
                            exchange_tp1_oid=None,
                            exchange_tp2_oid=None,
                            exchange_sl_oid=None)
            return True
        log.warning(f"[exchange] close_open_position #{setup_id}: code={resp.get('code')} msg={resp.get('msg')}")
        return False
    except Exception as e:
        log.warning(f"[exchange] close_open_position #{setup_id}: {e}")
        return False


def move_sl_to_entry(setup_id: int, new_sl_price: float) -> bool:
    """
    Przesuwa SL do ceny wejścia (break-even) dla otwartej pozycji.
    Wywoływana przy inwalidacji reżimu gdy setup jest na plusie.
    Zwraca True jeśli modyfikacja się powiodła.
    """
    client = _client()
    if client is None:
        log.warning(f"[exchange] move_sl_to_entry #{setup_id}: brak klienta")
        return False

    setups = db.get_active_setups()
    s = next((x for x in setups if x["setup_id"] == setup_id), None)
    if s is None:
        log.warning(f"[exchange] move_sl_to_entry #{setup_id}: nie znaleziono setupu")
        return False

    sl_oid       = s.get("exchange_sl_oid")
    full_qty_str = (s.get("exchange_qty_full") or "0").replace(",", ".")
    full_qty     = float(full_qty_str)

    if not sl_oid:
        log.warning(f"[exchange] move_sl_to_entry #{setup_id}: brak sl_oid — nie można zmodyfikować")
        return False

    _modify_sl(client, sl_oid, new_sl_price, full_qty)
    return True


# ── Monitoring pozycji po TP1 (wirtualna druga połowa) ───────────────────────

def _check_after_tp1_positions(client: BitgetClient) -> None:
    """
    Wirtualne śledzenie drugiej połowy pozycji dla STARYCH setupów (sprzed migracji).
    Stare setupy: exchange_done=TRUE AND status='after_tp1' — druga połowa nie jest
    na giełdzie, sprawdzamy cenę mark ręcznie.
    Nowe setupy (exchange_done=FALSE): obsługuje główna pętla _sync_inner przez TPSL ordery.
    """
    setups = [s for s in db.get_after_tp1_setups() if s.get("exchange_done")]
    if not setups:
        return

    try:
        resp = client.get("/api/v2/mix/market/ticker", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
        })
        price_data = (resp.get("data") or [{}])[0]
        current_price = float(price_data.get("markPrice") or 0)
    except Exception as e:
        log.warning(f"[exchange] after_tp1 monitor: błąd pobierania ceny — {e}")
        return

    if not current_price:
        return

    for s in setups:
        sid       = s.get("setup_id")
        direction = s.get("direction", "")
        tps       = s.get("tps") or []
        tp2       = float(tps[1]) if len(tps) > 1 else None
        sl_at_tp1 = float(s["sl_after_tp1"]) if s.get("sl_after_tp1") else None
        avg_entry = s.get("avg_entry")
        pnl_usd   = s.get("pnl_usd")
        label     = f"#{sid} {direction} after_tp1"

        if direction == "long":
            if tp2 and current_price >= tp2:
                print(f"[exchange] {label}: cena {current_price} >= TP2 {tp2} → TP1+TP2")
                db.resolve_setup(int(sid), "TP1+TP2", avg_entry, tp2, pnl_usd, None)
            elif sl_at_tp1 and current_price <= sl_at_tp1:
                print(f"[exchange] {label}: cena {current_price} <= sl@TP1 {sl_at_tp1} → TP1+BE")
                db.resolve_setup(int(sid), "TP1+BE", avg_entry, sl_at_tp1, pnl_usd, None)
        elif direction == "short":
            if tp2 and current_price <= tp2:
                print(f"[exchange] {label}: cena {current_price} <= TP2 {tp2} → TP1+TP2")
                db.resolve_setup(int(sid), "TP1+TP2", avg_entry, tp2, pnl_usd, None)
            elif sl_at_tp1 and current_price >= sl_at_tp1:
                print(f"[exchange] {label}: cena {current_price} >= sl@TP1 {sl_at_tp1} → TP1+BE")
                db.resolve_setup(int(sid), "TP1+BE", avg_entry, sl_at_tp1, pnl_usd, None)


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

_sync_lock = threading.Lock()

def sync():
    """
    Główna pętla — wywoływana przez scheduler co 15s i sol_alert.main() na końcu.

    Lock gwarantuje, że tylko jeden wątek wykonuje sync() w danym momencie.
    Bez tego wątek sol_alert (wywołujący sync() z main()) i wątek exchange_sync
    mogą jednocześnie odczytać stan setupu, a potem jeden nadpisuje zmiany drugiego
    — co prowadzi do podwójnych zleceń na Bitget.

    Stany setupu:
      NOWY            → składa plan order przy W1
      PLAN ZŁOŻONY    → sprawdza czy entry wykonany → składa TPSL
      TPSL ZŁOŻONE    → monitoruje:
                          SL executed  → anuluj TP1, TP2
                          TP1 executed → zmodyfikuj SL (size=half, trigger=SLpoTP1)
                          TP2 executed → anuluj SL
      ANULOWANY       → anuluje plan order jeśli jeszcze nie wykonany
    """
    if not _sync_lock.acquire(blocking=False):
        print("[exchange] sync() już działa w innym wątku — pomijam")
        return
    try:
        _sync_inner()
    finally:
        _sync_lock.release()


def _calc_dynamic_trade_usdt(balance: float | None, fallback: float) -> float:
    """Oblicza kwotę nowego zlecenia: 100% equity."""
    if balance is None:
        log.warning("[exchange] dynamic trade_usdt: brak balance z Bitget — fallback na ustawienia")
        return fallback
    dynamic = round(max(balance, 1.0), 2)
    log.info(f"[exchange] dynamic trade_usdt: equity={balance:.2f} → {dynamic:.2f}")
    return dynamic


def _sync_inner():
    client = _client()
    if client is None:
        return

    _set_hedge_mode(client)
    _set_leverage(client)

    # Pobierz saldo raz na cały sync — używane przy obliczaniu dynamicznego trade_usdt
    account_balance = get_account_balance()

    # Sprawdź pozycje po TP1 — wirtualna druga połowa
    _check_after_tp1_positions(client)

    pending  = _load_pending()
    modified = False

    # Guard: maksymalnie MAX_POSITIONS aktywnych pozycji na raz PER KIERUNEK.
    # Liczymy osobno long i short — pozwala na jednoczesne pozycje w obu kierunkach
    # (hedge mode na Bitget), np. range long + range short równocześnie.
    def _active_for_dir(d: str) -> int:
        return sum(
            1 for s in pending
            if s.get("exchange_position_opened")
            and s.get("direction") == d
            and not s.get("exchange_done", False)
            and not s.get("shadow", False)
            and not s.get("exchange_tp1_done", False)
        )
    active_longs  = _active_for_dir("long")
    active_shorts = _active_for_dir("short")
    if active_longs >= MAX_POSITIONS:
        print(f"[exchange] Limit LONG pozycji osiągnięty ({active_longs}/{MAX_POSITIONS}) — nowe long wstrzymane.")
    if active_shorts >= MAX_POSITIONS:
        print(f"[exchange] Limit SHORT pozycji osiągnięty ({active_shorts}/{MAX_POSITIONS}) — nowe short wstrzymane.")

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
            if plan_oid == "PENDING":
                # Rezerwacja bez realnego OID — wyczyść bez odpytywania Bitget
                s["exchange_plan_oid"] = None
                s["exchange_done"]     = True
                modified = True
                continue
            print(f"[exchange] {label}: anulowany → cancel plan order {plan_oid}")
            _cancel_order(client, plan_oid, "normal_plan")
            s["exchange_plan_oid"] = None
            s["exchange_done"]     = True
            modified = True
            continue

        # ── NOWY setup ────────────────────────────────────────────────────────
        if not shadow and not cancelled and not plan_oid and s.get("entry_hit_at") is None:
            dir_active = active_longs if direction == "long" else active_shorts
            if dir_active >= MAX_POSITIONS:
                print(f"[exchange] {label}: pominięty — limit {direction} pozycji ({dir_active}/{MAX_POSITIONS})")
                continue
            # Atomicznie zarezerwuj slot przed wywołaniem API
            if not db.claim_plan_order(s["setup_id"]):
                print(f"[exchange] {label}: plan order już zarezerwowany przez inny proces — pomijam")
                continue

            eff_usdt, eff_lev, type_enabled, eff_tp_strat = _get_effective_trade_params(
                s.get("type", ""), s.get("variant")
            )
            if not type_enabled:
                print(f"[exchange] {label}: pominięty — typ wyłączony w ustawieniach")
                db.release_plan_order_claim(s["setup_id"])
                continue
            if eff_tp_strat:
                s["tp_strategy"] = eff_tp_strat

            # Dynamiczny budżet: 100% equity
            eff_usdt = _calc_dynamic_trade_usdt(account_balance, fallback=eff_usdt)
            db.update_setup(s["setup_id"], trade_usdt=eff_usdt)

            if "aggressive" in s.get("type", ""):
                # Aggressive: market order natychmiast, bez czekania na trigger W1
                ok = _place_market_entry(client, s, trade_usdt=eff_usdt, leverage=eff_lev)
                if ok:
                    now_ts = int(time.time())
                    s["entry_hit_at"] = now_ts
                    db.update_setup(s["setup_id"], entry_hit_at=now_ts)
                    modified = True
                    print(f"[exchange] {label}: market entry sukces → pozycja otwarta")
                else:
                    db.release_plan_order_claim(s["setup_id"])
            else:
                # Pozostałe: 2 plan ordery przy W1 (half qty każdy)
                w1       = entries[0]
                full_qty = _round_qty((eff_usdt * eff_lev) / w1)
                half_qty = _round_qty(full_qty / 2)
                plan1_oid, plan2_oid = _place_entry_plan_orders(client, s, half_qty)
                if plan1_oid and plan2_oid:
                    s["exchange_plan_oid"]        = plan1_oid
                    s["exchange_plan2_oid"]       = plan2_oid
                    s["exchange_qty_full"]        = _fmt_qty(full_qty)
                    s["exchange_qty_half"]        = _fmt_qty(half_qty)
                    s["exchange_position_opened"] = False
                    modified = True
                    print(f"[exchange] {label}: 2 plan ordery złożone ({_fmt_qty(half_qty)} SOL each @ W1={w1})")
                else:
                    db.release_plan_order_claim(s["setup_id"])
            continue

        # ── Plan ordery złożone, pozycja jeszcze nie otwarta ─────────────────
        plan2_oid = s.get("exchange_plan2_oid")
        if plan_oid and not pos_open:
            if plan_oid == "PENDING":
                print(f"[exchange] {label}: stale PENDING claim — reset, next sync retry")
                s["exchange_plan_oid"] = None
                modified = True
                continue
            status1 = _plan_order_status(client, plan_oid)
            print(f"[exchange] {label}: plan1 status = {status1}")

            if status1 == "cancelled":
                print(f"[exchange] {label}: plan1 anulowany z zewnątrz — anuluję plan2 i zamykam")
                if plan2_oid:
                    _cancel_order(client, plan2_oid, "normal_plan")
                s["exchange_plan_oid"]  = None
                s["exchange_plan2_oid"] = None
                modified = True

            elif status1 == "executed":
                # Plan1 wykonany — sprawdź plan2 (ten sam trigger, powinien być executed)
                status2 = _plan_order_status(client, plan2_oid) if plan2_oid else "executed"
                print(f"[exchange] {label}: plan2 status = {status2}")

                if status2 not in ("executed", "unknown"):
                    # Plan2 jeszcze live — poczekaj na następny sync()
                    print(f"[exchange] {label}: plan2 jeszcze nie wykonany — czekam")
                    continue

                # Oba wykonane — weryfikuj pozycję
                calc_full = float(s.get("exchange_qty_full", "0").replace(",", ".") or "0")
                half_qty  = float(s.get("exchange_qty_half", "0").replace(",", ".") or "0")
                if calc_full <= 0:
                    w1        = entries[0]
                    calc_full = _round_qty((TRADE_USDT * LEVERAGE) / w1)
                    half_qty  = _round_qty(calc_full / 2)

                actual_qty = _get_open_position_size(client, direction)
                if actual_qty <= 0:
                    log.error(
                        f"[exchange] {label}: oba plan ordery wykonane ale pozycja=0 "
                        f"— traktuję jako brak wejścia"
                    )
                    s["exchange_plan_oid"]  = None
                    s["exchange_plan2_oid"] = None
                    s["exchange_done"]      = True
                    modified = True
                    continue

                full_qty = _round_qty(calc_full)
                half_qty = _round_qty(half_qty or full_qty / 2)

                # Szukaj 4 preset TPSL orderów (z obu plan orderów)
                tps      = s.get("tps", [])
                tp1_price = float(tps[0]) if len(tps) > 0 else None
                tp2_price = float(tps[1]) if len(tps) > 1 else None
                tp1_id, tp2_id, sl1_id, sl2_id = _find_preset_tpsl_pair(
                    client, direction, tp1_price, tp2_price
                )

                if tp1_id and sl1_id:
                    print(f"[exchange] {label}: preset TPSL znalezione "
                          f"(TP1={tp1_id} TP2={tp2_id} SL1={sl1_id} SL2={sl2_id})")
                else:
                    # Fallback — złóż 4 TPSL ręcznie
                    log.warning(f"[exchange] {label}: brak preset TPSL, składam ręcznie...")
                    tp1_id, tp2_id, sl1_id, sl2_id = _place_tpsl_orders_split(client, s, half_qty)

                s["exchange_position_opened"] = True
                s["exchange_qty_full"]        = _fmt_qty(full_qty)
                s["exchange_qty_half"]        = _fmt_qty(half_qty)
                s["exchange_tp1_oid"]         = tp1_id
                s["exchange_tp2_oid"]         = tp2_id
                s["exchange_sl_oid"]          = sl1_id
                s["exchange_sl2_oid"]         = sl2_id
                s["exchange_plan2_oid"]       = None   # plan ordery już wykonane
                modified = True
                print(f"[exchange] {label}: pozycja otwarta ({actual_qty} SOL) | "
                      f"TP1={tp1_id} TP2={tp2_id} SL1={sl1_id} SL2={sl2_id}")
            continue

        # ── Pozycja otwarta, brak TPSL — retry składania zleceń ──────────────
        sl2_oid = s.get("exchange_sl2_oid")
        if pos_open and not ex_done and not tp1_oid and not sl_oid and not tp1_done:
            half_qty_f = float((s.get("exchange_qty_half") or "0").replace(",", "."))
            if half_qty_f > 0:
                log.warning(f"[exchange] {label}: pozycja otwarta bez TPSL — retry składania zleceń")
                tp1_id, tp2_id, sl1_id, sl2_id = _place_tpsl_orders_split(client, s, half_qty_f)
                s["exchange_tp1_oid"] = tp1_id
                s["exchange_tp2_oid"] = tp2_id
                s["exchange_sl_oid"]  = sl1_id
                s["exchange_sl2_oid"] = sl2_id
                modified = True
            continue

        # ── Pozycja otwarta — monitoruj TPSL (faza 1: przed TP1) ─────────────
        if pos_open and not ex_done and not tp1_done:
            tp2_oid  = s.get("exchange_tp2_oid")
            sl2_oid  = s.get("exchange_sl2_oid")

            # Sprawdź SL1 jako pierwsze — pełna strata
            if sl_oid:
                sl_status = _tpsl_order_status(client, sl_oid)
                print(f"[exchange] {label}: SL1 status = {sl_status}")

                if sl_status == "executed":
                    # SL1 zamknął pierwszą połowę — anuluj TP1, TP2
                    # SL2 (ten sam trigger) powinien zamknąć drugą połowę automatycznie
                    print(f"[exchange] {label}: SL1 wykonany — anuluj TP1, TP2; SL2 zamknie resztę")
                    for oid, pt in [(tp1_oid, "profit_plan"), (tp2_oid, "profit_plan")]:
                        if oid:
                            _cancel_order(client, oid, pt)
                    s["exchange_sl_oid"]  = None
                    s["exchange_tp1_oid"] = None
                    s["exchange_tp2_oid"] = None
                    s["exchange_done"]    = True
                    modified = True
                    if sid and sid != "?":
                        avg_entry = s.get("avg_entry")
                        sl_price  = s.get("sl")
                        pnl_usd   = None
                        if avg_entry and sl_price:
                            fq       = (s.get("exchange_qty_full") or "0").replace(",", ".")
                            full_qty = float(fq)
                            sign     = 1 if s.get("direction") == "long" else -1
                            pnl_usd  = sign * full_qty * (float(sl_price) - float(avg_entry))
                        db.resolve_setup(int(sid), "SL", avg_entry, sl_price, pnl_usd, None)
                    continue

                if sl_status == "cancelled":
                    # SL1 anulowany — sprawdź czy TP1 odpowiedział
                    tp1_check = _tpsl_order_status(client, tp1_oid) if tp1_oid else "unknown"
                    print(f"[exchange] {label}: SL1 cancelled — sprawdzam TP1: {tp1_check}")
                    if tp1_check != "executed":
                        # Oba anulowane bez TP1 — zamknięcie ręczne
                        log.warning(f"[exchange] {label}: SL1 i TP1 anulowane ręcznie — zamykam setup")
                        for oid, pt in [(tp2_oid, "profit_plan"), (sl2_oid, "loss_plan")]:
                            if oid:
                                _cancel_order(client, oid, pt)
                        s["exchange_sl_oid"]  = None
                        s["exchange_tp1_oid"] = None
                        s["exchange_tp2_oid"] = None
                        s["exchange_sl2_oid"] = None
                        s["exchange_done"]    = True
                        modified = True
                        if sid and sid != "?":
                            db.resolve_setup(int(sid), "nieokreslone", s.get("avg_entry"), None, None, None)
                        continue
                    # TP1 executed — wpadamy w sekcję poniżej
                    tp1_oid = None  # czyścimy żeby sekcja TP1 go wykryła jako executed

            # Sprawdź TP1 (zamyka pierwszą połowę)
            if tp1_oid:
                tp1_status = _tpsl_order_status(client, tp1_oid)
                print(f"[exchange] {label}: TP1 status = {tp1_status}")

                if tp1_status == "executed":
                    tps_list   = s.get("tps") or []
                    tp1_price  = float(tps_list[0]) if tps_list else None
                    avg_entry  = s.get("avg_entry")
                    full_qty_f = float((s.get("exchange_qty_full") or "0").replace(",", "."))
                    half_qty_f = float((s.get("exchange_qty_half") or "0").replace(",", "."))

                    # Anuluj SL1 (Bitget mógł już auto-anulować)
                    if sl_oid:
                        _cancel_order(client, sl_oid, "loss_plan")

                    if s.get("tp_strategy") == "tp1_only":
                        # Cała pozycja zamknięta na TP1 — zakończ setup od razu
                        pnl_usd = None
                        if avg_entry and tp1_price and full_qty_f:
                            sign    = 1 if s.get("direction") == "long" else -1
                            pnl_usd = sign * full_qty_f * (tp1_price - float(avg_entry))
                        s["exchange_tp1_oid"] = None
                        s["exchange_done"]    = True
                        modified = True
                        print(f"[exchange] {label}: TP1-only wykonany — setup zamknięty pnl={pnl_usd}")
                        if sid and sid != "?":
                            db.resolve_setup(int(sid), "TP1", avg_entry, tp1_price, pnl_usd, None)
                        continue

                    # Strategia TP1+TP2: przesuń SL2 → sl_after_tp1 (BE) i czekaj na TP2
                    sl_new_raw = s.get("sl_after_tp1")
                    sl_new     = float(sl_new_raw) if sl_new_raw else (float(avg_entry) if avg_entry else None)

                    new_sl2_oid = sl2_oid
                    if sl_new and half_qty_f > 0:
                        if sl2_oid and _modify_sl(client, sl2_oid, sl_new, half_qty_f):
                            print(f"[exchange] {label}: SL2 zmodyfikowany → {sl_new}")
                        else:
                            if sl2_oid:
                                _cancel_order(client, sl2_oid, "loss_plan")
                            new_sl2_oid = _place_new_sl(client, direction, sl_new, half_qty_f, sid)
                            if new_sl2_oid:
                                print(f"[exchange] {label}: nowy SL2 złożony → {new_sl2_oid} @ {sl_new}")
                            else:
                                log.warning(f"[exchange] {label}: nie udało się złożyć nowego SL2!")

                    pnl_usd = None
                    if avg_entry and tp1_price and half_qty_f:
                        sign    = 1 if s.get("direction") == "long" else -1
                        pnl_usd = sign * half_qty_f * (tp1_price - float(avg_entry))
                    s["pnl_usd"] = pnl_usd  # zachowaj w pamięci dla późniejszego TP1+BE/TP1+TP2

                    s["exchange_tp1_oid"]  = None
                    s["exchange_tp1_done"] = True
                    s["exchange_sl_oid"]   = None
                    s["exchange_sl2_oid"]  = new_sl2_oid
                    modified = True
                    print(f"[exchange] {label}: TP1 wykonany — czekamy na TP2 lub SL2 (SL2={new_sl2_oid})")
                    if sid and sid != "?":
                        db.mark_tp1_hit(int(sid), avg_entry, tp1_price, pnl_usd)
                    continue

                elif tp1_status == "cancelled":
                    log.warning(f"[exchange] {label}: TP1 anulowany ręcznie")
                    s["exchange_tp1_oid"] = None
                    modified = True

            # Zwolnij slot gdy wszystkie TPSL zniknęły
            if (not s.get("exchange_sl_oid") and not s.get("exchange_tp1_oid")
                    and not s.get("exchange_tp2_oid") and not s.get("exchange_done")):
                log.warning(f"[exchange] {label}: wszystkie TPSL zniknęły — zwalniam slot")
                s["exchange_done"] = True
                modified = True

        # ── Pozycja otwarta — monitoruj faza 2 (po TP1: tp2 i sl2-BE) ────────
        elif pos_open and not ex_done and tp1_done:
            tp2_oid = s.get("exchange_tp2_oid")
            sl2_oid = s.get("exchange_sl2_oid")

            if not tp2_oid and not sl2_oid:
                # Faza 2 bez zleceń — stary setup wirtualnie trackowany (exchange_done=True)
                # lub błąd — zamykamy
                if not s.get("exchange_done"):
                    log.warning(f"[exchange] {label}: po TP1 brak tp2/sl2 — zamykam")
                    s["exchange_done"] = True
                    modified = True
                continue

            # Sprawdź TP2
            if tp2_oid:
                tp2_status = _tpsl_order_status(client, tp2_oid)
                print(f"[exchange] {label}: TP2 status = {tp2_status}")
                if tp2_status == "executed":
                    tps_list  = s.get("tps") or []
                    tp2_price = float(tps_list[1]) if len(tps_list) > 1 else None
                    if sl2_oid:
                        _cancel_order(client, sl2_oid, "loss_plan")
                    s["exchange_tp2_oid"] = None
                    s["exchange_sl2_oid"] = None
                    s["exchange_done"]    = True
                    modified = True
                    if sid and sid != "?":
                        pnl_tp1 = s.get("pnl_usd") or 0
                        half_qty_f = float((s.get("exchange_qty_half") or "0").replace(",", "."))
                        avg_entry  = s.get("avg_entry")
                        pnl_tp2 = None
                        if avg_entry and tp2_price and half_qty_f:
                            sign    = 1 if s.get("direction") == "long" else -1
                            pnl_tp2 = sign * half_qty_f * (tp2_price - float(avg_entry))
                        total_pnl = (pnl_tp1 or 0) + (pnl_tp2 or 0)
                        db.resolve_setup(int(sid), "TP1+TP2", s.get("avg_entry"), tp2_price, total_pnl, None)
                        print(f"[exchange] {label}: TP1+TP2 — total pnl={total_pnl:.2f}")
                    continue
                elif tp2_status == "cancelled":
                    log.warning(f"[exchange] {label}: TP2 anulowany ręcznie")
                    s["exchange_tp2_oid"] = None
                    modified = True

            # Sprawdź SL2 (teraz na BE)
            if sl2_oid:
                sl2_status = _tpsl_order_status(client, sl2_oid)
                print(f"[exchange] {label}: SL2-BE status = {sl2_status}")
                if sl2_status == "executed":
                    if tp2_oid:
                        _cancel_order(client, tp2_oid, "profit_plan")
                    s["exchange_tp2_oid"] = None
                    s["exchange_sl2_oid"] = None
                    s["exchange_done"]    = True
                    modified = True
                    if sid and sid != "?":
                        pnl_tp1  = s.get("pnl_usd") or 0
                        avg_entry = s.get("avg_entry")
                        db.resolve_setup(int(sid), "TP1+BE", avg_entry, avg_entry, pnl_tp1, None)
                        print(f"[exchange] {label}: TP1+BE — pnl tp1={pnl_tp1:.2f}")
                    continue
                elif sl2_status == "cancelled":
                    log.warning(f"[exchange] {label}: SL2-BE anulowany ręcznie")
                    s["exchange_sl2_oid"] = None
                    modified = True

            # Zwolnij slot gdy faza 2 bez zleceń
            if not s.get("exchange_tp2_oid") and not s.get("exchange_sl2_oid") and not s.get("exchange_done"):
                log.warning(f"[exchange] {label}: faza 2 — wszystkie zlecenia zniknęły — zwalniam slot")
                s["exchange_done"] = True
                modified = True
                if sid and sid != "?":
                    db.resolve_setup(int(sid), "nieokreslone", s.get("avg_entry"), None, None, None)

    # Po zamknięciu pozycji (lub zmianie salda): aktualizuj rozmiar oczekujących plan orderów
    fresh_balance = get_account_balance()
    if _resize_pending_plan_orders(client, pending, fresh_balance):
        modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")

    # Anuluj plan ordery dla setupów które wygasły bez wejścia (np. "nie weszlo")
    for s in db.get_resolved_with_open_orders():
        oid = s["exchange_plan_oid"]
        sid = s["setup_id"]
        print(f"[exchange] #{sid}: wygasł bez wejścia → cancel plan order {oid}")
        _cancel_order(client, oid, "normal_plan")
        db.mark_exchange_done(sid)


if __name__ == "__main__":
    sync()
