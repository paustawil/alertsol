"""
Diagnostyka detect_market_regime() na syntetycznych danych.

Symuluje scenariusz zbliżony do SOL 25-29 marca 2026:
- 25 marca: konsolidacja ~$93
- 26 marca: breakout down, spadek do ~$88
- 27 marca: kontynuacja spadku do ~$85
- 28 marca: dalsza erozja do ~$83, Grok produkował longi (problem)
- 29 marca: stabilizacja/lekki bounce ~$82-84

Testuje:
1. Czy algorytm wykrywa moment breakoutu?
2. Czy gubi breakout podczas kontynuacji trendu (ref range shifting)?
3. Jak szybko score spada?

Uruchomienie:
    python diagnose_regime.py
"""

import random
import math


# ── Funkcje skopiowane z sol_alert.py (identyczne) ──────────────────────────

def h1_trend(candles_h1: list[dict]) -> str:
    closes = [c["close"] for c in candles_h1[-20:]]
    pct = (sum(closes[-5:]) / 5 - sum(closes[-20:]) / 20) / (sum(closes[-20:]) / 20) * 100
    if pct > 1.0:  return "bullish"
    if pct < -1.0: return "bearish"
    return "neutral"


def detect_range(candles: list[dict], n: int = 32) -> dict:
    recent     = candles[-n:]
    resistance = max(c["high"] for c in recent)
    support    = min(c["low"]  for c in recent)
    rng_size   = resistance - support
    zone       = rng_size * 0.06
    return {
        "resistance": round(resistance, 2), "support": round(support, 2),
        "range_size": round(rng_size, 2),
        "r_touches": sum(1 for c in recent if c["high"] >= resistance - zone),
        "s_touches": sum(1 for c in recent if c["low"]  <= support    + zone),
    }


def detect_market_regime(
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
) -> dict:
    if len(candles_h1) < 20:
        rng = detect_range(candles_h1, n=min(len(candles_h1), 32))
        ref_support = rng["support"]
        ref_resistance = rng["resistance"]
    else:
        ref_candles = candles_h1[-40:-8] if len(candles_h1) >= 40 else candles_h1[:-8]
        ref_resistance = max(c["high"] for c in ref_candles)
        ref_support = min(c["low"] for c in ref_candles)

    rng_size = ref_resistance - ref_support
    if rng_size <= 0:
        return {"regime": "CONSOLIDATION", "support": ref_support, "resistance": ref_resistance,
                "range_size": 0, "details": "brak zakresu"}

    recent_m15 = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent_m15[:-2]) / max(len(recent_m15[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent_m15[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    last_3_closes = [c["close"] for c in candles_m15[-3:]]
    closes_below = sum(1 for c in last_3_closes if c < ref_support)
    closes_above = sum(1 for c in last_3_closes if c > ref_resistance)

    pct_below_support = (ref_support - current_price) / ref_support * 100 if current_price < ref_support else 0
    pct_above_resistance = (current_price - ref_resistance) / ref_resistance * 100 if current_price > ref_resistance else 0

    h1_recent = candles_h1[-8:]
    h1_lows = [c["low"] for c in h1_recent]
    h1_highs = [c["high"] for c in h1_recent]
    lower_lows = sum(1 for i in range(1, len(h1_lows)) if h1_lows[i] < h1_lows[i - 1])
    higher_highs = sum(1 for i in range(1, len(h1_highs)) if h1_highs[i] > h1_highs[i - 1])

    recent_low = min(c["low"] for c in candles_h1[-8:])
    recent_high = max(c["high"] for c in candles_h1[-8:])
    range_shift_down = (ref_support - recent_low) / rng_size * 100 if recent_low < ref_support else 0
    range_shift_up = (recent_high - ref_resistance) / rng_size * 100 if recent_high > ref_resistance else 0

    price_24h_ago = candles_h1[-24]["close"] if len(candles_h1) >= 24 else candles_h1[0]["close"]
    price_48h_ago = candles_h1[-48]["close"] if len(candles_h1) >= 48 else candles_h1[0]["close"]
    change_24h = (current_price - price_24h_ago) / price_24h_ago * 100
    change_48h = (current_price - price_48h_ago) / price_48h_ago * 100

    breakdown_score = 0
    breakdown_details = []

    if current_price < ref_support:
        breakdown_score += 1
        breakdown_details.append("cena<sup")
    if closes_below >= 2:
        breakdown_score += 1
        breakdown_details.append(f"{closes_below}/3 M15<sup")
    if pct_below_support >= 1.5:
        breakdown_score += 1
        breakdown_details.append(f"{pct_below_support:.1f}%<sup")
    if vol_ratio >= 1.5 and current_price < ref_support:
        breakdown_score += 1
        breakdown_details.append(f"vol{vol_ratio:.1f}x")
    if lower_lows >= 4:
        breakdown_score += 1
        breakdown_details.append(f"{lower_lows}LL")
    if range_shift_down >= 30:
        breakdown_score += 1
        breakdown_details.append(f"shift↓{range_shift_down:.0f}%")
    if change_24h <= -3.0:
        breakdown_score += 2
        breakdown_details.append(f"24h:{change_24h:+.1f}%!")
    elif change_24h <= -1.5:
        breakdown_score += 1
        breakdown_details.append(f"24h:{change_24h:+.1f}%")
    if change_48h <= -5.0:
        breakdown_score += 2
        breakdown_details.append(f"48h:{change_48h:+.1f}%!")
    elif change_48h <= -3.0:
        breakdown_score += 1
        breakdown_details.append(f"48h:{change_48h:+.1f}%")

    breakup_score = 0
    breakup_details = []

    if current_price > ref_resistance:
        breakup_score += 1
        breakup_details.append("cena>res")
    if closes_above >= 2:
        breakup_score += 1
        breakup_details.append(f"{closes_above}/3 M15>res")
    if pct_above_resistance >= 1.5:
        breakup_score += 1
        breakup_details.append(f"{pct_above_resistance:.1f}%>res")
    if vol_ratio >= 1.5 and current_price > ref_resistance:
        breakup_score += 1
        breakup_details.append(f"vol{vol_ratio:.1f}x")
    if higher_highs >= 4:
        breakup_score += 1
        breakup_details.append(f"{higher_highs}HH")
    if range_shift_up >= 30:
        breakup_score += 1
        breakup_details.append(f"shift↑{range_shift_up:.0f}%")
    if change_24h >= 3.0:
        breakup_score += 2
        breakup_details.append(f"24h:{change_24h:+.1f}%!")
    elif change_24h >= 1.5:
        breakup_score += 1
        breakup_details.append(f"24h:{change_24h:+.1f}%")
    if change_48h >= 5.0:
        breakup_score += 2
        breakup_details.append(f"48h:{change_48h:+.1f}%!")
    elif change_48h >= 3.0:
        breakup_score += 1
        breakup_details.append(f"48h:{change_48h:+.1f}%")

    if breakdown_score >= 2 and breakdown_score > breakup_score:
        return {
            "regime": "BREAKOUT_DOWN",
            "support": ref_support, "resistance": ref_resistance, "range_size": round(rng_size, 2),
            "score": breakdown_score, "vol_ratio": round(vol_ratio, 1),
            "pct_outside": round(pct_below_support, 1),
            "details": "; ".join(breakdown_details),
            "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
        }
    elif breakup_score >= 2 and breakup_score > breakdown_score:
        return {
            "regime": "BREAKOUT_UP",
            "support": ref_support, "resistance": ref_resistance, "range_size": round(rng_size, 2),
            "score": breakup_score, "vol_ratio": round(vol_ratio, 1),
            "pct_outside": round(pct_above_resistance, 1),
            "details": "; ".join(breakup_details),
            "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
        }
    else:
        return {
            "regime": "CONSOLIDATION",
            "support": ref_support, "resistance": ref_resistance, "range_size": round(rng_size, 2),
            "score": 0, "vol_ratio": round(vol_ratio, 1),
            "pct_outside": 0,
            "details": "brak sygnałów",
            "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
        }


# ── Generator syntetycznych danych ──────────────────────────────────────────

def generate_candles(base_price: float, hourly_returns: list[float], start_ts: int) -> tuple[list[dict], list[dict]]:
    """
    Generuje H1 i M15 świece na podstawie listy godzinnych zwrotów.

    hourly_returns: lista float, np. [-0.005, -0.002, ...] = procentowa zmiana na godzinę
    Zwraca: (candles_m15, candles_h1)
    """
    random.seed(42)

    h1_candles = []
    m15_candles = []
    price = base_price

    for i, h_ret in enumerate(hourly_returns):
        h_ts = start_ts + i * 3600
        h_open = price
        h_close = price * (1 + h_ret)

        # 4 świece M15 w ramach tej godziny
        m15_prices = [h_open]
        for j in range(4):
            step_ret = h_ret / 4 + random.gauss(0, abs(h_ret) * 0.3 + 0.001)
            m15_prices.append(m15_prices[-1] * (1 + step_ret))
        # Dopasuj ostatnią do close H1
        m15_prices[-1] = h_close

        for j in range(4):
            m_open = m15_prices[j]
            m_close = m15_prices[j + 1]
            m_high = max(m_open, m_close) * (1 + random.uniform(0.001, 0.004))
            m_low = min(m_open, m_close) * (1 - random.uniform(0.001, 0.004))
            m15_candles.append({
                "time": h_ts + j * 900,
                "open": round(m_open, 2),
                "high": round(m_high, 2),
                "low": round(m_low, 2),
                "close": round(m_close, 2),
                "volume": round(random.uniform(50000, 200000) * (1 + abs(h_ret) * 20), 0),
            })

        h_high = max(c["high"] for c in m15_candles[-4:])
        h_low = min(c["low"] for c in m15_candles[-4:])
        h1_candles.append({
            "time": h_ts,
            "open": round(h_open, 2),
            "high": round(h_high, 2),
            "low": round(h_low, 2),
            "close": round(h_close, 2),
            "volume": sum(c["volume"] for c in m15_candles[-4:]),
        })

        price = h_close

    return m15_candles, h1_candles


def build_scenario() -> tuple[list[dict], list[dict]]:
    """
    Scenariusz SOL ~25-29 marca 2026:
    - Wstecz 2 dni (23-24 mar): konsolidacja $91-$95
    - 25 marca: konsolidacja kontynuacja $91-$94
    - 26 marca: breakout down — gwałtowny spadek z $93 do $88 (od drugiej połowy dnia)
    - 27 marca: kontynuacja — $88 do $85
    - 28 marca: dalsza erozja — $85 do $83 (tu Grok generował longi)
    - 29 marca: stabilizacja — $82-$84
    """
    returns = []

    # 23 marca (24h): konsolidacja $91-$95
    for _ in range(24):
        returns.append(random.gauss(0, 0.003))

    # 24 marca (24h): konsolidacja kontynuacja
    for _ in range(24):
        returns.append(random.gauss(0, 0.003))

    # 25 marca (24h): konsolidacja z lekkim spadkiem
    for _ in range(24):
        returns.append(random.gauss(-0.001, 0.003))

    # 26 marca (24h): BREAKOUT DOWN — start spokojny, druga połowa gwałtowny spadek
    for _ in range(12):  # pierwsza połowa: spokojnie
        returns.append(random.gauss(-0.001, 0.003))
    for _ in range(12):  # druga połowa: silne spadki -5% w 12h
        returns.append(-0.004 + random.gauss(0, 0.002))

    # 27 marca (24h): kontynuacja spadków — wolniej ale stale
    for _ in range(24):
        returns.append(-0.0015 + random.gauss(0, 0.002))

    # 28 marca (24h): powolne osuwanie się + drobne odbicia
    for _ in range(24):
        returns.append(-0.001 + random.gauss(0, 0.003))

    # 29 marca (24h): stabilizacja
    for _ in range(24):
        returns.append(random.gauss(0, 0.003))

    base_price = 93.0
    start_ts = 1774483200  # 2026-03-23 00:00 UTC (aproks.)

    return generate_candles(base_price, returns, start_ts)


# ── Diagnostyka ──────────────────────────────────────────────────────────────

def run():
    print("Generowanie syntetycznych danych (scenariusz SOL 23-29 marca)...")
    all_m15, all_h1 = build_scenario()
    print(f"  M15: {len(all_m15)} świec | H1: {len(all_h1)} świec")
    print(f"  Cena start: ${all_h1[0]['open']:.2f} → koniec: ${all_h1[-1]['close']:.2f}")
    print()

    # Nagłówek
    hdr = (
        f"{'hour':>4} | {'datetime':>16} | {'price':>7} | {'regime':>15} | {'scr':>3} | "
        f"{'ref_sup':>7} | {'ref_res':>7} | {'h1_tr':>7} | {'24h%':>6} | {'48h%':>6} | details"
    )
    print(hdr)
    print("-" * len(hdr) + "-" * 20)

    # Zaczynamy diagnostykę od godziny 48 (25 marca 00:00) — potrzebujemy kontekstu wstecz
    start_offset = 48  # godziny od początku danych
    prev_regime = None
    regime_changes = []
    regime_hours = {"CONSOLIDATION": 0, "BREAKOUT_DOWN": 0, "BREAKOUT_UP": 0}

    for h_idx in range(start_offset, len(all_h1)):
        # Kontekst: H1 do h_idx (zamknięte świece), M15 odpowiednio
        ctx_h1 = all_h1[:h_idx]  # wszystko do tej godziny (ale nie bieżąca)
        ctx_m15 = all_m15[:h_idx * 4]  # M15 odpowiadające

        if len(ctx_h1) < 20 or len(ctx_m15) < 20:
            continue

        current_price = ctx_m15[-1]["close"]
        regime = detect_market_regime(ctx_m15[-100:], ctx_h1[-50:], current_price)
        trend = h1_trend(ctx_h1[-20:])

        regime_name = regime["regime"]
        score = regime.get("score", 0)
        ref_sup = regime.get("support", 0)
        ref_res = regime.get("resistance", 0)
        ch24 = regime.get("change_24h", 0)
        ch48 = regime.get("change_48h", 0)
        details = regime.get("details", "")

        regime_hours[regime_name] = regime_hours.get(regime_name, 0) + 1

        if len(details) > 50:
            details = details[:47] + "..."

        marker = ""
        if prev_regime is not None and regime_name != prev_regime:
            marker = " <<<"
            regime_changes.append((h_idx, regime_name, current_price))
        prev_regime = regime_name

        # Dzień etykieta
        day_offset = (h_idx - 48) // 24
        day_labels = ["25 mar", "26 mar", "27 mar", "28 mar", "29 mar"]
        day = day_labels[day_offset] if day_offset < len(day_labels) else "?"
        hour_in_day = (h_idx - 48) % 24

        print(
            f"{h_idx:>4} | {day:>6} {hour_in_day:02d}:00 UTC | ${current_price:>6.2f} | {regime_name:>15} | {score:>3} | "
            f"${ref_sup:>6.2f} | ${ref_res:>6.2f} | {trend:>7} | {ch24:>+5.1f}% | {ch48:>+5.1f}% | {details}{marker}"
        )

    # Podsumowanie
    print()
    print("=" * 70)
    print("PODSUMOWANIE:")
    print("=" * 70)
    for name, hours in regime_hours.items():
        total = sum(regime_hours.values())
        pct = hours / total * 100 if total > 0 else 0
        print(f"  {name:>15}: {hours:>3}h ({pct:.0f}%)")

    if regime_changes:
        print()
        print("ZMIANY REŻIMU:")
        for h_idx, to_regime, price in regime_changes:
            day_offset = (h_idx - 48) // 24
            day_labels = ["25 mar", "26 mar", "27 mar", "28 mar", "29 mar"]
            day = day_labels[day_offset] if day_offset < len(day_labels) else "?"
            hour_in_day = (h_idx - 48) % 24
            print(f"  {day} {hour_in_day:02d}:00 → {to_regime:<15} (${price:.2f})")

    # Kluczowe momenty do analizy
    print()
    print("KLUCZOWE PYTANIA:")
    print("  1. Czy 26 marca (breakout) algorytm wykrył BREAKOUT_DOWN?")
    print("  2. Czy 27-28 marca (kontynuacja) nadal widzi BREAKOUT_DOWN?")
    print("  3. Jak ref_support się przesuwa z dnia na dzień?")
    print("  4. Kiedy (i czy) traci detekcję trendu?")


if __name__ == "__main__":
    run()
