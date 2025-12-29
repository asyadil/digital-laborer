# deploy.ps1
# Script deployment untuk Windows PowerShell
param(
    [switch]$backup = $true,
    [switch]$skipTests = $false,
    [string]$environment = "production",
    [string]$appDir = "..",
    [string]$venvDir = "..\\venv",
    [string]$serviceName = "ReferralAutomation",
    [string]$configPath = "..\\config\\config.yaml",
    [string]$logFile = "..\\logs\\referral.log"
)

$ErrorActionPreference = "Stop"
$startTime = Get-Date

function Write-Header($message) {
    Write-Host "`n=== $message ===" -ForegroundColor Cyan
}

try {
    Write-Header "Memulai proses deployment [$environment]"
    
    # Validasi direktori
    if (-not (Test-Path "$appDir\\src")) {
        throw "Direktori src tidak ditemukan di $appDir. Jalankan dari folder scripts/ atau set -appDir."
    }
    
    # Backup database jika diperlukan
    if ($backup) {
        Write-Header "Membuat backup"
        .\backup.ps1
    }
    
    # Update kode dari repository
    Write-Header "Update kode"
    try {
        git -C $appDir pull origin main
    } catch {
        Write-Warning "Gagal update kode dari git: $_"
    }
    
    # Update dependencies
    Write-Header "Update dependencies"
    $pipPath = Join-Path $venvDir "Scripts\\pip.exe"
    if (-not (Test-Path $pipPath)) {
        python -m venv $venvDir
        $pipPath = Join-Path $venvDir "Scripts\\pip.exe"
    }
    & $pipPath install --upgrade pip
    & $pipPath install -r (Join-Path $appDir "requirements.txt")
    
    # Jalankan migrasi database (jika menggunakan Alembic)
    if (Test-Path (Join-Path $appDir "alembic.ini")) {
        Write-Header "Menjalankan migrasi database"
        Push-Location $appDir
        alembic upgrade head
        Pop-Location
    }
    
    # Jalankan tests (jika tidak di-skip)
    if (-not $skipTests) {
        Write-Header "Menjalankan tests"
        Push-Location $appDir
        & (Join-Path $venvDir "Scripts\\pytest.exe") .\tests\
        Pop-Location
    }
    
    # Restart service (opsional)
    if ($environment -eq "production") {
        Write-Header "Restarting service"
        try {
            Stop-Service -Name $serviceName -ErrorAction SilentlyContinue
            Start-Service -Name $serviceName
        } catch {
            Write-Warning "Gagal restart service: $_"
        }
    }
    
    $duration = (Get-Date) - $startTime
    Write-Host "`n✅ Deployment selesai dalam $($duration.TotalSeconds.ToString('0.00')) detik" -ForegroundColor Green
    Write-Host "   Config : $configPath" -ForegroundColor Gray
    Write-Host "   Logs   : $logFile" -ForegroundColor Gray
    
} catch {
    Write-Host "`n❌ Deployment gagal: $_" -ForegroundColor Red
    exit 1
}
