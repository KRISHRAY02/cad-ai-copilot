# Architecture

## Overview

The CAD AI copilot is split into four layers:

1. **CAD adapters** (`cad_adapters/`) — talk to the CAD application itself.
2. **MCP server** (`mcp_server.py`) — exposes CAD data and derived
   calculations (mass, material, cost estimate, carbon estimate, DFM
   checks) as tools an LLM can call.
3. **AI orchestrator** (`ai_orchestrator.py`) — routes natural-language
   questions to a local LLM (via Ollama), which calls MCP tools as needed
   and produces a natural-language answer.
4. **UI** (`app.py`) — a Streamlit chat interface for the user.

Each layer only depends on the one below it through a fixed interface, and
none of the upper three layers know anything about which specific CAD
package is running underneath.

## The adapter pattern

All CAD-specific code lives behind a single abstract interface,
`CadAdapter` (`cad_adapters/base_adapter.py`), defined with Python's `ABC`
module. It declares the contract every supported CAD platform must
fulfil:

- `connect()` — establish a connection to the running CAD application.
- `get_current_part_info()` — identify the active part (name, path, type,
  units).
- `get_mass()` — return the part's mass.
- `get_material()` — return the assigned material.
- `get_features()` — return the feature tree.

Concrete adapters (`SolidWorksAdapter`, `MockAdapter`, and any future
platform adapter such as one for Fusion 360 or Onshape) each implement
this same interface using whatever platform-specific mechanism is
required — COM automation for SolidWorks, a REST API for a cloud CAD
tool, or hardcoded values for the mock adapter used in testing.

## Why this exists: extensibility

The motivation is straightforward: **CAD platforms are interchangeable
implementation details, but the AI/MCP/UI stack is not CAD-specific and
shouldn't have to change when the underlying CAD tool does.**

Without this abstraction, SolidWorks-specific API calls (COM objects,
platform quirks, unit conventions) would leak into the MCP tool
definitions, the orchestrator's prompts, and potentially the UI. Adding
support for a second CAD platform later would then mean hunting down and
rewriting every place that assumed SolidWorks, with high risk of
regressions in the layers that had nothing to do with CAD access at all.

With the adapter pattern:

- `mcp_server.py` is written entirely against the `CadAdapter` interface.
  It calls `adapter.get_mass()`, not `solidworks_app.ModelDoc.MassProps`.
- Adding a new CAD platform means writing one new class that implements
  the same five methods — no changes to the MCP tool definitions, the AI
  orchestrator, or the Streamlit UI.
- The `MockAdapter` lets the entire AI/MCP/UI stack be developed and
  tested end-to-end without any CAD software installed or running,
  because it satisfies the exact same interface as a real adapter.

In short, `CadAdapter` is the seam in the system: everything on one side
of it is CAD-specific and disposable per platform; everything on the
other side is CAD-agnostic and reusable across platforms.
