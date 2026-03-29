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
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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

    history_rows = ""
    for s in recent:
        result  = s.get("result", "—")
        pnl     = f"{float(s['pnl_usd']):+.2f}" if s.get("pnl_usd") is not None else "—"
        color   = "green" if s.get("pnl_usd") and float(s["pnl_usd"]) > 0 else ("gray" if pnl == "—" else "red")
        history_rows += (
            f"<tr><td>#{s['setup_id']}</td><td>{s['model']}</td>"
            f"<td>{s['direction'].upper()}</td><td>{result}</td>"
            f"<td style='color:{color}'>{pnl}</td></tr>\n"
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
  body {{ font-family: monospace; max-width: 900px; margin: 20px auto; background: #1a1a1a; color: #e0e0e0; }}
  h2 {{ color: #90caf9; }} h3 {{ color: #80deea; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
  th, td {{ border: 1px solid #444; padding: 6px 12px; text-align: left; }}
  th {{ background: #333; }}
  .stat {{ display: inline-block; margin: 0 20px 10px 0; font-size: 1.2em; }}
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
<table><tr><th>#</th><th>Model</th><th>Kier.</th><th>Wynik</th><th>PnL $</th></tr>
{history_rows or '<tr><td colspan=5 style="color:#888">Brak historii</td></tr>'}
</table>
</body></html>"""
    return HTMLResponse(html)


@app.get("/health")
def health():
    """Endpoint healthcheck dla Railway."""
    jobs = [{"id": j.id, "next_run": str(j.next_run_time)} for j in scheduler.get_jobs()]
    return {"status": "ok", "jobs": jobs}


@app.get("/admin/test-klines")
def admin_test_klines():
    """Testuje fetch_klines z Bitget — zwraca ostatnią świecę lub błąd."""
    try:
        candles = sol_alert.fetch_klines("SOLUSDT", "15m", limit=3)
        return {"ok": True, "count": len(candles), "last": candles[-1] if candles else None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/admin/resolve-setup/{setup_id}")
def admin_resolve_setup(setup_id: int):
    """Tymczasowy endpoint do ręcznego zamknięcia setupu w bazie."""
    db.resolve_setup(setup_id, "nieokreslone", None, None, 0, None)
    return {"ok": True, "setup_id": setup_id, "result": "nieokreslone"}


@app.get("/api/stats")
def api_stats():
    """JSON API dla przyszłej integracji z Metabase."""
    return {
        "summary":     db.get_summary_stats(),
        "active":      db.get_active_setups(),
        "recent":      db.get_recent_resolved(50),
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
