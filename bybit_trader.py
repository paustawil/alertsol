#!/usr/bin/env python3
"""
ByBit Auto-Trader — integracja z ByBit Testnet/Production
Obsługuje SOLUSDT USDT Perpetual z dźwignią 20x

Wywoływany przez sol_alert.py co 15 minut.
Zarządza conditional stop-limit orderami, TP1/TP2 i SL.
"""

import os
import json
import math
import time
import logging

try:
    from pybit.unified_trading import HTTP as BybitHTTP
    PYBIT_AVAILABLE = True
except ImportError:
    PYBIT_AVAILABLE = False

log = logging.getLogger("bybit")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Konfiguracja ───────────────────────────────────────────────────────────────
SYMBOL       = "SOLUSDT"
LEVERAGE     = 20
TRADE_USDT   = 100.0    # kwota margin na jeden trade
QTY_STEP     = 0.1      # minimalny krok qty dla SOLUSDT
PRICE_DEC    = 2        # miejsca po przecinku dla ceny
PENDING_FILE = "pending_setups.json"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _round_qty(qty: float) -> str:
    """Zaokrągla qty w dół do najbliższego kroku QTY_STEP."""
    steps = math.floor(qty / QTY_STEP)
    return f"{max(steps, 1) * QTY_STEP:.1f}"


def _price(p: float) -> str:
    return f"{p:.{PRICE_DEC}f}"


def _load_pending() -> list[dict]:
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE) as f:
        return json.load(f)


def _save_pending(pending: list[dict]):
    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)


def _session():
    """Tworzy sesję ByBit. Zwraca None jeśli brak kluczy."""
    if not PYBIT_AVAILABLE:
        print("[bybit] pybit nie zainstalowany — pomijam.")
        return None
    key    = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_API_SECRET", "")
    if not key or not secret:
        print("[bybit] Brak BYBIT_API_KEY/BYBIT_API_SECRET — pomijam.")
        return None
    testnet = os.getenv("BYBIT_TESTNET", "true").lower() != "false"
    env_label = "TESTNET" if testnet else "PRODUKCJA"
    print(f"[bybit] Sesja {env_label} | {SYMBOL} | dźwignia {LEVERAGE}x | {TRADE_USDT} USDT/trade")
    return BybitHTTP(testnet=testnet, api_key=key, api_secret=secret)


def _set_leverage(session) -> bool:
    """Ustawia dźwignię. Ignoruje błąd 'already set'."""
    try:
        resp = session.set_leverage(
            category="linear",
            symbol=SYMBOL,
            buyLeverage=str(LEVERAGE),
            sellLeverage=str(LEVERAGE),
        )
        if resp["retCode"] in (0, 110043):  # 110043 = leverage not modified
            return True
        log.warning(f"[bybit] set_leverage: {resp['retMsg']}")
        return False
    except Exception as e:
        log.warning(f"[bybit] set_leverage exception: {e}")
        return False


# ── Składanie zleceń ───────────────────────────────────────────────────────────

def _place_conditional(session, s: dict) -> str | None:
    """
    Składa conditional stop-limit order przy W1.
    Nie blokuje środków do momentu aktywacji.
    Zwraca orderId lub None przy błędzie.
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    qty       = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    side      = "Buy" if direction == "long" else "Sell"

    # triggerDirection: 1 = cena rośnie do trigger, 2 = cena spada do trigger
    et          = s.get("entry_trigger", "falling")
    trigger_dir = 1 if et == "rising" else 2

    try:
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Limit",
            qty=qty,
            price=_price(w1),
            triggerPrice=_price(w1),
            triggerDirection=trigger_dir,
            triggerBy="LastPrice",
            timeInForce="GTC",
            reduceOnly=False,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0:
            oid = resp["result"]["orderId"]
            print(f"[bybit] Conditional order złożony: {oid} | {side} {qty} SOL @ {w1}")
            return oid
        log.error(f"[bybit] place_order retCode={resp['retCode']}: {resp['retMsg']}")
        return None
    except Exception as e:
        log.error(f"[bybit] place_order exception: {e}")
        return None


def _cancel_order(session, order_id: str):
    """Anuluje conditional stop order."""
    try:
        resp = session.cancel_order(
            category="linear",
            symbol=SYMBOL,
            orderId=order_id,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0:
            print(f"[bybit] Order anulowany: {order_id}")
        else:
            log.warning(f"[bybit] cancel_order {order_id}: {resp['retMsg']}")
    except Exception as e:
        log.warning(f"[bybit] cancel_order exception: {e}")


def _place_tp_sl_orders(session, s: dict) -> tuple[str | None, str | None]:
    """
    Po wejściu w pozycję: składa dwa zlecenia TP (50%/50%) i ustawia SL.
    Zwraca (tp1_order_id, tp2_order_id).
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    full_qty  = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    half_qty  = _round_qty(float(full_qty) / 2)
    tp_side   = "Sell" if direction == "long" else "Buy"
    tps       = s.get("tps", [])
    sl        = s["sl"]
    tp1       = tps[0] if tps else None
    tp2       = tps[1] if len(tps) > 1 else None
    tp1_id    = None
    tp2_id    = None

    # TP1 — 50% pozycji
    if tp1:
        try:
            resp = session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=tp_side,
                orderType="Limit",
                qty=half_qty,
                price=_price(tp1),
                timeInForce="GTC",
                reduceOnly=True,
            )
            if resp["retCode"] == 0:
                tp1_id = resp["result"]["orderId"]
                print(f"[bybit] TP1 order: {tp1_id} | {half_qty} SOL @ {tp1}")
            else:
                log.error(f"[bybit] TP1 error: {resp['retMsg']}")
        except Exception as e:
            log.error(f"[bybit] TP1 exception: {e}")

    # TP2 — pozostałe 50%
    if tp2:
        try:
            resp = session.place_order(
                category="linear",
                symbol=SYMBOL,
                side=tp_side,
                orderType="Limit",
                qty=half_qty,
                price=_price(tp2),
                timeInForce="GTC",
                reduceOnly=True,
            )
            if resp["retCode"] == 0:
                tp2_id = resp["result"]["orderId"]
                print(f"[bybit] TP2 order: {tp2_id} | {half_qty} SOL @ {tp2}")
            else:
                log.error(f"[bybit] TP2 error: {resp['retMsg']}")
        except Exception as e:
            log.error(f"[bybit] TP2 exception: {e}")

    # SL na pozycji
    try:
        session.set_trading_stop(
            category="linear",
            symbol=SYMBOL,
            stopLoss=_price(sl),
            slTriggerBy="LastPrice",
            positionIdx=0,
        )
        print(f"[bybit] SL ustawiony @ {sl}")
    except Exception as e:
        log.error(f"[bybit] set_trading_stop exception: {e}")

    return tp1_id, tp2_id


def _update_sl_after_tp1(session, new_sl: float):
    """Przesuwa SL na pozycji po trafieniu TP1."""
    try:
        session.set_trading_stop(
            category="linear",
            symbol=SYMBOL,
            stopLoss=_price(new_sl),
            slTriggerBy="LastPrice",
            positionIdx=0,
        )
        print(f"[bybit] SL przesunięty po TP1 → {new_sl}")
    except Exception as e:
        log.error(f"[bybit] update_sl exception: {e}")


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _conditional_status(session, order_id: str) -> str:
    """
    Sprawdza status conditional stop order.
    Zwraca: 'untriggered' | 'filled' | 'cancelled' | 'unknown'
    """
    try:
        # Najpierw sprawdź czy jest nadal w otwartych stop orderach
        resp = session.get_open_orders(
            category="linear",
            symbol=SYMBOL,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0:
            open_ids = {o["orderId"] for o in resp["result"]["list"]}
            if order_id in open_ids:
                return "untriggered"

        # Nie ma w otwartych — sprawdź historię
        resp = session.get_order_history(
            category="linear",
            symbol=SYMBOL,
            orderId=order_id,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0 and resp["result"]["list"]:
            status = resp["result"]["list"][0]["orderStatus"]
            if status in ("Filled", "PartiallyFilledCanceled"):
                return "filled"
            if status == "Triggered":
                # Conditional triggered — sprawdź czy wynikowy limit order wypełniony
                return "filled"
            if status in ("Cancelled", "Deactivated"):
                return "cancelled"
    except Exception as e:
        log.warning(f"[bybit] conditional_status {order_id}: {e}")
    return "unknown"


def _is_order_filled(session, order_id: str) -> bool:
    """Sprawdza czy zwykłe zlecenie (TP1/TP2) zostało wypełnione."""
    try:
        resp = session.get_order_history(
            category="linear",
            symbol=SYMBOL,
            orderId=order_id,
        )
        if resp["retCode"] == 0 and resp["result"]["list"]:
            return resp["result"]["list"][0]["orderStatus"] == "Filled"
    except Exception as e:
        log.warning(f"[bybit] is_order_filled {order_id}: {e}")
    return False


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY: składa conditional stop-limit order
      - ANULOWANY: anuluje order na ByBit
      - OCZEKUJĄCY: sprawdza czy conditional został triggerowany
      - OTWARTY: sprawdza TP1, ewentualnie przesuwa SL
    """
    session = _session()
    if session is None:
        return

    _set_leverage(session)

    pending  = _load_pending()
    modified = False

    for s in pending:
        sid       = s.get("setup_id", "?")
        model     = s.get("model", "?")
        direction = s.get("direction", "?")
        shadow    = s.get("shadow", False)
        cancelled = bool(s.get("cancel_reason"))
        order_id  = s.get("bybit_order_id")
        opened    = s.get("bybit_position_opened", False)
        entries   = s.get("entries", [])

        if not entries:
            continue

        label = f"#{sid} [{model}] {direction.upper()}"

        # ── Anuluj zlecenie gdy setup anulowany ─────────────────────────────
        if (shadow or cancelled) and order_id and not opened:
            print(f"[bybit] {label}: setup anulowany → cancel order {order_id}")
            _cancel_order(session, order_id)
            s["bybit_order_id"] = None
            modified = True
            continue

        # ── Nowy setup — złóż conditional order ─────────────────────────────
        if not shadow and not cancelled and not order_id and s["entry_hit_at"] is None:
            w1  = entries[0]
            qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid = _place_conditional(session, s)
            if oid:
                s["bybit_order_id"]        = oid
                s["bybit_position_opened"] = False
                s["bybit_tp1_order_id"]    = None
                s["bybit_tp2_order_id"]    = None
                s["bybit_qty"]             = qty
                modified = True
                print(f"[bybit] {label}: conditional order → {oid} ({qty} SOL @ {w1})")
            continue

        # ── Oczekujący order — sprawdź czy triggered ─────────────────────────
        if order_id and not opened:
            status = _conditional_status(session, order_id)
            if status == "untriggered":
                continue  # jeszcze nie aktywowany
            if status == "cancelled":
                print(f"[bybit] {label}: conditional anulowany z zewnątrz")
                s["bybit_order_id"] = None
                modified = True
                continue
            if status in ("filled", "unknown"):
                if status == "filled":
                    print(f"[bybit] {label}: conditional triggered! Składam TP1/TP2/SL.")
                    tp1_id, tp2_id = _place_tp_sl_orders(session, s)
                    s["bybit_position_opened"] = True
                    s["bybit_tp1_order_id"]    = tp1_id
                    s["bybit_tp2_order_id"]    = tp2_id
                    modified = True
            continue

        # ── Pozycja otwarta — sprawdź TP1, ewentualnie przesuń SL ────────────
        if opened and not s.get("sl_adjusted"):
            tp1_oid = s.get("bybit_tp1_order_id")
            if tp1_oid and _is_order_filled(session, tp1_oid):
                w1       = entries[0]
                sl_after = s.get("sl_after_tp1") or w1  # null → W1 = breakeven
                _update_sl_after_tp1(session, sl_after)
                s["sl_adjusted"] = True
                s["tp1_hit_at"]  = s.get("tp1_hit_at") or int(time.time())
                modified = True
                be_label = "BE" if abs(sl_after - w1) < 0.05 else f"+${abs(sl_after - w1):.2f}"
                print(f"[bybit] {label}: TP1 wypełniony → SL przesunięty na {sl_after} ({be_label})")

    if modified:
        _save_pending(pending)
        print("[bybit] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
