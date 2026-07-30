"""SolidWorks implementation of the CadAdapter interface.

Drives a running SolidWorks instance via its COM API (through pywin32) to
read the active document's identity, mass properties, material, and
feature tree. Requires SolidWorks to be installed and running with a part
or assembly open; this module only runs on Windows.
"""

import pythoncom
import win32com.client
from win32com.client import CastTo

from cad_adapters.base_adapter import CadAdapter, Feature, MaterialInfo, PartInfo

# swDocumentTypes_e
_DOC_TYPE_NAMES = {
    1: "part",
    2: "assembly",
    3: "drawing",
}

# swLengthUnit_e (values returned by GetUserPreferenceIntegerValue for
# swUnitsLinear)
_LENGTH_UNIT_NAMES = {
    0: "mm",
    1: "cm",
    2: "m",
    3: "in",
    4: "ft",
    5: "ft/in",
    6: "angstrom",
    7: "nm",
    8: "micron",
}

# swUserPreferenceIntegerValue_e.swUnitsLinear
_SW_UNITS_LINEAR = 1


class SolidWorksAdapter(CadAdapter):
    """CadAdapter implementation backed by a running SolidWorks instance.

    Connects to an already-running copy of SolidWorks (starting a new one
    if none is found) and reads data from whichever document is currently
    active in the session.
    """

    def __init__(self) -> None:
        self._sw_app = None

    def connect(self) -> bool:
        """Attach to a running SolidWorks instance, launching one if needed."""
        try:
            self._sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        except pythoncom.com_error:
            try:
                self._sw_app = win32com.client.Dispatch("SldWorks.Application")
                self._sw_app.Visible = True
            except pythoncom.com_error:
                self._sw_app = None
                return False
        return True

    def _get_active_doc(self):
        if self._sw_app is None:
            raise RuntimeError("Not connected to SolidWorks. Call connect() first.")
        active_doc = self._sw_app.ActiveDoc
        if active_doc is None:
            raise RuntimeError("No part or assembly is currently open in SolidWorks.")
        return active_doc

    def get_current_part_info(self) -> PartInfo:
        model = self._get_active_doc()
        ext = model.Extension

        doc_type = _DOC_TYPE_NAMES.get(model.GetType(), "unknown")
        units_code = ext.GetUserPreferenceIntegerValue(_SW_UNITS_LINEAR)
        units = _LENGTH_UNIT_NAMES.get(units_code, "unknown")

        return PartInfo(
            name=model.GetTitle(),
            file_path=model.GetPathName(),
            part_type=doc_type,
            units=units,
        )

    def get_mass(self) -> float:
        model = self._get_active_doc()
        mass_property = model.Extension.CreateMassProperty()
        mass_property.UseSystemUnits = True  # forces SI units: kg, m
        return mass_property.Mass

    def get_material(self) -> MaterialInfo:
        model = self._get_active_doc()

        part_doc = CastTo(model, "IPartDoc")
        if part_doc is None:
            raise RuntimeError(
                "Material lookup is only supported for parts, not assemblies "
                "or drawings."
            )

        config_name = model.ConfigurationManager.ActiveConfiguration.Name
        material_name, _database_path = part_doc.GetMaterialPropertyName2(
            config_name, None
        )

        mass_property = model.Extension.CreateMassProperty()
        mass_property.UseSystemUnits = True  # kg/m^3
        density = mass_property.Density

        return MaterialInfo(
            name=material_name or "Not specified",
            density_kg_m3=density,
        )

    def get_features(self) -> list[Feature]:
        model = self._get_active_doc()
        features: list[Feature] = []

        feat = model.FirstFeature()
        while feat is not None:
            features.append(
                Feature(
                    name=feat.Name,
                    feature_type=feat.GetTypeName2(),
                    suppressed=self._is_suppressed(feat),
                )
            )
            feat = feat.GetNextFeature()

        return features

    @staticmethod
    def _is_suppressed(feat) -> bool:
        """Best-effort read of a feature's suppression state.

        GetSuppression2 returns a swFeatureSuppressionState_e code; any
        value other than "unsuppressed" is treated as suppressed. Falls
        back to False if the active document type doesn't support this
        query (e.g. some drawing view features).
        """
        try:
            return feat.GetSuppression2() not in (1, 3)
        except Exception:
            return False
