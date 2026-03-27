#!/usr/bin/env python3
"""
Exchange Trader — ByBit Margin Spot
Obsługuje SOLUSDT z dźwignią do 10x

Wywoływany przez sol_alert.py co 15 minut.
Zarządza conditional stop-limit orderami (nie blokują środków),
TP1/TP2 i SL jako osobnymi zleceniami.

Uwaga: ByBit Spot nie ma set_trading_stop — SL to osobny conditional
stop-market order, który musimy śledzić i anulować przy zmianie (po TP1).
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

log = logging.getLogger("exchange")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Konfiguracja ───────────────────────────────────────────────────────────────
SYMBOL       = "SOLUSDT"
LEVERAGE     = 10        # max dla ByBit Margin Spot
TRADE_USDT   = 100.0     # kwota margin na jeden trade
QTY_STEP     = 0.1       # minimalny krok qty dla SOLUSDT
PRICE_DEC    = 2         # miejsca po przecinku dla ceny
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


def _session():
    """Tworzy sesję ByBit. Zwraca None jeśli brak kluczy."""
    if not PYBIT_AVAILABLE:
        print("[exchange] pybit nie zainstalowany — pomijam.")
        return None
    key    = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_API_SECRET", "")
    if not key or not secret:
        print("[exchange] Brak BYBIT_API_KEY/BYBIT_API_SECRET — pomijam.")
        return None
    testnet = os.getenv("BYBIT_TESTNET", "true").lower() != "false"
    env_label = "TESTNET" if testnet else "PRODUKCJA"
    print(f"[exchange] Sesja ByBit {env_label} | {SYMBOL} Margin Spot | {LEVERAGE}x | {TRADE_USDT} USDT/trade")
    return BybitHTTP(testnet=testnet, api_key=key, api_secret=secret)


# ── Składanie zleceń ───────────────────────────────────────────────────────────

def _place_conditional(session, s: dict) -> str | None:
    """
    Składa conditional stop-limit order przy W1 (Margin Spot).
    Nie blokuje środków do momentu aktywacji.
    Zwraca orderId lub None przy błędzie.

    triggerDirection: 1 = cena rośnie do W1, 2 = cena spada do W1
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    qty       = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    side      = "Buy" if direction == "long" else "Sell"
    et        = s.get("entry_trigger", "falling")
    trig_dir  = 1 if et == "rising" else 2

    try:
        resp = session.place_order(
            category="spot",
            symbol=SYMBOL,
            side=side,
            orderType="Limit",
            qty=_fmt_qty(qty),
            price=_fmt_price(w1),
            triggerPrice=_fmt_price(w1),
            triggerDirection=trig_dir,
            triggerBy="LastPrice",
            timeInForce="GTC",
            isLeverage=1,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0:
            oid = resp["result"]["orderId"]
            print(f"[exchange] Conditional złożony: {oid} | {side} {_fmt_qty(qty)} SOL @ {w1}")
            return oid
        log.error(f"[exchange] place_conditional retCode={resp['retCode']}: {resp['retMsg']}")
        return None
    except Exception as e:
        log.error(f"[exchange] place_conditional: {e}")
        return None


def _cancel_order(session, order_id: str, is_stop: bool = False):
    """Anuluje zlecenie. is_stop=True dla conditional/stop orderów."""
    try:
        kwargs = dict(category="spot", symbol=SYMBOL, orderId=order_id)
        if is_stop:
            kwargs["orderFilter"] = "StopOrder"
        resp = session.cancel_order(**kwargs)
        if resp["retCode"] == 0:
            print(f"[exchange] Order anulowany: {order_id}")
        else:
            log.warning(f"[exchange] cancel_order {order_id}: {resp['retMsg']}")
    except Exception as e:
        log.warning(f"[exchange] cancel_order {order_id}: {e}")


def _place_tp_orders(session, s: dict) -> tuple[str | None, str | None]:
    """
    Składa TP1 i TP2 jako limit ordery (50%/50%).
    Zwraca (tp1_order_id, tp2_order_id).
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    full_qty  = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    half_qty  = _round_qty(full_qty / 2)
    tp_side   = "Sell" if direction == "long" else "Buy"
    tps       = s.get("tps", [])
    tp1       = tps[0] if tps else None
    tp2       = tps[1] if len(tps) > 1 else None
    tp1_id    = None
    tp2_id    = None

    if tp1:
        try:
            resp = session.place_order(
                category="spot",
                symbol=SYMBOL,
                side=tp_side,
                orderType="Limit",
                qty=_fmt_qty(half_qty),
                price=_fmt_price(tp1),
                timeInForce="GTC",
                isLeverage=1,
            )
            if resp["retCode"] == 0:
                tp1_id = resp["result"]["orderId"]
                print(f"[exchange] TP1: {tp1_id} | {_fmt_qty(half_qty)} SOL @ {tp1}")
            else:
                log.error(f"[exchange] TP1: {resp['retMsg']}")
        except Exception as e:
            log.error(f"[exchange] TP1: {e}")

    if tp2:
        try:
            resp = session.place_order(
                category="spot",
                symbol=SYMBOL,
                side=tp_side,
                orderType="Limit",
                qty=_fmt_qty(half_qty),
                price=_fmt_price(tp2),
                timeInForce="GTC",
                isLeverage=1,
            )
            if resp["retCode"] == 0:
                tp2_id = resp["result"]["orderId"]
                print(f"[exchange] TP2: {tp2_id} | {_fmt_qty(half_qty)} SOL @ {tp2}")
            else:
                log.error(f"[exchange] TP2: {resp['retMsg']}")
        except Exception as e:
            log.error(f"[exchange] TP2: {e}")

    return tp1_id, tp2_id


def _place_sl_order(session, s: dict, sl_price: float, qty: float) -> str | None:
    """
    Składa SL jako conditional stop-market order.
    Zwraca orderId lub None.

    Long SL: sprzedaj gdy cena spadnie do SL → triggerDirection=2, side=Sell
    Short SL: kup gdy cena wzrośnie do SL    → triggerDirection=1, side=Buy
    """
    direction = s["direction"]
    side      = "Sell" if direction == "long" else "Buy"
    trig_dir  = 2 if direction == "long" else 1

    try:
        resp = session.place_order(
            category="spot",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=_fmt_qty(qty),
            triggerPrice=_fmt_price(sl_price),
            triggerDirection=trig_dir,
            triggerBy="LastPrice",
            isLeverage=1,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0:
            oid = resp["result"]["orderId"]
            print(f"[exchange] SL: {oid} | {side} {_fmt_qty(qty)} SOL stop @ {sl_price}")
            return oid
        log.error(f"[exchange] SL: {resp['retMsg']}")
        return None
    except Exception as e:
        log.error(f"[exchange] SL: {e}")
        return None


def _place_tp_sl_orders(session, s: dict) -> tuple[str | None, str | None, str | None]:
    """Po wejściu: składa TP1, TP2 i SL. Zwraca (tp1_id, tp2_id, sl_id)."""
    w1       = s["entries"][0]
    full_qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    sl       = s["sl"]

    tp1_id, tp2_id = _place_tp_orders(session, s)
    sl_id          = _place_sl_order(session, s, sl, full_qty)

    return tp1_id, tp2_id, sl_id


def _update_sl(session, s: dict, new_sl: float):
    """
    Przesuwa SL po TP1: anuluje stary SL, składa nowy na 50% pozycji.
    """
    old_sl_id = s.get("exchange_sl_order_id")
    if old_sl_id:
        _cancel_order(session, old_sl_id, is_stop=True)

    w1       = s["entries"][0]
    half_qty = _round_qty(_round_qty((TRADE_USDT * LEVERAGE) / w1) / 2)
    new_sl_id = _place_sl_order(session, s, new_sl, half_qty)

    if new_sl_id:
        s["exchange_sl_order_id"] = new_sl_id
        be_label = "BE" if abs(new_sl - w1) < 0.05 else f"+${abs(new_sl - w1):.2f}"
        print(f"[exchange] SL przesunięty po TP1 → {new_sl} ({be_label})")


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _conditional_status(session, order_id: str) -> str:
    """
    Sprawdza status conditional stop order.
    Zwraca: 'untriggered' | 'filled' | 'cancelled' | 'unknown'
    """
    try:
        # Czy jest nadal w otwartych stop orderach?
        resp = session.get_open_orders(
            category="spot",
            symbol=SYMBOL,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0:
            open_ids = {o["orderId"] for o in resp["result"]["list"]}
            if order_id in open_ids:
                return "untriggered"

        # Nie ma w otwartych — sprawdź historię
        resp = session.get_order_history(
            category="spot",
            symbol=SYMBOL,
            orderId=order_id,
            orderFilter="StopOrder",
        )
        if resp["retCode"] == 0 and resp["result"]["list"]:
            status = resp["result"]["list"][0]["orderStatus"]
            if status in ("Filled", "Triggered"):
                return "filled"
            if status in ("Cancelled", "Deactivated"):
                return "cancelled"
    except Exception as e:
        log.warning(f"[exchange] conditional_status {order_id}: {e}")
    return "unknown"


def _is_order_filled(session, order_id: str) -> bool:
    """Sprawdza czy zwykłe zlecenie (TP1/TP2) zostało wypełnione."""
    try:
        resp = session.get_order_history(
            category="spot",
            symbol=SYMBOL,
            orderId=order_id,
        )
        if resp["retCode"] == 0 and resp["result"]["list"]:
            return resp["result"]["list"][0]["orderStatus"] == "Filled"
    except Exception as e:
        log.warning(f"[exchange] is_order_filled {order_id}: {e}")
    return False


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY:      składa conditional stop-limit (nie blokuje środków)
      - ANULOWANY: anuluje order na ByBit
      - OCZEKUJĄCY: sprawdza czy conditional wypełniony → TP1/TP2/SL
      - OTWARTY:   sprawdza TP1 → przesuwa SL
    """
    session = _session()
    if session is None:
        return

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
            print(f"[exchange] {label}: setup anulowany → cancel {order_id}")
            _cancel_order(session, order_id, is_stop=True)
            s["exchange_order_id"] = None
            modified = True
            continue

        # ── Nowy setup — złóż conditional order ─────────────────────────────
        if not shadow and not cancelled and not order_id and s["entry_hit_at"] is None:
            w1  = entries[0]
            qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid = _place_conditional(session, s)
            if oid:
                s["exchange_order_id"]        = oid
                s["exchange_position_opened"] = False
                s["exchange_tp1_order_id"]    = None
                s["exchange_tp2_order_id"]    = None
                s["exchange_sl_order_id"]     = None
                s["exchange_qty"]             = _fmt_qty(qty)
                modified = True
                print(f"[exchange] {label}: conditional → {oid} ({_fmt_qty(qty)} SOL @ {w1})")
            continue

        # ── Oczekujący — sprawdź czy triggered ──────────────────────────────
        if order_id and not opened:
            status = _conditional_status(session, order_id)
            if status == "untriggered":
                continue
            if status == "cancelled":
                print(f"[exchange] {label}: conditional anulowany z zewnątrz")
                s["exchange_order_id"] = None
                modified = True
                continue
            if status == "filled":
                print(f"[exchange] {label}: entry wypełniony! Składam TP/SL.")
                tp1_id, tp2_id, sl_id = _place_tp_sl_orders(session, s)
                s["exchange_position_opened"] = True
                s["exchange_tp1_order_id"]    = tp1_id
                s["exchange_tp2_order_id"]    = tp2_id
                s["exchange_sl_order_id"]     = sl_id
                modified = True
            continue

        # ── Pozycja otwarta — sprawdź TP1, przesuń SL ───────────────────────
        if opened and not s.get("sl_adjusted"):
            tp1_oid = s.get("exchange_tp1_order_id")
            if tp1_oid and _is_order_filled(session, tp1_oid):
                w1       = entries[0]
                sl_after = s.get("sl_after_tp1") or w1  # null → W1 = breakeven
                _update_sl(session, s, sl_after)
                s["sl_adjusted"] = True
                s["tp1_hit_at"]  = s.get("tp1_hit_at") or int(time.time())
                modified = True

    if modified:
        _save_pending(pending)
        print("[exchange] pending_setups.json zaktualizowany.")


if __name__ == "__main__":
    sync()
