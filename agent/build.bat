@echo off
REM Builds dist\HanwhaMonitor.exe on the Windows PC next to the lathe.
REM Fwlib32.dll is 32-bit, so the exe must be built with 32-bit Python.
REM This script finds (or installs) uv, which downloads a private 32-bit Python just for this build.
setlocal
cd /d "%~dp0"
echo.
echo === Building HanwhaMonitor.exe - 32-bit ===
if exist .build-venv rmdir /s /q .build-venv

REM ---- 1. find uv (it is often installed but not on PATH) -------------------
set "UV="
where uv >nul 2>nul && set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if defined UV goto :use_uv

echo uv not found - installing it now, one-off download...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if defined UV goto :use_uv
echo Could not install uv automatically.
goto :try_existing

REM ---- 2. create a 32-bit build environment with uv --------------------------
:use_uv
echo Using uv: %UV%
"%UV%" venv --python cpython-3.12-windows-x86-none .build-venv
if exist .build-venv\Scripts\python.exe goto :uv_deps
if exist .build-venv rmdir /s /q .build-venv
"%UV%" venv --python cpython-3.12-windows-x86 .build-venv
if not exist .build-venv\Scripts\python.exe goto :try_existing
:uv_deps
"%UV%" pip install --python .build-venv\Scripts\python.exe -r requirements.txt pyinstaller
if errorlevel 1 goto :fail
goto :check

REM ---- 3. fallbacks: the old script's 32-bit venv, or the py launcher -----------
:try_existing
if exist .build-venv rmdir /s /q .build-venv
if exist "C:\Users\Hanwha\focas\.venv\Scripts\python.exe" (
    echo Trying the Python from C:\Users\Hanwha\focas\.venv ...
    "C:\Users\Hanwha\focas\.venv\Scripts\python.exe" -m venv .build-venv
)
if not exist .build-venv\Scripts\python.exe py -3-32 -m venv .build-venv 2>nul
if not exist .build-venv\Scripts\python.exe goto :nopython
.build-venv\Scripts\python.exe -m ensurepip >nul 2>nul
.build-venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

REM ---- 4. build ----------------------------------------------------------------
:check
.build-venv\Scripts\python.exe -c "import struct,sys; print('Python', sys.version.split()[0], struct.calcsize('P')*8, 'bit'); sys.exit(0 if struct.calcsize('P')==4 else 1)"
if errorlevel 1 goto :not32

.build-venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed --name HanwhaMonitor --icon icon.ico --hidden-import pystray._win32 run.py
if errorlevel 1 goto :fail

REM Copy the FANUC DLLs next to the exe if we can find them
for %%D in (Fwlib32.dll fwlibe1.dll) do (
    if exist "%%D" copy /y "%%D" dist\ >nul
    if not exist "dist\%%D" if exist "C:\Users\Hanwha\focas\%%D" copy /y "C:\Users\Hanwha\focas\%%D" dist\ >nul
)
echo.
echo Done:  %cd%\dist\HanwhaMonitor.exe
echo Keep Fwlib32.dll and fwlibe1.dll in the same folder as the exe.
pause
exit /b 0

:nopython
echo.
echo Could not get a 32-bit Python. Easiest fix: download HanwhaMonitor.exe from the GitHub Actions build instead.
pause
exit /b 1
:not32
echo.
echo The Python found is 64-bit, but Fwlib32.dll needs 32-bit. Download the exe from GitHub Actions instead.
pause
exit /b 1
:fail
echo.
echo Build failed - see messages above.
pause
exit /b 1
