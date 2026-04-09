#!/usr/bin/env python3
"""
Impulse Backtest — porownanie 3 wersji logiki impulse trade dla SOL/USDT.

Uruchomienie:
    python impulse_backtest.py --days 90
    python impulse_backtest.py --from "2025-08-01 00:00" --to "2025-09-30 23:59"
"""

import argparse
from datetime import datetime, timezone
from statistics import mean

import requests


# ── Helpers (kopie z gpt3_validator_backtest.py) ──────────────────────────────

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


def evaluate_setup(setup, future_m15, entry_window_h=24):
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

        if tp1_hit and not sl_hit:
            if tp2_hit:
                pnl = abs(w - tp2) if direction == "short" else abs(tp2 - w)
                return {"wynik": "TP1+TP2", "pnl_tp1": abs(w - tp1), "pnl_tp2": pnl}
        if sl_hit and not tp1_hit:
            return {"wynik": "SL", "pnl_tp1": -abs(sl - w), "pnl_tp2": -abs(sl - w)}
        if sl_hit and tp1_hit:
            return {"wynik": "TP1+BE", "pnl_tp1": abs(w - tp1), "pnl_tp2": 0}

    return {"wynik": "open", "pnl_tp1": 0, "pnl_tp2": 0}


# ── Data fetching (kopie z gpt3_validator_backtest.py) ─────────────────────────

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
    result = fetch_klines_bitget(symbol, interval, total, end_ts_s)
    if len(result) >= total * 0.5:
        print(f"  [zrodlo: Bitget]")
        return result
    print(f"  [Bitget: za malo danych ({len(result)}), probe OKX...]")
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


# ── Setup generators ──────────────────────────────────────────────────────────

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


def setup_version_d(regime, ctx_m15, ctx_h1, price, vol_threshold=2.0):
    """
    Wersja D/E: AGRESYWNE wejście po aktualnej cenie — bez czekania na pullback.

    Filozofia: skoro pullback albo się nie wydarza, albo oznacza fake impuls,
    wchodzimy natychmiast przy aktualnej cenie gdy IMPULSE jest potwierdzony
    przez podwyższony wolumen.

    vol_threshold: minimalny vol_ratio (D=2.0, E=1.7)
    SL ustawiony na ATR × 1.2 od ceny wejścia.
    RR minimum 1.2 (nieco złagodzone bo wchodzimy blisko rynku).
    """
    regime_name = regime["regime"]
    vol_ratio = regime.get("vol_ratio", 1.0)

    if vol_ratio < vol_threshold:
        return None

    atr = calc_atr(ctx_h1[-20:]) if len(ctx_h1) >= 20 else calc_atr(ctx_h1)
    if atr <= 0:
        return None

    swing_high, swing_low = find_swing_points(ctx_h1, n=12)
    swing_low  = min(swing_low,  price)
    swing_high = max(swing_high, price)

    label = "D" if vol_threshold >= 2.0 else "E"

    if regime_name == "IMPULSE_DOWN":
        w   = price
        sl  = price + atr * 1.2
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
        w   = price
        sl  = price - atr * 1.2
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


# ── Statistics helpers ────────────────────────────────────────────────────────

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
        if wynik == "TP1+TP2":
            st["spike_tp1_wins"] += 1
            st["spike_tp2_wins"] += 1
        elif wynik == "TP1+BE":
            st["spike_tp1_wins"] += 1
        elif wynik == "SL":
            st["spike_sl_losses"] += 1
        return

    st["total"] += 1
    if wynik == "no_entry":
        return
    st["filled"] += 1
    # Normalize pnl to $100 per trade (pnl expressed as fraction of entry)
    if wynik == "TP1+TP2":
        st["tp1_wins"] += 1
        st["tp2_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
    elif wynik == "TP1+BE":
        st["tp1_wins"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
    elif wynik == "SL":
        st["sl_losses"] += 1
        st["pnl_tp1_sum"] += result["pnl_tp1"] * scale
        st["pnl_tp2_sum"] += result["pnl_tp2"] * scale
    elif wynik == "open":
        st["open"] += 1


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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Impulse Backtest — 3 wersje logiki impulse trade")
    parser.add_argument("--from", dest="from_dt", help='Data poczatkowa, np. "2025-08-01 00:00"')
    parser.add_argument("--to", dest="to_dt", help='Data koncowa, np. "2025-09-30 23:59"')
    parser.add_argument("--days", type=int, default=90, help="Liczba dni wstecz (domyslnie 90)")
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

    print(f"Zakres: {_ts_fmt(from_ts)} → {_ts_fmt(to_ts)}")
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

    # Statistics per version
    stats_a = make_stats()
    stats_b = make_stats()
    stats_c = make_stats()
    stats_d = make_stats()
    stats_e = make_stats()

    # Cooldown tracking
    last_impulse_ts_down = 0
    last_impulse_ts_up = 0
    cooldown_s = 4 * 3600

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
        elif not is_spike_filtered:
            stats_a[direction_key]["total"] += 1

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
        elif not is_spike_filtered:
            stats_d[direction_key]["total"] += 1

        # Version E (market entry, vol_ratio >= 1.7, entry window 1h)
        setup_e = setup_version_d(regime, ctx_m15, ctx_h1, price, vol_threshold=1.7)
        res_e = None
        if setup_e:
            res_e = evaluate_setup(setup_e, future_m15, entry_window_h=1)
            record_outcome(stats_e, direction_key, res_e, setup_e["w"], spike_filtered=is_spike_filtered)
        elif not is_spike_filtered:
            stats_e[direction_key]["total"] += 1

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
            f"A={fmt_res(setup_a, res_a)} | B={fmt_res(setup_b, res_b)} | C={fmt_res(setup_c, res_c)} | D={fmt_res(setup_d, res_d)} | E={fmt_res(setup_e, res_e)}"
        )

        ts += 900

    # Print final report
    print(f"\n{'='*70}")
    print(f"PODSUMOWANIE — {signal_count} sygnalow IMPULSE w zakresie")
    print(f"{'='*70}")

    print_version_stats("WERSJA A", "green/red candles (pullback 1-2 swiece)", stats_a)
    print_version_stats("WERSJA B", "Fibonacci limit, czeka na pullback", stats_b)
    print_version_stats("WERSJA C", "Fibonacci limit, antycypuje", stats_c)
    print_version_stats("WERSJA D", "market entry natychmiast, vol >= 2.0x [AGRESYWNA]", stats_d)
    print_version_stats("WERSJA E", "market entry natychmiast, vol >= 1.7x [AGRESYWNA-]", stats_e)

    print()


if __name__ == "__main__":
    main()
