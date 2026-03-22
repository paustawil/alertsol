#!/usr/bin/env python3
"""
backtest.py — Symulacja setupów SOL/USDT dla godzin 1–9, 22.03.2026
Testuje wszystkie 3 modele (Algo, Claude, GPT) na danych historycznych.

Dla każdego snapshot'u godzinowego (dane dostępne do H:00):
  1. Algo  — algorytmiczna detekcja (algo_detect)
  2. Claude — Forteca v1.0 (Sonnet)
  3. GPT    — Forteca GPT (gpt-4o)

Wyniki symulacji lądują w arkuszach "Alerty_TEST" i "Wyniki_TEST"
(tworzone automatycznie jeśli nie istnieją).

Uruchamianie:
  python backtest.py [--no-llm]      # --no-llm pomija API Claude/GPT (szybciej)
"""

import os, sys, json, re, time, argparse

# Workaround: system cryptography (Debian) jest zepsute — używamy lokalnej kopii
_CRYPTO_FIX = "/tmp/cryptofix"
if _CRYPTO_FIX not in sys.path:
    os.makedirs(_CRYPTO_FIX, exist_ok=True)
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "cryptography",
                    "--target", _CRYPTO_FIX, "-q"], check=False)
    sys.path.insert(0, _CRYPTO_FIX)

import requests
import anthropic
import openai
import gspread
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

# ── Importujemy logikę z sol_alert ────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from sol_alert import (
    ANTHROPIC_KEY, OPENAI_KEY, SHEET_ID,
    FORTECA_PROMPT, FORTECA_GPT_PROMPT,
    detect_range, algo_detect, validate_setup,
    h1_trend, rr_calc, _hits,
    MIN_SCORE,
)

TZ            = ZoneInfo("Europe/Warsaw")
SYMBOL        = "SOLUSDT"
# Tydzień handlowy + sobota (pon–sob 17–22.03.2026)
TEST_DATES     = ["2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20", "2026-03-21", "2026-03-22"]
# Sesje EU (8–17) i US (14–22) Warsaw — co godzinę
SNAPSHOT_HOURS = list(range(8, 23))   # 8, 9, …, 22
# Ile świec M15 do przodu sprawdzamy wyniki (24h * 4 = 96)
FUTURE_CANDLES_LIMIT = 96
# Timeout na wejście (w świecach M15, 4 = 1h)
ENTRY_TIMEOUT_CANDLES = 16   # 4h
TRADE_TIMEOUT_CANDLES = 96   # 24h


# ── Fetch historycznych świec z CryptoCompare (z parametrem toTs) ──────────────
def fetch_klines_at(symbol: str, interval: str, limit: int, to_ts: int) -> list[dict]:
    """Pobiera `limit` świec kończących się <= to_ts."""
    endpoint, aggregate = {"15m": ("histominute", 15), "1h": ("histohour", 1)}[interval]
    fsym = symbol.replace("USDT", "").replace("USD", "")
    r = requests.get(
        f"https://min-api.cryptocompare.com/data/v2/{endpoint}",
        params={"fsym": fsym, "tsym": "USDT", "limit": limit,
                "aggregate": aggregate, "toTs": to_ts},
        timeout=15,
    )
    r.raise_for_status()
    return [
        {"time": d["time"], "open": float(d["open"]), "high": float(d["high"]),
         "low": float(d["low"]), "close": float(d["close"]), "volume": float(d["volumefrom"])}
        for d in r.json()["Data"]["Data"]
    ]


def snapshot_ts(date_str: str, hour: int) -> int:
    """Unix timestamp dla DATE HH:00:00 Warsaw time."""
    y, m, d = map(int, date_str.split("-"))
    dt = datetime(y, m, d, hour, 0, 0, tzinfo=TZ)
    return int(dt.timestamp())


# ── LLM wrappers (akceptują gotowe świece zamiast fetchować same) ──────────────
def call_claude_hist(candles_m15: list[dict], candles_h1: list[dict],
                     current_price: float) -> dict | None:
    if not ANTHROPIC_KEY:
        print("[claude] Brak klucza API."); return None
    try:
        m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
            f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
            for c in candles_m15[-60:]
        )
        h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
            f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
            for c in candles_h1[-24:]
        )
        user_msg = (f"Aktualna cena SOL: ${current_price:.2f}\n\n"
                    f"M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
                    f"H1 (ostatnie 24 swiece):\n{h1_csv}")
        client   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1500,
            system=FORTECA_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text  = response.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"[claude] Blad: {e}")
    return None


def call_gpt_hist(candles_m15: list[dict], candles_h1: list[dict],
                  current_price: float) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt] Brak klucza API."); return None
    try:
        m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
            f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
            for c in candles_m15[-60:]
        )
        h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
            f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
            for c in candles_h1[-24:]
        )
        user_msg = (f"Aktualna cena SOL: ${current_price:.2f}\n\n"
                    f"M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
                    f"H1 (ostatnie 24 swiece):\n{h1_csv}")
        client   = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model="gpt-4o", max_tokens=1024,
            messages=[
                {"role": "system", "content": FORTECA_GPT_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        text  = response.choices[0].message.content.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"[gpt] Blad: {e}")
    return None


# ── Symulacja wyniku setupu na przyszłych świecach ─────────────────────────────
def simulate_result(setup: dict, future_candles: list[dict]) -> dict:
    """
    Symuluje wynik setupu na dostarczonych świecach.
    Zwraca słownik z kluczami: result, entries_hit, entry_ts, exit_ts, pnl,
    eff_entry, eff_exit.
    """
    entries       = setup.get("entries", [])
    sl            = setup.get("sl")
    sl_after_tp1  = setup.get("sl_after_tp1")
    tps           = setup.get("tps", [])
    tp1           = tps[0] if tps else None
    tp2           = tps[1] if len(tps) > 1 else None
    d             = setup.get("direction", "long")
    w1            = entries[0] if entries else None

    if not entries or sl is None or w1 is None:
        return {"result": "brak_danych", "entries_hit": 0, "entry_ts": None,
                "exit_ts": None, "pnl": 0.0, "eff_entry": None, "eff_exit": None}

    # ── Szukamy wejścia ──
    entry_ts     = None
    for c in future_candles[:ENTRY_TIMEOUT_CANDLES]:
        if _hits(c, w1, d, "entry"):
            entry_ts = c["time"]; break

    if entry_ts is None:
        return {"result": "nie_weszlo", "entries_hit": 0, "entry_ts": None,
                "exit_ts": None, "pnl": 0.0, "eff_entry": None, "eff_exit": None}

    # ── Monitorujemy po wejściu ──
    after_entry  = [c for c in future_candles if c["time"] > entry_ts]
    result       = None
    exit_ts      = None
    tp1_hit_at   = None
    sl_adjusted  = False
    effective_sl = sl

    for c in after_entry[:TRADE_TIMEOUT_CANDLES]:
        sl_hit  = _hits(c, effective_sl, d, "sl")
        tp2_hit = tp2 is not None and _hits(c, tp2, d, "tp")
        tp1_now = tp1 is not None and _hits(c, tp1, d, "tp")

        if tp2_hit:
            result, exit_ts = "TP2", c["time"]; break

        if tp1_now and sl_hit and tp1_hit_at is None:
            result, exit_ts = "SL", c["time"]; break

        if tp1_now and tp1_hit_at is None:
            tp1_hit_at = c["time"]
            if sl_after_tp1 is not None and not sl_adjusted:
                effective_sl = sl_after_tp1
                sl_adjusted  = True
            continue

        if sl_hit:
            label  = ("TP1+BE" if sl_adjusted and sl_after_tp1 is not None
                                   and abs(effective_sl - w1) < 0.05
                      else "TP1+SL" if tp1_hit_at is not None
                      else "SL")
            result, exit_ts = label, c["time"]; break

    if result is None:
        return {"result": "timeout", "entries_hit": 1, "entry_ts": entry_ts,
                "exit_ts": None, "pnl": 0.0, "eff_entry": w1, "eff_exit": None}

    # ── Ile wejść zostało trafione? ──
    scan        = [c for c in after_entry if c["time"] <= exit_ts]
    entries_hit = 1
    if len(entries) > 1 and any(_hits(c, entries[1], d, "entry") for c in scan):
        entries_hit = 2

    active_entries = entries[:entries_hit]
    eff_entry      = sum(active_entries) / len(active_entries)

    # ── Średnia cen wyjść ──
    eff_sl_exit = effective_sl   # ostatni SL jaki był aktywny przy wyjściu
    if result == "SL":
        exit_prices = [sl]       # wyszło SL bez TP1
    elif result == "TP2":
        exit_prices = [tp1, tp2] if tp1 else [tp2]
    else:                        # TP1+BE lub TP1+SL
        exit_prices = [tp1, eff_sl_exit] if tp1 else [eff_sl_exit]

    eff_exit = sum(exit_prices) / len(exit_prices)
    pnl      = round((eff_exit - eff_entry) if d == "long"
                     else (eff_entry - eff_exit), 2)

    return {"result": result, "entries_hit": entries_hit, "entry_ts": entry_ts,
            "exit_ts": exit_ts, "pnl": pnl, "eff_entry": eff_entry, "eff_exit": eff_exit}


# ── Google Sheets ──────────────────────────────────────────────────────────────
def _get_test_sheets():
    creds  = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    wb     = client.open_by_key(SHEET_ID)

    ALERTY_HEADER = [
        "Snapshot", "Model", "Filter", "Typ", "Kierunek", "Score",
        "W1", "W2", "SL", "SL@TP1", "TP1", "TP2", "RR", "Reasoning",
    ]
    WYNIKI_HEADER = [
        "Snapshot", "Model", "Typ", "Kierunek", "Score",
        "W1", "SL", "TP1", "TP2", "RR",
        "Entries_hit", "Śr.Entry", "Śr.Exit",
        "Wejście o", "Wyjście o", "Wynik", "PnL $",
    ]

    for name, header, rows in [
        ("Alerty_TEST", ALERTY_HEADER, 500),
        ("Wyniki_TEST", WYNIKI_HEADER, 500),
    ]:
        try:
            sh = wb.worksheet(name)
            sh.clear()
            sh.append_row(header)
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet(name, rows=rows, cols=20)
            sh.append_row(header)
        if name == "Alerty_TEST":
            sh1 = sh
        else:
            sh2 = sh

    return sh1, sh2


def ts_to_str(ts: int | None) -> str:
    if ts is None: return "-"
    return datetime.fromtimestamp(ts, tz=TZ).strftime("%H:%M")


def log_alert(sh1, snapshot_label: str, model: str, passed: bool, setup: dict):
    entries = setup.get("entries", [])
    tps     = setup.get("tps", [])
    sh1.append_row([
        snapshot_label,
        model,
        "TAK" if passed else "NIE",
        setup.get("type", setup.get("setup_type", "-")),
        setup.get("direction", "-"),
        setup.get("total", setup.get("score", "-")),
        entries[0] if len(entries) > 0 else "-",
        entries[1] if len(entries) > 1 else "-",
        setup.get("sl", "-"),
        setup.get("sl_after_tp1", "-"),
        tps[0] if len(tps) > 0 else setup.get("tp1", "-"),
        tps[1] if len(tps) > 1 else setup.get("tp2", "-"),
        setup.get("rr", "-"),
        setup.get("reasoning", "-"),
    ])


def log_wynik(sh2, snapshot_label: str, model: str, setup: dict, sim: dict):
    entries = setup.get("entries", [])
    tps     = setup.get("tps", [])
    eff_entry = f"{sim['eff_entry']:.2f}" if sim["eff_entry"] is not None else "-"
    eff_exit  = f"{sim['eff_exit']:.2f}"  if sim["eff_exit"]  is not None else "-"
    n_w = sim["entries_hit"]
    sh2.append_row([
        snapshot_label,
        model,
        setup.get("type", setup.get("setup_type", "-")),
        setup.get("direction", "-"),
        setup.get("total", setup.get("score", "-")),
        entries[0] if entries else "-",
        setup.get("sl", "-"),
        tps[0] if tps else setup.get("tp1", "-"),
        tps[1] if len(tps) > 1 else setup.get("tp2", "-"),
        setup.get("rr", "-"),
        "+".join(f"W{i+1}" for i in range(n_w)) if n_w > 0 else "-",
        eff_entry,
        eff_exit,
        ts_to_str(sim["entry_ts"]),
        ts_to_str(sim["exit_ts"]),
        sim["result"],
        sim["pnl"] if sim["pnl"] != 0.0 or sim["result"] not in ("nie_weszlo", "brak_danych", "timeout") else "-",
    ])


# ── Normalizacja setupu z LLM do wspólnego formatu ────────────────────────────
def normalize_llm_setup(raw: dict) -> dict | None:
    """Konwertuje surową odpowiedź LLM do formatu zgodnego z algo_detect."""
    if not raw or not raw.get("setup_found"):
        return None
    tp1 = raw.get("tp1")
    tp2 = raw.get("tp2")
    tps = [t for t in [tp1, tp2] if t is not None]
    return {
        "type":         raw.get("setup_type", "-"),
        "direction":    raw.get("direction", "-"),
        "entries":      raw.get("entries", []),
        "sl":           raw.get("sl"),
        "sl_after_tp1": raw.get("sl_after_tp1"),
        "tps":          tps,
        "rr":           raw.get("rr", 0),
        "score":        raw.get("score", 0),
        "total":        raw.get("score", 0),
        "reasoning":    raw.get("reasoning", "-"),
    }


# ── Główna pętla backtestowa ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true",
                        help="Pomiń API Claude i GPT (tylko algo_detect)")
    args = parser.parse_args()

    dates_str = ", ".join(TEST_DATES)
    print(f"=== Backtest SOL | {dates_str} | sesje EU/US ({SNAPSHOT_HOURS[0]}–{SNAPSHOT_HOURS[-1]} Warsaw) ===")
    if args.no_llm:
        print("Tryb: tylko algorytm (--no-llm)")

    print("\nOtwieram arkusze testowe...")
    sh1, sh2 = _get_test_sheets()

    for test_date in TEST_DATES:
        # Pobieramy dane do końca ostatniego snapshotu + bufor na przyszłe świece
        end_ts = snapshot_ts(test_date, SNAPSHOT_HOURS[-1]) + 25 * 3600
        print(f"\n{'═'*55}")
        print(f"DZIEŃ: {test_date}")
        print(f"Pobieram dane M15 i H1...")

        # ~500 świec M15 = 125h kontekstu wstecz + przyszłe świece (pewny margines)
        all_m15 = fetch_klines_at(SYMBOL, "15m", 500, end_ts)
        # ~120 świec H1 = 5 dób kontekstu
        all_h1  = fetch_klines_at(SYMBOL, "1h",  120, end_ts)
        if all_m15:
            m15_from = datetime.fromtimestamp(all_m15[0]["time"],  tz=TZ).strftime("%d.%m %H:%M")
            m15_to   = datetime.fromtimestamp(all_m15[-1]["time"], tz=TZ).strftime("%d.%m %H:%M")
        else:
            m15_from = m15_to = "brak"
        print(f"  M15: {len(all_m15)} swiec ({m15_from} → {m15_to}) | H1: {len(all_h1)} swiec")

        for hour in SNAPSHOT_HOURS:
            snap_ts    = snapshot_ts(test_date, hour)
            snap_label = f"{test_date} {hour:02d}:00"
            print(f"\n{'─'*55}")
            print(f"SNAPSHOT {snap_label}")

            m15_snap = [c for c in all_m15 if c["time"] <= snap_ts][-60:]
            h1_snap  = [c for c in all_h1  if c["time"] <= snap_ts][-24:]

            if not m15_snap:
                print("  Brak danych M15, pomijam.")
                continue

            current_price = m15_snap[-1]["close"]
            print(f"  Cena: ${current_price:.2f}")

            future_m15 = [c for c in all_m15 if c["time"] > snap_ts]

            # ── 1. Algo ──
            rng         = detect_range(m15_snap)
            algo_setups = algo_detect(m15_snap, h1_snap, rng)
            print(f"  [Algo]   {len(algo_setups)} setup(ów)")
            for s in algo_setups:
                passed = validate_setup(s, "Algo") and s.get("total", 0) >= MIN_SCORE
                log_alert(sh1, snap_label, "Algo", passed, s)
                if passed:
                    sim = simulate_result(s, future_m15)
                    log_wynik(sh2, snap_label, "Algo", s, sim)
                    sign = "+" if sim["pnl"] >= 0 else ""
                    print(f"    {s['direction']:5s} {s['type']:12s} score={s['total']} "
                          f"→ {sim['result']:8s} {sign}{sim['pnl']:.2f}$")
                else:
                    print(f"    {s['direction']:5s} {s['type']:12s} score={s.get('total',0)} → [odrzucony]")

            time.sleep(0.5)

            # ── 2. Claude ──
            if not args.no_llm and ANTHROPIC_KEY:
                print("  [Claude] Wywołuję API...")
                raw_c   = call_claude_hist(m15_snap, h1_snap, current_price)
                setup_c = normalize_llm_setup(raw_c)
                if setup_c:
                    passed = validate_setup(setup_c, "Claude")
                    log_alert(sh1, snap_label, "Claude", passed, setup_c)
                    if passed:
                        sim = simulate_result(setup_c, future_m15)
                        log_wynik(sh2, snap_label, "Claude", setup_c, sim)
                        sign = "+" if sim["pnl"] >= 0 else ""
                        print(f"    {setup_c['direction']:5s} {setup_c['type']:12s} "
                              f"→ {sim['result']:8s} {sign}{sim['pnl']:.2f}$")
                    else:
                        print("    Setup Claude odrzucony przez walidację")
                else:
                    print("    Brak setupu (setup_found=false lub błąd)")
                    if raw_c and not raw_c.get("setup_found"):
                        log_alert(sh1, snap_label, "Claude", False,
                                  {"type": "-", "direction": "-", "entries": [],
                                   "reasoning": raw_c.get("reasoning", "-")})
                time.sleep(1.0)
            elif not args.no_llm:
                print("  [Claude] Pominięty — brak klucza API")

            # ── 3. GPT ──
            if not args.no_llm and OPENAI_KEY:
                print("  [GPT]    Wywołuję API...")
                raw_g   = call_gpt_hist(m15_snap, h1_snap, current_price)
                setup_g = normalize_llm_setup(raw_g)
                if setup_g:
                    passed = validate_setup(setup_g, "GPT")
                    log_alert(sh1, snap_label, "GPT", passed, setup_g)
                    if passed:
                        sim = simulate_result(setup_g, future_m15)
                        log_wynik(sh2, snap_label, "GPT", setup_g, sim)
                        sign = "+" if sim["pnl"] >= 0 else ""
                        print(f"    {setup_g['direction']:5s} {setup_g['type']:12s} "
                              f"→ {sim['result']:8s} {sign}{sim['pnl']:.2f}$")
                    else:
                        print("    Setup GPT odrzucony przez walidację")
                else:
                    print("    Brak setupu (setup_found=false lub błąd)")
                    if raw_g and not raw_g.get("setup_found"):
                        log_alert(sh1, snap_label, "GPT", False,
                                  {"type": "-", "direction": "-", "entries": [],
                                   "reasoning": raw_g.get("reasoning", "-")})
                time.sleep(1.0)
            elif not args.no_llm:
                print("  [GPT]    Pominięty — brak klucza API")

        time.sleep(2.0)  # pauza między dniami

    print(f"\n{'='*55}")
    print("Backtest zakończony. Arkusze: Alerty_TEST, Wyniki_TEST")


if __name__ == "__main__":
    main()
