"""Feature schema shared between train_model.py and predict.py, so both
always agree on which columns the model expects and how categoricals are
encoded. Kept dependency-light (only sklearn's LabelEncoder, no
RandomForest/GridSearchCV) so predict.py doesn't need to import
train_model.py's heavier training-time machinery.
"""

import pandas as pd
from sklearn.preprocessing import LabelEncoder

# Matches the paper's feature set: geometric properties (volume, surface
# area, face count, density, mass) + order-level properties (material
# type, order quantity, order year, supplier) -- machine_type is an extra
# categorical feature this project's synthetic generator also varies,
# included the same way as the other categoricals. bend_count is a
# geometric feature relevant mainly to Sheet Metal parts (0 for every
# other process, see production_cost.py); manufacturing_process (CNC
# Machining / Injection Molding / Sheet Metal) is the process-aware
# extension to the paper -- see production_cost.py for the per-process
# production cost formulas whose output is what the synthetic target cost
# is grounded in (see generate_synthetic_dataset.py).
NUMERIC_FEATURES = [
    "volume_m3",
    "surface_area_m2",
    "face_count",
    "bend_count",
    "density_kg_m3",
    "mass_kg",
    "order_quantity",
    "order_year",
]
CATEGORICAL_FEATURES = ["material_type", "supplier", "machine_type", "manufacturing_process"]
FEATURE_COLUMNS = NUMERIC_FEATURES + CATEGORICAL_FEATURES

# Coarse material-type buckets inferred from the material name, since
# materials.csv itself doesn't store a category column (that's only
# available live from SolidWorks -- see fill_metal_costs.py). Shared
# between generate_synthetic_dataset.py (labeling training rows) and
# mcp_server.py (labeling a live part at prediction time), so both sides
# always bucket a given material name the same way.
_MATERIAL_TYPE_KEYWORDS = [
    ("stainless", "Stainless Steel"),
    ("tool steel", "Tool Steel"),
    ("steel", "Steel"),
    ("iron", "Iron"),
    ("aluminum", "Aluminum"),
    ("aluminium", "Aluminum"),
    ("copper", "Copper"),
    ("brass", "Copper"),
    ("bronze", "Copper"),
    ("titanium", "Titanium"),
    ("zinc", "Zinc"),
    ("magnesium", "Magnesium"),
    ("nickel", "Nickel"),
    ("plastic", "Plastic"),
    ("nylon", "Plastic"),
    ("abs", "Plastic"),
    ("rubber", "Rubber"),
]


def infer_material_type(material_name: str) -> str:
    lowered = material_name.lower()
    for keyword, material_type in _MATERIAL_TYPE_KEYWORDS:
        if keyword in lowered:
            return material_type
    return "Other"


def fit_encoders(df: pd.DataFrame) -> dict[str, LabelEncoder]:
    """Fit one LabelEncoder per categorical column (training time only)."""
    encoders = {}
    for col in CATEGORICAL_FEATURES:
        encoder = LabelEncoder()
        encoder.fit(df[col])
        encoders[col] = encoder
    return encoders


def encode_categoricals(df: pd.DataFrame, encoders: dict[str, LabelEncoder]) -> pd.DataFrame:
    """Apply already-fitted encoders to turn categorical columns into the
    integer codes a RandomForestRegressor can split on.

    A RandomForestRegressor makes binary threshold splits on numeric
    values, so string categories (material_type, supplier, machine_type)
    must first become integers. Label encoding (one integer code per
    category) is sufficient for a tree-based model -- unlike a linear
    model, a random forest doesn't assume the encoded integers are
    ordered/continuous, since each split just asks "is this category's
    code <= threshold", which a tree can use to isolate any subset of
    categories across a few splits.

    Any category value not seen during training is mapped to a new
    "unknown" code (len(encoder.classes_)) rather than raising, so a
    live prediction can't crash on an unfamiliar material_type/supplier/
    machine_type value.
    """
    df = df.copy()
    for col in CATEGORICAL_FEATURES:
        encoder = encoders[col]
        known = set(encoder.classes_)
        df[col] = df[col].apply(
            lambda v, enc=encoder, known=known: (
                enc.transform([v])[0] if v in known else len(enc.classes_)
            )
        )
    return df
