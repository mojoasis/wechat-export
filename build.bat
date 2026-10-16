@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "BUILD_PYTHON=%~dp0build_venv\Scripts\python.exe"
if not exist "%BUILD_PYTHON%" (
    echo [*] Creating build environment...
    python -m venv "%~dp0build_venv"
    if errorlevel 1 exit /b 1
)

echo [*] Installing build dependencies...
"%BUILD_PYTHON%" -m pip install -r "%~dp0requirements-build.txt"
if errorlevel 1 exit /b 1

echo [*] Building dist\wechat_exp.exe...
"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean "%~dp0wechat_exp.spec" %*
if errorlevel 1 exit /b 1

echo [+] Ready: dist\wechat_exp.exe
exit /b 0
