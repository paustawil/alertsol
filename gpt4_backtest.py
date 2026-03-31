"""
GPT4 backtest — ostatnie 48 godzin, jedno zapytanie na każdą pełną godzinę.
Wyniki zapisywane do arkusza 'GPT4 test' w tym samym skoroszycie co Alerty/Wyniki.

Prompt: pattern-based, anticipatory entry (sweep/retest/continuation/range edge).

Uruchomienie:
    python gpt4_backtest.py
"""

import json
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

ENTRY_WINDOW_S   = 24 * 3600
OUTCOME_WINDOW_S = 24 * 3600

SHEET_NAME = "GPT4 test"
SHEET_HEADER = [
    "Data i godzina", "Kierunek", "Pewność", "W", "TP1", "TP2", "SL",
    "Wynik", "Czas do entry", "Delta (TP1+TP2)", "DeltaTP1",
]

# ── Prompty (zsynchronizowane z sol_alert.py) ─────────────────────────────────
GPT4_SYSTEM_PROMPT = """Jesteś doświadczonym traderem kryptowalut specjalizującym się wyłącznie w SOL/USDT na interwałach H1 i M15.

Twoim zadaniem NIE jest ogólne komentowanie rynku ani przewidywanie przyszłości.
Twoim zadaniem jest rozpoznanie, czy na wykresie występuje konkretny, grywalny setup oraz wskazanie najlepszej strefy do ustawienia zleceń.

System działa jako skaner poziomów i stref przewagi w stałych interwałach.
Nie działa tickowo i nie łapie precyzyjnych triggerów intrabar.

Twoim zadaniem jest:
- rozpoznać mechanizm rynkowy (pattern)
- wskazać strefę, gdzie przewaga pojawia się najwcześniej
- zaproponować anticipacyjne wejście (nie czekając na perfekcyjne potwierdzenie)
- zwrócić wynik w formacie JSON

---

## DOZWOLONE SETUPY (PATTERNY)

Setup może powstać tylko, jeśli występuje jeden z poniższych układów:

1. Retest wybitego poziomu
- poziom został wybity
- cena wraca do poziomu
- poziom ma sens jako wsparcie/opór

2. Sweep i powrót
- poprzedni swing high/low został naruszony
- cena wraca do zakresu
- sugeruje fałszywe wybicie

3. Continuation po korekcie
- istnieje kierunek na H1
- impuls na M15 jest zgodny z tym kierunkiem
- korekta wraca do sensownej strefy

4. Range edge
- rynek jest w konsolidacji
- cena znajduje się przy krawędzi zakresu

Jeśli żaden z tych setupów nie występuje → brak setupu.

---

## LOGIKA BUDOWY SETUPU

Zawsze działaj w tej kolejności:

1. Określ kontekst H1 (trend / range / poziomy)
2. Określ strukturę M15
3. Rozpoznaj pattern (jeśli brak → brak setupu)
4. Wyznacz strefę reakcji (gdzie cena powinna zareagować)
5. Ustal wejście (anticipacyjne, w strefie)
6. Ustal SL (logiczny, za unieważnieniem)
7. Ustal TP (kolejne poziomy)

Bias NIE generuje setupu.
Bias tylko opisuje kierunek wynikający z patternu.

---

## STREFA I WEJŚCIE

Wejście NIE jest reakcją na świecę.
Wejście jest ustawiane anticipacyjnie w strefie przewagi.

Nie ustawiaj wejścia:
- w środku zakresu
- w środku ruchu bez poziomu

Wejście musi być powiązane z:
- poziomem
- patternem

---

## ZASADA BUFORA (BARDZO WAŻNE)

Jeśli poziom jest oczywisty (high/low, round number, range edge):

USTAW:
- entry trochę wcześniej
- TP trochę wcześniej
- SL trochę dalej

Bufor:
- 0.05 – 0.20 USD
- dostosuj do zmienności M15

Cel:
- uniknąć "prawie weszło / prawie TP"

---

## WARUNKI BRAKU SETUPU

Zwróć brak setupu jeśli:
- brak patternu
- brak czytelnego poziomu
- cena w środku range
- brak sensownej przewagi

---

## FORMAT JSON (OBOWIĄZKOWY)

Zwróć WYŁĄCZNIE JSON. Bez markdownu. Bez komentarzy poza JSON.

### Jeśli jest setup:
{"send_alert":true,"bias":"long","bias_proc":70,"tf_aligned":true,"sentyment":"...","analiza":"...","wejscia":[{"poziom":124.50,"warunek":"wejście w strefie reakcji"}],"tp1":127.00,"tp2":129.50,"sl":122.80,"sl_after_tp1":123.00,"rr":2.1,"akcja":"..."}

### Jeśli brak setupu:
{"send_alert":false,"bias":"neutral","bias_proc":50,"tf_aligned":false,"sentyment":"...","analiza":"...","akcja":"..."}

---

## DODATKOWE ZASADY

- bias: long / short / neutral
- bias_proc: 0–100
- rr > 0
- wejscia tylko gdy send_alert = true
- tp1, tp2, sl, sl_after_tp1, rr tylko gdy send_alert = true
- jeśli send_alert = false, nie dodawaj pól poza: send_alert, bias, bias_proc, tf_aligned, sentyment, analiza, akcja
- jeśli sentyment nie jest podany, wpisz "brak danych" w polu sentyment"""


def build_gpt4_user_prompt(
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
    sentiment: str | None = None,
) -> str:
    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-100:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-50:]
    )
    sentiment_line = sentiment if sentiment else "brak"
    return (
        "Przeanalizuj SOL/USDT i zwróć wyłącznie JSON.\n\n"
        "Tryb działania:\n"
        "System działa jako skaner poziomów pod zlecenia oczekujące.\n"
        "Nie działa tickowo.\n\n"
        "Dane:\n\n"
        f"- aktualna cena: ${current_price:.2f}\n\n"
        f"- sentyment (opcjonalny):\n{sentiment_line}\n\n"
        f"- H1 candles (50):\n{h1_csv}\n\n"
        f"- M15 candles (100):\n{m15_csv}\n\n"
        "Świece są chronologiczne.\n"
        "Ostatnia świeca jest zamknięta.\n\n"
        "Wymagania:\n"
        "- znajdź jeden najlepszy setup albo brak setupu\n"
        "- nie zgaduj\n"
        "- nie generuj setupu bez patternu\n"
        "- poziomy mają być konkretne\n"
        "- zwróć tylko JSON"
    )


# ── Bitget: pobieranie świec historycznych (paginacja wstecz) ─────────────────
def fetch_klines_paginated(symbol: str, interval: str, total: int, end_ts_s: int | None = None) -> list[dict]:
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


# ── GPT4 call ────────────────────────────────────────────────────────────────
def call_gpt4_raw(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt4] Brak klucza OPENAI_API_KEY.")
        return None

    user_msg = build_gpt4_user_prompt(candles_m15, candles_h1, current_price, sentiment=None)

    try:
        client = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model=GPT_MODEL,
            max_tokens=2048,
            timeout=GPT_TIMEOUT_S,
            messages=[
                {"role": "system", "content": GPT4_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        text = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[gpt4] Błąd API: {e}")
        return None

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        print(f"[gpt4] Brak JSON: {text[:200]}")
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[gpt4] Błąd JSON: {e}")
        return None


# ── Ewaluacja wyniku ──────────────────────────────────────────────────────────
def _round_to_quarter(hours: float) -> float:
    return round(hours * 4) / 4


def evaluate_outcome(gpt_result: dict, future_m15: list[dict], signal_ts: int) -> dict:
    direction    = gpt_result.get("bias", "neutral")
    wejscia      = gpt_result.get("wejscia", [])
    entries      = [w["poziom"] for w in wejscia if "poziom" in w]
    tp1          = gpt_result.get("tp1")
    tp2          = gpt_result.get("tp2")
    sl           = gpt_result.get("sl")
    sl_after_tp1 = gpt_result.get("sl_after_tp1")

    empty = {"entry_activated": False, "entry_ts": None, "entry_price": None,
             "wynik": "no entry", "czas_do_entry_h": None, "delta": None, "delta_tp1": None}

    if not entries or tp1 is None or sl is None:
        return empty

    entry_deadline = signal_ts + ENTRY_WINDOW_S
    entry_ts = entry_price = None

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
        final_wynik = "TP1+BE" if tp1_hit else "SL"

    # Delta (TP1+TP2 strategy)
    avg_entry = entry_price
    if final_wynik == "TP1+TP2" and tp1 is not None and tp2 is not None:
        avg_exit = (tp1 + tp2) / 2
    elif final_wynik == "TP1+BE":
        sl_guard = sl_after_tp1 if sl_after_tp1 is not None else entry_price
        avg_exit = (tp1 + sl_guard) / 2 if tp1 is not None else entry_price
    else:
        avg_exit = sl

    delta = round(avg_exit - avg_entry if direction == "long" else avg_entry - avg_exit, 4)

    # DeltaTP1 (zamknij całość na TP1)
    exit_tp1 = sl
    for c in future_m15:
        if c["time"] <= entry_ts:
            continue
        if c["time"] > entry_ts + OUTCOME_WINDOW_S:
            break
        if direction == "long":
            tp1_c = c["high"] >= tp1
            sl_c  = c["low"]  <= sl
        else:
            tp1_c = c["low"]  <= tp1
            sl_c  = c["high"] >= sl

        if tp1_c and sl_c:
            tp1_c = ((tp1 - avg_entry) <= (avg_entry - sl)) if direction == "long" \
                    else ((avg_entry - tp1) <= (sl - avg_entry))
        if sl_c and not tp1_c:
            exit_tp1 = sl
            break
        if tp1_c:
            exit_tp1 = tp1
            break

    delta_tp1 = round(exit_tp1 - avg_entry if direction == "long" else avg_entry - exit_tp1, 4)

    return {
        "entry_activated": True,
        "entry_ts":        entry_ts,
        "entry_price":     entry_price,
        "wynik":           final_wynik,
        "czas_do_entry_h": czas_h,
        "delta":           delta,
        "delta_tp1":       delta_tp1,
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
        sh = wb.worksheet(SHEET_NAME)
        sh.clear()
    except gspread.WorksheetNotFound:
        sh = wb.add_worksheet(SHEET_NAME, rows=200, cols=len(SHEET_HEADER) + 2)
    sh.append_row(SHEET_HEADER)
    return sh


# ── Główna logika backtestu ───────────────────────────────────────────────────
def run_backtest() -> None:
    print(f"=== GPT4 Backtest — start ===")

    now_ts = int(time.time())
    print("Pobieranie świec M15 (550 szt)...")
    all_m15 = fetch_klines_paginated(SYMBOL, "15m", total=550, end_ts_s=now_ts)
    print(f"  Pobrano {len(all_m15)} świec M15 ({_ts_fmt(all_m15[0]['time'])} – {_ts_fmt(all_m15[-1]['time'])})")

    print("Pobieranie świec H1 (150 szt)...")
    all_h1 = fetch_klines_paginated(SYMBOL, "1h", total=150, end_ts_s=now_ts)
    print(f"  Pobrano {len(all_h1)} świec H1 ({_ts_fmt(all_h1[0]['time'])} – {_ts_fmt(all_h1[-1]['time'])})")

    latest_full_hour = (now_ts // 3600) * 3600
    test_hours = [latest_full_hour - i * 3600 for i in range(48, 0, -1)]

    print("Łączenie z Google Sheets...")
    sheet = get_test_sheet()
    print("Gotowe.")

    for i, signal_ts in enumerate(test_hours):
        label = _ts_fmt(signal_ts)
        print(f"\n[{i+1}/48] {label}")

        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 30 or len(ctx_h1) < 10:
            print(f"  Za mało danych kontekstu (M15:{len(ctx_m15)}, H1:{len(ctx_h1)}), pomijam.")
            sheet.append_row([label, "", "", "", "", "", "", "brak danych", "", "", ""])
            continue

        current_price = ctx_m15[-1]["close"]

        gpt_result = call_gpt4_raw(ctx_m15, ctx_h1, current_price)
        time.sleep(1)

        if gpt_result is None:
            print("  Brak odpowiedzi GPT4.")
            sheet.append_row([label, "", "", "", "", "", "", "błąd GPT", "", "", ""])
            continue

        send_alert = gpt_result.get("send_alert", False)
        bias       = gpt_result.get("bias", "neutral")
        bias_proc  = gpt_result.get("bias_proc", 0)

        print(f"  send_alert={send_alert} | bias={bias} ({bias_proc}%)")

        if not send_alert or bias == "neutral":
            sheet.append_row([label, "null", bias_proc, "", "", "", "", "no entry", "", "", ""])
            continue

        tp1     = gpt_result.get("tp1", "")
        tp2     = gpt_result.get("tp2", "")
        sl      = gpt_result.get("sl",  "")
        wejscia = gpt_result.get("wejscia", [])
        entries = [w["poziom"] for w in wejscia if "poziom" in w]
        avg_w   = round(sum(entries) / len(entries), 4) if entries else ""

        future_m15 = [c for c in all_m15 if c["time"] > signal_ts]
        outcome    = evaluate_outcome(gpt_result, future_m15, signal_ts)

        wynik         = outcome["wynik"]
        czas_str      = f"{outcome['czas_do_entry_h']}h" if outcome["czas_do_entry_h"] is not None else ""
        delta_val     = outcome["delta"]     if outcome["delta"]     is not None else ""
        delta_tp1_val = outcome["delta_tp1"] if outcome["delta_tp1"] is not None else ""

        print(f"  Wynik: {wynik} | czas: {czas_str} | delta: {delta_val} | deltaTP1: {delta_tp1_val}")

        sheet.append_row([
            label, bias.upper(), bias_proc, avg_w,
            tp1, tp2, sl, wynik, czas_str, delta_val, delta_tp1_val,
        ])

    print(f"\n=== Backtest zakończony ===")
    print(f"Wyniki zapisane w arkuszu '{SHEET_NAME}' (SHEET_ID={SHEET_ID})")


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    run_backtest()
