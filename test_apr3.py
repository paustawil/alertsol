#!/usr/bin/env python3
"""
test_apr3.py — Porównanie starego vs nowego algorytmu detekcji reżimu.

Uruchomienie:
    python test_apr3.py
    python test_apr3.py --from "2026-04-01 00:00" --to "2026-04-04 00:00"
"""

import argparse
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

# ── API fetch ─────────────────────────────────────────────────────────────────

def fetch_klines_okx(symbol: str, interval: str, total: int, end_ts_s: int) -> list[dict]:
    """Pobiera historyczne świece z OKX API."""
    okx_bar = {"15m": "15m", "1h": "1H"}[interval]
    interval_s = {"15m": 900, "1h": 3600}[interval]
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
    # odfiltruj świece poza zakresem
    result = [c for c in result if c["time"] < end_ts_s]
    return result[-total:] if len(result) > total else result

# ── STARY algorytm detekcji reżimu ───────────────────────────────────────────

def detect_regime_old(candles_m15, candles_h1, current_price):
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_4h = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    price_24h = candles_h1[-24]["close"] if len(candles_h1) >= 24 else candles_h1[0]["close"]
    price_48h = candles_h1[-48]["close"] if len(candles_h1) >= 48 else candles_h1[0]["close"]
    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    last4 = candles_m15[-4:]
    bearish = sum(1 for c in last4 if c["close"] < c["open"])
    bullish = sum(1 for c in last4 if c["close"] > c["open"])

    h1_12 = candles_h1[-12:]
    lower_lows  = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
    higher_highs= sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

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
        return f"IMPULSE_{impulse_dir.upper()}", change_24h, change_48h

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
        return f"TREND_{trend_dir.upper()}", change_24h, change_48h

    return "RANGE", change_24h, change_48h


# ── NOWY algorytm detekcji reżimu (z poprawkami) ─────────────────────────────

def detect_regime_new(candles_m15, candles_h1, current_price):
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_4h = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    price_24h = candles_h1[-24]["close"] if len(candles_h1) >= 24 else candles_h1[0]["close"]
    price_48h = candles_h1[-48]["close"] if len(candles_h1) >= 48 else candles_h1[0]["close"]
    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    last4 = candles_m15[-4:]
    bearish = sum(1 for c in last4 if c["close"] < c["open"])
    bullish = sum(1 for c in last4 if c["close"] > c["open"])

    h1_12 = candles_h1[-12:]
    lower_lows  = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
    higher_highs= sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

    # IMPULSE (bez zmian)
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
        return f"IMPULSE_{impulse_dir.upper()}", change_24h, change_48h

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

    # NOWOŚĆ: blokada konfliktu 24h vs 48h
    signals_conflict = (
        abs(change_24h) >= 1.5
        and abs(change_48h) >= 1.5
        and change_24h * change_48h < 0
    )
    conflict_note = ""
    if signals_conflict:
        trend_score -= 2
        conflict_note = f" [KONFLIKT: 24h={change_24h:+.1f}% vs 48h={change_48h:+.1f}%]"

    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"
        return f"TREND_{trend_dir.upper()}{conflict_note}", change_24h, change_48h

    return f"RANGE{conflict_note}", change_24h, change_48h


# ── find_consolidation STARY vs NOWY ─────────────────────────────────────────

def find_consolidation_old(candles_h1):
    for n in range(4, min(11, len(candles_h1))):
        recent = candles_h1[-n:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        atr = calc_atr(candles_h1[-20:])
        if atr > 0 and rng < atr * 2.5:
            return {"high": hi, "low": lo, "range": rng, "candles": n}
    return None

def find_consolidation_new(candles_h1):
    atr = calc_atr(candles_h1[-20:])
    if atr <= 0:
        return None
    for n in range(min(10, len(candles_h1) - 1), 3, -1):
        recent = candles_h1[-n:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        if rng < atr * 2.5:
            return {"high": hi, "low": lo, "range": rng, "candles": n}
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="dt_from", default="2026-04-03 06:00")
    parser.add_argument("--to",   dest="dt_to",   default="2026-04-03 21:00")
    args = parser.parse_args()

    from_ts = _parse_dt(args.dt_from)
    to_ts   = _parse_dt(args.dt_to)
    num_hours = (to_ts - from_ts) // 3600

    print(f"Pobieranie danych: {args.dt_from} – {args.dt_to} ({num_hours}h)...")

    m15_needed = 100 + (to_ts - from_ts) // 900
    h1_needed  = 60  + num_hours

    m15 = fetch_klines_okx("SOLUSDT", "15m", m15_needed, to_ts)
    h1  = fetch_klines_okx("SOLUSDT", "1h",  h1_needed,  to_ts)

    if not m15 or not h1:
        print("Brak danych — sprawdź połączenie z API")
        return

    print(f"M15: {len(m15)} świec | H1: {len(h1)} świec")
    print()

    test_hours = list(range(from_ts, to_ts, 3600))

    print(f"{'Godz':>8}  {'Cena':>7}  {'STARY':>18}  {'NOWY':>18}  {'24h':>6}  {'48h':>6}  {'Konsolidacja STARA':>22}  {'Konsolidacja NOWA':>22}")
    print("-" * 140)

    changed_hours = 0
    for ts in test_hours:
        # Dane dostępne do ts
        m15_snap = [c for c in m15 if c["time"] < ts]
        h1_snap  = [c for c in h1  if c["time"] < ts]

        if len(m15_snap) < 20 or len(h1_snap) < 12:
            continue

        price = m15_snap[-1]["close"]
        dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")

        old_regime, c24, c48 = detect_regime_old(m15_snap, h1_snap, price)
        new_regime, _, _     = detect_regime_new(m15_snap, h1_snap, price)

        # find_consolidation porównanie
        co = find_consolidation_old(h1_snap)
        cn = find_consolidation_new(h1_snap)

        co_str = f"H={co['high']:.2f} n={co['candles']}" if co else "brak"
        cn_str = f"H={cn['high']:.2f} n={cn['candles']}" if cn else "brak"

        changed = " ◄" if old_regime.split()[0] != new_regime.split()[0] else ""
        changed_hours += 1 if changed else 0

        print(f"{dt_str:>8}  {price:>7.2f}  {old_regime:>18}  {new_regime:>18}  {c24:>+6.1f}%  {c48:>+6.1f}%  {co_str:>22}  {cn_str:>22}{changed}")

    print()
    print(f"Godziny gdzie reżim się zmienił: {changed_hours}/{len(test_hours)}")


if __name__ == "__main__":
    main()
