"""Mock implementation of the CadAdapter interface.

Provides realistic synthetic/hardcoded part data so the AI orchestrator,
MCP server, and UI can be developed and tested without SolidWorks (or any
CAD software) running. The sample data models a simple machined aluminum
mounting bracket.
"""

import tempfile
from pathlib import Path

from cad_adapters.base_adapter import (
    AssemblyComponent,
    CadAdapter,
    Feature,
    MaterialInfo,
    PartInfo,
)
from cad_adapters.dfm_checks import (
    MAX_HOLE_DEPTH_TO_DIAMETER_RATIO,
    MIN_HOLE_DIAMETER_MM,
    MIN_TOLERANCE_BAND_MM,
    make_finding,
)

_PLACEHOLDER_SCREENSHOT_SIZE = (640, 480)
_PLACEHOLDER_SCREENSHOT_BG = (230, 234, 240)
_PLACEHOLDER_SCREENSHOT_TEXT_COLOR = (91, 107, 133)

_ASSEMBLY_ROOT = r"C:\CAD\Projects\demo_assembly"
_TOOLBOX_ROOT = r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS Data\browser\toolbox\ansi metric"


def _build_raw_assembly_components() -> list[dict]:
    """One dict per component *instance* in a small synthetic multi-level
    assembly, before aggregation -- demonstrates every case
    get_assembly_components() has to handle for real: a part repeated
    multiple times (Bracket x2, hardware), one part referenced under two
    different configurations with different mass/material (Base Plate),
    one nested sub-assembly (Hinge Assembly, level 1) whose own children
    are also captured, one suppressed component that must be skipped
    entirely (Old Bracket), one reference/envelope-only component that
    must also be skipped (Packaging Envelope), one hardware item that
    matches standard_hardware.csv (Hex Bolt, Hex Nut) and one that looks
    like Toolbox hardware but has no CSV entry yet (Washer) -- exercising
    the "Buy - price not available" path.
    """
    aluminum = MaterialInfo(name="6061 Alloy", density_kg_m3=2700.0, category="Aluminum")
    cast_alloy = MaterialInfo(
        name="201.0-T7 Insulated Mold Casting (SS)", density_kg_m3=2680.0, category="Aluminum Casting"
    )
    stainless = MaterialInfo(
        name="AISI 316 Stainless Steel Sheet (SS)", density_kg_m3=8000.0, category="Stainless Steel"
    )

    def instance(
        part_name, file_path, configuration, *, parent_assembly=None, level=0,
        suppressed=False, envelope=False, mass_kg=None, volume_m3=None,
        material=None, face_count=None, bend_count=0, bounding_box_mm=None,
    ):
        return {
            "part_name": part_name,
            "file_path": file_path,
            "configuration": configuration,
            "parent_assembly": parent_assembly,
            "level": level,
            "suppressed": suppressed,
            "envelope": envelope,
            "mass_kg": mass_kg,
            "volume_m3": volume_m3,
            "material": material,
            "face_count": face_count,
            "bend_count": bend_count,
            "bounding_box_mm": bounding_box_mm,
        }

    components = []

    # Bracket x2 (top level, Make) -- same part+config, aggregates to qty 2.
    for i in (1, 2):
        components.append(
            instance(
                "Bracket", rf"{_ASSEMBLY_ROOT}\bracket.sldprt", "Default",
                mass_kg=0.842, volume_m3=3.12e-4, material=aluminum,
                face_count=18, bounding_box_mm=(120.0, 80.0, 25.0),
            )
        )

    # Base Plate under two different configurations (Make) -- distinct BOM
    # rows since the configuration changes mass and material.
    components.append(
        instance(
            "Base Plate", rf"{_ASSEMBLY_ROOT}\base_plate.sldprt", "Default",
            mass_kg=1.5, volume_m3=5.6e-4, material=cast_alloy,
            face_count=12, bounding_box_mm=(150.0, 100.0, 10.0),
        )
    )
    components.append(
        instance(
            "Base Plate", rf"{_ASSEMBLY_ROOT}\base_plate.sldprt", "Heavy Duty",
            mass_kg=3.0, volume_m3=5.6e-4, material=stainless,
            face_count=12, bounding_box_mm=(150.0, 100.0, 14.0),
        )
    )

    # Suppressed component -- must never appear in get_assembly_components().
    components.append(
        instance(
            "Old Bracket (deprecated)", rf"{_ASSEMBLY_ROOT}\old_bracket.sldprt", "Default",
            suppressed=True,
        )
    )

    # Reference/envelope-only component (packaging clearance volume, not a
    # real manufacturable part) -- must also never appear in the output.
    components.append(
        instance(
            "Packaging Envelope", rf"{_ASSEMBLY_ROOT}\packaging_envelope.sldprt", "Default",
            envelope=True,
        )
    )

    # Standard hardware (Buy, priced via standard_hardware.csv) -- 4 bolts,
    # 4 nuts, both matching a row in standard_hardware.csv.
    for i in range(4):
        components.append(
            instance(
                "M6x20 Hex Bolt", rf"{_TOOLBOX_ROOT}\bolts and screws\hex bolt.sldprt", "Default",
                mass_kg=0.012, volume_m3=1.5e-6, face_count=6, bounding_box_mm=(6.0, 6.0, 20.0),
            )
        )
    for i in range(4):
        components.append(
            instance(
                "M6 Hex Nut", rf"{_TOOLBOX_ROOT}\nuts\hex nut.sldprt", "Default",
                mass_kg=0.003, volume_m3=4.0e-7, face_count=8, bounding_box_mm=(10.0, 10.0, 5.0),
            )
        )

    # Standard hardware (Toolbox path, but no standard_hardware.csv entry
    # yet) -- demonstrates "Buy - price not available", not a guessed cost.
    for i in range(2):
        components.append(
            instance(
                "M6 Washer", rf"{_TOOLBOX_ROOT}\washers\washer.sldprt", "Default",
                mass_kg=0.001, volume_m3=1.2e-7, face_count=4, bounding_box_mm=(12.0, 12.0, 1.5),
            )
        )

    # Nested sub-assembly: Hinge Assembly (level 1) containing its own
    # Make components -- Pin x2 and Bushing x1.
    for i in (1, 2):
        components.append(
            instance(
                "Pin", rf"{_ASSEMBLY_ROOT}\pin.sldprt", "Default",
                parent_assembly="Hinge Assembly", level=1,
                mass_kg=0.05, volume_m3=6.3e-6, material=aluminum,
                face_count=6, bounding_box_mm=(8.0, 8.0, 40.0),
            )
        )
    components.append(
        instance(
            "Bushing", rf"{_ASSEMBLY_ROOT}\bushing.sldprt", "Default",
            parent_assembly="Hinge Assembly", level=1,
            mass_kg=0.02, volume_m3=2.5e-6, material=cast_alloy,
            face_count=6, bounding_box_mm=(12.0, 12.0, 10.0),
        )
    )

    return components


class MockAdapter(CadAdapter):
    """CadAdapter implementation backed by hardcoded sample data.

    Simulates a single open part — a machined aluminum mounting bracket —
    so the rest of the application can be built and tested without any
    CAD software installed or running. `connect()` never fails.

    Pass `simulate_assembly=True` to instead simulate a small open
    assembly (see _build_raw_assembly_components() below) -- lets the
    assembly BOM/rollup feature (is_assembly(), get_assembly_components(),
    get_assembly_bom()) be demoed and tested without a real SolidWorks
    assembly open. The single-part sample data above is unaffected/unused
    in this mode; only the assembly-specific methods behave differently.
    """

    def __init__(self, simulate_assembly: bool = False) -> None:
        self._connected = False
        self._simulate_assembly = simulate_assembly

        self._part_info = PartInfo(
            name="mounting_bracket_v3",
            file_path=r"C:\CAD\Projects\mounting_bracket_v3.sldprt",
            part_type="part",
            units="mm",
        )

        self._material = MaterialInfo(
            name="6061 Alloy",
            density_kg_m3=2700.0,
            category="Aluminum",
        )

        self._mass_kg = 0.842
        self._face_count = 18
        self._bend_count = 0  # this sample part is a machined bracket, not sheet metal
        self._bounding_box_mm = (120.0, 80.0, 25.0)

        self._features = [
            Feature(
                name="Boss-Extrude1",
                feature_type="Extrude",
                suppressed=False,
                parameters={"depth_mm": 25.0, "draft_deg": 0.0},
            ),
            Feature(
                name="Sketch1",
                feature_type="Sketch",
                suppressed=False,
                parameters={"plane": "Front Plane"},
            ),
            Feature(
                name="Fillet1",
                feature_type="Fillet",
                suppressed=False,
                parameters={"radius_mm": 3.0, "edge_count": 4},
            ),
            Feature(
                name="Cut-Extrude1",
                feature_type="Extrude Cut",
                suppressed=False,
                parameters={"depth_mm": 25.0, "through_all": True},
            ),
            Feature(
                name="Mounting Holes",
                feature_type="Hole Wizard",
                suppressed=False,
                parameters={
                    "hole_type": "Counterbore",
                    "diameter_mm": 6.5,
                    "count": 4,
                },
            ),
            Feature(
                name="Chamfer1",
                feature_type="Chamfer",
                suppressed=True,
                parameters={"distance_mm": 1.0, "edge_count": 8},
            ),
            Feature(
                name="Mirror1",
                feature_type="Mirror",
                suppressed=False,
                parameters={"mirror_plane": "Right Plane"},
            ),
        ]

        self._assembly_info = PartInfo(
            name="demo_gearbox_assembly",
            file_path=r"C:\CAD\Projects\demo_assembly\demo_gearbox_assembly.sldasm",
            part_type="assembly",
            units="mm",
        )
        self._raw_assembly_components = _build_raw_assembly_components()

    def connect(self) -> bool:
        self._connected = True
        return True

    def is_assembly(self) -> bool:
        return self._simulate_assembly

    def get_current_part_info(self) -> PartInfo:
        if self._simulate_assembly:
            return self._assembly_info
        return self._part_info

    def get_mass(self) -> float:
        return self._mass_kg

    def get_mass_properties(self) -> dict:
        return {
            "mass_kg": self._mass_kg,
            "volume_m3": 3.12e-4,
            "surface_area_m2": 0.0421,
        }

    def get_material(self) -> MaterialInfo:
        return self._material

    def get_features(self) -> list[Feature]:
        return list(self._features)

    def get_face_count(self) -> int:
        return self._face_count

    def get_bend_count(self) -> int:
        return self._bend_count

    def get_bounding_box_mm(self) -> tuple[float, float, float]:
        return self._bounding_box_mm

    def get_hole_features(self) -> list[dict]:
        """Synthetic holes matching this bracket's own "Mounting Holes"
        Hole Wizard feature plus one hypothetical deep/narrow dowel hole,
        the same two holes used by run_dfm_check()'s demo data -- kept
        consistent so cost-driver demos and DFM demos tell the same story
        about this mock part.
        """
        return [
            {"feature_name": "Mounting Holes", "diameter_mm": 6.5, "depth_mm": 10.0},
            {"feature_name": "Dowel Pin Hole", "diameter_mm": 0.8, "depth_mm": 15.0},
        ]

    def highlight_feature(self, feature_id: str) -> dict:
        """No real CAD viewport exists for the mock adapter -- returns a
        simple success acknowledgment instead of actually selecting
        anything.
        """
        return {
            "success": True,
            "message": (
                f"[Mock Adapter -- no real CAD viewport] Would highlight "
                f"feature '{feature_id}'."
            ),
        }

    def get_assembly_components(self) -> list[AssemblyComponent]:
        """Aggregate the raw synthetic component list into one row per
        unique (file_path, configuration), skipping suppressed/envelope
        entries -- same aggregation logic a real adapter's assembly
        traversal has to do, just against hardcoded data instead of a
        live COM tree walk.
        """
        if not self._simulate_assembly:
            raise RuntimeError(
                "The current document is not an assembly -- construct "
                "MockAdapter(simulate_assembly=True) to use this."
            )

        aggregated: dict[tuple[str, str], dict] = {}
        order: list[tuple[str, str]] = []
        for raw in self._raw_assembly_components:
            if raw["suppressed"] or raw["envelope"]:
                continue
            key = (raw["file_path"], raw["configuration"])
            if key not in aggregated:
                aggregated[key] = dict(raw)
                aggregated[key]["quantity"] = 0
                order.append(key)
            aggregated[key]["quantity"] += 1

        return [
            AssemblyComponent(
                part_name=data["part_name"],
                file_path=data["file_path"],
                configuration=data["configuration"],
                quantity=data["quantity"],
                parent_assembly=data["parent_assembly"],
                level=data["level"],
                mass_kg=data["mass_kg"],
                volume_m3=data["volume_m3"],
                material=data["material"],
                face_count=data["face_count"],
                bend_count=data["bend_count"],
                bounding_box_mm=data["bounding_box_mm"],
            )
            for key in order
            for data in [aggregated[key]]
        ]

    def capture_screenshot(self, output_path: str | None = None) -> str:
        """Draws a simple placeholder image (no real CAD viewport exists
        for the mock adapter) labeled clearly as a placeholder, so a
        generated report is never mistaken for showing real part
        geometry.
        """
        from PIL import Image, ImageDraw

        if output_path is None:
            output_path = str(
                Path(tempfile.gettempdir()) / f"{self._part_info.name}_placeholder.png"
            )

        image = Image.new("RGB", _PLACEHOLDER_SCREENSHOT_SIZE, color=_PLACEHOLDER_SCREENSHOT_BG)
        draw = ImageDraw.Draw(image)
        lines = [
            "[Mock Adapter -- no CAD viewport]",
            self._part_info.name,
            "(placeholder image, not a real screenshot)",
        ]
        width, height = _PLACEHOLDER_SCREENSHOT_SIZE
        y = height // 2 - (len(lines) * 16) // 2
        for line in lines:
            bbox = draw.textbbox((0, 0), line)
            text_width = bbox[2] - bbox[0]
            draw.text(
                ((width - text_width) / 2, y),
                line,
                fill=_PLACEHOLDER_SCREENSHOT_TEXT_COLOR,
            )
            y += 20

        image.save(output_path)
        return output_path

    def run_dfm_check(self) -> list[dict]:
        """Synthetic DFM results demonstrating all three outcome types
        (flagged, pass, not_applicable) for demo purposes, including one
        "no such feature" not_applicable (draft) and one "curved
        surfaces" not_applicable (wall thickness) -- see
        cad_adapters/dfm_checks.py for the thresholds and
        cad_adapters/solidworks_adapter.py for what a real SolidWorks
        read of the same checks looks like.
        """
        findings = []

        # 1. Hole check: one good hole (pass), one too-small/too-deep
        # dowel hole (flagged), matching this bracket's "Mounting Holes"
        # Hole Wizard feature plus one hypothetical extra hole for demo
        # variety.
        findings.append(
            make_finding(
                "hole", "pass", feature="Mounting Holes",
                message="Hole diameter and depth-to-diameter ratio within limits.",
                diameter_mm=6.5, depth_mm=10.0,
            )
        )
        findings.append(
            make_finding(
                "hole", "flagged", feature="Dowel Pin Hole",
                message=(
                    f"diameter 0.80mm is below the {MIN_HOLE_DIAMETER_MM}mm "
                    f"minimum; depth:diameter ratio 18.8:1 exceeds the "
                    f"{MAX_HOLE_DEPTH_TO_DIAMETER_RATIO}:1 maximum"
                ),
                diameter_mm=0.8, depth_mm=15.0,
            )
        )

        # 2. Wall thickness: this bracket has a Fillet1 feature, i.e. it
        # has curved surfaces, so thickness can't be determined -- same
        # "not applicable" outcome a real curved SolidWorks part hits.
        findings.append(
            make_finding(
                "wall_thickness", "not_applicable",
                message="Wall thickness can't be determined for parts with curved surfaces.",
            )
        )

        # 3. Draft check: this mock part has no Draft feature at all.
        findings.append(
            make_finding(
                "draft_angle", "not_applicable",
                message="Not applicable to this part (no Draft feature present to measure).",
            )
        )

        # 4. Tolerance check: one tight/flagged dimension, one normal/pass
        # dimension, plus the part's general tolerance limit.
        findings.append(
            make_finding(
                "tolerance", "flagged", feature="D2@Boss-Extrude1",
                message=(
                    f"Tolerance band 0.0004mm is tighter than the "
                    f"{MIN_TOLERANCE_BAND_MM}mm threshold -- may increase "
                    "manufacturing cost."
                ),
                min_tolerance_mm=-0.0002, max_tolerance_mm=0.0002,
            )
        )
        findings.append(
            make_finding(
                "tolerance", "pass", feature="D1@Sketch1",
                message="Tolerance band within normal manufacturing limits.",
                min_tolerance_mm=-0.05, max_tolerance_mm=0.05,
            )
        )
        findings.append(
            make_finding(
                "tolerance", "pass", feature=None,
                message="General tolerance limit: ISO 2768-mK (medium).",
                general_tolerance_class="ISO 2768-mK",
            )
        )

        return findings
