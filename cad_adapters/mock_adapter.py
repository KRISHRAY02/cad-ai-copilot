"""Mock implementation of the CadAdapter interface.

Provides realistic synthetic/hardcoded part data so the AI orchestrator,
MCP server, and UI can be developed and tested without SolidWorks (or any
CAD software) running. The sample data models a simple machined aluminum
mounting bracket.
"""

from cad_adapters.base_adapter import CadAdapter, Feature, MaterialInfo, PartInfo


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
