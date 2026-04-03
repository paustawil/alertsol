"""
GPT-Relaxed backtest — ostatnie 48 godzin, jedno zapytanie na każdą pełną godzinę.
Używa modelu gpt-4o z web_search_preview (live sentiment BTC/ETH/SOL + F&G).
Wyniki zapisywane do arkusza 'GPT-Relaxed test'.

Uruchomienie:
    python gpt_relaxed_backtest.py
"""

import json
import math
import os
import re
import time
from datetime import datetime, timezone

import openai
import requests
from google.oauth2.service_account import Credentials
import gspread

# ── Konfiguracja ─────────────────────────────────────────────────────────────
SYMBOL        = "SOLUSDT"
SHEET_ID      = "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE"
OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
GPT_MODEL     = "gpt-4o"
GPT_TIMEOUT_S = 120

# Okno po sygnale do szukania wejścia i wyniku (w sekundach)
ENTRY_WINDOW_S  = 24 * 3600   # 24h na aktywację wejścia
OUTCOME_WINDOW_S = 24 * 3600  # 24h na rozstrzygnięcie po wejściu

SHEET_HEADER = [
    "Data i godzina", "Kierunek", "Pewność", "W", "TP1", "TP2", "SL",
    "Wynik", "Czas do entry", "Delta (TP1+TP2)", "DeltaTP1",
]

# ── Prompt GPT-Relaxed (z sol_alert.py) ──────────────────────────────────────
GPT_RELAXED_PROMPT = """Jesteś doświadczonym traderem kryptowalut, specjalizującym się w SOL/USDT na interwałach M15 i H1.

Masz dostęp do internetu — użyj go, żeby pobrać:
- Aktualne ceny BTC, ETH, SOL (USD)
- Aktualny Fear & Greed Index (wartość 0–100 + etykieta)

Otrzymasz też dane OHLCV: M15 (ostatnie 60 świec) i H1 (ostatnie 24 świece) dla SOL.

Twoje zadanie:
1. Krótko oceń sentyment: BTC/ETH/SOL (24h zmiana, relatywna siła SOL), Fear & Greed.
2. Przeanalizuj strukturę techniczną H1 i M15: kluczowe supporty i resistancey, trend, formacje, RSI, MACD, volume. Bez lania wody — tylko to co istotne.
3. Podaj bias (long / short / neutral) z prawdopodobieństwem w %.
4. Jeśli bias nie jest neutral — zaproponuj 1–2 konkretne poziomy wejścia z warunkiem aktywacji.
5. Podaj TP1 (bezpieczny, bliższy) i TP2 (ambitny, ale realistyczny).
6. Podaj ciasny SL i przybliżone R:R (minimum 1:2).
7. Na końcu: co teraz robisz (np. "Czekam na pullback do X i wchodzę long").

Zasady:
- Analiza techniczna ma priorytet (70–80%). Sentyment i kontekst makro — 20–30%.
- Odpowiadaj zawsze po polsku, konkretnie, bez powtarzania ostrzeżeń o ryzyku.
- Ustaw send_alert=true TYLKO gdy spełnione są WSZYSTKIE poniższe warunki:
  a) H1 i M15 wskazują ten sam kierunek (tf_aligned=true) — jeśli timeframy są sprzeczne, send_alert=false.
  b) bias_proc >= 65 — jeśli przekonanie jest niższe, oznacza to zawahanie rynku, ustaw send_alert=false.
  c) Widzisz wyraźny, konkretny setup z jasnym entry, SL i TP.
- Przy bocznym rynku, choppingu, sprzecznych sygnałach H1/M15 lub niskim przekonaniu — send_alert=false.
- tf_aligned: Oceń czy H1 i M15 pokazują ten sam kierunek. true = zgodne, false = sprzeczne lub jeden neutralny.
- sl_after_tp1: Po osiągnięciu TP1 SL należy przesunąć. Znajdź ostatni strukturalny support (long) lub resistance (short) między W1 a TP1. Jeśli taki poziom istnieje i jest w strefie zysku (powyżej W1 dla long, poniżej W1 dla short) — użyj go jako sl_after_tp1. Jeśli nie — użyj W1 (break-even). Zawsze podaj tę wartość gdy send_alert=true.

Zwróć dokładnie jeden obiekt JSON. Bez markdownu, bez tekstu poza JSON.

Gdy send_alert=true:
{"send_alert":true,"bias":"long","bias_proc":70,"tf_aligned":true,"sentyment":"krótka ocena BTC/ETH/SOL + F&G z aktualnymi wartościami","analiza":"konkretna analiza techniczna H1/M15","wejscia":[{"poziom":124.50,"warunek":"zamknięcie M15 powyżej 124.80"}],"tp1":127.00,"tp2":129.50,"sl":122.80,"sl_after_tp1":123.00,"rr":2.1,"akcja":"Czekam na pullback do 124.50 i wchodzę long"}

Gdy send_alert=false:
{"send_alert":false,"bias":"neutral","bias_proc":50,"tf_aligned":false,"sentyment":"krótka ocena BTC/ETH/SOL + F&G z aktualnymi wartościami","analiza":"co widzisz na wykresie i dlaczego brak setupu","akcja":"Obserwuję, czekam na wyklarowanie sytuacji"}"""


# ── Bitget: pobieranie świec historycznych (paginacja wstecz) ─────────────────
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


# ── GPT-Relaxed call (Responses API z web_search_preview) ────────────────────
def call_gpt_relaxed_raw(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt-r] Brak klucza OPENAI_API_KEY.")
        return None

    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-60:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-24:]
    )
    user_msg = (
        f"Aktualna cena SOL z moich danych: ${current_price:.2f}\n\n"
        f"SOL M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
        f"SOL H1 (ostatnie 24 swiece):\n{h1_csv}"
    )

    try:
        client = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.responses.create(
            model=GPT_MODEL,
            tools=[{"type": "web_search_preview"}],
            instructions=GPT_RELAXED_PROMPT,
            input=user_msg,
            max_output_tokens=2048,
        )
        text = response.output_text.strip()
    except Exception as e:
        print(f"[gpt-r] Błąd API: {e}")
        return None

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        print(f"[gpt-r] Brak JSON: {text[:200]}")
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[gpt-r] Błąd JSON: {e}")
        return None


# ── Ewaluacja wyniku ──────────────────────────────────────────────────────────
def _round_to_quarter(hours: float) -> float:
    return round(hours * 4) / 4


def evaluate_outcome(
    gpt_result: dict,
    future_m15: list[dict],
    signal_ts: int,
) -> dict:
    direction = gpt_result.get("bias", "neutral")
    wejscia   = gpt_result.get("wejscia", [])
    entries   = [w["poziom"] for w in wejscia if "poziom" in w]
    tp1 = gpt_result.get("tp1")
    tp2 = gpt_result.get("tp2")
    sl  = gpt_result.get("sl")
    sl_after_tp1 = gpt_result.get("sl_after_tp1")

    if not entries or tp1 is None or sl is None:
        return {"entry_activated": False, "entry_ts": None, "entry_price": None,
                "wynik": "no entry", "czas_do_entry_h": None, "delta": None, "delta_tp1": None}

    entry_deadline = signal_ts + ENTRY_WINDOW_S

    entry_ts    = None
    entry_price = None
    for c in future_m15:
        if c["time"] > entry_deadline:
            break
        for lvl in entries:
            if c["low"] <= lvl <= c["high"]:
                entry_ts    = c["time"]
                entry_price = lvl
                break
        if entry_ts:
            break

    if entry_ts is None:
        return {"entry_activated": False, "entry_ts": None, "entry_price": None,
                "wynik": "no entry", "czas_do_entry_h": None, "delta": None, "delta_tp1": None}

    czas_h = _round_to_quarter((entry_ts - signal_ts) / 3600)

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
                sl_guard = sl_after_tp1 if sl_after_tp1 is not None else entry_price
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
        "entry_activated":  True,
        "entry_ts":         entry_ts,
        "entry_price":      entry_price,
        "wynik":            final_wynik,
        "czas_do_entry_h":  czas_h,
        "delta":            delta,
        "delta_tp1":        delta_tp1,
    }


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_test_sheet() -> gspread.Worksheet:
    creds  = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    wb     = client.open_by_key(SHEET_ID)
    try:
        sh = wb.worksheet("GPT-Relaxed test")
        sh.clear()
    except gspread.WorksheetNotFound:
        sh = wb.add_worksheet("GPT-Relaxed test", rows=200, cols=len(SHEET_HEADER) + 2)
    sh.append_row(SHEET_HEADER)
    return sh


# ── Główna logika backtestu ───────────────────────────────────────────────────
def run_backtest() -> None:
    print("=== GPT-Relaxed Backtest — start ===")

    now_ts = int(time.time())
    # M15: 60 kontekst + 48*4 testowe + 24*4 outcome = 444 świec
    # H1:  24 kontekst + 48 testowe + 24 outcome = 96 świec
    print("Pobieranie świec M15 (450 szt)...")
    all_m15 = fetch_klines_paginated(SYMBOL, "15m", total=450, end_ts_s=now_ts)
    print(f"  Pobrano {len(all_m15)} świec M15 ({_ts_fmt(all_m15[0]['time'])} – {_ts_fmt(all_m15[-1]['time'])})")

    print("Pobieranie świec H1 (120 szt)...")
    all_h1 = fetch_klines_paginated(SYMBOL, "1h", total=120, end_ts_s=now_ts)
    print(f"  Pobrano {len(all_h1)} świec H1 ({_ts_fmt(all_h1[0]['time'])} – {_ts_fmt(all_h1[-1]['time'])})")

    latest_full_hour = (now_ts // 3600) * 3600
    test_hours = [latest_full_hour - i * 3600 for i in range(48, 0, -1)]

    print("Łączenie z Google Sheets...")
    sheet = get_test_sheet()
    print("Gotowe.")

    for i, signal_ts in enumerate(test_hours):
        label = _ts_fmt(signal_ts)
        print(f"\n[{i+1}/48] {label}")

        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-60:]
        ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-24:]

        if len(ctx_m15) < 20 or len(ctx_h1) < 8:
            print(f"  Za mało danych kontekstu (M15:{len(ctx_m15)}, H1:{len(ctx_h1)}), pomijam.")
            sheet.append_row([label, "", "", "", "", "", "", "brak danych", "", ""])
            continue

        current_price = ctx_m15[-1]["close"]

        gpt_result = call_gpt_relaxed_raw(ctx_m15, ctx_h1, current_price)
        time.sleep(2)  # web_search_preview jest wolniejszy

        if gpt_result is None:
            print("  Brak odpowiedzi GPT-Relaxed.")
            sheet.append_row([label, "", "", "", "", "", "", "błąd GPT", "", ""])
            continue

        send_alert = gpt_result.get("send_alert", False)
        bias       = gpt_result.get("bias", "neutral")
        bias_proc  = gpt_result.get("bias_proc", 0)

        print(f"  send_alert={send_alert} | bias={bias} ({bias_proc}%)")

        if not send_alert or bias == "neutral":
            sheet.append_row([label, "null", bias_proc, "", "", "", "", "no entry", "", ""])
            continue

        tp1 = gpt_result.get("tp1", "")
        tp2 = gpt_result.get("tp2", "")
        sl  = gpt_result.get("sl",  "")
        wejscia = gpt_result.get("wejscia", [])
        entries = [w["poziom"] for w in wejscia if "poziom" in w]
        avg_w   = round(sum(entries) / len(entries), 4) if entries else ""

        future_m15 = [c for c in all_m15 if c["time"] > signal_ts]
        outcome = evaluate_outcome(gpt_result, future_m15, signal_ts)

        wynik         = outcome["wynik"]
        czas_str      = f"{outcome['czas_do_entry_h']}h" if outcome["czas_do_entry_h"] is not None else ""
        delta_val     = outcome["delta"]    if outcome["delta"]    is not None else ""
        delta_tp1_val = outcome["delta_tp1"] if outcome["delta_tp1"] is not None else ""

        print(f"  Wynik: {wynik} | czas do entry: {czas_str} | delta: {delta_val} | deltaTP1: {delta_tp1_val}")

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

    print("\n=== Backtest zakończony ===")
    print(f"Wyniki zapisane w arkuszu 'GPT-Relaxed test' (SHEET_ID={SHEET_ID})")


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    run_backtest()
