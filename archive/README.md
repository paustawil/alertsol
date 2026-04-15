# archive/

Folder zawiera kod wyłączony z produkcji — zachowany na wypadek powrotu.

## Pliki skryptowe

| Plik | Opis | Wyłączony od |
|------|------|-------------|
| `migrate_json_to_db.py` | Jednorazowa migracja JSON → PostgreSQL | Po wykonaniu migracji |
| `backtest.py` | Backtest pierwszej wersji algorytmu | Zastąpiony przez Algo2 |
| `grok_backtest.py` | Backtest Groka v1 | Zastąpiony przez Algo2 |
| `grok2_backtest.py` | Backtest Groka v2 | Zastąpiony przez Algo2 |
| `gpt3_backtest.py` | Backtest GPT-3 detektora | Zastąpiony przez Algo2 |
| `gpt3_validator_backtest.py` | Backtest GPT-3 jako walidatora | Walidator aktywny, backtest zbędny |
| `gpt4_backtest.py` | Backtest GPT-4 | Zastąpiony przez Algo2 |
| `gpt5_backtest.py` | Backtest GPT-4 (wersja 5) | Zastąpiony przez Algo2 |
| `gpt_relaxed_backtest.py` | Backtest GPT relaxed prompt | Zastąpiony przez GPT3 Validator |
| `impulse_backtest.py` | Backtest setupów impulsowych | Zastąpiony przez testy live |
| `range_backtest.py` | Porównanie wariantów A/B/C range setupów | Zastąpiony przez testy live |
| `diagnose_regime.py` | Narzędzie diagnostyczne — detekcja reżimu | Jednorazowe |
| `diagnose_positions.py` | Narzędzie diagnostyczne — pozycje Bitget | Jednorazowe |
| `test_exchange.py` | Testy manualne exchange_trader | Zastąpione przez testy live |
| `test_apr3.py` | Testy z 2026-04-03 | Jednorazowe |

## Kod wyłączony z sol_alert.py

| Plik | Opis |
|------|------|
| `disabled_models.py` | Wyłączone modele LLM: Claude (FORTECA_PROMPT), GPT, Grok, GPT3 standalone, GPT-Relaxed, GPT4. Zawiera też `trend_consolidation_short/long` z `algo_detect_setups()`. |

## Jak przywrócić

Aby reaktywować któryś z modeli:
1. Skopiuj funkcję z `disabled_models.py` z powrotem do `sol_alert.py`
2. Ustaw odpowiednią flagę `ENABLE_*=True` w `sol_alert.py`
3. Upewnij się że klucz API jest ustawiony w zmiennych środowiskowych
