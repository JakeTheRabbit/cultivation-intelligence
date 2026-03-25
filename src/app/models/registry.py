"""
Model registry for versioning, persisting, and loading ML models.

Models are stored under:
    {registry_path}/
        {model_id}/
            artifact.joblib   — serialised model (LightGBM Booster, sklearn Pipeline, etc.)
            metadata.json     — ModelMetadata as JSON

The registry supports promotion: exactly one model per prediction_type
can hold is_production = True at any time.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import joblib
import structlog

logger = structlog.get_logger(__name__)

_ARTIFACT_FILENAME = "artifact.joblib"
_METADATA_FILENAME = "metadata.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModelMetadata:
    """Version record for a registered model."""

    model_id: str
    model_type: str              # "lightgbm", "sklearn_pipeline", "tft", etc.
    prediction_type: str         # "yield", "risk", "quality", "stage_completion"
    version: str                 # semver string, e.g. "1.0.0"
    trained_at: datetime         # timezone-aware UTC datetime
    training_batches: list[str]  # UUIDs of grow batches used in training
    eval_metrics: dict[str, float]  # e.g. {"mae": 0.12, "rmse": 0.18, "r2": 0.91}
    feature_names: list[str]     # ordered list of input feature column names
    description: str             # free-text summary of this model version
    is_production: bool = False  # only one model per prediction_type may be True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trained_at"] = self.trained_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModelMetadata":
        d = dict(d)
        trained_at = d.get("trained_at")
        if isinstance(trained_at, str):
            d["trained_at"] = datetime.fromisoformat(trained_at)
        return cls(**d)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """File-system backed model registry.

    Thread-safety: not guaranteed.  Wrap with a lock if used concurrently.
    """

    def __init__(self, registry_path: Path) -> None:
        self.registry_path = Path(registry_path)
        self.registry_path.mkdir(parents=True, exist_ok=True)
        logger.info("registry_initialised", path=str(self.registry_path))

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _model_dir(self, model_id: str) -> Path:
        """Return the directory for a given model_id."""
        return self.registry_path / model_id

    def _artifact_path(self, model_id: str) -> Path:
        return self._model_dir(model_id) / _ARTIFACT_FILENAME

    def _metadata_path(self, model_id: str) -> Path:
        return self._model_dir(model_id) / _METADATA_FILENAME

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def register(self, model: Any, metadata: ModelMetadata) -> str:
        """Persist a model artifact and its metadata to the registry.

        If metadata.model_id is empty, a new UUID4 is assigned.

        Args:
            model:    Any joblib-serialisable object (lgb.Booster, Pipeline, …)
            metadata: Metadata describing this version.

        Returns:
            The model_id string used to identify this registration.
        """
        if not metadata.model_id:
            metadata.model_id = str(uuid.uuid4())

        model_dir = self._model_dir(metadata.model_id)
        model_dir.mkdir(parents=True, exist_ok=True)

        # Serialise artifact
        artifact_path = self._artifact_path(metadata.model_id)
        joblib.dump(model, artifact_path, compress=3)
        logger.info(
            "model_artifact_saved",
            model_id=metadata.model_id,
            path=str(artifact_path),
        )

        # Serialise metadata
        meta_path = self._metadata_path(metadata.model_id)
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(metadata.to_dict(), fh, indent=2, default=str)
        logger.info(
            "model_metadata_saved",
            model_id=metadata.model_id,
            prediction_type=metadata.prediction_type,
            version=metadata.version,
        )

        return metadata.model_id

    def load(self, model_id: str) -> tuple[Any, ModelMetadata]:
        """Load a model artifact and its metadata from the registry.

        Args:
            model_id: The model ID returned by register().

        Returns:
            (model_object, ModelMetadata)

        Raises:
            FileNotFoundError: If model_id is not found in the registry.
        """
        artifact_path = self._artifact_path(model_id)
        meta_path = self._metadata_path(model_id)

        if not artifact_path.exists():
            raise FileNotFoundError(
                f"Model artifact not found for model_id='{model_id}' "
                f"(expected: {artifact_path})"
            )
        if not meta_path.exists():
            raise FileNotFoundError(
                f"Model metadata not found for model_id='{model_id}' "
                f"(expected: {meta_path})"
            )

        model = joblib.load(artifact_path)
        with meta_path.open("r", encoding="utf-8") as fh:
            metadata = ModelMetadata.from_dict(json.load(fh))

        logger.info(
            "model_loaded",
            model_id=model_id,
            prediction_type=metadata.prediction_type,
            version=metadata.version,
        )
        return model, metadata

    def get_production_model(
        self, prediction_type: str
    ) -> Optional[tuple[Any, ModelMetadata]]:
        """Return the current production model for a given prediction_type.

        Returns None if no production model is registered for that type.
        """
        for metadata in self.list_models(prediction_type=prediction_type):
            if metadata.is_production:
                return self.load(metadata.model_id)
        logger.warning(
            "no_production_model", prediction_type=prediction_type
        )
        return None

    def promote(self, model_id: str) -> None:
        """Mark model_id as the production model for its prediction_type.

        Demotes any previously promoted model of the same prediction_type.

        Args:
            model_id: ID of the model to promote.

        Raises:
            FileNotFoundError: If model_id is not found.
        """
        _, target_meta = self.load(model_id)
        prediction_type = target_meta.prediction_type

        # Demote existing production models of the same prediction_type
        for meta in self.list_models(prediction_type=prediction_type):
            if meta.is_production and meta.model_id != model_id:
                meta.is_production = False
                meta_path = self._metadata_path(meta.model_id)
                with meta_path.open("w", encoding="utf-8") as fh:
                    json.dump(meta.to_dict(), fh, indent=2, default=str)
                logger.info(
                    "model_demoted",
                    model_id=meta.model_id,
                    prediction_type=prediction_type,
                )

        # Promote target
        target_meta.is_production = True
        meta_path = self._metadata_path(model_id)
        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(target_meta.to_dict(), fh, indent=2, default=str)

        logger.info(
            "model_promoted",
            model_id=model_id,
            prediction_type=prediction_type,
            version=target_meta.version,
        )

    def list_models(
        self, prediction_type: Optional[str] = None
    ) -> list[ModelMetadata]:
        """List all registered models, optionally filtered by prediction_type.

        Returns models sorted by trained_at descending (newest first).
        """
        results: list[ModelMetadata] = []

        for model_dir in self.registry_path.iterdir():
            if not model_dir.is_dir():
                continue
            meta_path = model_dir / _METADATA_FILENAME
            if not meta_path.exists():
                continue

            try:
                with meta_path.open("r", encoding="utf-8") as fh:
                    metadata = ModelMetadata.from_dict(json.load(fh))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "metadata_parse_error",
                    path=str(meta_path),
                    exc=str(exc),
                )
                continue

            if prediction_type and metadata.prediction_type != prediction_type:
                continue

            results.append(metadata)

        results.sort(key=lambda m: m.trained_at, reverse=True)
        return results

    def compare(self, model_id_a: str, model_id_b: str) -> dict:
        """Produce a side-by-side metric comparison of two registered models.

        Returns a dict structured as:
        {
            "model_a": {model_id, version, prediction_type, trained_at, metrics},
            "model_b": {model_id, version, prediction_type, trained_at, metrics},
            "delta":   {metric_name: (value_a - value_b), ...},
            "winner":  {metric_name: "a" | "b" | "tie", ...}
        }

        Raises:
            FileNotFoundError: If either model_id is not found.
        """
        _, meta_a = self.load(model_id_a)
        _, meta_b = self.load(model_id_b)

        def _fmt(meta: ModelMetadata) -> dict:
            return {
                "model_id": meta.model_id,
                "version": meta.version,
                "prediction_type": meta.prediction_type,
                "trained_at": meta.trained_at.isoformat(),
                "is_production": meta.is_production,
                "metrics": meta.eval_metrics,
            }

        # Build delta and winner for metrics present in both models
        all_metrics = set(meta_a.eval_metrics) | set(meta_b.eval_metrics)
        delta: dict[str, Optional[float]] = {}
        winner: dict[str, str] = {}

        # Metrics where lower is better
        lower_is_better = {"mae", "rmse", "mape", "loss", "log_loss", "brier"}

        for metric in sorted(all_metrics):
            val_a = meta_a.eval_metrics.get(metric)
            val_b = meta_b.eval_metrics.get(metric)

            if val_a is None or val_b is None:
                delta[metric] = None
                winner[metric] = "n/a"
                continue

            diff = val_a - val_b
            delta[metric] = round(diff, 6)

            if abs(diff) < 1e-8:
                winner[metric] = "tie"
            elif metric.lower() in lower_is_better:
                winner[metric] = "a" if val_a < val_b else "b"
            else:
                winner[metric] = "a" if val_a > val_b else "b"

        return {
            "model_a": _fmt(meta_a),
            "model_b": _fmt(meta_b),
            "delta": delta,
            "winner": winner,
        }
