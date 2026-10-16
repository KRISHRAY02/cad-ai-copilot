@echo off
REM ---------------------------------------------------------------------
REM build_exe.bat -- packages the CAD AI Copilot desktop app (desktop_app.py)
REM into a standalone Windows .exe using `flet pack` (PyInstaller under
REM the hood). Re-run this any time after changing code or data files.
REM
REM Must be run from the project root (the folder this .bat lives in).
REM
REM Usage:
REM   build_exe.bat          -- normal build, no console window on launch
REM   build_exe.bat debug    -- keeps a console window open on launch, so
REM                              Python tracebacks/print output are visible
REM                              instead of silently swallowed -- use this
REM                              to debug a build that fails to start.
REM ---------------------------------------------------------------------

setlocal

REM Python interpreter that has flet / mcp / ollama / pywin32 / sklearn /
REM joblib / bcrypt installed. Change this if your setup differs -- see
REM PACKAGING_NOTES.md for how this project's environment was set up.
set PYTHON=C:\Python314\python.exe
set FLET=C:\Users\Admin\AppData\Roaming\Python\Python314\Scripts\flet.exe

if not exist "%FLET%" (
    echo [build_exe] flet CLI not found at %FLET%
    echo [build_exe] Install it with: %PYTHON% -m pip install flet
    exit /b 1
)

set DEBUG_CONSOLE_FLAG=
if /i "%~1"=="debug" (
    echo [build_exe] Debug build: packaged .exe will keep a console window open.
    set DEBUG_CONSOLE_FLAG=--debug-console show
)

echo [build_exe] Cleaning previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [build_exe] Packaging desktop_app.py with flet pack...

"%FLET%" pack desktop_app.py ^
    --name CADCopilot ^
    %DEBUG_CONSOLE_FLAG% ^
    --add-data "materials.csv:." ^
    --add-data "materials_fusion.csv:." ^
    --add-data "am_materials.csv:." ^
    --add-data "standard_hardware.csv:." ^
    --add-data "cost_model\trained_model.joblib:cost_model" ^
    --hidden-import win32com ^
    --hidden-import win32com.client ^
    --hidden-import pythoncom ^
    --hidden-import pywintypes ^
    --hidden-import sklearn ^
    --hidden-import sklearn.ensemble ^
    --hidden-import sklearn.tree ^
    --hidden-import joblib ^
    --hidden-import mcp ^
    --hidden-import mcp.server.mcpserver ^
    --hidden-import mcp.client.stdio ^
    --hidden-import ollama ^
    --hidden-import bcrypt ^
    -y

if errorlevel 1 (
    echo [build_exe] flet pack FAILED.
    exit /b 1
)

echo.
echo [build_exe] Done. Executable: dist\CADCopilot.exe
echo [build_exe] Also copy the fusion_bridge_addin\ folder next to the .exe
echo [build_exe] if the target machine will use the Fusion 360 adapter --
echo [build_exe] it is a separate Fusion Add-In installed into Fusion itself,
echo [build_exe] not something the .exe bundles or launches. See PACKAGING_NOTES.md.

endlocal
