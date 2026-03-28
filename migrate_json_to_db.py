#!/usr/bin/env python3
"""
migrate_json_to_db.py — jednorazowy import stanu JSON do PostgreSQL

Uruchom lokalnie przed przełączeniem na Railway:
    DATABASE_URL=postgresql://... python migrate_json_to_db.py

Skrypt jest idempotentny — bezpieczny do wielokrotnego uruchomienia.
"""

import json
import os
import sys

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("BŁĄD: Brak zmiennej DATABASE_URL")
    sys.exit(1)

PENDING_FILE       = "pending_setups.json"
COOLDOWN_FILE      = "last_alerts.json"
SETUP_COUNTER_FILE = "setup_counter.json"


def run_schema(conn):
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("[migrate] Schema zastosowana.")


def import_pending(conn):
    if not os.path.exists(PENDING_FILE):
        print(f"[migrate] {PENDING_FILE} nie znaleziony — pomijam.")
        return

    with open(PENDING_FILE) as f:
        rows = json.load(f)

    if not rows:
        print("[migrate] pending_setups.json jest pusty.")
        return

    imported = 0
    with conn.cursor() as cur:
        for s in rows:
            cur.execute(
                """
                INSERT INTO setups (
                    setup_id, alert_time, alert_timestamp,
                    model, rejection, type, direction,
                    score, kurs, price_at_alert,
                    warunek, entry_trigger, reasoning,
                    entries, tps, sl, sl_after_tp1, rr,
                    entry_hit_at, avg_entry, entries_hit, sl_adjusted,
                    tp1_hit_at,
                    shadow, cancel_reason, cancel_time, cancel_price,
                    exchange_plan_oid, exchange_qty_full, exchange_qty_half,
                    exchange_position_opened,
                    exchange_tp1_oid, exchange_tp2_oid, exchange_sl_oid,
                    exchange_tp1_done, exchange_done,
                    resolved
                ) VALUES (
                    %(setup_id)s, %(alert_time)s, %(alert_timestamp)s,
                    %(model)s, %(rejection)s, %(type)s, %(direction)s,
                    %(score)s, %(kurs)s, %(price_at_alert)s,
                    %(warunek)s, %(entry_trigger)s, %(reasoning)s,
                    %(entries)s, %(tps)s, %(sl)s, %(sl_after_tp1)s, %(rr)s,
                    %(entry_hit_at)s, %(avg_entry)s, %(entries_hit)s, %(sl_adjusted)s,
                    %(tp1_hit_at)s,
                    %(shadow)s, %(cancel_reason)s, %(cancel_time)s, %(cancel_price)s,
                    %(exchange_plan_oid)s, %(exchange_qty_full)s, %(exchange_qty_half)s,
                    %(exchange_position_opened)s,
                    %(exchange_tp1_oid)s, %(exchange_tp2_oid)s, %(exchange_sl_oid)s,
                    %(exchange_tp1_done)s, %(exchange_done)s,
                    FALSE
                )
                ON CONFLICT (setup_id) DO NOTHING
                """,
                {
                    "setup_id":                s.get("setup_id"),
                    "alert_time":              s.get("alert_time"),
                    "alert_timestamp":         s.get("alert_timestamp", 0),
                    "model":                   s.get("model", ""),
                    "rejection":               s.get("rejection", ""),
                    "type":                    s.get("type", ""),
                    "direction":               s.get("direction", ""),
                    "score":                   s.get("score"),
                    "kurs":                    s.get("kurs"),
                    "price_at_alert":          s.get("price_at_alert"),
                    "warunek":                 s.get("warunek"),
                    "entry_trigger":           s.get("entry_trigger"),
                    "reasoning":               s.get("reasoning"),
                    "entries":                 json.dumps(s.get("entries", [])),
                    "tps":                     json.dumps(s.get("tps", [])),
                    "sl":                      s.get("sl"),
                    "sl_after_tp1":            s.get("sl_after_tp1"),
                    "rr":                      s.get("rr"),
                    "entry_hit_at":            s.get("entry_hit_at"),
                    "avg_entry":               s.get("avg_entry"),
                    "entries_hit":             s.get("entries_hit", 1),
                    "sl_adjusted":             s.get("sl_adjusted", False),
                    "tp1_hit_at":              s.get("tp1_hit_at"),
                    "shadow":                  s.get("shadow", False),
                    "cancel_reason":           s.get("cancel_reason"),
                    "cancel_time":             s.get("cancel_time"),
                    "cancel_price":            s.get("cancel_price"),
                    "exchange_plan_oid":       s.get("exchange_plan_oid"),
                    "exchange_qty_full":       s.get("exchange_qty_full"),
                    "exchange_qty_half":       s.get("exchange_qty_half"),
                    "exchange_position_opened": s.get("exchange_position_opened", False),
                    "exchange_tp1_oid":        s.get("exchange_tp1_oid"),
                    "exchange_tp2_oid":        s.get("exchange_tp2_oid"),
                    "exchange_sl_oid":         s.get("exchange_sl_oid"),
                    "exchange_tp1_done":       s.get("exchange_tp1_done", False),
                    "exchange_done":           s.get("exchange_done", False),
                },
            )
            if cur.rowcount > 0:
                imported += 1

    conn.commit()

    # Przesuń sekwencję SERIAL poza najwyższe istniejące setup_id
    max_id = max((s.get("setup_id") or 0 for s in rows), default=0)
    if max_id > 0:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT setval('setups_setup_id_seq', %s, false)",
                (max_id + 1,),
            )
        conn.commit()
        print(f"[migrate] Sekwencja ustawiona na {max_id + 1}.")

    print(f"[migrate] Setups: {imported} zaimportowanych, {len(rows) - imported} pominiętych (już istniały).")


def import_alerts_log(conn):
    if not os.path.exists(COOLDOWN_FILE):
        print(f"[migrate] {COOLDOWN_FILE} nie znaleziony — pomijam.")
        return

    with open(COOLDOWN_FILE) as f:
        data = json.load(f)

    if not data:
        print("[migrate] last_alerts.json jest pusty.")
        return

    imported = 0
    with conn.cursor() as cur:
        for model, info in data.items():
            cur.execute(
                """
                INSERT INTO alerts_log (model, level, direction, alerted_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    model,
                    info.get("level"),
                    info.get("direction"),
                    info.get("time"),
                ),
            )
            if cur.rowcount > 0:
                imported += 1

    conn.commit()
    print(f"[migrate] Cooldown: {imported} wpisów zaimportowanych.")


def verify(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE resolved=FALSE) FROM setups")
        total, active = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM alerts_log")
        cooldowns = cur.fetchone()[0]
    print(f"\n[migrate] Weryfikacja:")
    print(f"  setups łącznie: {total}, aktywnych: {active}")
    print(f"  alerts_log: {cooldowns}")


if __name__ == "__main__":
    conn = psycopg2.connect(DATABASE_URL)
    try:
        run_schema(conn)
        import_pending(conn)
        import_alerts_log(conn)
        verify(conn)
        print("\n[migrate] Gotowe. Możesz teraz deploować na Railway.")
    finally:
        conn.close()
