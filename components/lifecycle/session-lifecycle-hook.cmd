@echo off
setlocal
"%~1" "%~2" "%~3" %4
exit /b %errorlevel%
