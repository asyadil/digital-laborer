# deploy.ps1
# Script deployment untuk Windows PowerShell
param(
    [switch]$backup = $true,
    [switch]$skipTests = $false,
    [string]$environment = "production"
)

$ErrorActionPreference = "Stop"
$startTime = Get-Date

function Write-Header($message) {
    Write-Host "`n=== $message ===" -ForegroundColor Cyan
}

try {
    Write-Header "Memulai proses deployment [$environment]"
    
    # Validasi direktori
    if (-not (Test-Path "..\src")) {
        throw "Direktori src tidak ditemukan. Pastikan script dijalankan dari folder scripts/"
    }
    
    # Backup database jika diperlukan
    if ($backup) {
        Write-Header "Membuat backup"
        .\backup.ps1
    }
    
    # Update kode dari repository
    Write-Header "Update kode"
    try {
        git pull origin main
    } catch {
        Write-Warning "Gagal update kode dari git: $_"
    }
    
    # Update dependencies
    Write-Header "Update dependencies"
    ..\venv\Scripts\pip.exe install -r ..\requirements.txt
    
    # Jalankan migrasi database (jika menggunakan Alembic)
    if (Test-Path "..\alembic.ini") {
        Write-Header "Menjalankan migrasi database"
        alembic upgrade head
    }
    
    # Jalankan tests (jika tidak di-skip)
    if (-not $skipTests) {
        Write-Header "Menjalankan tests"
        pytest ..\tests\
    }
    
    # Restart service (opsional)
    if ($environment -eq "production") {
        Write-Header "Restarting service"
        try {
            Stop-Service -Name "ReferralAutomation" -ErrorAction SilentlyContinue
            Start-Service -Name "ReferralAutomation"
        } catch {
            Write-Warning "Gagal restart service: $_"
        }
    }
    
    $duration = (Get-Date) - $startTime
    Write-Host "`n✅ Deployment selesai dalam $($duration.TotalSeconds.ToString('0.00')) detik" -ForegroundColor Green
    
} catch {
    Write-Host "`n❌ Deployment gagal: $_" -ForegroundColor Red
    exit 1
}
