#!/usr/bin/env python3
"""
backtest_variants.py — Porównanie wariantów parametrów trend_pullback na danych historycznych.

Pobiera N dni świec M15+H1 z Bitget, uruchamia algo_detect_setups() w trybie replay
dla każdego punktu w historii, symuluje wejścia i wyjścia, porównuje warianty.

Użycie:
  python backtest_variants.py [--days 60] [--out wyniki.csv]

Wymaga: requests (już w projekcie)
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

# ── Import loiki algo (bez DB i exchange) ─────────────────────────────────────
sys.path.insert(0, ".")
from sol_alert import (
    detect_market_regime,
    _PULLBACK_VARIANTS,
    calc_atr,
    find_swing_points,
    _hits,
    SYMBOL,
)

# ── Konfiguracja ──────────────────────────────────────────────────────────────
LEVERAGE   = 20
TRADE_USDT = 100.0
MIN_RR     = 1.5

# Ile świec M15 czekamy na wejście (4h = 16 świec)
ENTRY_TIMEOUT_CANDLES = 16
# Ile świec M15 maksymalnie trzymamy pozycję po wejściu (16h = 64 świece)
HOLD_TIMEOUT_CANDLES  = 64


# ── Pobieranie danych historycznych ───────────────────────────────────────────

def _fetch_page(symbol: str, granularity: str, end_ms: int | None, limit: int = 200) -> list[dict]:
    """Pobiera jedną stronę (max `limit` świec) z Bitget API, kończącą się w `end_ms`.
    Uwaga: Bitget /candles nie obsługuje poprawnie startTime+endTime razem dla starszych
    okien (zwraca pustkę) — paginacja idzie WSTECZ przez samo endTime, tak jak w
    sprawdzonych archiwalnych skryptach backtestowych (np. archive/gpt3_validator_backtest.py)."""
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    params = {
        "symbol":      symbol,
        "productType": "USDT-FUTURES",
        "granularity": granularity,
        "limit":       str(limit),
    }
    if end_ms is not None:
        params["endTime"] = str(end_ms)
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json().get("data") or []
    return [
        {
            "time":   int(d[0]) // 1000,
            "open":   float(d[1]),
            "high":   float(d[2]),
            "low":    float(d[3]),
            "close":  float(d[4]),
            "volume": float(d[5]),
        }
        for d in data
    ]


def fetch_history(symbol: str, granularity: str, days: int) -> list[dict]:
    """Pobiera kompletną historię świec za ostatnie `days` dni, paginując wstecz od teraz."""
    interval_s = {"15m": 15 * 60, "1H": 60 * 60}[granularity]
    start_ts = int(time.time()) - days * 24 * 60 * 60

    all_candles: list[dict] = []
    end_ms: int | None = None
    print(f"[fetch] {granularity} {days}d: ", end="", flush=True)

    while True:
        batch = _fetch_page(symbol, granularity, end_ms)
        if not batch:
            break
        batch.sort(key=lambda c: c["time"])
        all_candles = batch + all_candles
        end_ms = batch[0]["time"] * 1000 - interval_s * 1000
        print(".", end="", flush=True)
        time.sleep(0.15)  # rate limit
        if batch[0]["time"] <= start_ts or len(batch) < 2:
            break

    print(f" {len(all_candles)} świec")
    # deduplicate + sort + przytnij do żądanego okna
    seen: set[int] = set()
    unique = []
    for c in sorted(all_candles, key=lambda x: x["time"]):
        if c["time"] not in seen and c["time"] >= start_ts:
            seen.add(c["time"])
            unique.append(c)
    return unique


# ── Symulacja jednego trade'u ─────────────────────────────────────────────────

def simulate_trade(
    setup: dict,
    candles_m15_future: list[dict],
) -> dict:
    """
    Symuluje wynik jednego setupu na przyszłych świecach M15.

    Zwraca słownik z polami:
      entered, result, pnl_pct, hours_to_entry, hold_hours, entry_price, exit_price
    """
    d         = setup["direction"]
    w1        = setup["entries"][0]
    sl        = setup["sl"]
    sl_be     = setup.get("sl_after_tp1", w1)
    tps       = setup.get("tps", [])
    tp1       = tps[0] if tps else None
    tp2       = tps[1] if len(tps) > 1 else None
    alert_ts  = setup["alert_ts"]

    # Szukamy wejścia (max ENTRY_TIMEOUT_CANDLES)
    entry_ts    = None
    entry_price = None
    for c in candles_m15_future[:ENTRY_TIMEOUT_CANDLES]:
        if _hits(c, w1, d, "entry"):
            entry_ts    = c["time"]
            entry_price = w1
            break

    if entry_ts is None:
        return {"entered": False, "result": None, "pnl_pct": None, "pnl_usd": None,
                "hours_to_entry": None, "hold_hours": None,
                "entry_price": None, "exit_price": None}

    hours_to_entry = round((entry_ts - alert_ts) / 3600.0, 2)

    # Monitorujemy po wejściu
    after_entry  = [c for c in candles_m15_future if c["time"] > entry_ts]
    result       = None
    exit_price   = None
    exit_ts      = None
    tp1_hit_at   = None
    effective_sl = sl

    for c in after_entry[:HOLD_TIMEOUT_CANDLES]:
        sl_hit  = _hits(c, effective_sl, d, "sl")
        tp1_hit = tp1 is not None and _hits(c, tp1, d, "tp")
        tp2_hit = tp2 is not None and _hits(c, tp2, d, "tp")

        if tp2_hit and tp1_hit_at is not None:
            result    = "TP2"
            exit_price = (tp1 + tp2) / 2  # half at TP1 (already locked), half at TP2
            exit_ts   = c["time"]
            break
        if tp1_hit and tp2_hit and not sl_hit and tp1_hit_at is None:
            # TP1 i TP2 trafione na tej samej świecy — rejestruj TP2 od razu
            result    = "TP2"
            exit_price = (tp1 + tp2) / 2
            exit_ts   = c["time"]
            break
        if tp1_hit and sl_hit and tp1_hit_at is None:
            result    = "SL"
            exit_price = sl
            exit_ts   = c["time"]
            break
        if tp1_hit and tp1_hit_at is None:
            tp1_hit_at   = c["time"]
            effective_sl = sl_be  # przestaw SL na BE
            if tp2 is None:
                result    = "TP1"
                exit_price = tp1
                exit_ts   = c["time"]
                break
            continue
        if sl_hit:
            if tp1_hit_at is not None:
                result    = "TP1+BE" if abs(effective_sl - w1) < 0.05 else "TP1+SL"
                exit_price = (tp1 + effective_sl) / 2
            else:
                result    = "SL"
                exit_price = sl
            exit_ts = c["time"]
            break

    if result is None:
        return {"entered": True, "result": "timeout", "pnl_pct": None, "pnl_usd": None,
                "hours_to_entry": hours_to_entry, "hold_hours": None,
                "entry_price": entry_price, "exit_price": None}

    # PnL jako % TRADE_USDT (tak jak live system)
    qty = (TRADE_USDT * LEVERAGE) / entry_price
    if result == "SL":
        price_move = (exit_price - entry_price) if d == "long" else (entry_price - exit_price)
        pnl_usd = price_move * qty
    elif result == "TP1":
        price_move = (tp1 - entry_price) if d == "long" else (entry_price - tp1)
        pnl_usd = price_move * qty
    elif result == "TP2":
        # half at TP1, half at TP2
        half_qty = qty / 2
        move1 = (tp1 - entry_price) if d == "long" else (entry_price - tp1)
        move2 = (tp2 - entry_price) if d == "long" else (entry_price - tp2)
        pnl_usd = (move1 + move2) * half_qty
    elif result in ("TP1+BE", "TP1+SL"):
        half_qty = qty / 2
        move1 = (tp1 - entry_price) if d == "long" else (entry_price - tp1)
        move2 = (effective_sl - entry_price) if d == "long" else (entry_price - effective_sl)
        pnl_usd = (move1 + move2) * half_qty
    else:
        pnl_usd = 0.0

    pnl_pct    = round(pnl_usd / TRADE_USDT * 100, 2)
    hold_hours = round((exit_ts - entry_ts) / 3600.0, 2) if exit_ts else None

    return {
        "entered":        True,
        "result":         result,
        "pnl_pct":        pnl_pct,
        "pnl_usd":        round(pnl_usd, 2),
        "hours_to_entry": hours_to_entry,
        "hold_hours":     hold_hours,
        "entry_price":    round(entry_price, 3),
        "exit_price":     round(exit_price, 3) if exit_price else None,
    }


# ── Generowanie setupów per wariant bez zależności od DB ─────────────────────

def gen_pullback_setups_for_snapshot(
    candles_h1:  list[dict],
    candles_m15: list[dict],
    current_price: float,
    alert_ts: int,
) -> list[dict]:
    """
    Generuje setupy trend_pullback dla wszystkich wariantów na danym snapshocie.
    Nie korzysta z DB — tylko algorytm i dane świecowe.
    """
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    if atr <= 0:
        return []

    try:
        regime = detect_market_regime(candles_m15, candles_h1, current_price)
    except Exception:
        return []

    regime_name = regime["regime"]
    direction   = regime.get("direction", "none")
    strength    = regime.get("score", 0)

    if direction not in ("up", "down"):
        return []

    max_entry_dist = current_price * 0.03

    if direction == "down":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        swing_low  = min(swing_low,  current_price)
        swing_high = max(swing_high, current_price)
        tp_direction = "short"
        tp1_base = swing_low
        tp2_base = lambda r: swing_low - r * 0.3
        def _w_fn(mid, r): return swing_low + mid * r
        def _sl_fn(fib_sl, atr_sl, r): return swing_low + fib_sl * r + atr * atr_sl
        def _rr(w, tp1, sl): return round((w - tp1) / (sl - w), 1) if sl > w else 0
        def _ok(w): return w > current_price * 1.003 and (w - current_price) <= max_entry_dist
    else:
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        swing_low  = min(swing_low,  current_price)
        swing_high = max(swing_high, current_price)
        tp_direction = "long"
        tp1_base = swing_high
        tp2_base = lambda r: swing_high + r * 0.3
        def _w_fn(mid, r): return swing_high - mid * r
        def _sl_fn(fib_sl, atr_sl, r): return swing_high - fib_sl * r - atr * atr_sl
        def _rr(w, tp1, sl): return round((tp1 - w) / (w - sl), 1) if w > sl else 0
        def _ok(w): return w < current_price * 0.997 and (current_price - w) <= max_entry_dist

    if not (swing_high > swing_low):
        return []

    swing_range = swing_high - swing_low
    setups = []

    # Przełamanie struktury (patrz sol_alert.py _setup_ctx): ostatnie 2 świece H1,
    # które ZAMKNĘŁY się poza swing_high (short) / swing_low (long).
    boundary = swing_high if tp_direction == "short" else swing_low
    recent_h1 = candles_h1[-2:] if len(candles_h1) >= 2 else []
    closes_beyond_structure = sum(
        1 for c in recent_h1
        if (c["close"] > boundary if tp_direction == "short" else c["close"] < boundary)
    )
    structure_broken = closes_beyond_structure >= 2

    for vname, (fib_lo, fib_hi, fib_sl, atr_sl, str_min, _) in _PULLBACK_VARIANTS.items():
        # Uwaga: w live systemie str4 odpala tylko przy strength==4 (baseline pokrywa >=5),
        # ale w backteście każdy wariant jest niezależny — str4 odpala przy strength>=4.
        if strength < str_min:
            continue

        entry_mid = (fib_lo + fib_hi) / 2
        w   = round(_w_fn(entry_mid, swing_range), 2)
        sl  = round(_sl_fn(fib_sl, atr_sl, swing_range), 2)
        tp1 = round(tp1_base, 2)
        tp2 = round(tp2_base(swing_range), 2)
        rr_val = _rr(w, tp1, sl)

        if rr_val < MIN_RR:
            continue
        if not _ok(w):
            continue

        setups.append({
            "type":      f"trend_pullback_{tp_direction}",
            "direction": tp_direction,
            "variant":   vname,
            "regime":    regime_name,
            "strength":  strength,
            "entries":   [w],
            "sl":        sl,
            "sl_after_tp1": w,
            "tps":       [tp1, tp2],
            "rr":        rr_val,
            "swing_range": round(swing_range, 2),
            "alert_ts":  alert_ts,
            "closes_beyond_structure": closes_beyond_structure,
            "structure_broken": structure_broken,
        })

    return setups


# ── Główna pętla backtestowa ───────────────────────────────────────────────────

def run_backtest(days: int = 60, out_path: str = "backtest_variants_result.csv") -> dict:
    """Zwraca dict {ok, message, candles_m15, candles_h1, setups_generated, rows}
    — zamiast milczeć przy braku wyników, żeby wywołujący (np. main_runner.py)
    mógł pokazać prawdziwą przyczynę zamiast crashować przy otwieraniu pliku."""
    print(f"\n=== Backtest wariantów trend_pullback | {days} dni ===\n")

    # Pobierz dane historyczne
    candles_m15 = fetch_history(SYMBOL, "15m",  days + 3)  # +3 na lookback
    candles_h1  = fetch_history(SYMBOL, "1H",   days + 3)

    if len(candles_m15) < 100 or len(candles_h1) < 50:
        msg = f"Za mało danych historycznych (M15={len(candles_m15)}, H1={len(candles_h1)})."
        print(msg)
        return {"ok": False, "message": msg, "candles_m15": len(candles_m15),
                "candles_h1": len(candles_h1), "setups_generated": 0, "rows": 0}

    # Indeks H1 po timestampie dla szybkiego lookup
    h1_by_ts = {c["time"]: i for i, c in enumerate(candles_h1)}

    results: list[dict] = []
    step = 4  # co ile świec M15 (co 1h) odpalamy detekcję

    # Blokada: czas (unix) do kiedy dany wariant+kierunek jest "zajęty"
    # Modeluje zachowanie live systemu — nowy setup nie jest generowany
    # dopóki poprzedni nie wygaśnie (ENTRY_TIMEOUT + HOLD_TIMEOUT max).
    BLOCKED_SECONDS = (ENTRY_TIMEOUT_CANDLES + HOLD_TIMEOUT_CANDLES) * 15 * 60
    active_until: dict[str, int] = {}  # klucz: "{variant}_{direction}"

    print(f"Replay: {len(candles_m15)} świec M15 (step={step}) ...", flush=True)
    generated = 0

    for i in range(80, len(candles_m15) - HOLD_TIMEOUT_CANDLES, step):
        snap_m15 = candles_m15[max(0, i - 100):i]
        if not snap_m15:
            continue

        snap_ts      = snap_m15[-1]["time"]
        current_price = snap_m15[-1]["close"]

        # Dopasuj okno H1 do tego momentu w czasie
        h1_idx = next((j for j, c in enumerate(candles_h1) if c["time"] >= snap_ts), None)
        if h1_idx is None or h1_idx < 20:
            continue
        snap_h1 = candles_h1[max(0, h1_idx - 60):h1_idx]

        # Generuj setupy dla wszystkich wariantów
        setups_all = gen_pullback_setups_for_snapshot(snap_h1, snap_m15, current_price, snap_ts)
        if not setups_all:
            continue

        # n_vars: ile wariantów MOGŁO odpałić (przed blokadą) — do porównania Entry%
        n_vars = len(setups_all)

        # Odfiltruj warianty które są aktualnie "zajęte"
        setups = [s for s in setups_all
                  if snap_ts >= active_until.get(f"{s['variant']}_{s['direction']}", 0)]
        if not setups:
            continue

        generated += len(setups)
        future_m15 = candles_m15[i:]
        n_vars = len(setups)  # ile wariantów odpaliło na tym snapshotu

        for s in setups:
            trade = simulate_trade(s, future_m15)
            results.append({
                "alert_dt":      datetime.fromtimestamp(snap_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "type":          s["type"],
                "direction":     s["direction"],
                "variant":       s["variant"],
                "regime":        s["regime"],
                "strength":      s["strength"],
                "w1":            s["entries"][0],
                "tp1":           s["tps"][0],
                "tp2":           s["tps"][1] if len(s["tps"]) > 1 else "",
                "sl":            s["sl"],
                "rr":            s["rr"],
                "swing_range":   s["swing_range"],
                "closes_beyond_structure": s["closes_beyond_structure"],
                "structure_broken": s["structure_broken"],
                "n_vars":        n_vars,
                **{k: v for k, v in trade.items()},
            })
            # Zablokuj wariant+kierunek na czas trwania tego setupu
            block_key = f"{s['variant']}_{s['direction']}"
            active_until[block_key] = snap_ts + BLOCKED_SECONDS

    print(f"\nWygenerowano {generated} setupów → {len(results)} rekordów wyników")

    # Zapisz CSV
    if results:
        fieldnames = list(results[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"Zapisano: {out_path}")

    # Podsumowanie per wariant
    _print_summary(results)

    if not results:
        return {
            "ok": False,
            "message": (
                f"Pobrano {len(candles_m15)} świec M15 / {len(candles_h1)} H1, "
                f"ale 0 setupów przeszło filtry (RR>={MIN_RR}, strength, dystans od ceny) "
                f"w {days} dniach — sprawdź logikę filtrów w gen_pullback_setups_for_snapshot()."
            ),
            "candles_m15": len(candles_m15), "candles_h1": len(candles_h1),
            "setups_generated": generated, "rows": 0,
        }
    return {
        "ok": True, "message": f"Zapisano {len(results)} rekordów.",
        "candles_m15": len(candles_m15), "candles_h1": len(candles_h1),
        "setups_generated": generated, "rows": len(results),
    }


def _print_summary(results: list[dict]) -> None:
    """Drukuje tabelę porównawczą per wariant."""
    from collections import defaultdict

    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "entered": 0, "sl": 0, "tp1": 0, "tp2": 0, "tp1_be": 0,
        "pnl_sum": 0.0, "rr_sum": 0.0, "rr_count": 0,
        "rr_tp2_sum": 0.0, "rr_tp2_count": 0,
    })

    for r in results:
        v = r["variant"]
        stats[v]["total"] += 1
        try:
            rr1 = float(r.get("rr") or 0)
            if rr1 > 0:
                stats[v]["rr_sum"]   += rr1
                stats[v]["rr_count"] += 1
        except (TypeError, ValueError):
            pass
        try:
            w1  = float(r.get("w1") or 0)
            tp2 = float(r.get("tp2") or 0)
            slv = float(r.get("sl")  or 0)
            denom = abs(w1 - slv)
            numer = abs(tp2 - w1)
            if denom > 0 and numer > 0:
                stats[v]["rr_tp2_sum"]   += numer / denom
                stats[v]["rr_tp2_count"] += 1
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        if r["entered"]:
            stats[v]["entered"] += 1
            res = r.get("result")
            if res == "SL":       stats[v]["sl"]     += 1
            elif res == "TP1":    stats[v]["tp1"]    += 1
            elif res == "TP2":    stats[v]["tp2"]    += 1
            elif res in ("TP1+BE", "TP1+SL"):
                                  stats[v]["tp1_be"] += 1
            if r.get("pnl_pct") is not None:
                stats[v]["pnl_sum"] += r["pnl_pct"]

    print("\n" + "=" * 100)
    print(f"{'Variant':<12} {'Total':>6} {'Entry%':>7} "
          f"{'RR_TP1':>7} {'RR_TP2':>7} "
          f"{'SL':>5} {'TP1+BE':>7} {'TP2':>5} "
          f"{'WR_TP1+':>8} {'WR_TP2':>7} {'ΣPnL%':>8} {'AvgPnL%':>9}")
    print("-" * 100)

    for vname, s in sorted(stats.items()):
        total    = s["total"]
        entered  = s["entered"]
        entry_r  = f"{entered/total*100:.1f}%" if total else "-"
        tp1_plus = s["tp1"] + s["tp1_be"] + s["tp2"]
        wr_tp1   = f"{tp1_plus/entered*100:.1f}%" if entered else "-"
        wr_tp2   = f"{s['tp2']/entered*100:.1f}%" if entered else "-"
        rr_tp1   = f"{s['rr_sum']/s['rr_count']:.2f}"     if s["rr_count"]      else "-"
        rr_tp2   = f"{s['rr_tp2_sum']/s['rr_tp2_count']:.2f}" if s["rr_tp2_count"] else "-"
        pnl_sum  = s["pnl_sum"]
        avg_pnl  = pnl_sum / entered if entered else 0
        print(f"{vname:<12} {total:>6} {entry_r:>7} "
              f"{rr_tp1:>7} {rr_tp2:>7} "
              f"{s['sl']:>5} {s['tp1_be']:>7} {s['tp2']:>5} "
              f"{wr_tp1:>8} {wr_tp2:>7} {pnl_sum:>+8.1f}% {avg_pnl:>+9.2f}%")
    print("=" * 100)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest wariantów parametrów trend_pullback")
    parser.add_argument("--days", type=int, default=60,
                        help="Liczba dni historii (domyślnie: 60)")
    parser.add_argument("--out", type=str, default="backtest_variants_result.csv",
                        help="Ścieżka pliku wyjściowego CSV")
    args = parser.parse_args()
    run_backtest(days=args.days, out_path=args.out)
