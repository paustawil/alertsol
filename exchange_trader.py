#!/usr/bin/env python3
"""
Exchange Trader — Binance USDT-M Futures
Obsługuje SOLUSDT z dźwignią 20x

Wywoływany przez sol_alert.py co 15 minut.
Zarządza conditional orderami (nie blokują margin), TP1/TP2 i SL.

Typy conditional orderów:
  Long  + falling (pullback): TAKE_PROFIT BUY  — trigger gdy cena spada do W1
  Long  + rising  (breakout): STOP        BUY  — trigger gdy cena rośnie do W1
  Short + rising  (pullback): TAKE_PROFIT SELL — trigger gdy cena rośnie do W1
  Short + falling (breakdown):STOP        SELL — trigger gdy cena spada do W1
"""

import os
import json
import math
import time
import logging

try:
    from binance.um_futures import UMFutures
    BINANCE_AVAILABLE = True
except ImportError:
    BINANCE_AVAILABLE = False

log = logging.getLogger("exchange")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── Konfiguracja ───────────────────────────────────────────────────────────────
SYMBOL       = "SOLUSDT"
LEVERAGE     = 20        # zmień jeśli konto ma inny limit
TRADE_USDT   = 100.0     # kwota margin na jeden trade
QTY_STEP     = 0.1       # minimalny krok qty dla SOLUSDT
PRICE_DEC    = 2         # miejsca po przecinku dla ceny
PENDING_FILE = "pending_setups.json"
TESTNET_URL  = "https://testnet.binancefuture.com"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _round_qty(qty: float) -> float:
    """Zaokrągla qty w dół do najbliższego kroku QTY_STEP."""
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


def _client():
    """Tworzy klienta Binance Futures. Zwraca None jeśli brak kluczy."""
    if not BINANCE_AVAILABLE:
        print("[exchange] binance-futures-connector nie zainstalowany — pomijam.")
        return None
    key    = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_API_SECRET", "")
    if not key or not secret:
        print("[exchange] Brak BINANCE_API_KEY/BINANCE_API_SECRET — pomijam.")
        return None
    testnet = os.getenv("BINANCE_TESTNET", "true").lower() != "false"
    env_label = "TESTNET" if testnet else "PRODUKCJA"
    base_url = TESTNET_URL if testnet else None  # None = domyślny prod URL
    print(f"[exchange] Sesja Binance {env_label} | {SYMBOL} | {LEVERAGE}x | {TRADE_USDT} USDT/trade")
    return UMFutures(key=key, secret=secret, base_url=base_url)


def _set_leverage(client) -> bool:
    """Ustawia dźwignię na SYMBOL."""
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        return True
    except Exception as e:
        # Binance rzuca wyjątek gdy dźwignia już ustawiona na tę wartość
        if "No need to change leverage" in str(e):
            return True
        log.warning(f"[exchange] set_leverage: {e}")
        return False


# ── Wybór typu conditional order ───────────────────────────────────────────────

def _conditional_order_type(direction: str, entry_trigger: str) -> str:
    """
    Zwraca typ Binance Futures dla conditional entry order.
    STOP       = triggeruje gdy cena idzie W KIERUNKU zlecenia (breakout/breakdown)
    TAKE_PROFIT = triggeruje gdy cena idzie PRZECIWNIE (pullback)
    """
    if direction == "long":
        return "STOP" if entry_trigger == "rising" else "TAKE_PROFIT"
    else:  # short
        return "STOP" if entry_trigger == "falling" else "TAKE_PROFIT"


# ── Składanie zleceń ───────────────────────────────────────────────────────────

def _place_conditional(client, s: dict) -> int | None:
    """
    Składa conditional stop-limit order przy W1.
    Nie blokuje margin do momentu aktywacji.
    Zwraca orderId (int) lub None przy błędzie.
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    qty       = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    side      = "BUY" if direction == "long" else "SELL"
    et        = s.get("entry_trigger", "falling")
    cond_type = _conditional_order_type(direction, et)

    try:
        resp = client.new_order(
            symbol=SYMBOL,
            side=side,
            type=cond_type,
            quantity=_fmt_qty(qty),
            price=_fmt_price(w1),
            stopPrice=_fmt_price(w1),
            timeInForce="GTC",
            workingType="MARK_PRICE",
            priceProtect=True,
        )
        oid = resp["orderId"]
        print(f"[exchange] Conditional {cond_type} złożony: {oid} | {side} {_fmt_qty(qty)} SOL @ {w1}")
        return oid
    except Exception as e:
        log.error(f"[exchange] place_conditional: {e}")
        return None


def _cancel_order(client, order_id: int):
    """Anuluje zlecenie po orderId."""
    try:
        client.cancel_order(symbol=SYMBOL, orderId=order_id)
        print(f"[exchange] Order anulowany: {order_id}")
    except Exception as e:
        log.warning(f"[exchange] cancel_order {order_id}: {e}")


def _place_tp_sl_orders(client, s: dict) -> tuple[int | None, int | None, int | None]:
    """
    Po wejściu w pozycję: składa TP1, TP2 (limit reduce-only) i SL (stop-market).
    Zwraca (tp1_order_id, tp2_order_id, sl_order_id).
    """
    direction = s["direction"]
    w1        = s["entries"][0]
    full_qty  = _round_qty((TRADE_USDT * LEVERAGE) / w1)
    half_qty  = _round_qty(full_qty / 2)
    tp_side   = "SELL" if direction == "long" else "BUY"
    sl_side   = "SELL" if direction == "long" else "BUY"
    tps       = s.get("tps", [])
    sl        = s["sl"]
    tp1       = tps[0] if tps else None
    tp2       = tps[1] if len(tps) > 1 else None
    tp1_id    = None
    tp2_id    = None
    sl_id     = None

    # TP1 — 50% pozycji, limit reduce-only
    if tp1:
        try:
            resp = client.new_order(
                symbol=SYMBOL,
                side=tp_side,
                type="LIMIT",
                quantity=_fmt_qty(half_qty),
                price=_fmt_price(tp1),
                timeInForce="GTC",
                reduceOnly=True,
            )
            tp1_id = resp["orderId"]
            print(f"[exchange] TP1 order: {tp1_id} | {_fmt_qty(half_qty)} SOL @ {tp1}")
        except Exception as e:
            log.error(f"[exchange] TP1: {e}")

    # TP2 — pozostałe 50%, limit reduce-only
    if tp2:
        try:
            resp = client.new_order(
                symbol=SYMBOL,
                side=tp_side,
                type="LIMIT",
                quantity=_fmt_qty(half_qty),
                price=_fmt_price(tp2),
                timeInForce="GTC",
                reduceOnly=True,
            )
            tp2_id = resp["orderId"]
            print(f"[exchange] TP2 order: {tp2_id} | {_fmt_qty(half_qty)} SOL @ {tp2}")
        except Exception as e:
            log.error(f"[exchange] TP2: {e}")

    # SL — stop-market reduce-only na pełną pozycję
    sl_id = _place_sl_order(client, sl_side, full_qty, sl)

    return tp1_id, tp2_id, sl_id


def _place_sl_order(client, sl_side: str, qty: float, sl_price: float) -> int | None:
    """Składa SL jako stop-market reduce-only. Zwraca orderId."""
    try:
        resp = client.new_order(
            symbol=SYMBOL,
            side=sl_side,
            type="STOP_MARKET",
            quantity=_fmt_qty(qty),
            stopPrice=_fmt_price(sl_price),
            workingType="MARK_PRICE",
            priceProtect=True,
            reduceOnly=True,
        )
        oid = resp["orderId"]
        print(f"[exchange] SL order: {oid} | {_fmt_qty(qty)} SOL stop @ {sl_price}")
        return oid
    except Exception as e:
        log.error(f"[exchange] SL order: {e}")
        return None


def _update_sl(client, s: dict, new_sl: float):
    """
    Przesuwa SL po trafieniu TP1:
    anuluje stary SL, składa nowy stop-market na pozostałe 50%.
    """
    old_sl_id = s.get("exchange_sl_order_id")
    if old_sl_id:
        _cancel_order(client, old_sl_id)

    direction = s["direction"]
    w1        = s["entries"][0]
    half_qty  = _round_qty(_round_qty((TRADE_USDT * LEVERAGE) / w1) / 2)
    sl_side   = "SELL" if direction == "long" else "BUY"

    new_sl_id = _place_sl_order(client, sl_side, half_qty, new_sl)
    if new_sl_id:
        s["exchange_sl_order_id"] = new_sl_id
        be_label = "BE" if abs(new_sl - w1) < 0.05 else f"+${abs(new_sl - w1):.2f}"
        print(f"[exchange] SL przesunięty po TP1 → {new_sl} ({be_label})")


# ── Sprawdzanie statusu zleceń ─────────────────────────────────────────────────

def _order_status(client, order_id: int) -> str:
    """
    Zwraca status zlecenia: 'new' | 'filled' | 'cancelled' | 'unknown'
    """
    try:
        resp = client.query_order(symbol=SYMBOL, orderId=order_id)
        status = resp.get("status", "")
        if status == "FILLED":
            return "filled"
        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            return "cancelled"
        if status in ("NEW", "PARTIALLY_FILLED"):
            return "new"
    except Exception as e:
        log.warning(f"[exchange] query_order {order_id}: {e}")
    return "unknown"


# ── Główna funkcja synchronizacji ─────────────────────────────────────────────

def sync():
    """
    Główna pętla — wywoływana co 15 min przez sol_alert.py.

    Dla każdego setupu w pending_setups.json:
      - NOWY:      składa conditional order (nie blokuje margin)
      - ANULOWANY: anuluje order na Binance
      - OCZEKUJĄCY: sprawdza czy conditional się wypełnił → otwiera TP1/TP2/SL
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
            print(f"[exchange] {label}: setup anulowany → cancel {order_id}")
            _cancel_order(client, order_id)
            s["exchange_order_id"] = None
            modified = True
            continue

        # ── Nowy setup — złóż conditional order ─────────────────────────────
        if not shadow and not cancelled and not order_id and s["entry_hit_at"] is None:
            w1  = entries[0]
            qty = _round_qty((TRADE_USDT * LEVERAGE) / w1)
            oid = _place_conditional(client, s)
            if oid:
                s["exchange_order_id"]         = oid
                s["exchange_position_opened"]  = False
                s["exchange_tp1_order_id"]     = None
                s["exchange_tp2_order_id"]     = None
                s["exchange_sl_order_id"]      = None
                s["exchange_qty"]              = _fmt_qty(qty)
                modified = True
                print(f"[exchange] {label}: conditional → {oid} ({_fmt_qty(qty)} SOL @ {w1})")
            continue

        # ── Oczekujący order — sprawdź czy wypełniony ─────────────────────────
        if order_id and not opened:
            status = _order_status(client, order_id)
            if status == "new":
                continue  # jeszcze czeka
            if status == "cancelled":
                print(f"[exchange] {label}: conditional anulowany z zewnątrz")
                s["exchange_order_id"] = None
                modified = True
                continue
            if status == "filled":
                print(f"[exchange] {label}: entry wypełniony! Składam TP1/TP2/SL.")
                tp1_id, tp2_id, sl_id = _place_tp_sl_orders(client, s)
                s["exchange_position_opened"] = True
                s["exchange_tp1_order_id"]    = tp1_id
                s["exchange_tp2_order_id"]    = tp2_id
                s["exchange_sl_order_id"]     = sl_id
                modified = True
            continue

        # ── Pozycja otwarta — sprawdź TP1, ewentualnie przesuń SL ────────────
        if opened and not s.get("sl_adjusted"):
            tp1_oid = s.get("exchange_tp1_order_id")
            if tp1_oid and _order_status(client, tp1_oid) == "filled":
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
