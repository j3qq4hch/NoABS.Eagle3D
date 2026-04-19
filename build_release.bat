@echo off
setlocal
cd /d "%~dp0"

echo === Build release_artifacts ===

:: ── build_venv: create if missing ───────────────────────────────────────────
if not exist build_venv\Scripts\python.exe (
    echo Creating build_venv...
    python -m venv build_venv
    build_venv\Scripts\pip install pillow pyinstaller
)

:: ── tools: eagle2gltf, eagle2step ───────────────────────────────────────────
echo.
echo [1/3] Building eagle2gltf...
build_venv\Scripts\pyinstaller software\eagle2gltf.spec -y --distpath release_artifacts\tools
if errorlevel 1 ( echo FAILED: eagle2gltf & exit /b 1 )

echo.
echo [2/3] Building eagle2step...
build_venv\Scripts\pyinstaller software\eagle2step.spec -y --distpath release_artifacts\tools
if errorlevel 1 ( echo FAILED: eagle2step & exit /b 1 )

:: ── glue ─────────────────────────────────────────────────────────────────────
echo.
echo [3/3] Building glue...
pyinstaller glue\glue.spec -y --distpath release_artifacts
if errorlevel 1 ( echo FAILED: glue & exit /b 1 )

:: ── ULP files ────────────────────────────────────────────────────────────────
echo.
echo Copying ULPs...
copy /y software\export3d.ulp      release_artifacts\tools\export3D.ulp
copy /y software\export3d_step2.ulp release_artifacts\tools\export3D_step2.ulp

echo.
echo Done. release_artifacts is ready.
endlocal
