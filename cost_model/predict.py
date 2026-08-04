"""Loads the trained Random Forest cost model and predicts a part's cost
from its features.

Only depends on cost_model/features.py (schema + encoding helpers), not
train_model.py, so the MCP server (mcp_server.py) can import this at
request time without pulling in GridSearchCV/RandomForestRegressor
training machinery it never uses.
"""

import sys
from pathlib import Path

import joblib
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost_model.features import FEATURE_COLUMNS, encode_categoricals  # noqa: E402

_MODEL_PATH = Path(__file__).parent / "trained_model.joblib"

_bundle = None


def _load_bundle() -> dict:
    """Lazily load trained_model.joblib, same deferred-load reasoning as
    materials.csv elsewhere in this project: a missing model file should
    only break predict_cost() calls, not importing this module.
    """
    global _bundle
    if _bundle is None:
        if not _MODEL_PATH.exists():
            raise FileNotFoundError(
                f"{_MODEL_PATH} not found. Run "
                f"cost_model/generate_synthetic_dataset.py then "
                f"cost_model/train_model.py first."
            )
        _bundle = joblib.load(_MODEL_PATH)
    return _bundle


def predict_cost(features: dict) -> dict:
    """Predict a part's cost (INR) from its features.

    `features` must contain every key in FEATURE_COLUMNS (see
    cost_model/features.py): volume_m3, surface_area_m2, face_count,
    density_kg_m3, mass_kg, order_quantity, order_year (numeric), plus
    material_type, supplier, machine_type (categorical). Categorical
    values not seen during training are mapped to an "unknown" bucket
    rather than raising (see features.encode_categoricals).

    Returns {"predicted_cost_inr": <float>, "features_used": <dict>} so
    the caller can show which inputs drove the prediction.
    """
    missing = [col for col in FEATURE_COLUMNS if col not in features]
    if missing:
        raise ValueError(f"predict_cost() missing required features: {missing}")

    bundle = _load_bundle()
    model = bundle["model"]
    encoders = bundle["encoders"]

    row = {col: features[col] for col in FEATURE_COLUMNS}
    df = pd.DataFrame([row])
    df = encode_categoricals(df, encoders)

    predicted_cost = float(model.predict(df[FEATURE_COLUMNS])[0])

    return {
        "predicted_cost_inr": round(max(predicted_cost, 0.0), 2),
        "features_used": row,
    }
