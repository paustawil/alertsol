#!/usr/bin/env python3
"""
main_runner.py — główny proces Railway dla AlertSol

Uruchamia 3 zadania w tle:
  1. exchange_monitor  — co 15 sekund (natychmiastowa reakcja na order fills)
  2. sol_alert_job     — co 15 minut (wykrywanie setupów)
  3. sheets_export_job — co 5 minut (eksport zamkniętych setupów do Google Sheets)

+ FastAPI web dashboard dostępny pod URL przydzielonym przez Railway.
"""

import logging
import math
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import HTMLResponse
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

import db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("runner")

scheduler = BackgroundScheduler(timezone="UTC")


# ── Zadania ────────────────────────────────────────────────────────────────────

def run_exchange_sync():
    try:
        import exchange_trader
        exchange_trader.sync()
    except Exception:
        log.exception("exchange_trader.sync() BŁĄD")


def run_sol_alert():
    log.info("=== sol_alert.main() START ===")
    try:
        import sol_alert
        sol_alert.main()
    except Exception:
        log.exception("sol_alert.main() BŁĄD")
    log.info("=== sol_alert.main() END ===")


def run_breakout_scan():
    """Szybki skan breakoutowy — co 3 min, bez Groka chyba że wykryje breakout."""
    try:
        import sol_alert
        sol_alert.breakout_scan()
    except Exception:
        log.exception("breakout_scan() BŁĄD")


def run_sheets_export():
    """Eksportuje nowo zamknięte setupy do Google Sheets."""
    try:
        import sol_alert
        unexported = db.get_unexported_resolved()
        if not unexported:
            return
        log.info(f"[sheets-export] Eksportuję {len(unexported)} setupów...")
        for s in unexported:
            entry_ts = s.get("entry_hit_at")
            exit_dt  = s.get("exit_time")
            exit_ts  = int(exit_dt.timestamp()) if exit_dt else None
            result   = s.get("result", "")
            avg_entry = float(s["avg_entry"]) if s.get("avg_entry") else None
            avg_exit  = float(s["avg_exit"])  if s.get("avg_exit")  else None
            move      = float(s["pnl_usd"])   if s.get("pnl_usd")   else 0.0

            try:
                if s.get("shadow") and s.get("model") == "Grok":
                    ok = sol_alert.log_to_grok_shadow(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move)
                elif s.get("shadow"):
                    ok = sol_alert.log_to_anulowane_grok(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move)
                else:
                    ok = sol_alert.log_to_wyniki(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move)
                if ok:
                    db.mark_sheets_exported(s["setup_id"])
                    log.info(f"[sheets-export] Setup #{s['setup_id']} wyeksportowany.")
                else:
                    log.warning(f"[sheets-export] Setup #{s['setup_id']} — eksport nieudany, spróbuję ponownie.")
            except Exception:
                log.exception(f"[sheets-export] Błąd eksportu setupu #{s['setup_id']}")
    except Exception:
        log.exception("[sheets-export] Błąd ogólny")


def run_profit_calculator_export():
    """Odświeża arkusz kalkulatora zysku/straty w Google Sheets."""
    try:
        import sol_alert
        sol_alert.export_profit_calculator_to_sheets()
    except Exception:
        log.exception("[kalkulator] Błąd eksportu kalkulatora")


def run_grok_shadow():
    """Grok shadow — detekcja co 60 min (lub co 5 min podczas IMPULSE), wirtualny tracking."""
    try:
        import sol_alert
        sol_alert.grok_shadow_main()
    except Exception:
        log.exception("[grok-shadow] grok_shadow_main() BŁĄD")


# ── FastAPI dashboard ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db.init_schema()
    log.info("Schema DB zweryfikowana.")

    # Exchange monitor — co 15 sekund
    scheduler.add_job(
        run_exchange_sync,
        "interval",
        seconds=15,
        id="exchange_monitor",
        max_instances=1,
        coalesce=True,
    )

    # Sol alert — co 5 minut (wewnętrzny throttle: 15 min RANGE/TREND, 5 min IMPULSE)
    scheduler.add_job(
        run_sol_alert,
        "interval",
        minutes=5,
        id="sol_alert",
        max_instances=1,
        coalesce=True,
    )

    # Breakout scanner — co 3 minuty (szybki, bez Groka chyba że wykryje breakout)
    scheduler.add_job(
        run_breakout_scan,
        "interval",
        minutes=3,
        id="breakout_scan",
        max_instances=1,
        coalesce=True,
    )

    # Sheets export — co 5 minut
    scheduler.add_job(
        run_sheets_export,
        "interval",
        minutes=5,
        id="sheets_export",
        max_instances=1,
        coalesce=True,
    )

    # Kalkulator zysku/straty — co godzinę (nadpisuje arkusz aktualnymi danymi)
    scheduler.add_job(
        run_profit_calculator_export,
        "interval",
        hours=1,
        id="profit_calculator",
        max_instances=1,
        coalesce=True,
    )

    # Grok shadow — co 5 min (wewnętrznie throttled: detekcja co 60 min lub co 5 min podczas IMPULSE)
    scheduler.add_job(
        run_grok_shadow,
        "interval",
        minutes=5,
        id="grok_shadow",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info("Scheduler uruchomiony. exchange: co 15s | sol_alert: co 5min (throttle Algo2: 15min RANGE/TREND, 5min IMPULSE) | grok_shadow: co 5min (throttle: 30min RANGE/TREND, 5min IMPULSE) | sheets: co 5min | kalkulator: co 1h")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    log.info("Scheduler zatrzymany.")


app = FastAPI(title="AlertSol Dashboard", lifespan=lifespan)

# ── Prosta autoryzacja kluczem dla endpointów analitycznych ──────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def _require_api_key(key: str | None = Security(_api_key_header)):
    """Dependency: sprawdza X-API-Key względem env var CLAUDE_API_KEY.
    Jeśli CLAUDE_API_KEY nie jest ustawione — endpoint jest otwarty (dev mode)."""
    expected = os.getenv("CLAUDE_API_KEY", "")
    if expected and key != expected:
        raise HTTPException(status_code=403, detail="Invalid API key")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Prosta strona HTML z aktywnymi setupami i statystykami."""
    try:
        active = db.get_active_setups()
        stats  = db.get_summary_stats()
    except Exception as e:
        return HTMLResponse(f"<pre>Błąd DB: {e}</pre>", status_code=500)

    win_rate = f"{stats.get('win_rate_pct', 0) or 0:.1f}%" if stats.get("win_rate_pct") is not None else "—"
    total_pnl = f"{stats.get('total_pnl_usd') or 0:+.2f}" if stats.get("total_pnl_usd") is not None else "—"

    active_rows = ""
    for s in active:
        entries = s.get("entries") or []
        tps     = s.get("tps") or []
        w1   = f"${entries[0]:.2f}" if entries else "—"
        tp1  = f"${tps[0]:.2f}"    if len(tps) > 0 else "—"
        tp2  = f"${tps[1]:.2f}"    if len(tps) > 1 else "—"
        sl   = f"${s['sl']:.2f}"           if s.get("sl")          else "—"
        sl2  = f"${s['sl_after_tp1']:.2f}" if s.get("sl_after_tp1") else "—"
        if s.get("entry_hit_at") and s.get("tp1_hit_at"):
            pnl_tp1 = s.get("pnl_usd")
            pnl_str = f" ({pnl_tp1:+.2f}$)" if pnl_tp1 is not None else ""
            status = f"✅ po TP1{pnl_str}"
        elif s.get("entry_hit_at"):
            status = "📈 w pozycji"
        else:
            status = "⏳ czeka"
        sid      = s["setup_id"]
        plan_oid = s.get("exchange_plan_oid") or ""
        tp1_oid  = s.get("exchange_tp1_oid")  or ""
        tp2_oid  = s.get("exchange_tp2_oid")  or ""
        sl_oid   = s.get("exchange_sl_oid")   or ""
        qty_full = s.get("exchange_qty_full")  or ""
        pos_open = "true" if s.get("exchange_position_opened") else "false"
        tp1_done = "true" if s.get("exchange_tp1_done") else "false"
        tp1_raw  = f"{tps[0]:.2f}" if len(tps) > 0 else ""
        tp2_raw  = f"{tps[1]:.2f}" if len(tps) > 1 else ""
        sl_raw   = f"{s['sl_after_tp1']:.2f}" if s.get("exchange_tp1_done") and s.get("sl_after_tp1") else (f"{s['sl']:.2f}" if s.get("sl") else "")
        # RR calculation
        e1_num = entries[0] if entries else None
        t1_num = tps[0] if len(tps) > 0 else None
        t2_num = tps[1] if len(tps) > 1 else None
        sl_num = s.get("sl")
        rr1_str = "—"
        rr2_str = "—"
        if e1_num and t1_num and sl_num and abs(e1_num - sl_num) > 0.0001:
            if s["direction"].lower() == "long":
                risk = e1_num - sl_num
                if risk > 0:
                    rr1_str = f"{(t1_num - e1_num) / risk:.2f}"
                    if t2_num:
                        rr2_str = f"{(t2_num - e1_num) / risk:.2f}"
            else:
                risk = sl_num - e1_num
                if risk > 0:
                    rr1_str = f"{(e1_num - t1_num) / risk:.2f}"
                    if t2_num:
                        rr2_str = f"{(e1_num - t2_num) / risk:.2f}"
        active_rows += (
            f'<tr data-sid="{sid}" data-plan-oid="{plan_oid}" '
            f'data-tp1-oid="{tp1_oid}" data-tp2-oid="{tp2_oid}" data-sl-oid="{sl_oid}" '
            f'data-qty-full="{qty_full}" data-pos-open="{pos_open}" data-tp1-done="{tp1_done}">'
            f"<td>#{sid}</td><td>{s['model']}</td>"
            f"<td>{s['direction'].upper()}</td>"
            f"<td style='font-size:0.75em;color:#aaa;white-space:nowrap'>{s.get('type','')}</td>"
            f"<td>{status}</td>"
            f"<td>{w1}</td>"
            f'<td>'
            f'<span class="av-view">{tp1}</span>'
            f'<input class="av-edit tp1-input" type="number" step="0.01" value="{tp1_raw}" style="width:72px;background:#2a2a2a;color:#e0e0e0;border:1px solid #555;font-family:monospace;padding:2px 4px">'
            f'</td>'
            f'<td>'
            f'<span class="av-view">{tp2}</span>'
            f'<input class="av-edit tp2-input" type="number" step="0.01" value="{tp2_raw}" style="width:72px;background:#2a2a2a;color:#e0e0e0;border:1px solid #555;font-family:monospace;padding:2px 4px">'
            f'</td>'
            f'<td>'
            f'<span class="av-view">{sl}</span>'
            f'<input class="av-edit sl-input" type="number" step="0.01" value="{sl_raw}" style="width:72px;background:#2a2a2a;color:#e0e0e0;border:1px solid #884444;font-family:monospace;padding:2px 4px">'
            f'</td>'
            f"<td>{sl2}</td>"
            f'<td style="color:#80deea;text-align:center">{rr1_str}</td>'
            f'<td style="color:#80deea;text-align:center">{rr2_str}</td>'
            f'<td style="white-space:nowrap">'
            f'<button class="av-view btn-edit" onclick="editActiveTp(this)">Zmień TP</button>'
            f'<button class="av-edit btn-action" onclick="saveActiveTp(this)">Zapisz</button>'
            f'<button class="av-edit btn-action" onclick="cancelActiveTpEdit(this)">Zamknij</button>'
            f' <button class="btn-action" style="color:#ffaaaa;border-color:#884444" onclick="cancelActiveSetup(this)">Anuluj setup</button>'
            f'</td>'
            f'</tr>\n'
        )

    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))

    by_model_rows = ""
    for m in (stats.get("by_model") or []):
        all_s   = m.get("all_setups") or 0
        entered = m.get("entered") or 0
        wins    = m.get("wins") or 0
        entry_pct = f"{entered / all_s * 100:.0f}%" if all_s else "—"
        win_pct   = f"{wins / entered * 100:.0f}%" if entered else "—"
        pnl_usd_m = float(m["pnl_usd"]) if m.get("pnl_usd") is not None else None
        pnl_m     = f"{pnl_usd_m:+.2f}" if pnl_usd_m is not None else "—"
        _pnl_pct_v = float(m["pnl_pct"]) if m.get("pnl_pct") is not None else None
        pnl_pct_m = f"{_pnl_pct_v:+.1f}%" if _pnl_pct_v is not None else "—"
        tp1_usd_m = float(m["tp1_only_pnl_usd"]) if m.get("tp1_only_pnl_usd") is not None else None
        tp1_m     = f"{tp1_usd_m:+.2f}" if tp1_usd_m is not None else "—"
        _tp1_pct_v = float(m["tp1_only_pnl_pct"]) if m.get("tp1_only_pnl_pct") is not None else None
        tp1_pct_m = f"{_tp1_pct_v:+.1f}%" if _tp1_pct_v is not None else "—"
        by_model_rows += (
            f"<tr><td>{m['model']}</td><td>{entry_pct}</td><td>{win_pct}</td>"
            f"<td>{pnl_m}</td><td>{pnl_pct_m}</td>"
            f"<td>{tp1_m}</td><td>{tp1_pct_m}</td></tr>\n"
        )

    model_names = sorted({m["model"] for m in (stats.get("by_model") or []) if m.get("model")})
    model_checkboxes = " ".join(
        f'<label style="font-size:0.85em"><input type="checkbox" class="model-filter" value="{m}"> {m}</label>'
        for m in model_names
    )

    now = datetime.now(ZoneInfo("Europe/Warsaw")).strftime("%Y-%m-%d %H:%M %Z")
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>AlertSol Dashboard</title>
<style>
  body {{ font-family: monospace; max-width: 1100px; margin: 20px auto; background: #1a1a1a; color: #e0e0e0; }}
  h2 {{ color: #90caf9; }} h3 {{ color: #80deea; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
  th, td {{ border: 1px solid #444; padding: 6px 10px; text-align: left; }}
  th {{ background: #333; }}
  .stat {{ display: inline-block; margin: 0 20px 10px 0; font-size: 1.2em; }}
  .emode {{ display: none; }}
  tr.editing .emode {{ display: inline; }}
  tr.editing .vmode {{ display: none; }}
  .result-select, .avg-exit-input, .avg-entry-input {{ background: #2a2a2a; color: #e0e0e0; border: 1px solid #555; padding: 2px 4px; font-family: monospace; }}
  .avg-exit-input, .avg-entry-input {{ width: 72px; }}
  .btn-edit {{ background: #2a2a2a; color: #aaa; border: 1px solid #555; padding: 2px 8px; cursor: pointer; font-family: monospace; font-size: 0.85em; }}
  .btn-edit:hover {{ background: #3a3a3a; color: #e0e0e0; }}
  .btn-action {{ background: #333; color: #e0e0e0; border: 1px solid #555; padding: 2px 8px; cursor: pointer; font-family: monospace; font-size: 0.85em; margin-right: 2px; }}
  .btn-action:hover {{ background: #444; }}
  #active-table th[title] {{ font-size: 0.9em; min-width: 42px; text-align: center; }}
  .av-edit {{ display: none; }}
  tr.editing-tp .av-edit {{ display: inline; }}
  tr.editing-tp .av-view {{ display: none; }}

  /* ── Indicators panel ──────────────────────────────────────────────── */
  .indicators-panel {{ background: #222; border: 1px solid #444; border-radius: 8px; padding: 16px 20px; margin-bottom: 18px; }}
  .indicators-panel h3 {{ margin: 0 0 10px 0; font-size: 1em; color: #80deea; }}
  .ind-row {{ display: flex; flex-wrap: wrap; gap: 16px 28px; align-items: center; }}
  .ind-card {{ background: #2a2a2a; border: 1px solid #444; border-radius: 6px; padding: 8px 14px; min-width: 140px; }}
  .ind-card .label {{ font-size: 0.75em; color: #888; margin-bottom: 2px; }}
  .ind-card .value {{ font-size: 1.15em; font-weight: bold; color: #e0e0e0; }}
  .ind-card .sub {{ font-size: 0.8em; color: #aaa; }}
  .settings-input {{ background: #1a1a1a; color: #e0e0e0; border: 1px solid #555; padding: 3px 6px; font-family: monospace; font-size: 0.95em; width: 70px; border-radius: 3px; }}
  .period-btn {{ background: #333; color: #aaa; border: 1px solid #555; padding: 3px 10px; cursor: pointer; font-family: monospace; font-size: 0.85em; border-radius: 3px; }}
  .period-btn.active {{ background: #1a5276; color: #e0e0e0; border-color: #5dade2; }}
  .period-btn:hover {{ background: #444; }}
  .settings-save {{ background: #1a5276; color: #e0e0e0; border: 1px solid #5dade2; padding: 3px 12px; cursor: pointer; font-family: monospace; font-size: 0.85em; border-radius: 3px; }}
  .settings-save:hover {{ background: #2471a3; }}
  /* ── Settings gear popover ──────────────────────────────────────────── */
  .gear-btn {{ background: none; border: none; cursor: pointer; font-size: 1em; padding: 0 0 0 10px; color: #80deea; vertical-align: middle; line-height: 1; }}
  .gear-btn:hover {{ color: #e0e0e0; }}
  #settings-popover {{ display:none; position:fixed; top:60px; left:20px; z-index:200; background:#1e1e1e; border:1px solid #5dade2; border-radius:8px; padding:16px 20px; box-shadow:0 4px 20px rgba(0,0,0,0.7); min-width:420px; }}
  #settings-popover h4 {{ margin:0 0 12px 0; color:#80deea; font-size:0.95em; }}
</style></head><body>
<div style="position:relative">
<h2 style="display:inline">🤖 AlertSol Dashboard</h2>
<button class="gear-btn" onclick="toggleSettings()" title="Ustawienia">⚙</button>
<div id="settings-popover" style="display:none">
  <h4>⚙ Ustawienia</h4>
  <div class="ind-row">
    <div class="ind-card">
      <div class="label">Kwota zlecenia (USDT)</div>
      <div class="value"><input type="number" step="1" min="1" id="set-trade-usdt" class="settings-input" value="" placeholder="—"></div>
    </div>
    <div class="ind-card">
      <div class="label">Częstotliwość alertów (min)</div>
      <div class="value"><input type="number" step="1" min="1" max="60" id="set-alert-interval" class="settings-input" value="" placeholder="—"></div>
    </div>
    <div class="ind-card">
      <div class="label">Maks. otwartych zleceń</div>
      <div class="value"><input type="number" step="1" min="1" max="20" id="set-max-positions" class="settings-input" value="" placeholder="—"></div>
    </div>
    <div style="display:flex;align-items:flex-end;margin-top:8px">
      <button class="settings-save" id="settings-save-btn" onclick="saveSettings()">Zapisz ustawienia</button>
      <span id="settings-status" style="margin-left:8px;font-size:0.8em;color:#888"></span>
    </div>
  </div>
</div>
</div>
<p style="color:#888">Ostatnia aktualizacja: {now}</p>

<!-- ── Market status bar ───────────────────────────────────────────────── -->
<div style="margin-bottom:14px">
  <div id="market-status-bar" style="display:flex;gap:20px;background:#222;border:1px solid #444;border-radius:6px 6px 0 0;padding:8px 16px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #333">
    <span style="color:#aaa;font-size:0.85em">SOL/USDT:</span>
    <span id="ms-price" style="font-weight:bold;font-size:1.1em;color:#e0e0e0">—</span>
    <span style="color:#aaa;font-size:0.85em">Regime:</span>
    <span id="ms-regime" style="font-weight:bold;color:#e0e0e0">—</span>
    <span id="ms-regime-detail" style="font-size:0.8em;color:#888"></span>
    <span id="ms-loading" style="font-size:0.75em;color:#555;margin-left:auto"></span>
  </div>
  <div id="ms-scans" style="display:flex;gap:0;background:#1e1e1e;border:1px solid #444;border-top:none;border-radius:0 0 6px 6px;flex-wrap:wrap">
    <div id="ms-scan-Algo2" style="flex:1;min-width:300px;padding:7px 16px;word-wrap:break-word;overflow-wrap:break-word">
      <span style="color:#aaa;font-size:0.8em">Algo2: ładowanie...</span>
    </div>
  </div>
</div>

<!-- ── Wykres SOL/USDT ─────────────────────────────────────────────────── -->
<div style="margin-bottom:18px;background:#222;border:1px solid #444;border-radius:8px;padding:12px 16px">
  <h3 style="margin:0 0 10px;font-size:1em;color:#80deea">📈 SOL/USDT — wykres live</h3>
  <div class="tradingview-widget-container" style="height:420px">
    <div id="tradingview_sol" style="height:100%"></div>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <script type="text/javascript">
    new TradingView.widget({{
      "width": "100%",
      "height": 420,
      "symbol": "BITGET:SOLUSDT",
      "interval": "15",
      "timezone": "Europe/Warsaw",
      "theme": "dark",
      "style": "1",
      "locale": "pl",
      "hide_side_toolbar": false,
      "container_id": "tradingview_sol"
    }});
    </script>
  </div>
</div>

<!-- ── Wyniki za okres ────────────────────────────────────────────────── -->
<div class="indicators-panel">
  <h3>📈 Wyniki
    <span style="margin-left:14px">
      <button class="period-btn active" data-period="24h" onclick="setPeriod('24h',this)">24h</button>
      <button class="period-btn" data-period="1d" onclick="setPeriod('1d',this)">Dziś</button>
      <button class="period-btn" data-period="7d" onclick="setPeriod('7d',this)">7 dni</button>
      <button class="period-btn" data-period="30d" onclick="setPeriod('30d',this)">30 dni</button>
    </span>
    <span id="period-loading" style="margin-left:8px;font-size:0.7em;color:#888"></span>
  </h3>
  <div class="ind-row" id="period-stats-row">
    <div class="ind-card">
      <div class="label">Maks. zaangażowana kwota</div>
      <div class="value" id="ps-max-capital">—</div>
      <div class="sub" id="ps-max-capital-mult"></div>
    </div>
    <div class="ind-card">
      <div class="label">Średni dzienny zwrot</div>
      <div class="value" id="ps-avg-daily">—</div>
      <div class="sub" id="ps-avg-daily-mult"></div>
    </div>
    <div class="ind-card">
      <div class="label">PnL rzeczywisty (TP1+TP2)</div>
      <div class="value" id="ps-total-income">—</div>
      <div class="sub" id="ps-total-income-pct"></div>
    </div>
    <div class="ind-card">
      <div class="label">PnL TP1-only</div>
      <div class="value" id="ps-tp1-income">—</div>
      <div class="sub" id="ps-tp1-income-pct"></div>
    </div>
    <div class="ind-card">
      <div class="label">Entry rate</div>
      <div class="value" id="ps-entry-rate">—</div>
      <div class="sub" id="ps-entry-detail"></div>
    </div>
    <div class="ind-card">
      <div class="label">Win rate (wygrane / uruchomione)</div>
      <div class="value" id="ps-win-rate">—</div>
      <div class="sub" id="ps-win-detail"></div>
    </div>
  </div>
</div>

<!-- ── Aktywne setupy ─────────────────────────────────────────────────── -->
<h3>Aktywne setupy ({len(active)})</h3>
<table id="active-table">
<tr><th>#</th><th>Model</th><th>Kier.</th><th>Typ</th><th>Status</th><th>W1</th><th>TP1</th><th>TP2</th><th>SL</th><th>SL@TP1</th>
<th title="Risk/Reward do TP1">RR→TP1</th>
<th title="Risk/Reward do TP2">RR→TP2</th>
<th>Akcje</th></tr>
{active_rows or '<tr><td colspan=13 style="color:#888">Brak aktywnych setupów</td></tr>'}
</table>

<!-- ── Analityka Algo2 ───────────────────────────────────────────────── -->
<div class="indicators-panel" style="margin-top:18px">
  <h3>🔬 Analityka Algo2
    <span style="margin-left:14px">
      <button class="period-btn active" data-a2period="7" onclick="setA2Period(7,this)">7 dni</button>
      <button class="period-btn" data-a2period="30" onclick="setA2Period(30,this)">30 dni</button>
      <button class="period-btn" data-a2period="0" onclick="setA2Period(0,this)">All-time</button>
    </span>
    <span id="a2-loading" style="margin-left:8px;font-size:0.7em;color:#888"></span>
  </h3>

  <!-- Per typ setupu -->
  <h4 style="color:#80deea;margin:10px 0 6px">Per typ setupu</h4>
  <div style="overflow-x:auto">
    <table id="a2-type-table" style="min-width:760px">
      <tr>
        <th>Typ</th><th>Kier.</th><th title="Łączna liczba setupów">Ilość</th>
        <th title="Procent setupów które weszły w pozycję">% entry</th>
        <th title="Win rate: TP1/TP2/TP1+BE/TP1+SL z uruchomionych">Win rate</th>
        <th title="Średni PnL z zamkniętych pozycji">Avg PnL $</th>
        <th title="Procent setupów które dotarły do TP2">TP2 rate</th>
        <th title="Średni czas od alertu do wejścia w pozycję">Śr. do entry</th>
        <th title="Średni czas trzymania pozycji">Śr. hold</th>
      </tr>
      <tr id="a2-type-loading"><td colspan="9" style="color:#888;text-align:center">ładowanie...</td></tr>
    </table>
  </div>

  <!-- Heatmapa godzinowa (zwijana) -->
  <details style="margin:16px 0 6px">
    <summary style="color:#80deea;font-size:0.95em;font-weight:bold;cursor:pointer;user-select:none">Heatmapa godzinowa (czas PL)</summary>
    <div style="overflow-x:auto;margin-top:8px">
      <table id="a2-heatmap-table" style="min-width:580px;font-size:0.85em">
        <tr>
          <th>Godz.</th>
          <th title="Liczba alertów">Alerty</th>
          <th title="Procent alertów które weszły">% entry</th>
          <th title="Win rate z pozycji uruchomionych o tej godzinie">Win rate</th>
          <th title="Liczba wygranych">W</th>
          <th title="Liczba strat">L</th>
        </tr>
        <tr id="a2-heatmap-loading"><td colspan="6" style="color:#888;text-align:center">ładowanie...</td></tr>
      </table>
    </div>
  </details>

  <!-- Analiza RR -->
  <h4 style="color:#80deea;margin:16px 0 6px">Analiza RR i poziomów wyjścia</h4>
  <div style="overflow-x:auto">
    <table id="a2-rr-table" style="min-width:680px">
      <tr>
        <th>Typ</th><th>Kier.</th>
        <th title="Liczba setupów które weszły">Wejść</th>
        <th title="Średni deklarowany R:R">Dekl. RR</th>
        <th title="% setupów które osiągnęły TP1 lub wyżej">TP1 rate</th>
        <th title="% setupów które osiągnęły TP2">TP2 rate</th>
        <th title="Setupy z TP1 ale wyjście na SL lub BE">TP1+BE/SL</th>
        <th title="% setupów zamkniętych na SL">SL rate</th>
      </tr>
      <tr id="a2-rr-loading"><td colspan="8" style="color:#888;text-align:center">ładowanie...</td></tr>
    </table>
  </div>

  <!-- Backtest wariantów -->
  <h4 style="color:#80deea;margin:20px 0 6px">Backtest wariantów parametrów</h4>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
    <label style="color:#aaa;font-size:0.85em">Dni historii:</label>
    <input id="bt-days" type="number" value="60" min="7" max="180"
           style="width:60px;background:#2a2a2a;color:#e0e0e0;border:1px solid #555;padding:3px 6px;font-family:monospace">
    <button class="btn-action" onclick="runBacktestVariants()">▶ Uruchom backtest</button>
    <span id="bt-status" style="color:#888;font-size:0.85em"></span>
    <a id="bt-csv-link" href="/api/backtest-variants/csv" download="backtest_variants.csv"
       style="display:none;color:#80deea;font-size:0.85em">⬇ Pobierz CSV</a>
  </div>
  <div id="bt-summary" style="overflow-x:auto;display:none">
    <table id="bt-table" style="min-width:700px">
      <tr>
        <th>Wariant</th>
        <th title="Liczba wygenerowanych setupów">Setups</th>
        <th title="% setupów które weszły w pozycję">Entry%</th>
        <th>SL</th><th>TP1</th><th>TP1+BE</th><th>TP2</th>
        <th title="% wygranych z wejść">Win%</th>
        <th title="Suma PnL wszystkich wejść (% od $100 trade)">ΣPnL%</th>
        <th title="Średni PnL na jedno wejście">Avg PnL%</th>
      </tr>
    </table>
  </div>
</div>

<h3>Per model</h3>
<table><tr><th>Model</th><th title="% setupów które weszły na giełdę">% entry</th><th title="% wygranych (TP1+BE+TP2) z uruchomionych">% win</th><th>PnL $</th><th>PnL %</th><th title="PnL gdyby każda pozycja wyszła na TP1">TP1-only $</th><th>TP1-only %</th></tr>
{by_model_rows or '<tr><td colspan=7 style="color:#888">Brak danych</td></tr>'}
</table>

<h3>Zamknięte setupy <span id="hist-count" style="color:#888;font-size:0.7em"></span></h3>
<div style="margin-bottom:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
  <label style="color:#aaa;font-size:0.85em">Model:</label>
  {model_checkboxes}
  <span style="width:1px;height:16px;background:#444;display:inline-block;margin:0 4px"></span>
  <label style="color:#aaa;font-size:0.85em">Wynik:</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="TP1"> TP1</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="TP2"> TP2</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="TP1+BE"> TP1+BE</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="TP1+SL"> TP1+SL</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="SL"> SL</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="nie weszlo"> Nie weszło</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="anulowany"> Anulowane</label>
  <label style="font-size:0.85em"><input type="checkbox" class="res-filter" value="nieokreslone"> Nieokreślone</label>
  <span style="margin-left:12px;color:#aaa;font-size:0.85em">Od:</span>
  <input type="date" id="date-from" style="background:#2a2a2a;color:#e0e0e0;border:1px solid #555;font-family:monospace;padding:2px 4px">
  <span style="color:#aaa;font-size:0.85em">Do:</span>
  <input type="date" id="date-to" style="background:#2a2a2a;color:#e0e0e0;border:1px solid #555;font-family:monospace;padding:2px 4px">
  <button class="btn-action" onclick="loadHistory(true)">Filtruj</button>
  <button class="btn-action" onclick="exportCsv()" title="Eksport CSV">Eksport CSV</button>
</div>
<table id="history-table">
<thead>
<tr><th>#</th><th>Alert</th><th>Wejście dt</th><th>Wyjście dt</th><th>Model</th><th>Kier.</th><th>Typ</th><th>Wejście</th><th>Wynik</th><th>Wyjście</th><th style="background:#1a2a2a">PnL $</th><th style="background:#1a2a2a">PnL %</th><th style="background:#1a2a2a" title="PnL gdyby cała pozycja wyszła na TP1 (dla SL = rzeczywisty PnL)">TP1-only $</th><th style="background:#1a2a2a" title="TP1-only %">TP1-only %</th><th title="Rzeczywisty PnL minus TP1-only (czy TP2 opłacał się)">Δ(real-TP1)</th><th></th></tr>
<tr id="hist-totals" style="background:#1a2a1a;font-weight:bold;font-size:0.9em"><td colspan=10 style="color:#888;font-size:0.8em">∑ filtr:</td><td id="ht-pnl" style="background:#1a2a2a">—</td><td id="ht-pnl-pct" style="background:#1a2a2a">—</td><td id="ht-tp1" style="background:#1a2a2a">—</td><td id="ht-tp1-pct" style="background:#1a2a2a">—</td><td id="ht-delta">—</td><td></td></tr>
</thead>
<tbody id="hist-body"><tr><td colspan=16 style="color:#888">Ładowanie...</td></tr></tbody>
</table>
<div style="text-align:center;margin:10px 0">
  <button class="btn-action" id="load-more-btn" onclick="loadHistory(false)" style="display:none">Załaduj więcej</button>
</div>
</body>
<script>
var RESULT_LABELS = {{
  'TP1':'TP1','TP2':'TP2','TP1+BE':'TP1+BE','TP1+SL':'TP1+SL','SL':'SL',
  'nieokreslone':'Nieokreślone','nie weszlo':'Nie weszło','anulowany':'Anulowane'
}};

function getSetupData(tr) {{
  return JSON.parse(tr.dataset.setup);
}}

function editRow(btn) {{
  var tr = btn.closest('tr');
  tr.dataset.setupOriginal = tr.dataset.setup;
  tr.dataset.pnlSnapshot = JSON.stringify({{
    pnl: tr.querySelector('.pnl-cell').textContent,       pnlC: tr.querySelector('.pnl-cell').style.color,
    pct: tr.querySelector('.pnl-pct-cell').textContent,   pctC: tr.querySelector('.pnl-pct-cell').style.color,
    alt: tr.querySelector('.alt-pnl-cell').textContent,   altC: tr.querySelector('.alt-pnl-cell').style.color,
    dlt: tr.querySelector('.delta-cell').textContent,     dltC: tr.querySelector('.delta-cell').style.color,
  }});
  tr.classList.add('editing');
}}

function cancelEdit(btn) {{
  var tr = btn.closest('tr');
  tr.dataset.setup = tr.dataset.setupOriginal;
  var d = JSON.parse(tr.dataset.setupOriginal);
  tr.querySelector('.avg-entry-input').value = d.avg_entry != null ? d.avg_entry : '';
  var exitDisp = tr.querySelector('.exit-display').textContent;
  tr.querySelector('.avg-exit-input').value = (exitDisp === '—') ? '' : exitDisp;
  try {{
    var snap = JSON.parse(tr.dataset.pnlSnapshot);
    tr.querySelector('.pnl-cell').textContent     = snap.pnl; tr.querySelector('.pnl-cell').style.color     = snap.pnlC;
    tr.querySelector('.pnl-pct-cell').textContent = snap.pct; tr.querySelector('.pnl-pct-cell').style.color = snap.pctC;
    tr.querySelector('.alt-pnl-cell').textContent = snap.alt; tr.querySelector('.alt-pnl-cell').style.color = snap.altC;
    tr.querySelector('.delta-cell').textContent   = snap.dlt; tr.querySelector('.delta-cell').style.color   = snap.dltC;
  }} catch(e) {{}}
  tr.classList.remove('editing');
}}

function calcAvgExit(result, d) {{
  if (result === 'SL')     return d.sl;
  if (result === 'TP1')    return d.tp1;
  if (result === 'TP2')    return (d.tp1 != null && d.tp2 != null) ? (d.tp1 + d.tp2) / 2 : d.tp2;
  if (result === 'TP1+BE') return (d.tp1 != null && d.sl_after_tp1 != null) ? (d.tp1 + d.sl_after_tp1) / 2 : null;
  if (result === 'TP1+SL') return (d.tp1 != null && d.sl_after_tp1 != null) ? (d.tp1 + d.sl_after_tp1) / 2 : null;
  return null;
}}

function calcPnl(result, d, avgExit) {{
  if (!d.avg_entry) return null;
  if (!['TP1','TP1+TP2','TP2','TP1+BE','TP1+SL','SL'].includes(result)) return null;
  var sign = d.direction === 'long' ? 1 : -1;
  if (result === 'TP1+BE' && d.tp1 && d.half_qty) {{
    return Math.round(sign * d.half_qty * (d.tp1 - d.avg_entry) * 100) / 100;
  }}
  if (result === 'TP1+SL' && d.tp1 && d.half_qty) {{
    var tp1Pnl = sign * d.half_qty * (d.tp1 - d.avg_entry);
    var slPnl  = (avgExit != null && !isNaN(avgExit)) ? sign * d.half_qty * (avgExit - d.avg_entry) : 0;
    return Math.round((tp1Pnl + slPnl) * 100) / 100;
  }}
  if (!d.full_qty || avgExit == null || isNaN(avgExit)) return null;
  return Math.round(sign * d.full_qty * (avgExit - d.avg_entry) * 100) / 100;
}}

function refreshAllCells(tr, pnl) {{
  var d      = getSetupData(tr);
  var result = tr.querySelector('.result-select').value;

  var fmt = function(v) {{ return (v >= 0 ? '+' : '') + v.toFixed(2); }};
  var clr = function(v) {{ return v == null ? 'gray' : (v >= 0 ? 'lightgreen' : 'salmon'); }};

  var pnlCell = tr.querySelector('.pnl-cell');
  pnlCell.textContent = pnl != null ? fmt(pnl) : '—';
  pnlCell.style.color = clr(pnl);

  var pctCell = tr.querySelector('.pnl-pct-cell');
  var tu = d.trade_usdt || 100;
  var pct = (pnl != null && tu) ? pnl / tu * 100 : null;
  pctCell.textContent = pct != null ? (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%' : '—';
  pctCell.style.color = clr(pct);

  var altCell = tr.querySelector('.alt-pnl-cell');
  var deltaCell = tr.querySelector('.delta-cell');
  var alt = null;
  if ((result === 'TP2' || result === 'TP1+BE' || result === 'TP1+SL') && d.tp1 && d.avg_entry && d.full_qty) {{
    alt = Math.round((d.direction === 'long' ? 1 : -1) * d.full_qty * (d.tp1 - d.avg_entry) * 100) / 100;
  }}
  altCell.textContent = alt != null ? fmt(alt) : '—';
  altCell.style.color = clr(alt);
  var dlt = (alt != null && pnl != null) ? pnl - alt : null;
  deltaCell.textContent = dlt != null ? fmt(dlt) : '—';
  deltaCell.style.color = clr(dlt);
}}

function onEntryChange(inp) {{
  var tr = inp.closest('tr');
  var d  = JSON.parse(tr.dataset.setup);
  var newEntry = parseFloat(inp.value) || null;
  d.avg_entry = newEntry;
  if (newEntry && !d.full_qty) {{
    var tu = d.trade_usdt || 100;
    d.full_qty = Math.max(Math.floor((tu * 20 / newEntry) / 0.1) * 0.1, 0.1);
    d.half_qty = Math.max(Math.floor((d.full_qty / 2) / 0.1) * 0.1, 0.1);
  }}
  tr.dataset.setup = JSON.stringify(d);
  var res = tr.querySelector('.result-select').value;
  var ae  = parseFloat(tr.querySelector('.avg-exit-input').value);
  refreshAllCells(tr, calcPnl(res, d, isNaN(ae) ? null : ae));
}}

function onResultChange(sel) {{
  var tr  = sel.closest('tr');
  var d   = getSetupData(tr);
  var inp = tr.querySelector('.avg-exit-input');
  var ae  = calcAvgExit(sel.value, d);
  inp.value = ae != null ? ae.toFixed(2) : '';
  refreshAllCells(tr, calcPnl(sel.value, d, ae));
}}

function onExitChange(inp) {{
  var tr  = inp.closest('tr');
  var d   = getSetupData(tr);
  var res = tr.querySelector('.result-select').value;
  refreshAllCells(tr, calcPnl(res, d, parseFloat(inp.value)));
}}

// ── koniec Bitget live ───────────────────────────────────────────────────────

// ── Zarządzanie aktywnymi setupami ───────────────────────────────────────────
function editActiveTp(btn) {{
  btn.closest('tr').classList.add('editing-tp');
}}

function cancelActiveTpEdit(btn) {{
  btn.closest('tr').classList.remove('editing-tp');
}}

async function saveActiveTp(btn) {{
  var tr  = btn.closest('tr');
  var sid = tr.dataset.sid;
  var tp1Str = tr.querySelector('.tp1-input').value;
  var tp2Str = tr.querySelector('.tp2-input').value;
  var slStr  = tr.querySelector('.sl-input').value;
  var tp1 = tp1Str !== '' ? parseFloat(tp1Str) : null;
  var tp2 = tp2Str !== '' ? parseFloat(tp2Str) : null;
  var sl  = slStr  !== '' ? parseFloat(slStr)  : null;
  if (tp1 === null && tp2 === null && sl === null) {{
    alert('Wpisz co najmniej jedną wartość (TP1, TP2 lub SL).');
    return;
  }}
  btn.textContent = '...'; btn.disabled = true;
  try {{
    var resp = await fetch('/api/update-tps/' + sid, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{tp1: tp1, tp2: tp2, sl: sl}})
    }});
    var data = await resp.json();
    if (resp.ok && data.ok) {{
      if (tp1 !== null) tr.querySelector('.tp1-input').closest('td').querySelector('.av-view').textContent = '$' + tp1.toFixed(2);
      if (tp2 !== null) tr.querySelector('.tp2-input').closest('td').querySelector('.av-view').textContent = '$' + tp2.toFixed(2);
      if (sl  !== null) tr.querySelector('.sl-input').closest('td').querySelector('.av-view').textContent  = '$' + sl.toFixed(2);
      tr.classList.remove('editing-tp');
    }} else {{
      alert('Błąd zapisu: ' + (data.failed || []).join(', ') || data.detail || 'nieznany błąd');
    }}
  }} catch(e) {{
    alert('Błąd: ' + e.message);
  }}
  btn.textContent = 'Zapisz'; btn.disabled = false;
}}

async function cancelActiveSetup(btn) {{
  var tr      = btn.closest('tr');
  var sid     = tr.dataset.sid;
  var posOpen = tr.dataset.posOpen === 'true';
  var tp1Done = tr.dataset.tp1Done === 'true';
  var qtyFull = tr.dataset.qtyFull;
  var closeQtyInfo = tp1Done ? '½ pozycji (' + qtyFull + '/2 SOL)' : qtyFull ? qtyFull + ' SOL' : 'pozycja';
  var msg     = posOpen
    ? 'Anulować setup #' + sid + '?\\n\\nZlecenia TP1/TP2/SL zostaną anulowane, a ' + closeQtyInfo + ' zostanie zamknięta RYNKOWO na bieżącej cenie.'
    : 'Anulować setup #' + sid + '? Plan order zostanie anulowany.';
  if (!confirm(msg)) return;
  btn.textContent = '...'; btn.disabled = true;
  try {{
    var resp = await fetch('/api/cancel-setup/' + sid, {{method: 'POST'}});
    var data = await resp.json();
    if (resp.ok && data.ok) {{
      tr.style.opacity = '0.4';
      btn.textContent = '✓';
      setTimeout(function() {{ location.reload(); }}, 1500);
    }} else {{
      alert('Błąd: ' + (data.detail || data.message || 'nieznany błąd'));
      btn.textContent = 'Anuluj setup'; btn.disabled = false;
    }}
  }} catch(e) {{
    alert('Błąd: ' + e.message);
    btn.textContent = 'Anuluj setup'; btn.disabled = false;
  }}
}}
// ── koniec zarządzania setupami ──────────────────────────────────────────────

async function saveResult(btn) {{
  var tr       = btn.closest('tr');
  var setupId  = tr.dataset.setupId;
  var result   = tr.querySelector('.result-select').value;
  var exitVal  = tr.querySelector('.avg-exit-input').value;
  var entryVal = tr.querySelector('.avg-entry-input').value;
  var avgExit  = exitVal  !== '' ? parseFloat(exitVal)  : null;
  var avgEntry = entryVal !== '' ? parseFloat(entryVal) : null;

  btn.textContent = '...'; btn.disabled = true;
  try {{
    var resp = await fetch('/api/update-result/' + setupId, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{result: result, avg_exit: avgExit, avg_entry: avgEntry}})
    }});
    var data = await resp.json();
    if (resp.ok && data.ok) {{
      btn.textContent = '✓'; btn.style.color = 'lightgreen';
      tr.querySelector('.result-display').textContent = RESULT_LABELS[result] || result;
      if (data.avg_exit != null) {{
        var ae = parseFloat(data.avg_exit).toFixed(2);
        tr.querySelector('.exit-display').textContent = ae;
        tr.querySelector('.avg-exit-input').value = ae;
      }}
      if (data.avg_entry != null) {{
        var ent = parseFloat(data.avg_entry).toFixed(2);
        tr.querySelector('.avg-entry-display').textContent = ent;
        tr.querySelector('.avg-entry-input').value = ent;
        var d2 = getSetupData(tr);
        d2.avg_entry = data.avg_entry;
        tr.dataset.setup = JSON.stringify(d2);
      }}
      refreshAllCells(tr, data.pnl_usd != null ? data.pnl_usd : null);
      tr.classList.remove('editing');
    }} else {{
      btn.textContent = '✗'; btn.style.color = 'salmon';
      setTimeout(function() {{ btn.textContent = 'Zapisz'; btn.style.color = ''; btn.disabled = false; }}, 2000);
    }}
  }} catch(e) {{
    btn.textContent = '✗'; btn.style.color = 'salmon';
    setTimeout(function() {{ btn.textContent = 'Zapisz'; btn.style.color = ''; btn.disabled = false; }}, 2000);
  }}
}}

// ── Historia z filtrami ──────────────────────────────────────────────────────
var TRADE_USDT = {trade_usdt};
var RESULT_OPTS_ARR = ['TP1','TP2','TP1+BE','TP1+SL','SL','nieokreslone','nie weszlo','anulowany'];
var histOffset = 0;
var HIST_PAGE  = 50;

function getFilterParams() {{
  var checked = [];
  document.querySelectorAll('.res-filter:checked').forEach(function(cb) {{ checked.push(cb.value); }});
  var models = [];
  document.querySelectorAll('.model-filter:checked').forEach(function(cb) {{ models.push(cb.value); }});
  var params = new URLSearchParams();
  if (checked.length) params.set('results', checked.join(','));
  if (models.length)  params.set('models',  models.join(','));
  var df = document.getElementById('date-from').value;
  var dt = document.getElementById('date-to').value;
  if (df) params.set('date_from', df);
  if (dt) params.set('date_to', dt);
  return params;
}}

function fmtDt(v) {{
  // Format ISO datetime or Unix timestamp (ms or s) as "DD.MM HH:MM"
  if (!v) return '—';
  var d;
  if (typeof v === 'number') {{
    d = new Date(v > 1e12 ? v : v * 1000);
  }} else {{
    d = new Date(v);
  }}
  if (isNaN(d)) return '—';
  var dd = String(d.getDate()).padStart(2,'0');
  var mm = String(d.getMonth()+1).padStart(2,'0');
  var hh = String(d.getHours()).padStart(2,'0');
  var mi = String(d.getMinutes()).padStart(2,'0');
  return dd + '.' + mm + ' ' + hh + ':' + mi;
}}

function buildHistRow(s) {{
  var TRADING = {{'TP1':1,'TP2':1,'TP1+BE':1,'TP1+SL':1,'SL':1}};
  var entries = s.entries || [];
  var tps     = s.tps || [];
  var w1      = entries[0] || null;
  var avgE    = s.avg_entry || null;
  var avgX    = s.avg_exit  || null;
  var result  = s.result || '';
  var dir     = s.direction || 'long';
  var sign    = dir === 'long' ? 1 : -1;

  // Date/time columns
  var alertDt = fmtDt(s.alert_time);
  var entryDt = fmtDt(s.entry_hit_at);
  var exitDt  = fmtDt(s.exit_time);

  // Per-row trade_usdt (kwota zlecenia z momentu otwarcia pozycji)
  var rowTU = s.trade_usdt != null ? parseFloat(s.trade_usdt) : TRADE_USDT;

  // Qty
  var fq = s.exchange_qty_full ? parseFloat(s.exchange_qty_full) : null;
  var hq = s.exchange_qty_half ? parseFloat(s.exchange_qty_half) : null;
  var efc = avgE || (TRADING[result] ? w1 : null);
  if (!fq && efc) fq = Math.max(Math.floor((rowTU * 20 / efc) / 0.1) * 0.1, 0.1);
  if (!hq && fq) hq = Math.max(Math.floor((fq / 2) / 0.1) * 0.1, 0.1);

  // PnL
  var pnl = s.pnl_usd != null ? s.pnl_usd : null;
  if (pnl == null && TRADING[result] && avgX && efc && fq) {{
    pnl = Math.round(sign * fq * (avgX - efc) * 100) / 100;
  }}
  var pnlPct = s.pnl_pct != null ? s.pnl_pct : (pnl != null && rowTU ? Math.round(pnl / rowTU * 10000) / 100 : null);

  // Alt PnL (TP1-only): for SL = same as actual; for TP2/TP1+BE/TP1+SL = TP1 price
  var tp1p = tps[0] || null;
  var alt = null, dlt = null;
  if (result === 'SL') {{
    alt = pnl;
  }} else if ((result === 'TP2' || result === 'TP1+BE' || result === 'TP1+SL') && tp1p && efc && fq) {{
    alt = Math.round(sign * fq * (tp1p - efc) * 100) / 100;
  }}
  var altPct = alt != null && rowTU ? Math.round(alt / rowTU * 10000) / 100 : null;
  if (alt != null && pnl != null) dlt = Math.round((pnl - alt) * 100) / 100;

  var fmt  = function(v) {{ return v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2); }};
  var fmtP = function(v) {{ return v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%'; }};
  var clr  = function(v) {{ return v == null ? 'gray' : (v > 0 ? 'lightgreen' : 'salmon'); }};

  var entryStr = avgE ? avgE.toFixed(2) : (TRADING[result] && w1 ? w1.toFixed(2) : '—');
  var exitStr  = avgX != null ? avgX.toFixed(2) : '—';
  var resLabel = RESULT_LABELS[result] || result || '—';

  // Setup data JSON for edit mode
  var sd = {{
    avg_entry: avgE || w1, w1: w1, tp1: tp1p, tp2: tps[1] || null,
    sl: s.sl || null, sl_after_tp1: s.sl_after_tp1 || null,
    direction: dir, full_qty: fq, half_qty: hq, trade_usdt: TRADE_USDT
  }};
  var sdJson = JSON.stringify(sd).replace(/&/g,'&amp;').replace(/"/g,'&quot;');

  // Result dropdown
  var opts = '';
  RESULT_OPTS_ARR.forEach(function(o) {{
    var lbl = RESULT_LABELS[o] || o;
    opts += '<option value="' + o + '"' + (o === result ? ' selected' : '') + '>' + lbl + '</option>';
  }});

  var exitInp = avgX != null ? avgX.toFixed(2) : '';
  var entryInp = avgE ? avgE : (w1 || '');

  return '<tr data-setup-id="' + s.setup_id + '" data-setup="' + sdJson + '">'
    + '<td>#' + s.setup_id + '</td>'
    + '<td style="font-size:0.8em;color:#aaa">' + alertDt + '</td>'
    + '<td style="font-size:0.8em;color:#aaa">' + entryDt + '</td>'
    + '<td style="font-size:0.8em;color:#aaa">' + exitDt + '</td>'
    + '<td>' + s.model + '</td>'
    + '<td>' + dir.toUpperCase() + '</td>'
    + '<td style="font-size:0.75em;color:#aaa;white-space:nowrap">' + (s.type || '') + '</td>'
    + '<td><span class="vmode avg-entry-display">' + entryStr + '</span>'
    +   '<input class="emode avg-entry-input" type="number" step="0.01" value="' + entryInp + '" oninput="onEntryChange(this)"></td>'
    + '<td><span class="vmode result-display">' + resLabel + '</span>'
    +   '<select class="emode result-select" onchange="onResultChange(this)">' + opts + '</select></td>'
    + '<td><span class="vmode exit-display">' + exitStr + '</span>'
    +   '<input class="emode avg-exit-input" type="number" step="0.01" value="' + exitInp + '" oninput="onExitChange(this)"></td>'
    + '<td class="pnl-cell" style="background:#1a2a2a;color:' + clr(pnl) + '">' + fmt(pnl) + '</td>'
    + '<td class="pnl-pct-cell" style="background:#1a2a2a;color:' + clr(pnlPct) + '">' + fmtP(pnlPct) + '</td>'
    + '<td class="alt-pnl-cell" style="background:#1a2a2a;color:' + clr(alt) + '">' + fmt(alt) + '</td>'
    + '<td class="alt-pct-cell" style="background:#1a2a2a;color:' + clr(altPct) + '">' + fmtP(altPct) + '</td>'
    + '<td class="delta-cell" style="color:' + clr(dlt) + '">' + fmt(dlt) + '</td>'
    + '<td style="white-space:nowrap">'
    +   '<button class="btn-edit vmode" onclick="editRow(this)">Edytuj</button>'
    +   '<button class="btn-action emode" onclick="saveResult(this)">Zapisz</button>'
    +   '<button class="btn-action emode" onclick="cancelEdit(this)">Anuluj</button>'
    + '</td></tr>';
}}

async function loadHistory(reset) {{
  if (reset) histOffset = 0;
  var params = getFilterParams();
  params.set('limit', HIST_PAGE);
  params.set('offset', histOffset);
  try {{
    var resp = await fetch('/api/resolved?' + params.toString());
    var data = await resp.json();
    var body = document.getElementById('hist-body');
    if (reset) body.innerHTML = '';
    if (!data.rows || data.rows.length === 0) {{
      if (reset) body.innerHTML = '<tr><td colspan=15 style="color:#888">Brak wyników</td></tr>';
    }} else {{
      data.rows.forEach(function(s) {{ body.innerHTML += buildHistRow(s); }});
    }}
    histOffset += (data.rows || []).length;
    var total = data.total || 0;
    document.getElementById('hist-count').textContent = '(' + histOffset + '/' + total + ')';
    document.getElementById('load-more-btn').style.display = histOffset < total ? '' : 'none';
    // Update totals row (always reflects full filter, not just loaded page)
    if (reset && data.totals) {{
      var t = data.totals;
      var fmtT = function(v) {{ return v == null ? '—' : (v >= 0 ? '+' : '') + parseFloat(v).toFixed(2); }};
      var fmtPT = function(v) {{ return v == null ? '—' : (v >= 0 ? '+' : '') + parseFloat(v).toFixed(1) + '%'; }};
      var clrT = function(v) {{ return v == null ? '#888' : (parseFloat(v) >= 0 ? 'lightgreen' : 'salmon'); }};
      var setT = function(id, val, fmt) {{
        var el = document.getElementById(id);
        el.textContent = fmt(val);
        el.style.color = clrT(val);
      }};
      setT('ht-pnl',     t.sum_pnl_usd,      fmtT);
      setT('ht-pnl-pct', t.sum_pnl_pct,      fmtPT);
      setT('ht-tp1',     t.sum_tp1_only_usd,  fmtT);
      setT('ht-tp1-pct', t.sum_tp1_only_pct,  fmtPT);
      var delta = (t.sum_pnl_usd != null && t.sum_tp1_only_usd != null)
        ? Math.round((parseFloat(t.sum_pnl_usd) - parseFloat(t.sum_tp1_only_usd)) * 100) / 100
        : null;
      setT('ht-delta', delta, fmtT);
    }}
  }} catch(e) {{
    document.getElementById('hist-body').innerHTML = '<tr><td colspan=15 style="color:salmon">Błąd: ' + e.message + '</td></tr>';
  }}
}}

function exportCsv() {{
  var params = getFilterParams();
  window.open('/api/resolved/csv?' + params.toString());
}}

// ── koniec historii ──────────────────────────────────────────────────────────

// ── Ustawienia ──────────────────────────────────────────────────────────────
async function loadSettings() {{
  try {{
    var resp = await fetch('/api/settings');
    var data = await resp.json();
    document.getElementById('set-trade-usdt').value = data.trade_usdt;
    document.getElementById('set-alert-interval').value = data.alert_interval;
    document.getElementById('set-max-positions').value = data.max_positions;
  }} catch(e) {{
    console.error('loadSettings:', e);
  }}
}}

async function saveSettings() {{
  var btn = document.getElementById('settings-save-btn');
  var st  = document.getElementById('settings-status');
  btn.disabled = true; btn.textContent = '...';
  try {{
    var body = {{
      trade_usdt:     parseFloat(document.getElementById('set-trade-usdt').value) || null,
      alert_interval: parseInt(document.getElementById('set-alert-interval').value) || null,
      max_positions:  parseInt(document.getElementById('set-max-positions').value) || null,
    }};
    var resp = await fetch('/api/settings', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body)
    }});
    var data = await resp.json();
    if (data.ok) {{
      st.textContent = '✓ Zapisano'; st.style.color = 'lightgreen';
    }} else {{
      st.textContent = '✗ Błąd'; st.style.color = 'salmon';
    }}
  }} catch(e) {{
    st.textContent = '✗ ' + e.message; st.style.color = 'salmon';
  }}
  btn.disabled = false; btn.textContent = 'Zapisz ustawienia';
  setTimeout(function() {{ st.textContent = ''; }}, 4000);
}}

loadSettings();

// ── Wyniki za okres ─────────────────────────────────────────────────────────
var currentPeriod = '24h';

function setPeriod(p, btn) {{
  currentPeriod = p;
  document.querySelectorAll('.period-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  btn.classList.add('active');
  loadPeriodStats();
}}

async function loadPeriodStats() {{
  var loading = document.getElementById('period-loading');
  loading.textContent = 'ładowanie...';
  try {{
    var resp = await fetch('/api/period-stats?period=' + currentPeriod);
    var d = await resp.json();

    var fmt  = function(v) {{ return v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2); }};
    var clr  = function(v) {{ return v == null ? '#e0e0e0' : (v >= 0 ? 'lightgreen' : 'salmon'); }};
    var tu   = d.trade_usdt || 100;

    // Max capital
    document.getElementById('ps-max-capital').textContent = '$' + d.max_capital.toFixed(0);
    document.getElementById('ps-max-capital').style.color = '#e0e0e0';
    document.getElementById('ps-max-capital-mult').textContent = d.max_capital_mult.toFixed(1) + 'x × $' + tu.toFixed(0) + ' (' + d.max_open_positions + ' poz.)';

    // Avg daily
    document.getElementById('ps-avg-daily').textContent = fmt(d.avg_daily_pnl) + ' $';
    document.getElementById('ps-avg-daily').style.color = clr(d.avg_daily_pnl);
    document.getElementById('ps-avg-daily-mult').textContent = (d.avg_daily_mult >= 0 ? '+' : '') + (d.avg_daily_mult * 100).toFixed(1) + '% kwoty';

    // Actual PnL (TP1+TP2)
    var actualInc = d.total_income != null ? d.total_income : null;
    var actualPct = d.total_income_pct != null ? d.total_income_pct : null;
    document.getElementById('ps-total-income').textContent = actualInc != null ? fmt(actualInc) + ' $' : '—';
    document.getElementById('ps-total-income').style.color = clr(actualInc);
    document.getElementById('ps-total-income-pct').textContent = actualPct != null ? (actualPct >= 0 ? '+' : '') + actualPct.toFixed(1) + '%' : '';
    document.getElementById('ps-total-income-pct').style.color = clr(actualPct);
    // TP1-only PnL
    var tp1Inc = d.tp1_only_income != null ? d.tp1_only_income : null;
    var tp1Pct = d.tp1_only_income_pct != null ? d.tp1_only_income_pct : null;
    document.getElementById('ps-tp1-income').textContent = tp1Inc != null ? fmt(tp1Inc) + ' $' : '—';
    document.getElementById('ps-tp1-income').style.color = clr(tp1Inc);
    document.getElementById('ps-tp1-income-pct').textContent = tp1Pct != null ? (tp1Pct >= 0 ? '+' : '') + tp1Pct.toFixed(1) + '%' : '';
    document.getElementById('ps-tp1-income-pct').style.color = clr(tp1Pct);

    // Entry rate
    document.getElementById('ps-entry-rate').textContent = d.entry_rate.toFixed(1) + '%';
    document.getElementById('ps-entry-detail').textContent = d.entered + ' / ' + d.total_setups + ' zleceń';

    // Win rate
    document.getElementById('ps-win-rate').textContent = d.win_rate.toFixed(1) + '%';
    document.getElementById('ps-win-detail').textContent = d.wins + ' wygranych / ' + d.entered + ' uruchomionych';

    loading.textContent = '';
  }} catch(e) {{
    loading.textContent = '⚠️ ' + e.message;
  }}
}}

loadPeriodStats();

// ── Algo2 Analytics ──────────────────────────────────────────────────────────
var currentA2Period = 7;

function setA2Period(p, btn) {{
  currentA2Period = p;
  document.querySelectorAll('[data-a2period]').forEach(function(b) {{ b.classList.remove('active'); }});
  if (btn) btn.classList.add('active');
  loadAlgo2Analytics();
}}

function fmtPct(v) {{
  if (v == null) return '—';
  var s = v.toFixed(1) + '%';
  return s;
}}
function fmtPnl(v) {{
  if (v == null) return '—';
  return (v >= 0 ? '+' : '') + parseFloat(v).toFixed(2) + ' $';
}}
function pnlColor(v) {{
  if (v == null) return '#e0e0e0';
  return v >= 0 ? 'lightgreen' : 'salmon';
}}
function pctColor(v, lo, hi) {{
  // lo = red threshold, hi = green threshold
  if (v == null) return '#e0e0e0';
  if (v >= hi) return 'lightgreen';
  if (v >= lo) return '#e0e0e0';
  return 'salmon';
}}
function heatColor(v, max) {{
  if (!v || !max) return '';
  var intensity = Math.min(v / max, 1.0);
  var g = Math.round(100 + intensity * 100);
  return 'rgba(64,' + g + ',64,0.35)';
}}

async function loadAlgo2Analytics() {{
  var loading = document.getElementById('a2-loading');
  loading.textContent = 'ładowanie...';
  var periodParam = currentA2Period ? '?period=' + currentA2Period : '';
  try {{
    var [tsResp, hmResp, rrResp] = await Promise.all([
      fetch('/api/algo2/type-stats' + periodParam),
      fetch('/api/algo2/time-heatmap' + periodParam),
      fetch('/api/algo2/rr-analysis' + periodParam),
    ]);
    var typeData = await tsResp.json();
    var hmData   = await hmResp.json();
    var rrData   = await rrResp.json();
    renderA2TypeTable(typeData);
    renderA2Heatmap(hmData);
    renderA2RR(rrData);
    loading.textContent = '';
  }} catch(e) {{
    loading.textContent = '⚠️ ' + e.message;
  }}
}}

function renderA2TypeTable(rows) {{
  var tbl = document.getElementById('a2-type-table');
  var header = tbl.rows[0];
  while (tbl.rows.length > 1) tbl.deleteRow(1);
  if (!rows || rows.length === 0) {{
    var tr = tbl.insertRow(); tr.insertCell().colSpan = 9;
    tr.cells[0].colSpan = 9; tr.cells[0].textContent = 'Brak danych'; tr.cells[0].style.color = '#888';
    return;
  }}
  rows.forEach(function(r) {{
    var tr = tbl.insertRow();
    var dirColor = r.direction === 'long' ? '#81c995' : '#f28b82';
    [
      r.type,
      r.direction,
      r.total,
      r.entry_rate != null ? r.entry_rate.toFixed(1) + '%' : '—',
      r.win_rate   != null ? r.win_rate.toFixed(1)   + '%' : '—',
      fmtPnl(r.avg_pnl_usd),
      r.tp2_rate   != null ? r.tp2_rate.toFixed(1)   + '%' : '—',
      r.avg_time_to_entry_h != null ? r.avg_time_to_entry_h.toFixed(1) + 'h' : '—',
      r.avg_hold_h != null ? r.avg_hold_h.toFixed(1) + 'h' : '—',
    ].forEach(function(val, i) {{
      var td = tr.insertCell();
      td.textContent = val;
      if (i === 1) td.style.color = dirColor;
      if (i === 4 && r.win_rate != null) td.style.color = pctColor(r.win_rate, 40, 55);
      if (i === 5 && r.avg_pnl_usd != null) td.style.color = pnlColor(r.avg_pnl_usd);
      if (i === 6 && r.tp2_rate != null) td.style.color = pctColor(r.tp2_rate, 20, 40);
    }});
  }});
}}

function renderA2Heatmap(rows) {{
  var tbl = document.getElementById('a2-heatmap-table');
  while (tbl.rows.length > 1) tbl.deleteRow(1);
  if (!rows || rows.length === 0) {{
    var tr = tbl.insertRow();
    tr.insertCell().colSpan = 6; tr.cells[0].colSpan = 6;
    tr.cells[0].textContent = 'Brak danych'; tr.cells[0].style.color = '#888';
    return;
  }}
  var maxTotal = Math.max.apply(null, rows.map(function(r) {{ return r.total || 0; }}));
  rows.forEach(function(r) {{
    var tr = tbl.insertRow();
    var bgColor = heatColor(r.total, maxTotal);
    if (bgColor) tr.style.background = bgColor;
    var h = r.hour;
    var hStr = (h < 10 ? '0' : '') + h + ':00';
    [
      hStr,
      r.total,
      r.entry_rate != null ? r.entry_rate.toFixed(1) + '%' : '—',
      r.win_rate   != null ? r.win_rate.toFixed(1)   + '%' : '—',
      r.wins  || 0,
      r.losses || 0,
    ].forEach(function(val, i) {{
      var td = tr.insertCell();
      td.textContent = val;
      if (i === 2 && r.entry_rate != null) td.style.color = pctColor(r.entry_rate, 20, 50);
      if (i === 3 && r.win_rate   != null) td.style.color = pctColor(r.win_rate, 40, 55);
    }});
  }});
}}

function renderA2RR(rows) {{
  var tbl = document.getElementById('a2-rr-table');
  while (tbl.rows.length > 1) tbl.deleteRow(1);
  if (!rows || rows.length === 0) {{
    var tr = tbl.insertRow();
    tr.insertCell().colSpan = 8; tr.cells[0].colSpan = 8;
    tr.cells[0].textContent = 'Brak danych'; tr.cells[0].style.color = '#888';
    return;
  }}
  rows.forEach(function(r) {{
    var tr = tbl.insertRow();
    var dirColor = r.direction === 'long' ? '#81c995' : '#f28b82';
    [
      r.type,
      r.direction,
      r.entered || 0,
      r.avg_rr_declared != null ? r.avg_rr_declared.toFixed(2) : '—',
      r.tp1_rate != null ? r.tp1_rate.toFixed(1) + '%' : '—',
      r.tp2_rate != null ? r.tp2_rate.toFixed(1) + '%' : '—',
      r.tp1_be_sl_hits || 0,
      r.sl_rate  != null ? r.sl_rate.toFixed(1)  + '%' : '—',
    ].forEach(function(val, i) {{
      var td = tr.insertCell();
      td.textContent = val;
      if (i === 1) td.style.color = dirColor;
      if (i === 4 && r.tp1_rate != null) td.style.color = pctColor(r.tp1_rate, 40, 60);
      if (i === 5 && r.tp2_rate != null) td.style.color = pctColor(r.tp2_rate, 20, 40);
      if (i === 7 && r.sl_rate  != null) td.style.color = pctColor(100 - r.sl_rate, 40, 60);
    }});
  }});
}}

loadAlgo2Analytics();

// ── Backtest wariantów ────────────────────────────────────────────────────────
var _btPollTimer = null;

function runBacktestVariants() {{
  var days = parseInt(document.getElementById('bt-days').value) || 60;
  var statusEl = document.getElementById('bt-status');
  var csvLink  = document.getElementById('bt-csv-link');
  var summaryEl = document.getElementById('bt-summary');
  statusEl.textContent = 'Uruchamianie...';
  csvLink.style.display = 'none';
  summaryEl.style.display = 'none';

  fetch('/admin/run-backtest-variants?days=' + days, {{method: 'POST'}})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (!data.ok) {{ statusEl.textContent = '⚠️ ' + data.message; return; }}
      statusEl.textContent = '⏳ Pobieranie danych i obliczenia... (kilka minut)';
      if (_btPollTimer) clearInterval(_btPollTimer);
      _btPollTimer = setInterval(pollBacktestStatus, 5000);
    }})
    .catch(function(e) {{ statusEl.textContent = '⚠️ ' + e.message; }});
}}

function pollBacktestStatus() {{
  fetch('/api/backtest-variants/status')
    .then(function(r) {{ return r.json(); }})
    .then(function(s) {{
      var statusEl = document.getElementById('bt-status');
      if (s.running) {{
        statusEl.textContent = '⏳ Trwa... (started: ' + (s.started_at || '').substring(11,16) + ' UTC)';
      }} else if (s.done) {{
        clearInterval(_btPollTimer);
        statusEl.textContent = '✅ Gotowe! (' + s.rows + ' rekordów)';
        document.getElementById('bt-csv-link').style.display = 'inline';
        loadBacktestSummary();
      }} else if (s.error) {{
        clearInterval(_btPollTimer);
        statusEl.textContent = '❌ Błąd: ' + s.error;
      }}
    }});
}}

function loadBacktestSummary() {{
  fetch('/api/backtest-variants/result?limit=0')
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var tbl = document.getElementById('bt-table');
      // usuń poprzednie wiersze (poza nagłówkiem)
      while (tbl.rows.length > 1) tbl.deleteRow(1);
      (data.summary || []).forEach(function(s) {{
        var tr = tbl.insertRow();
        var winColor = s.win_rate >= 55 ? '#81c995' : s.win_rate >= 40 ? '#e0e0e0' : '#f28b82';
        var pnlColor = s.avg_pnl_pct >= 0 ? '#81c995' : '#f28b82';
        [
          s.variant,
          s.total,
          s.entry_rate + '%',
          s.sl,
          s.tp1,
          s.tp1_be,
          s.tp2,
          s.win_rate + '%',
          (s.pnl_sum_pct >= 0 ? '+' : '') + s.pnl_sum_pct + '%',
          (s.avg_pnl_pct >= 0 ? '+' : '') + s.avg_pnl_pct + '%',
        ].forEach(function(val, i) {{
          var td = tr.insertCell();
          td.textContent = val;
          if (i === 7) td.style.color = winColor;
          if (i === 8 || i === 9) td.style.color = pnlColor;
        }});
      }});
      document.getElementById('bt-summary').style.display = 'block';
    }});
}}

// Przy załadowaniu sprawdź czy są już jakieś wyniki
fetch('/api/backtest-variants/status')
  .then(function(r) {{ return r.json(); }})
  .then(function(s) {{
    if (s.done) {{
      document.getElementById('bt-status').textContent = '✅ Ostatni backtest: ' + s.rows + ' rekordów';
      document.getElementById('bt-csv-link').style.display = 'inline';
      loadBacktestSummary();
    }} else if (s.running) {{
      document.getElementById('bt-status').textContent = '⏳ Trwa...';
      if (_btPollTimer) clearInterval(_btPollTimer);
      _btPollTimer = setInterval(pollBacktestStatus, 5000);
    }}
  }}).catch(function(){{}});

// ── Settings popover ─────────────────────────────────────────────────────────
function toggleSettings() {{
  var p = document.getElementById('settings-popover');
  p.style.display = p.style.display === 'none' ? 'block' : 'none';
}}
document.addEventListener('click', function(e) {{
  var pop = document.getElementById('settings-popover');
  if (pop.style.display !== 'none' &&
      !pop.contains(e.target) &&
      !e.target.closest('.gear-btn')) {{
    pop.style.display = 'none';
  }}
}});

// ── Market status ────────────────────────────────────────────────────────────
function fmtAgo(isoStr) {{
  if (!isoStr) return '';
  var diff = Math.round((Date.now() - new Date(isoStr).getTime()) / 60000);
  if (diff < 1)  return 'przed chwilą';
  if (diff < 60) return diff + ' min temu';
  var h = Math.floor(diff / 60);
  return h + 'h ' + (diff % 60) + 'min temu';
}}

function escHtml(s) {{
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function renderScanBlock(el, model, scan) {{
  var label = '<span style="color:#80deea;font-size:0.8em;font-weight:bold">' + model + ':</span> ';
  if (!scan || !scan.text) {{
    el.innerHTML = label + '<span style="color:#aaa;font-size:0.8em">brak danych (oczekiwanie na pierwsze skanowanie...)</span>';
    return;
  }}
  var ago = scan.time ? '<span style="color:#888;font-size:0.75em">' + fmtAgo(scan.time) + '</span> · ' : '';
  var foundBadge = scan.found
    ? '<span style="color:lightgreen;font-size:0.8em">✓ setup</span> · '
    : '<span style="color:#888;font-size:0.8em">✗ brak setupu</span> · ';

  var rawLines = (scan.text || '').trim().split('\\n');
  var htmlLines = [];
  for (var i = 0; i < rawLines.length; i++) {{
    var line = rawLines[i].trim();
    if (!line || /^=+$/.test(line) || line.indexOf('candles count') !== -1) continue;
    if (/^\[.*\] Algo2 analiza/.test(line)) continue;
    var safe = escHtml(line);
    if (line.startsWith('WYNIK:')) {{
      var hasSetup = !/:\s*0 setup/.test(line);
      htmlLines.push('<span style="color:' + (hasSetup ? 'lightgreen' : '#777') + ';font-size:0.8em;font-weight:bold">' + safe + '</span>');
    }} else if (line.startsWith('→')) {{
      var isSkip = line.indexOf('SKIP') !== -1;
      htmlLines.push('<span style="color:' + (isSkip ? '#666' : '#ffcc66') + ';font-size:0.8em">' + safe + '</span>');
    }} else if (line.startsWith('Cena:')) {{
      htmlLines.push('<span style="color:#e0e0e0;font-size:0.8em;font-weight:bold">' + safe + '</span>');
    }} else {{
      htmlLines.push('<span style="color:#999;font-size:0.78em">' + safe + '</span>');
    }}
  }}

  var body = htmlLines.length
    ? '<div style="margin-top:4px;line-height:1.65">' + htmlLines.join('<br>') + '</div>'
    : '';
  el.innerHTML = label + ago + foundBadge + body;
}}

async function loadMarketStatus() {{
  var loading = document.getElementById('ms-loading');
  loading.textContent = '↻';
  try {{
    var resp = await fetch('/api/market-status');
    var d = await resp.json();
    // Price + regime
    document.getElementById('ms-price').textContent = d.price != null ? '$' + parseFloat(d.price).toFixed(2) : '—';
    var regime = d.regime || '—';
    var regEl = document.getElementById('ms-regime');
    regEl.textContent = regime;
    var dir = (d.direction || '');
    regEl.style.color = dir === 'up' ? 'lightgreen' : dir === 'down' ? 'salmon' : '#aaa';
    var details = [];
    if (d.score != null) details.push('score:' + d.score);
    if (d.change_24h != null) details.push('24h:' + (d.change_24h >= 0 ? '+' : '') + parseFloat(d.change_24h).toFixed(1) + '%');
    document.getElementById('ms-regime-detail').textContent = details.join('  ');
    // Algo feedback (last_scans is now a dict keyed by model name)
    var scans = d.last_scans || {{}};
    var isRange = (d.regime || '').indexOf('RANGE') !== -1;
    var srSuffix = '';
    if (isRange && d.support != null && d.resistance != null) {{
      srSuffix = ' | Support: $' + parseFloat(d.support).toFixed(2) + ', Resistance: $' + parseFloat(d.resistance).toFixed(2);
    }}
    var scanAlgo2 = scans['Algo2'] ? Object.assign({{}}, scans['Algo2'], srSuffix ? {{text: (scans['Algo2'].text || '') + srSuffix}} : {{}}) : null;
    var el2 = document.getElementById('ms-scan-Algo2');
    if (el2) renderScanBlock(el2, 'Algo2', isRange && !scans['Algo2'] && srSuffix
      ? {{text: 'RANGE — brak skanowania.' + srSuffix}} : scanAlgo2);
    loading.textContent = '';
  }} catch(e) {{
    document.getElementById('ms-loading').textContent = '⚠';
  }}
}}
loadMarketStatus();
setInterval(loadMarketStatus, 60000);

// ── Default filter: exclude 'nie weszlo' ─────────────────────────────────────
document.querySelectorAll('.res-filter').forEach(function(cb) {{
  if (cb.value !== 'nie weszlo') cb.checked = true;
}});
loadHistory(true);

// ── koniec wskaźników ───────────────────────────────────────────────────────
</script>
</html>"""
    return HTMLResponse(html)


@app.get("/health")
def health():
    """Endpoint healthcheck dla Railway."""
    jobs = [{"id": j.id, "next_run": str(j.next_run_time)} for j in scheduler.get_jobs()]
    return {"status": "ok", "jobs": jobs}



@app.get("/admin/resolve-setup/{setup_id}")
def admin_resolve_setup(setup_id: int):
    """Tymczasowy endpoint do ręcznego zamknięcia setupu w bazie."""
    db.resolve_setup(setup_id, "nieokreslone", None, None, 0, None)
    return {"ok": True, "setup_id": setup_id, "result": "nieokreslone"}


@app.get("/admin/reset-entry/{setup_id}")
def admin_reset_entry(setup_id: int):
    """Resetuje entry_hit_at do NULL — cofa setup do statusu 'oczekujący'."""
    db.update_setup(
        setup_id,
        entry_hit_at=None, tp1_hit_at=None, sl_adjusted=False,
        exchange_done=False, resolved=False,
    )
    return {"ok": True, "setup_id": setup_id, "result": "entry zresetowane — setup wrócił do oczekujących"}


@app.get("/admin/force-position-open/{setup_id}")
def admin_force_position_open(setup_id: int):
    """Oznacza pozycję jako otwartą — gdy Bitget ma otwartą pozycję ale system tego nie wie.
    Exchange_trader złoży TP/SL automatycznie w ciągu 15 sekund."""
    import time
    db.update_setup(
        setup_id,
        exchange_position_opened=True,
        exchange_done=False,
        resolved=False,
        entry_hit_at=int(time.time()),
        exchange_tp1_oid=None,
        exchange_tp2_oid=None,
        exchange_sl_oid=None,
    )
    return {"ok": True, "setup_id": setup_id, "result": "pozycja oznaczona jako otwarta — exchange_trader złoży TP/SL za ~15s"}


@app.get("/admin/fix-position-qty/{setup_id}")
def admin_fix_position_qty(setup_id: int, full_qty: float):
    """Jednorazowa korekta rozmiaru TPSL dla setupu z błędnie dużą pozycją.
    MODYFIKUJE (nie anuluje) istniejące zlecenia TPSL na Bitget — zmienia tylko size.
    Bezpieczne: nie wywołuje "SL cancelled" w exchange_trader, brak kaskadowego zamknięcia.

    Przykład: /admin/fix-position-qty/25?full_qty=2.2
    """
    import math
    import exchange_trader as et

    client = et._client()
    if client is None:
        return {"error": "Brak konfiguracji BITGET"}

    # Pobierz setup
    with db._conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
    if not row:
        return {"error": f"Setup #{setup_id} nie znaleziony"}

    s    = db._row_to_dict(row)
    tps  = s.get("tps") or []
    tp1_price = float(tps[0]) if len(tps) > 0 else None
    tp2_price = float(tps[1]) if len(tps) > 1 else None
    sl_price  = float(s["sl"]) if s.get("sl") else None

    tp1_oid = s.get("exchange_tp1_oid")
    tp2_oid = s.get("exchange_tp2_oid")
    sl_oid  = s.get("exchange_sl_oid")

    # Przelicz half_qty
    qty_step = et.QTY_STEP
    half_qty = max(math.floor((full_qty / 2) / qty_step) * qty_step, qty_step)

    modified = []
    failed   = []

    # Modyfikuj istniejące TPSL — zmień tylko size, zachowaj trigger price.
    # modify-tpsl-order używa konkretnego orderId — nie wpływa na inne zlecenia.
    for label, oid, price, qty in [
        ("TP1", tp1_oid, tp1_price, half_qty),
        ("TP2", tp2_oid, tp2_price, half_qty),
        ("SL",  sl_oid,  sl_price,  full_qty),
    ]:
        if not oid or price is None:
            continue
        try:
            resp = client.post("/api/v2/mix/order/modify-tpsl-order", {
                "symbol":       et.SYMBOL,
                "productType":  et.PRODUCT_TYPE,
                "marginCoin":   et.MARGIN_COIN,
                "orderId":      oid,
                "triggerPrice": et._fmt_price(price),
                "triggerType":  "mark_price",
                "size":         et._fmt_qty(qty),
            })
            if resp.get("code") == "00000":
                modified.append(f"{label}→{qty}")
            else:
                failed.append(f"{label}:{resp.get('msg')}")
        except Exception as e:
            failed.append(f"{label}:{e}")

    # Zaktualizuj DB — tylko qty, OID pozostają bez zmian
    db.update_setup(
        setup_id,
        exchange_qty_full=f"{full_qty:.1f}",
        exchange_qty_half=f"{half_qty:.1f}",
    )

    return {
        "ok":       len(failed) == 0,
        "setup_id": setup_id,
        "full_qty": full_qty,
        "half_qty": half_qty,
        "modified": modified,
        "failed":   failed,
    }


@app.get("/admin/reopen-setup/{setup_id}")
def admin_reopen_setup(setup_id: int):
    """Przywraca błędnie zamknięty setup jako aktywny z otwartą pozycją.
    Używaj gdy pozycja jest nadal otwarta na Bitget ale setup został zamknięty przez błąd (np. race condition).
    Czyści wszystkie OID TPSL — exchange_trader złoży nowe zlecenia za ~15s."""
    import time
    # Wyczyść pola wyniku i przywróć status aktywny
    db.update_setup(
        setup_id,
        resolved=False,
        result=None,
        avg_exit=None,
        pnl_usd=None,
        pnl_pct=None,
        exit_time=None,
        resolved_at=None,
        exchange_done=False,
        exchange_position_opened=True,
        exchange_tp1_done=False,
        exchange_tp1_oid=None,
        exchange_tp2_oid=None,
        exchange_sl_oid=None,
        entry_hit_at=int(time.time()),
    )
    return {
        "ok": True,
        "setup_id": setup_id,
        "message": "Setup przywrócony jako aktywny — exchange_trader złoży TPSL za ~15s",
    }


@app.get("/admin/replace-tps/{setup_id}")
def admin_replace_tps(setup_id: int):
    """Resetuje exchange_tp1_done → False i czyści TP OID.
    Na następnym sync exchange_trader automatycznie złoży TP1 i TP2 (bez ruszania SL)."""
    db.update_setup(
        setup_id,
        exchange_tp1_oid=None,
        exchange_tp2_oid=None,
        exchange_tp1_done=False,
    )
    return {"ok": True, "setup_id": setup_id, "result": "TP zresetowane — exchange_trader złoży TP1/TP2 za ~15s (SL bez zmian)"}


@app.get("/admin/setup/{setup_id}")
def admin_get_setup(setup_id: int):
    """Zwraca pełny stan setupu z bazy — do diagnostyki."""
    rows = db.get_active_setups()
    # szukaj też w resolved
    with db._conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
    if not row:
        return {"error": f"setup #{setup_id} nie znaleziony"}
    return dict(row)


@app.post("/admin/init-sheets")
def admin_init_sheets():
    """Tworzy brakujące zakładki Google Sheets (Alerty, Wyniki_Railway, Anulowane_Grok)."""
    try:
        import sol_alert
        sol_alert._get_sheets()
        return {"ok": True, "message": "Zakładki zainicjalizowane (lub już istniały)."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/admin/run-sheets-export")
def admin_run_sheets_export():
    """Uruchamia eksport do Sheets synchronicznie i zwraca szczegółowy raport."""
    import sol_alert

    # Sprawdź czy kolumna sheets_exported istnieje
    try:
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM setups WHERE resolved = TRUE AND sheets_exported = FALSE"
                )
                pending_count = cur.fetchone()[0]
    except Exception as e:
        return {"ok": False, "stage": "db_check", "error": str(e)}

    if pending_count == 0:
        # Sprawdź ile jest w ogóle zamkniętych setupów
        with db._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM setups WHERE resolved = TRUE")
                total_resolved = cur.fetchone()[0]
        return {
            "ok": True,
            "pending_export": 0,
            "total_resolved": total_resolved,
            "message": "Brak setupów do wyeksportowania (wszystkie już wyeksportowane lub brak zamkniętych).",
        }

    unexported = db.get_unexported_resolved()
    exported_ok, exported_fail, errors = 0, 0, []

    # Otwórz arkusze raz dla całego batcha — unika rate limitingu Google Sheets API
    try:
        _, sh2 = sol_alert._get_sheets()
    except Exception as e:
        return {"ok": False, "stage": "open_sheets", "error": str(e)}

    for s in unexported:
        sid = s.get("setup_id")
        try:
            entry_ts  = s.get("entry_hit_at")
            exit_dt   = s.get("exit_time")
            exit_ts   = int(exit_dt.timestamp()) if exit_dt else None
            result    = s.get("result", "")
            avg_entry = float(s["avg_entry"]) if s.get("avg_entry") else None
            avg_exit  = float(s["avg_exit"])  if s.get("avg_exit")  else None
            move      = float(s["pnl_usd"])   if s.get("pnl_usd")   else 0.0

            if s.get("shadow"):
                ok = sol_alert.log_to_anulowane_grok(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move)
            else:
                ok = sol_alert.log_to_wyniki(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move, _sh2=sh2)

            if ok:
                db.mark_sheets_exported(sid)
                exported_ok += 1
            else:
                exported_fail += 1
                errors.append({"setup_id": sid, "error": "eksport zwrócił False — sprawdź logi Railway"})
        except Exception as e:
            exported_fail += 1
            errors.append({"setup_id": sid, "error": str(e)})

    return {
        "ok": exported_fail == 0,
        "exported": exported_ok,
        "failed": exported_fail,
        "errors": errors,
    }


@app.post("/admin/run-profit-calculator")
def admin_run_profit_calculator():
    """Uruchamia kalkulator zysku/straty i eksportuje wyniki do Google Sheets."""
    import sol_alert
    ok = sol_alert.export_profit_calculator_to_sheets()
    return {"ok": ok}


@app.get("/admin/test-candles")
def admin_test_candles():
    """Test świeżości danych z Bitget: pobiera świece i zwraca zakres dat + wiek najnowszej."""
    from datetime import datetime, timezone
    import time
    import sol_alert
    result = {}
    for interval, limit in [("15m", 5), ("1h", 5)]:
        try:
            candles = sol_alert.fetch_klines(sol_alert.SYMBOL, interval, limit=limit)
            if candles:
                newest = candles[-1]
                oldest = candles[0]
                now_ts = time.time()
                age_min = (now_ts - newest["time"]) / 60
                max_age = 90 if interval == "1h" else 30
                result[interval] = {
                    "oldest": datetime.fromtimestamp(oldest["time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "newest": datetime.fromtimestamp(newest["time"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "newest_age_min": round(age_min, 1),
                    "newest_close": newest["close"],
                    "fresh": age_min < max_age,
                }
            else:
                result[interval] = {"error": "empty response"}
        except Exception as e:
            result[interval] = {"error": str(e)}
    result["server_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return result


@app.post("/admin/reset-sheets-export")
def admin_reset_sheets_export():
    """Resetuje sheets_exported=FALSE dla wszystkich zamkniętych setupów.
    Użyj jednorazowo po naprawie buga z eksportem do Sheets."""
    with db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE setups SET sheets_exported = FALSE WHERE resolved = TRUE AND sheets_exported = TRUE"
            )
            count = cur.rowcount
    return {"ok": True, "reset_count": count, "message": f"Zresetowano {count} setupów — zostaną wyeksportowane przy następnym cyklu (co 5 min)"}


@app.get("/admin/diagnose-positions")
def admin_diagnose_positions():
    """Diagnostyka: porównuje stan DB z rzeczywistymi pozycjami i TPSL na Bitget."""
    import exchange_trader as et

    client = et._client()
    if client is None:
        return {"error": "Brak konfiguracji BITGET"}

    # ── DB: setupy nierozwiązane ──────────────────────────────────────────────
    pending = db.load_pending()
    db_summary = []
    for s in pending:
        sid      = s.get("setup_id")
        pos_open = s.get("exchange_position_opened", False)
        ex_done  = s.get("exchange_done", False)
        db_summary.append({
            "setup_id":   sid,
            "model":      s.get("model"),
            "direction":  s.get("direction"),
            "pos_open":   pos_open,
            "ex_done":    ex_done,
            "shadow":     s.get("shadow", False),
            "cancelled":  bool(s.get("cancel_reason")),
            "plan_oid":   s.get("exchange_plan_oid"),
            "tp1_oid":    s.get("exchange_tp1_oid"),
            "tp2_oid":    s.get("exchange_tp2_oid"),
            "sl_oid":     s.get("exchange_sl_oid"),
            "tp1_done":   s.get("exchange_tp1_done", False),
        })

    db_open = [s for s in db_summary if s["pos_open"] and not s["ex_done"]]

    # ── Bitget: rzeczywiste pozycje ───────────────────────────────────────────
    bitget_positions = []
    try:
        resp = client.get("/api/v2/mix/position/all-position", {
            "productType": et.PRODUCT_TYPE,
            "marginCoin":  et.MARGIN_COIN,
        })
        if resp.get("code") == "00000":
            bitget_positions = [
                {
                    "symbol":    p["symbol"],
                    "holdSide":  p["holdSide"],
                    "total":     p.get("total"),
                    "avgPrice":  p.get("openPriceAvg"),
                    "unrealPnl": p.get("unrealizedPL"),
                }
                for p in (resp.get("data") or [])
                if float(p.get("total", 0)) > 0
            ]
    except Exception as e:
        bitget_positions = [{"error": str(e)}]

    # ── Bitget: aktywne TPSL ─────────────────────────────────────────────────
    live_tpsl = []
    live_tpsl_ids = set()
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      et.SYMBOL,
            "productType": et.PRODUCT_TYPE,
            "planType":    "profit_loss",
        })
        if resp.get("code") == "00000":
            for o in (resp["data"].get("entrustedList") or []):
                live_tpsl_ids.add(o["orderId"])
                live_tpsl.append({
                    "orderId":      o["orderId"],
                    "planType":     o.get("planType"),
                    "side":         o.get("side"),
                    "triggerPrice": o.get("triggerPrice"),
                    "size":         o.get("size"),
                    "status":       o.get("planStatus"),
                })
    except Exception as e:
        live_tpsl = [{"error": str(e)}]

    # ── Bitget: aktywne plan ordery wejścia ───────────────────────────────────
    live_plan_orders = []
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      et.SYMBOL,
            "productType": et.PRODUCT_TYPE,
            "planType":    "normal_plan",
        })
        if resp.get("code") == "00000":
            for o in (resp["data"].get("entrustedList") or []):
                live_plan_orders.append({
                    "orderId":      o["orderId"],
                    "side":         o.get("side"),
                    "triggerPrice": o.get("triggerPrice"),
                    "size":         o.get("size"),
                    "status":       o.get("planStatus"),
                })
    except Exception as e:
        live_plan_orders = [{"error": str(e)}]

    # ── Analiza rozbieżności ──────────────────────────────────────────────────
    issues = []
    for s in db_open:
        sid    = s["setup_id"]
        for label, oid in [("TP1", s["tp1_oid"]), ("TP2", s["tp2_oid"]), ("SL", s["sl_oid"])]:
            if oid and oid not in live_tpsl_ids:
                issues.append({
                    "setup_id": sid,
                    "issue":    f"{label} OID w DB ale nieznaleziony wśród aktywnych TPSL na Bitget",
                    "oid":      oid,
                })
        missing_tpsl = [l for l, o in [("TP1", s["tp1_oid"]), ("TP2", s["tp2_oid"]), ("SL", s["sl_oid"])] if not o]
        if missing_tpsl and not s["tp1_done"]:
            issues.append({
                "setup_id": sid,
                "issue":    f"Pozycja otwarta w DB ale brak OID dla: {', '.join(missing_tpsl)}",
            })

    return {
        "db_pending_count":       len(pending),
        "db_open_positions":      db_open,
        "bitget_open_positions":  bitget_positions,
        "bitget_live_tpsl":       live_tpsl,
        "bitget_live_plan_orders": live_plan_orders,
        "issues":                 issues,
        "summary": {
            "db_open":        len(db_open),
            "bitget_open":    len(bitget_positions),
            "bitget_tpsl":    len(live_tpsl),
            "issue_count":    len(issues),
        },
    }


@app.get("/api/market-status")
def api_market_status():
    """Zwraca aktualny kurs SOL i reżim rynkowy."""
    import sol_alert as sa
    price = None
    reg: dict = {}
    try:
        price = sa.fetch_current_price(sa.SYMBOL)
    except Exception as e:
        log.warning(f"[market-status] price: {e}")
    try:
        m15 = sa.fetch_klines(sa.SYMBOL, "15m", 100)
        h1  = sa.fetch_klines(sa.SYMBOL, "1h", 50)
        reg = sa.detect_market_regime(m15, h1, price or 0) or {}
    except Exception as e:
        log.warning(f"[market-status] regime: {e}")
    return {
        "price":      price,
        "regime":     reg.get("regime"),
        "direction":  reg.get("direction"),
        "score":      reg.get("score"),
        "change_24h":  reg.get("change_24h"),
        "change_48h":  reg.get("change_48h"),
        "support":     reg.get("support"),
        "resistance":  reg.get("resistance"),
        "last_scans":  _get_last_scans(),
    }


def _get_last_scans() -> dict:
    """Zwraca feedback z ostatniego uruchomienia Algo2 i Grok (z sol_alert._last_feedback)."""
    try:
        import sol_alert as sa
        return dict(sa._last_feedback)
    except Exception as e:
        log.warning(f"[market-status] last_scans: {e}")
        return {}


@app.get("/api/bitget-live")
def api_bitget_live():
    """Zwraca live dane z Bitget: otwarte pozycje, aktywne TPSL i plan ordery.
    Używane przez dashboard JS do wypełnienia kolumn qtP/qtTP1/qtTP2/qtSL."""
    import exchange_trader as et

    client = et._client()
    if client is None:
        return {"error": "no_bitget"}

    tpsl_by_id: dict = {}
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol": et.SYMBOL, "productType": et.PRODUCT_TYPE, "planType": "profit_loss",
        })
        if resp.get("code") == "00000":
            for o in (resp["data"].get("entrustedList") or []):
                tpsl_by_id[o["orderId"]] = {
                    "size":         o.get("size"),
                    "planType":     o.get("planType"),
                    "triggerPrice": o.get("triggerPrice"),
                }
    except Exception as e:
        log.warning(f"[bitget-live] TPSL: {e}")

    plan_by_id: dict = {}
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol": et.SYMBOL, "productType": et.PRODUCT_TYPE, "planType": "normal_plan",
        })
        if resp.get("code") == "00000":
            for o in (resp["data"].get("entrustedList") or []):
                plan_by_id[o["orderId"]] = {
                    "size":         o.get("size"),
                    "triggerPrice": o.get("triggerPrice"),
                }
    except Exception as e:
        log.warning(f"[bitget-live] plans: {e}")

    positions: dict = {}
    try:
        resp = client.get("/api/v2/mix/position/all-position", {
            "productType": et.PRODUCT_TYPE, "marginCoin": et.MARGIN_COIN,
        })
        if resp.get("code") == "00000":
            for p in (resp.get("data") or []):
                if float(p.get("total", 0)) > 0:
                    positions[p["holdSide"]] = {
                        "total":    p.get("total"),
                        "avgPrice": p.get("openPriceAvg"),
                        "unrealPnl": p.get("unrealizedPL"),
                    }
    except Exception as e:
        log.warning(f"[bitget-live] positions: {e}")

    return {"tpsl": tpsl_by_id, "plans": plan_by_id, "positions": positions}


@app.get("/api/stats")
def api_stats():
    """JSON API dla przyszłej integracji z Metabase."""
    return {
        "summary":     db.get_summary_stats(),
        "active":      db.get_active_setups(),
        "recent":      db.get_recent_resolved(50),
    }


@app.get("/api/resolved")
def api_resolved(
    results: str | None = None,
    models:  str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """Zamknięte setupy z filtrami. results/models = listy oddzielone przecinkami."""
    result_list = [r.strip() for r in results.split(",") if r.strip()] if results else None
    model_list  = [m.strip() for m in models.split(",")  if m.strip()] if models  else None
    data = db.get_resolved_filtered(result_list, date_from, date_to, min(limit, 200), offset, model_list)
    return data


@app.get("/api/resolved/csv")
def api_resolved_csv(
    results: str | None = None,
    models:  str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Eksport wyfiltrowanych setupów do CSV."""
    from fastapi.responses import Response
    import csv
    import io

    result_list = [r.strip() for r in results.split(",") if r.strip()] if results else None
    model_list  = [m.strip() for m in models.split(",")  if m.strip()] if models  else None
    data = db.get_resolved_filtered(result_list, date_from, date_to, limit=5000, offset=0, models=model_list)
    rows = data["rows"]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ID", "Data", "Model", "Kierunek", "Wynik", "Wejście", "Wyjście",
        "PnL $", "PnL %", "Hypo wynik", "Hypo PnL $",
    ])
    for s in rows:
        writer.writerow([
            s.get("setup_id"),
            str(s.get("alert_time", ""))[:19],
            s.get("model"),
            s.get("direction"),
            s.get("result"),
            s.get("avg_entry") or "",
            s.get("avg_exit") or "",
            s.get("pnl_usd") or "",
            s.get("pnl_pct") or "",
            s.get("hypo_result") or "",
            s.get("hypo_pnl_usd") or "",
        ])

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=alertsol_export.csv"},
    )


class ResultUpdate(BaseModel):
    result: str
    avg_exit: float | None = None
    avg_entry: float | None = None


class TpsUpdate(BaseModel):
    tp1: float | None = None
    tp2: float | None = None
    sl:  float | None = None


class SettingsUpdate(BaseModel):
    trade_usdt: float | None = None
    alert_interval: int | None = None
    max_positions: int | None = None


@app.post("/api/update-tps/{setup_id}")
def api_update_tps(setup_id: int, body: TpsUpdate):
    """Modyfikuje TP1 i/lub TP2 aktywnego setupu — aktualizuje DB i zlecenia na Bitget."""
    import exchange_trader as et

    with db._conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Setup #{setup_id} nie znaleziony")

    s = db._row_to_dict(row)
    if s.get("resolved"):
        raise HTTPException(status_code=400, detail="Setup jest już zamknięty")

    tps = list(s.get("tps") or [])
    tp1_new = body.tp1
    tp2_new = body.tp2

    modified = []
    failed = []

    client = et._client()

    # Modify TP1 on Bitget if order exists
    tp1_oid = s.get("exchange_tp1_oid")
    if tp1_new is not None:
        tp1_ok = False
        if tp1_oid and client:
            half_qty_str = s.get("exchange_qty_half")
            half_qty = float(half_qty_str) if half_qty_str else None
            try:
                params = {
                    "symbol":       et.SYMBOL,
                    "productType":  et.PRODUCT_TYPE,
                    "marginCoin":   et.MARGIN_COIN,
                    "orderId":      tp1_oid,
                    "triggerPrice": et._fmt_price(tp1_new),
                    "triggerType":  "mark_price",
                }
                if half_qty:
                    params["size"] = et._fmt_qty(half_qty)
                resp = client.post("/api/v2/mix/order/modify-tpsl-order", params)
                if resp.get("code") == "00000":
                    modified.append(f"TP1→{tp1_new}")
                    tp1_ok = True
                else:
                    failed.append(f"TP1:{resp.get('msg')}")
            except Exception as e:
                failed.append(f"TP1:{e}")
        else:
            modified.append(f"TP1→{tp1_new} (tylko DB)")
            tp1_ok = True
        # Aktualizuj lokalne tps tylko jeśli Bitget przyjął zmianę
        if tp1_ok:
            if len(tps) > 0:
                tps[0] = tp1_new
            elif len(tps) == 0:
                tps = [tp1_new]

    # Modify TP2 on Bitget if order exists
    tp2_oid = s.get("exchange_tp2_oid")
    if tp2_new is not None:
        tp2_ok = False
        if tp2_oid and client:
            half_qty_str = s.get("exchange_qty_half")
            half_qty = float(half_qty_str) if half_qty_str else None
            try:
                params = {
                    "symbol":       et.SYMBOL,
                    "productType":  et.PRODUCT_TYPE,
                    "marginCoin":   et.MARGIN_COIN,
                    "orderId":      tp2_oid,
                    "triggerPrice": et._fmt_price(tp2_new),
                    "triggerType":  "mark_price",
                }
                if half_qty:
                    params["size"] = et._fmt_qty(half_qty)
                resp = client.post("/api/v2/mix/order/modify-tpsl-order", params)
                if resp.get("code") == "00000":
                    modified.append(f"TP2→{tp2_new}")
                    tp2_ok = True
                else:
                    failed.append(f"TP2:{resp.get('msg')}")
            except Exception as e:
                failed.append(f"TP2:{e}")
        else:
            modified.append(f"TP2→{tp2_new} (tylko DB)")
            tp2_ok = True
        # Aktualizuj lokalne tps tylko jeśli Bitget przyjął zmianę
        if tp2_ok:
            if len(tps) > 1:
                tps[1] = tp2_new
            elif len(tps) == 1:
                tps.append(tp2_new)
            else:
                tps = [None, tp2_new]

    # Modify SL on Bitget if order exists
    sl_new   = body.sl
    sl_oid   = s.get("exchange_sl_oid")
    tp1_done = s.get("exchange_tp1_done", False)
    if sl_new is not None:
        sl_ok = False
        if sl_oid and client:
            # Rozmiar SL: po TP1 jest już half_qty, przed TP1 — full_qty.
            # Zachowujemy istniejący rozmiar (modify nie wymaga size jeśli się nie zmienia).
            try:
                resp = client.post("/api/v2/mix/order/modify-tpsl-order", {
                    "symbol":       et.SYMBOL,
                    "productType":  et.PRODUCT_TYPE,
                    "marginCoin":   et.MARGIN_COIN,
                    "orderId":      sl_oid,
                    "triggerPrice": et._fmt_price(sl_new),
                    "triggerType":  "mark_price",
                })
                if resp.get("code") == "00000":
                    modified.append(f"SL→{sl_new}")
                    sl_ok = True
                else:
                    failed.append(f"SL:{resp.get('msg')}")
            except Exception as e:
                failed.append(f"SL:{e}")
        else:
            modified.append(f"SL→{sl_new} (tylko DB)")
            sl_ok = True
        # Aktualizuj DB SL tylko jeśli Bitget przyjął zmianę — zapobiega fałszywemu
        # wykryciu "SL hit" przez monitor gdy Bitget nadal ma stary poziom SL.
        if sl_ok:
            if tp1_done:
                db.update_setup(setup_id, sl_after_tp1=sl_new)
            else:
                db.update_setup(setup_id, sl=sl_new)

    if modified:
        db.update_setup(setup_id, tps=tps)

    return {
        "ok":       len(failed) == 0,
        "setup_id": setup_id,
        "modified": modified,
        "failed":   failed,
        "tps":      tps,
    }


@app.post("/api/cancel-setup/{setup_id}")
def api_cancel_setup(setup_id: int):
    """Anuluje setup:
    - czekający na wejście → anuluje plan order
    - w pozycji → anuluje TP1/TP2/SL i zamyka część pozycji tego setupu (market)
    Zawsze zamyka setup w DB jako 'anulowany'."""
    import exchange_trader as et

    with db._conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Setup #{setup_id} nie znaleziony")

    s = db._row_to_dict(row)
    if s.get("resolved"):
        raise HTTPException(status_code=400, detail="Setup jest już zamknięty")

    client = et._client()
    cancelled_on_bitget = []
    failed_on_bitget = []

    plan_oid  = s.get("exchange_plan_oid")
    tp1_oid   = s.get("exchange_tp1_oid")
    tp2_oid   = s.get("exchange_tp2_oid")
    sl_oid    = s.get("exchange_sl_oid")
    pos_open  = s.get("exchange_position_opened", False)
    tp1_done  = s.get("exchange_tp1_done", False)
    direction = s.get("direction", "long")

    # Qty do zamknięcia: jeśli TP1 już trafiony — połowa (SL jest już na half_qty),
    # w przeciwnym razie — pełna pozycja setupu.
    try:
        full_qty = float(s["exchange_qty_full"]) if s.get("exchange_qty_full") else None
        half_qty = float(s["exchange_qty_half"]) if s.get("exchange_qty_half") else None
    except (ValueError, TypeError):
        full_qty = half_qty = None
    close_qty = (half_qty if tp1_done and half_qty else full_qty) if pos_open else None

    close_price = None  # przybliżona cena wyjścia do PnL

    if client:
        # ── Setup czekający na wejście — anuluj plan order ─────────────────────
        if plan_oid and not pos_open:
            try:
                resp = client.post("/api/v2/mix/order/cancel-plan-order", {
                    "symbol":      et.SYMBOL,
                    "productType": et.PRODUCT_TYPE,
                    "orderId":     plan_oid,
                    "planType":    "normal_plan",
                })
                if resp.get("code") == "00000":
                    cancelled_on_bitget.append("plan_order")
                else:
                    failed_on_bitget.append(f"plan_order:{resp.get('msg')}")
            except Exception as e:
                failed_on_bitget.append(f"plan_order:{e}")

        # ── Pozycja otwarta — anuluj TPSL, potem zamknij rynkowo ──────────────
        if pos_open:
            for label, oid, plan_type in [
                ("TP1", tp1_oid, "profit_plan"),
                ("TP2", tp2_oid, "profit_plan"),
                ("SL",  sl_oid,  "loss_plan"),
            ]:
                if oid:
                    try:
                        resp = client.post("/api/v2/mix/order/cancel-plan-order", {
                            "symbol":      et.SYMBOL,
                            "productType": et.PRODUCT_TYPE,
                            "orderId":     oid,
                            "planType":    plan_type,
                        })
                        if resp.get("code") == "00000":
                            cancelled_on_bitget.append(label)
                        else:
                            failed_on_bitget.append(f"{label}:{resp.get('msg')}")
                    except Exception as e:
                        failed_on_bitget.append(f"{label}:{e}")

            # Pobierz bieżącą cenę mark przed market close (do szacowania PnL)
            try:
                ticker = client.get("/api/v2/mix/market/ticker", {
                    "symbol":      et.SYMBOL,
                    "productType": et.PRODUCT_TYPE,
                })
                if ticker.get("code") == "00000":
                    close_price = float((ticker.get("data") or [{}])[0].get("markPrice") or 0) or None
            except Exception:
                pass

            # Market close order — zmniejsza pozycję o qty tego setupu
            if close_qty and close_qty > 0:
                close_side = "sell" if direction == "long" else "buy"
                try:
                    resp = client.post("/api/v2/mix/order/place-order", {
                        "symbol":      et.SYMBOL,
                        "productType": et.PRODUCT_TYPE,
                        "marginCoin":  et.MARGIN_COIN,
                        "side":        close_side,
                        "tradeSide":   "close",
                        "posSide":     direction,
                        "orderType":   "market",
                        "size":        et._fmt_qty(close_qty),
                    })
                    if resp.get("code") == "00000":
                        cancelled_on_bitget.append(f"market_close({et._fmt_qty(close_qty)} SOL)")
                    else:
                        failed_on_bitget.append(f"market_close:{resp.get('msg')}")
                except Exception as e:
                    failed_on_bitget.append(f"market_close:{e}")

    # Oblicz przybliżone PnL jeśli mamy cenę zamknięcia
    pnl_usd = None
    avg_entry = float(s["avg_entry"]) if s.get("avg_entry") else None
    if close_price and avg_entry and close_qty:
        sign    = 1 if direction == "long" else -1
        pnl_usd = round(sign * close_qty * (close_price - avg_entry), 2)

    db.resolve_setup(setup_id, "anulowany", avg_entry, close_price, pnl_usd, None)
    db.update_setup(setup_id, exchange_done=True, cancel_reason="manual")

    return {
        "ok":                  True,
        "setup_id":            setup_id,
        "close_qty":           close_qty,
        "close_price":         close_price,
        "pnl_usd":             pnl_usd,
        "cancelled_on_bitget": cancelled_on_bitget,
        "failed_on_bitget":    failed_on_bitget,
        "message":             "Setup anulowany" + (f" (błędy Bitget: {failed_on_bitget})" if failed_on_bitget else ""),
    }


@app.post("/api/update-result/{setup_id}")
def api_update_result(setup_id: int, body: ResultUpdate):
    """Ręczna korekta wyniku i PnL zamkniętego setupu."""
    VALID_RESULTS = {"TP1", "TP2", "TP1+BE", "TP1+SL", "SL", "nieokreslone", "nie weszlo", "anulowany"}
    if body.result not in VALID_RESULTS:
        raise HTTPException(status_code=400, detail=f"Nieprawidłowy wynik: {body.result}")

    with db._conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Setup #{setup_id} nie znaleziony")

    s         = db._row_to_dict(row)
    avg_exit  = body.avg_exit
    pnl_usd   = None
    # Użyj avg_entry z body (ręczne wpisanie) albo z bazy
    avg_entry = body.avg_entry if body.avg_entry is not None else (
        float(s["avg_entry"]) if s.get("avg_entry") else None
    )

    if avg_entry and body.result in ("TP1", "TP2", "TP1+BE", "TP1+SL", "SL"):
        direction = s.get("direction", "long")
        sign      = 1 if direction == "long" else -1

        # Wyznacz qty
        try:
            full_qty = float(s["exchange_qty_full"]) if s.get("exchange_qty_full") else None
            half_qty = float(s["exchange_qty_half"]) if s.get("exchange_qty_half") else None
        except (ValueError, TypeError):
            full_qty = half_qty = None

        if not full_qty:
            trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
            full_qty = max(math.floor((trade_usdt * 20 / avg_entry) / 0.1) * 0.1, 0.1)
        if not half_qty:
            half_qty = max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)

        tps_list  = s.get("tps") or []
        tp1_setup = float(tps_list[0]) if tps_list else None

        if body.result in ("SL", "TP1", "TP2") and avg_exit is not None:
            pnl_usd = sign * full_qty * (avg_exit - avg_entry)
        elif body.result == "TP1+BE" and tp1_setup:
            # Pierwsza połowa wychodzi na TP1, druga na BE (avg_entry) → PnL tylko z TP1
            pnl_usd = sign * half_qty * (tp1_setup - avg_entry)
        elif body.result == "TP1+SL" and tp1_setup:
            # Pierwsza połowa na TP1, druga na SL (avg_exit)
            sl_pnl  = sign * half_qty * (avg_exit - avg_entry) if avg_exit is not None else 0
            pnl_usd = sign * half_qty * (tp1_setup - avg_entry) + sl_pnl

    db.resolve_setup(setup_id, body.result, avg_entry, avg_exit, pnl_usd, None)
    return {
        "ok":       True,
        "setup_id": setup_id,
        "result":   body.result,
        "avg_entry": avg_entry,
        "avg_exit":  avg_exit,
        "pnl_usd":  round(pnl_usd, 2) if pnl_usd is not None else None,
    }


@app.get("/api/settings")
def api_get_settings():
    """Zwraca aktualne ustawienia systemu."""
    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
    max_positions = int(os.getenv("BITGET_MAX_POSITIONS", "5"))
    # Alert interval: extract from scheduler job
    alert_minutes = 15
    try:
        job = scheduler.get_job("sol_alert")
        if job and hasattr(job.trigger, "fields"):
            for f in job.trigger.fields:
                if f.name == "minute":
                    expr = str(f)
                    parts = expr.split(",")
                    if len(parts) >= 2:
                        alert_minutes = int(parts[1]) - int(parts[0])
    except Exception:
        pass
    return {
        "trade_usdt": trade_usdt,
        "alert_interval": alert_minutes,
        "max_positions": max_positions,
    }


@app.post("/api/settings")
def api_update_settings(body: SettingsUpdate):
    """Aktualizuje ustawienia systemu w runtime (env vars + scheduler)."""
    updated = []

    if body.trade_usdt is not None and body.trade_usdt > 0:
        os.environ["BITGET_TRADE_USDT"] = str(body.trade_usdt)
        try:
            import exchange_trader
            exchange_trader.TRADE_USDT = body.trade_usdt
        except Exception:
            pass
        try:
            import sol_alert
            sol_alert.TRADE_USDT = body.trade_usdt
        except Exception:
            pass
        updated.append(f"trade_usdt={body.trade_usdt}")

    if body.max_positions is not None and body.max_positions > 0:
        os.environ["BITGET_MAX_POSITIONS"] = str(body.max_positions)
        # Update exchange_trader module-level var if already imported
        try:
            import exchange_trader
            exchange_trader.MAX_POSITIONS = body.max_positions
        except Exception:
            pass
        updated.append(f"max_positions={body.max_positions}")

    if body.alert_interval is not None and body.alert_interval > 0:
        minutes = body.alert_interval
        # Build cron minute expression: 0, N, 2N, ... < 60
        cron_parts = [str(m) for m in range(0, 60, minutes)]
        cron_expr = ",".join(cron_parts)
        try:
            scheduler.reschedule_job(
                "sol_alert",
                trigger=CronTrigger(minute=cron_expr),
            )
            updated.append(f"alert_interval={minutes}min (cron: {cron_expr})")
        except Exception as e:
            updated.append(f"alert_interval=BŁĄD: {e}")

    return {"ok": True, "updated": updated}


@app.get("/api/period-stats")
def api_period_stats(period: str = "24h"):
    """Statystyki za okres: 1d, 24h, 7d, 30d."""
    if period not in ("1d", "24h", "7d", "30d"):
        raise HTTPException(status_code=400, detail="Dozwolone okresy: 1d, 24h, 7d, 30d")
    return db.get_period_stats(period)


@app.get("/api/algo2/type-stats")
def api_algo2_type_stats(period: int | None = None):
    """Statystyki per typ setupu dla Algo2. period = liczba dni (np. 7, 30) lub brak = all-time."""
    return db.get_algo2_type_stats(period)


@app.get("/api/algo2/time-heatmap")
def api_algo2_time_heatmap(period: int | None = None):
    """Heatmapa godzinowa alertów Algo2 (czas Warsaw). period = liczba dni lub brak = all-time."""
    return db.get_algo2_time_heatmap(period)


@app.get("/api/algo2/rr-analysis")
def api_algo2_rr_analysis(period: int | None = None):
    """Analiza RR dla Algo2: deklarowany RR vs TP1/TP2 hit rate. period = liczba dni lub brak = all-time."""
    return db.get_algo2_rr_analysis(period)


@app.get("/api/algo2/variant-stats")
def api_algo2_variant_stats(period: int | None = None, _: None = Security(_require_api_key)):
    """Porównanie wariantów kalibracji dla trend_pullback_long/short. period = dni lub brak = all-time."""
    return db.get_algo2_variant_stats(period)


@app.get("/api/analytics/export")
def api_analytics_export(
    days: int | None = None,
    variant: str | None = None,
    type_filter: str | None = None,
    _: None = Security(_require_api_key),
):
    """Eksport surowych danych setupów do analizy.

    Parametry:
      days        — ostatnie N dni (brak = wszystkie)
      variant     — filtr po wariancie (np. 'baseline', 'shallow', 'str4')
      type_filter — filtr po typie setupu (np. 'trend_pullback_long')

    Zwraca JSON z polami kluczowymi dla każdego setupu.
    """
    with db._conn() as conn:
        with db._conn().__class__() if False else conn.cursor(
            cursor_factory=__import__("psycopg2").extras.RealDictCursor
        ) as cur:
            where = ["model = 'Algo2'"]
            params: dict = {}
            if days:
                where.append("alert_time >= NOW() - %(interval)s::interval")
                params["interval"] = f"{days} days"
            if variant:
                where.append("COALESCE(variant, 'baseline') = %(variant)s")
                params["variant"] = variant
            if type_filter:
                where.append("type = %(type_filter)s")
                params["type_filter"] = type_filter
            where_sql = " AND ".join(where)
            cur.execute(
                f"""
                SELECT
                    setup_id, alert_time, type, direction,
                    COALESCE(variant, 'baseline') AS variant,
                    entries, tps, sl, sl_after_tp1, rr,
                    entry_hit_at, result, pnl_usd, pnl_pct,
                    avg_entry, avg_exit,
                    hypo_result, hypo_pnl_usd,
                    shadow, resolved, score,
                    EXTRACT(EPOCH FROM alert_time)::bigint AS alert_ts,
                    CASE WHEN entry_hit_at IS NOT NULL AND alert_timestamp IS NOT NULL
                         THEN ROUND((entry_hit_at - alert_timestamp) / 3600.0, 2) END AS hours_to_entry,
                    CASE WHEN exit_time IS NOT NULL AND entry_hit_at IS NOT NULL
                         THEN ROUND(EXTRACT(EPOCH FROM exit_time - to_timestamp(entry_hit_at)) / 3600.0, 2)
                    END AS hold_hours
                FROM setups
                WHERE {where_sql}
                ORDER BY alert_time DESC
                LIMIT 2000
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "rows": rows}


_backtest_variants_status: dict = {"running": False, "done": False, "error": None, "rows": 0, "started_at": None}
_BACKTEST_VARIANTS_CSV = "/tmp/backtest_variants_result.csv"


@app.post("/admin/run-backtest-variants")
def admin_run_backtest_variants(days: int = 60):
    """Uruchamia backtest wariantów trend_pullback w tle (Railway).

    Parametry query:
      days — liczba dni historii (domyślnie 60)

    Wyniki dostępne pod GET /api/backtest-variants/result (JSON)
    i GET /api/backtest-variants/csv (plik CSV).
    """
    global _backtest_variants_status
    if _backtest_variants_status["running"]:
        return {"ok": False, "message": "Backtest już działa — poczekaj na zakończenie."}

    import threading
    import backtest_variants

    def _run():
        global _backtest_variants_status
        _backtest_variants_status = {
            "running": True, "done": False, "error": None, "rows": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            backtest_variants.run_backtest(days=days, out_path=_BACKTEST_VARIANTS_CSV)
            import csv as _csv
            with open(_BACKTEST_VARIANTS_CSV, newline="", encoding="utf-8") as f:
                rows = sum(1 for _ in _csv.DictReader(f))
            _backtest_variants_status.update({"running": False, "done": True, "rows": rows})
        except Exception as e:
            logging.error(f"[backtest-variants] Błąd: {e}", exc_info=True)
            _backtest_variants_status.update({"running": False, "done": False, "error": str(e)})

    threading.Thread(target=_run, daemon=True).start()
    return {
        "ok": True,
        "message": f"Backtest wariantów uruchomiony w tle ({days} dni). Sprawdź status: GET /api/backtest-variants/status",
    }


@app.get("/api/backtest-variants/status")
def api_backtest_variants_status(_: None = Security(_require_api_key)):
    """Status ostatniego uruchomienia backtestów wariantów."""
    return _backtest_variants_status


@app.get("/api/backtest-variants/result")
def api_backtest_variants_result(variant: str | None = None, limit: int = 2000, _: None = Security(_require_api_key)):
    """Wyniki backtestów wariantów jako JSON.

    Parametry query:
      variant — filtr po wariancie (np. 'baseline', 'shallow', 'str4')
      limit   — max liczba wierszy (domyślnie 2000)
    """
    import csv as _csv
    import os
    if not os.path.exists(_BACKTEST_VARIANTS_CSV):
        return {"error": "Brak wyników — uruchom najpierw POST /admin/run-backtest-variants"}
    rows = []
    with open(_BACKTEST_VARIANTS_CSV, newline="", encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if variant and row.get("variant") != variant:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break

    # Agregaty per wariant
    from collections import defaultdict
    agg: dict = defaultdict(lambda: {"total": 0, "entered": 0, "sl": 0, "tp1": 0,
                                      "tp1_be": 0, "tp2": 0, "pnl_sum": 0.0})
    for r in rows:
        v = r["variant"]
        agg[v]["total"] += 1
        if r.get("entered") == "True":
            agg[v]["entered"] += 1
            res = r.get("result", "")
            if res == "SL":                   agg[v]["sl"]     += 1
            elif res == "TP1":                agg[v]["tp1"]    += 1
            elif res == "TP2":                agg[v]["tp2"]    += 1
            elif res in ("TP1+BE", "TP1+SL"): agg[v]["tp1_be"] += 1
            try:
                agg[v]["pnl_sum"] += float(r.get("pnl_pct") or 0)
            except ValueError:
                pass

    summary = []
    for vname, s in sorted(agg.items()):
        entered = s["entered"]
        wins    = s["tp1"] + s["tp2"] + s["tp1_be"]
        summary.append({
            "variant":    vname,
            "total":      s["total"],
            "entered":    entered,
            "entry_rate": round(s["entered"] / s["total"] * 100, 1) if s["total"] else 0,
            "sl":         s["sl"],
            "tp1":        s["tp1"],
            "tp1_be":     s["tp1_be"],
            "tp2":        s["tp2"],
            "win_rate":   round(wins / entered * 100, 1) if entered else 0,
            "pnl_sum_pct": round(s["pnl_sum"], 1),
            "avg_pnl_pct": round(s["pnl_sum"] / entered, 2) if entered else 0,
        })

    return {"summary": summary, "rows": rows[:limit]}


@app.get("/api/backtest-variants/csv")
def api_backtest_variants_csv():
    """Pobierz surowy CSV z wynikami backtestów wariantów."""
    import os
    from fastapi.responses import FileResponse
    if not os.path.exists(_BACKTEST_VARIANTS_CSV):
        return {"error": "Brak wyników — uruchom najpierw POST /admin/run-backtest-variants"}
    return FileResponse(
        _BACKTEST_VARIANTS_CSV,
        media_type="text/csv",
        filename="backtest_variants_result.csv",
    )


@app.post("/admin/run-gpt5-backtest")
def admin_run_gpt5_backtest():
    """Uruchamia backtest GPT5 (vision: wykresy PNG) w tle. Wyniki: arkusz 'GPT5 test'."""
    import threading
    import gpt5_backtest

    def _run():
        try:
            gpt5_backtest.run_backtest()
        except Exception as e:
            logging.error(f"[gpt5-backtest] Błąd: {e}", exc_info=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "message": "Backtest GPT5 uruchomiony w tle. Wyniki pojawią się w arkuszu 'GPT5 test' (~60-90 min)."}


@app.post("/admin/run-gpt4-backtest")
def admin_run_gpt4_backtest():
    """Uruchamia backtest GPT4 w tle. Wyniki trafiają do arkusza 'GPT4 test'."""
    import threading
    import gpt4_backtest

    def _run():
        try:
            gpt4_backtest.run_backtest()
        except Exception as e:
            logging.error(f"[gpt4-backtest] Błąd: {e}", exc_info=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "message": "Backtest GPT4 uruchomiony w tle. Wyniki pojawią się w arkuszu 'GPT4 test' (~30-60 min)."}


@app.post("/admin/run-gpt3-backtest")
def admin_run_gpt3_backtest():
    """Uruchamia backtest GPT3 w tle. Wyniki trafiają do arkusza 'GPT3 test'."""
    import threading
    import gpt3_backtest

    def _run():
        try:
            gpt3_backtest.run_backtest()
        except Exception as e:
            logging.error(f"[gpt3-backtest] Błąd: {e}", exc_info=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "message": "Backtest GPT3 uruchomiony w tle. Wyniki pojawią się w arkuszu 'GPT3 test' (~30-60 min)."}


@app.post("/admin/run-gpt-relaxed-backtest")
def admin_run_gpt_relaxed_backtest():
    """Uruchamia backtest GPT-Relaxed (web search) w tle. Wyniki: arkusz 'GPT-Relaxed test'."""
    import threading
    import gpt_relaxed_backtest

    def _run():
        try:
            gpt_relaxed_backtest.run_backtest()
        except Exception as e:
            logging.error(f"[gpt-relaxed-backtest] Błąd: {e}", exc_info=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return {"ok": True, "message": "Backtest GPT-Relaxed uruchomiony w tle. Wyniki pojawią się w arkuszu 'GPT-Relaxed test' (~60-90 min)."}


# ── Uruchomienie ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))

    def _shutdown(sig, frame):
        log.info(f"Sygnał {sig} — zatrzymywanie...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(f"Startuję na porcie {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)
