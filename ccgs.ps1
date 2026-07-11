[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = "Stop"
$cli = Join-Path $PSScriptRoot ".ccgs-core\scripts\ccgs_cli.py"

if (-not (Test-Path -LiteralPath $cli)) {
    Write-Error "CCGS CLI not found at '$cli'."
    exit 2
}

if ($env:CCGS_PYTHON) {
    & $env:CCGS_PYTHON $cli @Arguments
    exit $LASTEXITCODE
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($launcher) {
    & $launcher.Source -3 $cli @Arguments
    exit $LASTEXITCODE
}

$launcher = Get-Command python.exe -ErrorAction SilentlyContinue
if ($launcher) {
    & $launcher.Source $cli @Arguments
    exit $LASTEXITCODE
}

Write-Error "Python 3.10+ was not found. Set CCGS_PYTHON to a Python executable."
exit 2
