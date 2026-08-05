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


def estimate_cnc_machining_cost(feature_count: int) -> ProductionCostResult:
    """CNC Machining production cost, per unit.

    estimated_machining_time_minutes = SETUP_TIME_MINUTES +
        (PER_FEATURE_MACHINING_MINUTES * feature_count)
    production_cost_per_unit = (estimated_machining_time / 60) *
        (MACHINE_HOURLY_RATE + LABOR_HOURLY_RATE)

    `feature_count` is approximated by the part's face count (see
    CadAdapter.get_face_count()) -- more faces generally means more
    distinct machining operations were needed to produce the part. This is
    an approximation, not a literal count of machining operations from a
    CAM program.
    """
    machining_time_minutes = CNC_SETUP_TIME_MINUTES + (
        CNC_PER_FEATURE_MACHINING_MINUTES * feature_count
    )
    machining_time_hours = machining_time_minutes / 60.0
    production_cost = machining_time_hours * (
        CNC_MACHINE_HOURLY_RATE_INR + CNC_LABOR_HOURLY_RATE_INR
    )

    return ProductionCostResult(
        process="CNC Machining",
        production_cost_inr=round(production_cost, 2),
        breakdown={
            "feature_count": feature_count,
            "estimated_machining_time_minutes": round(machining_time_minutes, 2),
            "machine_hourly_rate_inr": CNC_MACHINE_HOURLY_RATE_INR,
            "labor_hourly_rate_inr": CNC_LABOR_HOURLY_RATE_INR,
        },
        assumptions=(
            f"CNC Machining: {CNC_SETUP_TIME_MINUTES} min setup + "
            f"{CNC_PER_FEATURE_MACHINING_MINUTES} min/feature x "
            f"{feature_count} features (approximated from face count) = "
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
) -> ProductionCostResult:
    """Dispatch to the process-specific production cost formula.

    `process` must be one of PROCESSES ("CNC Machining", "Injection
    Molding", "Sheet Metal"). Only the arguments relevant to the chosen
    process are used; the rest are ignored (kept as keyword args so
    callers can pass one unified geometry dict regardless of process).
    """
    if process == "CNC Machining":
        return estimate_cnc_machining_cost(feature_count)
    if process == "Injection Molding":
        return estimate_injection_molding_cost(volume_m3, order_quantity)
    if process == "Sheet Metal":
        return estimate_sheet_metal_cost(bounding_box_mm, bend_count)
    raise ValueError(
        f"Unknown manufacturing_process {process!r}; must be one of {PROCESSES}"
    )
