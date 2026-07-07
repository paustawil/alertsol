#!/usr/bin/env python3
"""
orderbook_analysis.py — Testuje hipotezę: "ściana" w order booku (zapisana przy sygnale
w market_context) przewiduje, gdzie cena faktycznie zawróci (MFE), lepiej niż obecny TP2
liczony z fib/ATR.

Dla każdego rozwiązanego setupu z cechami order booka (patrz sol_alert.py:fetch_order_book,
CLAUDE.md "order book depth features") rekonstruuje MFE (max favorable excursion) ze świec
M15 Bitget w oknie [entry_hit_at, exit_time] i porównuje dystans MFE do ściany vs do TP2.

Falsyfikacja: jeśli mean_abs_diff(wall) nie jest wyraźnie mniejszy niż mean_abs_diff(tp2),
albo ściany rzadko występują w istotnym zasięgu — hipoteza odrzucona, nic nie wpinamy w TP2.

Użycie:
  python orderbook_analysis.py [--date-from 2026-07-07] [--out orderbook_analysis.csv]

Wymaga: requests, psycopg2 (już w projekcie), dostęp do tej samej bazy co main_runner.py.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

import requests

import db

SYMBOL = "SOLUSDT"


def _fetch_page(end_ms: int | None, limit: int = 200) -> list[dict]:
    """Pobiera jedną stronę świec M15 z Bitget, kończącą się w `end_ms` (paginacja wstecz,
    jak w backtest_variants.py — /candles nie obsługuje startTime+endTime razem)."""
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    params = {
        "symbol": SYMBOL, "productType": "USDT-FUTURES",
        "granularity": "15m", "limit": str(limit),
    }
    if end_ms is not None:
        params["endTime"] = str(end_ms)
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json().get("data") or []
    return [
        {"time": int(d[0]) // 1000, "high": float(d[2]), "low": float(d[3])}
        for d in data
    ]


def fetch_window(start_ts: int, end_ts: int) -> list[dict]:
    """Pobiera świece M15 pokrywające [start_ts, end_ts] (unix seconds), paginując wstecz."""
    all_candles: list[dict] = []
    end_ms: int | None = (end_ts + 900) * 1000  # +1 świeca marginesu
    for _ in range(10):  # bezpiecznik — max 10 stron (2000 świec ~ 20 dni)
        batch = _fetch_page(end_ms)
        if not batch:
            break
        batch.sort(key=lambda c: c["time"])
        all_candles = batch + all_candles
        end_ms = batch[0]["time"] * 1000 - 900 * 1000
        time.sleep(0.15)  # rate limit
        if batch[0]["time"] <= start_ts or len(batch) < 2:
            break
    return [c for c in all_candles if start_ts <= c["time"] <= end_ts]


def compute_mfe(direction: str, entry: float, candles: list[dict]) -> float | None:
    """Max favorable excursion: najwyższy high (long) / najniższy low (short) w oknie."""
    if not candles:
        return None
    if direction == "long":
        return max(c["high"] for c in candles)
    return min(c["low"] for c in candles)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date-from", default=None, help="np. 2026-07-07")
    ap.add_argument("--out", default="orderbook_analysis.csv")
    args = ap.parse_args()

    rows = db.get_orderbook_exit_analysis(args.date_from)
    print(f"Setupów z cechami order booka: {len(rows)}")
    if not rows:
        print("Brak danych — poczekaj aż uzbiera się kilka/kilkanaście rozwiązanych setupów.")
        sys.exit(0)

    results = []
    for row in rows:
        direction = row["direction"]
        entry = row.get("entry_w")
        if entry is None:
            continue
        entry = float(entry)

        wall_dist_pct = (
            row.get("ob_wall_ask_dist_pct") if direction == "long"
            else row.get("ob_wall_bid_dist_pct")
        )
        tp2 = row.get("tp2")

        entry_ts = int(row["entry_hit_at"])
        exit_ts = int(row["exit_time"].timestamp())
        candles = fetch_window(entry_ts, exit_ts)
        mfe = compute_mfe(direction, entry, candles)
        if mfe is None:
            continue

        mfe_pct = (
            (mfe - entry) / entry * 100 if direction == "long"
            else (entry - mfe) / entry * 100
        )
        tp2_dist_pct = (
            round(abs(float(tp2) - entry) / entry * 100, 3) if tp2 is not None else None
        )

        results.append({
            "setup_id": row["setup_id"],
            "alert_time": row["alert_time"],
            "type": row["type"],
            "variant": row["variant"],
            "direction": direction,
            "result": row["result"],
            "ob_imbalance": row.get("ob_imbalance"),
            "ob_spread_pct": row.get("ob_spread_pct"),
            "wall_dist_pct": wall_dist_pct,
            "tp2_dist_pct": tp2_dist_pct,
            "mfe_pct": round(mfe_pct, 3),
            "wall_vs_mfe_abs_diff": (
                round(abs(float(wall_dist_pct) - mfe_pct), 3) if wall_dist_pct is not None else None
            ),
            "tp2_vs_mfe_abs_diff": (
                round(abs(tp2_dist_pct - mfe_pct), 3) if tp2_dist_pct is not None else None
            ),
        })
        print(f"  #{row['setup_id']} {direction} wall={wall_dist_pct} tp2_dist={tp2_dist_pct} mfe={mfe_pct:.2f}%")

    if not results:
        print("Brak rekonstruowalnych MFE (brak świec w oknie entry→exit).")
        sys.exit(0)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(f"\nZapisano: {args.out} ({len(results)} rekordów)")

    wall_diffs = [r["wall_vs_mfe_abs_diff"] for r in results if r["wall_vs_mfe_abs_diff"] is not None]
    tp2_diffs = [r["tp2_vs_mfe_abs_diff"] for r in results if r["tp2_vs_mfe_abs_diff"] is not None]
    print("\n── Podsumowanie ──")
    if wall_diffs:
        print(f"Ściana vs MFE: mean abs diff = {sum(wall_diffs)/len(wall_diffs):.3f}% (n={len(wall_diffs)})")
    else:
        print("Ściana vs MFE: brak danych (ściany nie występowały w zebranych setupach)")
    if tp2_diffs:
        print(f"TP2 vs MFE:    mean abs diff = {sum(tp2_diffs)/len(tp2_diffs):.3f}% (n={len(tp2_diffs)})")
    print(
        "\nMniejszy mean abs diff = lepszy predyktor rzeczywistego zawrócenia ceny. "
        "Potrzeba realnie kilkudziesięciu setupów zanim to coś znaczy statystycznie."
    )


if __name__ == "__main__":
    main()
