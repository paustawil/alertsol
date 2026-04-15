# AlertSol — Przegląd systemu

## 1. Zbędny kod — co można usunąć

### 1.1 Martwy kod produkcyjny (`sol_alert.py`)

| Flaga | Linia | Co zawiera | Uwagi |
|-------|-------|-----------|-------|
| `ENABLE_CLAUDE = False` | 43, blok 4097–4130 | Prompt `FORTECA_PROMPT` (~350 linii) + `call_claude()` | Tymczasowo wyłączony, kod zachowany |
| `ENABLE_GPT = False` | 44, blok 4133–4166 | GPT jako samodzielny detektor setupów | Zastąpiony Algo2 |
| `ENABLE_GROK = False` | 48, blok 4169–4235 | Grok jako samodzielny detektor setupów | Zastąpiony Algo2 |
| `ENABLE_GPT_RELAXED = False` | 45, blok 4258–4316 | Przestarzały wariant promptu GPT | Zastąpiony GPT3 Validator |
| `ENABLE_GPT3 = False` | 46, blok 4319–4404 | Standalone GPT3 detektor | Validator aktywny, detektor nie |

Dodatkowy martwy kod:
- `trend_consolidation_short` — otoczony `if False:` (linia 1735) — WR za niski
- `trend_consolidation_long` — wyłączony (linia 1870) — WR 31%, stratny
- `check_open_setups_invalidation()` — logika reżimu zakomentowana (linia 3723), pozostaje tylko timeout; funkcja może być uproszczona

### 1.2 Pliki archiwalne / jednorazowe

**Migrator (jednorazowe użycie):**
- `migrate_json_to_db.py` — migracja JSON→DB już wykonana, bezużyteczny w produkcji

**Skrypty backtestowe (nie uruchamiane przez scheduler, brak importu produkcyjnego):**
- `backtest.py`, `grok_backtest.py`, `grok2_backtest.py`
- `gpt3_backtest.py`, `gpt3_validator_backtest.py`
- `gpt4_backtest.py`, `gpt5_backtest.py`, `gpt_relaxed_backtest.py`
- `impulse_backtest.py`, `range_backtest.py`
- `diagnose_regime.py`, `diagnose_positions.py`
- `test_exchange.py`, `test_apr3.py`

**Zbędne importy (gdy wszystkie flagi `False`):**
- `ANTHROPIC_KEY`, `OPENAI_KEY`, `XAI_KEY` importowane mimo wyłączonych modeli
- `import anthropic`, `import openai` w `sol_alert.py` — niepotrzebne gdy wszystkie flagi `False`

---

## 2. Cykl życia setupu

### 2.1 Tworzenie setupu

**Źródło:** `sol_alert.py::main()` + `algo_detect_setups()` + `save_pending()`

Scheduler (`main_runner.py`) co 5 min wywołuje `run_sol_alert()` → `sol_alert.main()`:

1. **Pobieranie danych:** `fetch_klines()` → Bitget API, świece M15 (100 szt.) i H1 (50 szt.)
2. **Wykrycie reżimu:** `detect_market_regime()` → `RANGE` / `TREND_UP` / `TREND_DOWN` / `IMPULSE_UP` / `IMPULSE_DOWN`
3. **Detekcja Algo2:** `algo_detect_setups(regime, m15, h1, price)` → lista kandydatów
4. **Filtr GPT3 Validator** (jeśli `ENABLE_GPT3_VALIDATOR=True` i nie IMPULSE): `call_gpt3_validator()` → akceptuje / odrzuca kandydatów
5. **Zapis przez `save_pending()`:**
   - Deduplikacja (patrz sekcja 2.5)
   - Wyznaczenie `entry_trigger` (`rising` / `falling`) na podstawie relacji W1 vs cena
   - `db.insert_setup()` z `shadow=ALGO2_SHADOW_MODE` (obecnie `True`)
   - Powiadomienie Telegram

**Throttle wewnętrzny Algo2:** 15 min dla RANGE/TREND, 5 min dla IMPULSE.

### 2.2 Typy setupów

| Typ | Reżim | Kierunek | Logika wejścia | Status |
|-----|-------|----------|----------------|--------|
| `trend_pullback_short` | TREND_DOWN / IMPULSE_DOWN | short | Fib 38–50% korekty, W1 powyżej ceny | **aktywny** |
| `trend_pullback_long` | TREND_UP / IMPULSE_UP | long | Fib 38–50% korekty, W1 poniżej ceny, strength ≥ 5 | **aktywny** |
| `impulse_continuation_short` | IMPULSE_DOWN | short | Mini-pullback 1–2 zielone M15 z 6, spike_score < 2 | **aktywny** |
| `impulse_continuation_long` | IMPULSE_UP | long | Mini-pullback 1–2 czerwone M15 z 6, spike_score < 2 | **aktywny** |
| `range_resistance_short` | RANGE | short | Potwierdzone zamknięcie M15 poniżej oporu (od 2026-04-14) | **aktywny** |
| `range_support_long` | RANGE | long | Potwierdzone zamknięcie M15 powyżej wsparcia (od 2026-04-14) | **aktywny** |
| `impulse_aggressive_short` | IMPULSE_DOWN | short | Market entry, vol ≥ 2.0x, `force_shadow=True` | testowy (zawsze shadow) |
| `impulse_aggressive_long` | IMPULSE_UP | long | Market entry, vol ≥ 2.0x, `force_shadow=True` | testowy (zawsze shadow) |
| `trend_consolidation_short` | TREND_DOWN | short | Konsolidacja przy oporze | **wyłączony** (`if False:`) |
| `trend_consolidation_long` | TREND_UP | long | Konsolidacja przy wsparciu | **wyłączony** (`if False:`) |

### 2.3 Składanie zleceń na Bitget

**Warunek wejścia na giełdę:**
```
shadow = False
AND cancel_reason IS NULL
AND exchange_plan_oid IS NULL
AND entry_hit_at IS NULL
```

> **Aktualnie:** `ALGO2_SHADOW_MODE = True` → wszystkie Algo2 setupy mają `shadow=True`
> → **żadne zlecenia nie są składane na Bitget** — system działa w trybie śledzenia wirtualnego.

Gdy `shadow=False`, `exchange_trader.sync()` (co 15 sek, `main_runner.py:134`) wykonuje:

1. `db.claim_plan_order(setup_id)` — atomicznie rezerwuje slot (ustawia `exchange_plan_oid='PENDING'`)
2. `_place_entry_plan_orders()` — dwa plan ordery na Bitget:
   - Plan 1: `half_qty` @ W1, preset TP=TP1, preset SL
   - Plan 2: `half_qty` @ W1, preset TP=TP2, preset SL
   - Po wyzwoleniu: Bitget auto-tworzy 4 TPSL ordery
3. Guard limitu pozycji: `MAX_POSITIONS` (domyślnie 5, env `BITGET_MAX_POSITIONS`) per kierunek
4. Weryfikacja `_get_open_position_size()` → jeśli qty=0, zwolnienie slotu

**Obliczanie wielkości pozycji** (`exchange_trader.py`):
```python
full_qty = floor((TRADE_USDT * LEVERAGE) / W1 / 0.1) * 0.1
half_qty = floor(full_qty / 2 / 0.1) * 0.1
```
Przykład: W1=$150, TRADE_USDT=100, LEVERAGE=20 → full_qty ≈ 13.3 SOL → half_qty ≈ 6.6 SOL

### 2.4 Śledzenie wejścia (tryb shadow)

`check_pending()` działa na świecach M15 i wykrywa przejścia:

- **Wejście:** `_hits(candle, level, trigger)` → zapisuje `entry_hit_at`, `avg_entry`, status → `open`
- **TP1 hit:** zapisuje `tp1_hit_at`, status → `after_tp1`, przesuwa SL → `sl_after_tp1`
- **TP2 / SL hit:** `db.resolve_setup()`, status → `closed`

Timeout wejścia: `ENTRY_TIMEOUT_H = 4h` (linia 38) — zarówno dla shadow jak i Bitget.
Timeout pozycji otwartej: `OPEN_TRADE_TIMEOUT_H = 16h` (linia 40).

### 2.5 Deduplikacja setupów

Logika w `save_pending()` (linia 2833), aktywna nawet gdy `shadow=True` dla Algo2:

| Różnica W1 stary vs nowy | Akcja |
|--------------------------|-------|
| < $0.10 (`REPLACE_MIN_DIFF`) | Prawdziwy duplikat → pomiń, nie zapisuj |
| $0.10 – $0.50 (`REPLACE_MAX_DIFF`) | Zaktualizowane poziomy → anuluj stary, wstaw nowy |
| > $0.50 | Osobny setup → zachowaj oba |

Dodatkowy guard na poziomie DB (`db.py:200`):
```python
SELECT pg_advisory_xact_lock(hashtext(lock_key))
-- + WHERE NOT EXISTS (...)
```
Blokada race condition przy równoległych zapisach.

### 2.6 Anulowanie setupów

**A. Stale — `check_stale_setups()` (linia 3548)** — dla setupów pending (nie weszły):
- Cena uciekła > 5% (`STALE_DIST_PCT`) od W1 → anulowany
- Cena przebiła TP1 bez wejścia → anulowany
- ~~Zmiana reżimu~~ → **WYŁĄCZONE** (zakomentowane, linia 3579)

**B. Open invalidation — `check_open_setups_invalidation()` (linia 3691)** — dla setupów w pozycji:
- Timeout > 16h od `entry_hit_at` → zamknięcie market orderem
- ~~Zmiana reżimu → BE / zamknięcie~~ → **WYŁĄCZONE** (zakomentowane, linia 3723)

**C. Ręczne / przez dashboard:**
- `POST /api/cancel-setup/{id}` → ustawia `shadow=True` + `cancel_reason`
- `exchange_trader` wykrywa to przy następnym `sync()` i anuluje plan order na Bitget przez `get_resolved_with_open_orders()`

---

## 3. Wyłączone warianty

| Wariant | Plik | Linia | Status | Powód wyłączenia |
|---------|------|-------|--------|-----------------|
| Claude jako detektor | `sol_alert.py` | 4097 | `ENABLE_CLAUDE=False` | Tymczasowo — kod zachowany |
| GPT jako detektor | `sol_alert.py` | 4133 | `ENABLE_GPT=False` | Zastąpiony Algo2 |
| GPT Relaxed | `sol_alert.py` | 4258 | `ENABLE_GPT_RELAXED=False` | Zastąpiony GPT3 Validator |
| Standalone GPT3 | `sol_alert.py` | 4319 | `ENABLE_GPT3=False` | Validator aktywny, detektor nie |
| Grok jako detektor | `sol_alert.py` | 4169 | `ENABLE_GROK=False` | Zastąpiony Algo2 |
| `trend_consolidation_short` | `sol_alert.py` | 1735 | `if False:` | WR < 50%, wymaga przeprojektowania |
| `trend_consolidation_long` | `sol_alert.py` | 1870 | `if False:` | WR 31%, stratny |
| Reżim jako kryterium anulowania | `sol_alert.py` | 3579 | zakomentowane | Algorytm trendu zbyt zawodny |
| Reżim jako kryterium inwalidacji | `sol_alert.py` | 3723 | zakomentowane | Fałszywe triggery po pullbackach |
| Składanie zleceń na Bitget | `exchange_trader.py` | — | `ALGO2_SHADOW_MODE=True` | Tryb obserwacji shadow |

---

## 4. Scheduler — harmonogram zadań

| Job | Częstotliwość | Funkcja | Opis |
|-----|--------------|---------|------|
| `exchange_monitor` | co 15 sek | `exchange_trader.sync()` | Monitorowanie zleceń na Bitget |
| `sol_alert` | co 5 min | `sol_alert.main()` | Detekcja setupów (throttle: 15min RANGE/TREND, 5min IMPULSE) |
| `breakout_scan` | co 3 min | `sol_alert.breakout_scan()` | Szybki scan breakoutów |
| `sheets_export` | co 5 min | `run_sheets_export()` | Eksport wyników do Google Sheets |
| `profit_calculator` | co 1h | `run_profit_calculator_export()` | Kalkulator PnL do arkusza |
| `grok_shadow` | co 5 min | `run_grok_shadow()` | Grok shadow (throttle: 30min RANGE/TREND, 5min IMPULSE) |

---

## 5. Propozycje monitoringu i analizy

### 5.1 Brakujące metryki w obecnym dashboardzie

- Brak per-typ-setupu statystyk (pullback vs range vs impulse) — jest tylko per-model
- Brak analizy czasowej: o której godzinie / dniu setupy wchodzą najczęściej
- Brak histogramu czasu trzymania pozycji (od entry do exit)
- Brak rozkładu RR: ile setupów z RR > 2 rzeczywiście osiągało TP2
- Brak widoku "hypo vs real" — porównanie setupów które weszły vs `hypo_pnl_usd` dla tych co nie weszły
- Brak alertu gdy TP2 nie jest osiągany mimo TP1 (statystyki TP1+BE / TP1+SL)

### 5.2 Szybkie usprawnienia (bez LLM)

Nowe zapytania SQL w `db.py` + karty w dashboardzie:

```
GET /api/type-stats     → win_rate, avg_pnl, count per type (pullback/range/impulse)
GET /api/time-heatmap   → godzina alertu vs % entry rate / win rate
GET /api/rr-analysis    → faktyczny RR vs zadeklarowany RR per typ
```

Propozycja tabeli "Statystyki per typ setupu":

| Typ | Liczba | % wejść | WR | Avg PnL | TP2% | Czas do wejścia |
|-----|--------|---------|-----|---------|------|----------------|
| pullback_short | — | — | — | — | — | — |
| pullback_long | — | — | — | — | — | — |
| range_resistance | — | — | — | — | — | — |
| range_support | — | — | — | — | — | — |
| impulse_continuation | — | — | — | — | — | — |

### 5.3 Moduł analizy LLM w panelu

Nowy endpoint `POST /api/llm-analysis` + sekcja w dashboardzie.

**Architektura:**
- Backend: FastAPI endpoint przyjmuje `{"scope": "pullback", "period": "30d", "direction": "all"}`
- Pobiera dane z DB (ostatnie N setupów danego typu, wyniki, parametry)
- Buduje prompt dla Claude (`claude-sonnet-4-6` lub `claude-haiku-4-5` dla tańszego wariantu)
- Zwraca structured JSON: `{summary, key_findings, issues, recommendations, data_table}`
- Frontend: przycisk "Analizuj" per scope, overlay z wynikami, cache 10 min

**Szablon promptu:**
```
Jesteś analitykiem systemu tradingowego. Oto dane z ostatnich 30 dni
dla setupów typu {scope}:

[Tabela: setup_id, direction, type, regime_at_alert, rr, avg_entry,
 avg_exit, result, pnl_usd, hold_time_h, entry_trigger, reasoning]

Odpowiedz na pytania:
1. Jaki jest rzeczywisty WR i średni RR dla tych setupów?
2. Czy istnieje różnica między LONG a SHORT?
3. Które reżimy rynkowe dawały najlepsze wyniki?
4. Jakie wzorce są widoczne w setupach zakończonych SL?
5. Rekomendacje: co zmienić w logice wykrywania?

Odpowiedź w języku polskim, format JSON:
{summary, findings: [...], concerns: [...], recommendations: [...]}
```

> Pole `reasoning` z DB zawiera log algorytmu dla każdego setupu — LLM może je analizować bezpośrednio.

**Dodatkowy scope: `weekly_report`** — automatyczny raport tygodniowy wysyłany Telegramem w każdy poniedziałek (nowe zadanie schedulera).

---

## 6. Plan implementacji kolejnych kroków

### Krok 3 — Czyszczenie kodu (za zgodą)
- Usunąć bloki za flagami `ENABLE_*=False` (lub przenieść do `archive/`)
- Przenieść pliki backtest/diagnose/test do folderu `tools/`
- Uprościć `check_open_setups_invalidation()` do samego timeoutu

### Krok 4 — Moduł LLM analizy (za zgodą)
- Nowe zapytania w `db.py`: `get_type_stats()`, `get_setups_for_analysis(scope, period)`
- Nowy endpoint w `main_runner.py`: `POST /api/llm-analysis`
- Sekcja w dashboardzie HTML z przyciskami analizy per typ setupu
- Integracja z Claude API (biblioteka `anthropic` już zaimportowana w `sol_alert.py`)

### Weryfikacja po zmianach
```bash
python -c "import sol_alert; import exchange_trader; import db"
# Sprawdzić czy /api/stats i / działają
```
