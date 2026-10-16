@echo off
call "%~dp0build.bat" %*
exit /b %errorlevel%
