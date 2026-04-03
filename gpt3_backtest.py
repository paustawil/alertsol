"""
GPT3 backtest — ostatnie 48 godzin, jedno zapytanie na każdą pełną godzinę.
Wyniki zapisywane do arkusza 'GPT3 test' w tym samym skoroszycie co Alerty/Wyniki.

Uruchomienie:
    python gpt3_backtest.py
"""

import argparse
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
    "Data i godzina", "Reżim", "Setup type", "Kierunek", "Pewność", "W", "TP1", "TP2", "SL",
    "Wynik", "Czas do entry", "Delta (TP1+TP2)", "DeltaTP1",
]

# ── Prompt systemowy GPT3 (zsynchronizowany z sol_alert.py) ──────────────────
GPT3_SYSTEM_PROMPT = """Jesteś doświadczonym traderem kryptowalut specjalizującym się wyłącznie w SOL/USDT na interwałach H1 i M15.

Twoim zadaniem jest wykrywanie sensownych setupów transakcyjnych i zwracanie wyniku w ściśle określonym formacie JSON.
Masz działać jak selektor setupów, nie jak ostrożny komentator.
Jeżeli istnieje choć jeden logiczny setup o jakości minimum 10/15, masz go wskazać.

## Dane wejściowe

Otrzymujesz:
- aktualna cena SOL i jej pozycja w bieżącym H1 range (0% = support, 100% = resistance)
- support i resistance H1 (z ostatnich 32 świec H1)
- ATR (14-period) — bieżąca zmienność
- volume_ratio — stosunek ostatnich 2 świec M15 do średniego wolumenu z 10 świec
- regime_hint — klasyfikacja rynku przez algo (wskazówka, możesz ją potwierdzić lub nadpisać)
- 100 świec M15 i 50 świec H1 (OHLCV)
- sentyment: opcjonalny (BTC/ETH/SOL + Fear & Greed)

Nie zakładaj żadnych danych spoza wejścia. Nie odwołuj się do internetu.

## Reżimy rynkowe

Algo klasyfikuje rynek jako jeden z:
- IMPULSE_UP / IMPULSE_DOWN — gwałtowny ruch trwający 2-6h (impulse_strength ≥ 2, vol_ratio ≥ 1.5, zmiana 4h ≥ 1.5%)
  → Priorytet: setupy Z kierunkiem impulsu, nie przeciwko
- TREND_UP / TREND_DOWN — kierunkowy ruch trwający 24-48h (zmiana 24h ≥ 1.5% lub 48h ≥ 3.0%, struktura HH/HL lub LH/LL)
  → Priorytet: pullbacki z trendem, konsolidacje jako pauza przed kontynuacją
- RANGE — brak kierunku
  → Priorytet: long z supportu, short z resistance

Otrzymujesz regime_hint jako wskazówkę. Masz prawo go nadpisać jeśli widzisz w danych wyraźną sprzeczność.
Jeśli nadpisujesz, zwróć "regime_override_reason" z krótkim uzasadnieniem.

## Dozwolone typy setupów

### 1. trend_consolidation_short
Reżim: TREND_DOWN lub IMPULSE_DOWN
- 4-10 świec H1 konsoliduje się w zakresie ≤ ATR × 2.5
- Wejście: górna 1/3 konsolidacji (pullback w górę)
- SL: powyżej szczytu konsolidacji + margines
- TP1: zasięg konsolidacji odmierzony w dół od dołu konsolidacji
- TP2: 1.5-2× zasięg konsolidacji poniżej dołu

### 2. trend_retest_short
Reżim: TREND_DOWN
- Cena retestuje przebity support (teraz opór), który jest powyżej aktualnej ceny
- Wejście: przy strefie retestowanego oporu
- SL: powyżej strefy retestowanego oporu
- TP1: poprzedni swing low
- TP2: nowy dołek wynikający z kontynuacji

### 3. trend_pullback_long
Reżim: TREND_UP (impulse_strength ≥ 5 lub wyraźna struktura HH/HL)
- Pullback do strefy Fibonacci 38-50% ostatniego swingu wzrostowego
- Wejście: strefa fib38-50%
- SL: poniżej fib61.8% - margines
- TP1: poprzedni szczyt swingu
- TP2: szczyt + 30% zasięgu swingu

### 4. trend_consolidation_long ← KLUCZOWY SETUP
Reżim: TREND_UP
- WARUNKI JAKOŚCI (wszystkie muszą być spełnione):
  a) Wolumen podczas konsolidacji (4-10 świec H1) maleje lub jest niższy od vol_ratio < 1.0 — zdrowe wyczekiwanie, nie dystrybucja
  b) Konsolidacja tworzy się przy wcześniejszym poziomie oporu (który stał się wsparciem) lub w strefie Fibonacci 38-50%
  c) Poprzedni impuls wzrostowy musi być wyraźny: ≥ 3 zielone świece H1 z rosnącym wolumenem LUB zmiana 4h ≥ 2%
  d) Konsolidacja NIE może być w górnych 70% H1 range — zbyt blisko resistance, ryzyko odrzucenia
  e) Struktura H1 musi pokazywać HH/HL (nie LH/LL)
- Wejście: dolna 1/3 konsolidacji (pullback w dół w ramach konsolidacji)
- SL: poniżej dołu konsolidacji - margines
- TP1: zasięg konsolidacji odmierzony w górę od szczytu konsolidacji
- TP2: 1.5-2× zasięg konsolidacji powyżej szczytu
- UWAGA: Jeśli warunki jakości nie są spełnione, NIE generuj tego setupu. Mechaniczne wybicia bez potwierdzenia wolumenu i poziomu historycznie zawodzą.

### 5. range_support_long / range_resistance_short
Reżim: RANGE
- Long: cena przy dolnych 15% H1 range, SL 1× ATR poniżej support, TP1 środek range, TP2 resistance
- Short: cena przy górnych 15% H1 range, SL 1× ATR powyżej resistance, TP1 środek range, TP2 support

## Model oceny setupu (5 filarów, 0-3 pkt każdy, max 15)

1. Trend: 0=pod dominujący ruch, 1=niejasny, 2=umiarkowana zgodność, 3=wysoka zgodność lub mocny reversal
2. Struktura: 0=chaos, 1=częściowy układ, 2=widoczny HH/HL lub LH/LL+trigger, 3=bardzo czytelna z miejscem unieważnienia
3. Poziom: 0=przypadkowy, 1=słaby, 2=lokalnie istotny, 3=range edge/swing/retest wielokrotny
4. Momentum: 0=brak przewagi, 1=mieszane, 2=umiarkowana, 3=silny impuls/wyraźna przewaga wolumenu
5. RR: 0=zły, 1=przeciętny, 2=dobry, 3=bardzo dobry i logiczny strukturalnie

Wynik ≥ 10/15 → send_alert = true.

## Zasady decyzyjne

1. Zweryfikuj regime_hint na podstawie własnej analizy danych — potwierdź lub nadpisz
2. Oceń kontekst H1: trend, wsparcia/opory, pozycja w range
3. Oceń kontekst M15: bieżąca struktura, ostatni impuls, korekta/kontynuacja
4. Wybierz maksymalnie 1 najlepszy setup (najwyższy score, przy remisie — najlepsze RR)
5. Jeśli brak setupu 10/15+, nadal zwróć bias, bias_proc, tf_aligned, analiza, akcja

Zasady wykonawcze:
- Sentyment może tylko wzmacniać lub osłabiać istniejący setup, nigdy go nie tworzy
- tf_aligned = true tylko gdy H1 i M15 realnie wspierają ten sam kierunek
- SL logiczny strukturalnie, nie sztucznie zawężony
- TP wynika z kolejnych poziomów strukturalnych, nie z okrągłych liczb
- bias_proc: liczba całkowita 0-100
- rr: liczba dodatnia
- Pozycja w H1 range > 80%: proponuj long tylko przy potwierdzonym wybiciu z retestem, inaczej short lub brak
- Pozycja w H1 range < 20%: proponuj short tylko przy potwierdzonym przełamaniu, inaczej long lub brak

## Format wyjścia JSON

Masz zwrócić WYŁĄCZNIE poprawny JSON. Bez markdownu. Bez bloków ```json.

### Gdy setup istnieje:
{"send_alert":true,"regime_confirmed":"TREND_UP","regime_override_reason":null,"setup_type":"trend_consolidation_long","bias":"long","bias_proc":72,"tf_aligned":true,"sentyment":"brak danych","analiza":"opis analizy H1/M15","wejscia":[{"poziom":124.50,"warunek":"zamknięcie H1 powyżej 124.80"}],"tp1":127.00,"tp2":129.50,"sl":122.80,"sl_after_tp1":123.00,"rr":2.1,"akcja":"opis akcji"}

### Gdy setup nie istnieje:
{"send_alert":false,"regime_confirmed":"RANGE","regime_override_reason":null,"bias":"neutral","bias_proc":50,"tf_aligned":false,"sentyment":"brak danych","analiza":"...","akcja":"..."}

## Ograniczenia pól

- bias: "long", "short" lub "neutral"
- regime_confirmed: jeden z: "IMPULSE_UP", "IMPULSE_DOWN", "TREND_UP", "TREND_DOWN", "RANGE"
- regime_override_reason: null jeśli potwierdzasz hint, krótki string jeśli nadpisujesz
- setup_type: tylko gdy send_alert = true, jeden z dozwolonych typów powyżej
- wejscia, tp1, tp2, sl, sl_after_tp1, rr, setup_type: tylko gdy send_alert = true
- jeśli send_alert = false: tylko send_alert, regime_confirmed, regime_override_reason, bias, bias_proc, tf_aligned, sentyment, analiza, akcja
- sentyment: krótkie podsumowanie, jeśli brak danych wpisz "brak danych"
- analiza i akcja: konkretne i praktyczne, bez ogólników"""


# ── Pomocnicze funkcje techniczne (standalone — bez importu sol_alert) ────────
def _calc_atr_bt(candles: list[dict], period: int = 14) -> float:
    """Oblicza ATR (14-period) z listy świec OHLCV."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low  = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return 0.0
    recent = trs[-period:] if len(trs) >= period else trs
    return sum(recent) / len(recent)


def _detect_regime_bt(
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
) -> dict:
    """
    Uproszczona detekcja reżimu dla backtestu (bez EMA, struktura swingów).
    Zwraca dict zgodny z formatem detect_market_regime() z sol_alert.py.
    """
    # Support / resistance z ostatnich 32 świec H1
    h1_slice = candles_h1[-32:] if len(candles_h1) >= 32 else candles_h1
    support    = min(c["low"]  for c in h1_slice) if h1_slice else current_price * 0.95
    resistance = max(c["high"] for c in h1_slice) if h1_slice else current_price * 1.05
    range_size = resistance - support

    # Zmiany procentowe
    if len(candles_h1) >= 24:
        price_24h_ago = candles_h1[-24]["close"]
        change_24h = (current_price - price_24h_ago) / price_24h_ago * 100
    else:
        change_24h = 0.0

    if len(candles_h1) >= 48:
        price_48h_ago = candles_h1[-48]["close"]
        change_48h = (current_price - price_48h_ago) / price_48h_ago * 100
    else:
        change_48h = 0.0

    # Volume ratio (ostatnie 2 M15 vs avg 10)
    if len(candles_m15) >= 12:
        recent_vol = (candles_m15[-1]["volume"] + candles_m15[-2]["volume"]) / 2
        avg_vol = sum(c["volume"] for c in candles_m15[-12:-2]) / 10
        vol_ratio = round(recent_vol / avg_vol, 1) if avg_vol > 0 else 1.0
    else:
        vol_ratio = 1.0

    # Zmiana 4h (ostatnie 16 świec M15)
    if len(candles_m15) >= 16:
        price_4h_ago = candles_m15[-16]["close"]
        change_4h = (current_price - price_4h_ago) / price_4h_ago * 100
    else:
        change_4h = 0.0

    # Klasyfikacja reżimu
    score = 0
    direction = "none"

    # Impulse detection
    imp_score = 0
    if vol_ratio >= 1.5:
        imp_score += 1
    if abs(change_4h) >= 2.0 or (abs(change_4h) >= 1.5 and vol_ratio >= 1.3):
        imp_score += 1
    last4 = candles_m15[-4:] if len(candles_m15) >= 4 else candles_m15
    bull_count = sum(1 for c in last4 if c["close"] > c["open"])
    bear_count = sum(1 for c in last4 if c["close"] < c["open"])
    if bull_count >= 3:
        imp_score += 1
    elif bear_count >= 3:
        imp_score += 1

    if imp_score >= 2:
        score = imp_score
        direction = "up" if change_4h > 0 else "down"
        regime = "IMPULSE_UP" if direction == "up" else "IMPULSE_DOWN"
        return {
            "regime": regime, "direction": direction, "score": min(score, 10),
            "support": round(support, 4), "resistance": round(resistance, 4),
            "range_size": round(range_size, 4), "vol_ratio": vol_ratio,
            "change_24h": round(change_24h, 2), "change_48h": round(change_48h, 2),
        }

    # Trend detection
    trend_score = 0
    if abs(change_24h) >= 3.0:
        trend_score += 2
    elif abs(change_24h) >= 1.5:
        trend_score += 1
    if abs(change_48h) >= 5.0:
        trend_score += 2
    elif abs(change_48h) >= 3.0:
        trend_score += 1

    if trend_score >= 2 and abs(change_24h) >= 1.5:
        direction = "up" if change_24h > 0 else "down"
        regime = "TREND_UP" if direction == "up" else "TREND_DOWN"
        return {
            "regime": regime, "direction": direction, "score": min(trend_score, 10),
            "support": round(support, 4), "resistance": round(resistance, 4),
            "range_size": round(range_size, 4), "vol_ratio": vol_ratio,
            "change_24h": round(change_24h, 2), "change_48h": round(change_48h, 2),
        }

    return {
        "regime": "RANGE", "direction": "none", "score": 0,
        "support": round(support, 4), "resistance": round(resistance, 4),
        "range_size": round(range_size, 4), "vol_ratio": vol_ratio,
        "change_24h": round(change_24h, 2), "change_48h": round(change_48h, 2),
    }


def build_gpt3_user_prompt(
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
    regime_hint: dict | None = None,
    atr: float | None = None,
    volume_ratio: float | None = None,
    price_pct_in_range: float | None = None,
    support: float | None = None,
    resistance: float | None = None,
) -> str:
    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-100:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-50:]
    )

    # Kontekst strukturalny
    ctx_lines = [f"aktualna cena SOL: ${current_price:.2f}"]
    if support is not None and resistance is not None:
        ctx_lines.append(f"support H1: ${support:.2f} | resistance H1: ${resistance:.2f}")
    if price_pct_in_range is not None:
        ctx_lines.append(f"pozycja w H1 range: {price_pct_in_range:.0f}% (0%=support, 100%=resistance)")
    if atr is not None:
        ctx_lines.append(f"ATR(14): ${atr:.3f}")
    if volume_ratio is not None:
        ctx_lines.append(f"volume_ratio (2M15/avg10): {volume_ratio:.2f}")
    ctx_lines.append("sentyment: brak danych")

    # Regime hint
    if regime_hint:
        r = regime_hint
        regime_str = (
            f"regime_hint: {r.get('regime', '?')} "
            f"(score={r.get('score', 0)}, direction={r.get('direction', '?')}, "
            f"24h={r.get('change_24h', 0):+.1f}%, 48h={r.get('change_48h', 0):+.1f}%, "
            f"vol_ratio={r.get('vol_ratio', 0):.2f})"
        )
        ctx_lines.append(regime_str)
    else:
        ctx_lines.append("regime_hint: brak (ustal samodzielnie)")

    ctx_block = "\n".join(f"- {l}" for l in ctx_lines)

    return (
        "Przeanalizuj SOL/USDT i zwróć wyłącznie poprawny JSON zgodny z wymaganym formatem.\n\n"
        "Świece są ułożone chronologicznie od najstarszej do najnowszej.\n"
        "Ostatni wiersz to ostatnia zamknięta świeca. Aktualna cena jest nowsza.\n\n"
        f"Kontekst:\n{ctx_block}\n\n"
        f"H1 candles (50):\n{h1_csv}\n\n"
        f"M15 candles (100):\n{m15_csv}\n\n"
        "Wymagania:\n"
        "- zweryfikuj regime_hint lub nadpisz z uzasadnieniem w regime_override_reason\n"
        "- oceń kontekst H1 i M15\n"
        "- wybierz 1 najlepszy setup lub brak setupu\n"
        "- setup < 10/15 → send_alert = false\n"
        "- dla trend_consolidation_long: sprawdź WSZYSTKIE warunki jakości (wolumen, poziom, impuls, pozycja w range)\n"
        "- zwróć wyłącznie poprawny JSON, nic więcej"
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

    # Oblicz kontekst strukturalny
    regime = _detect_regime_bt(candles_m15, candles_h1, current_price)
    atr = _calc_atr_bt(candles_m15)
    vol_ratio = regime.get("vol_ratio", 1.0)
    support = regime.get("support")
    resistance = regime.get("resistance")
    range_size = regime.get("range_size", 0)
    if range_size and range_size > 0 and support is not None:
        pct = max(0.0, min(100.0, (current_price - support) / range_size * 100))
    else:
        pct = 50.0

    user_msg = build_gpt3_user_prompt(
        candles_m15, candles_h1, current_price,
        regime_hint=regime,
        atr=atr,
        volume_ratio=vol_ratio,
        price_pct_in_range=pct,
        support=support,
        resistance=resistance,
    )

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
        result = json.loads(match.group())
        # Dołącz regime_hint do wyniku (do logowania)
        result["_regime_hint"] = regime.get("regime", "?")
        return result
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
def get_test_sheet(sheet_suffix: str = "") -> gspread.Worksheet:
    sheet_name = f"GPT3 test {sheet_suffix}".strip() if sheet_suffix else "GPT3 test"
    creds  = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    client = gspread.authorize(creds)
    wb     = client.open_by_key(SHEET_ID)
    try:
        sh = wb.worksheet(sheet_name)
        sh.clear()
    except gspread.WorksheetNotFound:
        sh = wb.add_worksheet(sheet_name, rows=200, cols=len(SHEET_HEADER) + 2)
    sh.append_row(SHEET_HEADER)
    return sh


# ── Główna logika backtestu ───────────────────────────────────────────────────
def run_backtest(from_ts: int, to_ts: int, sheet_suffix: str = "") -> None:
    now_ts    = int(time.time())
    num_hours = (to_ts - from_ts) // 3600

    if num_hours <= 0:
        print(f"[BŁĄD] Nieprawidłowy zakres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)}")
        return

    print("=== GPT3 Backtest — start ===")
    print(f"Okres: {_ts_fmt(from_ts)} – {_ts_fmt(to_ts)} ({num_hours}h, {num_hours} punktów)")

    # ── 1. Pobierz dane historyczne ───────────────────────────────────────────
    outcome_margin_s = ENTRY_WINDOW_S + OUTCOME_WINDOW_S  # 48h na outcome po ostatnim punkcie
    data_end_ts = to_ts + outcome_margin_s
    if data_end_ts > now_ts:
        data_end_ts = now_ts
        margin_h = (data_end_ts - to_ts) / 3600
        print(f"  Outcome data ograniczona do {margin_h:.0f}h po ostatnim punkcie (brak przyszłych danych)")

    m15_total = 100 + num_hours * 4 + (data_end_ts - to_ts) // 900 + 50
    h1_total  = 50  + num_hours     + (data_end_ts - to_ts) // 3600 + 10

    print(f"Pobieranie świec M15 ({m15_total} szt)...")
    all_m15 = fetch_klines_paginated(SYMBOL, "15m", total=m15_total, end_ts_s=data_end_ts)
    print(f"  Pobrano {len(all_m15)} świec M15 ({_ts_fmt(all_m15[0]['time'])} – {_ts_fmt(all_m15[-1]['time'])})")

    print(f"Pobieranie świec H1 ({h1_total} szt)...")
    all_h1 = fetch_klines_paginated(SYMBOL, "1h", total=h1_total, end_ts_s=data_end_ts)
    print(f"  Pobrano {len(all_h1)} świec H1 ({_ts_fmt(all_h1[0]['time'])} – {_ts_fmt(all_h1[-1]['time'])})")

    # ── 2. Wyznacz punkty testowe ─────────────────────────────────────────────
    test_hours = [from_ts + i * 3600 for i in range(num_hours)]

    # ── 3. Przygotuj arkusz ───────────────────────────────────────────────────
    print("Łączenie z Google Sheets...")
    sheet = get_test_sheet(sheet_suffix)
    print("Gotowe.")

    # ── 4. Pętla testowa ──────────────────────────────────────────────────────
    for i, signal_ts in enumerate(test_hours):
        label = _ts_fmt(signal_ts)
        print(f"\n[{i+1}/{num_hours}] {label}")

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
            sheet.append_row([label, "", "", "", "", "", "", "", "", "błąd GPT", "", "", ""])
            continue

        send_alert       = gpt_result.get("send_alert", False)
        bias             = gpt_result.get("bias", "neutral")
        bias_proc        = gpt_result.get("bias_proc", 0)
        regime_confirmed = gpt_result.get("regime_confirmed", gpt_result.get("_regime_hint", ""))
        setup_type       = gpt_result.get("setup_type", "")

        print(f"  send_alert={send_alert} | bias={bias} ({bias_proc}%) | regime={regime_confirmed} | setup={setup_type}")

        if not send_alert or bias == "neutral":
            sheet.append_row([label, regime_confirmed, "", "null", bias_proc, "", "", "", "", "no entry", "", "", ""])
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
            regime_confirmed,
            setup_type,
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

    sheet_name = f"GPT3 test {sheet_suffix}".strip() if sheet_suffix else "GPT3 test"
    print("\n=== Backtest zakończony ===")
    print(f"Wyniki zapisane w arkuszu '{sheet_name}' (SHEET_ID={SHEET_ID})")


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _parse_dt(s: str) -> int:
    """Parsuje datę 'YYYY-MM-DD HH:MM' (UTC) na unix timestamp."""
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="dt_from", type=str, default=None,
                        help="Początek okresu UTC, np. '2026-03-25 00:00'")
    parser.add_argument("--to", dest="dt_to", type=str, default=None,
                        help="Koniec okresu UTC, np. '2026-03-28 00:00'. Puste = teraz")
    parser.add_argument("--hours", type=int, default=None,
                        help="Ile godzin wstecz od --to (lub od teraz). Ignorowane gdy podano --from.")
    parser.add_argument("--sheet-suffix", default="", help='Sufiks nazwy arkusza (np. "v2" → "GPT3 test v2")')
    args = parser.parse_args()

    now_ts = int(time.time())

    if args.dt_from and args.dt_to:
        from_ts = _parse_dt(args.dt_from)
        to_ts   = _parse_dt(args.dt_to)
    elif args.dt_from:
        from_ts = _parse_dt(args.dt_from)
        to_ts   = (now_ts // 3600) * 3600
    elif args.dt_to:
        to_ts   = _parse_dt(args.dt_to)
        from_ts = to_ts - (args.hours or 48) * 3600
    else:
        to_ts   = (now_ts // 3600) * 3600
        from_ts = to_ts - (args.hours or 48) * 3600

    # Zaokrąglij do pełnych godzin
    from_ts = ((from_ts + 3599) // 3600) * 3600
    to_ts   = (to_ts // 3600) * 3600

    run_backtest(from_ts=from_ts, to_ts=to_ts, sheet_suffix=args.sheet_suffix)
