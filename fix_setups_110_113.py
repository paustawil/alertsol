#!/usr/bin/env python3
"""
Jednorazowa korekta danych dla setupów 110 i 113 (grok shadow).

Problem: trade_usdt zapisało się jako 100 zamiast 10, przez co pnl_usd
i hypo_pnl_usd są 10x za duże. pnl_pct są poprawne — nie ruszamy ich.

Uruchomienie na Railway:
  DATABASE_URL=... python fix_setups_110_113.py
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ["DATABASE_URL"]

SETUP_IDS = (110, 113)
OLD_TRADE_USDT = 100.0
NEW_TRADE_USDT = 10.0
RATIO = OLD_TRADE_USDT / NEW_TRADE_USDT  # = 10

def main():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # --- Podgląd przed zmianą ---
            cur.execute(
                """
                SELECT setup_id, direction, result, trade_usdt,
                       pnl_usd, pnl_pct, hypo_result, hypo_pnl_usd,
                       exchange_qty_full, exchange_qty_half
                FROM setups WHERE setup_id = ANY(%s)
                ORDER BY setup_id
                """,
                (list(SETUP_IDS),),
            )
            rows = cur.fetchall()
            print("=== PRZED ZMIANĄ ===")
            for r in rows:
                print(dict(r))

            # Weryfikacja — sprawdź że trade_usdt = 100 (inaczej przerwij)
            for r in rows:
                if r["trade_usdt"] is None:
                    print(f"\nSETUP {r['setup_id']}: trade_usdt = NULL — pomijam")
                    continue
                if float(r["trade_usdt"]) != OLD_TRADE_USDT:
                    raise ValueError(
                        f"Setup {r['setup_id']} ma trade_usdt={r['trade_usdt']}, "
                        f"oczekiwano {OLD_TRADE_USDT}. Przerwano."
                    )

            # --- UPDATE ---
            cur.execute(
                """
                UPDATE setups SET
                    trade_usdt   = %(new_tu)s,
                    pnl_usd      = CASE WHEN pnl_usd IS NOT NULL
                                        THEN ROUND(pnl_usd / %(ratio)s, 4)
                                        ELSE NULL END,
                    hypo_pnl_usd = CASE WHEN hypo_pnl_usd IS NOT NULL
                                        THEN ROUND(hypo_pnl_usd / %(ratio)s, 4)
                                        ELSE NULL END
                WHERE setup_id = ANY(%(ids)s)
                  AND trade_usdt = %(old_tu)s
                RETURNING setup_id, trade_usdt, pnl_usd, pnl_pct, hypo_pnl_usd
                """,
                {
                    "new_tu": NEW_TRADE_USDT,
                    "old_tu": OLD_TRADE_USDT,
                    "ratio":  RATIO,
                    "ids":    list(SETUP_IDS),
                },
            )
            updated = cur.fetchall()

            print("\n=== PO ZMIANIE ===")
            for r in updated:
                print(dict(r))

            print(f"\nZaktualizowano {len(updated)} setup(ów).")

        conn.commit()
        print("COMMIT OK")
    except Exception as e:
        conn.rollback()
        print(f"ROLLBACK — błąd: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
