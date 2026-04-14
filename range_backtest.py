#!/usr/bin/env python3
"""
Range Backtest — porownanie logiki range setupow:
  Wersja A (aktualna): TP na % range'a, oba kierunki niezaleznie od EMA
  Wersja B (nowa):     EMA alignment decyduje o kierunku, TP na poziomach EMA

Uruchomienie:
    python range_backtest.py
    python range_backtest.py --from 2026-04-01 --to 2026-04-14
    python range_backtest.py --max-rng-atr 4.0   # filtr szerokosci range
"""

import argparse
import math
import random
from datetime import datetime, timezone

import requests

TRADE_USDT = 100
LEVERAGE   = 20
COOLDOWN_M15 = 16   # 4h / 15min = 16 świec przerwy między setupami tego samego typu


# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = [max(c["high"] - c["low"],
               abs(c["high"] - p["close"]),
               abs(c["low"]  - p["close"]))
           for c, p in zip(candles[1:], candles)]
    return sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0


def calc_sma(candles_h1: list[dict], n: int) -> float | None:
    closes = [c["close"] for c in candles_h1[-n:]]
    if len(closes) < n:
        return None
    return sum(closes) / n


def calc_emas(candles_h1: list[dict]) -> dict:
    """Zwraca SMA5/10/30/60 na H1 (to co widac na wykresie jako EMA)."""
    return {
        "ema5":  calc_sma(candles_h1, 5),
        "ema10": calc_sma(candles_h1, 10),
        "ema30": calc_sma(candles_h1, 30),
        "ema60": calc_sma(candles_h1, 60),
    }


def ema_alignment(e: dict) -> str:
    """Bycze / niedźwiedzie / mieszane."""
    e5, e30, e60 = e.get("ema5"), e.get("ema30"), e.get("ema60")
    if None in (e5, e30, e60):
        return "mixed"
    if e5 > e30 > e60:
        return "bullish"
    if e5 < e30 < e60:
        return "bearish"
    return "mixed"


def detect_range(candles_h1: list[dict], n: int = 32) -> dict:
    recent = candles_h1[-n:]
    res  = max(c["high"] for c in recent)
    sup  = min(c["low"]  for c in recent)
    size = res - sup
    zone = size * 0.06
    return {
        "resistance": round(res, 3),
        "support":    round(sup, 3),
        "size":       round(size, 3),
        "r_touches":  sum(1 for c in recent if c["high"] >= res - zone),
        "s_touches":  sum(1 for c in recent if c["low"]  <= sup + zone),
    }


def ema_tps_long(entry: float, emas: dict, resistance: float,
                 sup: float, rng_size: float) -> tuple[float, float]:
    """TP1/TP2 dla longa oparte na EMA (lub fallback na % range)."""
    levels = sorted(
        [v for v in emas.values() if v is not None and v > entry + 0.30 and v < resistance],
    )
    if len(levels) >= 2:
        return levels[0], levels[1]
    if len(levels) == 1:
        # drugi TP: fallback na 65% range od support
        tp2_fallback = round(sup + rng_size * 0.65, 3)
        return levels[0], max(levels[0] + 0.30, tp2_fallback)
    # brak EMA pomiędzy — fallback % skalowany do szerokości
    if rng_size > 0:
        tp1 = round(sup + rng_size * 0.35, 3)
        tp2 = round(sup + rng_size * 0.60, 3)
    else:
        tp1 = entry + 0.50
        tp2 = entry + 1.00
    return tp1, tp2


def ema_tps_short(entry: float, emas: dict, support: float,
                  res: float, rng_size: float) -> tuple[float, float]:
    """TP1/TP2 dla shorta oparte na EMA (lub fallback na % range)."""
    levels = sorted(
        [v for v in emas.values() if v is not None and v < entry - 0.30 and v > support],
        reverse=True,
    )
    if len(levels) >= 2:
        return levels[0], levels[1]
    if len(levels) == 1:
        tp2_fallback = round(res - rng_size * 0.65, 3)
        return levels[0], min(levels[0] - 0.30, tp2_fallback)
    if rng_size > 0:
        tp1 = round(res - rng_size * 0.35, 3)
        tp2 = round(res - rng_size * 0.60, 3)
    else:
        tp1 = entry - 0.50
        tp2 = entry - 1.00
    return tp1, tp2


# ── Setup generators ──────────────────────────────────────────────────────────

def _ma_m15(candles_m15: list[dict], n: int) -> float | None:
    closes = [c["close"] for c in candles_m15[-n:]]
    return sum(closes) / len(closes) if len(closes) >= n else None


def gen_version_a(candles_h1: list[dict], candles_m15: list[dict],
                  price: float, atr: float, max_rng_atr: float) -> list[dict]:
    """
    Wersja A — aktualna logika range setupow (wierne odwzorowanie sol_alert.py):
    - TP1 = 50% range, TP2 = 90% range
    - MA filter na M15 (jak w sol_alert)
    - RR >= 1.5
    """
    rng    = detect_range(candles_h1)
    sup    = rng["support"]
    res    = rng["resistance"]
    size   = rng["size"]
    if size < atr * 1.5:
        return []
    if max_rng_atr and size > atr * max_rng_atr:
        return []
    if rng["r_touches"] < 2 or rng["s_touches"] < 2:
        return []

    # MA filter na M15 (tak jak w sol_alert.py)
    ma30_m15 = _ma_m15(candles_m15, 30)
    ma60_m15 = _ma_m15(candles_m15, 60)
    setups   = []

    # range_resistance_short
    w_s  = round(res - size * 0.10, 3)
    sl_s = round(res + atr,         3)
    tp1_s = round(sup + size * 0.50, 3)
    tp2_s = round(sup + size * 0.10, 3)
    dist_ok_s = abs(w_s - price) <= price * 0.03
    tp1_margin_ok_s = price >= tp1_s + size * 0.15
    ma_bullish = ma30_m15 and ma60_m15 and price > ma30_m15 > ma60_m15
    ma_ok_s = not ma_bullish
    rr_s = (w_s - tp1_s) / (sl_s - w_s) if sl_s > w_s else 0
    if dist_ok_s and tp1_margin_ok_s and ma_ok_s and rr_s >= 1.5:
        setups.append({
            "direction": "short", "type": "A_range_short",
            "w": w_s, "sl": sl_s, "tp1": tp1_s, "tp2": tp2_s,
            "rr": round(rr_s, 2),
        })

    # range_support_long
    w_l  = round(sup + size * 0.10, 3)
    sl_l = round(sup - atr,         3)
    tp1_l = round(sup + size * 0.50, 3)
    tp2_l = round(res - size * 0.10, 3)
    dist_ok_l = abs(w_l - price) <= price * 0.03
    tp1_margin_ok_l = price <= tp1_l - size * 0.15
    ma_bearish = ma30_m15 and ma60_m15 and price < ma30_m15 < ma60_m15
    ma_ok_l = not ma_bearish
    rr_l = (tp1_l - w_l) / (w_l - sl_l) if w_l > sl_l else 0
    if dist_ok_l and tp1_margin_ok_l and ma_ok_l and rr_l >= 1.5:
        setups.append({
            "direction": "long", "type": "A_range_long",
            "w": w_l, "sl": sl_l, "tp1": tp1_l, "tp2": tp2_l,
            "rr": round(rr_l, 2),
        })

    return setups


def gen_version_b(candles_h1: list[dict], candles_m15: list[dict],
                  price: float, atr: float, max_rng_atr: float) -> list[dict]:
    """
    Wersja B — nowa logika:
    - EMA H1 alignment decyduje o kierunku (bullish→tylko long, bearish→tylko short, mixed→skip)
    - TP na poziomach EMA H1 (fallback na mniejszy % range)
    - Filtr szerokości range (max_rng_atr)
    """
    rng    = detect_range(candles_h1)
    sup    = rng["support"]
    res    = rng["resistance"]
    size   = rng["size"]
    if size < atr * 1.5:
        return []
    if max_rng_atr and size > atr * max_rng_atr:
        return []
    if rng["r_touches"] < 2 or rng["s_touches"] < 2:
        return []

    emas      = calc_emas(candles_h1)
    alignment = ema_alignment(emas)
    if alignment == "mixed":
        return []

    setups = []

    if alignment == "bearish":
        # range_resistance_short
        w_s  = round(res - size * 0.10, 3)
        sl_s = round(res + atr,         3)
        dist_ok = abs(w_s - price) <= price * 0.03
        tp1_margin_ok = price >= w_s - size * 0.15
        if not dist_ok or not tp1_margin_ok:
            return []
        tp1_s, tp2_s = ema_tps_short(w_s, emas, sup, res, size)
        rr_s = (w_s - tp1_s) / (sl_s - w_s) if sl_s > w_s and tp1_s < w_s else 0
        if rr_s >= 1.2:
            setups.append({
                "direction": "short", "type": "B_range_short",
                "w": w_s, "sl": sl_s, "tp1": tp1_s, "tp2": tp2_s,
                "rr": round(rr_s, 2),
            })

    if alignment == "bullish":
        # range_support_long
        w_l  = round(sup + size * 0.10, 3)
        sl_l = round(sup - atr,         3)
        dist_ok = abs(w_l - price) <= price * 0.03
        tp1_margin_ok = price <= w_l + size * 0.15
        if not dist_ok or not tp1_margin_ok:
            return []
        tp1_l, tp2_l = ema_tps_long(w_l, emas, res, sup, size)
        rr_l = (tp1_l - w_l) / (w_l - sl_l) if w_l > sl_l and tp1_l > w_l else 0
        if rr_l >= 1.2:
            setups.append({
                "direction": "long", "type": "B_range_long",
                "w": w_l, "sl": sl_l, "tp1": tp1_l, "tp2": tp2_l,
                "rr": round(rr_l, 2),
            })

    return setups


# ── Trade simulation ──────────────────────────────────────────────────────────

def simulate(setup: dict, future_m15: list[dict],
             trade_usdt: float = TRADE_USDT,
             leverage: float   = LEVERAGE) -> dict | None:
    """
    Symuluje trade na świecach M15.
    Zwraca wynik lub None jeśli brak wejścia w ciągu 4h (16 świec).
    """
    w         = setup["w"]
    sl        = setup["sl"]
    tp1       = setup["tp1"]
    tp2       = setup["tp2"]
    direction = setup["direction"]
    qty       = round(trade_usdt * leverage / w / 0.1) * 0.1

    entry_candle_idx = None
    for i, c in enumerate(future_m15[:16]):          # max 4h na wejście
        if direction == "long"  and c["low"]  <= w:
            entry_candle_idx = i; break
        if direction == "short" and c["high"] >= w:
            entry_candle_idx = i; break
    if entry_candle_idx is None:
        return None

    # Od momentu wejścia śledź TP/SL
    half = qty / 2
    tp1_hit = tp2_hit = sl_hit = False
    for c in future_m15[entry_candle_idx:entry_candle_idx + 64]:   # max 16h
        if direction == "long":
            if not tp1_hit and c["high"] >= tp1:
                tp1_hit = True
            if tp1_hit and not tp2_hit and c["high"] >= tp2:
                tp2_hit = True
            if c["low"] <= sl:
                sl_hit = True; break
        else:
            if not tp1_hit and c["low"] <= tp1:
                tp1_hit = True
            if tp1_hit and not tp2_hit and c["low"] <= tp2:
                tp2_hit = True
            if c["high"] >= sl:
                sl_hit = True; break

    # PnL (odległości od W, bez prowizji)
    dist_tp1 = abs(tp1 - w)
    dist_tp2 = abs(tp2 - w)
    dist_sl  = abs(sl  - w)

    if tp2_hit and not sl_hit:
        wynik = "TP2"
        pnl   = round((dist_tp1 * half + dist_tp2 * half) * qty / w * leverage / leverage
                      if False else (dist_tp1 + dist_tp2) / 2 * qty, 2)
        # prostsza kalkulacja: pełna pozycja, average exit
        avg_exit = (tp1 + tp2) / 2
        pnl = round(abs(avg_exit - w) * qty, 2)
    elif tp1_hit and sl_hit:
        wynik = "TP1+BE"
        pnl   = round(dist_tp1 * half, 2)  # połowa na TP1, połowa na BE
    elif tp1_hit and not sl_hit:
        wynik = "TP1"
        pnl   = round(dist_tp1 * qty, 2)
    elif sl_hit:
        wynik = "SL"
        pnl   = round(-dist_sl * qty, 2)
    else:
        wynik = "timeout"
        pnl   = 0.0

    return {
        "wynik":     wynik,
        "pnl":       pnl,
        "entry":     w,
        "tp1":       tp1,
        "tp2":       tp2,
        "sl":        sl,
        "rr":        setup["rr"],
        "direction": direction,
        "type":      setup["type"],
    }


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_okx(interval: str, total: int, end_ts_s: float | None = None) -> list[dict]:
    bar      = {"15m": "15m", "1h": "1H"}[interval]
    inst_id  = "SOL-USDT-SWAP"
    result   = []
    after_ms = str(int(end_ts_s * 1000)) if end_ts_s else ""
    while len(result) < total:
        params = {"instId": inst_id, "bar": bar, "limit": "100"}
        if after_ms:
            params["after"] = after_ms
        try:
            r = requests.get("https://www.okx.com/api/v5/market/history-candles",
                             params=params, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            print(f"[okx] Blad: {e}"); break
        if not data:
            break
        batch = [{"time": int(d[0]) // 1000, "open": float(d[1]), "high": float(d[2]),
                  "low": float(d[3]), "close": float(d[4]), "volume": float(d[5])}
                 for d in data]
        batch.sort(key=lambda c: c["time"])
        result   = batch + result
        after_ms = str(int(batch[0]["time"] * 1000))
        if len(batch) < 2:
            break
    seen   = set()
    deduped = [c for c in result if c["time"] not in seen and not seen.add(c["time"])]
    deduped.sort(key=lambda c: c["time"])
    return deduped[-total:] if len(deduped) > total else deduped


def _parse_dt(s: str) -> int:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Zly format daty: {s!r}")


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


# ── Main backtest loop ────────────────────────────────────────────────────────

def run_backtest(from_ts: int, to_ts: int, max_rng_atr: float | None) -> None:
    span_days  = (to_ts - from_ts) / 86400
    m15_needed = int(span_days * 96) + 200    # 96 świec M15 na dobę + bufor
    h1_needed  = int(span_days * 24) + 120    # + kontekst 5 dni wstecz na EMAs

    print(f"\nPobieranie danych M15 ({m15_needed} świec)…")
    all_m15 = fetch_okx("15m", m15_needed, end_ts_s=to_ts)
    print(f"Pobieranie danych H1 ({h1_needed} świec)…")
    all_h1  = fetch_okx("1h",  h1_needed,  end_ts_s=to_ts)
    print(f"Pobrano: {len(all_m15)} M15, {len(all_h1)} H1\n")

    m15_window = [c for c in all_m15 if c["time"] >= from_ts]
    if not m15_window:
        print("Brak danych M15 w zadanym oknie. Sprobuj --synthetic.")
        return
    print(f"Swiec M15 w oknie: {len(m15_window)} "
          f"({_fmt(m15_window[0]['time'])} → {_fmt(m15_window[-1]['time'])})")
    _run_loop(m15_window, all_m15, all_h1, max_rng_atr)


def _print_summary(label: str, results: list[dict]) -> None:
    if not results:
        print(f"\n{'─'*60}")
        print(f"Wersja {label}: BRAK SETUPOW")
        return

    total     = len(results)
    wins      = [r for r in results if r["wynik"] not in ("SL", "timeout", "open")]
    losses    = [r for r in results if r["wynik"] == "SL"]
    timeouts  = [r for r in results if r["wynik"] in ("timeout", "open")]
    pnl_total = sum(r["pnl"] for r in results)
    pnl_wins  = sum(r["pnl"] for r in wins)
    pnl_loss  = sum(r["pnl"] for r in losses)
    wr        = len(wins) / (total - len(timeouts)) * 100 if (total - len(timeouts)) > 0 else 0

    by_type: dict[str, list] = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r)

    print(f"\n{'═'*60}")
    print(f"Wersja {label}")
    print(f"{'─'*60}")
    print(f"  Setupy:   {total}  |  Wygrane: {len(wins)}  |  SL: {len(losses)}  |  Timeout: {len(timeouts)}")
    print(f"  WR:       {wr:.0f}%")
    print(f"  PnL:      {pnl_total:+.2f}$  (wins: {pnl_wins:+.2f}  SL: {pnl_loss:+.2f})")

    for t, rs in sorted(by_type.items()):
        w2 = [r for r in rs if r["wynik"] not in ("SL", "timeout")]
        p2 = sum(r["pnl"] for r in rs)
        wr2 = len(w2) / len(rs) * 100
        print(f"  [{t}] {len(rs)} setupow  WR={wr2:.0f}%  PnL={p2:+.2f}$")

    # Wyniki
    cnt: dict[str, int] = {}
    for r in results:
        cnt[r["wynik"]] = cnt.get(r["wynik"], 0) + 1
    print(f"  Wyniki:   {dict(sorted(cnt.items()))}")


def _print_detail(results: list[dict], version: str) -> None:
    if not results:
        return
    print(f"\n  Szczegoly wersji {version}:")
    print(f"  {'Data':12} {'Typ':20} {'Kier':6} {'W':7} {'TP1':7} {'TP2':7} {'SL':7} "
          f"{'RR':5} {'Wynik':10} {'PnL':8}")
    print(f"  {'─'*95}")
    for r in sorted(results, key=lambda x: x["time"]):
        print(f"  {_fmt(r['time']):12} {r['type']:20} {r['direction']:6} "
              f"{r['entry']:7.2f} {r['tp1']:7.2f} {r['tp2']:7.2f} {r['sl']:7.2f} "
              f"{r['rr']:5.1f} {r['wynik']:10} {r['pnl']:+8.2f}$")


# ── Synthetic data generator ──────────────────────────────────────────────────

def generate_synthetic(from_ts: int, to_ts: int, seed: int = 42) -> tuple[list[dict], list[dict]]:
    """
    Generuje realistyczne dane syntetyczne SOL/USDT imitujące konsolidację z kwietnia 2026.
    Zwraca (candles_m15, candles_h1).

    Parametry rynku:
    - Range: support=81.50, resistance=86.50 (5$ szerokości, ~6%)
    - Spike do ~88 po ok 7 dniach (testuje spike filter)
    - ATR M15 ≈ 0.45-0.65$
    - Trend sentyment: lekko bycze na początku, potem mieszany
    """
    rng = random.Random(seed)

    sup    = 81.50
    res    = 86.50
    mid    = (sup + res) / 2   # 84.00
    WIDTH  = res - sup          # 5.00

    m15_interval  = 15 * 60
    h1_interval   = 3600
    m15_total_secs = to_ts - from_ts
    n_m15 = m15_total_secs // m15_interval + 1

    # Generuj ścieżkę cen (close) na M15
    # Mean-reversion do środka range, z losowymi oscylacjami
    price = mid + rng.uniform(-0.5, 0.5)
    prices_close: list[float] = []

    # Podziel na fazy:
    #   0- 40% czasu: oscylacja w range, lekki trend UP
    #   40-55% czasu: spike do 88 i powrót
    #   55-100%: oscylacja w range, mieszane EMA

    spike_start = int(n_m15 * 0.43)
    spike_peak  = int(n_m15 * 0.48)
    spike_end   = int(n_m15 * 0.54)

    for i in range(n_m15):
        # Mean reversion siła
        if spike_start <= i < spike_end:
            if i < spike_peak:
                # spike w górę
                target = 88.0
                mr_strength = 0.04
            else:
                # powrót po spike
                target = mid + 0.5
                mr_strength = 0.06
        else:
            # Faza range: mean-reversion do środka z losowością
            # Im bliżej krawędzi, tym silniejszy powrót
            dist_to_sup = price - sup
            dist_to_res = res - price
            if dist_to_sup < WIDTH * 0.15:   # blisko dołu → bycze odbicie
                target = sup + WIDTH * 0.35
                mr_strength = 0.09
            elif dist_to_res < WIDTH * 0.15:  # blisko góry → niedźwiedzie odbicie
                target = res - WIDTH * 0.35
                mr_strength = 0.09
            else:
                target = mid
                mr_strength = 0.020

        # Mały szum (realistyczne M15 dla SOL w range)
        noise = rng.gauss(0, 0.12)
        price = price + mr_strength * (target - price) + noise
        # Ogranicz do range ±małe wybicia
        price = max(sup - 0.60, min(res + 0.60, price))
        price = round(price, 3)
        prices_close.append(price)

    # Generuj świece M15 z close, open, high, low, vol
    # ATR M15 ~0.20-0.35$ → H1 ATR ~0.35-0.55$ (realistyczne dla SOL w konsolidacji)
    candles_m15: list[dict] = []
    for i, close in enumerate(prices_close):
        ts = from_ts + i * m15_interval
        open_p = prices_close[i - 1] if i > 0 else close + rng.gauss(0, 0.05)
        atr_sim = rng.uniform(0.12, 0.28)
        # Kierunek świecy
        if close >= open_p:
            high = max(open_p, close) + rng.uniform(0.03, atr_sim * 0.6)
            low  = min(open_p, close) - rng.uniform(0.03, atr_sim * 0.4)
        else:
            high = max(open_p, close) + rng.uniform(0.03, atr_sim * 0.4)
            low  = min(open_p, close) - rng.uniform(0.03, atr_sim * 0.6)
        candles_m15.append({
            "time":   ts,
            "open":   round(open_p, 3),
            "high":   round(high, 3),
            "low":    round(low, 3),
            "close":  round(close, 3),
            "volume": round(rng.uniform(5000, 25000), 0),
        })

    # Agreguj M15 → H1 (każde 4 świece M15)
    candles_h1: list[dict] = []
    for j in range(0, len(candles_m15) - 3, 4):
        batch = candles_m15[j:j + 4]
        candles_h1.append({
            "time":   batch[0]["time"],
            "open":   batch[0]["open"],
            "high":   max(c["high"] for c in batch),
            "low":    min(c["low"]  for c in batch),
            "close":  batch[-1]["close"],
            "volume": sum(c["volume"] for c in batch),
        })

    # Dodaj pre-history H1 (60 świec wstecz) żeby EMA miały kontekst
    pre_price  = mid + rng.uniform(-0.8, 0.8)
    pre_h1: list[dict] = []
    for k in range(60):
        ts = from_ts - (60 - k) * h1_interval
        noise = rng.gauss(0, 0.18)
        pre_price = pre_price + 0.02 * (mid - pre_price) + noise
        pre_price = round(max(sup - 1.5, min(res + 1.5, pre_price)), 3)
        h = pre_price + rng.uniform(0.08, 0.30)
        l = pre_price - rng.uniform(0.08, 0.30)
        o = pre_price + rng.gauss(0, 0.10)
        pre_h1.append({"time": ts, "open": round(o, 3), "high": round(h, 3),
                       "low": round(l, 3), "close": pre_price, "volume": 30000})

    all_h1 = pre_h1 + candles_h1
    return candles_m15, all_h1


def run_backtest_synthetic(from_ts: int, to_ts: int, max_rng_atr: float | None,
                           seed: int = 42) -> None:
    print(f"\nGenerowanie danych syntetycznych (seed={seed})…")
    all_m15, all_h1 = generate_synthetic(from_ts, to_ts, seed)
    m15_window = [c for c in all_m15 if c["time"] >= from_ts]
    print(f"Wygenerowano: {len(all_m15)} M15, {len(all_h1)} H1")
    print(f"Swiec M15 w oknie: {len(m15_window)} "
          f"({_fmt(m15_window[0]['time'])} → {_fmt(m15_window[-1]['time'])})")

    _run_loop(m15_window, all_m15, all_h1, max_rng_atr)


# ── Entry point ───────────────────────────────────────────────────────────────

def _run_loop(m15_window: list[dict], all_m15: list[dict], all_h1: list[dict],
              max_rng_atr: float | None) -> None:
    results_a: list[dict] = []
    results_b: list[dict] = []

    last_entry_a: dict[str, int] = {}
    last_entry_b: dict[str, int] = {}

    for i, candle in enumerate(m15_window):
        t = candle["time"]

        ctx_h1 = [c for c in all_h1 if c["time"] <= t]
        if len(ctx_h1) < 60:
            continue

        # M15 kontekst (min 60 świec = 15h do filtrów MA30/MA60 na M15)
        ctx_m15 = [c for c in all_m15 if c["time"] <= t]
        if len(ctx_m15) < 60:
            continue

        price = candle["close"]
        atr   = calc_atr(ctx_h1[-20:])
        if atr <= 0:
            continue

        future = m15_window[i + 1: i + 81]
        if len(future) < 16:
            continue

        for s in gen_version_a(ctx_h1, ctx_m15, price, atr, max_rng_atr):
            key = s["direction"]
            if i - last_entry_a.get(key, -COOLDOWN_M15) < COOLDOWN_M15:
                continue
            res = simulate(s, future)
            if res:
                res["time"] = t
                results_a.append(res)
                last_entry_a[key] = i

        for s in gen_version_b(ctx_h1, ctx_m15, price, atr, max_rng_atr):
            key = s["direction"]
            if i - last_entry_b.get(key, -COOLDOWN_M15) < COOLDOWN_M15:
                continue
            res = simulate(s, future)
            if res:
                res["time"] = t
                results_b.append(res)
                last_entry_b[key] = i

    _print_summary("A — aktualna (% range, oba kierunki)", results_a)
    _print_summary("B — nowa    (EMA direction + EMA TPs)", results_b)
    _print_detail(results_a, "A")
    _print_detail(results_b, "B")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Range Backtest v A vs B")
    parser.add_argument("--from",  dest="date_from", default="2026-04-01",
                        help="Start (YYYY-MM-DD lub YYYY-MM-DD HH:MM)")
    parser.add_argument("--to",    dest="date_to",
                        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        help="Koniec (domyslnie: dzis)")
    parser.add_argument("--max-rng-atr", type=float, default=None,
                        help="Max szerokosc range w ATR (np. 4.0). Domyslnie: brak limitu")
    parser.add_argument("--synthetic", action="store_true",
                        help="Uzyj syntetycznych danych zamiast API (gdy brak dostepu)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Seed dla danych syntetycznych (domyslnie: 42)")
    args = parser.parse_args()

    from_ts = _parse_dt(args.date_from)
    to_ts   = _parse_dt(args.date_to) + 86400  # include full day
    rng_lim = args.max_rng_atr

    print(f"Range Backtest: {args.date_from} → {args.date_to}")
    print(f"Max range/ATR: {rng_lim if rng_lim else 'brak limitu'}")

    if args.synthetic:
        run_backtest_synthetic(from_ts, to_ts, rng_lim, args.seed)
    else:
        run_backtest(from_ts, to_ts, rng_lim)
