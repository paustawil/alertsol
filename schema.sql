-- AlertSol PostgreSQL Schema
-- Zastępuje: pending_setups.json, last_alerts.json, setup_counter.json

CREATE TABLE IF NOT EXISTS setups (
    -- Identyfikacja
    setup_id          SERIAL PRIMARY KEY,
    alert_time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_timestamp   BIGINT NOT NULL,

    -- Sygnał
    model             TEXT NOT NULL,
    rejection         TEXT NOT NULL DEFAULT '',
    type              TEXT NOT NULL DEFAULT '',
    direction         TEXT NOT NULL,
    score             NUMERIC(6,2),
    kurs              NUMERIC(10,2),
    price_at_alert    NUMERIC(10,2),
    warunek           TEXT,
    entry_trigger     TEXT,
    reasoning         TEXT,
    llm_scores        JSONB,

    -- Poziomy
    entries           JSONB NOT NULL DEFAULT '[]',
    tps               JSONB NOT NULL DEFAULT '[]',
    sl                NUMERIC(10,2),
    sl_after_tp1      NUMERIC(10,2),
    rr                NUMERIC(5,2),

    -- Śledzenie wejścia
    entry_hit_at      BIGINT,
    avg_entry         NUMERIC(10,4),
    entries_hit       INT NOT NULL DEFAULT 1,
    sl_adjusted       BOOLEAN NOT NULL DEFAULT FALSE,

    -- Śledzenie wyjścia
    tp1_hit_at        BIGINT,
    exit_time         TIMESTAMPTZ,
    avg_exit          NUMERIC(10,4),
    result            TEXT,
    pnl_usd           NUMERIC(10,4),
    pnl_pct           NUMERIC(8,4),

    -- Grok shadow tracking
    shadow            BOOLEAN NOT NULL DEFAULT FALSE,
    cancel_reason     TEXT,
    cancel_time       TIMESTAMPTZ,
    cancel_price      NUMERIC(10,2),

    -- Exchange (Bitget) order tracking
    exchange_plan_oid         TEXT,
    exchange_plan2_oid        TEXT,
    exchange_qty_full         TEXT,
    exchange_qty_half         TEXT,
    exchange_position_opened  BOOLEAN NOT NULL DEFAULT FALSE,
    exchange_tp1_oid          TEXT,
    exchange_tp2_oid          TEXT,
    exchange_sl_oid           TEXT,
    exchange_sl2_oid          TEXT,
    exchange_tp1_done         BOOLEAN NOT NULL DEFAULT FALSE,
    exchange_done             BOOLEAN NOT NULL DEFAULT FALSE,

    -- Hipotetyczne wyniki (dla setupów które nie weszły)
    hypo_result       TEXT,
    hypo_pnl_usd      NUMERIC(10,4),

    -- Zamknięcie
    resolved          BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at       TIMESTAMPTZ,
    sheets_exported   BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_setups_active  ON setups (resolved) WHERE resolved = FALSE;
CREATE INDEX IF NOT EXISTS idx_setups_model   ON setups (model);
CREATE INDEX IF NOT EXISTS idx_setups_result  ON setups (result) WHERE result IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_setups_export  ON setups (sheets_exported) WHERE resolved = TRUE AND sheets_exported = FALSE;

-- Cooldown tracking (zastępuje last_alerts.json)
CREATE TABLE IF NOT EXISTS alerts_log (
    id         SERIAL PRIMARY KEY,
    model      TEXT NOT NULL,
    level      NUMERIC(10,2) NOT NULL,
    direction  TEXT NOT NULL,
    alerted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_recent ON alerts_log (model, alerted_at DESC);

-- ── Migracje kolumn (idempotentne — bezpieczne do ponownego uruchomienia) ──────
-- Uruchamiane przy każdym init_schema() — dodają brakujące kolumny do istniejących tabel.

ALTER TABLE setups ADD COLUMN IF NOT EXISTS entry_trigger          TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS reasoning              TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS llm_scores             JSONB;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS price_at_alert         NUMERIC(10,2);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS sl_after_tp1           NUMERIC(10,2);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS avg_entry              NUMERIC(10,4);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS entries_hit            INT NOT NULL DEFAULT 1;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS sl_adjusted            BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS tp1_hit_at             BIGINT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS avg_exit               NUMERIC(10,4);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exit_time              TIMESTAMPTZ;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS result                 TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS pnl_usd                NUMERIC(10,4);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS pnl_pct                NUMERIC(8,4);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS shadow                 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS cancel_reason          TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS cancel_time            TIMESTAMPTZ;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS cancel_price           NUMERIC(10,2);
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_plan_oid      TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_plan2_oid     TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_qty_full      TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_qty_half      TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_position_opened BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_tp1_oid       TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_tp2_oid       TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_sl_oid        TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_sl2_oid       TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_tp1_done      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS exchange_done          BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS resolved_at            TIMESTAMPTZ;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS sheets_exported        BOOLEAN NOT NULL DEFAULT FALSE;

-- Hipotetyczne wyniki dla setupów które nie weszły (np. brak slotu na Bitget)
ALTER TABLE setups ADD COLUMN IF NOT EXISTS hypo_result            TEXT;
ALTER TABLE setups ADD COLUMN IF NOT EXISTS hypo_pnl_usd           NUMERIC(10,4);

-- Status setupu: pending | open | after_tp1 | closed
ALTER TABLE setups ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
CREATE INDEX IF NOT EXISTS idx_setups_status ON setups (status);

-- Backfill statusu dla istniejących rekordów
UPDATE setups SET status = 'closed'    WHERE resolved = TRUE AND status = 'pending';
UPDATE setups SET status = 'after_tp1' WHERE resolved = FALSE AND exchange_tp1_done = TRUE AND status = 'pending';
UPDATE setups SET status = 'open'      WHERE resolved = FALSE AND exchange_position_opened = TRUE AND exchange_tp1_done = FALSE AND status = 'pending';

-- Kwota zlecenia (USDT) użyta przy otwieraniu pozycji — do poprawnego liczenia %
ALTER TABLE setups ADD COLUMN IF NOT EXISTS trade_usdt NUMERIC(10,2);

-- Wariant parametrów algo (kalibracja) — baseline + eksperymenty równoległe
ALTER TABLE setups ADD COLUMN IF NOT EXISTS variant TEXT NOT NULL DEFAULT 'baseline';
CREATE INDEX IF NOT EXISTS idx_setups_variant ON setups (variant);

-- Backfill trade_usdt: odtwórz z exchange_qty_full * avg_entry / leverage
-- (odwrotność wzoru: qty = FLOOR(trade_usdt * leverage / entry / 0.1) * 0.1)
-- Bezpieczny cast: pomija rekordy z nienumerycznym qty (np. 'PENDING', zepsute dane
-- migracyjne) żeby startup nie failował gdy w DB jest taki rekord.
UPDATE setups SET trade_usdt = ROUND(
    (CASE WHEN exchange_qty_full ~ '^[0-9]+(\.[0-9]+)?$'
          THEN exchange_qty_full::numeric ELSE NULL END)
    * COALESCE(avg_entry, (entries->>0)::numeric)
    / 20, 2)
WHERE trade_usdt IS NULL
  AND exchange_qty_full IS NOT NULL
  AND exchange_qty_full != ''
  AND exchange_qty_full ~ '^[0-9]+(\.[0-9]+)?$'
  AND COALESCE(avg_entry, (entries->>0)::numeric) IS NOT NULL;

-- Przelicz pnl_pct dla setupów z odtworzonym trade_usdt (naprawia błędne %)
UPDATE setups SET pnl_pct = ROUND(pnl_usd / NULLIF(trade_usdt, 0) * 100, 2)
WHERE pnl_usd IS NOT NULL
  AND trade_usdt IS NOT NULL
  AND resolved = TRUE;
