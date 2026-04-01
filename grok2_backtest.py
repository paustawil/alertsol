"""
Grok2 backtest — porównanie Grok (obecny prompt) vs Grok2 (nowy prompt ze strukturą).

Ostatnie 48 godzin, jedno zapytanie na każdą pełną godzinę.
Oba modele dostają identyczne dane OHLCV; Grok2 dodatkowo otrzymuje sentyment i pozycję w zakresie.
Wyniki zapisywane do arkuszy 'Grok test' i 'Grok2 test' w tym samym skoroszycie.

Uruchomienie:
    python grok2_backtest.py
    python grok2_backtest.py --grok2-only      # pomiń stary Grok (oszczędność kredytów)
"""

import concurrent.futures
import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from google.oauth2.service_account import Credentials
import gspread

# ── Konfiguracja ─────────────────────────────────────────────────────────────
SYMBOL        = "SOLUSDT"
SHEET_ID      = "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE"
XAI_KEY       = os.getenv("XAI_API_KEY", "")
GROK_MODEL    = "grok-4"
GROK_TIMEOUT_S = 120

ENTRY_WINDOW_S   = 24 * 3600
OUTCOME_WINDOW_S = 24 * 3600

SHEET_HEADER = [
    "Data i godzina", "Kierunek", "Pewność", "W", "TP1", "TP2", "SL",
    "Wynik", "Czas do entry", "Delta (TP1+TP2)", "DeltaTP1",
]


# ── Prompty (importowane z sol_alert.py) ─────────────────────────────────────
from sol_alert import GROK_PROMPT, GROK2_PROMPT


# ── Pobieranie danych sentymentu ─────────────────────────────────────────────

def fetch_bitget_price(symbol: str) -> float | None:
    """Pobiera aktualną cenę z tickera Bitget futures."""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/ticker",
            params={"symbol": symbol, "productType": "USDT-FUTURES"},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        if data:
            return float(data[0]["lastPr"])
    except Exception as e:
        print(f"[sentiment] Błąd pobierania ceny {symbol}: {e}")
    return None


def fetch_fear_greed_history(days: int = 30) -> dict[str, tuple[int, str]]:
    """Pobiera historię F&G z alternative.me. Zwraca {data_YYYY-MM-DD: (wartość, etykieta)}."""
    result: dict[str, tuple[int, str]] = {}
    try:
        r = requests.get(
            "https://api.alternative.me/fng/",
            params={"limit": str(days), "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        for entry in r.json().get("data", []):
            ts = int(entry["timestamp"])
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            result[date_str] = (int(entry["value"]), entry["value_classification"])
    except Exception as e:
        print(f"[sentiment] Błąd pobierania historii F&G: {e}")
    print(f"  F&G: pobrano {len(result)} dni historii")
    return result


def fetch_price_history(symbol: str, total: int, end_ts_s: int) -> list[dict]:
    """Pobiera historyczne świece H1 dla BTC/ETH z Bitget."""
    return fetch_klines_paginated(symbol, "1h", total=total, end_ts_s=end_ts_s)


def build_sentiment_line_historical(
    btc_candles: list[dict],
    eth_candles: list[dict],
    fg_history: dict[str, tuple[int, str]],
    signal_ts: int,
) -> str:
    """Buduje linię sentymentu z historycznych danych dla danego punktu czasowego."""
    # BTC/ETH — ostatnia zamknięta świeca H1 przed signal_ts
    btc_price = None
    for c in reversed(btc_candles):
        if c["time"] <= signal_ts:
            btc_price = c["close"]
            break

    eth_price = None
    for c in reversed(eth_candles):
        if c["time"] <= signal_ts:
            eth_price = c["close"]
            break

    # F&G — wartość z tego dnia (F&G publikuje raz dziennie)
    signal_date = datetime.fromtimestamp(signal_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    fg_val, fg_label = fg_history.get(signal_date, (None, None))

    # Jeśli brak dokładnej daty, szukaj najbliższej wcześniejszej
    if fg_val is None:
        for offset_days in range(1, 4):
            prev_date = datetime.fromtimestamp(signal_ts - offset_days * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
            if prev_date in fg_history:
                fg_val, fg_label = fg_history[prev_date]
                break

    parts = []
    if btc_price:
        parts.append(f"BTC ${btc_price:,.0f}")
    if eth_price:
        parts.append(f"ETH ${eth_price:,.0f}")
    if fg_val is not None:
        parts.append(f"Fear & Greed: {fg_val}/100 ({fg_label})")

    return " | ".join(parts) if parts else "brak danych sentymentu"


# Zachowane dla kompatybilności (nie używane w backteście)
def build_sentiment_line() -> str:
    """Buduje linię sentymentu z aktualnych danych Bitget + F&G."""
    btc = fetch_bitget_price("BTCUSDT")
    eth = fetch_bitget_price("ETHUSDT")
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1&format=json", timeout=5)
        r.raise_for_status()
        entry = r.json()["data"][0]
        fg_val, fg_label = int(entry["value"]), entry["value_classification"]
    except Exception:
        fg_val, fg_label = None, None

    parts = []
    if btc:
        parts.append(f"BTC ${btc:,.0f}")
    if eth:
        parts.append(f"ETH ${eth:,.0f}")
    if fg_val is not None:
        parts.append(f"Fear & Greed: {fg_val}/100 ({fg_label})")

    return " | ".join(parts) if parts else "brak danych sentymentu"


# ── Detect range (identycznie jak w sol_alert.py) ───────────────────────────

def detect_range(candles: list[dict], n: int = 32) -> dict:
    recent     = candles[-n:]
    resistance = max(c["high"] for c in recent)
    support    = min(c["low"]  for c in recent)
    rng_size   = resistance - support
    return {
        "resistance": round(resistance, 2),
        "support":    round(support, 2),
        "range_size": round(rng_size, 2),
    }


# ── Bitget: pobieranie świec historycznych (paginacja wstecz) ────────────────

def fetch_klines_paginated(symbol: str, interval: str, total: int, end_ts_s: int | None = None) -> list[dict]:
    """Zwraca `total` świec interwału `interval` kończących się PRZED end_ts_s (lub teraz)."""
    granularity = {"15m": "15m", "1h": "1H"}[interval]
    interval_s  = {"15m": 900,   "1h": 3600}[interval]
    result: list[dict] = []
    end_ms = (end_ts_s * 1000) if end_ts_s else None

    while len(result) < total:
        params: dict = {
            "symbol":      symbol,
            "productType": "USDT-FUTURES",
            "granularity": granularity,
            "limit":       str(min(total - len(result), 200)),
        }
        if end_ms:
            params["endTime"] = str(end_ms)

        try:
            r = requests.get(
                "https://api.bitget.com/api/v2/mix/market/candles",
                params=params,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            print(f"[fetch] Błąd API: {e}")
            break

        if not data:
            break

        batch = [
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
        batch.sort(key=lambda c: c["time"])
        result = batch + result
        oldest_ts_ms = batch[0]["time"] * 1000
        end_ms = oldest_ts_ms - (interval_s * 1000)

        if len(batch) < 2:
            break

    seen: set[int] = set()
    deduped = []
    for c in result:
        if c["time"] not in seen:
            seen.add(c["time"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["time"])
    return deduped[-total:] if len(deduped) > total else deduped


# ── Grok API call ────────────────────────────────────────────────────────────

def call_grok_raw(system_prompt: str, user_msg: str, use_web_search: bool = True, label: str = "grok") -> dict | None:
    """Wywołuje Grok z podanym system promptem i user message."""
    if not XAI_KEY:
        print(f"[{label}] Brak klucza XAI_API_KEY.")
        return None

    def _call() -> str:
        from xai_sdk import Client as XaiClient
        from xai_sdk.chat import system as xai_system, user as xai_user
        from xai_sdk.tools import web_search
        client = XaiClient(api_key=XAI_KEY)
        tools = [web_search()] if use_web_search else []
        chat = client.chat.create(model=GROK_MODEL, tools=tools)
        chat.append(xai_system(system_prompt))
        chat.append(xai_user(user_msg))
        return chat.sample().content.strip()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                text = future.result(timeout=GROK_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"[{label}] Timeout — brak odpowiedzi w ciagu {GROK_TIMEOUT_S}s")
                future.cancel()
                return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        else:
            print(f"[{label}] Brak JSON w odpowiedzi: {text[:200]}")
    except Exception as e:
        print(f"[{label}] Błąd API: {e}")
    return None


# ── Budowanie user message ───────────────────────────────────────────────────

def build_user_msg_grok(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> str:
    """User message dla starego Groka (bez sentymentu — sam pobiera przez web search)."""
    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-60:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-24:]
    )
    return (
        f"Aktualna cena SOL z moich danych: ${current_price:.2f}\n\n"
        f"SOL M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
        f"SOL H1 (ostatnie 24 swiece):\n{h1_csv}"
    )


def build_user_msg_grok2(
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
    sentiment_line: str,
    range_info: dict,
) -> str:
    """User message dla Grok2 (z sentymentem, pozycją w zakresie, bez web search)."""
    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-60:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-24:]
    )

    # Pozycja w zakresie
    rng_size = range_info["range_size"]
    if rng_size > 0:
        range_pos = (current_price - range_info["support"]) / rng_size * 100
        range_pos = max(0.0, min(100.0, range_pos))
    else:
        range_pos = 50.0

    if range_pos > 80:
        range_label = "blisko resistance"
    elif range_pos < 20:
        range_label = "blisko supportu"
    else:
        range_label = "środek zakresu"

    return (
        f"Aktualne dane z Bitget: {sentiment_line}\n"
        f"Aktualna cena SOL: ${current_price:.2f}\n\n"
        f"Zakres H1 (ostatnie 32 świece): support ${range_info['support']:.2f} — resistance ${range_info['resistance']:.2f} "
        f"(range ${rng_size:.2f})\n"
        f"Pozycja ceny w zakresie: {range_pos:.0f}% ({range_label})\n\n"
        f"SOL M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
        f"SOL H1 (ostatnie 24 swiece):\n{h1_csv}"
    )


# ── Ewaluacja wyniku (identyczna z gpt3_backtest.py) ────────────────────────

def _round_to_quarter(hours: float) -> float:
    return round(hours * 4) / 4


def evaluate_outcome(
    result: dict,
    future_m15: list[dict],
    signal_ts: int,
) -> dict:
    direction = result.get("bias", "neutral")
    wejscia   = result.get("wejscia", [])
    entries   = [w["poziom"] for w in wejscia if "poziom" in w]
    tp1 = result.get("tp1")
    tp2 = result.get("tp2")
    sl  = result.get("sl")
    sl_after_tp1 = result.get("sl_after_tp1")

    empty = {"entry_activated": False, "entry_ts": None, "entry_price": None,
             "wynik": "no entry", "czas_do_entry_h": None, "delta": None, "delta_tp1": None}

    if not entries or tp1 is None or sl is None:
        return empty

    entry_deadline = signal_ts + ENTRY_WINDOW_S

    # Szukaj aktywacji wejścia
    entry_ts = None
    entry_price = None
    for c in future_m15:
        if c["time"] > entry_deadline:
            break
        for lvl in entries:
            if c["low"] <= lvl <= c["high"]:
                entry_ts = c["time"]
                entry_price = lvl
                break
        if entry_ts:
            break

    if entry_ts is None:
        return empty

    czas_h = _round_to_quarter((entry_ts - signal_ts) / 3600)

    # Śledź wynik
    outcome_deadline = entry_ts + OUTCOME_WINDOW_S
    tp1_hit = False
    final_wynik = "SL"

    for c in future_m15:
        if c["time"] <= entry_ts:
            continue
        if c["time"] > outcome_deadline:
            break

        if not tp1_hit:
            if direction == "long":
                tp1_hit_now = c["high"] >= tp1
                sl_hit_now  = c["low"]  <= sl
            else:
                tp1_hit_now = c["low"]  <= tp1
                sl_hit_now  = c["high"] >= sl

            if tp1_hit_now and sl_hit_now:
                if direction == "long":
                    tp1_hit_now = (tp1 - entry_price) <= (entry_price - sl)
                else:
                    tp1_hit_now = (entry_price - tp1) <= (sl - entry_price)

            if sl_hit_now and not tp1_hit_now:
                final_wynik = "SL"
                break
            if tp1_hit_now:
                tp1_hit = True
                if tp2 is None:
                    final_wynik = "TP1+BE"
                    break
        else:
            sl_guard = sl_after_tp1 if sl_after_tp1 is not None else entry_price
            if direction == "long":
                tp2_hit_now = tp2 is not None and c["high"] >= tp2
                sl_hit_now  = c["low"] <= sl_guard
            else:
                tp2_hit_now = tp2 is not None and c["low"] <= tp2
                sl_hit_now  = c["high"] >= sl_guard

            if tp2_hit_now and sl_hit_now:
                tp2_hit_now = True

            if sl_hit_now and not tp2_hit_now:
                final_wynik = "TP1+BE"
                break
            if tp2_hit_now:
                final_wynik = "TP1+TP2"
                break
    else:
        if tp1_hit:
            final_wynik = "TP1+BE"
        else:
            final_wynik = "SL"

    # Delta
    avg_entry = entry_price
    if final_wynik == "TP1+TP2" and tp1 is not None and tp2 is not None:
        avg_exit = (tp1 + tp2) / 2
    elif final_wynik == "TP1+BE":
        sl_guard = sl_after_tp1 if sl_after_tp1 is not None else entry_price
        avg_exit = (tp1 + sl_guard) / 2 if tp1 is not None else entry_price
    else:
        avg_exit = sl

    if direction == "long":
        delta = round(avg_exit - avg_entry, 4)
    else:
        delta = round(avg_entry - avg_exit, 4)

    # DeltaTP1
    delta_tp1: float | None = None
    for c in future_m15:
        if c["time"] <= entry_ts:
            continue
        if c["time"] > entry_ts + OUTCOME_WINDOW_S:
            break
        if direction == "long":
            tp1_hit_c = c["high"] >= tp1
            sl_hit_c  = c["low"]  <= sl
        else:
            tp1_hit_c = c["low"]  <= tp1
            sl_hit_c  = c["high"] >= sl

        if tp1_hit_c and sl_hit_c:
            if direction == "long":
                tp1_hit_c = (tp1 - avg_entry) <= (avg_entry - sl)
            else:
                tp1_hit_c = (avg_entry - tp1) <= (sl - avg_entry)

        if sl_hit_c and not tp1_hit_c:
            exit_tp1 = sl
            break
        if tp1_hit_c:
            exit_tp1 = tp1
            break
    else:
        exit_tp1 = sl

    if direction == "long":
        delta_tp1 = round(exit_tp1 - avg_entry, 4)
    else:
        delta_tp1 = round(avg_entry - exit_tp1, 4)

    return {
        "entry_activated": True,
        "entry_ts":        entry_ts,
        "entry_price":     entry_price,
        "wynik":           final_wynik,
        "czas_do_entry_h": czas_h,
        "delta":           delta,
        "delta_tp1":       delta_tp1,
    }


# ── Google Sheets ────────────────────────────────────────────────────────────

def get_test_sheet(sheet_name: str) -> gspread.Worksheet:
    creds = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    wb = client.open_by_key(SHEET_ID)
    try:
        sh = wb.worksheet(sheet_name)
        sh.clear()
    except gspread.WorksheetNotFound:
        sh = wb.add_worksheet(sheet_name, rows=200, cols=len(SHEET_HEADER) + 2)
    sh.append_row(SHEET_HEADER)
    return sh


# ── Przetwarzanie wyniku i zapis do arkusza ──────────────────────────────────

def process_and_write(
    label: str,
    model_label: str,
    grok_result: dict | None,
    future_m15: list[dict],
    signal_ts: int,
    sheet: gspread.Worksheet,
) -> dict | None:
    """Przetwarza wynik Groka, ewaluuje outcome i zapisuje do arkusza. Zwraca outcome."""
    if grok_result is None:
        print(f"  [{model_label}] Brak odpowiedzi.")
        sheet.append_row([label, "", "", "", "", "", "", f"błąd {model_label}", "", ""])
        return None

    send_alert = grok_result.get("send_alert", False)
    bias       = grok_result.get("bias", "neutral")
    bias_proc  = grok_result.get("bias_proc", 0)

    print(f"  [{model_label}] send_alert={send_alert} | bias={bias} ({bias_proc}%)")

    if not send_alert or bias == "neutral":
        sheet.append_row([label, "null", bias_proc, "", "", "", "", "no entry", "", ""])
        return {"wynik": "no entry", "delta": None, "delta_tp1": None}

    tp1 = grok_result.get("tp1", "")
    tp2 = grok_result.get("tp2", "")
    sl  = grok_result.get("sl",  "")
    wejscia = grok_result.get("wejscia", [])
    entries = [w["poziom"] for w in wejscia if "poziom" in w]
    avg_w   = round(sum(entries) / len(entries), 4) if entries else ""

    outcome = evaluate_outcome(grok_result, future_m15, signal_ts)

    wynik         = outcome["wynik"]
    czas_str      = f"{outcome['czas_do_entry_h']}h" if outcome["czas_do_entry_h"] is not None else ""
    delta_val     = outcome["delta"]     if outcome["delta"]     is not None else ""
    delta_tp1_val = outcome["delta_tp1"] if outcome["delta_tp1"] is not None else ""

    print(f"  [{model_label}] Wynik: {wynik} | delta: {delta_val} | deltaTP1: {delta_tp1_val}")

    kierunek = bias.upper()
    sheet.append_row([
        label,
        kierunek,
        bias_proc,
        avg_w,
        tp1,
        tp2,
        sl,
        wynik,
        czas_str,
        delta_val,
        delta_tp1_val,
    ])

    return outcome


# ── Główna logika backtestu ──────────────────────────────────────────────────

def _parse_dt(s: str) -> int:
    """Parsuje datę 'YYYY-MM-DD HH:MM' (UTC) na unix timestamp."""
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def run_backtest() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--grok2-only", action="store_true",
                        help="Pomiń stary Grok — testuj tylko Grok2 (oszczędność kredytów)")
    parser.add_argument("--from", dest="dt_from", type=str, default=None,
                        help="Początek okresu UTC, np. '2026-03-25 00:00'")
    parser.add_argument("--to", dest="dt_to", type=str, default=None,
                        help="Koniec okresu UTC, np. '2026-03-28 00:00'")
    parser.add_argument("--hours", type=int, default=None,
                        help="Ile godzin wstecz od --to (lub od teraz). Ignorowane gdy podano --from.")
    parser.add_argument("--sheet-suffix", type=str, default="",
                        help="Sufiks nazwy arkusza (np. 'v2' → 'Grok2 test v2')")
    args = parser.parse_args()

    run_grok1 = not args.grok2_only
    now_ts = int(time.time())

    # ── Wyznacz zakres from/to ───────────────────────────────────────────────
    if args.dt_from and args.dt_to:
        from_ts = _parse_dt(args.dt_from)
        to_ts   = _parse_dt(args.dt_to)
    elif args.dt_from:
        from_ts = _parse_dt(args.dt_from)
        to_ts   = (now_ts // 3600) * 3600
    elif args.dt_to:
        to_ts   = _parse_dt(args.dt_to)
        hours   = args.hours or 48
        from_ts = to_ts - hours * 3600
    else:
        to_ts   = (now_ts // 3600) * 3600
        hours   = args.hours or 48
        from_ts = to_ts - hours * 3600

    # Zaokrąglij do pełnych godzin
    from_ts = ((from_ts + 3599) // 3600) * 3600  # ceil
    to_ts   = (to_ts // 3600) * 3600              # floor

    num_hours = (to_ts - from_ts) // 3600
    if num_hours <= 0:
        print(f"[BŁĄD] Nieprawidłowy zakres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)}")
        return

    print("=== Grok2 Backtest — start ===")
    if run_grok1:
        print("Tryb: Grok (stary) vs Grok2 (nowy)")
    else:
        print("Tryb: tylko Grok2 (--grok2-only)")
    print(f"Okres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)} ({num_hours}h, {num_hours} punktów)")

    # ── 1. Pobierz dane historyczne ──────────────────────────────────────────
    # Kontekst PRZED from_ts: 60 M15 (~15h) + 50 H1 (~2d)
    # Outcome PO to_ts: 24h (ENTRY_WINDOW + OUTCOME_WINDOW)
    outcome_margin_s = ENTRY_WINDOW_S + OUTCOME_WINDOW_S  # 48h
    data_end_ts = to_ts + outcome_margin_s

    # Nie możemy pobrać świec z przyszłości
    if data_end_ts > now_ts:
        data_end_ts = now_ts
        margin_h = (data_end_ts - to_ts) / 3600
        print(f"  ⚠ Outcome data ograniczona do {margin_h:.0f}h po ostatnim punkcie (brak przyszłych danych)")

    m15_total = 60 + num_hours * 4 + (data_end_ts - to_ts) // 900 + 50
    h1_total  = 50 + num_hours + (data_end_ts - to_ts) // 3600 + 10

    print(f"Pobieranie świec M15 ({m15_total} szt)...")
    all_m15 = fetch_klines_paginated(SYMBOL, "15m", total=m15_total, end_ts_s=data_end_ts)
    print(f"  Pobrano {len(all_m15)} świec M15 ({_ts_fmt(all_m15[0]['time'])} – {_ts_fmt(all_m15[-1]['time'])})")

    print(f"Pobieranie świec H1 ({h1_total} szt)...")
    all_h1 = fetch_klines_paginated(SYMBOL, "1h", total=h1_total, end_ts_s=data_end_ts)
    print(f"  Pobrano {len(all_h1)} świec H1 ({_ts_fmt(all_h1[0]['time'])} – {_ts_fmt(all_h1[-1]['time'])})")

    # ── 2. Pobierz historyczne dane sentymentu ─────────────────────────────────
    print("Pobieranie historycznych danych sentymentu...")

    # F&G — historia dzienna
    fg_days = (data_end_ts - from_ts) // 86400 + 5  # zapas
    fg_history = fetch_fear_greed_history(days=max(fg_days, 30))

    # BTC/ETH — historyczne świece H1 (ten sam zakres co SOL)
    btc_h1_total = h1_total
    eth_h1_total = h1_total

    print(f"Pobieranie świec BTC H1 ({btc_h1_total} szt)...")
    btc_h1 = fetch_klines_paginated("BTCUSDT", "1h", total=btc_h1_total, end_ts_s=data_end_ts)
    print(f"  BTC H1: {len(btc_h1)} świec ({_ts_fmt(btc_h1[0]['time'])} – {_ts_fmt(btc_h1[-1]['time'])})")

    print(f"Pobieranie świec ETH H1 ({eth_h1_total} szt)...")
    eth_h1 = fetch_klines_paginated("ETHUSDT", "1h", total=eth_h1_total, end_ts_s=data_end_ts)
    print(f"  ETH H1: {len(eth_h1)} świec ({_ts_fmt(eth_h1[0]['time'])} – {_ts_fmt(eth_h1[-1]['time'])})")

    # ── 3. Wyznacz punkty testowe ────────────────────────────────────────────
    test_hours = [from_ts + i * 3600 for i in range(num_hours)]

    # ── 4. Przygotuj arkusze ─────────────────────────────────────────────────
    sfx = f" {args.sheet_suffix}" if args.sheet_suffix else ""
    sheet_name_grok2 = f"Grok2 test{sfx}"
    sheet_name_grok  = f"Grok test{sfx}"
    print(f"Łączenie z Google Sheets ({sheet_name_grok2})...")
    sheet_grok2 = get_test_sheet(sheet_name_grok2)
    sheet_grok  = get_test_sheet(sheet_name_grok) if run_grok1 else None
    print("Gotowe.")

    # ── 5. Statystyki ────────────────────────────────────────────────────────
    stats = {"grok": {"alerts": 0, "entries": 0, "delta_sum": 0.0, "delta_tp1_sum": 0.0},
             "grok2": {"alerts": 0, "entries": 0, "delta_sum": 0.0, "delta_tp1_sum": 0.0}}

    # ── 6. Pętla testowa ─────────────────────────────────────────────────────
    errors = 0
    for i, signal_ts in enumerate(test_hours):
        label = _ts_fmt(signal_ts)
        print(f"\n[{i+1}/{num_hours}] {label}")

        try:
            # Kontekst świec
            ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-60:]
            ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-50:]

            if len(ctx_m15) < 30 or len(ctx_h1) < 10:
                print(f"  Za mało danych (M15:{len(ctx_m15)}, H1:{len(ctx_h1)}), pomijam.")
                if sheet_grok:
                    sheet_grok.append_row([label, "", "", "", "", "", "", "brak danych", "", ""])
                sheet_grok2.append_row([label, "", "", "", "", "", "", "brak danych", "", ""])
                continue

            current_price = ctx_m15[-1]["close"]
            future_m15 = [c for c in all_m15 if c["time"] > signal_ts]

            # Range info dla Grok2
            range_info = detect_range(ctx_h1)

            # ── Grok (stary) ─────────────────────────────────────────────────
            grok1_result = None
            if run_grok1:
                user_msg_v1 = build_user_msg_grok(ctx_m15, ctx_h1, current_price)
                grok1_result = call_grok_raw(GROK_PROMPT, user_msg_v1, use_web_search=True, label="grok")
                time.sleep(1)

                outcome1 = process_and_write(label, "grok", grok1_result, future_m15, signal_ts, sheet_grok)
                if outcome1:
                    if outcome1["wynik"] != "no entry":
                        stats["grok"]["alerts"] += 1
                        if outcome1.get("delta") is not None:
                            stats["grok"]["entries"] += 1
                            stats["grok"]["delta_sum"] += outcome1["delta"]
                        if outcome1.get("delta_tp1") is not None:
                            stats["grok"]["delta_tp1_sum"] += outcome1["delta_tp1"]

            # ── Grok2 (nowy) ─────────────────────────────────────────────────
            sentiment_line = build_sentiment_line_historical(btc_h1, eth_h1, fg_history, signal_ts)
            user_msg_v2 = build_user_msg_grok2(ctx_m15, ctx_h1, current_price, sentiment_line, range_info)
            grok2_result = call_grok_raw(GROK2_PROMPT, user_msg_v2, use_web_search=False, label="grok2")
            time.sleep(1)

            outcome2 = process_and_write(label, "grok2", grok2_result, future_m15, signal_ts, sheet_grok2)
            if outcome2:
                if outcome2["wynik"] != "no entry":
                    stats["grok2"]["alerts"] += 1
                    if outcome2.get("delta") is not None:
                        stats["grok2"]["entries"] += 1
                        stats["grok2"]["delta_sum"] += outcome2["delta"]
                    if outcome2.get("delta_tp1") is not None:
                        stats["grok2"]["delta_tp1_sum"] += outcome2["delta_tp1"]

        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  [BŁĄD] Iteracja {i+1} ({label}) pominięta: {exc}")
            errors += 1
            if errors >= 5:
                print(f"\n[ABORT] Zbyt wiele błędów ({errors}), przerywam.")
                break
            time.sleep(2)

    # ── 7. Podsumowanie ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PODSUMOWANIE")
    print("=" * 60)

    for name, s in stats.items():
        if not run_grok1 and name == "grok":
            continue
        print(f"\n{name.upper()}:")
        print(f"  Alerty (send_alert=true): {s['alerts']}/{num_hours}")
        print(f"  Wejścia aktywowane:       {s['entries']}")
        if s["entries"] > 0:
            print(f"  Suma delta (TP1+TP2):     {s['delta_sum']:+.2f}")
            print(f"  Suma deltaTP1:            {s['delta_tp1_sum']:+.2f}")
            print(f"  Avg delta/trade:          {s['delta_sum']/s['entries']:+.4f}")
        else:
            print(f"  Brak wejść do ewaluacji.")

    print(f"\nWyniki w arkuszu: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    print("=== Backtest zakończony ===")


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    run_backtest()
