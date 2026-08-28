"""Fusion 360 Add-In: local HTTP bridge exposing basic info about the
currently active document.

WHY THIS EXISTS (see the top-level explanation given to Krish, also
summarized here): Fusion 360's Python API only runs *inside* Fusion's own
embedded interpreter, loaded as an Add-In -- there is no equivalent of
SolidWorks' COM server that an external, independently-launched Python
process can dial into. An external process (cad_adapters/fusion_adapter.py)
therefore cannot call adsk.core/adsk.fusion directly. This Add-In bridges
that gap: it runs *inside* Fusion, and when Fusion loads it, it starts a
plain local HTTP server on a background thread. FusionAdapter, running as
an ordinary external Python process, talks to Fusin only by making HTTP
requests to this server -- never by importing the Fusion API itself.

Fusion calls two module-level entry points automatically:
  - run(context)  -- called when the Add-In is started (manually via
    Utilities > Add-Ins, or automatically at Fusion startup if "Run on
    Startup" is checked).
  - stop(context) -- called when the Add-In is stopped/disabled, or when
    Fusion closes while it's running.

Only the standard library is used (http.server, json, threading) --
Fusion's embedded Python environment does not have pip access set up by
default, so this deliberately avoids any third-party dependency (e.g.
Flask) that would need to be vendored or installed into that environment
separately.
"""

import json
import math
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import adsk.core
import adsk.fusion

# Loopback-only: this bridge is meant to be reached by another process on
# the SAME machine (cad_adapters/fusion_adapter.py), never over the
# network -- binding to 127.0.0.1 instead of 0.0.0.0 keeps it unreachable
# from any other machine.
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 19191

# adsk.core.DocumentTypes enum values -> human-readable strings. Fusion
# doesn't have SolidWorks' part/assembly/drawing split (a single Fusion
# "design" document can contain many components, i.e. what SolidWorks
# would call an assembly, with no separate document type for it) -- these
# are the actual adsk.core.DocumentTypes members that exist.
# Product.productType string values -> readable labels. Product (not
# Document) is what actually identifies "what kind of thing is open" in
# this Fusion API version -- Document itself turned out NOT to have a
# `documentType` property at all (confirmed live: accessing it raised
# `AttributeError: 'FusionDocument' object has no attribute
# 'documentType'`), despite that being the documented approach in older
# API references. `Application.activeProduct.productType` is what's
# actually used here instead.
_PRODUCT_TYPE_NAMES = {
    "DesignProductType": "design",
    "DrawingProductType": "drawing",
    "CAMProductType": "cam",
    "PresentationProductType": "presentation",
}

_app = None
_ui = None
_httpd = None
_server_thread = None


def _get_active_document_info() -> dict:
    """Read basic info about Fusion's currently active document.

    Called from the HTTP handler thread (a background thread spun up by
    HTTPServer), not Fusion's main UI thread. Fusion's API is documented
    as expecting to be driven from the main thread; this simple read-only
    property access has worked reliably in testing, but a production
    Add-In doing anything beyond simple reads (geometry edits, command
    execution, etc.) should marshal onto the main thread via
    adsk.core.Application.registerCustomEvent +
    Application.fireCustomEvent instead of calling the API directly from
    a worker thread like this does.
    """
    app = adsk.core.Application.get()
    doc = app.activeDocument
    if doc is None:
        return {"error": "No document is currently open in Fusion 360."}

    # Every field below is read defensively and independently: this
    # environment's Fusion API build already turned up one documented-but-
    # wrong assumption (Document.documentType doesn't exist here -- see
    # the module comment above _PRODUCT_TYPE_NAMES), so one unexpected
    # missing/renamed member must degrade that one field to "unknown"/""
    # instead of taking down the whole response.

    title = "unknown"
    try:
        title = doc.name
    except Exception:
        pass

    doc_type = "unknown"
    try:
        product = app.activeProduct
        if product is not None:
            doc_type = _PRODUCT_TYPE_NAMES.get(product.productType, product.productType)
    except Exception:
        pass

    is_saved = None
    try:
        is_saved = doc.isSaved
    except Exception:
        pass

    file_path = ""
    try:
        # Cloud-stored Fusion documents don't have a local file path the
        # way a SolidWorks .SLDPRT does; dataFile.name is the closest
        # analog (the Data Panel item name) -- empty string if
        # unavailable (e.g. a brand-new, never-saved document).
        if is_saved and doc.dataFile:
            file_path = doc.dataFile.name
    except Exception:
        pass

    units = "unknown"
    try:
        design = _get_design()
        if design is not None:
            units = design.unitsManager.defaultLengthUnits
    except Exception:
        pass  # not a design (e.g. drawing) -- leave units as "unknown"

    return {
        "title": title,
        "document_type": doc_type,
        "is_saved": is_saved,
        "file_path": file_path,
        "units": units,
    }


def _get_design() -> "adsk.fusion.Design | None":
    """Return the active document's Design product, or None if it doesn't
    have one -- shared by _get_mass_kg/_get_material/_get_features so each
    one reports the same "not a design" error instead of a different
    AttributeError each time.

    **Not** `Design.cast(app.activeProduct)` (tried first, and what
    _get_active_document_info() still uses for the /part_info units
    field) -- `activeProduct` is whichever workspace TAB is currently
    showing in the UI, not necessarily the Design. Confirmed live: with a
    Simulation study open/active for a design document, activeProduct's
    productType was "SimCaseProductType", so casting it to Design silently
    returned None even though the document unambiguously has a Design.
    `Document.products.itemByProductType("DesignProductType")` fetches the
    Design product directly off the document regardless of which tab the
    user happens to be looking at, which is what "the current part's
    mass/material/features" should mean here.
    """
    doc = adsk.core.Application.get().activeDocument
    if doc is None:
        return None
    try:
        design_product = doc.products.itemByProductType("DesignProductType")
    except Exception:
        return None
    return adsk.fusion.Design.cast(design_product)


def _get_mass_kg() -> dict:
    """Mass of the whole current design (all bodies in the root
    component), in kilograms -- same "whole active document" scope as
    SolidWorksAdapter.get_mass() reading CreateMassProperty off the
    active doc.

    Uses Component.physicalProperties (default accuracy), whose .mass is
    documented to already be in kilograms regardless of the document's
    display unit setting -- Fusion's internal database units always use
    kg for mass, cm for length, the same way SolidWorks' UseSystemUnits
    forces SI units in solidworks_adapter.py's _read_mass.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    try:
        mass_kg = design.rootComponent.physicalProperties.mass
    except Exception as e:
        return {"error": f"Could not read mass: {e}"}

    return {"mass_kg": mass_kg}


def _get_material() -> dict:
    """Material assigned to the current design's first solid body --
    same "one material representing the current part" scope as
    SolidWorksAdapter.get_material() (which only supports single-material
    parts, not per-body/per-face material assignment).

    Density is read from the material's "Density" MaterialProperty.
    **Units for this value are not yet confirmed against a live part with
    a non-default material assigned** -- Fusion's MaterialProperty.value
    for a physical property is returned in the property's own database
    unit, which for density is documented as kg/m^3, matching what
    MaterialInfo.density_kg_m3 expects, but this hasn't been empirically
    verified the way solidworks_adapter.py's quirks were (see that
    module's history) since it depends on which library material happens
    to be assigned. Returns density_kg_m3=None (not a guessed number) if
    the Density property can't be found or read.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    bodies = design.rootComponent.bRepBodies
    if bodies.count == 0:
        return {"error": "No solid bodies found in the current design."}

    try:
        material = bodies.item(0).material
    except Exception as e:
        return {"error": f"Could not read material from the first body: {e}"}

    if material is None:
        return {"error": "The current design's first body has no material assigned."}

    name = "Not specified"
    try:
        name = material.name
    except Exception:
        pass

    density_kg_m3 = None
    try:
        density_prop = material.materialProperties.itemByName("Density")
        if density_prop is not None:
            density_kg_m3 = density_prop.value
    except Exception:
        pass

    return {"name": name, "density_kg_m3": density_kg_m3, "category": ""}


def _get_features() -> dict:
    """Walk the current design's Timeline -- Fusion's single ordered
    record of every modeling operation (sketches, features, etc.),
    analogous to SolidWorksAdapter's FirstFeature/GetNextFeature walk.

    Each TimelineObject wraps one underlying entity (e.g. an
    ExtrudeFeature) via `.entity`; the entity's Python class name (e.g.
    "ExtrudeFeature") is used as feature_type since Fusion has no single
    GetTypeName2-style string property the way SolidWorks does.
    TimelineObject.isSuppressed is a real, direct suppression flag (not
    inferred), matching what `suppressed` means in SolidWorksAdapter's
    Feature dataclass.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    features = []
    timeline = design.timeline
    for i in range(timeline.count):
        item = timeline.item(i)

        entity = None
        try:
            entity = item.entity
        except Exception:
            pass

        name = f"TimelineItem{i}"
        try:
            if entity is not None and getattr(entity, "name", None):
                name = entity.name
        except Exception:
            pass

        feature_type = type(entity).__name__ if entity is not None else "Unknown"

        suppressed = False
        try:
            suppressed = bool(item.isSuppressed)
        except Exception:
            pass

        features.append({"name": name, "feature_type": feature_type, "suppressed": suppressed})

    return {"features": features}


# TimelineObject.entity Python class names for Fusion's Sheet Metal
# features that produce a bend line -- the closest Fusion equivalent to
# solidworks_adapter.py's _SHEET_METAL_BEND_FEATURE_TYPES. Same documented
# approximation as that set: one count per bend-*producing feature*, not
# per physical bend line (a Flange run along a multi-segment edge can
# still produce more than one physical bend from a single feature).
# **Not yet live-verified against a real Sheet Metal part** -- the test
# part available for this pass has no Sheet Metal features, so only the
# "zero bends" path has been exercised, same caveat SolidWorksAdapter's
# get_bend_count() carries.
_SHEET_METAL_BEND_FEATURE_TYPES = {
    "BendFeature",
    "FlangeFeature",
    "ContourFlangeFeature",
    "HemFeature",
    "UnfoldFeature",
    "FoldFeature",
}


def _iter_all_bodies(design):
    """Yield every BRepBody in the design: the root component's own
    bodies plus every occurrence's bodies -- same "whole document" scope
    as _get_mass_kg's rootComponent.physicalProperties (which aggregates
    across the whole tree), rather than only counting bodies that happen
    to live directly in the root component.
    """
    yield from design.rootComponent.bRepBodies
    for occurrence in design.rootComponent.allOccurrences:
        yield from occurrence.bRepBodies


def _get_face_count() -> dict:
    """Total face count across every solid body in the current design --
    same "whole document" scope as SolidWorksAdapter.get_face_count()
    summing IBody2.GetFaceCount() over every body.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    try:
        total = sum(body.faces.count for body in _iter_all_bodies(design))
    except Exception as e:
        return {"error": f"Could not read face count: {e}"}

    return {"face_count": total}


def _get_bend_count() -> dict:
    """Count Sheet Metal bend-producing features in the design's Timeline
    -- see the _SHEET_METAL_BEND_FEATURE_TYPES module comment for the
    approximation this makes. Returns 0 for a design with no such
    features (including any non-sheet-metal part) -- a real, valid
    answer, not an error, matching SolidWorksAdapter.get_bend_count().
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    count = 0
    timeline = design.timeline
    for i in range(timeline.count):
        item = timeline.item(i)
        try:
            entity = item.entity
        except Exception:
            continue
        if entity is not None and type(entity).__name__ in _SHEET_METAL_BEND_FEATURE_TYPES:
            count += 1

    return {"bend_count": count}


# -- DFM raw-data endpoints --------------------------------------------
#
# These four endpoints only read and return raw Fusion geometry/feature
# data -- the actual threshold comparisons and finding construction
# (make_finding(), MIN_HOLE_DIAMETER_MM, etc.) live entirely in
# cad_adapters/fusion_adapter.py's _check_* methods, reusing the exact
# same cad_adapters/dfm_checks.py constants and shape SolidWorksAdapter
# uses. This keeps the DFM decision logic in exactly one place instead of
# duplicating it inside Fusion's embedded interpreter.
#
# There is deliberately NO /dfm/tolerance endpoint: confirmed against the
# Fusion API reference (help.autodesk.com's ModelParameter page) that
# ModelParameter -- the object backing every dimension on a Fusion model
# -- has no tolerance-related property at all (no type, no plus/minus
# values). Manufacturing tolerance annotations in Fusion exist only in
# the separate Drawing environment, not on the Design this bridge reads,
# and even there the API reference has no documented way to read a
# tolerance value back out. FusionAdapter's tolerance check is therefore
# always not_applicable and needs no bridge round-trip at all.


def _get_dfm_holes() -> dict:
    """One entry per HoleFeature in the design's Timeline -- Fusion's
    general "Hole" command is the closest equivalent to SOLIDWORKS' Hole
    Wizard feature that solidworks_adapter.py's _read_holes() reads.

    diameter_mm comes from HoleFeature.holeDiameter (a ModelParameter,
    value in cm -> mm). depth_mm comes from HoleFeature.extentDefinition:
    a DistanceExtentDefinition gives a real depth (cm -> mm); a
    ThroughAllExtentDefinition (confirmed to exist as a distinct class in
    the API reference) means "through all", reported as depth_mm=None --
    same "don't guess a depth for a through hole" rule
    solidworks_adapter.py's _read_holes() already follows.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    holes = []
    timeline = design.timeline
    for i in range(timeline.count):
        item = timeline.item(i)
        try:
            entity = item.entity
        except Exception:
            continue
        if entity is None or type(entity).__name__ != "HoleFeature":
            continue

        name = getattr(entity, "name", None) or f"TimelineItem{i}"

        diameter_mm = None
        error = None
        try:
            diameter_mm = entity.holeDiameter.value * 10.0  # cm -> mm
        except Exception as e:
            error = f"Could not read diameter for hole '{name}': {e}"

        depth_mm = None
        if error is None:
            try:
                extent = entity.extentDefinition
                if isinstance(extent, adsk.fusion.DistanceExtentDefinition):
                    depth_mm = extent.distance.value * 10.0  # cm -> mm
                # ThroughAllExtentDefinition (or anything else unrecognized)
                # leaves depth_mm as None, same as SolidWorks' "through
                # all" -- its real depth isn't a fixed feature parameter.
            except Exception:
                depth_mm = None

        holes.append({
            "feature_name": name,
            "diameter_mm": diameter_mm,
            "depth_mm": depth_mm,
            "error": error,
        })

    return {"holes": holes}


def _get_dfm_wall_thickness() -> dict:
    """Raw data for the wall-thickness check: whether the design has any
    curved-surface bodies (in which case, like solidworks_adapter.py,
    thickness can't be reliably measured and the check is skipped), plus
    the first ShellFeature's thickness if one exists and no curved
    surface was found.

    Curved-surface detection uses BRepFace.geometry, checking
    `isinstance(face.geometry, adsk.core.Plane)` -- Fusion's direct
    equivalent of solidworks_adapter.py's IFace2.GetSurface().IsPlane
    check (which raised a COM error in that environment; untested here
    whether Fusion's equivalent is reliable, so it's still wrapped
    defensively and reported via curved_check_error if it fails, exactly
    mirroring the SOLIDWORKS not_applicable fallback for that failure).

    Shell thickness tries insideThickness first, falling back to
    outsideThickness if the part was shelled outward instead of inward --
    both are documented as "the inside/outside thickness, edit through
    ModelParameter" in the Fusion API reference.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    bodies = list(_iter_all_bodies(design))
    if not bodies:
        return {"has_bodies": False, "has_curved_surface": None, "curved_check_error": None, "shell": None}

    has_curved_surface = False
    curved_check_error = None
    try:
        for body in bodies:
            for face in body.faces:
                if not isinstance(face.geometry, adsk.core.Plane):
                    has_curved_surface = True
                    break
            if has_curved_surface:
                break
    except Exception as e:
        curved_check_error = str(e)

    shell = None
    if curved_check_error is None and not has_curved_surface:
        timeline = design.timeline
        for i in range(timeline.count):
            item = timeline.item(i)
            try:
                entity = item.entity
            except Exception:
                continue
            if entity is None or type(entity).__name__ != "ShellFeature":
                continue

            name = getattr(entity, "name", None) or f"TimelineItem{i}"
            thickness_mm = None
            thickness_error = None
            try:
                thickness_param = entity.insideThickness
                if thickness_param is None or thickness_param.value == 0:
                    thickness_param = entity.outsideThickness
                if thickness_param is not None:
                    thickness_mm = thickness_param.value * 10.0  # cm -> mm
                else:
                    thickness_error = "Neither insideThickness nor outsideThickness returned a value."
            except Exception as e:
                thickness_error = str(e)

            shell = {"feature_name": name, "thickness_mm": thickness_mm, "error": thickness_error}
            break  # first Shell feature only, matching solidworks_adapter.py

    return {
        "has_bodies": True,
        "has_curved_surface": has_curved_surface,
        "curved_check_error": curved_check_error,
        "shell": shell,
    }


def _get_dfm_draft() -> dict:
    """One entry per DraftFeature in the design's Timeline.

    DraftFeature.draftDefinition is either an AngleExtentDefinition (a
    single ModelParameter angle -- radians, matching SolidWorks'
    DraftAngle/Angle convention, converted to degrees here) or a
    TwoSidesAngleExtentDefinition (independent angleOne/angleTwo -- both
    confirmed as distinct real properties in the API reference). A
    two-sided draft has no single angle value to report, so it's flagged
    with two_sided=True instead of guessing/averaging one.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    drafts = []
    timeline = design.timeline
    for i in range(timeline.count):
        item = timeline.item(i)
        try:
            entity = item.entity
        except Exception:
            continue
        if entity is None or type(entity).__name__ != "DraftFeature":
            continue

        name = getattr(entity, "name", None) or f"TimelineItem{i}"
        angle_deg = None
        two_sided = False
        error = None
        try:
            definition = entity.draftDefinition
            if isinstance(definition, adsk.fusion.TwoSidesAngleExtentDefinition):
                two_sided = True
            elif isinstance(definition, adsk.fusion.AngleExtentDefinition):
                angle_deg = math.degrees(definition.angle.value)
            else:
                error = f"Unrecognized draft definition type: {type(definition).__name__}"
        except Exception as e:
            error = str(e)

        drafts.append({
            "feature_name": name,
            "angle_deg": angle_deg,
            "two_sided": two_sided,
            "error": error,
        })

    return {"drafts": drafts}


# -- Assembly traversal endpoints ---------------------------------------


def _is_occurrence_suppressed(occurrence) -> bool:
    """Best-effort suppression check, mirroring solidworks_adapter.py's
    _is_component_suppressed try-then-fallback pattern.

    Occurrence has NO documented `isSuppressed` property in the Fusion
    API reference (only Feature.isSuppressed is documented) -- confirmed
    by checking Autodesk's own Occurrence property list before writing
    this, not guessed. Tried anyway via getattr, since some Fusion API
    builds expose members beyond what's documented; falls back to False
    (not suppressed) if it doesn't resolve, so a missing property can't
    crash the whole traversal.
    """
    try:
        return bool(occurrence.isSuppressed)
    except Exception:
        return False


def _is_occurrence_invisible(occurrence) -> bool:
    """isLightBulbOn is Occurrence's documented visibility toggle -- the
    closest Fusion equivalent to "this isn't a real manufacturable part
    that should show up in a BOM" when isSuppressed isn't available.
    """
    try:
        return not bool(occurrence.isLightBulbOn)
    except Exception:
        return False


def _read_component_bounding_box_mm(component):
    """Axis-aligned bounding box (x, y, z) in millimeters, unioned across
    the component's own bodies -- same "part's own local geometry, not
    its rotated placement in the assembly" scope as
    solidworks_adapter.py's get_bounding_box_mm(). BRepBody.boundingBox
    returns a BoundingBox3D with minPoint/maxPoint Point3D objects in cm
    (Fusion's internal length unit, same convention used everywhere else
    in this bridge).
    """
    bodies = list(component.bRepBodies)
    if not bodies:
        return None

    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    found_any = False
    for body in bodies:
        try:
            box = body.boundingBox
        except Exception:
            continue
        found_any = True
        xmin, ymin, zmin = min(xmin, box.minPoint.x), min(ymin, box.minPoint.y), min(zmin, box.minPoint.z)
        xmax, ymax, zmax = max(xmax, box.maxPoint.x), max(ymax, box.maxPoint.y), max(zmax, box.maxPoint.z)

    if not found_any:
        return None
    return [(xmax - xmin) * 10.0, (ymax - ymin) * 10.0, (zmax - zmin) * 10.0]


def _read_component_material(component):
    """Material of the component's first body -- same single-material-
    per-part scope as _get_material() (which reads the whole design's
    first body); reused here per-component instead of per-document.
    """
    bodies = list(component.bRepBodies)
    if not bodies:
        return None
    try:
        material = bodies[0].material
    except Exception:
        return None
    if material is None:
        return None

    name = "Not specified"
    try:
        name = material.name
    except Exception:
        pass

    density_kg_m3 = None
    try:
        density_prop = material.materialProperties.itemByName("Density")
        if density_prop is not None:
            density_kg_m3 = density_prop.value
    except Exception:
        pass

    return {"name": name, "density_kg_m3": density_kg_m3, "category": ""}


def _read_component_bend_count(design, component) -> int:
    """Same Sheet Metal bend-feature detection as _get_bend_count(), but
    scoped to features owned by this one component -- Fusion's Timeline
    is document-wide (one timeline covers every component in a
    single-document assembly), so each entry's `.parentComponent` (a
    documented Feature property) is what scopes it to a specific
    component, unlike SolidWorks where each component is a separate
    document with its own independent feature tree.
    """
    count = 0
    timeline = design.timeline
    for i in range(timeline.count):
        item = timeline.item(i)
        try:
            entity = item.entity
        except Exception:
            continue
        if entity is None or type(entity).__name__ not in _SHEET_METAL_BEND_FEATURE_TYPES:
            continue
        try:
            owner = entity.parentComponent
        except Exception:
            owner = None
        if owner is not None and owner.id == component.id:
            count += 1
    return count


def _get_is_assembly() -> dict:
    """A Fusion design "is an assembly" if its root component references
    any occurrences at all -- Fusion has no separate assembly/part
    document type the way SOLIDWORKS does (.sldasm vs .sldprt); it's
    purely structural.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    try:
        is_asm = design.rootComponent.occurrences.count > 0
    except Exception as e:
        return {"error": f"Could not read the root component's occurrences: {e}"}

    return {"is_assembly": is_asm}


def _get_assembly_components() -> dict:
    """Recursively walk the design's occurrence tree (root ->
    childOccurrences, all the way down) and return one row per unique
    Component, aggregated by Component.id across the WHOLE tree --
    Fusion's occurrence model means the same Component can legitimately
    be referenced by many Occurrences (e.g. 8 identical bolts), and
    Component.id (a documented persistent, stable identifier) is what
    identifies "the same underlying part" the way a (file_path,
    configuration) pair does for solidworks_adapter.py.

    An occurrence with further childOccurrences is a sub-assembly (no row
    emitted for it -- only its leaf descendants become rows, exactly
    matching solidworks_adapter.py's get_assembly_components()).
    Suppressed and invisible (light-bulb-off) occurrences are skipped
    entirely during the walk, not filtered out afterward -- see
    _is_occurrence_suppressed/_is_occurrence_invisible above.

    **Known scope limitation, not yet live-verified against a real
    multi-document assembly**: this only resolves components modeled
    inside the SAME Fusion document. An occurrence referencing a
    component from a separate linked/external document (a genuinely
    different Design/Timeline) is not specially handled -- its own
    internal structure may not resolve correctly. This mirrors
    solidworks_adapter.py's own "not yet live-verified against a real
    assembly" disclaimer on get_assembly_components().

    file_path and configuration are always "" -- Fusion components
    living inside one cloud document don't have local file paths the way
    separate SOLIDWORKS part files do, and this project doesn't attempt
    to resolve Fusion's (rarely used) Configurations feature here.
    """
    design = _get_design()
    if design is None:
        return {"error": "The active document is not a Fusion Design (parametric modeling) document."}

    root = design.rootComponent
    if root.occurrences.count == 0:
        return {"error": "The active document's root component has no occurrences -- it is not an assembly."}

    aggregated: dict[str, dict] = {}
    order: list[str] = []

    def visit(occurrence, parent_assembly, level) -> None:
        if occurrence is None:
            return
        if _is_occurrence_suppressed(occurrence) or _is_occurrence_invisible(occurrence):
            return

        component = occurrence.component
        try:
            comp_name = occurrence.name or component.name
        except Exception:
            comp_name = component.name

        children = None
        try:
            children = occurrence.childOccurrences
        except Exception:
            pass

        if children is not None and children.count > 0:
            # Sub-assembly: recurse one level deeper instead of emitting
            # a row for the sub-assembly itself.
            for child in children:
                visit(child, comp_name, level + 1)
            return

        key = component.id

        if key not in aggregated:
            mass_kg = None
            volume_m3 = None
            try:
                props = occurrence.physicalProperties
                mass_kg = props.mass  # already kg, per PhysicalProperties docs
                volume_m3 = props.volume / 1_000_000.0  # cm^3 -> m^3
            except Exception:
                pass

            face_count = None
            try:
                face_count = sum(body.faces.count for body in component.bRepBodies)
            except Exception:
                pass

            aggregated[key] = {
                "part_name": component.name,
                "file_path": "",
                "configuration": "",
                "mass_kg": mass_kg,
                "volume_m3": volume_m3,
                "material": _read_component_material(component),
                "face_count": face_count,
                "bend_count": _read_component_bend_count(design, component),
                "bounding_box_mm": _read_component_bounding_box_mm(component),
                "parent_assembly": parent_assembly,
                "level": level,
                "quantity": 0,
            }
            order.append(key)

        aggregated[key]["quantity"] += 1

    for occurrence in root.occurrences:
        visit(occurrence, None, 0)

    return {"components": [aggregated[key] for key in order]}


# -- Open document --------------------------------------------------------


def _find_data_files_by_name(folder, name_lower: str) -> list:
    """Recursively search a DataFolder (and every subfolder) for
    DataFiles whose name matches `name_lower` (case-insensitive exact
    match). Returns every match, not just the first, so an ambiguous name
    can be reported honestly instead of silently opening the wrong one.
    """
    matches = []
    try:
        for f in folder.dataFiles:
            try:
                if f.name.lower() == name_lower:
                    matches.append(f)
            except Exception:
                continue
    except Exception:
        pass

    try:
        for sub_folder in folder.dataFolders:
            matches.extend(_find_data_files_by_name(sub_folder, name_lower))
    except Exception:
        pass

    return matches


def _open_document_impl(file_path: str) -> dict:
    """Open a design by name so it becomes Fusion's active document.

    **MUST be called on Fusion's main thread** -- see
    _OpenDocumentEventHandler and _open_document() below, which marshal
    onto the main thread via registerCustomEvent/fireCustomEvent before
    calling this. Confirmed live (2026-08-29) that calling
    Documents.open() directly from the HTTP handler's background thread
    silently "succeeds" (returns a Document, no exception) but does NOT
    actually make it Fusion's active document -- get_current_part_info()
    right after still showed the previous document. This function itself
    is unchanged from that first attempt; only how it's invoked changed.

    Fusion is cloud-based: documents live in a Project/Folder/File
    hierarchy in the Data Panel, not at an OS filesystem path the way a
    SOLIDWORKS .SLDPRT does. `file_path` is therefore matched against the
    Data Panel document NAME (case-insensitive exact match), searched
    recursively through the CURRENTLY ACTIVE project's folder tree only
    -- not by any real filesystem path, and not across every project in
    the hub (a documented scope limitation, not an oversight).

    Ambiguous (same name in two folders) or not-found results in an
    "error" response rather than guessing which one to open.
    """
    app = adsk.core.Application.get()

    try:
        project = app.data.activeProject
    except Exception as e:
        return {"error": f"Could not read the active Data Panel project: {e}"}

    if project is None:
        return {
            "error": (
                "No active Fusion Team/Data Panel project is available to "
                "search -- make sure Fusion is signed in, online, and has "
                "a project open in the Data Panel."
            )
        }

    name_lower = file_path.strip().lower()
    if not name_lower:
        return {"error": "file_path must be a non-empty document name."}

    try:
        root_folder = project.rootFolder
    except Exception as e:
        return {"error": f"Could not read the active project's root folder: {e}"}

    matches = _find_data_files_by_name(root_folder, name_lower)

    if not matches:
        project_name = "unknown"
        try:
            project_name = project.name
        except Exception:
            pass
        return {
            "error": (
                f"No design named '{file_path}' was found in the active "
                f"Data Panel project ('{project_name}'). Fusion documents "
                "are identified by their Data Panel name, not an OS file "
                "path -- check the exact name shown in the Data Panel "
                "(matching is case-insensitive but must be exact, and "
                "only searches the currently active project, not every "
                "project in the hub)."
            )
        }

    if len(matches) > 1:
        return {
            "error": (
                f"'{file_path}' matches {len(matches)} different designs "
                "in the active project (same name in different folders) "
                "-- ambiguous, not opening any of them."
            )
        }

    data_file = matches[0]
    try:
        opened_doc = app.documents.open(data_file, True)
    except Exception as e:
        return {"error": f"Fusion refused to open '{file_path}': {e}"}

    if opened_doc is None:
        return {
            "error": (
                f"Fusion's Documents.open() returned no document for "
                f"'{file_path}' -- the open failed for an unspecified "
                "reason."
            )
        }

    title = file_path
    try:
        title = opened_doc.name
    except Exception:
        pass

    return {"opened": True, "title": title}


# -- Main-thread marshaling for _open_document_impl ----------------------
#
# registerCustomEvent/fireCustomEvent is Fusion's documented mechanism for
# a worker thread to hand work to the add-in's primary thread. The HTTP
# handler thread fires the event with a request id + file_path; the
# handler (running on the main thread) does the real work and signals a
# threading.Event the HTTP thread is blocked on, so the HTTP response
# still returns synchronously once Fusion finishes opening the document.

_OPEN_DOCUMENT_EVENT_ID = "cad_ai_copilot_open_document_event"
_pending_open_requests: dict = {}  # request_id -> {"event": threading.Event, "result": dict}
_event_handlers = []  # keeps handler instances alive -- Fusion drops unreferenced ones

_request_id_counter = 0
_request_id_lock = threading.Lock()


def _next_request_id() -> str:
    global _request_id_counter
    with _request_id_lock:
        _request_id_counter += 1
        return str(_request_id_counter)


class _OpenDocumentEventHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        request_id = None
        try:
            payload = json.loads(args.additionalInfo)
            request_id = payload["request_id"]
            result = _open_document_impl(payload["file_path"])
        except Exception as e:
            result = {"error": f"Bridge failed opening document on the main thread: {e}"}

        pending = _pending_open_requests.get(request_id)
        if pending is not None:
            pending["result"] = result
            pending["event"].set()


def _open_document(file_path: str, timeout_seconds: float = 30.0) -> dict:
    """Thread-safe entry point called by the HTTP handler: hands the real
    work to _open_document_impl() on Fusion's main thread via a custom
    event, then blocks (this is fine -- it's the HTTP handler thread, not
    the main thread) until that finishes or `timeout_seconds` elapses.
    """
    request_id = _next_request_id()
    event = threading.Event()
    _pending_open_requests[request_id] = {"event": event, "result": None}

    try:
        adsk.core.Application.get().fireCustomEvent(
            _OPEN_DOCUMENT_EVENT_ID,
            json.dumps({"request_id": request_id, "file_path": file_path}),
        )
    except Exception as e:
        _pending_open_requests.pop(request_id, None)
        return {"error": f"Could not dispatch open_document to Fusion's main thread: {e}"}

    completed = event.wait(timeout=timeout_seconds)
    pending = _pending_open_requests.pop(request_id, None)

    if not completed or pending is None or pending["result"] is None:
        return {
            "error": (
                f"Timed out after {timeout_seconds:.0f}s waiting for Fusion "
                f"to open '{file_path}' on the main thread -- it may still "
                "be loading, or the document is very large."
            )
        }

    return pending["result"]


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/ping":
            self._send_json(200, {"status": "ok", "message": "Fusion bridge Add-In is running."})
            return

        if path == "/open_document":
            name_values = query.get("name")
            if not name_values or not name_values[0]:
                self._send_json(400, {"error": "Missing required query parameter 'name'."})
                return
            try:
                result = _open_document(name_values[0])
            except Exception as e:
                self._send_json(500, {"error": f"Bridge failed handling '/open_document': {e}"})
                return
            self._send_json(200, result)
            return

        endpoints = {
            "/part_info": _get_active_document_info,
            "/mass": _get_mass_kg,
            "/material": _get_material,
            "/features": _get_features,
            "/face_count": _get_face_count,
            "/bend_count": _get_bend_count,
            "/dfm/holes": _get_dfm_holes,
            "/dfm/wall_thickness": _get_dfm_wall_thickness,
            "/dfm/draft": _get_dfm_draft,
            "/is_assembly": _get_is_assembly,
            "/assembly_components": _get_assembly_components,
        }
        handler = endpoints.get(path)
        if handler is not None:
            try:
                result = handler()
            except Exception as e:
                self._send_json(500, {"error": f"Bridge failed handling '{path}': {e}"})
                return
            self._send_json(200, result)
            return

        self._send_json(
            404,
            {"error": f"Unknown endpoint '{path}'. Try one of: {', '.join(list(endpoints) + ['/open_document'])}."},
        )

    # BaseHTTPRequestHandler logs every request to stderr by default, which
    # Fusion has nowhere sensible to show -- silence it.
    def log_message(self, format, *args):
        pass


def run(context):
    global _app, _ui, _httpd, _server_thread
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        custom_event = _app.registerCustomEvent(_OPEN_DOCUMENT_EVENT_ID)
        open_document_handler = _OpenDocumentEventHandler()
        custom_event.add(open_document_handler)
        _event_handlers.append(open_document_handler)

        _httpd = HTTPServer((BRIDGE_HOST, BRIDGE_PORT), _BridgeRequestHandler)
        _server_thread = threading.Thread(target=_httpd.serve_forever, daemon=True)
        _server_thread.start()

        _ui.messageBox(
            "CAD AI Copilot bridge started.\n"
            f"Listening on http://{BRIDGE_HOST}:{BRIDGE_PORT}\n\n"
            "Leave this Add-In running while using the copilot; disable it "
            "from Utilities > Add-Ins when done."
        )
    except Exception:
        if _ui:
            _ui.messageBox(f"Failed to start CAD AI Copilot bridge:\n{traceback.format_exc()}")


def stop(context):
    global _httpd, _server_thread
    try:
        if _httpd is not None:
            _httpd.shutdown()
            _httpd.server_close()
            _httpd = None
        if _server_thread is not None:
            _server_thread.join(timeout=2.0)
            _server_thread = None
        if _app is not None:
            _app.unregisterCustomEvent(_OPEN_DOCUMENT_EVENT_ID)
        _event_handlers.clear()
    except Exception:
        if _ui:
            _ui.messageBox(f"Error stopping CAD AI Copilot bridge:\n{traceback.format_exc()}")
