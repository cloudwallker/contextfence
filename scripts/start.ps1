param([string]$Root = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
try {
    $python = Get-Command python -ErrorAction SilentlyContinue
    $pythonPrefix = @()
    if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
    if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue; $pythonPrefix = @('-3') }
    if (-not $python) { throw 'Python 3 is required.' }
    $ErrorActionPreference = 'Continue'
    & $python.Source @pythonPrefix (Join-Path $PSScriptRoot 'start.py') --root $Root
    exit $LASTEXITCODE
} catch { [Console]::Error.WriteLine('Start failed: Python or the project directory is unavailable.'); exit 1 }
