#!/usr/bin/env python3
"""
SOL Alert Bot
Wykrywa setupy tradingowe na SOL/USDT i wysyła alerty na Telegram.
Sprawdza: Range Trading + Breakout Retest na M15/H1.
"""

import os
import json
import requests
from datetime import datetime, timezone

# ── Konfiguracja ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN",  "8645260464:AAGe_uTew0H1gJnijdcR7oav_A4U8n1HLHI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7442390334")
SYMBOL          = "SOLUSDT"
MIN_SCORE       = 11          # Minimum punktów żeby wysłać alert
COOLDOWN_HOURS  = 4           # Ile godzin ciszy po tym samym setupie
LAST_ALERT_FILE = "last_alert.json"


# ── Binance API ───────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str, limit: int = 100) -> list[dict]:
    """Pobiera dane OHLCV z Binance (bez klucza API)."""
    r = requests.get(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=10
    )
    r.raise_for_status()
    return [
        {
            "time":   d[0],
            "open":   float(d[1]),
            "high":   float(d[2]),
            "low":    float(d[3]),
            "close":  float(d[4]),
            "volume": float(d[5]),
        }
        for d in r.json()
    ]


# ── Wskaźniki techniczne ──────────────────────────────────────────────────────
def calc_atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range."""
    trs = [
        max(c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"]  - p["close"]))
        for c, p in zip(candles[1:], candles)
    ]
    return sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0


def h1_trend(candles_h1: list[dict]) -> str:
    """Trend na H1: bullish / bearish / neutral."""
    closes = [c["close"] for c in candles_h1[-20:]]
    fast = sum(closes[-5:])  / 5
    slow = sum(closes[-20:]) / 20
    pct  = (fast - slow) / slow * 100
    if pct >  1.0: return "bullish"
    if pct < -1.0: return "bearish"
    return "neutral"


def impulse_strength(candles_m15: list[dict]) -> int:
    """Siła impulsu poprzedzającego setup (0–3)."""
    atr = calc_atr(candles_m15)
    sizes = [abs(c["close"] - c["open"]) for c in candles_m15[-15:-5]]
    avg   = sum(sizes) / len(sizes) if sizes else 0
    ratio = avg / atr if atr > 0 else 0
    if ratio >= 1.4: return 3
    if ratio >= 0.9: return 2
    if ratio >= 0.5: return 1
    return 0


# ── Wykrywanie zakresu ────────────────────────────────────────────────────────
def detect_range(candles: list[dict], n: int = 32) -> dict:
    """
    Analizuje ostatnie n świec M15 (~8h) i zwraca:
    resistance, support, range_size, r_touches, s_touches
    """
    recent     = candles[-n:]
    resistance = max(c["high"] for c in recent)
    support    = min(c["low"]  for c in recent)
    rng_size   = resistance - support
    zone       = rng_size * 0.06  # 6% zakresu = strefa dotknięcia

    return {
        "resistance": round(resistance, 2),
        "support":    round(support,    2),
        "range_size": round(rng_size,   2),
        "r_touches":  sum(1 for c in recent if c["high"] >= resistance - zone),
        "s_touches":  sum(1 for c in recent if c["low"]  <= support    + zone),
    }


# ── Punktacja ─────────────────────────────────────────────────────────────────
def score_range_size(size: float) -> int:
    """Filar 2: Potencjał ruchu (sweet spot ~1–2 USD)."""
    if 1.2 <= size <= 2.0: return 3
    if 0.8 <= size <  1.2 or 2.0 < size <= 3.0: return 2
    if 0.5 <= size <  0.8 or 3.0 < size <= 4.0: return 1
    return 0


def score_rr(rr: float) -> int:
    """Filar 4: Relacja RR."""
    if rr >= 2.5: return 3
    if rr >= 2.0: return 2
    if rr >= 1.5: return 1
    return 0


def rr_calc(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    return round(abs(tp - entry) / risk, 2) if risk > 0 else 0.0


def build_scores(touches: int, rng_size: float, trend: str,
                 direction: str, rr: float, candles_m15: list[dict]) -> dict:
    ctx = 3 if (
        (direction == "long"  and trend == "bullish") or
        (direction == "short" and trend == "bearish")
    ) else (2 if trend == "neutral" else 1)

    return {
        "poziom":   min(3, touches),
        "ruch":     score_range_size(rng_size),
        "kontekst": ctx,
        "rr":       score_rr(rr),
        "impuls":   impulse_strength(candles_m15),
    }


# ── Kierunek ruchu ceny ───────────────────────────────────────────────────────
def is_moving_toward(candles: list[dict], direction: str) -> bool:
    """
    Sprawdza czy cena zmierza w danym kierunku (ostatnie 4 świece).
    direction='down' → cena opada (w stronę wsparcia)
    direction='up'   → cena rośnie (w stronę oporu)
    """
    closes = [c["close"] for c in candles[-4:]]
    if direction == "down":
        return closes[-1] < closes[0]
    return closes[-1] > closes[0]


# ── Setup: Range Trading ──────────────────────────────────────────────────────
def check_range_setup(candles_m15, candles_h1, rng) -> list[dict]:
    """
    Alert wyprzedzający: wysyłany gdy cena zbliża się do granicy zakresu,
    zanim tam dotrze — żeby zdążyć ustawić zlecenia limit.
    Strefa alertu: 10–35% szerokości zakresu przed poziomem.
    """
    setups  = []
    current = candles_m15[-1]["close"]
    trend   = h1_trend(candles_h1)
    size    = rng["range_size"]

    if size < 0.5:
        return []

    near  = size * 0.10   # minimalny dystans od poziomu (już "przy nim")
    far   = size * 0.35   # maksymalny dystans (jeszcze za daleko)

    # ── Long przy wsparciu ────────────────────────────────────────────────────
    dist_to_support = current - rng["support"]
    if (trend != "bearish"
            and near <= dist_to_support <= far
            and is_moving_toward(candles_m15, "down")):
        base    = rng["support"]
        entries = [round(base + 0.05, 2), round(base - 0.20, 2), round(base - 0.40, 2)]
        sl      = round(base - 0.65, 2)
        tp1     = round((rng["support"] + rng["resistance"]) / 2, 2)
        tp2     = round(rng["resistance"] - 0.10, 2)
        rr      = rr_calc(entries[0], sl, tp2)

        if rr >= 1.5:
            scores = build_scores(rng["s_touches"], size, trend, "long", rr, candles_m15)
            total  = sum(scores.values())
            if total >= MIN_SCORE:
                setups.append({
                    "type": "Range", "direction": "long", "level": base,
                    "scores": scores, "total": total,
                    "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr,
                })

    # ── Short przy oporze ─────────────────────────────────────────────────────
    dist_to_resistance = rng["resistance"] - current
    if (trend != "bullish"
            and near <= dist_to_resistance <= far
            and is_moving_toward(candles_m15, "up")):
        base    = rng["resistance"]
        entries = [round(base - 0.05, 2), round(base + 0.20, 2), round(base + 0.40, 2)]
        sl      = round(base + 0.65, 2)
        tp1     = round((rng["support"] + rng["resistance"]) / 2, 2)
        tp2     = round(rng["support"] + 0.10, 2)
        rr      = rr_calc(entries[0], sl, tp2)

        if rr >= 1.5:
            scores = build_scores(rng["r_touches"], size, trend, "short", rr, candles_m15)
            total  = sum(scores.values())
            if total >= MIN_SCORE:
                setups.append({
                    "type": "Range", "direction": "short", "level": base,
                    "scores": scores, "total": total,
                    "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr,
                })

    return setups


# ── Setup: Breakout Retest ────────────────────────────────────────────────────
def check_breakout_retest(candles_m15, candles_h1, rng) -> list[dict]:
    """Retest wybitego poziomu (wsparcia lub oporu)."""
    setups   = []
    current  = candles_m15[-1]["close"]
    trend    = h1_trend(candles_h1)
    size     = rng["range_size"]
    lookback = candles_m15[-12:-1]
    zone     = size * 0.04  # 4% zakresu = strefa retestu

    # ── Bullish retest ────────────────────────────────────────────────────────
    if trend != "bearish":
        for c in lookback:
            if c["close"] > rng["resistance"] and c["close"] > c["open"]:
                if abs(current - rng["resistance"]) <= zone:
                    base    = rng["resistance"]
                    entries = [round(base + 0.05, 2), round(base - 0.20, 2), round(base - 0.40, 2)]
                    sl      = round(base - 0.70, 2)
                    tp1     = round(base + size * 0.5, 2)
                    tp2     = round(base + size,       2)
                    rr      = rr_calc(entries[0], sl, tp2)

                    if rr >= 1.5:
                        scores = build_scores(rng["r_touches"], size, trend, "long", rr, candles_m15)
                        total  = sum(scores.values())
                        if total >= MIN_SCORE:
                            setups.append({
                                "type": "Breakout Retest", "direction": "long", "level": base,
                                "scores": scores, "total": total,
                                "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr,
                            })
                break

    # ── Bearish retest ────────────────────────────────────────────────────────
    if trend != "bullish":
        for c in lookback:
            if c["close"] < rng["support"] and c["open"] > c["close"]:
                if abs(current - rng["support"]) <= zone:
                    base    = rng["support"]
                    entries = [round(base - 0.05, 2), round(base + 0.20, 2), round(base + 0.40, 2)]
                    sl      = round(base + 0.70, 2)
                    tp1     = round(base - size * 0.5, 2)
                    tp2     = round(base - size,       2)
                    rr      = rr_calc(entries[0], sl, tp2)

                    if rr >= 1.5:
                        scores = build_scores(rng["s_touches"], size, trend, "short", rr, candles_m15)
                        total  = sum(scores.values())
                        if total >= MIN_SCORE:
                            setups.append({
                                "type": "Breakout Retest", "direction": "short", "level": base,
                                "scores": scores, "total": total,
                                "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr,
                            })
                break

    return setups


# ── Anti-spam (cooldown) ──────────────────────────────────────────────────────
def was_recently_alerted(level: float, direction: str) -> bool:
    if not os.path.exists(LAST_ALERT_FILE):
        return False
    try:
        with open(LAST_ALERT_FILE) as f:
            data = json.load(f)
        last = datetime.fromisoformat(data["time"])
        now  = datetime.now(timezone.utc)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        hours = (now - last).total_seconds() / 3600
        return (
            abs(data.get("level", 0) - level) < 0.5 and
            data.get("direction") == direction and
            hours < COOLDOWN_HOURS
        )
    except Exception:
        return False


def save_alert(level: float, direction: str):
    with open(LAST_ALERT_FILE, "w") as f:
        json.dump({
            "level":     level,
            "direction": direction,
            "time":      datetime.now(timezone.utc).isoformat(),
        }, f)


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10
    )
    r.raise_for_status()


def format_alert(s: dict, current_price: float) -> str:
    sc        = s["scores"]
    dir_icon  = "📈 Long" if s["direction"] == "long" else "📉 Short"
    dist      = abs(current_price - s["level"])
    entries   = "\n".join(f"  W{i+1}: ${e:.2f}" for i, e in enumerate(s["entries"]))
    tps       = "\n".join(
        f"  TP{i+1}: ${tp:.2f}  (+${abs(tp - s['entries'][0]):.2f})"
        for i, tp in enumerate(s["tps"])
    )
    now = datetime.now().strftime("%d.%m  %H:%M")

    return (
        f"🎯 <b>SOL/USDT – {s['type']} [{s['total']}/15]</b>\n"
        f"{dir_icon}  |  {now}\n\n"
        f"Cena teraz: <b>${current_price:.2f}</b>\n"
        f"Strefa wejscia za: ~<b>${dist:.2f}</b>\n\n"
        f"Poziom {sc['poziom']} · Ruch {sc['ruch']} · Kontekst {sc['kontekst']} · "
        f"RR {sc['rr']} · Impuls {sc['impuls']}\n\n"
        f"<b>Ustaw zlecenia:</b>\n{entries}\n\n"
        f"<b>SL:</b>  ${s['sl']:.2f}\n\n"
        f"<b>Cele:</b>\n{tps}\n\n"
        f"<b>RR:</b>  {s['rr']:.1f}:1\n\n"
        f"⚠️ <i>Decyzja nalezy do Ciebie.</i>"
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] SOL Alert – start")

    candles_m15 = fetch_klines(SYMBOL, "15m", limit=100)
    candles_h1  = fetch_klines(SYMBOL, "1h",  limit=50)
    current     = candles_m15[-1]["close"]
    rng         = detect_range(candles_m15)
    trend       = h1_trend(candles_h1)

    print(f"SOL: ${current:.2f} | Zakres: ${rng['support']}–${rng['resistance']} "
          f"(${rng['range_size']:.2f}) | Trend H1: {trend}")

    all_setups = (
        check_range_setup(candles_m15, candles_h1, rng) +
        check_breakout_retest(candles_m15, candles_h1, rng)
    )

    if not all_setups:
        print("Brak setupu >=11/15.")
        return

    best = max(all_setups, key=lambda x: x["total"])

    if was_recently_alerted(best["level"], best["direction"]):
        print(f"Cooldown - ten setup byl juz wyslany (4h). Score: {best['total']}/15")
        return

    message = format_alert(best, current)
    send_telegram(message)
    save_alert(best["level"], best["direction"])
    print(f"✅ Alert wysłany!  {best['type']} {best['direction']}  [{best['total']}/15]")


if __name__ == "__main__":
    main()
