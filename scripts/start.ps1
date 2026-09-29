param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
try {
    $projectRoot = (Resolve-Path -LiteralPath $Root).Path
    $composeFile = Join-Path $projectRoot 'compose.yaml'
    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) { throw 'compose.yaml is missing from the project directory.' }
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) { throw 'Docker with the Compose plugin is required. Install Docker and retry.' }
    $python = Get-Command python -ErrorAction SilentlyContinue
    $pythonPrefix = @()
    if (-not $python) {
        $python = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $python) {
        $python = Get-Command py -ErrorAction SilentlyContinue
        $pythonPrefix = @('-3')
    }
    if (-not $python) { throw 'Python 3 is required to initialize local credentials.' }

    $ErrorActionPreference = 'Continue'
    & $docker.Source compose version --short
    $commandExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($commandExit -ne 0) { [Console]::Error.WriteLine('Docker Compose is unavailable.'); exit $commandExit }

    $ErrorActionPreference = 'Continue'
    & $python.Source @pythonPrefix (Join-Path $PSScriptRoot 'bootstrap.py') --root $projectRoot
    $commandExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($commandExit -ne 0) { exit $commandExit }

    $composeArgs = @('compose', '--project-name', 'contextfence', '--project-directory', $projectRoot,
        '--file', $composeFile, '--env-file', (Join-Path $projectRoot '.env'),
        'up', '--detach', '--build', '--wait', '--wait-timeout', '180')
    $ErrorActionPreference = 'Continue'
    & $docker.Source @composeArgs
    $commandExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($commandExit -ne 0) {
        [Console]::Error.WriteLine('ContextFence did not start successfully. Docker errors are preserved above; check the engine, build, or configured ports.')
        exit $commandExit
    }
    Write-Output 'ContextFence is healthy. Use scripts/stop.ps1 to stop this project while retaining its database volume.'
} catch {
    [Console]::Error.WriteLine('Start failed: ' + $_.Exception.Message)
    exit 1
}
