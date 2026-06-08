@echo off
setlocal
cd /d "%~dp0"

set REL=release_artifacts
set SW=%REL%\software

echo === Build NoABS.Eagle3D release ===

:: build_venv: create if missing, always ensure deps
if not exist build_venv\Scripts\python.exe (
    echo Creating build_venv...
    python -m venv build_venv
)
echo Ensuring build deps (pillow, numpy, pyinstaller)...
build_venv\Scripts\python -m pip install --no-cache-dir --upgrade pip pillow numpy mapbox-earcut pyinstaller pywebview pythonnet
if errorlevel 1 ( echo FAILED: pip install & exit /b 1 )

:: conda stdlib DLLs (libexpat for pyexpat/xml, etc.) live in <base>\Library\bin.
:: Put them on PATH so PyInstaller's dependency scan bundles them into the exe.
for /f "delims=" %%i in ('build_venv\Scripts\python -c "import sys,os;print(os.path.join(sys.base_prefix,'Library','bin'))"') do set "CONDALIB=%%i"
if exist "%CONDALIB%" set "PATH=%CONDALIB%;%PATH%"
echo Using conda DLLs from: %CONDALIB%

:: clean previous outputs
if exist "%REL%\tools" rmdir /s /q "%REL%\tools"
if exist "%SW%" rmdir /s /q "%SW%"
mkdir "%SW%"

:: one-file tools: eagle2gltf.exe, eagle2step.exe -> software/
echo.
echo [1/5] Building eagle2gltf.exe...
build_venv\Scripts\pyinstaller software\eagle2gltf.spec -y --distpath "%SW%" --workpath build\gltf
if errorlevel 1 ( echo FAILED: eagle2gltf & exit /b 1 )

echo.
echo [2/5] Building eagle2step.exe...
build_venv\Scripts\pyinstaller software\eagle2step.spec -y --distpath "%SW%" --workpath build\step
if errorlevel 1 ( echo FAILED: eagle2step & exit /b 1 )

:: ULPs + noabs.ini next to the exes
echo.
echo [3/5] Copying ULPs + noabs.ini...
copy /y software\export3D.ulp             "%SW%\" >nul
copy /y software\export3D_raw_step1.ulp   "%SW%\" >nul
copy /y software\export3D_raw_step2.ulp   "%SW%\" >nul
copy /y software\export3D_raw_stepgen.ulp "%SW%\" >nul
copy /y software\export3D_raw_spawn.ulp   "%SW%\" >nul
copy /y software\noabs.ini                "%SW%\" >nul

:: bundles as siblings of software/
echo.
echo [4/5] Copying eagle_bundle + drawexe_bundle...
robocopy eagle_bundle   "%REL%\eagle_bundle"   /MIR /NJH /NJS /NDL /NFL >nul
robocopy drawexe_bundle "%REL%\drawexe_bundle" /MIR /NJH /NJS /NDL /NFL >nul

:: glue GUI
echo.
echo [5/5] Building glue...
build_venv\Scripts\pyinstaller glue\glue.spec -y --distpath "%REL%" --workpath build\glue
if errorlevel 1 ( echo WARN: glue build failed ^(non-fatal^) )

echo.
echo Done. Release in %REL%\ (software, eagle_bundle, drawexe_bundle, glue)
endlocal
