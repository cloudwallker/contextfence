param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
try {
    $projectRoot = (Resolve-Path -LiteralPath $Root).Path
    $composeFile = Join-Path $projectRoot 'compose.yaml'
    $envFile = Join-Path $projectRoot '.env'
    if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) { throw 'compose.yaml is missing from the project directory.' }
    if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'The project .env is missing; no configuration has been generated or replaced.' }
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) { throw 'Docker with the Compose plugin is required.' }
    $composeArgs = @('compose', '--project-name', 'contextfence', '--project-directory', $projectRoot,
        '--file', $composeFile, '--env-file', $envFile, 'down')
    $ErrorActionPreference = 'Continue'
    & $docker.Source @composeArgs
    $commandExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($commandExit -ne 0) { [Console]::Error.WriteLine('Stopping ContextFence failed. Docker errors are preserved above.'); exit $commandExit }
    Write-Output 'ContextFence stopped. Database volumes and local credentials were retained.'
} catch {
    [Console]::Error.WriteLine('Stop failed: ' + $_.Exception.Message)
    exit 1
}
