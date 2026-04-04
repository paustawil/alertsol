"""
db.py — moduł bazy danych dla AlertSol
Zastępuje: pending_setups.json, last_alerts.json, setup_counter.json

Wymaga zmiennej środowiskowej DATABASE_URL (Railway dostarcza automatycznie).
"""

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

log = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# Snapshot dla change-detection w save_pending_list()
# Thread-local: każdy wątek (exchange_sync / sol_alert) ma własny baseline,
# żeby równoczesne wywołania sync() nie nadpisywały sobie nawzajem snapshotu.
_thread_local = threading.local()

# Pola exchange_* monitorowane przez exchange_trader.py
_EXCHANGE_FIELDS = [
    "exchange_plan_oid",
    "exchange_qty_full",
    "exchange_qty_half",
    "exchange_position_opened",
    "exchange_tp1_oid",
    "exchange_tp2_oid",
    "exchange_sl_oid",
    "exchange_tp1_done",
    "exchange_done",
]


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL nie jest ustawiona")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=url,
        )
        log.info("DB pool zainicjalizowany.")
    return _pool


@contextmanager
def _conn():
    """Kontekst menedżer: pobiera połączenie z poola, commit lub rollback."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _row_to_dict(row: psycopg2.extras.RealDictRow) -> dict:
    """Konwertuje RealDictRow na zwykły dict; JSONB jest już sparsowany przez psycopg2.
    Decimal (kolumny NUMERIC) konwertowane na float — unikamy TypeError w arytmetyce."""
    d = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif isinstance(v, list):
            d[k] = [float(x) if isinstance(x, Decimal) else x for x in v]
        else:
            d[k] = v
    if d.get("entries") is None:
        d["entries"] = []
    if d.get("tps") is None:
        d["tps"] = []
    return d


def init_schema():
    """Tworzy tabele jeśli nie istnieją. Bezpieczne do wywołania przy starcie."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    log.info("Schema zainicjalizowana.")


# ── Setups ────────────────────────────────────────────────────────────────────

def get_active_setups() -> list[dict]:
    """Zwraca wszystkie nierozwiązane setupy jako listę słowników."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM setups WHERE resolved = FALSE ORDER BY alert_timestamp ASC"
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def insert_setup(row: dict) -> int | None:
    """
    Wstawia nowy setup do bazy. Zwraca nowo nadany setup_id (SERIAL).
    row: słownik z polami odpowiadającymi kolumnom tabeli setups
         (bez setup_id — generowany przez DB).
    """
    params = {
        "alert_time":      row.get("alert_time"),
        "alert_timestamp": row.get("alert_timestamp"),
        "model":           row.get("model", ""),
        "rejection":       row.get("rejection", ""),
        "type":            row.get("type", ""),
        "direction":       row.get("direction", ""),
        "score":           row.get("score"),
        "kurs":            row.get("kurs"),
        "price_at_alert":  row.get("price_at_alert"),
        "warunek":         row.get("warunek"),
        "entry_trigger":   row.get("entry_trigger"),
        "reasoning":       row.get("reasoning"),
        "llm_scores":      json.dumps(row["llm_scores"]) if row.get("llm_scores") else None,
        "entries":         json.dumps(row.get("entries", [])),
        "tps":             json.dumps(row.get("tps", [])),
        "sl":              row.get("sl"),
        "sl_after_tp1":    row.get("sl_after_tp1"),
        "rr":              row.get("rr"),
        "entry_hit_at":    row.get("entry_hit_at"),
        "entries_hit":     row.get("entries_hit", 1),
        "sl_adjusted":     row.get("sl_adjusted", False),
    }

    with _conn() as conn:
        with conn.cursor() as cur:
            # Advisory lock serializuje równoczesne INSERTy dla tego samego kierunku.
            # Bez tego READ COMMITTED pozwala dwóm transakcjom (Railway + GitHub Actions)
            # przejść WHERE NOT EXISTS jednocześnie i wstawić duplikat.
            lock_key = f"insert_setup_{params.get('direction', '')}"
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))

            cur.execute(
                """
                INSERT INTO setups (
                    alert_time, alert_timestamp, model, rejection, type,
                    direction, score, kurs, price_at_alert, warunek,
                    entry_trigger, reasoning, llm_scores,
                    entries, tps, sl, sl_after_tp1, rr,
                    entry_hit_at, entries_hit, sl_adjusted
                )
                SELECT
                    %(alert_time)s, %(alert_timestamp)s, %(model)s, %(rejection)s, %(type)s,
                    %(direction)s, %(score)s, %(kurs)s, %(price_at_alert)s, %(warunek)s,
                    %(entry_trigger)s, %(reasoning)s, %(llm_scores)s,
                    %(entries)s, %(tps)s, %(sl)s, %(sl_after_tp1)s, %(rr)s,
                    %(entry_hit_at)s, %(entries_hit)s, %(sl_adjusted)s
                WHERE NOT EXISTS (
                    SELECT 1 FROM setups
                    WHERE resolved = FALSE
                      AND direction = %(direction)s
                      AND ABS((entries->0)::numeric - (%(entries)s::jsonb->0)::numeric) < 0.5
                )
                RETURNING setup_id
                """,
                params,
            )
            result = cur.fetchone()
            if result is None:
                log.info(f"[db] Duplikat na poziomie DB — pominięto ({row.get('model')} {row.get('direction')})")
                return None
            setup_id = result[0]
    log.info(f"[db] Nowy setup #{setup_id} ({row.get('model')} {row.get('direction')})")
    return setup_id


def update_setup(setup_id: int, **fields: Any) -> None:
    """
    Aktualizuje podane pola setupu. Puste wywołanie jest bezpieczne (no-op).
    Obsługuje JSONB pola (entries, tps, llm_scores) automatycznie.
    """
    if not fields:
        return

    _JSONB = {"entries", "tps", "llm_scores"}
    set_parts = []
    values: dict[str, Any] = {}
    for key, val in fields.items():
        param = f"p_{key}"
        if key in _JSONB and val is not None:
            values[param] = json.dumps(val)
        else:
            values[param] = val
        set_parts.append(f"{key} = %({param})s")

    values["setup_id"] = setup_id
    sql = f"UPDATE setups SET {', '.join(set_parts)} WHERE setup_id = %(setup_id)s"

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)


def resolve_setup(
    setup_id: int,
    result: str,
    avg_entry: float | None,
    avg_exit: float | None,
    pnl_usd: float | None,
    exit_ts: int | None = None,
) -> None:
    """
    Zamknij setup: zapisz wynik, PnL, czas wyjścia.
    pnl_pct obliczany automatycznie jeśli avg_entry jest dostępne.
    """
    pnl_pct = None
    if pnl_usd is not None:
        trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
        try:
            pnl_pct = round(pnl_usd / trade_usdt * 100, 2)
        except ZeroDivisionError:
            pass

    exit_time = None
    if exit_ts:
        exit_time = datetime.fromtimestamp(exit_ts, tz=timezone.utc)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE setups SET
                    result      = %(result)s,
                    avg_entry   = %(avg_entry)s,
                    avg_exit    = %(avg_exit)s,
                    pnl_usd     = %(pnl_usd)s,
                    pnl_pct     = %(pnl_pct)s,
                    exit_time   = %(exit_time)s,
                    resolved    = TRUE,
                    resolved_at = NOW()
                WHERE setup_id = %(setup_id)s
                """,
                {
                    "setup_id":  setup_id,
                    "result":    result,
                    "avg_entry": avg_entry,
                    "avg_exit":  avg_exit,
                    "pnl_usd":   pnl_usd,
                    "pnl_pct":   pnl_pct,
                    "exit_time": exit_time,
                },
            )
    log.info(f"[db] Setup #{setup_id} zamknięty: {result}, PnL={pnl_usd}")


# ── Cooldown (was_alerted / save_alerted) ────────────────────────────────────

def was_alerted(model: str, level: float, direction: str) -> bool:
    """Sprawdza czy ten sam setup (model+poziom+kierunek) był alertowany w ostatnich COOLDOWN_HOURS."""
    cooldown_hours = int(os.getenv("COOLDOWN_HOURS", "4"))
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM alerts_log
                WHERE model     = %(model)s
                  AND direction = %(direction)s
                  AND ABS(level - %(level)s) < 0.5
                  AND alerted_at > NOW() - %(interval)s::interval
                LIMIT 1
                """,
                {
                    "model":     model,
                    "direction": direction,
                    "level":     level,
                    "interval":  f"{cooldown_hours} hours",
                },
            )
            return cur.fetchone() is not None


def save_alerted(model: str, level: float, direction: str) -> None:
    """Zapisuje nowy wpis cooldownu."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts_log (model, level, direction) VALUES (%s, %s, %s)",
                (model, level, direction),
            )


# ── Exchange trader integration ───────────────────────────────────────────────

def claim_plan_order(setup_id: int) -> bool:
    """
    Atomicznie rezerwuje prawo do złożenia plan order dla danego setupu.
    Ustawia exchange_plan_oid = 'PENDING' w jednej operacji UPDATE WHERE IS NULL.
    Zwraca True jeśli rezerwacja się udała (ten proces może złożyć order),
    False jeśli inny proces już zarezerwował lub złożył order.

    Chroni przed race condition między Railway (co 15s) a GitHub Actions (co 5min)
    gdy oba wywołują sync() jednocześnie i widzą plan_oid=NULL dla nowego setupu.
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE setups
                SET exchange_plan_oid = 'PENDING'
                WHERE setup_id = %s
                  AND exchange_plan_oid IS NULL
                  AND entry_hit_at IS NULL
                  AND resolved = FALSE
                RETURNING setup_id
                """,
                (setup_id,),
            )
            return cur.fetchone() is not None


def release_plan_order_claim(setup_id: int) -> None:
    """Zwalnia rezerwację 'PENDING' gdy API call do Bitget nie udał się."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE setups SET exchange_plan_oid = NULL "
                "WHERE setup_id = %s AND exchange_plan_oid = 'PENDING'",
                (setup_id,),
            )


def load_pending() -> list[dict]:
    """
    Zwraca aktywne setupy i zapamiętuje snapshot do change-detection.
    Odpowiednik _load_pending() z exchange_trader.py.
    Baseline jest thread-local — bezpieczny przy równoczesnych sync().
    """
    rows = get_active_setups()
    _thread_local.baseline = {r["setup_id"]: {f: r.get(f) for f in _EXCHANGE_FIELDS} for r in rows}
    return rows


def save_pending_list(pending: list[dict]) -> None:
    """
    Zapisuje tylko te pola exchange_*, które zmieniły się względem snapshotu.
    Odpowiednik _save_pending() z exchange_trader.py.
    """
    baseline_map = getattr(_thread_local, "baseline", {})
    for s in pending:
        sid = s.get("setup_id")
        if sid is None:
            continue
        baseline = baseline_map.get(sid, {})
        changed = {
            f: s[f]
            for f in _EXCHANGE_FIELDS
            if f in s and s.get(f) != baseline.get(f)
        }
        if changed:
            update_setup(sid, **changed)
            log.debug(f"[db] Setup #{sid}: zaktualizowano {list(changed.keys())}")


# ── Google Sheets eksport ─────────────────────────────────────────────────────

def get_resolved_with_open_orders() -> list[dict]:
    """Zwraca rozwiązane setupy które mają jeszcze aktywny plan order na Bitget (do anulowania)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT setup_id, exchange_plan_oid, exchange_position_opened
                FROM setups
                WHERE resolved = TRUE
                  AND exchange_done = FALSE
                  AND exchange_plan_oid IS NOT NULL
                  AND exchange_position_opened = FALSE
                """
            )
            return [dict(r) for r in cur.fetchall()]


def mark_exchange_done(setup_id: int) -> None:
    """Oznacza setup jako zakończony po stronie exchange (order anulowany)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE setups SET exchange_done = TRUE, exchange_plan_oid = NULL WHERE setup_id = %s",
                (setup_id,),
            )


def get_unexported_resolved() -> list[dict]:
    """Zwraca zamknięte setupy, które jeszcze nie zostały wyeksportowane do Sheets."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM setups
                WHERE resolved = TRUE AND sheets_exported = FALSE
                ORDER BY resolved_at ASC
                """
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def mark_sheets_exported(setup_id: int) -> None:
    """Oznacza setup jako wyeksportowany do Google Sheets."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE setups SET sheets_exported = TRUE WHERE setup_id = %s",
                (setup_id,),
            )


# ── Dashboard / statystyki ───────────────────────────────────────────────────

def get_summary_stats() -> dict:
    """Zwraca statystyki podsumowujące dla dashboardu."""
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage   = 20

    # Fragment SQL obliczający PnL z fallbackiem gdy pnl_usd IS NULL
    pnl_calc = f"""
        COALESCE(pnl_usd,
            CASE WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','SL')
                      AND avg_exit IS NOT NULL
                      AND COALESCE(avg_entry, (entries->>0)::numeric) IS NOT NULL
            THEN
                CASE direction WHEN 'long'
                    THEN (avg_exit - COALESCE(avg_entry, (entries->>0)::numeric))
                    ELSE (COALESCE(avg_entry, (entries->>0)::numeric) - avg_exit)
                END *
                CASE result WHEN 'SL'
                    THEN COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                         FLOOR({trade_usdt}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                    WHEN 'TP1'
                    THEN COALESCE(NULLIF(exchange_qty_half,'')::numeric,
                         FLOOR(COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                               FLOOR({trade_usdt}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                               /2/0.1)*0.1)
                    ELSE COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                         FLOOR({trade_usdt}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                END
            END
        )"""

    trading_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','SL')"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE resolved = FALSE)             AS active_count,
                    COUNT(*) FILTER (WHERE resolved = TRUE)              AS total_resolved,
                    ROUND(SUM({pnl_calc}) FILTER (WHERE resolved = TRUE
                        AND {trading_filter})::numeric, 2)               AS total_pnl_usd,
                    COUNT(*) FILTER (WHERE resolved = TRUE
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL'))   AS wins,
                    COUNT(*) FILTER (WHERE resolved = TRUE
                        AND result = 'SL')                               AS losses
                FROM setups
                """
            )
            row = dict(cur.fetchone())

            # Win rate
            wins   = row.get("wins") or 0
            losses = row.get("losses") or 0
            total  = wins + losses
            row["win_rate_pct"] = round(wins / total * 100, 1) if total > 0 else None

            # Per-model breakdown
            cur.execute(
                f"""
                SELECT model,
                       COUNT(*)                                              AS all_setups,
                       COUNT(*) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})                             AS entered,
                       ROUND(SUM({pnl_calc}) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})::numeric, 2)                AS pnl_usd,
                       COUNT(*) FILTER (WHERE resolved = TRUE
                           AND result IN ('TP1','TP2','TP1+BE','TP1+SL'))    AS wins
                FROM setups
                GROUP BY model
                ORDER BY model
                """
            )
            row["by_model"] = [dict(r) for r in cur.fetchall()]

    return row


def get_period_stats(period: str) -> dict:
    """Zwraca statystyki za podany okres: 1d, 24h, 7d, 30d.
    - max_capital: maksymalna jednoczesna liczba otwartych pozycji * trade_usdt
    - avg_daily_pnl: średni dzienny PnL
    - total_income: łączny dochód
    - entry_rate: uruchomione / złożone zlecenia
    - win_rate: TP1 / (TP1 + SL)
    """
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))

    # Determine time window
    if period == "24h":
        interval = "24 hours"
    elif period == "7d":
        interval = "7 days"
    elif period == "30d":
        interval = "30 days"
    else:  # 1d — calendar day
        interval = None  # special handling

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if interval:
                time_filter = "alert_time >= NOW() - %(interval)s::interval"
                time_params: dict = {"interval": interval}
            else:
                time_filter = "alert_time::date = CURRENT_DATE"
                time_params = {}

            # Total setups in period
            cur.execute(
                f"SELECT COUNT(*) AS total FROM setups WHERE {time_filter}",
                time_params,
            )
            total_setups = cur.fetchone()["total"] or 0

            # Entered = resolved with trading result (TP1/TP2/TP1+BE/TP1+SL/SL)
            # PnL with fallback computation when pnl_usd is NULL
            leverage = 20
            pnl_calc = f"""
                COALESCE(pnl_usd,
                    CASE WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','SL')
                              AND avg_exit IS NOT NULL
                              AND COALESCE(avg_entry, (entries->>0)::numeric) IS NOT NULL
                    THEN
                        CASE direction WHEN 'long'
                            THEN (avg_exit - COALESCE(avg_entry, (entries->>0)::numeric))
                            ELSE (COALESCE(avg_entry, (entries->>0)::numeric) - avg_exit)
                        END *
                        CASE result WHEN 'SL'
                            THEN COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                 FLOOR({trade_usdt}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                            WHEN 'TP1'
                            THEN COALESCE(NULLIF(exchange_qty_half,'')::numeric,
                                 FLOOR(COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                       FLOOR({trade_usdt}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                                       /2/0.1)*0.1)
                            ELSE COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                 FLOOR({trade_usdt}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                        END
                    END
                )"""
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','SL')) AS entered,
                    COUNT(*) FILTER (WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL'))   AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                               AS losses,
                    COALESCE(ROUND(SUM({pnl_calc}) FILTER (WHERE resolved = TRUE
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL','SL'))::numeric, 2), 0) AS total_income
                FROM setups
                WHERE {time_filter}
                """,
                time_params,
            )
            row = dict(cur.fetchone())
            entered = row["entered"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            total_income = float(row["total_income"])

            # Entry rate
            entry_rate = round(entered / total_setups * 100, 1) if total_setups > 0 else 0

            # Win rate: all wins (TP1+TP2+TP1+BE) / all entered (wins + SL)
            win_rate = round(wins / entered * 100, 1) if entered > 0 else 0

            # Max simultaneous open positions — count overlapping setups
            # Use entry_hit_at (Unix ts) and exit_time to determine overlap
            cur.execute(
                f"""
                SELECT COUNT(*) AS max_open
                FROM (
                    SELECT alert_time,
                           generate_series(
                               date_trunc('hour', COALESCE(
                                   to_timestamp(entry_hit_at) AT TIME ZONE 'UTC',
                                   alert_time
                               )),
                               date_trunc('hour', COALESCE(exit_time, NOW())),
                               interval '1 hour'
                           ) AS h
                    FROM setups
                    WHERE {time_filter}
                      AND (entry_hit_at IS NOT NULL OR exchange_position_opened = TRUE)
                ) sub
                GROUP BY h
                ORDER BY max_open DESC
                LIMIT 1
                """,
                time_params,
            )
            max_row = cur.fetchone()
            max_open = max_row["max_open"] if max_row else 0
            max_capital = round(max_open * trade_usdt, 2)
            max_capital_mult = round(max_open * 1.0, 1)  # multiplier of trade_usdt

            # Average daily PnL
            cur.execute(
                f"""
                SELECT
                    COALESCE(ROUND(SUM(pnl_usd)::numeric, 2), 0) AS sum_pnl,
                    COUNT(DISTINCT resolved_at::date) AS days
                FROM setups
                WHERE {time_filter} AND resolved = TRUE AND pnl_usd IS NOT NULL
                """,
                time_params,
            )
            avg_row = dict(cur.fetchone())
            sum_pnl = float(avg_row["sum_pnl"])
            days = avg_row["days"] or 1
            avg_daily_pnl = round(sum_pnl / days, 2)
            avg_daily_mult = round(avg_daily_pnl / trade_usdt, 3) if trade_usdt else 0

    return {
        "period": period,
        "trade_usdt": trade_usdt,
        "total_setups": total_setups,
        "entered": entered,
        "entry_rate": entry_rate,
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "total_income": total_income,
        "avg_daily_pnl": avg_daily_pnl,
        "avg_daily_mult": avg_daily_mult,
        "max_capital": max_capital,
        "max_capital_mult": max_capital_mult,
        "max_open_positions": max_open,
    }


def get_recent_resolved(limit: int = 20) -> list[dict]:
    """Zwraca ostatnie zamknięte setupy dla dashboardu."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT setup_id, alert_time, model, direction, score,
                       result, avg_entry, avg_exit, pnl_usd, pnl_pct,
                       exit_time, entries, tps, sl, sl_after_tp1,
                       exchange_qty_full, exchange_qty_half
                FROM setups
                WHERE resolved = TRUE
                ORDER BY resolved_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def get_resolved_filtered(
    results: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Zwraca zamknięte setupy z filtrami + total count."""
    where = ["resolved = TRUE"]
    params: dict = {}

    if results:
        where.append("result = ANY(%(results)s)")
        params["results"] = results

    if date_from:
        where.append("resolved_at >= %(date_from)s::date")
        params["date_from"] = date_from

    if date_to:
        where.append("resolved_at < (%(date_to)s::date + interval '1 day')")
        params["date_to"] = date_to

    where_sql = " AND ".join(where)

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM setups WHERE {where_sql}", params)
            total = cur.fetchone()["cnt"]

            cur.execute(
                f"""
                SELECT setup_id, alert_time, model, direction, score,
                       result, avg_entry, avg_exit, pnl_usd, pnl_pct,
                       exit_time, entries, tps, sl, sl_after_tp1,
                       exchange_qty_full, exchange_qty_half,
                       hypo_result, hypo_pnl_usd
                FROM setups
                WHERE {where_sql}
                ORDER BY resolved_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {**params, "limit": limit, "offset": offset},
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]

    return {"total": total, "rows": rows}


def get_all_resolved_for_calc() -> list[dict]:
    """Zwraca wszystkie zamknięte setupy z polami potrzebnymi do kalkulatora zysku."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT setup_id, alert_time, model, type, direction, score,
                       entries, tps, sl, sl_after_tp1, rr,
                       result, avg_entry, avg_exit, pnl_usd, pnl_pct,
                       entries_hit, exit_time,
                       hypo_result, hypo_pnl_usd
                FROM setups
                WHERE resolved = TRUE
                ORDER BY resolved_at ASC
                """
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def save_hypo_result(setup_id: int, hypo_result: str, hypo_pnl_usd: float | None) -> None:
    """Zapisuje hipotetyczny wynik dla setupu który nie weszął."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE setups
                SET hypo_result  = %(hypo_result)s,
                    hypo_pnl_usd = %(hypo_pnl_usd)s
                WHERE setup_id = %(setup_id)s
                """,
                {"setup_id": setup_id, "hypo_result": hypo_result,
                 "hypo_pnl_usd": hypo_pnl_usd},
            )
