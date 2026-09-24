@echo off
setlocal
cd /d "%~dp0"

rem Clean stale Trim/Fade frontend versions from unzip-over upgrades.
for %%F in ("%~dp0web\urn_trim_fade_visual_v6.js" "%~dp0web\urn_trim_fade_visual_v7.js" "%~dp0web\urn_trim_fade_visual_v8.js" "%~dp0web\urn_mp3_trim_fade.js") do (
    if exist "%%~F" del /q "%%~F" >nul 2>nul
)

echo =====================================================
echo URN Audio Nodes - combined dependency installer
echo =====================================================
echo.

set "PYEXE="

rem Normal ComfyUI Windows portable layout:
rem ComfyUI_windows_portable\ComfyUI\custom_nodes\URN_Audio_Tools\install.bat
if exist "%~dp0..\..\..\python_embeded\python.exe" set "PYEXE=%~dp0..\..\..\python_embeded\python.exe"

rem Other common layouts.
if not defined PYEXE if exist "%~dp0..\..\python_embeded\python.exe" set "PYEXE=%~dp0..\..\python_embeded\python.exe"
if not defined PYEXE if exist "%CD%\python_embeded\python.exe" set "PYEXE=%CD%\python_embeded\python.exe"

if not defined PYEXE (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Could not find ComfyUI embedded Python or system Python.
        echo.
        echo Put the "URN Audio Nodes" folder under ComfyUI\custom_nodes and run this file again.
        pause
        exit /b 1
    )
    set "PYEXE=python"
)

echo Using Python:
echo   %PYEXE%
echo.
echo Installing dependencies for the URN Audio Nodes:
echo   - URN Audio Smart Splitter
echo   - URN Smart Seamless Audio Extender
echo   - URN Audio Trim Fade

echo.
echo Existing compatible packages, including Torch/CUDA, are left in place.
echo.

"%PYEXE%" -m pip install -r "%~dp0requirements.txt" --upgrade-strategy only-if-needed
if errorlevel 1 (
    echo.
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo Checking required modules...
"%PYEXE%" -c "import importlib.util,sys; mods=['faster_whisper','audio_separator','librosa','scipy']; missing=[m for m in mods if importlib.util.find_spec(m) is None]; print('Missing: ' + ', '.join(missing) if missing else 'All URN Audio Nodes dependencies found.'); sys.exit(1 if missing else 0)"
if errorlevel 1 (
    echo.
    echo ERROR: One or more dependencies are still unavailable.
    pause
    exit /b 1
)

echo.
echo =====================================================
echo Installation complete.
echo Restart ComfyUI before using URN Audio Nodes.
echo Mel-RoFormer / Whisper model files download on first use when required.
echo =====================================================
pause
endlocal
