"""SolidWorks implementation of the CadAdapter interface.

Drives a running SolidWorks instance via its COM API (through pywin32) to
read the active document's identity, mass properties, material, and
feature tree. Requires SolidWorks to be installed and running with a part
or assembly open; this module only runs on Windows.
"""

import pythoncom
import win32com.client

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


class SolidWorksConnectionError(RuntimeError):
    """Base class for failures connecting to SolidWorks."""


class SolidWorksNotRunningError(SolidWorksConnectionError):
    """Raised when SolidWorks could not be reached via COM at all."""


class NoDocumentOpenError(SolidWorksConnectionError):
    """Raised when SolidWorks is reachable but has no document open."""


class SolidWorksAdapter(CadAdapter):
    """CadAdapter implementation backed by a running SolidWorks instance.

    Connects to an already-running copy of SolidWorks and reads data from
    whichever document is currently active in the session.
    """

    def __init__(self) -> None:
        self._sw_app = None

    def connect(self) -> bool:
        """Attach to the running SolidWorks instance and verify a document is open.

        Raises:
            SolidWorksNotRunningError: SolidWorks isn't running/reachable via COM.
            NoDocumentOpenError: SolidWorks is running but has no document open.
        """
        try:
            self._sw_app = win32com.client.Dispatch("SldWorks.Application")
        except pythoncom.com_error as e:
            self._sw_app = None
            raise SolidWorksNotRunningError(
                "Could not connect to SolidWorks via COM. Make sure "
                "SolidWorks is installed and running, then try again."
            ) from e

        active_doc = self._sw_app.ActiveDoc
        if active_doc is None:
            raise NoDocumentOpenError(
                "Connected to SolidWorks, but no part, assembly, or drawing "
                "is currently open. Open a document in SolidWorks and try "
                "again."
            )

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

        doc_type = _DOC_TYPE_NAMES.get(model.GetType, "unknown")
        # GetUserPreferenceIntegerValue lives on the application object
        # (ISldWorks), not on the document's Extension.
        units_code = self._sw_app.GetUserPreferenceIntegerValue(_SW_UNITS_LINEAR)
        units = _LENGTH_UNIT_NAMES.get(units_code, "unknown")

        return PartInfo(
            name=model.GetTitle,
            file_path=model.GetPathName,
            part_type=doc_type,
            units=units,
        )

    def get_mass(self) -> float:
        model = self._get_active_doc()
        mass_property = model.Extension.CreateMassProperty
        mass_property.UseSystemUnits = True  # forces SI units: kg, m
        return mass_property.Mass

    def get_mass_properties(self) -> dict:
        """Return mass, volume, and surface area, in SI units.

        SolidWorks can't compute mass properties on a part with no solid
        bodies (e.g. a sketch-only or surface-only part) — CreateMassProperty
        raises a COM error in that case, so each property is read
        defensively and reported as None rather than crashing.
        """
        model = self._get_active_doc()
        mass_property = model.Extension.CreateMassProperty
        mass_property.UseSystemUnits = True  # forces SI units: kg, m, m^2

        properties = {}
        for key, attr in (
            ("mass_kg", "Mass"),
            ("volume_m3", "Volume"),
            ("surface_area_m2", "SurfaceArea"),
        ):
            try:
                properties[key] = getattr(mass_property, attr)
            except pythoncom.com_error:
                properties[key] = None

        return properties

    def get_material(self) -> MaterialInfo:
        model = self._get_active_doc()

        if _DOC_TYPE_NAMES.get(model.GetType) != "part":
            raise RuntimeError(
                "Material lookup is only supported for parts, not assemblies "
                "or drawings."
            )

        # GetMaterialPropertyName2 belongs to IPartDoc, but dynamic COM
        # dispatch resolves members by name against the underlying object
        # regardless of interface, so it can be called directly on `model`
        # without an explicit CastTo (which requires makepy/gencache
        # type-library binding that isn't set up in this environment).
        #
        # Its second argument is a ByRef output (database path). Without
        # gencache/makepy, dynamic dispatch has no type-library info to
        # marshal a plain str as byref, so it must be wrapped explicitly
        # as a byref VARIANT or the call fails with "Type mismatch".
        config_name = model.ConfigurationManager.ActiveConfiguration.Name
        database_path = win32com.client.VARIANT(
            pythoncom.VT_BYREF | pythoncom.VT_BSTR, ""
        )
        material_name = model.GetMaterialPropertyName2(config_name, database_path)

        mass_property = model.Extension.CreateMassProperty
        mass_property.UseSystemUnits = True  # kg/m^3
        density = mass_property.Density

        return MaterialInfo(
            name=material_name or "Not specified",
            density_kg_m3=density,
        )

    def get_features(self) -> list[Feature]:
        model = self._get_active_doc()
        features: list[Feature] = []

        feat = model.FirstFeature
        while feat is not None:
            features.append(
                Feature(
                    name=feat.Name,
                    feature_type=feat.GetTypeName2,
                    suppressed=self._is_suppressed(feat),
                )
            )
            feat = feat.GetNextFeature

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
            return feat.GetSuppression2 not in (1, 3)
        except Exception:
            return False
