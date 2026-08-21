#!/usr/bin/env python3
"""
backfill_tp1_tp2.py — Jednorazowy backfill dla setupów rozstrzygniętych PRZED wejściem
poprawki mislabelingu TP2 (patrz commit "Fix TP2 mislabeled as bare TP2 instead of
TP1+TP2"): check_pending() zapisywało result='TP2' zamiast 'TP1+TP2', i nigdy nie
ustawiało tp1_hit_at dla tych setupów (db.resolve_setup() nie miało wtedy takiego
parametru). TP2 zawsze implikuje wcześniejsze przejście przez TP1 (TP1 jest bliżej
entry niż TP2), więc 'TP2' samo w sobie nigdy nie było legalnym, odrębnym wynikiem —
to była czysta pomyłka etykiety.

Dla każdego setupu z result='TP2' (db.get_tp2_mislabeled_setups()):
  1. Pobiera świece M15 z Bitget w oknie [entry_hit_at, exit_time]
  2. Znajduje pierwszą świecę, która trafia poziom TP1 (tps[0]) — to jest odtworzony
     tp1_hit_at
  3. Relabeluje result -> 'TP1+TP2', i ustawia tp1_hit_at jeśli udało się go odtworzyć
     (jeśli się nie uda — np. brakujące świece — result i tak jest relabelowany, ale
     tp1_hit_at zostaje NULL; symulator portfela wtedy po prostu spada z powrotem na
     blokadę do exit_time, tak jak przed tym backfillem — nie pogarsza, tylko nie
     poprawia dla tego jednego wiersza)

Bez --apply działa w trybie dry-run: tylko drukuje co by zmienił + zapisuje CSV
z podglądem (i oryginalnymi wartościami), niczego nie modyfikuje w bazie. Z --apply
faktycznie robi UPDATE — ale preview CSV z wartościami SPRZED zmiany zapisuje się
zawsze, więc zmianę da się ręcznie cofnąć w razie potrzeby.

Użycie:
  python backfill_tp1_tp2.py                # dry-run, drukuje + zapisuje preview CSV
  python backfill_tp1_tp2.py --apply        # faktycznie modyfikuje bazę

Wymaga: requests, psycopg2 (już w projekcie), dostęp do tej samej bazy co main_runner.py
(zmienna środowiskowa DATABASE_URL) i do publicznego API Bitget (świece historyczne).
"""

import argparse
import csv
import sys
import time

import requests

import db

SYMBOL = "SOLUSDT"


def _fetch_page(end_ms: int | None, limit: int = 200) -> list[dict]:
    """Pobiera jedną stronę świec M15 z Bitget, kończącą się w `end_ms` (paginacja
    wstecz — jak w orderbook_analysis.py/backtest_variants.py — /candles nie obsługuje
    startTime+endTime razem)."""
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


def find_tp1_hit(direction: str, tp1: float, candles: list[dict]) -> int | None:
    """Pierwsza świeca (chronologicznie), która trafia poziom TP1."""
    for c in sorted(candles, key=lambda c: c["time"]):
        if direction == "long" and c["high"] >= tp1:
            return c["time"]
        if direction == "short" and c["low"] <= tp1:
            return c["time"]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                     help="faktycznie modyfikuj bazę (domyślnie dry-run — nic nie zapisuje)")
    ap.add_argument("--out", default="backfill_tp1_tp2_preview.csv")
    args = ap.parse_args()

    rows = db.get_tp2_mislabeled_setups()
    print(f"Setupów z result='TP2': {len(rows)}")
    if not rows:
        print("Brak nic do zrobienia.")
        sys.exit(0)

    preview = []
    reconstructed = 0
    for row in rows:
        setup_id = row["setup_id"]
        tps = row.get("tps") or []
        old_tp1_hit_at = row.get("tp1_hit_at")

        if not tps:
            print(f"  #{setup_id}: brak tps w bazie — tylko relabel, tp1_hit_at zostaje bez zmian")
            preview.append({
                "setup_id": setup_id, "old_result": "TP2", "old_tp1_hit_at": old_tp1_hit_at,
                "new_result": "TP1+TP2", "new_tp1_hit_at": old_tp1_hit_at,
            })
            continue

        tp1 = float(tps[0])
        entry_ts = int(row["entry_hit_at"])
        exit_ts = int(row["exit_time"].timestamp())

        try:
            candles = fetch_window(entry_ts, exit_ts)
            tp1_hit_at = find_tp1_hit(row["direction"], tp1, candles)
        except Exception as e:
            print(f"  #{setup_id}: błąd pobierania świec ({e}) — tylko relabel")
            tp1_hit_at = None

        if tp1_hit_at is not None:
            reconstructed += 1
        else:
            tp1_hit_at = old_tp1_hit_at  # nie nadpisuj czymś gorszym niż to co już jest (zwykle None)

        print(f"  #{setup_id} {row['direction']} tp1={tp1} entry@{entry_ts} exit@{exit_ts} "
              f"-> tp1_hit_at={tp1_hit_at}")
        preview.append({
            "setup_id": setup_id, "old_result": "TP2", "old_tp1_hit_at": old_tp1_hit_at,
            "new_result": "TP1+TP2", "new_tp1_hit_at": tp1_hit_at,
        })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(preview[0].keys()))
        writer.writeheader()
        writer.writerows(preview)
    print(f"\nPodgląd (i kopia zapasowa starych wartości) zapisany: {args.out} "
          f"({len(preview)} setupów, {reconstructed} z odtworzonym tp1_hit_at)")

    if not args.apply:
        print("\nDRY-RUN — nic nie zmieniono w bazie. Uruchom z --apply, żeby faktycznie zapisać.")
        return

    print(f"\nZapisuję zmiany dla {len(preview)} setupów...")
    for p in preview:
        db.backfill_tp1_tp2(p["setup_id"], p["new_tp1_hit_at"])
    print(f"Gotowe — zaktualizowano {len(preview)} setupów (result: TP2 -> TP1+TP2).")


if __name__ == "__main__":
    main()
