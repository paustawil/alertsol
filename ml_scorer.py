"""
ml_scorer.py — Scoring setupów modelem LightGBM w runtime.

Ładuje wytrenowany model lazily przy pierwszym wywołaniu.
Jeśli model nie istnieje — score_setup() zwraca None (graceful degradation).
"""

import json
import os
from pathlib import Path

import numpy as np

_model = None
_meta = None
_loaded = False

MODEL_PATH = os.getenv("ML_MODEL_PATH", "model/setup_scorer.lgb")
META_PATH = MODEL_PATH.replace(".lgb", "_meta.json")


def _load():
    global _model, _meta, _loaded
    _loaded = True

    if not Path(MODEL_PATH).exists():
        print(f"[ml_scorer] Model not found at {MODEL_PATH} — scoring disabled")
        return

    try:
        import lightgbm as lgb
        _model = lgb.Booster(model_file=MODEL_PATH)
        with open(META_PATH) as f:
            _meta = json.load(f)
        print(f"[ml_scorer] Model loaded: {_meta.get('n_samples', '?')} samples, "
              f"features: {len(_meta.get('feature_cols', []))}")
    except Exception as e:
        print(f"[ml_scorer] Failed to load model: {e}")
        _model = None
        _meta = None


def _extract_features(setup: dict) -> dict | None:
    """Wyciąga features z setup dict (kompatybilny z ml_training.build_features)."""
    if _meta is None:
        return None

    from datetime import datetime, timezone

    entries = setup.get("entries", [])
    tps = setup.get("tps", [])
    entry_price = float(entries[0]) if entries else None
    sl = setup.get("sl")
    sl_after_tp1 = setup.get("sl_after_tp1")
    tp1 = float(tps[0]) if tps else None

    if entry_price is None or sl is None:
        return None

    type_map = _meta.get("type_map", {})
    variant_map = _meta.get("variant_map", {})
    direction_map = _meta.get("direction_map", {"long": 0, "short": 1})
    trigger_map = _meta.get("trigger_map", {"falling": 0, "rising": 1})

    now = datetime.now(timezone.utc)
    features = {
        "rr": setup.get("rr", 0),
        "score": setup.get("score", 0),
        "type_enc": type_map.get(setup.get("type", ""), -1),
        "direction_enc": direction_map.get(setup.get("direction", ""), -1),
        "variant_enc": variant_map.get(setup.get("variant", ""), -1),
        "trigger_enc": trigger_map.get(setup.get("entry_trigger", ""), -1),
        "hour": now.hour,
        "day_of_week": now.weekday(),
        "sl_distance": abs(entry_price - float(sl)) if sl else 0,
        "tp1_distance": abs(float(tp1) - entry_price) if tp1 else 0,
        "sl_after_tp1_dist": abs(float(sl_after_tp1) - entry_price) if sl_after_tp1 else 0,
    }

    # Extended features from market_context
    mc = setup.get("market_context", {}) or {}
    if _meta.get("has_market_context") and mc:
        mc_numeric = [
            "atr_h1", "atr_m15", "vol_ratio", "regime_score", "swing_range",
            "change_1h", "change_2h", "change_4h", "change_8h", "change_12h",
            "change_24h", "change_48h",
            "ma20_h1_dist_pct", "ma30_m15_dist_pct", "ma60_m15_dist_pct",
            "exhaustion_count", "bearish_count_6m15", "bullish_count_6m15",
            "spike_reversal_score", "entry_dist_pct", "sl_dist_pct",
            "s_touches", "r_touches",
        ]
        for col in mc_numeric:
            features[f"mc_{col}"] = mc.get(col)

        atr_h1 = mc.get("atr_h1", 0)
        atr_m15 = mc.get("atr_m15", 0)
        features["mc_atr_ratio"] = atr_h1 / atr_m15 if atr_m15 else None
        features["mc_entry_atr_mult"] = features["sl_distance"] / atr_h1 if atr_h1 else None

    return features


def score_setup(setup: dict) -> float | None:
    """Zwraca prawdopodobieństwo sukcesu (0.0-1.0) lub None jeśli model niedostępny."""
    if not _loaded:
        _load()
    if _model is None or _meta is None:
        return None

    features = _extract_features(setup)
    if features is None:
        return None

    feature_cols = _meta["feature_cols"]
    row = [features.get(col) for col in feature_cols]

    try:
        prob = _model.predict([row])[0]
        return round(float(prob), 4)
    except Exception as e:
        print(f"[ml_scorer] Prediction error: {e}")
        return None


def composite_score(ml_prob: float | None, rr: float) -> float | None:
    """Łączy ML probability z R:R w composite score."""
    if ml_prob is None:
        return None
    rr_norm = min(rr / 4.0, 1.0)
    return round(ml_prob * 0.7 + rr_norm * 0.3, 4)


def get_model_info() -> dict | None:
    """Zwraca metadane modelu (do dashboard API)."""
    if not _loaded:
        _load()
    return _meta
