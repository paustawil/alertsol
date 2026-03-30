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
                    sol_alert.log_to_anulowane_grok(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move)
                else:
                    sol_alert.log_to_wyniki(s, result, entry_ts, exit_ts, avg_entry, avg_exit, move)
                db.mark_sheets_exported(s["setup_id"])
                log.info(f"[sheets-export] Setup #{s['setup_id']} wyeksportowany.")
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
        active_rows += (
            f"<tr><td>#{s['setup_id']}</td><td>{s['model']}</td>"
            f"<td>{s['direction'].upper()}</td><td>{status}</td>"
            f"<td>{w1}</td><td>{tp1}</td><td>{tp2}</td>"
            f"<td>{sl}</td><td>{sl2}</td></tr>\n"
        )

    trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
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

        avg_entry_str = f"{avg_entry:.2f}" if avg_entry else "—"
        avg_exit_str  = f"{avg_exit_v:.2f}" if avg_exit_v is not None else ""

        # Qty — z bazy lub oszacowane
        try:
            full_qty = float(s["exchange_qty_full"]) if s.get("exchange_qty_full") else None
            half_qty = float(s["exchange_qty_half"]) if s.get("exchange_qty_half") else None
        except (ValueError, TypeError):
            full_qty = half_qty = None
        if not full_qty and avg_entry:
            full_qty = max(math.floor((trade_usdt * 20 / avg_entry) / 0.1) * 0.1, 0.1)
        if not half_qty and full_qty:
            half_qty = max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)

        # Dane setupu zakodowane w atrybucie data-setup (dla JS)
        setup_data = {
            "avg_entry":    avg_entry,
            "tp1":          float(tps[0]) if len(tps) > 0 else None,
            "tp2":          float(tps[1]) if len(tps) > 1 else None,
            "sl":           float(s["sl"]) if s.get("sl") else None,
            "sl_after_tp1": float(s["sl_after_tp1"]) if s.get("sl_after_tp1") else None,
            "direction":    s.get("direction", "long"),
            "full_qty":     full_qty,
            "half_qty":     half_qty,
        }
        setup_json = _html.escape(_json.dumps(setup_data))

        # Dropdown wynik
        options = ""
        for opt, label in [("TP1","TP1"),("TP2","TP2"),("TP1+BE","TP1+BE"),("SL","SL"),("nieokreslone","nieokreślone")]:
            sel = " selected" if opt == result_val else ""
            options += f'<option value="{opt}"{sel}>{label}</option>'

        history_rows += (
            f'<tr data-setup-id="{sid}" data-setup="{setup_json}">'
            f'<td>#{sid}</td>'
            f'<td>{s["model"]}</td>'
            f'<td>{s["direction"].upper()}</td>'
            f'<td>{avg_entry_str}</td>'
            f'<td><select class="result-select" onchange="onResultChange(this)">{options}</select></td>'
            f'<td><input class="avg-exit-input" type="number" step="0.01" value="{avg_exit_str}"'
            f' oninput="onExitChange(this)"></td>'
            f'<td class="pnl-cell" style="color:{pnl_color}">{pnl_str}</td>'
            f'<td><button class="save-btn" onclick="saveResult(this)">Zapisz</button></td>'
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
  .result-select {{ background: #2a2a2a; color: #e0e0e0; border: 1px solid #555; padding: 2px 4px; font-family: monospace; }}
  .avg-exit-input {{ background: #2a2a2a; color: #e0e0e0; border: 1px solid #555; padding: 2px 4px; width: 72px; font-family: monospace; }}
  .save-btn {{ background: #333; color: #e0e0e0; border: 1px solid #555; padding: 2px 10px; cursor: pointer; font-family: monospace; }}
  .save-btn:hover {{ background: #444; }}
</style></head><body>
<h2>🤖 AlertSol Dashboard</h2>
<p style="color:#888">Ostatnia aktualizacja: {now}</p>

<div>
  <span class="stat">📊 Win rate: <b>{win_rate}</b></span>
  <span class="stat">💰 Łączny PnL: <b>{total_pnl}</b></span>
  <span class="stat">🎯 Aktywne: <b>{len(active)}</b></span>
  <span class="stat">✅ Zamknięte: <b>{stats.get('total_resolved', 0)}</b></span>
</div>

<h3>Aktywne setupy ({len(active)})</h3>
<table><tr><th>#</th><th>Model</th><th>Kier.</th><th>Status</th><th>W1</th><th>TP1</th><th>TP2</th><th>SL</th><th>SL@TP1</th></tr>
{active_rows or '<tr><td colspan=9 style="color:#888">Brak aktywnych setupów</td></tr>'}
</table>

<h3>Per model</h3>
<table><tr><th>Model</th><th>W/Total</th><th>PnL $</th></tr>
{by_model_rows or '<tr><td colspan=3 style="color:#888">Brak danych</td></tr>'}
</table>

<h3>Ostatnie 20 zamkniętych</h3>
<table><tr><th>#</th><th>Model</th><th>Kier.</th><th>Wejście</th><th>Wynik</th><th>Wyjście</th><th>PnL $</th><th></th></tr>
{history_rows or '<tr><td colspan=8 style="color:#888">Brak historii</td></tr>'}
</table>
</body>
<script>
function getSetupData(tr) {{
  return JSON.parse(tr.dataset.setup);
}}

function calcAvgExit(result, d) {{
  if (result === 'SL')     return d.sl;
  if (result === 'TP1')    return d.tp1;
  if (result === 'TP2')    return (d.tp1 != null && d.tp2 != null) ? (d.tp1 + d.tp2) / 2 : d.tp2;
  if (result === 'TP1+BE') return (d.tp1 != null && d.sl_after_tp1 != null) ? (d.tp1 + d.sl_after_tp1) / 2 : null;
  return null;
}}

function calcPnl(result, d, avgExit) {{
  if (!d.avg_entry || avgExit == null || isNaN(avgExit)) return null;
  if (result === 'nieokreslone') return null;
  var sign = d.direction === 'long' ? 1 : -1;
  if (result === 'SL')              return sign * d.full_qty * (avgExit - d.avg_entry);
  if (result === 'TP1')             return sign * d.half_qty * (avgExit - d.avg_entry);
  if (result === 'TP2' || result === 'TP1+BE')
                                    return sign * (d.half_qty + d.half_qty) * (avgExit - d.avg_entry);
  return null;
}}

function refreshPnlCell(tr, pnl) {{
  var cell = tr.querySelector('.pnl-cell');
  if (pnl == null) {{ cell.textContent = '—'; cell.style.color = 'gray'; return; }}
  cell.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
  cell.style.color = pnl >= 0 ? 'lightgreen' : 'salmon';
}}

function onResultChange(sel) {{
  var tr  = sel.closest('tr');
  var d   = getSetupData(tr);
  var inp = tr.querySelector('.avg-exit-input');
  var ae  = calcAvgExit(sel.value, d);
  inp.value = ae != null ? ae.toFixed(2) : '';
  refreshPnlCell(tr, calcPnl(sel.value, d, ae));
}}

function onExitChange(inp) {{
  var tr  = inp.closest('tr');
  var d   = getSetupData(tr);
  var res = tr.querySelector('.result-select').value;
  refreshPnlCell(tr, calcPnl(res, d, parseFloat(inp.value)));
}}

async function saveResult(btn) {{
  var tr      = btn.closest('tr');
  var setupId = tr.dataset.setupId;
  var result  = tr.querySelector('.result-select').value;
  var exitVal = tr.querySelector('.avg-exit-input').value;
  var avgExit = exitVal !== '' ? parseFloat(exitVal) : null;

  btn.textContent = '...'; btn.disabled = true;
  try {{
    var resp = await fetch('/api/update-result/' + setupId, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{result: result, avg_exit: avgExit}})
    }});
    var data = await resp.json();
    if (resp.ok && data.ok) {{
      btn.textContent = '✓'; btn.style.color = 'lightgreen';
      if (data.pnl_usd != null) refreshPnlCell(tr, data.pnl_usd);
    }} else {{
      btn.textContent = '✗'; btn.style.color = 'salmon';
    }}
  }} catch(e) {{
    btn.textContent = '✗'; btn.style.color = 'salmon';
  }}
  setTimeout(function() {{
    btn.textContent = 'Zapisz'; btn.style.color = ''; btn.disabled = false;
  }}, 2000);
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


@app.post("/api/update-result/{setup_id}")
def api_update_result(setup_id: int, body: ResultUpdate):
    """Ręczna korekta wyniku i PnL zamkniętego setupu."""
    VALID_RESULTS = {"TP1", "TP2", "TP1+BE", "SL", "nieokreslone"}
    if body.result not in VALID_RESULTS:
        raise HTTPException(status_code=400, detail=f"Nieprawidłowy wynik: {body.result}")

    with db._conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2").extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM setups WHERE setup_id = %s", (setup_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Setup #{setup_id} nie znaleziony")

    s          = db._row_to_dict(row)
    avg_exit   = body.avg_exit
    pnl_usd    = None
    avg_entry  = float(s["avg_entry"]) if s.get("avg_entry") else None

    if avg_exit is not None and avg_entry:
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

        # Wylicz PnL
        if body.result == "SL":
            pnl_usd = sign * full_qty * (avg_exit - avg_entry)
        elif body.result == "TP1":
            pnl_usd = sign * half_qty * (avg_exit - avg_entry)
        elif body.result in ("TP2", "TP1+BE"):
            # Obie połówki, avg_exit = średnia ważona obu cen wyjścia
            pnl_usd = sign * (half_qty + half_qty) * (avg_exit - avg_entry)

    db.resolve_setup(setup_id, body.result, avg_entry, avg_exit, pnl_usd, None)
    return {
        "ok":      True,
        "setup_id": setup_id,
        "result":  body.result,
        "avg_exit": avg_exit,
        "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
    }


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
