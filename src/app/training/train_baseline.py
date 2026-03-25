"""
LightGBM baseline training entrypoint.

Trains yield (regression), quality (multiclass), or risk (binary) models
using time-series cross-validation and registers the result in the model
registry.

Usage:
    python -m src.app.training.train_baseline \\
        --data-path /data/training/grow_features.parquet \\
        --output-dir /models/registry \\
        --task yield \\
        --n-splits 5
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
import structlog
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import LabelEncoder

from src.app.models.registry import ModelMetadata, ModelRegistry

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Columns that are never used as features (identifiers, targets, metadata)
# ---------------------------------------------------------------------------

_NON_FEATURE_COLUMNS: set[str] = {
    # Identifiers
    "batch_id",
    "sensor_id",
    "id",
    "row_id",
    # Targets
    "yield_g",
    "quality_grade",
    "quality_grade_encoded",
    "risk_label",
    "risk_score",
    # Timestamps / metadata
    "timestamp",
    "harvest_date",
    "start_date",
    "created_at",
    "updated_at",
    "stage",
    "notes",
    "description",
}

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_training_data(data_path: Path) -> pd.DataFrame:
    """Load a parquet or CSV training file and perform basic validation.

    Expected required columns vary by task, but at minimum the file should
    contain sensor feature columns and one or more target columns.

    Args:
        data_path: Path to .parquet or .csv file.

    Returns:
        Loaded DataFrame.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError:        If no rows are present or file format is unsupported.
    """
    data_path = Path(data_path)
    if not data_path.exists():
        raise FileNotFoundError(f"Training data not found: {data_path}")

    suffix = data_path.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(data_path)
    elif suffix in {".csv", ".tsv"}:
        sep = "\t" if suffix == ".tsv" else ","
        df = pd.read_csv(data_path, sep=sep)
    else:
        raise ValueError(
            f"Unsupported file format: '{suffix}'. Use .parquet or .csv"
        )

    if df.empty:
        raise ValueError(f"Training data file is empty: {data_path}")

    logger.info(
        "training_data_loaded",
        path=str(data_path),
        rows=len(df),
        columns=list(df.columns),
    )
    return df


# ---------------------------------------------------------------------------
# Feature preparation
# ---------------------------------------------------------------------------


def prepare_yield_features(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """Separate feature matrix from yield target column.

    Drops non-feature columns and any column with all-null values.
    Fills remaining NaNs with column median.

    Args:
        df: Raw training DataFrame.  Must contain a 'yield_g' column.

    Returns:
        (X, y) where X is the feature DataFrame and y is the yield Series.

    Raises:
        ValueError: If 'yield_g' column is absent.
    """
    if "yield_g" not in df.columns:
        raise ValueError(
            "DataFrame must contain a 'yield_g' target column for yield training."
        )

    y: pd.Series = df["yield_g"].astype(float)

    # Drop non-feature columns that happen to be present
    drop_cols = list(_NON_FEATURE_COLUMNS.intersection(df.columns))
    X = df.drop(columns=drop_cols)

    # Drop columns that are entirely NaN
    all_nan = X.columns[X.isna().all()].tolist()
    if all_nan:
        logger.warning("dropping_all_nan_columns", columns=all_nan)
        X = X.drop(columns=all_nan)

    # Encode any remaining object columns with LabelEncoder
    for col in X.select_dtypes(include=["object", "category"]).columns:
        le = LabelEncoder()
        X[col] = le.fit_transform(X[col].astype(str))

    # Fill NaN with median
    X = X.fillna(X.median(numeric_only=True))

    logger.info("features_prepared", n_features=X.shape[1], n_samples=len(X))
    return X, y


# ---------------------------------------------------------------------------
# LightGBM hyperparameters
# ---------------------------------------------------------------------------


def get_lgbm_params(task: str) -> dict:
    """Return a LightGBM parameter dict tuned for the given task.

    Args:
        task: One of "yield", "quality", "risk".

    Returns:
        LightGBM parameter dict.

    Raises:
        ValueError: For unknown task names.
    """
    common = {
        "verbosity": -1,
        "n_jobs": -1,
        "seed": 42,
        "bagging_seed": 42,
        "feature_fraction_seed": 42,
        "num_leaves": 63,
        "min_child_samples": 20,
        "learning_rate": 0.05,
        "n_estimators": 1000,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "feature_fraction": 0.7,
        "reg_alpha": 0.1,
        "reg_lambda": 0.1,
    }

    if task == "yield":
        return {
            **common,
            "objective": "regression_l1",  # MAE objective — robust to outliers
            "metric": ["mae", "rmse"],
            "boosting_type": "gbdt",
        }

    if task == "quality":
        return {
            **common,
            "objective": "multiclass",
            "metric": "multi_logloss",
            "num_class": 4,  # GRADE_A, GRADE_B, GRADE_C, REJECTED
            "boosting_type": "gbdt",
            "class_weight": "balanced",
        }

    if task == "risk":
        return {
            **common,
            "objective": "binary",
            "metric": ["binary_logloss", "auc"],
            "boosting_type": "gbdt",
            "is_unbalance": True,   # handles class imbalance automatically
            "scale_pos_weight": 3,  # further boost minority (high-risk) class
        }

    raise ValueError(
        f"Unknown task '{task}'. Choose one of: yield, quality, risk"
    )


# ---------------------------------------------------------------------------
# Cross-validated training
# ---------------------------------------------------------------------------


def train_with_cv(
    X: pd.DataFrame,
    y: pd.Series,
    params: dict,
    n_splits: int = 5,
) -> tuple[lgb.Booster, dict]:
    """Train a LightGBM model using time-series cross-validation.

    NOTE: TimeSeriesSplit is used here at the *row* level, not at the
    batch level.  In production, rows should be pre-sorted by timestamp
    so that earlier grow weeks appear first, guaranteeing temporal order.
    If batch-level CV is required, group rows by batch_id before splitting.

    Args:
        X:        Feature DataFrame (rows × features).
        y:        Target Series, same index as X.
        params:   LightGBM parameter dict from get_lgbm_params().
        n_splits: Number of time-series folds.

    Returns:
        (final_booster, eval_metrics_dict)
        where final_booster is trained on the full dataset and eval_metrics
        contains OOF aggregates: mae, rmse, mape (regression) or auc, f1
        (classification).
    """
    tscv = TimeSeriesSplit(n_splits=n_splits)
    X_arr = X.values
    y_arr = y.values

    oof_preds: list[np.ndarray] = []
    oof_trues: list[np.ndarray] = []
    fold_models: list[lgb.Booster] = []

    objective = params.get("objective", "regression")
    is_binary = objective == "binary"
    is_multiclass = objective == "multiclass"

    # Early stopping callback compatible with LightGBM 4.x
    callbacks = [lgb.early_stopping(stopping_rounds=50, verbose=False)]

    for fold_idx, (train_idx, val_idx) in enumerate(tscv.split(X_arr)):
        X_train, X_val = X_arr[train_idx], X_arr[val_idx]
        y_train, y_val = y_arr[train_idx], y_arr[val_idx]

        dtrain = lgb.Dataset(X_train, label=y_train, feature_name=list(X.columns))
        dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)

        # Derive num_boost_round from params (pop it to avoid duplicate kwarg)
        fold_params = {k: v for k, v in params.items() if k != "n_estimators"}
        num_boost_round = params.get("n_estimators", 1000)

        booster = lgb.train(
            fold_params,
            dtrain,
            num_boost_round=num_boost_round,
            valid_sets=[dval],
            callbacks=callbacks,
        )

        fold_preds = booster.predict(X_val)
        oof_preds.append(fold_preds)
        oof_trues.append(y_val)
        fold_models.append(booster)

        logger.info(
            "fold_complete",
            fold=fold_idx + 1,
            n_splits=n_splits,
            val_rows=len(val_idx),
            best_iter=booster.best_iteration,
        )

    # Aggregate OOF metrics
    all_preds = np.concatenate(oof_preds)
    all_trues = np.concatenate(oof_trues)

    eval_metrics: dict[str, float] = {}

    if is_binary:
        from sklearn.metrics import roc_auc_score, f1_score

        binary_preds = (all_preds >= 0.5).astype(int)
        eval_metrics["auc"] = float(roc_auc_score(all_trues, all_preds))
        eval_metrics["f1"] = float(f1_score(all_trues, binary_preds, zero_division=0))
        eval_metrics["log_loss"] = float(
            -np.mean(
                all_trues * np.log(np.clip(all_preds, 1e-7, 1 - 1e-7))
                + (1 - all_trues) * np.log(np.clip(1 - all_preds, 1e-7, 1 - 1e-7))
            )
        )

    elif is_multiclass:
        from sklearn.metrics import f1_score, log_loss

        # all_preds is (n_samples, n_classes) for multiclass
        class_preds = np.argmax(all_preds, axis=1)
        eval_metrics["log_loss"] = float(log_loss(all_trues, all_preds))
        eval_metrics["f1_macro"] = float(
            f1_score(all_trues, class_preds, average="macro", zero_division=0)
        )
        eval_metrics["f1_weighted"] = float(
            f1_score(all_trues, class_preds, average="weighted", zero_division=0)
        )

    else:
        # Regression
        mae = mean_absolute_error(all_trues, all_preds)
        rmse = float(np.sqrt(mean_squared_error(all_trues, all_preds)))
        # MAPE — guard against zero targets
        nonzero_mask = all_trues != 0
        mape = (
            float(
                np.mean(
                    np.abs(
                        (all_trues[nonzero_mask] - all_preds[nonzero_mask])
                        / all_trues[nonzero_mask]
                    )
                )
                * 100
            )
            if nonzero_mask.any()
            else float("nan")
        )
        r2 = float(
            1
            - np.sum((all_trues - all_preds) ** 2)
            / (np.sum((all_trues - np.mean(all_trues)) ** 2) + 1e-10)
        )
        eval_metrics["mae"] = round(mae, 6)
        eval_metrics["rmse"] = round(rmse, 6)
        eval_metrics["mape"] = round(mape, 4)
        eval_metrics["r2"] = round(r2, 6)

    logger.info("oof_metrics", **eval_metrics)

    # Retrain on full dataset
    dtrain_full = lgb.Dataset(X_arr, label=y_arr, feature_name=list(X.columns))

    # Use best iteration from the last fold as a proxy; average is also reasonable
    best_iter = max(m.best_iteration for m in fold_models if m.best_iteration > 0)
    final_params = {k: v for k, v in params.items() if k != "n_estimators"}

    final_booster = lgb.train(
        final_params,
        dtrain_full,
        num_boost_round=best_iter,
    )

    logger.info(
        "final_model_trained",
        num_boost_round=best_iter,
        features=list(X.columns),
    )

    return final_booster, eval_metrics


# ---------------------------------------------------------------------------
# SHAP feature importance
# ---------------------------------------------------------------------------


def compute_shap_summary(
    model: lgb.Booster, X: pd.DataFrame
) -> dict[str, float]:
    """Compute SHAP-based feature importances and return top-10 as a dict.

    Uses LightGBM's built-in pred_contrib to get SHAP values without the
    external shap package dependency.

    Args:
        model: Trained LightGBM Booster.
        X:     Feature DataFrame (used for SHAP computation).

    Returns:
        Dict of {feature_name: mean_abs_shap_value} for the top-10 features,
        sorted descending by importance.
    """
    # pred_contrib returns shape (n_samples, n_features + 1); last col is bias
    shap_values = model.predict(X.values, pred_contrib=True)

    # For multiclass, shape is (n_samples, (n_features+1) * n_classes)
    n_features = X.shape[1]
    if shap_values.ndim == 2 and shap_values.shape[1] > n_features + 1:
        # Multiclass: reshape and average across classes
        n_classes = shap_values.shape[1] // (n_features + 1)
        shap_values = shap_values.reshape(-1, n_classes, n_features + 1)
        # Take absolute mean across samples and classes, drop bias term
        mean_abs_shap = np.abs(shap_values[:, :, :n_features]).mean(axis=(0, 1))
    else:
        # Regression / binary: drop bias column (last)
        mean_abs_shap = np.abs(shap_values[:, :n_features]).mean(axis=0)

    feature_names = list(X.columns)
    importance: dict[str, float] = {
        name: float(score) for name, score in zip(feature_names, mean_abs_shap)
    }

    # Return top-10 sorted descending
    top10 = dict(
        sorted(importance.items(), key=lambda kv: kv[1], reverse=True)[:10]
    )
    return top10


# ---------------------------------------------------------------------------
# Main training pipeline
# ---------------------------------------------------------------------------


def main(args: argparse.Namespace) -> None:
    """Execute the end-to-end training pipeline."""
    data_path = Path(args.data_path)
    output_dir = Path(args.output_dir)
    task: str = args.task
    n_splits: int = args.n_splits

    logger.info(
        "training_started",
        task=task,
        data_path=str(data_path),
        output_dir=str(output_dir),
        n_splits=n_splits,
    )

    # 1. Load data
    df = load_training_data(data_path)

    # 2. Prepare features
    if task == "yield":
        X, y = prepare_yield_features(df)

    elif task == "quality":
        if "quality_grade" not in df.columns:
            raise ValueError("DataFrame must contain 'quality_grade' for quality training.")
        le = LabelEncoder()
        y = pd.Series(le.fit_transform(df["quality_grade"].astype(str)), name="quality_grade_encoded")
        drop_cols = list(_NON_FEATURE_COLUMNS.intersection(df.columns))
        X = df.drop(columns=drop_cols)
        for col in X.select_dtypes(include=["object", "category"]).columns:
            X[col] = LabelEncoder().fit_transform(X[col].astype(str))
        X = X.fillna(X.median(numeric_only=True))

    elif task == "risk":
        if "risk_label" not in df.columns:
            raise ValueError("DataFrame must contain 'risk_label' (0/1) for risk training.")
        y = df["risk_label"].astype(int)
        drop_cols = list(_NON_FEATURE_COLUMNS.intersection(df.columns))
        X = df.drop(columns=drop_cols)
        for col in X.select_dtypes(include=["object", "category"]).columns:
            X[col] = LabelEncoder().fit_transform(X[col].astype(str))
        X = X.fillna(X.median(numeric_only=True))

    else:
        raise ValueError(f"Unknown task: '{task}'. Choose: yield, quality, risk")

    # 3. Get params
    params = get_lgbm_params(task)

    # 4. Train with cross-validation
    booster, eval_metrics = train_with_cv(X, y, params, n_splits=n_splits)

    # 5. SHAP summary
    shap_summary = compute_shap_summary(booster, X)
    logger.info("shap_top_features", **{k: round(v, 4) for k, v in shap_summary.items()})

    # 6. Register model
    registry = ModelRegistry(output_dir)
    model_id = str(uuid.uuid4())
    trained_at = datetime.now(timezone.utc)

    metadata = ModelMetadata(
        model_id=model_id,
        model_type="lightgbm",
        prediction_type=task,
        version="1.0.0",
        trained_at=trained_at,
        training_batches=[],  # populated by caller if batch IDs are known
        eval_metrics=eval_metrics,
        feature_names=list(X.columns),
        description=(
            f"LightGBM {task} baseline model. "
            f"Trained on {len(X)} samples with {n_splits}-fold TimeSeriesSplit CV. "
            f"SHAP top feature: {next(iter(shap_summary), 'n/a')}."
        ),
        is_production=False,
    )

    registered_id = registry.register(booster, metadata)

    # 7. Print evaluation summary
    print("\n" + "=" * 60)
    print(f"  Task          : {task}")
    print(f"  Model ID      : {registered_id}")
    print(f"  Samples       : {len(X)}")
    print(f"  Features      : {X.shape[1]}")
    print(f"  CV Folds      : {n_splits}")
    print("-" * 60)
    print("  OOF Metrics:")
    for k, v in eval_metrics.items():
        print(f"    {k:12s}: {v:.6f}")
    print("-" * 60)
    print("  SHAP Top Features:")
    for feat, imp in shap_summary.items():
        print(f"    {feat:30s}: {imp:.4f}")
    print("=" * 60 + "\n")

    logger.info(
        "training_complete",
        model_id=registered_id,
        task=task,
        eval_metrics=eval_metrics,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Train a LightGBM baseline model for the cultivation intelligence platform.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--data-path",
        required=True,
        help="Path to training data (.parquet or .csv).",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where the model registry lives.",
    )
    parser.add_argument(
        "--task",
        choices=["yield", "quality", "risk"],
        default="yield",
        help="Prediction task type.",
    )
    parser.add_argument(
        "--n-splits",
        type=int,
        default=5,
        help="Number of TimeSeriesSplit folds for cross-validation.",
    )

    parsed_args = parser.parse_args()
    main(parsed_args)
