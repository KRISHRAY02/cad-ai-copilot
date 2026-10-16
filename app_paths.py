"""Central path resolution for running both from source and as a packaged
.exe (PyInstaller / `flet pack`).

Two different kinds of path exist in this project and they need different
resolution rules:

1. Bundled read-only files shipped with the app (materials.csv,
   materials_fusion.csv, am_materials.csv, standard_hardware.csv,
   cost_model/trained_model.joblib). When run from source these live next
   to the .py file that uses them (``Path(__file__).parent``). When
   PyInstaller packages a onefile .exe, it extracts everything named in
   ``--add-data`` into a temporary directory at startup and sets
   ``sys._MEIPASS`` to that directory's path -- bundled files must be read
   from there instead, since ``__file__`` for a frozen module does not
   point at a real directory containing the data files.

2. Writable files the app creates/updates while running (chat_history.db,
   tool_call_log.jsonl, exported PDFs/BOMs). These must NOT live next to
   ``__file__``/under ``_MEIPASS`` in a packaged build: a onefile .exe's
   ``_MEIPASS`` extraction directory is a fresh temp folder every launch
   (and is deleted on exit), so anything written there is silently lost
   between runs. They go in a per-user app-data folder instead
   (``%APPDATA%\\CADCopilot`` on Windows), created on first run, so a
   packaged .exe behaves like a normal installed Windows app: data
   persists across launches and the install location can be read-only.

Running from source is unaffected either way -- ``sys._MEIPASS`` is absent
so bundled_path() falls back to ``Path(__file__).parent``, and
user_data_dir() already pointed at %APPDATA% before packaging was a
concern.
"""

import os
import sys
from pathlib import Path

APP_NAME = "CADCopilot"


def bundled_path(relative: str) -> Path:
    """Resolve a bundled read-only resource (a CSV, the trained model,
    etc.) that ships alongside the app's source/executable.

    `relative` is relative to the project root (e.g. "materials.csv" or
    "cost_model/trained_model.joblib").
    """
    base = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
    return base / relative


def user_data_dir() -> Path:
    """Per-user writable folder for this app's database/logs/exports.

    %APPDATA%\\CADCopilot on Windows (falls back to the home directory if
    APPDATA isn't set, e.g. non-Windows). Created on first access.
    """
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home()
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_data_path(filename: str) -> Path:
    """Shorthand for user_data_dir() / filename."""
    return user_data_dir() / filename


def exports_dir() -> Path:
    """Per-user folder for exported reports/BOMs, created on first access."""
    path = user_data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_frozen() -> bool:
    """True when running as a PyInstaller-packaged executable."""
    return bool(getattr(sys, "frozen", False))
