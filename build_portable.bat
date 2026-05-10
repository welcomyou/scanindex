@echo off
setlocal enabledelayedexpansion
title ScanIndex - Build Portable

cd /d "%~dp0"

set "APP_NAME=ScanIndex"
set "OLD_APP_NAME=Lightweight_OCR"
set "ENTRYPOINT=ocr_app.py"
set "SPEC_FILE=Lightweight_OCR.spec"
set "VENV_DIR=.venv_build"

REM Capture version from git tag (or fallback) so dist/ folder is named per release.
REM Mirrors d:/App/asr-vn/build-portable/build_portable.py pattern:
REM   DIST_DIR = dist/<app>-<version>/
set "APP_VERSION="
for /f "usebackq tokens=*" %%V in (`python -c "from scanindex.infra.version import get_version_short; print(get_version_short())" 2^>nul`) do set "APP_VERSION=%%V"
if "%APP_VERSION%"=="" set "APP_VERSION=dev"

set "DIST_DIR=dist\%APP_NAME%-%APP_VERSION%"
set "OLD_DIST_DIR=dist\%OLD_APP_NAME%"
set "LEGACY_DIST_DIR=dist\%APP_NAME%"
set "MODE=%~1"
set "INTERACTIVE=0"
if "%INCLUDE_CORRECTION%"=="" set "INCLUDE_CORRECTION=1"
if "%INCLUDE_LEGACY_CHROME%"=="" set "INCLUDE_LEGACY_CHROME=0"

echo ===================================================
echo   SCANINDEX - PORTABLE BUILD
echo ===================================================
echo.
echo   App:     %APP_NAME%  (%ENTRYPOINT%)
echo   Version: %APP_VERSION%
echo   Output:  %DIST_DIR%
echo.

if "%MODE%"=="" (
    set "INTERACTIVE=1"
    echo   [1] Full build       ^(venv + deps + model checks + exe^)
    echo   [2] Quick rebuild    ^(code/spec changes only, reuse venv^)
    echo.
    set /p MODE="Select mode (1/2): "
)

if /I "%MODE%"=="full" set "MODE=1"
if /I "%MODE%"=="quick" set "MODE=2"
if "%MODE%"=="" set "MODE=2"

if not "%MODE%"=="1" if not "%MODE%"=="2" (
    echo ERROR: Invalid mode "%MODE%". Use 1/full or 2/quick.
    goto :fail
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 goto :fail
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 goto :fail

if "%MODE%"=="1" (
    echo.
    echo [FULL] Installing dependencies...
    python -m pip install --upgrade pip
    if errorlevel 1 goto :fail
    pip install -r requirements_qt.txt
    if errorlevel 1 goto :fail
    pip install pyinstaller
    if errorlevel 1 goto :fail

    echo.
    echo [FULL] Checking offline runtime assets...
    call :check_dir "models\screen_ai" "ScreenAI models"
    call :check_dir "models\orientation" "Orientation ONNX"
    call :check_dir "models\doclayout_yolo_onnx_dynamic" "DocLayout-YOLO dynamic ONNX"
    call :check_dir "models\lightgbm_splitter" "Archive page splitter"
    call :check_dir "models\layoutlmv3_fontgray_norm_final_epoch25" "LayoutLMv3 text KIE"
    call :check_dir "models\archive_models\e5-small-mix50-v2-onnx-fp32" "Kho archive E5 ONNX embedding"
    if "%INCLUDE_CORRECTION%"=="1" call :check_dir "models\distilled_ct2" "Distilled Proton CT2"
)

echo.
echo Ensuring PyInstaller is available...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    pip install pyinstaller
    if errorlevel 1 goto :fail
)

echo.
echo Writing VERSION file from git describe...
python -c "from scanindex.infra.version import get_version; open('VERSION','w',encoding='utf-8').write(get_version())"
if errorlevel 1 (
    echo   [WARN] Could not auto-derive version; bundle will fall back to _FALLBACK_VERSION at runtime.
) else (
    for /f "usebackq delims=" %%V in ("VERSION") do echo   [OK] VERSION = %%V
)

echo.
echo Killing running processes...
taskkill /F /IM "%APP_NAME%.exe" 2>nul
taskkill /F /IM "%OLD_APP_NAME%.exe" 2>nul
taskkill /F /IM "chromedriver.exe" 2>nul

echo.
echo ===================================================
echo   Building: %APP_NAME%
echo ===================================================

if exist "%DIST_DIR%" (
    echo Cleaning %DIST_DIR%...
    rmdir /s /q "%DIST_DIR%"
)
if exist "%OLD_DIST_DIR%" (
    echo Cleaning old portable output %OLD_DIST_DIR%...
    rmdir /s /q "%OLD_DIST_DIR%"
)
if exist "%LEGACY_DIST_DIR%" (
    echo Cleaning legacy unversioned %LEGACY_DIST_DIR%...
    rmdir /s /q "%LEGACY_DIST_DIR%"
)
if exist "%DIST_DIR%" (
    echo ERROR: Cannot clean %DIST_DIR%. Close the app and retry.
    goto :fail
)

python -m PyInstaller "%SPEC_FILE%" --noconfirm --clean
if errorlevel 1 goto :fail

echo.
echo Copying portable runtime resources...
echo   Filter: ONNX/current pipeline assets only. Legacy caches and training outputs are skipped.

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\copy_portable_runtime.ps1" -DistDir "%CD%\%DIST_DIR%" -IncludeCorrection "%INCLUDE_CORRECTION%" -IncludeLegacyChrome "%INCLUDE_LEGACY_CHROME%"
if errorlevel 1 goto :fail

echo.
echo ===================================================
echo   BUILD COMPLETE
echo ===================================================
if exist "%DIST_DIR%\%APP_NAME%.exe" (
    echo   [OK] %DIST_DIR%\%APP_NAME%.exe
) else (
    echo   [ERROR] EXE not found after build.
    goto :fail
)

if "%INTERACTIVE%"=="1" pause
exit /b 0

:check_dir
if exist "%~1\" (
    echo   [OK] %~2
) else (
    echo   [WARN] Missing %~2: %~1
)
goto :eof

:copy_file
set "SRC=%~1"
set "DST=%~2"
set "LABEL=%~3"
if exist "%SRC%" (
    echo   [+] %LABEL%
    if not exist "%~dp2" mkdir "%~dp2" >nul 2>&1
    copy /Y "%SRC%" "%DST%" >nul
)
goto :eof

:copy_dir
set "SRC=%~1"
set "DST=%~2"
set "LABEL=%~3"
if exist "%SRC%\" (
    echo   [+] %LABEL%
    if not exist "%DST%" mkdir "%DST%" >nul 2>&1
    xcopy /E /I /Y /D "%SRC%" "%DST%" >nul
)
goto :eof

:copy_dir_quiet
set "SRC=%~1"
set "DST=%~2"
if exist "%SRC%\" (
    if not exist "%DST%" mkdir "%DST%" >nul 2>&1
    xcopy /E /I /Y /D "%SRC%" "%DST%" >nul
)
goto :eof

:copy_file_quiet
set "SRC=%~1"
set "DST=%~2"
if exist "%SRC%" (
    if not exist "%~dp2" mkdir "%~dp2" >nul 2>&1
    copy /Y "%SRC%" "%DST%" >nul
)
goto :eof

:copy_matching_files
set "PATTERN=%~1"
set "DST=%~2"
set "LABEL=%~3"
dir /b "%PATTERN%" >nul 2>&1
if not errorlevel 1 (
    echo   [+] %LABEL%
    if not exist "%DST%" mkdir "%DST%" >nul 2>&1
    copy /Y "%PATTERN%" "%DST%\" >nul
)
goto :eof

:copy_screen_ai
set "SRC=%~1"
set "DST=%~2"
set "LABEL=%~3"
if not exist "%SRC%\" goto :eof
set "SCREEN_AI_VER="
for /f "delims=" %%V in ('dir /b /ad "%SRC%" 2^>nul ^| sort /R') do (
    if not defined SCREEN_AI_VER if exist "%SRC%\%%V\chrome_screen_ai.dll" set "SCREEN_AI_VER=%%V"
)
if not defined SCREEN_AI_VER (
    echo   [WARN] ScreenAI base DLL not found under %SRC%
    goto :eof
)
echo   [+] %LABEL% (!SCREEN_AI_VER!, filtered)
if not exist "%DST%\!SCREEN_AI_VER!" mkdir "%DST%\!SCREEN_AI_VER!" >nul 2>&1
robocopy "%SRC%\!SCREEN_AI_VER!" "%DST%\!SCREEN_AI_VER!" /E /NFL /NDL /NJH /NJS /NP /XF chrome_screen_ai_w_*.dll chrome_screen_ai_copy*.dll chrome_screen_ai_worker*.dll chrome_screen_ai_p*w*.dll >nul
set "ROBO_RC=!ERRORLEVEL!"
if !ROBO_RC! GEQ 8 exit /b !ROBO_RC!
goto :eof

:copy_layoutlmv3_text
set "SRC=models\layoutlmv3_fontgray_norm_final_epoch25"
set "DST=%DIST_DIR%\models\layoutlmv3_fontgray_norm_final_epoch25"
if not exist "%SRC%\" goto :eof
echo   [+] LayoutLMv3 text KIE (int8 ONNX + tokenizer/config)
call :copy_file_quiet "%SRC%\layoutlmv3_fontgray_norm_final_epoch25.int8.onnx" "%DST%\layoutlmv3_fontgray_norm_final_epoch25.int8.onnx"
call :copy_file_quiet "%SRC%\label_list.json" "%DST%\label_list.json"
call :copy_file_quiet "%SRC%\layoutlmv3_fontgray_config.json" "%DST%\layoutlmv3_fontgray_config.json"
call :copy_file_quiet "%SRC%\config.json" "%DST%\config.json"
call :copy_file_quiet "%SRC%\tokenizer.json" "%DST%\tokenizer.json"
call :copy_file_quiet "%SRC%\tokenizer_config.json" "%DST%\tokenizer_config.json"
call :copy_file_quiet "%SRC%\special_tokens_map.json" "%DST%\special_tokens_map.json"
call :copy_file_quiet "%SRC%\vocab.json" "%DST%\vocab.json"
call :copy_file_quiet "%SRC%\merges.txt" "%DST%\merges.txt"
goto :eof

:copy_archive_embedder
set "ROOT=models\archive_models"
if not exist "%ROOT%\" goto :eof
if exist "%ROOT%\e5-small-mix50-v2-onnx-fp32\model.onnx" (
    call :copy_dir "%ROOT%\e5-small-mix50-v2-onnx-fp32" "%DIST_DIR%\models\archive_models\e5-small-mix50-v2-onnx-fp32" "Kho E5 mix50 ONNX fp32 embedding"
)
if exist "%ROOT%\e5-small-mix50-v2-onnx-int8\model_quantized.onnx" (
    call :copy_dir "%ROOT%\e5-small-mix50-v2-onnx-int8" "%DIST_DIR%\models\archive_models\e5-small-mix50-v2-onnx-int8" "Kho E5 mix50 ONNX int8 embedding"
)
goto :eof

:fail
echo.
echo BUILD FAILED.
if "%INTERACTIVE%"=="1" pause
exit /b 1
