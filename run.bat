@echo off
rem ===========================================================
rem  tronclass-picker launcher
rem  Keep this file ASCII-only: cmd.exe parses .bat with the GBK
rem  code page, so Chinese text here would break into garbage
rem  commands. All Chinese output comes from Python.
rem ===========================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

set PYEXE=python
where python >nul 2>nul
if not errorlevel 1 goto :checkver
set PYEXE=py -3
where py >nul 2>nul
if not errorlevel 1 goto :checkver
goto :nopython

:checkver
%PYEXE% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :oldpython

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem config.json is created automatically on first run.
%PYEXE% -m tronclass_picker %*
set RC=%errorlevel%
echo.
pause
endlocal & exit /b %RC%

:nopython
echo.
echo   [ERROR] Python was not found in PATH.
echo.
echo   Install Python 3.10 or newer, then start this file again.
echo   If Python is already installed, add its folder to PATH,
echo   or open cmd and run:  py -3 -m tronclass_picker
echo.
pause
exit /b 127

:oldpython
echo.
echo   [ERROR] Python 3.10 or newer is required.
echo.
echo   Run this to see the version in use:  %PYEXE% --version
echo.
pause
exit /b 126
