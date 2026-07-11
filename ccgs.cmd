@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "CCGS_ROOT=%~dp0"
set "CCGS_CLI=%CCGS_ROOT%.ccgs-core\scripts\ccgs_cli.py"

if not exist "%CCGS_CLI%" (
  echo CCGS error: CLI not found at "%CCGS_CLI%". 1>&2
  exit /b 2
)

if defined CCGS_PYTHON (
  "%CCGS_PYTHON%" "%CCGS_CLI%" %*
  exit /b !ERRORLEVEL!
)

where py.exe >nul 2>nul
if !ERRORLEVEL! EQU 0 (
  py -3 "%CCGS_CLI%" %*
  exit /b !ERRORLEVEL!
)

where python.exe >nul 2>nul
if !ERRORLEVEL! EQU 0 (
  python "%CCGS_CLI%" %*
  exit /b !ERRORLEVEL!
)

echo CCGS error: Python 3.10+ was not found. Set CCGS_PYTHON to a Python executable. 1>&2
exit /b 2
