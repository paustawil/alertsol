"""
Diagnostyka reżimu rynkowego — porównanie starego algorytmu z nową logiką IMPULSE/TREND/RANGE.

Pobiera dane historyczne z Bitget API i iteruje godzina po godzinie,
wypisując co stary i nowy algorytm wykrywają.

Uruchomienie:
    python diagnose_regime.py
    python diagnose_regime.py --from "2026-03-25 00:00" --to "2026-03-31 00:00"
"""

import argparse
from datetime import datetime, timezone

import requests


# ── Kopie funkcji z sol_alert.py (unikamy importu który ciągnie gspread/cryptography) ──

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

def detect_market_regime(candles_m15, candles_h1, current_price):
    """Stary algorytm — kopia z sol_alert.py."""
    if len(candles_h1) < 20:
        rng = detect_range(candles_h1, n=min(len(candles_h1), 32))
        ref_support, ref_resistance = rng["support"], rng["resistance"]
    else:
        ref_candles = candles_h1[-40:-8] if len(candles_h1) >= 40 else candles_h1[:-8]
        ref_resistance = max(c["high"] for c in ref_candles)
        ref_support = min(c["low"] for c in ref_candles)
    rng_size = ref_resistance - ref_support
    if rng_size <= 0:
        return {"regime": "CONSOLIDATION", "score": 0, "details": "brak zakresu"}

    recent_m15 = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent_m15[:-2]) / max(len(recent_m15[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent_m15[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    last_3 = [c["close"] for c in candles_m15[-3:]]
    closes_below = sum(1 for c in last_3 if c < ref_support)
    closes_above = sum(1 for c in last_3 if c > ref_resistance)
    pct_below = (ref_support - current_price) / ref_support * 100 if current_price < ref_support else 0
    pct_above = (current_price - ref_resistance) / ref_resistance * 100 if current_price > ref_resistance else 0

    h1_recent = candles_h1[-8:]
    lower_lows = sum(1 for i in range(1, len(h1_recent)) if h1_recent[i]["low"] < h1_recent[i-1]["low"])
    higher_highs = sum(1 for i in range(1, len(h1_recent)) if h1_recent[i]["high"] > h1_recent[i-1]["high"])

    recent_low = min(c["low"] for c in candles_h1[-8:])
    recent_high = max(c["high"] for c in candles_h1[-8:])
    shift_down = (ref_support - recent_low) / rng_size * 100 if recent_low < ref_support else 0
    shift_up = (recent_high - ref_resistance) / rng_size * 100 if recent_high > ref_resistance else 0

    p24 = candles_h1[-24]["close"] if len(candles_h1) >= 24 else candles_h1[0]["close"]
    p48 = candles_h1[-48]["close"] if len(candles_h1) >= 48 else candles_h1[0]["close"]
    c24 = (current_price - p24) / p24 * 100
    c48 = (current_price - p48) / p48 * 100

    bd = 0
    if current_price < ref_support: bd += 1
    if closes_below >= 2: bd += 1
    if pct_below >= 1.5: bd += 1
    if vol_ratio >= 1.5 and current_price < ref_support: bd += 1
    if lower_lows >= 4: bd += 1
    if shift_down >= 30: bd += 1
    if c24 <= -3.0: bd += 2
    elif c24 <= -1.5: bd += 1
    if c48 <= -5.0: bd += 2
    elif c48 <= -3.0: bd += 1

    bu = 0
    if current_price > ref_resistance: bu += 1
    if closes_above >= 2: bu += 1
    if pct_above >= 1.5: bu += 1
    if vol_ratio >= 1.5 and current_price > ref_resistance: bu += 1
    if higher_highs >= 4: bu += 1
    if shift_up >= 30: bu += 1
    if c24 >= 3.0: bu += 2
    elif c24 >= 1.5: bu += 1
    if c48 >= 5.0: bu += 2
    elif c48 >= 3.0: bu += 1

    if bd >= 2 and bd > bu:
        return {"regime": "BREAKOUT_DOWN", "score": bd}
    elif bu >= 2 and bu > bd:
        return {"regime": "BREAKOUT_UP", "score": bu}
    return {"regime": "CONSOLIDATION", "score": 0}


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _parse_dt(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def fetch_klines_paginated(symbol: str, interval: str, total: int, end_ts_s: int | None = None) -> list[dict]:
    """Pobiera historyczne świece z Bitget API (kopia z grok2_backtest.py)."""
    granularity = {"15m": "15m", "1h": "1H"}[interval]
    interval_s = {"15m": 900, "1h": 3600}[interval]
    result: list[dict] = []
    end_ms = (end_ts_s * 1000) if end_ts_s else None

    while len(result) < total:
        params: dict = {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "granularity": granularity, "limit": str(min(total - len(result), 200)),
        }
        if end_ms:
            params["endTime"] = str(end_ms)
        try:
            r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params=params, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            print(f"[fetch] Błąd API: {e}")
            break
        if not data:
            break
        batch = [{"time": int(d[0]) // 1000, "open": float(d[1]), "high": float(d[2]),
                  "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])} for d in data]
        batch.sort(key=lambda c: c["time"])
        result = batch + result
        end_ms = batch[0]["time"] * 1000 - interval_s * 1000
        if len(batch) < 2:
            break

    seen: set[int] = set()
    deduped = [c for c in result if c["time"] not in seen and not seen.add(c["time"])]
    deduped.sort(key=lambda c: c["time"])
    return deduped[-total:] if len(deduped) > total else deduped


# ── Nowa logika IMPULSE / TREND / RANGE (prototyp do porównania) ────────────

def detect_regime_new(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict:
    """Nowy 3-stanowy detektor reżimu: IMPULSE / TREND / RANGE."""

    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    # Volume ratio (ostatnie 2 M15 vs średnia z 10)
    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    # Zmiana 4h (ostatnie 16 M15)
    price_4h_ago = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    change_4h = (current_price - price_4h_ago) / price_4h_ago * 100

    # Zmiana 24h / 48h
    price_24h = candles_h1[-24]["close"] if len(candles_h1) >= 24 else candles_h1[0]["close"]
    price_48h = candles_h1[-48]["close"] if len(candles_h1) >= 48 else candles_h1[0]["close"]
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    # Kierunek ostatnich 4 M15
    last4 = candles_m15[-4:]
    bearish_closes = sum(1 for c in last4 if c["close"] < c["open"])
    bullish_closes = sum(1 for c in last4 if c["close"] > c["open"])

    # Struktura H1: lower lows / higher highs (last 12h)
    h1_12 = candles_h1[-12:] if len(candles_h1) >= 12 else candles_h1
    h1_lows = [c["low"] for c in h1_12]
    h1_highs = [c["high"] for c in h1_12]
    lower_lows = sum(1 for i in range(1, len(h1_lows)) if h1_lows[i] < h1_lows[i - 1])
    higher_highs = sum(1 for i in range(1, len(h1_highs)) if h1_highs[i] > h1_highs[i - 1])
    lower_highs = sum(1 for i in range(1, len(h1_highs)) if h1_highs[i] < h1_highs[i - 1])
    higher_lows = sum(1 for i in range(1, len(h1_lows)) if h1_lows[i] > h1_lows[i - 1])

    # ── IMPULSE: gwałtowny ruch w ostatnich godzinach ────────────────────────
    impulse_score = 0
    impulse_dir = "none"

    if imp_str >= 2:
        impulse_score += 1
    if vol_ratio >= 1.5:
        impulse_score += 1
    if abs(change_4h) >= 2.0:
        impulse_score += 1
    elif abs(change_4h) >= 1.5 and vol_ratio >= 1.3:
        impulse_score += 1

    if bearish_closes >= 3:
        impulse_score += 1
        impulse_dir = "down"
    elif bullish_closes >= 3:
        impulse_score += 1
        impulse_dir = "up"

    if impulse_score >= 3:
        if impulse_dir == "none":
            impulse_dir = "down" if change_4h < 0 else "up"
        strength = min(10, impulse_score * 2 + imp_str)
        regime = f"IMPULSE_{impulse_dir.upper()}"
        details = [f"4h:{change_4h:+.1f}%", f"imp:{imp_str}", f"vol:{vol_ratio:.1f}x",
                   f"dir:{bearish_closes}B/{bullish_closes}G"]
        return {
            "regime": regime, "direction": impulse_dir, "strength": strength,
            "change_4h": round(change_4h, 1), "change_24h": round(change_24h, 1),
            "change_48h": round(change_48h, 1),
            "impulse": imp_str, "h1_trend": trend, "vol_ratio": round(vol_ratio, 1),
            "details": "; ".join(details),
        }

    # ── TREND: utrzymujący się ruch kierunkowy ────────────────────────────────
    trend_score = 0
    trend_details = []

    if abs(change_24h) >= 3.0:
        trend_score += 2
        trend_details.append(f"24h:{change_24h:+.1f}%")
    elif abs(change_24h) >= 1.5:
        trend_score += 1
        trend_details.append(f"24h:{change_24h:+.1f}%")

    if abs(change_48h) >= 5.0:
        trend_score += 2
        trend_details.append(f"48h:{change_48h:+.1f}%")
    elif abs(change_48h) >= 3.0:
        trend_score += 1
        trend_details.append(f"48h:{change_48h:+.1f}%")

    if lower_lows >= 5:
        trend_score += 1
        trend_details.append(f"LL:{lower_lows}/{len(h1_12)-1}")
    if higher_highs >= 5:
        trend_score += 1
        trend_details.append(f"HH:{higher_highs}/{len(h1_12)-1}")
    if lower_highs >= 5:
        trend_score += 1
        trend_details.append(f"LH:{lower_highs}/{len(h1_12)-1}")
    if higher_lows >= 5:
        trend_score += 1
        trend_details.append(f"HL:{higher_lows}/{len(h1_12)-1}")

    if trend != "neutral":
        trend_score += 1
        trend_details.append(f"h1:{trend}")

    # TREND wymaga zmiany cenowej — sama struktura nie wystarczy
    has_price_change = abs(change_24h) >= 1.5 or abs(change_48h) >= 3.0

    if trend_score >= 3 and has_price_change:
        # Kierunek: 48h ma priorytet nad 24h gdy się kłócą
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        elif change_48h < -2.0:
            trend_dir = "down"
        elif change_48h > 2.0:
            trend_dir = "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"

        strength = min(10, trend_score + imp_str)
        regime = f"TREND_{trend_dir.upper()}"
        return {
            "regime": regime, "direction": trend_dir, "strength": strength,
            "change_4h": round(change_4h, 1), "change_24h": round(change_24h, 1),
            "change_48h": round(change_48h, 1),
            "impulse": imp_str, "h1_trend": trend, "vol_ratio": round(vol_ratio, 1),
            "details": "; ".join(trend_details),
        }

    # ── RANGE: domyślny ──────────────────────────────────────────────────────
    range_details = [f"24h:{change_24h:+.1f}%", f"48h:{change_48h:+.1f}%"]
    return {
        "regime": "RANGE", "direction": "none", "strength": 0,
        "change_4h": round(change_4h, 1), "change_24h": round(change_24h, 1),
        "change_48h": round(change_48h, 1),
        "impulse": imp_str, "h1_trend": trend, "vol_ratio": round(vol_ratio, 1),
        "details": "; ".join(range_details),
    }


# ── Algorytmiczne setupy per reżim ──────────────────────────────────────────

def find_swing_points(candles_h1: list[dict], n: int = 12):
    """Znajduje swing high i swing low z ostatnich n świec H1."""
    recent = candles_h1[-n:]
    swing_high = max(c["high"] for c in recent)
    swing_low = min(c["low"] for c in recent)
    return swing_high, swing_low


def find_consolidation(candles_h1: list[dict], min_candles: int = 4, max_candles: int = 10):
    """Szuka konsolidacji przy dnie/szczycie — wąski zakres w ostatnich świecach."""
    for n in range(min_candles, min(max_candles + 1, len(candles_h1))):
        recent = candles_h1[-n:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        # ATR z szerszego kontekstu
        atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
        if atr > 0 and rng < atr * 2.5:  # zakres < 2.5 ATR = konsolidacja
            return {"high": hi, "low": lo, "range": rng, "candles": n}
    return None


def find_broken_support(candles_h1: list[dict], current_price: float):
    """Szuka wybitego supportu powyżej aktualnej ceny (teraz resistance)."""
    # Szukaj w starszych świecach (8-30h temu) dołków które były wielokrotnie dotykane
    older = candles_h1[-30:-6] if len(candles_h1) >= 30 else candles_h1[:-6]
    if len(older) < 5:
        return None

    # Znajdź lokalne dołki w starszych świecach
    lows = [c["low"] for c in older]
    support_levels = []
    for i in range(1, len(lows) - 1):
        if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]:
            support_levels.append(lows[i])

    # Szukaj poziomu który jest POWYŻEJ aktualnej ceny (został wybity)
    for level in sorted(support_levels, reverse=True):
        if level > current_price * 1.005 and level < current_price * 1.06:
            # Poziom 0.5-6% powyżej ceny = dobry retest target
            return level
    return None


def find_broken_resistance(candles_h1: list[dict], current_price: float):
    """Szuka wybitej resistance poniżej aktualnej ceny (teraz support)."""
    older = candles_h1[-30:-6] if len(candles_h1) >= 30 else candles_h1[:-6]
    if len(older) < 5:
        return None
    highs = [c["high"] for c in older]
    resistance_levels = []
    for i in range(1, len(highs) - 1):
        if highs[i] >= highs[i-1] and highs[i] >= highs[i+1]:
            resistance_levels.append(highs[i])
    for level in sorted(resistance_levels):
        if level < current_price * 0.995 and level > current_price * 0.94:
            return level
    return None


def algo_detect_setups(regime: dict, candles_m15: list[dict], candles_h1: list[dict],
                       current_price: float) -> list[dict]:
    """Algorytmicznie wykrywa setupy na podstawie reżimu. Zwraca listę setupów."""
    regime_name = regime["regime"]
    direction = regime.get("direction", "none")
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    setups = []

    if atr <= 0:
        return setups

    # ── TREND_DOWN / IMPULSE_DOWN ─────────────────────────────────────────
    if direction == "down":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)

        # 1. trend_retest_short — retest wybitego supportu
        broken_sup = find_broken_support(candles_h1, current_price)
        if broken_sup:
            w = broken_sup - atr * 0.2  # lekko poniżej poziomu (strefa)
            sl = broken_sup + atr * 1.0  # 1 ATR powyżej
            tp1 = swing_low
            tp2 = swing_low - (broken_sup - swing_low) * 0.5
            if sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5:
                setups.append({
                    "type": "trend_retest_short", "direction": "short",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((w - tp1) / (sl - w), 1),
                })

        # 2. trend_consolidation_short — konsolidacja przy dnie
        consol = find_consolidation(candles_h1)
        if consol:
            w = consol["high"] - consol["range"] * 0.2  # górna 1/3
            sl = consol["high"] + atr * 1.0  # 1 ATR powyżej szczytu
            tp1 = consol["low"] - consol["range"]  # zakres poniżej dna
            tp2 = consol["low"] - consol["range"] * 1.5
            if sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5:
                setups.append({
                    "type": "trend_consolidation_short", "direction": "short",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((w - tp1) / (sl - w), 1),
                })

        # 3. trend_pullback_short — 38-50% korekty
        if swing_high > swing_low:
            swing_range = swing_high - swing_low
            fib38 = swing_low + swing_range * 0.38
            fib50 = swing_low + swing_range * 0.50
            fib618 = swing_low + swing_range * 0.618
            w = round((fib38 + fib50) / 2, 2)  # środek strefy 38-50%
            sl = round(fib618 + atr * 0.3, 2)  # powyżej 61.8% + bufor
            tp1 = round(swing_low, 2)
            tp2 = round(swing_low - swing_range * 0.3, 2)
            if sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5 and w > current_price * 1.003:
                setups.append({
                    "type": "trend_pullback_short", "direction": "short",
                    "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "rr": round((w - tp1) / (sl - w), 1),
                })

        # 4. impulse_continuation_short — mini-pullback w impulsie (tylko IMPULSE)
        if regime_name.startswith("IMPULSE_"):
            last6 = candles_m15[-6:]
            greens = [c for c in last6 if c["close"] > c["open"]]
            if len(greens) >= 1 and len(greens) <= 2:
                pullback_high = max(c["high"] for c in last6[-2:])
                w = round(pullback_high, 2)
                sl = round(pullback_high + atr * 0.8, 2)
                tp1 = round(swing_low, 2)
                tp2 = round(swing_low - atr, 2)
                if sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5:
                    setups.append({
                        "type": "impulse_continuation_short", "direction": "short",
                        "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                        "rr": round((w - tp1) / (sl - w), 1),
                    })

    # ── TREND_UP / IMPULSE_UP ─────────────────────────────────────────────
    elif direction == "up":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)

        # 1. trend_retest_long
        broken_res = find_broken_resistance(candles_h1, current_price)
        if broken_res:
            w = broken_res + atr * 0.2
            sl = broken_res - atr * 1.0
            tp1 = swing_high
            tp2 = swing_high + (swing_high - broken_res) * 0.5
            if sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5:
                setups.append({
                    "type": "trend_retest_long", "direction": "long",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

        # 2. trend_consolidation_long
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

        # 3. trend_pullback_long
        if swing_high > swing_low:
            swing_range = swing_high - swing_low
            fib38 = swing_high - swing_range * 0.38
            fib50 = swing_high - swing_range * 0.50
            fib618 = swing_high - swing_range * 0.618
            w = round((fib38 + fib50) / 2, 2)
            sl = round(fib618 - atr * 0.3, 2)
            tp1 = round(swing_high, 2)
            tp2 = round(swing_high + swing_range * 0.3, 2)
            if sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5 and w < current_price * 0.997:
                setups.append({
                    "type": "trend_pullback_long", "direction": "long",
                    "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

    # ── RANGE ─────────────────────────────────────────────────────────────
    elif regime_name == "RANGE":
        rng = detect_range(candles_h1)
        sup, res = rng["support"], rng["resistance"]
        rng_size = res - sup
        if rng_size > atr * 1.5:
            # range_resistance_short
            w = res - rng_size * 0.1
            sl = res + atr * 1.0
            tp1 = sup + rng_size * 0.5  # środek range
            tp2 = sup + rng_size * 0.1  # blisko supportu
            if (w - tp1) / (sl - w) >= 1.5:
                setups.append({
                    "type": "range_resistance_short", "direction": "short",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((w - tp1) / (sl - w), 1),
                })
            # range_support_long
            w = sup + rng_size * 0.1
            sl = sup - atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = res - rng_size * 0.1
            if (tp1 - w) / (w - sl) >= 1.5:
                setups.append({
                    "type": "range_support_long", "direction": "long",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

    return setups


def evaluate_setup(setup: dict, future_m15: list[dict], entry_window_h: int = 24) -> dict:
    """Ewaluuje setup na przyszłych danych M15. Sprawdza czy entry, TP1, TP2, SL zostały trafione."""
    w = setup["w"]
    sl = setup["sl"]
    tp1 = setup["tp1"]
    tp2 = setup["tp2"]
    direction = setup["direction"]
    entry_window_s = entry_window_h * 3600

    # Szukaj entry
    entry_ts = None
    for c in future_m15:
        if entry_ts is None and c["time"] <= future_m15[0]["time"] + entry_window_s:
            if direction == "short" and c["high"] >= w:
                entry_ts = c["time"]
            elif direction == "long" and c["low"] <= w:
                entry_ts = c["time"]

    if entry_ts is None:
        return {"wynik": "no_entry", "pnl_tp1": 0, "pnl_tp2": 0}

    # Po entry — sprawdź co trafione pierwsze
    tp1_hit = False
    tp2_hit = False
    sl_hit = False

    for c in future_m15:
        if c["time"] < entry_ts:
            continue

        if direction == "short":
            if c["low"] <= tp1:
                tp1_hit = True
            if c["low"] <= tp2:
                tp2_hit = True
            if c["high"] >= sl:
                sl_hit = True
        else:  # long
            if c["high"] >= tp1:
                tp1_hit = True
            if c["high"] >= tp2:
                tp2_hit = True
            if c["low"] <= sl:
                sl_hit = True

        # Sprawdź kolejność — kto pierwszy
        if tp1_hit and not sl_hit:
            if tp2_hit:
                pnl = abs(w - tp2) if direction == "short" else abs(tp2 - w)
                return {"wynik": "TP1+TP2", "pnl_tp1": abs(w - tp1), "pnl_tp2": pnl}
            # Czekaj dalej na tp2 lub sl
        if sl_hit and not tp1_hit:
            pnl = -abs(sl - w)
            return {"wynik": "SL", "pnl_tp1": pnl, "pnl_tp2": pnl}
        if sl_hit and tp1_hit:
            # TP1 trafione, potem SL — TP1+BE (break even na drugiej połowie)
            return {"wynik": "TP1+BE", "pnl_tp1": abs(w - tp1), "pnl_tp2": 0}

    # Nic nie trafione w dostępnych danych
    return {"wynik": "open", "pnl_tp1": 0, "pnl_tp2": 0}


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Diagnostyka reżimu + algorytmiczne setupy")
    parser.add_argument("--from", dest="dt_from", default="2026-03-25 00:00")
    parser.add_argument("--to", dest="dt_to", default="2026-03-31 00:00")
    args = parser.parse_args()

    from_ts = _parse_dt(args.dt_from)
    to_ts = _parse_dt(args.dt_to)
    num_hours = (to_ts - from_ts) // 3600

    print(f"Okres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)} ({num_hours}h)")

    # Potrzebujemy dane outcome 24h po to_ts
    outcome_end = to_ts + 24 * 3600

    print("Pobieranie M15...")
    m15_total = 100 + (outcome_end - from_ts) // 900 + 50
    all_m15 = fetch_klines_paginated("SOLUSDT", "15m", total=m15_total, end_ts_s=outcome_end)
    print(f"  Pobrano {len(all_m15)} świec M15")

    print("Pobieranie H1...")
    h1_total = 50 + num_hours + 30
    all_h1 = fetch_klines_paginated("SOLUSDT", "1h", total=h1_total, end_ts_s=outcome_end)
    print(f"  Pobrano {len(all_h1)} świec H1")

    test_hours = [from_ts + i * 3600 for i in range(num_hours)]

    # Statystyki
    total_setups = 0
    results = {"TP1+TP2": 0, "TP1+BE": 0, "SL": 0, "no_entry": 0, "open": 0}
    pnl_sum = 0.0
    by_type = {}
    last_setup_type = None  # deduplikacja — nie powtarzaj tego samego setupu

    print()
    print(f"{'Czas':<18} {'Cena':>8} {'Reżim':<18} {'Setup':<28} {'W':>8} {'SL':>8} {'TP1':>8} {'TP2':>8} {'RR':>5} {'Wynik':<10} {'PnL':>7}")
    print("-" * 160)

    for signal_ts in test_hours:
        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1 = [c for c in all_h1 if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 30 or len(ctx_h1) < 10:
            continue

        price = ctx_m15[-1]["close"]
        regime = detect_regime_new(ctx_m15, ctx_h1, price)
        regime_label = f"{regime['regime']}({regime['strength']})"

        setups = algo_detect_setups(regime, ctx_m15, ctx_h1, price)

        if not setups:
            print(f"{_ts_fmt(signal_ts):<18} ${price:>7.2f} {regime_label:<18} {'— brak setupu —':<28}")
            continue

        # Wybierz najlepszy setup (najwyższy RR)
        best = max(setups, key=lambda s: s["rr"])

        # Deduplikacja: nie powtarzaj tego samego setupu z tymi samymi poziomami
        setup_key = f"{best['type']}_{best['w']:.0f}_{best['sl']:.0f}"
        if setup_key == last_setup_type:
            continue
        last_setup_type = setup_key

        # Ewaluacja
        future = [c for c in all_m15 if c["time"] > signal_ts]
        outcome = evaluate_setup(best, future)

        total_setups += 1
        results[outcome["wynik"]] += 1
        pnl = outcome["pnl_tp1"]
        pnl_sum += pnl

        # Stats per type
        t = best["type"]
        if t not in by_type:
            by_type[t] = {"count": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        by_type[t]["count"] += 1
        if outcome["wynik"] in ("TP1+TP2", "TP1+BE"):
            by_type[t]["wins"] += 1
        elif outcome["wynik"] == "SL":
            by_type[t]["losses"] += 1
        by_type[t]["pnl"] += pnl

        print(
            f"{_ts_fmt(signal_ts):<18} "
            f"${price:>7.2f} "
            f"{regime_label:<18} "
            f"{best['type']:<28} "
            f"${best['w']:>7.2f} "
            f"${best['sl']:>7.2f} "
            f"${best['tp1']:>7.2f} "
            f"${best['tp2']:>7.2f} "
            f"{best['rr']:>4.1f} "
            f"{outcome['wynik']:<10} "
            f"${pnl:>+6.2f}"
        )

    # Podsumowanie
    print("\n" + "=" * 80)
    print("PODSUMOWANIE ALGO")
    print("=" * 80)
    print(f"Setupy: {total_setups}")
    wins = results["TP1+TP2"] + results["TP1+BE"]
    losses = results["SL"]
    print(f"Wyniki: {results}")
    if wins + losses > 0:
        print(f"Win rate: {wins}/{wins+losses} = {wins/(wins+losses)*100:.0f}%")
    print(f"PnL suma: ${pnl_sum:+.2f}")

    print(f"\nPer setup type:")
    for t, s in sorted(by_type.items()):
        wr = s["wins"] / (s["wins"] + s["losses"]) * 100 if s["wins"] + s["losses"] > 0 else 0
        print(f"  {t:<30} count={s['count']:>3}  wins={s['wins']}  losses={s['losses']}  WR={wr:.0f}%  PnL=${s['pnl']:+.2f}")


if __name__ == "__main__":
    main()
