"""Fusion 360 implementation of the CadAdapter interface.

Unlike SolidWorksAdapter (which drives a running SolidWorks instance
directly via COM from this same external process), FusionAdapter never
imports Fusion's API and never talks to Fusion directly at all. It only
makes local HTTP requests to fusion_bridge_addin/, a Fusion 360 Add-In
that must be running *inside* Fusion for this adapter to work. See that
package's module docstring, and the explanation given alongside this file,
for why that indirection is required (Fusion's API only runs inside
Fusion's own embedded Python interpreter -- there is no external COM-style
entry point to connect to).

connect(), get_current_part_info(), get_mass(), get_material(),
get_features(), get_face_count(), get_bend_count(), run_dfm_check(),
is_assembly(), and get_assembly_components() are all implemented against
matching fusion_bridge_addin/ endpoints, matching SolidWorksAdapter's
return shapes exactly (same PartInfo/MaterialInfo/Feature/
AssemblyComponent dataclasses, same units -- kg for mass, kg/m^3 for
density, mm for length). get_assembly_bom() and everything else built on
CadAdapter's concrete methods in base_adapter.py work unmodified for
FusionAdapter, since they only ever call other CadAdapter methods.
run_dfm_check()'s tolerance checks always report not_applicable -- see
that method's docstring for why (a genuine Fusion API gap, confirmed
against Autodesk's own reference, not a shortcut).
Every other CadAdapter method still raises NotImplementedError with a
message saying so, rather than being silently missing or crashing with an
unrelated AttributeError.

open_document(file_path) is also implemented, but is deliberately NOT
part of the CadAdapter interface -- SolidWorksAdapter has no matching
method, so this is a Fusion-only extra rather than an interface method
with a stub on every adapter (see the discussion that led to this when
the method was added).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from cad_adapters.base_adapter import (
    AssemblyComponent,
    CadAdapter,
    Feature,
    MaterialInfo,
    PartInfo,
)
from cad_adapters.dfm_checks import (
    MAX_HOLE_DEPTH_TO_DIAMETER_RATIO,
    MIN_DRAFT_ANGLE_DEG,
    MIN_HOLE_DIAMETER_MM,
    MIN_WALL_THICKNESS_MM,
    make_finding,
)

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 19191
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"

_REQUEST_TIMEOUT_SECONDS = 3.0


class FusionBridgeConnectionError(RuntimeError):
    """Base class for failures reaching the Fusion bridge Add-In."""


class FusionBridgeNotReachableError(FusionBridgeConnectionError):
    """Raised when the bridge Add-In's HTTP server can't be reached at all."""


class NoFusionDocumentOpenError(FusionBridgeConnectionError):
    """Raised when the bridge is reachable but Fusion has no document open."""


class FusionDataUnavailableError(FusionBridgeConnectionError):
    """Raised when the bridge is reachable and a document is open, but the
    requested data itself couldn't be read (e.g. the active document isn't
    a parametric Design, or a part has no solid bodies/material assigned).
    Distinct from NoFusionDocumentOpenError so the message accurately
    reflects what actually went wrong instead of implying nothing is open.
    """


class FusionAdapter(CadAdapter):
    """CadAdapter implementation backed by a running Fusion 360 session,
    reached indirectly through the fusion_bridge_addin/ Add-In's local
    HTTP server (see that package's docstring for why this indirection is
    necessary).
    """

    def __init__(self, base_url: str = BRIDGE_BASE_URL) -> None:
        self._base_url = base_url

    def _request(self, path: str, timeout: float = _REQUEST_TIMEOUT_SECONDS) -> dict:
        """GET `path` from the bridge and return the parsed JSON body.

        Raises FusionBridgeNotReachableError -- with a message pointing
        Krish at the actual fix -- instead of letting a raw
        urllib/socket/JSON exception propagate, for any failure that
        means the bridge simply isn't there to answer: connection
        refused, DNS/timeout, or a response that isn't valid JSON.

        `timeout` defaults to _REQUEST_TIMEOUT_SECONDS (3s), fine for
        simple property reads. **Confirmed live (2026-08-29) that 3s is
        too short for /open_document**: the client timed out and raised
        FusionBridgeNotReachableError while the bridge server was still
        up and legitimately still working (Get-NetTCPConnection showed it
        still Listening) -- opening/switching a document is slow enough
        that callers doing that need to pass a much longer timeout
        explicitly (see open_document() below).
        """
        url = f"{self._base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            # The bridge IS reachable and answered -- it just returned a
            # non-2xx status (e.g. 500 from an exception inside
            # _get_active_document_info(), or 404 for an unknown path).
            # HTTPError is a URLError subclass, so without this branch it
            # would be swallowed by the except below and misreported as
            # "could not reach the bridge", hiding the real error message
            # the Add-In tried to report.
            body = e.read()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise FusionBridgeNotReachableError(
                    f"Fusion bridge Add-In at {self._base_url} returned "
                    f"HTTP {e.code} with a non-JSON body: {body!r}"
                ) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise FusionBridgeNotReachableError(
                "Could not reach the Fusion 360 bridge Add-In at "
                f"{self._base_url}. Make sure Fusion 360 is running and the "
                "'fusion_bridge_addin' Add-In is started (Utilities > "
                "Add-Ins > Scripts and Add-Ins > Add-Ins tab > "
                "fusion_bridge_addin > Run), then try again."
            ) from e

        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise FusionBridgeNotReachableError(
                f"Fusion bridge Add-In at {self._base_url} returned a "
                f"response that wasn't valid JSON: {e}"
            ) from e

    def connect(self) -> bool:
        """Verify the bridge Add-In's HTTP server is reachable.

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable (Fusion isn't open, or the Add-In isn't
                started).
        """
        self._request("/ping")
        return True

    def get_current_part_info(self) -> PartInfo:
        """Fetch basic info about Fusion's currently active document via
        the bridge's /part_info endpoint.

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            NoFusionDocumentOpenError: the bridge is reachable, but Fusion
                has no document open.
        """
        data = self._request("/part_info")
        if "error" in data:
            raise NoFusionDocumentOpenError(data["error"])

        return PartInfo(
            name=data.get("title", ""),
            file_path=data.get("file_path", ""),
            part_type=data.get("document_type", "unknown"),
            units=data.get("units", "unknown"),
        )

    def open_document(self, file_path: str) -> bool:
        """Open a Fusion design so it becomes the active document, via the
        bridge's /open_document endpoint. Not part of the CadAdapter
        interface -- Fusion-specific, since SolidWorksAdapter has no
        equivalent method to match.

        Fusion is cloud-based and has no OS filesystem path for a design
        the way SolidWorks has a .SLDPRT path. `file_path` is matched
        against the document's NAME as shown in the Data Panel
        (case-insensitive exact match), searched recursively through the
        currently active Data Panel project only -- not by any real
        filesystem path, and not across every project in the hub. See
        fusion_bridge_addin.py's _open_document() for the full search and
        the reasoning behind this scope.

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: no design matched `file_path`
                (not found, ambiguous match, no active Data Panel
                project, or Fusion refused to open it) -- the message
                explains exactly which case occurred.
        """
        data = self._request(
            f"/open_document?name={urllib.parse.quote(file_path)}",
            timeout=35.0,  # bridge itself waits up to 30s for Fusion's main thread; leave margin
        )
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])
        return True

    # -- Everything below is not yet implemented. --------------------------
    #
    # These all raise NotImplementedError rather than being silently
    # missing (which FusionAdapter still can't be, since CadAdapter is an
    # ABC -- every abstract method needs a body to be instantiable at
    # all) or guessing at data the bridge doesn't expose yet. Extending
    # fusion_bridge_addin/ with more endpoints (mass properties, material,
    # feature tree, etc.) and wiring the matching method here is future
    # work -- see the module docstring.

    def _not_implemented(self, method_name: str):
        raise NotImplementedError(
            f"FusionAdapter.{method_name}() is not implemented yet -- only "
            "connect() and get_current_part_info() are currently wired to "
            "the fusion_bridge_addin/ bridge. Extend the bridge Add-In with "
            "a matching endpoint and this method to support it."
        )

    def get_mass(self) -> float:
        """Mass of the current design, in kilograms, via the bridge's
        /mass endpoint (Component.physicalProperties.mass on the root
        component -- see fusion_bridge_addin.py's _get_mass_kg).

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design, or its mass couldn't be computed.
        """
        data = self._request("/mass")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])
        return data["mass_kg"]

    def get_mass_properties(self) -> dict:
        self._not_implemented("get_mass_properties")

    def get_material(self) -> MaterialInfo:
        """Material assigned to the current design's first solid body, via
        the bridge's /material endpoint -- same "one material representing
        the current part" scope as SolidWorksAdapter.get_material().

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design, has no solid bodies, or has no material
                assigned to its first body.
        """
        data = self._request("/material")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])

        return MaterialInfo(
            name=data.get("name", "Not specified"),
            density_kg_m3=data.get("density_kg_m3"),
            category=data.get("category", ""),
        )

    def get_features(self) -> list[Feature]:
        """The current design's Timeline, via the bridge's /features
        endpoint -- Fusion's ordered record of every modeling operation,
        analogous to SolidWorksAdapter's feature-tree walk.

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design.
        """
        data = self._request("/features")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])

        return [
            Feature(
                name=f["name"],
                feature_type=f["feature_type"],
                suppressed=f["suppressed"],
            )
            for f in data.get("features", [])
        ]

    def run_dfm_check(self) -> list[dict]:
        """Run the three DFM checks Fusion's API can support -- hole
        geometry, wall thickness, and draft angle -- reusing the exact
        same thresholds and make_finding() shape from
        cad_adapters/dfm_checks.py that SolidWorksAdapter uses. Nothing
        is redefined here.

        **Dimension tolerance (both per-dimension and general/
        document-level) always reports not_applicable.** Confirmed
        against Autodesk's own Fusion API reference
        (help.autodesk.com/.../ModelParameter.htm): ModelParameter, the
        object backing every dimension on a Fusion model, has no
        tolerance-related property at all -- no type, no plus/minus
        values. This is a real Fusion API gap, not a shortcut: unlike
        SOLIDWORKS' IDimension.GetToleranceType/GetToleranceValues,
        there's nothing to read. See _check_tolerances() below.

        Unlike connect()/get_current_part_info(), an unreachable/wrong
        document type doesn't raise here -- each check independently
        turns that into a not_applicable finding for itself, since
        run_dfm_check()'s contract is to always return a list of
        findings, never guess, and never leave the caller with nothing.
        FusionBridgeNotReachableError (the bridge server itself being
        down) still propagates, same as every other method here.
        """
        findings: list[dict] = []
        findings.extend(self._check_holes())
        findings.extend(self._check_wall_thickness())
        findings.extend(self._check_draft())
        findings.extend(self._check_tolerances())
        return findings

    def _check_holes(self) -> list[dict]:
        data = self._request("/dfm/holes")
        if "error" in data:
            return [make_finding("hole", "not_applicable", message=data["error"])]

        holes = data["holes"]
        if not holes:
            return [
                make_finding(
                    "hole", "not_applicable",
                    message=(
                        "No Hole features found on this part. (Fusion's "
                        "general 'Hole' command is the closest equivalent "
                        "to SOLIDWORKS Hole Wizard; plain cylindrical cuts "
                        "modeled with Extrude aren't detected by this "
                        "check.)"
                    ),
                )
            ]

        findings = []
        for hole in holes:
            name = hole["feature_name"]
            if hole["error"]:
                findings.append(make_finding("hole", "not_applicable", feature=name, message=hole["error"]))
                continue

            diameter_mm = hole["diameter_mm"]
            depth_mm = hole["depth_mm"]

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

        return findings

    def _check_wall_thickness(self) -> list[dict]:
        data = self._request("/dfm/wall_thickness")
        if "error" in data:
            return [make_finding("wall_thickness", "not_applicable", message=data["error"])]

        if not data["has_bodies"]:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message="No solid bodies found on this part.",
                )
            ]

        if data["curved_check_error"]:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message=(
                        "Could not read face surface types via the Fusion "
                        f"API ({data['curved_check_error']}) -- wall "
                        "thickness check skipped."
                    ),
                )
            ]

        if data["has_curved_surface"]:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message="Wall thickness can't be determined for parts with curved surfaces.",
                )
            ]

        shell = data["shell"]
        if shell is None:
            return [
                make_finding(
                    "wall_thickness", "not_applicable",
                    message="Not applicable to this part (no Shell feature present to measure).",
                )
            ]

        if shell["error"]:
            return [
                make_finding(
                    "wall_thickness", "not_applicable", feature=shell["feature_name"],
                    message=f"Could not read Shell feature thickness: {shell['error']}",
                )
            ]

        thickness_mm = shell["thickness_mm"]
        if thickness_mm < MIN_WALL_THICKNESS_MM:
            return [
                make_finding(
                    "wall_thickness", "flagged", feature=shell["feature_name"],
                    message=(
                        f"Shell thickness {thickness_mm:.2f}mm is below the "
                        f"{MIN_WALL_THICKNESS_MM}mm minimum."
                    ),
                    thickness_mm=round(thickness_mm, 3),
                )
            ]
        return [
            make_finding(
                "wall_thickness", "pass", feature=shell["feature_name"],
                message="Shell thickness within limits.",
                thickness_mm=round(thickness_mm, 3),
            )
        ]

    def _check_draft(self) -> list[dict]:
        data = self._request("/dfm/draft")
        if "error" in data:
            return [make_finding("draft_angle", "not_applicable", message=data["error"])]

        drafts = data["drafts"]
        if not drafts:
            return [
                make_finding(
                    "draft_angle", "not_applicable",
                    message="Not applicable to this part (no Draft feature present to measure).",
                )
            ]

        findings = []
        for draft in drafts:
            name = draft["feature_name"]

            if draft["two_sided"]:
                findings.append(
                    make_finding(
                        "draft_angle", "not_applicable", feature=name,
                        message=(
                            f"Draft feature '{name}' uses independent angles "
                            "on each side (Fusion's two-sided draft), which "
                            "has no single angle value to check against the "
                            "minimum -- not approximated as one number."
                        ),
                    )
                )
                continue

            if draft["error"] or draft["angle_deg"] is None:
                findings.append(
                    make_finding(
                        "draft_angle", "not_applicable", feature=name,
                        message=draft["error"] or f"Could not read draft angle for '{name}'.",
                    )
                )
                continue

            angle_deg = draft["angle_deg"]
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

        return findings

    def _check_tolerances(self) -> list[dict]:
        """Always not_applicable -- see run_dfm_check()'s docstring. No
        bridge round-trip needed: there is nothing in the Fusion API to
        query, confirmed against the ModelParameter API reference.
        """
        return [
            make_finding(
                "tolerance", "not_applicable",
                message=(
                    "Per-dimension manufacturing tolerances are not "
                    "exposed by the Fusion 360 API on model dimensions "
                    "(confirmed against the ModelParameter API reference: "
                    "no tolerance type or plus/minus values exist on it) "
                    "-- unlike SOLIDWORKS, where IDimension."
                    "GetToleranceType/GetToleranceValues reads them "
                    "directly. Fusion tolerance annotations exist only in "
                    "the separate Drawing environment, not on the "
                    "currently open Design."
                ),
            ),
            make_finding(
                "tolerance", "not_applicable",
                message=(
                    "General tolerance setting not available for this "
                    "part (same category of limitation as SOLIDWORKS' "
                    "DimXpert general tolerance table, also unavailable "
                    "in this project)."
                ),
            ),
        ]

    def get_face_count(self) -> int:
        """Total face count across every solid body in the current design,
        via the bridge's /face_count endpoint -- same "whole document"
        scope as SolidWorksAdapter.get_face_count().

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design.
        """
        data = self._request("/face_count")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])
        return data["face_count"]

    def get_bend_count(self) -> int:
        """Number of Sheet Metal bend-producing features in the current
        design's Timeline, via the bridge's /bend_count endpoint -- see
        fusion_bridge_addin.py's _SHEET_METAL_BEND_FEATURE_TYPES for the
        same one-count-per-feature approximation
        SolidWorksAdapter.get_bend_count() documents.

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design.
        """
        data = self._request("/bend_count")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])
        return data["bend_count"]

    def get_hole_features(self) -> list:
        self._not_implemented("get_hole_features")

    def highlight_feature(self, feature_id: str) -> dict:
        self._not_implemented("highlight_feature")

    def capture_screenshot(self, output_path: str | None = None) -> str:
        self._not_implemented("capture_screenshot")

    def get_bounding_box_mm(self) -> tuple:
        self._not_implemented("get_bounding_box_mm")

    def is_assembly(self) -> bool:
        """True if the design's root component references any occurrences
        at all, via the bridge's /is_assembly endpoint -- Fusion has no
        separate assembly/part document type the way SOLIDWORKS does; it's
        purely structural (see fusion_bridge_addin.py's _get_is_assembly).

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design.
        """
        data = self._request("/is_assembly")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])
        return data["is_assembly"]

    def get_assembly_components(self) -> list[AssemblyComponent]:
        """Recursively walk the design's occurrence tree via the bridge's
        /assembly_components endpoint -- see
        fusion_bridge_addin.py's _get_assembly_components() for the full
        traversal (root component -> childOccurrences, aggregated by
        Component.id across the whole tree, sub-assemblies not emitted as
        rows, suppressed/invisible occurrences skipped during the walk).

        file_path and configuration are always "" on the returned rows --
        Fusion components inside one cloud document don't have local file
        paths, and this doesn't attempt to resolve Fusion's Configurations
        feature.

        Raises:
            FusionBridgeNotReachableError: the bridge server isn't
                running/reachable.
            FusionDataUnavailableError: the active document isn't a
                Fusion Design, or isn't an assembly (call is_assembly()
                first).
        """
        data = self._request("/assembly_components")
        if "error" in data:
            raise FusionDataUnavailableError(data["error"])

        components = []
        for row in data["components"]:
            material_data = row.get("material")
            material = (
                MaterialInfo(
                    name=material_data.get("name", "Not specified"),
                    density_kg_m3=material_data.get("density_kg_m3"),
                    category=material_data.get("category", ""),
                )
                if material_data is not None
                else None
            )

            bounding_box = row.get("bounding_box_mm")
            components.append(
                AssemblyComponent(
                    part_name=row["part_name"],
                    file_path=row["file_path"],
                    configuration=row["configuration"],
                    quantity=row["quantity"],
                    parent_assembly=row["parent_assembly"],
                    level=row["level"],
                    mass_kg=row["mass_kg"],
                    volume_m3=row["volume_m3"],
                    material=material,
                    face_count=row["face_count"],
                    bend_count=row["bend_count"],
                    bounding_box_mm=tuple(bounding_box) if bounding_box is not None else None,
                )
            )
        return components
