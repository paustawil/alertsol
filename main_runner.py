#!/usr/bin/env python3
"""
main_runner.py — główny proces Railway dla AlertSol

Uruchamia 3 zadania w tle:
  1. exchange_monitor  — co 15 sekund (natychmiastowa reakcja na order fills)
  2. sol_alert_job     — co 15 minut (wykrywanie setupów)
  3. sheets_export_job — co 5 minut (eksport zamkniętych setupów do Google Sheets)

+ FastAPI web dashboard dostępny pod URL przydzielonym przez Railway.
"""

import html as _html
import json as _json
import logging
import math
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
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
                if s.get("shadow"):
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

    # Sol alert — co 15 minut (minuty 0, 15, 30, 45)
    scheduler.add_job(
        run_sol_alert,
        CronTrigger(minute="0,15,30,45"),
        id="sol_alert",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
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

    scheduler.start()
    log.info("Scheduler uruchomiony. exchange: co 15s | sol_alert: co 15min | sheets: co 5min")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    log.info("Scheduler zatrzymany.")


app = FastAPI(title="AlertSol Dashboard", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Prosta strona HTML z aktywnymi setupami i statystykami."""
    try:
        active = db.get_active_setups()
        stats  = db.get_summary_stats()
        recent = db.get_recent_resolved(20)
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
            status = "✅ po TP1"
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
        active_rows += (
            f'<tr data-sid="{sid}" data-plan-oid="{plan_oid}" '
            f'data-tp1-oid="{tp1_oid}" data-tp2-oid="{tp2_oid}" data-sl-oid="{sl_oid}" '
            f'data-qty-full="{qty_full}" data-pos-open="{pos_open}" data-tp1-done="{tp1_done}">'
            f"<td>#{sid}</td><td>{s['model']}</td>"
            f"<td>{s['direction'].upper()}</td><td>{status}</td>"
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
            f'<td class="qt-p"  id="qp-{sid}"><span class="qt-loading">…</span></td>'
            f'<td class="qt-tp1" id="qt1-{sid}"><span class="qt-loading">…</span></td>'
            f'<td class="qt-tp2" id="qt2-{sid}"><span class="qt-loading">…</span></td>'
            f'<td class="qt-sl"  id="qsl-{sid}"><span class="qt-loading">…</span></td>'
            f'<td style="white-space:nowrap">'
            f'<button class="av-view btn-edit" onclick="editActiveTp(this)">Zmień TP</button>'
            f'<button class="av-edit btn-action" onclick="saveActiveTp(this)">Zapisz</button>'
            f'<button class="av-edit btn-action" onclick="cancelActiveTpEdit(this)">Zamknij</button>'
            f' <button class="btn-action" style="color:#ffaaaa;border-color:#884444" onclick="cancelActiveSetup(this)">Anuluj setup</button>'
            f'</td>'
            f'</tr>\n'
        )

    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))

    RESULT_LABELS = {
        "TP1": "TP1", "TP2": "TP2", "TP1+BE": "TP1+BE", "SL": "SL",
        "nieokreslone": "Nieokreślone", "nie weszlo": "Nie weszło", "anulowany": "Anulowane",
    }
    RESULT_OPTS = [
        ("TP1","TP1"), ("TP2","TP2"), ("TP1+BE","TP1+BE"), ("SL","SL"),
        ("nieokreslone","Nieokreślone"), ("nie weszlo","Nie weszło"), ("anulowany","Anulowane"),
    ]

    history_rows = ""
    for s in recent:
        sid        = s["setup_id"]
        result_val = s.get("result") or ""
        pnl_val    = float(s["pnl_usd"]) if s.get("pnl_usd") is not None else None
        pnl_str    = f"{pnl_val:+.2f}" if pnl_val is not None else "—"
        pnl_color  = "lightgreen" if pnl_val and pnl_val > 0 else ("gray" if pnl_val is None else "salmon")
        avg_entry  = float(s["avg_entry"]) if s.get("avg_entry") else None
        avg_exit_v = float(s["avg_exit"])  if s.get("avg_exit")  else None
        tps        = s.get("tps") or []
        entries_list = s.get("entries") or []
        w1           = float(entries_list[0]) if entries_list else None

        TRADING_RESULTS = {"TP1", "TP2", "TP1+BE", "SL"}
        # Wejście: pokaż W1 tylko dla rzeczywistych transakcji
        if avg_entry:
            avg_entry_str = f"{avg_entry:.2f}"
        elif result_val in TRADING_RESULTS and w1:
            avg_entry_str = f"{w1:.2f}"
        else:
            avg_entry_str = "—"
        avg_exit_str      = f"{avg_exit_v:.2f}" if avg_exit_v is not None else "—"
        avg_exit_inp_val  = f"{avg_exit_v:.2f}" if avg_exit_v is not None else ""

        # Qty — z bazy lub oszacowane
        try:
            full_qty = float(s["exchange_qty_full"]) if s.get("exchange_qty_full") else None
            half_qty = float(s["exchange_qty_half"]) if s.get("exchange_qty_half") else None
        except (ValueError, TypeError):
            full_qty = half_qty = None
        entry_for_calc = avg_entry or (w1 if result_val in TRADING_RESULTS else None)
        if not full_qty and entry_for_calc:
            full_qty = max(math.floor((trade_usdt * 20 / entry_for_calc) / 0.1) * 0.1, 0.1)
        if not half_qty and full_qty:
            half_qty = max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)

        # PnL$ — z bazy lub obliczony on-the-fly gdy pnl_usd=NULL
        if pnl_val is None and result_val in TRADING_RESULTS and avg_exit_v and entry_for_calc and full_qty:
            _sign = 1 if s.get("direction") == "long" else -1
            _hq   = half_qty or max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)
            if result_val == "SL":
                pnl_val = round(_sign * full_qty * (avg_exit_v - entry_for_calc), 2)
            elif result_val == "TP1":
                pnl_val = round(_sign * _hq * (avg_exit_v - entry_for_calc), 2)
            elif result_val in ("TP2", "TP1+BE"):
                pnl_val = round(_sign * (_hq + _hq) * (avg_exit_v - entry_for_calc), 2)
        pnl_str   = f"{pnl_val:+.2f}" if pnl_val is not None else "—"
        pnl_color = "lightgreen" if pnl_val and pnl_val > 0 else ("gray" if pnl_val is None else "salmon")

        # PnL %
        pnl_pct = float(s["pnl_pct"]) if s.get("pnl_pct") is not None else None
        if pnl_pct is None and pnl_val is not None and trade_usdt:
            pnl_pct = round(pnl_val / trade_usdt * 100, 2)
        pnl_pct_str   = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "—"
        pnl_pct_color = "lightgreen" if pnl_pct and pnl_pct > 0 else ("gray" if pnl_pct is None else "salmon")

        # Alternatywny scenariusz: całość zamknięta na TP1 (TP2 i TP1+BE)
        alt_pnl = None
        delta   = None
        tp1_price = float(tps[0]) if tps else None
        if result_val in ("TP2", "TP1+BE") and tp1_price and entry_for_calc and full_qty:
            sign    = 1 if s.get("direction") == "long" else -1
            alt_pnl = round(sign * full_qty * (tp1_price - entry_for_calc), 2)
            if pnl_val is not None:
                delta = round(pnl_val - alt_pnl, 2)
        alt_pnl_str   = f"{alt_pnl:+.2f}" if alt_pnl is not None else "—"
        delta_str     = f"{delta:+.2f}"   if delta   is not None else "—"
        alt_color     = "lightgreen" if alt_pnl and alt_pnl > 0 else ("gray" if alt_pnl is None else "salmon")
        delta_color   = "lightgreen" if delta   and delta   > 0 else ("gray" if delta   is None else "salmon")

        result_label = RESULT_LABELS.get(result_val, result_val or "—")

        # Dane setupu zakodowane w atrybucie data-setup (dla JS)
        # avg_entry: używa W1 jako fallback żeby JS mógł liczyć PnL w trybie edycji
        setup_data = {
            "avg_entry":    avg_entry or w1,
            "w1":           w1,
            "tp1":          tp1_price,
            "tp2":          float(tps[1]) if len(tps) > 1 else None,
            "sl":           float(s["sl"]) if s.get("sl") else None,
            "sl_after_tp1": float(s["sl_after_tp1"]) if s.get("sl_after_tp1") else None,
            "direction":    s.get("direction", "long"),
            "full_qty":     full_qty,
            "half_qty":     half_qty,
            "trade_usdt":   trade_usdt,
        }
        setup_json = _html.escape(_json.dumps(setup_data))

        # Dropdown wynik
        options = ""
        for opt, label in RESULT_OPTS:
            sel = " selected" if opt == result_val else ""
            options += f'<option value="{opt}"{sel}>{label}</option>'

        avg_entry_inp = avg_entry if avg_entry else (w1 if result_val in TRADING_RESULTS and w1 else "")
        history_rows += (
            f'<tr data-setup-id="{sid}" data-setup="{setup_json}">'
            f'<td>#{sid}</td>'
            f'<td>{s["model"]}</td>'
            f'<td>{s["direction"].upper()}</td>'
            f'<td>'
            f'<span class="vmode avg-entry-display">{avg_entry_str}</span>'
            f'<input class="emode avg-entry-input" type="number" step="0.01" value="{avg_entry_inp}" oninput="onEntryChange(this)">'
            f'</td>'
            f'<td>'
            f'<span class="vmode result-display">{result_label}</span>'
            f'<select class="emode result-select" onchange="onResultChange(this)">{options}</select>'
            f'</td>'
            f'<td>'
            f'<span class="vmode exit-display">{avg_exit_str}</span>'
            f'<input class="emode avg-exit-input" type="number" step="0.01" value="{avg_exit_inp_val}" oninput="onExitChange(this)">'
            f'</td>'
            f'<td class="pnl-cell" style="color:{pnl_color}">{pnl_str}</td>'
            f'<td class="pnl-pct-cell" style="color:{pnl_pct_color}">{pnl_pct_str}</td>'
            f'<td class="alt-pnl-cell" style="color:{alt_color}">{alt_pnl_str}</td>'
            f'<td class="delta-cell" style="color:{delta_color}">{delta_str}</td>'
            f'<td style="white-space:nowrap">'
            f'<button class="btn-edit vmode" onclick="editRow(this)">Edytuj</button>'
            f'<button class="btn-action emode" onclick="saveResult(this)">Zapisz</button>'
            f'<button class="btn-action emode" onclick="cancelEdit(this)">Anuluj</button>'
            f'</td>'
            f'</tr>\n'
        )

    by_model_rows = ""
    for m in (stats.get("by_model") or []):
        wr = f"{m['wins']}/{m['total']}" if m.get("total") else "—"
        pnl_m = f"{float(m['pnl_usd']):+.2f}" if m.get("pnl_usd") else "—"
        by_model_rows += f"<tr><td>{m['model']}</td><td>{wr}</td><td>{pnl_m}</td></tr>\n"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
  .qt-loading {{ color: #555; }}
  #active-table th[title], #active-table td.qt-p,
  #active-table td.qt-tp1, #active-table td.qt-tp2, #active-table td.qt-sl
    {{ font-size: 0.9em; min-width: 42px; text-align: right; }}
  .av-edit {{ display: none; }}
  tr.editing-tp .av-edit {{ display: inline; }}
  tr.editing-tp .av-view {{ display: none; }}
</style></head><body>
<h2>🤖 AlertSol Dashboard</h2>
<p style="color:#888">Ostatnia aktualizacja: {now}</p>

<div>
  <span class="stat">📊 Win rate: <b>{win_rate}</b></span>
  <span class="stat">💰 Łączny PnL: <b>{total_pnl}</b></span>
  <span class="stat">🎯 Aktywne: <b>{len(active)}</b></span>
  <span class="stat">✅ Zamknięte: <b>{stats.get('total_resolved', 0)}</b></span>
</div>

<h3>Aktywne setupy ({len(active)}) <small style="color:#888;font-size:0.7em" id="bitget-live-status">ładowanie Bitget…</small></h3>
<table id="active-table">
<tr><th>#</th><th>Model</th><th>Kier.</th><th>Status</th><th>W1</th><th>TP1</th><th>TP2</th><th>SL</th><th>SL@TP1</th>
<th title="SOL w otwartej pozycji (Bitget)" style="background:#1a2a1a">qtP</th>
<th title="SOL na zleceniu TP1 (Bitget)" style="background:#1a2a1a">qtTP1</th>
<th title="SOL na zleceniu TP2 (Bitget)" style="background:#1a2a1a">qtTP2</th>
<th title="SOL na zleceniu SL (Bitget)" style="background:#1a2a1a">qtSL</th>
<th>Akcje</th></tr>
{active_rows or '<tr><td colspan=14 style="color:#888">Brak aktywnych setupów</td></tr>'}
</table>

<h3>Per model</h3>
<table><tr><th>Model</th><th>W/Total</th><th>PnL $</th></tr>
{by_model_rows or '<tr><td colspan=3 style="color:#888">Brak danych</td></tr>'}
</table>

<h3>Ostatnie 20 zamkniętych</h3>
<table><tr><th>#</th><th>Model</th><th>Kier.</th><th>Wejście</th><th>Wynik</th><th>Wyjście</th><th>PnL $</th><th>PnL %</th><th title="PnL gdyby cała pozycja wyszła na TP1">TP1-only $</th><th title="Rzeczywisty PnL minus TP1-only (czy TP2 opłacał się)">Δ(real-TP1)</th><th></th></tr>
{history_rows or '<tr><td colspan=11 style="color:#888">Brak historii</td></tr>'}
</table>
</body>
<script>
var RESULT_LABELS = {{
  'TP1':'TP1','TP2':'TP2','TP1+BE':'TP1+BE','SL':'SL',
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
  return null;
}}

function calcPnl(result, d, avgExit) {{
  if (!d.avg_entry || !d.full_qty || avgExit == null || isNaN(avgExit)) return null;
  if (!['TP1','TP2','TP1+BE','SL'].includes(result)) return null;
  var sign = d.direction === 'long' ? 1 : -1;
  if (result === 'SL')  return sign * d.full_qty  * (avgExit - d.avg_entry);
  if (result === 'TP1') return sign * d.half_qty  * (avgExit - d.avg_entry);
  return sign * (d.half_qty + d.half_qty) * (avgExit - d.avg_entry);
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
  if ((result === 'TP2' || result === 'TP1+BE') && d.tp1 && d.avg_entry && d.full_qty) {{
    alt = (d.direction === 'long' ? 1 : -1) * d.full_qty * (d.tp1 - d.avg_entry);
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

// ── Bitget live data ────────────────────────────────────────────────────────
async function loadBitgetLive() {{
  var statusEl = document.getElementById('bitget-live-status');
  try {{
    var resp = await fetch('/api/bitget-live');
    var data = await resp.json();
    if (data.error) {{
      if (statusEl) statusEl.textContent = '⚠️ brak Bitget';
      clearQtCells();
      return;
    }}

    var tpsl  = data.tpsl  || {{}};
    var plans = data.plans || {{}};
    var rows  = document.querySelectorAll('#active-table tr[data-sid]');

    rows.forEach(function(row) {{
      var sid     = row.dataset.sid;
      var planOid = row.dataset.planOid;
      var tp1Oid  = row.dataset.tp1Oid;
      var tp2Oid  = row.dataset.tp2Oid;
      var slOid   = row.dataset.slOid;
      var posOpen = row.dataset.posOpen === 'true';
      var tp1Done = row.dataset.tp1Done === 'true';
      var qtyFull = row.dataset.qtyFull;

      var qpCell  = document.getElementById('qp-'  + sid);
      var qt1Cell = document.getElementById('qt1-' + sid);
      var qt2Cell = document.getElementById('qt2-' + sid);
      var qslCell = document.getElementById('qsl-' + sid);

      // qtP — rozmiar otwartej pozycji
      if (posOpen) {{
        // Pozycja otwarta: pokaż exchange_qty_full z DB (plan order size)
        qpCell.textContent = qtyFull || '—';
        qpCell.style.color = '#90ee90';
      }} else if (planOid && plans[planOid]) {{
        // Czeka na wejście: pokaż rozmiar planu z nawiasem
        qpCell.textContent = '(' + plans[planOid].size + ')';
        qpCell.style.color = '#aaa';
      }} else if (planOid) {{
        // OID w DB ale nie znaleziono na Bitget — może wykonany lub anulowany
        qpCell.textContent = qtyFull ? '(' + qtyFull + ')?' : '?';
        qpCell.style.color = 'orange';
      }} else {{
        qpCell.textContent = qtyFull ? '(' + qtyFull + ')' : '—';
        qpCell.style.color = '#aaa';
      }}

      // qtTP1
      if (tp1Done && tp1Oid && tpsl[tp1Oid]) {{
        // Anomalia: oznaczone jako done ale zlecenie wciąż aktywne na Bitget
        qt1Cell.textContent = '⚠' + tpsl[tp1Oid].size;
        qt1Cell.style.color = 'orange';
      }} else if (tp1Done) {{
        qt1Cell.textContent = '✓';
        qt1Cell.style.color = '#90ee90';
      }} else if (tp1Oid && tpsl[tp1Oid]) {{
        qt1Cell.textContent = tpsl[tp1Oid].size;
        qt1Cell.style.color = '#e0e0e0';
      }} else if (tp1Oid) {{
        // OID w DB ale nie znaleziono na Bitget — może anulowane lub wykonane
        qt1Cell.textContent = '?';
        qt1Cell.style.color = 'orange';
      }} else {{
        qt1Cell.textContent = '—';
        qt1Cell.style.color = '#555';
      }}

      // qtTP2
      if (tp2Oid && tpsl[tp2Oid]) {{
        qt2Cell.textContent = tpsl[tp2Oid].size;
        qt2Cell.style.color = '#e0e0e0';
      }} else if (tp2Oid) {{
        qt2Cell.textContent = '?';
        qt2Cell.style.color = 'orange';
      }} else {{
        qt2Cell.textContent = '—';
        qt2Cell.style.color = '#555';
      }}

      // qtSL
      if (slOid && tpsl[slOid]) {{
        qslCell.textContent = tpsl[slOid].size;
        qslCell.style.color = '#e0e0e0';
      }} else if (slOid) {{
        qslCell.textContent = '?';
        qslCell.style.color = 'orange';
      }} else {{
        qslCell.textContent = '—';
        qslCell.style.color = '#555';
      }}
    }});

    var now = new Date().toLocaleTimeString('pl-PL', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
    if (statusEl) statusEl.textContent = '✓ ' + now;
  }} catch(e) {{
    if (statusEl) statusEl.textContent = '⚠️ błąd: ' + e.message;
    clearQtCells();
  }}
}}

function clearQtCells() {{
  document.querySelectorAll('.qt-p,.qt-tp1,.qt-tp2,.qt-sl').forEach(function(td) {{
    td.textContent = '?';
    td.style.color = '#888';
  }});
}}

loadBitgetLive();
setInterval(loadBitgetLive, 15000);
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


class ResultUpdate(BaseModel):
    result: str
    avg_exit: float | None = None
    avg_entry: float | None = None


class TpsUpdate(BaseModel):
    tp1: float | None = None
    tp2: float | None = None
    sl:  float | None = None


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
                else:
                    failed.append(f"TP1:{resp.get('msg')}")
            except Exception as e:
                failed.append(f"TP1:{e}")
        else:
            modified.append(f"TP1→{tp1_new} (tylko DB)")
        if len(tps) > 0:
            tps[0] = tp1_new
        elif len(tps) == 0:
            tps = [tp1_new]

    # Modify TP2 on Bitget if order exists
    tp2_oid = s.get("exchange_tp2_oid")
    if tp2_new is not None:
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
                else:
                    failed.append(f"TP2:{resp.get('msg')}")
            except Exception as e:
                failed.append(f"TP2:{e}")
        else:
            modified.append(f"TP2→{tp2_new} (tylko DB)")
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
                else:
                    failed.append(f"SL:{resp.get('msg')}")
            except Exception as e:
                failed.append(f"SL:{e}")
        else:
            modified.append(f"SL→{sl_new} (tylko DB)")
        # Aktualizuj odpowiednie pole w DB: sl_after_tp1 gdy po TP1, inaczej sl
        if tp1_done:
            db.update_setup(setup_id, sl_after_tp1=sl_new)
        else:
            db.update_setup(setup_id, sl=sl_new)

    if modified or not failed:
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
    VALID_RESULTS = {"TP1", "TP2", "TP1+BE", "SL", "nieokreslone", "nie weszlo", "anulowany"}
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

    if avg_exit is not None and avg_entry and body.result in ("TP1", "TP2", "TP1+BE", "SL"):
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

        if body.result == "SL":
            pnl_usd = sign * full_qty * (avg_exit - avg_entry)
        elif body.result == "TP1":
            pnl_usd = sign * half_qty * (avg_exit - avg_entry)
        elif body.result in ("TP2", "TP1+BE"):
            pnl_usd = sign * (half_qty + half_qty) * (avg_exit - avg_entry)

    db.resolve_setup(setup_id, body.result, avg_entry, avg_exit, pnl_usd, None)
    return {
        "ok":       True,
        "setup_id": setup_id,
        "result":   body.result,
        "avg_entry": avg_entry,
        "avg_exit":  avg_exit,
        "pnl_usd":  round(pnl_usd, 2) if pnl_usd is not None else None,
    }


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
