#!/usr/bin/env python3
"""
test_apr3.py — Porównanie starego vs nowego algorytmu detekcji reżimu.
Testuje wszystkie aktywne typy setupów (trend_consolidation_short/long wyłączone jak w produkcji).
Pokazuje statystyki per typ setupu.

Uruchomienie:
    python test_apr3.py --from "2026-03-01 00:00" --to "2026-04-04 00:00"
"""

import argparse
import json
import os
import requests
from datetime import datetime, timezone

# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_atr(candles: list[dict], period: int = 14) -> float:
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for c, p in zip(candles[1:], candles)]
    return sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0

def h1_trend(candles_h1: list[dict]) -> str:
    closes = [c["close"] for c in candles_h1[-20:]]
    pct = (sum(closes[-5:]) / 5 - sum(closes[-20:]) / 20) / (sum(closes[-20:]) / 20) * 100
    if pct > 1.0:  return "bullish"
    if pct < -1.0: return "bearish"
    return "neutral"

def impulse_strength(candles_m15: list[dict]) -> int:
    atr = calc_atr(candles_m15)
    sizes = [abs(c["close"] - c["open"]) for c in candles_m15[-15:-5]]
    ratio = (sum(sizes) / len(sizes) if sizes else 0) / atr if atr > 0 else 0
    if ratio >= 1.4: return 3
    if ratio >= 0.9: return 2
    if ratio >= 0.5: return 1
    return 0

def find_swing_points(candles_h1: list[dict], n: int = 12):
    recent = candles_h1[-n:]
    return max(c["high"] for c in recent), min(c["low"] for c in recent)

def detect_range(candles_h1: list[dict], n: int = 32) -> dict:
    recent = candles_h1[-n:]
    resistance = max(c["high"] for c in recent)
    support    = min(c["low"]  for c in recent)
    return {"resistance": resistance, "support": support, "range_size": resistance - support}

# ── API fetch ─────────────────────────────────────────────────────────────────

def fetch_klines_okx(symbol: str, interval: str, total: int, end_ts_s: int) -> list[dict]:
    okx_bar = {"15m": "15m", "1h": "1H"}[interval]
    result = []
    after_ms = str(int(end_ts_s * 1000))

    while len(result) < total:
        params = {"instId": "SOL-USDT-SWAP", "bar": okx_bar, "limit": "100"}
        if after_ms:
            params["after"] = after_ms
        try:
            r = requests.get("https://www.okx.com/api/v5/market/history-candles",
                             params=params, timeout=15)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break
            batch = [{"time": int(d[0]) // 1000, "open": float(d[1]), "high": float(d[2]),
                      "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])}
                     for d in data]
            result.extend(batch)
            after_ms = str(min(int(d[0]) for d in data) - 1)
            if len(data) < 100:
                break
        except Exception as e:
            print(f"  [OKX error: {e}]")
            break

    result.sort(key=lambda c: c["time"])
    result = [c for c in result if c["time"] < end_ts_s]
    return result[-total:] if len(result) > total else result

# ── Detekcja reżimu — STARY ───────────────────────────────────────────────────

def detect_regime_old(candles_m15, candles_h1, current_price):
    """Zwraca (regime_str, direction, score)."""
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_4h  = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    price_24h = candles_h1[-24]["close"]  if len(candles_h1) >= 24  else candles_h1[0]["close"]
    price_48h = candles_h1[-48]["close"]  if len(candles_h1) >= 48  else candles_h1[0]["close"]
    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    last4 = candles_m15[-4:]
    bearish = sum(1 for c in last4 if c["close"] < c["open"])
    bullish = sum(1 for c in last4 if c["close"] > c["open"])

    h1_12 = candles_h1[-12:]
    lower_lows   = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
    higher_highs = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

    impulse_score = 0
    impulse_dir = "none"
    if imp_str >= 2: impulse_score += 1
    if vol_ratio >= 1.5: impulse_score += 1
    if abs(change_4h) >= 2.0: impulse_score += 1
    elif abs(change_4h) >= 1.5 and vol_ratio >= 1.3: impulse_score += 1
    if bearish >= 3: impulse_score += 1; impulse_dir = "down"
    elif bullish >= 3: impulse_score += 1; impulse_dir = "up"

    if impulse_score >= 3:
        if impulse_dir == "none":
            impulse_dir = "down" if change_4h < 0 else "up"
        return f"IMPULSE_{impulse_dir.upper()}", impulse_dir, min(10, impulse_score * 2 + imp_str)

    trend_score = 0
    if abs(change_24h) >= 3.0: trend_score += 2
    elif abs(change_24h) >= 1.5: trend_score += 1
    if abs(change_48h) >= 5.0: trend_score += 2
    elif abs(change_48h) >= 3.0: trend_score += 1
    if lower_lows >= 5: trend_score += 1
    if higher_highs >= 5: trend_score += 1
    if trend != "neutral": trend_score += 1

    has_price_change = abs(change_24h) >= 1.5 or abs(change_48h) >= 3.0

    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"
        return f"TREND_{trend_dir.upper()}", trend_dir, trend_score

    return "RANGE", "none", 0


# ── Detekcja reżimu — NOWY (stary algo + wygładzone referencje 24h/48h) ───────

def detect_regime_new(candles_m15, candles_h1, current_price):
    """
    Stary algorytm change_24h/48h, ale zamiast jednej świecy — średnia z 3 świec
    w okolicy punktu referencyjnego. Eliminuje niestabilność bez zmiany logiki.
    Zwraca (regime_str, direction, score, change_24h).
    """
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_4h  = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]

    # Wygładzone referencje — średnia 3 świec wokół punktu 24h / 48h wstecz
    price_24h = (sum(c["close"] for c in candles_h1[-25:-22]) / 3
                 if len(candles_h1) >= 25 else candles_h1[0]["close"])
    price_48h = (sum(c["close"] for c in candles_h1[-49:-46]) / 3
                 if len(candles_h1) >= 49 else candles_h1[0]["close"])

    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    last4 = candles_m15[-4:]
    bearish = sum(1 for c in last4 if c["close"] < c["open"])
    bullish = sum(1 for c in last4 if c["close"] > c["open"])

    h1_12 = candles_h1[-12:]
    lower_lows   = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
    higher_highs = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

    # IMPULSE
    impulse_score = 0
    impulse_dir = "none"
    if imp_str >= 2: impulse_score += 1
    if vol_ratio >= 1.5: impulse_score += 1
    if abs(change_4h) >= 2.0: impulse_score += 1
    elif abs(change_4h) >= 1.5 and vol_ratio >= 1.3: impulse_score += 1
    if bearish >= 3: impulse_score += 1; impulse_dir = "down"
    elif bullish >= 3: impulse_score += 1; impulse_dir = "up"

    if impulse_score >= 3:
        if impulse_dir == "none":
            impulse_dir = "down" if change_4h < 0 else "up"
        return f"IMPULSE_{impulse_dir.upper()}", impulse_dir, min(10, impulse_score * 2 + imp_str), change_24h

    # TREND
    trend_score = 0
    if abs(change_24h) >= 3.0: trend_score += 2
    elif abs(change_24h) >= 1.5: trend_score += 1
    if abs(change_48h) >= 5.0: trend_score += 2
    elif abs(change_48h) >= 3.0: trend_score += 1
    if lower_lows >= 5: trend_score += 1
    if higher_highs >= 5: trend_score += 1
    if trend != "neutral": trend_score += 1

    has_price_change = abs(change_24h) >= 1.5 or abs(change_48h) >= 3.0

    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"
        return f"TREND_{trend_dir.upper()}", trend_dir, trend_score, change_24h

    return "RANGE", "none", 0, change_24h


# ── Generowanie setupów (wszystkie aktywne typy) ──────────────────────────────

def generate_setups(regime_str, direction, score, candles_m15, candles_h1, current_price):
    """
    Generuje setupy identycznie jak algo_detect_setups w sol_alert.py.
    Wyłączone: trend_consolidation_short, trend_consolidation_long (jak w produkcji).
    Zwraca listę dictów: {type, direction, w, sl, tp1, tp2, rr}
    """
    setups = []
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    if atr <= 0:
        return setups
    max_dist = current_price * 0.03

    if direction == "down":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        swing_low  = min(swing_low,  current_price)
        swing_high = max(swing_high, current_price)

        # trend_consolidation_short — WYŁĄCZONY

        # trend_pullback_short — fib 38-50%
        if swing_high > swing_low:
            swing_range = swing_high - swing_low
            fib38 = swing_low + swing_range * 0.38
            fib50 = swing_low + swing_range * 0.50
            fib618 = swing_low + swing_range * 0.618
            w   = round((fib38 + fib50) / 2, 2)
            sl  = round(fib618 + atr * 0.3, 2)
            tp1 = round(swing_low, 2)
            tp2 = round(swing_low - swing_range * 0.3, 2)
            rr_val = (w - tp1) / (sl - w) if sl > w else 0
            if sl > w and tp1 < w and rr_val >= 1.5 and w > current_price * 1.003 and (w - current_price) <= max_dist:
                setups.append({"type": "trend_pullback_short", "direction": "short",
                                "w": w, "sl": sl, "sl_after_tp1": w, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

        # impulse_continuation_short — tylko przy IMPULSE
        if regime_str.startswith("IMPULSE_"):
            last6 = candles_m15[-6:]
            greens = [c for c in last6 if c["close"] > c["open"]]
            if 1 <= len(greens) <= 2:
                pullback_high = max(c["high"] for c in last6[-2:])
                w   = round(pullback_high, 2)
                sl  = round(pullback_high + atr * 0.8, 2)
                tp1 = round(swing_low, 2)
                tp2 = round(swing_low - atr, 2)
                rr_val = (w - tp1) / (sl - w) if sl > w else 0
                if sl > w and tp1 < w and rr_val >= 1.5 and abs(w - current_price) <= max_dist:
                    setups.append({"type": "impulse_continuation_short", "direction": "short",
                                   "w": w, "sl": sl, "sl_after_tp1": w, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

    elif direction == "up":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        swing_low  = min(swing_low,  current_price)
        swing_high = max(swing_high, current_price)

        # trend_consolidation_long — WYŁĄCZONY

        # trend_pullback_long — fib 38-50% (wymaga score >= 5)
        if swing_high > swing_low and score >= 5:
            swing_range = swing_high - swing_low
            fib38  = swing_high - swing_range * 0.38
            fib50  = swing_high - swing_range * 0.50
            fib618 = swing_high - swing_range * 0.618
            w   = round((fib38 + fib50) / 2, 2)
            sl  = round(fib618 - atr * 0.3, 2)
            tp1 = round(swing_high, 2)
            tp2 = round(swing_high + swing_range * 0.3, 2)
            rr_val = (tp1 - w) / (w - sl) if w > sl else 0
            if sl < w and tp1 > w and rr_val >= 1.5 and w < current_price * 0.997 and (current_price - w) <= max_dist:
                setups.append({"type": "trend_pullback_long", "direction": "long",
                                "w": w, "sl": sl, "sl_after_tp1": w, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

    elif regime_str == "RANGE":
        rng = detect_range(candles_h1)
        sup, res = rng["support"], rng["resistance"]
        rng_size = res - sup
        if rng_size > atr * 1.5:
            # range_resistance_short
            w   = round(res - rng_size * 0.1, 2)
            sl  = round(res + atr * 1.0, 2)
            tp1 = round(sup + rng_size * 0.5, 2)
            tp2 = round(sup + rng_size * 0.1, 2)
            rr_val = (w - tp1) / (sl - w) if sl > w else 0
            if rr_val >= 1.5 and abs(w - current_price) <= max_dist:
                setups.append({"type": "range_resistance_short", "direction": "short",
                                "w": w, "sl": sl, "sl_after_tp1": w, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})
            # range_support_long
            w   = round(sup + rng_size * 0.1, 2)
            sl  = round(sup - atr * 1.0, 2)
            tp1 = round(sup + rng_size * 0.5, 2)
            tp2 = round(res - rng_size * 0.1, 2)
            rr_val = (tp1 - w) / (w - sl) if w > sl else 0
            if rr_val >= 1.5 and abs(w - current_price) <= max_dist:
                setups.append({"type": "range_support_long", "direction": "long",
                                "w": w, "sl": sl, "sl_after_tp1": w, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

    return setups


# ── Ewaluacja setupu na przyszłych świecach M15 ───────────────────────────────

def evaluate(setup: dict, future_m15: list[dict], window_h: int = 24) -> str:
    if not setup or not future_m15:
        return "-"
    w, sl, tp1 = setup["w"], setup["sl"], setup["tp1"]
    direction = setup.get("direction", "short")
    window_s = window_h * 3600
    t0 = future_m15[0]["time"]

    entry_ts = None
    for c in future_m15:
        if c["time"] > t0 + window_s:
            break
        if direction == "short" and c["high"] >= w:
            entry_ts = c["time"]; break
        if direction == "long" and c["low"] <= w:
            entry_ts = c["time"]; break

    if entry_ts is None:
        return "no_entry"

    for c in future_m15:
        if c["time"] < entry_ts:
            continue
        if direction == "short":
            if c["low"]  <= tp1: return "TP1 ✓"
            if c["high"] >= sl:  return "SL ✗"
        else:
            if c["high"] >= tp1: return "TP1 ✓"
            if c["low"]  <= sl:  return "SL ✗"
    return "open"


def pnl_pts(setup: dict, res: str) -> float:
    if not setup: return 0.0
    if "TP1" in res: return round(abs(setup["w"] - setup["tp1"]), 2)
    if "SL"  in res: return round(abs(setup["sl"] - setup["w"]),  2) * -1
    return 0.0


def evaluate_extended(setup: dict, future_m15: list[dict], window_h: int = 24):
    """
    Ewaluuje setup z podziałem na 3 scenariusze zamknięcia.
    Zwraca (wynik, pnl) gdzie:
      wynik: "no_entry" | "SL" | "TP1+TP2" | "TP1+BE" | "TP1+open" | "open"
      pnl:   P&L w punktach (50/50 split przy TP1)
        SL        → -(sl - w)             [pełna strata]
        TP1+TP2   → 0.5*(w-tp1)+0.5*(w-tp2)  [połowa na TP1, połowa na TP2]
        TP1+BE    → 0.5*(w-tp1)           [połowa na TP1, połowa na BE=0]
        TP1+open  → 0.5*(w-tp1)           [TP1 trafiony, druga połowa otwarta]
    """
    if not setup or not future_m15:
        return "no_entry", 0.0

    w        = setup["w"]
    sl       = setup["sl"]
    tp1      = setup["tp1"]
    tp2      = setup["tp2"]
    sl_be    = setup.get("sl_after_tp1", w)  # BE = punkt wejścia
    direction = setup.get("direction", "short")
    window_s = window_h * 3600
    t0       = future_m15[0]["time"]

    # Faza 1: szukaj entry
    entry_ts = None
    for c in future_m15:
        if c["time"] > t0 + window_s:
            break
        if direction == "short" and c["high"] >= w:
            entry_ts = c["time"]; break
        if direction == "long"  and c["low"]  <= w:
            entry_ts = c["time"]; break

    if entry_ts is None:
        return "no_entry", 0.0

    # Faza 2: czekaj na TP1 lub SL
    tp1_ts = None
    for c in future_m15:
        if c["time"] < entry_ts:
            continue
        if direction == "short":
            if c["low"]  <= tp1: tp1_ts = c["time"]; break
            if c["high"] >= sl:
                return "SL", round(-(sl - w), 2)
        else:
            if c["high"] >= tp1: tp1_ts = c["time"]; break
            if c["low"]  <= sl:
                return "SL", round(-(w - sl), 2)

    if tp1_ts is None:
        return "open", 0.0

    pnl_half1 = abs(w - tp1) * 0.5  # zysk z pierwszej połowy

    # Faza 3: po TP1 — czekaj na TP2 lub SL@BE
    for c in future_m15:
        if c["time"] < tp1_ts:
            continue
        if direction == "short":
            if c["low"]  <= tp2:
                return "TP1+TP2", round(pnl_half1 + abs(w - tp2) * 0.5, 2)
            if c["high"] >= sl_be:
                return "TP1+BE",  round(pnl_half1, 2)
        else:
            if c["high"] >= tp2:
                return "TP1+TP2", round(pnl_half1 + abs(tp2 - w) * 0.5, 2)
            if c["low"]  <= sl_be:
                return "TP1+BE",  round(pnl_half1, 2)

    return "TP1+open", round(pnl_half1, 2)


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())

ALL_TYPES = [
    "trend_pullback_short",
    "impulse_continuation_short",
    "trend_pullback_long",
    "range_resistance_short",
    "range_support_long",
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="dt_from", default="2026-03-01 00:00")
    parser.add_argument("--to",   dest="dt_to",   default="2026-04-04 00:00")
    args = parser.parse_args()

    from_ts = _parse_dt(args.dt_from)
    to_ts   = _parse_dt(args.dt_to)

    fetch_end = to_ts + 24 * 3600
    m15_needed = 100 + (fetch_end - from_ts) // 900
    h1_needed  = 200 + (fetch_end - from_ts) // 3600  # 200 zapas na MA20/swing

    print(f"Pobieranie danych: {args.dt_from} – {args.dt_to} (+24h ewaluacja)...")
    m15_all = fetch_klines_okx("SOLUSDT", "15m", m15_needed, fetch_end)
    h1_all  = fetch_klines_okx("SOLUSDT", "1h",  h1_needed,  fetch_end)

    if not m15_all or not h1_all:
        print("Brak danych — sprawdź połączenie z API")
        return

    print(f"M15: {len(m15_all)} świec | H1: {len(h1_all)} świec")
    print()

    # stats[type]["old"/"new"] = {tp, sl, no, pnl}
    stats = {t: {"old": {"tp": 0, "sl": 0, "no": 0, "pnl": 0.0},
                 "new": {"tp": 0, "sl": 0, "no": 0, "pnl": 0.0}}
             for t in ALL_TYPES}
    sheet_rows: list[dict] = []

    # Per-hour tabela: tylko reżimy
    hdr = f"{'Godz':>11}  {'Cena':>7}  {'slope':>7}  {'STARY':>14}  {'NOWY':>14}  {'setup old':>28}  {'setup new':>28}"
    print(hdr)
    print("-" * len(hdr))

    for ts in range(from_ts, to_ts, 3600):
        m15_snap = [c for c in m15_all if c["time"] < ts]
        h1_snap  = [c for c in h1_all  if c["time"] < ts]
        future   = [c for c in m15_all if c["time"] >= ts]

        if len(m15_snap) < 20 or len(h1_snap) < 25:
            continue

        price = m15_snap[-1]["close"]
        dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m %H:%M")

        old_r, old_dir, old_score        = detect_regime_old(m15_snap, h1_snap, price)
        new_r, new_dir, new_score, slope = detect_regime_new(m15_snap, h1_snap, price)

        old_setups = generate_setups(old_r, old_dir, old_score, m15_snap, h1_snap, price)
        new_setups = generate_setups(new_r, new_dir, new_score, m15_snap, h1_snap, price)

        # Akumuluj statystyki + zbieraj wiersze dla Sheets (tylko nowy algo)
        for s in old_setups:
            res = evaluate(s, future)
            p   = pnl_pts(s, res)
            st  = stats[s["type"]]["old"]
            if "TP1" in res: st["tp"] += 1
            elif "SL" in res: st["sl"] += 1
            else:             st["no"] += 1
            st["pnl"] += p

        for s in new_setups:
            res = evaluate(s, future)
            p   = pnl_pts(s, res)
            st  = stats[s["type"]]["new"]
            if "TP1" in res: st["tp"] += 1
            elif "SL" in res: st["sl"] += 1
            else:             st["no"] += 1
            st["pnl"] += p
            # Extended ewaluacja dla Sheets
            ext_res, ext_pnl = evaluate_extended(s, future)
            sheet_rows.append({
                "ts":      datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "type":    s["type"],
                "dir":     s["direction"],
                "regime":  new_r,
                "w":       s["w"],
                "sl":      s["sl"],
                "tp1":     s["tp1"],
                "tp2":     s["tp2"],
                "rr":      s["rr"],
                "wynik":   ext_res,
                "pnl":     ext_pnl,
            })

        # Skrócone nazwy setupów do wyświetlenia
        def fmt_setups(sl):
            if not sl: return f"{'—':>28}"
            names = [s["type"].replace("trend_","t_").replace("impulse_","imp_").replace("range_","r_")
                     .replace("_short","↓").replace("_long","↑").replace("continuation","cont")
                     .replace("pullback","pb").replace("resistance","res").replace("support","sup")
                     for s in sl]
            return ", ".join(names)[:28].ljust(28)

        regime_mark = " ◄" if old_r != new_r else ""
        print(f"{dt_str}  {price:>7.2f}  c24={slope:>+5.1f}%  {old_r:>14}  {new_r:>14}  "
              f"{fmt_setups(old_setups)}  {fmt_setups(new_setups)}{regime_mark}")

    # ── Podsumowanie per typ ───────────────────────────────────────────────────
    print()
    print("=" * 90)
    print(f"{'Typ setupu':<30}  {'--- STARY ---':>32}  {'--- NOWY ---':>32}")
    print(f"{'':30}  {'TP1':>4} {'SL':>4} {'brak':>4} {'setupy':>6} {'P&L':>8}  "
          f"{'TP1':>4} {'SL':>4} {'brak':>4} {'setupy':>6} {'P&L':>8}")
    print("-" * 90)

    tot_old = {"tp": 0, "sl": 0, "no": 0, "pnl": 0.0}
    tot_new = {"tp": 0, "sl": 0, "no": 0, "pnl": 0.0}

    for t in ALL_TYPES:
        o = stats[t]["old"]
        n = stats[t]["new"]
        n_o = o["tp"] + o["sl"] + o["no"]
        n_n = n["tp"] + n["sl"] + n["no"]
        print(f"{t:<30}  {o['tp']:>4} {o['sl']:>4} {o['no']:>4} {n_o:>6} {o['pnl']:>+8.2f}  "
              f"{n['tp']:>4} {n['sl']:>4} {n['no']:>4} {n_n:>6} {n['pnl']:>+8.2f}")
        for k in ("tp", "sl", "no", "pnl"):
            tot_old[k] += o[k]
            tot_new[k] += n[k]

    print("-" * 90)
    n_o = tot_old["tp"] + tot_old["sl"] + tot_old["no"]
    n_n = tot_new["tp"] + tot_new["sl"] + tot_new["no"]
    print(f"{'RAZEM':<30}  {tot_old['tp']:>4} {tot_old['sl']:>4} {tot_old['no']:>4} {n_o:>6} {tot_old['pnl']:>+8.2f}  "
          f"{tot_new['tp']:>4} {tot_new['sl']:>4} {tot_new['no']:>4} {n_n:>6} {tot_new['pnl']:>+8.2f}")

    # ── Zapis do Google Sheets ────────────────────────────────────────────────
    _write_to_sheets(args.dt_from, args.dt_to, sheet_rows)


def _write_to_sheets(dt_from: str, dt_to: str, rows: list[dict]):
    creds_json = os.getenv("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        print("Brak GOOGLE_CREDENTIALS — pomijam zapis do Sheets")
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds  = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        client = gspread.authorize(creds)
        SHEET_ID = "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE"
        wb = client.open_by_key(SHEET_ID)

        sheet_name = f"Backtest_Regime {dt_from[:10]}–{dt_to[:10]}"

        HEADER = [
            "Timestamp", "Typ setupu", "Kierunek", "Reżim",
            "W", "SL", "TP1", "TP2", "RR",
            "Wynik", "P&L pkt",
        ]

        try:
            sh = wb.worksheet(sheet_name)
            sh.clear()
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet(sheet_name, rows=len(rows) + 10, cols=12)

        # Batch update — szybciej niż append_row w pętli
        data = [HEADER] + [
            [r["ts"], r["type"], r["dir"], r["regime"],
             r["w"], r["sl"], r["tp1"], r["tp2"], r["rr"],
             r["wynik"], r["pnl"]]
            for r in rows
        ]
        sh.update("A1", data)

        print(f"\nZapisano {len(rows)} setupów do Sheets: '{sheet_name}'")

    except Exception as e:
        print(f"Błąd zapisu do Sheets: {e}")


if __name__ == "__main__":
    main()
