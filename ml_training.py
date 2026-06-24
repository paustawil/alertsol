#!/usr/bin/env python3
"""
ml_training.py — Trening modelu LightGBM do oceny jakości setupów.

Faza 0: trenuje na istniejących kolumnach (type, direction, rr, score, variant, ...).
Faza 2: retrenuje z pełnym market_context (ATR, volume, swing, exhaustion, ...).

Użycie:
  python ml_training.py [--db-url DATABASE_URL] [--out model/setup_scorer.lgb]
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    classification_report, roc_auc_score,
)

_lgb_error = None
try:
    import lightgbm as lgb
except (ImportError, OSError) as e:
    lgb = None
    _lgb_error = str(e)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


WIN_RESULTS = {"TP1", "TP2", "TP1+BE", "TP1+SL", "TP1+TP2"}
LOSS_RESULTS = {"SL"}


def export_training_data(db_url: str) -> pd.DataFrame:
    """Pobiera resolved setupy z bazy jako DataFrame."""
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT setup_id, alert_time, model, type, direction, score,
                       variant, rr, entry_trigger, entries, tps, sl, sl_after_tp1,
                       result, hypo_result, pnl_usd, hypo_pnl_usd, pnl_pct,
                       ml_data_only, market_context,
                       shadow
                FROM setups
                WHERE resolved = TRUE
                  AND (result IS NOT NULL OR hypo_result IS NOT NULL)
                ORDER BY alert_time ASC
            """)
            rows = cur.fetchall()
    finally:
        conn.close()

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    numeric_cols = ["sl", "sl_after_tp1", "rr", "score", "pnl_usd", "hypo_pnl_usd", "pnl_pct"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["effective_result"] = df.apply(
        lambda r: r["hypo_result"] if r["ml_data_only"] and r["hypo_result"] else r["result"],
        axis=1,
    )
    return df


def build_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """Buduje macierz features i labels z DataFrame setupów."""

    df = df[df["effective_result"].isin(WIN_RESULTS | LOSS_RESULTS)].copy()
    if df.empty:
        return pd.DataFrame(), pd.Series(dtype=int), []

    df["label"] = (df["effective_result"].isin(WIN_RESULTS)).astype(int)

    # --- Basic features (Faza 0 — zawsze dostępne) ---
    df["hour"] = pd.to_datetime(df["alert_time"]).dt.hour
    df["day_of_week"] = pd.to_datetime(df["alert_time"]).dt.dayofweek

    df["entry_price"] = df["entries"].apply(
        lambda e: float(e[0]) if isinstance(e, list) and e else np.nan
    )
    df["tp1_price"] = df["tps"].apply(
        lambda t: float(t[0]) if isinstance(t, list) and t else np.nan
    )
    df["tp2_price"] = df["tps"].apply(
        lambda t: float(t[1]) if isinstance(t, list) and len(t) > 1 else np.nan
    )

    df["sl_distance"] = abs(df["entry_price"] - df["sl"])
    df["tp1_distance"] = abs(df["tp1_price"] - df["entry_price"])
    df["sl_after_tp1_dist"] = abs(df["sl_after_tp1"] - df["entry_price"]).fillna(0)

    # Categorical encoding
    type_map = {t: i for i, t in enumerate(sorted(df["type"].dropna().unique()))}
    variant_map = {v: i for i, v in enumerate(sorted(df["variant"].dropna().unique()))}
    direction_map = {"long": 0, "short": 1}
    trigger_map = {"falling": 0, "rising": 1}

    df["type_enc"] = df["type"].map(type_map).fillna(-1).astype(int)
    df["direction_enc"] = df["direction"].map(direction_map).fillna(-1).astype(int)
    df["variant_enc"] = df["variant"].map(variant_map).fillna(-1).astype(int)
    df["trigger_enc"] = df["entry_trigger"].map(trigger_map).fillna(-1).astype(int)

    basic_features = [
        "rr", "score", "type_enc", "direction_enc", "variant_enc",
        "trigger_enc", "hour", "day_of_week",
        "sl_distance", "tp1_distance", "sl_after_tp1_dist",
    ]

    # --- Extended features (Faza 2 — z market_context) ---
    mc_features = []
    has_mc = df["market_context"].notna().sum() > len(df) * 0.3

    if has_mc:
        mc_df = df["market_context"].apply(
            lambda x: x if isinstance(x, dict) else {}
        )

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
            df[f"mc_{col}"] = mc_df.apply(lambda x: x.get(col))
            mc_features.append(f"mc_{col}")

        # Derived features
        df["mc_atr_ratio"] = df["mc_atr_h1"] / df["mc_atr_m15"].replace(0, np.nan)
        df["mc_entry_atr_mult"] = df["sl_distance"] / df["mc_atr_h1"].replace(0, np.nan)
        mc_features.extend(["mc_atr_ratio", "mc_entry_atr_mult"])

    feature_cols = basic_features + mc_features

    # Save feature names and category mappings for scorer
    meta = {
        "feature_cols": feature_cols,
        "basic_features": basic_features,
        "mc_features": mc_features,
        "type_map": type_map,
        "variant_map": variant_map,
        "direction_map": direction_map,
        "trigger_map": trigger_map,
        "has_market_context": has_mc,
        "trained_at": datetime.utcnow().isoformat(),
        "n_samples": len(df),
        "n_wins": int(df["label"].sum()),
        "n_losses": int((df["label"] == 0).sum()),
    }

    X = df[feature_cols].copy()
    y = df["label"].copy()

    return X, y, meta


def train_model(X: pd.DataFrame, y: pd.Series, meta: dict) -> tuple:
    """Trenuje LightGBM z TimeSeriesSplit cross-validation."""

    print(f"\n{'='*60}")
    print(f"Training data: {len(X)} samples ({y.sum()} wins, {(y==0).sum()} losses)")
    mc_count = len(meta['mc_features'])
    mc_info = f" + {mc_count} market_context" if meta['mc_features'] else ""
    print(f"Features: {len(X.columns)} ({len(meta['basic_features'])} basic{mc_info})")
    print(f"{'='*60}\n")

    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "verbosity": -1,
        "num_leaves": 15,
        "max_depth": 4,
        "learning_rate": 0.05,
        "n_estimators": 200,
        "min_child_samples": 10,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.1,
        "reg_lambda": 1.0,
    }

    # Time-series cross-validation
    tscv = TimeSeriesSplit(n_splits=min(5, max(2, len(X) // 30)))
    cv_results = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model = lgb.LGBMClassifier(**params)
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.log_evaluation(0)],
        )

        y_pred = model.predict(X_val)
        y_prob = model.predict_proba(X_val)[:, 1]

        fold_metrics = {
            "fold": fold,
            "accuracy": accuracy_score(y_val, y_pred),
            "precision": precision_score(y_val, y_pred, zero_division=0),
            "recall": recall_score(y_val, y_pred, zero_division=0),
            "f1": f1_score(y_val, y_pred, zero_division=0),
        }
        try:
            fold_metrics["auc"] = roc_auc_score(y_val, y_prob)
        except ValueError:
            fold_metrics["auc"] = 0.0

        cv_results.append(fold_metrics)
        print(f"Fold {fold}: acc={fold_metrics['accuracy']:.3f} "
              f"prec={fold_metrics['precision']:.3f} "
              f"rec={fold_metrics['recall']:.3f} "
              f"f1={fold_metrics['f1']:.3f} "
              f"auc={fold_metrics['auc']:.3f}")

    # Final model on all data
    final_model = lgb.LGBMClassifier(**params)
    final_model.fit(X, y, callbacks=[lgb.log_evaluation(0)])

    # Feature importance
    importances = dict(zip(X.columns, final_model.feature_importances_.tolist()))
    sorted_imp = sorted(importances.items(), key=lambda x: x[1], reverse=True)

    print(f"\n{'='*60}")
    print("Feature importances:")
    for name, imp in sorted_imp[:15]:
        print(f"  {name:30s} {imp:6.0f}")

    # Average CV metrics
    avg_metrics = {}
    for key in ["accuracy", "precision", "recall", "f1", "auc"]:
        vals = [r[key] for r in cv_results]
        avg_metrics[key] = sum(vals) / len(vals) if vals else 0.0

    print(f"\nAverage CV: acc={avg_metrics['accuracy']:.3f} "
          f"prec={avg_metrics['precision']:.3f} "
          f"rec={avg_metrics['recall']:.3f} "
          f"f1={avg_metrics['f1']:.3f} "
          f"auc={avg_metrics['auc']:.3f}")

    meta["cv_results"] = cv_results
    meta["avg_metrics"] = avg_metrics
    meta["feature_importances"] = importances

    return final_model, meta


def save_model(model, meta: dict, model_path: str, meta_path: str):
    """Zapisuje model i metadane."""
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(model_path)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2, default=str)
    print(f"\nModel saved: {model_path}")
    print(f"Meta saved:  {meta_path}")


def run_training(db_url: str = None, model_path: str = "model/setup_scorer.lgb") -> dict:
    """Uruchamia trening i zwraca wyniki jako dict (do API)."""
    if lgb is None:
        return {"error": f"lightgbm niedostępny: {_lgb_error}"}
    if psycopg2 is None:
        return {"error": "psycopg2 not installed — run: pip install psycopg2-binary"}
    db_url = db_url or os.getenv("DATABASE_URL")
    if not db_url:
        return {"error": "Brak DATABASE_URL."}

    df = export_training_data(db_url)
    if df.empty:
        return {"error": "Brak resolved setupów w bazie."}

    n_total = len(df)
    X, y, meta = build_features(df)
    if X.empty:
        return {"error": "Brak setupów z wynikiem win/loss do treningu.",
                "total_resolved": n_total}

    model, meta = train_model(X, y, meta)

    meta_path = model_path.replace(".lgb", "_meta.json")
    save_model(model, meta, model_path, meta_path)

    try:
        import ml_scorer
        ml_scorer._loaded = False
    except ImportError:
        pass

    feature_names_pl = {
        "rr": "Risk:Reward",
        "score": "Siła reżimu",
        "type_enc": "Typ setupu",
        "direction_enc": "Kierunek (long/short)",
        "variant_enc": "Wariant",
        "trigger_enc": "Entry trigger",
        "hour": "Godzina",
        "day_of_week": "Dzień tygodnia",
        "sl_distance": "Odległość SL",
        "tp1_distance": "Odległość TP1",
        "sl_after_tp1_dist": "Odległość SL po TP1",
    }

    importances = meta.get("feature_importances", {})
    sorted_imp = sorted(importances.items(), key=lambda x: x[1], reverse=True)
    top_features = [
        {"name": feature_names_pl.get(k, k), "key": k, "importance": v}
        for k, v in sorted_imp[:10]
    ]

    avg = meta.get("avg_metrics", {})
    return {
        "status": "ok",
        "training_data": {
            "total_resolved": n_total,
            "used_for_training": meta["n_samples"],
            "wins": meta["n_wins"],
            "losses": meta["n_losses"],
            "win_rate": round(meta["n_wins"] / meta["n_samples"] * 100, 1),
        },
        "model_quality": {
            "accuracy": round(avg.get("accuracy", 0) * 100, 1),
            "precision": round(avg.get("precision", 0) * 100, 1),
            "recall": round(avg.get("recall", 0) * 100, 1),
            "f1": round(avg.get("f1", 0) * 100, 1),
            "auc": round(avg.get("auc", 0), 3),
        },
        "top_features": top_features,
        "has_market_context": meta.get("has_market_context", False),
        "cv_folds": len(meta.get("cv_results", [])),
        "model_path": model_path,
    }


def main():
    if lgb is None:
        sys.exit("lightgbm not installed — run: pip install lightgbm>=4.0.0")
    if psycopg2 is None:
        sys.exit("psycopg2 not installed — run: pip install psycopg2-binary")

    parser = argparse.ArgumentParser(description="Train ML model for setup scoring")
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL"),
                        help="PostgreSQL connection string (default: $DATABASE_URL)")
    parser.add_argument("--out", default="model/setup_scorer.lgb",
                        help="Output model path")
    args = parser.parse_args()

    if not args.db_url:
        sys.exit("ERROR: No DATABASE_URL. Use --db-url or set DATABASE_URL env var.")

    result = run_training(args.db_url, args.out)
    if result.get("error"):
        sys.exit(f"ERROR: {result['error']}")

    print(f"\nTraining complete:")
    print(f"  Samples: {result['training_data']['used_for_training']}")
    print(f"  Accuracy: {result['model_quality']['accuracy']}%")
    print(f"  AUC: {result['model_quality']['auc']}")


if __name__ == "__main__":
    main()
