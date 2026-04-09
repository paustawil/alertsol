#!/usr/bin/env python3
"""
Impulse Backtest — porownanie 3 wersji logiki impulse trade dla SOL/USDT.

Uruchomienie:
    python impulse_backtest.py --days 90
    python impulse_backtest.py --from "2025-08-01 00:00" --to "2025-09-30 23:59"
"""

import argparse
import bisect
import io
import sys
from datetime import datetime, timezone
from statistics import mean

# Windows cp1250 fix: force UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests


# -- Helpers (kopie z gpt3_validator_backtest.py) ------------------------------

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


def detect_range(candles: list[dict], n: int = 32) -> dict:
    recent = candles[-n:]
    resistance = max(c["high"] for c in recent)
    support = min(c["low"] for c in recent)
    rng_size = resistance - support
    zone = rng_size * 0.06
    return {
        "resistance": round(resistance, 2), "support": round(support, 2),
        "range_size": round(rng_size, 2),
        "r_touches": sum(1 for c in recent if c["high"] >= resistance - zone),
        "s_touches": sum(1 for c in recent if c["low"] <= support + zone),
    }


def detect_regime_new(candles_m15, candles_h1, current_price):
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_4h_ago = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    change_4h = (current_price - price_4h_ago) / price_4h_ago * 100

    price_24h = candles_h1[-24]["close"] if len(candles_h1) >= 24 else candles_h1[0]["close"]
    price_48h = candles_h1[-48]["close"] if len(candles_h1) >= 48 else candles_h1[0]["close"]
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    last4 = candles_m15[-4:]
    bearish_closes = sum(1 for c in last4 if c["close"] < c["open"])
    bullish_closes = sum(1 for c in last4 if c["close"] > c["open"])

    h1_12 = candles_h1[-12:] if len(candles_h1) >= 12 else candles_h1
    h1_lows = [c["low"] for c in h1_12]
    h1_highs = [c["high"] for c in h1_12]
    lower_lows  = sum(1 for i in range(1, len(h1_lows))  if h1_lows[i]  < h1_lows[i-1])
    higher_highs = sum(1 for i in range(1, len(h1_highs)) if h1_highs[i] > h1_highs[i-1])
    lower_highs  = sum(1 for i in range(1, len(h1_highs)) if h1_highs[i] < h1_highs[i-1])
    higher_lows  = sum(1 for i in range(1, len(h1_lows))  if h1_lows[i]  > h1_lows[i-1])

    impulse_score = 0
    impulse_dir = "none"
    if imp_str >= 2: impulse_score += 1
    if vol_ratio >= 1.5: impulse_score += 1
    if abs(change_4h) >= 2.0: impulse_score += 1
    elif abs(change_4h) >= 1.5 and vol_ratio >= 1.3: impulse_score += 1
    if bearish_closes >= 3:
        impulse_score += 1; impulse_dir = "down"
    elif bullish_closes >= 3:
        impulse_score += 1; impulse_dir = "up"

    if impulse_score >= 3:
        if impulse_dir == "none":
            impulse_dir = "down" if change_4h < 0 else "up"
        strength = min(10, impulse_score * 2 + imp_str)
        return {
            "regime": f"IMPULSE_{impulse_dir.upper()}", "direction": impulse_dir,
            "strength": strength, "change_4h": round(change_4h, 1),
            "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
            "impulse": imp_str, "h1_trend": trend, "vol_ratio": round(vol_ratio, 1),
        }

    trend_score = 0
    if abs(change_24h) >= 3.0: trend_score += 2
    elif abs(change_24h) >= 1.5: trend_score += 1
    if abs(change_48h) >= 5.0: trend_score += 2
    elif abs(change_48h) >= 3.0: trend_score += 1
    if lower_lows >= 5: trend_score += 1
    if higher_highs >= 5: trend_score += 1
    if lower_highs >= 5: trend_score += 1
    if higher_lows >= 5: trend_score += 1
    if trend != "neutral": trend_score += 1

    has_price_change = abs(change_24h) >= 1.5 or abs(change_48h) >= 3.0
    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"
        return {
            "regime": f"TREND_{trend_dir.upper()}", "direction": trend_dir,
            "strength": min(10, trend_score + imp_str),
            "change_4h": round(change_4h, 1), "change_24h": round(change_24h, 1),
            "change_48h": round(change_48h, 1),
            "impulse": imp_str, "h1_trend": trend, "vol_ratio": round(vol_ratio, 1),
        }

    return {
        "regime": "RANGE", "direction": "none", "strength": 0,
        "change_4h": round(change_4h, 1), "change_24h": round(change_24h, 1),
        "change_48h": round(change_48h, 1),
        "impulse": imp_str, "h1_trend": trend, "vol_ratio": round(vol_ratio, 1),
    }


def compute_spike_filter(candles_m15: list[dict], impulse_dir: str) -> tuple[int, bool]:
    """
    Sprawdza czy sygnał IMPULSE wygląda jak spike-then-reversal.
    Zwraca (spike_score, is_filtered).
    is_filtered=True gdy spike_score >= 2 i impulse_min_score zostałby podniesiony do 4.

    Sygnały:
      1. change_1h silnie przeciwny do kierunku impulsu
      2. change_2h też pod prąd
      3. Rejection wicks na ostatnich 3 świecach M15
    """
    if len(candles_m15) < 8:
        return 0, False

    current_price = candles_m15[-1]["close"]
    price_1h = candles_m15[-4]["close"] if len(candles_m15) >= 4 else candles_m15[0]["close"]
    price_2h = candles_m15[-8]["close"] if len(candles_m15) >= 8 else candles_m15[0]["close"]
    change_1h = (current_price - price_1h) / price_1h * 100
    change_2h  = (current_price - price_2h)  / price_2h  * 100

    spike_score = 0

    # Sygnał 1: 1h silnie przeciwny
    if impulse_dir == "up"   and change_1h < -0.8:
        spike_score += 1
    elif impulse_dir == "down" and change_1h >  0.8:
        spike_score += 1

    # Sygnał 2: 2h też pod prąd
    if impulse_dir == "up"   and change_2h < -0.6:
        spike_score += 1
    elif impulse_dir == "down" and change_2h >  0.6:
        spike_score += 1

    # Sygnał 3: rejection wicks na ostatnich 3 M15
    recent3 = candles_m15[-3:]
    bodies = [abs(c["close"] - c["open"]) + 0.001 for c in recent3]
    if impulse_dir == "up":
        wicks = [c["high"] - max(c["open"], c["close"]) for c in recent3]
    else:
        wicks = [min(c["open"], c["close"]) - c["low"] for c in recent3]
    if sum(w / b for w, b in zip(wicks, bodies)) / 3 > 1.5:
        spike_score += 1

    return spike_score, spike_score >= 2


def find_swing_points(candles_h1, n=12):
    recent = candles_h1[-n:]
    return max(c["high"] for c in recent), min(c["low"] for c in recent)


def evaluate_setup(setup, future_m15, entry_window_h=24, tp1_only=False, no_be=False):
    """
    tp1_only=False, no_be=False  (domyślnie):
        pozycja podzielona 50/50 — połowa zamykana na TP1, połowa na TP2
        lub BE (SL przesunięty na entry po TP1).
    tp1_only=True:
        CAŁA pozycja zamykana na TP1. SL=1× strata, brak premii TP2.
    no_be=True (split bez BE):
        pozycja podzielona 50/50 — połowa na TP1, połowa czeka na TP2
        lub ORYGINALNY SL (brak przesunięcia na BE). Prostsze w ustawieniu.
    """
    w = setup["w"]; sl = setup["sl"]; tp1 = setup["tp1"]; tp2 = setup["tp2"]
    direction = setup["direction"]
    entry_window_s = entry_window_h * 3600

    entry_ts = None
    for c in future_m15:
        if entry_ts is None and c["time"] <= future_m15[0]["time"] + entry_window_s:
            if direction == "short" and c["high"] >= w:
                entry_ts = c["time"]
            elif direction == "long" and c["low"] <= w:
                entry_ts = c["time"]

    if entry_ts is None:
        return {"wynik": "no_entry", "pnl_tp1": 0, "pnl_tp2": 0}

    tp1_hit = tp2_hit = sl_hit = False
    for c in future_m15:
        if c["time"] < entry_ts:
            continue
        if direction == "short":
            if c["low"] <= tp1: tp1_hit = True
            if c["low"] <= tp2: tp2_hit = True
            if c["high"] >= sl: sl_hit = True
        else:
            if c["high"] >= tp1: tp1_hit = True
            if c["high"] >= tp2: tp2_hit = True
            if c["low"] <= sl: sl_hit = True

        if tp1_only:
            # Cała pozycja na TP1 — SL strata 1×, TP1 zysk 1×
            if tp1_hit and not sl_hit:
                return {"wynik": "TP1", "pnl_tp1": abs(w - tp1), "pnl_tp2": 0}
            if sl_hit and not tp1_hit:
                return {"wynik": "SL", "pnl_tp1": -abs(sl - w), "pnl_tp2": 0}
            if sl_hit and tp1_hit:
                return {"wynik": "TP1", "pnl_tp1": abs(w - tp1), "pnl_tp2": 0}
        else:
            if tp1_hit and not sl_hit:
                if tp2_hit:
                    pnl = abs(w - tp2) if direction == "short" else abs(tp2 - w)
                    return {"wynik": "TP1+TP2", "pnl_tp1": abs(w - tp1), "pnl_tp2": pnl}
            if sl_hit and not tp1_hit:
                return {"wynik": "SL", "pnl_tp1": -abs(sl - w), "pnl_tp2": -abs(sl - w)}
            if sl_hit and tp1_hit:
                if no_be:
                    # Bez przesunięcia SL: połowa na TP1 (zysk), połowa na oryginalnym SL (strata)
                    return {"wynik": "TP1+SL", "pnl_tp1": abs(w - tp1), "pnl_tp2": -abs(sl - w)}
                return {"wynik": "TP1+BE", "pnl_tp1": abs(w - tp1), "pnl_tp2": 0}

    return {"wynik": "open", "pnl_tp1": 0, "pnl_tp2": 0}


# -- Data fetching (kopie z gpt3_validator_backtest.py) -------------------------

def fetch_klines_binance(symbol, interval, total, end_ts_s=None):
    okx_bar = {"15m": "15m", "1h": "1H"}[interval]
    interval_s = {"15m": 900, "1h": 3600}[interval]
    inst_id = "SOL-USDT-SWAP"
    result = []
    after_ms = str(int(end_ts_s * 1000)) if end_ts_s else ""
    while len(result) < total:
        params = {"instId": inst_id, "bar": okx_bar, "limit": "100"}
        if after_ms: params["after"] = after_ms
        try:
            r = requests.get("https://www.okx.com/api/v5/market/history-candles", params=params, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            print(f"[okx] Blad API: {e}"); break
        if not data: break
        batch = [{"time": int(d[0])//1000, "open": float(d[1]), "high": float(d[2]),
                  "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])} for d in data]
        batch.sort(key=lambda c: c["time"])
        result = batch + result
        after_ms = str(int(batch[0]["time"] * 1000))
        if len(batch) < 2: break
    seen = set()
    deduped = [c for c in result if c["time"] not in seen and not seen.add(c["time"])]
    deduped.sort(key=lambda c: c["time"])
    return deduped[-total:] if len(deduped) > total else deduped


def fetch_klines_bitget(symbol, interval, total, end_ts_s=None):
    granularity = {"15m": "15m", "1h": "1H"}[interval]
    interval_s = {"15m": 900, "1h": 3600}[interval]
    result = []
    end_ms = (end_ts_s * 1000) if end_ts_s else None
    while len(result) < total:
        params = {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "granularity": granularity, "limit": str(min(total - len(result), 200)),
        }
        if end_ms: params["endTime"] = str(end_ms)
        try:
            r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            print(f"[bitget] Blad API: {e}"); break
        if not data: break
        batch = [{"time": int(d[0])//1000, "open": float(d[1]), "high": float(d[2]),
                  "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])} for d in data]
        batch.sort(key=lambda c: c["time"])
        result = batch + result
        end_ms = batch[0]["time"] * 1000 - interval_s * 1000
        if len(batch) < 2: break
    seen = set()
    deduped = [c for c in result if c["time"] not in seen and not seen.add(c["time"])]
    deduped.sort(key=lambda c: c["time"])
    return deduped[-total:] if len(deduped) > total else deduped


def fetch_klines_paginated(symbol, interval, total, end_ts_s=None):
    print(f"  [zrodlo: OKX]")
    return fetch_klines_binance(symbol, interval, total, end_ts_s)


def _ts_fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _parse_dt(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def _parse_date_only(s):
    """Akceptuje format YYYY-MM-DD lub YYYY-MM-DD HH:MM."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Nieprawidlowy format daty: {s!r}. Uzyj YYYY-MM-DD lub YYYY-MM-DD HH:MM")


# -- Setup generators ----------------------------------------------------------

def setup_version_a(regime, ctx_m15, ctx_h1, price):
    """
    Wersja A: zielone/czerwone swiece jako potwierdzenie odreagowania.
    """
    regime_name = regime["regime"]
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None

    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low = min(swing_low, price)
    swing_high = max(swing_high, price)

    last6 = ctx_m15[-6:]

    if regime_name == "IMPULSE_DOWN":
        green_count = sum(1 for c in last6 if c["close"] > c["open"])
        if not (1 <= green_count <= 2):
            return None
        last2 = ctx_m15[-2:]
        w = max(c["high"] for c in last2)
        sl = w + 0.8 * atr
        tp1 = swing_low
        tp2 = swing_low - atr
        if tp1 >= w or sl <= w:
            return None
        risk = sl - w
        reward_tp1 = w - tp1
        if risk <= 0:
            return None
        rr = reward_tp1 / risk
        if rr < 1.5:
            return None
        if abs(w - price) > price * 0.03:
            return None
        return {
            "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "short", "type": "A_short",
            "rr": round(rr, 2),
        }

    elif regime_name == "IMPULSE_UP":
        red_count = sum(1 for c in last6 if c["close"] < c["open"])
        if not (1 <= red_count <= 2):
            return None
        last2 = ctx_m15[-2:]
        w = min(c["low"] for c in last2)
        sl = w - 0.8 * atr
        tp1 = swing_high
        tp2 = swing_high + atr
        if tp1 <= w or sl >= w:
            return None
        risk = w - sl
        reward_tp1 = tp1 - w
        if risk <= 0:
            return None
        rr = reward_tp1 / risk
        if rr < 1.5:
            return None
        if abs(w - price) > price * 0.03:
            return None
        return {
            "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "long", "type": "A_long",
            "rr": round(rr, 2),
        }

    return None


def setup_version_b(regime, ctx_m15, ctx_h1, price):
    """
    Wersja B: Fibonacci limit order, czeka az pullback sie rozpocznie.
    """
    regime_name = regime["regime"]
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None

    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low = min(swing_low, price)
    swing_high = max(swing_high, price)

    imp_range = swing_high - swing_low
    if imp_range < atr * 2.0:
        return None

    strength = regime.get("strength", 0)
    last8 = ctx_m15[-8:] if len(ctx_m15) >= 8 else ctx_m15

    if regime_name == "IMPULSE_DOWN":
        fib382 = swing_low + imp_range * 0.382
        fib500 = swing_low + imp_range * 0.500
        fib618 = swing_low + imp_range * 0.618

        pullback_pct = (price - swing_low) / imp_range
        if not (0.03 <= pullback_pct <= 0.72):
            return None

        # Pullback quality filter
        if len(last8) >= 2:
            net_rise = (last8[-1]["close"] - last8[0]["open"]) / last8[0]["open"] * 100
            bodies = [abs(c["close"] - c["open"]) for c in last8]
            ranges = [c["high"] - c["low"] for c in last8]
            avg_body = mean(bodies) if bodies else 0
            avg_range = mean(ranges) if ranges else 1
            body_ratio = avg_body / avg_range if avg_range > 0 else 1.0
            if not (net_rise < 1.5 and body_ratio < 0.55):
                return None

        # Choose fib level based on strength
        candidates = []
        if strength >= 7:
            candidates = [
                (fib382, fib382 + atr * 0.4, "fib38"),
                (fib500, fib500 + atr * 0.5, "fib50"),
                (fib618, fib618 + atr * 0.8, "fib62"),
            ]
        elif strength >= 5:
            candidates = [
                (fib500, fib500 + atr * 0.5, "fib50"),
                (fib618, fib618 + atr * 0.8, "fib62"),
            ]
        else:
            candidates = [
                (fib618, fib618 + atr * 0.8, "fib62"),
            ]

        chosen_w = chosen_sl = chosen_fib_pct = None
        for cand_w, cand_sl, fib_pct in candidates:
            # Entry must be ABOVE current price
            if cand_w > price * 1.002 and abs(cand_w - price) <= price * 0.03:
                chosen_w = cand_w
                chosen_sl = cand_sl
                chosen_fib_pct = fib_pct
                break

        if chosen_w is None:
            return None

        tp1 = swing_low
        tp2 = swing_low - imp_range * 0.20
        risk = chosen_sl - chosen_w
        if risk <= 0 or chosen_w <= tp1:
            return None
        reward_tp1 = chosen_w - tp1
        rr = reward_tp1 / risk
        if rr < 1.5:
            return None

        return {
            "w": chosen_w, "sl": chosen_sl, "tp1": tp1, "tp2": tp2,
            "direction": "short", "type": f"B_short_{chosen_fib_pct}",
            "rr": round(rr, 2),
        }

    elif regime_name == "IMPULSE_UP":
        fib382 = swing_high - imp_range * 0.382
        fib500 = swing_high - imp_range * 0.500
        fib618 = swing_high - imp_range * 0.618

        pullback_pct = (swing_high - price) / imp_range
        if not (0.03 <= pullback_pct <= 0.72):
            return None

        # Pullback quality filter
        if len(last8) >= 2:
            net_drop = (last8[0]["open"] - last8[-1]["close"]) / last8[0]["open"] * 100
            bodies = [abs(c["close"] - c["open"]) for c in last8]
            ranges = [c["high"] - c["low"] for c in last8]
            avg_body = mean(bodies) if bodies else 0
            avg_range = mean(ranges) if ranges else 1
            body_ratio = avg_body / avg_range if avg_range > 0 else 1.0
            if not (net_drop < 1.5 and body_ratio < 0.55):
                return None

        # Choose fib level based on strength
        candidates = []
        if strength >= 7:
            candidates = [
                (fib382, fib382 - atr * 0.4, "fib38"),
                (fib500, fib500 - atr * 0.5, "fib50"),
                (fib618, fib618 - atr * 0.8, "fib62"),
            ]
        elif strength >= 5:
            candidates = [
                (fib500, fib500 - atr * 0.5, "fib50"),
                (fib618, fib618 - atr * 0.8, "fib62"),
            ]
        else:
            candidates = [
                (fib618, fib618 - atr * 0.8, "fib62"),
            ]

        chosen_w = chosen_sl = chosen_fib_pct = None
        for cand_w, cand_sl, fib_pct in candidates:
            # Entry must be BELOW current price
            if cand_w < price * 0.998 and abs(cand_w - price) <= price * 0.03:
                chosen_w = cand_w
                chosen_sl = cand_sl
                chosen_fib_pct = fib_pct
                break

        if chosen_w is None:
            return None

        tp1 = swing_high
        tp2 = swing_high + imp_range * 0.20
        risk = chosen_w - chosen_sl
        if risk <= 0 or chosen_w >= tp1:
            return None
        reward_tp1 = tp1 - chosen_w
        rr = reward_tp1 / risk
        if rr < 1.5:
            return None

        return {
            "w": chosen_w, "sl": chosen_sl, "tp1": tp1, "tp2": tp2,
            "direction": "long", "type": f"B_long_{chosen_fib_pct}",
            "rr": round(rr, 2),
        }

    return None


def setup_version_c(regime, ctx_m15, ctx_h1, price):
    """
    Wersja C: Fibonacci limit, antycypuje — BEZ wymogu pullbacku, triggerowany natychmiast.
    """
    regime_name = regime["regime"]
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None

    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low = min(swing_low, price)
    swing_high = max(swing_high, price)

    imp_range = swing_high - swing_low
    if imp_range < atr * 2.0:
        return None

    strength = regime.get("strength", 0)

    if regime_name == "IMPULSE_DOWN":
        fib382 = swing_low + imp_range * 0.382
        fib500 = swing_low + imp_range * 0.500
        fib618 = swing_low + imp_range * 0.618

        # Allow pullback_pct up to 0.30 (early stage)
        pullback_pct = (price - swing_low) / imp_range if imp_range > 0 else 0
        if pullback_pct > 0.30:
            return None

        # Choose fib level based on strength
        candidates = []
        if strength >= 7:
            candidates = [
                (fib382, fib382 + atr * 0.4, "fib38"),
                (fib500, fib500 + atr * 0.5, "fib50"),
                (fib618, fib618 + atr * 0.8, "fib62"),
            ]
        elif strength >= 5:
            candidates = [
                (fib500, fib500 + atr * 0.5, "fib50"),
                (fib618, fib618 + atr * 0.8, "fib62"),
            ]
        else:
            candidates = [
                (fib618, fib618 + atr * 0.8, "fib62"),
            ]

        chosen_w = chosen_sl = chosen_fib_pct = None
        for cand_w, cand_sl, fib_pct in candidates:
            # Entry must be ABOVE current price
            if cand_w > price and abs(cand_w - price) <= price * 0.03:
                chosen_w = cand_w
                chosen_sl = cand_sl
                chosen_fib_pct = fib_pct
                break

        if chosen_w is None:
            return None

        tp1 = swing_low
        tp2 = swing_low - imp_range * 0.20
        risk = chosen_sl - chosen_w
        if risk <= 0 or chosen_w <= tp1:
            return None
        reward_tp1 = chosen_w - tp1
        rr = reward_tp1 / risk
        if rr < 1.5:
            return None

        return {
            "w": chosen_w, "sl": chosen_sl, "tp1": tp1, "tp2": tp2,
            "direction": "short", "type": f"C_short_{chosen_fib_pct}",
            "rr": round(rr, 2),
        }

    elif regime_name == "IMPULSE_UP":
        fib382 = swing_high - imp_range * 0.382
        fib500 = swing_high - imp_range * 0.500
        fib618 = swing_high - imp_range * 0.618

        # Allow pullback_pct up to 0.30 (early stage)
        pullback_pct = (swing_high - price) / imp_range if imp_range > 0 else 0
        if pullback_pct > 0.30:
            return None

        # Choose fib level based on strength
        candidates = []
        if strength >= 7:
            candidates = [
                (fib382, fib382 - atr * 0.4, "fib38"),
                (fib500, fib500 - atr * 0.5, "fib50"),
                (fib618, fib618 - atr * 0.8, "fib62"),
            ]
        elif strength >= 5:
            candidates = [
                (fib500, fib500 - atr * 0.5, "fib50"),
                (fib618, fib618 - atr * 0.8, "fib62"),
            ]
        else:
            candidates = [
                (fib618, fib618 - atr * 0.8, "fib62"),
            ]

        chosen_w = chosen_sl = chosen_fib_pct = None
        for cand_w, cand_sl, fib_pct in candidates:
            # Entry must be BELOW current price
            if cand_w < price and abs(cand_w - price) <= price * 0.03:
                chosen_w = cand_w
                chosen_sl = cand_sl
                chosen_fib_pct = fib_pct
                break

        if chosen_w is None:
            return None

        tp1 = swing_high
        tp2 = swing_high + imp_range * 0.20
        risk = chosen_w - chosen_sl
        if risk <= 0 or chosen_w >= tp1:
            return None
        reward_tp1 = tp1 - chosen_w
        rr = reward_tp1 / risk
        if rr < 1.5:
            return None

        return {
            "w": chosen_w, "sl": chosen_sl, "tp1": tp1, "tp2": tp2,
            "direction": "long", "type": f"C_long_{chosen_fib_pct}",
            "rr": round(rr, 2),
        }

    return None


def setup_version_d(regime, ctx_m15, ctx_h1, price, vol_threshold=2.0,
                    swing_n=12, atr_fallback=False, label=None):
    """
    Wersja D/E/G/H/I: AGRESYWNE wejście po aktualnej cenie — bez czekania na pullback.

    vol_threshold : minimalny vol_ratio (D/G/H/I=2.0, E=1.7)
    swing_n       : lookback świec H1 dla swing points (D/E=12, G/I=24)
    atr_fallback  : gdy swing TP zbyt blisko ceny -> użyj ATR×2.0 jako TP1 (H/I=True)
    """
    regime_name = regime["regime"]
    vol_ratio = regime.get("vol_ratio", 1.0)

    if vol_ratio < vol_threshold:
        return None

    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None

    swing_high, swing_low = find_swing_points(ctx_h1, n=swing_n)
    swing_low  = min(swing_low,  price)
    swing_high = max(swing_high, price)

    if label is None:
        label = "D" if vol_threshold >= 2.0 else "E"

    if regime_name == "IMPULSE_DOWN":
        w  = price
        sl = price + atr * 1.2
        # ATR fallback: gdy swing_low zbyt blisko ceny (impuls w toku), użyj ATR
        if atr_fallback and (w - swing_low) < atr * 1.5:
            tp1 = price - atr * 2.0
            tp2 = price - atr * 3.5
        else:
            tp1 = swing_low
            tp2 = swing_low - atr
        if tp1 >= w or sl <= w:
            return None
        risk = sl - w
        reward = w - tp1
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < 1.2:
            return None
        return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                "direction": "short", "type": f"{label}_short", "rr": round(rr, 2)}

    elif regime_name == "IMPULSE_UP":
        w  = price
        sl = price - atr * 1.2
        # ATR fallback: gdy swing_high zbyt blisko ceny
        if atr_fallback and (swing_high - w) < atr * 1.5:
            tp1 = price + atr * 2.0
            tp2 = price + atr * 3.5
        else:
            tp1 = swing_high
            tp2 = swing_high + atr
        if tp1 <= w or sl >= w:
            return None
        risk = w - sl
        reward = tp1 - w
        if risk <= 0:
            return None
        rr = reward / risk
        if rr < 1.2:
            return None
        return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                "direction": "long", "type": f"{label}_long", "rr": round(rr, 2)}

    return None


def setup_version_f(regime, ctx_m15, ctx_h1, price):
    """
    Wersja F: VOLUME-SWITCHING — strategia mieszana.

    Decyzja na podstawie wolumenu:
      vol >= 2.0x -> market entry natychmiast (jak Wersja D)
      vol 1.5-2.0x -> czekaj na pullback 1-2 świece (jak Wersja A)
      vol < 1.5x   -> brak setupu (IMPULSE nie potwierdzony wolumenem)

    Filozofia: silny wolumen = impuls ma momentum, nie czekaj.
    Słabszy wolumen = niepewność, poczekaj na lepszą cenę pullbacku.
    """
    regime_name = regime["regime"]
    vol_ratio = regime.get("vol_ratio", 1.0)

    if vol_ratio < 1.5:
        return None

    if vol_ratio >= 2.0:
        # Market entry (jak D)
        setup = setup_version_d(regime, ctx_m15, ctx_h1, price, vol_threshold=2.0)
        if setup:
            setup["type"] = setup["type"].replace("D_", "F_mkt_")
        return setup
    else:
        # Pullback entry (jak A), ale z wymogiem vol >= 1.5x
        setup = setup_version_a(regime, ctx_m15, ctx_h1, price)
        if setup:
            setup["type"] = setup["type"].replace("A_", "F_pb_")
        return setup


# -- Setup generators — TREND & RANGE (live algo simulation) ------------------

def setup_trend_pullback_short(regime, ctx_m15, ctx_h1, price):
    """trend_pullback_short: fib 38-50% korekty — short przy pullbacku w dół."""
    if regime.get("direction", "none") != "down":
        return None
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None
    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low = min(swing_low, price)
    swing_high = max(swing_high, price)
    if swing_high <= swing_low:
        return None
    swing_range = swing_high - swing_low
    fib38 = swing_low + swing_range * 0.38
    fib50 = swing_low + swing_range * 0.50
    fib618 = swing_low + swing_range * 0.618
    w  = round((fib38 + fib50) / 2, 2)
    sl = round(fib618 + atr * 0.3, 2)
    tp1 = round(swing_low, 2)
    tp2 = round(swing_low - swing_range * 0.3, 2)
    if not (sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5):
        return None
    if not (w > price * 1.003 and w - price <= price * 0.03):
        return None
    return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "short", "type": f"TPBS_{regime['regime']}",
            "rr": round((w - tp1) / (sl - w), 2)}


def setup_trend_pullback_long(regime, ctx_m15, ctx_h1, price):
    """trend_pullback_long: fib 38-50% korekty — long przy pullbacku w górę. Wymaga strength >= 5."""
    if regime.get("direction", "none") != "up":
        return None
    if regime.get("strength", 0) < 5:
        return None
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None
    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low = min(swing_low, price)
    swing_high = max(swing_high, price)
    if swing_high <= swing_low:
        return None
    swing_range = swing_high - swing_low
    fib38 = swing_high - swing_range * 0.38
    fib50 = swing_high - swing_range * 0.50
    fib618 = swing_high - swing_range * 0.618
    w  = round((fib38 + fib50) / 2, 2)
    sl = round(fib618 - atr * 0.3, 2)
    tp1 = round(swing_high, 2)
    tp2 = round(swing_high + swing_range * 0.3, 2)
    if not (sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5):
        return None
    if not (w < price * 0.997 and price - w <= price * 0.03):
        return None
    return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "long", "type": f"TPBL_{regime['regime']}",
            "rr": round((tp1 - w) / (w - sl), 2)}


def setup_impulse_cont_short(regime, ctx_m15, ctx_h1, price):
    """impulse_continuation_short: mini-pullback w impulsie (1-2 zielone z 6 ostatnich M15)."""
    if not regime["regime"].startswith("IMPULSE_DOWN"):
        return None
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None
    last6 = ctx_m15[-6:]
    greens = [c for c in last6 if c["close"] > c["open"]]
    if not (1 <= len(greens) <= 2):
        return None
    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low = min(swing_low, price)
    pullback_high = max(c["high"] for c in last6[-2:])
    w  = round(pullback_high, 2)
    sl = round(pullback_high + atr * 0.8, 2)
    tp1 = round(swing_low, 2)
    tp2 = round(swing_low - atr, 2)
    if sl <= w or tp1 >= w:
        return None
    if abs(w - price) > price * 0.03:
        return None
    risk = sl - w
    reward = w - tp1
    if risk <= 0 or reward / risk < 1.5:
        return None
    return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "short", "type": "IMP_CONT_short",
            "rr": round(reward / risk, 2)}


def setup_range_short(regime, ctx_m15, ctx_h1, price):
    """range_resistance_short: short przy górnej granicy range (z 3 filtrami z live algo)."""
    if regime["regime"] != "RANGE":
        return None
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None
    rng = detect_range(ctx_h1)
    sup, res = rng["support"], rng["resistance"]
    rng_size = res - sup
    if rng_size <= atr * 1.5:
        return None
    w  = round(res - rng_size * 0.1, 2)
    sl = round(res + atr * 1.0, 2)
    tp1 = round(sup + rng_size * 0.5, 2)
    tp2 = round(sup + rng_size * 0.1, 2)
    if abs(w - price) > price * 0.03:
        return None
    if (sl - w) <= 0 or (w - tp1) / (sl - w) < 1.5:
        return None
    # Filtr 1: momentum
    last6 = ctx_m15[-6:]
    bullish_count = sum(1 for c in last6 if c["close"] > c["open"])
    m15_rise = (last6[-1]["close"] - last6[0]["open"]) / last6[0]["open"] * 100
    if bullish_count >= 5 or m15_rise > 1.5:
        return None
    # Filtr 2: touches
    if rng["r_touches"] < 2:
        return None
    # Filtr 3: MA alignment
    closes = [c["close"] for c in ctx_m15]
    if len(closes) >= 30:
        ma30 = sum(closes[-30:]) / 30
        ma60 = sum(closes[-60:]) / min(60, len(closes))
        if price > ma30 > ma60:
            return None
    return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "short", "type": "RANGE_short",
            "rr": round((w - tp1) / (sl - w), 2)}


def setup_range_long(regime, ctx_m15, ctx_h1, price):
    """range_support_long: long przy dolnej granicy range (z 3 filtrami z live algo)."""
    if regime["regime"] != "RANGE":
        return None
    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None
    rng = detect_range(ctx_h1)
    sup, res = rng["support"], rng["resistance"]
    rng_size = res - sup
    if rng_size <= atr * 1.5:
        return None
    w  = round(sup + rng_size * 0.1, 2)
    sl = round(sup - atr * 1.0, 2)
    tp1 = round(sup + rng_size * 0.5, 2)
    tp2 = round(res - rng_size * 0.1, 2)
    if abs(w - price) > price * 0.03:
        return None
    if (w - sl) <= 0 or (tp1 - w) / (w - sl) < 1.5:
        return None
    # Filtr 1: momentum
    last6 = ctx_m15[-6:]
    bearish_count = sum(1 for c in last6 if c["close"] < c["open"])
    m15_drop = (last6[-1]["close"] - last6[0]["open"]) / last6[0]["open"] * 100
    if bearish_count >= 5 or m15_drop < -1.5:
        return None
    # Filtr 2: touches
    if rng["s_touches"] < 2:
        return None
    # Filtr 3: MA alignment
    closes = [c["close"] for c in ctx_m15]
    if len(closes) >= 30:
        ma30 = sum(closes[-30:]) / 30
        ma60 = sum(closes[-60:]) / min(60, len(closes))
        if price < ma30 < ma60:
            return None
    return {"w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
            "direction": "long", "type": "RANGE_long",
            "rr": round((tp1 - w) / (w - sl), 2)}


# -- Statistics helpers --------------------------------------------------------

def make_stats():
    return {
        "IMPULSE_DOWN": {
            "total": 0, "filled": 0,
            "tp1_wins": 0, "tp2_wins": 0, "sl_losses": 0, "open": 0,
            "pnl_tp1_sum": 0.0, "pnl_tp2_sum": 0.0,
            # Spike-filter tracking
            "spike_filtered": 0,        # ile odrzucone przez filtr
            "spike_tp1_wins": 0,        # ile z odrzuconych trafiło TP1
            "spike_tp2_wins": 0,        # ile z odrzuconych trafiło TP2
            "spike_sl_losses": 0,       # ile z odrzuconych uderzyło SL
            "spike_pnl_sum": 0.0,       # zsumowany pnl odrzuconych
        },
        "IMPULSE_UP": {
            "total": 0, "filled": 0,
            "tp1_wins": 0, "tp2_wins": 0, "sl_losses": 0, "open": 0,
            "pnl_tp1_sum": 0.0, "pnl_tp2_sum": 0.0,
            "spike_filtered": 0,
            "spike_tp1_wins": 0,
            "spike_tp2_wins": 0,
            "spike_sl_losses": 0,
            "spike_pnl_sum": 0.0,
        },
    }


def record_outcome(stats, direction_key, result, entry_price, spike_filtered=False):
    st = stats[direction_key]
    wynik = result["wynik"]
    scale = 100.0 / entry_price if entry_price > 0 else 1.0

    if spike_filtered:
        # Śledzimy co by się stało z odrzuconymi setupami
        st["spike_filtered"] += 1
        if wynik == "no_entry":
            return
        pnl = (result["pnl_tp1"] + result["pnl_tp2"]) * scale
        st["spike_pnl_sum"] += pnl
        if wynik in ("TP1+TP2", "TP1"):
            st["spike_tp1_wins"] += 1
            if wynik == "TP1+TP2":
                st["spike_tp2_wins"] += 1
        elif wynik in ("TP1+BE", "TP1+SL"):
            st["spike_tp1_wins"] += 1
        elif wynik == "SL":
            st["spike_sl_losses"] += 1
        return

    st["total"] += 1
    if wynik == "no_entry":
        return
    st["filled"] += 1
    # Normalize pnl to $100 per trade (pnl expressed as fraction of entry)
    if wynik in ("TP1+TP2", "TP1"):
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
        if wynik == "TP1+TP2":
            st["tp2_wins"] += 1
    elif wynik == "TP1+BE":
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
    elif wynik == "TP1+SL":
        # Połowa na TP1 (zysk), połowa na oryginalnym SL (strata) — bez przesunięcia BE
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale  # ujemne
    elif wynik == "SL":
        st["sl_losses"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
    elif wynik == "open":
        st["open"] += 1


def run_h1_scan(from_ts, to_ts, all_m15, all_h1, m15_times, h1_times, stats_algo=None):
    """Uruchamia H1 scan dla zadanego zakresu. Zwraca (stats_algo, h1_regime_counts)."""
    if stats_algo is None:
        stats_algo = make_algo_stats()
    h1_regime_counts = {}
    ts_h1 = from_ts
    while ts_h1 <= to_ts:
        idx_m15 = bisect.bisect_left(m15_times, ts_h1)
        idx_h1  = bisect.bisect_left(h1_times,  ts_h1)
        ctx_m15_h = all_m15[max(0, idx_m15 - 100):idx_m15]
        ctx_h1_h  = all_h1[max(0, idx_h1  -  50):idx_h1]
        if len(ctx_m15_h) < 30 or len(ctx_h1_h) < 10:
            ts_h1 += 3600
            continue
        ph = ctx_m15_h[-1]["close"]
        rh = detect_regime_new(ctx_m15_h, ctx_h1_h, ph)
        rh_name = rh["regime"]
        rh_dir  = rh.get("direction", "none")
        h1_regime_counts[rh_name] = h1_regime_counts.get(rh_name, 0) + 1
        fut_h = all_m15[idx_m15:idx_m15 + 200]
        if not fut_h:
            ts_h1 += 3600
            continue
        if rh_dir == "down":
            s = setup_trend_pullback_short(rh, ctx_m15_h, ctx_h1_h, ph)
            if s:
                record_algo_outcome(stats_algo, "trend_pb_short", s,
                                    evaluate_setup(s, fut_h, entry_window_h=24))
            if rh_name == "IMPULSE_DOWN":
                s = setup_impulse_cont_short(rh, ctx_m15_h, ctx_h1_h, ph)
                if s:
                    record_algo_outcome(stats_algo, "imp_cont_short", s,
                                        evaluate_setup(s, fut_h, entry_window_h=24))
                _, is_spk = compute_spike_filter(ctx_m15_h, "down")
                if not is_spk:
                    s = setup_version_d(rh, ctx_m15_h, ctx_h1_h, ph, vol_threshold=2.0)
                    if s:
                        record_algo_outcome(stats_algo, "impulse_d_short", s,
                                            evaluate_setup(s, fut_h, entry_window_h=1))
        elif rh_dir == "up":
            s = setup_trend_pullback_long(rh, ctx_m15_h, ctx_h1_h, ph)
            if s:
                record_algo_outcome(stats_algo, "trend_pb_long", s,
                                    evaluate_setup(s, fut_h, entry_window_h=24))
            if rh_name == "IMPULSE_UP":
                _, is_spk = compute_spike_filter(ctx_m15_h, "up")
                if not is_spk:
                    s = setup_version_d(rh, ctx_m15_h, ctx_h1_h, ph, vol_threshold=2.0)
                    if s:
                        record_algo_outcome(stats_algo, "impulse_d_long", s,
                                            evaluate_setup(s, fut_h, entry_window_h=1))
        elif rh_name == "RANGE":
            s = setup_range_short(rh, ctx_m15_h, ctx_h1_h, ph)
            if s:
                record_algo_outcome(stats_algo, "range_short", s,
                                    evaluate_setup(s, fut_h, entry_window_h=24))
            s = setup_range_long(rh, ctx_m15_h, ctx_h1_h, ph)
            if s:
                record_algo_outcome(stats_algo, "range_long", s,
                                    evaluate_setup(s, fut_h, entry_window_h=24))
        ts_h1 += 3600
    return stats_algo, h1_regime_counts


def _collect_setups_for_hour(rh, ctx_m15_h, ctx_h1_h, ph):
    """Zbiera wszystkie valid setupy dla danej H1 godziny. Zwraca listę (key, setup, entry_window_h)."""
    rh_name = rh["regime"]
    rh_dir  = rh.get("direction", "none")
    candidates = []
    if rh_dir == "down":
        s = setup_trend_pullback_short(rh, ctx_m15_h, ctx_h1_h, ph)
        if s: candidates.append(("trend_pb_short", s, 24))
        if rh_name == "IMPULSE_DOWN":
            s = setup_impulse_cont_short(rh, ctx_m15_h, ctx_h1_h, ph)
            if s: candidates.append(("imp_cont_short", s, 24))
            _, is_spk = compute_spike_filter(ctx_m15_h, "down")
            if not is_spk:
                s = setup_version_d(rh, ctx_m15_h, ctx_h1_h, ph, vol_threshold=2.0)
                if s: candidates.append(("impulse_d_short", s, 1))
    elif rh_dir == "up":
        s = setup_trend_pullback_long(rh, ctx_m15_h, ctx_h1_h, ph)
        if s: candidates.append(("trend_pb_long", s, 24))
        if rh_name == "IMPULSE_UP":
            _, is_spk = compute_spike_filter(ctx_m15_h, "up")
            if not is_spk:
                s = setup_version_d(rh, ctx_m15_h, ctx_h1_h, ph, vol_threshold=2.0)
                if s: candidates.append(("impulse_d_long", s, 1))
    elif rh_name == "RANGE":
        s = setup_range_short(rh, ctx_m15_h, ctx_h1_h, ph)
        if s: candidates.append(("range_short", s, 24))
        s = setup_range_long(rh, ctx_m15_h, ctx_h1_h, ph)
        if s: candidates.append(("range_long", s, 24))
    return candidates


def run_h1_scan_best(from_ts, to_ts, all_m15, all_h1, m15_times, h1_times):
    """
    H1 scan w stylu starego Algo2: per godzina wybiera JEDEN setup z najwyższym RR.
    Zwraca (stats_algo, h1_regime_counts).
    """
    stats_algo = make_algo_stats()
    h1_regime_counts = {}
    ts_h1 = from_ts
    while ts_h1 <= to_ts:
        idx_m15 = bisect.bisect_left(m15_times, ts_h1)
        idx_h1  = bisect.bisect_left(h1_times,  ts_h1)
        ctx_m15_h = all_m15[max(0, idx_m15 - 100):idx_m15]
        ctx_h1_h  = all_h1[max(0, idx_h1  -  50):idx_h1]
        if len(ctx_m15_h) < 30 or len(ctx_h1_h) < 10:
            ts_h1 += 3600
            continue
        ph = ctx_m15_h[-1]["close"]
        rh = detect_regime_new(ctx_m15_h, ctx_h1_h, ph)
        rh_name = rh["regime"]
        h1_regime_counts[rh_name] = h1_regime_counts.get(rh_name, 0) + 1
        fut_h = all_m15[idx_m15:idx_m15 + 200]
        if not fut_h:
            ts_h1 += 3600
            continue
        candidates = _collect_setups_for_hour(rh, ctx_m15_h, ctx_h1_h, ph)
        if candidates:
            # Wybierz setup z najwyższym RR (jak stary Algo2)
            best_key, best_s, best_ew = max(candidates, key=lambda x: x[1].get("rr", 0))
            record_algo_outcome(stats_algo, best_key, best_s,
                                evaluate_setup(best_s, fut_h, entry_window_h=best_ew))
        ts_h1 += 3600
    return stats_algo, h1_regime_counts


def _month_ranges(from_ts, to_ts):
    """Generator: zwraca (label, month_from_ts, month_to_ts) dla każdego miesiąca w zakresie."""
    dt = datetime.fromtimestamp(from_ts, tz=timezone.utc)
    while True:
        year, month = dt.year, dt.month
        # Pierwszy dzień miesiąca
        m_start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())
        # Pierwszy dzień następnego miesiąca
        if month == 12:
            m_end = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()) - 1
        else:
            m_end = int(datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp()) - 1
        # Przytnij do zakresu
        chunk_from = max(from_ts, m_start)
        chunk_to   = min(to_ts,   m_end)
        label = f"{year}-{month:02d}"
        yield label, chunk_from, chunk_to
        # Następny miesiąc
        if month == 12:
            dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            dt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        if int(dt.timestamp()) > to_ts:
            break


def print_monthly_table(monthly_results):
    """
    Drukuje tabelę miesięczną.
    monthly_results: lista (label, stats_algo, h1_regime_counts)
    """
    _SETUP_COLS = [
        ("trend_pb_short",  "tpb_s"),
        ("trend_pb_long",   "tpb_l"),
        ("imp_cont_short",  "imc_s"),
        ("range_short",     "rng_s"),
        ("range_long",      "rng_l"),
        ("impulse_d_short", "impD_s"),
        ("impulse_d_long",  "impD_l"),
    ]
    header_keys = ["miesiąc", "rezimy(%)", *[c[1] for c in _SETUP_COLS], "wejść", "avg$/tx"]
    print(f"\n{'='*90}")
    print("WYNIKI MIESIĘCZNE — SYMULACJA LIVE ALGO (split 50/50 z BE)")
    print(f"{'='*90}")
    # nagłówek
    print(f"  {'Miesiąc':<9}  {'Reżimy (ImpD/ImpU/TrD/TrU/Rng%)':<32}  "
          + "  ".join(f"{c[1]:>6}" for c in _SETUP_COLS)
          + f"  {'wejść':>5}  {'avg$/tx':>8}")
    print(f"  {'-'*9}  {'-'*32}  " + "  ".join("-" * 6 for _ in _SETUP_COLS) + f"  {'-----':>5}  {'--------':>8}")

    totals = make_algo_stats()

    for label, stats_algo, h1_regime_counts in monthly_results:
        total_h1 = sum(h1_regime_counts.values()) or 1
        def rp(k):
            return f"{h1_regime_counts.get(k,0)/total_h1*100:.0f}"
        regime_str = (f"ID:{rp('IMPULSE_DOWN')} IU:{rp('IMPULSE_UP')} "
                      f"TD:{rp('TREND_DOWN')} TU:{rp('TREND_UP')} R:{rp('RANGE')}")

        month_filled = 0
        month_pnl = 0.0
        col_strs = []
        for key, _ in _SETUP_COLS:
            st = stats_algo[key]
            f  = st["filled"]
            tp1 = st["tp1_wins"]
            month_filled += f
            month_pnl += st["pnl_tp1_sum"] + st["pnl_tp2_sum"]
            wr = f"{tp1/f*100:.0f}%" if f > 0 else "  —"
            col_strs.append(f"{f:>2}/{wr:>4}")
            # akumuluj w totals
            for field in ("total","filled","tp1_wins","tp2_wins","sl_losses","open",
                          "pnl_tp1_sum","pnl_tp2_sum"):
                totals[key][field] += st[field]

        avg = month_pnl / month_filled if month_filled > 0 else 0.0
        sign = "+" if avg >= 0 else ""
        print(f"  {label:<9}  {regime_str:<32}  "
              + "  ".join(col_strs)
              + f"  {month_filled:>5}  {sign}${avg:>6.2f}")

    # Wiersz sumaryczny
    tot_filled = sum(totals[k]["filled"] for k in totals)
    tot_pnl    = sum(totals[k]["pnl_tp1_sum"] + totals[k]["pnl_tp2_sum"] for k in totals)
    avg_tot    = tot_pnl / tot_filled if tot_filled > 0 else 0.0
    sign_tot   = "+" if avg_tot >= 0 else ""
    tot_cols   = []
    for key, _ in _SETUP_COLS:
        st = totals[key]
        f  = st["filled"]
        tp1 = st["tp1_wins"]
        wr = f"{tp1/f*100:.0f}%" if f > 0 else "  —"
        tot_cols.append(f"{f:>2}/{wr:>4}")
    print(f"  {'-'*9}  {'-'*32}  " + "  ".join("-" * 6 for _ in _SETUP_COLS) + f"  {'-----':>5}  {'--------':>8}")
    print(f"  {'RAZEM':<9}  {'':32}  "
          + "  ".join(tot_cols)
          + f"  {tot_filled:>5}  {sign_tot}${avg_tot:>6.2f}")
    print(f"\n  Legenda kolumn: tpb_s=trend_pb_short  tpb_l=trend_pb_long  imc_s=imp_cont_short")
    print(f"                  rng_s=range_short  rng_l=range_long  impD_s/l=impulse_D short/long")
    print(f"  Format komórki: wejść/WR%")


def make_algo_stats():
    def _entry():
        return {
            "total": 0, "filled": 0,
            "tp1_wins": 0, "tp2_wins": 0, "sl_losses": 0, "open": 0,
            "pnl_tp1_sum": 0.0, "pnl_tp2_sum": 0.0,
        }
    return {
        "trend_pb_short":  _entry(),  # TREND_DOWN + IMPULSE_DOWN fib pullback
        "trend_pb_long":   _entry(),  # TREND_UP + IMPULSE_UP fib pullback (str>=5)
        "imp_cont_short":  _entry(),  # IMPULSE_DOWN continuation (1-2 greens/6)
        "range_short":     _entry(),  # RANGE resistance short
        "range_long":      _entry(),  # RANGE support long
        "impulse_d_short": _entry(),  # IMPULSE_DOWN Version D (vol>=2.0x, spike-filter)
        "impulse_d_long":  _entry(),  # IMPULSE_UP Version D (vol>=2.0x, spike-filter)
    }


def record_algo_outcome(stats, key, setup, result):
    """Rejestruje wynik setupu algo — wywoływana tylko gdy setup wygenerowany."""
    st = stats[key]
    st["total"] += 1
    wynik = result["wynik"]
    if wynik == "no_entry":
        return
    st["filled"] += 1
    scale = 100.0 / setup["w"] if setup["w"] > 0 else 1.0
    if wynik in ("TP1+TP2", "TP1"):
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
        if wynik == "TP1+TP2":
            st["tp2_wins"] += 1
    elif wynik == "TP1+BE":
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
    elif wynik == "TP1+SL":
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
    elif wynik == "SL":
        st["sl_losses"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
    elif wynik == "open":
        st["open"] += 1


_ALGO_KEYS_LABELS = [
    ("trend_pb_short",  "TREND_DOWN/IMPULSE_DOWN -> fib38-50% pullback short"),
    ("trend_pb_long",   "TREND_UP/IMPULSE_UP    -> fib38-50% pullback long (str>=5)"),
    ("imp_cont_short",  "IMPULSE_DOWN           -> continuation (1-2 greens/6 M15)"),
    ("range_short",     "RANGE                  -> resistance short (3 filtry)"),
    ("range_long",      "RANGE                  -> support long (3 filtry)"),
    ("impulse_d_short", "IMPULSE_DOWN           -> Version D market short (vol>=2.0x)"),
    ("impulse_d_long",  "IMPULSE_UP             -> Version D market long  (vol>=2.0x)"),
]


def print_algo_stats(stats):
    print(f"\n{'='*70}")
    print("SYMULACJA LIVE ALGO — WSZYSTKIE REZIMY (split 50/50 z BE, H1 scan)")
    print("  total = wygenerowane setupy na kazdej swiece H1 (bez cooldown)")
    print("  Uwaga: ta sama okazja moze byc liczona wielokrotnie gdy utrzymuje sie >1h")
    print(f"{'='*70}")
    total_pnl = 0.0
    total_filled = 0
    for key, label in _ALGO_KEYS_LABELS:
        st = stats[key]
        n = st["total"]
        f = st["filled"]
        tp1 = st["tp1_wins"]
        tp2 = st["tp2_wins"]
        sl = st["sl_losses"]
        op = st["open"]
        avg_pnl = (st["pnl_tp1_sum"] + st["pnl_tp2_sum"]) / f if f > 0 else 0.0
        total_pnl += st["pnl_tp1_sum"] + st["pnl_tp2_sum"]
        total_filled += f
        sign = "+" if avg_pnl >= 0 else ""
        fp = f"{f/n*100:.0f}%" if n > 0 else "N/A"
        t1p = f"{tp1/f*100:.0f}%" if f > 0 else "N/A"
        print(
            f"  {key:<18}: {n:3d} alertow, fill {f}/{n} ({fp}), "
            f"TP1 {tp1}/{f} ({t1p}), TP2 {tp2}/{f} ({pct(tp2,f)}), "
            f"SL {sl}/{f} ({pct(sl,f)}), open {op}, avg pnl: {sign}${avg_pnl:.2f}"
        )
        print(f"  {'':18}  -> {label}")
    ts = "+" if total_pnl >= 0 else ""
    ta = "+" if (total_pnl / total_filled if total_filled else 0) >= 0 else ""
    avg = total_pnl / total_filled if total_filled > 0 else 0.0
    print(f"\n  LACZNIE: {total_filled} wejsc, avg: {ta}${avg:.2f}/transakcje, "
          f"suma pnl: {ts}${total_pnl:.2f}")


def pct(num, denom):
    if denom == 0: return "0%"
    return f"{num/denom*100:.0f}%"


def print_version_stats(name, desc, stats):
    print(f"\n=== {name} ({desc}) ===")
    for dir_key in ["IMPULSE_DOWN", "IMPULSE_UP"]:
        st = stats[dir_key]
        n = st["total"]
        f = st["filled"]
        tp1 = st["tp1_wins"]
        tp2 = st["tp2_wins"]
        sl = st["sl_losses"]
        op = st["open"]
        avg_pnl = (st["pnl_tp1_sum"] + st["pnl_tp2_sum"]) / f if f > 0 else 0.0
        sign = "+" if avg_pnl >= 0 else ""
        print(
            f"  {dir_key:<14}: {n:3d} setupow, fill {f}/{n} ({pct(f, n)}), "
            f"TP1 {tp1}/{f} ({pct(tp1, f)}), TP2 {tp2}/{f} ({pct(tp2, f)}), "
            f"SL {sl}/{f} ({pct(sl, f)}), open {op}, "
            f"avg pnl: {sign}${avg_pnl:.2f}"
        )
        # Spike filter summary
        spk = st["spike_filtered"]
        if spk > 0:
            spk_tp1 = st["spike_tp1_wins"]
            spk_sl  = st["spike_sl_losses"]
            spk_pnl = st["spike_pnl_sum"]
            spk_sign = "+" if spk_pnl >= 0 else ""
            print(
                f"  {'':14}  [SPIKE-FILTER] zablokowano {spk} setupow: "
                f"TP1 {spk_tp1}/{spk} ({pct(spk_tp1, spk)}), "
                f"SL {spk_sl}/{spk} ({pct(spk_sl, spk)}), "
                f"pnl gdyby weszly: {spk_sign}${spk_pnl:.2f}"
            )


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Impulse Backtest — 3 wersje logiki impulse trade")
    parser.add_argument("--from", dest="from_dt", help='Data poczatkowa, np. "2025-08-01 00:00"')
    parser.add_argument("--to", dest="to_dt", help='Data koncowa, np. "2025-09-30 23:59"')
    parser.add_argument("--days", type=int, default=90, help="Liczba dni wstecz (domyslnie 90)")
    parser.add_argument("--monthly", action="store_true",
                        help="Podziel zakres na miesiace i pokaz wyniki miesiac po miesiacu")
    args = parser.parse_args()

    now_ts = int(datetime.now(tz=timezone.utc).timestamp())

    if args.from_dt and args.to_dt:
        from_ts = _parse_date_only(args.from_dt)
        to_ts = _parse_date_only(args.to_dt)
    elif args.from_dt:
        from_ts = _parse_date_only(args.from_dt)
        to_ts = now_ts
    else:
        from_ts = now_ts - args.days * 86400
        to_ts = now_ts

    print(f"Zakres: {_ts_fmt(from_ts)} -> {_ts_fmt(to_ts)}")
    total_days = (to_ts - from_ts) / 86400
    print(f"Lacznie {total_days:.1f} dni")

    symbol = "SOLUSDT"
    # Fetch all data once
    # We need candles up to to_ts; fetch enough for the full range + lookback
    needed_m15 = int((to_ts - from_ts) / 900) + 200
    needed_h1 = int((to_ts - from_ts) / 3600) + 100

    print(f"\nPobieram swiece M15 ({needed_m15} sztuk)...")
    all_m15 = fetch_klines_paginated(symbol, "15m", needed_m15, end_ts_s=to_ts + 900)
    print(f"Pobrano {len(all_m15)} swiec M15")

    print(f"\nPobieram swiece H1 ({needed_h1} sztuk)...")
    all_h1 = fetch_klines_paginated(symbol, "1h", needed_h1, end_ts_s=to_ts + 3600)
    print(f"Pobrano {len(all_h1)} swiec H1\n")

    # -- Tryb MONTHLY — tylko H1 scan, bez M15 signal loop ---------------------
    if args.monthly:
        m15_times = [c["time"] for c in all_m15]
        h1_times  = [c["time"] for c in all_h1]
        monthly_all  = []   # wariant: wszystkie setupy per H1
        monthly_best = []   # wariant: najlepszy RR per H1 (jak stary Algo2)
        for m_label, m_from, m_to in _month_ranges(from_ts, to_ts):
            print(f"  Skanuje {m_label} ({_ts_fmt(m_from)} -> {_ts_fmt(m_to)})...")
            m_all,  m_reg_all  = run_h1_scan(m_from, m_to, all_m15, all_h1,
                                              m15_times, h1_times)
            m_best, m_reg_best = run_h1_scan_best(m_from, m_to, all_m15, all_h1,
                                                   m15_times, h1_times)
            monthly_all.append((m_label, m_all, m_reg_all))
            monthly_best.append((m_label, m_best, m_reg_best))
        print("\n-- WARIANT A: wszystkie setupy per H1 (obecna logika) --")
        print_monthly_table(monthly_all)
        print("\n-- WARIANT B: najlepszy RR per H1 (logika starego Algo2) --")
        print_monthly_table(monthly_best)
        print()
        return

    # Statistics per version
    stats_a = make_stats()
    stats_b = make_stats()
    stats_c = make_stats()
    stats_d = make_stats()
    stats_e = make_stats()
    stats_f = make_stats()
    stats_g = make_stats()   # D + swing n=24
    stats_h = make_stats()   # D + ATR fallback
    stats_i = make_stats()   # D + swing n=24 + ATR fallback
    stats_d_tp1  = make_stats()  # D  tp1_only
    stats_a_tp1  = make_stats()  # A  tp1_only
    stats_e_tp1  = make_stats()  # E  tp1_only (vol>=1.7x)
    stats_g_tp1  = make_stats()  # G  tp1_only (swing n=24)
    stats_h_tp1  = make_stats()  # H  tp1_only (ATR fallback)
    stats_i_tp1  = make_stats()  # I  tp1_only (G+H)
    stats_d_nobe = make_stats()  # D  split bez BE (oryg. SL na 2. połowie)
    stats_a_nobe = make_stats()  # A  split bez BE

    # Live algo stats — wszystkie rezimy
    stats_algo = make_algo_stats()

    # Cooldown tracking — IMPULSE-only analysis
    last_impulse_ts_down = 0
    last_impulse_ts_up = 0
    cooldown_s = 4 * 3600

    # Cooldown tracking — live algo (all regimes), per kierunek
    # USUNIĘTE — cooldown zastąpiony osobną pętlą H1 poniżej

    signal_count = 0

    # Iterate every 15 minutes
    ts = from_ts
    while ts <= to_ts:
        # Build context
        ctx_m15 = [c for c in all_m15 if c["time"] < ts][-100:]
        ctx_h1  = [c for c in all_h1  if c["time"] < ts][-50:]

        if len(ctx_m15) < 30 or len(ctx_h1) < 10:
            ts += 900
            continue

        price = ctx_m15[-1]["close"]
        regime = detect_regime_new(ctx_m15, ctx_h1, price)
        regime_name = regime["regime"]
        direction   = regime.get("direction", "none")

        # -- IMPULSE-ONLY analysis (unchanged) --------------------------------
        if regime_name not in ("IMPULSE_DOWN", "IMPULSE_UP"):
            ts += 900
            continue

        # Cooldown check
        if regime_name == "IMPULSE_DOWN":
            if ts - last_impulse_ts_down < cooldown_s:
                ts += 900
                continue
            last_impulse_ts_down = ts
            direction_key = "IMPULSE_DOWN"
        else:
            if ts - last_impulse_ts_up < cooldown_s:
                ts += 900
                continue
            last_impulse_ts_up = ts
            direction_key = "IMPULSE_UP"

        signal_count += 1

        # Spike-reversal filter
        spike_score, is_spike_filtered = compute_spike_filter(ctx_m15, regime["direction"])

        # Future candles for evaluation
        future_m15 = [c for c in all_m15 if c["time"] >= ts][:200]

        if not future_m15:
            ts += 900
            continue

        # Version A (entry window 2h)
        setup_a = setup_version_a(regime, ctx_m15, ctx_h1, price)
        res_a = None
        if setup_a:
            res_a = evaluate_setup(setup_a, future_m15, entry_window_h=2)
            record_outcome(stats_a, direction_key, res_a, setup_a["w"], spike_filtered=is_spike_filtered)
            res_a_tp1 = evaluate_setup(setup_a, future_m15, entry_window_h=2, tp1_only=True)
            record_outcome(stats_a_tp1, direction_key, res_a_tp1, setup_a["w"], spike_filtered=is_spike_filtered)
            res_a_nobe = evaluate_setup(setup_a, future_m15, entry_window_h=2, no_be=True)
            record_outcome(stats_a_nobe, direction_key, res_a_nobe, setup_a["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_a[direction_key]["total"] += 1
            stats_a_tp1[direction_key]["total"] += 1
            stats_a_nobe[direction_key]["total"] += 1

        # Version B (entry window 2h)
        setup_b = setup_version_b(regime, ctx_m15, ctx_h1, price)
        res_b = None
        if setup_b:
            res_b = evaluate_setup(setup_b, future_m15, entry_window_h=2)
            record_outcome(stats_b, direction_key, res_b, setup_b["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_b[direction_key]["total"] += 1

        # Version C (entry window 4h)
        setup_c = setup_version_c(regime, ctx_m15, ctx_h1, price)
        res_c = None
        if setup_c:
            res_c = evaluate_setup(setup_c, future_m15, entry_window_h=4)
            record_outcome(stats_c, direction_key, res_c, setup_c["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_c[direction_key]["total"] += 1

        # Version D (market entry, vol_ratio >= 2.0, entry window 1h)
        setup_d = setup_version_d(regime, ctx_m15, ctx_h1, price, vol_threshold=2.0)
        res_d = None
        if setup_d:
            res_d = evaluate_setup(setup_d, future_m15, entry_window_h=1)
            record_outcome(stats_d, direction_key, res_d, setup_d["w"], spike_filtered=is_spike_filtered)
            res_d_tp1 = evaluate_setup(setup_d, future_m15, entry_window_h=1, tp1_only=True)
            record_outcome(stats_d_tp1, direction_key, res_d_tp1, setup_d["w"], spike_filtered=is_spike_filtered)
            res_d_nobe = evaluate_setup(setup_d, future_m15, entry_window_h=1, no_be=True)
            record_outcome(stats_d_nobe, direction_key, res_d_nobe, setup_d["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_d[direction_key]["total"] += 1
            stats_d_tp1[direction_key]["total"] += 1
            stats_d_nobe[direction_key]["total"] += 1

        # Version E (market entry, vol_ratio >= 1.7, entry window 1h)
        setup_e = setup_version_d(regime, ctx_m15, ctx_h1, price, vol_threshold=1.7)
        res_e = None
        if setup_e:
            res_e = evaluate_setup(setup_e, future_m15, entry_window_h=1)
            record_outcome(stats_e, direction_key, res_e, setup_e["w"], spike_filtered=is_spike_filtered)
            res_e_tp1 = evaluate_setup(setup_e, future_m15, entry_window_h=1, tp1_only=True)
            record_outcome(stats_e_tp1, direction_key, res_e_tp1, setup_e["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_e[direction_key]["total"] += 1
            stats_e_tp1[direction_key]["total"] += 1

        # Version F (volume-switching: vol>=2.0x->market, 1.5-2.0x->pullback)
        setup_f = setup_version_f(regime, ctx_m15, ctx_h1, price)
        res_f = None
        entry_window_f = 1 if (setup_f and "mkt" in setup_f.get("type", "")) else 2
        if setup_f:
            res_f = evaluate_setup(setup_f, future_m15, entry_window_h=entry_window_f)
            record_outcome(stats_f, direction_key, res_f, setup_f["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_f[direction_key]["total"] += 1

        # Version G: D + swing n=24
        setup_g = setup_version_d(regime, ctx_m15, ctx_h1, price, swing_n=24, label="G")
        res_g = None
        if setup_g:
            res_g = evaluate_setup(setup_g, future_m15, entry_window_h=1)
            record_outcome(stats_g, direction_key, res_g, setup_g["w"], spike_filtered=is_spike_filtered)
            res_g_tp1 = evaluate_setup(setup_g, future_m15, entry_window_h=1, tp1_only=True)
            record_outcome(stats_g_tp1, direction_key, res_g_tp1, setup_g["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_g[direction_key]["total"] += 1
            stats_g_tp1[direction_key]["total"] += 1

        # Version H: D + ATR fallback
        setup_h = setup_version_d(regime, ctx_m15, ctx_h1, price, atr_fallback=True, label="H")
        res_h = None
        if setup_h:
            res_h = evaluate_setup(setup_h, future_m15, entry_window_h=1)
            record_outcome(stats_h, direction_key, res_h, setup_h["w"], spike_filtered=is_spike_filtered)
            res_h_tp1 = evaluate_setup(setup_h, future_m15, entry_window_h=1, tp1_only=True)
            record_outcome(stats_h_tp1, direction_key, res_h_tp1, setup_h["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_h[direction_key]["total"] += 1
            stats_h_tp1[direction_key]["total"] += 1

        # Version I: D + swing n=24 + ATR fallback (kombinacja)
        setup_i = setup_version_d(regime, ctx_m15, ctx_h1, price, swing_n=24, atr_fallback=True, label="I")
        res_i = None
        if setup_i:
            res_i = evaluate_setup(setup_i, future_m15, entry_window_h=1)
            record_outcome(stats_i, direction_key, res_i, setup_i["w"], spike_filtered=is_spike_filtered)
            res_i_tp1 = evaluate_setup(setup_i, future_m15, entry_window_h=1, tp1_only=True)
            record_outcome(stats_i_tp1, direction_key, res_i_tp1, setup_i["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_i[direction_key]["total"] += 1
            stats_i_tp1[direction_key]["total"] += 1

        # Print signal line
        def fmt_res(setup, res):
            if setup is None:
                return "brak_setupu"
            if res is None:
                return "brak_setupu"
            t = setup.get("type", "?")
            w = res["wynik"]
            return f"{t}:{w}"

        spk_tag = f" [SPIKE-FILT spk={spike_score}]" if is_spike_filtered else (f" spk={spike_score}" if spike_score > 0 else "")
        print(
            f"[{_ts_fmt(ts)}] {regime_name} price={price:.2f} vol={regime.get('vol_ratio',0):.1f}x str={regime['strength']}{spk_tag} | "
            f"D={fmt_res(setup_d, res_d)} | G={fmt_res(setup_g, res_g)} | H={fmt_res(setup_h, res_h)} | I={fmt_res(setup_i, res_i)}"
        )

        ts += 900

    # -- H1 ALGO SCAN — wszystkie rezimy, bez cooldown -------------------------
    m15_times = [c["time"] for c in all_m15]
    h1_times  = [c["time"] for c in all_h1]

    print(f"\nH1 scan — wszystkie rezimy ({int((to_ts - from_ts)/3600)} swiece H1)...")
    stats_algo, h1_regime_counts = run_h1_scan(
        from_ts, to_ts, all_m15, all_h1, m15_times, h1_times, stats_algo
    )

    total_h1_scanned = sum(h1_regime_counts.values())
    if total_h1_scanned > 0:
        regime_summary = ", ".join(
            f"{k}:{v}({v/total_h1_scanned*100:.0f}%)"
            for k, v in sorted(h1_regime_counts.items(), key=lambda x: -x[1])
        )
        print(f"  Rezimy H1: {regime_summary}")

    # Print final report
    print(f"\n{'='*70}")
    print(f"PODSUMOWANIE — {signal_count} sygnalow IMPULSE w zakresie")
    print(f"{'='*70}")

    print(f"\n{'-'*70}")
    print("POROWNANIE: TP1+TP2 (split 50/50) vs TP1-ONLY (cala pozycja)")
    print(f"{'-'*70}")
    print_version_stats("WERSJA D [TP1+TP2 split]", "polowa na TP1, polowa na TP2/BE", stats_d)
    print_version_stats("WERSJA D [TP1-ONLY]",       "cala pozycja zamykana na TP1", stats_d_tp1)
    print_version_stats("WERSJA D [split bez BE]",   "polowa na TP1, polowa na TP2 lub ORYGINALNYM SL", stats_d_nobe)

    print(f"\n{'-'*70}")
    print("POROWNANIE TP1-ONLY — kto wygrywa przy zamknieciu calej pozycji na TP1?")
    print(f"{'-'*70}")
    print_version_stats("WERSJA A [pullback, TP1-only]",    "czeka na 1-2 swiece odreagowania", stats_a_tp1)
    print_version_stats("WERSJA D [market vol>=2.0x, TP1-only]", "agresywne wejscie market", stats_d_tp1)
    print_version_stats("WERSJA E [market vol>=1.7x, TP1-only]", "latwiejszy prog wolumenu", stats_e_tp1)
    print_version_stats("WERSJA G [swing n=24, TP1-only]",  "szerszy lookback swing points", stats_g_tp1)
    print_version_stats("WERSJA H [ATR fallback, TP1-only]","ATR*2.0 gdy swing zbyt blisko ceny", stats_h_tp1)
    print_version_stats("WERSJA I [G+H, TP1-only]",         "swing n=24 + ATR fallback", stats_i_tp1)

    print(f"\n{'-'*70}")
    print("SPLIT BEZ BE vs SPLIT Z BE (polowa pozycji, wersja D)")
    print(f"{'-'*70}")
    print_version_stats("WERSJA D [split z BE]",   "po TP1 SL przesuniete na entry", stats_d)
    print_version_stats("WERSJA D [split bez BE]", "po TP1 SL zostaje na oryg. poziomie", stats_d_nobe)
    print_version_stats("WERSJA A [split z BE]",   "pullback — po TP1 SL na entry", stats_a)
    print_version_stats("WERSJA A [split bez BE]", "pullback — po TP1 SL na oryg.", stats_a_nobe)

    print(f"\n{'-'*70}")
    print("POROWNANIE: NAPRAWA TP1 (wszystkie vol>=2.0x, market entry)")
    print(f"{'-'*70}")
    print_version_stats("WERSJA D [oryginalna]",   "swing n=12, bez fallback", stats_d)
    print_version_stats("WERSJA G [swing n=24]",   "szerszy lookback swing points", stats_g)
    print_version_stats("WERSJA H [ATR fallback]", "ATR*2.0 gdy swing zbyt blisko ceny", stats_h)
    print_version_stats("WERSJA I [G+H]",          "swing n=24 + ATR fallback", stats_i)

    print(f"\n{'-'*70}")
    print("POROWNANIE STRATEGII WEJSCIA")
    print(f"{'-'*70}")
    print_version_stats("STRATEGIA 1 [pullback]",  "czeka na 1-2 swiece odreagowania (Wersja A)", stats_a)
    print_version_stats("STRATEGIA 2 [agresywna]", "market entry vol>=2.0x (Wersja D)", stats_d)
    print_version_stats("STRATEGIA 3 [switching]", "vol>=2.0x->market / 1.5-2.0x->pullback (F)", stats_f)

    print(f"\n{'-'*70}")
    print("SZCZEGOLY POMOCNICZE")
    print(f"{'-'*70}")
    print_version_stats("WERSJA B", "Fibonacci limit, czeka na pullback", stats_b)
    print_version_stats("WERSJA C", "Fibonacci limit, antycypuje", stats_c)
    print_version_stats("WERSJA E", "market entry natychmiast, vol >= 1.7x", stats_e)

    print_algo_stats(stats_algo)

    print()


if __name__ == "__main__":
    main()
