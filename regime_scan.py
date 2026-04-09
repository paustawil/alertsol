"""
Prosty skaner reżimu — wypisuje co godzinę jaki reżim był wykrywany.
Użycie:
    python regime_scan.py --from "2026-04-01 00:00" --to "2026-04-09 23:00"
"""
import argparse
import sys
from diagnose_regime import (
    detect_regime_new, fetch_klines_paginated,
    _ts_fmt, _parse_dt,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="dt_from", default="2026-04-01 00:00")
    parser.add_argument("--to",   dest="dt_to",   default="2026-04-09 23:00")
    args = parser.parse_args()

    from_ts = _parse_dt(args.dt_from)
    to_ts   = _parse_dt(args.dt_to)
    num_hours = (to_ts - from_ts) // 3600

    print(f"Okres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)} ({num_hours}h)\n")

    print("Pobieranie M15...")
    m15_total = 100 + (to_ts - from_ts) // 900 + 50
    all_m15 = fetch_klines_paginated("SOLUSDT", "15m", total=m15_total, end_ts_s=to_ts + 3600)
    print(f"  Pobrano {len(all_m15)} świec M15")

    print("Pobieranie H1...")
    h1_total = 60 + num_hours + 30
    all_h1 = fetch_klines_paginated("SOLUSDT", "1h", total=h1_total, end_ts_s=to_ts + 3600)
    print(f"  Pobrano {len(all_h1)} świec H1\n")

    test_hours = [from_ts + i * 3600 for i in range(num_hours + 1)]

    print(f"{'Czas':<18} {'Cena':>8}  {'Reżim':<16}  {'4h':>6}  {'24h':>6}  {'48h':>6}  {'ll':>3}  {'hh':>3}  {'details'}")
    print("-" * 110)

    for signal_ts in test_hours:
        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 20 or len(ctx_h1) < 10:
            continue

        price = ctx_m15[-1]["close"]

        # ll/hh z ostatnich 12 H1
        h1_12 = ctx_h1[-12:]
        ll = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
        hh = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

        r = detect_regime_new(ctx_m15, ctx_h1, price)

        print(
            f"{_ts_fmt(signal_ts):<18} ${price:>7.2f}  {r['regime']:<16}  "
            f"{r['change_4h']:>+5.1f}%  {r['change_24h']:>+5.1f}%  {r['change_48h']:>+5.1f}%  "
            f"{ll:>3}  {hh:>3}  {r.get('details','')}"
        )


if __name__ == "__main__":
    main()
