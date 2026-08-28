"""Process-aware production cost formulas for estimate_cost().

Real part cost = Material Cost + Production Cost. Material cost is mass *
cost_per_kg (materials.csv, see materials_db.py); this module supplies the
Production Cost half -- the part of the price that comes from *making* the
part, not the raw stock it's made from -- following the general shape of
Nirmalakumari K. et al., "Efficient Method for Product Cost Estimation
using Artificial Intelligence Techniques," 2025 3rd ICAECA, DOI:
10.1109/ICAECA63854.2025.11012624, extended here with an explicit,
process-specific formula per manufacturing process instead of one flat
number.

**Every _RATE/_COST constant below marked "PLACEHOLDER" is an illustrative
default, not a researched figure.** They exist so the formulas run
end-to-end during development. Before citing any cost produced by this
module in a report, replace them with real shop rates / tooling quotes /
material-supplier cutting rates for your region -- do not present the
defaults as accurate.

What each formula represents, in plain language (for the Implementation
section of a report):

- **CNC Machining**: cost is driven by *time on the machine plus the
  machinist's time*. Setup time is the fixed cost of clamping the stock,
  zeroing the machine, and loading the program -- it happens once per job
  regardless of part complexity. Per-feature machining time is the
  variable cost: every additional hole, pocket, or face the tool has to
  cut adds tool-path time. Multiplying total time by (machine rate + labor
  rate) reflects that a CNC machine and its operator are both "occupied",
  and therefore both billed, for the same stretch of time.
  Hole Wizard holes get their own per-hole time estimate instead of the
  flat per-feature number (see estimate_cnc_machining_cost's docstring for
  why depth:diameter ratio is used as the time driver); every other
  feature type still uses the flat per-feature estimate, grouped into one
  "Other Features" bucket.
- **Injection Molding**: cost has two very different drivers. Per-unit
  machine cost (cycle time x machine rate) is the recurring cost of one
  "shot" -- injecting, cooling, and ejecting one part -- which scales with
  part size/volume because bigger/thicker parts take longer to cool.
  Tooling cost is the one-time, large upfront cost of cutting the steel
  mold itself; it doesn't recur per part, so it's *amortized* (divided) by
  the order quantity -- this is why injection molding only becomes cheap
  per-unit at high volumes, and is why the paper's order-quantity feature
  matters so much for this process specifically.
- **Sheet Metal**: cost is driven by how much material has to be cut and
  how many bends have to be formed. Cutting cost scales with the length of
  the cut path (a laser/plasma/punch head moving along that path takes
  time proportional to its length). Bend cost is per-operation: each bend
  is a separate press-brake setup and stroke, regardless of how long the
  bend line is, so it's charged per bend rather than per millimeter.
"""

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# CNC Machining
# ---------------------------------------------------------------------------

CNC_SETUP_TIME_MINUTES = 15.0
CNC_PER_FEATURE_MACHINING_MINUTES = 4.0

# PLACEHOLDER -- fixed per-hole overhead (seconds converted to minutes): tool
# approach, spot-drill/center-drill touch-off, and retract, which every
# drilled hole incurs regardless of its size.
CNC_HOLE_BASE_MACHINING_MINUTES = 0.5

# PLACEHOLDER -- additional minutes per unit of depth:diameter ratio. See
# estimate_cnc_machining_cost()'s docstring for why this ratio (not depth or
# diameter alone) is used as the per-hole time driver.
CNC_HOLE_DEPTH_TO_DIAMETER_MINUTES_PER_RATIO = 0.3

# PLACEHOLDER -- replace with your shop's real machine hourly rate (INR/hr)
# for your region/market before citing any results computed from this.
CNC_MACHINE_HOURLY_RATE_INR = 800.0

# PLACEHOLDER -- replace with a real machinist labor hourly rate (INR/hr)
# for your region/market before citing any results computed from this.
CNC_LABOR_HOURLY_RATE_INR = 400.0


# ---------------------------------------------------------------------------
# Injection Molding
# ---------------------------------------------------------------------------

# PLACEHOLDER -- replace with a real injection molding machine hourly rate
# (INR/hr) for your region/market before citing any results computed from
# this.
IM_MACHINE_HOURLY_RATE_INR = 1500.0

# PLACEHOLDER -- baseline cycle time (seconds) for a small part: mold close
# + injection + hold + eject, before any size adjustment.
IM_CYCLE_TIME_SECONDS_BASE = 15.0

# PLACEHOLDER -- additional cycle time (seconds) per cubic centimeter of
# part volume, standing in for the fact that a bigger/thicker shot takes
# longer to cool before it can be ejected without warping.
IM_CYCLE_TIME_SECONDS_PER_CM3 = 0.4

# PLACEHOLDER -- one-time steel mold/tooling cost (INR), amortized across
# the order quantity. Real tooling quotes vary by orders of magnitude with
# part size/complexity/cavity count -- this is a single illustrative
# number, not a quote.
IM_TOOLING_COST_INR = 500_000.0


# ---------------------------------------------------------------------------
# Sheet Metal
# ---------------------------------------------------------------------------

# PLACEHOLDER -- replace with a real laser/plasma/punch cutting rate
# (INR per mm of cut length) for your region/market.
SM_CUTTING_RATE_PER_MM_INR = 2.5

# PLACEHOLDER -- replace with a real per-bend press-brake operation cost
# (INR/bend) for your region/market.
SM_COST_PER_BEND_INR = 50.0

PROCESSES = ("CNC Machining", "Injection Molding", "Sheet Metal")


@dataclass
class ProductionCostResult:
    process: str
    production_cost_inr: float
    breakdown: dict
    assumptions: str


def _hourly_rate_to_cost_inr(minutes: float) -> float:
    return (minutes / 60.0) * (CNC_MACHINE_HOURLY_RATE_INR + CNC_LABOR_HOURLY_RATE_INR)


def estimate_cnc_machining_cost(
    hole_features: list[dict] | None = None,
    other_feature_count: int = 0,
) -> ProductionCostResult:
    """CNC Machining production cost, per unit -- with per-hole attribution.

    Every feature used to be charged the same flat
    PER_FEATURE_MACHINING_MINUTES, which gives nothing meaningful to rank
    or highlight as a "cost driver". Hole Wizard holes now get their own
    individually estimated machining time from real geometry (diameter,
    depth); every other feature type still uses the old flat per-feature
    estimate, grouped into a single "Other Features" bucket (labeled as a
    flat estimate, not a per-feature one, in the returned breakdown).

    `hole_features` is a list of dicts, each with at least "feature_name",
    "diameter_mm" (float or None), and "depth_mm" (float or None) -- see
    CadAdapter.get_hole_features(). `other_feature_count` is the count of
    every remaining (non-hole) feature, billed at the flat
    CNC_PER_FEATURE_MACHINING_MINUTES rate as before.

    Per-hole time formula:
        estimated_time_minutes = CNC_HOLE_BASE_MACHINING_MINUTES +
            (depth_mm / diameter_mm) * CNC_HOLE_DEPTH_TO_DIAMETER_MINUTES_PER_RATIO

    **Why depth:diameter ratio, not depth or diameter alone**: a drill
    doesn't slow down because a hole is merely deep, or merely narrow -- it
    slows down because of how deep it is *relative to* its own diameter.
    A 20mm-deep hole is trivial at 10mm diameter but is a "deep hole" at
    2mm diameter. Three real machining effects all scale with this ratio,
    not with depth or diameter in isolation:
      1. Chip evacuation -- flutes have to carry chips back out along the
         same channel the drill bit occupies; a narrower channel packs
         chips faster per unit of depth, forcing more peck-drilling
         retraction cycles (each one extra time) as the ratio grows.
      2. Tool deflection/wander -- a thin, long drill bit flexes under
         cutting load; shops compensate with reduced feed rate as
         depth:diameter increases, directly trading speed for straightness.
      3. Heat buildup -- a narrow flute channel also carries less coolant,
         so deep/narrow holes need slower feeds to avoid overheating the
         bit.
    This project's own DFM hole check (see
    cad_adapters/dfm_checks.py::MAX_HOLE_DEPTH_TO_DIAMETER_RATIO) already
    uses this exact ratio as the standard manufacturability threshold for
    "is this hole a problem" -- reusing it here as the cost driver keeps
    the same physical justification for "is this hole expensive" that the
    DFM check already relies on for "is this hole risky", rather than
    inventing a second, unrelated formula.

    A hole missing depth data (e.g. a "through all" hole, whose real depth
    depends on part thickness rather than a fixed feature parameter -- see
    _check_holes in solidworks_adapter.py) falls back to the flat
    CNC_PER_FEATURE_MACHINING_MINUTES estimate for that hole specifically,
    and is marked is_flat_estimate=True in its breakdown entry so this
    fallback is visible rather than silently blended in.
    """
    hole_features = hole_features or []

    hole_entries = []
    hole_time_total = 0.0
    for hole in hole_features:
        name = hole.get("feature_name")
        diameter_mm = hole.get("diameter_mm")
        depth_mm = hole.get("depth_mm")

        if diameter_mm and depth_mm:
            ratio = depth_mm / diameter_mm
            time_minutes = CNC_HOLE_BASE_MACHINING_MINUTES + (
                ratio * CNC_HOLE_DEPTH_TO_DIAMETER_MINUTES_PER_RATIO
            )
            is_flat_estimate = False
        else:
            # Missing diameter or depth (e.g. a "through all" hole) -- fall
            # back to the flat per-feature estimate for this hole only,
            # rather than guessing a depth.
            ratio = None
            time_minutes = CNC_PER_FEATURE_MACHINING_MINUTES
            is_flat_estimate = True

        hole_time_total += time_minutes
        hole_entries.append(
            {
                "feature_name": name,
                "diameter_mm": diameter_mm,
                "depth_mm": depth_mm,
                "depth_to_diameter_ratio": round(ratio, 2) if ratio is not None else None,
                "estimated_time_minutes": round(time_minutes, 3),
                "estimated_cost_inr": round(_hourly_rate_to_cost_inr(time_minutes), 2),
                "is_flat_estimate": is_flat_estimate,
            }
        )

    other_time_minutes = CNC_PER_FEATURE_MACHINING_MINUTES * other_feature_count
    other_entry = {
        "label": "Other Features (flat per-feature estimate, not individually modeled)",
        "count": other_feature_count,
        "estimated_time_minutes": round(other_time_minutes, 3),
        "estimated_cost_inr": round(_hourly_rate_to_cost_inr(other_time_minutes), 2),
        "is_flat_estimate": True,
    }

    setup_entry = {
        "label": "Setup Time (fixed per-job cost, not per feature)",
        "estimated_time_minutes": CNC_SETUP_TIME_MINUTES,
        "estimated_cost_inr": round(_hourly_rate_to_cost_inr(CNC_SETUP_TIME_MINUTES), 2),
    }

    machining_time_minutes = CNC_SETUP_TIME_MINUTES + hole_time_total + other_time_minutes
    production_cost = _hourly_rate_to_cost_inr(machining_time_minutes)

    feature_count = len(hole_entries) + other_feature_count

    return ProductionCostResult(
        process="CNC Machining",
        production_cost_inr=round(production_cost, 2),
        breakdown={
            "feature_count": feature_count,
            "estimated_machining_time_minutes": round(machining_time_minutes, 2),
            "machine_hourly_rate_inr": CNC_MACHINE_HOURLY_RATE_INR,
            "labor_hourly_rate_inr": CNC_LABOR_HOURLY_RATE_INR,
            "setup": setup_entry,
            "holes": hole_entries,
            "other_features": other_entry,
        },
        assumptions=(
            f"CNC Machining: {CNC_SETUP_TIME_MINUTES} min setup + "
            f"{len(hole_entries)} Hole Wizard hole(s) individually estimated "
            f"from real diameter/depth (base {CNC_HOLE_BASE_MACHINING_MINUTES} "
            f"min + depth:diameter ratio x "
            f"{CNC_HOLE_DEPTH_TO_DIAMETER_MINUTES_PER_RATIO} min/ratio) + "
            f"{other_feature_count} other feature(s) at the flat "
            f"{CNC_PER_FEATURE_MACHINING_MINUTES} min/feature estimate = "
            f"{machining_time_minutes:.1f} min, billed at "
            f"Rs {CNC_MACHINE_HOURLY_RATE_INR}/hr machine + "
            f"Rs {CNC_LABOR_HOURLY_RATE_INR}/hr labor "
            "(PLACEHOLDER rates -- replace with real shop rates)."
        ),
    )


def estimate_injection_molding_cost(
    volume_m3: float, order_quantity: int
) -> ProductionCostResult:
    """Injection Molding production cost, per unit.

    cycle_time_seconds = CYCLE_TIME_SECONDS_BASE +
        (volume_cm3 * CYCLE_TIME_SECONDS_PER_CM3)
    machine_cost_per_shot = (cycle_time_seconds / 3600) * MACHINE_HOURLY_RATE
    production_cost_per_unit = machine_cost_per_shot +
        (TOOLING_COST / quantity)
    """
    order_quantity = max(order_quantity, 1)
    volume_cm3 = volume_m3 * 1e6  # m^3 -> cm^3
    cycle_time_seconds = IM_CYCLE_TIME_SECONDS_BASE + (
        volume_cm3 * IM_CYCLE_TIME_SECONDS_PER_CM3
    )
    machine_cost_per_shot = (cycle_time_seconds / 3600.0) * IM_MACHINE_HOURLY_RATE_INR
    tooling_cost_per_unit = IM_TOOLING_COST_INR / order_quantity
    production_cost = machine_cost_per_shot + tooling_cost_per_unit

    return ProductionCostResult(
        process="Injection Molding",
        production_cost_inr=round(production_cost, 2),
        breakdown={
            "volume_cm3": round(volume_cm3, 2),
            "cycle_time_seconds": round(cycle_time_seconds, 2),
            "machine_cost_per_shot_inr": round(machine_cost_per_shot, 2),
            "tooling_cost_total_inr": IM_TOOLING_COST_INR,
            "tooling_cost_per_unit_inr": round(tooling_cost_per_unit, 2),
            "order_quantity_used_for_amortization": order_quantity,
        },
        assumptions=(
            f"Injection Molding: {cycle_time_seconds:.1f}s cycle time "
            f"({IM_CYCLE_TIME_SECONDS_BASE}s base + "
            f"{IM_CYCLE_TIME_SECONDS_PER_CM3}s/cm3 x {volume_cm3:.1f} cm3) at "
            f"Rs {IM_MACHINE_HOURLY_RATE_INR}/hr machine rate, plus "
            f"Rs {IM_TOOLING_COST_INR:,.0f} tooling amortized over "
            f"{order_quantity} units (PLACEHOLDER rates/tooling cost -- "
            "replace with real figures)."
        ),
    )


def estimate_sheet_metal_cost(
    bounding_box_mm: tuple[float, float, float], bend_count: int
) -> ProductionCostResult:
    """Sheet Metal production cost, per unit.

    cutting_length_mm is approximated as the perimeter of the two largest
    bounding-box dimensions (2 * (length + width)) -- i.e. treating the
    part's footprint as a rectangle. This is a **documented
    approximation**, not a true flat-pattern cut-length measurement (which
    would require unfolding the actual sheet metal flat pattern and
    summing every real cut edge, including internal cutouts). It will
    under-estimate cutting length for parts with internal holes/slots or a
    non-rectangular outline.

    production_cost_per_unit = (cutting_length_mm * CUTTING_RATE_PER_MM) +
        (bend_count * COST_PER_BEND)
    """
    dims_sorted = sorted(bounding_box_mm, reverse=True)
    length_mm, width_mm = dims_sorted[0], dims_sorted[1]
    cutting_length_mm = 2 * (length_mm + width_mm)

    cutting_cost = cutting_length_mm * SM_CUTTING_RATE_PER_MM_INR
    bending_cost = bend_count * SM_COST_PER_BEND_INR
    production_cost = cutting_cost + bending_cost

    return ProductionCostResult(
        process="Sheet Metal",
        production_cost_inr=round(production_cost, 2),
        breakdown={
            "bounding_box_mm": [round(d, 2) for d in bounding_box_mm],
            "cutting_length_mm_approx": round(cutting_length_mm, 2),
            "cutting_cost_inr": round(cutting_cost, 2),
            "bend_count": bend_count,
            "bending_cost_inr": round(bending_cost, 2),
        },
        assumptions=(
            f"Sheet Metal: cutting length approximated as the bounding-box "
            f"footprint perimeter (2 x ({length_mm:.1f}mm + "
            f"{width_mm:.1f}mm) = {cutting_length_mm:.1f}mm -- NOT a true "
            f"flat-pattern cut length) at Rs {SM_CUTTING_RATE_PER_MM_INR}/mm, "
            f"plus {bend_count} bend(s) at Rs {SM_COST_PER_BEND_INR}/bend "
            "(PLACEHOLDER rates -- replace with real figures)."
        ),
    )


def estimate_production_cost(
    process: str,
    *,
    feature_count: int = 0,
    volume_m3: float = 0.0,
    order_quantity: int = 1,
    bounding_box_mm: tuple[float, float, float] = (0.0, 0.0, 0.0),
    bend_count: int = 0,
    hole_features: list[dict] | None = None,
    other_feature_count: int | None = None,
) -> ProductionCostResult:
    """Dispatch to the process-specific production cost formula.

    `process` must be one of PROCESSES ("CNC Machining", "Injection
    Molding", "Sheet Metal"). Only the arguments relevant to the chosen
    process are used; the rest are ignored (kept as keyword args so
    callers can pass one unified geometry dict regardless of process).

    For CNC Machining specifically: callers that have real per-hole
    geometry (diameter/depth) should pass `hole_features` +
    `other_feature_count` for per-hole cost attribution (see
    estimate_cnc_machining_cost). Callers that don't (e.g. the ML cost
    model's synthetic training data, which has no per-hole geometry) can
    keep passing just `feature_count` as before -- every feature is then
    treated as part of the flat "Other Features" bucket, reproducing the
    old flat-formula behavior exactly.
    """
    if process == "CNC Machining":
        if hole_features is not None or other_feature_count is not None:
            return estimate_cnc_machining_cost(
                hole_features=hole_features,
                other_feature_count=other_feature_count
                if other_feature_count is not None
                else feature_count,
            )
        return estimate_cnc_machining_cost(hole_features=[], other_feature_count=feature_count)
    if process == "Injection Molding":
        return estimate_injection_molding_cost(volume_m3, order_quantity)
    if process == "Sheet Metal":
        return estimate_sheet_metal_cost(bounding_box_mm, bend_count)
    raise ValueError(
        f"Unknown manufacturing_process {process!r}; must be one of {PROCESSES}"
    )
