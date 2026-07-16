[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [AllowEmptyString()]
    [string[]] $Arguments
)

$ErrorActionPreference = "Stop"
$cli = Join-Path $PSScriptRoot ".ccgs-core\scripts\ccgs_cli.py"

if (-not (Test-Path -LiteralPath $cli)) {
    [Console]::Error.WriteLine("VIBE_LAUNCHER_ERROR CLI_NOT_FOUND")
    exit 2
}

function Test-VibePython {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Executable,
        [string[]] $PrefixArguments = @()
    )

    try {
        & $Executable @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 1>$null 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

if ($env:CCGS_PYTHON) {
    if (Test-VibePython -Executable $env:CCGS_PYTHON) {
        & $env:CCGS_PYTHON $cli @Arguments
        exit $LASTEXITCODE
    }
    [Console]::Error.WriteLine("VIBE_LAUNCHER_ERROR PYTHON_NOT_FOUND")
    exit 2
}

$pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyLauncher -and (Test-VibePython -Executable $pyLauncher.Source -PrefixArguments @("-3"))) {
    & $pyLauncher.Source -3 $cli @Arguments
    exit $LASTEXITCODE
}

$pythonLauncher = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonLauncher -and (Test-VibePython -Executable $pythonLauncher.Source)) {
    & $pythonLauncher.Source $cli @Arguments
    exit $LASTEXITCODE
}

[Console]::Error.WriteLine("VIBE_LAUNCHER_ERROR PYTHON_NOT_FOUND")
exit 2
