# AlertSol — SOL/USDT Perpetual Futures Trading Bot

## Overview

Automated crypto trading system for SOL/USDT perpetual futures on Bitget exchange.
Detects trading setups algorithmically, validates them with GPT-4o, places and manages
orders, tracks P&L, and exposes a web dashboard.

Deployed on Railway PaaS. Single-asset (SOL/USDT), single-exchange (Bitget).

---

## Architecture

```
┌──────────────┐    ┌──────────────┐    ┌───────────────┐
│  sol_alert   │───▶│    db.py      │◀───│ main_runner   │
│  (detection) │    │ (PostgreSQL)  │    │ (FastAPI+Sched│
└──────┬───────┘    └──────────────┘    └───────┬───────┘
       │                                        │
       ▼                                        ▼
┌──────────────┐                        ┌───────────────┐
│exchange_trader│                       │  Web Dashboard │
│ (Bitget API) │                        │ (React SPA)   │
└──────────────┘                        └───────────────┘
       │
       ▼
   Telegram
  (notifications)
```

### Files

| File | Lines | Role |
|------|-------|------|
| `sol_alert.py` | ~2900 | Signal detection, GPT validation, Telegram alerts, Google Sheets export |
| `exchange_trader.py` | ~1450 | Bitget REST API client, order placement/management, position sync |
| `main_runner.py` | ~3800 | FastAPI app, APScheduler jobs, web dashboard, REST API, backtest runner |
| `db.py` | ~2000 | PostgreSQL data layer, analytics queries, migrations |
| `backtest_variants.py` | ~500 | Historical backtesting framework for parameter variants |
| `schema.sql` | ~180 | Database schema + idempotent migrations |

---

## Signal Detection (`sol_alert.py`)

### Market Regime Classification

Each 5-minute cycle classifies the market into one of three regimes using M15 + H1 candles:
- **IMPULSE** — strong directional move (large candle body, high volume)
- **TREND** — sustained directional movement (based on swing analysis and price changes)
- **RANGE** — sideways consolidation (low volatility, no clear direction)

Inputs: price changes over multiple periods, volume ratios, swing high/low analysis, ATR.

### Setup Types (Algo2)

| Type | Regime | Description |
|------|--------|-------------|
| `trend_pullback` | TREND | Pullback to support/resistance in a trend; variants: `baseline`, `shallow` |
| `impulse_continuation` | IMPULSE | Continuation after an impulse move |
| `impulse_aggressive` | IMPULSE | More aggressive entry; variants: `h1_atr`, `trend_boost` |
| `range_support_long` | RANGE | Long at range support |
| `range_resistance_short` | RANGE | Short at range resistance |
| `breakout_retest` | any | Breakout followed by retest of the broken level |

### Validation Pipeline

1. **Algo2 detection** — algorithmic scan produces candidate setups with entries, TPs, SL, score
2. **GPT-4o validation** — `call_gpt3_validator()` sends market context + setup to OpenAI; GPT assigns score, can reject or modify levels
3. **Score threshold** — `MIN_SCORE = 9` required for acceptance
4. **Dedup** — `save_pending()` skips/replaces a new candidate if an unresolved setup with the same `model+direction+variant+type` already exists within ~$0.50 (`sol_alert.py:2222`). Note: `COOLDOWN_HOURS`/`was_alerted()`/`save_alerted()` in `db.py` exist but are **dead code** — never called anywhere. Once a setup resolves (SL/TP/timeout), a new one can fire immediately; there is no "wait N hours after a loss" cooldown today.
5. **Impulse cooldown** — prevents false reversals after strong directional moves

### Setup Lifecycle

```
save_pending() → [PENDING] → check_pending() → entry_hit → [OPEN]
                                                          → tp1_hit → [AFTER_TP1]
                                                          → sl_hit / timeout → [CLOSED]
```

- **Entry timeout**: `ENTRY_TIMEOUT_H = 4` — cancel if entry not hit
- **Trade timeout**: `TRADE_TIMEOUT_H = 24` — force close after 24h
- **Open trade timeout**: `OPEN_TRADE_TIMEOUT_H = 16` — close open positions after 16h
- **Stale setup cancellation**: invalidates pending setups when price moves away

### Shadow Mode

Grok (xAI) can run in shadow mode — detecting setups that are tracked but not traded.
Controlled by `ENABLE_GROK_SHADOW` env var.

### Gemini2

Independent detector using Google Gemini. Currently disabled (`ENABLE_GEMINI2 = False`).

### Experiment: `regime_alt` (RANGE misclassification rescue) — started 2026-07-04

**Problem found:** `detect_market_regime()` classifies TREND vs RANGE using `change_24h`/
`change_48h` measured against a single reference candle from 24h/48h ago. If that reference
candle happens to land near a local peak/trough (e.g. right after a pullback in an otherwise
clear multi-day uptrend), the computed % change looks artificially flat, `trend_score < 3`,
and the regime falls through to `RANGE` even though a real trend exists. Confirmed by eye on
a live SOL chart showing an obvious multi-day uptrend that the bot had classified as RANGE.

This matters because `RANGE` classification gates which setups can even be generated
(`regime['direction']` becomes `"none"`, so `trend_pullback` never fires), and it's the
suspected root cause of why `trend_pullback_short` and `range_resistance_short` — the two
counter-trend (short) variants — have shown much worse win rates than their long-side
mirrors (`trend_pullback_long`, `range_support_long`) despite identical code/filters on both
sides: SOL trended up for most of the analyzed period (~April–July 2026), so this bug
would silently suppress correct-direction long-side trend detection less often than it lets
bad counter-trend shorts through in the RANGE branch's own (too-short-lookback) filters.

**What was shipped (all log-only, zero effect on live trading — see PRs #287, #288):**
1. `regime_alt` field: when regime falls to `RANGE`, additionally checks if `change_4h`/
   `change_8h`/`change_12h` unanimously agree on a trend direction (same 3-vote consensus
   logic as the existing "Fix 3" override, just applied at the RANGE boundary instead of
   only flipping an already-detected trend's direction). Stored in `market_context.regime_alt`.
2. Shadow `trend_pullback` setup: when `regime_alt` disagrees with `RANGE`, generates the
   same `baseline` fib-pullback geometry that would have fired had the regime actually been
   `TREND_{alt_dir}` — tagged `variant="regime_alt"`, always `not_tradeable=True`. This is
   what actually makes the idea testable (the plain `regime_alt` field alone is inert, since
   nothing downstream reads it — `trend_pullback` detection still gates on the real
   `regime['direction']`).

**Real `baseline` (and everything else live-tradeable) is completely untouched.** This is
purely collecting comparison data.

**To check back (after ~1 month of data, i.e. ~early August 2026):**
- Query `setups` where `type LIKE 'trend_pullback_%'` and `variant = 'regime_alt'`.
- Compare win rate / expectancy against real `baseline` for the same period (see
  `/api/pullback-analysis/csv` and `/api/setup-prices/csv` endpoints in `main_runner.py`,
  added this session for exactly this kind of analysis).
- If `regime_alt` setups show meaningfully better win rate than what `range_resistance_short`/
  `trend_pullback_short` actually achieved during the same misclassified windows, consider
  wiring `regime_alt` into the real classification (i.e. let it rescue `RANGE` → `TREND`)
  — but only after this historical validation, not before.

### Experiment: order book depth features — started 2026-07-07

**Motivation:** two open questions — (1) can regime classification be improved (see
`regime_alt` above), (2) can Bitget order book depth help place better exit levels
(TP/SL) than the current fib/ATR-based geometry. Starting with data collection only,
same log-only pattern as `regime_alt` — no live trading impact.

**What was shipped:** `fetch_order_book()` (`sol_alert.py`) pulls a depth snapshot
(`GET /api/v2/mix/market/merge-depth`, public endpoint, top 50 levels/side) once per
Algo2 cycle, from `_algo2_run()` only — never from the backtest/replay path, so
historical replays are unaffected and `algo_detect_setups()`'s `orderbook` param
defaults to `None`. `compute_orderbook_features()` derives, per cycle:
- `ob_imbalance` — bid volume share of total bid+ask volume (top 50 levels)
- `ob_spread_pct` — best bid/ask spread, % of current price
- `ob_wall_bid_dist_pct` / `ob_wall_ask_dist_pct` — distance to the nearest bid/ask
  level with volume >= 3x the median level size ("wall"), % of current price

These land in every setup's `market_context` JSONB (merged into the existing `_ml_ctx`
dict) — no schema changes, no new gating, no effect on entries/TP/SL/scoring.

**Hypothesis (falsifiable, not just "collect and see"):** distance to an order-book
wall recorded at signal time (`ob_wall_ask_dist_pct` for longs / `ob_wall_bid_dist_pct`
for shorts) predicts how far price actually moves in our favor (MFE — max favorable
excursion) *better than* the current fib/ATR-based TP2 distance. Falsified if MFE isn't
meaningfully closer to the wall level than to TP2, or if walls rarely appear within a
relevant range.

**Analysis tooling (`orderbook_analysis.py`, `db.get_orderbook_exit_analysis()`):**
for every resolved setup with order-book features in `market_context`, reconstructs MFE
from Bitget M15 candles in the `[entry_hit_at, exit_time]` window and compares
`mean_abs(wall_dist_pct − mfe_pct)` against `mean_abs(tp2_dist_pct − mfe_pct)` — writes
a CSV plus a summary. Run with `python orderbook_analysis.py [--date-from YYYY-MM-DD]`.

**To check back (after a few weeks / a few dozen resolved setups):**
- Run `orderbook_analysis.py` and compare the two mean-abs-diff numbers.
- Only wire wall distance into real TP2 geometry if it's a clearly better (smaller)
  predictor of MFE than the current TP2 across enough setups to trust the signal —
  otherwise the hypothesis is rejected and nothing changes in live trading.

---

## Exchange Trading (`exchange_trader.py`)

### Bitget Integration

- **Auth**: HMAC-SHA256 signed REST API requests
- **Mode**: Hedge mode, cross margin, 20x leverage
- **Product**: `SUSDT` (USDT-M perpetual futures)

### Order Flow

1. **Plan order** — trigger order at entry price (W1 level)
   - For aggressive setups: immediate market order instead
2. **Position opened** → place TPSL orders:
   - **tp1_tp2 strategy**: split position — half at TP1, half at TP2
   - **tp1_only strategy**: full quantity exits at TP1
3. **TP1 hit** → move SL to breakeven (avg_entry), let TP2 ride
4. **TP2 hit or SL hit** → position closed, setup resolved

### Position Monitoring

`sync()` runs every 15 seconds with a threading lock:
- **Phase 1** (before TP1): monitors for TP1 fill, checks SL
- **Phase 2** (after TP1): monitors for TP2/SL fill, resolves setup
- Detects externally closed positions
- SL modification with atomic fallback (place new → cancel old)

### Trade Sizing

- Uses 100% of available account equity per trade
- `MAX_POSITIONS` limits concurrent positions per direction (hedge mode)
- Weekly profit transfer: 50% of weekly PnL moved to spot account (Fridays 8:00 Warsaw)

---

## Web Dashboard & API (`main_runner.py`)

### Authentication

Google OAuth2, restricted to single email (`paulina@lerta.pl`).
Session cookies with `itsdangerous` signing.

### Scheduler Jobs

| Job | Interval | Function |
|-----|----------|----------|
| `exchange_monitor` | 15s | `exchange_trader.sync()` — order/position monitoring |
| `sol_alert` | 5min | `run_sol_alert()` — market scan + setup detection |
| `breakout_scan` | 3min | `run_breakout_scan()` — breakout retest detection |
| `grok_shadow` | 5min | `run_grok_shadow()` — shadow mode detection (if enabled) |
| `weekly_transfer` | Fri 8:00 | `exchange_trader.weekly_transfer()` — profit to spot |

### REST API Endpoints

**Public (after auth):**
- `GET /api/market-status` — current price, regime, indicators
- `GET /api/budget-info` — account balance, equity, positions
- `GET /api/stats` — trading performance statistics
- `GET /api/resolved` — closed/resolved setups list
- `GET /api/algo2/*` — variant analysis, daily stats, heatmap, R:R analysis

**Admin actions:**
- `POST /api/update-tps` — modify TP/SL levels on active setup
- `POST /api/cancel-setup` — cancel pending setup
- `POST /admin/resolve-setup` — force-resolve a setup
- `POST /admin/restore-after-tp1` — restore setup to post-TP1 state
- `POST /admin/reset-entry` — reset entry tracking
- `POST /admin/force-position-open` — mark position as opened
- `POST /admin/fix-position-qty` — correct position quantity
- `POST /admin/reopen-setup` — reopen a resolved setup
- `GET /admin/diagnose-positions` — compare DB state vs exchange positions

**Settings:**
- `GET/POST /api/settings` — app-wide settings (JSONB, single-row table)

### Dashboard

Two versions:
1. **Legacy HTML** — inline HTML/JS/CSS in Python string (at `/dashboard-old`)
2. **React SPA** — served from `static/index.html` (at `/`)

---

## Database (`db.py` + `schema.sql`)

### Tables

| Table | Purpose |
|-------|---------|
| `setups` | Main table — ~50 columns covering signal, levels, entry/exit tracking, exchange state |
| `alerts_log` | Cooldown tracking — prevents duplicate alerts at same level |
| `app_settings` | Single-row JSONB for application settings |
| `exchange_events` | Audit log for SL modifications, fallbacks, errors |

### Key Patterns

- **Connection pool**: `psycopg2.pool.ThreadedConnectionPool`
- **Advisory locks**: `pg_advisory_xact_lock()` prevents race conditions on concurrent INSERTs
- **Baseline snapshots**: thread-local snapshots detect what changed between `save_pending_list()` calls
- **Idempotent migrations**: `ALTER TABLE ADD COLUMN IF NOT EXISTS` in schema.sql

### Setup Status Flow

```
pending → open → after_tp1 → closed
                           → closed (SL hit)
pending → closed (timeout / cancelled)
```

---

## Backtesting (`backtest_variants.py`)

- Fetches historical M15 + H1 candles from Bitget API
- Replays `algo_detect_setups()` on sliding windows
- Simulates trades: entry timeout (16 candles), hold timeout (64 candles)
- Per-variant blocking models live behavior (one active setup per variant)
- Outputs CSV + summary table

---

## Configuration

### Key Parameters

| Parameter | Value | Location |
|-----------|-------|----------|
| `MIN_SCORE` | 9 | sol_alert.py — minimum GPT score to accept setup |
| `COOLDOWN_HOURS` | 4 | sol_alert.py — time between alerts at same level |
| `ENTRY_TIMEOUT_H` | 4 | sol_alert.py — cancel pending if entry not hit |
| `TRADE_TIMEOUT_H` | 24 | sol_alert.py — force close after 24h |
| `OPEN_TRADE_TIMEOUT_H` | 16 | sol_alert.py — close open positions after 16h |
| `MIN_SL_DISTANCE` | 0.30 | sol_alert.py — minimum SL distance in USD |
| `LEVERAGE` | 20 | sol_alert.py, exchange_trader.py |
| `MAX_POSITIONS` | env var | exchange_trader.py — max concurrent positions per direction |
| `TRADE_USDT` | env var (default 100) | sol_alert.py — trade size in USDT |

### Environment Variables

- `DATABASE_URL` — PostgreSQL connection string
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` — Telegram bot notifications
- `OPENAI_API_KEY` — GPT-4o validator
- `XAI_API_KEY` — Grok shadow mode
- `GEMINI_API_KEY` — Gemini2 detector (disabled)
- `BITGET_API_KEY`, `BITGET_SECRET`, `BITGET_PASSPHRASE` — exchange access
- `BITGET_TRADE_USDT` — trade size override
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` — dashboard auth
- `SESSION_SECRET` — cookie signing key
- `ALLOWED_EMAIL` — authorized dashboard user
- `ENABLE_GROK_SHADOW` — enable Grok shadow trading
- `ENABLE_GEMINI2` — enable Gemini2 detector

### Deployment

- **Platform**: Railway PaaS
- **Entry point**: `python main_runner.py` (Procfile)
- **Port**: `$PORT` env var (Railway provides this)
