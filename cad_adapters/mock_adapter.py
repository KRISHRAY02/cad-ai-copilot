"""Mock implementation of the CadAdapter interface.

Provides realistic synthetic/hardcoded part data so the AI orchestrator,
MCP server, and UI can be developed and tested without SolidWorks (or any
CAD software) running. The sample data models a simple machined aluminum
mounting bracket.
"""

from cad_adapters.base_adapter import CadAdapter, Feature, MaterialInfo, PartInfo
from cad_adapters.dfm_checks import (
    MAX_HOLE_DEPTH_TO_DIAMETER_RATIO,
    MIN_HOLE_DIAMETER_MM,
    MIN_TOLERANCE_BAND_MM,
    make_finding,
)


class MockAdapter(CadAdapter):
    """CadAdapter implementation backed by hardcoded sample data.

    Simulates a single open part — a machined aluminum mounting bracket —
    so the rest of the application can be built and tested without any
    CAD software installed or running. `connect()` never fails.
    """

    def __init__(self) -> None:
        self._connected = False

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

    def connect(self) -> bool:
        self._connected = True
        return True

    def get_current_part_info(self) -> PartInfo:
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
