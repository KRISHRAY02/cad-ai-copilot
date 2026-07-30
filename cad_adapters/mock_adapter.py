"""Mock implementation of the CadAdapter interface.

Provides realistic synthetic/hardcoded part data so the AI orchestrator,
MCP server, and UI can be developed and tested without SolidWorks (or any
CAD software) running. Not yet implemented — see
cad_adapters/base_adapter.py for the contract this class must fulfil.
"""

from cad_adapters.base_adapter import CadAdapter, Feature, MaterialInfo, PartInfo


class MockAdapter(CadAdapter):
    """CadAdapter implementation backed by hardcoded sample data."""

    def connect(self) -> bool:
        raise NotImplementedError

    def get_current_part_info(self) -> PartInfo:
        raise NotImplementedError

    def get_mass(self) -> float:
        raise NotImplementedError

    def get_material(self) -> MaterialInfo:
        raise NotImplementedError

    def get_features(self) -> list[Feature]:
        raise NotImplementedError
