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
from production_cost import estimate_production_cost  # noqa: E402

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
    """Predict a part's total cost (INR) from its features, AND compute an
    explicit material_cost / production_cost breakdown the same way the
    synthetic training target was computed (see
    generate_synthetic_dataset.py) -- so a chat answer can show the user
    where the number comes from, not just one opaque total.

    `features` must contain every key in FEATURE_COLUMNS (see
    cost_model/features.py): volume_m3, surface_area_m2, face_count,
    bend_count, density_kg_m3, mass_kg, order_quantity, order_year
    (numeric), plus material_type, supplier, machine_type,
    manufacturing_process (categorical). Categorical values not seen
    during training are mapped to an "unknown" bucket rather than raising
    (see features.encode_categoricals).

    Two extra keys are required beyond FEATURE_COLUMNS, since the model
    itself doesn't need them but the breakdown does:
    - "cost_per_kg": the current material's real cost_per_kg (INR/kg,
      from materials.csv) -- material_cost_inr = mass_kg * cost_per_kg.
    - "bounding_box_mm": (x, y, z) bounding box in millimeters -- only
      used when manufacturing_process is "Sheet Metal" (see
      production_cost.estimate_sheet_metal_cost); pass (0, 0, 0) for
      other processes.

    Returns predicted_total_cost_inr (from the trained Random Forest
    model -- this is the ML-predicted figure, the paper's headline
    output), plus material_cost_inr and production_cost_inr computed
    directly from real material data and production_cost.py's
    process-specific formula (not from the model) so the two numbers are
    shown separately and explicitly, as required by estimate_cost().
    """
    missing = [col for col in FEATURE_COLUMNS if col not in features]
    if missing:
        raise ValueError(f"predict_cost() missing required features: {missing}")
    if "cost_per_kg" not in features:
        raise ValueError("predict_cost() missing required key: cost_per_kg")

    bundle = _load_bundle()
    model = bundle["model"]
    encoders = bundle["encoders"]

    row = {col: features[col] for col in FEATURE_COLUMNS}
    df = pd.DataFrame([row])
    df = encode_categoricals(df, encoders)

    predicted_total_cost = float(model.predict(df[FEATURE_COLUMNS])[0])

    material_cost_inr = row["mass_kg"] * features["cost_per_kg"]
    production_result = estimate_production_cost(
        row["manufacturing_process"],
        feature_count=row["face_count"],
        volume_m3=row["volume_m3"],
        order_quantity=row["order_quantity"],
        bounding_box_mm=features.get("bounding_box_mm", (0.0, 0.0, 0.0)),
        bend_count=row["bend_count"],
    )

    return {
        "predicted_total_cost_inr": round(max(predicted_total_cost, 0.0), 2),
        "material_cost_inr": round(material_cost_inr, 2),
        "production_cost_inr": production_result.production_cost_inr,
        "production_cost_breakdown": production_result.breakdown,
        "production_cost_assumptions": production_result.assumptions,
        "manufacturing_process": row["manufacturing_process"],
        "features_used": row,
    }
