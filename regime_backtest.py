#!/usr/bin/env python3
"""
regime_backtest.py — Porównanie nowej vs starej logiki reżimu pod kątem wyników TP/SL.

Obie logiki reżimu testowane są na TYM SAMYM generatorze setupów — wiernej
kopii algo_detect_setups z sol_alert.py (produkcja), bez sprawdzania świeżości.

  - STARA: detect_market_regime z sol_alert.py (produkcja)
  - NOWA:  detect_regime_new   z diagnose_regime.py (prototyp)

Uruchomienie:
    python regime_backtest.py --from "2026-04-01 00:00" --to "2026-04-09 23:00"
    python regime_backtest.py --from "2026-03-01 00:00" --to "2026-04-09 23:00"
    python regime_backtest.py --from "2026-03-01 00:00" --to "2026-04-09 23:00" --diff
"""

import argparse
import sys
from collections import defaultdict

from diagnose_regime import (
    detect_regime_new, evaluate_setup,
    fetch_klines_paginated, _ts_fmt, _parse_dt,
    calc_atr, h1_trend, impulse_strength, detect_range,
    find_swing_points, find_consolidation,
)


# ── Generator setupów — wierna kopia algo_detect_setups z sol_alert.py ───────
# Różnice vs diagnose_regime.py:
#   - trend_consolidation_short WYŁĄCZONY (if False — identycznie jak produkcja)
#   - RANGE: filtry momentum, touches, MA alignment — identycznie jak produkcja
#   - Brak freshness check (zbędny w backteście)

def prod_detect_setups(regime: dict, candles_m15: list[dict], candles_h1: list[dict],
                       current_price: float) -> list[dict]:
    """Produkcyjna logika setupów (sol_alert.algo_detect_setups) bez freshness check."""
    regime_name = regime["regime"]
    direction   = regime.get("direction", "none")
    strength    = regime.get("strength", regime.get("score", 0))
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    setups = []

    if atr <= 0:
        return setups

    max_entry_dist = current_price * 0.03  # 3%

    # ── TREND_DOWN / IMPULSE_DOWN ─────────────────────────────────────────────
    if direction == "down":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        swing_low  = min(swing_low,  current_price)
        swing_high = max(swing_high, current_price)

        # trend_consolidation_short — WYŁĄCZONY (jak w produkcji: if False)
        # consol = None

        # trend_pullback_short — 38-50% korekty
        if swing_high > swing_low:
            sr = swing_high - swing_low
            fib38 = swing_low + sr * 0.38
            fib50 = swing_low + sr * 0.50
            fib618 = swing_low + sr * 0.618
            w  = round((fib38 + fib50) / 2, 2)
            sl = round(fib618 + atr * 0.3, 2)
            tp1 = round(swing_low, 2)
            tp2 = round(swing_low - sr * 0.3, 2)
            rr_ok    = sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
            above_p  = w > current_price * 1.003
            dist_ok  = w - current_price <= max_entry_dist
            if rr_ok and above_p and dist_ok:
                setups.append({
                    "type": "trend_pullback_short", "direction": "short",
                    "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "rr": round((w - tp1) / (sl - w), 1),
                })

        # impulse_continuation_short — mini-pullback (tylko IMPULSE)
        if regime_name.startswith("IMPULSE_"):
            last6 = candles_m15[-6:]
            greens = [c for c in last6 if c["close"] > c["open"]]
            if 1 <= len(greens) <= 2:
                pb_high = max(c["high"] for c in last6[-2:])
                w  = round(pb_high, 2)
                sl = round(pb_high + atr * 0.8, 2)
                tp1 = round(swing_low, 2)
                tp2 = round(swing_low - atr, 2)
                rr_ok   = sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                dist_ok = abs(w - current_price) <= max_entry_dist
                if rr_ok and dist_ok:
                    setups.append({
                        "type": "impulse_continuation_short", "direction": "short",
                        "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                        "rr": round((w - tp1) / (sl - w), 1),
                    })

    # ── TREND_UP / IMPULSE_UP ─────────────────────────────────────────────────
    elif direction == "up":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        swing_low  = min(swing_low,  current_price)
        swing_high = max(swing_high, current_price)

        # trend_consolidation_long — WYŁĄCZONY (jak w produkcji)

        # trend_pullback_long — wymaga strength >= 5
        if swing_high > swing_low and strength >= 5:
            sr = swing_high - swing_low
            fib38 = swing_high - sr * 0.38
            fib50 = swing_high - sr * 0.50
            fib618 = swing_high - sr * 0.618
            w  = round((fib38 + fib50) / 2, 2)
            sl = round(fib618 - atr * 0.3, 2)
            tp1 = round(swing_high, 2)
            tp2 = round(swing_high + sr * 0.3, 2)
            below_p = w < current_price * 0.997
            dist_ok = current_price - w <= max_entry_dist
            if (sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5
                    and below_p and dist_ok):
                setups.append({
                    "type": "trend_pullback_long", "direction": "long",
                    "w": w, "sl": sl, "tp1": tp1, "tp2": tp2,
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

    # ── RANGE ─────────────────────────────────────────────────────────────────
    elif regime_name == "RANGE":
        rng = detect_range(candles_h1)
        sup, res = rng["support"], rng["resistance"]
        rng_size = res - sup
        if rng_size > atr * 1.5:
            # ── range_resistance_short ────────────────────────────────────────
            w  = res - rng_size * 0.1
            sl = res + atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = sup + rng_size * 0.1
            dist_ok = abs(w - current_price) <= max_entry_dist
            rr_ok   = (w - tp1) / (sl - w) >= 1.5

            # Filtr 1: momentum — nie shortuj przy silnym wzroście
            last6s = candles_m15[-6:]
            bull_cnt = sum(1 for c in last6s if c["close"] > c["open"])
            m15_rise = (last6s[-1]["close"] - last6s[0]["open"]) / last6s[0]["open"] * 100
            momentum_ok_s = not (bull_cnt >= 5 or m15_rise > 1.5)

            # Filtr 2: touches — opór musi mieć min 2 testy
            touches_ok_s = rng["r_touches"] >= 2

            # Filtr 3: MA alignment — nie shortuj gdy cena > MA30 > MA60
            closes_s = [c["close"] for c in candles_m15]
            ma30_s = sum(closes_s[-30:]) / 30 if len(closes_s) >= 30 else None
            ma60_s = sum(closes_s[-60:]) / 60 if len(closes_s) >= 60 else None
            ma_ok_s = not (ma30_s and ma60_s and current_price > ma30_s > ma60_s)

            if rr_ok and dist_ok and momentum_ok_s and touches_ok_s and ma_ok_s:
                setups.append({
                    "type": "range_resistance_short", "direction": "short",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((w - tp1) / (sl - w), 1),
                })

            # ── range_support_long ────────────────────────────────────────────
            w  = sup + rng_size * 0.1
            sl = sup - atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = res - rng_size * 0.1
            dist_ok = abs(w - current_price) <= max_entry_dist
            rr_ok   = (tp1 - w) / (w - sl) >= 1.5

            # Filtr 1: momentum — nie kupuj przy silnym spadku
            last6 = candles_m15[-6:]
            bear_cnt = sum(1 for c in last6 if c["close"] < c["open"])
            m15_drop = (last6[-1]["close"] - last6[0]["open"]) / last6[0]["open"] * 100
            momentum_ok = not (bear_cnt >= 5 or m15_drop < -1.5)

            # Filtr 2: touches — wsparcie musi mieć min 2 odbicia
            touches_ok = rng["s_touches"] >= 2

            # Filtr 3: MA alignment — nie kupuj gdy cena < MA30 < MA60
            closes = [c["close"] for c in candles_m15]
            ma30 = sum(closes[-30:]) / 30 if len(closes) >= 30 else None
            ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
            ma_ok = not (ma30 and ma60 and current_price < ma30 < ma60)

            if rr_ok and dist_ok and momentum_ok and touches_ok and ma_ok:
                setups.append({
                    "type": "range_support_long", "direction": "long",
                    "w": round(w, 2), "sl": round(sl, 2),
                    "tp1": round(tp1, 2), "tp2": round(tp2, 2),
                    "rr": round((tp1 - w) / (w - sl), 1),
                })

    return setups


# ── Stara logika reżimu — kopia z sol_alert.py (bez external imports) ────────

def old_regime(candles_m15: list[dict], candles_h1: list[dict],
               current_price: float) -> dict:
    """
    detect_market_regime z sol_alert.py (commit 1e7439b) — IMPULSE/TREND/RANGE.
    Normalizuje score→strength dla kompatybilności z algo_detect_setups.
    """
    trend   = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)
    rng     = detect_range(candles_h1)

    recent_m15 = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent_m15[:-2]) / max(len(recent_m15[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent_m15[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    price_2h  = candles_m15[-8]["close"]  if len(candles_m15) >= 8  else candles_m15[0]["close"]
    price_4h  = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    price_8h  = candles_h1[-8]["close"]   if len(candles_h1) >= 8   else candles_h1[0]["close"]
    price_12h = candles_h1[-12]["close"]  if len(candles_h1) >= 12  else candles_h1[0]["close"]
    price_24h = (sum(c["close"] for c in candles_h1[-25:-22]) / 3
                 if len(candles_h1) >= 25 else candles_h1[0]["close"])
    price_48h = (sum(c["close"] for c in candles_h1[-49:-46]) / 3
                 if len(candles_h1) >= 49 else candles_h1[0]["close"])
    change_2h  = (current_price - price_2h)  / price_2h  * 100
    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_8h  = (current_price - price_8h)  / price_8h  * 100
    change_12h = (current_price - price_12h) / price_12h * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    last6 = candles_m15[-6:]
    bearish_closes = sum(1 for c in last6 if c["close"] < c["open"])
    bullish_closes = sum(1 for c in last6 if c["close"] > c["open"])

    base = {
        "support": rng["support"], "resistance": rng["resistance"],
        "range_size": rng["range_size"], "vol_ratio": round(vol_ratio, 1),
        "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
    }

    # IMPULSE
    impulse_score = 0
    impulse_dir   = "none"
    if imp_str >= 2:                              impulse_score += 1
    if vol_ratio >= 1.5:                          impulse_score += 1
    if abs(change_4h) >= 2.0:                     impulse_score += 1
    elif abs(change_4h) >= 1.5 and vol_ratio >= 1.3: impulse_score += 1
    if abs(change_2h) >= 1.2:
        impulse_score += 1
        if impulse_dir == "none":
            impulse_dir = "down" if change_2h < 0 else "up"
    if bearish_closes >= 4:
        impulse_score += 1; impulse_dir = "down"
    elif bullish_closes >= 4:
        impulse_score += 1; impulse_dir = "up"

    impulse_min_score = 2 if abs(change_4h) >= 3.0 else 3
    if impulse_score >= impulse_min_score:
        if impulse_dir == "none":
            impulse_dir = "down" if change_4h < 0 else "up"
        strength = min(10, impulse_score * 2 + imp_str)
        return {
            **base,
            "regime": f"IMPULSE_{impulse_dir.upper()}",
            "direction": impulse_dir, "score": strength, "strength": strength,
            "pct_outside": 0,
        }

    # TREND
    h1_12 = candles_h1[-12:]
    lower_lows   = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
    higher_highs = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

    trend_score = 0
    if abs(change_24h) >= 3.0:   trend_score += 2
    elif abs(change_24h) >= 1.5: trend_score += 1
    if abs(change_48h) >= 5.0:   trend_score += 2
    elif abs(change_48h) >= 3.0: trend_score += 1
    if lower_lows   >= 5:        trend_score += 1
    if higher_highs >= 5:        trend_score += 1
    if trend != "neutral":       trend_score += 1

    has_price_change = abs(change_24h) >= 1.5 or abs(change_48h) >= 3.0

    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"

        # Fix 2: ruch 4h przeczy wyznaczonemu kierunkowi
        if abs(change_4h) >= 2.5:
            recent_dir = "down" if change_4h < 0 else "up"
            if recent_dir != trend_dir:
                if (recent_dir == "down" and lower_lows >= higher_highs) or \
                   (recent_dir == "up"   and higher_highs >= lower_lows):
                    trend_dir = recent_dir

        # Fix 3: multi-timeframe consensus override
        if trend_dir == "up":
            mtf_down = sum([change_4h < -0.5, change_8h < -0.5, change_12h < -1.0])
            if mtf_down == 3:
                trend_dir = "down"
        elif trend_dir == "down":
            mtf_up = sum([change_4h > 0.5, change_8h > 0.5, change_12h > 1.0])
            if mtf_up == 3:
                trend_dir = "up"

        return {
            **base,
            "regime": f"TREND_{trend_dir.upper()}",
            "direction": trend_dir, "score": trend_score, "strength": trend_score,
            "pct_outside": 0,
        }

    # RANGE
    return {
        **base,
        "regime": "RANGE",
        "direction": "none", "score": 0, "strength": 0,
        "pct_outside": 0,
    }


def _setup_key(s: dict) -> tuple:
    """Klucz unikalności setupu — ignoruje drobne różnice w poziomach."""
    return (s["direction"], s["type"])


def run_backtest(regime_fn, all_m15: list[dict], all_h1: list[dict],
                 test_hours: list[int]) -> dict:
    """
    Dla każdej godziny: wykrywa reżim → generuje setupy → symuluje wyniki.
    Deduplicates: ten sam typ+kierunek setupu może pojawić się maks co DEDUP_HOURS godzin.
    """
    DEDUP_HOURS = 12   # ignoruj powtórzenie tego samego setupu w ciągu 12h
    SIM_HOURS   = 48   # ile godzin naprzód sprawdzamy TP/SL

    stats = {
        "setups": 0, "no_entry": 0, "open": 0,
        "TP1+TP2": 0, "TP1+BE": 0, "SL": 0,
        "pnl_tp1_only": 0.0, "pnl_combined": 0.0,
        "by_regime": defaultdict(lambda: {"setups": 0, "TP1+TP2": 0, "TP1+BE": 0,
                                          "SL": 0, "no_entry": 0, "open": 0}),
        "by_type":   defaultdict(lambda: {"setups": 0, "TP1+TP2": 0, "TP1+BE": 0,
                                          "SL": 0, "no_entry": 0, "open": 0}),
        "detail_rows": [],
        "regime_counts": defaultdict(int),
    }

    last_setup_ts: dict[tuple, int] = {}  # klucz → timestamp ostatniego setupu

    for signal_ts in test_hours:
        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 20 or len(ctx_h1) < 10:
            continue

        price  = ctx_m15[-1]["close"]
        regime = regime_fn(ctx_m15, ctx_h1, price)
        stats["regime_counts"][regime["regime"]] += 1

        setups = prod_detect_setups(regime, ctx_m15, ctx_h1, price)
        if not setups:
            continue

        # Future M15 dla symulacji
        future_m15 = [c for c in all_m15
                      if signal_ts < c["time"] <= signal_ts + SIM_HOURS * 3600]

        for setup in setups:
            key = _setup_key(setup)
            # Deduplicacja: pomiń jeśli ten sam setup był wygenerowany < DEDUP_HOURS temu
            if signal_ts - last_setup_ts.get(key, 0) < DEDUP_HOURS * 3600:
                continue
            last_setup_ts[key] = signal_ts

            if not future_m15:
                outcome = {"wynik": "open", "pnl_tp1": 0, "pnl_tp2": 0}
            else:
                outcome = evaluate_setup(setup, future_m15, entry_window_h=24)

            wynik = outcome["wynik"]
            stats["setups"] += 1
            stats[wynik] = stats.get(wynik, 0) + 1

            pnl_tp1 = outcome["pnl_tp1"]
            stats["pnl_tp1_only"] += pnl_tp1

            if wynik == "TP1+TP2":
                pnl_c = (outcome["pnl_tp1"] + outcome["pnl_tp2"]) / 2
            elif wynik == "TP1+BE":
                pnl_c = outcome["pnl_tp1"] / 2
            else:
                pnl_c = pnl_tp1
            stats["pnl_combined"] += pnl_c

            r_name = regime["regime"]
            stats["by_regime"][r_name]["setups"] += 1
            stats["by_regime"][r_name][wynik] = \
                stats["by_regime"][r_name].get(wynik, 0) + 1

            t_name = setup["type"]
            stats["by_type"][t_name]["setups"] += 1
            stats["by_type"][t_name][wynik] = \
                stats["by_type"][t_name].get(wynik, 0) + 1

            stats["detail_rows"].append({
                "ts": signal_ts, "price": price,
                "regime": r_name, "setup_type": t_name,
                "direction": setup["direction"],
                "w": setup["w"], "sl": setup["sl"],
                "tp1": setup["tp1"], "tp2": setup["tp2"],
                "rr": setup.get("rr", 0),
                "wynik": wynik, "pnl": round(pnl_tp1, 2),
            })

    return stats


def print_stats(label: str, stats: dict) -> None:
    setups = stats["setups"]
    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")

    if setups == 0:
        print("  Brak setupów.")
        return

    no_entry = stats.get("no_entry", 0)
    open_t   = stats.get("open", 0)
    tp12     = stats.get("TP1+TP2", 0)
    tp1be    = stats.get("TP1+BE", 0)
    sl       = stats.get("SL", 0)
    wins     = tp12 + tp1be
    decided  = wins + sl
    wr       = wins / decided * 100 if decided > 0 else 0.0

    print(f"  Setups (po dedup.):   {setups}")
    print(f"  Entry weszło:         {setups - no_entry}  (no_entry={no_entry}  open={open_t})")
    print(f"  TP1+TP2:              {tp12}")
    print(f"  TP1+BE:               {tp1be}")
    print(f"  SL:                   {sl}")
    print(f"  Win rate (TP/SL):     {wr:.0f}%  ({wins}W / {sl}L)")
    print(f"  PnL TP1-only ($/pkt): ${stats['pnl_tp1_only']:+.2f}")
    print(f"  PnL 50/50 combined:   ${stats['pnl_combined']:+.2f}")

    print(f"\n  Reżim → ile razy wykryty (wszystkie godziny):")
    for rname, cnt in sorted(stats["regime_counts"].items()):
        print(f"    {rname:<22} {cnt}x")

    if stats["by_regime"]:
        print(f"\n  Setupy według reżimu:")
        for rname, rs in sorted(stats["by_regime"].items()):
            w_r = rs.get("TP1+TP2", 0) + rs.get("TP1+BE", 0)
            l_r = rs.get("SL", 0)
            wr_r = w_r / (w_r + l_r) * 100 if (w_r + l_r) > 0 else 0
            print(f"    {rname:<22} setups={rs['setups']}  TP={w_r}  SL={l_r}  "
                  f"WR={wr_r:.0f}%  no_entry={rs.get('no_entry',0)}")

    if stats["by_type"]:
        print(f"\n  Setupy według typu:")
        for tname, ts in sorted(stats["by_type"].items()):
            w_t = ts.get("TP1+TP2", 0) + ts.get("TP1+BE", 0)
            l_t = ts.get("SL", 0)
            wr_t = w_t / (w_t + l_t) * 100 if (w_t + l_t) > 0 else 0
            print(f"    {tname:<35} setups={ts['setups']}  TP={w_t}  SL={l_t}  WR={wr_t:.0f}%")


def print_diff(old_stats: dict, new_stats: dict) -> None:
    """Wypisuje godziny gdzie reżim lub wynik setupu się różni."""
    old_rows = {r["ts"]: r for r in old_stats["detail_rows"]}
    new_rows = {r["ts"]: r for r in new_stats["detail_rows"]}
    all_ts   = sorted(set(list(old_rows) + list(new_rows)))

    print(f"\n{'='*100}")
    print("  Godziny z różnicą w reżimie lub setupie")
    print(f"{'='*100}")
    print(f"{'Czas':<18} {'Cena':>8}  {'STARY reżim':<20} {'Stary wynik':<12}  "
          f"{'NOWY reżim':<20} {'Nowy wynik':<12}  Komentarz")
    print("-" * 100)

    diffs = 0
    for ts in all_ts:
        old_r = old_rows.get(ts)
        new_r = new_rows.get(ts)

        old_regime  = old_r["regime"]   if old_r else "—"
        old_wynik   = old_r["wynik"]    if old_r else "—"
        old_setup   = old_r["setup_type"] if old_r else ""
        new_regime  = new_r["regime"]   if new_r else "—"
        new_wynik   = new_r["wynik"]    if new_r else "—"
        new_setup   = new_r["setup_type"] if new_r else ""

        if old_regime == new_regime and old_wynik == new_wynik:
            continue

        price = (old_r or new_r)["price"]
        comment = ""
        if old_regime != new_regime:
            comment += f"reżim zmieniony "
        if old_wynik != new_wynik:
            if old_wynik in ("SL",) and new_wynik in ("TP1+TP2", "TP1+BE"):
                comment += "POPRAWA (SL→TP)"
            elif new_wynik in ("SL",) and old_wynik in ("TP1+TP2", "TP1+BE"):
                comment += "POGORSZENIE (TP→SL)"
            elif old_wynik == "no_entry" and new_wynik not in ("no_entry", "—"):
                comment += "nowy sygnał"
            elif new_wynik == "no_entry" and old_wynik not in ("no_entry", "—"):
                comment += "utracony sygnał"

        print(f"{_ts_fmt(ts):<18} ${price:>7.2f}  "
              f"{old_regime:<20} {old_wynik:<12}  "
              f"{new_regime:<20} {new_wynik:<12}  {comment}")
        diffs += 1

    print(f"\n  Łącznie różnic: {diffs}")


def main():
    parser = argparse.ArgumentParser(
        description="Backtest: stara vs nowa logika reżimu"
    )
    parser.add_argument("--from", dest="dt_from", default="2026-04-01 00:00")
    parser.add_argument("--to",   dest="dt_to",   default="2026-04-09 23:00")
    parser.add_argument("--diff", action="store_true",
                        help="Wypisz godziny z różnicą reżimu/wyniku")
    args = parser.parse_args()

    from_ts   = _parse_dt(args.dt_from)
    to_ts     = _parse_dt(args.dt_to)
    num_hours = (to_ts - from_ts) // 3600

    print(f"Okres: {_ts_fmt(from_ts)} — {_ts_fmt(to_ts)} ({num_hours}h)")

    # Dodajemy 48h buffer za `to_ts` na symulację przyszłych świec
    m15_total = 150 + (to_ts - from_ts) // 900 + 48 * 4 + 10
    h1_total  = 60 + num_hours + 50 + 48 + 10

    print("Pobieranie M15...")
    all_m15 = fetch_klines_paginated("SOLUSDT", "15m", total=m15_total,
                                      end_ts_s=to_ts + 49 * 3600)
    print(f"  Pobrano {len(all_m15)} świec M15")

    print("Pobieranie H1...")
    all_h1 = fetch_klines_paginated("SOLUSDT", "1h", total=h1_total,
                                     end_ts_s=to_ts + 49 * 3600)
    print(f"  Pobrano {len(all_h1)} świec H1\n")

    test_hours = [from_ts + i * 3600 for i in range(num_hours + 1)]

    print("Uruchamiam backtest — STARA logika...")
    old_stats = run_backtest(old_regime, all_m15, all_h1, test_hours)
    print(f"  Gotowe. Setups: {old_stats['setups']}")

    print("Uruchamiam backtest — NOWA logika...")
    new_stats = run_backtest(detect_regime_new, all_m15, all_h1, test_hours)
    print(f"  Gotowe. Setups: {new_stats['setups']}")

    print_stats("STARA logika — detect_market_regime (produkcja)", old_stats)
    print_stats("NOWA logika  — detect_regime_new    (prototyp)",  new_stats)

    if args.diff:
        print_diff(old_stats, new_stats)


if __name__ == "__main__":
    main()
