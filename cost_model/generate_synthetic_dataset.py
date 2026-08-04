"""Generates a synthetic training dataset for the part-cost Random Forest
model (see cost_model/train_model.py).

**What "synthetic" means here, precisely (for the report's Methodology
section):** there is no proprietary industrial order history available for
this project. Every row's *feature values* (volume, surface area, face
count, order quantity, order year, supplier) are randomly sampled, not
drawn from real orders. But each row's *material* is a real entry picked
from materials.csv (via materials_db.load_materials()), so the material
name, its density, and -- critically -- its cost_per_kg are the actual
researched figures already used elsewhere in this project, not invented.
The target cost column is then computed from those real per-material
figures plus an explicit, documented production-cost formula (not a random
number), with a small amount of Gaussian noise layered on top to imitate
real-world estimation variance. This makes the dataset a *grounded
synthetic* dataset: fabricated order records, but a ground-truth cost
formula anchored in real material cost data -- not a real industrial
dataset and should not be described as one.

Run with:

    C:\\Python314\\python.exe cost_model/generate_synthetic_dataset.py
"""

import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# materials_db.py lives at the project root, one level up from cost_model/,
# and this script is run directly (not as part of the `cost_model` package),
# so the project root needs to be on sys.path explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cost_model.features import infer_material_type  # noqa: E402
from materials_db import load_materials  # noqa: E402

_OUTPUT_CSV_PATH = Path(__file__).parent / "synthetic_cost_dataset.csv"
_NUM_ROWS = 300
_RANDOM_SEED = 42

# Plausible ranges for a small-to-medium machined part, in SI units.
_VOLUME_M3_RANGE = (1e-6, 2e-3)  # ~1 cm^3 to ~2000 cm^3
_FACE_COUNT_RANGE = (4, 60)

# Excludes precious/exotic metals (pure gold, pure silver, beryllium) from
# the sampled material pool. These are priced orders of magnitude above
# ordinary engineering metals in materials.csv (e.g. silver ~77,400 INR/kg
# vs. steel ~73 INR/kg), and this project's target parts are ordinary
# machined mechanical parts (brackets, housings, etc. -- see
# MockAdapter's sample part) that would never realistically be solid
# blocks of silver/gold at the kg-scale masses _VOLUME_M3_RANGE produces.
# Leaving them in skews the target distribution so heavily (a handful of
# rows costing 50-100x the median) that RMSE-based accuracy collapses to
# ~0% even though the model fits the realistic bulk of the data well --
# an artifact of unrealistic sampling, not a real model limitation.
_MAX_COST_PER_KG_INR = 20000.0

_ORDER_QUANTITIES = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]
_ORDER_YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026]
_SUPPLIERS = ["Supplier_A", "Supplier_B", "Supplier_C", "Supplier_D"]
_MACHINE_TYPES = ["CNC_3axis", "CNC_5axis", "Manual_Mill", "Laser_Cut"]

# Multiplier applied to the production-cost component per machine type --
# an illustrative relative-difficulty factor (5-axis CNC costs more per
# part than laser cutting), not sourced from a real shop rate card.
_MACHINE_TYPE_FACTOR = {
    "CNC_3axis": 1.00,
    "CNC_5axis": 1.35,
    "Manual_Mill": 0.80,
    "Laser_Cut": 0.60,
}

# Arbitrary-but-consistent per-supplier price premium/discount. There is no
# real supplier pricing data behind this -- it exists purely so "supplier"
# is a learnable feature rather than pure noise, since the paper's feature
# list includes supplier. Documented here so this isn't mistaken for real
# supplier pricing in the report.
_SUPPLIER_PREMIUM = {
    "Supplier_A": 1.00,
    "Supplier_B": 1.05,
    "Supplier_C": 0.97,
    "Supplier_D": 1.10,
}

# Production-cost formula constants, in INR -- illustrative figures, not
# a real shop rate card (same spirit as the flat machining fee this
# project's old formula-based estimate_cost() used before it moved here).
_BASE_SETUP_FEE_INR = 2000.0
_PER_FACE_COST_INR = 45.0
_FINISHING_RATE_INR_PER_M2 = 15000.0

# Batch-size discount tiers: per-unit setup/overhead amortizes better at
# larger batch sizes, same tiered shape used elsewhere in this project
# before estimate_cost() moved to this ML model (see git history of
# cad_adapters/base_adapter.py).
_QUANTITY_DISCOUNT_TIERS = [(100, 0.80), (10, 0.90), (1, 1.00)]

# Simple year-over-year cost inflation applied relative to 2020.
_ANNUAL_INFLATION_RATE = 0.03

# Multiplicative Gaussian noise (5% std dev) layered on the final cost, to
# imitate the estimation variance real cost data would have.
_NOISE_STD = 0.05

def _quantity_multiplier(quantity: int) -> float:
    for threshold, multiplier in _QUANTITY_DISCOUNT_TIERS:
        if quantity >= threshold:
            return multiplier
    return 1.0


def _generate_row(rng: random.Random, np_rng: np.random.Generator, materials: list[dict]) -> dict:
    material = rng.choice(materials)
    density_kg_m3 = material["density_kg_m3"]
    cost_per_kg = material["cost_per_kg"]

    volume_m3 = np_rng.uniform(*_VOLUME_M3_RANGE)
    # Surface area is sampled around the value a compact solid of this
    # volume would have (~ volume^(2/3)) times a random "shape irregularity"
    # factor, so it stays roughly physically plausible while still varying
    # independently of volume, as a real part's surface area would.
    base_surface_area = volume_m3 ** (2 / 3)
    surface_area_m2 = base_surface_area * np_rng.uniform(3.0, 12.0)
    face_count = np_rng.integers(_FACE_COUNT_RANGE[0], _FACE_COUNT_RANGE[1] + 1)

    order_quantity = rng.choice(_ORDER_QUANTITIES)
    order_year = rng.choice(_ORDER_YEARS)
    supplier = rng.choice(_SUPPLIERS)
    machine_type = rng.choice(_MACHINE_TYPES)

    mass_kg = volume_m3 * density_kg_m3

    # --- Ground-truth cost formula (documented for the report) ---
    # 1. Material cost: real mass x real materials.csv cost_per_kg.
    material_cost_inr = mass_kg * cost_per_kg

    # 2. Production/process cost: a flat setup fee, plus a per-face
    #    machining cost (more faces ~ more machining operations), plus a
    #    finishing cost proportional to surface area, all scaled by a
    #    machine-type difficulty factor.
    production_cost_inr = (
        _BASE_SETUP_FEE_INR
        + _PER_FACE_COST_INR * face_count
        + _FINISHING_RATE_INR_PER_M2 * surface_area_m2
    ) * _MACHINE_TYPE_FACTOR[machine_type]

    # 3. Batch discount: larger order quantities amortize setup/overhead
    #    better, see _QUANTITY_DISCOUNT_TIERS above.
    quantity_multiplier = _quantity_multiplier(order_quantity)

    # 4. Year-over-year inflation relative to 2020, and a fixed
    #    per-supplier price premium/discount (both synthetic, see module
    #    docstring for _SUPPLIER_PREMIUM).
    inflation_factor = 1.0 + _ANNUAL_INFLATION_RATE * (order_year - 2020)
    supplier_factor = _SUPPLIER_PREMIUM[supplier]

    unit_cost_before_noise = (
        (material_cost_inr + production_cost_inr)
        * quantity_multiplier
        * inflation_factor
        * supplier_factor
    )

    # 5. Noise: +/-5% multiplicative Gaussian noise, imitating the
    #    estimation variance real-world cost data would have.
    noise_factor = max(np_rng.normal(1.0, _NOISE_STD), 0.5)
    cost_inr = max(unit_cost_before_noise * noise_factor, 0.0)

    return {
        "material_name": material["material_name"],
        "material_type": infer_material_type(material["material_name"]),
        "density_kg_m3": density_kg_m3,
        "volume_m3": volume_m3,
        "surface_area_m2": surface_area_m2,
        "face_count": int(face_count),
        "mass_kg": mass_kg,
        "order_quantity": order_quantity,
        "order_year": order_year,
        "supplier": supplier,
        "machine_type": machine_type,
        "cost_inr": round(cost_inr, 2),
    }


def generate_dataset(num_rows: int = _NUM_ROWS, seed: int = _RANDOM_SEED) -> pd.DataFrame:
    all_materials = load_materials()
    # Only materials with both a real density and a real cost_per_kg can
    # ground a row's target cost -- materials.csv leaves many non-metal
    # cost_per_kg cells blank (see fill_metal_costs.py), so those are
    # excluded rather than substituting a made-up cost.
    priced_materials = [
        m
        for m in all_materials.values()
        if m["density_kg_m3"] is not None
        and m["cost_per_kg"] is not None
        and m["cost_per_kg"] <= _MAX_COST_PER_KG_INR
    ]
    if not priced_materials:
        raise ValueError(
            "No materials in materials.csv have both density_kg_m3 and "
            "cost_per_kg filled in -- run fill_metal_costs.py first."
        )

    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    rows = [_generate_row(rng, np_rng, priced_materials) for _ in range(num_rows)]
    return pd.DataFrame(rows)


def main() -> None:
    df = generate_dataset()
    df.to_csv(_OUTPUT_CSV_PATH, index=False)
    print(f"Generated {len(df)} synthetic rows -> {_OUTPUT_CSV_PATH}")
    print(f"Priced materials available: {len(df['material_name'].unique())} unique used")
    print(df.describe(include="all").transpose())


if __name__ == "__main__":
    main()
