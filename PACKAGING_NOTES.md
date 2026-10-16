# Packaging CAD AI Copilot as a standalone .exe

This documents the packaging smoke test done on 2026-10-16: what the app
needs beyond its Python source, what broke when run as a packaged .exe,
and what was fixed. No features or AI/CAD logic were changed -- this was
purely about making the existing app launchable as a standalone
executable.

## How to build

1. Make sure `materials.csv`, `materials_fusion.csv`, `am_materials.csv`,
   `standard_hardware.csv`, and `cost_model/trained_model.joblib` exist
   (they do in a normal checkout; the model file is gitignored and
   regenerated via `cost_model/generate_synthetic_dataset.py` then
   `cost_model/train_model.py` if missing).
2. `C:\Python314\python.exe -m pip install -r requirements.txt` (now
   includes `pyinstaller`, used by `flet pack`).
3. Run `build_exe.bat` from the project root. Re-run it any time after
   changing code or data files -- it always does a clean build (wipes
   `build/`, `dist/` first).
   - `build_exe.bat` -- normal build, no console window when the .exe runs.
   - `build_exe.bat debug` -- same build, but keeps a console window open
     so Python tracebacks/print output are visible instead of silently
     swallowed. Use this if a build fails to start and you need to see why.
4. Output: `dist\CADCopilot.exe` (single file, ~160 MB -- bundles the
   Python interpreter, sklearn/pandas/numpy, the Flet runtime, and the
   data files below).

If `flet` isn't installed under the `flet` CLI's own Python (it may live
under a per-user `AppData\Roaming\Python\...\Scripts\flet.exe` rather than
`C:\Python314\Scripts`), edit the `FLET` path at the top of
`build_exe.bat`.

## What the running app needs beyond Python code (the inventory)

Found by reading the code, not guessing:

| What | Used from | Kind |
|---|---|---|
| `materials.csv` | `materials_db.py:12`, `cad_adapters/base_adapter.py:45` | bundled, read-only |
| `materials_fusion.csv` | `materials_db.py:13` | bundled, read-only |
| `am_materials.csv` | `am_material_advisor.py:23` | bundled, read-only |
| `standard_hardware.csv` | `hardware_db.py:17` | bundled, read-only |
| `cost_model/trained_model.joblib` | `cost_model/predict.py:20` | bundled, read-only |
| `chat_history.db` | `chat_db.py:21` | **writable**, per-user |
| `tool_call_log.jsonl` | `ai_orchestrator.py:49` | **writable**, per-user (append log) |
| exported PDFs (`generate_report`) | `mcp_server.py:982`, `desktop_app.py` export button | **writable**, per-user |
| exported BOM `.xlsx` | `mcp_server.py:1318`, `desktop_app.py` export button | **writable**, per-user |
| `mcp_server.py` | launched as a subprocess by `ai_orchestrator.py:945-966` (`AiOrchestrator.__init__`) | subprocess, needs a Python interpreter unless packaged |
| `fusion_bridge_addin/` | a separate Fusion 360 Add-In the user installs **into Fusion itself** (`cad_adapters/fusion_adapter.py`); talked to over local HTTP, never imported/copied by the app at runtime | external, not bundled |
| Ollama + `qwen2.5:7b-instruct` | `ai_orchestrator.py` (`ollama.AsyncClient`), model name in `DEFAULT_MODEL` | external service, must be installed/running separately |

## Problems found and fixed

### 1. Bundled data files resolved relative to `__file__`, not the bundle

Every data-file consumer (`materials_db.py`, `am_material_advisor.py`,
`hardware_db.py`, `cost_model/predict.py`) built its path as
`Path(__file__).parent / "whatever.csv"`. In a PyInstaller **onefile**
build, the app is extracted to a fresh temp directory at launch
(`sys._MEIPASS`), and `__file__` for a frozen module does not point at a
real directory containing the original data files -- these lookups would
have silently pointed at a path that doesn't exist.

**Fix:** new `app_paths.py` module, `bundled_path(relative)`: resolves
against `sys._MEIPASS` when present (packaged), else falls back to
`Path(__file__).parent` (source) -- same behavior as before when running
from source, correct behavior when packaged. All four files above were
updated to use it instead of building `Path(__file__).parent / ...`
directly.

### 2. Writable files (DB, logs, exports) would have lived in a temp folder that's wiped every launch

`chat_db.py`'s `DB_PATH` and `ai_orchestrator.py`'s tool-call log were
also built from `Path(__file__).parent` -- in a packaged onefile build
that resolves inside the `_MEIPASS` extraction directory, which is a
**brand-new temp folder every launch and is deleted on exit**. Chat
history and logs would have silently vanished after every session.

**Fix:** `app_paths.py`'s `user_data_dir()` resolves to
`%APPDATA%\CADCopilot`, created on first access. `chat_db.DB_PATH` and
the tool-call-log file handler in `ai_orchestrator.py` now use
`user_data_path(...)`. The two default export paths (PDF report, BOM
`.xlsx`) in both `mcp_server.py` and `desktop_app.py` were moved from
`tempfile.gettempdir()` to a new `exports_dir()` (`%APPDATA%\CADCopilot\exports`)
so exported files are easy to find again later instead of sitting in a
temp folder a user wouldn't think to check.

**Verified**: ran the packaged .exe, confirmed `chat_history.db` and
`tool_call_log.jsonl` are created under `%APPDATA%\CADCopilot\` on first
launch, not next to the .exe or in a temp folder.

**Migration note**: this also changes where `chat_history.db` lives when
running **from source** (not just packaged) -- it was previously created
next to `chat_db.py` in the project root. The existing project-root
`chat_history.db` (Krish's real chat history, 286 KB as of this pass) was
copied once to `%APPDATA%\CADCopilot\chat_history.db` during this smoke
test so it isn't orphaned; the old project-root copy was left in place
untouched (not deleted) in case this needs to be reverted. Future
`python desktop_app.py` runs will read/write the `%APPDATA%` copy only.

### 3. The MCP server subprocess assumed a Python interpreter exists

`AiOrchestrator.__init__` (`ai_orchestrator.py`) launched the MCP server
as `sys.executable mcp_server.py` -- fine from source (`sys.executable` is
`python.exe`), but a packaged .exe's `sys.executable` **is the .exe
itself**, and `mcp_server.py` isn't guaranteed to exist as a loose file
next to it, nor is there necessarily any other Python on the target
machine.

**Fix:** `AiOrchestrator.__init__` now checks `sys.frozen` (set by
PyInstaller). When frozen, it launches `<this same .exe> --mcp-server`
instead of `python mcp_server.py`. `desktop_app.py`'s `if __name__ ==
"__main__":` block checks `sys.argv` for `--mcp-server` first and, if
present, runs `mcp_server.mcp.run()` directly (the exact same stdio MCP
server `mcp_server.py` runs standalone) instead of opening the chat
window. Source-mode behavior is unchanged -- `sys.frozen` is unset there,
so it still runs `python mcp_server.py` exactly as before.

**Verified end-to-end**: drove a real `AiOrchestrator` session against
`dist\CADCopilot.exe --mcp-server` as the MCP backend (both a raw MCP
`get_mass`/`estimate_cost` tool call, and a full Ollama -> tool-call ->
answer round trip via `AiOrchestrator.ask()`). Both the mass question and
the cost question returned correct answers, confirming the packaged .exe
can serve as its own MCP server subprocess with no Python installed
separately.

### 4. No startup self-check surfaced plain-language failures

`desktop_app.py` already had a `check_environment()` that checked Ollama
reachability and that the MCP server started, but didn't check: the
model itself is pulled (vs. just Ollama being up), the bundled data files
are actually present, or the writable user-data folder is actually
writable. A missing file or un-pulled model would have surfaced later as
a confusing per-tool error instead of a clear message at launch.

**Fix:** new `run_startup_self_check()` in `desktop_app.py`, five
independent checks (each runs regardless of whether an earlier one
failed, so one launch surfaces everything wrong at once):
1. Data files found (checks all 5 bundled files via `bundled_path`)
2. User data folder writable (round-trip write+delete a probe file)
3. Ollama reachable
4. The actual model (`qwen2.5:7b-instruct`) is pulled, not just "Ollama is up"
5. CAD MCP server subprocess starts (same path real chat questions use,
   including the packaged `--mcp-server` relaunch)

Each failure includes a plain-language fix ("Install Ollama from
https://ollama.com, run `ollama serve`..."), no stack traces.
`check_environment()` is kept as a thin wrapper for the existing banner
UI, now showing every problem found, not just the first.

**Verified**: ran the self-check from source with all 5 checks passing;
then temporarily renamed `materials.csv` away and re-ran it, confirming
it correctly reported `Data files: False -- Missing: materials.csv...`
with the other 4 checks still passing independently. File was restored
immediately after (`git status` confirmed clean before and after).

## Test plan: what was actually verified, and what wasn't

| Test | Result |
|---|---|
| The .exe builds | **Pass** -- `build_exe.bat` ran clean, produced `dist\CADCopilot.exe` |
| The .exe starts, doesn't crash | **Pass** -- process stays alive and responsive 15+ seconds after launch, memory footprint (~700MB working set once the real app process is up) consistent with the full Flet/sklearn/pandas stack having loaded successfully |
| DB and logs created in the user folder | **Pass** -- confirmed `%APPDATA%\CADCopilot\chat_history.db` and `tool_call_log.jsonl` exist after launch, not next to the .exe |
| Startup self-check logic | **Pass** -- all 5 checks verified individually from source (including a deliberate negative test: a missing data file is correctly caught with a plain-language message) |
| MCP server works when the .exe is its own subprocess (`--mcp-server`) | **Pass** -- verified directly via a raw MCP session against `CADCopilot.exe --mcp-server`, and via a full `AiOrchestrator` session |
| **One mass question works** | **Pass** -- "What is the mass of this part?" through the real Ollama + qwen2.5:7b-instruct + packaged-.exe-as-MCP-server loop returned "The mass of the currently open part is 0.842 kg." |
| **One cost question works** | **Pass** -- "Estimate the cost for CNC Machining, quantity 10." through the same full loop returned a correct, complete cost breakdown matching the real `estimate_cost` tool's output |
| Login works (UI) | **Not verified visually.** The .exe runs and `chat_db.init_db()` demonstrably executes (the DB file is created), but this environment's screenshot/window-capture tooling could not get a reliable capture of the app window -- a pre-existing, previously-documented limitation of this specific sandboxed environment (see this project's `desktop_app.py` history notes on Flet rendering capture), not something newly introduced by packaging. Could not click through the actual login form. |
| CAD popup detects SolidWorks/Fusion | **Not tested.** No SolidWorks or Fusion 360 session was open/available in this environment during the smoke test. The detection code itself (`_detect_solidworks`/`_detect_fusion360` in `desktop_app.py`) was not touched by any packaging change, so it should behave identically to the already-verified source-mode behavior, but this wasn't re-confirmed against the packaged .exe specifically. |

**Honest summary**: everything that can be tested headlessly (data file
resolution, writable-folder creation, the MCP subprocess mechanism, and a
full real question through Ollama -> MCP -> tool -> answer, for both a
mass and a cost question) was tested against the actual packaged .exe and
passed. The one thing genuinely not confirmed is visually clicking
through the GUI (login screen, CAD-detection popup) in the packaged
build -- the screenshot tooling in this environment could not reliably
capture the Flet window. Krish should do one manual click-through on his
own machine before considering this packaging done; if that surfaces a
problem, it's very unlikely to be in paths/subprocess-launch/self-check
(all independently verified above) and more likely a UI-layer issue
unrelated to packaging.

## What a new machine needs (besides copying the .exe)

- **Ollama** installed and running (`ollama serve`), with
  `ollama pull qwen2.5:7b-instruct` done at least once. The self-check
  screen will say so clearly if either is missing.
- **SolidWorks or Fusion 360** installed, if that adapter is used (the
  Mock adapter needs neither, but isn't exposed as a normal user-facing
  option in the current UI).
- If using the **Fusion 360 adapter**: copy `fusion_bridge_addin\` next to
  the .exe and enable it as a Fusion Add-In (Utilities > Add-Ins >
  Scripts and Add-Ins > Add-Ins tab > fusion_bridge_addin > Run) -- this
  is a separate Fusion-side component, not something the .exe bundles or
  launches itself.
- No Python installation needed -- the .exe is fully self-contained
  (including its own MCP server, via `--mcp-server`).
- First launch creates `%APPDATA%\CADCopilot\` automatically (chat
  history, tool-call log, exports subfolder) -- no manual setup needed.

## Known non-issues / things deliberately left alone

- This was a packaging smoke test only -- no AI/CAD/cost-model logic was
  changed, per the task's instructions.
- `report/generate_report.py`'s screenshot capture, DFM checks, and every
  other feature already working from source were not touched and were
  not the focus of this pass (not re-verified against the packaged build
  beyond the mass/cost questions above).
- The `.exe` is large (~160 MB) because it statically bundles the Python
  interpreter, scikit-learn, pandas, numpy, and the Flet runtime. This is
  normal for a PyInstaller onefile scientific-Python app and wasn't
  investigated for size reduction (out of scope for a smoke test).
