# SOL Alert Bot

Automatyczny system tradingowy dla **SOL/USDT** — wykrywa setupy, wysyła alerty Telegram i automatycznie składa zlecenia na Bitget Futures.

Działa jako serwis **Railway** (FastAPI + APScheduler), nie wymaga GitHub Actions.

---

## Jak działa

1. Co 15 minut skrypt pobiera świece M15 i H1 z **Bitget API** (SOLUSDTU perpetual)
2. Kilka modeli AI analizuje setup równolegle (Grok, GPT)
3. Jeśli setup jest wystarczająco dobry → alert Telegram
4. **Automatycznie składany plan order** na Bitget przy poziomie W1
5. Gdy pozycja się otworzy → automatyczne zlecenia TP1, TP2, SL
6. Monitoring pozycji co 15 sekund — TP1 przesuwa SL na break-even
7. Wyniki trafiają do Google Sheets

---

## Architektura

```
Railway (always-on)
├── FastAPI + dashboard (/)
├── APScheduler
│   ├── co 15 min → sol_alert.main()    — detekcja setupów + weryfikacja pending
│   ├── co 15 sek → exchange_trader.sync() — monitoring pozycji na Bitget
│   └── co 5 min  → sheets_export()    — eksport wyników do Google Sheets
└── PostgreSQL (Railway Postgres)

Bitget Futures API
├── Plan orders (wejście przy W1)
├── TPSL orders (TP1, TP2, SL)
└── Position monitoring
```

---

## Modele AI

| Model | Rola |
|-------|------|
| **Grok** (xAI) | Główna detekcja setupów — ma dostęp do internetu (sentyment, BTC/ETH/F&G) |
| **GPT-4o** | Drugi model (relaxed prompt) — weryfikacja |
| **Grok Validation** | Walidacja oczekujących setupów — czy nadal aktualne |

---

## Typy setupów (Forteca v1.0)

- **Range Trading** — odbicie od wsparcia/oporu w konsolidacji
- **Breakout Retest** — powrót do przebitego poziomu
- **False Breakout** — fałszywe wybicie i powrót

---

## Zarządzanie pozycją

- **W1 / W2** — dwa poziomy wejścia (agresywny i konserwatywny)
- **TP1** — pierwsza realizacja (½ pozycji), SL przesuwa się na SLpoTP1
- **TP2** — pełna realizacja pozostałej ½
- **SL** — stop loss, anuluje wszystkie TP
- Limit aktywnych pozycji: **5** (konfigurowalny przez `BITGET_MAX_POSITIONS`)

---

## Ochrona przed niepełnym wypełnieniem

Po wykonaniu plan ordera system weryfikuje rzeczywistą pozycję przez Bitget API:
- Pozycja = 0 → brak wejścia, slot zwolniony
- Pozycja < 80% oczekiwanej → ostrzeżenie, TPSL złożone na faktycznym rozmiarze
- Manualne zamknięcie pozycji → system wykrywa anulowane TPSL i zamyka setup w bazie

---

## Pliki

```
sol_alert.py          — detekcja setupów, modele AI, weryfikacja pending
exchange_trader.py    — Bitget API, plan orders, TPSL, monitoring pozycji
main_runner.py        — FastAPI dashboard, APScheduler, endpointy
db.py                 — PostgreSQL (setup CRUD, stats, eksport)
schema.sql            — schemat bazy danych
sheets_exporter.py    — eksport do Google Sheets
requirements.txt      — zależności Python
```

---

## Konfiguracja — zmienne Railway

| Zmienna | Opis |
|---------|------|
| `TELEGRAM_TOKEN` | Token bota Telegram |
| `TELEGRAM_CHAT_ID` | Chat ID |
| `ANTHROPIC_API_KEY` | Klucz Anthropic (Claude) |
| `OPENAI_API_KEY` | Klucz OpenAI (GPT) |
| `XAI_API_KEY` | Klucz xAI (Grok) |
| `BITGET_API_KEY` | Klucz Bitget |
| `BITGET_API_SECRET` | Secret Bitget |
| `BITGET_PASSPHRASE` | Passphrase Bitget |
| `BITGET_DEMO` | `true` = tryb demo, brak/`false` = produkcja |
| `BITGET_TRADE_USDT` | Rozmiar pozycji w USDT (domyślnie `100`) |
| `BITGET_MAX_POSITIONS` | Max otwartych pozycji jednocześnie (domyślnie `5`) |
| `GOOGLE_CREDENTIALS` | JSON konta serwisowego Google |
| `DATABASE_URL` | Automatycznie z Railway Postgres |

---

## Dashboard

Railway udostępnia panel pod adresem serwisu:

| Endpoint | Opis |
|----------|------|
| `/` | Dashboard HTML — aktywne setupy, ostatnie wyniki |
| `/health` | Healthcheck + status schedulera |
| `/api/stats` | JSON — statystyki, aktywne, ostatnie wyniki |
| `/docs` | Swagger UI |

---

## PnL

- `pnl_usd` — realny zysk/strata w USD dla danego trade'u (ruch ceny × qty SOL)
- `pnl_pct` — % zwrotu z zainwestowanego kapitału (`pnl_usd / BITGET_TRADE_USDT × 100`)
