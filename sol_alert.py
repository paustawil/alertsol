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
ENTRY_TIMEOUT_H  = 4
TRADE_TIMEOUT_H  = 24
MIN_SL_DISTANCE  = 0.30   # minimalna odleglosc W1-SL w USD; ponizej = odrzucony setup
MIN_GROK_BIAS_PROC = 65   # minimalny bias_proc Groka; ponizej = sygnał odrzucony jako zbyt niepewny
ENABLE_CLAUDE        = False  # wyłączony tymczasowo — kod zachowany
ENABLE_GPT           = False  # wyłączony tymczasowo — kod zachowany
ENABLE_GPT_RELAXED   = True   # GPT z luźnym promptem (wzorowanym na Groku)


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


# ── System prompt dla Grok ────────────────────────────────────────────────────
GROK_PROMPT = """Jesteś doświadczonym traderem kryptowalut, specjalizującym się w SOL/USDT na interwałach M15 i H1.

Masz dostęp do internetu — użyj go, żeby pobrać:
- Aktualne ceny BTC, ETH, SOL (USD)
- Aktualny Fear & Greed Index (wartość 0–100 + etykieta)

Otrzymasz też dane OHLCV: M15 (ostatnie 60 świec) i H1 (ostatnie 24 świece) dla SOL.

Twoje zadanie:
1. Krótko oceń sentyment: BTC/ETH/SOL (24h zmiana, relatywna siła SOL), Fear & Greed.
2. Przeanalizuj strukturę techniczną H1 i M15: kluczowe supporty i resistancey, trend, formacje, RSI, MACD, volume. Bez lania wody — tylko to co istotne.
3. Podaj bias (long / short / neutral) z prawdopodobieństwem w %.
4. Jeśli bias nie jest neutral — zaproponuj 1–2 konkretne poziomy wejścia z warunkiem aktywacji.
5. Podaj TP1 (bezpieczny, bliższy) i TP2 (ambitny, ale realistyczny).
6. Podaj ciasny SL i przybliżone R:R (minimum 1:2).
7. Na końcu: co teraz robisz (np. "Czekam na pullback do X i wchodzę long").

Zasady:
- Analiza techniczna ma priorytet (70–80%). Sentyment i kontekst makro — 20–30%.
- Odpowiadaj zawsze po polsku, konkretnie, bez powtarzania ostrzeżeń o ryzyku.
- Ustaw send_alert=true TYLKO gdy spełnione są WSZYSTKIE poniższe warunki:
  a) H1 i M15 wskazują ten sam kierunek (tf_aligned=true) — jeśli timeframy są sprzeczne, send_alert=false.
  b) bias_proc >= 65 — jeśli przekonanie jest niższe, oznacza to zawahanie rynku, ustaw send_alert=false.
  c) Widzisz wyraźny, konkretny setup z jasnym entry, SL i TP.
- Przy bocznym rynku, choppingu, sprzecznych sygnałach H1/M15 lub niskim przekonaniu — send_alert=false.
- tf_aligned: Oceń czy H1 i M15 pokazują ten sam kierunek. true = zgodne, false = sprzeczne lub jeden neutralny.
- sl_after_tp1: Po osiągnięciu TP1 SL należy przesunąć. Znajdź ostatni strukturalny support (long) lub resistance (short) między W1 a TP1. Jeśli taki poziom istnieje i jest w strefie zysku (powyżej W1 dla long, poniżej W1 dla short) — użyj go jako sl_after_tp1. Jeśli nie — użyj W1 (break-even). Zawsze podaj tę wartość gdy send_alert=true.

Zwróć dokładnie jeden obiekt JSON. Bez markdownu, bez tekstu poza JSON.

Gdy send_alert=true:
{"send_alert":true,"bias":"long","bias_proc":70,"tf_aligned":true,"sentyment":"krótka ocena BTC/ETH/SOL + F&G z aktualnymi wartościami","analiza":"konkretna analiza techniczna H1/M15","wejscia":[{"poziom":124.50,"warunek":"zamknięcie M15 powyżej 124.80"}],"tp1":127.00,"tp2":129.50,"sl":122.80,"sl_after_tp1":123.00,"rr":2.1,"akcja":"Czekam na pullback do 124.50 i wchodzę long"}

Gdy send_alert=false:
{"send_alert":false,"bias":"neutral","bias_proc":50,"tf_aligned":false,"sentyment":"krótka ocena BTC/ETH/SOL + F&G z aktualnymi wartościami","analiza":"co widzisz na wykresie i dlaczego brak setupu","akcja":"Obserwuję, czekam na wyklarowanie sytuacji"}"""


# ── System prompt dla Grok — walidacja oczekujących setupów ──────────────────
GROK_VALIDATION_PROMPT = """Jesteś doświadczonym traderem kryptowalut weryfikującym aktywne zlecenia oczekujące na SOL/USDT.

Masz dostęp do internetu — użyj go, żeby pobrać aktualne ceny BTC, ETH, SOL i Fear & Greed Index.

Otrzymasz aktualne dane OHLCV SOL (M15 i H1) oraz listę setupów oczekujących na wejście.

Twoje zadanie:
1. Pobierz live: ceny BTC/ETH/SOL i Fear & Greed Index.
2. Oceń aktualną sytuację techniczną H1 i M15.
3. Dla każdego setupu zdecyduj: keep=true (zachowaj) lub keep=false (anuluj).

Anuluj setup TYLKO jeśli zachodzi co najmniej jeden z poniższych warunków:
- Rynek uciekł zbyt daleko i poziom wejścia jest technicznie nieosiągalny w rozsądnym czasie.
- Trend wyraźnie się odwrócił i setup działa teraz bezpośrednio przeciwko dominującej strukturze.
- Kluczowy poziom struktury definiujący setup (support/resistance) został złamany i nie jest już ważny.

Zachowaj setup jeśli:
- Poziom wejścia jest nadal w zasięgu i ma techniczne uzasadnienie.
- Nie ma wyraźnego powodu do anulowania — wątpliwość działa na korzyść zachowania.

Zasady:
- Powód anulowania: konkretny, zwięzły, po polsku (1–2 zdania).
- Odpowiadaj zawsze po polsku.
- Zwróć dokładnie jeden obiekt JSON. Bez markdownu, bez tekstu poza JSON.

Format:
{"decyzje":[{"setup_id":1,"keep":false,"powod":"Rynek wybił trwale powyżej 88.0 — poziom wejścia short 86.50 przestał być strukturalnie istotny"},{"setup_id":2,"keep":true}]}"""


# ── System prompt dla GPT Relaxed (wzorowany na Groku) ───────────────────────
GPT_RELAXED_PROMPT = """Jesteś doświadczonym traderem kryptowalut, specjalizującym się w SOL/USDT na interwałach M15 i H1.

Masz dostęp do internetu — użyj go, żeby pobrać:
- Aktualne ceny BTC, ETH, SOL (USD)
- Aktualny Fear & Greed Index (wartość 0–100 + etykieta)

Otrzymasz też dane OHLCV: M15 (ostatnie 60 świec) i H1 (ostatnie 24 świece) dla SOL.

Twoje zadanie:
1. Krótko oceń sentyment: BTC/ETH/SOL (24h zmiana, relatywna siła SOL), Fear & Greed.
2. Przeanalizuj strukturę techniczną H1 i M15: kluczowe supporty i resistancey, trend, formacje, RSI, MACD, volume. Bez lania wody — tylko to co istotne.
3. Podaj bias (long / short / neutral) z prawdopodobieństwem w %.
4. Jeśli bias nie jest neutral — zaproponuj 1–2 konkretne poziomy wejścia z warunkiem aktywacji.
5. Podaj TP1 (bezpieczny, bliższy) i TP2 (ambitny, ale realistyczny).
6. Podaj ciasny SL i przybliżone R:R (minimum 1:2).
7. Na końcu: co teraz robisz (np. "Czekam na pullback do X i wchodzę long").

Zasady:
- Analiza techniczna ma priorytet (70–80%). Sentyment i kontekst makro — 20–30%.
- Odpowiadaj zawsze po polsku, konkretnie, bez powtarzania ostrzeżeń o ryzyku.
- Ustaw send_alert=true TYLKO gdy spełnione są WSZYSTKIE poniższe warunki:
  a) H1 i M15 wskazują ten sam kierunek (tf_aligned=true) — jeśli timeframy są sprzeczne, send_alert=false.
  b) bias_proc >= 65 — jeśli przekonanie jest niższe, oznacza to zawahanie rynku, ustaw send_alert=false.
  c) Widzisz wyraźny, konkretny setup z jasnym entry, SL i TP.
- Przy bocznym rynku, choppingu, sprzecznych sygnałach H1/M15 lub niskim przekonaniu — send_alert=false.
- tf_aligned: Oceń czy H1 i M15 pokazują ten sam kierunek. true = zgodne, false = sprzeczne lub jeden neutralny.
- sl_after_tp1: Po osiągnięciu TP1 SL należy przesunąć. Znajdź ostatni strukturalny support (long) lub resistance (short) między W1 a TP1. Jeśli taki poziom istnieje i jest w strefie zysku (powyżej W1 dla long, poniżej W1 dla short) — użyj go jako sl_after_tp1. Jeśli nie — użyj W1 (break-even). Zawsze podaj tę wartość gdy send_alert=true.

Zwróć dokładnie jeden obiekt JSON. Bez markdownu, bez tekstu poza JSON.

Gdy send_alert=true:
{"send_alert":true,"bias":"long","bias_proc":70,"tf_aligned":true,"sentyment":"krótka ocena BTC/ETH/SOL + F&G z aktualnymi wartościami","analiza":"konkretna analiza techniczna H1/M15","wejscia":[{"poziom":124.50,"warunek":"zamknięcie M15 powyżej 124.80"}],"tp1":127.00,"tp2":129.50,"sl":122.80,"sl_after_tp1":123.00,"rr":2.1,"akcja":"Czekam na pullback do 124.50 i wchodzę long"}

Gdy send_alert=false:
{"send_alert":false,"bias":"neutral","bias_proc":50,"tf_aligned":false,"sentyment":"krótka ocena BTC/ETH/SOL + F&G z aktualnymi wartościami","analiza":"co widzisz na wykresie i dlaczego brak setupu","akcja":"Obserwuję, czekam na wyklarowanie sytuacji"}"""


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
    r = requests.get(
        "https://api.bitget.com/api/v2/mix/market/candles",
        params={
            "symbol":      bg_symbol,
            "productType": "USDT-FUTURES",
            "granularity": granularity,
            "limit":       str(limit),
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    # Bitget zwraca [ts_ms, open, high, low, close, baseVol, quoteVol], newest first
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
    candles.reverse()  # oldest first (jak CryptoCompare)
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


# ── GPT Relaxed API (OpenAI — z web search, luźny prompt wzorowany na Groku) ──
_GPT_RELAXED_TIMEOUT_S = 120


def call_gpt_relaxed(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not OPENAI_KEY:
        print("[gpt-r] Brak klucza API.")
        return None

    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-60:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-24:]
    )
    user_msg = (
        f"Aktualna cena SOL z moich danych: ${current_price:.2f}\n\n"
        f"SOL M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
        f"SOL H1 (ostatnie 24 swiece):\n{h1_csv}"
    )

    def _call() -> str:
        client = openai.OpenAI(api_key=OPENAI_KEY)
        response = client.responses.create(
            model="gpt-4o",
            tools=[{"type": "web_search_preview"}],
            instructions=GPT_RELAXED_PROMPT,
            input=user_msg,
            max_output_tokens=2048,
        )
        return response.output_text.strip()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                text = future.result(timeout=_GPT_RELAXED_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"[gpt-r] Timeout — brak odpowiedzi w ciagu {_GPT_RELAXED_TIMEOUT_S}s")
                future.cancel()
                return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"[gpt-r] Blad: {e}")
    return None


# ── Grok API (xAI — OpenAI-compatible + live search) ─────────────────────────
_GROK_CREDIT_KEYWORDS = ("credit", "quota", "billing", "payment", "insufficient", "balance",
                          "exceeded", "limit", "kredyt", "płatność", "rozliczenie")
_GROK_TIMEOUT_S = 120  # 2 minuty


def call_grok(candles_m15: list[dict], candles_h1: list[dict], current_price: float) -> dict | None:
    if not XAI_KEY:
        print("[grok] Brak klucza API.")
        return None

    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-60:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-24:]
    )
    user_msg = (
        f"Aktualna cena SOL z moich danych: ${current_price:.2f}\n\n"
        f"SOL M15 (ostatnie 60 swiec):\n{m15_csv}\n\n"
        f"SOL H1 (ostatnie 24 swiece):\n{h1_csv}"
    )

    def _call() -> str:
        from xai_sdk import Client as XaiClient
        from xai_sdk.chat import system as xai_system, user as xai_user
        from xai_sdk.tools import web_search
        client = XaiClient(api_key=XAI_KEY)
        chat   = client.chat.create(model="grok-4", tools=[web_search()])
        chat.append(xai_system(GROK_PROMPT))
        chat.append(xai_user(user_msg))
        return chat.sample().content.strip()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                text = future.result(timeout=_GROK_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"[grok] Timeout — brak odpowiedzi w ciagu {_GROK_TIMEOUT_S}s")
                future.cancel()
                return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        err_str = str(e).lower()
        print(f"[grok] Blad: {e}")
        if any(kw in err_str for kw in _GROK_CREDIT_KEYWORDS):
            try:
                send_telegram(
                    "⚠️ <b>Grok API — brak kredytów</b>\n"
                    "Konto xAI wyczerpało limit. Sprawdź saldo na console.x.ai"
                )
            except Exception:
                pass
    return None


# ── Google Sheets ─────────────────────────────────────────────────────────────
ALERTY_HEADER = [
    "ID", "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
    "Kurs", "W1", "W2", "Warunek", "SL", "SL@TP1", "TP1", "TP2", "RR", "Reasoning",
]
WYNIKI_HEADER = [
    "ID", "Snapshot", "Model", "Filtr_powód", "Typ", "Kierunek", "Score",
    "Kurs", "W1", "W2", "Warunek", "SL", "TP1", "TP2", "RR",
    "Entries_hit", "Śr.Entry", "Śr.Exit", "Wejście o", "Wyjście o", "Wynik", "PnL $",
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
        ("Wyniki", WYNIKI_HEADER, 1000),
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
        elif name == "Wyniki":
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
        score_val = f"{raw_score}%" if model == "Grok" else raw_score
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
            setup.get("reasoning", "") or "",
        ])
        print(f"[sheets] Alerty: {model} {setup.get('direction')} [{setup.get('total', setup.get('score'))}]")
    except Exception as e:
        print(f"[sheets] Blad Alerty: {e}")


def log_to_wyniki(s: dict, result: str, entry_ts, exit_ts,
                  eff_entry, eff_exit, move: float) -> bool:
    """Zapisuje wynik rozwiązanego setupu do Sheet 2. Zwraca True jeśli sukces."""
    try:
        _, sh2   = _get_sheets()
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
        score_val = f"{raw_score}%" if model == "Grok" else raw_score
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
            entry_dt, exit_dt, result, round(move, 2),
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


def save_pending(setup: dict, model: str, rejection: str, current_price: float):
    entries   = setup.get("entries", [])
    tps       = setup.get("tps", [setup.get("tp1"), setup.get("tp2")])
    tps       = [t for t in tps if t is not None]
    new_level = entries[0] if entries else current_price
    direction = setup.get("direction", "-")

    # Nie dodawaj duplikatu — ten sam model/kierunek/poziom już w pending
    for p in db.get_active_setups():
        if (p["model"] == model
                and p["direction"] == direction
                and abs((p["entries"][0] if p["entries"] else 0) - new_level) < 0.5
                and p["entry_hit_at"] is None):
            print(f"[pending] Duplikat pominiêty: {model} {direction} ~${new_level:.2f}")
            return

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
    }
    sid = db.insert_setup(row)
    setup["setup_id"] = sid  # mutujemy dict żeby format_alert/format_grok_alert miały dostęp


def _hits(candle: dict, price: float, direction: str, side: str, entry_trigger: str = None) -> bool:
    if side == "entry":
        trigger = entry_trigger or ("falling" if direction == "long" else "rising")
        return candle["low"] <= price if trigger == "falling" else candle["high"] >= price
    if side == "sl":
        return candle["low"] <= price if direction == "long" else candle["high"] >= price
    if side == "tp":
        return candle["high"] >= price if direction == "long" else candle["low"] <= price
    return False


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

            # TP1 trafiony po raz pierwszy — zapisz, wyślij powiadomienie, przestaw SL
            if tp1_now and tp1_hit_at is None:
                tp1_hit_at = c["time"]
                s["tp1_hit_at"] = tp1_hit_at
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


# ── Grok — walidacja oczekujących setupów ────────────────────────────────────
def call_grok_validation(pending_non_entered: list[dict], candles_m15: list[dict],
                         candles_h1: list[dict], current_price: float) -> list[dict] | None:
    """Pyta Groka czy nieotwarte setupy są nadal aktualne. Zwraca listę decyzji lub None."""
    if not XAI_KEY or not pending_non_entered:
        return None

    m15_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_m15[-60:]
    )
    h1_csv = "time,open,high,low,close,volume\n" + "\n".join(
        f"{c['time']},{c['open']},{c['high']},{c['low']},{c['close']},{c['volume']}"
        for c in candles_h1[-24:]
    )
    setups_txt = json.dumps([{
        "setup_id":  s.get("setup_id"),
        "direction": s["direction"],
        "w1":        float(s["entries"][0]) if s["entries"] else None,
        "sl":        float(s["sl"]),
        "tp1":       float(s["tps"][0]) if s["tps"] else None,
        "tp2":       float(s["tps"][1]) if len(s["tps"]) > 1 else None,
        "warunek":   s.get("warunek", ""),
        "alert_time": s["alert_time"],
    } for s in pending_non_entered], ensure_ascii=False)
    user_msg = (
        f"Aktualna cena SOL: ${current_price:.2f}\n\n"
        f"Setupy oczekujące na wejście:\n{setups_txt}\n\n"
        f"SOL M15 (ostatnie 60 świec):\n{m15_csv}\n\n"
        f"SOL H1 (ostatnie 24 świece):\n{h1_csv}"
    )

    def _call() -> str:
        from xai_sdk import Client as XaiClient
        from xai_sdk.chat import system as xai_system, user as xai_user
        from xai_sdk.tools import web_search
        client = XaiClient(api_key=XAI_KEY)
        chat   = client.chat.create(model="grok-4", tools=[web_search()])
        chat.append(xai_system(GROK_VALIDATION_PROMPT))
        chat.append(xai_user(user_msg))
        return chat.sample().content.strip()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_call)
            try:
                text = future.result(timeout=_GROK_TIMEOUT_S)
            except concurrent.futures.TimeoutError:
                print(f"[grok-valid] Timeout — brak odpowiedzi w ciągu {_GROK_TIMEOUT_S}s")
                return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group()).get("decyzje", [])
    except Exception as e:
        print(f"[grok-valid] Blad: {e}")
    return None


def check_pending_with_grok(candles_m15: list[dict], candles_h1: list[dict], current_price: float):
    """Pyta Groka o nieotwarte setupy i przenosi anulowane w tryb shadow tracking."""
    pending = db.get_active_setups()

    non_entered = [s for s in pending if s.get("entry_hit_at") is None and not s.get("shadow")]
    if not non_entered:
        print("[grok-valid] Brak nieotwartych setupów do sprawdzenia.")
        return

    print(f"[grok-valid] Sprawdzam {len(non_entered)} nieotwartych setupów z Grokiem...")
    decisions = call_grok_validation(non_entered, candles_m15, candles_h1, current_price)
    if not decisions:
        print("[grok-valid] Brak odpowiedzi od Groka.")
        return

    cancel_map = {d["setup_id"]: d for d in decisions if not d.get("keep", True)}
    if not cancel_map:
        print("[grok-valid] Grok zachowuje wszystkie setupy.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    cancelled = 0
    for s in pending:
        sid = s.get("setup_id")
        if sid in cancel_map and s.get("entry_hit_at") is None and not s.get("shadow"):
            dec = cancel_map[sid]
            s["shadow"]        = True
            s["cancel_reason"] = dec.get("powod", "")
            s["cancel_time"]   = now_iso
            s["cancel_price"]  = round(current_price, 2)
            cancelled += 1
            w1  = s["entries"][0] if s["entries"] else None
            tp1 = s["tps"][0] if s["tps"] else None
            d   = s["direction"]
            di  = "📉" if d == "short" else "📈"
            try:
                send_telegram(
                    f"🚫 <b>Grok anulował setup #{sid}</b>\n"
                    f"{di} {d.upper()}"
                    + (f" | W1: ${w1:.2f}" if w1 else "")
                    + (f" | TP1: ${tp1:.2f}" if tp1 else "") + "\n"
                    f"<i>{dec.get('powod', '')}</i>\n"
                    f"(Shadow tracking aktywny)"
                )
            except Exception:
                pass

    for s in pending:
        if s.get("shadow") and s.get("setup_id") in cancel_map:
            sid = s["setup_id"]
            db.update_setup(sid,
                            shadow=True,
                            cancel_reason=s.get("cancel_reason", ""),
                            cancel_time=s.get("cancel_time"),
                            cancel_price=s.get("cancel_price"))
            # Natychmiastowe zamknięcie — exchange_trader anuluje plan order
            # przez get_resolved_with_open_orders() przy następnym sync (co 15s)
            db.resolve_setup(sid, "anulowany", None, None, None, None)
    print(f"[grok-valid] Anulowano {cancelled} setupów.")


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
    filtr   = "✅ filtr" if filter_passed else "⚠️ bez filtra"
    entries_txt = "\n".join(f"  W{i+1}: ${e:.2f}" for i, e in enumerate(entries))
    tps_txt     = "\n".join(f"  TP{i+1}: ${t:.2f}  (+${abs(t - entries[0]):.2f})" for i, t in enumerate(tps)) if entries else "-"
    reasoning   = setup.get("reasoning", "")

    sl_after_tp1     = setup.get("sl_after_tp1")
    sl_after_tp1_txt = ""
    if sl_after_tp1 is not None and entries:
        be_label = "BE" if abs(sl_after_tp1 - entries[0]) < 0.05 else f"+${abs(sl_after_tp1 - entries[0]):.2f}"
        sl_after_tp1_txt = f"<b>SL po TP1:</b>  ${sl_after_tp1:.2f}  ({be_label})\n"

    sid_txt = f" #{setup.get('setup_id')}" if setup.get("setup_id") else ""
    return (
        f"🎯 <b>SOL/USDT [{score}/15] — {model}{sid_txt}</b>\n"
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


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] SOL Alert v2 — start")

    _migrate_setup_ids()

    candles_m15 = fetch_klines(SYMBOL, "15m", limit=100)
    candles_h1  = fetch_klines(SYMBOL, "1h",  limit=50)
    current     = fetch_current_price(SYMBOL) or candles_m15[-1]["close"]
    rng         = detect_range(candles_m15)
    trend       = h1_trend(candles_h1)

    print(f"SOL: ${current:.2f} | Zakres: ${rng['support']}-${rng['resistance']} (${rng['range_size']:.2f}) | H1: {trend}")

    # Sprawdz oczekujace setupy
    check_pending(candles_m15)

    # Exchange sync wyłączony — Bitget testowany osobnym workflow
    # exchange_trader.sync()

    # O :45 każdej godziny — Grok weryfikuje nieotwarte setupy
    if datetime.now(TZ).minute == 45:
        check_pending_with_grok(candles_m15, candles_h1, current)

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
            save_pending(best_algo, "Algorytm", rejection, current)
            log_to_alerty("Algorytm", rejection, best_algo)
            save_alerted("Algorytm", best_algo["level"], best_algo["direction"])
            if best_algo["total"] >= MIN_SCORE:
                send_telegram(format_alert("Algorytm", best_algo, current, filter_passed))
        else:
            print(f"[algo] Duplikat w cooldown, pomijam.")
    else:
        print("[algo] Brak setupu.")

    # ── 2. Claude (wyłączony — ENABLE_CLAUDE = False) ─────────────────────────
    if ENABLE_CLAUDE:
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
                    save_pending(claude_result, "Claude", rejection, current)
                    log_to_alerty("Claude", rejection, claude_result)
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
    else:
        print("[claude] Pominiêty (ENABLE_CLAUDE=False).")

    # ── 3. GPT (wyłączony — ENABLE_GPT = False) ───────────────────────────────
    if ENABLE_GPT:
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
                    save_pending(gpt_result, "GPT", rejection, current)
                    log_to_alerty("GPT", rejection, gpt_result)
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
    else:
        print("[gpt] Pominięty (ENABLE_GPT=False).")

    # ── 4. Grok (live search — sam pobiera BTC/ETH/F&G) ───────────────────────
    print("[grok] Wysylam dane do analizy (live search wlaczony)...")
    grok_result = call_grok(candles_m15, candles_h1, current)

    if grok_result:
        bias       = grok_result.get("bias", "neutral")
        bias_proc  = grok_result.get("bias_proc", 0)
        send_alert = grok_result.get("send_alert", False)
        tf_aligned = grok_result.get("tf_aligned", True)
        print(f"[grok] Bias: {bias} ({bias_proc}%) | tf_aligned={tf_aligned} | send_alert={send_alert}")

        # Filtr zawahania: odrzuć setup jeśli przekonanie za niskie
        if send_alert and bias_proc < MIN_GROK_BIAS_PROC:
            print(f"[grok] Odrzucono: bias_proc={bias_proc}% < próg {MIN_GROK_BIAS_PROC}% — zbyt niepewny sygnał.")
            send_alert = False

        if send_alert and bias != "neutral":
            wejscia = grok_result.get("wejscia", [])
            entries = [w["poziom"] for w in wejscia if "poziom" in w]
            if entries:
                # Ustal warunek wejścia na podstawie tekstu akcji
                akcja_lower = grok_result.get("akcja", "").lower()
                if "pullback" in akcja_lower:
                    warunek = "pullback"
                elif any(kw in akcja_lower for kw in ["break", "breakdown", "przebicie"]):
                    warunek = "przebicie"
                else:
                    w1_lvl = entries[0]
                    if bias == "short":
                        warunek = "przebicie" if w1_lvl < current else "pullback"
                    else:
                        warunek = "przebicie" if w1_lvl > current else "pullback"

                grok_setup = {
                    "type":         "",
                    "direction":    bias,
                    "score":        bias_proc,
                    "kurs":         round(current, 2),
                    "entries":      entries,
                    "warunek":      warunek,
                    "sl":           grok_result.get("sl"),
                    "sl_after_tp1": grok_result.get("sl_after_tp1"),
                    "tps":          [t for t in [grok_result.get("tp1"), grok_result.get("tp2")] if t is not None],
                    "rr":           grok_result.get("rr", 0),
                    "reasoning":    " | ".join(filter(None, [grok_result.get("analiza", ""), grok_result.get("akcja", "")])),
                }
                save_pending(grok_setup, "Grok", "", current)  # ustawia grok_setup["setup_id"]
                log_to_alerty("Grok", "", grok_setup)

            send_telegram(format_grok_alert(grok_result, current, grok_setup.get("setup_id") if entries else None))
        else:
            print(f"[grok] Brak konkretnego setupu — pomijam Telegram i arkusz.")
    else:
        print("[grok] Brak odpowiedzi.")

    # ── 5. GPT Relaxed (live search — sam pobiera BTC/ETH/F&G) ──────────────
    if ENABLE_GPT_RELAXED:
        print("[gpt-r] Wysylam dane do analizy (live search wlaczony)...")
        gpt_r_result = call_gpt_relaxed(candles_m15, candles_h1, current)

        if gpt_r_result:
            bias       = gpt_r_result.get("bias", "neutral")
            bias_proc  = gpt_r_result.get("bias_proc", 0)
            send_alert = gpt_r_result.get("send_alert", False)
            tf_aligned = gpt_r_result.get("tf_aligned", True)
            print(f"[gpt-r] Bias: {bias} ({bias_proc}%) | tf_aligned={tf_aligned} | send_alert={send_alert}")

            if send_alert and bias_proc < MIN_GROK_BIAS_PROC:
                print(f"[gpt-r] Odrzucono: bias_proc={bias_proc}% < prog {MIN_GROK_BIAS_PROC}% — zbyt niepewny sygnal.")
                send_alert = False

            gpt_r_setup = {}
            if send_alert and bias != "neutral":
                wejscia = gpt_r_result.get("wejscia", [])
                entries = [w["poziom"] for w in wejscia if "poziom" in w]
                if entries:
                    akcja_lower = gpt_r_result.get("akcja", "").lower()
                    if "pullback" in akcja_lower:
                        warunek = "pullback"
                    elif any(kw in akcja_lower for kw in ["break", "breakdown", "przebicie"]):
                        warunek = "przebicie"
                    else:
                        w1_lvl = entries[0]
                        if bias == "short":
                            warunek = "przebicie" if w1_lvl < current else "pullback"
                        else:
                            warunek = "przebicie" if w1_lvl > current else "pullback"

                    gpt_r_setup = {
                        "type":         "",
                        "direction":    bias,
                        "score":        bias_proc,
                        "kurs":         round(current, 2),
                        "entries":      entries,
                        "warunek":      warunek,
                        "sl":           gpt_r_result.get("sl"),
                        "sl_after_tp1": gpt_r_result.get("sl_after_tp1"),
                        "tps":          [t for t in [gpt_r_result.get("tp1"), gpt_r_result.get("tp2")] if t is not None],
                        "rr":           gpt_r_result.get("rr", 0),
                        "reasoning":    " | ".join(filter(None, [gpt_r_result.get("analiza", ""), gpt_r_result.get("akcja", "")])),
                    }
                    save_pending(gpt_r_setup, "GPT-R", "", current)
                    log_to_alerty("GPT-R", "", gpt_r_setup)

                send_telegram(format_grok_alert(gpt_r_result, current, gpt_r_setup.get("setup_id") if gpt_r_setup else None, model_name="GPT-R"))
            else:
                print(f"[gpt-r] Brak konkretnego setupu — pomijam Telegram i arkusz.")
        else:
            print("[gpt-r] Brak odpowiedzi.")
    else:
        print("[gpt-r] Pominieto (ENABLE_GPT_RELAXED=False).")

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
