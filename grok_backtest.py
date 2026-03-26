#!/usr/bin/env python3
"""
grok_backtest.py — jednorazowy backtest 12 historycznych alertów Groka (26.03.2026)

Pobiera dane M15 z CryptoCompare i symuluje każdy setup:
  - Entry: zależnie od entry_trigger (falling/rising)
  - Exit:  TP1 → SL przesuwa się, TP2 lub SL lub timeout
  - Wyniki trafiają do arkusza "Wyniki" (ten sam co produkcyjny)

Uruchamianie:
  python grok_backtest.py [--dry-run]   # --dry-run tylko drukuje, nie pisze do arkusza
"""

import os, sys, json, argparse
import requests
import gspread
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(__file__))
from sol_alert import (
    SHEET_ID, TZ, TRADE_TIMEOUT_H, ENTRY_TIMEOUT_H,
    _hits, log_to_wyniki,
)

# ── Historyczne alerty Groka (26.03.2026, czas warszawski = UTC+1) ────────────

def _ts(date_str: str) -> int:
    """Konwertuje 'YYYY-MM-DD HH:MM' (Warsaw UTC+1) na Unix timestamp."""
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Warsaw"))
    return int(dt.timestamp())


GROK_SETUPS = [
    {
        "alert_time":      "2026-03-26T03:16:00+00:00",
        "alert_timestamp": _ts("2026-03-26 04:16"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 65,
        "kurs": 91.04, "price_at_alert": 91.04,
        "warunek": "przebicie", "entry_trigger": "falling",
        "entries": [90.90], "sl": 91.80, "sl_after_tp1": None,
        "tps": [89.00, 87.00], "rr": 2.1,
        "reasoning": "Czekam na break poniżej 90.70 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T03:31:00+00:00",
        "alert_timestamp": _ts("2026-03-26 04:31"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 65,
        "kurs": 90.70, "price_at_alert": 90.70,
        "warunek": "przebicie", "entry_trigger": "falling",
        "entries": [90.50], "sl": 91.20, "sl_after_tp1": None,
        "tps": [89.00, 87.50], "rr": 2.0,
        "reasoning": "Czekam na break poniżej 90.60 i wchodzę short przy 90.50",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T05:16:00+00:00",
        "alert_timestamp": _ts("2026-03-26 06:16"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "long", "score": 65,
        "kurs": 89.97, "price_at_alert": 89.97,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [90.50], "sl": 89.80, "sl_after_tp1": None,
        "tps": [91.50, 93.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 90.50 i wchodzę long po potwierdzeniu",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T05:31:00+00:00",
        "alert_timestamp": _ts("2026-03-26 06:31"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 70,
        "kurs": 90.03, "price_at_alert": 90.03,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [90.50], "sl": 91.00, "sl_after_tp1": None,
        "tps": [89.00, 88.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 90.50 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T05:46:00+00:00",
        "alert_timestamp": _ts("2026-03-26 06:46"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 70,
        "kurs": 89.43, "price_at_alert": 89.43,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [90.00], "sl": 90.80, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 90.00 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T06:01:00+00:00",
        "alert_timestamp": _ts("2026-03-26 07:01"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 70,
        "kurs": 89.15, "price_at_alert": 89.15,
        "warunek": "przebicie", "entry_trigger": "falling",
        "entries": [89.00], "sl": 90.00, "sl_after_tp1": None,
        "tps": [87.50, 86.00], "rr": 2.0,
        "reasoning": "Czekam na breakdown poniżej 89.00 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T06:16:00+00:00",
        "alert_timestamp": _ts("2026-03-26 07:16"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 65,
        "kurs": 89.22, "price_at_alert": 89.22,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [89.50], "sl": 90.00, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 89.50 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T06:31:00+00:00",
        "alert_timestamp": _ts("2026-03-26 07:31"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 75,
        "kurs": 89.02, "price_at_alert": 89.02,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [89.50], "sl": 90.50, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 89.50 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T06:46:00+00:00",
        "alert_timestamp": _ts("2026-03-26 07:46"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 65,
        "kurs": 89.00, "price_at_alert": 89.00,
        "warunek": "przebicie", "entry_trigger": "falling",
        "entries": [88.90], "sl": 89.50, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na break below 89.00 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T07:01:00+00:00",
        "alert_timestamp": _ts("2026-03-26 08:01"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 70,
        "kurs": 89.03, "price_at_alert": 89.03,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [89.50], "sl": 90.20, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 89.50 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T07:17:00+00:00",
        "alert_timestamp": _ts("2026-03-26 08:17"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 70,
        "kurs": 89.14, "price_at_alert": 89.14,
        "warunek": "przebicie", "entry_trigger": "rising",
        "entries": [89.20], "sl": 89.80, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 89.20 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
    {
        "alert_time":      "2026-03-26T07:31:00+00:00",
        "alert_timestamp": _ts("2026-03-26 08:31"),
        "model": "Grok", "rejection": "", "type": "",
        "direction": "short", "score": 70,
        "kurs": 88.99, "price_at_alert": 88.99,
        "warunek": "pullback", "entry_trigger": "rising",
        "entries": [89.20], "sl": 90.00, "sl_after_tp1": None,
        "tps": [88.00, 87.00], "rr": 2.5,
        "reasoning": "Czekam na pullback do 89.20 i wchodzę short",
        "entry_hit_at": None, "tp1_hit_at": None, "sl_adjusted": False, "entries_hit": 1,
    },
]


# ── Pobieranie danych historycznych ──────────────────────────────────────────

def fetch_m15_history(limit: int = 300) -> list[dict]:
    """Pobiera ostatnie `limit` świec M15 z CryptoCompare."""
    r = requests.get(
        "https://min-api.cryptocompare.com/data/v2/histominute",
        params={"fsym": "SOL", "tsym": "USDT", "limit": limit, "aggregate": 15},
        timeout=15,
    )
    r.raise_for_status()
    return [
        {"time": d["time"], "open": float(d["open"]), "high": float(d["high"]),
         "low": float(d["low"]), "close": float(d["close"])}
        for d in r.json()["Data"]["Data"]
    ]


# ── Symulacja pojedynczego setupu ─────────────────────────────────────────────

def simulate_setup(s: dict, candles: list[dict]) -> tuple[str, float | None, float | None, int | None, int | None]:
    """
    Zwraca (result, eff_entry, eff_exit, entry_ts, exit_ts).
    result: 'TP2' | 'TP1+SL' | 'TP1+BE' | 'SL' | 'nie weszlo' | 'nieokreslone'
    """
    w1  = s["entries"][0]
    sl  = s["sl"]
    tp1 = s["tps"][0] if s["tps"] else None
    tp2 = s["tps"][1] if len(s["tps"]) > 1 else None
    d   = s["direction"]
    et  = s.get("entry_trigger")

    after_alert = [c for c in candles if c["time"] > s["alert_timestamp"]]

    # Szukaj wejścia
    entry_ts = next((c["time"] for c in after_alert if _hits(c, w1, d, "entry", et)), None)

    if entry_ts is None:
        age_h = (candles[-1]["time"] - s["alert_timestamp"]) / 3600
        return ("nie weszlo" if age_h > ENTRY_TIMEOUT_H else "nieokreslone"), None, None, None, None

    after_entry = [c for c in candles if c["time"] > entry_ts]
    result, exit_ts = None, None
    tp1_hit_at    = None
    effective_sl  = sl
    sl_after_tp1  = s.get("sl_after_tp1")

    for c in after_entry:
        sl_hit  = _hits(c, effective_sl, d, "sl")
        tp2_hit = tp2 and _hits(c, tp2, d, "tp")
        tp1_now = tp1 and _hits(c, tp1, d, "tp")

        if tp2_hit:
            result, exit_ts = "TP2", c["time"]; break

        if tp1_now and sl_hit and tp1_hit_at is None:
            result, exit_ts = "SL", c["time"]; break

        if tp1_now and tp1_hit_at is None:
            tp1_hit_at = c["time"]
            if sl_after_tp1 is not None:
                effective_sl = sl_after_tp1
            continue

        if sl_hit:
            label = ("TP1+BE" if tp1_hit_at and sl_after_tp1 is not None and abs(effective_sl - w1) < 0.05
                     else "TP1+SL" if tp1_hit_at
                     else "SL")
            result, exit_ts = label, c["time"]; break

    if result is None:
        age_h = (candles[-1]["time"] - entry_ts) / 3600
        result = "nieokreslone" if age_h <= TRADE_TIMEOUT_H else "nieokreslone"

    # PnL
    eff_entry, eff_exit = None, None
    if entry_ts:
        eff_entry = w1
        eff_sl_exit = effective_sl
        if result == "SL":
            eff_exit = sl
        elif result == "TP2":
            eff_exit = (tp1 + tp2) / 2 if tp1 else tp2
        elif result in ("TP1+SL", "TP1+BE"):
            eff_exit = (tp1 + eff_sl_exit) / 2 if tp1 else eff_sl_exit
        elif result == "nieokreslone":
            eff_exit = None

    move = None
    if eff_entry and eff_exit:
        move = round((eff_entry - eff_exit) if d == "short" else (eff_exit - eff_entry), 2)

    return result, eff_entry, eff_exit, entry_ts, exit_ts


# ── Zapis do arkusza Alerty (jednorazowe wstawienie historycznych wierszy) ───

def insert_alerty(setups: list[dict], dry_run: bool):
    """Wstawia historyczne alerty do arkusza Alerty."""
    if dry_run:
        print("[alerty] dry-run — pomijam zapis do arkusza")
        return
    try:
        creds  = Credentials.from_service_account_info(
            json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        wb     = client.open_by_key(SHEET_ID)
        sh     = wb.worksheet("Alerty")
        for s in setups:
            ts  = datetime.fromisoformat(s["alert_time"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
            w1  = s["entries"][0] if s["entries"] else ""
            tp1 = s["tps"][0] if s["tps"] else ""
            tp2 = s["tps"][1] if len(s["tps"]) > 1 else ""
            sh.append_row([
                ts,
                "Grok",
                "",
                "",
                s["direction"],
                f"{s['score']}%",
                s["kurs"],
                w1,
                "",
                s["warunek"],
                s["sl"],
                "",
                tp1,
                tp2,
                s["rr"],
                s["reasoning"],
            ])
        print(f"[alerty] Zapisano {len(setups)} wierszy do arkusza Alerty.")
    except Exception as e:
        print(f"[alerty] Błąd: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Tylko wydrukuj wyniki — nie zapisuj do arkusza")
    parser.add_argument("--results-only", action="store_true",
                        help="Pomiń wstawianie alertów do Alerty (przy ponownym uruchomieniu)")
    args = parser.parse_args()

    print("Pobieranie danych M15 (ostatnie 300 świec ≈ 75h)...")
    candles = fetch_m15_history(limit=300)
    print(f"Pobrano {len(candles)} świec | zakres: "
          f"{datetime.utcfromtimestamp(candles[0]['time']).strftime('%m-%d %H:%M')} – "
          f"{datetime.utcfromtimestamp(candles[-1]['time']).strftime('%m-%d %H:%M')} UTC")

    # Wstaw alerty do arkusza Alerty (jednorazowo — pomiń przy --results-only)
    if not args.results_only:
        insert_alerty(GROK_SETUPS, args.dry_run)
    else:
        print("[alerty] Pominięto wstawianie do Alerty (--results-only)")

    print("\n{'='*72}")
    print(f"{'Czas alert':14} {'Kier':6} {'Score':6} {'Kurs':7} {'W1':7} {'Warunek':12} {'Wynik':12} {'Entry':7} {'Exit':7} {'PnL':>7}")
    print("-" * 90)

    total_pnl = 0.0
    results_for_sheet = []

    for s in GROK_SETUPS:
        result, eff_entry, eff_exit, entry_ts, exit_ts = simulate_setup(s, candles)

        alert_dt  = datetime.fromisoformat(s["alert_time"]).astimezone(TZ).strftime("%m-%d %H:%M")
        entry_str = datetime.utcfromtimestamp(entry_ts).astimezone(TZ).strftime("%H:%M") if entry_ts else "-"
        exit_str  = datetime.utcfromtimestamp(exit_ts).astimezone(TZ).strftime("%H:%M")  if exit_ts  else "-"
        move      = round((s["entries"][0] - eff_exit) if s["direction"] == "short" and eff_exit else
                          (eff_exit - s["entries"][0]) if eff_exit else 0, 2) if eff_exit else 0

        sign = "+" if move > 0 else ""
        print(f"{alert_dt:14} {s['direction']:6} {s['score']:>4}%  ${s['kurs']:.2f}  "
              f"${s['entries'][0]:.2f}  {s['warunek']:12} {result:12} {entry_str:7} {exit_str:7} "
              f"{sign}${move:.2f}" if eff_exit else
              f"{alert_dt:14} {s['direction']:6} {s['score']:>4}%  ${s['kurs']:.2f}  "
              f"${s['entries'][0]:.2f}  {s['warunek']:12} {result:12} {entry_str:7} {exit_str:7}   -")

        total_pnl += move
        results_for_sheet.append((s, result, eff_entry, eff_exit, entry_ts, exit_ts, move))

    sign = "+" if total_pnl >= 0 else ""
    print("-" * 90)
    print(f"{'ŁĄCZNIE':>76}  {sign}${total_pnl:.2f}")

    # Zapisz wyniki do arkusza Wyniki
    if not args.dry_run:
        print("\nZapisuję wyniki do arkusza Wyniki...")
        ok, fail = 0, 0
        for s, result, eff_entry, eff_exit, entry_ts, exit_ts, move in results_for_sheet:
            if result not in ("nieokreslone",) and eff_entry is not None:
                if log_to_wyniki(s, result, entry_ts, exit_ts, eff_entry, eff_exit, move):
                    ok += 1
                else:
                    fail += 1
            elif result in ("nie weszlo",):
                if log_to_wyniki(s, result, None, None, None, None, 0):
                    ok += 1
                else:
                    fail += 1
        print(f"Wyniki: {ok} zapisanych, {fail} błędów.")
    else:
        print("\n[dry-run] Wyniki nie zostały zapisane do arkusza.")


if __name__ == "__main__":
    main()
