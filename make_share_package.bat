@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

call "%~dp0build.bat" %*
if errorlevel 1 exit /b 1

if not exist "%~dp0share_package" mkdir "%~dp0share_package"
if errorlevel 1 exit /b 1
copy /y "%~dp0dist\wechat_exp.exe" "%~dp0share_package\wechat_exp.exe" >nul
if errorlevel 1 exit /b 1
copy /y "%~dp0share_template\*" "%~dp0share_package\" >nul
if errorlevel 1 exit /b 1

echo [+] Ready: share_package\
echo     Share only wechat_exp.exe, Open.bat and the usage instructions.
echo     Do not include backup, export or config files from previous runs.
exit /b 0
