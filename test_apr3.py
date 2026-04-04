#!/usr/bin/env python3
"""
test_apr3.py — Test nowego algorytmu detekcji reżimu.
Testuje wszystkie aktywne typy setupów (trend_consolidation_short/long wyłączone jak w produkcji).
Oblicza PnL $100 @ 20x dla wariantów TP1only i TP1+TP2.
Eksportuje wyniki do Google Sheets.

Uruchomienie:
    python test_apr3.py --from "2026-03-01 00:00" --to "2026-04-04 00:00"
"""

import argparse
import json
import math
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
                                "w": w, "sl": sl, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

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
                                   "w": w, "sl": sl, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

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
                                "w": w, "sl": sl, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

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
                                "w": w, "sl": sl, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})
            # range_support_long
            w   = round(sup + rng_size * 0.1, 2)
            sl  = round(sup - atr * 1.0, 2)
            tp1 = round(sup + rng_size * 0.5, 2)
            tp2 = round(res - rng_size * 0.1, 2)
            rr_val = (tp1 - w) / (w - sl) if w > sl else 0
            if rr_val >= 1.5 and abs(w - current_price) <= max_dist:
                setups.append({"type": "range_support_long", "direction": "long",
                                "w": w, "sl": sl, "tp1": tp1, "tp2": tp2, "rr": round(rr_val, 1)})

    return setups


# ── Ewaluacja setupu na przyszłych świecach M15 ───────────────────────────────

ENTRY_WINDOW_H = 24  # max czas oczekiwania na wejście
TRADE_WINDOW_H = 24  # max czas trwania pozycji

# Próg duplikatu (jak w produkcji: ABS(W_nowy - W_aktywny) < 0.5)
DEDUP_THRESHOLD = 0.5


def evaluate(setup: dict, future_m15: list[dict]) -> tuple[str, int | None]:
    """Ewaluuje setup na przyszłych świecach.

    Symuluje zachowanie produkcyjne:
    - Szuka wejścia przez max ENTRY_WINDOW_H
    - Po TP1: przesuwa SL do W (breakeven) — symulacja sl_after_tp1
    - Monitoruje przez max TRADE_WINDOW_H

    Wyniki:
      'TP2'    — TP1 potem TP2
      'TP1+W'  — TP1 potem SL trafił W (breakeven)
      'TP1'    — TP1 trafiony, brak TP2 w setupie
      'SL'     — SL przed TP1
      'no_entry' — brak wejścia w oknie ENTRY_WINDOW_H
      'open'   — nie rozwiązany w oknie TRADE_WINDOW_H

    Zwraca (wynik, timestamp_rozwiązania).
    """
    if not setup or not future_m15:
        return "-", None

    w, sl, tp1 = setup["w"], setup["sl"], setup["tp1"]
    tp2 = setup.get("tp2")
    d   = setup.get("direction", "short")
    t0  = future_m15[0]["time"]
    entry_window_end = t0 + ENTRY_WINDOW_H * 3600
    trade_window_end = t0 + (ENTRY_WINDOW_H + TRADE_WINDOW_H) * 3600

    # Szukaj wejścia
    entry_ts = None
    for c in future_m15:
        if c["time"] > entry_window_end:
            break
        if d == "short" and c["high"] >= w:
            entry_ts = c["time"]; break
        if d == "long"  and c["low"]  <= w:
            entry_ts = c["time"]; break

    if entry_ts is None:
        return "no_entry", None

    # Monitoruj pozycję; po TP1 przesuń SL do W (breakeven)
    effective_sl = sl
    tp1_hit_at   = None

    for c in future_m15:
        if c["time"] < entry_ts:
            continue
        if c["time"] > trade_window_end:
            break

        if d == "short":
            tp1_now = c["low"]  <= tp1
            tp2_now = tp2 is not None and c["low"]  <= tp2
            sl_now  = c["high"] >= effective_sl
        else:
            tp1_now = c["high"] >= tp1
            tp2_now = tp2 is not None and c["high"] >= tp2
            sl_now  = c["low"]  <= effective_sl

        if tp2_now:
            return "TP2", c["time"]
        if tp1_now and tp1_hit_at is None:
            # TP1 ma priorytet nad SL w tej samej świecy (jak w starym kodzie)
            if tp2 is None:
                return "TP1", c["time"]
            tp1_hit_at   = c["time"]
            effective_sl = w   # przesuń SL do W po TP1
            continue
        if sl_now:
            result = "TP1+W" if tp1_hit_at is not None else "SL"
            return result, c["time"]

    return "TP1+W" if tp1_hit_at is not None else "open", tp1_hit_at


TRADE_USDT = 100.0
LEVERAGE   = 20


def calc_pnl_scenarios(setup: dict, result: str) -> tuple[float, float]:
    """Zwraca (pnl_tp1only, pnl_tp1tp2) w USD dla $100 @ 20x dźwigni.

    Scenariusze:
      SL      → obie strategie: cała pozycja wychodzi na SL
      TP1     → obie strategie: cała pozycja wychodzi na TP1 (brak TP2 w setupie)
      TP2     → TP1only: cała na TP1 | TP1+TP2: połowa na TP1, połowa na TP2
      TP1+W   → TP1only: cała na TP1 | TP1+TP2: połowa na TP1, połowa na W (BE, zysk=0)
    """
    if result in ("no_entry", "open", "-"):
        return 0.0, 0.0

    w    = setup["w"]
    sl   = setup["sl"]
    tp1  = setup["tp1"]
    tp2  = setup.get("tp2")
    sign = 1 if setup.get("direction", "short") == "long" else -1

    full_qty = max(math.floor((TRADE_USDT * LEVERAGE / w) / 0.1) * 0.1, 0.1)
    half_qty = max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)

    # ── TP1only ──────────────────────────────────────────────────────────────
    if result in ("TP1", "TP2", "TP1+W"):
        pnl_tp1only = round(sign * full_qty * (tp1 - w), 2)
    else:  # SL
        pnl_tp1only = round(sign * full_qty * (sl - w), 2)

    # ── TP1+TP2 ──────────────────────────────────────────────────────────────
    if result == "TP2" and tp2 is not None:
        # Połowa na TP1, połowa na TP2
        pnl_tp1tp2 = round(sign * half_qty * (tp1 - w) + sign * half_qty * (tp2 - w), 2)
    elif result == "TP1+W":
        # Połowa na TP1, połowa na W (breakeven — zysk 0 na drugiej połowie)
        pnl_tp1tp2 = round(sign * half_qty * (tp1 - w), 2)
    elif result == "TP1":
        # Brak TP2 w setupie → cała pozycja na TP1 (jak TP1only)
        pnl_tp1tp2 = round(sign * full_qty * (tp1 - w), 2)
    else:  # SL
        pnl_tp1tp2 = round(sign * full_qty * (sl - w), 2)

    return pnl_tp1only, pnl_tp1tp2



# ── Google Sheets export ──────────────────────────────────────────────────────

SHEETS_HEADER = [
    "Godz (UTC)", "Cena", "Reżim", "c24%", "Score",
    "Typ setupu", "Kierunek", "W", "SL", "TP1", "TP2", "RR",
    "Wynik",
    f"TP1only PnL($100@{LEVERAGE}x)", "TP1only PnL%",
    f"TP1+TP2 PnL($100@{LEVERAGE}x)", "TP1+TP2 PnL%",
]


def export_to_sheets(sheet_name: str, rows: list[list], stats: dict) -> None:
    """Eksportuje wyniki testu do Google Sheets. Wymaga GOOGLE_CREDENTIALS w env."""
    creds_json = os.getenv("GOOGLE_CREDENTIALS", "")
    sheet_id   = os.getenv("SHEET_ID", "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE")
    if not creds_json:
        print("[sheets] Brak GOOGLE_CREDENTIALS — pomijam eksport.")
        return

    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds  = Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        wb     = client.open_by_key(sheet_id)

        try:
            sh = wb.worksheet(sheet_name)
            sh.clear()
        except Exception:
            sh = wb.add_worksheet(sheet_name, rows=len(rows) + 20, cols=len(SHEETS_HEADER) + 2)

        sh.append_row(SHEETS_HEADER)
        if rows:
            sh.append_rows(rows, value_input_option="USER_ENTERED")

        # Wiersz podsumowania
        tot = stats["RAZEM"]
        n   = tot["tp1"] + tot["tp2"] + tot["tp1w"] + tot["sl"] + tot["no"]
        summary = [
            "SUMA", "", "", "", f"n={n}",
            "", "", "", "", "", "", "",
            f"TP1={tot['tp1']} TP2={tot['tp2']} TP1+W={tot['tp1w']} SL={tot['sl']} brak={tot['no']}",
            round(tot["pnl_tp1only"], 2), "",
            round(tot["pnl_tp1tp2"],  2), "",
        ]
        sh.append_row(summary, value_input_option="USER_ENTERED")

        print(f"[sheets] Eksport OK → '{sheet_name}' ({len(rows)} setupów)")
    except Exception as e:
        print(f"[sheets] Błąd eksportu: {e}")


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
    parser.add_argument("--from",  dest="dt_from", default="2026-03-01 00:00")
    parser.add_argument("--to",    dest="dt_to",   default="2026-04-04 00:00")
    parser.add_argument("--dedup", action="store_true",
                        help="Pomijaj setup jeśli aktywny setup z tym samym kierunkiem "
                             f"i W w odległości <${DEDUP_THRESHOLD} już istnieje (jak na produkcji)")
    args = parser.parse_args()

    from_ts = _parse_dt(args.dt_from)
    to_ts   = _parse_dt(args.dt_to)

    fetch_end  = to_ts + (ENTRY_WINDOW_H + TRADE_WINDOW_H) * 3600
    m15_needed = 100 + (fetch_end - from_ts) // 900
    h1_needed  = 200 + (fetch_end - from_ts) // 3600

    dedup_label = " [--dedup]" if args.dedup else ""
    print(f"Pobieranie danych: {args.dt_from} – {args.dt_to}{dedup_label}...")
    m15_all = fetch_klines_okx("SOLUSDT", "15m", m15_needed, fetch_end)
    h1_all  = fetch_klines_okx("SOLUSDT", "1h",  h1_needed,  fetch_end)

    if not m15_all or not h1_all:
        print("Brak danych — sprawdź połączenie z API")
        return

    print(f"M15: {len(m15_all)} świec | H1: {len(h1_all)} świec")
    print()

    # Statystyki per typ setupu (tp1 = "TP1" bez TP2, tp1w = "TP1+W")
    stats = {t: {"tp1": 0, "tp2": 0, "tp1w": 0, "sl": 0, "no": 0,
                 "pnl_tp1only": 0.0, "pnl_tp1tp2": 0.0}
             for t in ALL_TYPES + ["RAZEM"]}

    # Aktywne setupy dla dedup: {direction, w, expires_ts}
    # expires_ts = resolve_ts jeśli znany, inaczej ts_generacji + max_okno
    active_for_dedup: list[dict] = []

    # Wiersze do Sheets
    sheet_rows: list[list] = []

    hdr = f"{'Godz':>11}  {'Cena':>7}  {'c24':>7}  {'Reżim':>16}  {'Typ setupu':>28}  {'Wynik':>8}  {'TP1only':>9}  {'TP1+TP2':>9}"
    print(hdr)
    print("-" * len(hdr))

    for ts in range(from_ts, to_ts, 3600):
        m15_snap = [c for c in m15_all if c["time"] < ts]
        h1_snap  = [c for c in h1_all  if c["time"] < ts]
        future   = [c for c in m15_all if c["time"] >= ts]

        if len(m15_snap) < 20 or len(h1_snap) < 25:
            continue

        # Usuń wygasłe aktywne setupy
        if args.dedup:
            active_for_dedup = [a for a in active_for_dedup if a["expires_ts"] > ts]

        price = m15_snap[-1]["close"]
        dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m %H:%M")

        new_r, new_dir, new_score, slope = detect_regime_new(m15_snap, h1_snap, price)
        new_setups = generate_setups(new_r, new_dir, new_score, m15_snap, h1_snap, price)

        for s in new_setups:
            # Dedup: pomiń jeśli aktywny setup z podobnym W w tym samym kierunku
            if args.dedup:
                is_dup = any(
                    a["direction"] == s["direction"] and abs(a["w"] - s["w"]) < DEDUP_THRESHOLD
                    for a in active_for_dedup
                )
                if is_dup:
                    continue

            res, resolve_ts          = evaluate(s, future)
            pnl_tp1only, pnl_tp1tp2  = calc_pnl_scenarios(s, res)

            # Dodaj do aktywnych (dla kolejnych iteracji dedup)
            if args.dedup:
                max_expire = ts + (ENTRY_WINDOW_H + TRADE_WINDOW_H) * 3600
                active_for_dedup.append({
                    "direction":  s["direction"],
                    "w":          s["w"],
                    "expires_ts": resolve_ts if resolve_ts else max_expire,
                })

            st = stats[s["type"]]
            if res == "TP2":    st["tp2"]  += 1
            elif res == "TP1":  st["tp1"]  += 1
            elif res == "TP1+W": st["tp1w"] += 1
            elif res == "SL":   st["sl"]   += 1
            else:               st["no"]   += 1
            st["pnl_tp1only"] += pnl_tp1only
            st["pnl_tp1tp2"]  += pnl_tp1tp2

            has_result = res not in ("no_entry", "open")
            tp1only_str = f"{pnl_tp1only:+.2f}" if has_result else "—"
            tp1tp2_str  = f"{pnl_tp1tp2:+.2f}"  if has_result else "—"

            short_type = (s["type"]
                          .replace("trend_", "t_").replace("impulse_", "imp_").replace("range_", "r_")
                          .replace("_short", "↓").replace("_long", "↑")
                          .replace("continuation", "cont").replace("pullback", "pb")
                          .replace("resistance", "res").replace("support", "sup"))

            print(f"{dt_str}  {price:>7.2f}  c24={slope:>+5.1f}%  {new_r:>16}  "
                  f"{short_type:>28}  {res:>8}  {tp1only_str:>9}  {tp1tp2_str:>9}")

            sheet_rows.append([
                datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                round(price, 2),
                new_r,
                round(slope, 2),
                new_score,
                s["type"],
                s["direction"],
                s["w"], s["sl"], s["tp1"],
                s.get("tp2", ""),
                s.get("rr", ""),
                res,
                round(pnl_tp1only, 2) if has_result else "",
                round(pnl_tp1only / TRADE_USDT * 100, 1) if has_result else "",
                round(pnl_tp1tp2,  2) if has_result else "",
                round(pnl_tp1tp2  / TRADE_USDT * 100, 1) if has_result else "",
            ])

    # Uzupełnij RAZEM
    for t in ALL_TYPES:
        for k in ("tp1", "tp2", "tp1w", "sl", "no", "pnl_tp1only", "pnl_tp1tp2"):
            stats["RAZEM"][k] += stats[t][k]

    # ── Podsumowanie per typ ──────────────────────────────────────────────────
    print()
    print("=" * 100)
    print(f"{'Typ setupu':<30}  {'TP1':>4} {'TP2':>4} {'T1+W':>4} {'SL':>4} {'brak':>4} {'n':>5}  "
          f"{'TP1only$':>10}  {'TP1+TP2$':>10}")
    print("-" * 100)

    for t in ALL_TYPES:
        st = stats[t]
        n  = st["tp1"] + st["tp2"] + st["tp1w"] + st["sl"] + st["no"]
        print(f"{t:<30}  {st['tp1']:>4} {st['tp2']:>4} {st['tp1w']:>4} {st['sl']:>4} {st['no']:>4} {n:>5}  "
              f"{st['pnl_tp1only']:>+10.2f}  {st['pnl_tp1tp2']:>+10.2f}")

    print("-" * 100)
    tot = stats["RAZEM"]
    n   = tot["tp1"] + tot["tp2"] + tot["tp1w"] + tot["sl"] + tot["no"]
    print(f"{'RAZEM':<30}  {tot['tp1']:>4} {tot['tp2']:>4} {tot['tp1w']:>4} {tot['sl']:>4} {tot['no']:>4} {n:>5}  "
          f"{tot['pnl_tp1only']:>+10.2f}  {tot['pnl_tp1tp2']:>+10.2f}")
    print()
    print(f"TP1only: {tot['pnl_tp1only']:+.2f}$ ({tot['pnl_tp1only'] / TRADE_USDT * 100:+.1f}% na $100)")
    print(f"TP1+TP2: {tot['pnl_tp1tp2']:+.2f}$ ({tot['pnl_tp1tp2'] / TRADE_USDT * 100:+.1f}% na $100)")

    # ── Eksport do Sheets ─────────────────────────────────────────────────────
    date_from  = datetime.fromtimestamp(from_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    date_to    = datetime.fromtimestamp(to_ts,   tz=timezone.utc).strftime("%Y-%m-%d")
    dedup_sfx  = "_dedup" if args.dedup else ""
    sheet_name = f"TestRegime_{date_from}_{date_to}{dedup_sfx}"
    export_to_sheets(sheet_name, sheet_rows, stats)


if __name__ == "__main__":
    main()
