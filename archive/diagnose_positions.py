#!/usr/bin/env python3
"""
diagnose_positions.py — jednorazowy skrypt diagnostyczny
Sprawdza rozbieżności między stanem DB a rzeczywistymi pozycjami/TPSL na Bitget.
"""

import os
import sys

import db
import exchange_trader as et

def main():
    client = et._client()
    if client is None:
        print("BŁĄD: Brak konfiguracji BITGET — nie można połączyć się z API.")
        sys.exit(1)

    # ── 1. Stan bazy danych ───────────────────────────────────────────────────
    pending = db.load_pending()
    print(f"\n{'='*70}")
    print(f"BAZA DANYCH — setupy nierozwiązane: {len(pending)}")
    print(f"{'='*70}")

    db_positions_open = []
    for s in pending:
        sid       = s.get("setup_id", "?")
        model     = s.get("model", "?")
        direction = s.get("direction", "?")
        pos_open  = s.get("exchange_position_opened", False)
        plan_oid  = s.get("exchange_plan_oid")
        tp1_oid   = s.get("exchange_tp1_oid")
        tp2_oid   = s.get("exchange_tp2_oid")
        sl_oid    = s.get("exchange_sl_oid")
        tp1_done  = s.get("exchange_tp1_done", False)
        ex_done   = s.get("exchange_done", False)
        cancelled = bool(s.get("cancel_reason"))
        shadow    = s.get("shadow", False)

        has_tpsl = bool(tp1_oid or tp2_oid or sl_oid)

        status_parts = []
        if ex_done:
            status_parts.append("exchange_done=TRUE")
        if shadow:
            status_parts.append("SHADOW")
        if cancelled:
            status_parts.append(f"ANULOWANY({s.get('cancel_reason')})")
        if pos_open:
            status_parts.append("POZYCJA_OTWARTA")
        if plan_oid:
            status_parts.append(f"plan_oid={plan_oid[:12]}…")
        if tp1_done:
            status_parts.append("TP1_DONE")

        tpsl_str = []
        if tp1_oid:
            tpsl_str.append(f"TP1={tp1_oid[:12]}…")
        if tp2_oid:
            tpsl_str.append(f"TP2={tp2_oid[:12]}…")
        if sl_oid:
            tpsl_str.append(f"SL={sl_oid[:12]}…")

        print(f"\n  Setup #{sid} | {model} | {direction.upper()}")
        print(f"    Stan:  {', '.join(status_parts) if status_parts else '(brak)'}")
        print(f"    TPSL:  {', '.join(tpsl_str) if tpsl_str else '⚠️  BRAK TPSL'}")
        if pos_open and not ex_done:
            db_positions_open.append(s)

    print(f"\n  → Pozycje oznaczone jako otwarte w DB: {len(db_positions_open)}")

    # ── 2. Rzeczywiste pozycje na Bitget ─────────────────────────────────────
    print(f"\n{'='*70}")
    print("BITGET — rzeczywiste otwarte pozycje (all-position):")
    print(f"{'='*70}")

    try:
        resp = client.get("/api/v2/mix/position/all-position", {
            "productType": et.PRODUCT_TYPE,
            "marginCoin":  et.MARGIN_COIN,
        })
        if resp.get("code") == "00000":
            positions = [p for p in (resp.get("data") or []) if float(p.get("total", 0)) > 0]
            if positions:
                for p in positions:
                    print(f"  {p['symbol']} | {p['holdSide']:5s} | size={p.get('total')} | "
                          f"avgPrice={p.get('openPriceAvg')} | unreal.PnL={p.get('unrealizedPL')}")
            else:
                print("  Brak otwartych pozycji na Bitget.")
        else:
            print(f"  BŁĄD API: {resp.get('msg')}")
    except Exception as e:
        print(f"  BŁĄD: {e}")

    # ── 3. Aktywne zlecenia TPSL na Bitget ───────────────────────────────────
    print(f"\n{'='*70}")
    print("BITGET — aktywne zlecenia TPSL (profit_loss pending):")
    print(f"{'='*70}")

    live_tpsl_ids = set()
    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      et.SYMBOL,
            "productType": et.PRODUCT_TYPE,
            "planType":    "profit_loss",
        })
        if resp.get("code") == "00000":
            orders = resp["data"].get("entrustedList") or []
            if orders:
                for o in orders:
                    live_tpsl_ids.add(o["orderId"])
                    print(f"  orderId={o['orderId'][:16]}… | planType={o.get('planType'):12s} | "
                          f"side={o.get('side'):4s} | triggerPrice={o.get('triggerPrice')} | "
                          f"size={o.get('size')} | status={o.get('planStatus')}")
            else:
                print("  Brak aktywnych zleceń TPSL na Bitget.")
        else:
            print(f"  BŁĄD API: {resp.get('msg')}")
    except Exception as e:
        print(f"  BŁĄD: {e}")

    # ── 4. Aktywne plan ordery wejścia na Bitget ──────────────────────────────
    print(f"\n{'='*70}")
    print("BITGET — aktywne plan ordery wejścia (normal_plan pending):")
    print(f"{'='*70}")

    try:
        resp = client.get("/api/v2/mix/order/orders-plan-pending", {
            "symbol":      et.SYMBOL,
            "productType": et.PRODUCT_TYPE,
            "planType":    "normal_plan",
        })
        if resp.get("code") == "00000":
            orders = resp["data"].get("entrustedList") or []
            if orders:
                for o in orders:
                    print(f"  orderId={o['orderId'][:16]}… | side={o.get('side'):4s} | "
                          f"triggerPrice={o.get('triggerPrice')} | size={o.get('size')} | "
                          f"status={o.get('planStatus')}")
            else:
                print("  Brak aktywnych plan orderów wejścia.")
        else:
            print(f"  BŁĄD API: {resp.get('msg')}")
    except Exception as e:
        print(f"  BŁĄD: {e}")

    # ── 5. Analiza rozbieżności ───────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("ANALIZA ROZBIEŻNOŚCI:")
    print(f"{'='*70}")

    for s in db_positions_open:
        sid     = s.get("setup_id", "?")
        tp1_oid = s.get("exchange_tp1_oid")
        tp2_oid = s.get("exchange_tp2_oid")
        sl_oid  = s.get("exchange_sl_oid")

        missing = []
        orphan  = []

        for label, oid in [("TP1", tp1_oid), ("TP2", tp2_oid), ("SL", sl_oid)]:
            if oid:
                if oid not in live_tpsl_ids:
                    orphan.append(f"{label}={oid[:12]}… (w DB ale NIE na Bitget)")
            else:
                missing.append(label)

        if missing:
            print(f"  ⚠️  Setup #{sid}: brak w DB → {', '.join(missing)}")
        if orphan:
            print(f"  ⚠️  Setup #{sid}: zlecenia w DB ale nieznalezione na Bitget → {', '.join(orphan)}")
        if not missing and not orphan:
            print(f"  ✅ Setup #{sid}: DB i Bitget zgodne")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    main()
