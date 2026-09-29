[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$Settings,
    [switch]$NoSystemProxy,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$MavenArgs = @('verify')
)

$ErrorActionPreference = 'Stop'

function Test-Java21Home([string]$Candidate) {
    if (-not $Candidate) { return $false }
    $releaseFile = Join-Path $Candidate 'release'
    if (-not (Test-Path -LiteralPath (Join-Path $Candidate 'bin/java.exe') -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path $Candidate 'bin/javac.exe') -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath $releaseFile -PathType Leaf)) { return $false }
    return [bool]((Get-Content -LiteralPath $releaseFile) -match '^JAVA_VERSION="21(?:[.\-+]|")')
}

function Find-Java21Home {
    if (Test-Java21Home $env:JAVA_HOME) { return $env:JAVA_HOME }
    $javac = Get-Command javac.exe -ErrorAction SilentlyContinue
    if ($javac) {
        $candidate = Split-Path -Parent (Split-Path -Parent $javac.Source)
        if (Test-Java21Home $candidate) { return $candidate }
    }
    $profileDirectory = $env:USERPROFILE
    if (-not $profileDirectory) { $profileDirectory = [Environment]::GetFolderPath('UserProfile') }
    $searchRoots = @((Join-Path $profileDirectory '.jdks'))
    if ($env:ProgramFiles) {
        foreach ($vendor in @('Java', 'Eclipse Adoptium', 'Microsoft', 'Amazon Corretto', 'Zulu', 'BellSoft')) {
            $searchRoots += Join-Path $env:ProgramFiles $vendor
        }
    }
    foreach ($directory in $searchRoots) {
        foreach ($candidate in (Get-ChildItem -LiteralPath $directory -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
            if (Test-Java21Home $candidate.FullName) { return $candidate.FullName }
        }
    }
    throw 'JDK 21 was not found. Set JAVA_HOME to an installed JDK 21; Docker builds do not require a host JDK or Maven.'
}

function Find-Maven([string]$ProjectRoot) {
    $wrapper = Join-Path $ProjectRoot 'mvnw.cmd'
    if (Test-Path -LiteralPath $wrapper -PathType Leaf) { return $wrapper }
    $command = Get-Command mvn.cmd -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command mvn -ErrorAction SilentlyContinue }
    if ($command) { return $command.Source }
    foreach ($directory in (Get-ChildItem -LiteralPath (Join-Path $ProjectRoot '.tools') -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'apache-maven-*' } | Sort-Object Name -Descending)) {
        $candidate = Join-Path $directory.FullName 'bin/mvn.cmd'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $profileDirectory = $env:USERPROFILE
    if (-not $profileDirectory) { $profileDirectory = [Environment]::GetFolderPath('UserProfile') }
    $wrapperCache = Join-Path $profileDirectory '.m2/wrapper/dists'
    foreach ($distribution in (Get-ChildItem -LiteralPath $wrapperCache -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'apache-maven-*-bin' } | Sort-Object Name -Descending)) {
        foreach ($cacheEntry in (Get-ChildItem -LiteralPath $distribution.FullName -Directory -ErrorAction SilentlyContinue)) {
            foreach ($mavenDirectory in (Get-ChildItem -LiteralPath $cacheEntry.FullName -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'apache-maven-*' })) {
                $candidate = Join-Path $mavenDirectory.FullName 'bin/mvn.cmd'
                if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
            }
        }
    }
    throw 'Maven was not found on PATH, in the project wrapper/tools, or in the user wrapper cache. Install Maven or use the Docker build.'
}

function Select-MavenSettings([string]$ProjectRoot) {
    if ($Settings) {
        if (-not (Test-Path -LiteralPath $Settings -PathType Leaf)) { throw 'The requested Maven settings file does not exist.' }
        return (Resolve-Path -LiteralPath $Settings).Path
    }
    $localSettings = Join-Path $ProjectRoot '.local/maven-settings.xml'
    if (Test-Path -LiteralPath $localSettings -PathType Leaf) { return $localSettings }
    if ($NoSystemProxy) { return $null }
    $target = [uri]'https://repo.maven.apache.org/maven2/'
    $proxy = $null
    $proxyAddress = $env:HTTPS_PROXY
    if (-not $proxyAddress) { $proxyAddress = $env:HTTP_PROXY }
    if ($proxyAddress) {
        try { $proxy = [uri]$proxyAddress } catch { throw 'The configured proxy address is invalid; pass -Settings to use explicit Maven settings.' }
    } else {
        $systemProxy = [System.Net.WebRequest]::DefaultWebProxy
        if ($systemProxy) { $proxy = $systemProxy.GetProxy($target) }
    }
    if (-not $proxy -or $proxy.Authority -eq $target.Authority) { return $null }
    if ($proxy.Scheme -notin @('http', 'https') -or -not $proxy.Host) { throw 'The system proxy requires explicit Maven settings; pass -Settings or -NoSystemProxy.' }
    if ($proxy.UserInfo) { throw 'An authenticated proxy requires an explicitly managed -Settings file. Proxy credentials were not copied.' }
    $localDirectory = Join-Path $ProjectRoot '.local'
    New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
    $generatedSettings = Join-Path $localDirectory 'maven-settings.auto.xml'
    $hostName = [System.Security.SecurityElement]::Escape($proxy.Host)
    $protocol = [System.Security.SecurityElement]::Escape($proxy.Scheme)
    $xml = "<settings xmlns=`"http://maven.apache.org/SETTINGS/1.2.0`"><proxies><proxy><id>local-system-proxy</id><active>true</active><protocol>$protocol</protocol><host>$hostName</host><port>$($proxy.Port)</port><nonProxyHosts>localhost|127.0.0.1|[::1]</nonProxyHosts></proxy></proxies></settings>"
    [System.IO.File]::WriteAllText($generatedSettings, $xml, (New-Object System.Text.UTF8Encoding($false)))
    return $generatedSettings
}

try {
    $projectRoot = (Resolve-Path -LiteralPath $Root).Path
    $env:JAVA_HOME = Find-Java21Home
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + [System.IO.Path]::PathSeparator + $env:PATH
    $maven = Find-Maven $projectRoot
    $settingsFile = Select-MavenSettings $projectRoot
    $options = @('--batch-mode', '--no-transfer-progress')
    if ($settingsFile) { $options += @('--settings', $settingsFile) }
    if (-not $MavenArgs -or $MavenArgs.Count -eq 0) { $MavenArgs = @('verify') }
    Push-Location -LiteralPath $projectRoot
    try {
        $ErrorActionPreference = 'Continue'
        & $maven @options @MavenArgs
        $commandExit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } finally { Pop-Location }
    exit $commandExit
} catch {
    [Console]::Error.WriteLine('Maven setup failed: ' + $_.Exception.Message)
    exit 1
}
