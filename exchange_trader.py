#!/usr/bin/env python3
"""
Exchange Trader — Bitget USDT-M Futures (SOLUSDT Perpetual)

Flow dla każdego setupu:
  1. Nowy setup → 1 plan order przy W1 (pełna pozycja, bez presetów TP/SL)
  2. Plan order wykonany → 2 TPSL ordery (składane ręcznie, bez presetów):
       TP1: profit_plan, full_qty SOL, trigger=TP1  (zamyka całość)
       SL:  loss_plan,   full_qty SOL, trigger=SL
  3. Monitoring TPSL:
       SL wykonany  → anuluj TP1
       TP1 wykonany → anuluj SL, zamknij setup

Wymagane zmienne środowiskowe:
  BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE
  BITGET_DEMO       — "true" = demo (domyślnie true)
  BITGET_TRADE_SOL  — rozmiar pozycji w SOL (np. 2.4); jeśli nie ustawiony —
  BITGET_TRADE_USDT — rozmiar pozycji w USDT (domyślnie 100), przeliczany na SOL
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
LEVERAGE     = 20
TRADE_USDT    = float(os.getenv("BITGET_TRADE_USDT") or "100.0")
TRADE_SOL     = float(os.getenv("BITGET_TRADE_SOL") or "0")    # 0 = przelicz z TRADE_USDT
MAX_POSITIONS = int(os.getenv("BITGET_MAX_POSITIONS") or "5")
QTY_STEP     = 0.1
PRICE_DEC    = 2
BASE_URL     = "https://api.bitget.com"


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    if TRADE_SOL > 0:
        print(f"[exchange] Sesja Bitget {'DEMO' if demo else 'PRODUKCJA'} | {SYMBOL} | {LEVERAGE}x | {TRADE_SOL} SOL/trade")
    else:
        print(f"[exchange] Sesja Bitget {'DEMO' if demo else 'PRODUKCJA'} | {SYMBOL} | {LEVERAGE}x | {TRADE_USDT} USDT/trade (przelicz na SOL)")
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
    Składa plan order przy W1 BEZ presetów TP/SL.
    TPSL ordery są składane osobno po wejściu w pozycję przez _place_tpsl_orders().
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
        "triggerPrice": _fmt_price(w1),
        "triggerType":  "mark_price",
        "side":         side,
        "tradeSide":    "open",
        "posSide":      direction,
        "orderType":    "market",
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
    skip_sl: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """
    Po wejściu w pozycję składa 2 oddzielne TPSL ordery:
      TP1: profit_plan, full_qty SOL, trigger=TP1 (zamyka całość)
      SL:  loss_plan,   full_qty SOL, trigger=SL
    skip_sl=True — pomija SL (gdy SL już istnieje w Bitget).
    Zwraca (tp1_id, None, sl_id).  TP2 nie jest składany na giełdzie.
    """
    direction = s["direction"]
    hold_side = direction  # "long" lub "short"
    tps       = s.get("tps", [])
    tp1       = tps[0] if len(tps) > 0 else None
    sl        = s.get("sl")
    sid       = s.get("setup_id", "?")

    tp1_id = sl_id = None

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
                "size":         _fmt_qty(full_qty),
            })
            if resp.get("code") == "00000":
                tp1_id = resp["data"]["orderId"]
                print(f"[exchange] #{sid} TP1 order: {tp1_id} | {_fmt_qty(full_qty)} SOL @ {tp1} (100% pozycji)")
            else:
                log.error(f"[exchange] #{sid} place TP1: code={resp.get('code')} msg={resp.get('msg')}")
        except Exception as e:
            log.error(f"[exchange] #{sid} place TP1: {e}")

    if sl is not None and not skip_sl:
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

    return tp1_id, None, sl_id


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


def _find_preset_tpsl(client: BitgetClient, hold_side: str) -> tuple[str | None, str | None]:
    """
    Szuka aktywnych TPSL orderów (z presetu plan order) dla danej strony pozycji.
    Zwraca (tp_order_id, sl_order_id) lub (None, None).
    """
    tp_id = sl_id = None
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      SYMBOL,
            "productType": PRODUCT_TYPE,
            "planType":    "profit_loss",
        })
        if resp.get("code") == "00000":
            for o in (resp["data"].get("entrustedList") or []):
                if o.get("posSide", o.get("holdSide", "")) == hold_side:
                    plan_type = o.get("planType", "")
                    if plan_type == "profit_plan" and tp_id is None:
                        tp_id = o["orderId"]
                    elif plan_type == "loss_plan" and sl_id is None:
                        sl_id = o["orderId"]
    except Exception as e:
        log.warning(f"[exchange] _find_preset_tpsl: {e}")
    return tp_id, sl_id


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


def _sync_inner():
    client = _client()
    if client is None:
        return

    _set_hedge_mode(client)
    _set_leverage(client)

    pending  = _load_pending()
    modified = False

    # Guard: maksymalnie MAX_POSITIONS aktywnych pozycji na raz.
    active_count = sum(
        1 for s in pending
        if s.get("exchange_position_opened")
        and not s.get("exchange_done", False)
    )
    exchange_slot_taken = active_count >= MAX_POSITIONS
    if exchange_slot_taken:
        print(f"[exchange] Limit pozycji osiągnięty ({active_count}/{MAX_POSITIONS}) — nowe wstrzymane.")

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

        # ── NOWY setup — złóż plan order przy W1 ─────────────────────────────
        if not shadow and not cancelled and not plan_oid and s.get("entry_hit_at") is None:
            if exchange_slot_taken:
                print(f"[exchange] {label}: pominięty — slot zajęty (tryb jedna pozycja na raz)")
                continue
            # Atomicznie zarezerwuj slot przed wywołaniem API — chroni przed race condition
            # gdy Railway (co 15s) i GitHub Actions (co 5min) wywołują sync() równocześnie.
            if not db.claim_plan_order(s["setup_id"]):
                print(f"[exchange] {label}: plan order już zarezerwowany przez inny proces — pomijam")
                continue
            w1       = entries[0]
            full_qty = _round_qty(TRADE_SOL) if TRADE_SOL > 0 else _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid      = _place_entry_plan_order(client, s, full_qty)
            if oid:
                s["exchange_plan_oid"]        = oid
                s["exchange_qty_full"]        = _fmt_qty(full_qty)
                s["exchange_qty_half"]        = _fmt_qty(_round_qty(full_qty / 2))
                s["exchange_position_opened"] = False
                modified = True
                print(f"[exchange] {label}: plan order złożony ({_fmt_qty(full_qty)} SOL @ W1={w1})")
            else:
                # API call nie udał się — zwolnij rezerwację żeby następny sync() mógł spróbować
                db.release_plan_order_claim(s["setup_id"])
            continue

        # ── Plan order złożony, pozycja jeszcze nie otwarta ───────────────────
        if plan_oid and not pos_open:
            if plan_oid == "PENDING":
                # Stała rezerwacja bez OID — proces poprzedni musiał się wysypać; resetuj
                print(f"[exchange] {label}: stale PENDING claim — reset, next sync retry")
                s["exchange_plan_oid"] = None
                modified = True
                continue
            status = _plan_order_status(client, plan_oid)
            print(f"[exchange] {label}: plan order status = {status}")

            if status == "cancelled":
                print(f"[exchange] {label}: plan order anulowany z zewnątrz")
                s["exchange_plan_oid"] = None
                modified = True

            elif status == "executed":
                # Entry hit — zweryfikuj rzeczywistą pozycję przed złożeniem TPSL
                actual_qty = _get_open_position_size(client, direction)
                calc_qty   = float(s.get("exchange_qty_full", "0").replace(",", ".") or "0")
                if calc_qty <= 0:
                    w1       = entries[0]
                    calc_qty = _round_qty(TRADE_SOL) if TRADE_SOL > 0 else _round_qty((TRADE_USDT * LEVERAGE) / w1)

                if actual_qty <= 0:
                    # Plan order "executed" ale pozycja = 0 — częściowe wypełnienie
                    # zamknięte natychmiast (SL/liq) lub fałszywy status Bitget
                    log.error(
                        f"[exchange] {label}: plan order wykonany ale pozycja=0 "
                        f"(oczekiwano {calc_qty} SOL) — traktuję jako brak wejścia"
                    )
                    s["exchange_plan_oid"] = None
                    s["exchange_done"]     = True
                    modified = True
                    continue

                # actual_qty = cała pozycja na Bitget (może zawierać inne setupy).
                # Używamy calc_qty — rozmiaru obliczonego dla TEGO setupu przy plan orderze.
                # actual_qty służy tylko do weryfikacji czy wejście w ogóle nastąpiło.
                if actual_qty < calc_qty * 0.8:
                    log.warning(
                        f"[exchange] {label}: częściowe wypełnienie — "
                        f"oczekiwano {calc_qty} SOL, otwarto {actual_qty} SOL"
                    )

                full_qty = _round_qty(calc_qty)
                half_qty = _round_qty(full_qty / 2)

                # Zawsze składaj TPSL ręcznie — presety w plan orderze były źródłem
                # race condition (duplikaty lub zgubione IDs), więc plan order
                # nie zawiera presetów TP/SL, a TPSL są zawsze składane tutaj.
                print(f"[exchange] {label}: składam TPSL po wejściu w pozycję...")
                tp1_id, _, sl_id = _place_tpsl_orders(client, s, full_qty, half_qty)

                s["exchange_position_opened"] = True
                s["exchange_qty_full"]        = _fmt_qty(full_qty)
                s["exchange_qty_half"]        = _fmt_qty(half_qty)
                s["exchange_tp1_oid"]         = tp1_id
                s["exchange_tp2_oid"]         = None
                s["exchange_sl_oid"]          = sl_id
                modified = True
                print(f"[exchange] {label}: pozycja otwarta ({actual_qty} SOL), TPSL aktywne "
                      f"(TP1={tp1_id} SL={sl_id})")
            continue

        # ── Pozycja otwarta, SL jest ale brak TP — re-place tylko TP ─────────
        if pos_open and not ex_done and sl_oid and not tp1_oid and not tp1_done:
            full_qty = float((s.get("exchange_qty_full") or "0").replace(",", "."))
            half_qty = full_qty  # unused, kept for API compat
            if full_qty > 0:
                log.warning(f"[exchange] {label}: brakuje TP przy istniejącym SL — re-place TP")
                tp1_id, _, _ = _place_tpsl_orders(client, s, full_qty, half_qty, skip_sl=True)
                s["exchange_tp1_oid"] = tp1_id
                modified = True
            continue

        # ── Pozycja otwarta, brak TPSL — retry składania zleceń ──────────────
        if pos_open and not ex_done and not tp1_oid and not sl_oid:
            full_qty = float((s.get("exchange_qty_full") or "0").replace(",", "."))
            half_qty = full_qty  # unused, kept for API compat
            if full_qty > 0:
                log.warning(f"[exchange] {label}: pozycja otwarta bez TPSL — retry składania zleceń")
                tp1_id, _, sl_id = _place_tpsl_orders(client, s, full_qty, half_qty)
                s["exchange_tp1_oid"] = tp1_id
                s["exchange_sl_oid"]  = sl_id
                modified = True
            continue

        # ── Pozycja otwarta — monitoruj TPSL ─────────────────────────────────
        if pos_open and (tp1_oid or tp2_oid or sl_oid):

            # Sprawdź SL jako pierwsze — ma priorytet
            if sl_oid:
                sl_status = _tpsl_order_status(client, sl_oid)
                print(f"[exchange] {label}: SL status = {sl_status}")

                if sl_status == "executed":
                    # SL zamknął całą pozycję — anuluj TP1
                    print(f"[exchange] {label}: SL wykonany — anuluj TP1")
                    if tp1_oid:
                        _cancel_order(client, tp1_oid, "profit_plan")
                    s["exchange_sl_oid"]  = None
                    s["exchange_tp1_oid"] = None
                    s["exchange_tp2_oid"] = None
                    s["exchange_done"]    = True
                    modified = True
                    if sid and sid != "?":
                        avg_entry = s.get("avg_entry")
                        sl_price  = s.get("sl")
                        avg_exit  = sl_price
                        pnl_usd   = None
                        if avg_entry and sl_price:
                            fq       = (s.get("exchange_qty_full") or "0").replace(",", ".")
                            full_qty = float(fq)
                            sign     = 1 if s.get("direction") == "long" else -1
                            pnl_usd  = sign * full_qty * (float(sl_price) - float(avg_entry))
                        db.resolve_setup(int(sid), "SL", avg_entry, avg_exit, pnl_usd, None)
                    continue

                if sl_status == "cancelled":
                    # SL anulowany ręcznie — pozycja zamknięta manualnie
                    log.warning(f"[exchange] {label}: SL anulowany ręcznie — zwalniam slot i zamykam setup")
                    if tp1_oid:
                        _cancel_order(client, tp1_oid, "profit_plan")
                    s["exchange_sl_oid"]  = None
                    s["exchange_tp1_oid"] = None
                    s["exchange_tp2_oid"] = None
                    s["exchange_done"]    = True
                    modified = True
                    if sid and sid != "?":
                        db.resolve_setup(int(sid), "nieokreslone", s.get("avg_entry"), None, None, None)
                    continue

            # Sprawdź TP1 (zamyka 100% pozycji)
            if tp1_oid and not tp1_done:
                tp1_status = _tpsl_order_status(client, tp1_oid)
                print(f"[exchange] {label}: TP1 status = {tp1_status}")

                if tp1_status == "executed":
                    # TP1 zamknął całą pozycję — anuluj SL
                    print(f"[exchange] {label}: TP1 wykonany — anuluj SL, pozycja zamknięta")
                    if sl_oid:
                        _cancel_order(client, sl_oid, "loss_plan")
                    s["exchange_tp1_oid"]  = None
                    s["exchange_tp1_done"] = True
                    s["exchange_sl_oid"]   = None
                    s["exchange_tp2_oid"]  = None
                    s["exchange_done"]     = True
                    modified = True
                    if sid and sid != "?":
                        tps       = s.get("tps") or []
                        tp1_price = float(tps[0]) if tps else None
                        avg_entry = s.get("avg_entry")
                        avg_exit  = tp1_price
                        pnl_usd   = None
                        if avg_entry and tp1_price:
                            fq       = (s.get("exchange_qty_full") or "0").replace(",", ".")
                            full_qty = float(fq)
                            sign     = 1 if s.get("direction") == "long" else -1
                            pnl_usd  = sign * full_qty * (tp1_price - float(avg_entry))
                        db.resolve_setup(int(sid), "TP1", avg_entry, avg_exit, pnl_usd, None)

                elif tp1_status == "cancelled":
                    log.warning(f"[exchange] {label}: TP1 anulowany — zostanie ponownie złożony")
                    s["exchange_tp1_oid"] = None
                    modified = True

            # Jeśli wszystkie TPSL anulowane — zwolnij slot
            if (not s.get("exchange_sl_oid")
                    and not s.get("exchange_tp1_oid")
                    and not s.get("exchange_tp2_oid")
                    and not s.get("exchange_done")):
                log.warning(f"[exchange] {label}: wszystkie TPSL zniknęły — zwalniam slot")
                s["exchange_done"] = True
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
