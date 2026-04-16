#!/usr/bin/env python3
"""
SOL Alert Bot v2
Algorytm vs Claude Sonnet — porównanie dwóch podejść do detekcji setupów SOL/USDT
"""

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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
import exchange_trader
import db

TZ = ZoneInfo("Europe/Warsaw")

# ── Konfiguracja ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",  "8645260464:AAGe_uTew0H1gJnijdcR7oav_A4U8n1HLHI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7442390334")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY       = os.getenv("OPENAI_API_KEY", "")
XAI_KEY          = os.getenv("XAI_API_KEY", "")
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
ALGO2_SHADOW_MODE    = True   # tryb obserwacji — wszystkie Algo2 setupy jako shadow (dedup aktywny)

# ── Feedback z ostatniego uruchomienia (odczytywany przez dashboard) ──────────
_last_feedback: dict = {}  # {"Algo2": {...}, "Grok": {...}}


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
    return "  ".join(lines)


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
        from datetime import datetime, timezone as _tz
        newest_ts = datetime.fromtimestamp(int(data[0][0]) // 1000, tz=_tz.utc).strftime("%Y-%m-%d %H:%M")
        oldest_ts = datetime.fromtimestamp(int(data[-1][0]) // 1000, tz=_tz.utc).strftime("%Y-%m-%d %H:%M")
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
        "change_24h": round(change_24h, 1), "change_48h": round(change_48h, 1),
    }

    # ── IMPULSE: gwałtowny ruch w ostatnich godzinach ────────────────────────
    impulse_score = 0
    impulse_dir = "none"

    if imp_str >= 2:
        impulse_score += 1
    if vol_ratio >= 1.5:
        impulse_score += 1
    # Zmiana 4h LUB silna zmiana 2h (łapie świeże impulsy)
    if abs(change_4h) >= 2.0:
        impulse_score += 1
    elif abs(change_4h) >= 1.5 and vol_ratio >= 1.3:
        impulse_score += 1
    if abs(change_2h) >= 1.2:
        impulse_score += 1
        if impulse_dir == "none":
            impulse_dir = "down" if change_2h < 0 else "up"
    # Kierunek z ostatnich 6 M15 (4+ bearish/bullish = silny kierunek)
    if bearish_closes >= 4:
        impulse_score += 1
        impulse_dir = "down"
    elif bullish_closes >= 4:
        impulse_score += 1
        impulse_dir = "up"

    # Próg obniżony do 2 gdy ruch 4h jest bardzo silny (>= 3%) —
    # łapie aktywny impuls w fazie krótkiego pullbacku, gdy vol/bearish_closes
    # chwilowo nie spełniają pełnego kryterium.
    impulse_min_score = 2 if abs(change_4h) >= 3.0 else 3

    # ── SPIKE-REVERSAL FILTER ────────────────────────────────────────────────
    # Wykrywa sytuacje gdy change_4h sugeruje impuls, ale krótkoterminowe dane
    # wskazują że ruch już się odwraca (spike pump-and-dump / wick rejection).
    # Jeśli ≥ 2 sygnały aktywne → podnosimy próg do 4 (trudniejsze wejście).
    spike_reversal_score = 0
    _idir = impulse_dir if impulse_dir != "none" else ("down" if change_4h < 0 else "up")

    # Sygnał 1: zmiana 1h silnie przeciwna do kierunku impulsu (odwrót już trwa)
    if _idir == "up"   and change_1h < -0.8:
        spike_reversal_score += 1
    elif _idir == "down" and change_1h >  0.8:
        spike_reversal_score += 1

    # Sygnał 2: zmiana 2h też już pod prąd (odwrót trwa dłużej)
    if _idir == "up"   and change_2h < -0.6:
        spike_reversal_score += 1
    elif _idir == "down" and change_2h >  0.6:
        spike_reversal_score += 1

    # Sygnał 3: rejection wicks na ostatnich 3 świecach M15
    # (cień po stronie impulsu > 1.5× ciało → odrzucenie poziomu)
    _recent3 = candles_m15[-3:]
    _bodies  = [abs(c["close"] - c["open"]) + 0.001 for c in _recent3]
    if _idir == "up":
        _wicks = [c["high"] - max(c["open"], c["close"]) for c in _recent3]
    else:
        _wicks = [min(c["open"], c["close"]) - c["low"]  for c in _recent3]
    if sum(w / b for w, b in zip(_wicks, _bodies)) / 3 > 1.5:
        spike_reversal_score += 1

    if spike_reversal_score >= 2:
        impulse_min_score = max(impulse_min_score, 4)
        log.info(f"[REGIME] Spike-reversal filter: score={spike_reversal_score}, "
                 f"1h:{change_1h:+.1f}% 2h:{change_2h:+.1f}%, min_score→{impulse_min_score}")

    if impulse_score >= impulse_min_score:
        if impulse_dir == "none":
            impulse_dir = "down" if change_4h < 0 else "up"
        strength = min(10, impulse_score * 2 + imp_str)
        details = (f"2h:{change_2h:+.1f}% 4h:{change_4h:+.1f}%; imp:{imp_str}; "
                   f"vol:{vol_ratio:.1f}x; bear:{bearish_closes}/6; spk:{spike_reversal_score}")
        return {
            **base,
            "regime": f"IMPULSE_{impulse_dir.upper()}",
            "direction": impulse_dir, "score": strength,
            "spike_score": spike_reversal_score,
            "pct_outside": 0, "details": details,
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
    return {
        **base,
        "regime": "RANGE",
        "direction": "none", "score": 0,
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
# Klucz → (fib_wejście_lo, fib_wejście_hi, fib_sl, atr_sl_mult, strength_min, force_shadow)
# baseline  = aktualne ustawienia produkcyjne
# str4      = identyczna geometria, ale próg strength obniżony do 4
# shallow   = płytszy pullback (fib25-38) z ciaśniejszym SL (fib50), też strength>=4
_PULLBACK_VARIANTS: dict[str, tuple] = {
    "baseline": (0.38, 0.50, 0.618, 0.3, 5, False),
    "str4":     (0.38, 0.50, 0.618, 0.3, 4, True),
    "shallow":  (0.25, 0.38, 0.500, 0.1, 4, True),
}


def algo_detect_setups(regime: dict, candles_m15: list[dict], candles_h1: list[dict],
                       current_price: float) -> tuple[list[dict], str]:
    """Algorytmicznie wykrywa setupy trend/impulse/range.
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
            for vname, (fib_lo, fib_hi, fib_sl, atr_sl, str_min, v_shadow) in _PULLBACK_VARIANTS.items():
                # str4 generuje tylko gdy strength==4 (baseline już pokrywa strength>=5)
                if vname == "str4" and strength != 4:
                    continue
                if strength < str_min:
                    continue
                entry_mid = (fib_lo + fib_hi) / 2
                w   = round(swing_low + entry_mid * swing_range, 2)
                sl  = round(swing_low + fib_sl * swing_range + atr * atr_sl, 2)
                tp1 = round(swing_low, 2)
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
                if rr_ok and above_price and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED [{vname}]")
                    setups.append({
                        "type": "trend_pullback_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": rr_val,
                        "score": strength,
                        "variant": vname,
                        "force_shadow": v_shadow,
                        "reasoning": f"{regime_name}({strength}); swing ${swing_low:.0f}-${swing_high:.0f} [{vname}]",
                    })
                else:
                    reasons = []
                    if not rr_ok: reasons.append(f"RR<1.5({rr_val})")
                    if not above_price: reasons.append("W<=cena")
                    if not dist_ok: reasons.append(f"dist>3%({w-current_price:.2f})")
                    log_lines.append(f"    ✗ REJECTED [{vname}]: {', '.join(reasons)}")

        # impulse_continuation_short — mini-pullback w impulsie
        if regime_name.startswith("IMPULSE_"):
            _cont_spike = regime.get("spike_score", 0)
            last6 = candles_m15[-6:]
            greens = [c for c in last6 if c["close"] > c["open"]]
            log_lines.append(f"  → impulse_cont: greens={len(greens)}/6 (need 1-2) spike={_cont_spike}")
            if _cont_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_cont_spike}>=2 (odwrót)")
            elif len(greens) >= 1 and len(greens) <= 2:
                pullback_high = max(c["high"] for c in last6[-2:])
                w = round(pullback_high, 2)
                sl = round(pullback_high + atr * 0.8, 2)
                tp1 = round(swing_low, 2)
                tp2 = round(swing_low - atr, 2)
                rr_ok = sl > w and tp1 < w and (w - tp1) / (sl - w) >= 1.5
                dist_ok = abs(w - current_price) <= max_entry_dist
                log_lines.append(f"    W=${w:.2f} dist=${abs(w-current_price):.2f} rr_ok={rr_ok} dist_ok={dist_ok}")
                if rr_ok and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED")
                    setups.append({
                        "type": "impulse_continuation_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((w - tp1) / (sl - w), 1),
                        "score": strength,
                        "reasoning": f"{regime_name}({strength}); pullback M15",
                    })

        # impulse_aggressive_short — market entry natychmiast, vol >= 2.0x (force_shadow — tryb testowy)
        if regime_name.startswith("IMPULSE_"):
            _agg_vol   = regime.get("vol_ratio", 1.0)
            _agg_spike = regime.get("spike_score", 0)
            log_lines.append(f"  → impulse_aggressive: vol={_agg_vol:.1f}x spike={_agg_spike}")
            if _agg_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_agg_spike}>=2")
            elif _agg_vol < 2.0:
                log_lines.append(f"    ✗ SKIP: vol={_agg_vol:.1f}x<2.0")
            else:
                w   = round(current_price, 2)
                sl  = round(current_price + atr * 1.2, 2)
                tp1 = round(swing_low, 2)
                tp2 = round(swing_low - atr, 2)
                rr_ok = tp1 < w and (w - tp1) / (sl - w) >= 1.5
                log_lines.append(f"    W=${w:.2f} SL=${sl:.2f} TP1=${tp1:.2f} rr_ok={rr_ok}")
                if rr_ok:
                    log_lines.append(f"    ✓ ACCEPTED (force_shadow — tryb testowy)")
                    setups.append({
                        "type": "impulse_aggressive_short", "direction": "short",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((w - tp1) / (sl - w), 1),
                        "score": strength,
                        "reasoning": f"{regime_name}({strength}); vol={_agg_vol:.1f}x aggressive",
                        "force_shadow": True,
                    })
                else:
                    log_lines.append(f"    ✗ REJECTED: RR<1.5")

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
            for vname, (fib_lo, fib_hi, fib_sl, atr_sl, str_min, v_shadow) in _PULLBACK_VARIANTS.items():
                # str4 generuje tylko gdy strength==4 (baseline już pokrywa strength>=5)
                if vname == "str4" and strength != 4:
                    continue
                if strength < str_min:
                    log_lines.append(f"  → pullback_long [{vname}]: SKIP (strength={strength}<{str_min})")
                    continue
                entry_mid = (fib_lo + fib_hi) / 2
                w   = round(swing_high - entry_mid * swing_range, 2)
                sl  = round(swing_high - fib_sl * swing_range - atr * atr_sl, 2)
                tp1 = round(swing_high, 2)
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
                if rr_ok and below_price and dist_ok:
                    log_lines.append(f"    ✓ ACCEPTED [{vname}]")
                    setups.append({
                        "type": "trend_pullback_long", "direction": "long",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": rr_val,
                        "score": strength,
                        "variant": vname,
                        "force_shadow": v_shadow,
                        "reasoning": f"{regime_name}({strength}); swing ${swing_low:.0f}-${swing_high:.0f} [{vname}]",
                    })
                else:
                    reasons = []
                    if not rr_ok: reasons.append(f"RR<1.5({rr_val})")
                    if not below_price: reasons.append("W>=cena")
                    if not dist_ok: reasons.append(f"dist>3%({current_price-w:.2f})")
                    log_lines.append(f"    ✗ REJECTED [{vname}]: {', '.join(reasons)}")

        # impulse_aggressive_long — market entry natychmiast, vol >= 2.0x (force_shadow — tryb testowy)
        if regime_name.startswith("IMPULSE_"):
            _agg_vol   = regime.get("vol_ratio", 1.0)
            _agg_spike = regime.get("spike_score", 0)
            log_lines.append(f"  → impulse_aggressive: vol={_agg_vol:.1f}x spike={_agg_spike}")
            if _agg_spike >= 2:
                log_lines.append(f"    ✗ SKIP: spike_score={_agg_spike}>=2")
            elif _agg_vol < 2.0:
                log_lines.append(f"    ✗ SKIP: vol={_agg_vol:.1f}x<2.0")
            else:
                w   = round(current_price, 2)
                sl  = round(current_price - atr * 1.2, 2)
                tp1 = round(swing_high, 2)
                tp2 = round(swing_high + atr, 2)
                rr_ok = tp1 > w and (tp1 - w) / (w - sl) >= 1.5
                log_lines.append(f"    W=${w:.2f} SL=${sl:.2f} TP1=${tp1:.2f} rr_ok={rr_ok}")
                if rr_ok:
                    log_lines.append(f"    ✓ ACCEPTED (force_shadow — tryb testowy)")
                    setups.append({
                        "type": "impulse_aggressive_long", "direction": "long",
                        "entries": [w], "sl": sl, "sl_after_tp1": w,
                        "tps": [tp1, tp2], "rr": round((tp1 - w) / (w - sl), 1),
                        "score": strength,
                        "reasoning": f"{regime_name}({strength}); vol={_agg_vol:.1f}x aggressive",
                        "force_shadow": True,
                    })
                else:
                    log_lines.append(f"    ✗ REJECTED: RR<1.5")

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

            if (w - tp1) / (sl - w) >= 1.5 and dist_ok and momentum_ok_s and touches_ok_s and ma_ok_s:
                log_lines.append(f"    ✓ ACCEPTED")
                setups.append({
                    "type": "range_resistance_short", "direction": "short",
                    "entries": [round(w, 2)], "sl": round(sl, 2),
                    "sl_after_tp1": round(w, 2),
                    "tps": [round(tp1, 2), round(tp2, 2)],
                    "rr": round((w - tp1) / (sl - w), 1),
                    "score": 0,
                    "reasoning": f"RANGE; S=${sup:.2f} R=${res:.2f} touches={r_touches}",
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

            if (tp1 - w) / (w - sl) >= 1.5 and dist_ok and momentum_ok and touches_ok and ma_ok:
                log_lines.append(f"    ✓ ACCEPTED")
                setups.append({
                    "type": "range_support_long", "direction": "long",
                    "entries": [round(w, 2)], "sl": round(sl, 2),
                    "sl_after_tp1": round(w, 2),
                    "tps": [round(tp1, 2), round(tp2, 2)],
                    "rr": round((tp1 - w) / (w - sl), 1),
                    "score": 0,
                    "reasoning": f"RANGE; S=${sup:.2f} R=${res:.2f} touches={s_touches}",
                })
    else:
        log_lines.append(f"  Brak setupów dla direction={direction}")

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
        entry_dt = datetime.utcfromtimestamp(entry_ts).astimezone(TZ).strftime("%H:%M") if entry_ts else ""
        exit_dt  = datetime.utcfromtimestamp(exit_ts).astimezone(TZ).strftime("%H:%M")  if exit_ts  else ""
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
    """Zapisuje wynik shadow-trackowanego (anulowanego przez Groka) setupu."""
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
        entry_dt = datetime.utcfromtimestamp(entry_ts).astimezone(TZ).strftime("%H:%M") if entry_ts else ""
        exit_dt  = datetime.utcfromtimestamp(exit_ts).astimezone(TZ).strftime("%H:%M")  if exit_ts  else ""
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
        entry_dt = datetime.utcfromtimestamp(entry_ts).astimezone(TZ).strftime("%H:%M") if entry_ts else ""
        exit_dt  = datetime.utcfromtimestamp(exit_ts).astimezone(TZ).strftime("%H:%M")  if exit_ts  else ""
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


def save_pending(setup: dict, model: str, rejection: str, current_price: float, shadow: bool = False):
    entries   = setup.get("entries", [])
    tps       = setup.get("tps", [setup.get("tp1"), setup.get("tp2")])
    tps       = [t for t in tps if t is not None]
    new_level = entries[0] if entries else current_price
    direction = setup.get("direction", "-")

    replaced_setup = None  # wypełniane gdy nowy setup zastępuje stary

    # Shadow setups (Grok) — brak deduplikacji, każda detekcja zapisywana niezależnie.
    # Zwykłe setups (Algo2) — blokuj duplikat jeśli jakikolwiek model ma ten sam kierunek/poziom.
    # Algo2 shadow mode — dedup aktywny nawet gdy shadow=True (obserwacja w warunkach live).
    new_variant = setup.get("variant", "baseline")
    if not shadow or model == "Algo2":
        for p in db.get_active_setups():
            if (p["direction"] == direction and p["model"] == model
                    and p.get("variant", "baseline") == new_variant):
                old_w1 = p["entries"][0] if p["entries"] else 0
                diff = abs(old_w1 - new_level)

                if diff < REPLACE_MIN_DIFF:
                    # Identyczny poziom — prawdziwy duplikat
                    print(f"[pending] Duplikat pominięty: {model} {direction} ~${new_level:.2f} "
                          f"(już istnieje #{p['setup_id']} od {p['model']})")
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
                    db.update_setup(p["setup_id"],
                                    shadow=True,
                                    cancel_reason=reason,
                                    cancel_time=now_iso,
                                    cancel_price=round(current_price, 2))
                    db.resolve_setup(p["setup_id"], "anulowany", None, None, None, None)
                    replaced_setup = {"sid": p["setup_id"], "w1": old_w1}
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
        "entry_hit_at":    None,
        "tp1_hit_at":      None,
        "sl_adjusted":     False,
        "entries_hit":     1,
        "shadow":          shadow,
        "variant":         setup.get("variant", "baseline"),
    }
    sid = db.insert_setup(row)
    if sid is None:
        # Duplikat wykryty na poziomie DB (race condition) — nie ustawiamy setup_id
        print(f"[pending] Duplikat DB: {model} {direction} ~${new_level:.2f}")
        return
    setup["setup_id"] = sid  # mutujemy dict żeby format_alert/format_grok_alert miały dostęp

    if replaced_setup:
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
            hypo_pnl = round(sign * half_qty * (eff_exit - eff_entry), 2)
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
                        if not s.get("shadow"):
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
            if not s.get("shadow"):
                try:
                    sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                    send_telegram(
                        f"✅ <b>ENTRY HIT</b> [{s['model']}]{sid_txt}\n"
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
                    if not s.get("shadow"):
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
                    if not s.get("shadow"):
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
            if not s.get("shadow"):
                icon = "💰" if move > 0 else ("⚖️" if move == 0 else "🔴")
                sid_txt = f" #{s['setup_id']}" if s.get("setup_id") else ""
                try:
                    send_telegram(
                        f"{icon} <b>{result}</b> [{s['model']}]{sid_txt}\n"
                        f"Setup {s['type']} {d.upper()} zamknięty\n"
                        f"Śr. entry: ${eff_entry:.2f} | PnL: {sign}${move:.2f}"
                    )
                except Exception:
                    pass
        elif age_h > TRADE_TIMEOUT_H:
            db.resolve_setup(s["setup_id"], "nieokreslone", s.get("avg_entry"), None, None, None)
        else:
            still_pending.append(s)
            db.update_setup(s["setup_id"],
                            entry_hit_at=s.get("entry_hit_at"),
                            tp1_hit_at=s.get("tp1_hit_at"),
                            sl_adjusted=s.get("sl_adjusted", False),
                            entries_hit=s.get("entries_hit", 1))



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

def check_stale_setups(regime: dict, current_price: float):
    """Anuluje nieotwarte setupy, które się zdezaktualizowały.
    Kryteria:
    1. Cena uciekła >5% od entry
    2. Reżim zmienił kierunek (setup short, teraz IMPULSE_UP/TREND_UP i odwrotnie)
    3. Cena przebiła TP1 bez wejścia w pozycję
    """
    pending = db.get_active_setups()
    non_entered = [s for s in pending
                   if s.get("entry_hit_at") is None and not s.get("shadow")]
    if not non_entered:
        return

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
            db.update_setup(sid,
                            shadow=True,
                            cancel_reason=reason,
                            cancel_time=now_iso,
                            cancel_price=round(current_price, 2))
            db.resolve_setup(sid, "anulowany", None, None, None, None)
            cancelled += 1

            di = "📉" if d == "short" else "📈"
            tp1 = s["tps"][0] if s.get("tps") else None
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
                        shadow=True,
                        cancel_reason=reason,
                        cancel_time=now_iso,
                        cancel_price=round(current_price, 2))
        db.resolve_setup(setup_id, "inwalidacja", avg_entry,
                         round(current_price, 2), move,
                         int(time.time() * 1000))

        pnl_str = f"{move:+.2f} USD" if move is not None else "n/d"
        try:
            send_telegram(
                f"🛑 <b>Open setup #{setup_id} zamknięty — inwalidacja</b>\n"
                f"{di} {direction.upper()}"
                + (f" | entry: ${avg_entry:.2f}" if avg_entry else "") + "\n"
                f"<i>{reason}</i>\n"
                f"Cena zamknięcia: ${current_price:.2f} | P&L: {pnl_str}"
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
    score   = setup.get("total", setup.get("score", 0))
    sl      = setup.get("sl", 0)
    rr      = setup.get("rr", 0)
    d       = setup.get("direction", "")
    dist    = abs(current_price - entries[0]) if entries else 0
    icon    = "📈 Long" if d == "long" else "📉 Short"
    setup_type = setup.get("type", "")
    entries_txt = "\n".join(f"  W{i+1}: ${e:.2f}" for i, e in enumerate(entries))
    tps_txt     = "\n".join(f"  TP{i+1}: ${t:.2f}  (+${abs(t - entries[0]):.2f})" for i, t in enumerate(tps)) if entries else "-"
    reasoning   = setup.get("reasoning", "")

    sl_after_tp1     = setup.get("sl_after_tp1")
    sl_after_tp1_txt = ""
    if sl_after_tp1 is not None and entries:
        be_label = "BE" if abs(sl_after_tp1 - entries[0]) < 0.05 else f"+${abs(sl_after_tp1 - entries[0]):.2f}"
        sl_after_tp1_txt = f"<b>SL po TP1:</b>  ${sl_after_tp1:.2f}  ({be_label})\n"

    sid_txt = f" #{setup.get('setup_id')}" if setup.get("setup_id") else ""

    # Typ setupu + skrócona diagnostyka (dla Algo2)
    type_line = f"<b>Typ:</b> {setup_type}\n" if setup_type else ""

    # Wyciągnij kluczowe linie z loga (swing, consol, reżim) do Telegrama
    diag_lines = []
    if reasoning:
        for line in reasoning.split("\n"):
            line = line.strip()
            if any(k in line for k in ["Cena:", "Swing", "Consolidation:", "WYNIK:"]):
                diag_lines.append(line)
    diag_txt = "\n".join(diag_lines) if diag_lines else ""

    return (
        f"🎯 <b>SOL/USDT — {model}{sid_txt}</b>\n"
        f"{icon}  |  {datetime.now(TZ).strftime('%d.%m  %H:%M')}\n\n"
        + type_line
        + (f"<pre>{diag_txt}</pre>\n" if diag_txt else "")
        + f"\nCena teraz: <b>${current_price:.2f}</b>  (~${dist:.2f} do wejścia)\n\n"
        f"<b>Ustaw zlecenia:</b>\n{entries_txt}\n\n"
        f"<b>SL:</b>  ${sl:.2f}\n"
        + sl_after_tp1_txt
        + f"\n<b>Cele:</b>\n{tps_txt}\n\n"
        f"<b>RR:</b>  {rr:.1f}:1\n"
        + f"\n⚠️ <i>Decyzja nalezy do Ciebie.</i>"
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


# ── Migracja setup_id dla istniejących setupów bez ID ─────────────────────────
def _migrate_setup_ids():
    """Nieaktualna — ID są teraz generowane przez SERIAL w PostgreSQL."""
    pass


# ── Breakout scanner (szybki, co 2-3 min) ────────────────────────────────────

# Cooldown na powiadomienie Telegram (nie spamuj tym samym reżimem częściej niż co 30 min)
_last_breakout_tg_ts: float = 0.0
_last_breakout_tg_regime: str = ""

def _algo2_run(regime: dict, candles_m15: list, candles_h1: list, current: float, is_impulse: bool) -> str:
    """
    Wykonuje detekcję Algo2 i zapis setupów. Wywoływana z main() i breakout_scan().
    Zakłada, że throttle został już sprawdzony i _last_algo2_ts zaktualizowany przez wywołującego.

    Logika zapisu:
    - force_shadow (np. impulse_aggressive): zawsze shadow=True, bez GPT3, bez Telegrama.
    - regularne (pozostałe): najlepszy RR → walidacja → GPT3 → real order;
      gorsze RR → shadow=True dla analizy porównawczej.

    Zwraca: 'rejected' gdy GPT3 Validator odrzucił best (main() powinien wtedy return),
            'saved' / 'no_setups' / 'skipped' / 'duplicate' w pozostałych przypadkach.
    """
    algo2_setups, algo2_log = algo_detect_setups(regime, candles_m15, candles_h1, current)
    n_total = len(algo2_setups)
    print(f"[algo2] Reżim: {regime['regime']}({regime.get('score', 0)}) | Setupów: {n_total}")
    _last_feedback["Algo2"] = {
        "time":  datetime.now(TZ).isoformat(),
        "found": bool(algo2_setups),
        "count": n_total,
        "text":  _clean_log(algo2_log),
    }

    if not algo2_setups:
        log_to_alerty("Algo2", "brak_setupu", {
            "type": "", "direction": "", "reasoning": algo2_log,
            "kurs": round(current, 2),
        })
        return "no_setups"

    # Podziel na force_shadow (testy) i regularne
    force_shadow_setups = [s for s in algo2_setups if s.get("force_shadow")]
    regular_setups = sorted(
        [s for s in algo2_setups if not s.get("force_shadow")],
        key=lambda s: s["rr"], reverse=True,
    )

    # ── Force-shadow setups — zapis bez GPT3 i bez Telegrama ─────────────
    for s in force_shadow_setups:
        s["reasoning"] = algo2_log
        if not validate_setup(s, "Algo2"):
            save_pending(s, "Algo2", "", current, shadow=True)
            if s.get("setup_id"):
                print(f"[algo2] Shadow (test): {s['type']} #{s['setup_id']} RR={s['rr']}")

    if not regular_setups:
        return "saved" if any(s.get("setup_id") for s in force_shadow_setups) else "no_setups"

    # ── Regularne: najlepszy RR → real, gorsze → shadow dla analizy ──────
    best = regular_setups[0]
    best["reasoning"] = algo2_log

    for s in regular_setups[1:]:
        s["reasoning"] = algo2_log
        if not validate_setup(s, "Algo2"):
            save_pending(s, "Algo2", "", current, shadow=True)
            if s.get("setup_id"):
                print(f"[algo2] Shadow (gorszy RR): {s['type']} #{s['setup_id']} RR={s['rr']}")

    level = best["entries"][0]
    dist  = abs(current - level)
    print(f"[algo2] Best: {best['type']} {best['direction']} W=${level:.2f} (dist=${dist:.2f}) RR={best['rr']}")

    rejection = validate_setup(best, "Algo2")
    if rejection:
        log_to_alerty("Algo2", rejection, best)
        return "skipped"

    # ── GPT3 Validator — pomijany w IMPULSE (szybkość > jakość) ──────────
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
                log_to_alerty("Algo2", f"GPT3-val odrzucił: {val_reason}", best)
                save_pending(best, "Algo2", f"GPT3-val odrzucił: {val_reason}", current, shadow=True)
                if best.get("setup_id"):
                    db.update_setup(best["setup_id"], llm_scores={
                        "gpt3_validator": {"confidence": val_conf, "approved": False, "reason": val_reason}
                    })
                    db.resolve_setup(best["setup_id"], "odrzucony_validator", None, None, None, None)
                print(f"[algo2] Setup odrzucony przez GPT3 Validator.")
                return "rejected"
        else:
            print("[gpt3-val] Brak odpowiedzi — kontynuuję bez walidacji.")
    elif is_impulse:
        print("[algo2] IMPULSE — GPT3 Validator pominięty.")
    # ── koniec walidatora ─────────────────────────────────────────────────

    is_shadow = ALGO2_SHADOW_MODE
    save_pending(best, "Algo2", "", current, shadow=is_shadow)
    if best.get("setup_id"):
        log_to_alerty("Algo2", "", best)
        if is_shadow:
            send_telegram(f"👁 <b>[Algo2-shadow]</b> {best['type']} {best['direction'].upper()}"
                          f" | W=${best['entries'][0]:.2f} SL=${best['sl']:.2f}"
                          f" TP1=${best['tps'][0]:.2f} RR={best['rr']}")
        else:
            send_telegram(format_alert("Algo2", best, current, True))
        if val_result and not is_shadow:
            db.update_setup(best["setup_id"], llm_scores={
                "gpt3_validator": {"confidence": val_conf, "approved": True, "reason": val_reason}
            })
        return "saved"
    else:
        print("[algo2] Duplikat pominięty — setup już istnieje.")
        return "duplicate"


def breakout_scan():
    """Szybki skan co 3 min — inwalidacja setupów + Telegram przy IMPULSE + Algo2 przy IMPULSE."""
    global _last_breakout_tg_ts, _last_breakout_tg_regime, _last_algo2_ts

    candles_m15 = fetch_klines(SYMBOL, "15m", limit=100)
    candles_h1  = fetch_klines(SYMBOL, "1h",  limit=50)
    current     = fetch_current_price(SYMBOL) or candles_m15[-1]["close"]
    regime      = detect_market_regime(candles_m15, candles_h1, current)

    # Anuluj przestarzałe setupy (co 3 min — szybciej niż main)
    check_stale_setups(regime, current)
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
        send_telegram(msg)

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


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _last_algo2_ts
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] SOL Alert v2 — start")

    _migrate_setup_ids()

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
    check_stale_setups(regime, current)
    check_open_setups_invalidation(regime, current)

    # ── 1. Algorytm (stary, range-based) — WYŁĄCZONY, zastąpiony przez Algo2 ──
    # algo_setups  = algo_detect(candles_m15, candles_h1, rng)
    # filter_passed = bool(algo_setups)
    # best_algo    = max(algo_setups, key=lambda x: x["total"]) if algo_setups else None
    print("[algo] Pominięty (zastąpiony przez Algo2).")

    # ── 2. Claude (wyłączony — ENABLE_CLAUDE = False) ─────────────────────────
    print("[claude] Wyłączony (ENABLE_CLAUDE=False).")

    # ── 3. GPT (wyłączony — ENABLE_GPT = False) ───────────────────────────────
    print("[gpt] Wyłączony (ENABLE_GPT=False).")

    # ── 4. Grok (wyłączony — zastąpiony przez Algo2) ────────────────────────
    print("[grok] Wyłączony (ENABLE_GROK=False).")

    # ── 4b. Algo2 — algorytmiczne setupy trend/impulse/range ─────────────
    # Internal throttle: 3 min w IMPULSE (bez GPT3 Validator), 15 min w pozostałych reżimach.
    # Lock chroni _last_algo2_ts przed race condition z breakout_scan().
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

    # ── 5. GPT Relaxed (live search — sam pobiera BTC/ETH/F&G) ──────────────
    print("[gpt-r] Wyłączony (ENABLE_GPT_RELAXED=False).")

    # ── 6. GPT3 — regime-aware, trend_consolidation_long włączony ───────────────
    print("[gpt3] Wyłączony (ENABLE_GPT3=False).")

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
