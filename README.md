# SOL Alert Bot

Automatyczny skrypt wykrywający setupy tradingowe na **SOL/USDT** i wysyłający alerty na Telegram.
Działa na GitHub Actions — sprawdza rynek co 15 minut, nie wymaga żadnego serwera.

---

## Jak działa

1. Co 15 minut GitHub uruchamia skrypt
2. Skrypt pobiera świece M15 i H1 z CryptoCompare API
3. Szuka dwóch typów setupów: **Range Trading** i **Breakout Retest**
4. Ocenia setup w skali 0–15 pkt (5 filarów × 3 pkt)
5. Jeśli score ≥ 11 i cena zmierza ku poziomowi → alert na Telegram
6. Setupy ≥ 10 pkt są śledzone i wyniki trafiają do Google Sheets

---

## Model oceny setupu (15 pkt)

| Filar | Max | Opis |
|-------|-----|------|
| Siła poziomu | 3 | Ile razy cena respektowała S/R |
| Potencjał ruchu | 3 | Szerokość zakresu (sweet spot 1.2–2.0 USD) |
| Kontekst rynku | 3 | Trend H1 zgodny z kierunkiem |
| RR | 3 | Relacja ryzyko/zysk (min 1.5:1) |
| Impuls | 3 | Siła poprzedzającego ruchu |

**Progi:**
- ≥ 11/15 → alert Telegram
- ≥ 10/15 → śledzenie w Google Sheets

---

## Alert na Telegram

Skrypt wysyła alert **z wyprzedzeniem** — gdy cena zbliża się do poziomu (10–35% szerokości zakresu), nie gdy już tam jest. Dzięki temu masz czas ustawić zlecenia limit.

Format alertu:
```
🎯 SOL/USDT – Range [12/15]
📈 Long  |  20.03  14:30

Cena teraz: $89.40
Strefa wejscia za: ~$0.45

Poziom 3 · Ruch 3 · Kontekst 2 · RR 2 · Impuls 2

Ustaw zlecenia:
  W1: $88.95
  W2: $88.70
  W3: $88.50

SL:  $88.25

Cele:
  TP1: $89.80  (+$0.85)
  TP2: $90.60  (+$1.65)

RR:  2.2:1
```

---

## Śledzenie wyników

Każdy setup ≥ 10/15 jest automatycznie weryfikowany:
- **2h na wejście** — czy cena osiągnęła W1
- **24h na wynik** — czy najpierw TP1, TP2 czy SL

Wyniki trafiają do Google Sheets (arkusz `SOL Alert Log`).

---

## Pliki

```
sol_alert.py                  — główny skrypt
requirements.txt              — zależności Python
.github/workflows/sol_alert.yml — harmonogram GitHub Actions
```

**Pliki tworzone automatycznie (cache GitHub Actions):**
```
last_alert.json       — anty-spam (cooldown 4h)
pending_setups.json   — setupy czekające na weryfikację
```

---

## Konfiguracja (GitHub Secrets)

| Secret | Opis |
|--------|------|
| `TELEGRAM_TOKEN` | Token bota Telegram (z @BotFather) |
| `TELEGRAM_CHAT_ID` | Twoje Chat ID (z @userinfobot) |
| `GOOGLE_CREDENTIALS` | Zawartość JSON konta serwisowego Google |

---

## Parametry do kalibracji (w sol_alert.py)

| Parametr | Wartość | Opis |
|----------|---------|------|
| `MIN_SCORE` | 11 | Minimalny próg alertu |
| `TRACK_MIN_SCORE` | 10 | Minimalny próg śledzenia |
| `COOLDOWN_HOURS` | 4 | Cisza po tym samym setupie |
| `ENTRY_TIMEOUT_H` | 2 | Max godzin na wejście |
| `TRADE_TIMEOUT_H` | 24 | Max godzin na wynik |
| `SHEET_ID` | `19TWH...` | ID arkusza Google Sheets |

---

## Ręczne uruchomienie

GitHub → zakładka **Actions** → **SOL Alert** → **Run workflow**

---

## Następne kroki

Po zebraniu kilkunastu rozwiązanych setupów — analiza wyników i kalibracja parametrów.
Docelowo: rozbudowa o automatyczne składanie zleceń przez Bybit API.
