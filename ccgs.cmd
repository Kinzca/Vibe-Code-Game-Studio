@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "CCGS_ROOT=%~dp0"
set "CCGS_CLI=%CCGS_ROOT%.ccgs-core\scripts\ccgs_cli.py"

if not exist "%CCGS_CLI%" (
  1>&2 echo VIBE_LAUNCHER_ERROR CLI_NOT_FOUND
  exit /b 2
)

if defined CCGS_PYTHON goto :ccgs_python

where py.exe >nul 2>nul
if not errorlevel 1 goto :py_launcher

where python.exe >nul 2>nul
if not errorlevel 1 goto :python_launcher

goto :python_missing

:ccgs_python
"%CCGS_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :python_missing
"%CCGS_PYTHON%" "%CCGS_CLI%" %*
exit /b %ERRORLEVEL%

:py_launcher
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :python_missing
py -3 "%CCGS_CLI%" %*
exit /b %ERRORLEVEL%

:python_launcher
python.exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :python_missing
python.exe "%CCGS_CLI%" %*
exit /b %ERRORLEVEL%

:python_missing
1>&2 echo VIBE_LAUNCHER_ERROR PYTHON_NOT_FOUND
exit /b 2
