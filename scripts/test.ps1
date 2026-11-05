# Runs the test suite against a disposable PostgreSQL cluster.
#
# The cluster lives in %TEMP%\resumemaker-pgtest, listens on 127.0.0.1:55432 and uses trust
# authentication, so no password is required and the system PostgreSQL service is untouched.
# Set RESUMEMAKER_TEST_DATABASE_URL to reuse an existing server instead.

[CmdletBinding()]
param(
    [switch]$Stop,
    [int]$Port = 55432,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$dataDir = Join-Path $env:TEMP "resumemaker-pgtest"

function Find-PgBin {
    if ($env:RESUMEMAKER_PG_BIN) { return $env:RESUMEMAKER_PG_BIN }
    foreach ($rootPath in @("C:\Programs\PostgreSQL", "C:\Program Files\PostgreSQL")) {
        if (-not (Test-Path $rootPath)) { continue }
        $versions = Get-ChildItem $rootPath -Directory | Sort-Object Name -Descending
        foreach ($version in $versions) {
            $bin = Join-Path $version.FullName "bin"
            if (Test-Path (Join-Path $bin "postgres.exe")) { return $bin }
        }
    }
    throw "PostgreSQL binaries not found. Install PostgreSQL or set RESUMEMAKER_PG_BIN."
}

$pgBin = $null
if (-not $env:RESUMEMAKER_TEST_DATABASE_URL) {
    $pgBin = Find-PgBin
}

if ($Stop) {
    if ($pgBin -and (Test-Path (Join-Path $dataDir "PG_VERSION"))) {
        & (Join-Path $pgBin "pg_ctl.exe") -D $dataDir -m fast stop
        Write-Host "Test PostgreSQL cluster stopped."
    }
    exit 0
}

if ($pgBin) {
    if (-not (Test-Path (Join-Path $dataDir "PG_VERSION"))) {
        Write-Host "Initialising disposable PostgreSQL cluster in $dataDir"
        & (Join-Path $pgBin "initdb.exe") -D $dataDir -U postgres -A trust -E UTF8 --locale=C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
    }

    $isReady = $false
    & (Join-Path $pgBin "pg_isready.exe") -h 127.0.0.1 -p $Port -q 2>$null
    if ($LASTEXITCODE -eq 0) { $isReady = $true }

    if (-not $isReady) {
        Write-Host "Starting test PostgreSQL on port $Port"
        Start-Process -FilePath (Join-Path $pgBin "postgres.exe") `
            -ArgumentList @("-D", $dataDir, "-p", "$Port") `
            -WindowStyle Hidden | Out-Null
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            Start-Sleep -Milliseconds 500
            & (Join-Path $pgBin "pg_isready.exe") -h 127.0.0.1 -p $Port -q 2>$null
            if ($LASTEXITCODE -eq 0) { $isReady = $true; break }
        }
        if (-not $isReady) { throw "test PostgreSQL did not become ready on port $Port" }
    }

    $env:RESUMEMAKER_TEST_DATABASE_URL = "postgresql+psycopg://postgres@127.0.0.1:$Port/postgres"
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run: python -m venv .venv; .\.venv\Scripts\python -m pip install -e '.[dev]'"
}

Push-Location $root
try {
    & $python -m pytest @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
