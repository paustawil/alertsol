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
import openai
import gspread
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

TZ = ZoneInfo("Europe/Warsaw")

# ── Konfiguracja ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",  "8645260464:AAGe_uTew0H1gJnijdcR7oav_A4U8n1HLHI")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7442390334")
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_KEY       = os.getenv("OPENAI_API_KEY", "")
SYMBOL           = "SOLUSDT"
MIN_SCORE        = 9
COOLDOWN_HOURS   = 4
PENDING_FILE     = "pending_setups.json"
COOLDOWN_FILE    = "last_alerts.json"
SHEET_ID         = "19TWHI4sJnJznyaGzA97AOBQp7oKUauSqBY1K0jiuPZE"
ENTRY_TIMEOUT_H  = 4
TRADE_TIMEOUT_H  = 24
MIN_SL_DISTANCE  = 0.30   # minimalna odleglosc W1-SL w USD; ponizej = odrzucony setup


# ── System prompt dla Claude ──────────────────────────────────────────────────
FORTECA_PROMPT = """FORTeca v1.0 — CLAUDE EDITION — SOL/USDT SETUP DETECTION

You are a precise technical analyst. Your goal is ACCURACY — neither force setups that aren't there, nor miss setups that clearly are. Evaluate the data objectively and call what you see.

==================================================
PRECISION RULES
==================================================
- Do NOT force a setup when market context is genuinely ambiguous or between levels.
- Do NOT call range trading a "setup" if price is drifting without cleanly approaching a key boundary.
- Do NOT return a setup just because some structure exists — structure alone is not enough.
- Calculate RR from natural technical levels. If the result is borderline (1.7–1.9), state it in reasoning and let the score reflect it.
- If a level seems "okay" rather than clearly significant, Level = 1, not 2 or 3.
- Score accurately: 3 = clearly strong, 2 = solid, 1 = weak but present, 0 = absent or opposing. Use the full scale.
- Never invent levels that are not clearly visible in the supplied OHLCV data.

==================================================
TIMEFRAME LOGIC
==================================================
Use H1 candles for market context: dominant bias, major support/resistance zones, trending/ranging.
Use M15 candles for execution: exact entry zone, setup timing, invalidation, TP placement.
Establish H1 context FIRST, then evaluate if M15 confirms.
If H1 and M15 are materially misaligned, reduce score significantly and reject unless the misalignment itself defines the setup (e.g., false breakout after failed H1 breakout).

==================================================
VALID SETUP FAMILIES — only these 4
==================================================

SETUP 1 — PULLBACK IN TREND
H1 shows a clear directional trend. M15 is pulling back into meaningful support (long) or resistance (short): prior breakout area, prior swing level turned S/R, H1/M15 overlap level, local demand/supply zone. Pullback should look corrective, not like a structural reversal.
REJECT if: pullback broke prior structure in the opposite direction, price is between levels, impulse is exhausted with poor RR, or entry is already late.

SETUP 2 — BOUNCE FROM KEY LEVEL
Price reaches a genuinely strong support or resistance with visible historical significance and reacts from it.
REJECT if: level is weak or touched only once, price is chopping around the level, or RR is poor.

SETUP 3 — BREAKOUT RETEST
A meaningful level was broken with clear directional intent. Price is now retesting that level from the other side and holding.
REJECT if: breakout was weak or gradual, retest goes too deep through the level, or entry is already extended.

SETUP 4 — FALSE BREAKOUT / RECLAIM
Price briefly breaks an important level, fails to continue, and returns back through it — trapping breakout participants.
REJECT if: the level is not important, the reclaim or rejection is weak, or price returns into noisy consolidation with no edge.

==================================================
KEY LEVEL DETECTION
==================================================
Identify support and resistance ONLY from the supplied H1 and M15 OHLCV data. Do not invent levels.
Strong level criteria (need at least one): H1 swing high/low, 2+ visible reactions, recent breakout/retest zone, aligns on both H1 and M15.
A level touched only once with no visible follow-up reaction is NOT strong enough to build a setup on.

==================================================
5-PILLAR SCORING (integer 0–3 each, max total 15)
==================================================

1. TREND (directional alignment, mainly H1)
3 = H1 shows clear directional bias AND M15 broadly confirms setup direction
2 = decent H1 directional bias in setup direction
1 = weak or partial directional alignment only
0 = H1 direction unclear or contradicts setup direction
Note: Countertrend setups cannot score 3. Ranging H1 cannot exceed 1.

2. STRUCTURE (how clean is market structure relative to setup)
3 = clean, readable structure clearly matching setup logic
2 = structure mostly readable and supportive of setup
1 = setup idea exists but structure is damaged, messy, or recovering
0 = chaotic structure, no readable sequence, or structure opposes setup

3. LEVEL (quality of the actual level where the trade is built)
3 = strong, obvious, recently respected key level — multiple reactions OR clear H1 swing OR confirmed breakout/retest
2 = relevant level with visible technical importance
1 = weak or questionable level
0 = no meaningful level at the entry zone
IMPORTANT: Never accept a setup if Level < 2.

4. MOMENTUM (whether price behavior supports the expected move)
3 = strong, clear price behavior supporting the setup (impulsive move in setup direction + visible stall/rejection on opposing side)
2 = acceptable support from recent price behavior
1 = mixed signals, weak confirmation
0 = momentum clearly against setup direction

5. RR (reward/risk ratio, computed to TP2 from average layered entry)
average_entry = mean(W1, W2)
risk = |average_entry - SL|
reward = |TP2 - average_entry|
rr = reward / risk
3 = rr >= 2.5
2 = rr 1.8 to 2.49
1 = rr 1.2 to 1.79
0 = rr < 1.2
Do NOT manipulate SL to improve RR. If the natural technical SL gives rr < 1.6, reject the setup.

==================================================
MANDATORY ACCEPTANCE THRESHOLD
==================================================
Return setup_found=true ONLY if ALL of the following are true:
- total score >= 9
- Level >= 2
- RR >= 1.6 (natural, not forced by artificially tight SL)
- setup logic is specific, clear, and executable
- SL is placed at a genuine technical invalidation point (beyond structure)
- entries are realistic levels derived from the supplied data

Score guide: 9-10 = acceptable, 11-12 = strong, 13-15 = exceptional.

==================================================
ENTRY MODEL — W1 / W2 (two layers only)
==================================================
For long: W1 = higher entry (first/aggressive entry near current price or zone top), W2 = deeper entry (at zone bottom or structural confluence). List W1 first (closer to current price), W2 second (deeper).
For short: W1 = lower entry (first/aggressive entry), W2 = higher entry (deeper). List W1 first, W2 second.
Use both layers only if a real zone justifies two distinct entry points. Keep the zone tight enough to represent one idea.
average_entry for RR purposes = mean(W1, W2).

==================================================
STOP LOSS LOGIC
==================================================
Long: SL below the structural low / support that defines the setup idea.
Short: SL above the structural high / resistance that defines the setup idea.
SL must be at the true invalidation point — where the setup logic is broken. Never use fixed pip/ATR distance as SL.
One SL applies to both W1 and W2.

==================================================
TARGET SIZE CONSTRAINTS
==================================================
- TP1 must be at least 0.5 USD from W1. If the nearest meaningful level is closer than 0.5 USD, REJECT the setup.
- TP2 is placed at the next meaningful technical level in the setup direction. No hard distance cap — place it where the chart dictates.
- TP2 must always be farther from W1 than TP1 in the trade direction.

==================================================
TARGET LOGIC
==================================================
TP1 = first realistic reaction point (nearest opposing intraday structure in setup direction, minimum 0.5 USD from W1).
TP2 = main Forteca target — next meaningful technical level in the setup direction. No hard distance cap. Used for RR calculation.
TP2 must always be farther than TP1 in trade direction. Both targets must be technically grounded in the data.

==================================================
POSITION MANAGEMENT — sl_after_tp1
==================================================
After TP1 is hit, the SL is moved to protect the trade. Calculate sl_after_tp1 as follows:
- Identify the most recent structural support (long) or resistance (short) that formed between W1/W2 and TP1.
- If such a level exists and is above W1 (long) or below W1 (short): use it as sl_after_tp1.
- If no clear structural level exists between entry and TP1, or if the level is not in profit territory: use W1 as sl_after_tp1 (break-even).
- sl_after_tp1 must always be: above W1 for long (in profit or at BE), below W1 for short (in profit or at BE).
- sl_after_tp1 is the SL level used for TP2 monitoring after TP1 is hit. Include it in the output.

==================================================
WHEN TO RETURN NO SETUP
==================================================
Return setup_found=false in any of these situations:
- Market is in messy or overlapping consolidation with no clear level being approached.
- Price is between levels, not at a reaction zone.
- H1 and M15 are materially conflicting.
- Entry zone has already passed (too late, price already extended toward TP).
- RR to TP2 does not reach 1.6 with a natural SL.
- No clean technical invalidation level exists.
- Setup logic is vague, uncertain, or "possible but not clear".
- TP1 is less than 0.5 USD from W1.

==================================================
OUTPUT FORMAT — CRITICAL
==================================================

Your ENTIRE response MUST be a single JSON object — nothing else.
Start your response with { and end with }. No text before, no text after, no markdown, no code fences.
If you write anything outside the JSON, the system will fail. JSON only.

Write "reasoning" and "invalidation" values in Polish.

If setup found:
{"setup_found":true,"setup_type":"setup family name","direction":"long","score":12,"pillars":{"trend":3,"structure":2,"level":3,"momentum":2,"rr":2},"entries":[88.95,88.70],"sl":88.10,"sl_after_tp1":88.95,"tp1":89.80,"tp2":90.60,"rr":2.3,"reasoning":"krótkie uzasadnienie po polsku z konkretnymi poziomami z danych","invalidation":"warunek unieważnienia setupu po polsku"}

If no setup:
{"setup_found":false,"reasoning":"konkretny powód braku setupu po polsku"}"""


# ── System prompt dla GPT (Forteca v1.0 pełna wersja) ────────────────────────
FORTECA_GPT_PROMPT = """FORTeca v1.0 — RULES FOR SOL/USDT SETUP DETECTION AND TRADE PLAN GENERATION
You are not a generic analyst. You must evaluate SOL/USDT only through the Forteca v1.0 framework.
Your task is to accurately decide whether a tradeable Forteca setup exists. Be precise — neither force setups that aren't there, nor miss setups that clearly are. If yes, return a precise level-based plan.

==================================================
FORTeca CORE PHILOSOPHY
==================================================
1. Forteca is a setup-based model, not a prediction model. Detect whether a high-quality setup is present now or is very close to activation.
2. Forteca is level-based, not time-based. Entries, stop loss and targets are placed at price levels. Wick touches do not matter by themselves.
3. Forteca does not force trades. If the market is inside messy consolidation, between levels, or without a clear edge, return no setup.
4. Forteca prioritizes quality. A setup that clearly meets the criteria should be reported — do not suppress it out of excessive caution.
5. Do not artificially tighten SL just to improve RR.
6. The plan must be executable. All entries must be realistic nearby levels derived from the provided H1 and M15 data.

==================================================
TIMEFRAME LOGIC
==================================================
Use H1 for context and M15 for execution.
H1 answers: dominant directional bias, major support/resistance zones, trending/reversing/ranging.
M15 answers: executable setup, entry zone, invalidation, TP1 and TP2 placement.
If H1 and M15 are strongly misaligned, lower setup quality significantly.
If H1 is neutral and M15 gives only a weak local setup, do not force a trade.

==================================================
FORTeca SETUPS
==================================================
Only 4 setup families are valid:

SETUP 1 — PULLBACK IN TREND
H1 shows clear directional context. M15 is pulling back into a meaningful support (long) or resistance (short): prior breakout area, prior swing level turned S/R, local demand/supply zone, clustered lows/highs, H1/M15 overlap level. The pullback should look corrective, not like full structural breakdown.
Reject if: pullback already broke prior structure in opposite direction, price is between levels, or impulse is stretched with poor RR.

SETUP 2 — BOUNCE FROM KEY LEVEL
Price reaches a strong support or resistance and reacts from it. Valid only when the level is genuinely important (repeatedly respected or clearly visible on H1).
Reject if: level is weak or only touched once, price is chopping around the level, or RR is poor.

SETUP 3 — BREAKOUT RETEST
A meaningful level was broken with directional intent. Price is now retesting that level from the other side and holds.
Reject if: breakout was weak, retest goes too deep through the level, or entry is already late.

SETUP 4 — FALSE BREAKOUT / RECLAIM
Price briefly breaks an important level, fails to continue, and returns back through it, trapping breakout participants.
Reject if: the level is not important, the reclaim/rejection is weak, or price returns into noisy consolidation.

==================================================
KEY LEVEL DETECTION
==================================================
Derive support/resistance only from the supplied H1 and M15 OHLCV data.
Priority: (1) obvious H1 swing highs/lows, (2) H1 breakout/breakdown levels, (3) M15 levels repeatedly respected, (4) clustered rejection highs/lows, (5) recent local extremes controlling current price.
A level is stronger if: respected multiple times, caused impulsive reaction, exists on both H1 and M15, is recent and relevant.

==================================================
FORTeca 5-PILLAR SCORING
==================================================
Each pillar is an integer 0-3. Maximum total = 15.

1. TREND (directional alignment, mainly H1)
0 = H1 direction unclear or contradictory to setup
1 = weak directional bias or partial alignment only
2 = decent directional bias in favor of setup
3 = clear directional bias, M15 broadly aligned
Notes: Countertrend bounces usually cannot receive 3. If H1 is flat/ranging, trend usually cannot exceed 1.

2. STRUCTURE (how clean is market structure relative to setup)
0 = chaotic, no readable sequence, or structure against setup
1 = setup idea exists but structure is damaged or messy
2 = structure is readable and mostly supportive
3 = clean structure clearly matching setup logic

3. LEVEL (quality of the actual level where trade is built)
0 = no meaningful level
1 = weak or questionable level
2 = relevant level with visible technical importance
3 = strong, obvious, recent, repeatedly respected or clearly broken/retested key level
Note: A setup should almost never be accepted if Level < 2.

4. MOMENTUM (whether price behavior supports expected move)
0 = momentum clearly against setup
1 = mixed momentum, weak confirmation
2 = acceptable support from price behavior
3 = strong supportive price behavior
Favor impulsive moves in setup direction. Favor visible slowdown/stalling against opposing side.

5. RR (reward-to-risk to TP2 from average layered entry)
Compute: average_entry = mean(W1,W2), risk = |average_entry - SL|, reward = |TP2 - average_entry|, rr = reward/risk
0 = rr < 1.2
1 = rr 1.2 to 1.79
2 = rr 1.8 to 2.49
3 = rr >= 2.5
Do not manipulate SL to force better RR.

==================================================
VALID SETUP THRESHOLD
==================================================
Return setup_found=true ONLY if ALL are true:
- total score >= 9
- Level >= 2
- RR >= 1.6
- setup logic is clear and executable
- SL is placed beyond true invalidation, not artificially tight

Score 9-10 = acceptable. Score 11-12 = strong. Score 13-15 = exceptional.

==================================================
ENTRY MODEL — W1 / W2 (two layers only)
==================================================
For long: W1 = higher entry (first/aggressive entry, zone top or nearest level), W2 = deeper entry (zone bottom or structural confluence). List W1 first, W2 second.
For short: W1 = lower entry (first/aggressive), W2 = higher entry (deeper). List W1 first, W2 second.
Use both layers only when a real zone justifies two distinct entry points. Keep zone tight enough to represent one idea.
average_entry for RR = mean(W1, W2). One SL applies to both layers.

==================================================
STOP LOSS LOGIC
==================================================
SL must invalidate the idea, not just the exact entry.
Long: SL below the support/reclaim/structural low defining the setup.
Short: SL above the resistance/retest/structural high defining the setup.
Never tighten SL purely to improve RR.

==================================================
TARGET SIZE CONSTRAINTS
==================================================
- TP1 must be at least 0.5 USD from W1. If the nearest meaningful level is closer than 0.5 USD, REJECT the setup.
- TP2 is placed at the next meaningful technical level in the setup direction. No hard distance cap — place it where the chart dictates.
- TP2 must always be farther from W1 than TP1 in the trade direction.

==================================================
TARGET LOGIC
==================================================
TP1 = first realistic reaction point (nearest opposing intraday structure, minimum 0.5 USD from W1).
TP2 = main target — next meaningful technical level in the setup direction. No hard distance cap. Used for RR calculation.
TP2 must always be farther than TP1 in trade direction. Targets must be technically grounded.

==================================================
POSITION MANAGEMENT — sl_after_tp1
==================================================
After TP1 is hit, the SL is moved to protect the trade. Calculate sl_after_tp1:
- Find the most recent structural support (long) or resistance (short) between W1/W2 and TP1.
- If it exists and is in profit territory (above W1 for long, below W1 for short): use it.
- Otherwise: use W1 (break-even).
- sl_after_tp1 must always be at or above W1 (long) or at or below W1 (short).

==================================================
WHEN TO RETURN NO SETUP
==================================================
Return setup_found=false if: market in messy consolidation, price between levels, H1/M15 materially conflicting, entry already late, RR to TP2 below 1.6, no clear invalidation level, TP1 less than 0.5 USD from W1, no clean Forteca setup among the 4 families.

==================================================
TECHNICAL NORMALIZATION RULES
==================================================
- Swing high: high is higher than at least 2 candles before and 2 after.
- Swing low: low is lower than at least 2 candles before and 2 after.
- Meaningful breakout: price clearly exceeds a visible prior level and is not immediately fully reversed.
- Strong level: H1 swing level, OR 2+ visible reactions, OR recent breakout/retest level, OR aligns on H1 and M15.
- Late entry filter: reject if price is already too extended toward TP1/TP2 and remaining RR is unattractive.
- Consolidation filter: if recent M15 candles overlap heavily and alternate direction without clean reaction, treat as consolidation and reject unless very clear false breakout/reclaim exists.

==================================================
OUTPUT FORMAT
==================================================
Return exactly one JSON object. No markdown. No extra commentary. No alternative scenarios.

Write "reasoning" and "invalidation" values in Polish.

If setup found:
{"setup_found":true,"setup_type":"setup name","direction":"long","score":12,"pillars":{"trend":3,"structure":2,"level":3,"momentum":2,"rr":2},"entries":[88.95,88.70],"sl":88.10,"sl_after_tp1":88.95,"tp1":89.80,"tp2":90.60,"rr":2.3,"reasoning":"krótkie uzasadnienie po polsku z konkretnymi poziomami z danych","invalidation":"warunek unieważnienia setupu po polsku"}

If no setup:
{"setup_found":false,"reasoning":"konkretny powód braku setupu po polsku"}"""


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
            max_tokens=1500,
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


# ── GPT API ───────────────────────────────────────────────────────────────────
def call_gpt(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt] Brak klucza API.")
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

        client   = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=2048,
            messages=[
                {"role": "system", "content": FORTECA_GPT_PROMPT},
                {"role": "user",   "content": user_msg}
            ]
        )
        text  = response.choices[0].message.content.strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"[gpt] Blad: {e}")
    return None


# ── Google Sheets ─────────────────────────────────────────────────────────────
ALERTY_HEADER = [
    "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
    "W1", "W2", "SL", "SL@TP1", "TP1", "TP2", "RR", "Reasoning",
]
WYNIKI_HEADER = [
    "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
    "W1", "W2", "SL", "TP1", "TP2", "RR",
    "Entries_hit", "Śr.Entry", "Śr.Exit", "Wejście o", "Wyjście o", "Wynik", "PnL $",
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
        ("Wyniki", WYNIKI_HEADER, 1000),
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
        else:
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
        sh1.append_row([
            now,
            model,
            rejection or "OK",
            setup.get("type", setup.get("setup_type", "-")),
            setup.get("direction", "-"),
            setup.get("total", setup.get("score", 0)),
            entries[0] if len(entries) > 0 else "-",
            entries[1] if len(entries) > 1 else "-",
            setup.get("sl", "-"),
            setup.get("sl_after_tp1", "-"),
            tps[0] if tps else setup.get("tp1", "-"),
            tps[1] if len(tps) > 1 else setup.get("tp2", "-"),
            setup.get("rr", "-"),
            setup.get("reasoning", "-"),
        ])
        print(f"[sheets] Alerty: {model} {setup.get('direction')} [{setup.get('total', setup.get('score'))}]")
    except Exception as e:
        print(f"[sheets] Blad Alerty: {e}")


def log_to_wyniki(s: dict, result: str, entry_ts, exit_ts,
                  eff_entry, eff_exit, move: float) -> bool:
    """Zapisuje wynik rozwiązanego setupu do Sheet 2. Zwraca True jeśli sukces."""
    try:
        _, sh2   = _get_sheets()
        alert_dt = datetime.fromisoformat(s["alert_time"]).strftime("%Y-%m-%d %H:%M")
        entry_dt = datetime.utcfromtimestamp(entry_ts).astimezone(TZ).strftime("%H:%M") if entry_ts else "-"
        exit_dt  = datetime.utcfromtimestamp(exit_ts).astimezone(TZ).strftime("%H:%M")  if exit_ts  else "-"
        entries  = s.get("entries", [])
        tps      = s.get("tps", [])
        n_w      = s.get("entries_hit", 1)
        sh2.append_row([
            alert_dt,
            s.get("model", "-"),
            s.get("rejection", "OK"),
            s.get("type", s.get("setup_type", "-")),
            s.get("direction", "-"),
            s.get("score", s.get("total", 0)),
            entries[0] if entries else "-",
            entries[1] if len(entries) > 1 else "-",
            s.get("sl", "-"),
            tps[0] if tps else "-",
            tps[1] if len(tps) > 1 else "-",
            s.get("rr", "-"),
            "+".join(f"W{i+1}" for i in range(n_w)) if n_w > 0 else "-",
            round(eff_entry, 2) if eff_entry is not None else "-",
            round(eff_exit,  2) if eff_exit  is not None else "-",
            entry_dt, exit_dt, result, round(move, 2),
        ])
        print(f"[sheets] Wyniki: {s.get('model')} {s.get('direction')} -> {result} ${move:.2f} [{entry_dt}-{exit_dt}]")
        return True
    except Exception as e:
        print(f"[sheets] Blad Wyniki: {e}")
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
def save_pending(setup: dict, model: str, rejection: str, current_price: float):
    pending = []
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            pending = json.load(f)

    entries = setup.get("entries", [])
    tps     = setup.get("tps", [setup.get("tp1"), setup.get("tp2")])
    tps     = [t for t in tps if t is not None]
    new_level = entries[0] if entries else current_price
    direction = setup.get("direction", "-")

    # Nie dodawaj duplikatu — ten sam model/kierunek/poziom już w pending
    for p in pending:
        if (p["model"] == model
                and p["direction"] == direction
                and abs((p["entries"][0] if p["entries"] else 0) - new_level) < 0.5
                and p["entry_hit_at"] is None):
            print(f"[pending] Duplikat pominiêty: {model} {direction} ~${new_level:.2f}")
            return

    pending.append({
        "alert_time":      datetime.now(timezone.utc).isoformat(),
        "alert_timestamp": int(datetime.now(timezone.utc).timestamp()),
        "model":           model,
        "rejection":       rejection or "OK",
        "type":            setup.get("type", setup.get("setup_type", "-")),
        "direction":       setup.get("direction", "-"),
        "score":           setup.get("total", setup.get("score", 0)),
        "price_at_alert":  round(current_price, 2),
        "entries":         entries,
        "sl":              setup.get("sl"),
        "sl_after_tp1":    setup.get("sl_after_tp1"),
        "tps":             tps,
        "rr":              setup.get("rr", 0),
        "entry_hit_at":    None,
        "tp1_hit_at":      None,
        "sl_adjusted":     False,
        "entries_hit":     1,
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
                    print(f"[pending] {s['model']} {d}: nie weszlo")
                    if log_to_wyniki(s, "nie weszlo", None, None, None, None, 0):
                        try:
                            send_telegram(
                                f"⏳ <b>Nie weszło</b> [{s['model']}]\n"
                                f"Setup {s['type']} {d.upper()} wygasł bez entry\n"
                                f"W1: ${w1:.2f} | SL: ${sl:.2f}"
                            )
                        except Exception:
                            pass
                    else:
                        still_pending.append(s)
                else:
                    still_pending.append(s)
                continue
            s["entry_hit_at"] = hit
            try:
                send_telegram(
                    f"✅ <b>ENTRY HIT</b> [{s['model']}]\n"
                    f"Setup {s['type']} {d.upper()} aktywowany!\n"
                    f"W1: ${w1:.2f} | SL: ${sl:.2f} | "
                    f"TP1: ${tp1:.2f}" + (f" | TP2: ${tp2:.2f}" if tp2 else "")
                )
            except Exception:
                pass

        after_entry   = [c for c in candles_m15 if c["time"] > s["entry_hit_at"]]
        result, move  = None, 0.0
        exit_ts       = None
        tp1_hit_at    = s.get("tp1_hit_at")   # może być ustawione z poprzedniego cyklu
        sl_after_tp1  = s.get("sl_after_tp1")
        # Jeśli SL był już przesunięty w poprzednim cyklu, używamy sl_after_tp1 od razu
        effective_sl  = sl_after_tp1 if s.get("sl_adjusted") and sl_after_tp1 is not None else sl

        for c in after_entry:
            sl_hit  = _hits(c, effective_sl, d, "sl")
            tp2_hit = tp2 and _hits(c, tp2, d, "tp")
            tp1_now = tp1 and _hits(c, tp1, d, "tp")

            if tp2_hit:
                result, exit_ts = "TP2", c["time"]; break

            # TP1 i SL na tej samej świecy — nie znamy kolejności, bezpieczniej SL
            if tp1_now and sl_hit and tp1_hit_at is None:
                result, exit_ts = "SL", c["time"]; break

            # TP1 trafiony po raz pierwszy — zapisz, wyślij powiadomienie, przestaw SL
            if tp1_now and tp1_hit_at is None:
                tp1_hit_at = c["time"]
                s["tp1_hit_at"] = tp1_hit_at
                if sl_after_tp1 is not None and not s.get("sl_adjusted"):
                    effective_sl   = sl_after_tp1
                    s["sl_adjusted"] = True
                    try:
                        be_label = "BE" if abs(sl_after_tp1 - w1) < 0.05 else f"+${abs(sl_after_tp1 - w1):.2f}"
                        send_telegram(
                            f"📌 <b>TP1 HIT — SL przesunięty</b> [{s['model']}]\n"
                            f"Setup {s['type']} {d.upper()}\n"
                            f"TP1: ${tp1:.2f} osiągnięty ✅\n"
                            f"Nowy SL: ${sl_after_tp1:.2f} ({be_label})\n"
                            + (f"Cel: TP2 ${tp2:.2f}" if tp2 else "")
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
            elif result == "TP2":
                exit_prices = [tp1, tp2] if tp1 else [tp2]
            else:  # TP1+BE lub TP1+SL
                exit_prices = [tp1, eff_sl_exit] if tp1 else [eff_sl_exit]
            eff_exit = sum(exit_prices) / len(exit_prices)

            # Signed PnL (pozytywny = zysk)
            move = round((eff_exit - eff_entry) if d == "long" else (eff_entry - eff_exit), 2)

        if result:
            sign = "+" if move >= 0 else ""
            print(f"[pending] {s['model']} {d}: {result} {sign}${move:.2f}")
            if log_to_wyniki(s, result, s["entry_hit_at"], exit_ts, eff_entry, eff_exit, move):
                icon = "💰" if move > 0 else ("⚖️" if move == 0 else "🔴")
                try:
                    send_telegram(
                        f"{icon} <b>{result}</b> [{s['model']}]\n"
                        f"Setup {s['type']} {d.upper()} zamknięty\n"
                        f"Śr. entry: ${eff_entry:.2f} | PnL: {sign}${move:.2f}"
                    )
                except Exception:
                    pass
            else:
                print(f"[pending] Blad zapisu Wyniki — setup zostaje w pending, retry za 15 min")
                still_pending.append(s)
        elif age_h > TRADE_TIMEOUT_H:
            log_to_wyniki(s, "nieokreslone", s["entry_hit_at"], None, None, None, 0)
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

    sl_after_tp1     = setup.get("sl_after_tp1")
    sl_after_tp1_txt = ""
    if sl_after_tp1 is not None and entries:
        be_label = "BE" if abs(sl_after_tp1 - entries[0]) < 0.05 else f"+${abs(sl_after_tp1 - entries[0]):.2f}"
        sl_after_tp1_txt = f"<b>SL po TP1:</b>  ${sl_after_tp1:.2f}  ({be_label})\n"

    return (
        f"🎯 <b>SOL/USDT [{score}/15] — {model}</b>\n"
        f"{icon}  |  {datetime.now(TZ).strftime('%d.%m  %H:%M')}  |  {filtr}\n\n"
        f"Cena teraz: <b>${current_price:.2f}</b>  (~${dist:.2f} do wejscia)\n\n"
        f"<b>Ustaw zlecenia:</b>\n{entries_txt}\n\n"
        f"<b>SL:</b>  ${sl:.2f}\n"
        + sl_after_tp1_txt
        + f"\n<b>Cele:</b>\n{tps_txt}\n\n"
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
        if not validate_setup(best_algo, "Algorytm"):
            pass
        elif not was_alerted("Algorytm", best_algo["level"], best_algo["direction"]):
            rejection = _rejection_reason(best_algo)
            log_to_alerty("Algorytm", rejection, best_algo)
            save_pending(best_algo, "Algorytm", rejection, current)
            save_alerted("Algorytm", best_algo["level"], best_algo["direction"])
            if best_algo["total"] >= MIN_SCORE:
                send_telegram(format_alert("Algorytm", best_algo, current, filter_passed))
        else:
            print(f"[algo] Duplikat w cooldown, pomijam.")
    else:
        print("[algo] Brak setupu.")

    # ── 2. Claude ─────────────────────────────────────────────────────────────
    print("[claude] Wysylam dane do analizy...")
    claude_result = call_claude(candles_m15, candles_h1, current)

    if claude_result:
        if claude_result.get("setup_found"):
            score     = claude_result.get("score", 0)
            direction = claude_result.get("direction", "-")
            entries   = claude_result.get("entries", [current])
            level     = entries[0] if entries else current
            print(f"[claude] Setup: {claude_result.get('setup_type')} {direction} [{score}/15]")
            if not validate_setup(claude_result, "Claude"):
                pass
            elif not was_alerted("Claude", level, direction):
                rejection = _rejection_reason(claude_result)
                log_to_alerty("Claude", rejection, claude_result)
                save_pending(claude_result, "Claude", rejection, current)
                save_alerted("Claude", level, direction)
                if score >= MIN_SCORE:
                    send_telegram(format_alert("Claude", claude_result, current, filter_passed))
            else:
                print(f"[claude] Duplikat w cooldown, pomijam.")
        else:
            reasoning = claude_result.get('reasoning', '')
            print(f"[claude] Brak setupu: {reasoning}")
            log_to_alerty("Claude", "brak_setupu", {"reasoning": reasoning})
    else:
        print("[claude] Brak odpowiedzi.")
        log_to_alerty("Claude", "brak_odpowiedzi", {"reasoning": "API nie zwróciło odpowiedzi"})

    # ── 3. GPT ────────────────────────────────────────────────────────────────
    print("[gpt] Wysylam dane do analizy...")
    gpt_result = call_gpt(candles_m15, candles_h1, current)

    if gpt_result:
        if gpt_result.get("setup_found"):
            score     = gpt_result.get("score", 0)
            direction = gpt_result.get("direction", "-")
            entries   = gpt_result.get("entries", [current])
            level     = entries[0] if entries else current
            print(f"[gpt] Setup: {gpt_result.get('setup_type')} {direction} [{score}/15]")
            if not validate_setup(gpt_result, "GPT"):
                pass
            elif not was_alerted("GPT", level, direction):
                rejection = _rejection_reason(gpt_result)
                log_to_alerty("GPT", rejection, gpt_result)
                save_pending(gpt_result, "GPT", rejection, current)
                save_alerted("GPT", level, direction)
                if score >= MIN_SCORE:
                    send_telegram(format_alert("GPT", gpt_result, current, filter_passed))
            else:
                print(f"[gpt] Duplikat w cooldown, pomijam.")
        else:
            reasoning = gpt_result.get('reasoning', '')
            print(f"[gpt] Brak setupu: {reasoning}")
            log_to_alerty("GPT", "brak_setupu", {"reasoning": reasoning})
    else:
        print("[gpt] Brak odpowiedzi.")
        log_to_alerty("GPT", "brak_odpowiedzi", {"reasoning": "API nie zwróciło odpowiedzi"})


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
