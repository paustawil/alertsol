# Algo2 — Historia zmian

Plik dokumentuje zmiany logiki algorytmu detekcji setupów (Algo2 / `algo_detect_setups`,
`detect_market_regime`, `save_pending` i powiązane funkcje w `sol_alert.py`).
Zmiany infrastrukturalne (dashboard, baza, Telegram) są tu pominięte.

---

## 2026-04-12

### Spike-reversal filter dla detekcji TREND
Po spike'u (np. pump do $88 i powrót do $81) `change_24h`/`48h` był sztucznie duży, bo
punkt odniesienia trafił na szczyt spike'a → błędna detekcja TREND_DOWN gdy cena wróciła
do pre-spike range'a.

Nowy filtr (analogiczny do istniejącego dla IMPULSE): jeśli `max_high` z 48h był >3%
powyżej referencji i potencjalny kierunek to DOWN → `trend_score -= 2`. I odwrotnie dla
UP. Filtr kierunkowy — nie penalizuje prawdziwego trendu.

### Hedge mode: limit pozycji per-kierunek
Zamiast jednego globalnego limitu, osobne liczniki dla long i short.
`MAX_POSITIONS=1` oznacza teraz: max 1 long + max 1 short jednocześnie.
Umożliwia prowadzenie range_support_long i range_resistance_short jednocześnie.

### IMPULSE/TREND tylko po przekroczeniu granic range'a
Silny ruch *wewnątrz* range'a (np. z dołu do góry między support a resistance) nie nadpisuje
już reżimu RANGE na IMPULSE/TREND. Cena musi faktycznie przekroczyć granicę:
- `IMPULSE_UP / TREND_UP`: `current_price >= resistance`
- `IMPULSE_DOWN / TREND_DOWN`: `current_price <= support`

### Guard IMPULSE/TREND tylko dla prawdziwego range'a
Powyższy guard aktywuje się tylko gdy range jest potwierdzony: `r_touches >= 2 AND
s_touches >= 2`. W trendzie `detect_range()` zwraca min/max trendu (1 dotknięcie szczytu)
→ `genuine_range = False` → IMPULSE/TREND działa bez blokady.

---

## 2026-04-10

### Histerezy reżimu rynkowego (`get_stable_regime`)
Zmiana kierunku reżimu (`UP ↔ DOWN`) wymaga **2 kolejnych** potwierdzających detekcji
zamiast natychmiastowego akceptowania każdego flip-a. Eliminuje kaskadowe anulowania
setupów i spam sygnałów w warunkach chaotycznych (pullback po gwałtownym ruchu).

Przejścia **natychmiastowe** (nie wymagają potwierdzenia):
- ten sam kierunek: `TREND_UP → IMPULSE_UP`
- do/z RANGE: `IMPULSE_UP → RANGE → IMPULSE_DOWN`

---

## 2026-04-11

### Filtr TP1 margin dla range setupów
Setup RANGE mógł powstawać gdy cena była blisko środka range'a (przy TP1), a następnie
natychmiast był anulowany przez `check_stale_setups` (cena przekroczyła TP1 bez wejścia).

Nowy warunek przy tworzeniu setupu:
- `range_support_long`: `current <= tp1 - rng_size * 0.15`
- `range_resistance_short`: `current >= tp1 + rng_size * 0.15`

Wymagany co najmniej 15% range'a marginesu pomiędzy ceną a TP1.

---

## 2026-04-09

### Spike-reversal filter dla detekcji IMPULSE
Trzy sygnały odwrotu po spike'u (change_1h pod prąd, change_2h pod prąd, rejection wicks
na M15). Gdy `spike_score >= 2` → próg wejścia w IMPULSE rośnie z 3 do 4 punktów.
Wynik widoczny jako `spike_score` w logach detekcji.

### Nowe typy setupów: `impulse_aggressive` i `impulse_continuation`
- **`impulse_continuation_short/long`**: wejście przy mini-pullbacku w trakcie impulsu
  (1-2 zielone/czerwone świece z ostatnich 6 M15), blokowane gdy `spike_score >= 2`
- **`impulse_aggressive_short/long`**: wejście market przy wysokim wolumenie (vol ≥ 2.0x),
  tryb force_shadow — tylko testowy

### Wyłączenie inwalidacji setupów zmianą reżimu
Inwalidacja otwartych setupów przez zmianę reżimu (Zasada 1 z 2026-04-08) została
**wyłączona**. Algorytm trendu jest zbyt zawodny by zamykać otwarte pozycje na podstawie
samej zmiany reżimu. Otwarte pozycje żyją do SL/TP/timeout.

*Zmiana motywowana wielokrotnym falsy trigger po pullbackach 08.04.*

### Deduplikacja per-model
Grok i Algo2 nie blokują się nawzajem przy tworzeniu setupów. Deduplikacja działa
oddzielnie w obrębie każdego modelu.

---

## 2026-04-08

### Zasady inwalidacji otwartych setupów (`check_open_setups_invalidation`)
*(Zasady wyłączone 2026-04-09 — zachowane dla historii)*

Mechanizm działający na setupach **po wejściu** w pozycję:
- Zmiana reżimu na przeciwny + setup na plusie → SL przesuwa się do ceny wejścia (BE)
- Zmiana reżimu na przeciwny + setup na minusie → natychmiastowe zamknięcie market orderem
- RANGE nie traktowany jako inwalidacja
- Timeout `OPEN_TRADE_TIMEOUT_H = 16h` od wejścia pozostaje aktywny

---

## 2026-04-07

### Zastępowanie pending setupu nowszą analizą
Gdy Algo wykryje nowy setup z W1 różniącym się o `$0.10–$0.50` od istniejącego pending
setupu (tego samego kierunku i modelu) → stary jest anulowany, wstawiany nowy z
aktualnymi poziomami. Powiadomienie Telegram informuje o zastąpieniu.

Progi:
- `< $0.10` → prawdziwy duplikat → pomiń (nie zapisuj)
- `$0.10–$0.50` → zaktualizowane poziomy → zastąp
- `> $0.50` → osobny setup → zachowaj oba

### Anulowanie setupu gdy cena przebije TP1 bez wejścia
Jeśli cena przekroczy poziom TP1 przed wejściem w pozycję (setup pending) → setup
anulowany jako nieaktualny (`check_stale_setups`).

---

## 2026-04-05

### Naprawa `impulse_strength()` — fałszywe sygnały LONG
`impulse_strength()` sprawdzała tylko stare świece M15 `[-15:-5]`, pomijając świeże
impulsy z ostatniej godziny. Skutek: impulsy w dół były klasyfikowane jako RANGE →
fałszywe sygnały LONG przy wsparciu.

Poprawka: sprawdza teraz **dwie grupy**: `[-15:-5]` (stare) i `[-6:]` (świeże).
Dodano też `change_2h` jako krótkoterminową referencję cenową.

### 3 filtry bezpieczeństwa dla range setupów
Dodane do `range_support_long` i `range_resistance_short`:
1. **Momentum**: nie kupuj/sprzedaj podczas silnego ruchu pod prąd (≥5/6 świec M15
   niedźwiedzich/byczych lub ruch >1.5%)
2. **Touches**: wsparcie/opór musi mieć ≥2 wcześniejsze testy
3. **MA alignment**: nie kupuj gdy `cena < MA30 < MA60`; nie sprzedaj gdy
   `cena > MA30 > MA60` (MA na M15)

---

## 2026-04-03

### Wyłączenie `trend_consolidation_short`
WR za niski — setup wchodził short gdy cena **rosła przez W** (recovery bounce po
trendzie), co skutkowało natychmiastowym SL. Wymaga przeprojektowania z filtrem kierunku
podejścia do W (cena musi spadać do W, nie rosnąć).

**Status: wyłączony (`if False:` w kodzie). Do przeprojektowania.**

### Tymczasowa próba: TREND oparty na MA20 slope
Przeprojektowanie `detect_market_regime()` — MA5/MA20 slope zamiast `change_%`.
Przetestowane, wyniki gorsze od aktualnego podejścia → **wycofane**, powrót do
`change_24h/48h` z wygładzonymi referencjami (average 3 świec zamiast 1 świecy).

### Blokada TREND_UP w bear market
Dodana osłona: blokada `TREND_UP` gdy `change_7d < -5%` — zapobiega detekcji trendu
wzrostowego podczas odbicia w szerszym trendzie spadkowym.

---

## Aktualne otwarte kwestie (2026-04-14)

### Problem: setup spóźniony przy górnej krawędzi konsolidacji
`range_resistance_short` generowany po tym, jak cena już opuściła opór i spada.
Setup czeka na odbicie (`entry_trigger = rising`), ale "mięso" ruchu już trwa.

Podejrzana przyczyna: filtr `momentum_ok_s` blokuje short gdy M15 jest byczy —
a dokładnie wtedy cena podchodzi do oporu. Gdy momentum staje się niedźwiedzie (filtr
przepuszcza), cena jest już poniżej optymalnej strefy wejścia.

**Do analizy/decyzji.**

### Problem: oba SL trafione przy dolnej krawędzi konsolidacji
`range_support_long` i `trend_pullback_short` mogą być jednocześnie aktywne po zmianie
reżimu `RANGE → TREND_DOWN`. Whipsaw przy wsparciu może trafić SL obu setupów kolejno.

Akceptowalne: jeden SL trafiony.
Nieakceptowalne: oba trafione.

**Do analizy/decyzji — jak powinien zachować się algorytm gdy SL jednego jest trafiony?**
