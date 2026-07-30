"""SolidWorks implementation of the CadAdapter interface.

Will drive SolidWorks via its COM API (through pywin32) to read the
active document's mass properties, material, and feature tree. Not yet
implemented — see cad_adapters/base_adapter.py for the contract this
class must fulfil.
"""

from cad_adapters.base_adapter import CadAdapter, Feature, MaterialInfo, PartInfo


class SolidWorksAdapter(CadAdapter):
    """CadAdapter implementation backed by a running SolidWorks instance."""

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
