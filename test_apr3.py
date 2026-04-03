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
        return f"IMPULSE_{impulse_dir.upper()}", change_24h, change_48h, 0.0

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
        return f"TREND_{trend_dir.upper()}", change_24h, change_48h, 0.0

    return "RANGE", change_24h, change_48h, 0.0


# ── NOWY algorytm detekcji reżimu (z poprawkami) ─────────────────────────────

def detect_regime_new(candles_m15, candles_h1, current_price):
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    recent = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent[:-2]) / max(len(recent[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_4h  = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    price_24h = candles_h1[-24]["close"]  if len(candles_h1) >= 24  else candles_h1[0]["close"]
    price_48h = candles_h1[-48]["close"]  if len(candles_h1) >= 48  else candles_h1[0]["close"]
    price_7d  = candles_h1[-168]["close"] if len(candles_h1) >= 168 else candles_h1[0]["close"]
    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100
    change_7d  = (current_price - price_7d)  / price_7d  * 100

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
        return f"IMPULSE_{impulse_dir.upper()}", change_24h, change_48h, change_7d

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

    signals_conflict = (
        abs(change_24h) >= 1.5
        and abs(change_48h) >= 1.5
        and change_24h * change_48h < 0
    )
    if signals_conflict:
        trend_score -= 2

    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"

        # Blokada TREND_UP gdy kontekst 7d jest silnie niedźwiedzi
        if trend_dir == "up" and change_7d < -5.0:
            trend_dir = "down"

        return f"TREND_{trend_dir.upper()}", change_24h, change_48h, change_7d

    return f"RANGE", change_24h, change_48h, change_7d


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


# ── Generowanie setupu trend_consolidation_short (stary vs nowy) ─────────────

def make_consol_short(consol, h1_snap, swing_high, price, max_dist):
    """Generuje parametry trend_consolidation_short. Zwraca dict lub None."""
    if consol is None:
        return None
    # Nowy warunek: odrzuć jeśli konsolidacja nie sięga blisko swing_high
    if consol["high"] < swing_high * 0.97:
        return None
    atr = calc_atr(h1_snap[-20:])
    w   = consol["high"] - consol["range"] * 0.2
    sl  = consol["high"] + atr * 1.0
    tp1 = consol["low"]  - consol["range"]
    tp2 = consol["low"]  - consol["range"] * 1.5
    if not (sl > w and tp1 < w):
        return None
    rr = (w - tp1) / (sl - w)
    if rr < 1.5:
        return None
    if abs(w - price) > max_dist:
        return None
    return {"w": round(w, 2), "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "rr": round(rr, 1)}

def make_consol_short_old(consol, h1_snap, price, max_dist):
    """Stara wersja — bez warunku swing_high."""
    if consol is None:
        return None
    atr = calc_atr(h1_snap[-20:])
    w   = consol["high"] - consol["range"] * 0.2
    sl  = consol["high"] + atr * 1.0
    tp1 = consol["low"]  - consol["range"]
    tp2 = consol["low"]  - consol["range"] * 1.5
    if not (sl > w and tp1 < w):
        return None
    rr = (w - tp1) / (sl - w)
    if rr < 1.5:
        return None
    if abs(w - price) > max_dist:
        return None
    return {"w": round(w, 2), "sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "rr": round(rr, 1)}


# ── Ewaluacja setupu na przyszłych świecach M15 ───────────────────────────────

def evaluate(setup: dict, future_m15: list[dict], window_h: int = 24) -> str:
    """Sprawdza co trafione pierwsze: TP1, SL, brak wejścia."""
    if not setup or not future_m15:
        return "-"
    w, sl, tp1 = setup["w"], setup["sl"], setup["tp1"]
    window_s = window_h * 3600
    t0 = future_m15[0]["time"]

    entry_ts = None
    for c in future_m15:
        if c["time"] > t0 + window_s:
            break
        if c["high"] >= w:  # short — entry gdy cena dotyka W od dołu
            entry_ts = c["time"]
            break

    if entry_ts is None:
        return "no_entry"

    for c in future_m15:
        if c["time"] < entry_ts:
            continue
        if c["low"] <= tp1:
            return "TP1 ✓"
        if c["high"] >= sl:
            return "SL ✗"
    return "open"


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

    # Pobieramy dane + 24h do przodu na ewaluację
    fetch_end = to_ts + 24 * 3600
    m15_needed = 100 + (fetch_end - from_ts) // 900
    h1_needed  = 60  + (fetch_end - from_ts) // 3600

    print(f"Pobieranie danych: {args.dt_from} – {args.dt_to} (+24h ewaluacja)...")
    m15_all = fetch_klines_okx("SOLUSDT", "15m", m15_needed, fetch_end)
    h1_all  = fetch_klines_okx("SOLUSDT", "1h",  h1_needed,  fetch_end)

    if not m15_all or not h1_all:
        print("Brak danych — sprawdź połączenie z API")
        return

    print(f"M15: {len(m15_all)} świec | H1: {len(h1_all)} świec")
    print()

    test_hours = list(range(from_ts, to_ts, 3600))

    hdr = f"{'Godz':>5}  {'Cena':>7}  {'STARY reżim':>16}  {'NOWY reżim':>16}  " \
          f"{'STARY setup':>30}  {'wynik':>8}  {'NOWY setup':>30}  {'wynik':>8}"
    print(hdr)
    print("-" * len(hdr))

    stats = {"old_sl": 0, "old_tp": 0, "old_no": 0, "old_pnl": 0.0,
             "new_sl": 0, "new_tp": 0, "new_no": 0, "new_pnl": 0.0}

    for ts in test_hours:
        m15_snap = [c for c in m15_all if c["time"] < ts]
        h1_snap  = [c for c in h1_all  if c["time"] < ts]
        future   = [c for c in m15_all if c["time"] >= ts]

        if len(m15_snap) < 20 or len(h1_snap) < 12:
            continue

        price = m15_snap[-1]["close"]
        max_dist = price * 0.03
        dt_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m %H:%M")

        old_regime, c24, c48, _ = detect_regime_old(m15_snap, h1_snap, price)
        new_regime, _, _, c7d   = detect_regime_new(m15_snap, h1_snap, price)

        swing_high = max(c["high"] for c in h1_snap[-12:])

        # Stary setup (stary reżim + stara konsolidacja)
        old_consol = find_consolidation_old(h1_snap)
        old_setup  = None
        if "DOWN" in old_regime.split()[0]:
            old_setup = make_consol_short_old(old_consol, h1_snap, price, max_dist)
        old_res = evaluate(old_setup, future)

        # Nowy setup (nowy reżim + nowa konsolidacja + warunek swing_high)
        new_consol = find_consolidation_new(h1_snap)
        new_setup  = None
        if "DOWN" in new_regime.split()[0]:
            new_setup = make_consol_short(new_consol, h1_snap, swing_high, price, max_dist)
        new_res = evaluate(new_setup, future)

        # Formatowanie setupu
        def fmt(s):
            if s is None: return f"{'brak':>30}"
            return f"W={s['w']:.2f} SL={s['sl']:.2f} TP1={s['tp1']:.2f} RR={s['rr']:.1f}"

        # P&L w punktach (W→TP1 lub W→SL)
        def pnl(s, res):
            if not s: return 0.0
            if "TP1" in res: return round(s["w"] - s["tp1"], 2)   # short: zysk
            if "SL"  in res: return round(s["sl"] - s["w"],  2) * -1  # short: strata
            return 0.0

        old_pnl = pnl(old_setup, old_res)
        new_pnl = pnl(new_setup, new_res)

        # Statystyki
        if old_setup:
            if "TP1" in old_res: stats["old_tp"] += 1; stats["old_pnl"] += old_pnl
            elif "SL" in old_res: stats["old_sl"] += 1; stats["old_pnl"] += old_pnl
            else:                  stats["old_no"] += 1
        if new_setup:
            if "TP1" in new_res: stats["new_tp"] += 1; stats["new_pnl"] += new_pnl
            elif "SL" in new_res: stats["new_sl"] += 1; stats["new_pnl"] += new_pnl
            else:                  stats["new_no"] += 1

        pnl_str_old = f"{old_pnl:+.2f}" if old_setup and old_pnl != 0 else ""
        pnl_str_new = f"{new_pnl:+.2f}" if new_setup and new_pnl != 0 else ""
        regime_changed = " ◄" if old_regime.split()[0] != new_regime.split()[0] else ""

        print(f"{dt_str}  {price:>7.2f}  {c7d:>+5.1f}%7d  {old_regime.split()[0]:>14}  {new_regime.split()[0]:>14}  "
              f"{fmt(old_setup)}  {old_res:>8} {pnl_str_old:>6}  "
              f"{fmt(new_setup)}  {new_res:>8} {pnl_str_new:>6}{regime_changed}")

    print()
    print("=" * 90)
    n_old = stats['old_tp'] + stats['old_sl'] + stats['old_no']
    n_new = stats['new_tp'] + stats['new_sl'] + stats['new_no']
    print(f"STARY: TP1={stats['old_tp']}  SL={stats['old_sl']}  brak={stats['old_no']}  "
          f"setupów={n_old}  P&L suma={stats['old_pnl']:+.2f} pkt")
    print(f"NOWY:  TP1={stats['new_tp']}  SL={stats['new_sl']}  brak={stats['new_no']}  "
          f"setupów={n_new}  P&L suma={stats['new_pnl']:+.2f} pkt")


if __name__ == "__main__":
    main()
