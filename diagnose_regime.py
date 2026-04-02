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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Diagnostyka reżimu rynkowego")
    parser.add_argument("--from", dest="dt_from", default="2026-03-25 00:00")
    parser.add_argument("--to", dest="dt_to", default="2026-03-31 00:00")
    args = parser.parse_args()

    from_ts = _parse_dt(args.dt_from)
    to_ts = _parse_dt(args.dt_to)
    num_hours = (to_ts - from_ts) // 3600

    print(f"Okres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)} ({num_hours}h)")

    # Pobierz dane — potrzebujemy kontekst 50 H1 + 100 M15 przed from_ts
    print("Pobieranie M15...")
    m15_total = 100 + num_hours * 4 + 50
    all_m15 = fetch_klines_paginated("SOLUSDT", "15m", total=m15_total, end_ts_s=to_ts)
    print(f"  Pobrano {len(all_m15)} świec M15")

    print("Pobieranie H1...")
    h1_total = 50 + num_hours + 10
    all_h1 = fetch_klines_paginated("SOLUSDT", "1h", total=h1_total, end_ts_s=to_ts)
    print(f"  Pobrano {len(all_h1)} świec H1")

    # Nagłówek tabeli
    print()
    hdr = (f"{'Czas':<18} {'Cena':>8} {'STARY reżim':<22} {'NOWY reżim':<18} "
           f"{'24h%':>7} {'48h%':>7} {'Imp':>3} {'Vol':>5} {'Detale nowy'}")
    print(hdr)
    print("-" * 140)

    test_hours = [from_ts + i * 3600 for i in range(num_hours)]

    for signal_ts in test_hours:
        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1 = [c for c in all_h1 if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 30 or len(ctx_h1) < 10:
            print(f"{_ts_fmt(signal_ts):<18} {'---':>8} {'brak danych':<22}")
            continue

        price = ctx_m15[-1]["close"]

        # Stary algorytm
        old = detect_market_regime(ctx_m15, ctx_h1, price)
        old_label = f"{old['regime']}({old.get('score', 0)})"

        # Nowy algorytm
        new = detect_regime_new(ctx_m15, ctx_h1, price)
        new_label = f"{new['regime']}({new['strength']})"

        print(
            f"{_ts_fmt(signal_ts):<18} "
            f"${price:>7.2f} "
            f"{old_label:<22} "
            f"{new_label:<18} "
            f"{new['change_24h']:>+6.1f}% "
            f"{new['change_48h']:>+6.1f}% "
            f"{new['impulse']:>3} "
            f"{new['vol_ratio']:>4.1f}x "
            f"{new['details']}"
        )


if __name__ == "__main__":
    main()
