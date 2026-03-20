#!/usr/bin/env python3
"""
SOL Alert Bot v2
Algorytm vs Claude Sonnet — porównanie dwóch podejść do detekcji setupów SOL/USDT
"""

import os
import json
import re
import requests
import anthropic
import gspread
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

TZ = ZoneInfo("Europe/Warsaw")

# ── Konfiguracja ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",  "8645260464:AAGe_uTew0H1gJnijdcR7oav_A4U8n1HLHI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7442390334")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
SYMBOL           = "SOLUSDT"
MIN_SCORE        = 10
COOLDOWN_HOURS   = 4
PENDING_FILE     = "pending_setups.json"
COOLDOWN_FILE    = "last_alerts.json"
SHEET_ID         = "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE"
ENTRY_TIMEOUT_H  = 2
TRADE_TIMEOUT_H  = 24


# ── System prompt dla Claude ──────────────────────────────────────────────────
FORTECA_PROMPT = """Jesteś analitykiem technicznym SOL/USDT. Analizujesz dane OHLCV i szukasz setupów tradingowych.

Szukasz jednego z 7 setupów Forteca:
1. Korekta do 50% impulsu — wejście warstwowe w strefie 38–62% Fibo
2. Płytka korekta ~38% — mocny trend, wejście przy 38%
3. Retest wybicia — cena wraca do wybitego poziomu S/R
4. Konfluencja: retest + 50% impulsu — zbieg poziomu S/R z Fibo
5. Fałszywe wybicie — szpila poza poziom, szybki powrót
6. Breakout z konsolidacji — wybicie zakresu z lub bez retestu
7. Range trading — handel przy granicach konsolidacji

Oceniasz setup w 5 filarach (0–3 pkt każdy, max 15):
- trend: zgodność z trendem H1 (3=wyraźny trend zgodny, 2=neutralny, 1=przeciw)
- struktura: jakość impulsu i korekty (3=czytelna, 2=mniej czytelna, 1=chaotyczna)
- poziom: siła poziomu S/R (3=wielokrotnie testowany, 2=solidny, 1=słaby)
- momentum: siła impulsu (3=silny i szybki, 2=umiarkowany, 1=słaby)
- rr: relacja RR (3=≥2.5, 2=2.0–2.5, 1=1.5–2.0, 0=<1.5 → odrzuć)

Zasady:
- SL zawsze techniczny (za strukturą), nigdy matematyczny
- Minimum RR 1.5:1 — jeśli nie osiągalne, setup_found: false
- Nie gonisz rynku — tylko korekty i retesty
- Podajesz konkretne ceny, nigdy "okolice"
- Wejścia warstwowe: 2–3 poziomy

Zwracasz WYŁĄCZNIE poprawny JSON, bez żadnego tekstu przed ani po:

Jeśli setup znaleziony:
{"setup_found":true,"setup_type":"nazwa setupu","direction":"long","score":12,"pillars":{"trend":3,"structure":2,"level":3,"momentum":2,"rr":2},"entries":[88.95,88.70,88.45],"sl":88.10,"tp1":89.80,"tp2":90.60,"rr":2.3,"reasoning":"krótkie uzasadnienie","invalidation":"warunek unieważnienia"}

Jeśli brak setupu:
{"setup_found":false,"reasoning":"dlaczego brak"}"""


# ── CryptoCompare API ─────────────────────────────────────────────────────────
CC_ENDPOINTS = {"15m": ("histominute", 15), "1h": ("histohour", 1)}

def fetch_klines(symbol: str, interval: str, limit: int = 100) -> list[dict]:
    endpoint, aggregate = CC_ENDPOINTS.get(interval, ("histominute", 15))
    fsym = symbol.replace("USDT", "").replace("USD", "")
    r = requests.get(
        f"https://min-api.cryptocompare.com/data/v2/{endpoint}",
        params={"fsym": fsym, "tsym": "USDT", "limit": limit, "aggregate": aggregate},
        timeout=10
    )
    r.raise_for_status()
    return [
        {"time": d["time"], "open": float(d["open"]), "high": float(d["high"]),
         "low": float(d["low"]), "close": float(d["close"]), "volume": float(d["volumefrom"])}
        for d in r.json()["Data"]["Data"]
    ]


# ── Wskaźniki techniczne ──────────────────────────────────────────────────────
def calc_atr(candles: list[dict], period: int = 14) -> float:
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for c, p in zip(candles[1:], candles)]
    return sum(trs[-period:]) / min(period, len(trs)) if trs else 0.0

def h1_trend(candles_h1: list[dict]) -> str:
    closes = [c["close"] for c in candles_h1[-20:]]
    pct = (sum(closes[-5:]) / 5 - sum(closes[-20:]) / 20) / (sum(closes[-20:]) / 20) * 100
    if pct > 1.0:  return "bullish"
    if pct < -1.0: return "bearish"
    return "neutral"

def impulse_strength(candles_m15: list[dict]) -> int:
    atr   = calc_atr(candles_m15)
    sizes = [abs(c["close"] - c["open"]) for c in candles_m15[-15:-5]]
    ratio = (sum(sizes) / len(sizes) if sizes else 0) / atr if atr > 0 else 0
    if ratio >= 1.4: return 3
    if ratio >= 0.9: return 2
    if ratio >= 0.5: return 1
    return 0

def detect_range(candles: list[dict], n: int = 32) -> dict:
    recent     = candles[-n:]
    resistance = max(c["high"] for c in recent)
    support    = min(c["low"]  for c in recent)
    rng_size   = resistance - support
    zone       = rng_size * 0.06
    return {
        "resistance": round(resistance, 2), "support": round(support, 2),
        "range_size": round(rng_size, 2),
        "r_touches": sum(1 for c in recent if c["high"] >= resistance - zone),
        "s_touches": sum(1 for c in recent if c["low"]  <= support    + zone),
    }


# ── Punktacja algorytmu ───────────────────────────────────────────────────────
def score_range_size(size: float) -> int:
    if 1.2 <= size <= 2.0: return 3
    if 0.8 <= size <  1.2 or 2.0 < size <= 3.0: return 2
    if 0.5 <= size <  0.8 or 3.0 < size <= 4.0: return 1
    return 0

def score_rr(rr: float) -> int:
    if rr >= 2.5: return 3
    if rr >= 2.0: return 2
    if rr >= 1.5: return 1
    return 0

def rr_calc(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    return round(abs(tp - entry) / risk, 2) if risk > 0 else 0.0

def build_scores(touches, rng_size, trend, direction, rr, candles_m15) -> dict:
    ctx = 3 if ((direction == "long" and trend == "bullish") or
                (direction == "short" and trend == "bearish")) else (2 if trend == "neutral" else 1)
    return {"trend": ctx, "structure": min(3, touches), "level": score_range_size(rng_size),
            "momentum": impulse_strength(candles_m15), "rr": score_rr(rr)}

def is_moving_toward(candles: list[dict], direction: str) -> bool:
    closes = [c["close"] for c in candles[-4:]]
    return closes[-1] < closes[0] if direction == "down" else closes[-1] > closes[0]


# ── Algorytmiczne setupy ──────────────────────────────────────────────────────
def algo_detect(candles_m15, candles_h1, rng) -> list[dict]:
    setups  = []
    current = candles_m15[-1]["close"]
    trend   = h1_trend(candles_h1)
    size    = rng["range_size"]
    if size < 0.5: return []

    near, far = size * 0.10, size * 0.35

    # Long przy wsparciu
    if trend != "bearish" and near <= current - rng["support"] <= far and is_moving_toward(candles_m15, "down"):
        base    = rng["support"]
        entries = [round(base + 0.05, 2), round(base - 0.20, 2), round(base - 0.40, 2)]
        sl      = round(base - 0.65, 2)
        tp1     = round((rng["support"] + rng["resistance"]) / 2, 2)
        tp2     = round(rng["resistance"] - 0.10, 2)
        rr      = rr_calc(entries[0], sl, tp2)
        if rr >= 1.5:
            scores = build_scores(rng["s_touches"], size, trend, "long", rr, candles_m15)
            total  = sum(scores.values())
            setups.append({"type": "Range", "direction": "long", "level": base,
                           "pillars": scores, "total": total,
                           "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr})

    # Short przy oporze
    if trend != "bullish" and near <= rng["resistance"] - current <= far and is_moving_toward(candles_m15, "up"):
        base    = rng["resistance"]
        entries = [round(base - 0.05, 2), round(base + 0.20, 2), round(base + 0.40, 2)]
        sl      = round(base + 0.65, 2)
        tp1     = round((rng["support"] + rng["resistance"]) / 2, 2)
        tp2     = round(rng["support"] + 0.10, 2)
        rr      = rr_calc(entries[0], sl, tp2)
        if rr >= 1.5:
            scores = build_scores(rng["r_touches"], size, trend, "short", rr, candles_m15)
            total  = sum(scores.values())
            setups.append({"type": "Range", "direction": "short", "level": base,
                           "pillars": scores, "total": total,
                           "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr})

    # Breakout retest
    lookback = candles_m15[-12:-1]
    zone     = size * 0.04

    if trend != "bearish":
        for c in lookback:
            if c["close"] > rng["resistance"] and c["close"] > c["open"]:
                if abs(current - rng["resistance"]) <= zone:
                    base    = rng["resistance"]
                    entries = [round(base + 0.05, 2), round(base - 0.20, 2), round(base - 0.40, 2)]
                    sl, tp1, tp2 = round(base - 0.70, 2), round(base + size * 0.5, 2), round(base + size, 2)
                    rr      = rr_calc(entries[0], sl, tp2)
                    if rr >= 1.5:
                        scores = build_scores(rng["r_touches"], size, trend, "long", rr, candles_m15)
                        setups.append({"type": "Breakout Retest", "direction": "long", "level": base,
                                       "pillars": scores, "total": sum(scores.values()),
                                       "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr})
                break

    if trend != "bullish":
        for c in lookback:
            if c["close"] < rng["support"] and c["open"] > c["close"]:
                if abs(current - rng["support"]) <= zone:
                    base    = rng["support"]
                    entries = [round(base - 0.05, 2), round(base + 0.20, 2), round(base + 0.40, 2)]
                    sl, tp1, tp2 = round(base + 0.70, 2), round(base - size * 0.5, 2), round(base - size, 2)
                    rr      = rr_calc(entries[0], sl, tp2)
                    if rr >= 1.5:
                        scores = build_scores(rng["s_touches"], size, trend, "short", rr, candles_m15)
                        setups.append({"type": "Breakout Retest", "direction": "short", "level": base,
                                       "pillars": scores, "total": sum(scores.values()),
                                       "entries": entries, "sl": sl, "tps": [tp1, tp2], "rr": rr})
                break

    return setups


# ── Claude API ────────────────────────────────────────────────────────────────
def call_claude(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not ANTHROPIC_KEY:
        print("[claude] Brak klucza API.")
        return None
    try:
        m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
            f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
            for c in candles_m15[-60:]
        )
        h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
            f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
            for c in candles_h1[-24:]
        )
        user_msg = f"Aktualna cena SOL: ${current_price:.2f}\n\nM15 (ostatnie 60 swiec):\n{m15_csv}\n\nH1 (ostatnie 24 swiece):\n{h1_csv}"

        client   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=FORTECA_PROMPT,
            messages=[{"role": "user", "content": user_msg}]
        )
        text = response.content[0].text.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"[claude] Blad: {e}")
    return None


# ── Google Sheets ─────────────────────────────────────────────────────────────
def _get_sheets():
    """Zwraca (sheet_alerty, sheet_wyniki) — tworzy arkusze jeśli nie istnieją."""
    creds  = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    wb     = client.open_by_key(SHEET_ID)

    # Sheet 1: Alerty
    try:
        sh1 = wb.worksheet("Alerty")
    except gspread.WorksheetNotFound:
        sh1 = wb.add_worksheet("Alerty", rows=1000, cols=20)
        sh1.append_row(["Data", "Godzina", "Model", "Filtr", "Typ", "Kierunek",
                         "Score", "W1", "W2", "W3", "SL", "TP1", "TP2", "RR",
                         "Cena alertu", "Reasoning"])

    # Sheet 2: Wyniki
    try:
        sh2 = wb.worksheet("Wyniki")
    except gspread.WorksheetNotFound:
        sh2 = wb.add_worksheet("Wyniki", rows=1000, cols=12)
        sh2.append_row(["Data alertu", "Model", "Typ", "Kierunek", "Score",
                         "W1", "SL", "TP1", "TP2", "RR", "Wejscie o", "Wynik", "Ruch $"])

    return sh1, sh2


def log_to_alerty(model: str, filter_passed: bool, setup: dict, current_price: float):
    """Zapisuje wykryty setup do Sheet 1 (natychmiast)."""
    try:
        sh1, _ = _get_sheets()
        entries = setup.get("entries", [])
        now     = datetime.now(TZ)
        sh1.append_row([
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M"),
            model,
            "TAK" if filter_passed else "NIE",
            setup.get("type", setup.get("setup_type", "-")),
            setup.get("direction", "-"),
            setup.get("total", setup.get("score", 0)),
            entries[0] if len(entries) > 0 else "-",
            entries[1] if len(entries) > 1 else "-",
            entries[2] if len(entries) > 2 else "-",
            setup.get("sl", "-"),
            setup.get("tp1", setup.get("tps", ["-"])[0]),
            setup.get("tp2", setup.get("tps", ["-", "-"])[1] if len(setup.get("tps", [])) > 1 else "-"),
            setup.get("rr", "-"),
            round(current_price, 2),
            setup.get("reasoning", "-"),
        ])
        print(f"[sheets] Alerty: {model} {setup.get('direction')} [{setup.get('total', setup.get('score'))}]")
    except Exception as e:
        print(f"[sheets] Blad Alerty: {e}")


def log_to_wyniki(s: dict, result: str, entry_ts, move: float):
    """Zapisuje wynik rozwiązanego setupu do Sheet 2."""
    try:
        _, sh2   = _get_sheets()
        alert_dt = datetime.fromisoformat(s["alert_time"]).strftime("%Y-%m-%d %H:%M")
        entry_dt = datetime.utcfromtimestamp(entry_ts).strftime("%H:%M") if entry_ts else "-"
        entries  = s.get("entries", [])
        tps      = s.get("tps", [])
        sh2.append_row([
            alert_dt, s.get("model", "-"),
            s.get("type", s.get("setup_type", "-")),
            s.get("direction", "-"),
            s.get("score", s.get("total", 0)),
            entries[0] if entries else "-",
            s.get("sl", "-"),
            tps[0] if tps else "-",
            tps[1] if len(tps) > 1 else "-",
            s.get("rr", "-"),
            entry_dt, result, round(move, 2),
        ])
        print(f"[sheets] Wyniki: {s.get('model')} {s.get('direction')} -> {result} ${move:.2f}")
    except Exception as e:
        print(f"[sheets] Blad Wyniki: {e}")


# ── Śledzenie setupów (pending) ───────────────────────────────────────────────
def save_pending(setup: dict, model: str, current_price: float):
    pending = []
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            pending = json.load(f)

    entries = setup.get("entries", [])
    tps     = setup.get("tps", [setup.get("tp1"), setup.get("tp2")])
    tps     = [t for t in tps if t is not None]

    pending.append({
        "alert_time":      datetime.now(timezone.utc).isoformat(),
        "alert_timestamp": int(datetime.now(timezone.utc).timestamp()),
        "model":           model,
        "type":            setup.get("type", setup.get("setup_type", "-")),
        "direction":       setup.get("direction", "-"),
        "score":           setup.get("total", setup.get("score", 0)),
        "price_at_alert":  round(current_price, 2),
        "entries":         entries,
        "sl":              setup.get("sl"),
        "tps":             tps,
        "rr":              setup.get("rr", 0),
        "entry_hit_at":    None,
    })

    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2)


def _hits(candle: dict, price: float, direction: str, side: str) -> bool:
    if side in ("entry", "sl"):
        return candle["low"] <= price if direction == "long" else candle["high"] >= price
    if side == "tp":
        return candle["high"] >= price if direction == "long" else candle["low"] <= price
    return False


def check_pending(candles_m15: list[dict]):
    if not os.path.exists(PENDING_FILE): return
    with open(PENDING_FILE) as f:
        pending = json.load(f)
    if not pending: return

    now_ts        = int(datetime.now(timezone.utc).timestamp())
    still_pending = []

    for s in pending:
        age_h       = (now_ts - s["alert_timestamp"]) / 3600
        after_alert = [c for c in candles_m15 if c["time"] > s["alert_timestamp"]]
        w1, sl      = s["entries"][0] if s["entries"] else 0, s["sl"]
        tp1         = s["tps"][0] if s["tps"] else None
        tp2         = s["tps"][1] if len(s["tps"]) > 1 else None
        d           = s["direction"]

        if s["entry_hit_at"] is None:
            hit = next((c["time"] for c in after_alert if _hits(c, w1, d, "entry")), None)
            if hit is None:
                if age_h > ENTRY_TIMEOUT_H:
                    log_to_wyniki(s, "nie weszlo", None, 0)
                    print(f"[pending] {s['model']} {d}: nie weszlo")
                else:
                    still_pending.append(s)
                continue
            s["entry_hit_at"] = hit

        after_entry  = [c for c in candles_m15 if c["time"] >= s["entry_hit_at"]]
        result, move = None, 0.0

        for c in after_entry:
            sl_hit  = _hits(c, sl,  d, "sl")
            tp2_hit = tp2 and _hits(c, tp2, d, "tp")
            tp1_hit = tp1 and _hits(c, tp1, d, "tp")

            if sl_hit and (tp1_hit or tp2_hit):
                result, move = "SL", round(abs(sl - w1), 2); break
            if tp2_hit:
                result, move = "TP2", round(abs(tp2 - w1), 2); break
            if tp1_hit:
                result, move = "TP1", round(abs(tp1 - w1), 2); break
            if sl_hit:
                result, move = "SL",  round(abs(sl - w1), 2); break

        if result:
            log_to_wyniki(s, result, s["entry_hit_at"], move)
            print(f"[pending] {s['model']} {d}: {result} ${move:.2f}")
        elif age_h > TRADE_TIMEOUT_H:
            log_to_wyniki(s, "nieokreslone", s["entry_hit_at"], 0)
        else:
            still_pending.append(s)

    with open(PENDING_FILE, "w") as f:
        json.dump(still_pending, f, indent=2)


# ── Anti-spam ─────────────────────────────────────────────────────────────────
def was_alerted(model: str, level: float, direction: str) -> bool:
    if not os.path.exists(COOLDOWN_FILE): return False
    try:
        data = json.load(open(COOLDOWN_FILE)).get(model, {})
        last = datetime.fromisoformat(data["time"])
        if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return abs(data.get("level", 0) - level) < 0.5 and data.get("direction") == direction and hours < COOLDOWN_HOURS
    except Exception:
        return False

def save_alerted(model: str, level: float, direction: str):
    data = {}
    if os.path.exists(COOLDOWN_FILE):
        with open(COOLDOWN_FILE) as f:
            data = json.load(f)
    data[model] = {"level": level, "direction": direction, "time": datetime.now(timezone.utc).isoformat()}
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(data, f)


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10
    ).raise_for_status()

def format_alert(model: str, setup: dict, current_price: float, filter_passed: bool) -> str:
    entries = setup.get("entries", [])
    tps     = setup.get("tps", [setup.get("tp1"), setup.get("tp2")])
    tps     = [t for t in tps if t is not None]
    score   = setup.get("total", setup.get("score", 0))
    sl      = setup.get("sl", 0)
    rr      = setup.get("rr", 0)
    d       = setup.get("direction", "")
    dist    = abs(current_price - entries[0]) if entries else 0
    icon    = "📈 Long" if d == "long" else "📉 Short"
    filtr   = "✅ filtr" if filter_passed else "⚠️ bez filtra"
    entries_txt = "\n".join(f"  W{i+1}: ${e:.2f}" for i, e in enumerate(entries))
    tps_txt     = "\n".join(f"  TP{i+1}: ${t:.2f}  (+${abs(t - entries[0]):.2f})" for i, t in enumerate(tps)) if entries else "-"
    reasoning   = setup.get("reasoning", "")

    return (
        f"🎯 <b>SOL/USDT [{score}/15] — {model}</b>\n"
        f"{icon}  |  {datetime.now(TZ).strftime('%d.%m  %H:%M')}  |  {filtr}\n\n"
        f"Cena teraz: <b>${current_price:.2f}</b>  (~${dist:.2f} do wejscia)\n\n"
        f"<b>Ustaw zlecenia:</b>\n{entries_txt}\n\n"
        f"<b>SL:</b>  ${sl:.2f}\n\n"
        f"<b>Cele:</b>\n{tps_txt}\n\n"
        f"<b>RR:</b>  {rr:.1f}:1\n"
        + (f"\n<i>{reasoning}</i>\n" if reasoning else "")
        + f"\n⚠️ <i>Decyzja nalezy do Ciebie.</i>"
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] SOL Alert v2 — start")

    candles_m15 = fetch_klines(SYMBOL, "15m", limit=100)
    candles_h1  = fetch_klines(SYMBOL, "1h",  limit=50)
    current     = candles_m15[-1]["close"]
    rng         = detect_range(candles_m15)
    trend       = h1_trend(candles_h1)

    print(f"SOL: ${current:.2f} | Zakres: ${rng['support']}-${rng['resistance']} (${rng['range_size']:.2f}) | H1: {trend}")

    # Sprawdz oczekujace setupy
    check_pending(candles_m15)

    # ── 1. Algorytm ───────────────────────────────────────────────────────────
    algo_setups  = algo_detect(candles_m15, candles_h1, rng)
    filter_passed = bool(algo_setups)
    best_algo    = max(algo_setups, key=lambda x: x["total"]) if algo_setups else None

    if best_algo:
        print(f"[algo] Setup: {best_algo['type']} {best_algo['direction']} [{best_algo['total']}/15]")
        log_to_alerty("Algorytm", filter_passed, best_algo, current)
        if best_algo["total"] >= MIN_SCORE and not was_alerted("Algorytm", best_algo["level"], best_algo["direction"]):
            send_telegram(format_alert("Algorytm", best_algo, current, filter_passed))
            save_alerted("Algorytm", best_algo["level"], best_algo["direction"])
            save_pending(best_algo, "Algorytm", current)
    else:
        print("[algo] Brak setupu.")

    # ── 2. Claude ─────────────────────────────────────────────────────────────
    print("[claude] Wysylam dane do analizy...")
    claude_result = call_claude(candles_m15, candles_h1, current)

    if claude_result:
        if claude_result.get("setup_found"):
            score = claude_result.get("score", 0)
            direction = claude_result.get("direction", "-")
            entries = claude_result.get("entries", [current])
            level   = entries[0] if entries else current
            print(f"[claude] Setup: {claude_result.get('setup_type')} {direction} [{score}/15]")
            log_to_alerty("Claude", filter_passed, claude_result, current)
            if score >= MIN_SCORE and not was_alerted("Claude", level, direction):
                send_telegram(format_alert("Claude", claude_result, current, filter_passed))
                save_alerted("Claude", level, direction)
                save_pending(claude_result, "Claude", current)
        else:
            print(f"[claude] Brak setupu: {claude_result.get('reasoning', '')}")
    else:
        print("[claude] Brak odpowiedzi.")


if __name__ == "__main__":
    main()
