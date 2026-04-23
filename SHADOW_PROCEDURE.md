# Procedura wyłączania shadow — Algo2

## Kontekst

Wszystkie setupy Algo2 działają w `shadow=True` (tryb obserwacji, brak realnych zleceń).
Wyłączanie shadow powinno być stopniowe i oparte na danych empirycznych.

## Fazy zbierania danych

### Faza 1 — Teraz (aktualna)
- Wszystkie setupy w shadow
- Sygnały wyczerpania (exhaustion) są **logowane**, ale **nic nie blokują**
- Sygnały widoczne w polu `reasoning` w DB i Google Sheets: `| EXH:sygnał1,sygnał2`
- Cel: zebrać dane o tym, które sygnały poprzedzają złe wejścia

### Faza 2 — Przed wyłączeniem shadow na TREND lub RANGE
**Warunek wejścia:** setup_type ma dobrą historię w shadow (analiza Wyniki_Railway)

**Zasada:** Jeśli w momencie generowania setupu present jest **co najmniej 1** sygnał wyczerpania
→ setup zostaje w `force_shadow=True` mimo że typ jest już "produkcyjny"

Sygnały wyczerpania (zdefiniowane w `sol_alert.py`, funkcja `algo_detect_setups`):
- `malejace_HH` — malejąca amplituda wyższych maksimów (UP trend)
- `malejace_LL` — malejąca amplituda niższych minimów (DOWN trend)
- `malejacy_wolumen_M15` — wolumen spada 3 świece z rzędu
- `malejace_body_M15` — ciała świec maleją 3 świece z rzędu
- `konwergencja_MA20` — cena bliżej niż 0.5×ATR od MA20 H1

**Implementacja Fazy 2:**
W `_PULLBACK_VARIANTS` lub w logice `algo_detect_setups`, przed przypisaniem `force_shadow`,
dodać:
```python
if exhaustion_signals:
    v_shadow = True  # jeden sygnał wystarczy do utrzymania shadow
```

### Faza 3 — Kalibracja progu
Po zebraniu danych z Fazy 2: analiza które sygnały były trafne (korelacja z SL hit).
Możliwe zaostrzenie do ≥2 sygnałów lub rozluźnienie do konkretnych sygnałów.

## Kolejność wyłączania typów setupów

Rekomendowana kolejność (od najbardziej dojrzałych danych):

1. `trend_pullback_long/short` variant `baseline` — najbardziej sprawdzony
2. `trend_pullback_long/short` variant `str4` — ostrożnie, niższy próg strength
3. `trend_pullback_long/short` variant `shallow` — płytki pullback, wymaga więcej danych
4. `range_support_long` / `range_resistance_short` — po przebudowie detekcji RANGE
5. `impulse_continuation_long/short` — dopiero po osobnej analizie
6. `impulse_aggressive_*` — zostawić w shadow najdłużej

## Co sprawdzić przed wyłączeniem danego typu

- [ ] Min. 20 setupów tego typu zakończonych (tp/sl) w shadow
- [ ] Win rate shadow ≥ 50% lub RR kompensuje niższy win rate
- [ ] Brak anomalii w danych (np. wszystkie SL hity przy jednym reżimie)
- [ ] Exhaustion signals: sprawdzić czy setupy z EXH: miały gorsze wyniki
- [ ] Brak aktywnych znanych bugów dla tego typu (ALGO2_CHANGELOG.md)

## Flagi kontrolne w kodzie

```python
# sol_alert.py, linia ~48
ALGO2_SHADOW_MODE = True   # True = wszystkie Algo2 w shadow (nadpisuje force_shadow)
```

Aby wyłączyć shadow dla konkretnego typu (nie globalnie):
1. Ustaw `ALGO2_SHADOW_MODE = True` (zostaje)
2. W `_PULLBACK_VARIANTS` zmień `v_shadow = False` dla danego wariantu
3. Logika w `_algo2_run()` respektuje `force_shadow` per-setup, nie globalnie
