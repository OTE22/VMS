# Detect whether an NVIDIA GPU is usable, persist COMPUTE=cpu|gpu into .env, and
# print the docker compose command to run.
#
#   .\scripts\detect-compute.ps1            # detect + write .env
#   .\scripts\detect-compute.ps1 -Up        # detect + write .env + start the stack
#
# Detection is conservative: GPU is only selected when nvidia-smi reports a GPU AND
# the NVIDIA container runtime is available to Docker. Otherwise we stay on CPU,
# which always works.

param([switch]$Up)

$ErrorActionPreference = 'SilentlyContinue'
$repo = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repo '.env'

function Test-NvidiaGpu {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) { return $false }
    $out = & nvidia-smi --query-gpu=name --format=csv,noheader 2>$null
    return ($LASTEXITCODE -eq 0 -and $out)
}

function Test-DockerNvidiaRuntime {
    $info = & docker info --format '{{json .Runtimes}}' 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    return ($info -match 'nvidia')
}

$gpuPresent = Test-NvidiaGpu
$runtimeOk = $false
if ($gpuPresent) { $runtimeOk = Test-DockerNvidiaRuntime }

if ($gpuPresent -and $runtimeOk) {
    $compute = 'gpu'
    $gpuName = (& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null | Select-Object -First 1)
    Write-Host "GPU detected: $gpuName  ->  COMPUTE=gpu" -ForegroundColor Green
} elseif ($gpuPresent -and -not $runtimeOk) {
    $compute = 'cpu'
    Write-Host "NVIDIA GPU found but Docker has no 'nvidia' runtime (install the NVIDIA Container Toolkit)." -ForegroundColor Yellow
    Write-Host "Falling back to COMPUTE=cpu" -ForegroundColor Yellow
} else {
    $compute = 'cpu'
    Write-Host "No NVIDIA GPU detected  ->  COMPUTE=cpu" -ForegroundColor Cyan
}

# Persist COMPUTE into .env (replace an existing line, else append)
if (Test-Path $envFile) {
    $lines = Get-Content $envFile
    if ($lines -match '^\s*COMPUTE\s*=') {
        $lines = $lines -replace '^\s*COMPUTE\s*=.*', "COMPUTE=$compute"
        Set-Content -Path $envFile -Value $lines -Encoding utf8
    } else {
        Add-Content -Path $envFile -Value "COMPUTE=$compute" -Encoding utf8
    }
} else {
    Set-Content -Path $envFile -Value "COMPUTE=$compute" -Encoding utf8
}
Write-Host "Wrote COMPUTE=$compute to .env"

if ($compute -eq 'gpu') {
    $cmd = 'docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build'
} else {
    $cmd = 'docker compose up -d --build'
}

if ($Up) {
    Write-Host "Running: $cmd" -ForegroundColor Green
    Push-Location $repo
    Invoke-Expression $cmd
    Pop-Location
} else {
    Write-Host ""
    Write-Host "Next step:" -ForegroundColor Green
    Write-Host "  $cmd"
}
