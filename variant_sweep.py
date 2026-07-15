#!/usr/bin/env python3
"""
variant_sweep.py — Wielokrotne odpalenie Symulatora portfela (patrz static/index.html
ViewSimulator, /api/simulator w main_runner.py) z różnymi datami startu, żeby ocenić
warianty/kombinacje niezależnie od "szczęścia" jednego okna czasowego.

Używa PRAWDZIWYCH, rozstrzygniętych setupów z bazy (db.get_simulator_trades()) — nie
generuje nowych setupów z historycznych świec (to robi backtest_variants.py, inne
narzędzie). Equity curve jest portem 1:1 logiki z ViewSimulator (compounding kapitału,
blokada nakładających się pozycji, drawdown) — bez wypłat okresowych (uproszczenie:
harmonogram wypłat jest zakotwiczony w realnym kalendarzu, więc nie przenosi się
sensownie na przesunięte okna historyczne; jeśli potrzebne, można dodać później).

Co robi:
  1. Dla każdego wariantu (pary type+variant): przesuwa 30-dniowe okno dzień po dniu,
     od najwcześniejszej dostępnej daty do (dziś − 30 dni), i zbiera best/avg/worst
     zwrotu % na koniec okna.
  2. To samo, ale jedno stałe okno 90-dniowe (dziś − 90 dni → dziś) — tylko dla
     wariantów mających co najmniej 90 dni historii (na razie mało która to spełnia).
  3. Ranking wariantów po (2) i (1) — best/avg/worst, posortowany po najgorszym wyniku
     (żeby faworyzować warianty stabilne, a nie tylko "raz się udało").
  4. Dla top N wariantów: wszystkie kombinacje rozmiaru 1-4 (wspólny kapitał, limit
     jednej pozycji na raz — tak jak wybór kilku wariantów jednocześnie w Symulatorze),
     ten sam sweep 30-dniowy, ranking kombinacji.

Użycie:
  python variant_sweep.py [--capital 1000] [--pnl-mode tp12|tp1] [--top-n 8]
                          [--step-days 1] [--min-regime-score 3]
                          [--out-prefix variant_sweep]

Wymaga: psycopg2 (już w projekcie), dostęp do tej samej bazy co main_runner.py.
"""

import argparse
import csv
import itertools
from datetime import datetime, timedelta, timezone
from statistics import mean

import db

WINDOW_30D = 30
WINDOW_90D = 90


# ── Equity curve (port 1:1 logiki ViewSimulator z static/index.html) ─────────

def simulate_equity(trades: list[dict], start_capital: float, pnl_mode: str) -> dict:
    """trades musi być posortowane rosnąco po entry_time (tak zwraca db.get_simulator_trades)."""
    capital = start_capital
    current_exit_time = None
    wins = losses = skipped = entered = 0
    max_capital = capital
    max_drawdown = 0.0

    for t in trades:
        entry_time = t["entry_time"]
        exit_time = t["exit_time"]
        pnl_pct = t["tp1_only_pnl_pct"] if pnl_mode == "tp1" else t["pnl_pct"]

        if current_exit_time is not None and entry_time < current_exit_time:
            skipped += 1
            continue
        current_exit_time = exit_time
        if pnl_pct is None:
            continue
        pnl_pct = float(pnl_pct)
        capital += capital * (pnl_pct / 100)
        if capital > max_capital:
            max_capital = capital
        dd = (max_capital - capital) / max_capital * 100 if max_capital > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd
        entered += 1
        if pnl_pct > 0:
            wins += 1
        else:
            losses += 1

    return_pct = (capital - start_capital) / start_capital * 100
    return {
        "final_capital": round(capital, 2),
        "return_pct": round(return_pct, 2),
        "trades": entered, "wins": wins, "losses": losses, "skipped": skipped,
        "max_drawdown_pct": round(max_drawdown, 2),
    }


# ── Ładowanie i grupowanie danych ─────────────────────────────────────────────

def _to_naive_utc(dt: datetime | None) -> datetime | None:
    """db.get_simulator_trades() zwraca entry_time jako naiwny timestamp (Postgres
    "AT TIME ZONE 'UTC'" na timestamptz daje timestamp bez strefy), ale exit_time bywa
    timezone-aware — COALESCE(exit_time, <wyrażenie AT TIME ZONE>) w tamtym zapytaniu
    ujednolica typ do timestamptz, gdy sama kolumna exit_time jest timestamptz. Mieszanie
    naiwnych i aware datetime w porównaniach rzuca TypeError, więc normalizujemy tu."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def load_trades_by_pair(min_regime_score: int | None = None) -> dict[tuple[str, str], list[dict]]:
    """Jedno zapytanie do bazy, potem grupowanie w Pythonie — szybsze i prostsze niż
    osobne zapytanie na każde okno/kombinację."""
    all_trades = db.get_simulator_trades(min_regime_score=min_regime_score)
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for t in all_trades:
        t["entry_time"] = _to_naive_utc(t.get("entry_time"))
        t["exit_time"] = _to_naive_utc(t.get("exit_time"))
        if t["entry_time"] is None or t["exit_time"] is None:
            continue
        key = (t.get("type") or "unknown", t.get("variant") or "baseline")
        by_pair.setdefault(key, []).append(t)
    return by_pair


def merge_trades(*trade_lists: list[dict]) -> list[dict]:
    merged = [t for lst in trade_lists for t in lst]
    merged.sort(key=lambda t: t["entry_time"])
    return merged


# ── Sweep 30-dniowy (wszystkie możliwe daty startu) ──────────────────────────

def sweep_window(trades: list[dict], window_days: int, step_days: int,
                  today: datetime, start_capital: float, pnl_mode: str,
                  first_date=None) -> dict | None:
    """first_date: nadpisuje datę "od kiedy wariant/kombinacja jest dostępna". Domyślnie
    (pojedynczy wariant) to najwcześniejszy trade. Dla kombinacji musi to być MAX
    (najpóźniejsza) z dat startu poszczególnych składników — inaczej wczesne okna
    testowałyby tylko część kombinacji (te warianty, które już wystartowały), a nie
    prawdziwe jednoczesne działanie wszystkich naraz."""
    if not trades:
        return None
    if first_date is None:
        first_date = min(t["entry_time"] for t in trades).date()
    last_start = (today - timedelta(days=window_days)).date()
    if first_date > last_start:
        return {"eligible": False, "reason": f"za mało historii (od {first_date}, potrzeba {window_days}d)"}

    results = []
    d = first_date
    while d <= last_start:
        win_start = datetime(d.year, d.month, d.day)
        win_end = win_start + timedelta(days=window_days)
        window_trades = [t for t in trades if win_start <= t["entry_time"] < win_end]
        sim = simulate_equity(window_trades, start_capital, pnl_mode)
        results.append({"start": d.isoformat(), **sim})
        d += timedelta(days=step_days)

    returns = [r["return_pct"] for r in results]
    best = max(results, key=lambda r: r["return_pct"])
    worst = min(results, key=lambda r: r["return_pct"])
    return {
        "eligible": True, "n_windows": len(results),
        "best_pct": best["return_pct"], "best_start": best["start"],
        "worst_pct": worst["return_pct"], "worst_start": worst["start"],
        "avg_pct": round(mean(returns), 2),
    }


def run_90d(trades: list[dict], today: datetime, start_capital: float, pnl_mode: str) -> dict | None:
    if not trades:
        return None
    first_date = min(t["entry_time"] for t in trades).date()
    win_start_date = (today - timedelta(days=WINDOW_90D)).date()
    if first_date > win_start_date:
        return {"eligible": False, "reason": f"za mało historii (od {first_date}, potrzeba {WINDOW_90D}d)"}

    win_start = datetime(win_start_date.year, win_start_date.month, win_start_date.day)
    window_trades = [t for t in trades if win_start <= t["entry_time"] < today]
    sim = simulate_equity(window_trades, start_capital, pnl_mode)
    return {"eligible": True, **sim}


# ── Główny przebieg ───────────────────────────────────────────────────────────

def pair_label(pair: tuple[str, str]) -> str:
    return f"{pair[0]}:{pair[1]}"


def run_sweep(capital: float = 1000.0, pnl_mode: str = "tp12", top_n: int = 8,
              step_days: int = 1, min_regime_score: int | None = None) -> dict:
    # naive UTC — spójne z entry_time/exit_time zwracanymi przez db.get_simulator_trades()
    # (kolumny konwertowane w SQL przez "AT TIME ZONE 'UTC'", psycopg2 zwraca je bez tzinfo)
    today = datetime.now(timezone.utc).replace(tzinfo=None)
    by_pair = load_trades_by_pair(min_regime_score)

    singles = []
    for pair, trades in sorted(by_pair.items()):
        w30 = sweep_window(trades, WINDOW_30D, step_days, today, capital, pnl_mode)
        w90 = run_90d(trades, today, capital, pnl_mode)
        first_trade_date = min(t["entry_time"] for t in trades).date()
        last_trade_date = max(t["entry_time"] for t in trades).date()
        singles.append({
            "pair": pair, "label": pair_label(pair), "n_trades": len(trades),
            "first_trade_date": first_trade_date, "last_trade_date": last_trade_date,
            "history_days": (today.date() - first_trade_date).days,
            "w30": w30, "w90": w90,
        })

    # Ranking: tylko warianty z wystarczającą historią na sweep 30d, posortowane
    # po najgorszym wyniku (worst_pct) malejąco — patrz CLAUDE.md/prośba: "każda
    # z wartości (best/avg/worst) możliwie wysoka", więc faworyzujemy odporność
    # na zły punkt startu zamiast tylko najlepszego przypadku.
    eligible = [s for s in singles if s["w30"] and s["w30"]["eligible"]]
    eligible.sort(key=lambda s: s["w30"]["worst_pct"], reverse=True)

    # data startu każdego wariantu — potrzebna do wyznaczenia daty startu kombinacji (MAX)
    first_date_by_pair = {s["pair"]: s["first_trade_date"] for s in singles}

    top_pairs = [s["pair"] for s in eligible[:top_n]]
    combos = []
    for r in range(1, min(4, len(top_pairs)) + 1):
        for combo in itertools.combinations(top_pairs, r):
            trades = merge_trades(*[by_pair[p] for p in combo])
            combo_first_date = max(first_date_by_pair[p] for p in combo)
            w30 = sweep_window(trades, WINDOW_30D, step_days, today, capital, pnl_mode,
                                first_date=combo_first_date)
            if not w30 or not w30["eligible"]:
                continue
            combos.append({
                "combo": combo, "label": " + ".join(pair_label(p) for p in combo),
                "size": r, "n_trades": len(trades), "w30": w30,
            })
    combos.sort(key=lambda c: c["w30"]["worst_pct"], reverse=True)

    return {
        "today": today.isoformat(), "capital": capital, "pnl_mode": pnl_mode,
        "singles": singles, "singles_ranked": eligible, "combos": combos,
    }


# ── Wyjście: CSV + podsumowanie w konsoli ────────────────────────────────────

def _print_history(singles: list[dict]) -> None:
    """Ile historii ma KAŻDY wariant/typ setupu (nie tylko trend_pullback) — odpowiedź
    na 'ile historii mają range/impulse/itp.', niezależnie od tego czy się kwalifikują
    do sweepu 30d/90d."""
    print("\n" + "=" * 100)
    print("Historia danych per typ+wariant (wszystkie typy setupów, nie tylko trend_pullback):")
    print(f"{'Wariant':<45} {'N':>5} {'Od':>12} {'Do':>12} {'Dni historii':>13}")
    print("-" * 100)
    for s in sorted(singles, key=lambda s: -s["history_days"]):
        print(f"{s['label']:<45} {s['n_trades']:>5} {str(s['first_trade_date']):>12} "
              f"{str(s['last_trade_date']):>12} {s['history_days']:>13}")
    print("=" * 100)


def _print_singles(ranked: list[dict]) -> None:
    print("\n" + "=" * 100)
    print(f"{'Wariant':<45} {'N':>5} {'Okna':>5} {'Worst%':>8} {'Avg%':>8} {'Best%':>8}")
    print("-" * 100)
    for s in ranked:
        w30 = s["w30"]
        print(f"{s['label']:<45} {s['n_trades']:>5} {w30['n_windows']:>5} "
              f"{w30['worst_pct']:>+8.1f} {w30['avg_pct']:>+8.1f} {w30['best_pct']:>+8.1f}")
    print("=" * 100)


def _print_not_eligible(singles: list[dict]) -> None:
    skipped = [s for s in singles if not (s["w30"] and s["w30"]["eligible"])]
    if not skipped:
        return
    print("\nPominięte (za mało historii na sweep 30d):")
    for s in skipped:
        reason = s["w30"]["reason"] if s["w30"] else "brak rozstrzygniętych trade'ów"
        print(f"  {s['label']}: {reason}")


def _print_90d(singles: list[dict]) -> None:
    rows = [s for s in singles if s["w90"] and s["w90"]["eligible"]]
    print("\n" + "=" * 100)
    print("Okno 90-dniowe (dziś − 90d → dziś), tylko warianty z wystarczającą historią:")
    if not rows:
        print("  (żaden wariant nie ma jeszcze 90 dni historii)")
    else:
        print(f"{'Wariant':<45} {'Trades':>7} {'Return%':>9} {'MaxDD%':>8}")
        print("-" * 100)
        for s in rows:
            w90 = s["w90"]
            print(f"{s['label']:<45} {w90['trades']:>7} {w90['return_pct']:>+9.1f} {w90['max_drawdown_pct']:>8.1f}")
    print("=" * 100)


def _print_combos(combos: list[dict], limit: int = 20) -> None:
    print("\n" + "=" * 100)
    print(f"Top {min(limit, len(combos))} kombinacji (1-4 warianty), sweep 30-dniowy:")
    print(f"{'Kombinacja':<70} {'Okna':>5} {'Worst%':>8} {'Avg%':>8} {'Best%':>8}")
    print("-" * 100)
    for c in combos[:limit]:
        w30 = c["w30"]
        print(f"{c['label']:<70} {w30['n_windows']:>5} "
              f"{w30['worst_pct']:>+8.1f} {w30['avg_pct']:>+8.1f} {w30['best_pct']:>+8.1f}")
    print("=" * 100)


def _write_singles_csv(singles: list[dict], path: str) -> None:
    rows = []
    for s in singles:
        w30, w90 = s["w30"] or {}, s["w90"] or {}
        rows.append({
            "type": s["pair"][0], "variant": s["pair"][1], "n_trades": s["n_trades"],
            "first_trade_date": s["first_trade_date"], "last_trade_date": s["last_trade_date"],
            "history_days": s["history_days"],
            "w30_eligible": w30.get("eligible", False),
            "w30_n_windows": w30.get("n_windows"),
            "w30_worst_pct": w30.get("worst_pct"), "w30_worst_start": w30.get("worst_start"),
            "w30_avg_pct": w30.get("avg_pct"),
            "w30_best_pct": w30.get("best_pct"), "w30_best_start": w30.get("best_start"),
            "w90_eligible": w90.get("eligible", False),
            "w90_return_pct": w90.get("return_pct"),
            "w90_max_drawdown_pct": w90.get("max_drawdown_pct"),
        })
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def _write_combos_csv(combos: list[dict], path: str) -> None:
    rows = [{
        "size": c["size"], "combo": c["label"], "n_trades": c["n_trades"],
        "w30_n_windows": c["w30"]["n_windows"],
        "w30_worst_pct": c["w30"]["worst_pct"], "w30_avg_pct": c["w30"]["avg_pct"],
        "w30_best_pct": c["w30"]["best_pct"],
    } for c in combos]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Sweep symulatora portfela po datach startu i kombinacjach wariantów")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--pnl-mode", choices=["tp12", "tp1"], default="tp12")
    ap.add_argument("--top-n", type=int, default=8, help="ile najlepszych wariantów bierze udział w kombinacjach")
    ap.add_argument("--step-days", type=int, default=1, help="co ile dni przesuwać okno startu (1 = każdy możliwy dzień)")
    ap.add_argument("--min-regime-score", type=int, default=None)
    ap.add_argument("--out-prefix", type=str, default="variant_sweep")
    args = ap.parse_args()

    result = run_sweep(capital=args.capital, pnl_mode=args.pnl_mode, top_n=args.top_n,
                        step_days=args.step_days, min_regime_score=args.min_regime_score)

    _print_history(result["singles"])
    _print_singles(result["singles_ranked"])
    _print_not_eligible(result["singles"])
    _print_90d(result["singles"])
    _print_combos(result["combos"])

    _write_singles_csv(result["singles"], f"{args.out_prefix}_singles.csv")
    _write_combos_csv(result["combos"], f"{args.out_prefix}_combos.csv")
    print(f"\nZapisano: {args.out_prefix}_singles.csv, {args.out_prefix}_combos.csv")


if __name__ == "__main__":
    main()
