# AlertSol — Analiza błędów w kodzie

Raport z przeglądu kodu. Błędy posortowane wg ważności.

---

## KRYTYCZNE

### 1. Wyciek sekretów — hardcoded Telegram token
**Plik:** `sol_alert.py:27-28`

Token bota Telegram i chat ID są wpisane jako domyślne wartości `os.getenv()`:
```python
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",  "8645260464:AAGe_uTew0H1gJnijdcR7oav_A4U8n1HLHI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7442390334")
```
Każdy z dostępem do repozytorium widzi aktywny token bota. Token powinien być odwołany i zastąpiony nowym, ustawionym wyłącznie przez zmienną środowiskową.

**Naprawa:** Usunąć domyślne wartości, używać pustego stringa i wyłączyć Telegram gdy token nie ustawiony.

---

### 2. `NameError` — brak definicji `log` w `sol_alert.py`
**Plik:** `sol_alert.py:589`

```python
log.info("[REGIME] Spike-reversal: rejection wicks na ostatnich 3 M15")
```
W pliku `sol_alert.py` nie ma `import logging` ani `log = logging.getLogger(...)`. Gdy warunek spike-reversal jest spełniony, aplikacja rzuci `NameError` i przerwie detekcję reżimu rynkowego — potencjalnie pomijając sygnały.

**Naprawa:** Dodać `import logging` i `log = logging.getLogger(__name__)` na początku pliku, lub zamienić na `print()`.

---

### 3. Pozycja bez ochrony SL/TP po anulowaniu zleceń (`exchange_trader.py`)
**Plik:** `exchange_trader.py:814-846`

`close_open_position()` najpierw anuluje wszystkie zlecenia TPSL (linie 818-820), a dopiero potem składa zlecenie market close (linia 824). Jeśli market close się nie powiedzie (błąd API, brak marginu), pozycja zostaje otwarta bez żadnej ochrony SL/TP.

**Naprawa:** Odwrócić kolejność — najpierw złożyć market close, potem anulować TPSL.

---

### 4. "Wszystkie TPSL zniknęły" — pozycja na giełdzie bez zarządzania
**Plik:** `exchange_trader.py:1357-1361`

Gdy wszystkie OID zleceń TPSL są `None`, kod ustawia `exchange_done=True`, ale NIE zamyka pozycji na Bitget. Setup jest oznaczony jako zakończony w DB, a realna pozycja z dźwignią 20x pozostaje niezarządzana.

**Naprawa:** Przed oznaczeniem `exchange_done=True` sprawdzić czy pozycja faktycznie istnieje na giełdzie i ją zamknąć.

---

### 5. SL1 wykonany — zakładanie że SL2 automatycznie zamknie resztę
**Plik:** `exchange_trader.py:1231-1253`

Gdy SL1 się wykonuje, kod zakłada że SL2 (ten sam trigger) automatycznie zamknie drugą połowę pozycji. Ale SL1 i SL2 to niezależne zlecenia TPSL — jeśli cena odbije po triggerze SL1, SL2 może się nie wykonać. Kod ustawia `exchange_done=True` i rozlicza pełną pozycję bez weryfikacji SL2.

**Naprawa:** Po wykonaniu SL1 sprawdzić status SL2, lub zamknąć resztę pozycji zleceniem market.

---

### 6. Słaby domyślny sekret sesji + brak HTTPS-only cookies
**Plik:** `main_runner.py:227, 324`

```python
_SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me-set-SESSION_SECRET-env-var")
```
Jeśli `SESSION_SECRET` nie jest ustawiony, każdy znający ten domyślny string może sfałszować cookie sesji. Dodatkowo `https_only=False` na linii 324 oznacza że cookie jest przesyłane przez nieszyfrowane połączenie.

**Naprawa:** Rzucić wyjątek jeśli `SESSION_SECRET` nie jest ustawiony. Ustawić `https_only=True`.

---

### 7. Admin endpointy używają GET zamiast POST
**Plik:** `main_runner.py:1963-2030`

Endpointy zmieniające stan (`/admin/resolve-setup`, `/admin/restore-after-tp1`, `/admin/reset-entry`, `/admin/force-position-open`, `/admin/reopen-setup`, `/admin/fix-position-qty`) używają `@app.get`. Przeglądarka, crawler lub atak CSRF (przez `<img src>`) mogą je nieumyślnie wywołać. Na systemie tradingowym to może spowodować uszkodzenie pozycji.

**Naprawa:** Zmienić na `@app.post`.

---

## ŚREDNIE

### 8. `datetime.utcfromtimestamp()` — deprecated, zwraca naiwny datetime
**Plik:** `sol_alert.py:1441-1442, 1523-1524, 1580-1581`

```python
entry_dt = datetime.utcfromtimestamp(entry_ts).astimezone(TZ)
```
`utcfromtimestamp()` zwraca datetime bez tzinfo. `.astimezone(TZ)` zakłada strefę czasową systemu, nie UTC. Jeśli serwer nie jest w UTC, godziny będą błędne.

**Naprawa:** `datetime.fromtimestamp(entry_ts, tz=timezone.utc).astimezone(TZ)`

---

### 9. Potencjalny `ZeroDivisionError` w detekcji range
**Plik:** `sol_alert.py:1262, 1308`

```python
if (w - tp1) / (sl - w) >= 1.5   # short — jeśli sl == w → ZeroDivisionError
if (tp1 - w) / (w - sl) >= 1.5   # long  — jeśli w == sl → ZeroDivisionError
```
Przy bardzo małym ATR, `sl` i `w` mogą się zrównać.

**Naprawa:** Dodać guard: `if (sl - w) > 0 and (w - tp1) / (sl - w) >= 1.5`

---

### 10. Błędny PnL w hypo dla TP1-only setupów
**Plik:** `sol_alert.py:2092`

```python
elif result == "TP1":
    hypo_pnl = round(sign * half_qty * (eff_exit - eff_entry), 2)
```
Dla setupów TP1-only cała pozycja zamyka się na TP1, ale kalkulacja używa `half_qty` zamiast `full_qty`. Hipotetyczny PnL jest zaniżony o ~50%.

**Naprawa:** Sprawdzić `tp_strategy` setupu i użyć `full_qty` gdy strategia to `tp1_only`.

---

### 11. `close_open_position` nie czyści `exchange_sl2_oid`
**Plik:** `exchange_trader.py:836-841`

`db.update_setup()` czyści `exchange_sl_oid`, `exchange_tp1_oid`, `exchange_tp2_oid`, ale pomija `exchange_sl2_oid`. Stała referencja do nieistniejącego zlecenia zostaje w bazie.

**Naprawa:** Dodać `exchange_sl2_oid=None` do wywołania `update_setup`.

---

### 12. `move_sl_to_entry` używa `full_qty` po TP1
**Plik:** `exchange_trader.py:881`

Funkcja zawsze pobiera `exchange_qty_full`, ale po TP1 pozycja ma tylko połowę rozmiaru. Nowy SL z pełną ilością nie zadziała poprawnie.

**Naprawa:** Sprawdzić status TP1 i użyć `exchange_qty_half` po TP1.

---

### 13. Race condition przy inicjalizacji puli połączeń
**Plik:** `db.py:46-58`

`get_pool()` sprawdza `_pool is None` bez locka. Dwa równoległe wątki mogą utworzyć dwie pule; jedna wycieknie.

**Naprawa:** Dodać `threading.Lock` wokół inicjalizacji.

---

### 14. Cancel setup rozlicza setup mimo nieudanego zamknięcia na giełdzie
**Plik:** `main_runner.py:2928-2929`

`api_cancel_setup` wywołuje `db.resolve_setup()` nawet gdy `failed_on_bitget` nie jest puste (market close się nie powiódł). Setup jest oznaczony jako "anulowany" w DB, a pozycja może wciąż być otwarta na Bitget.

**Naprawa:** Nie rozliczać setupu gdy zamknięcie pozycji się nie powiodło, lub oznaczyć do ręcznej interwencji.

---

### 15. `send_telegram` bez try/except w `breakout_scan`
**Plik:** `sol_alert.py:2806`

Wywołanie `send_telegram(msg)` nie jest opakowane w `try/except` (w przeciwieństwie do innych miejsc). Błąd sieci przerwie job schedulera.

**Naprawa:** Dodać try/except jak w innych call site'ach.

---

### 16. Thread-local baseline snapshot — ciche gubienie zmian
**Plik:** `db.py:506-519`

`save_pending_list()` czyta `_thread_local.baseline` ustawiony przez `load_pending()`. Jeśli funkcje są wywołane z różnych wątków, zmiany są cicho ignorowane.

**Naprawa:** Obecna architektura jest poprawna (exchange_sync i sol_alert mają oddzielne wątki), ale brak walidacji — dodać asercję że baseline istnieje.

---

## NISKIE

### 17. Martwy kod — `_migrate_setup_ids()`
**Plik:** `sol_alert.py:2630-2632, 2837`

Funkcja jest no-op (`pass`), ale wywoływana przy każdym cyklu `main()`.

### 18. Zbędne importy wewnątrz funkcji
**Plik:** `sol_alert.py:389`

`from datetime import datetime, timezone as _tz` reimportowane wewnątrz `fetch_klines` co wywołanie.

### 19. Niepotrzebne wywołania API co 15 sekund
**Plik:** `exchange_trader.py:1000-1001`

`_set_hedge_mode()` i `_set_leverage()` wywoływane w każdym `sync()` (co 15s = 5760 wywołań/dzień). Powinny być cached/jednorazowe.

### 20. Potencjalne wycieknięcie detali DB w błędzie
**Plik:** `main_runner.py:437`

Surowy wyjątek bazy danych wyświetlany użytkownikowi — może zawierać fragmenty zapytań lub connection string.

---

## Podsumowanie

| Ważność | Liczba | Przykłady |
|---------|--------|-----------|
| Krytyczne | 7 | Wyciek tokenów, NameError crash, pozycje bez SL/TP, słaba auth |
| Średnie | 9 | ZeroDivisionError, błędny PnL, race conditions, stale OIDs |
| Niskie | 4 | Martwy kod, zbędne API calls, info leak |

Najważniejsze do natychmiastowej naprawy:
1. Odwołać wyciekły token Telegram i usunąć hardcoded defaults
2. Naprawić `NameError` na `log` w `sol_alert.py:589`
3. Odwrócić kolejność w `close_open_position` (market close przed cancel TPSL)
4. Zmienić admin endpointy z GET na POST
