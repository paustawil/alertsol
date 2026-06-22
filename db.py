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
_pool_lock = threading.Lock()

# Snapshot dla change-detection w save_pending_list()
# Thread-local: każdy wątek (exchange_sync / sol_alert) ma własny baseline,
# żeby równoczesne wywołania sync() nie nadpisywały sobie nawzajem snapshotu.
_thread_local = threading.local()

# Pola exchange_* monitorowane przez exchange_trader.py
_EXCHANGE_FIELDS = [
    "exchange_plan_oid",
    "exchange_plan2_oid",
    "exchange_qty_full",
    "exchange_qty_half",
    "exchange_position_opened",
    "exchange_tp1_oid",
    "exchange_tp2_oid",
    "exchange_sl_oid",
    "exchange_sl2_oid",
    "exchange_tp1_done",
    "exchange_done",
    "exchange_fee_open",
    "exchange_fee_close",
]


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
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
    _migrate_tp1_pnl()
    _migrate_variant_from_type()


def _migrate_tp1_pnl() -> None:
    """
    Jednorazowa migracja: przelicz pnl_usd / pnl_pct dla zamkniętych TP1
    używając pełnej ilości (exchange_qty_full), nie połowy.
    Bezpieczne do wielokrotnego wywołania — nadpisuje tylko gdy avg_entry/avg_exit są dostępne.
    """
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage   = 20
    _tu = f"COALESCE(trade_usdt, {trade_usdt})"
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE setups SET
                    pnl_usd = ROUND(
                        CASE direction
                            WHEN 'long'  THEN (avg_exit - avg_entry)
                            WHEN 'short' THEN (avg_entry - avg_exit)
                        END
                        * COALESCE(
                            NULLIF(exchange_qty_full, '')::numeric,
                            FLOOR({_tu}*{leverage} / avg_entry / 0.1) * 0.1
                        ),
                        4),
                    pnl_pct = ROUND(
                        CASE direction
                            WHEN 'long'  THEN (avg_exit - avg_entry)
                            WHEN 'short' THEN (avg_entry - avg_exit)
                        END
                        * COALESCE(
                            NULLIF(exchange_qty_full, '')::numeric,
                            FLOOR({_tu}*{leverage} / avg_entry / 0.1) * 0.1
                        )
                        / NULLIF({_tu}, 0) * 100,
                        2)
                WHERE result = 'TP1'
                  AND status = 'closed'
                  AND avg_entry IS NOT NULL
                  AND avg_exit  IS NOT NULL
                """,
            )
            updated = cur.rowcount
    if updated:
        log.info(f"[migrate] Naprawiono pnl_usd/pnl_pct dla {updated} rekordów TP1.")


def _migrate_variant_from_type() -> None:
    """Jednorazowa naprawa: przywraca oryginalne warianty dla setupów,
    którym variant został błędnie ustawiony na type."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE setups SET variant = 'baseline'
                WHERE variant = type
                  AND type IN (
                    'range_support_long', 'range_resistance_short',
                    'impulse_continuation_long', 'impulse_continuation_short'
                  )
                """,
            )
            b = cur.rowcount
            cur.execute(
                """
                UPDATE setups SET variant = 'h1_atr'
                WHERE variant = type
                  AND type IN ('impulse_aggressive_long', 'impulse_aggressive_short')
                """,
            )
            h = cur.rowcount
    if b + h:
        log.info(f"[migrate] Przywrócono variant: baseline={b}, h1_atr={h}.")


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
        "shadow":          row.get("shadow", False),
        "trade_usdt":      row.get("trade_usdt") or float(os.getenv("BITGET_TRADE_USDT", "100")),
        "variant":         row.get("variant", "baseline"),
        "status":          row.get("status", "pending"),
    }

    with _conn() as conn:
        with conn.cursor() as cur:
            # Advisory lock serializuje równoczesne INSERTy dla tego samego kierunku.
            # Bez tego READ COMMITTED pozwala dwóm transakcjom (Railway + GitHub Actions)
            # przejść WHERE NOT EXISTS jednocześnie i wstawić duplikat.
            lock_key = f"insert_setup_{params.get('model', '')}_{params.get('direction', '')}"
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))

            cur.execute(
                """
                INSERT INTO setups (
                    alert_time, alert_timestamp, model, rejection, type,
                    direction, score, kurs, price_at_alert, warunek,
                    entry_trigger, reasoning, llm_scores,
                    entries, tps, sl, sl_after_tp1, rr,
                    entry_hit_at, entries_hit, sl_adjusted, shadow,
                    trade_usdt, variant, status
                )
                SELECT
                    %(alert_time)s, %(alert_timestamp)s, %(model)s, %(rejection)s, %(type)s,
                    %(direction)s, %(score)s, %(kurs)s, %(price_at_alert)s, %(warunek)s,
                    %(entry_trigger)s, %(reasoning)s, %(llm_scores)s,
                    %(entries)s, %(tps)s, %(sl)s, %(sl_after_tp1)s, %(rr)s,
                    %(entry_hit_at)s, %(entries_hit)s, %(sl_adjusted)s, %(shadow)s,
                    %(trade_usdt)s, %(variant)s, %(status)s
                WHERE NOT EXISTS (
                    SELECT 1 FROM setups
                    WHERE resolved = FALSE
                      AND direction = %(direction)s
                      AND model = %(model)s
                      AND variant = %(variant)s
                      AND ABS((entries->0)::numeric - (%(entries)s::jsonb->0)::numeric) < 0.5
                ) OR %(shadow)s = TRUE
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
    exit_time = None
    if exit_ts:
        exit_time = datetime.fromtimestamp(exit_ts, tz=timezone.utc)

    # Pobierz trade_usdt zapisany przy tworzeniu setupu (fallback: env var)
    _default_tu = float(os.getenv("BITGET_TRADE_USDT", "100"))
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT trade_usdt FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
            trade_usdt = float(row[0]) if row and row[0] else _default_tu

            pnl_pct = None
            if pnl_usd is not None:
                try:
                    pnl_pct = round(pnl_usd / trade_usdt * 100, 2)
                except ZeroDivisionError:
                    pass

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
                    resolved_at = NOW(),
                    status      = 'closed'
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


def mark_tp1_hit(
    setup_id: int,
    avg_entry: float | None,
    tp1_price: float | None,
    pnl_usd: float | None,
) -> None:
    """
    TP1 wykonany na Bitget — zapisz PnL częściowy, zmień status na after_tp1.
    Setup pozostaje resolved=FALSE i widoczny w aktywnych aż do TP2 lub BE.
    """
    _default_tu = float(os.getenv("BITGET_TRADE_USDT", "100"))
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT trade_usdt FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
            trade_usdt = float(row[0]) if row and row[0] else _default_tu

            pnl_pct = None
            if pnl_usd is not None:
                try:
                    pnl_pct = round(pnl_usd / trade_usdt * 100, 2)
                except ZeroDivisionError:
                    pass

            cur.execute(
                """
                UPDATE setups SET
                    status            = 'after_tp1',
                    result            = 'TP1',
                    avg_entry         = COALESCE(avg_entry, %(avg_entry)s),
                    avg_exit          = %(tp1_price)s,
                    pnl_usd           = %(pnl_usd)s,
                    pnl_pct           = %(pnl_pct)s,
                    tp1_hit_at        = EXTRACT(EPOCH FROM NOW())::BIGINT,
                    exchange_tp1_done = TRUE
                WHERE setup_id = %(setup_id)s
                """,
                {
                    "setup_id":  setup_id,
                    "avg_entry": avg_entry,
                    "tp1_price": tp1_price,
                    "pnl_usd":   pnl_usd,
                    "pnl_pct":   pnl_pct,
                },
            )
    log.info(f"[db] Setup #{setup_id} po TP1: PnL={pnl_usd}, czeka na TP2/BE")


def get_after_tp1_setups() -> list[dict]:
    """Zwraca wszystkie setupy w stanie after_tp1 (TP1 trafiony, czekamy na TP2/BE)."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM setups WHERE status = 'after_tp1' ORDER BY tp1_hit_at ASC"
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


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
    baseline_map = getattr(_thread_local, "baseline", None)
    if baseline_map is None:
        log.warning("save_pending_list: brak baseline — load_pending() nie został wywołany w tym wątku")
        baseline_map = {}
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


def get_committed_trade_usdt(exclude_setup_id: int | None = None) -> float:
    """Sumuje trade_usdt wszystkich aktywnych i pending setupów (exchange_done=FALSE, resolved=FALSE).
    exclude_setup_id: wyklucza setup który właśnie wylicza swój budżet (unika self-counting)."""
    default_tu = float(os.getenv("BITGET_TRADE_USDT", "100"))
    with _conn() as conn:
        with conn.cursor() as cur:
            if exclude_setup_id is not None:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(COALESCE(trade_usdt, %s)), 0)
                    FROM setups
                    WHERE exchange_done = FALSE
                      AND resolved = FALSE
                      AND shadow = FALSE
                      AND setup_id != %s
                    """,
                    (default_tu, exclude_setup_id),
                )
            else:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(COALESCE(trade_usdt, %s)), 0)
                    FROM setups
                    WHERE exchange_done = FALSE
                      AND resolved = FALSE
                      AND shadow = FALSE
                    """,
                    (default_tu,),
                )
            row = cur.fetchone()
            return float(row[0]) if row else 0.0


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

def get_summary_stats(period_days: int | None = None) -> dict:
    """Zwraca statystyki podsumowujące dla dashboardu."""
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage   = 20

    # Per-row trade_usdt z fallbackiem na aktualną wartość env var
    _tu = f"COALESCE(trade_usdt, {trade_usdt})"

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
                COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                     FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
            END
        )"""

    # PnL % per row — używa trade_usdt z momentu otwarcia pozycji
    pnl_pct_calc = f"({pnl_calc}) / NULLIF({_tu}, 0) * 100"

    trading_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')"

    time_sql, time_params = _algo2_time_filter(period_days)

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
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2'))   AS wins,
                    COUNT(*) FILTER (WHERE resolved = TRUE
                        AND result = 'SL')                               AS losses
                FROM setups
                WHERE TRUE {time_sql}
                """,
                time_params,
            )
            row = dict(cur.fetchone())

            # Win rate
            wins   = row.get("wins") or 0
            losses = row.get("losses") or 0
            total  = wins + losses
            row["win_rate_pct"] = round(wins / total * 100, 1) if total > 0 else None

            # Per-model breakdown
            tp1_only_calc = f"""
                CASE
                    WHEN result = 'SL' THEN {pnl_calc}
                    WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')
                         AND (tps->>0) IS NOT NULL
                         AND COALESCE(avg_entry,(entries->>0)::numeric) IS NOT NULL
                    THEN CASE direction WHEN 'long'
                         THEN ((tps->>0)::numeric - COALESCE(avg_entry,(entries->>0)::numeric)) *
                              COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                   FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                         ELSE (COALESCE(avg_entry,(entries->>0)::numeric) - (tps->>0)::numeric) *
                              COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                   FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                         END
                END"""
            tp1_only_pct_calc = f"({tp1_only_calc}) / NULLIF({_tu}, 0) * 100"
            cur.execute(
                f"""
                SELECT model,
                       COUNT(*)                                              AS all_setups,
                       COUNT(*) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})                             AS entered,
                       ROUND(SUM({pnl_calc}) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})::numeric, 2)                AS pnl_usd,
                       ROUND(SUM({tp1_only_calc}) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})::numeric, 2)                AS tp1_only_pnl_usd,
                       ROUND(AVG({pnl_pct_calc}) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})::numeric, 1)                AS avg_pnl_pct,
                       ROUND(AVG({tp1_only_pct_calc}) FILTER (WHERE resolved = TRUE
                           AND {trading_filter})::numeric, 1)                AS avg_tp1only_pct,
                       COUNT(*) FILTER (WHERE resolved = TRUE
                           AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2'))    AS wins
                FROM setups
                WHERE TRUE {time_sql}
                GROUP BY model
                ORDER BY model
                """,
                time_params,
            )
            row["by_model"] = [dict(r) for r in cur.fetchall()]
            row["trade_usdt"] = trade_usdt

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
            _tu = f"COALESCE(trade_usdt, {trade_usdt})"
            pnl_calc = f"""
                COALESCE(pnl_usd,
                    CASE WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                              AND avg_exit IS NOT NULL
                              AND COALESCE(avg_entry, (entries->>0)::numeric) IS NOT NULL
                    THEN
                        CASE direction WHEN 'long'
                            THEN (avg_exit - COALESCE(avg_entry, (entries->>0)::numeric))
                            ELSE (COALESCE(avg_entry, (entries->>0)::numeric) - avg_exit)
                        END *
                        COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                             FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                    END
                )"""
            pnl_pct_calc = f"({pnl_calc}) / NULLIF({_tu}, 0) * 100"
            tp1_only_calc_period = f"""
                CASE
                    WHEN result = 'SL' THEN {pnl_calc}
                    WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')
                         AND (tps->>0) IS NOT NULL
                         AND COALESCE(avg_entry,(entries->>0)::numeric) IS NOT NULL
                    THEN CASE direction WHEN 'long'
                         THEN ((tps->>0)::numeric - COALESCE(avg_entry,(entries->>0)::numeric)) *
                              COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                   FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                         ELSE (COALESCE(avg_entry,(entries->>0)::numeric) - (tps->>0)::numeric) *
                              COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                                   FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                         END
                END"""
            tp1_only_pct_calc = f"({tp1_only_calc_period}) / NULLIF({_tu}, 0) * 100"
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')) AS entered,
                    COUNT(*) FILTER (WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2'))   AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                                         AS losses,
                    COALESCE(ROUND(SUM({pnl_calc}) FILTER (WHERE resolved = TRUE
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL'))::numeric, 2), 0) AS total_income,
                    COALESCE(ROUND(SUM({pnl_pct_calc}) FILTER (WHERE resolved = TRUE
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL'))::numeric, 1), 0) AS total_income_pct,
                    COALESCE(ROUND(SUM({tp1_only_calc_period}) FILTER (WHERE resolved = TRUE
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL'))::numeric, 2), 0) AS tp1_only_income,
                    COALESCE(ROUND(SUM({tp1_only_pct_calc}) FILTER (WHERE resolved = TRUE
                        AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL'))::numeric, 1), 0) AS tp1_only_income_pct
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
            total_income_pct = float(row["total_income_pct"])
            tp1_only_income = float(row["tp1_only_income"])
            tp1_only_income_pct = float(row["tp1_only_income_pct"])

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
                               date_trunc('hour', COALESCE(exit_time, resolved_at, NOW())),
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
        "total_income_pct": total_income_pct,
        "tp1_only_income": tp1_only_income,
        "tp1_only_income_pct": tp1_only_income_pct,
        "avg_daily_pnl": avg_daily_pnl,
        "avg_daily_mult": avg_daily_mult,
        "max_capital": max_capital,
        "max_capital_mult": max_capital_mult,
        "max_open_positions": max_open,
    }


def get_last_setups_per_model(models: list[str] | None = None) -> list[dict]:
    """Zwraca ostatni setup dla każdego modelu (też shadow/odrzucone) — do panelu feedback."""
    if models is None:
        models = ["Algo2", "Grok", "Grok2"]
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (model)
                    setup_id, model, alert_time, direction, type, score,
                    reasoning, status, result, resolved, entries, tps, sl
                FROM setups
                WHERE model = ANY(%(models)s)
                ORDER BY model, alert_time DESC
                """,
                {"models": models},
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def get_recent_resolved(limit: int = 20) -> list[dict]:
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
    models: list[str] | None = None,
    variants: list[str] | None = None,
    types: list[str] | None = None,
    result_cats: list[str] | None = None,
) -> dict:
    """Zwraca zamknięte setupy z filtrami + total count."""
    where = ["resolved = TRUE"]
    params: dict = {}

    if results:
        where.append("result = ANY(%(results)s)")
        params["results"] = results

    if result_cats:
        cat_conds = []
        if "win" in result_cats:
            cat_conds.append("result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')")
        if "loss" in result_cats:
            cat_conds.append("result = 'SL'")
        if "no_entry" in result_cats:
            cat_conds.append("(entry_hit_at IS NULL AND result IS NULL AND cancel_reason IS NULL)")
        if "cancelled" in result_cats:
            cat_conds.append("cancel_reason IS NOT NULL")
        if cat_conds:
            where.append(f"({' OR '.join(cat_conds)})")

    if models:
        where.append("model = ANY(%(models)s)")
        params["models"] = models

    if variants:
        where.append("COALESCE(variant, 'baseline') = ANY(%(variants)s)")
        params["variants"] = variants

    if types:
        where.append("type = ANY(%(types)s)")
        params["types"] = types

    if date_from:
        where.append("resolved_at >= %(date_from)s::date")
        params["date_from"] = date_from

    if date_to:
        where.append("resolved_at < (%(date_to)s::date + interval '1 day')")
        params["date_to"] = date_to

    where_sql = " AND ".join(where)

    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage = 20
    _tu = f"COALESCE(trade_usdt, {trade_usdt})"
    _entry = f"COALESCE(avg_entry,(entries->>0)::numeric)"
    _full_qty = f"""COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                    FLOOR({_tu}*{leverage}/{_entry}/0.1)*0.1)"""
    _half_qty = f"""COALESCE(NULLIF(exchange_qty_half,'')::numeric,
                    FLOOR({_full_qty}/2/0.1)*0.1)"""
    _sign = "CASE direction WHEN 'long' THEN 1 ELSE -1 END"
    pnl_calc_f = f"""
        COALESCE(pnl_usd,
            CASE WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                      AND {_entry} IS NOT NULL
            THEN CASE
                WHEN avg_exit IS NOT NULL
                THEN ({_sign}) * (avg_exit - {_entry}) * ({_full_qty})
                WHEN result = 'TP1+BE' AND (tps->>0) IS NOT NULL
                THEN ({_sign}) * ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                WHEN result = 'SL' AND sl IS NOT NULL
                THEN ({_sign}) * (sl - {_entry}) * ({_full_qty})
                WHEN result = 'TP1+TP2' AND (tps->>0) IS NOT NULL AND (tps->>1) IS NOT NULL
                THEN ({_sign}) * (((tps->>0)::numeric - {_entry}) + ((tps->>1)::numeric - {_entry})) * ({_half_qty})
                WHEN result = 'TP1+SL' AND (tps->>0) IS NOT NULL AND sl IS NOT NULL
                THEN ({_sign}) * ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                   + ({_sign}) * (sl - {_entry}) * ({_half_qty})
            END
            END
        )"""
    pnl_pct_calc_f = f"({pnl_calc_f}) / NULLIF({_tu}, 0) * 100"
    tp1_only_calc_f = f"""
        CASE
            WHEN result = 'SL' THEN {pnl_calc_f}
            WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')
                 AND (tps->>0) IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN ({_sign}) * ((tps->>0)::numeric - {_entry}) * ({_full_qty})
        END"""
    tp1_only_pct_calc_f = f"({tp1_only_calc_f}) / NULLIF({_tu}, 0) * 100"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM setups WHERE {where_sql}", params)
            total = cur.fetchone()["cnt"]

            cur.execute(
                f"""
                SELECT setup_id, alert_time, entry_hit_at, model, direction, type, variant, score,
                       result, avg_entry, avg_exit,
                       ROUND(({pnl_calc_f})::numeric, 2)                 AS pnl_usd,
                       ROUND(({pnl_pct_calc_f})::numeric, 2)             AS pnl_pct,
                       cancel_reason,
                       ROUND(({tp1_only_calc_f})::numeric, 2)            AS tp1_only_pnl,
                       ROUND(({tp1_only_pct_calc_f})::numeric, 2)        AS tp1_only_pnl_pct,
                       exit_time, entries, tps, sl, sl_after_tp1,
                       exchange_qty_full, exchange_qty_half,
                       hypo_result, hypo_pnl_usd, trade_usdt
                FROM setups
                WHERE {where_sql}
                ORDER BY resolved_at DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {**params, "limit": limit, "offset": offset},
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]

            # Totals for filtered set (all pages)
            cur.execute(
                f"""
                SELECT
                    ROUND(COALESCE(SUM({pnl_calc_f}) FILTER (
                        WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                    ), 0)::numeric, 2) AS sum_pnl_usd,
                    ROUND(COALESCE(SUM({pnl_pct_calc_f}) FILTER (
                        WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                    ), 0)::numeric, 2) AS sum_pnl_pct,
                    ROUND(COALESCE(SUM({tp1_only_calc_f}) FILTER (
                        WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                    ), 0)::numeric, 2) AS sum_tp1_only_usd,
                    ROUND(COALESCE(SUM({tp1_only_pct_calc_f}) FILTER (
                        WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                    ), 0)::numeric, 2) AS sum_tp1_only_pct
                FROM setups WHERE {where_sql}
                """,
                params,
            )
            totals_row = dict(cur.fetchone())
            totals = {
                "sum_pnl_usd":      float(totals_row["sum_pnl_usd"] or 0),
                "sum_pnl_pct":      float(totals_row["sum_pnl_pct"] or 0),
                "sum_tp1_only_usd": float(totals_row["sum_tp1_only_usd"] or 0),
                "sum_tp1_only_pct": float(totals_row["sum_tp1_only_pct"] or 0),
                "trade_usdt":       trade_usdt,
            }

    return {"total": total, "rows": rows, "totals": totals}


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


# ── Dashboard v2 statystyki ──────────────────────────────────────────────────

def get_dashboard_stats(period: str = "30d") -> dict:
    """Statystyki dla nowego dashboardu z obsługą okresu: today|24h|7d|30d."""
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage   = 20
    _tu        = f"COALESCE(trade_usdt, {trade_usdt})"
    pnl_expr   = f"""
        COALESCE(pnl_usd,
            CASE WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                      AND avg_exit IS NOT NULL
                      AND COALESCE(avg_entry,(entries->>0)::numeric) IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN (avg_exit - COALESCE(avg_entry,(entries->>0)::numeric))
                 ELSE (COALESCE(avg_entry,(entries->>0)::numeric) - avg_exit)
                 END *
                 COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                      FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
            END
        )"""
    trading   = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')"
    wins_cond = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')"

    _period_intervals = {
        "today": ("DATE(COALESCE(resolved_at, alert_time)) = CURRENT_DATE",
                  "DATE(alert_time) = CURRENT_DATE"),
        "24h":   ("COALESCE(resolved_at, alert_time) >= NOW() - INTERVAL '1 day'",
                  "alert_time >= NOW() - INTERVAL '1 day'"),
        "7d":    ("COALESCE(resolved_at, alert_time) >= NOW() - INTERVAL '7 days'",
                  "alert_time >= NOW() - INTERVAL '7 days'"),
        "30d":   ("COALESCE(resolved_at, alert_time) >= NOW() - INTERVAL '30 days'",
                  "alert_time >= NOW() - INTERVAL '30 days'"),
    }
    tf_closed, tf_all = _period_intervals.get(period, _period_intervals["30d"])

    settings = get_app_settings()
    cfg = settings.get("type_configs", {})
    enabled_pairs = [
        (k.split("__")[0], k.split("__")[1] if "__" in k else "baseline")
        for k, v in cfg.items()
        if isinstance(v, dict) and v.get("enabled") is True
    ]

    params: dict = {}
    if enabled_pairs:
        pair_clauses = []
        for i, (t, v) in enumerate(enabled_pairs):
            params[f"t{i}"] = t
            params[f"v{i}"] = v
            pair_clauses.append(f"(type = %(t{i})s AND variant = %(v{i})s)")
        type_filter = "AND (" + " OR ".join(pair_clauses) + ")"
    else:
        type_filter = ""

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (WHERE resolved = FALSE)                              AS active_count,
                    COUNT(*) FILTER (WHERE {tf_all})                                      AS total_period,
                    COUNT(*) FILTER (WHERE {tf_all} AND entry_hit_at IS NOT NULL)          AS entered_period,
                    COUNT(*) FILTER (WHERE resolved = TRUE AND {wins_cond} AND {tf_closed}) AS wins,
                    COUNT(*) FILTER (WHERE resolved = TRUE AND result = 'SL' AND {tf_closed}) AS losses,
                    ROUND(SUM({pnl_expr}) FILTER (
                        WHERE resolved = TRUE AND {trading} AND {tf_closed}
                    )::numeric, 2)                                                        AS pnl
                FROM setups
                WHERE model = 'Algo2'
                {type_filter}
                """,
                params,
            )
            row = dict(cur.fetchone())
    wins          = int(row.get("wins")          or 0)
    losses        = int(row.get("losses")        or 0)
    total         = wins + losses
    total_period  = int(row.get("total_period")  or 0)
    entered       = int(row.get("entered_period") or 0)
    return {
        "balance":      trade_usdt,
        "pnl":          float(row["pnl"]) if row.get("pnl") is not None else None,
        "win_rate":     round(wins / total * 100, 1)    if total > 0        else None,
        "entry_rate":   round(entered / total_period * 100, 1) if total_period > 0 else None,
        "active_count": int(row.get("active_count") or 0),
        "closed_count": total,
        "period":       period,
        "trade_usdt":   trade_usdt,
    }


# ── Analityka Algo2 ───────────────────────────────────────────────────────────

def _algo2_time_filter(period_days: int | None) -> tuple[str, dict]:
    """Zwraca fragment SQL i parametry dla filtra czasowego Algo2."""
    if period_days:
        return "AND alert_time >= NOW() - %(interval)s::interval", {"interval": f"{period_days} days"}
    return "", {}


def get_algo2_type_stats(period_days: int | None = None) -> list[dict]:
    """Statystyki per typ setupu dla Algo2 (shadow i non-shadow łącznie).

    Kolumny: type, direction, total, entered, entry_rate, wins, losses,
             win_rate, avg_pnl_usd, tp2_hits, tp2_rate,
             avg_time_to_entry_h, avg_hold_h
    """
    time_sql, time_params = _algo2_time_filter(period_days)
    wins_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(type,''), '(brak)') AS type,
                    direction,
                    COUNT(*)                                                              AS total,
                    COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)                     AS entered,
                    ROUND(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)::numeric
                          / NULLIF(COUNT(*), 0) * 100, 1)                                AS entry_rate,
                    COUNT(*) FILTER (WHERE {wins_filter})                                AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                                AS losses,
                    ROUND(COUNT(*) FILTER (WHERE {wins_filter})::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE {wins_filter})
                                 + COUNT(*) FILTER (WHERE result = 'SL'), 0) * 100, 1)  AS win_rate,
                    ROUND(AVG(pnl_usd) FILTER (
                          WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                    )::numeric, 2)                                                        AS avg_pnl_usd,
                    COUNT(*) FILTER (WHERE result IN ('TP2','TP1+TP2'))                  AS tp2_hits,
                    ROUND(COUNT(*) FILTER (WHERE result IN ('TP2','TP1+TP2'))::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                       AS tp2_rate,
                    ROUND(AVG(
                        CASE WHEN entry_hit_at IS NOT NULL AND alert_timestamp IS NOT NULL
                             THEN (entry_hit_at - alert_timestamp) / 3600.0 END
                    )::numeric, 1)                                                        AS avg_time_to_entry_h,
                    ROUND(AVG(
                        CASE WHEN exit_time IS NOT NULL AND entry_hit_at IS NOT NULL
                             THEN EXTRACT(EPOCH FROM exit_time - to_timestamp(entry_hit_at)) / 3600.0 END
                    )::numeric, 1)                                                        AS avg_hold_h
                FROM setups
                WHERE model = 'Algo2'
                  {time_sql}
                GROUP BY type, direction
                ORDER BY type, direction
                """,
                time_params,
            )
            return [dict(r) for r in cur.fetchall()]


def get_algo2_variant_stats(period_days: int | None = None) -> list[dict]:
    """Porównanie wariantów parametrów (kalibracja) dla trend_pullback_long/short.

    Kolumny: variant, type, direction, total, entered, entry_rate, wins, losses,
             win_rate, avg_pnl_usd, tp1_rate, tp2_rate, sl_rate, avg_rr, avg_hold_h
    """
    time_sql, time_params = _algo2_time_filter(period_days)
    wins_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COALESCE(variant, 'baseline')                                          AS variant,
                    COALESCE(NULLIF(type,''), '(brak)')                                    AS type,
                    direction,
                    COUNT(*)                                                               AS total,
                    COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)                      AS entered,
                    ROUND(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)::numeric
                          / NULLIF(COUNT(*), 0) * 100, 1)                                 AS entry_rate,
                    COUNT(*) FILTER (WHERE {wins_filter})                                  AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                                  AS losses,
                    ROUND(COUNT(*) FILTER (WHERE {wins_filter})::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE {wins_filter})
                                 + COUNT(*) FILTER (WHERE result = 'SL'), 0) * 100, 1)   AS win_rate,
                    ROUND(AVG(pnl_usd) FILTER (
                          WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                    )::numeric, 2)                                                          AS avg_pnl_usd,
                    ROUND(COUNT(*) FILTER (WHERE {wins_filter})::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS tp1_rate,
                    ROUND(COUNT(*) FILTER (WHERE result IN ('TP2','TP1+TP2'))::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS tp2_rate,
                    ROUND(COUNT(*) FILTER (WHERE result = 'SL')::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS sl_rate,
                    ROUND(AVG(rr) FILTER (WHERE entry_hit_at IS NOT NULL)::numeric, 2)    AS avg_rr,
                    ROUND(AVG(
                        CASE WHEN exit_time IS NOT NULL AND entry_hit_at IS NOT NULL
                             THEN EXTRACT(EPOCH FROM exit_time - to_timestamp(entry_hit_at)) / 3600.0
                        END
                    )::numeric, 1)                                                         AS avg_hold_h
                FROM setups
                WHERE model = 'Algo2'
                  AND type LIKE 'trend_pullback%'
                  {time_sql}
                GROUP BY variant, type, direction
                ORDER BY variant, type, direction
                """,
                time_params,
            )
            return [dict(r) for r in cur.fetchall()]


def get_algo2_variant_summary(period_days: int | None = None, pairs: list[tuple[str, str]] | None = None) -> list[dict]:
    """Zestawienie wyników Algo2 per wariant (wszystkie typy setupów łącznie)."""
    time_sql, time_params = _algo2_time_filter(period_days)
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage   = 20
    _tu        = f"COALESCE(trade_usdt, {trade_usdt})"
    wins_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')"
    trading_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')"
    _entry = "COALESCE(avg_entry,(entries->>0)::numeric)"
    _full_qty = f"""COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                           FLOOR({_tu}*{leverage}/{_entry}/0.1)*0.1)"""
    _half_qty = f"""GREATEST(FLOOR(
                    COALESCE(NULLIF(exchange_qty_half,'')::numeric, ({_full_qty}) / 2)
                    / 0.1) * 0.1, 0.1)"""
    tp1_only = f"""
        CASE
            WHEN result = 'SL'
                THEN pnl_usd
            WHEN {wins_filter}
                 AND (tps->>0) IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_full_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_full_qty})
                 END
        END"""
    tp1tp2_calc = f"""
        CASE
            WHEN result = 'SL' AND sl IS NOT NULL AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN (sl - {_entry}) * ({_full_qty})
                 ELSE ({_entry} - sl) * ({_full_qty})
                 END
            WHEN result IN ('TP1+TP2','TP2')
                 AND (tps->>0) IS NOT NULL AND (tps->>1) IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                    + ((tps->>1)::numeric - {_entry}) * ({_half_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_half_qty})
                    + ({_entry} - (tps->>1)::numeric) * ({_half_qty})
                 END
            WHEN result = 'TP1+BE'
                 AND (tps->>0) IS NOT NULL AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_half_qty})
                 END
            WHEN result = 'TP1+SL'
                 AND (tps->>0) IS NOT NULL AND sl IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                    + (sl - {_entry}) * ({_half_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_half_qty})
                    + ({_entry} - sl) * ({_half_qty})
                 END
            WHEN result = 'TP1'
                 AND (tps->>0) IS NOT NULL AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_full_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_full_qty})
                 END
        END"""
    pair_sql = ""
    pair_params: dict = {}
    if pairs:
        conds = " OR ".join(
            f"(type = %(pt{i})s AND COALESCE(variant,'baseline') = %(pv{i})s)"
            for i in range(len(pairs))
        )
        pair_sql = f"AND ({conds})"
        for i, (t, v) in enumerate(pairs):
            pair_params[f"pt{i}"] = t
            pair_params[f"pv{i}"] = v
    params = {**time_params, **pair_params}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COALESCE(type, 'unknown')                                              AS scenario,
                    COALESCE(variant, 'baseline')                                          AS variant,
                    COUNT(*)                                                               AS total,
                    COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)                      AS entered,
                    ROUND(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)::numeric
                          / NULLIF(COUNT(*), 0) * 100, 1)                                 AS entry_rate,
                    COUNT(*) FILTER (WHERE {wins_filter})                                  AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                                  AS losses,
                    ROUND(AVG({tp1_only}) FILTER (WHERE {trading_filter})::numeric, 2)    AS avg_tp1only_usd,
                    ROUND(AVG({tp1tp2_calc}) FILTER (WHERE {trading_filter})::numeric, 2) AS avg_tp1tp2_usd,
                    ROUND(AVG(rr) FILTER (WHERE entry_hit_at IS NOT NULL)::numeric, 2)    AS avg_rr,
                    ROUND(COUNT(*) FILTER (WHERE result IN ('TP2','TP1+TP2'))::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS tp2_rate,
                    ROUND(COUNT(*) FILTER (WHERE result = 'TP1+BE')::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS tp1_be_rate,
                    ROUND(COUNT(*) FILTER (WHERE result = 'SL')::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS sl_rate,
                    ROUND(AVG({_tu}) FILTER (WHERE {trading_filter})::numeric, 2)          AS avg_trade_usdt,
                    ROUND(SUM(({tp1_only}) / NULLIF({_tu}, 0) * 100) FILTER (WHERE {trading_filter})::numeric, 2) AS sum_pct_tp1,
                    ROUND(SUM(({tp1tp2_calc}) / NULLIF({_tu}, 0) * 100) FILTER (WHERE {trading_filter})::numeric, 2) AS sum_pct_tp12,
                    COUNT(DISTINCT (COALESCE(exit_time, resolved_at, alert_time) AT TIME ZONE 'Europe/Warsaw')::date)
                        FILTER (WHERE {trading_filter})                                    AS trading_days
                FROM setups
                WHERE model = 'Algo2'
                  {time_sql}
                  {pair_sql}
                GROUP BY type, variant
                ORDER BY type, variant
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]


def get_algo2_daily_stats(
    period_days: int | None = None,
    pairs: list[tuple[str, str]] | None = None,
) -> list[dict]:
    """Zestawienie wyników Algo2 per dzień kalendarzowy (czas Warsaw).
    pairs: lista par (type, variant) do filtrowania; None = wszystkie.
    """
    time_sql, time_params = _algo2_time_filter(period_days)
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage   = 20
    _tu        = f"COALESCE(trade_usdt, {trade_usdt})"
    wins_filter    = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')"
    trading_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')"
    _entry = "COALESCE(avg_entry,(entries->>0)::numeric)"
    _full_qty = f"""COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                           FLOOR({_tu}*{leverage}/{_entry}/0.1)*0.1)"""
    _half_qty = f"""GREATEST(FLOOR(
                    COALESCE(NULLIF(exchange_qty_half,'')::numeric, ({_full_qty}) / 2)
                    / 0.1) * 0.1, 0.1)"""
    tp1_only = f"""
        CASE
            WHEN result = 'SL'
                THEN pnl_usd
            WHEN {wins_filter}
                 AND (tps->>0) IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_full_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_full_qty})
                 END
        END"""
    tp1tp2_calc = f"""
        CASE
            WHEN result = 'SL' AND sl IS NOT NULL AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN (sl - {_entry}) * ({_full_qty})
                 ELSE ({_entry} - sl) * ({_full_qty})
                 END
            WHEN result IN ('TP1+TP2','TP2')
                 AND (tps->>0) IS NOT NULL AND (tps->>1) IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                    + ((tps->>1)::numeric - {_entry}) * ({_half_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_half_qty})
                    + ({_entry} - (tps->>1)::numeric) * ({_half_qty})
                 END
            WHEN result = 'TP1+BE'
                 AND (tps->>0) IS NOT NULL AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_half_qty})
                 END
            WHEN result = 'TP1+SL'
                 AND (tps->>0) IS NOT NULL AND sl IS NOT NULL
                 AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_half_qty})
                    + (sl - {_entry}) * ({_half_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_half_qty})
                    + ({_entry} - sl) * ({_half_qty})
                 END
            WHEN result = 'TP1'
                 AND (tps->>0) IS NOT NULL AND {_entry} IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - {_entry}) * ({_full_qty})
                 ELSE ({_entry} - (tps->>0)::numeric) * ({_full_qty})
                 END
        END"""
    pair_sql = ""
    pair_params: dict = {}
    if pairs:
        conds = " OR ".join(
            f"(type = %(pt{i})s AND COALESCE(variant,'baseline') = %(pv{i})s)"
            for i in range(len(pairs))
        )
        pair_sql = f"AND ({conds})"
        for i, (t, v) in enumerate(pairs):
            pair_params[f"pt{i}"] = t
            pair_params[f"pv{i}"] = v
    params = {**time_params, **pair_params}
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    (COALESCE(exit_time, resolved_at, alert_time) AT TIME ZONE 'Europe/Warsaw')::date AS day,
                    COUNT(*)                                                               AS total,
                    COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)                      AS entered,
                    COUNT(*) FILTER (WHERE {wins_filter})                                  AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                                  AS losses,
                    ROUND(COUNT(*) FILTER (WHERE {wins_filter})::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0)
                          * 100, 1)                                                        AS win_rate,
                    ROUND(AVG({tp1_only}) FILTER (WHERE {trading_filter})
                          / NULLIF(AVG({_tu}) FILTER (WHERE {trading_filter}), 0)
                          * 100::numeric, 1)                                               AS avg_pct_tp1,
                    ROUND(AVG({tp1tp2_calc}) FILTER (WHERE {trading_filter})
                          / NULLIF(AVG({_tu}) FILTER (WHERE {trading_filter}), 0)
                          * 100::numeric, 1)                                               AS avg_pct_tp12
                FROM setups
                WHERE model = 'Algo2'
                  {time_sql}
                  {pair_sql}
                GROUP BY day
                ORDER BY day DESC
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]


def get_algo2_time_heatmap(period_days: int | None = None) -> list[dict]:
    """Heatmapa godzinowa dla Algo2: liczba alertów, % entry, % wygranych per godzina (czas Warsaw).

    Kolumny: hour (0-23), total, entered, entry_rate, wins, losses, win_rate
    """
    time_sql, time_params = _algo2_time_filter(period_days)
    wins_filter = "result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    EXTRACT(HOUR FROM alert_time AT TIME ZONE 'Europe/Warsaw')::int AS hour,
                    COUNT(*)                                                          AS total,
                    COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)                 AS entered,
                    ROUND(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)::numeric
                          / NULLIF(COUNT(*), 0) * 100, 1)                            AS entry_rate,
                    COUNT(*) FILTER (WHERE {wins_filter})                            AS wins,
                    COUNT(*) FILTER (WHERE result = 'SL')                            AS losses,
                    ROUND(COUNT(*) FILTER (WHERE {wins_filter})::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE {wins_filter})
                                 + COUNT(*) FILTER (WHERE result = 'SL'), 0) * 100, 1) AS win_rate
                FROM setups
                WHERE model = 'Algo2'
                  {time_sql}
                GROUP BY hour
                ORDER BY hour
                """,
                time_params,
            )
            return [dict(r) for r in cur.fetchall()]


def get_algo2_rr_analysis(period_days: int | None = None) -> list[dict]:
    """Analiza RR dla Algo2: deklarowany RR vs rzeczywiste wyniki TP1/TP2.

    Kolumny: type, direction, entered, avg_rr_declared, tp1_rate, tp2_rate,
             tp1_be_sl_hits (TP1 ale exit na SL/BE), sl_rate
    """
    time_sql, time_params = _algo2_time_filter(period_days)
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(type,''), '(brak)') AS type,
                    direction,
                    COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL)             AS entered,
                    ROUND(AVG(rr)::numeric, 2)                                   AS avg_rr_declared,
                    COUNT(*) FILTER (WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')) AS tp1_hits,
                    ROUND(COUNT(*) FILTER (WHERE result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2'))::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0) * 100, 1) AS tp1_rate,
                    COUNT(*) FILTER (WHERE result IN ('TP2','TP1+TP2'))          AS tp2_hits,
                    ROUND(COUNT(*) FILTER (WHERE result IN ('TP2','TP1+TP2'))::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0) * 100, 1) AS tp2_rate,
                    COUNT(*) FILTER (WHERE result IN ('TP1+BE','TP1+SL'))        AS tp1_be_sl_hits,
                    COUNT(*) FILTER (WHERE result = 'SL')                        AS sl_hits,
                    ROUND(COUNT(*) FILTER (WHERE result = 'SL')::numeric
                          / NULLIF(COUNT(*) FILTER (WHERE entry_hit_at IS NOT NULL), 0) * 100, 1) AS sl_rate
                FROM setups
                WHERE model = 'Algo2'
                  {time_sql}
                GROUP BY type, direction
                ORDER BY type, direction
                """,
                time_params,
            )
            return [dict(r) for r in cur.fetchall()]


def get_resolved_types(date_from: str | None = None, date_to: str | None = None) -> dict:
    """Unikalne typy i warianty zamkniętych setupów — dla filtrów w Historii.
    Wyklucza typy ze spacjami (wolny tekst / opisy) i filtruje po zakresie dat.
    """
    where = [
        "resolved = TRUE",
        "type IS NOT NULL",
        "type NOT LIKE '%% %%'",     # wyklucz wolny tekst z opisami (psycopg2 escape: %% = %)
        "length(type) <= 60",      # dodatkowe zabezpieczenie przed długimi opisami
    ]
    params: dict = {}
    if date_from:
        where.append("alert_time >= %(date_from)s::date")
        params["date_from"] = date_from
    if date_to:
        where.append("alert_time < (%(date_to)s::date + INTERVAL '1 day')")
        params["date_to"] = date_to
    sql = (
        "SELECT DISTINCT type, variant FROM setups "
        f"WHERE {' AND '.join(where)} ORDER BY type, variant"
    )
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    types    = sorted({r[0] for r in rows if r[0]})
    variants = sorted({
        r[1] for r in rows
        if r[1] and ' ' not in r[1] and len(r[1]) <= 60 and r[1] not in types
    })
    return {"types": types, "variants": variants}


def get_all_types() -> dict:
    """Unikalne typy i warianty wszystkich setupów (aktywnych i zamkniętych)."""
    sql = (
        "SELECT DISTINCT type, variant FROM setups "
        "WHERE type IS NOT NULL AND type NOT LIKE '%% %%' AND length(type) <= 60 "
        "ORDER BY type, variant"
    )
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    types = sorted({r[0] for r in rows if r[0]})
    variants = sorted({
        r[1] for r in rows
        if r[1] and ' ' not in r[1] and len(r[1]) <= 60 and r[1] not in types
    })
    return {"types": types, "variants": variants}


def get_all_variants_tree_by_model() -> list[dict]:
    """3-poziomowe drzewo model→typ→wariant ze wszystkich setupów.
    active = miał setupy w ostatnich 90 dniach."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT COALESCE(model,'?') AS m, type, COALESCE(variant,'baseline') AS v "
                "FROM setups "
                "WHERE type IS NOT NULL AND type NOT LIKE '%% %%' AND length(type) <= 60 "
                "ORDER BY m, type, v"
            )
            all_rows = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT COALESCE(model,'?') AS m, type, COALESCE(variant,'baseline') AS v "
                "FROM setups "
                "WHERE type IS NOT NULL AND type NOT LIKE '%% %%' AND length(type) <= 60 "
                "  AND alert_time >= NOW() - INTERVAL '90 days'"
            )
            recent = {(r[0], r[1], r[2]) for r in cur.fetchall()}

    tree: dict = {}
    for (model, type_, variant) in all_rows:
        if model not in tree:
            tree[model] = {}
        if type_ not in tree[model]:
            tree[model][type_] = []
        tree[model][type_].append({"name": variant, "active": (model, type_, variant) in recent})

    result = []
    for model in sorted(tree):
        types = [
            {"name": t, "active": any(v["active"] for v in vs),
             "variants": sorted(vs, key=lambda x: x["name"])}
            for t, vs in sorted(tree[model].items())
        ]
        result.append({"name": model, "active": any(t["active"] for t in types), "types": types})
    return result


def get_all_setups_filtered(
    statuses: list[str] | None = None,
    types: list[str] | None = None,
    variants: list[str] | None = None,
    models: list[str] | None = None,
    shadow: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Zwraca wszystkie setupy (aktywne i zamknięte) z filtrami.
    Obsługiwane statusy: pending, open, after_tp1, zamkniete, anulowane, nie_weszlo."""
    where: list[str] = []
    params: dict = {}

    if statuses:
        status_conds: list[str] = []
        normal = [s for s in statuses if s not in ("zamkniete", "anulowane", "nie_weszlo")]
        if normal:
            status_conds.append("status = ANY(%(statuses_normal)s)")
            params["statuses_normal"] = normal
        if "zamkniete" in statuses:
            status_conds.append(
                "(status = 'closed' AND entry_hit_at IS NOT NULL"
                " AND (result IS NULL OR result != 'anulowany') AND cancel_reason IS NULL)"
            )
        if "anulowane" in statuses:
            status_conds.append(
                "(status = 'closed' AND (result = 'anulowany' OR cancel_reason IS NOT NULL))"
            )
        if "nie_weszlo" in statuses:
            status_conds.append(
                "(status = 'closed' AND entry_hit_at IS NULL AND cancel_reason IS NULL"
                " AND (result IS NULL OR result NOT IN ('anulowany')))"
            )
        if status_conds:
            where.append(f"({' OR '.join(status_conds)})")

    if types:
        where.append("type = ANY(%(types)s)")
        params["types"] = types

    if variants:
        where.append("COALESCE(variant, 'baseline') = ANY(%(variants)s)")
        params["variants"] = variants

    if models:
        where.append("COALESCE(model, '?') = ANY(%(models)s)")
        params["models"] = models

    if shadow is not None:
        where.append("shadow = %(shadow)s")
        params["shadow"] = shadow

    if date_from:
        where.append("alert_time >= %(date_from)s::date")
        params["date_from"] = date_from

    if date_to:
        where.append("alert_time < (%(date_to)s::date + interval '1 day')")
        params["date_to"] = date_to

    where_sql = " AND ".join(where) if where else "TRUE"

    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage = 20
    _tu = f"COALESCE(trade_usdt, {trade_usdt})"
    pnl_calc_f = f"""
        COALESCE(pnl_usd,
            CASE WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                      AND avg_exit IS NOT NULL
                      AND COALESCE(avg_entry,(entries->>0)::numeric) IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN (avg_exit - COALESCE(avg_entry,(entries->>0)::numeric))
                 ELSE (COALESCE(avg_entry,(entries->>0)::numeric) - avg_exit)
                 END *
                 COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                      FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
            END
        )"""
    tp1_only_calc_f = f"""
        CASE
            WHEN result = 'SL' THEN {pnl_calc_f}
            WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')
                 AND (tps->>0) IS NOT NULL
                 AND COALESCE(avg_entry,(entries->>0)::numeric) IS NOT NULL
            THEN CASE direction WHEN 'long'
                 THEN ((tps->>0)::numeric - COALESCE(avg_entry,(entries->>0)::numeric)) *
                      COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                           FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                 ELSE (COALESCE(avg_entry,(entries->>0)::numeric) - (tps->>0)::numeric) *
                      COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                           FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                 END
        END"""
    tp1_only_pct_calc_f = f"({tp1_only_calc_f}) / NULLIF({_tu}, 0) * 100"
    pnl_pct_calc_f = f"({pnl_calc_f}) / NULLIF({_tu}, 0) * 100"

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS cnt FROM setups WHERE {where_sql}", params)
            total = cur.fetchone()["cnt"]

            cur.execute(
                f"""
                SELECT setup_id, alert_time, entry_hit_at, exit_time, model,
                       direction, type, variant, score, rr, status, resolved,
                       result, avg_entry, avg_exit, pnl_usd, pnl_pct,
                       cancel_reason, shadow,
                       exchange_position_opened, exchange_tp1_done,
                       ROUND(({tp1_only_calc_f})::numeric, 2)     AS tp1_only_pnl,
                       ROUND(({tp1_only_pct_calc_f})::numeric, 2) AS tp1_only_pnl_pct,
                       entries, tps, sl,
                       trade_usdt, exchange_qty_full, exchange_qty_half,
                       exchange_tp1_oid, exchange_sl_oid
                FROM setups
                WHERE {where_sql}
                ORDER BY alert_time DESC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {**params, "limit": limit, "offset": offset},
            )
            rows = [_row_to_dict(r) for r in cur.fetchall()]

    return {"total": total, "rows": rows}


# ── Ustawienia aplikacji ───────────────────────────────────────────────────────

_SETTINGS_DEFAULT: dict = {
    "trade_usdt":    100.0,
    "leverage":      20,
    "max_positions": 5,
    "type_configs":  {},
}


def get_app_settings() -> dict:
    """Zwraca ustawienia aplikacji. Jeśli tabela pusta — zwraca defaults."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT data FROM app_settings WHERE id = 1")
                row = cur.fetchone()
        data = dict(row["data"]) if row and row["data"] else {}
    except Exception as e:
        log.warning(f"[settings] get_app_settings błąd: {e}")
        data = {}
    result = {**_SETTINGS_DEFAULT, **data}
    if "type_configs" not in result:
        result["type_configs"] = {}
    return result


def save_app_settings(data: dict) -> None:
    """Zapisuje ustawienia aplikacji do DB."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (id, data, updated_at)
                VALUES (1, %s::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                """,
                (json.dumps(data),),
            )


def save_algo_scans(scans: dict) -> None:
    """Persystuje ostatnie wyniki skanów algo do DB (przeżywa restart serwisu)."""
    try:
        settings = get_app_settings()
        settings["_last_algo_scans"] = scans
        save_app_settings(settings)
    except Exception as e:
        log.warning(f"[algo] save_algo_scans błąd: {e}")


def get_algo_scans() -> dict:
    """Zwraca ostatnio zapisane wyniki skanów algo z DB."""
    try:
        settings = get_app_settings()
        return settings.get("_last_algo_scans") or {}
    except Exception as e:
        log.warning(f"[algo] get_algo_scans błąd: {e}")
        return {}


def get_weekly_pnl(since_utc: "datetime") -> float:
    """Suma pnl_usd setupów zamkniętych od `since_utc` do teraz."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(pnl_usd), 0)
                FROM setups
                WHERE resolved = TRUE
                  AND shadow = FALSE
                  AND pnl_usd IS NOT NULL
                  AND resolved_at >= %s
                """,
                (since_utc,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else 0.0


def save_transfer_log(entry: dict) -> None:
    """Dołącza wpis do historii tygodniowych transferów w app_settings."""
    settings = get_app_settings()
    history = settings.get("transfer_history") or []
    history.append(entry)
    history = history[-52:]  # max rok historii
    settings["transfer_history"] = history
    save_app_settings(settings)


def get_trade_analysis(date_from: str | None = None) -> list[dict]:
    """Zestawienie setupów SHADOW do analizy symulacyjnej.
    Zwraca posortowane wg entry_hit_at rekordy z czasami wejścia/wyjścia
    oraz P&L% dla strategii TP1+TP2 (faktyczny wynik) i TP1-only (hipotetyczny).
    Typy: range, impulse_aggressive (h1_atr), trend_pullback baseline.
    """
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    leverage = 20
    _tu = f"COALESCE(trade_usdt, {trade_usdt})"

    tp1_only_pct_calc = f"""
        ROUND(
            CASE
                WHEN result = 'SL' THEN pnl_pct
                WHEN result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2')
                     AND (tps->>0) IS NOT NULL
                     AND COALESCE(avg_entry,(entries->>0)::numeric) IS NOT NULL
                THEN (
                    CASE direction
                        WHEN 'long' THEN
                            ((tps->>0)::numeric - COALESCE(avg_entry,(entries->>0)::numeric))
                        ELSE
                            (COALESCE(avg_entry,(entries->>0)::numeric) - (tps->>0)::numeric)
                    END
                    * COALESCE(NULLIF(exchange_qty_full,'')::numeric,
                               FLOOR({_tu}*{leverage}/COALESCE(avg_entry,(entries->>0)::numeric)/0.1)*0.1)
                ) / NULLIF({_tu}, 0) * 100
            END
        ::numeric, 2)"""

    if date_from:
        try:
            dt = datetime.fromisoformat(date_from)
        except ValueError:
            dt = datetime.fromisoformat(date_from + "T00:00:00")
        date_ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
    else:
        date_ts = 1778803200  # 2026-05-15 00:00:00 UTC

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    setup_id,
                    alert_time,
                    type,
                    variant,
                    direction,
                    result,
                    entry_hit_at                                           AS entry_ts,
                    to_char(to_timestamp(entry_hit_at) AT TIME ZONE 'UTC',
                            'YYYY-MM-DD HH24:MI')                         AS entry_time,
                    to_char(exit_time AT TIME ZONE 'UTC',
                            'YYYY-MM-DD HH24:MI')                         AS exit_time,
                    EXTRACT(EPOCH FROM
                        (exit_time - to_timestamp(entry_hit_at)))::int     AS duration_sec,
                    ROUND(pnl_pct::numeric, 2)                             AS pnl_tp1tp2_pct,
                    {tp1_only_pct_calc}                                    AS pnl_tp1only_pct
                FROM setups
                WHERE shadow = TRUE
                  AND resolved = TRUE
                  AND entry_hit_at IS NOT NULL
                  AND entry_hit_at >= %(date_ts)s
                  AND result IN ('TP1','TP2','TP1+BE','TP1+SL','TP1+TP2','SL')
                  AND (
                      type IN ('range_support_long', 'range_resistance_short')
                      OR type IN ('impulse_aggressive_short', 'impulse_aggressive_long')
                      OR (type IN ('trend_pullback_short','trend_pullback_long')
                          AND variant = 'baseline')
                  )
                ORDER BY entry_hit_at ASC
                """,
                {"date_ts": date_ts},
            )
            return [_row_to_dict(r) for r in cur.fetchall()]


def get_exchange_events(setup_id: int | None = None, limit: int = 100) -> list[dict]:
    """Zwraca ostatnie zdarzenia exchange, opcjonalnie filtrowane po setup_id."""
    try:
        with _conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if setup_id is not None:
                    cur.execute(
                        "SELECT * FROM exchange_events WHERE setup_id = %s ORDER BY created_at DESC LIMIT %s",
                        (setup_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM exchange_events ORDER BY created_at DESC LIMIT %s",
                        (limit,),
                    )
                rows = cur.fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    if d.get("created_at"):
                        d["created_at"] = d["created_at"].isoformat()
                    if isinstance(d.get("detail"), str):
                        try:
                            d["detail"] = json.loads(d["detail"])
                        except Exception:
                            pass
                    result.append(d)
                return result
    except Exception as e:
        logging.getLogger("db").warning(f"get_exchange_events: {e}")
        return []


def log_exchange_event(setup_id: int | None, event: str, detail: dict | None = None) -> None:
    """Zapisuje zdarzenie exchange do tabeli exchange_events."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO exchange_events (setup_id, event, detail) VALUES (%s, %s, %s)",
                    (setup_id, event, json.dumps(detail or {})),
                )
    except Exception as e:
        logging.getLogger("db").warning(f"log_exchange_event: {e}")
