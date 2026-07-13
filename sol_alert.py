#!/usr/bin/env python3
"""
SOL Alert Bot v2
Algorytm vs Claude Sonnet — porównanie dwóch podejść do detekcji setupów SOL/USDT
"""

import logging
import math
import os
import json
import re
import time
import threading
import requests
import openai
import gspread
import concurrent.futures
import google.generativeai as genai
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
import exchange_trader
import db

TZ = ZoneInfo("Europe/Warsaw")
log = logging.getLogger(__name__)

# ── Konfiguracja ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY       = os.getenv("OPENAI_API_KEY", "")
XAI_KEY          = os.getenv("XAI_API_KEY", "")
GEMINI_KEY       = os.getenv("GEMINI_API_KEY", "")
SYMBOL           = "SOLUSDT"
TRADE_USDT       = float(os.getenv("BITGET_TRADE_USDT", "100"))
LEVERAGE         = 20
MIN_SCORE        = 9
COOLDOWN_HOURS   = 4
SHEET_ID         = "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE"
ENTRY_TIMEOUT_H      = 4
TRADE_TIMEOUT_H      = 24
OPEN_TRADE_TIMEOUT_H = 16   # timeout od wejścia (entry_hit_at) dla otwartych setupów
MIN_SL_DISTANCE  = 0.30   # minimalna odleglosc W1-SL w USD; ponizej = odrzucony setup
MIN_GROK_BIAS_PROC = 65   # minimalny bias_proc Groka; ponizej = sygnał odrzucony jako zbyt niepewny
ENABLE_CLAUDE        = False  # wyłączony tymczasowo — kod zachowany
ENABLE_GPT           = False  # wyłączony tymczasowo — kod zachowany
ENABLE_GPT_RELAXED   = False  # wyłączony tymczasowo — zastąpiony przez GPT3
ENABLE_GPT3          = False  # standalone GPT3 detektor — wyłączony
ENABLE_GPT3_VALIDATOR = True  # GPT3 jako filtr Algo2 setupów — aktywny (backtest Mar 15-29: +$19.76)
ENABLE_GROK          = False  # wyłączony — zastąpiony przez Algo2 (algorytmiczne setupy)
ENABLE_GEMINI2       = False  # Gemini niezależny detektor — gołe świece H1, bez Algo
ALGO2_SHADOW_MODE    = False  # LIVE — best candidate (jeśli enabled w type_configs) idzie na realny handel

# ── Feedback z ostatniego uruchomienia (odczytywany przez dashboard) ──────────
_last_feedback: dict = {}  # {"Algo2": {...}, "Grok": {...}}

# ── Stan ostatniego potwierdzonego impulsu (filtr fałszywych odwrotów) ─────────
# Zapisywany przy każdym IMPULSE detection score >= IMPULSE_COOLDOWN_MIN_SCORE.
# Blokuje impuls w przeciwnym kierunku przez IMPULSE_COOLDOWN_SEC LUB do momentu
# gdy cena cofnie się o IMPULSE_COOLDOWN_RETRACE_PCT pierwotnego ruchu.
IMPULSE_COOLDOWN_SEC          = 3 * 3600   # 3 godziny
IMPULSE_COOLDOWN_MIN_SCORE    = 5          # tylko silne impulsy aktywują blokadę
IMPULSE_COOLDOWN_RETRACE_PCT  = 0.50       # 50% retrace = blokada odpada
_last_confirmed_impulse: dict = {}         # direction, time, start_price, peak_price, score


def _is_type_bitget_enabled(setup_type: str, variant: str | None) -> bool:
    """Zwraca True jeśli typ+wariant ma Bitget enabled=True w ustawieniach aplikacji."""
    try:
        settings = db.get_app_settings()
        key = f"{setup_type}__{variant or 'baseline'}"
        cfg = (settings.get("type_configs") or {}).get(key, {})
        return bool(cfg.get("enabled", False))
    except Exception:
        return False


def _clean_log(text: str) -> str:
    """Usuwa linie separatorów (===) i timestamp-header z logów algo."""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # pomiń linie złożone głównie z '='
        if stripped and len(stripped) > 3 and stripped.count("=") / len(stripped) > 0.7:
            continue
        lines.append(stripped)
    return "\n".join(lines)


# ── GPT3 Validator — ocenia setup wygenerowany przez Algo2 ───────────────────
GPT3_VALIDATOR_SYSTEM_PROMPT = """Jesteś ekspertem od oceny jakości setupów tradingowych na SOL/USDT.

Algorytm wykrył potencjalny setup transakcyjny. Twoim jedynym zadaniem jest ocenić czy ten setup powinien zostać wykonany.

Otrzymujesz:
- Dane setupu: typ, kierunek, poziom wejścia, SL, TP1, TP2
- Aktualną cenę i kontekst strukturalny (ATR, support, resistance, pozycja w range)
- 50 świec H1 i 100 świec M15 (OHLCV) do własnej oceny kontekstu

Oceniasz setup pod kątem:
1. Czy reżim rynkowy (który sam określasz z danych) wspiera ten typ setupu?
2. Czy poziom wejścia ma sens strukturalnie (jest przy istotnym poziomie, nie w środku niczego)?
3. Czy SL i TP są logiczne względem aktualnej struktury?
4. Czy nie ma oczywistych powodów odrzucenia (np. setup long w silnym downtrend, wejście pod oporem)?

Zatwierdź setup gdy: reżim wspiera kierunek, poziom wejścia sensowny, brak oczywistych sygnałów contra.
Odrzuć setup gdy: reżim sprzeczny z kierunkiem, poziom wejścia bez sensu strukturalnego, setup long w crash, itp.

Zwróć WYŁĄCZNIE poprawny JSON:
{"approve":true,"reason":"krótkie uzasadnienie max 1 zdanie","confidence":85}
lub
{"approve":false,"reason":"krótkie uzasadnienie max 1 zdanie","confidence":80}

- approve: true lub false
- reason: max 1 zdanie, konkretne
- confidence: 0-100, Twoja pewność co do decyzji"""


def build_gpt3_validator_prompt(
    setup: dict,
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
    atr: float | None = None,
    support: float | None = None,
    resistance: float | None = None,
    price_pct_in_range: float | None = None,
) -> str:
    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-100:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-50:]
    )

    entries = setup.get("entries", [])
    tps = setup.get("tps", [])
    setup_block = (
        f"typ: {setup.get('type', '?')}\n"
        f"kierunek: {setup.get('direction', '?')}\n"
        f"wejście: {entries[0] if entries else '?'}\n"
        f"SL: {setup.get('sl', '?')}\n"
        f"TP1: {tps[0] if len(tps) > 0 else '?'}\n"
        f"TP2: {tps[1] if len(tps) > 1 else '?'}\n"
        f"RR: {setup.get('rr', '?')}"
    )

    ctx_lines = [f"aktualna cena SOL: ${current_price:.2f}"]
    if support is not None and resistance is not None:
        ctx_lines.append(f"support H1: ${support:.2f} | resistance H1: ${resistance:.2f}")
    if price_pct_in_range is not None:
        ctx_lines.append(f"pozycja w H1 range: {price_pct_in_range:.0f}%")
    if atr is not None:
        ctx_lines.append(f"ATR(14): ${atr:.3f}")
    ctx_block = "\n".join(f"- {l}" for l in ctx_lines)

    return (
        f"Oceń poniższy setup wygenerowany przez algorytm.\n\n"
        f"Setup:\n{setup_block}\n\n"
        f"Kontekst rynkowy:\n{ctx_block}\n\n"
        f"H1 candles (50):\n{h1_csv}\n\n"
        f"M15 candles (100):\n{m15_csv}\n\n"
        f"Określ reżim samodzielnie z danych i zdecyduj: approve true/false.\n"
        f"Zwróć wyłącznie JSON."
    )


_GPT3_VALIDATOR_TIMEOUT_S = 60


def call_gpt3_validator(
    setup: dict,
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
    atr: float | None = None,
    support: float | None = None,
    resistance: float | None = None,
    price_pct_in_range: float | None = None,
) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt3-val] Brak klucza API.")
        return None

    user_msg = build_gpt3_validator_prompt(
        setup, candles_m15, candles_h1, current_price,
        atr=atr, support=support, resistance=resistance,
        price_pct_in_range=price_pct_in_range,
    )

    def _call() -> str:
        client = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=256,
            messages=[
                {"role": "system", "content": GPT3_VALIDATOR_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
        )
        return response.choices[0].message.content.strip()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                text = future.result(timeout=_GPT3_VALIDATOR_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"[gpt3-val] Timeout ({_GPT3_VALIDATOR_TIMEOUT_S}s)")
                future.cancel()
                return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            print(f"[gpt3-val] Brak JSON: {text[:200]}")
            return None
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        print(f"[gpt3-val] Blad JSON: {e}")
        return None
    except Exception as e:
        print(f"[gpt3-val] Blad: {e}")
        return None


# ── Gemini2 — niezależny detektor, gołe świece H1 ───────────────────────────

_GEMINI2_TIMEOUT_S = 60
_GEMINI2_MIN_PROBABILITY = 50

_GEMINI2_SCHEMA = {
    "type": "object",
    "properties": {
        "trend":          {"type": "string", "enum": ["uptrend", "downtrend", "consolidation", "impulse_up", "impulse_down"]},
        "side":           {"type": "string", "enum": ["long", "short"]},
        "sentiment":      {"type": "string"},
        "supports":       {"type": "array", "items": {"type": "number"}},
        "resistances":    {"type": "array", "items": {"type": "number"}},
        "recommendation": {"type": "string"},
        "reasoning":      {"type": "string"},
        "shortReasoning": {"type": "string"},
        "entryType":      {"type": "string", "enum": ["pullback", "market", "breakout"]},
        "profitTarget":   {"type": "number"},
        "stopLoss":       {"type": "number"},
        "entryPrice":     {"type": "number"},
        "riskReward":     {"type": "number"},
        "probability":    {"type": "number"},
    },
    "required": ["trend", "side", "sentiment", "supports", "resistances",
                 "recommendation", "reasoning", "shortReasoning", "entryType",
                 "riskReward", "probability", "profitTarget", "stopLoss", "entryPrice"],
}


def call_gemini2(candles_h1: list[dict], symbol: str) -> dict | None:
    if not GEMINI_KEY:
        print("[gemini2] Brak klucza API.")
        return None
    candle_data = candles_h1[-60:]
    prompt = (
        f"Analiza techniczna dla {symbol}. Dane: {json.dumps(candle_data)}. "
        "Trend (uptrend, downtrend, consolidation, impulse_up, impulse_down). "
        "Wyznacz trade setup: wejście (entryPrice), cel (profitTarget), poziom obronny (stopLoss). "
        "WAŻNE dla Kierunku (side): "
        "- Jeśli side to \"long\": profitTarget MUSI być wyższy niż entryPrice, stopLoss MUSI być niższy. "
        "- Jeśli side to \"short\": profitTarget MUSI być niższy niż entryPrice, stopLoss MUSI być wyższy. "
        "RR (Risk/Reward) powyżej 2.0. Prawdopodobieństwo 0-100. "
        "Głębokie uzasadnienie (reasoning) i krótkie (shortReasoning) po polsku. ZWRÓĆ JSON."
    )

    def _call() -> dict:
        genai.configure(api_key=GEMINI_KEY)
        model = genai.GenerativeModel("gemini-3-flash-preview")
        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                response_schema=_GEMINI2_SCHEMA,
                temperature=0.2,
            ),
        )
        return json.loads(response.text)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                return future.result(timeout=_GEMINI2_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"[gemini2] Timeout ({_GEMINI2_TIMEOUT_S}s)")
                future.cancel()
                return None
    except Exception as e:
        print(f"[gemini2] Błąd: {e}")
        return None


def _gemini2_to_setup(g: dict) -> dict:
    reasoning = (
        f"[Gemini2] {g.get('shortReasoning', '')}\n"
        f"Trend: {g['trend']} | Typ wejścia: {g.get('entryType', '?')} | Prob: {g.get('probability', 0)}%\n"
        f"{g.get('reasoning', '')}"
    )
    return {
        "type":         f"gemini2_{g['trend']}",
        "direction":    g["side"],
        "entries":      [g["entryPrice"]],
        "tps":          [g["profitTarget"]],
        "sl":           g["stopLoss"],
        "sl_after_tp1": g["entryPrice"],
        "rr":           g.get("riskReward", 0),
        "score":        round(g.get("probability", 0) / 10, 1),
        "variant":      "gemini2",
        "reasoning":    reasoning,
    }


def gemini2_main():
    """Wywoływana co godzinę przez scheduler. Niezależna od main() / Algo2."""
    if not ENABLE_GEMINI2:
        print("[gemini2] Wyłączony (ENABLE_GEMINI2=False).")
        return

    candles_h1 = fetch_klines(SYMBOL, "1h", limit=60)
    current    = fetch_current_price(SYMBOL) or candles_h1[-1]["close"]

    g2_raw = call_gemini2(candles_h1, SYMBOL)
    if g2_raw is None:
        print("[gemini2] Brak odpowiedzi od modelu.")
        return

    prob = g2_raw.get("probability", 0)
    if prob <= _GEMINI2_MIN_PROBABILITY:
        print(f"[gemini2] Odrzucony — probability={prob}% ≤ {_GEMINI2_MIN_PROBABILITY}%")
        return

    setup = _gemini2_to_setup(g2_raw)
    rr_ok = setup["rr"] >= 2.0
    sl_ok = abs(setup["entries"][0] - setup["sl"]) >= MIN_SL_DISTANCE
    if not rr_ok or not sl_ok:
        print(f"[gemini2] Odrzucony — RR={setup['rr']:.2f}, SL_dist={abs(setup['entries'][0] - setup['sl']):.2f}")
        return

    print(f"[gemini2] Setup {setup['direction']} entry={setup['entries'][0]} TP={setup['tps'][0]} SL={setup['sl']} RR={setup['rr']:.2f} prob={prob}%")
    save_pending(setup, "Gemini2", "", current, tradeable=False)


# ── Bitget API — cena na żywo ────────────────────────────────────────────────
def fetch_current_price(symbol: str) -> float | None:
    """Pobiera aktualną cenę last z tickera Bitget futures."""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/ticker",
            params={"symbol": symbol, "productType": "USDT-FUTURES"},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("data") or []
        if data:
            return float(data[0]["lastPr"])
    except Exception as e:
        print(f"[ticker] Błąd pobierania ceny: {e}")
    return None


# ── Bitget API — świece ───────────────────────────────────────────────────────
_BITGET_GRANULARITY = {"15m": "15m", "1h": "1H"}

def fetch_klines(symbol: str, interval: str, limit: int = 100) -> list[dict]:
    bg_symbol = symbol  # Bitget candles API używa SOLUSDT (bez sufixu U)
    granularity = _BITGET_GRANULARITY.get(interval, "15min")
    end_time_ms = str(int(time.time() * 1000))
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    params = {
        "symbol":      bg_symbol,
        "productType": "USDT-FUTURES",
        "granularity": granularity,
        "limit":       str(limit),
        "endTime":     end_time_ms,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    raw = r.json()
    data = raw.get("data") or []
    # Debug: loguj pierwszy i ostatni element (newest first) + ewentualne błędy
    if data:
        newest_ts = datetime.fromtimestamp(int(data[0][0]) // 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        oldest_ts = datetime.fromtimestamp(int(data[-1][0]) // 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"[fetch] {granularity} {len(data)} candles: {oldest_ts} → {newest_ts} UTC | now={now_utc} | endpoint=candles")
    else:
        print(f"[fetch] {granularity} EMPTY response | endTime={end_time_ms} | raw={str(raw)[:200]}")
    # /candles zwraca oldest-first, /history-candles zwracał newest-first
    # sort() zapewnia zawsze oldest-first niezależnie od endpointu
    candles = [
        {
            "time":   int(d[0]) // 1000,
            "open":   float(d[1]),
            "high":   float(d[2]),
            "low":    float(d[3]),
            "close":  float(d[4]),
            "volume": float(d[5]),
        }
        for d in data
    ]
    candles.sort(key=lambda c: c["time"])  # oldest first
    return candles


# ── Bitget API — order book (depth) ──────────────────────────────────────────
def fetch_order_book(symbol: str, limit: str = "50") -> dict | None:
    """Pobiera snapshot order booka (bids/asks) z Bitget futures.
    Zwraca {'bids': [(price, size), ...], 'asks': [(price, size), ...]} — bids malejąco
    od best bid, asks rosnąco od best ask (kolejność jak z API) — lub None przy błędzie."""
    try:
        r = requests.get(
            "https://api.bitget.com/api/v2/mix/market/merge-depth",
            params={"symbol": symbol, "productType": "USDT-FUTURES", "precision": "scale0", "limit": limit},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
        if not bids or not asks:
            return None
        return {"bids": bids, "asks": asks}
    except Exception as e:
        print(f"[orderbook] Błąd pobierania depth: {e}")
        return None


def compute_orderbook_features(ob: dict | None, current_price: float) -> dict:
    """Liczy cechy order booka do market_context (obserwacyjne — nic nie blokuje w handlu):
    - ob_imbalance: udział wolumenu bidów w sumie bid+ask (top N poziomów z merge-depth)
    - ob_spread_pct: spread best bid/ask w % ceny
    - ob_wall_bid_dist_pct / ob_wall_ask_dist_pct: dystans do najbliższej "ściany" (poziom
      z wolumenem >= 3x mediana) po stronie bidów / asków, w % ceny
    Zwraca same None gdy brak danych (np. błąd fetchu — nie blokuje detekcji setupów)."""
    empty = {
        "ob_imbalance": None, "ob_spread_pct": None,
        "ob_wall_bid_dist_pct": None, "ob_wall_ask_dist_pct": None,
    }
    if not ob or not ob.get("bids") or not ob.get("asks") or current_price <= 0:
        return empty

    bids, asks = ob["bids"], ob["asks"]
    best_bid, best_ask = bids[0][0], asks[0][0]

    bid_vol = sum(q for _, q in bids)
    ask_vol = sum(q for _, q in asks)
    imbalance = bid_vol / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else None

    spread_pct = (best_ask - best_bid) / current_price * 100

    sizes = sorted(q for _, q in bids + asks)
    median_size = sizes[len(sizes) // 2] if sizes else 0

    def _nearest_wall_dist(levels):
        if median_size <= 0:
            return None
        for price, qty in levels:
            if qty >= median_size * 3:
                return abs(price - current_price) / current_price * 100
        return None

    wall_bid_dist = _nearest_wall_dist(bids)
    wall_ask_dist = _nearest_wall_dist(asks)
    return {
        "ob_imbalance": round(imbalance, 3) if imbalance is not None else None,
        "ob_spread_pct": round(spread_pct, 4),
        "ob_wall_bid_dist_pct": round(wall_bid_dist, 3) if wall_bid_dist is not None else None,
        "ob_wall_ask_dist_pct": round(wall_ask_dist, 3) if wall_ask_dist is not None else None,
    }


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
    if atr <= 0:
        return 0
    # Sprawdź zarówno starsze świece [-15:-5] jak i ostatnie [-6:]
    sizes_old = [abs(c["close"] - c["open"]) for c in candles_m15[-15:-5]]
    sizes_recent = [abs(c["close"] - c["open"]) for c in candles_m15[-6:]]
    ratio_old = (sum(sizes_old) / len(sizes_old)) / atr if sizes_old else 0
    ratio_recent = (sum(sizes_recent) / len(sizes_recent)) / atr if sizes_recent else 0
    # Bierz większy z dwóch — łapie zarówno trwające jak i świeże impulsy
    ratio = max(ratio_old, ratio_recent)
    if ratio >= 1.4: return 3
    if ratio >= 0.9: return 2
    if ratio >= 0.5: return 1
    return 0

def _count_touches(candles: list[dict], in_zone_fn, min_away: int = 2) -> int:
    """Count independent approaches to a zone, separated by ≥ min_away candles outside it."""
    count = 0
    away_streak = min_away  # treat start as already-away so first touch always counts
    for c in candles:
        if in_zone_fn(c):
            if away_streak >= min_away:
                count += 1
            away_streak = 0
        else:
            away_streak += 1
    return count


def _find_effective_level(
    candles: list[dict],
    start: float,
    atr: float,
    direction: str,
    min_touches: int = 2,
    max_steps: int = 30,
) -> float:
    """Walk inward from a spike extreme until ≥ min_touches independent approaches are found.

    For support: walks upward from the spike low.
    For resistance: walks downward from the spike high.
    Uses a fixed ATR-based proximity zone to avoid circular dependency with range_size.
    """
    zone = atr * 0.4
    step = atr * 0.25
    level = start
    for _ in range(max_steps):
        if direction == "support":
            t = _count_touches(candles, lambda c, l=level: c["low"] <= l + zone)
        else:
            t = _count_touches(candles, lambda c, l=level: c["high"] >= l - zone)
        if t >= min_touches:
            return level
        level = level + step if direction == "support" else level - step
    return level


def detect_range(candles: list[dict], n: int = 32) -> dict:
    recent         = candles[-n:]
    atr            = calc_atr(candles) or 1.0
    abs_support    = min(c["low"]  for c in recent)
    abs_resistance = max(c["high"] for c in recent)
    support    = _find_effective_level(recent, abs_support,    atr, "support")
    resistance = _find_effective_level(recent, abs_resistance, atr, "resistance")
    if support >= resistance:
        support, resistance = abs_support, abs_resistance
    rng_size = resistance - support
    zone     = rng_size * 0.06
    return {
        "resistance": round(resistance, 2), "support": round(support, 2),
        "range_size": round(rng_size, 2),
        "r_touches": _count_touches(recent, lambda c: c["high"] >= resistance - zone),
        "s_touches": _count_touches(recent, lambda c: c["low"]  <= support    + zone),
    }


# ── Detekcja reżimu rynkowego (IMPULSE / TREND / RANGE) ─────────────────────

def detect_market_regime(
    candles_m15: list[dict],
    candles_h1: list[dict],
    current_price: float,
) -> dict:
    """
    Rozpoznaje reżim rynkowy: IMPULSE_UP/DOWN, TREND_UP/DOWN, RANGE.

    Priorytet: IMPULSE > TREND > RANGE.
    IMPULSE = gwałtowny ruch (2-6h), TREND = utrzymujący się kierunek (24-48h),
    RANGE = brak kierunku (domyślny).
    """
    trend = h1_trend(candles_h1)
    imp_str = impulse_strength(candles_m15)

    # S/R z detect_range — zachowane dla kompatybilności (prompt, algo_detect)
    rng = detect_range(candles_h1)

    # ── Volume ratio (ostatnie 2 M15 vs średnia z 10) ────────────────────────
    recent_m15 = candles_m15[-12:]
    avg_vol = sum(c["volume"] for c in recent_m15[:-2]) / max(len(recent_m15[:-2]), 1)
    last_vol = sum(c["volume"] for c in recent_m15[-2:]) / 2
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

    # ── Zmiana cenowa 1h / 2h / 4h / 8h / 12h / 24h / 48h ──────────────────
    # 24h i 48h: średnia z 3 świec wokół punktu referencyjnego.
    # Eliminuje niestabilność gdy pojedyncza świeca trafia na spike/dno impulsu.
    price_1h  = candles_m15[-4]["close"]  if len(candles_m15) >= 4  else candles_m15[0]["close"]
    price_2h  = candles_m15[-8]["close"] if len(candles_m15) >= 8 else candles_m15[0]["close"]
    price_4h  = candles_m15[-16]["close"] if len(candles_m15) >= 16 else candles_m15[0]["close"]
    price_8h  = candles_h1[-8]["close"]  if len(candles_h1) >= 8  else candles_h1[0]["close"]
    price_12h = candles_h1[-12]["close"] if len(candles_h1) >= 12 else candles_h1[0]["close"]
    price_24h = (sum(c["close"] for c in candles_h1[-25:-22]) / 3
                 if len(candles_h1) >= 25 else candles_h1[0]["close"])
    price_48h = (sum(c["close"] for c in candles_h1[-49:-46]) / 3
                 if len(candles_h1) >= 49 else candles_h1[0]["close"])
    change_1h  = (current_price - price_1h)  / price_1h  * 100
    change_2h  = (current_price - price_2h)  / price_2h  * 100
    change_4h  = (current_price - price_4h)  / price_4h  * 100
    change_8h  = (current_price - price_8h)  / price_8h  * 100
    change_12h = (current_price - price_12h) / price_12h * 100
    change_24h = (current_price - price_24h) / price_24h * 100
    change_48h = (current_price - price_48h) / price_48h * 100

    # ── Kierunek ostatnich 6 M15 (dla IMPULSE) ───────────────────────────────
    last6 = candles_m15[-6:]
    bearish_closes = sum(1 for c in last6 if c["close"] < c["open"])
    bullish_closes = sum(1 for c in last6 if c["close"] > c["open"])

    # Bazowy dict zwracany przez każdy reżim
    base = {
        "support": rng["support"], "resistance": rng["resistance"],
        "range_size": rng["range_size"], "vol_ratio": round(vol_ratio, 1),
        "change_1h": round(change_1h, 2), "change_2h": round(change_2h, 2),
        "change_4h": round(change_4h, 2), "change_8h": round(change_8h, 2),
        "change_12h": round(change_12h, 2),
        "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
        "bearish_closes_6m15": bearish_closes, "bullish_closes_6m15": bullish_closes,
        "s_touches": rng.get("s_touches", 0), "r_touches": rng.get("r_touches", 0),
    }

    # ── IMPULSE: breakout 24h high/low + vol >= 2.0x + mocne ciała M15 ──────
    # Impuls = przebicie lokalnego max/min z ostatnich 24 H1, podwyższony wolumen
    # i silne ciała M15. Naturalne wygasanie do TREND gdy vol spada poniżej 2.0x.
    _ref = (candles_h1[-26:-2] if len(candles_h1) >= 26
            else candles_h1[:-2] if len(candles_h1) > 2
            else candles_h1)
    ref_high = max(c["high"] for c in _ref)
    ref_low  = min(c["low"]  for c in _ref)

    broke_high = current_price > ref_high
    broke_low  = current_price < ref_low
    _vol_ok    = vol_ratio >= 2.0
    _imp_ok    = imp_str >= 2

    if (broke_high or broke_low) and _vol_ok and _imp_ok:
        impulse_dir = "up" if broke_high else "down"
        strength    = min(10, int(vol_ratio) + imp_str * 2)

        # Spike-reversal: rejection wicks na ostatnich 3 M15
        spike_reversal_score = 0
        _r3     = candles_m15[-3:]
        _bodies = [abs(c["close"] - c["open"]) + 0.001 for c in _r3]
        _wicks  = (
            [c["high"] - max(c["open"], c["close"]) for c in _r3] if impulse_dir == "up"
            else [min(c["open"], c["close"]) - c["low"] for c in _r3]
        )
        if sum(w / b for w, b in zip(_wicks, _bodies)) / 3 > 1.5:
            spike_reversal_score = 1
            log.info("[REGIME] Spike-reversal: rejection wicks na ostatnich 3 M15")

        # Śledź peak bieżącego impulsu (kontekst dla setupów i potencjalnego cooldownu)
        global _last_confirmed_impulse
        _prev_lci = _last_confirmed_impulse
        if _prev_lci and _prev_lci.get("direction") == impulse_dir:
            _new_peak = (max(current_price, _prev_lci["peak_price"]) if impulse_dir == "up"
                         else min(current_price, _prev_lci["peak_price"]))
            _last_confirmed_impulse = {**_prev_lci, "peak_price": _new_peak, "score": strength}
        else:
            _last_confirmed_impulse = {
                "direction":   impulse_dir,
                "time":        time.time(),
                "start_price": ref_high if impulse_dir == "up" else ref_low,
                "peak_price":  current_price,
                "score":       strength,
            }

        _ref_level = ref_high if impulse_dir == "up" else ref_low
        details = (f"breakout:${_ref_level:.2f}; vol:{vol_ratio:.1f}x; imp:{imp_str}; "
                   f"4h:{change_4h:+.1f}%; spk:{spike_reversal_score}")
        return {
            **base,
            "regime":      f"IMPULSE_{impulse_dir.upper()}",
            "direction":   impulse_dir,
            "score":       strength,
            "spike_score": spike_reversal_score,
            "pct_outside": 0,
            "details":     details,
        }

    # ── TREND: change_24h / change_48h (wygładzone) + lower_lows/higher_highs ──
    h1_12 = candles_h1[-12:]
    lower_lows   = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["low"]  < h1_12[i-1]["low"])
    higher_highs = sum(1 for i in range(1, len(h1_12)) if h1_12[i]["high"] > h1_12[i-1]["high"])

    trend_score = 0
    if abs(change_24h) >= 3.0: trend_score += 2
    elif abs(change_24h) >= 1.5: trend_score += 1
    if abs(change_48h) >= 5.0: trend_score += 2
    elif abs(change_48h) >= 3.0: trend_score += 1
    if lower_lows >= 5: trend_score += 1
    if higher_highs >= 5: trend_score += 1
    if trend != "neutral": trend_score += 1

    has_price_change = abs(change_24h) >= 1.5 or abs(change_48h) >= 3.0

    details = f"4h:{change_4h:+.1f}% 8h:{change_8h:+.1f}% 12h:{change_12h:+.1f}% 24h:{change_24h:+.1f}% 48h:{change_48h:+.1f}% score:{trend_score} ll:{lower_lows} hh:{higher_highs}"

    if trend_score >= 3 and has_price_change:
        if abs(change_48h) >= 3.0:
            trend_dir = "down" if change_48h < 0 else "up"
        elif abs(change_24h) >= 1.5:
            trend_dir = "down" if change_24h < 0 else "up"
        else:
            trend_dir = "down" if lower_lows > higher_highs else "up"

        # Fix 2: ruch 4h silnie przeczy wyznaczonemu kierunkowi + struktura potwierdza.
        if abs(change_4h) >= 2.5:
            recent_dir = "down" if change_4h < 0 else "up"
            if recent_dir != trend_dir:
                if (recent_dir == "down" and lower_lows >= higher_highs) or \
                   (recent_dir == "up"   and higher_highs >= lower_lows):
                    trend_dir = recent_dir

        # Fix 3: multi-timeframe consensus override (spike-then-reversal i inne).
        # change_24h bywa mylący gdy punkt ref. trafił na lokalne dno/szczyt (np. tuż
        # przed short squeeze). Jeśli change_4h, change_8h i change_12h wszystkie
        # zgodnie wskazują w PRZECIWNYM kierunku niż wyznaczony trend_dir — override.
        # W prawdziwym uptrendzie z normalnym pullbackiem przynajmniej change_12h
        # pozostaje pozytywny → warunek nie jest spełniony → brak false positive.
        if trend_dir == "up":
            mtf_down = sum([
                1 if change_4h  < -0.5 else 0,
                1 if change_8h  < -0.5 else 0,
                1 if change_12h < -1.0 else 0,
            ])
            if mtf_down == 3:
                trend_dir = "down"
        elif trend_dir == "down":
            mtf_up = sum([
                1 if change_4h  > 0.5 else 0,
                1 if change_8h  > 0.5 else 0,
                1 if change_12h > 1.0 else 0,
            ])
            if mtf_up == 3:
                trend_dir = "up"

        return {
            **base,
            "regime": f"TREND_{trend_dir.upper()}",
            "direction": trend_dir, "score": trend_score,
            "pct_outside": 0, "details": details,
        }

    # ── RANGE ────────────────────────────────────────────────────────────────
    # Obserwacyjnie (nic nie zmienia w realnym filtrowaniu/handlu): sprawdź, czy
    # krótsze ramy czasowe (4h/8h/12h) zgodnie wskazują trend, mimo że change_24h/48h
    # wypadły płasko (bo punkt odniesienia trafił na lokalny szczyt/dołek — patrz
    # Fix 3 wyżej). Zapisywane do market_context jako regime_alt, do analizy na danych
    # historycznych przed ewentualnym użyciem w prawdziwej klasyfikacji.
    mtf_up_votes = sum([
        1 if change_4h  > 0.5 else 0,
        1 if change_8h  > 0.5 else 0,
        1 if change_12h > 1.0 else 0,
    ])
    mtf_down_votes = sum([
        1 if change_4h  < -0.5 else 0,
        1 if change_8h  < -0.5 else 0,
        1 if change_12h < -1.0 else 0,
    ])
    regime_alt = None
    if mtf_up_votes == 3:
        regime_alt = "TREND_UP"
    elif mtf_down_votes == 3:
        regime_alt = "TREND_DOWN"

    return {
        **base,
        "regime": "RANGE",
        "direction": "none", "score": 0,
        "regime_alt": regime_alt,
        "pct_outside": 0, "details": details,
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
        entries = [round(base + 0.05, 2), round(base - 0.25, 2)]
        sl      = round(base - 0.55, 2)
        tp1     = round((rng["support"] + rng["resistance"]) / 2, 2)
        tp2     = round(min(rng["resistance"] - 0.10, entries[0] + 2.0), 2)
        if abs(tp1 - entries[0]) >= 0.5 and abs(tp2 - entries[0]) >= 1.0:
            rr = rr_calc(sum(entries) / len(entries), sl, tp2)
            if rr >= 1.5:
                scores = build_scores(rng["s_touches"], size, trend, "long", rr, candles_m15)
                total  = sum(scores.values())
                setups.append({"type": "Range", "direction": "long", "level": base,
                               "pillars": scores, "total": total,
                               "entries": entries, "sl": sl, "sl_after_tp1": entries[0],
                               "tps": [tp1, tp2], "rr": rr})

    # Short przy oporze
    if trend != "bullish" and near <= rng["resistance"] - current <= far and is_moving_toward(candles_m15, "up"):
        base    = rng["resistance"]
        entries = [round(base - 0.05, 2), round(base + 0.25, 2)]
        sl      = round(base + 0.55, 2)
        tp1     = round((rng["support"] + rng["resistance"]) / 2, 2)
        tp2     = round(max(rng["support"] + 0.10, entries[0] - 2.0), 2)
        if abs(tp1 - entries[0]) >= 0.5 and abs(tp2 - entries[0]) >= 1.0:
            rr = rr_calc(sum(entries) / len(entries), sl, tp2)
            if rr >= 1.5:
                scores = build_scores(rng["r_touches"], size, trend, "short", rr, candles_m15)
                total  = sum(scores.values())
                setups.append({"type": "Range", "direction": "short", "level": base,
                               "pillars": scores, "total": total,
                               "entries": entries, "sl": sl, "sl_after_tp1": entries[0],
                               "tps": [tp1, tp2], "rr": rr})

    # Breakout retest
    lookback = candles_m15[-12:-1]
    zone     = size * 0.04

    if trend != "bearish":
        for c in lookback:
            if c["close"] > rng["resistance"] and c["close"] > c["open"]:
                if abs(current - rng["resistance"]) <= zone:
                    base    = rng["resistance"]
                    entries = [round(base + 0.05, 2), round(base - 0.25, 2)]
                    sl      = round(base - 0.65, 2)
                    tp1     = round(base + max(size * 0.5, 0.5), 2)
                    tp2     = round(min(base + size, entries[0] + 2.0), 2)
                    if abs(tp1 - entries[0]) >= 0.5 and abs(tp2 - entries[0]) >= 1.0:
                        rr = rr_calc(sum(entries) / len(entries), sl, tp2)
                        if rr >= 1.5:
                            scores = build_scores(rng["r_touches"], size, trend, "long", rr, candles_m15)
                            setups.append({"type": "Breakout Retest", "direction": "long", "level": base,
                                           "pillars": scores, "total": sum(scores.values()),
                                           "entries": entries, "sl": sl, "sl_after_tp1": entries[0],
                                           "tps": [tp1, tp2], "rr": rr})
                break

    if trend != "bullish":
        for c in lookback:
            if c["close"] < rng["support"] and c["open"] > c["close"]:
                if abs(current - rng["support"]) <= zone:
                    base    = rng["support"]
                    entries = [round(base - 0.05, 2), round(base + 0.25, 2)]
                    sl      = round(base + 0.65, 2)
                    tp1     = round(base - max(size * 0.5, 0.5), 2)
                    tp2     = round(max(base - size, entries[0] - 2.0), 2)
                    if abs(tp1 - entries[0]) >= 0.5 and abs(tp2 - entries[0]) >= 1.0:
                        rr = rr_calc(sum(entries) / len(entries), sl, tp2)
                        if rr >= 1.5:
                            scores = build_scores(rng["s_touches"], size, trend, "short", rr, candles_m15)
                            setups.append({"type": "Breakout Retest", "direction": "short", "level": base,
                                           "pillars": scores, "total": sum(scores.values()),
                                           "entries": entries, "sl": sl, "sl_after_tp1": entries[0],
                                           "tps": [tp1, tp2], "rr": rr})
                break

    return setups


# ── Algo2: algorytmiczne setupy (IMPULSE/TREND/RANGE) ───────────────────────

def find_swing_points(candles_h1: list[dict], n: int = 12):
    """Znajduje swing high i swing low z ostatnich n świec H1."""
    recent = candles_h1[-n:]
    return max(c["high"] for c in recent), min(c["low"] for c in recent)


def find_consolidation(candles_h1: list[dict], min_candles: int = 4, max_candles: int = 10):
    """Szuka konsolidacji — wąski zakres w ostatnich świecach H1.
    Iteruje od najszerszego okna do najwęższego, żeby uchwycić faktyczne granice
    konsolidacji zamiast mini-konsolidacji wewnątrz szerszego range."""
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    if atr <= 0:
        return None
    for n in range(min(max_candles, len(candles_h1) - 1), min_candles - 1, -1):
        recent = candles_h1[-n:]
        hi = max(c["high"] for c in recent)
        lo = min(c["low"] for c in recent)
        rng = hi - lo
        if rng < atr * 2.5:
            return {"high": hi, "low": lo, "range": rng, "candles": n}
    return None


# ── Warianty parametrów trend_pullback (kalibracja) ──────────────────────────
# Klucz → (fib_wejście_lo, fib_wejście_hi, fib_sl, atr_sl_mult, strength_min, not_tradeable)
# baseline  = aktualne ustawienia produkcyjne
# str4      = identyczna geometria, ale próg strength obniżony do 4
# shallow   = płytszy pullback (fib25-38) z ciaśniejszym SL (fib50), też strength>=4
_PULLBACK_VARIANTS: dict[str, tuple] = {
    "baseline": (0.38, 0.50, 0.618, 0.3, 5, False),
    "shallow":  (0.25, 0.38, 0.500, 0.1, 4, True),
    "micro":    (0.15, 0.25, 0.382, 0.1, 4, False),
    "deep":     (0.50, 0.618, 0.786, 0.4, 5, True),
}


def algo_detect_setups(regime: dict, candles_m15: list[dict], candles_h1: list[dict],
                       current_price: float, orderbook: dict | None = None) -> tuple[list[dict], str]:
    """Algorytmicznie wykrywa setupy trend/impulse/range.
    `orderbook` (opcjonalny, z fetch_order_book) — snapshot depth, wyłącznie do zapisu
    cech w market_context (obserwacyjne, nic nie zmienia w detekcji/filtrowaniu). None
    w backtest/replay, gdzie nie ma live depth.
    Zwraca (setupy, log_text) — log_text trafia do reasoning/arkusza."""
    regime_name = regime["regime"]
    direction = regime.get("direction", "none")
    atr = calc_atr(candles_h1[-20:]) if len(candles_h1) >= 20 else calc_atr(candles_h1)
    strength = regime.get("score", 0)
    setups = []

    # Logowanie pełnej analizy
    log_lines = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    log_lines.append(f"\n{'='*70}")
    log_lines.append(f"[{now_str}] Algo2 analiza")
    log_lines.append(f"  Cena: ${current_price:.2f} | Reżim: {regime_name}({strength}) | ATR: ${atr:.2f}")
    # Ostatnie 12 H1 świec — zakres
    h1_12 = candles_h1[-12:] if len(candles_h1) >= 12 else candles_h1
    h1_last_ts = datetime.fromtimestamp(candles_h1[-1]["time"], tz=timezone.utc).strftime("%H:%M") if candles_h1 else "?"
    m15_last_ts = datetime.fromtimestamp(candles_m15[-1]["time"], tz=timezone.utc).strftime("%H:%M") if candles_m15 else "?"
    h1_closes = [f"${c['close']:.2f}" for c in h1_12[-6:]]
    log_lines.append(f"  H1 closes (last 6): {', '.join(h1_closes)} [ostatnia H1: {h1_last_ts} UTC]")
    log_lines.append(f"  M15 last close: ${candles_m15[-1]['close']:.2f} [{m15_last_ts} UTC]")

    if atr <= 0:
        log_lines.append(f"  SKIP: ATR <= 0")
        return setups, "\n".join(log_lines)

    # Sprawdź świeżość danych H1 — jeśli ostatnia świeca > 2h temu, dane są przestarzałe
    now_ts = datetime.now(timezone.utc).timestamp()
    h1_age_min = (now_ts - candles_h1[-1]["time"]) / 60 if candles_h1 else 9999
    if h1_age_min > 120:
        log_lines.append(f"  SKIP: dane H1 przestarzałe ({h1_age_min:.0f} min temu)")
        return setups, "\n".join(log_lines)

    # Max dystans entry od aktualnej ceny — odrzuć setupy z nierealistycznym pullbackiem
    max_entry_dist = current_price * 0.03  # 3%
    log_lines.append(f"  Max dystans entry: ${max_entry_dist:.2f} (3%)")

    # ── SYGNAŁY WYCZERPANIA — logowanie bez blokowania setupów ───────────────
    # Dane zbierane w obserwacji; docelowo jeden sygnał blokujący tradeable
    exhaustion_signals: list[str] = []

    # Sygnał 1: Malejąca amplituda kolejnych ekstremów H1
    if len(h1_12) >= 4:
        highs = [c["high"] for c in h1_12[-4:]]
        lows  = [c["low"]  for c in h1_12[-4:]]
        hh_deltas = [highs[i+1] - highs[i] for i in range(len(highs) - 1)]
        ll_deltas = [lows[i] - lows[i+1]   for i in range(len(lows)  - 1)]
        if direction == "up"   and len(hh_deltas) >= 2 and hh_deltas[-1] < hh_deltas[-2]:
            exhaustion_signals.append("malejace_HH")
        if direction == "down" and len(ll_deltas) >= 2 and ll_deltas[-1] < ll_deltas[-2]:
            exhaustion_signals.append("malejace_LL")

    # Sygnał 2: Malejący wolumen na ostatnich 3 świecach M15
    if len(candles_m15) >= 4:
        vols = [c["volume"] for c in candles_m15[-4:]]
        if vols[-1] < vols[-2] < vols[-3]:
            exhaustion_signals.append("malejacy_wolumen_M15")

    # Sygnał 3: Malejące body świec M15 (słabnące momentum)
    if len(candles_m15) >= 4:
        bodies = [abs(c["close"] - c["open"]) for c in candles_m15[-4:]]
        if bodies[-1] < bodies[-2] < bodies[-3]:
            exhaustion_signals.append("malejace_body_M15")

    # Sygnał 4: Cena blisko MA20 H1 — konwergencja do średniej
    if len(candles_h1) >= 20:
        ma20_h1 = sum(c["close"] for c in candles_h1[-20:]) / 20
        dist_to_ma = abs(current_price - ma20_h1)
        if dist_to_ma < atr * 0.5:
            exhaustion_signals.append(f"konwergencja_MA20(${dist_to_ma:.2f})")

    exh_str = ", ".join(exhaustion_signals) if exhaustion_signals else "brak"
    log_lines.append(f"  Exhaustion signals: [{exh_str}]")

    # ── Market context snapshot (ML training data) ───────────────────────
    atr_m15_ctx = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
    ma20_h1 = sum(c["close"] for c in candles_h1[-20:]) / min(20, len(candles_h1)) if candles_h1 else 0
    m15_closes_ctx = [c["close"] for c in candles_m15]
    ma30_m15 = sum(m15_closes_ctx[-30:]) / min(30, len(m15_closes_ctx)) if len(m15_closes_ctx) >= 10 else None
    ma60_m15 = sum(m15_closes_ctx[-60:]) / min(60, len(m15_closes_ctx)) if len(m15_closes_ctx) >= 30 else None
    _ob_features = compute_orderbook_features(orderbook, current_price)

    _ml_ctx = {
        "atr_h1": round(atr, 4),
        "atr_m15": round(atr_m15_ctx, 4) if atr_m15_ctx else None,
        "vol_ratio": regime.get("vol_ratio", 1.0),
        "regime": regime_name,
        "regime_score": strength,
        "regime_direction": direction,
        "regime_alt": regime.get("regime_alt"),
        "change_1h": regime.get("change_1h"), "change_2h": regime.get("change_2h"),
        "change_4h": regime.get("change_4h"), "change_8h": regime.get("change_8h"),
        "change_12h": regime.get("change_12h"),
        "change_24h": regime.get("change_24h"), "change_48h": regime.get("change_48h"),
        "support": regime.get("support"), "resistance": regime.get("resistance"),
        "ma20_h1_dist_pct": round((current_price - ma20_h1) / current_price * 100, 3) if ma20_h1 else None,
        "ma30_m15_dist_pct": round((current_price - ma30_m15) / current_price * 100, 3) if ma30_m15 else None,
        "ma60_m15_dist_pct": round((current_price - ma60_m15) / current_price * 100, 3) if ma60_m15 else None,
        "exhaustion_signals": exhaustion_signals,
        "exhaustion_count": len(exhaustion_signals),
        "bearish_count_6m15": regime.get("bearish_closes_6m15", 0),
        "bullish_count_6m15": regime.get("bullish_closes_6m15", 0),
        "spike_reversal_score": regime.get("spike_score", 0),
        "s_touches": regime.get("s_touches", 0),
        "r_touches": regime.get("r_touches", 0),
        **_ob_features,
    }

    def _setup_ctx(entry_price, sl_price, fib_lvl=None, swing_h=None, swing_l=None):
        ctx = dict(_ml_ctx)
        if swing_h is not None:
            ctx["swing_high"] = round(swing_h, 2)
        if swing_l is not None:
            ctx["swing_low"] = round(swing_l, 2)
        if swing_h is not None and swing_l is not None:
            ctx["swing_range"] = round(swing_h - swing_l, 2)
        # Przełamanie struktury (obserwacyjne, nic nie blokuje): licz ostatnie 2 świece H1,
        # które ZAMKNĘŁY się poza swing_high (short) / swing_low (long) użytym do zbudowania
        # setupu — odróżnia trwałe złamanie trendu od zwykłego korekcyjnego knota.
        boundary = None
        if sl_price is not None and entry_price is not None:
            is_short = sl_price > entry_price
            boundary = swing_h if is_short else swing_l
        if boundary is not None and len(candles_h1) >= 2:
            recent_h1 = candles_h1[-2:]
            closes_beyond = sum(
                1 for c in recent_h1
                if (c["close"] > boundary if is_short else c["close"] < boundary)
            )
            ctx["closes_beyond_structure"] = closes_beyond
            ctx["structure_broken"] = closes_beyond >= 2
        ctx["entry_dist_pct"] = round(abs(current_price - entry_price) / current_price * 100, 3)
        ctx["sl_dist_pct"] = round(abs(entry_price - sl_price) / current_price * 100, 3) if sl_price else None
        if fib_lvl is not None:
            ctx["fib_level"] = round(fib_lvl, 3)
        return ctx
    # ─────────────────────────────────────────────────────────────────────────

    # ── TREND_DOWN / IMPULSE_DOWN ─────────────────────────────────────────
    if direction == "down":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        # Uwzględnij aktualną cenę jako przybliżenie niezamkniętej świecy H1
        swing_low = min(swing_low, current_price)
        swing_high = max(swing_high, current_price)
        log_lines.append(f"  Swing (12 H1+cena): high=${swing_high:.2f} low=${swing_low:.2f} range=${swing_high-swing_low:.2f}")

        # trend_pullback_short — warianty parametrów (baseline + eksperymenty)
        if swing_high > swing_low:
            swing_range = swing_high - swing_low
            for vname, (fib_lo, fib_hi, fib_sl, atr_sl, str_min, v_not_tradeable) in _PULLBACK_VARIANTS.items():
                # str4 generuje tylko gdy strength==4 (baseline już pokrywa strength>=5)
                if vname == "str4" and strength != 4:
                    continue
                if strength < str_min:
                    continue
                entry_mid = (fib_lo + fib_hi) / 2
                w   = round(swing_low + entry_mid * swing_range, 2)
                sl  = round(swing_low + fib_sl * swing_range + atr * atr_sl, 2)
                tp1 = round(swing_low + swing_range * 0.02, 2)
                tp2 = round(swing_low - swing_range * 0.3, 2)
                rr_ok     = sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                above_price = w > current_price * 1.003
                dist_ok   = w - current_price <= max_entry_dist
                rr_val    = round((w - tp1) / (sl - w), 1) if (sl - w) > 0 else 0
                log_lines.append(
                    f"  → pullback_short [{vname}]: fib{fib_lo:.0%}-{fib_hi:.0%} W=${w:.2f} "
                    f"SL=${sl:.2f} RR={rr_val} dist=${w-current_price:.2f} "
                    f"above={above_price} dist_ok={dist_ok} rr_ok={rr_ok}"
                )
                _ctx = _setup_ctx(w, sl, fib_lvl=entry_mid, swing_h=swing_high, swing_l=swing_low)
                if rr_ok and above_price and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED [{vname}]")
                    setups.append({
                        "type": "trend_pullback_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": rr_val,
                        "score": strength,
                        "variant": vname,
                        "tp_strategy": "tp1_tp2",
                        "not_tradeable": v_not_tradeable,
                        "market_context": _ctx,
                        "reasoning": f"{regime_name}({strength}); swing ${swing_low:.0f}-${swing_high:.0f} [{vname}]",
                    })
                else:
                    reasons = []
                    if not rr_ok: reasons.append(f"RR<1.5({rr_val})")
                    if not above_price: reasons.append("W<=cena")
                    if not dist_ok: reasons.append(f"dist>3%({w-current_price:.2f})")
                    log_lines.append(f"    ✗ REJECTED [{vname}]: {', '.join(reasons)}")
                    setups.append({
                        "type": "trend_pullback_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": rr_val,
                        "score": strength, "variant": vname,
                        "market_context": _ctx,
                        "rejected_by_algo": True,
                        "filter_reasons": reasons,
                        "not_tradeable": True,
                    })

            # trend_pullback_short — okno swing 24h zamiast 12h (obserwacyjne, zawsze not_tradeable).
            # Hipoteza: w długim, jednokierunkowym trendzie 12h swing ciągle się zawęża do coraz
            # niższych lokalnych maksimów, więc W1 (fib retracement) wypada coraz bliżej/poniżej
            # ceny zanim zdąży się cokolwiek złożyć. Sprawdzamy czy szersze okno łapie realniejsze
            # pullbacki — sama geometria (fib 38-50%, SL fib61.8%) identyczna jak baseline, różni
            # się tylko długość okna swing. Do porównania po pewnym czasie z wynikami baseline.
            if strength >= 5:
                swing_high_w, swing_low_w = find_swing_points(candles_h1, n=24)
                swing_low_w = min(swing_low_w, current_price)
                swing_high_w = max(swing_high_w, current_price)
                if swing_high_w > swing_low_w:
                    swing_range_w = swing_high_w - swing_low_w
                    entry_mid_w = 0.44  # (0.38+0.50)/2, jak baseline
                    w_w   = round(swing_low_w + entry_mid_w * swing_range_w, 2)
                    sl_w  = round(swing_low_w + 0.618 * swing_range_w + atr * 0.3, 2)
                    tp1_w = round(swing_low_w + swing_range_w * 0.02, 2)
                    tp2_w = round(swing_low_w - swing_range_w * 0.3, 2)
                    rr_ok_w       = sl_w > w_w and tp1_w < w_w and (w_w - tp1_w) / (sl_w - w_w) >= 1.5
                    above_price_w = w_w > current_price * 1.003
                    dist_ok_w     = w_w - current_price <= max_entry_dist
                    rr_val_w      = round((w_w - tp1_w) / (sl_w - w_w), 1) if (sl_w - w_w) > 0 else 0
                    log_lines.append(
                        f"  → pullback_short [swing24h]: W=${w_w:.2f} SL=${sl_w:.2f} RR={rr_val_w} "
                        f"swing24h=${swing_low_w:.2f}-${swing_high_w:.2f} dist=${w_w-current_price:.2f} "
                        f"above={above_price_w} dist_ok={dist_ok_w} rr_ok={rr_ok_w}"
                    )
                    _ctx_w = _setup_ctx(w_w, sl_w, fib_lvl=entry_mid_w, swing_h=swing_high_w, swing_l=swing_low_w)
                    if rr_ok_w and above_price_w and dist_ok_w:
                        log_lines.append(f"    ✓ ACCEPTED [swing24h] (not_tradeable)")
                        setups.append({
                            "type": "trend_pullback_short", "direction": "short",
                            "entries": [w_w], "sl": sl_w, "sl_after_tp1": w_w,
                            "tps": [tp1_w, tp2_w], "rr": rr_val_w,
                            "score": strength, "variant": "swing24h",
                            "tp_strategy": "tp1_tp2",
                            "not_tradeable": True,
                            "market_context": _ctx_w,
                            "reasoning": f"{regime_name}({strength}); swing24h ${swing_low_w:.0f}-${swing_high_w:.0f} [swing24h]",
                        })
                    else:
                        _rej_w = []
                        if not rr_ok_w: _rej_w.append(f"RR<1.5({rr_val_w})")
                        if not above_price_w: _rej_w.append("W<=cena")
                        if not dist_ok_w: _rej_w.append(f"dist>3%({w_w-current_price:.2f})")
                        log_lines.append(f"    ✗ REJECTED [swing24h]: {', '.join(_rej_w)}")
                        setups.append({
                            "type": "trend_pullback_short", "direction": "short",
                            "entries": [w_w], "sl": sl_w, "sl_after_tp1": w_w,
                            "tps": [tp1_w, tp2_w], "rr": rr_val_w,
                            "score": strength, "variant": "swing24h",
                            "market_context": _ctx_w,
                            "rejected_by_algo": True, "filter_reasons": _rej_w, "not_tradeable": True,
                        })

            # trend_pullback_short — pullback potwierdzony na M15 (obserwacyjne, zawsze not_tradeable).
            # Hipoteza: baseline/swing24h liczą fib ze sztywnego okna H1 niezależnie od tego, czy
            # korekta faktycznie w tej chwili trwa. Korekty na tym instrumencie trwają zwykle
            # kilkadziesiąt minut, nie godziny — więc zamiast okna H1 wymagamy realnego potwierdzenia
            # odbicia na M15 (2-3 ostatnie świece kontra-trend) ORAZ że szerszy trend wciąż jest
            # spadkowy (MA30<MA60, jak filtr MA w RANGE) — inaczej to może być już odwrócenie trendu,
            # nie pullback. Dopiero po potwierdzeniu liczymy fib z lokalnego swingu (krótkie okno M15)
            # zamiast starego okna H1. Sama geometria fib (38-50% / SL 61.8%) identyczna jak baseline —
            # zmienia się tylko WARUNEK startu i ANCHOR, żeby porównanie izolowało wpływ triggera.
            if strength >= 5:
                last3_m15_pb = candles_m15[-3:]
                bounce_candles = sum(1 for c in last3_m15_pb if c["close"] > c["open"])
                m15_closes_pb = [c["close"] for c in candles_m15]
                ma30_pb = sum(m15_closes_pb[-30:]) / min(30, len(m15_closes_pb)) if len(m15_closes_pb) >= 10 else None
                ma60_pb = sum(m15_closes_pb[-60:]) / min(60, len(m15_closes_pb)) if len(m15_closes_pb) >= 30 else None
                ma_trend_intact = ma30_pb is not None and ma60_pb is not None and ma30_pb < ma60_pb
                pullback_confirmed = bounce_candles >= 2 and ma_trend_intact
                log_lines.append(
                    f"  → pullback_short [m15_confirmed]: odbicie {bounce_candles}/3 świec M15, "
                    f"MA30={f'${ma30_pb:.2f}' if ma30_pb else 'N/A'} MA60={f'${ma60_pb:.2f}' if ma60_pb else 'N/A'} "
                    f"trend_intact={ma_trend_intact} → {'START KOREKTY' if pullback_confirmed else 'brak korekty'}"
                )
                if pullback_confirmed:
                    swing_high_m, swing_low_m = find_swing_points(candles_m15, n=12)  # ~3h lokalne okno
                    swing_low_m = min(swing_low_m, current_price)
                    swing_high_m = max(swing_high_m, current_price)
                    if swing_high_m > swing_low_m:
                        swing_range_m = swing_high_m - swing_low_m
                        entry_mid_m = 0.44  # (0.38+0.50)/2, jak baseline
                        w_m   = round(swing_low_m + entry_mid_m * swing_range_m, 2)
                        sl_m  = round(swing_low_m + 0.618 * swing_range_m + atr * 0.3, 2)
                        tp1_m = round(swing_low_m + swing_range_m * 0.02, 2)
                        tp2_m = round(swing_low_m - swing_range_m * 0.3, 2)
                        rr_ok_m       = sl_m > w_m and tp1_m < w_m and (w_m - tp1_m) / (sl_m - w_m) >= 1.5
                        above_price_m = w_m > current_price * 1.003
                        dist_ok_m     = w_m - current_price <= max_entry_dist
                        rr_val_m      = round((w_m - tp1_m) / (sl_m - w_m), 1) if (sl_m - w_m) > 0 else 0
                        log_lines.append(
                            f"    W=${w_m:.2f} SL=${sl_m:.2f} RR={rr_val_m} lokalny swing=${swing_low_m:.2f}-${swing_high_m:.2f} "
                            f"dist=${w_m-current_price:.2f} above={above_price_m} dist_ok={dist_ok_m} rr_ok={rr_ok_m}"
                        )
                        _ctx_m = _setup_ctx(w_m, sl_m, fib_lvl=entry_mid_m, swing_h=swing_high_m, swing_l=swing_low_m)
                        if rr_ok_m and above_price_m and dist_ok_m:
                            log_lines.append(f"    ✓ ACCEPTED [m15_confirmed] (not_tradeable)")
                            setups.append({
                                "type": "trend_pullback_short", "direction": "short",
                                "entries": [w_m], "sl": sl_m, "sl_after_tp1": w_m,
                                "tps": [tp1_m, tp2_m], "rr": rr_val_m,
                                "score": strength, "variant": "m15_confirmed",
                                "tp_strategy": "tp1_tp2",
                                "not_tradeable": True,
                                "market_context": _ctx_m,
                                "reasoning": f"{regime_name}({strength}); m15 bounce {bounce_candles}/3, "
                                             f"swing ${swing_low_m:.0f}-${swing_high_m:.0f} [m15_confirmed]",
                            })
                        else:
                            _rej_m = []
                            if not rr_ok_m: _rej_m.append(f"RR<1.5({rr_val_m})")
                            if not above_price_m: _rej_m.append("W<=cena")
                            if not dist_ok_m: _rej_m.append(f"dist>3%({w_m-current_price:.2f})")
                            log_lines.append(f"    ✗ REJECTED [m15_confirmed]: {', '.join(_rej_m)}")
                            setups.append({
                                "type": "trend_pullback_short", "direction": "short",
                                "entries": [w_m], "sl": sl_m, "sl_after_tp1": w_m,
                                "tps": [tp1_m, tp2_m], "rr": rr_val_m,
                                "score": strength, "variant": "m15_confirmed",
                                "market_context": _ctx_m,
                                "rejected_by_algo": True, "filter_reasons": _rej_m, "not_tradeable": True,
                            })

            # trend_pullback_short ATR-based — entry = cena + 0.5*ATR, SL = cena + 1.2*ATR
            atr_m15_pb = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            w_atr   = round(current_price + atr_m15_pb * 0.5, 2)
            sl_atr  = round(current_price + atr_m15_pb * 1.5, 2)
            tp1_atr = round(current_price - atr_m15_pb * 1.0, 2)
            tp2_atr = round(current_price - atr_m15_pb * 2.5, 2)
            rr_atr  = round((w_atr - tp1_atr) / (sl_atr - w_atr), 1) if (sl_atr - w_atr) > 0 else 0
            rr_ok_atr   = tp1_atr < w_atr and rr_atr >= 1.5
            above_atr   = w_atr > current_price * 1.003
            dist_ok_atr = w_atr - current_price <= max_entry_dist
            log_lines.append(
                f"  → pullback_short [atr_based]: W=${w_atr:.2f} SL=${sl_atr:.2f} "
                f"TP1=${tp1_atr:.2f} RR={rr_atr} ATR_M15=${atr_m15_pb:.2f}"
            )
            _ctx_atr = _setup_ctx(w_atr, sl_atr, swing_h=swing_high, swing_l=swing_low)
            if rr_ok_atr and above_atr and dist_ok_atr:
                log_lines.append(f"    ✓ ACCEPTED [atr_based] (not_tradeable)")
                setups.append({
                    "type": "trend_pullback_short", "direction": "short",
                    "entries": [w_atr], "sl": sl_atr, "sl_after_tp1": w_atr,
                    "tps": [tp1_atr, tp2_atr], "rr": rr_atr,
                    "score": strength, "variant": "atr_based",
                    "tp_strategy": "tp1_tp2",
                    "not_tradeable": True,
                    "market_context": _ctx_atr,
                    "reasoning": f"{regime_name}({strength}); ATR_M15=${atr_m15_pb:.2f} pullback [atr_based]",
                })
            else:
                _rej_atr = []
                if not rr_ok_atr: _rej_atr.append(f"RR<1.5({rr_atr})")
                if not above_atr: _rej_atr.append("W<=cena")
                if not dist_ok_atr: _rej_atr.append("dist")
                log_lines.append(f"    ✗ REJECTED [atr_based]: {', '.join(_rej_atr)}")
                setups.append({
                    "type": "trend_pullback_short", "direction": "short",
                    "entries": [w_atr], "sl": sl_atr, "sl_after_tp1": w_atr,
                    "tps": [tp1_atr, tp2_atr], "rr": rr_atr,
                    "score": strength, "variant": "atr_based",
                    "market_context": _ctx_atr,
                    "rejected_by_algo": True, "filter_reasons": _rej_atr, "not_tradeable": True,
                })

        # impulse_continuation_short
        if regime_name.startswith("IMPULSE_"):
            _cont_spike = regime.get("spike_score", 0)
            atr_m15 = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            last6 = candles_m15[-6:]
            greens = [c for c in last6 if c["close"] > c["open"]]
            log_lines.append(f"  → impulse_cont_short: greens={len(greens)}/6 (need 1-2) spike={_cont_spike}")
            if _cont_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_cont_spike}>=2 (odwrót)")
            elif len(greens) >= 1 and len(greens) <= 2:
                pullback_high = max(c["high"] for c in last6[-2:])
                w = round(pullback_high, 2)
                sl = round(pullback_high + atr * 0.8, 2)
                tp1 = round(current_price - atr_m15 * 1.5, 2)
                tp2 = round(current_price - atr_m15 * 2.5, 2)
                rr_ok = sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                dist_ok = abs(w - current_price) <= max_entry_dist
                log_lines.append(f"    W=${w:.2f} SL=${sl:.2f} TP1=${tp1:.2f} ATR_M15=${atr_m15:.2f} dist=${abs(w-current_price):.2f} rr_ok={rr_ok} dist_ok={dist_ok}")
                if rr_ok and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED")
                    setups.append({
                        "type": "impulse_continuation_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((w - tp1) / (sl - w), 1),
                        "score": strength,
                        "variant": "baseline",
                        "market_context": _setup_ctx(w, sl, swing_h=swing_high, swing_l=swing_low),
                        "reasoning": f"{regime_name}({strength}); pullback M15 cont",
                    })
                else:
                    _rej = []
                    if not rr_ok: _rej.append("RR<1.5")
                    if not dist_ok: _rej.append(f"dist>{max_entry_dist}")
                    log_lines.append(f"    ✗ REJECTED: {', '.join(_rej)}")
                    setups.append({
                        "type": "impulse_continuation_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((w - tp1) / (sl - w), 1) if (sl - w) > 0 else 0,
                        "score": strength, "variant": "baseline",
                        "market_context": _setup_ctx(w, sl, swing_h=swing_high, swing_l=swing_low),
                        "rejected_by_algo": True, "filter_reasons": _rej, "not_tradeable": True,
                    })

        # impulse_aggressive_short — dwa warianty ATR (h1_atr vs m15_atr) dla porównania
        if regime_name.startswith("IMPULSE_"):
            _agg_vol   = regime.get("vol_ratio", 1.0)
            _agg_spike = regime.get("spike_score", 0)
            atr_m15 = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            log_lines.append(f"  → impulse_aggressive: vol={_agg_vol:.1f}x spike={_agg_spike} ATR_H1=${atr:.2f} ATR_M15=${atr_m15:.2f}")
            if _agg_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_agg_spike}>=2")
            elif _agg_vol < 2.0:
                log_lines.append(f"    ✗ SKIP: vol={_agg_vol:.1f}x<2.0")
            else:
                w = round(current_price, 2)
                for _vname, _vatr, _tp2_mult in [("h1_atr", atr, 3.0), ("m15_atr", atr_m15, 3.0)]:
                    _is_m15 = _vname == "m15_atr"
                    _sl  = round(current_price + _vatr * 1.2, 2)
                    _tp1 = round(current_price - _vatr * 2.0, 2)
                    _tp2 = round(current_price - _vatr * _tp2_mult, 2)
                    _rr_ok = _tp1 < w and (w - _tp1) / (_sl - w) >= 1.5
                    log_lines.append(f"    [{_vname}] W=${w:.2f} SL=${_sl:.2f} TP1=${_tp1:.2f} rr_ok={_rr_ok}")
                    if _rr_ok:
                        log_lines.append(f"    ✓ ACCEPTED [{_vname}] (not_tradeable — tryb testowy)")
                        setups.append({
                            "type": "impulse_aggressive_short", "direction": "short",
                            "entries": [w], "sl": _sl, "sl_after_tp1": w,
                            "tps": [_tp1, _tp2], "rr": round((w - _tp1) / (_sl - w), 1),
                            "score": strength, "variant": _vname,
                            "tp_strategy": "tp1_only",
                            "not_tradeable": True,
                            "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                            "reasoning": f"{regime_name}({strength}); vol={_agg_vol:.1f}x aggressive [{_vname}]",
                        })
                    else:
                        log_lines.append(f"    ✗ REJECTED [{_vname}]: RR<1.5")
                        setups.append({
                            "type": "impulse_aggressive_short", "direction": "short",
                            "entries": [w], "sl": _sl, "sl_after_tp1": w,
                            "tps": [_tp1, _tp2], "rr": round((w - _tp1) / (_sl - w), 1) if (_sl - w) > 0 else 0,
                            "score": strength, "variant": _vname,
                            "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                            "rejected_by_algo": True, "filter_reasons": ["RR<1.5"], "not_tradeable": True,
                        })

        # impulse_aggressive_short (trend_boost) — lokalny impuls w TREND_DOWN bez vol_ratio
        if regime_name == "TREND_DOWN":
            _c1h  = (current_price - candles_m15[-4]["close"]) / candles_m15[-4]["close"] * 100 if len(candles_m15) >= 4 else 0
            _c2h  = (current_price - candles_m15[-8]["close"]) / candles_m15[-8]["close"] * 100 if len(candles_m15) >= 8 else 0
            _imp  = impulse_strength(candles_m15)
            _last6_s = candles_m15[-6:]
            _bearish_s = sum(1 for c in _last6_s if c["close"] < c["open"])
            _last2_s = candles_m15[-2:]
            _spike_s = sum(1 for c in _last2_s if (min(c["open"], c["close"]) - c["low"]) > abs(c["close"] - c["open"]) * 1.5)
            log_lines.append(f"  → aggressive_trend_boost_short: c1h={_c1h:+.1f}% c2h={_c2h:+.1f}% imp={_imp} bearish={_bearish_s}/6 spike={_spike_s}")
            if _c2h <= -2.0 and _imp >= 2 and _bearish_s >= 4 and _spike_s == 0:
                w    = round(current_price, 2)
                _sl  = round(current_price + atr * 1.2, 2)
                _tp1 = round(current_price - atr * 2.0, 2)
                _tp2 = round(current_price - atr * 3.0, 2)
                _rr_ok = _tp1 < w and (w - _tp1) / (_sl - w) >= 1.5
                log_lines.append(f"    W=${w:.2f} SL=${_sl:.2f} TP1=${_tp1:.2f} rr_ok={_rr_ok}")
                if _rr_ok:
                    log_lines.append(f"    ✓ ACCEPTED [trend_boost] (not_tradeable — wariant testowy)")
                    setups.append({
                        "type": "impulse_aggressive_short", "direction": "short",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((w - _tp1) / (_sl - w), 1),
                        "score": strength, "variant": "trend_boost",
                        "tp_strategy": "tp1_only",
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "reasoning": f"TREND_DOWN({strength}); c2h={_c2h:+.1f}% imp={_imp} aggressive [trend_boost]",
                        "not_tradeable": True,
                    })
                else:
                    log_lines.append(f"    ✗ REJECTED [trend_boost]: RR<1.5")
                    setups.append({
                        "type": "impulse_aggressive_short", "direction": "short",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((w - _tp1) / (_sl - w), 1) if (_sl - w) > 0 else 0,
                        "score": strength, "variant": "trend_boost",
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "rejected_by_algo": True, "filter_reasons": ["RR<1.5"], "not_tradeable": True,
                    })
            else:
                reasons = []
                if _c2h > -2.0: reasons.append(f"c2h={_c2h:+.1f}%>-2%")
                if _imp < 2: reasons.append(f"imp={_imp}<2")
                if _bearish_s < 4: reasons.append(f"bearish={_bearish_s}/6<4")
                if _spike_s > 0: reasons.append("spike dolny wick")
                log_lines.append(f"    ✗ SKIP: {', '.join(reasons)}")

        # prawie-impulse short — TREND_DOWN z vol >= 2.0 ale bez formalnego breakoutu
        if regime_name == "TREND_DOWN" and regime.get("vol_ratio", 1.0) >= 2.0:
            _pv = regime.get("vol_ratio", 1.0)
            atr_m15 = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            log_lines.append(f"  → prawie_impulse_short: vol={_pv:.1f}x ATR_M15=${atr_m15:.2f}")
            w = round(current_price, 2)
            for _vname, _vatr in [("h1_atr", atr), ("m15_atr", atr_m15)]:
                _sl  = round(current_price + _vatr * 1.2, 2)
                _tp1 = round(current_price - _vatr * 2.0, 2)
                _tp2 = round(current_price - _vatr * 3.0, 2)
                _rr_ok = _tp1 < w and (w - _tp1) / (_sl - w) >= 1.5
                log_lines.append(f"    [{_vname}] W=${w:.2f} SL=${_sl:.2f} TP1=${_tp1:.2f} rr_ok={_rr_ok}")
                if _rr_ok:
                    log_lines.append(f"    ✓ ACCEPTED [prawie_impulse/{_vname}] (not_tradeable)")
                    setups.append({
                        "type": "impulse_aggressive_short", "direction": "short",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((w - _tp1) / (_sl - w), 1),
                        "score": strength, "variant": f"prawie_{_vname}",
                        "tp_strategy": "tp1_only",
                        "not_tradeable": True,
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "reasoning": f"TREND_DOWN({strength}); vol={_pv:.1f}x prawie-impulse [{_vname}]",
                    })
                else:
                    setups.append({
                        "type": "impulse_aggressive_short", "direction": "short",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((w - _tp1) / (_sl - w), 1) if (_sl - w) > 0 else 0,
                        "score": strength, "variant": f"prawie_{_vname}",
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "rejected_by_algo": True, "filter_reasons": ["RR<1.5"], "not_tradeable": True,
                    })

    # ── TREND_UP / IMPULSE_UP ─────────────────────────────────────────────
    elif direction == "up":
        swing_high, swing_low = find_swing_points(candles_h1, n=12)
        # Uwzględnij aktualną cenę jako przybliżenie niezamkniętej świecy H1
        swing_low = min(swing_low, current_price)
        swing_high = max(swing_high, current_price)
        log_lines.append(f"  Swing (12 H1+cena): high=${swing_high:.2f} low=${swing_low:.2f}")

        # trend_pullback_long — warianty parametrów (baseline + eksperymenty)
        if swing_high > swing_low:
            swing_range = swing_high - swing_low
            for vname, (fib_lo, fib_hi, fib_sl, atr_sl, str_min, v_not_tradeable) in _PULLBACK_VARIANTS.items():
                # str4 generuje tylko gdy strength==4 (baseline już pokrywa strength>=5)
                if vname == "str4" and strength != 4:
                    continue
                if strength < str_min:
                    log_lines.append(f"  → pullback_long [{vname}]: SKIP (strength={strength}<{str_min})")
                    continue
                entry_mid = (fib_lo + fib_hi) / 2
                w   = round(swing_high - entry_mid * swing_range, 2)
                sl  = round(swing_high - fib_sl * swing_range - atr * atr_sl, 2)
                tp1 = round(swing_high - swing_range * 0.02, 2)
                tp2 = round(swing_high + swing_range * 0.3, 2)
                rr_ok      = sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5
                below_price = w < current_price * 0.997
                dist_ok    = current_price - w <= max_entry_dist
                rr_val     = round((tp1 - w) / (w - sl), 1) if (w - sl) > 0 else 0
                log_lines.append(
                    f"  → pullback_long [{vname}]: fib{fib_lo:.0%}-{fib_hi:.0%} W=${w:.2f} "
                    f"SL=${sl:.2f} RR={rr_val} dist=${current_price-w:.2f} "
                    f"below={below_price} dist_ok={dist_ok} rr_ok={rr_ok}"
                )
                _ctx = _setup_ctx(w, sl, fib_lvl=entry_mid, swing_h=swing_high, swing_l=swing_low)
                if rr_ok and below_price and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED [{vname}]")
                    setups.append({
                        "type": "trend_pullback_long", "direction": "long",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": rr_val,
                        "score": strength,
                        "variant": vname,
                        "tp_strategy": "tp1_tp2",
                        "not_tradeable": v_not_tradeable,
                        "market_context": _ctx,
                        "reasoning": f"{regime_name}({strength}); swing ${swing_low:.0f}-${swing_high:.0f} [{vname}]",
                    })
                else:
                    reasons = []
                    if not rr_ok: reasons.append(f"RR<1.5({rr_val})")
                    if not below_price: reasons.append("W>=cena")
                    if not dist_ok: reasons.append(f"dist>3%({current_price-w:.2f})")
                    log_lines.append(f"    ✗ REJECTED [{vname}]: {', '.join(reasons)}")
                    setups.append({
                        "type": "trend_pullback_long", "direction": "long",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": rr_val,
                        "score": strength, "variant": vname,
                        "market_context": _ctx,
                        "rejected_by_algo": True,
                        "filter_reasons": reasons,
                        "not_tradeable": True,
                    })

            # trend_pullback_long — okno swing 24h zamiast 12h (obserwacyjne, zawsze not_tradeable).
            # Lustrzane odbicie eksperymentu swing24h z gałęzi short — patrz komentarz tam.
            if strength >= 5:
                swing_high_w, swing_low_w = find_swing_points(candles_h1, n=24)
                swing_low_w = min(swing_low_w, current_price)
                swing_high_w = max(swing_high_w, current_price)
                if swing_high_w > swing_low_w:
                    swing_range_w = swing_high_w - swing_low_w
                    entry_mid_w = 0.44  # (0.38+0.50)/2, jak baseline
                    w_w   = round(swing_high_w - entry_mid_w * swing_range_w, 2)
                    sl_w  = round(swing_high_w - 0.618 * swing_range_w - atr * 0.3, 2)
                    tp1_w = round(swing_high_w - swing_range_w * 0.02, 2)
                    tp2_w = round(swing_high_w + swing_range_w * 0.3, 2)
                    rr_ok_w       = sl_w < w_w and tp1_w > w_w and (tp1_w - w_w) / (w_w - sl_w) >= 1.5
                    below_price_w = w_w < current_price * 0.997
                    dist_ok_w     = current_price - w_w <= max_entry_dist
                    rr_val_w      = round((tp1_w - w_w) / (w_w - sl_w), 1) if (w_w - sl_w) > 0 else 0
                    log_lines.append(
                        f"  → pullback_long [swing24h]: W=${w_w:.2f} SL=${sl_w:.2f} RR={rr_val_w} "
                        f"swing24h=${swing_low_w:.2f}-${swing_high_w:.2f} dist=${current_price-w_w:.2f} "
                        f"below={below_price_w} dist_ok={dist_ok_w} rr_ok={rr_ok_w}"
                    )
                    _ctx_w = _setup_ctx(w_w, sl_w, fib_lvl=entry_mid_w, swing_h=swing_high_w, swing_l=swing_low_w)
                    if rr_ok_w and below_price_w and dist_ok_w:
                        log_lines.append(f"    ✓ ACCEPTED [swing24h] (not_tradeable)")
                        setups.append({
                            "type": "trend_pullback_long", "direction": "long",
                            "entries": [w_w], "sl": sl_w, "sl_after_tp1": w_w,
                            "tps": [tp1_w, tp2_w], "rr": rr_val_w,
                            "score": strength, "variant": "swing24h",
                            "tp_strategy": "tp1_tp2",
                            "not_tradeable": True,
                            "market_context": _ctx_w,
                            "reasoning": f"{regime_name}({strength}); swing24h ${swing_low_w:.0f}-${swing_high_w:.0f} [swing24h]",
                        })
                    else:
                        _rej_w = []
                        if not rr_ok_w: _rej_w.append(f"RR<1.5({rr_val_w})")
                        if not below_price_w: _rej_w.append("W>=cena")
                        if not dist_ok_w: _rej_w.append(f"dist>3%({current_price-w_w:.2f})")
                        log_lines.append(f"    ✗ REJECTED [swing24h]: {', '.join(_rej_w)}")
                        setups.append({
                            "type": "trend_pullback_long", "direction": "long",
                            "entries": [w_w], "sl": sl_w, "sl_after_tp1": w_w,
                            "tps": [tp1_w, tp2_w], "rr": rr_val_w,
                            "score": strength, "variant": "swing24h",
                            "market_context": _ctx_w,
                            "rejected_by_algo": True, "filter_reasons": _rej_w, "not_tradeable": True,
                        })

            # trend_pullback_long — pullback potwierdzony na M15 (obserwacyjne, zawsze not_tradeable).
            # Lustrzane odbicie eksperymentu m15_confirmed z gałęzi short — patrz komentarz tam.
            if strength >= 5:
                last3_m15_pb = candles_m15[-3:]
                dip_candles = sum(1 for c in last3_m15_pb if c["close"] < c["open"])
                m15_closes_pb = [c["close"] for c in candles_m15]
                ma30_pb = sum(m15_closes_pb[-30:]) / min(30, len(m15_closes_pb)) if len(m15_closes_pb) >= 10 else None
                ma60_pb = sum(m15_closes_pb[-60:]) / min(60, len(m15_closes_pb)) if len(m15_closes_pb) >= 30 else None
                ma_trend_intact = ma30_pb is not None and ma60_pb is not None and ma30_pb > ma60_pb
                pullback_confirmed = dip_candles >= 2 and ma_trend_intact
                log_lines.append(
                    f"  → pullback_long [m15_confirmed]: korekta {dip_candles}/3 świec M15, "
                    f"MA30={f'${ma30_pb:.2f}' if ma30_pb else 'N/A'} MA60={f'${ma60_pb:.2f}' if ma60_pb else 'N/A'} "
                    f"trend_intact={ma_trend_intact} → {'START KOREKTY' if pullback_confirmed else 'brak korekty'}"
                )
                if pullback_confirmed:
                    swing_high_m, swing_low_m = find_swing_points(candles_m15, n=12)  # ~3h lokalne okno
                    swing_low_m = min(swing_low_m, current_price)
                    swing_high_m = max(swing_high_m, current_price)
                    if swing_high_m > swing_low_m:
                        swing_range_m = swing_high_m - swing_low_m
                        entry_mid_m = 0.44  # (0.38+0.50)/2, jak baseline
                        w_m   = round(swing_high_m - entry_mid_m * swing_range_m, 2)
                        sl_m  = round(swing_high_m - 0.618 * swing_range_m - atr * 0.3, 2)
                        tp1_m = round(swing_high_m - swing_range_m * 0.02, 2)
                        tp2_m = round(swing_high_m + swing_range_m * 0.3, 2)
                        rr_ok_m       = sl_m < w_m and tp1_m > w_m and (tp1_m - w_m) / (w_m - sl_m) >= 1.5
                        below_price_m = w_m < current_price * 0.997
                        dist_ok_m     = current_price - w_m <= max_entry_dist
                        rr_val_m      = round((tp1_m - w_m) / (w_m - sl_m), 1) if (w_m - sl_m) > 0 else 0
                        log_lines.append(
                            f"    W=${w_m:.2f} SL=${sl_m:.2f} RR={rr_val_m} lokalny swing=${swing_low_m:.2f}-${swing_high_m:.2f} "
                            f"dist=${current_price-w_m:.2f} below={below_price_m} dist_ok={dist_ok_m} rr_ok={rr_ok_m}"
                        )
                        _ctx_m = _setup_ctx(w_m, sl_m, fib_lvl=entry_mid_m, swing_h=swing_high_m, swing_l=swing_low_m)
                        if rr_ok_m and below_price_m and dist_ok_m:
                            log_lines.append(f"    ✓ ACCEPTED [m15_confirmed] (not_tradeable)")
                            setups.append({
                                "type": "trend_pullback_long", "direction": "long",
                                "entries": [w_m], "sl": sl_m, "sl_after_tp1": w_m,
                                "tps": [tp1_m, tp2_m], "rr": rr_val_m,
                                "score": strength, "variant": "m15_confirmed",
                                "tp_strategy": "tp1_tp2",
                                "not_tradeable": True,
                                "market_context": _ctx_m,
                                "reasoning": f"{regime_name}({strength}); m15 dip {dip_candles}/3, "
                                             f"swing ${swing_low_m:.0f}-${swing_high_m:.0f} [m15_confirmed]",
                            })
                        else:
                            _rej_m = []
                            if not rr_ok_m: _rej_m.append(f"RR<1.5({rr_val_m})")
                            if not below_price_m: _rej_m.append("W>=cena")
                            if not dist_ok_m: _rej_m.append(f"dist>3%({current_price-w_m:.2f})")
                            log_lines.append(f"    ✗ REJECTED [m15_confirmed]: {', '.join(_rej_m)}")
                            setups.append({
                                "type": "trend_pullback_long", "direction": "long",
                                "entries": [w_m], "sl": sl_m, "sl_after_tp1": w_m,
                                "tps": [tp1_m, tp2_m], "rr": rr_val_m,
                                "score": strength, "variant": "m15_confirmed",
                                "market_context": _ctx_m,
                                "rejected_by_algo": True, "filter_reasons": _rej_m, "not_tradeable": True,
                            })

            # trend_pullback_long ATR-based — entry = cena - 0.5*ATR, SL = cena - 1.2*ATR
            atr_m15_pb = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            w_atr   = round(current_price - atr_m15_pb * 0.5, 2)
            sl_atr  = round(current_price - atr_m15_pb * 1.5, 2)
            tp1_atr = round(current_price + atr_m15_pb * 1.0, 2)
            tp2_atr = round(current_price + atr_m15_pb * 2.5, 2)
            rr_atr  = round((tp1_atr - w_atr) / (w_atr - sl_atr), 1) if (w_atr - sl_atr) > 0 else 0
            rr_ok_atr   = tp1_atr > w_atr and rr_atr >= 1.5
            below_atr   = w_atr < current_price * 0.997
            dist_ok_atr = current_price - w_atr <= max_entry_dist
            log_lines.append(
                f"  → pullback_long [atr_based]: W=${w_atr:.2f} SL=${sl_atr:.2f} "
                f"TP1=${tp1_atr:.2f} RR={rr_atr} ATR_M15=${atr_m15_pb:.2f}"
            )
            _ctx_atr = _setup_ctx(w_atr, sl_atr, swing_h=swing_high, swing_l=swing_low)
            if rr_ok_atr and below_atr and dist_ok_atr:
                log_lines.append(f"    ✓ ACCEPTED [atr_based] (not_tradeable)")
                setups.append({
                    "type": "trend_pullback_long", "direction": "long",
                    "entries": [w_atr], "sl": sl_atr, "sl_after_tp1": w_atr,
                    "tps": [tp1_atr, tp2_atr], "rr": rr_atr,
                    "score": strength, "variant": "atr_based",
                    "tp_strategy": "tp1_tp2",
                    "not_tradeable": True,
                    "market_context": _ctx_atr,
                    "reasoning": f"{regime_name}({strength}); ATR_M15=${atr_m15_pb:.2f} pullback [atr_based]",
                })
            else:
                _rej_atr = []
                if not rr_ok_atr: _rej_atr.append(f"RR<1.5({rr_atr})")
                if not below_atr: _rej_atr.append("W>=cena")
                if not dist_ok_atr: _rej_atr.append("dist")
                log_lines.append(f"    ✗ REJECTED [atr_based]: {', '.join(_rej_atr)}")
                setups.append({
                    "type": "trend_pullback_long", "direction": "long",
                    "entries": [w_atr], "sl": sl_atr, "sl_after_tp1": w_atr,
                    "tps": [tp1_atr, tp2_atr], "rr": rr_atr,
                    "score": strength, "variant": "atr_based",
                    "market_context": _ctx_atr,
                    "rejected_by_algo": True, "filter_reasons": _rej_atr, "not_tradeable": True,
                })

        # impulse_continuation_long
        if regime_name.startswith("IMPULSE_"):
            _cont_spike = regime.get("spike_score", 0)
            atr_m15 = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            last6 = candles_m15[-6:]
            reds = [c for c in last6 if c["close"] < c["open"]]
            log_lines.append(f"  → impulse_cont_long: reds={len(reds)}/6 (need 1-2) spike={_cont_spike}")
            if _cont_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_cont_spike}>=2 (odwrót)")
            elif len(reds) >= 1 and len(reds) <= 2:
                pullback_low = min(c["low"] for c in last6[-2:])
                w = round(pullback_low, 2)
                sl = round(pullback_low - atr * 0.8, 2)
                tp1 = round(current_price + atr_m15 * 1.5, 2)
                tp2 = round(current_price + atr_m15 * 2.5, 2)
                rr_ok = sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5
                dist_ok = abs(w - current_price) <= max_entry_dist
                log_lines.append(f"    W=${w:.2f} SL=${sl:.2f} TP1=${tp1:.2f} ATR_M15=${atr_m15:.2f} dist=${abs(w-current_price):.2f} rr_ok={rr_ok} dist_ok={dist_ok}")
                if rr_ok and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED")
                    setups.append({
                        "type": "impulse_continuation_long", "direction": "long",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((tp1 - w) / (w - sl), 1),
                        "score": strength,
                        "variant": "baseline",
                        "market_context": _setup_ctx(w, sl, swing_h=swing_high, swing_l=swing_low),
                        "reasoning": f"{regime_name}({strength}); pullback M15 cont",
                    })
                else:
                    _rej = []
                    if not rr_ok: _rej.append("RR<1.5")
                    if not dist_ok: _rej.append(f"dist>{max_entry_dist}")
                    log_lines.append(f"    ✗ REJECTED: {', '.join(_rej)}")
                    setups.append({
                        "type": "impulse_continuation_long", "direction": "long",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((tp1 - w) / (w - sl), 1) if (w - sl) > 0 else 0,
                        "score": strength, "variant": "baseline",
                        "market_context": _setup_ctx(w, sl, swing_h=swing_high, swing_l=swing_low),
                        "rejected_by_algo": True, "filter_reasons": _rej, "not_tradeable": True,
                    })

        # impulse_aggressive_long — dwa warianty ATR (h1_atr vs m15_atr) dla porównania
        if regime_name.startswith("IMPULSE_"):
            _agg_vol   = regime.get("vol_ratio", 1.0)
            _agg_spike = regime.get("spike_score", 0)
            atr_m15 = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            log_lines.append(f"  → impulse_aggressive: vol={_agg_vol:.1f}x spike={_agg_spike} ATR_H1=${atr:.2f} ATR_M15=${atr_m15:.2f}")
            if _agg_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_agg_spike}>=2")
            elif _agg_vol < 2.0:
                log_lines.append(f"    ✗ SKIP: vol={_agg_vol:.1f}x<2.0")
            else:
                w = round(current_price, 2)
                for _vname, _vatr, _tp2_mult in [("h1_atr", atr, 3.0), ("m15_atr", atr_m15, 3.0)]:
                    _is_m15 = _vname == "m15_atr"
                    _sl  = round(current_price - _vatr * 1.2, 2)
                    _tp1 = round(current_price + _vatr * 2.0, 2)
                    _tp2 = round(current_price + _vatr * _tp2_mult, 2)
                    _rr_ok = _tp1 > w and (_tp1 - w) / (w - _sl) >= 1.5
                    log_lines.append(f"    [{_vname}] W=${w:.2f} SL=${_sl:.2f} TP1=${_tp1:.2f} rr_ok={_rr_ok}")
                    if _rr_ok:
                        log_lines.append(f"    ✓ ACCEPTED [{_vname}] (not_tradeable — tryb testowy)")
                        setups.append({
                            "type": "impulse_aggressive_long", "direction": "long",
                            "entries": [w], "sl": _sl, "sl_after_tp1": w,
                            "tps": [_tp1, _tp2], "rr": round((_tp1 - w) / (w - _sl), 1),
                            "score": strength, "variant": _vname,
                            "tp_strategy": "tp1_only",
                            "not_tradeable": True,
                            "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                            "reasoning": f"{regime_name}({strength}); vol={_agg_vol:.1f}x aggressive [{_vname}]",
                        })
                    else:
                        log_lines.append(f"    ✗ REJECTED [{_vname}]: RR<1.5")
                        setups.append({
                            "type": "impulse_aggressive_long", "direction": "long",
                            "entries": [w], "sl": _sl, "sl_after_tp1": w,
                            "tps": [_tp1, _tp2], "rr": round((_tp1 - w) / (w - _sl), 1) if (w - _sl) > 0 else 0,
                            "score": strength, "variant": _vname,
                            "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                            "rejected_by_algo": True, "filter_reasons": ["RR<1.5"], "not_tradeable": True,
                        })

        # impulse_aggressive_long (trend_boost) — lokalny impuls w TREND_UP bez vol_ratio
        if regime_name == "TREND_UP":
            _c1h  = (current_price - candles_m15[-4]["close"]) / candles_m15[-4]["close"] * 100 if len(candles_m15) >= 4 else 0
            _c2h  = (current_price - candles_m15[-8]["close"]) / candles_m15[-8]["close"] * 100 if len(candles_m15) >= 8 else 0
            _imp  = impulse_strength(candles_m15)
            _last6_l = candles_m15[-6:]
            _bullish_l = sum(1 for c in _last6_l if c["close"] > c["open"])
            _last2_l = candles_m15[-2:]
            _spike_l = sum(1 for c in _last2_l if (c["high"] - max(c["open"], c["close"])) > abs(c["close"] - c["open"]) * 1.5)
            log_lines.append(f"  → aggressive_trend_boost_long: c1h={_c1h:+.1f}% c2h={_c2h:+.1f}% imp={_imp} bullish={_bullish_l}/6 spike={_spike_l}")
            if _c2h >= 2.0 and _imp >= 2 and _bullish_l >= 4 and _spike_l == 0:
                w    = round(current_price, 2)
                _sl  = round(current_price - atr * 1.2, 2)
                _tp1 = round(current_price + atr * 2.0, 2)
                _tp2 = round(current_price + atr * 3.0, 2)
                _rr_ok = _tp1 > w and (_tp1 - w) / (w - _sl) >= 1.5
                log_lines.append(f"    W=${w:.2f} SL=${_sl:.2f} TP1=${_tp1:.2f} rr_ok={_rr_ok}")
                if _rr_ok:
                    log_lines.append(f"    ✓ ACCEPTED [trend_boost] (not_tradeable — wariant testowy)")
                    setups.append({
                        "type": "impulse_aggressive_long", "direction": "long",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((_tp1 - w) / (w - _sl), 1),
                        "score": strength, "variant": "trend_boost",
                        "tp_strategy": "tp1_only",
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "reasoning": f"TREND_UP({strength}); c2h={_c2h:+.1f}% imp={_imp} aggressive [trend_boost]",
                        "not_tradeable": True,
                    })
                else:
                    log_lines.append(f"    ✗ REJECTED [trend_boost]: RR<1.5")
                    setups.append({
                        "type": "impulse_aggressive_long", "direction": "long",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((_tp1 - w) / (w - _sl), 1) if (w - _sl) > 0 else 0,
                        "score": strength, "variant": "trend_boost",
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "rejected_by_algo": True, "filter_reasons": ["RR<1.5"], "not_tradeable": True,
                    })
            else:
                reasons = []
                if _c2h < 2.0: reasons.append(f"c2h={_c2h:+.1f}%<2%")
                if _imp < 2: reasons.append(f"imp={_imp}<2")
                if _bullish_l < 4: reasons.append(f"bullish={_bullish_l}/6<4")
                if _spike_l > 0: reasons.append("spike górny wick")
                log_lines.append(f"    ✗ SKIP: {', '.join(reasons)}")

        # prawie-impulse long — TREND_UP z vol >= 2.0 ale bez formalnego breakoutu
        if regime_name == "TREND_UP" and regime.get("vol_ratio", 1.0) >= 2.0:
            _pv = regime.get("vol_ratio", 1.0)
            atr_m15 = calc_atr(candles_m15[-20:]) if len(candles_m15) >= 20 else calc_atr(candles_m15)
            log_lines.append(f"  → prawie_impulse_long: vol={_pv:.1f}x ATR_M15=${atr_m15:.2f}")
            w = round(current_price, 2)
            for _vname, _vatr in [("h1_atr", atr), ("m15_atr", atr_m15)]:
                _sl  = round(current_price - _vatr * 1.2, 2)
                _tp1 = round(current_price + _vatr * 2.0, 2)
                _tp2 = round(current_price + _vatr * 3.0, 2)
                _rr_ok = _tp1 > w and (_tp1 - w) / (w - _sl) >= 1.5
                log_lines.append(f"    [{_vname}] W=${w:.2f} SL=${_sl:.2f} TP1=${_tp1:.2f} rr_ok={_rr_ok}")
                if _rr_ok:
                    log_lines.append(f"    ✓ ACCEPTED [prawie_impulse/{_vname}] (not_tradeable)")
                    setups.append({
                        "type": "impulse_aggressive_long", "direction": "long",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((_tp1 - w) / (w - _sl), 1),
                        "score": strength, "variant": f"prawie_{_vname}",
                        "tp_strategy": "tp1_only",
                        "not_tradeable": True,
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "reasoning": f"TREND_UP({strength}); vol={_pv:.1f}x prawie-impulse [{_vname}]",
                    })
                else:
                    setups.append({
                        "type": "impulse_aggressive_long", "direction": "long",
                        "entries": [w], "sl": _sl, "sl_after_tp1": w,
                        "tps": [_tp1, _tp2], "rr": round((_tp1 - w) / (w - _sl), 1) if (w - _sl) > 0 else 0,
                        "score": strength, "variant": f"prawie_{_vname}",
                        "market_context": _setup_ctx(w, _sl, swing_h=swing_high, swing_l=swing_low),
                        "rejected_by_algo": True, "filter_reasons": ["RR<1.5"], "not_tradeable": True,
                    })

    # ── RANGE ─────────────────────────────────────────────────────────────
    elif regime_name == "RANGE":
        rng = detect_range(candles_h1)
        sup, res = rng["support"], rng["resistance"]
        rng_size = res - sup
        log_lines.append(f"  Range: S=${sup:.2f} R=${res:.2f} size=${rng_size:.2f} (min={atr*1.5:.2f})")
        if rng_size > atr * 1.5:
            # range_resistance_short
            w = res - rng_size * 0.1
            sl = res + atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = sup + rng_size * 0.1
            dist_ok = abs(w - current_price) <= max_entry_dist
            log_lines.append(f"  → range_short: W=${w:.2f} dist=${abs(w-current_price):.2f} dist_ok={dist_ok}")

            # ── Filtr 1: Bullish momentum – nie shortuj na oporze podczas silnego wzrostu
            last6_m15_s = candles_m15[-6:]
            bullish_count_s = sum(1 for c in last6_m15_s if c["close"] > c["open"])
            m15_rise = (last6_m15_s[-1]["close"] - last6_m15_s[0]["open"]) / last6_m15_s[0]["open"] * 100
            momentum_ok_s = not (bullish_count_s >= 5 or m15_rise > 1.5)
            log_lines.append(f"    momentum: {bullish_count_s}/6 bullish, rise={m15_rise:+.2f}% → {'OK' if momentum_ok_s else 'BLOCKED'}")

            # ── Filtr 2: Resistance touches – opór musi mieć min 2 wcześniejsze testy
            r_touches = rng["r_touches"]
            touches_ok_s = r_touches >= 2
            log_lines.append(f"    r_touches: {r_touches} → {'OK' if touches_ok_s else 'BLOCKED (min 2)'}")

            # ── Filtr 3: MA alignment – nie shortuj gdy cena > MA30 > MA60 (bullish alignment)
            m15_closes_s = [c["close"] for c in candles_m15]
            ma30_s2 = sum(m15_closes_s[-30:]) / min(30, len(m15_closes_s)) if len(m15_closes_s) >= 10 else None
            ma60_s2 = sum(m15_closes_s[-60:]) / min(60, len(m15_closes_s)) if len(m15_closes_s) >= 30 else None
            if ma30_s2 is not None and ma60_s2 is not None:
                ma_bullish = current_price > ma30_s2 > ma60_s2
            else:
                ma_bullish = False
            ma_ok_s = not ma_bullish
            ma30_str = f"${ma30_s2:.2f}" if ma30_s2 else "N/A"
            ma60_str = f"${ma60_s2:.2f}" if ma60_s2 else "N/A"
            log_lines.append(f"    MA filter: price=${current_price:.2f} MA30={ma30_str} MA60={ma60_str} → {'OK' if ma_ok_s else 'BLOCKED (bullish MA)'}")

            _rr_s = round((w - tp1) / (sl - w), 1) if (sl - w) > 0 else 0
            if (sl - w) > 0 and (w - tp1) / (sl - w) >= 1.5 and dist_ok and momentum_ok_s and touches_ok_s and ma_ok_s:
                log_lines.append(f"    ✓ ACCEPTED")
                setups.append({
                    "type": "range_resistance_short", "direction": "short",
                    "variant": "baseline",
                    "entries": [round(w, 2)], "sl": round(sl, 2),
                    "sl_after_tp1": round(w, 2),
                    "tps": [round(tp1, 2), round(tp2, 2)],
                    "rr": _rr_s,
                    "score": 0,
                    "tp_strategy": "tp1_only",
                    "market_context": _setup_ctx(round(w, 2), round(sl, 2)),
                    "reasoning": f"RANGE; S=${sup:.2f} R=${res:.2f} touches={r_touches}",
                })
            else:
                _rej = []
                if not ((sl - w) > 0 and (w - tp1) / (sl - w) >= 1.5 if (sl - w) > 0 else False): _rej.append(f"RR<1.5({_rr_s})")
                if not dist_ok: _rej.append("dist")
                if not momentum_ok_s: _rej.append("momentum")
                if not touches_ok_s: _rej.append(f"r_touches<2({r_touches})")
                if not ma_ok_s: _rej.append("bullish_MA")
                log_lines.append(f"    ✗ REJECTED: {', '.join(_rej)}")
                setups.append({
                    "type": "range_resistance_short", "direction": "short",
                    "variant": "baseline",
                    "entries": [round(w, 2)], "sl": round(sl, 2),
                    "sl_after_tp1": round(w, 2),
                    "tps": [round(tp1, 2), round(tp2, 2)],
                    "rr": _rr_s, "score": 0,
                    "market_context": _setup_ctx(round(w, 2), round(sl, 2)),
                    "rejected_by_algo": True, "filter_reasons": _rej, "not_tradeable": True,
                })
            # range_support_long
            w = sup + rng_size * 0.1
            sl = sup - atr * 1.0
            tp1 = sup + rng_size * 0.5
            tp2 = res - rng_size * 0.1
            dist_ok = abs(w - current_price) <= max_entry_dist
            log_lines.append(f"  → range_long: W=${w:.2f} dist=${abs(w-current_price):.2f} dist_ok={dist_ok}")

            # ── Filtr 1: Bearish momentum – nie kupuj na wsparciu podczas silnego spadku
            last6_m15 = candles_m15[-6:]
            bearish_count = sum(1 for c in last6_m15 if c["close"] < c["open"])
            m15_drop = (last6_m15[-1]["close"] - last6_m15[0]["open"]) / last6_m15[0]["open"] * 100
            momentum_ok = not (bearish_count >= 5 or m15_drop < -1.5)
            log_lines.append(f"    momentum: {bearish_count}/6 bearish, drop={m15_drop:+.2f}% → {'OK' if momentum_ok else 'BLOCKED'}")

            # ── Filtr 2: Support touches – wsparcie musi mieć min 2 wcześniejsze odbicia
            s_touches = rng["s_touches"]
            touches_ok = s_touches >= 2
            log_lines.append(f"    s_touches: {s_touches} → {'OK' if touches_ok else 'BLOCKED (min 2)'}")

            # ── Filtr 3: MA alignment – nie kupuj gdy cena < MA30 < MA60 (bearish alignment)
            m15_closes = [c["close"] for c in candles_m15]
            ma30 = sum(m15_closes[-30:]) / min(30, len(m15_closes)) if len(m15_closes) >= 10 else None
            ma60 = sum(m15_closes[-60:]) / min(60, len(m15_closes)) if len(m15_closes) >= 30 else None
            if ma30 is not None and ma60 is not None:
                ma_bearish = current_price < ma30 < ma60
            else:
                ma_bearish = False
            ma_ok = not ma_bearish
            ma30_s = f"${ma30:.2f}" if ma30 else "N/A"
            ma60_s = f"${ma60:.2f}" if ma60 else "N/A"
            log_lines.append(f"    MA filter: price=${current_price:.2f} MA30={ma30_s} MA60={ma60_s} → {'OK' if ma_ok else 'BLOCKED (bearish MA)'}")

            _rr_l = round((tp1 - w) / (w - sl), 1) if (w - sl) > 0 else 0
            if (w - sl) > 0 and (tp1 - w) / (w - sl) >= 1.5 and dist_ok and momentum_ok and touches_ok and ma_ok:
                log_lines.append(f"    ✓ ACCEPTED")
                setups.append({
                    "type": "range_support_long", "direction": "long",
                    "variant": "baseline",
                    "entries": [round(w, 2)], "sl": round(sl, 2),
                    "sl_after_tp1": round(w, 2),
                    "tps": [round(tp1, 2), round(tp2, 2)],
                    "rr": _rr_l,
                    "score": 0,
                    "tp_strategy": "tp1_only",
                    "market_context": _setup_ctx(round(w, 2), round(sl, 2)),
                    "reasoning": f"RANGE; S=${sup:.2f} R=${res:.2f} touches={s_touches}",
                })
            else:
                _rej = []
                if not ((w - sl) > 0 and (tp1 - w) / (w - sl) >= 1.5 if (w - sl) > 0 else False): _rej.append(f"RR<1.5({_rr_l})")
                if not dist_ok: _rej.append("dist")
                if not momentum_ok: _rej.append("momentum")
                if not touches_ok: _rej.append(f"s_touches<2({s_touches})")
                if not ma_ok: _rej.append("bearish_MA")
                log_lines.append(f"    ✗ REJECTED: {', '.join(_rej)}")
                setups.append({
                    "type": "range_support_long", "direction": "long",
                    "variant": "baseline",
                    "entries": [round(w, 2)], "sl": round(sl, 2),
                    "sl_after_tp1": round(w, 2),
                    "tps": [round(tp1, 2), round(tp2, 2)],
                    "rr": _rr_l, "score": 0,
                    "market_context": _setup_ctx(round(w, 2), round(sl, 2)),
                    "rejected_by_algo": True, "filter_reasons": _rej, "not_tradeable": True,
                })

        # ── Shadow: regime_alt rescue (obserwacyjne, zawsze not_tradeable) ──────
        # Gdy regime_alt wskazuje trend, którego RANGE nie złapał (patrz Fix 3 /
        # regime_alt w detect_market_regime), generuje ten sam baseline pullback,
        # który powstałby, gdyby regime faktycznie był TREND_{alt_dir} — żeby
        # zebrać dane porównawcze bez wpływu na realny handel (baseline pozostaje
        # sterowany wyłącznie prawdziwym regime['direction']).
        regime_alt = regime.get("regime_alt")
        if regime_alt in ("TREND_UP", "TREND_DOWN"):
            alt_dir = "down" if regime_alt == "TREND_DOWN" else "up"
            alt_strength = 6  # proxy: regime_alt wymaga jednomyślnych 3/3 głosów mtf
            swing_high, swing_low = find_swing_points(candles_h1, n=12)
            swing_low  = min(swing_low,  current_price)
            swing_high = max(swing_high, current_price)
            if swing_high > swing_low:
                swing_range = swing_high - swing_low
                fib_lo, fib_hi, fib_sl, atr_sl, _str_min, _ = _PULLBACK_VARIANTS["baseline"]
                entry_mid = (fib_lo + fib_hi) / 2
                if alt_dir == "down":
                    w   = round(swing_low + entry_mid * swing_range, 2)
                    sl  = round(swing_low + fib_sl * swing_range + atr * atr_sl, 2)
                    tp1 = round(swing_low + swing_range * 0.02, 2)
                    tp2 = round(swing_low - swing_range * 0.3, 2)
                    rr_ok = sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5 if (sl - w) > 0 else False
                    dist_ok = abs(w - current_price) <= max_entry_dist
                else:
                    w   = round(swing_high - entry_mid * swing_range, 2)
                    sl  = round(swing_high - fib_sl * swing_range - atr * atr_sl, 2)
                    tp1 = round(swing_high - swing_range * 0.02, 2)
                    tp2 = round(swing_high + swing_range * 0.3, 2)
                    rr_ok = sl < w and tp1 > w and (tp1 - w) / (w - sl) >= 1.5 if (w - sl) > 0 else False
                    dist_ok = abs(w - current_price) <= max_entry_dist
                rr_val = (round((w - tp1) / (sl - w), 1) if alt_dir == "down" and (sl - w) > 0
                          else round((tp1 - w) / (w - sl), 1) if alt_dir == "up" and (w - sl) > 0
                          else 0)
                log_lines.append(
                    f"  → pullback_{alt_dir} [regime_alt]: RANGE ale regime_alt={regime_alt} "
                    f"W=${w:.2f} SL=${sl:.2f} RR={rr_val} rr_ok={rr_ok} dist_ok={dist_ok}"
                )
                setups.append({
                    "type": f"trend_pullback_{'short' if alt_dir == 'down' else 'long'}",
                    "direction": "short" if alt_dir == "down" else "long",
                    "entries": [w], "sl": sl, "sl_after_tp1": w,
                    "tps": [tp1, tp2], "rr": rr_val,
                    "score": alt_strength,
                    "variant": "regime_alt",
                    "not_tradeable": True,
                    "market_context": _setup_ctx(w, sl, fib_lvl=entry_mid, swing_h=swing_high, swing_l=swing_low),
                    "reasoning": f"RANGE ale regime_alt={regime_alt}; swing ${swing_low:.0f}-${swing_high:.0f} [regime_alt] rr_ok={rr_ok} dist_ok={dist_ok}",
                })
    else:
        log_lines.append(f"  Brak setupów dla direction={direction}")

    # Dołącz sygnały wyczerpania do reasoning każdego setupu (widoczne w DB i Sheets)
    if exhaustion_signals:
        exh_tag = " | EXH:" + ",".join(exhaustion_signals)
        for s in setups:
            s["reasoning"] = s.get("reasoning", "") + exh_tag

    log_lines.append(f"  WYNIK: {len(setups)} setupów")
    return setups, "\n".join(log_lines)


# ── Google Sheets ─────────────────────────────────────────────────────────────
ALERTY_HEADER = [
    "ID", "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
    "Kurs", "W1", "W2", "Warunek", "SL", "SL@TP1", "TP1", "TP2", "RR", "Reasoning",
]
WYNIKI_HEADER = [
    "ID", "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
    "Kurs", "W1", "W2", "Warunek", "SL", "TP1", "TP2", "RR",
    "Entries_hit", "Śr.Entry", "Śr.Exit", "Wejście o", "Wyjście o", "Wynik",
    "PnL $", "PnL %", "Alt.PnL$(TP1)", "Δ(real-alt)",
    "Reasoning",
]
ANULOWANE_GROK_HEADER = [
    "ID", "Snapshot", "Kierunek", "W1", "SL", "TP1", "TP2", "RR", "Score",
    "Powód_Anulowania", "Cena_Anulowania", "Wynik_Cień",
    "Entries_hit", "Śr.Entry", "Śr.Exit", "Wejście o", "Wyjście o", "PnL $",
]


def _get_sheets(reset: bool = False):
    """Zwraca (sheet_alerty, sheet_wyniki) — tworzy/czyści arkusze jeśli trzeba."""
    creds  = Credentials.from_service_account_info(
        json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    wb     = client.open_by_key(SHEET_ID)

    for name, header, rows in [
        ("Alerty", ALERTY_HEADER, 1000),
        ("Wyniki_Railway", WYNIKI_HEADER, 1000),
        ("Anulowane_Grok", ANULOWANE_GROK_HEADER, 500),
    ]:
        try:
            sh = wb.worksheet(name)
            if reset:
                sh.clear()
                sh.append_row(header)
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet(name, rows=rows, cols=len(header) + 2)
            sh.append_row(header)
        if name == "Alerty":
            sh1 = sh
        elif name == "Wyniki_Railway":
            sh2 = sh

    return sh1, sh2


def _rejection_reason(setup: dict) -> str:
    """Zwraca powody odrzucenia setupu oddzielone ' | ', lub pusty string gdy OK."""
    reasons = []
    score = setup.get("total", setup.get("score", 0))
    if score < MIN_SCORE:
        reasons.append(f"Score<{MIN_SCORE} ({score})")
    rr = setup.get("rr", 0)
    if isinstance(rr, (int, float)) and rr > 0 and rr < 1.6:
        reasons.append(f"RR<1.6 ({rr:.2f})")
    geo = validate_setup(setup, "")
    if geo:
        reasons.append(geo)
    return " | ".join(reasons)


def log_to_alerty(model: str, rejection: str, setup: dict):
    """Zapisuje wykryty setup do Sheet 1 (natychmiast)."""
    try:
        sh1, _ = _get_sheets()
        entries = setup.get("entries", [])
        tps     = setup.get("tps", [])
        now     = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        raw_score = setup.get("total", setup.get("score", 0))
        score_val = f"{raw_score}%" if model in ("Grok", "Grok2") else raw_score
        sh1.append_row([
            setup.get("setup_id", "") or "",
            now,
            model,
            rejection or "",
            setup.get("type", setup.get("setup_type", "")) or "",
            setup.get("direction", ""),
            score_val,
            setup.get("kurs", setup.get("price_at_alert", "")) or "",
            entries[0] if len(entries) > 0 else "",
            entries[1] if len(entries) > 1 else "",
            setup.get("warunek", "") or "",
            setup.get("sl", "") or "",
            setup.get("sl_after_tp1", "") or "",
            tps[0] if tps else setup.get("tp1", "") or "",
            tps[1] if len(tps) > 1 else setup.get("tp2", "") or "",
            setup.get("rr", "") or "",
            (setup.get("reasoning", "") or "")[:500],
        ])
        print(f"[sheets] Alerty: {model} {setup.get('direction')} [{setup.get('total', setup.get('score'))}]")
    except Exception as e:
        print(f"[sheets] Blad Alerty: {e}")


def log_to_wyniki(s: dict, result: str, entry_ts, exit_ts,
                  eff_entry, eff_exit, move: float, *, _sh2=None) -> bool:
    """Zapisuje wynik rozwiązanego setupu do Sheet 2. Zwraca True jeśli sukces.
    Opcjonalny _sh2 pozwala przekazać już otwarty worksheet (batch export — 1 połączenie)."""
    try:
        sh2 = _sh2 if _sh2 is not None else _get_sheets()[1]
        _at      = s["alert_time"]
        if isinstance(_at, str):
            _at = datetime.fromisoformat(_at)
        alert_dt = _at.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        entry_dt = datetime.fromtimestamp(entry_ts, tz=timezone.utc).astimezone(TZ).strftime("%H:%M") if entry_ts else ""
        exit_dt  = datetime.fromtimestamp(exit_ts, tz=timezone.utc).astimezone(TZ).strftime("%H:%M")  if exit_ts  else ""
        entries  = s.get("entries", [])
        tps      = s.get("tps", [])
        n_w      = s.get("entries_hit", 1)
        model     = s.get("model", "")
        raw_score = s.get("score", s.get("total", 0))
        score_val = f"{raw_score}%" if model in ("Grok", "Grok2") else raw_score

        # PnL % — użyj trade_usdt z momentu otwarcia pozycji
        pnl_pct = s.get("pnl_pct")
        if pnl_pct is None and move:
            _tu = float(s.get("trade_usdt") or os.getenv("BITGET_TRADE_USDT", "100"))
            pnl_pct = round(move / _tu * 100, 2) if _tu else None
        pnl_pct_val = f"{pnl_pct:+.1f}%" if pnl_pct is not None else ""

        # Alternatywny scenariusz: całość na TP1 (tylko dla TP2 i TP1+BE)
        alt_pnl_val = ""
        delta_val   = ""
        if result in ("TP2", "TP1+BE", "TP1+SL") and eff_entry and tps:
            tp1_p = float(tps[0])
            sign  = 1 if s.get("direction") == "long" else -1
            fq    = (s.get("exchange_qty_full") or "0").replace(",", ".")
            try:
                fq_f = float(fq) if fq else 0.0
            except ValueError:
                fq_f = 0.0
            if fq_f <= 0:
                fq_f = (TRADE_USDT * LEVERAGE) / eff_entry
            if fq_f > 0:
                alt_pnl_val = round(sign * fq_f * (tp1_p - eff_entry), 2)
                delta_val   = round(move - alt_pnl_val, 2)

        sh2.append_row([
            s.get("setup_id", "") or "",
            alert_dt,
            model,
            s.get("rejection", "") or "",
            s.get("type", s.get("setup_type", "")) or "",
            s.get("direction", ""),
            score_val,
            s.get("kurs", s.get("price_at_alert", "")) or "",
            entries[0] if entries else "",
            entries[1] if len(entries) > 1 else "",
            s.get("warunek", "") or "",
            s.get("sl", "") or "",
            tps[0] if tps else "",
            tps[1] if len(tps) > 1 else "",
            s.get("rr", "") or "",
            "+".join(f"W{i+1}" for i in range(n_w)) if n_w > 0 else "",
            round(eff_entry, 2) if eff_entry is not None else "",
            round(eff_exit,  2) if eff_exit  is not None else "",
            entry_dt, exit_dt, result,
            round(move, 2), pnl_pct_val, alt_pnl_val, delta_val,
            s.get("reasoning", "") or "",
        ])
        print(f"[sheets] Wyniki: {s.get('model')} {s.get('direction')} -> {result} ${move:.2f} [{entry_dt}-{exit_dt}]")
        return True
    except Exception as e:
        print(f"[sheets] Blad Wyniki: {e}")
        return False


def log_to_anulowane_grok(s: dict, result: str, entry_ts, exit_ts,
                          eff_entry, eff_exit, move: float) -> bool:
    """Zapisuje wynik wirtualnie śledzonego (anulowanego przez Groka) setupu."""
    try:
        creds  = Credentials.from_service_account_info(
            json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        wb     = client.open_by_key(SHEET_ID)
        try:
            sh = wb.worksheet("Anulowane_Grok")
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet("Anulowane_Grok", rows=500, cols=len(ANULOWANE_GROK_HEADER) + 2)
            sh.append_row(ANULOWANE_GROK_HEADER)
        _at      = s["alert_time"]
        if isinstance(_at, str):
            _at = datetime.fromisoformat(_at)
        alert_dt = _at.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        entry_dt = datetime.fromtimestamp(entry_ts, tz=timezone.utc).astimezone(TZ).strftime("%H:%M") if entry_ts else ""
        exit_dt  = datetime.fromtimestamp(exit_ts, tz=timezone.utc).astimezone(TZ).strftime("%H:%M")  if exit_ts  else ""
        entries  = s.get("entries", [])
        tps      = s.get("tps", [])
        n_w      = s.get("entries_hit", 1)
        sh.append_row([
            s.get("setup_id", "") or "",
            alert_dt,
            s.get("direction", ""),
            entries[0] if entries else "",
            s.get("sl", ""),
            tps[0] if tps else "",
            tps[1] if len(tps) > 1 else "",
            s.get("rr", ""),
            s.get("score", ""),
            s.get("cancel_reason", ""),
            s.get("cancel_price", ""),
            result,
            "+".join(f"W{i+1}" for i in range(n_w)) if n_w > 0 else "",
            round(eff_entry, 2) if eff_entry is not None else "",
            round(eff_exit,  2) if eff_exit  is not None else "",
            entry_dt, exit_dt,
            round(move, 2),
        ])
        print(f"[sheets] Anulowane_Grok: #{s.get('setup_id')} -> {result} ${move:.2f}")
        return True
    except Exception as e:
        print(f"[sheets] Blad Anulowane_Grok: {e}")
        return False


GROK_SHADOW_HEADER = [
    "ID", "Snapshot", "Kierunek", "Typ", "W1", "SL", "TP1", "TP2", "RR", "Score",
    "Sprzeczność_Reżimu",
    "Wynik_Wirtualny", "Entries_hit", "Śr.Entry", "Śr.Exit", "Wejście o", "Wyjście o", "PnL $",
]


def log_to_grok_shadow(s: dict, result: str, entry_ts, exit_ts,
                       eff_entry, eff_exit, move: float) -> bool:
    """Zapisuje wirtualny wynik shadow Grok setupu do arkusza Grok_Shadow."""
    try:
        creds  = Credentials.from_service_account_info(
            json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        wb     = client.open_by_key(SHEET_ID)
        try:
            sh = wb.worksheet("Grok_Shadow")
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet("Grok_Shadow", rows=500, cols=len(GROK_SHADOW_HEADER) + 2)
            sh.append_row(GROK_SHADOW_HEADER)
        _at      = s["alert_time"]
        if isinstance(_at, str):
            _at = datetime.fromisoformat(_at)
        alert_dt = _at.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
        entry_dt = datetime.fromtimestamp(entry_ts, tz=timezone.utc).astimezone(TZ).strftime("%H:%M") if entry_ts else ""
        exit_dt  = datetime.fromtimestamp(exit_ts, tz=timezone.utc).astimezone(TZ).strftime("%H:%M")  if exit_ts  else ""
        entries  = s.get("entries", [])
        tps      = s.get("tps", [])
        n_w      = s.get("entries_hit", 1)
        # Wyciągnij sprzeczność z reasoning (prefiks "⚠️ SPRZECZNY Z REŻIMEM: X | ")
        reasoning    = s.get("reasoning", "")
        conflict_val = ""
        if reasoning.startswith("⚠️ SPRZECZNY Z REŻIMEM: "):
            conflict_val = reasoning.split("|")[0].replace("⚠️ SPRZECZNY Z REŻIMEM: ", "").strip()
        sh.append_row([
            s.get("setup_id", "") or "",
            alert_dt,
            s.get("direction", ""),
            s.get("type", ""),
            entries[0] if entries else "",
            s.get("sl", ""),
            tps[0] if tps else "",
            tps[1] if len(tps) > 1 else "",
            s.get("rr", ""),
            s.get("score", ""),
            conflict_val,
            result,
            "+".join(f"W{i+1}" for i in range(n_w)) if n_w > 0 else "",
            round(eff_entry, 2) if eff_entry is not None else "",
            round(eff_exit,  2) if eff_exit  is not None else "",
            entry_dt, exit_dt,
            round(move, 2),
        ])
        print(f"[sheets] Grok_Shadow: #{s.get('setup_id')} -> {result} ${move:.2f}")
        return True
    except Exception as e:
        print(f"[sheets] Błąd Grok_Shadow: {e}")
        return False


KALKULATOR_HEADER = [
    "ID", "Data alertu", "Model", "Typ", "Kierunek", "Score",
    "W1", "SL", "TP1", "TP2", "RR",
    "Entry faktyczne", "Wynik faktyczny", "PnL faktyczny($)",
    "TP1only PnL($)", "TP1only PnL(%)",
    "TP1+TP2 PnL($)", "TP1+TP2 PnL(%)",
]


def calc_pnl_scenarios(setup: dict, trade_usdt: float = 100.0, leverage: int = 20) -> dict | None:
    """Oblicza PnL dla dwóch wariantów przy założeniu $trade_usdt * leverage dźwignia.

    Wariant TP1only: całość pozycji zamykana na TP1.
    Wariant TP1+TP2: połowa na TP1, połowa na TP2 (lub SL po TP1 gdy TP2 nie trafiony).

    Zwraca słownik {tp1only_pnl, tp1only_pct, tp1tp2_pnl, tp1tp2_pct} lub None gdy brak danych.
    """
    result    = setup.get("result", "")
    direction = setup.get("direction", "long")
    entries   = setup.get("entries") or []
    tps       = setup.get("tps") or []
    sl        = setup.get("sl")
    sl_after_tp1 = setup.get("sl_after_tp1")

    if result in ("nie weszlo", "nieokreslone", "") or not result:
        return None

    tp1 = float(tps[0]) if tps else None
    tp2 = float(tps[1]) if len(tps) > 1 else None

    # Cena wejścia: preferuj faktyczną avg_entry, fallback na W1
    avg_entry = setup.get("avg_entry")
    if avg_entry is not None:
        entry = float(avg_entry)
    elif entries:
        entry = float(entries[0])
    else:
        return None

    if entry <= 0 or tp1 is None or sl is None:
        return None

    sign     = 1 if direction == "long" else -1
    sl_f     = float(sl)
    sl_tp1_f = float(sl_after_tp1) if sl_after_tp1 is not None else entry

    # Qty zaokrąglone do 0.1 SOL (standard Bitget)
    full_qty = max(math.floor((trade_usdt * leverage / entry) / 0.1) * 0.1, 0.1)
    half_qty = max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)

    tp1_reached = result in ("TP1", "TP2", "TP1+BE", "TP1+SL")

    # ── TP1only ──────────────────────────────────────────────────────────────
    if tp1_reached:
        tp1only_pnl = round(sign * full_qty * (tp1 - entry), 2)
    else:  # SL
        tp1only_pnl = round(sign * full_qty * (sl_f - entry), 2)
    tp1only_pct = round(tp1only_pnl / trade_usdt * 100, 1)

    # ── TP1+TP2 ──────────────────────────────────────────────────────────────
    if result == "TP2" and tp2 is not None:
        # Połowa na TP1, połowa na TP2
        tp1tp2_pnl = round(
            sign * half_qty * (tp1 - entry) + sign * half_qty * (tp2 - entry), 2
        )
    elif result in ("TP1+BE", "TP1+SL"):
        # Połowa na TP1, połowa na sl_after_tp1
        tp1tp2_pnl = round(
            sign * half_qty * (tp1 - entry) + sign * half_qty * (sl_tp1_f - entry), 2
        )
    elif result == "TP1":
        # Brak TP2 w setupie → całość na TP1 (identycznie jak TP1only)
        tp1tp2_pnl = round(sign * full_qty * (tp1 - entry), 2)
    else:  # SL przed TP1
        tp1tp2_pnl = round(sign * full_qty * (sl_f - entry), 2)
    tp1tp2_pct = round(tp1tp2_pnl / trade_usdt * 100, 1)

    return {
        "tp1only_pnl": tp1only_pnl,
        "tp1only_pct": tp1only_pct,
        "tp1tp2_pnl":  tp1tp2_pnl,
        "tp1tp2_pct":  tp1tp2_pct,
    }


def export_profit_calculator_to_sheets(trade_usdt: float = 100.0, leverage: int = 20) -> bool:
    """Eksportuje kalkulator zysku/straty do nowego arkusza w Google Sheets.

    Nazwa arkusza tworzona automatycznie z zakresu dat alertów:
    'Kalkulator_YYYY-MM-DD_YYYY-MM-DD'

    Dla każdego zamkniętego setupu oblicza oba warianty:
    - TP1only: całość pozycji zamykana na TP1
    - TP1+TP2: połowa na TP1, połowa na TP2
    """
    try:
        setups = db.get_all_resolved_for_calc()
        if not setups:
            print("[kalkulator] Brak zamkniętych setupów do eksportu.")
            return False

        # Wyznacz zakres dat z alert_time
        dates = []
        for s in setups:
            at = s.get("alert_time")
            if at:
                if isinstance(at, str):
                    at = datetime.fromisoformat(at)
                dates.append(at)
        if dates:
            date_from = min(dates).astimezone(TZ).strftime("%Y-%m-%d")
            date_to   = max(dates).astimezone(TZ).strftime("%Y-%m-%d")
        else:
            today = datetime.now(TZ).strftime("%Y-%m-%d")
            date_from = date_to = today

        sheet_name = f"Kalkulator_{date_from}_{date_to}"

        # Połącz z Google Sheets
        creds  = Credentials.from_service_account_info(
            json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}")),
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        wb     = client.open_by_key(SHEET_ID)

        # Utwórz lub nadpisz arkusz kalkulatora
        try:
            sh = wb.worksheet(sheet_name)
            sh.clear()
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet(sheet_name, rows=len(setups) + 10, cols=len(KALKULATOR_HEADER) + 2)

        sh.append_row(KALKULATOR_HEADER)

        # Akumulatory sum
        sum_faktyczny = 0.0
        sum_tp1only   = 0.0
        sum_tp1tp2    = 0.0
        n_calc        = 0

        rows = []
        for s in setups:
            at = s.get("alert_time")
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
            alert_dt  = at.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if at else ""
            entries   = s.get("entries") or []
            tps       = s.get("tps") or []
            avg_entry = s.get("avg_entry")
            pnl_real  = s.get("pnl_usd")

            scenarios = calc_pnl_scenarios(s, trade_usdt=trade_usdt, leverage=leverage)

            if scenarios:
                n_calc       += 1
                sum_tp1only  += scenarios["tp1only_pnl"]
                sum_tp1tp2   += scenarios["tp1tp2_pnl"]
                tp1only_pnl_v = f"{scenarios['tp1only_pnl']:+.2f}"
                tp1only_pct_v = f"{scenarios['tp1only_pct']:+.1f}%"
                tp1tp2_pnl_v  = f"{scenarios['tp1tp2_pnl']:+.2f}"
                tp1tp2_pct_v  = f"{scenarios['tp1tp2_pct']:+.1f}%"
            else:
                tp1only_pnl_v = tp1only_pct_v = tp1tp2_pnl_v = tp1tp2_pct_v = ""

            if pnl_real is not None:
                sum_faktyczny += float(pnl_real)
                pnl_real_v = f"{float(pnl_real):+.2f}"
            else:
                pnl_real_v = ""

            rows.append([
                s.get("setup_id", ""),
                alert_dt,
                s.get("model", ""),
                s.get("type", ""),
                s.get("direction", ""),
                s.get("score", ""),
                entries[0] if entries else "",
                s.get("sl", ""),
                tps[0] if tps else "",
                tps[1] if len(tps) > 1 else "",
                s.get("rr", ""),
                round(float(avg_entry), 2) if avg_entry else "",
                s.get("result", ""),
                pnl_real_v,
                tp1only_pnl_v, tp1only_pct_v,
                tp1tp2_pnl_v,  tp1tp2_pct_v,
            ])

        # Zapisz wszystkie wiersze naraz (batch)
        if rows:
            sh.append_rows(rows, value_input_option="USER_ENTERED")

        # Wiersz podsumowania
        summary = [
            "", "", "", "", "SUMA", f"n={n_calc}",
            "", "", "", "", "",
            "", "",
            f"{sum_faktyczny:+.2f}",
            f"{sum_tp1only:+.2f}", "",
            f"{sum_tp1tp2:+.2f}", "",
        ]
        sh.append_row(summary, value_input_option="USER_ENTERED")

        print(
            f"[kalkulator] Eksport OK → '{sheet_name}' | {n_calc} setupów | "
            f"TP1only: {sum_tp1only:+.2f}$ | TP1+TP2: {sum_tp1tp2:+.2f}$"
        )
        return True

    except Exception as e:
        print(f"[kalkulator] Błąd eksportu: {e}")
        return False


# ── Walidacja setupu ─────────────────────────────────────────────────────────
MIN_TP1_DISTANCE = 0.50   # minimalna odleglosc W1-TP1 w USD

def validate_setup(setup: dict, model: str) -> str:
    """Zwraca pusty string jeśli setup jest OK, albo wszystkie powody odrzucenia oddzielone ' | '."""
    entries   = setup.get("entries", [])
    sl        = setup.get("sl")
    direction = setup.get("direction", "-")
    reasons   = []
    if not entries:
        return "brak_W1"
    if sl is None:
        return "brak_SL"
    w1 = entries[0]
    if direction == "long" and sl >= w1:
        reasons.append(f"SL≥W1 ({sl}≥{w1})")
    elif direction == "short" and sl <= w1:
        reasons.append(f"SL≤W1 ({sl}≤{w1})")
    else:
        sl_dist = abs(w1 - sl)
        if sl_dist < MIN_SL_DISTANCE:
            reasons.append(f"SL<{MIN_SL_DISTANCE}$ (dist={sl_dist:.2f})")
    tps = setup.get("tps", [setup.get("tp1")])
    tp1 = tps[0] if tps else setup.get("tp1")
    if tp1 is not None:
        tp1_dist = abs(tp1 - w1)
        if tp1_dist < MIN_TP1_DISTANCE:
            reasons.append(f"TP1<{MIN_TP1_DISTANCE}$ (dist={tp1_dist:.2f})")
    if reasons:
        result = " | ".join(reasons)
        print(f"[{model}] FILTR: {result}")
        return result
    return ""


# ── Śledzenie setupów (pending) ───────────────────────────────────────────────
def next_setup_id() -> int:
    """Shim — ID jest teraz generowany przez SERIAL w PostgreSQL (patrz db.insert_setup)."""
    raise RuntimeError("next_setup_id() nie powinien być wywoływany bezpośrednio — użyj db.insert_setup()")


REPLACE_MIN_DIFF = 0.10  # poniżej → prawdziwy duplikat, pomiń
REPLACE_MAX_DIFF = 0.50  # powyżej → osobny setup


def save_pending(setup: dict, model: str, rejection: str, current_price: float,
                 tradeable: bool = False):
    entries   = setup.get("entries", [])
    tps       = setup.get("tps", [setup.get("tp1"), setup.get("tp2")])
    tps       = [t for t in tps if t is not None]
    new_level = entries[0] if entries else current_price
    direction = setup.get("direction", "-")

    replaced_setup = None  # wypełniane gdy nowy setup zastępuje stary

    # Deduplikacja per model+kierunek+typ+wariant.
    # Grok — brak deduplikacji, każda detekcja zapisywana niezależnie.
    # Algo2 — dedup aktywny zawsze (tradeable i nie-tradeable).
    new_variant = setup.get("variant", "baseline")
    new_type = setup.get("type", setup.get("setup_type", ""))
    if tradeable or model == "Algo2":
        for p in db.get_active_setups():
            if (p["direction"] == direction and p["model"] == model
                    and p.get("variant", "baseline") == new_variant
                    and p.get("type", "") == new_type):
                old_w1 = p["entries"][0] if p["entries"] else 0
                diff = abs(old_w1 - new_level)

                if diff < REPLACE_MIN_DIFF:
                    # Identyczny poziom — prawdziwy duplikat
                    print(f"[pending] Duplikat pominięty: {model} {direction} ~${new_level:.2f} "
                          f"(już istnieje #{p['setup_id']} od {p['model']})")
                    db.log_exchange_event(p["setup_id"], "duplicate_skipped", {
                        "new_w1": new_level, "existing_w1": old_w1, "diff": diff,
                    })
                    return

                if diff < REPLACE_MAX_DIFF:
                    if p.get("entry_hit_at") is not None:
                        # Stary setup już wszedł w pozycję — nie ruszaj
                        print(f"[pending] Pominięto zastępowanie #{p['setup_id']} — już w pozycji")
                        return
                    # Zaktualizowane poziomy — anuluj stary, wstaw nowy
                    now_iso = datetime.now(timezone.utc).isoformat()
                    reason = (f"zastąpiony nowszym setupem W1=${new_level:.2f} "
                              f"(poprzedni W1=${old_w1:.2f}, diff=${diff:.2f})")
                    print(f"[pending] Zastępuję #{p['setup_id']} W1=${old_w1:.2f} → ${new_level:.2f}")
                    db.log_exchange_event(p["setup_id"], "setup_replaced", {
                        "old_w1": old_w1, "new_w1": new_level, "diff": diff,
                        "old_tradeable": p.get("tradeable", False),
                        "old_had_plan_oid": bool(p.get("exchange_plan_oid")),
                    })
                    db.update_setup(p["setup_id"],
                                    tradeable=False,
                                    cancel_reason=reason,
                                    cancel_time=now_iso,
                                    cancel_price=round(current_price, 2))
                    db.resolve_setup(p["setup_id"], "anulowany", None, None, None, None)
                    replaced_setup = {"sid": p["setup_id"], "w1": old_w1, "tradeable": p.get("tradeable", False)}
                    break  # stary anulowany — kontynuuj wstawianie nowego

    # Ustal kierunek aktywacji wejścia (rising = cena musi wzrosnąć do W1, falling = spaść)
    w1_lvl    = entries[0] if entries else current_price
    direction = setup.get("direction", "-")
    if direction == "long":
        entry_trigger = "rising" if w1_lvl > current_price else "falling"
    elif direction == "short":
        entry_trigger = "falling" if w1_lvl < current_price else "rising"
    else:
        entry_trigger = "falling"

    # Natychmiastowe wejście gdy W1 == aktualna cena (np. impulse_aggressive) — tylko nie-tradeable.
    # Dla tradeable setups entry potwierdza exchange_trader przez plan order na Bitget.
    _immediate_entry = not tradeable and abs(w1_lvl - round(current_price, 2)) < 0.005

    row = {
        "alert_time":      datetime.now(timezone.utc).isoformat(),
        "alert_timestamp": int(datetime.now(timezone.utc).timestamp()),
        "model":           model,
        "rejection":       rejection or "",
        "type":            setup.get("type", setup.get("setup_type", "")) or "",
        "direction":       direction,
        "score":           setup.get("total", setup.get("score", 0)),
        "kurs":            round(current_price, 2),
        "price_at_alert":  round(current_price, 2),
        "warunek":         setup.get("warunek", "-"),
        "entry_trigger":   entry_trigger,
        "reasoning":       setup.get("reasoning", ""),
        "entries":         entries,
        "sl":              setup.get("sl"),
        "sl_after_tp1":    setup.get("sl_after_tp1"),
        "tps":             tps,
        "rr":              setup.get("rr", 0),
        "entry_hit_at":    int(datetime.now(timezone.utc).timestamp()) if _immediate_entry else None,
        "status":          "open" if _immediate_entry else "pending",
        "tp1_hit_at":      None,
        "sl_adjusted":     False,
        "shadow":          not tradeable,
        "tradeable":       tradeable,
        "variant":         setup.get("variant") or "baseline",
        "market_context":  setup.get("market_context"),
        "ml_data_only":    not tradeable and bool(rejection),
        "ml_score":        setup.get("ml_score"),
        "ml_composite":    setup.get("ml_composite"),
    }
    sid = db.insert_setup(row)
    if sid is None:
        # Duplikat wykryty na poziomie DB (race condition) — nie ustawiamy setup_id
        print(f"[pending] Duplikat DB: {model} {direction} ~${new_level:.2f}")
        return
    setup["setup_id"] = sid  # mutujemy dict żeby format_alert/format_grok_alert miały dostęp

    if replaced_setup and replaced_setup.get("tradeable"):
        try:
            di = "📉" if direction == "short" else "📈"
            tp1 = tps[0] if len(tps) > 0 else None
            tp2 = tps[1] if len(tps) > 1 else None
            sl  = row.get("sl")
            send_telegram(
                f"🔄 <b>Setup #{replaced_setup['sid']} zastąpiony przez #{sid}</b>\n"
                f"{di} {direction.upper()}"
                f" | W1: ${replaced_setup['w1']:.2f} → ${new_level:.2f}\n"
                + (f"TP1: ${tp1:.2f}" if tp1 else "")
                + (f" | TP2: ${tp2:.2f}" if tp2 else "")
                + (f" | SL: ${sl:.2f}" if sl else "") + "\n"
                f"<i>Algo zaktualizował poziomy</i>"
            )
        except Exception:
            pass


def _hits(candle: dict, price: float, direction: str, side: str, entry_trigger: str = None) -> bool:
    if side == "entry":
        trigger = entry_trigger or ("falling" if direction == "long" else "rising")
        return candle["low"] <= price if trigger == "falling" else candle["high"] >= price
    if side == "sl":
        return candle["low"] <= price if direction == "long" else candle["high"] >= price
    if side == "tp":
        return candle["high"] >= price if direction == "long" else candle["low"] <= price
    return False


def _calc_hypo_result(setup: dict, candles_m15: list[dict]) -> None:
    """Oblicza hipotetyczny wynik dla setupu 'nie weszlo' i zapisuje do DB.

    Symuluje trade na świecach M15: szuka wejścia (W1), potem monitoruje
    TP1/TP2/SL — tak samo jak backtest.simulate_result, ale bez importu backtest.py.
    """
    sid = setup.get("setup_id")
    if not sid:
        return
    try:
        entries      = setup.get("entries") or []
        sl           = setup.get("sl")
        sl_after_tp1 = setup.get("sl_after_tp1")
        tps          = setup.get("tps") or []
        tp1          = tps[0] if tps else None
        tp2          = tps[1] if len(tps) > 1 else None
        d            = setup.get("direction", "long")
        w1           = entries[0] if entries else None

        if not entries or sl is None or w1 is None:
            return

        after_alert = [c for c in candles_m15 if c["time"] > setup["alert_timestamp"]]
        if not after_alert:
            return

        # Szukamy wejścia (max 16 świec = 4h)
        entry_ts = None
        for c in after_alert[:16]:
            if _hits(c, w1, d, "entry"):
                entry_ts = c["time"]
                break
        if entry_ts is None:
            return  # nie weszło nawet hipotetycznie

        # Monitorujemy po wejściu (max 96 świec = 24h)
        after_entry  = [c for c in after_alert if c["time"] > entry_ts]
        result       = None
        tp1_hit_at   = None
        sl_adjusted  = False
        effective_sl = sl

        for c in after_entry[:96]:
            sl_hit  = _hits(c, effective_sl, d, "sl")
            tp2_hit = tp2 is not None and _hits(c, tp2, d, "tp")
            tp1_now = tp1 is not None and _hits(c, tp1, d, "tp")

            if tp2_hit:
                result = "TP2"
                break
            if tp1_now and sl_hit and tp1_hit_at is None:
                result = "SL"
                break
            if tp1_now and tp1_hit_at is None:
                tp1_hit_at = c["time"]
                if tp2 is None:
                    result = "TP1"
                    break
                if sl_after_tp1 is not None and not sl_adjusted:
                    effective_sl = sl_after_tp1
                    sl_adjusted  = True
                continue
            if sl_hit:
                if tp1_hit_at is not None:
                    result = "TP1+BE" if sl_adjusted and sl_after_tp1 is not None and abs(effective_sl - w1) < 0.05 else "TP1+SL"
                else:
                    result = "SL"
                break

        if result is None:
            return  # timeout — brak danych

        # Oblicz avg exit
        if result == "SL":
            eff_exit = sl
        elif result == "TP1":
            eff_exit = tp1
        elif result == "TP2":
            eff_exit = (tp1 + tp2) / 2 if tp1 else tp2
        else:  # TP1+BE, TP1+SL
            eff_exit = (tp1 + effective_sl) / 2 if tp1 else effective_sl

        eff_entry = w1

        # PnL w USD
        trade_usdt = float(os.getenv("BITGET_TRADE_USDT", "100"))
        full_qty   = max(math.floor((trade_usdt * 20 / eff_entry) / 0.1) * 0.1, 0.1)
        half_qty   = max(math.floor((full_qty / 2) / 0.1) * 0.1, 0.1)
        sign       = 1 if d == "long" else -1

        if result == "SL":
            hypo_pnl = round(sign * full_qty * (eff_exit - eff_entry), 2)
        elif result == "TP1":
            tp1_qty = full_qty if tp2 is None else half_qty
            hypo_pnl = round(sign * tp1_qty * (eff_exit - eff_entry), 2)
        else:  # TP2, TP1+BE, TP1+SL — obie połówki
            hypo_pnl = round(sign * (half_qty + half_qty) * (eff_exit - eff_entry), 2)

        db.save_hypo_result(sid, result, hypo_pnl)
        print(f"[pending] #{sid} hypo: {result} PnL={hypo_pnl}")
    except Exception as e:
        print(f"[pending] #{sid} hypo calc error: {e}")


def check_pending(candles_m15: list[dict]):
    pending = db.get_active_setups()
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
            if s.get("exchange_plan_oid"):
                # Setup zarządzany przez Bitget — nie wykrywaj wejścia przez świece.
                # Jedynym źródłem prawdy jest exchange_trader, który co 15s odpytuje
                # Bitget i ustawia exchange_position_opened=True gdy plan order zostanie wykonany.
                if not s.get("exchange_position_opened"):
                    if age_h > ENTRY_TIMEOUT_H:
                        print(f"[pending] #{s.get('setup_id')} Bitget nie weszlo (timeout {ENTRY_TIMEOUT_H}h)")
                        db.resolve_setup(s["setup_id"], "nie weszlo", None, None, None, None)
                        _calc_hypo_result(s, candles_m15)
                        # exchange_trader anuluje plan order przy następnym sync przez get_resolved_with_open_orders()
                    else:
                        still_pending.append(s)
                    continue
                # exchange_trader potwierdził otwarcie pozycji w Bitget
                hit = int(datetime.now(timezone.utc).timestamp())
                print(f"[pending] #{s.get('setup_id')} entry potwierdzony przez Bitget (exchange_position_opened=True)")
            else:
                # Brak plan order w Bitget — wykrywaj wejście przez symulację świec
                et = s.get("entry_trigger")
                if not et:
                    price_at_alert = s.get("price_at_alert") or s.get("kurs", 0)
                    if d == "long":
                        et = "rising" if w1 > price_at_alert else "falling"
                    elif d == "short":
                        et = "falling" if w1 < price_at_alert else "rising"
                    else:
                        et = "falling"
                    print(f"[pending] #{s.get('setup_id')} entry_trigger byl NULL — odtworzono jako '{et}' (W1={w1} price_at_alert={price_at_alert})")
                hit = next((c["time"] for c in after_alert if _hits(c, w1, d, "entry", et)), None)
                if hit is None:
                    if age_h > ENTRY_TIMEOUT_H:
                        print(f"[pending] {s['model']} {d}: nie weszlo")
                        db.resolve_setup(s["setup_id"], "nie weszlo", None, None, None, None)
                        _calc_hypo_result(s, candles_m15)
                        if s.get("tradeable"):
                            try:
                                sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                                send_telegram(
                                    f"⏳ <b>Nie weszło</b> [{s['model']}]{sid_txt}\n"
                                    f"Setup {s['type']} {d.upper()} wygasł bez entry\n"
                                    f"W1: ${w1:.2f} | SL: ${sl:.2f}"
                                )
                            except Exception:
                                pass
                    else:
                        still_pending.append(s)
                    continue
            s["entry_hit_at"] = hit
            # Zapisz entry_hit_at od razu — jeśli setup zdąży też się rozwiązać
            # (TP/SL) w tym samym przebiegu check_pending(), db.resolve_setup()
            # nie nadpisuje tej kolumny, więc bez tego zapisu zostałaby NULL na
            # zawsze mimo poprawnie zapisanego wyniku (setup wygląda jak "nie weszło").
            _entry_upd = {"entry_hit_at": hit}
            if s.get("status") != "after_tp1":
                _entry_upd["status"] = "open"
            db.update_setup(s["setup_id"], **_entry_upd)
            if s.get("tradeable"):
                try:
                    sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                    send_telegram(
                        f"🔔 <b>ENTRY HIT</b> [{s['model']}]{sid_txt}\n"
                        f"Setup {s['type']} {d.upper()} aktywowany!\n"
                        f"W1: ${w1:.2f} | SL: ${sl:.2f} | "
                        f"TP1: ${tp1:.2f}" + (f" | TP2: ${tp2:.2f}" if tp2 else "")
                    )
                except Exception:
                    pass

        result, move  = None, 0.0
        exit_ts       = None
        tp1_hit_at    = s.get("tp1_hit_at")   # może być ustawione z poprzedniego cyklu
        sl_after_tp1  = s.get("sl_after_tp1")
        # Jeśli SL był już przesunięty w poprzednim cyklu, używamy sl_after_tp1 od razu
        effective_sl  = sl_after_tp1 if s.get("sl_adjusted") and sl_after_tp1 is not None else sl

        # Jeśli TP1 był już trafiony w poprzednim cyklu, zaczynamy sprawdzać SL/TP2
        # dopiero od świec PO tp1_hit_at — inaczej świeca TP1 (która ma high blisko W1)
        # może fałszywie wyzwolić sl_hit z przestawionym SL.
        loop_from = tp1_hit_at if tp1_hit_at is not None else s["entry_hit_at"]
        after_entry = [c for c in candles_m15 if c["time"] > loop_from]

        for c in after_entry:
            sl_hit  = _hits(c, effective_sl, d, "sl")
            tp2_hit = tp2 and _hits(c, tp2, d, "tp")
            tp1_now = tp1 and _hits(c, tp1, d, "tp")

            if tp2_hit:
                result, exit_ts = "TP2", c["time"]; break

            # TP1 i SL na tej samej świecy — nie znamy kolejności, bezpieczniej SL
            if tp1_now and sl_hit and tp1_hit_at is None:
                result, exit_ts = "SL", c["time"]; break

            # TP1 trafiony po raz pierwszy — zapisz, wyślij powiadomienie
            if tp1_now and tp1_hit_at is None:
                tp1_hit_at = c["time"]
                s["tp1_hit_at"] = tp1_hit_at
                # Bez TP2 — cała pozycja zamykana na TP1
                if not tp2:
                    result, exit_ts = "TP1", c["time"]
                    if s.get("tradeable"):
                        try:
                            sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                            send_telegram(
                                f"📌 <b>TP1 HIT</b> [{s['model']}]{sid_txt}\n"
                                f"Setup {s['type']} {d.upper()}\n"
                                f"TP1: ${tp1:.2f} osiągnięty ✅\n"
                                f"Pozycja zamknięta na TP1."
                            )
                        except Exception:
                            pass
                    break
                # Z TP2 — przestaw SL i kontynuuj monitorowanie
                if sl_after_tp1 is not None and not s.get("sl_adjusted"):
                    effective_sl   = sl_after_tp1
                    s["sl_adjusted"] = True
                    if s.get("tradeable"):
                        try:
                            be_label = "BE" if abs(sl_after_tp1 - w1) < 0.05 else f"+${abs(sl_after_tp1 - w1):.2f}"
                            sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                            send_telegram(
                                f"📌 <b>TP1 HIT</b> [{s['model']}]{sid_txt}\n"
                                f"Setup {s['type']} {d.upper()}\n"
                                f"TP1: ${tp1:.2f} osiągnięty ✅\n"
                                f"<b>Przesuń SL na: ${sl_after_tp1:.2f}</b>  ({be_label})\n"
                                f"Cel: TP2 ${tp2:.2f}"
                            )
                        except Exception:
                            pass
                continue

            if sl_hit:
                label = ("TP1+BE" if s.get("sl_adjusted") and abs(effective_sl - w1) < 0.05
                         else "TP1+SL" if tp1_hit_at is not None
                         else "SL")
                result, exit_ts = label, c["time"]
                break

        # Które W zostały trafione podczas trwania pozycji + kalkulacja PnL
        if result:
            scan = [c for c in after_entry if c["time"] <= exit_ts]
            entries_hit = 1
            if len(s["entries"]) > 1 and any(_hits(c, s["entries"][1], d, "entry") for c in scan):
                entries_hit = 2
            s["entries_hit"] = entries_hit

            # Średnia arytmetyczna wejść
            active_entries = s["entries"][:entries_hit]
            eff_entry = sum(active_entries) / len(active_entries)

            # Średnia arytmetyczna wyjść (każdy aktywowany próg = jedna obserwacja)
            eff_sl_exit = sl_after_tp1 if s.get("sl_adjusted") and sl_after_tp1 is not None else sl
            if result == "SL":
                exit_prices = [sl]
            elif result == "TP1":
                exit_prices = [tp1]
            elif result == "TP2":
                exit_prices = [tp1, tp2] if tp1 else [tp2]
            else:  # TP1+BE lub TP1+SL
                exit_prices = [tp1, eff_sl_exit] if tp1 else [eff_sl_exit]
            eff_exit = sum(exit_prices) / len(exit_prices)

            # Signed PnL — realny zysk w USD dla danego trade'u
            price_move = (eff_exit - eff_entry) if d == "long" else (eff_entry - eff_exit)
            qty = float((s.get("exchange_qty_full") or "0").replace(",", "."))
            if qty <= 0:
                qty = (TRADE_USDT * LEVERAGE) / eff_entry
            move = round(price_move * qty, 2)

        if result:
            sign = "+" if move >= 0 else ""
            print(f"[pending] {s['model']} {d}: {result} {sign}${move:.2f}")
            db.resolve_setup(s["setup_id"], result, eff_entry, eff_exit, move, exit_ts)
            icon = "💰" if move > 0 else ("⚖️" if move == 0 else "🔴")
            if s.get("tradeable"):
                sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                try:
                    equity = exchange_trader.get_account_balance()
                    equity_txt = f"\nEquity: <b>${equity:.2f}</b>" if equity is not None else ""
                    send_telegram(
                        f"{icon} <b>{result}</b> [{s['model']}]{sid_txt}\n"
                        f"Setup {s['type']} {d.upper()} zamknięty\n"
                        f"Śr. entry: ${eff_entry:.2f} | PnL: {sign}${move:.2f}"
                        + equity_txt
                    )
                except Exception:
                    pass
        # elif not s.get("shadow") and age_h > TRADE_TIMEOUT_H:
        #     db.resolve_setup(s["setup_id"], "nieokreslone", s.get("avg_entry"), None, None, None)
        else:
            still_pending.append(s)
            _upd: dict = dict(
                entry_hit_at=s.get("entry_hit_at"),
                tp1_hit_at=s.get("tp1_hit_at"),
                sl_adjusted=s.get("sl_adjusted", False),
                entries_hit=s.get("entries_hit", 1),
            )
            if s.get("entry_hit_at") is not None and s.get("status") != "after_tp1":
                _upd["status"] = "open"
            db.update_setup(s["setup_id"], **_upd)



# ── Grok shadow — wyłączony (ENABLE_GROK=False) ─────────────────────────────
_last_grok_detection_ts: float = 0.0
_last_algo2_ts:          float = 0.0
_algo2_lock:             threading.Lock = threading.Lock()


def grok_shadow_main() -> None:
    """Wywoływana co 5 min przez scheduler. Aktualnie wyłączona (ENABLE_GROK=False)."""
    if not ENABLE_GROK:
        print("[grok-shadow] ENABLE_GROK=False — pomijam.")
        return


# ── Algorytmiczne anulowanie przestarzałych setupów ──────────────────────────
STALE_DIST_PCT = 0.05  # 5% — max dystans ceny od entry, powyżej = anuluj

def check_stale_setups(regime: dict, current_price: float, candles_h1: list[dict] | None = None):
    """Anuluje nieotwarte setupy, które się zdezaktualizowały.
    Kryteria:
    1. Cena uciekła >5% od entry
    2. Reżim zmienił kierunek (setup short, teraz IMPULSE_UP/TREND_UP i odwrotnie)
    3. Cena przebiła TP1 bez wejścia w pozycję
    Nie-tradeable setupy podlegają tym samym regułom — różnica tylko w tym że nie składamy zleceń.

    Dodatkowo (obserwacyjnie, nic nie anuluje): dla pending trend_pullback aktualizuje
    market_context.structure_broken/closes_beyond_structure, porównując ŚWIEŻE świece H1
    z swing_high/swing_low ZAPISANYM przy tworzeniu setupu (nie przeliczanym na nowo —
    inaczej warunek nigdy nie mógłby być prawdziwy, bo swing_high/low w momencie detekcji
    zawsze "podciąga się" do ówczesnej ceny).
    """
    pending = db.get_active_setups()
    non_entered = [s for s in pending if s.get("entry_hit_at") is None]
    if not non_entered:
        return

    if candles_h1:
        recent_h1 = candles_h1[-2:]
        for s in non_entered:
            if not str(s.get("type", "")).startswith("trend_pullback"):
                continue
            ctx = s.get("market_context") or {}
            swing_h = ctx.get("swing_high")
            swing_l = ctx.get("swing_low")
            d = s.get("direction", "")
            boundary = swing_h if d == "short" else swing_l
            if boundary is None:
                continue
            closes_beyond = sum(
                1 for c in recent_h1
                if (c["close"] > boundary if d == "short" else c["close"] < boundary)
            )
            structure_broken = closes_beyond >= 2
            if ctx.get("closes_beyond_structure") != closes_beyond or ctx.get("structure_broken") != structure_broken:
                db.update_setup(s["setup_id"], market_context={
                    **ctx,
                    "closes_beyond_structure": closes_beyond,
                    "structure_broken": structure_broken,
                })

    regime_dir = regime.get("direction", "none")
    now_iso = datetime.now(timezone.utc).isoformat()
    cancelled = 0

    for s in non_entered:
        sid = s.get("setup_id")
        w1 = s["entries"][0] if s.get("entries") else None
        d = s.get("direction", "")
        if not w1:
            continue

        reason = None

        # 1. Cena uciekła za daleko od entry
        dist_pct = abs(current_price - w1) / current_price
        if dist_pct > STALE_DIST_PCT:
            reason = f"cena uciekła ({dist_pct:.1%} od entry ${w1:.2f})"

        # 2. Reżim zmienił kierunek — wyłączone (algorytm trendu zbyt zawodny)
        # if not reason and regime_dir != "none":
        #     if d == "short" and regime_dir == "up":
        #         reason = f"zmiana reżimu na {regime['regime']} (setup short)"
        #     elif d == "long" and regime_dir == "down":
        #         reason = f"zmiana reżimu na {regime['regime']} (setup long)"

        # 3. Cena przebiła TP1 bez wejścia w pozycję
        if not reason and s.get("tps"):
            tp1 = s["tps"][0]
            if d == "long" and current_price > tp1:
                reason = f"cena przebiła TP1 ${tp1:.2f} bez wejścia (long)"
            elif d == "short" and current_price < tp1:
                reason = f"cena przebiła TP1 ${tp1:.2f} bez wejścia (short)"

        if reason:
            print(f"[stale] #{sid} anulowany: {reason}")
            db.log_exchange_event(sid, "stale_cancelled", {
                "reason": reason, "w1": w1, "direction": d,
                "tradeable": s.get("tradeable", False),
                "had_plan_oid": bool(s.get("exchange_plan_oid")),
            })
            db.update_setup(sid,
                            tradeable=False,
                            cancel_reason=reason,
                            cancel_time=now_iso,
                            cancel_price=round(current_price, 2))
            db.resolve_setup(sid, "anulowany", None, None, None, None)
            cancelled += 1

            di = "📉" if d == "short" else "📈"
            tp1 = s["tps"][0] if s.get("tps") else None
            if s.get("tradeable"):
                try:
                    send_telegram(
                        f"🚫 <b>Setup #{sid} anulowany</b>\n"
                        f"{di} {d.upper()}"
                        + (f" | W1: ${w1:.2f}" if w1 else "")
                        + (f" | TP1: ${tp1:.2f}" if tp1 else "") + "\n"
                        f"<i>{reason}</i>\n"
                        f"Cena: ${current_price:.2f}"
                    )
                except Exception:
                    pass

    if cancelled:
        print(f"[stale] Anulowano {cancelled} setupów.")


# ── Inwalidacja otwartych setupów (po wejściu w pozycję) ─────────────────────

def _handle_open_invalidation(setup: dict, reason: str, action: str, current_price: float) -> None:
    """
    Obsługuje inwalidację otwartego setupu.
    action == "move_sl_to_entry": przesuwa SL do ceny wejścia (break-even)
    action == "close":            zamyka pozycję market orderem
    """
    setup_id  = setup["setup_id"]
    direction = setup.get("direction", "")
    avg_entry = setup.get("avg_entry")
    now_iso   = datetime.now(timezone.utc).isoformat()
    di        = "📉" if direction == "short" else "📈"

    if action == "move_sl_to_entry":
        new_sl = round(float(avg_entry), 2) if avg_entry else round(current_price, 2)
        print(f"[open_inval] #{setup_id}: BE — SL → {new_sl} | {reason}")

        exchange_trader.move_sl_to_entry(setup_id, new_sl)

        db.update_setup(setup_id,
                        sl=new_sl,
                        cancel_reason=reason,
                        cancel_time=now_iso,
                        cancel_price=round(current_price, 2))
        if setup.get("tradeable"):
            try:
                send_telegram(
                    f"⚠️ <b>Open setup #{setup_id} — BE</b>\n"
                    f"{di} {direction.upper()}"
                    + (f" | entry: ${avg_entry:.2f}" if avg_entry else "") + "\n"
                    f"SL przesunięty → ${new_sl:.2f}\n"
                    f"<i>{reason}</i>\n"
                    f"Cena: ${current_price:.2f}"
                )
            except Exception:
                pass

    elif action == "close":
        print(f"[open_inval] #{setup_id}: zamknięcie pozycji | {reason}")

        exchange_trader.close_open_position(setup_id)

        move = None
        if avg_entry:
            move = round(current_price - float(avg_entry), 4) if direction == "long" \
                   else round(float(avg_entry) - current_price, 4)

        db.update_setup(setup_id,
                        tradeable=False,
                        cancel_reason=reason,
                        cancel_time=now_iso,
                        cancel_price=round(current_price, 2))
        db.resolve_setup(setup_id, "inwalidacja", avg_entry,
                         round(current_price, 2), move,
                         int(time.time()))

        pnl_str = f"{move:+.2f} USD" if move is not None else "n/d"
        if setup.get("tradeable"):
            try:
                equity = exchange_trader.get_account_balance()
                equity_txt = f"\nEquity: <b>${equity:.2f}</b>" if equity is not None else ""
                send_telegram(
                    f"🛑 <b>Open setup #{setup_id} zamknięty — inwalidacja</b>\n"
                    f"{di} {direction.upper()}"
                    + (f" | entry: ${avg_entry:.2f}" if avg_entry else "") + "\n"
                    f"<i>{reason}</i>\n"
                    f"Cena zamknięcia: ${current_price:.2f} | P&L: {pnl_str}"
                    + equity_txt
                )
            except Exception:
                pass


def check_open_setups_invalidation(regime: dict, current_price: float) -> None:
    """Zamyka otwarte setupy po przekroczeniu OPEN_TRADE_TIMEOUT_H od wejścia."""
    open_setups = [s for s in db.get_active_setups()
                   if s.get("entry_hit_at") is not None
                   and s.get("status") == "open"]

    if not open_setups:
        return

    invalidated = 0
    for s in open_setups:
        entry_hit_at = s.get("entry_hit_at")
        age_h = (time.time() - entry_hit_at) / 3600 if entry_hit_at else 0
        if age_h > OPEN_TRADE_TIMEOUT_H:
            reason = f"timeout {OPEN_TRADE_TIMEOUT_H}h od wejścia"
            _handle_open_invalidation(s, reason, "close", current_price)
            invalidated += 1

    if invalidated:
        print(f"[open_inval] Przetworzono {invalidated} otwartych setupów.")


# ── Anti-spam ─────────────────────────────────────────────────────────────────
def was_alerted(model: str, level: float, direction: str) -> bool:
    return db.was_alerted(model, level, direction)

def save_alerted(model: str, level: float, direction: str):
    db.save_alerted(model, level, direction)


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
    sl      = setup.get("sl", 0)
    rr      = setup.get("rr", 0)
    d       = setup.get("direction", "")
    dist    = abs(current_price - entries[0]) if entries else 0
    icon    = "📈 Long" if d == "long" else "📉 Short"
    setup_type = setup.get("type", "")
    reasoning  = setup.get("reasoning", "")

    entries_txt = "\n".join(f"W{i+1}: ${e:.2f}" for i, e in enumerate(entries))
    tps_txt = "\n".join(
        f"  TP{i+1}: ${t:.2f}  (+{abs(t - entries[0]) / entries[0] * 100:.1f}%)" if entries and entries[0] else f"  TP{i+1}: ${t:.2f}"
        for i, t in enumerate(tps)
    ) if tps else "-"

    sid_txt = f" #{setup.get('setup_id')}" if setup.get("setup_id") else ""
    type_line = f"<b>Typ:</b> {setup_type}\n" if setup_type else ""

    diag_lines = []
    if reasoning:
        for line in reasoning.split("\n"):
            line = line.strip()
            if any(k in line for k in ["Cena:", "Swing", "Consolidation:", "WYNIK:"]):
                diag_lines.append(line)
    diag_txt = "\n".join(diag_lines) if diag_lines else ""

    dynamic_usdt = setup.get("trade_usdt") or TRADE_USDT
    try:
        balance = exchange_trader.get_account_balance()
        if balance is not None:
            dynamic_usdt = round(max(balance, 1.0), 2)
    except Exception:
        pass
    eff_lev = LEVERAGE
    try:
        settings = db.get_app_settings()
        eff_lev = int(settings.get("leverage") or LEVERAGE)
    except Exception:
        pass
    trade_margin = round(dynamic_usdt, 2)

    return (
        f"🎯 <b>SOL/USDT — {model}{sid_txt}</b>\n"
        f"{icon}  |  {datetime.now(TZ).strftime('%d.%m  %H:%M')}\n"
        + type_line
        + (f"<pre>{diag_txt}</pre>\n" if diag_txt else "")
        + f"Cena teraz: <b>${current_price:.2f}</b>  (~${dist:.2f} do wejścia)\n"
        + f"{entries_txt}\n"
        f"<b>SL:</b>  ${sl:.2f}\n"
        + f"{tps_txt}\n"
        f"<b>RR:</b>  {rr:.1f}:1\n"
        f"Składam zlecenie o wartości: <b>${trade_margin}</b>"
    )


def format_grok_alert(result: dict, sol_price: float, setup_id=None, model_name: str = "Grok") -> str:
    bias      = result.get("bias", "neutral").capitalize()
    bias_proc = result.get("bias_proc", 0)
    sentyment = result.get("sentyment", "")
    analiza   = result.get("analiza", "")
    akcja     = result.get("akcja", "")

    icon    = "📈" if bias.lower() == "long" else ("📉" if bias.lower() == "short" else "⚖️")
    now     = datetime.now(TZ).strftime("%d.%m  %H:%M")
    sid_txt = f" #{setup_id}" if setup_id else ""

    lines = [
        f"{icon} <b>{model_name} SOL/USDT — {bias} ({bias_proc}%){sid_txt}</b>",
        f"{now}  |  SOL: <b>${sol_price:.2f}</b>",
    ]

    if sentyment:
        lines.append(f"\n<b>Sentyment:</b>  {sentyment}")
    if analiza:
        lines.append(f"\n<b>Analiza:</b>  {analiza}")

    if result.get("send_alert"):
        wejscia = result.get("wejscia", [])
        tp1     = result.get("tp1")
        tp2     = result.get("tp2")
        sl      = result.get("sl")
        rr      = result.get("rr")

        if wejscia:
            lines.append("\n<b>Wejścia:</b>")
            for i, w in enumerate(wejscia, 1):
                poziom  = w.get("poziom", "-")
                warunek = w.get("warunek", "")
                lines.append(f"  W{i}: <b>${poziom:.2f}</b>" + (f"  ({warunek})" if warunek else ""))
        if tp1 is not None:
            lines.append(f"<b>TP1:</b>  ${tp1:.2f}")
        if tp2 is not None:
            lines.append(f"<b>TP2:</b>  ${tp2:.2f}")
        if sl is not None:
            lines.append(f"<b>SL:</b>  ${sl:.2f}")
        sl_after_tp1 = result.get("sl_after_tp1")
        if sl_after_tp1 is not None and wejscia:
            w1_lvl = wejscia[0].get("poziom")
            be_label = "BE" if (w1_lvl and abs(sl_after_tp1 - w1_lvl) < 0.05) else f"+${abs(sl_after_tp1 - w1_lvl):.2f}" if w1_lvl else ""
            lines.append(f"<b>SL po TP1:</b>  ${sl_after_tp1:.2f}" + (f"  ({be_label})" if be_label else ""))
        if rr is not None:
            lines.append(f"<b>R:R:</b>  {rr:.1f}:1")

    if akcja:
        lines.append(f"\n<i>{akcja}</i>")

    return "\n".join(lines)


# ── Breakout scanner (szybki, co 2-3 min) ────────────────────────────────────

# Cooldown na powiadomienie Telegram (nie spamuj tym samym reżimem częściej niż co 30 min)
_last_breakout_tg_ts: float = 0.0
_last_breakout_tg_regime: str = ""

def _algo2_run(regime: dict, candles_m15: list, candles_h1: list, current: float, is_impulse: bool) -> str:
    """
    Wykonuje detekcję Algo2 i zapis setupów. Wywoływana z main() i breakout_scan().

    Flow: zapisz wszystko → filtruj → handluj.
    1. algo_detect_setups() generuje WSZYSTKICH kandydatów
    2. ML scoring (jeśli model dostępny)
    3. Zapisz KAŻDY setup do bazy (obserwacja, dane treningowe)
    4. Filtruj: validate_setup() + wariant włączony do live
    5. Najlepszy kandydat → GPT3 validator → live trade (update w bazie)

    Zwraca: 'rejected' gdy GPT3 Validator odrzucił best,
            'saved' / 'no_setups' / 'skipped' / 'duplicate' w pozostałych przypadkach.
    """
    orderbook = fetch_order_book(SYMBOL)
    algo2_setups, algo2_log = algo_detect_setups(regime, candles_m15, candles_h1, current, orderbook)
    n_total = len(algo2_setups)
    print(f"[algo2] Reżim: {regime['regime']}({regime.get('score', 0)}) | Setupów: {n_total}")
    _last_feedback["Algo2"] = {
        "time":  datetime.now(TZ).isoformat(),
        "found": bool(algo2_setups),
        "count": n_total,
        "text":  _clean_log(algo2_log),
    }
    try:
        import db as _db
        _db.save_algo_scans(dict(_last_feedback))
    except Exception:
        pass

    if not algo2_setups:
        return "no_setups"

    # ── ML scoring (jeśli model dostępny) ────────────────────────────────────
    try:
        import ml_scorer
        for s in algo2_setups:
            _ml_prob = ml_scorer.score_setup(s)
            if _ml_prob is not None:
                s["ml_score"] = _ml_prob
                s["ml_composite"] = ml_scorer.composite_score(_ml_prob, s.get("rr", 0))
    except ImportError:
        pass
    except Exception as _e:
        print(f"[algo2] ML scoring error: {_e}")

    # ── KROK 1: Zapisz WSZYSTKIE setupy do bazy ─────────────────────────────
    for s in algo2_setups:
        s["reasoning"] = algo2_log
        is_rejected = s.get("rejected_by_algo", False)
        rejection = "; ".join(s.get("filter_reasons", [])) if is_rejected else ""
        save_pending(s, "Algo2", rejection, current, tradeable=False)
        if s.get("setup_id"):
            tag = "ML data (rejected)" if is_rejected else "observation"
            print(f"[algo2] {tag}: {s['type']} [{s.get('variant','?')}] #{s['setup_id']} RR={s.get('rr',0)}")

    # ── KROK 2: Filtruj kandydatów do live handlu ────────────────────────────
    live_candidates = []
    for s in algo2_setups:
        if not s.get("setup_id"):
            continue
        if s.get("rejected_by_algo"):
            continue
        if s.get("not_tradeable"):
            continue
        val_reason = validate_setup(s, "Algo2")
        if val_reason:
            db.update_setup(s["setup_id"], rejection=val_reason)
            print(f"[algo2] #{s['setup_id']} → rejected (validate: {val_reason})")
            continue
        if not _is_type_bitget_enabled(s.get("type", ""), s.get("variant")):
            continue
        live_candidates.append(s)

    if not live_candidates:
        return "saved"

    # Sortuj po RR (docelowo: po ml_composite)
    live_candidates.sort(key=lambda s: s.get("rr", 0), reverse=True)
    best = live_candidates[0]

    level = best["entries"][0]
    dist  = abs(current - level)
    print(f"[algo2] Best live: {best['type']} {best['direction']} W=${level:.2f} "
          f"(dist=${dist:.2f}) RR={best['rr']} #{best['setup_id']}")

    # ── KROK 3: GPT3 Validator — pomijany w IMPULSE ─────────────────────────
    val_result = None
    if ENABLE_GPT3_VALIDATOR and not is_impulse:
        val_atr    = calc_atr(candles_m15)
        val_sup    = regime.get("support")
        val_res    = regime.get("resistance")
        val_rng    = regime.get("range_size", 0)
        val_pct    = max(0.0, min(100.0, (current - val_sup) / val_rng * 100)) if val_rng and val_sup else 50.0
        val_result = call_gpt3_validator(
            best, candles_m15, candles_h1, current,
            atr=val_atr, support=val_sup, resistance=val_res,
            price_pct_in_range=val_pct,
        )
        if val_result:
            approved   = val_result.get("approve", True)
            val_reason = val_result.get("reason", "")
            val_conf   = val_result.get("confidence", 0)
            print(f"[gpt3-val] {'APPROVE' if approved else 'REJECT'} ({val_conf}%) — {val_reason}")
            if not approved:
                db.update_setup(best["setup_id"], llm_scores={
                    "gpt3_validator": {"confidence": val_conf, "approved": False, "reason": val_reason}
                })
                db.resolve_setup(best["setup_id"], "odrzucony_validator", None, None, None, None)
                print(f"[algo2] Setup #{best['setup_id']} odrzucony przez GPT3 Validator.")
                return "rejected"
        else:
            print("[gpt3-val] Brak odpowiedzi — kontynuuję bez walidacji.")
    elif is_impulse:
        print("[algo2] IMPULSE — GPT3 Validator pominięty.")

    # ── KROK 4: Promuj best do live handlu ───────────────────────────────────
    is_tradeable = not bool(ALGO2_SHADOW_MODE)
    update_fields = {"tradeable": is_tradeable, "shadow": not is_tradeable, "ml_data_only": False}
    if is_tradeable:
        update_fields["status"] = "pending"
        update_fields["entry_hit_at"] = None
    db.update_setup(best["setup_id"], **update_fields)
    db.log_exchange_event(best["setup_id"], "setup_promoted_live" if is_tradeable else "setup_kept_observation", {
        "type": best.get("type", ""), "variant": best.get("variant", "baseline"),
        "direction": best.get("direction", ""), "w1": best["entries"][0] if best.get("entries") else None,
        "rr": best.get("rr", 0), "algo2_shadow_mode": bool(ALGO2_SHADOW_MODE),
    })
    if is_tradeable:
        send_telegram(format_alert("Algo2", best, current, True))
    if val_result and is_tradeable:
        db.update_setup(best["setup_id"], llm_scores={
            "gpt3_validator": {"confidence": val_conf, "approved": True, "reason": val_reason}
        })
    print(f"[algo2] #{best['setup_id']} → {'observation' if not is_tradeable else 'LIVE'}")
    return "saved"


def breakout_scan():
    """Szybki skan co 3 min — inwalidacja setupów + Telegram przy IMPULSE + Algo2 przy IMPULSE."""
    global _last_breakout_tg_ts, _last_breakout_tg_regime, _last_algo2_ts

    candles_m15 = fetch_klines(SYMBOL, "15m", limit=100)
    candles_h1  = fetch_klines(SYMBOL, "1h",  limit=50)
    current     = fetch_current_price(SYMBOL) or candles_m15[-1]["close"]
    regime      = detect_market_regime(candles_m15, candles_h1, current)

    # Anuluj przestarzałe setupy (co 3 min — szybciej niż main)
    check_stale_setups(regime, current, candles_h1)
    check_open_setups_invalidation(regime, current)

    if regime["regime"] == "RANGE":
        return

    is_impulse = regime["regime"] in ("IMPULSE_UP", "IMPULSE_DOWN")
    now = time.time()

    # Telegram notification — tylko przy IMPULSE, cooldown 30 min
    if is_impulse and not (regime["regime"] == _last_breakout_tg_regime
                           and now - _last_breakout_tg_ts < 1800):
        _last_breakout_tg_ts = now
        _last_breakout_tg_regime = regime["regime"]

        direction = regime.get("direction", "")
        if direction == "down":
            icon = "🔻"
        elif direction == "up":
            icon = "🔺"
        else:
            icon = "⚡"

        c24 = regime.get("change_24h", 0)
        c48 = regime.get("change_48h", 0)
        msg = (
            f"{icon} <b>Algo2: {regime['regime']} — SOL/USDT</b>\n\n"
            f"Cena ${current:.2f} | 24h: {c24:+.1f}% | 48h: {c48:+.1f}%\n"
            f"Siła: {regime.get('score', 0)}/10 | Volume: {regime['vol_ratio']}x\n"
            f"Sygnały: {regime['details']}"
        )
        try:
            send_telegram(msg)
        except Exception as e:
            print(f"[breakout_scan] send_telegram error: {e}")

    # Algo2 detekcja — natychmiast gdy IMPULSE, throttle 3 min (wspólny lock z main())
    if is_impulse:
        _bs_should_run = False
        with _algo2_lock:
            if now - _last_algo2_ts >= 3 * 60:
                _last_algo2_ts = now
                _bs_should_run = True
        if _bs_should_run:
            print("[algo2] IMPULSE w breakout_scan — uruchamiam detekcję Algo2.")
            _algo2_run(regime, candles_m15, candles_h1, current, is_impulse=True)

    # Vol-spike fast path — duży spike (>=5x) przed przebiciem szczytu (reżim TREND)
    # Pozwala zareagować na gwałtowny ruch zanim formalnie spełni warunki IMPULSE.
    elif not is_impulse and regime.get("vol_ratio", 1.0) >= 5.0:
        _bs_vol_should_run = False
        with _algo2_lock:
            if now - _last_algo2_ts >= 2 * 60:
                _last_algo2_ts = now
                _bs_vol_should_run = True
        if _bs_vol_should_run:
            print(f"[algo2] Vol-spike {regime['vol_ratio']:.1f}x w breakout_scan (TREND) — uruchamiam detekcję Algo2.")
            _algo2_run(regime, candles_m15, candles_h1, current, is_impulse=False)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _last_algo2_ts
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] SOL Alert v2 — start")

    candles_m15 = fetch_klines(SYMBOL, "15m", limit=100)
    candles_h1  = fetch_klines(SYMBOL, "1h",  limit=50)
    current     = fetch_current_price(SYMBOL) or candles_m15[-1]["close"]
    rng         = detect_range(candles_m15)
    trend       = h1_trend(candles_h1)
    regime      = detect_market_regime(candles_m15, candles_h1, current)

    print(f"SOL: ${current:.2f} | Zakres: ${rng['support']}-${rng['resistance']} (${rng['range_size']:.2f}) | H1: {trend} | Reżim: {regime['regime']}")

    # Sprawdz oczekujace setupy
    check_pending(candles_m15)

    # Exchange sync wyłączony — Bitget testowany osobnym workflow
    # exchange_trader.sync()

    # Algorytmiczne anulowanie przestarzałych setupów (co 15 min)
    check_stale_setups(regime, current, candles_h1)
    check_open_setups_invalidation(regime, current)

    # ── Algo2 — algorytmiczne setupy trend/impulse/range ───────────────────
    _a2_now      = time.time()
    _a2_impulse  = regime["regime"] in ("IMPULSE_UP", "IMPULSE_DOWN")
    _a2_threshold = 3 * 60 if _a2_impulse else 15 * 60
    _a2_should_run = False
    with _algo2_lock:
        if _a2_now - _last_algo2_ts >= _a2_threshold:
            _last_algo2_ts = _a2_now
            _a2_should_run = True
    if not _a2_should_run:
        _a2_mins = round((_a2_threshold - (_a2_now - _last_algo2_ts)) / 60, 1)
        print(f"[algo2] Za wcześnie — następna detekcja za ~{_a2_mins} min (reżim: {regime['regime']})")
    else:
        _a2_result = _algo2_run(regime, candles_m15, candles_h1, current, _a2_impulse)
        if _a2_result == "rejected":
            return

    # Składa plan order dla nowo zapisanych setupów (natychmiast po wygenerowaniu alertu)
    exchange_trader.sync()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Wyczyść arkusze Alerty i Wyniki przed uruchomieniem")
    args, _ = parser.parse_known_args()
    if args.reset:
        print("[reset] Czyszczenie arkuszy Alerty i Wyniki...")
        _get_sheets(reset=True)
        print("[reset] Gotowe.")
    else:
        main()
