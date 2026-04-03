#!/usr/bin/env python3
"""
GPT3 Validator Backtest — porownanie Algo2 samego vs Algo2+GPT3 filtr.

Uruchomienie:
    python gpt3_validator_backtest.py --hours 48
    python gpt3_validator_backtest.py --from "2026-04-01 00:00" --to "2026-04-03 00:00"
"""

import argparse
import json
import os
import re
import concurrent.futures
from datetime import datetime, timezone

import requests
import openai

OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")


# ── Helpers (kopie z diagnose_regime.py) ─────────────────────────────────────

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


def find_swing_points(candles_h1, n=12):
    recent = candles_h1[-n:]
    return max(c["high"] for c in recent), min(c["low"] for c in recent)


def find_consolidation(candles_h1, min_candles=4, max_candles=10):
    for n in range(min_candles, min(max_candles + 1, len(candles_h1))):
        recent = candles_h1[-n:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
        if atr > 0 and rng < atr * 2.5:
            return {"high": hi, "low": lo, "range": rng, "candles": n}
    return None


def find_broken_support(candles_h1, current_price):
    older = candles_h1[-16:-3] if len(candles_h1) >= 16 else candles_h1[:-3]
    if len(older) < 4: return None
    lows = [c["low"] for c in older]
    support_levels = [lows[i] for i in range(1, len(lows)-1) if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]]
    for level in sorted(support_levels):
        if level > current_price * 1.003 and level < current_price * 1.03:
            return level
    return None


def find_broken_resistance(candles_h1, current_price):
    older = candles_h1[-16:-3] if len(candles_h1) >= 16 else candles_h1[:-3]
    if len(older) < 4: return None
    highs = [c["high"] for c in older]
    res_levels = [highs[i] for i in range(1, len(highs)-1) if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]]
    for level in sorted(res_levels, reverse=True):
        if level < current_price * 0.997 and level > current_price * 0.97:
            return level
    return None


def algo_detect_setups(regime, candles_m15, candles_h1, current_price):
    regime_name = regime["regime"]
    direction = regime.get("direction", "none")
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    setups = []
    if atr <= 0:
        return setups
    max_entry_dist = current_price * 0.03

    if direction == "down":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)

        # trend_retest_short — WYLACZONY (17% WR)
        # find_broken_support nie uzywany

        # trend_consolidation_short
        consol = find_consolidation(candles_h1)
        if consol:
            w = consol["high"] - consol["range"] * 0.2
            sl = consol["high"] + atr * 1.0
            tp1 = consol["low"] - consol["range"]
            tp2 = consol["low"] - consol["range"] * 1.5
            if (sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                    and abs(w - current_price) <= max_entry_dist):
                setups.append({
                    "type": "trend_consolidation_short", "direction": "short",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((w - tp1) / (sl - w), 1),
                })

        # trend_pullback_short
        if swing_high > swing_low:
            swing_range = swing_high - swing_low
            fib38 = swing_low + swing_range * 0.38
            fib50 = swing_low + swing_range * 0.50
            fib618 = swing_low + swing_range * 0.618
            w = round((fib38 + fib50) / 2, 2)
            sl = round(fib618 + atr * 0.3, 2)
            tp1 = round(swing_low, 2)
            tp2 = round(swing_low - swing_range * 0.3, 2)
            if (sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                    and w > current_price * 1.003
                    and w - current_price <= max_entry_dist):
                setups.append({
                    "type": "trend_pullback_short", "direction": "short",
                    "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "rr": round((w - tp1) / (sl - w), 1),
                })

        # impulse_continuation_short (tylko IMPULSE)
        if regime_name.startswith("IMPULSE_"):
            last6 = candles_m15[-6:]
            greens = [c for c in last6 if c["close"] > c["open"]]
            if 1 <= len(greens) <= 2:
                pullback_high = max(c["high"] for c in last6[-2:])
                w = round(pullback_high, 2)
                sl = round(pullback_high + atr * 0.8, 2)
                tp1 = round(swing_low, 2)
                tp2 = round(swing_low - atr, 2)
                if (sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                        and abs(w - current_price) <= max_entry_dist):
                    setups.append({
                        "type": "impulse_continuation_short", "direction": "short",
                        "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                        "rr": round((w - tp1) / (sl - w), 1),
                    })

    elif direction == "up":
        strength = regime.get("strength", 0)
        swing_high, swing_low = find_swing_points(candles_h1, n=12)

        # trend_retest_long — WYLACZONY (30% WR)

        # trend_consolidation_long — WLACZONY dla backtestow GPT3 (filtr ma to oceniac)
        consol = find_consolidation(candles_h1)
        if consol:
            w = consol["low"] + consol["range"] * 0.2
            sl = consol["low"] - atr * 1.0
            tp1 = consol["high"] + consol["range"]
            tp2 = consol["high"] + consol["range"] * 1.5
            if sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5:
                setups.append({
                    "type": "trend_consolidation_long", "direction": "long",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

        # trend_pullback_long (wymaga strength >= 5)
        if swing_high > swing_low and strength >= 5:
            swing_range = swing_high - swing_low
            fib38 = swing_high - swing_range * 0.38
            fib50 = swing_high - swing_range * 0.50
            fib618 = swing_high - swing_range * 0.618
            w = round((fib38 + fib50) / 2, 2)
            sl = round(fib618 - atr * 0.3, 2)
            tp1 = round(swing_high, 2)
            tp2 = round(swing_high + swing_range * 0.3, 2)
            if (sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5
                    and w < current_price * 0.997 and current_price - w <= max_entry_dist):
                setups.append({
                    "type": "trend_pullback_long", "direction": "long",
                    "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

    elif regime_name == "RANGE":
        rng = detect_range(candles_h1)
        sup, res = rng["support"], rng["resistance"]
        rng_size = res - sup
        if rng_size > atr * 1.5:
            w = res - rng_size * 0.1
            sl = res + atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = sup + rng_size * 0.1
            if (w - tp1) / (sl - w) >= 1.5 and abs(w - current_price) <= max_entry_dist:
                setups.append({
                    "type": "range_resistance_short", "direction": "short",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((w - tp1) / (sl - w), 1),
                })
            w = sup + rng_size * 0.1
            sl = sup - atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = res - rng_size * 0.1
            if (tp1 - w) / (w - sl) >= 1.5 and abs(w - current_price) <= max_entry_dist:
                setups.append({
                    "type": "range_support_long", "direction": "long",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

    return setups


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


# ── Data fetching ─────────────────────────────────────────────────────────────

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


# ── GPT3 Validator ────────────────────────────────────────────────────────────

GPT3_VALIDATOR_SYSTEM_PROMPT = """Jestes ekspertem od oceny jakosci setupow tradingowych na SOL/USDT.

Algorytm wykryl potencjalny setup transakcyjny. Twoim jedynym zadaniem jest ocenic czy ten setup powinien zostac wykonany.

Otrzymujesz:
- Dane setupu: typ, kierunek, poziom wejscia, SL, TP1, TP2
- Aktualna cene i kontekst strukturalny (ATR, support, resistance, pozycja w range)
- 50 swiec H1 i 100 swiec M15 (OHLCV) do wlasnej oceny kontekstu

Oceniasz setup pod katem:
1. Czy rezim rynkowy (ktory sam okreslasz z danych) wspiera ten typ setupu?
2. Czy poziom wejscia ma sens strukturalnie (jest przy istotnym poziomie, nie w srodku niczego)?
3. Czy SL i TP sa logiczne wzgledem aktualnej struktury?
4. Czy nie ma oczywistych powodow odrzucenia (np. setup long w silnym downtrend, wejscie pod oporem)?

Zatwierdz setup gdy: rezim wspiera kierunek, poziom wejscia sensowny, brak oczywistych sygnalow contra.
Odrzuc setup gdy: rezim sprzeczny z kierunkiem, poziom wejscia bez sensu strukturalnego, setup long w crash, itp.

Zwroc WYLACZNIE poprawny JSON:
{"approve":true,"reason":"krotkie uzasadnienie max 1 zdanie","confidence":85}
lub
{"approve":false,"reason":"krotkie uzasadnienie max 1 zdanie","confidence":80}

- approve: true lub false
- reason: max 1 zdanie, konkretne
- confidence: 0-100, Twoja pewnosc co do decyzji"""


def build_validator_prompt(setup, candles_m15, candles_h1, current_price,
                            atr=None, support=None, resistance=None, price_pct_in_range=None):
    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-100:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-50:]
    )
    setup_block = (
        f"typ: {setup.get('type', '?')}\n"
        f"kierunek: {setup.get('direction', '?')}\n"
        f"wejscie: {setup.get('w', '?')}\n"
        f"SL: {setup.get('sl', '?')}\n"
        f"TP1: {setup.get('tp1', '?')}\n"
        f"TP2: {setup.get('tp2', '?')}\n"
        f"RR: {setup.get('rr', '?')}"
    )
    ctx_lines = [f"aktualna cena SOL: ${current_price:.2f}"]
    if support is not None and resistance is not None:
        ctx_lines.append(f"support H1: ${support:.2f} | resistance H1: ${resistance:.2f}")
    if price_pct_in_range is not None:
        ctx_lines.append(f"pozycja w H1 range: {price_pct_in_range:.0f}%")
    if atr is not None:
        ctx_lines.append(f"ATR(14): ${atr:.3f}")
    ctx_block = "\n".join(f"- {l}" for l in ctx_lines)
    return (
        f"Ocen ponizszy setup wygenerowany przez algorytm.\n\n"
        f"Setup:\n{setup_block}\n\n"
        f"Kontekst rynkowy:\n{ctx_block}\n\n"
        f"H1 candles (50):\n{h1_csv}\n\n"
        f"M15 candles (100):\n{m15_csv}\n\n"
        f"Okresl rezim samodzielnie z danych i zdecyduj: approve true/false.\n"
        f"Zwroc wylacznie JSON."
    )


_VALIDATOR_TIMEOUT_S = 60


def call_validator(setup, candles_m15, candles_h1, current_price,
                   atr=None, support=None, resistance=None, price_pct_in_range=None):
    if not OPENAI_KEY:
        return None
    user_msg = build_validator_prompt(setup, candles_m15, candles_h1, current_price,
                                      atr=atr, support=support, resistance=resistance,
                                      price_pct_in_range=price_pct_in_range)
    def _call():
        client = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model="gpt-4o", max_tokens=256,
            messages=[
                {"role": "system", "content": GPT3_VALIDATOR_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        return response.choices[0].message.content.strip()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                text = future.result(timeout=_VALIDATOR_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"  [gpt3-val] Timeout ({_VALIDATOR_TIMEOUT_S}s)")
                future.cancel()
                return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            print(f"  [gpt3-val] Brak JSON: {text[:200]}")
            return None
        return json.loads(match.group())
    except Exception as e:
        print(f"  [gpt3-val] Blad: {e}")
        return None


# ── Stats helpers ─────────────────────────────────────────────────────────────

def make_stats():
    return {
        "setups": 0, "entries": 0, "wins": 0, "losses": 0,
        "pnl_tp1": 0.0,      # TP1 alone (zamkniecie calej pozycji na TP1)
        "pnl_combined": 0.0, # TP1+TP2 combined (polowa na TP1, polowa na TP2)
        "by_type": {},
        "results": {"TP1+TP2": 0, "TP1+BE": 0, "SL": 0, "no_entry": 0, "open": 0},
    }


def record_outcome(stats, setup_type, outcome):
    stats["setups"] += 1
    wynik = outcome["wynik"]
    stats["results"][wynik] = stats["results"].get(wynik, 0) + 1

    pnl_tp1 = outcome["pnl_tp1"]   # wynik gdyby zamknac na TP1 (lub SL)
    pnl_tp2 = outcome["pnl_tp2"]   # dla TP1+TP2: dystans do TP2; dla SL: tez ujemny

    # TP1 alone: zamkniecie pelnej pozycji na TP1
    stats["pnl_tp1"] += pnl_tp1

    # TP1+TP2 combined: polowa pozycji na TP1, polowa na TP2
    if wynik == "TP1+TP2":
        combined = (pnl_tp1 + pnl_tp2) / 2
    elif wynik == "TP1+BE":
        combined = pnl_tp1 / 2  # polowa na TP1, polowa na BE (0)
    elif wynik == "SL":
        combined = pnl_tp1      # pelna strata (pnl_tp1 jest ujemny)
    else:
        combined = 0.0
    stats["pnl_combined"] += combined

    if wynik != "no_entry":
        stats["entries"] += 1
    if wynik in ("TP1+TP2", "TP1+BE"):
        stats["wins"] += 1
    elif wynik == "SL":
        stats["losses"] += 1
    t = stats["by_type"]
    if setup_type not in t:
        t[setup_type] = {"count": 0, "wins": 0, "losses": 0, "pnl_tp1": 0.0, "pnl_combined": 0.0}
    t[setup_type]["count"] += 1
    t[setup_type]["pnl_tp1"] += pnl_tp1
    t[setup_type]["pnl_combined"] += combined
    if wynik in ("TP1+TP2", "TP1+BE"):
        t[setup_type]["wins"] += 1
    elif wynik == "SL":
        t[setup_type]["losses"] += 1


def print_stats(label, stats):
    wins = stats["wins"]
    losses = stats["losses"]
    total_decided = wins + losses
    wr = wins / total_decided * 100 if total_decided > 0 else 0
    print(f"\n{'='*70}")
    print(f"PODSUMOWANIE: {label}")
    print(f"{'='*70}")
    print(f"  Setupy wygenerowane : {stats['setups']}")
    print(f"  Wejscia (entry hit) : {stats['entries']}")
    print(f"  Wins (TP1+TP2/BE)  : {wins}")
    print(f"  Losses (SL)        : {losses}")
    print(f"  Win rate           : {wins}/{total_decided} = {wr:.0f}%")
    print(f"  PnL (TP1 alone)    : ${stats['pnl_tp1']:+.2f}")
    print(f"  PnL (TP1+TP2 comb) : ${stats['pnl_combined']:+.2f}")
    print(f"  Wyniki             : {stats['results']}")
    if stats["by_type"]:
        print(f"\n  Per setup type:")
        for t, v in sorted(stats["by_type"].items()):
            twr = v["wins"] / (v["wins"] + v["losses"]) * 100 if (v["wins"] + v["losses"]) > 0 else 0
            avg1 = v["pnl_tp1"] / v["count"] if v["count"] > 0 else 0
            avg2 = v["pnl_combined"] / v["count"] if v["count"] > 0 else 0
            print(f"    {t:<34} count={v['count']:>3}  W={v['wins']}  L={v['losses']}  WR={twr:.0f}%  TP1=${v['pnl_tp1']:+.2f}  Comb=${v['pnl_combined']:+.2f}  avg1=${avg1:+.2f}  avg2=${avg2:+.2f}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GPT3 Validator Backtest — Algo2 vs Algo2+GPT3")
    parser.add_argument("--from", dest="dt_from", default="")
    parser.add_argument("--to",   dest="dt_to",   default="")
    parser.add_argument("--hours", type=int, default=48)
    args = parser.parse_args()

    now_ts = int(datetime.now(timezone.utc).timestamp())
    to_ts   = _parse_dt(args.dt_to)   if args.dt_to   else now_ts
    from_ts = _parse_dt(args.dt_from) if args.dt_from else to_ts - args.hours * 3600

    num_hours = (to_ts - from_ts) // 3600
    outcome_end = to_ts + 24 * 3600

    print(f"Okres: {_ts_fmt(from_ts)} - {_ts_fmt(to_ts)} ({num_hours}h)")
    print(f"GPT4o validator: {'AKTYWNY' if OPENAI_KEY else 'BRAK KLUCZA — tylko Algo2'}")

    print("Pobieranie M15...")
    m15_total = 100 + (outcome_end - from_ts) // 900 + 50
    all_m15 = fetch_klines_paginated("SOLUSDT", "15m", total=m15_total, end_ts_s=outcome_end)
    print(f"  Pobrano {len(all_m15)} swiec M15")

    print("Pobieranie H1...")
    h1_total = 50 + num_hours + 30
    all_h1 = fetch_klines_paginated("SOLUSDT", "1h", total=h1_total, end_ts_s=outcome_end)
    print(f"  Pobrano {len(all_h1)} swiec H1")

    test_hours = [from_ts + i * 3600 for i in range(num_hours)]
    algo2_stats = make_stats()
    gpt3_stats  = make_stats()
    last_setup_key = None

    sep = "-" * 120
    print()
    print(f"{'Czas':<18} {'Cena':>8} {'Rezim':<18} {'Setup':<30} {'W':>8} {'SL':>8} {'TP1':>8} {'RR':>5}  {'A2':^10}  {'GPT':^8}  {'G3':^10}")
    print(sep)

    for signal_ts in test_hours:
        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 30 or len(ctx_h1) < 10:
            continue

        price  = ctx_m15[-1]["close"]
        regime = detect_regime_new(ctx_m15, ctx_h1, price)
        regime_label = f"{regime['regime']}({regime['strength']})"

        setups = algo_detect_setups(regime, ctx_m15, ctx_h1, price)
        if not setups:
            continue

        best = max(setups, key=lambda s: s["rr"])

        setup_key = f"{best['type']}_{best['w']:.0f}_{best['sl']:.0f}"
        if setup_key == last_setup_key:
            continue
        last_setup_key = setup_key

        future = [c for c in all_m15 if c["time"] > signal_ts]
        outcome_a2 = evaluate_setup(best, future)
        record_outcome(algo2_stats, best["type"], outcome_a2)

        # GPT3 validator
        gpt_label = "—"
        g3_wynik  = "—"

        if OPENAI_KEY:
            rng = detect_range(ctx_h1)
            sup = rng["support"]; res = rng["resistance"]; rng_size = rng["range_size"]
            pct = max(0.0, min(100.0, (price - sup) / rng_size * 100)) if rng_size > 0 else 50.0
            atr = calc_atr(ctx_m15)

            val = call_validator(best, ctx_m15, ctx_h1, price,
                                 atr=atr, support=sup, resistance=res, price_pct_in_range=pct)
            if val is not None:
                approved = val.get("approve", True)
                reason   = val.get("reason", "")
                conf     = val.get("confidence", 0)
                if approved:
                    gpt_label = f"OK({conf})"
                    g3_wynik  = outcome_a2["wynik"]
                    record_outcome(gpt3_stats, best["type"], outcome_a2)
                    print(f"  [GPT3] APPROVE ({conf}%): {reason}")
                else:
                    gpt_label = f"REJ({conf})"
                    g3_wynik  = "filtered"
                    print(f"  [GPT3] REJECT  ({conf}%): {reason}")
            else:
                gpt_label = "ERR"
                g3_wynik  = outcome_a2["wynik"]
                record_outcome(gpt3_stats, best["type"], outcome_a2)
        else:
            record_outcome(gpt3_stats, best["type"], outcome_a2)
            g3_wynik = outcome_a2["wynik"]

        print(
            f"{_ts_fmt(signal_ts):<18} "
            f"${price:>7.2f} "
            f"{regime_label:<18} "
            f"{best['type']:<30} "
            f"${best['w']:>7.2f} "
            f"${best['sl']:>7.2f} "
            f"${best['tp1']:>7.2f} "
            f"{best['rr']:>4.1f}  "
            f"{outcome_a2['wynik']:^10}  "
            f"{gpt_label:^8}  "
            f"{g3_wynik:^10}"
        )

    # ── Podsumowania ─────────────────────────────────────────────────────────
    print_stats("ALGO2 (bez filtru GPT3)", algo2_stats)
    print_stats("ALGO2 + GPT3 FILTR", gpt3_stats)

    # Szybkie porownanie
    a_wr = algo2_stats["wins"] / (algo2_stats["wins"] + algo2_stats["losses"]) * 100 \
        if (algo2_stats["wins"] + algo2_stats["losses"]) > 0 else 0
    g_wr = gpt3_stats["wins"] / (gpt3_stats["wins"] + gpt3_stats["losses"]) * 100 \
        if (gpt3_stats["wins"] + gpt3_stats["losses"]) > 0 else 0
    delta_wr  = g_wr - a_wr
    delta_pnl_tp1  = gpt3_stats["pnl_tp1"]      - algo2_stats["pnl_tp1"]
    delta_pnl_comb = gpt3_stats["pnl_combined"] - algo2_stats["pnl_combined"]

    print(f"\n{'='*70}")
    print(f"POROWNANIE ALGO2 vs ALGO2+GPT3")
    print(f"{'='*70}")
    print(f"  {'Metryka':<26} {'Algo2 alone':>14} {'Algo2+GPT3':>14} {'Delta':>10}")
    print(f"  {'-'*64}")
    print(f"  {'Setupy':<26} {algo2_stats['setups']:>14} {gpt3_stats['setups']:>14} {gpt3_stats['setups']-algo2_stats['setups']:>+10}")
    print(f"  {'Wejscia':<26} {algo2_stats['entries']:>14} {gpt3_stats['entries']:>14} {gpt3_stats['entries']-algo2_stats['entries']:>+10}")
    print(f"  {'Win rate':<26} {a_wr:>13.0f}% {g_wr:>13.0f}% {delta_wr:>+9.0f}%")
    print(f"  {'PnL TP1 alone ($)':<26} ${algo2_stats['pnl_tp1']:>+12.2f} ${gpt3_stats['pnl_tp1']:>+12.2f} ${delta_pnl_tp1:>+9.2f}")
    print(f"  {'PnL TP1+TP2 combined ($)':<26} ${algo2_stats['pnl_combined']:>+12.2f} ${gpt3_stats['pnl_combined']:>+12.2f} ${delta_pnl_comb:>+9.2f}")


if __name__ == "__main__":
    main()
