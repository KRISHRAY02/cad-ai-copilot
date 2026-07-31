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

## The MCP server

`mcp_server.py` wraps a single `CadAdapter` instance and publishes its
capabilities as MCP tools — the standard mechanism an LLM uses to call
out to external functions. It selects which adapter to wrap via a
`CAD_ADAPTER` environment variable (defaulting to `SolidWorksAdapter`;
set `CAD_ADAPTER=mock` to use `MockAdapter` instead, with no CAD
software required), then registers seven tools built on top of the
adapter's methods:

- `get_current_part_info`, `get_mass`, `get_material`, `get_features` —
  direct pass-throughs to the corresponding `CadAdapter` methods.
- `estimate_cost` — a simple heuristic (material Rs/kg, looked up from
  materials.csv by material name or a per-category fallback, times mass,
  plus a flat machining fee) built from `get_mass()` and `get_material()`.
- `estimate_carbon` — the same pattern, using published average
  cradle-to-gate emission factors (kg CO2e per kg of material) instead of
  cost figures.
- `run_dfm_checks` — lightweight, rule-of-thumb design-for-manufacturing
  checks over the feature tree (e.g. fillet radii below a machinable
  minimum, suppressed features left in the tree, missing fillets/chamfers
  on sharp edges).

Every tool function calls only `adapter.get_...()` methods — never a
concrete adapter class — so this file is identical regardless of which
CAD platform is behind it. The derived tools (cost, carbon, DFM) are
themselves adapter-agnostic: they're just arithmetic and rule checks over
the data the adapter already returns.

## The AI orchestrator

`ai_orchestrator.py` implements `AiOrchestrator`, which is the only piece
of the system that talks to both the LLM and MCP. It:

1. Launches `mcp_server.py` as a subprocess and opens an MCP client
   session over stdio.
2. Fetches the server's tool definitions and converts each one into the
   JSON-schema function-calling format Ollama expects.
3. On each user question, sends the running conversation history plus the
   tool definitions to a local model (via `ollama.AsyncClient`, default
   `llama3.2`, chosen for its combination of tool-calling support and a
   small enough footprint to run comfortably on a laptop CPU).
4. If the model responds with one or more tool calls, executes them
   against the live MCP session, appends the results to the conversation
   as `tool` messages, and asks the model again — repeating up to a fixed
   number of rounds until it returns a plain-text answer instead of
   another tool call.

The orchestrator never imports a `CadAdapter` subclass, and never even
imports `cad_adapters` directly — its only CAD-related dependency is the
MCP tool schema it fetches at startup. Everything it knows about "the
current part" comes from tool results returned during the conversation.

## The UI

`app.py` is a Streamlit chat interface. It holds one `AiOrchestrator`
instance per user session (`st.session_state`), together with the
`asyncio` event loop used to drive it — necessary because Streamlit
reruns the whole script on every interaction, so the orchestrator (and
the MCP subprocess connection it owns) has to be created once and kept
alive across reruns rather than recreated each time. The UI layer's only
responsibilities are: render chat history, take a new question via
`st.chat_input`, hand it to `orchestrator.ask()`, and display the answer
(or a readable error if the backend or the LLM isn't reachable). It
contains no CAD logic and no LLM prompt logic of its own.

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
