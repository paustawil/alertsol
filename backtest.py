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


def _extract_first_json(text: str) -> dict:
    """Wyodrębnij pierwszy kompletny obiekt JSON z tekstu (śledzi głębokość nawiasów).

    Bezpieczniejsze niż greedy regex r"\\{.*\\}" z re.DOTALL, który lapie
    od pierwszego { do OSTATNIEGO } — i sypie się gdy Claude doda coś po JSON.
    """
    start = text.find('{')
    if start == -1:
        raise ValueError("Brak '{' w odpowiedzi")
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("Niekompletny obiekt JSON (niezamknięty '{')")

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
ENABLE_GPT    = True
# Tydzień handlowy + sobota (pon–sob 17–22.03.2026)
TEST_DATES     = ["2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20"]
# Definicje sesji (godziny Warsaw)
SESSION_HOURS = {
    "afternoon": list(range(14, 23)),   # 14:00–22:00  sesja US
    "morning":   list(range(8,  14)),   # 8:00–13:00   sesja EU (uzupełniająca)
    "full":      list(range(8,  23)),   # 8:00–22:00   obie sesje
}
# Ile świec M15 do przodu sprawdzamy wyniki (24h * 4 = 96)
FUTURE_CANDLES_LIMIT = 96
# Timeout na wejście (w świecach M15, 4 = 1h)
ENTRY_TIMEOUT_CANDLES = 16   # 4h
TRADE_TIMEOUT_CANDLES = 96   # 24h


# ── Fetch historycznych świec z CryptoCompare (z parametrem toTs i opcjonalnie fromTs) ──
def fetch_klines_at(symbol: str, interval: str, limit: int, to_ts: int,
                    from_ts: int | None = None) -> list[dict]:
    """Pobiera świece z zakresu (from_ts, to_ts]. limit=2000 to max CryptoCompare."""
    endpoint, aggregate = {"15m": ("histominute", 15), "1h": ("histohour", 1)}[interval]
    fsym = symbol.replace("USDT", "").replace("USD", "")
    params = {"fsym": fsym, "tsym": "USDT", "limit": limit,
              "aggregate": aggregate, "toTs": to_ts}
    if from_ts is not None:
        params["fromTs"] = from_ts
    r = requests.get(
        f"https://min-api.cryptocompare.com/data/v2/{endpoint}",
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    raw = r.json()["Data"]["Data"]
    # filtruj na wszelki wypadek — API może zwrócić więcej niż zakres
    if from_ts is not None:
        raw = [d for d in raw if d["time"] >= from_ts]
    return [
        {"time": d["time"], "open": float(d["open"]), "high": float(d["high"]),
         "low": float(d["low"]), "close": float(d["close"]), "volume": float(d["volumefrom"])}
        for d in raw
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
            model="claude-sonnet-4-6", max_tokens=2048,
            system=FORTECA_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()
        try:
            return _extract_first_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"[claude] Błąd parsowania JSON: {exc}\nRaw (300 zn.): {text[:300]!r}")
            return {"_error": str(exc), "_raw": text[:300]}
    except Exception as e:
        print(f"[claude] Blad API: {e}")
        return {"_error": str(e)}


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
            model="gpt-4o", max_tokens=2048,
            messages=[
                {"role": "system", "content": FORTECA_GPT_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        text = response.choices[0].message.content.strip()
        try:
            return _extract_first_json(text)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"[gpt] Błąd parsowania JSON: {exc}\nRaw (300 zn.): {text[:300]!r}")
            return {"_error": str(exc), "_raw": text[:300]}
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

    eff_exit   = sum(exit_prices) / len(exit_prices)
    pnl_unit   = round((eff_exit - eff_entry) if d == "long"
                       else (eff_entry - eff_exit), 2)
    stake_mult = 2 if entries_hit == 2 else 1
    pnl        = round(pnl_unit * stake_mult, 2)

    return {"result": result, "entries_hit": entries_hit, "entry_ts": entry_ts,
            "exit_ts": exit_ts, "pnl": pnl, "eff_entry": eff_entry, "eff_exit": eff_exit}


def simulate_tp1_only(setup: dict, future_candles: list[dict]) -> dict:
    """Symuluje strategię TP1-only: wychodzi w całości na TP1 (ignoruje TP2).
    Jeśli trafione W1+W2 — używa średniego entry do PnL."""
    entries = setup.get("entries", [])
    sl      = setup.get("sl")
    tps     = setup.get("tps", [])
    tp1     = tps[0] if tps else None
    d       = setup.get("direction", "long")
    w1      = entries[0] if entries else None
    w2      = entries[1] if len(entries) > 1 else None

    if not entries or sl is None or w1 is None or tp1 is None:
        return {"result": "brak_danych", "pnl": 0.0}

    entry_ts = None
    for c in future_candles[:ENTRY_TIMEOUT_CANDLES]:
        if _hits(c, w1, d, "entry"):
            entry_ts = c["time"]; break

    if entry_ts is None:
        return {"result": "nie_weszlo", "pnl": 0.0}

    after_entry = [c for c in future_candles if c["time"] > entry_ts]

    # Sprawdzamy czy W2 też zostało trafione przed TP1/SL
    w2_hit = False
    for c in after_entry[:TRADE_TIMEOUT_CANDLES]:
        if w2 is not None and not w2_hit and _hits(c, w2, d, "entry"):
            w2_hit = True
        if _hits(c, tp1, d, "tp"):
            eff_entry  = (w1 + w2) / 2 if w2_hit and w2 is not None else w1
            pnl_unit   = round((tp1 - eff_entry) if d == "long" else (eff_entry - tp1), 2)
            stake_mult = 2 if w2_hit else 1
            label      = "W1+W2→TP1" if w2_hit else "TP1"
            return {"result": label, "pnl": round(pnl_unit * stake_mult, 2)}
        if _hits(c, sl, d, "sl"):
            eff_entry  = (w1 + w2) / 2 if w2_hit and w2 is not None else w1
            pnl_unit   = round((sl - eff_entry) if d == "long" else (eff_entry - sl), 2)
            stake_mult = 2 if w2_hit else 1
            label      = "W1+W2→SL" if w2_hit else "SL"
            return {"result": label, "pnl": round(pnl_unit * stake_mult, 2)}

    return {"result": "timeout", "pnl": 0.0}


# ── Google Sheets ──────────────────────────────────────────────────────────────
def _get_test_sheets(reset: bool = False):
    creds  = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    wb     = client.open_by_key(SHEET_ID)

    ALERTY_HEADER = [
        "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
        "W1", "W2", "SL", "SL@TP1", "TP1", "TP2", "RR", "Reasoning",
    ]
    WYNIKI_HEADER = [
        "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
        "W1", "W2", "SL", "TP1", "TP2", "RR",
        "Entries_hit", "Śr.Entry", "Śr.Exit",
        "Wejście o", "Wyjście o", "Wynik (TP1+TP2)", "PnL $ (TP1+TP2)",
        "Wynik (TP1 only)", "PnL $ (TP1 only)", "Reasoning",
    ]

    for name, header, rows in [
        ("Alerty_TEST", ALERTY_HEADER, 500),
        ("Wyniki_TEST", WYNIKI_HEADER, 500),
    ]:
        try:
            sh = wb.worksheet(name)
            if reset:
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


def log_alert(sh1, snapshot_label: str, model: str, rejection: str, setup: dict):
    entries = setup.get("entries", [])
    tps     = setup.get("tps", [])
    sh1.append_row([
        snapshot_label,
        model,
        rejection or "OK",
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


def log_wynik(sh2, snapshot_label: str, model: str, rejection: str, setup: dict, sim: dict, sim_tp1: dict):
    entries = setup.get("entries", [])
    tps     = setup.get("tps", [])
    eff_entry = round(sim["eff_entry"], 2) if sim["eff_entry"] is not None else "-"
    eff_exit  = round(sim["eff_exit"],  2) if sim["eff_exit"]  is not None else "-"
    n_w = sim["entries_hit"]
    pnl_tp12  = sim["pnl"]     if sim["pnl"]     != 0.0 or sim["result"]     not in ("nie_weszlo", "brak_danych", "timeout") else "-"
    pnl_tp1   = sim_tp1["pnl"] if sim_tp1["pnl"] != 0.0 or sim_tp1["result"] not in ("nie_weszlo", "brak_danych", "timeout") else "-"
    sh2.append_row([
        snapshot_label,
        model,
        rejection or "OK",
        setup.get("type", setup.get("setup_type", "-")),
        setup.get("direction", "-"),
        setup.get("total", setup.get("score", "-")),
        entries[0] if entries else "-",
        entries[1] if len(entries) > 1 else "-",
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
        pnl_tp12,
        sim_tp1["result"],
        pnl_tp1,
        setup.get("reasoning", "-"),
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
        "pillars":      raw.get("pillars", {}),
    }


# ── Sprawdzenie kryteriów FORTeca (do Filtr_powód) ────────────────────────────
MIN_RR_THRESHOLD    = 1.6
MIN_LEVEL_PILLAR    = 2

def forteca_violations(setup: dict) -> str:
    """
    Zwraca wszystkie naruszone kryteria FORTeca oddzielone ' | '.
    Pusty string = setup spełnia wszystkie kryteria (pełne OK).
    Sprawdza: Score, Level pillar, RR, plus geometrię SL/TP.
    """
    reasons = []

    # 1. Score
    score = setup.get("total", setup.get("score", 0))
    if score < MIN_SCORE:
        reasons.append(f"Score<{MIN_SCORE} ({score})")

    # 2. Level pillar >= 2
    pillars = setup.get("pillars", {})
    lv = pillars.get("level", -1)
    if isinstance(lv, int) and lv >= 0 and lv < MIN_LEVEL_PILLAR:
        reasons.append(f"Level<{MIN_LEVEL_PILLAR} ({lv})")

    # 3. RR >= 1.6
    rr = setup.get("rr", 0)
    if isinstance(rr, (int, float)) and rr > 0 and rr < MIN_RR_THRESHOLD:
        reasons.append(f"RR<{MIN_RR_THRESHOLD} ({rr:.2f})")

    # 4. Geometria SL/TP (validate_setup)
    geo = validate_setup(setup, "")
    if geo:
        reasons.append(geo)

    return " | ".join(reasons)


# ── Pętla po godzinach jednej sesji ───────────────────────────────────────────
def run_session(test_date: str, hours: list[int],
                all_m15: list[dict], all_h1: list[dict],
                sh1, sh2, no_llm: bool, only_claude: bool = False) -> None:
    for hour in hours:
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
            rejection = forteca_violations(s)
            log_alert(sh1, snap_label, "Algo", rejection, s)
            sim     = simulate_result(s, future_m15)
            sim_tp1 = simulate_tp1_only(s, future_m15)
            log_wynik(sh2, snap_label, "Algo", rejection, s, sim, sim_tp1)
            sign = "+" if sim["pnl"] >= 0 else ""
            tag  = f"[FILTR: {rejection}] " if rejection else ""
            print(f"    {s['direction']:5s} {s['type']:12s} score={s['total']} "
                  f"{tag}→ {sim['result']:8s} {sign}{sim['pnl']:.2f}$ "
                  f"| TP1only: {sim_tp1['result']} {sim_tp1['pnl']:+.2f}$")

        time.sleep(0.5)

        # ── 2. Claude ──
        if not no_llm and ANTHROPIC_KEY:
            print("  [Claude] Wywołuję API...")
            try:
                raw_c   = call_claude_hist(m15_snap, h1_snap, current_price)
                setup_c = normalize_llm_setup(raw_c)
                if setup_c:
                    rejection = forteca_violations(setup_c)
                    log_alert(sh1, snap_label, "Claude", rejection, setup_c)
                    sim     = simulate_result(setup_c, future_m15)
                    sim_tp1 = simulate_tp1_only(setup_c, future_m15)
                    log_wynik(sh2, snap_label, "Claude", rejection, setup_c, sim, sim_tp1)
                    sign = "+" if sim["pnl"] >= 0 else ""
                    tag  = f"[FILTR: {rejection}] " if rejection else ""
                    print(f"    {setup_c['direction']:5s} {setup_c['type']:12s} "
                          f"{tag}→ {sim['result']:8s} {sign}{sim['pnl']:.2f}$ "
                          f"| TP1only: {sim_tp1['result']} {sim_tp1['pnl']:+.2f}$")
                else:
                    if raw_c is None:
                        reason = "błąd_API (None)"
                        print("    [Claude] BRAK ODPOWIEDZI (None) — błąd API lub timeout")
                    elif raw_c.get("_error"):
                        reason = f"błąd: {raw_c['_error']}"
                        raw_text = raw_c.get("_raw", "")
                        print(f"    [Claude] BŁĄD PARSOWANIA: {raw_c['_error']}")
                        print(f"    [Claude] Surowa odpowiedź: {raw_text[:400]!r}")
                    elif not raw_c.get("setup_found"):
                        reason = "brak_setupu"
                        reasoning_txt = raw_c.get("reasoning") or "-"
                        print(f"    [Claude] setup_found=false | {reasoning_txt[:120]}")
                    else:
                        reason = "brak_setupu"
                        print(f"    [Claude] Nieznany brak setupu: {str(raw_c)[:120]}")
                    # reasoning w arkuszu: przy błędzie pokaż surową odpowiedź Claude
                    if (raw_c or {}).get("_error"):
                        log_reasoning = f"[{reason}] raw: {(raw_c or {}).get('_raw', '')[:200]}"
                    else:
                        log_reasoning = (raw_c or {}).get("reasoning") or reason
                    log_alert(sh1, snap_label, "Claude", reason,
                              {"type": "-", "direction": "-", "entries": [],
                               "reasoning": log_reasoning})
            except Exception as exc:
                print(f"    [Claude] NIEOCZEKIWANY BŁĄD: {exc}")
                import traceback; traceback.print_exc()
                log_alert(sh1, snap_label, "Claude", f"wyjątek: {exc}",
                          {"type": "-", "direction": "-", "entries": [],
                           "reasoning": f"nieoczekiwany wyjątek: {exc}"})
            time.sleep(1.0)
        elif not no_llm:
            print("  [Claude] Pominięty — brak klucza API")

        # ── 3. GPT ──
        if ENABLE_GPT and not no_llm and not only_claude and OPENAI_KEY:
            print("  [GPT]    Wywołuję API...")
            raw_g   = call_gpt_hist(m15_snap, h1_snap, current_price)
            setup_g = normalize_llm_setup(raw_g)
            if setup_g:
                rejection = forteca_violations(setup_g)
                log_alert(sh1, snap_label, "GPT", rejection, setup_g)
                sim     = simulate_result(setup_g, future_m15)
                sim_tp1 = simulate_tp1_only(setup_g, future_m15)
                log_wynik(sh2, snap_label, "GPT", rejection, setup_g, sim, sim_tp1)
                sign = "+" if sim["pnl"] >= 0 else ""
                tag  = f"[FILTR: {rejection}] " if rejection else ""
                print(f"    {setup_g['direction']:5s} {setup_g['type']:12s} "
                      f"{tag}→ {sim['result']:8s} {sign}{sim['pnl']:.2f}$ "
                      f"| TP1only: {sim_tp1['result']} {sim_tp1['pnl']:+.2f}$")
            else:
                if raw_g is None:
                    reason = "błąd_API (None)"
                    print("    [GPT] BRAK ODPOWIEDZI (None) — błąd API lub timeout")
                elif raw_g.get("_error"):
                    reason = f"błąd: {raw_g['_error']}"
                    print(f"    [GPT] BŁĄD: {raw_g['_error']}")
                elif not raw_g.get("setup_found"):
                    reason = "brak_setupu"
                    print(f"    [GPT] setup_found=false | {raw_g.get('reasoning', '-')[:120]}")
                else:
                    reason = "brak_setupu"
                    print(f"    [GPT] Nieznany brak setupu: {str(raw_g)[:120]}")
                log_alert(sh1, snap_label, "GPT", reason,
                          {"type": "-", "direction": "-", "entries": [],
                           "reasoning": (raw_g or {}).get("reasoning", reason)})
            time.sleep(1.0)
        elif not ENABLE_GPT:
            pass  # GPT wyłączone (ENABLE_GPT = False)
        elif not no_llm and not only_claude:
            print("  [GPT]    Pominięty — brak klucza API")


# ── Główna pętla backtestowa ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true",
                        help="Pomiń API Claude i GPT (tylko algo_detect)")
    parser.add_argument("--only-claude", action="store_true",
                        help="Wywołuj tylko Claude (pomiń GPT)")
    parser.add_argument("--session", choices=["afternoon", "morning", "full"],
                        default="full",
                        help="Sesja: afternoon (14–22) | morning (8–13) | full (obie, domyślnie)")
    parser.add_argument("--reset", action="store_true",
                        help="Wyczyść arkusze TEST przed zapisem (domyślnie: dopisuje)")
    parser.add_argument("--date-from", default=None,
                        help="Pierwszy dzień zakresu YYYY-MM-DD (domyślnie: pierwszy z TEST_DATES)")
    parser.add_argument("--date-to", default=None,
                        help="Ostatni dzień zakresu YYYY-MM-DD (domyślnie: ostatni z TEST_DATES)")
    args = parser.parse_args()

    dates = TEST_DATES
    if args.date_from or args.date_to:
        d_from = args.date_from or TEST_DATES[0]
        d_to   = args.date_to   or TEST_DATES[-1]
        dates  = [d for d in TEST_DATES if d_from <= d <= d_to]
        if not dates:
            print(f"Brak dat w zakresie {d_from}–{d_to}. Dostępne: {', '.join(TEST_DATES)}")
            return

    dates_str = ", ".join(dates)
    print(f"=== Backtest SOL | {dates_str} ===")
    if args.no_llm:
        print("Tryb: tylko algorytm (--no-llm)")
    if args.only_claude:
        print("Tryb: tylko Claude (--only-claude, GPT pominięty)")
    if args.reset:
        print("Tryb: --reset — arkusze TEST zostaną wyczyszczone przed zapisem")

    print("\nOtwieram arkusze testowe...")
    sh1, sh2 = _get_test_sheets(reset=args.reset)

    # W trybie full: chronologicznie morning (8–13) → afternoon (14–22)
    sessions_to_run = (
        [("morning",   SESSION_HOURS["morning"]),
         ("afternoon", SESSION_HOURS["afternoon"])]
        if args.session == "full"
        else [(args.session, SESSION_HOURS[args.session])]
    )

    for sess_name, sess_hours in sessions_to_run:
        label = {"afternoon": "Sesja 14:00–22:00 (US)", "morning": "Sesja 8:00–13:00 (EU)"}[sess_name]
        print(f"\n{'█'*55}")
        print(f"  {label}")
        print(f"{'█'*55}")

        for test_date in dates:
            first_hour = min(sess_hours)
            last_hour  = max(sess_hours)
            # from_ts: 16h przed pierwszym snapshotem (kontekst historyczny)
            # end_ts:  25h po ostatnim snapshocie (symulacja wyników trade)
            from_ts = snapshot_ts(test_date, first_hour) - 16 * 3600
            end_ts  = snapshot_ts(test_date, last_hour)  + 25 * 3600
            # 2000 = max CryptoCompare histominute; z aggregate=15 to ~20 dni historii
            m15_limit = 2000
            h1_limit  = min(500, (end_ts - from_ts) // 3600 + 5)

            print(f"\n{'═'*55}")
            print(f"DZIEŃ: {test_date}")
            print("Pobieram dane M15 i H1...")

            all_m15 = fetch_klines_at(SYMBOL, "15m", m15_limit, end_ts, from_ts)
            all_h1  = fetch_klines_at(SYMBOL, "1h",  h1_limit,  end_ts, from_ts)

            if all_m15:
                m15_from = datetime.fromtimestamp(all_m15[0]["time"],  tz=TZ).strftime("%d.%m %H:%M")
                m15_to   = datetime.fromtimestamp(all_m15[-1]["time"], tz=TZ).strftime("%d.%m %H:%M")
            else:
                m15_from = m15_to = "brak"
            print(f"  M15: {len(all_m15)} swiec ({m15_from} → {m15_to}) | H1: {len(all_h1)} swiec")

            run_session(test_date, sess_hours, all_m15, all_h1, sh1, sh2,
                        args.no_llm, only_claude=args.only_claude)

            time.sleep(2.0)  # pauza między dniami

        print(f"\n{'─'*55}")
        print(f"  {label} — zakończona.")

    print(f"\n{'='*55}")
    print("Backtest zakończony. Arkusze: Alerty_TEST, Wyniki_TEST")


if __name__ == "__main__":
    main()
