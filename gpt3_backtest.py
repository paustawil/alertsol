"""
GPT3 backtest — ostatnie 48 godzin, jedno zapytanie na każdą pełną godzinę.
Wyniki zapisywane do arkusza 'GPT3 test' w tym samym skoroszycie co Alerty/Wyniki.

Uruchomienie:
    python gpt3_backtest.py
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

# ── Prompt systemowy GPT3 (zsynchronizowany z sol_alert.py) ──────────────────
GPT3_SYSTEM_PROMPT = """Jesteś doświadczonym traderem kryptowalut specjalizującym się wyłącznie w SOL/USDT na interwałach H1 i M15.

Twoim zadaniem NIE jest ogólne komentowanie rynku.
Twoim zadaniem jest wykrywanie sensownych setupów transakcyjnych i zwracanie wyniku w ściśle określonym formacie JSON.

Masz działać jak selektor setupów, nie jak ostrożny komentator.
Jeżeli istnieje choć jeden logiczny setup o jakości minimum 10/15, masz go wskazać.
Jeżeli istnieje setup 12/15+, ma on najwyższy priorytet.
Setup może być aktywny teraz albo oczekujący na dojście do poziomu.

Analizujesz wyłącznie dane wejściowe dostarczone przez użytkownika:
- aktualna cena SOL
- 100 świec M15: timestamp, open, high, low, close, volume
- 50 świec H1: timestamp, open, high, low, close, volume
- sentyment: opcjonalny (BTC/ETH/SOL + Fear & Greed)

Jeśli sentyment nie jest dostarczony:
- pomiń jego wpływ
- nie zgaduj sentymentu
- oprzyj analizę wyłącznie na danych OHLCV

Nie zakładaj żadnych danych spoza wejścia.
Nie odwołuj się do internetu.
Nie wymyślaj wskaźników, których nie da się oszacować z danych wejściowych.
Możesz wyciągać wnioski o:
- trendzie
- strukturze swingów
- impulsie i korekcie
- lokalnych strefach wsparcia/oporu
- wybiciu, retestach, odrzuceniach, range, sweepach
- relatywnym momentum świec i wolumenu
- zgodności lub niezgodności H1 i M15

## Model oceny setupu
Oceń każdy setup w 5 filarach, każdy po 0-3 punkty:

1. Trend
- 0 = setup pod wyraźnie dominujący ruch bez argumentów
- 1 = trend niejasny / mieszany
- 2 = umiarkowana zgodność z trendem lub sensowna kontra przy skrajnym poziomie
- 3 = wysoka zgodność z dominującym kierunkiem albo bardzo mocny reversal z czytelnym argumentem

2. Struktura
- 0 = chaos, środek konsolidacji, brak przewagi
- 1 = częściowy układ, ale bez czytelnej sekwencji
- 2 = widoczny układ HH/HL lub LH/LL, retest, odrzucenie, wybicie lub range edge
- 3 = bardzo czytelna struktura z jasnym triggerem i miejscem unieważnienia

3. Poziom
- 0 = przypadkowy poziom
- 1 = poziom średniej jakości
- 2 = lokalnie istotna strefa
- 3 = bardzo istotny poziom: range high/low, mocny swing, wielokrotny retest, sweep + reakcja

4. Momentum
- 0 = brak przewagi
- 1 = mieszane
- 2 = umiarkowana przewaga kierunkowa
- 3 = silny impuls / mocna reakcja / wyraźna przewaga świec i wolumenu

5. RR
- 0 = zły stosunek zysku do ryzyka lub bardzo niepraktyczny SL
- 1 = przeciętny
- 2 = dobry
- 3 = bardzo dobry i logiczny względem struktury

Maksimum: 15 punktów.

## Zasady decyzyjne
1. Najpierw określ kontekst H1:
- trend wzrostowy / spadkowy / konsolidacja
- najważniejsze wsparcia i opory
- czy rynek jest przy krawędzi range czy w środku

2. Potem określ kontekst M15:
- bieżąca struktura
- ostatni impuls
- korekta / kontynuacja / wybicie / odrzucenie

3. Potem wybierz maksymalnie 1 najlepszy setup do alertu.
Nie zwracaj wielu setupów. Zwróć tylko najlepszy setup albo brak setupu.

4. Setup musi być praktyczny. Jeśli go zwracasz, musi zawierać:
- bias
- bias_proc
- zgodność interwałów tf_aligned
- sentyment
- analizę
- jedno lub więcej wejść
- TP1
- TP2
- SL
- poziom przesunięcia SL po TP1
- RR
- akcję

5. Nie odrzucaj setupu tylko dlatego, że nie ma idealnych warunków.
Jeżeli setup jest logiczny i ma minimum 10/15, pokaż go.
Dopiero gdy rynek jest naprawdę w środku chaosu i nie ma sensownej przewagi, zwróć brak setupu.

6. Bardzo ważne:
- nie proponuj wejść ze środka konsolidacji, jeśli nie ma wyraźnej przewagi
- preferuj: retest poziomu, odrzucenie strefy, wybicie i retest, sweep i powrót, wejście przy krawędzi range
- poziomy mają wynikać z danych, nie być okrągłymi liczbami bez uzasadnienia
- SL ma być logiczny strukturalnie, nie sztucznie zawężony
- TP ma wynikać z kolejnych logicznych poziomów i zasięgu ruchu
- bias_proc ma być liczbą całkowitą 0-100
- rr ma być liczbą dodatnią
- tf_aligned = true tylko wtedy, gdy H1 i M15 realnie wspierają ten sam kierunek
- Sentyment nigdy nie może sam w sobie tworzyć setupu. Może tylko wzmacniać lub osłabiać istniejący setup techniczny.

7. Jeśli nie ma setupu 10/15+, nadal wskaż:
- bias: long, short albo neutral
- bias_proc
- tf_aligned
- sentyment
- analizę
- akcję opisującą, czego trzeba wypatrywać

## Reguły wyjścia JSON
Masz zwrócić WYŁĄCZNIE poprawny JSON.
Bez markdownu.
Bez komentarza przed JSON-em.
Bez komentarza po JSON-em.
Bez używania bloków ```json.

### Gdy setup istnieje, zwróć dokładnie taki kształt:
{"send_alert":true,"bias":"long","bias_proc":70,"tf_aligned":true,"sentyment":"ocena BTC/ETH/SOL + F&G","analiza":"analiza techniczna H1/M15","wejscia":[{"poziom":124.50,"warunek":"zamknięcie M15 powyżej 124.80"}],"tp1":127.00,"tp2":129.50,"sl":122.80,"sl_after_tp1":123.00,"rr":2.1,"akcja":"opis akcji"}

### Gdy setup nie istnieje, zwróć dokładnie taki kształt:
{"send_alert":false,"bias":"neutral","bias_proc":50,"tf_aligned":false,"sentyment":"...","analiza":"...","akcja":"..."}

## Dodatkowe ograniczenia
- bias musi być jednym z: "long", "short", "neutral"
- wejscia ma istnieć tylko wtedy, gdy send_alert = true
- tp1, tp2, sl, sl_after_tp1, rr mają istnieć tylko wtedy, gdy send_alert = true
- jeżeli send_alert = false, nie dodawaj żadnych dodatkowych pól poza:
  send_alert, bias, bias_proc, tf_aligned, sentyment, analiza, akcja
- jeżeli send_alert = true, nie pomijaj żadnego wymaganego pola
- analiza i akcja mają być konkretne, ale krótkie i praktyczne
- sentyment ma być krótkim podsumowaniem wejściowych danych sentymentu, nie długim komentarzem
- jeśli sentyment nie jest podany, wpisz "brak danych" w polu sentyment"""


def build_gpt3_user_prompt(
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
        "Przeanalizuj SOL/USDT i zwróć wyłącznie poprawny JSON zgodny z wymaganym formatem.\n\n"
        "Świece są ułożone chronologicznie od najstarszej do najnowszej.\n"
        "Ostatni wiersz to ostatnia zamknięta świeca.\n"
        "Aktualna cena jest nowsza niż ostatnia zamknięta świeca.\n\n"
        "Dane wejściowe:\n"
        f"- aktualna cena SOL: ${current_price:.2f}\n"
        f"- sentyment (opcjonalny): {sentiment_line}\n\n"
        f"- H1 candles (50):\n{h1_csv}\n\n"
        f"- M15 candles (100):\n{m15_csv}\n\n"
        "Wymagania wykonawcze:\n"
        "- oceń kontekst H1 i M15\n"
        "- wybierz tylko 1 najlepszy setup albo brak setupu\n"
        "- jeśli najlepszy setup ma mniej niż 10/15, zwróć send_alert = false\n"
        "- jeśli setup istnieje, podaj konkretne wejście lub wejścia, TP1, TP2, SL i sl_after_tp1\n"
        "- nie uciekaj w ogólniki\n"
        "- nie zwracaj nic poza poprawnym JSON-em\n"
        "- jeśli sentyment nie jest podany, całkowicie go pomiń przy analizie"
    )


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
        # Bitget zwraca newest first → odwracamy i dodajemy na początku
        batch.sort(key=lambda c: c["time"])
        result = batch + result
        # Następna strona: przed najstarszą świecą batcha
        oldest_ts_ms = batch[0]["time"] * 1000
        end_ms = oldest_ts_ms - (interval_s * 1000)

        if len(batch) < 2:
            break  # koniec dostępnych danych

    # Deduplikacja i sortowanie
    seen: set[int] = set()
    deduped = []
    for c in result:
        if c["time"] not in seen:
            seen.add(c["time"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["time"])
    return deduped[-total:] if len(deduped) > total else deduped


# ── GPT3 call ────────────────────────────────────────────────────────────────
def call_gpt3_raw(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt3] Brak klucza OPENAI_API_KEY.")
        return None

    user_msg = build_gpt3_user_prompt(candles_m15, candles_h1, current_price, sentiment=None)

    try:
        client = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model=GPT_MODEL,
            max_tokens=2048,
            timeout=GPT_TIMEOUT_S,
            messages=[
                {"role": "system", "content": GPT3_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        text = response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[gpt3] Błąd API: {e}")
        return None

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        print(f"[gpt3] Brak JSON: {text[:200]}")
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[gpt3] Błąd JSON: {e}")
        return None


# ── Ewaluacja wyniku ──────────────────────────────────────────────────────────
def _round_to_quarter(hours: float) -> float:
    """Zaokrągla do kwadransa (0.25h)."""
    return round(hours * 4) / 4


def evaluate_outcome(
    gpt_result: dict,
    future_m15: list[dict],
    signal_ts: int,
) -> dict:
    """
    Sprawdza co się stało po sygnale.

    Zwraca słownik:
      entry_activated: bool
      entry_ts:        int | None
      entry_price:     float | None
      wynik:           'no entry' | 'TP1+TP2' | 'TP1+BE' | 'SL'
      czas_do_entry_h: float | None
      delta:           float | None
    """
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

    # ── Szukaj aktywacji wejścia ──────────────────────────────────────────────
    entry_ts    = None
    entry_price = None
    for c in future_m15:
        if c["time"] > entry_deadline:
            break
        for lvl in entries:
            # Wejście dotknięte jeśli poziom mieści się w zasięgu świecy
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

    # ── Śledź wynik od momentu wejścia ───────────────────────────────────────
    outcome_deadline = entry_ts + OUTCOME_WINDOW_S
    tp1_hit = False
    final_wynik = "SL"  # domyślnie: brak rozstrzygnięcia = SL (timeout)

    for c in future_m15:
        if c["time"] <= entry_ts:
            continue
        if c["time"] > outcome_deadline:
            break

        if not tp1_hit:
            # Sprawdź TP1 vs SL
            if direction == "long":
                tp1_hit_now = c["high"] >= tp1
                sl_hit_now  = c["low"]  <= sl
            else:  # short
                tp1_hit_now = c["low"]  <= tp1
                sl_hit_now  = c["high"] >= sl

            if tp1_hit_now and sl_hit_now:
                # Trudno rozstrzygnąć jedną świecą — zakładamy że SL był bliżej
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
            # TP1 trafiony — pilnuj TP2 i sl_after_tp1
            sl_guard = sl_after_tp1 if sl_after_tp1 is not None else entry_price
            if direction == "long":
                tp2_hit_now = tp2 is not None and c["high"] >= tp2
                sl_hit_now  = c["low"] <= sl_guard
            else:
                tp2_hit_now = tp2 is not None and c["low"] <= tp2
                sl_hit_now  = c["high"] >= sl_guard

            if tp2_hit_now and sl_hit_now:
                tp2_hit_now = True  # zakładamy TP2 bliżej po TP1

            if sl_hit_now and not tp2_hit_now:
                final_wynik = "TP1+BE"
                break
            if tp2_hit_now:
                final_wynik = "TP1+TP2"
                break
    else:
        # Pętla skończyła się bez break — timeout
        if tp1_hit:
            final_wynik = "TP1+BE"
        else:
            final_wynik = "SL"

    # ── Delta (strategia TP1+TP2, po TP1 SL na BE) ───────────────────────────
    avg_entry = entry_price
    if final_wynik == "TP1+TP2" and tp1 is not None and tp2 is not None:
        avg_exit = (tp1 + tp2) / 2
    elif final_wynik == "TP1+BE":
        sl_guard = sl_after_tp1 if sl_after_tp1 is not None else entry_price
        avg_exit = (tp1 + sl_guard) / 2 if tp1 is not None else entry_price
    else:  # SL
        avg_exit = sl

    if direction == "long":
        delta = round(avg_exit - avg_entry, 4)
    else:
        delta = round(avg_entry - avg_exit, 4)

    # ── DeltaTP1 (zamykamy całość na TP1, ignorujemy TP2) ────────────────────
    # Szukamy pierwszego zdarzenia: TP1 trafiony LUB SL trafiony
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
            # Zakładamy pierwsze zdarzenie to to, które jest bliżej entry
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
        exit_tp1 = sl  # timeout = SL

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
        sh = wb.worksheet("GPT3 test")
        sh.clear()
    except gspread.WorksheetNotFound:
        sh = wb.add_worksheet("GPT3 test", rows=200, cols=len(SHEET_HEADER) + 2)
    sh.append_row(SHEET_HEADER)
    return sh


# ── Główna logika backtestu ───────────────────────────────────────────────────
def run_backtest() -> None:
    print("=== GPT3 Backtest — start ===")

    # ── 1. Pobierz dane historyczne ───────────────────────────────────────────
    now_ts   = int(time.time())
    # M15: potrzebujemy 100 kontekstu + 48*4 testowe + 24*4 outcome = 484 świec
    # H1:  potrzebujemy 50 kontekstu + 48 testowe + 24 outcome = 122 świec
    # Bierzemy z zapasem
    print("Pobieranie świec M15 (550 szt)...")
    all_m15 = fetch_klines_paginated(SYMBOL, "15m", total=550, end_ts_s=now_ts)
    print(f"  Pobrano {len(all_m15)} świec M15 ({_ts_fmt(all_m15[0]['time'])} – {_ts_fmt(all_m15[-1]['time'])})")

    print("Pobieranie świec H1 (150 szt)...")
    all_h1 = fetch_klines_paginated(SYMBOL, "1h",  total=150, end_ts_s=now_ts)
    print(f"  Pobrano {len(all_h1)} świec H1 ({_ts_fmt(all_h1[0]['time'])} – {_ts_fmt(all_h1[-1]['time'])})")

    # ── 2. Wyznacz 48 punktów testowych (ostatnie 48 pełnych godzin) ──────────
    # Pełna godzina = ts będący wielokrotnością 3600, zakończona przed now
    latest_full_hour = (now_ts // 3600) * 3600  # obecna pełna godzina (może trwać)
    test_hours = [latest_full_hour - i * 3600 for i in range(48, 0, -1)]
    # test_hours[0] = 48h temu, test_hours[-1] = 1h temu

    # ── 3. Przygotuj arkusz ───────────────────────────────────────────────────
    print("Łączenie z Google Sheets...")
    sheet = get_test_sheet()
    print("Gotowe.")

    # ── 4. Pętla testowa ──────────────────────────────────────────────────────
    for i, signal_ts in enumerate(test_hours):
        label = _ts_fmt(signal_ts)
        print(f"\n[{i+1}/48] {label}")

        # Wytnij kontekst świec do momentu signal_ts (ostatnia zamknięta świeca)
        ctx_m15 = [c for c in all_m15 if c["time"] <= signal_ts - 900][-100:]
        ctx_h1  = [c for c in all_h1  if c["time"] <= signal_ts - 3600][-50:]

        if len(ctx_m15) < 30 or len(ctx_h1) < 10:
            print(f"  Za mało danych kontekstu (M15:{len(ctx_m15)}, H1:{len(ctx_h1)}), pomijam.")
            sheet.append_row([label, "", "", "", "", "", "", "brak danych", "", ""])
            continue

        current_price = ctx_m15[-1]["close"]

        # Wywołaj GPT3
        gpt_result = call_gpt3_raw(ctx_m15, ctx_h1, current_price)
        time.sleep(1)  # drobne throttling

        if gpt_result is None:
            print("  Brak odpowiedzi GPT3.")
            sheet.append_row([label, "", "", "", "", "", "", "błąd GPT", "", ""])
            continue

        send_alert = gpt_result.get("send_alert", False)
        bias       = gpt_result.get("bias", "neutral")
        bias_proc  = gpt_result.get("bias_proc", 0)

        print(f"  send_alert={send_alert} | bias={bias} ({bias_proc}%)")

        if not send_alert or bias == "neutral":
            sheet.append_row([label, "null", bias_proc, "", "", "", "", "no entry", "", ""])
            continue

        # Dane setupu
        tp1 = gpt_result.get("tp1", "")
        tp2 = gpt_result.get("tp2", "")
        sl  = gpt_result.get("sl",  "")
        wejscia = gpt_result.get("wejscia", [])
        entries = [w["poziom"] for w in wejscia if "poziom" in w]
        avg_w   = round(sum(entries) / len(entries), 4) if entries else ""

        # Ewaluacja: świece po signal_ts
        future_m15 = [c for c in all_m15 if c["time"] > signal_ts]
        outcome = evaluate_outcome(gpt_result, future_m15, signal_ts)

        wynik         = outcome["wynik"]
        czas_str      = f"{outcome['czas_do_entry_h']}h" if outcome["czas_do_entry_h"] is not None else ""
        delta_val     = outcome["delta"]    if outcome["delta"]    is not None else ""
        delta_tp1_val = outcome["delta_tp1"] if outcome["delta_tp1"] is not None else ""

        print(f"  Wynik: {wynik} | czas do entry: {czas_str} | delta: {delta_val} | deltaTP1: {delta_tp1_val}")

        kierunek = bias.upper()  # LONG lub SHORT
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
    print(f"Wyniki zapisane w arkuszu 'GPT3 test' (SHEET_ID={SHEET_ID})")


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    run_backtest()
