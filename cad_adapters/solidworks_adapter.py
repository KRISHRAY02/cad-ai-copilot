"""SolidWorks implementation of the CadAdapter interface.

Drives a running SolidWorks instance via its COM API (through pywin32) to
read the active document's identity, mass properties, material, and
feature tree. Requires SolidWorks to be installed and running with a part
or assembly open; this module only runs on Windows.
"""

import math
import tempfile
from pathlib import Path

import pythoncom
import win32com.client

from cad_adapters.base_adapter import CadAdapter, Feature, MaterialInfo, PartInfo
from cad_adapters.dfm_checks import (
    MAX_HOLE_DEPTH_TO_DIAMETER_RATIO,
    MIN_DRAFT_ANGLE_DEG,
    MIN_HOLE_DIAMETER_MM,
    MIN_TOLERANCE_BAND_MM,
    MIN_WALL_THICKNESS_MM,
    make_finding,
)

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

_SCREENSHOT_WIDTH_PX = 800
_SCREENSHOT_HEIGHT_PX = 600

# GetTypeName2 strings for SOLIDWORKS Sheet Metal features that create a
# bend line, used by get_bend_count(). This counts *bend-producing
# features*, one count per feature -- not a true count of individual bend
# lines: an Edge-Flange or Miter-Flange applied along a multi-segment edge
# can produce more than one physical bend from a single feature, which
# this approximation would undercount. A true per-bend-line count would
# need to walk each body's bend table (ISheetMetalFolder /
# IFlatPatternFeatureData2), which isn't implemented here. "SketchBend"
# (Sketched Bend) is the one type guaranteed to be exactly one bend per
# feature.
_SHEET_METAL_BEND_FEATURE_TYPES = {
    "SketchBend",
    "Base-Flange",
    "Edge-Flange",
    "Miter-Flange",
    "Jog",
    "Lofted-Bend",
}


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
        self._get_active_doc()
        return True

    def _connect_com(self):
        """Initialize COM for the calling thread and attach to SolidWorks.

        pywin32 COM proxies are apartment-threaded: a proxy created on one
        thread can't be used from another. An MCP server dispatches each
        synchronous tool call onto a worker thread pool, so every call
        re-initializes COM and re-dispatches SldWorks.Application fresh
        for whichever thread is actually running, instead of reusing a
        proxy cached from a different thread. Dispatch to an
        already-running instance is cheap, so this adds negligible
        overhead per call.
        """
        pythoncom.CoInitialize()
        try:
            sw_app = win32com.client.Dispatch("SldWorks.Application")
        except pythoncom.com_error as e:
            raise SolidWorksNotRunningError(
                "Could not connect to SolidWorks via COM. Make sure "
                "SolidWorks is installed and running, then try again."
            ) from e
        self._sw_app = sw_app
        return sw_app

    def _get_active_doc(self):
        sw_app = self._connect_com()
        active_doc = sw_app.ActiveDoc
        if active_doc is None:
            raise NoDocumentOpenError(
                "Connected to SolidWorks, but no part, assembly, or drawing "
                "is currently open. Open a document in SolidWorks and try "
                "again."
            )
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
        return [
            Feature(
                name=feat.Name,
                feature_type=feat.GetTypeName2,
                suppressed=self._is_suppressed(feat),
            )
            for feat in self._iter_raw_features(model)
        ]

    @staticmethod
    def _iter_raw_features(model):
        """Yield each raw COM IFeature object in the feature tree, in
        modeling order. Shared by get_features() and the DFM checks below,
        which (unlike get_features()) need the actual COM object to call
        GetDefinition() on.
        """
        feat = model.FirstFeature
        while feat is not None:
            yield feat
            feat = feat.GetNextFeature

    @staticmethod
    def _iter_bodies(model):
        """Yield every IBody2 in the part -- solid bodies (swSolidBody=0)
        then sheet metal bodies (swSheetBody=1). Shared by get_face_count()
        and the wall-thickness DFM check.
        """
        for body_type in (0, 1):
            bodies = model.GetBodies2(body_type, True)
            if bodies:
                yield from bodies

    @staticmethod
    def _dyn_get(obj, member_name: str):
        """Read a COM member that may or may not auto-invoke under this
        environment's dynamic dispatch (no gencache/makepy type-library
        binding -- see the module-level notes above _is_suppressed).

        Most zero-arg members (FirstFeature, GetType, GetTypeName2, ...)
        auto-invoke on plain attribute access. But some don't --
        IBody2.GetFaceCount was found to return a bound *method* object
        instead (see get_face_count()'s history), which crashed arithmetic
        until called with explicit parens. Rather than verify each new
        member individually against a live part that may not even have
        the relevant feature type available to test, this helper handles
        both cases uniformly: call it only if what comes back is callable.
        """
        value = getattr(obj, member_name)
        if callable(value):
            value = value()
        return value

    def get_face_count(self) -> int:
        """Total face count across the part's solid and sheet metal bodies.

        Sums IBody2::GetFaceCount over every body from _iter_bodies().

        **Verified live 2026-08-04** against a real part ("5200 battery
        HV"): 19 faces on 1 solid body, 0 sheet bodies. Note this
        contradicts the usual dynamic-dispatch quirk documented elsewhere
        in this file (zero-arg members normally auto-invoke without
        parens) -- IBody2.GetFaceCount specifically does NOT auto-invoke;
        accessing it without parens returns a bound method object, not an
        int, so it must be called with explicit parens here.
        """
        model = self._get_active_doc()
        return sum(body.GetFaceCount() for body in self._iter_bodies(model))

    def capture_screenshot(self, output_path: str | None = None) -> str:
        """Save a screenshot of the current viewport (whatever view/zoom
        is currently active in SolidWorks) as a BMP file, returning the
        saved path.

        **Verified live 2026-08-05** against the real part "5200 battery
        HV": `IModelDoc2.SaveBMP(path, width_px, height_px)` produced a
        real ~900KB, non-blank bitmap (confirmed via PIL: varying
        grayscale pixel values, not a uniform blank image). Note the
        argument order was verified empirically, not from documentation:
        passing (300, 800) produced an image whose PIL `.size` was
        exactly `(300, 800)` (width, height) -- so despite SOLIDWORKS'
        own API documentation calling these arguments "Height, Width" in
        that order, they behave as (width_px, height_px) as passed
        through this dynamic-dispatch call in this environment.
        """
        model = self._get_active_doc()
        if output_path is None:
            part_name = model.GetTitle or "screenshot"
            output_path = str(Path(tempfile.gettempdir()) / f"{part_name}_screenshot.bmp")

        model.SaveBMP(output_path, _SCREENSHOT_WIDTH_PX, _SCREENSHOT_HEIGHT_PX)
        return output_path

    def get_bend_count(self) -> int:
        """Count Sheet Metal bend-producing features in the feature tree.

        See the _SHEET_METAL_BEND_FEATURE_TYPES module constant for the
        approximation this makes (one count per bend-producing feature,
        not per physical bend line) and its known undercounting case.
        Returns 0 for a part with no such features, e.g. any non-sheet-
        metal part -- a real, valid answer, not an error.
        """
        model = self._get_active_doc()
        return sum(
            1
            for feat in self._iter_raw_features(model)
            if feat.GetTypeName2 in _SHEET_METAL_BEND_FEATURE_TYPES
        )

    def get_bounding_box_mm(self) -> tuple[float, float, float]:
        """Axis-aligned bounding box dimensions (x, y, z), in millimeters.

        **Verified live 2026-08-05** against the real part "5200 battery
        HV": returned (139.0, 45.0, 52.0) mm.

        Uses IBody2::GetBodyBox() (per body, from _iter_bodies() -- same
        source as get_face_count()), NOT IModelDocExtension::GetBox --
        that was tried first and found to raise `AttributeError:
        <unknown>.GetBox` via dynamic dispatch in this environment
        regardless of argument count (0/1/2 args, on both `model` and
        `model.Extension`), the same deeper "member not resolvable via
        dynamic dispatch" limitation documented for IFace2.GetSurface in
        _check_wall_thickness -- not the usual auto-invoke-vs-parens
        quirk, since it isn't reachable at all. GetBodyBox() **does**
        auto-resolve, and like GetFaceCount() must be called with
        explicit parens (returns a bound method otherwise). Each body's
        box is 6 doubles (xmin,ymin,zmin,xmax,ymax,zmax) in meters; for a
        multi-body part, the boxes are unioned across all bodies to get
        the whole part's overall bounding box.
        """
        model = self._get_active_doc()
        bodies = list(self._iter_bodies(model))
        if not bodies:
            return (0.0, 0.0, 0.0)

        xmin = ymin = zmin = float("inf")
        xmax = ymax = zmax = float("-inf")
        for body in bodies:
            bx0, by0, bz0, bx1, by1, bz1 = body.GetBodyBox()
            xmin, ymin, zmin = min(xmin, bx0), min(ymin, by0), min(zmin, bz0)
            xmax, ymax, zmax = max(xmax, bx1), max(ymax, by1), max(zmax, bz1)

        x_mm = (xmax - xmin) * 1000.0
        y_mm = (ymax - ymin) * 1000.0
        z_mm = (zmax - zmin) * 1000.0
        return (x_mm, y_mm, z_mm)

    def run_dfm_check(self) -> list[dict]:
        """Run all four DFM checks (hole geometry, wall thickness, draft
        angle, dimension tolerance) against the current part.

        **Not yet fully live-verified**: verified live 2026-08-04 against
        the "5200 battery HV" part for the "no such feature"
        not_applicable paths (holes/draft/tolerance), since that part has
        none of those feature types, and for wall thickness's own
        not_applicable fallback (IFace2.GetSurface turned out to raise a
        COM error via dynamic dispatch in this environment -- see
        _check_wall_thickness -- so this path was exercised too, just not
        the curved/planar detection it was meant to guard). The Hole
        Wizard/Shell/Draft/tolerance *data-reading* code paths themselves
        (IWizardHoleFeatureData2.Diameter/Depth, IDraftFeatureData2's
        angle property, IDimension's tolerance methods) are implemented
        from SOLIDWORKS API documentation but have not been exercised
        against a live part that actually has those feature types --
        verify against a part with a Hole Wizard hole, a Shell feature,
        and a Draft feature before relying on those specific results.
        """
        model = self._get_active_doc()
        findings: list[dict] = []
        findings.extend(self._check_holes(model))
        findings.extend(self._check_wall_thickness(model))
        findings.extend(self._check_draft(model))
        findings.extend(self._check_tolerances(model))
        return findings

    def _check_holes(self, model) -> list[dict]:
        """Protects against holes that are too small to drill reliably, or
        too deep relative to their diameter -- a long thin drill flexes
        and wanders off-axis, breaks more often, and produces a
        rougher/less accurate bore than the CAD model implies.

        Only detects Hole Wizard features (GetTypeName2 == "HoleWzd") --
        plain cylindrical cut-extrudes used to model a hole aren't tagged
        as holes at all in the feature tree, so this is a scope
        limitation of what SolidWorks' API can tell us, not a bug.
        """
        findings = []
        found_any = False

        for feat in self._iter_raw_features(model):
            if feat.GetTypeName2 != "HoleWzd":
                continue
            found_any = True
            name = feat.Name

            try:
                definition = self._dyn_get(feat, "GetDefinition")
                diameter_m = self._dyn_get(definition, "Diameter")
                depth_m = self._dyn_get(definition, "Depth")
            except Exception as e:
                findings.append(
                    make_finding(
                        "hole", "not_applicable", feature=name,
                        message=f"Could not read hole geometry for '{name}': {e}",
                    )
                )
                continue

            if not diameter_m:
                findings.append(
                    make_finding(
                        "hole", "not_applicable", feature=name,
                        message=(
                            f"Diameter not available for hole '{name}' (some "
                            "Hole Wizard types, e.g. tapered holes, don't "
                            "expose a single diameter value)."
                        ),
                    )
                )
                continue

            diameter_mm = diameter_m * 1000
            # depth_m is 0/None for "through all" holes, whose real depth
            # depends on part thickness, not a fixed feature parameter --
            # skip the ratio check rather than guess a depth.
            depth_mm = depth_m * 1000 if depth_m else None

            issues = []
            if diameter_mm < MIN_HOLE_DIAMETER_MM:
                issues.append(
                    f"diameter {diameter_mm:.2f}mm is below the "
                    f"{MIN_HOLE_DIAMETER_MM}mm minimum"
                )
            if depth_mm is not None:
                ratio = depth_mm / diameter_mm
                if ratio > MAX_HOLE_DEPTH_TO_DIAMETER_RATIO:
                    issues.append(
                        f"depth:diameter ratio {ratio:.1f}:1 exceeds the "
                        f"{MAX_HOLE_DEPTH_TO_DIAMETER_RATIO}:1 maximum"
                    )

            extra = {
                "diameter_mm": round(diameter_mm, 3),
                "depth_mm": round(depth_mm, 3) if depth_mm is not None else None,
            }
            if issues:
                findings.append(
                    make_finding("hole", "flagged", feature=name, message="; ".join(issues), **extra)
                )
            else:
                findings.append(
                    make_finding(
                        "hole", "pass", feature=name,
                        message="Hole diameter and depth-to-diameter ratio within limits.",
                        **extra,
                    )
                )

        if not found_any:
            findings.append(
                make_finding(
                    "hole", "not_applicable",
                    message=(
                        "No Hole Wizard features found on this part. (Plain "
                        "cylindrical cut holes not created via Hole Wizard "
                        "aren't detected by this check.)"
                    ),
                )
            )
        return findings

    def _check_wall_thickness(self, model) -> list[dict]:
        """Protects against walls too thin for the manufacturing process
        (injection molding, casting) to fill/cool reliably -- thin walls
        warp, sink, or simply don't fill before the material solidifies.

        Only measurable for Shell features, whose thickness is an exact
        feature parameter. A part could be thin-walled by construction
        (e.g. two offset surfaces) without a Shell feature at all, but
        that thickness isn't a single readable number without a real
        measurement/probing pass this project doesn't implement -- rather
        than approximate it, this check only fires when a Shell feature
        gives an exact answer.
        """
        bodies = list(self._iter_bodies(model))
        if not bodies:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message="No solid or sheet metal bodies found on this part.",
                )
            ]

        has_curved_surface = False
        try:
            for body in bodies:
                faces = self._dyn_get(body, "GetFaces") or []
                for face in faces:
                    surface = self._dyn_get(face, "GetSurface")
                    if surface is None:
                        continue
                    if not self._dyn_get(surface, "IsPlane"):
                        has_curved_surface = True
                        break
                if has_curved_surface:
                    break
        except Exception as e:
            # **Verified live 2026-08-04**: IFace2.GetSurface raises a COM
            # error ("Unable to read write-only property") via dynamic
            # dispatch in this environment -- a deeper limitation than the
            # usual auto-invoke-vs-explicit-parens quirk (same family as
            # the CastTo/gencache limitation documented in get_material()
            # above), not something _dyn_get can paper over. Rather than
            # assume planar or curved, report that this couldn't be
            # determined at all.
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message=(
                        "Could not read face surface types via the "
                        f"SolidWorks API in this environment ({e}) -- wall "
                        "thickness check skipped."
                    ),
                )
            ]

        if has_curved_surface:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message="Wall thickness can't be determined for parts with curved surfaces.",
                )
            ]

        shell_feat = next(
            (f for f in self._iter_raw_features(model) if f.GetTypeName2 == "Shell"), None
        )
        if shell_feat is None:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message="Not applicable to this part (no Shell feature present to measure).",
                )
            ]

        try:
            definition = self._dyn_get(shell_feat, "GetDefinition")
            thickness_m = self._dyn_get(definition, "Thickness")
        except Exception as e:
            return [
                make_finding(
                    "wall_thickness", "not_applicable", feature=shell_feat.Name,
                    message=f"Could not read Shell feature thickness: {e}",
                )
            ]

        thickness_mm = thickness_m * 1000
        if thickness_mm < MIN_WALL_THICKNESS_MM:
            return [
                make_finding(
                    "wall_thickness", "flagged", feature=shell_feat.Name,
                    message=(
                        f"Shell thickness {thickness_mm:.2f}mm is below the "
                        f"{MIN_WALL_THICKNESS_MM}mm minimum."
                    ),
                    thickness_mm=round(thickness_mm, 3),
                )
            ]
        return [
            make_finding(
                "wall_thickness", "pass", feature=shell_feat.Name,
                message="Shell thickness within limits.",
                thickness_mm=round(thickness_mm, 3),
            )
        ]

    def _check_draft(self, model) -> list[dict]:
        """Protects against faces that stick in a mold or die on ejection
        -- without enough draft, a molded/cast part drags against the
        tool as it's pulled out, tearing, scoring, or simply refusing to
        release.
        """
        findings = []
        found_any = False

        for feat in self._iter_raw_features(model):
            if feat.GetTypeName2 != "Draft":
                continue
            found_any = True
            name = feat.Name

            angle_rad = None
            try:
                definition = self._dyn_get(feat, "GetDefinition")
                # Property name for the draft angle isn't confirmed for
                # this SOLIDWORKS version -- try the documented candidates
                # in order rather than guess a value.
                for member in ("DraftAngle", "Angle"):
                    try:
                        angle_rad = self._dyn_get(definition, member)
                        break
                    except AttributeError:
                        continue
            except Exception:
                angle_rad = None

            if angle_rad is None:
                findings.append(
                    make_finding(
                        "draft_angle", "not_applicable", feature=name,
                        message=f"Could not read draft angle for '{name}'.",
                    )
                )
                continue

            angle_deg = math.degrees(angle_rad)
            if angle_deg < MIN_DRAFT_ANGLE_DEG:
                findings.append(
                    make_finding(
                        "draft_angle", "flagged", feature=name,
                        message=(
                            f"Draft angle {angle_deg:.2f} deg is below the "
                            f"{MIN_DRAFT_ANGLE_DEG} deg minimum."
                        ),
                        angle_deg=round(angle_deg, 3),
                    )
                )
            else:
                findings.append(
                    make_finding(
                        "draft_angle", "pass", feature=name,
                        message="Draft angle within limits.",
                        angle_deg=round(angle_deg, 3),
                    )
                )

        if not found_any:
            findings.append(
                make_finding(
                    "draft_angle", "not_applicable",
                    message="Not applicable to this part (no Draft feature present to measure).",
                )
            )
        return findings

    def _check_tolerances(self, model) -> list[dict]:
        """Protects against tolerances tighter than a shop's normal
        process capability -- every extra 0.001mm of precision below
        what a standard process can reliably hold means special tooling,
        secondary operations (grinding, honing), and/or 100% inspection
        instead of sampling, all of which cost disproportionately more
        than the nominal dimension suggests.

        Scope limitation, **confirmed live 2026-08-04**: this walks each
        feature's *display* dimensions (GetFirstDisplayDimension /
        GetNextDisplayDimension), which is the standard SOLIDWORKS API
        approach for enumerating dimensions -- but a display dimension
        only exists once a dimension has actually been shown/inserted in
        the graphics area (e.g. via "Show Feature Dimensions"), not for
        every underlying sketch dimension automatically. Live-tested
        against "5200 battery HV", which has no dimensions displayed at
        all, so this returned zero regardless of whether any of its
        sketch dimensions actually carry a tolerance. This is a real
        SOLIDWORKS API constraint, not a bug: reading tolerance data off
        *hidden* dimensions would need walking every sketch's
        DimensionManager directly, which is out of scope here.
        """
        findings = []
        found_any = False
        seen_names = set()

        for feat in self._iter_raw_features(model):
            try:
                disp_dim = self._dyn_get(feat, "GetFirstDisplayDimension")
            except Exception:
                continue

            while disp_dim is not None:
                name = None
                try:
                    dim = disp_dim.GetDimension2(0)
                    name = self._dyn_get(dim, "GetNameForSelection")
                except Exception:
                    disp_dim = feat.GetNextDisplayDimension(disp_dim)
                    continue

                if name in seen_names:
                    disp_dim = feat.GetNextDisplayDimension(disp_dim)
                    continue
                seen_names.add(name)

                try:
                    tol_type = self._dyn_get(dim, "GetToleranceType")
                except Exception:
                    disp_dim = feat.GetNextDisplayDimension(disp_dim)
                    continue

                if not tol_type:  # swTolNONE (0) -- no explicit tolerance set
                    disp_dim = feat.GetNextDisplayDimension(disp_dim)
                    continue

                found_any = True
                try:
                    tol_values = self._dyn_get(dim, "GetToleranceValues")
                    min_tol_m, max_tol_m = tol_values[0], tol_values[1]
                except Exception as e:
                    findings.append(
                        make_finding(
                            "tolerance", "not_applicable", feature=name,
                            message=f"Tolerance type set but values couldn't be read: {e}",
                        )
                    )
                    disp_dim = feat.GetNextDisplayDimension(disp_dim)
                    continue

                band_mm = abs(max_tol_m - min_tol_m) * 1000
                extra = {
                    "min_tolerance_mm": round(min_tol_m * 1000, 4),
                    "max_tolerance_mm": round(max_tol_m * 1000, 4),
                }
                if band_mm < MIN_TOLERANCE_BAND_MM:
                    findings.append(
                        make_finding(
                            "tolerance", "flagged", feature=name,
                            message=(
                                f"Tolerance band {band_mm:.4f}mm is tighter than the "
                                f"{MIN_TOLERANCE_BAND_MM}mm threshold -- may increase "
                                "manufacturing cost."
                            ),
                            **extra,
                        )
                    )
                else:
                    findings.append(
                        make_finding(
                            "tolerance", "pass", feature=name,
                            message="Tolerance band within normal manufacturing limits.",
                            **extra,
                        )
                    )

                disp_dim = feat.GetNextDisplayDimension(disp_dim)

        if not found_any:
            findings.append(
                make_finding(
                    "tolerance", "not_applicable",
                    message="No dimensions with an explicit tolerance applied were found on this part.",
                )
            )

        # The part's overall/general tolerance limit (the default standard
        # applied to any dimension *without* an explicit tolerance, e.g.
        # an ISO 2768 class) lives in SOLIDWORKS' DimXpert general-
        # tolerance table, which requires a secondary DimXpert type
        # library and doesn't expose a single simple "current class"
        # property reachable via dynamic COM dispatch without
        # gencache/makepy binding in this environment (same class of
        # limitation as the CastTo failure documented in get_material()
        # above). Reported as unavailable rather than guessed.
        findings.append(
            make_finding(
                "tolerance", "not_applicable",
                message="General tolerance setting not available for this part.",
            )
        )

        return findings

    @staticmethod
    def _is_suppressed(feat) -> bool:
        """Best-effort read of a feature's suppression state.

        GetSuppression2 turned out not to be exposed on IFeature's default
        dispatch interface in this environment (every feature, including a
        verified-suppressed one, raised on access). IFeature::IsSuppressed
        is the one that actually works: verified live against a part with
        "Fillet1" suppressed — it returned True only for that feature and
        False for the other 23. Falls back to False if a feature type
        doesn't support the query at all.
        """
        try:
            return bool(feat.IsSuppressed)
        except Exception:
            return False
