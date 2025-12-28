# backup.ps1
# Script backup untuk Windows PowerShell
param(
    [string]$backupDir = "..\\data\\backups",
    [int]$keepDays = 30,
    [switch]$compress = $true,
    [string]$prefix = "backup_"
)

$ErrorActionPreference = "Stop"
$startTime = Get-Date

function Write-Header($message) {
    Write-Host "`n=== $message ===" -ForegroundColor Cyan
}

try {
    Write-Header "Memulai proses backup"
    
    # Validasi direktori
    $backupDir = [System.IO.Path]::GetFullPath($backupDir)
    $dataDir = [System.IO.Path]::GetFullPath("..\\data")
    
    if (-not (Test-Path $dataDir)) {
        New-Item -ItemType Directory -Path $dataDir | Out-Null
    }
    
    if (-not (Test-Path $backupDir)) {
        Write-Host "Membuat direktori backup: $backupDir" -ForegroundColor Yellow
        New-Item -ItemType Directory -Path $backupDir | Out-Null
    }
    
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupName = "${prefix}${timestamp}"
    $backupPath = Join-Path $backupDir $backupName
    
    # Backup database
    $dbFiles = @()
    if (Test-Path "$dataDir\\database.db") {
        Write-Host "Mencadangkan database..." -ForegroundColor Yellow
        $dbFile = "${backupPath}.db"
        Copy-Item "$dataDir\\database.db" $dbFile
        $dbFiles += $dbFile
    }
    
    # Backup logs
    $logFiles = @()
    if (Test-Path "$dataDir\\logs") {
        Write-Host "Mencadangkan log..." -ForegroundColor Yellow
        $logDir = "${backupPath}_logs"
        New-Item -ItemType Directory -Path $logDir | Out-Null
        Copy-Item -Path "$dataDir\\logs\\*" -Destination $logDir -Recurse -Force
        $logFiles += $logDir
    }
    
    # Kompresi jika diminta
    if ($compress) {
        Write-Host "Mengkompresi backup..." -ForegroundColor Yellow
        $zipFile = "${backupPath}.zip"
        $filesToCompress = $dbFiles + $logFiles | Where-Object { $_ -ne $null }
        
        if ($filesToCompress.Count -gt 0) {
            Compress-Archive -Path $filesToCompress -DestinationPath $zipFile -Force
            
            # Hapus file asli setelah dikompres
            $filesToCompress | Remove-Item -Recurse -Force
            
            $backupFile = $zipFile
        }
    } else {
        $backupFile = $dbFiles[0]  # Atau direktori log jika hanya log yang ada
    }
    
    # Bersihkan backup lama
    Write-Host "Membersihkan backup lama (lebih dari $keepDays hari)..." -ForegroundColor Yellow
    $cutoffDate = (Get-Date).AddDays(-$keepDays)
    
    # Hapus file backup lama
    Get-ChildItem -Path $backupDir -Filter "${prefix}*" -File | 
        Where-Object { $_.LastWriteTime -lt $cutoffDate } | 
        Remove-Item -Force -ErrorAction SilentlyContinue
    
    # Hapus direktori backup lama
    Get-ChildItem -Path $backupDir -Directory | 
        Where-Object { $_.Name -like "${prefix}*" -and $_.LastWriteTime -lt $cutoffDate } | 
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    
    $duration = (Get-Date) - $startTime
    $backupSize = if (Test-Path $backupFile) { 
        "{0:N2} MB" -f ((Get-Item $backupFile).Length / 1MB) 
    } else { "0 MB" }
    
    Write-Host "`n✅ Backup berhasil dibuat: $(Split-Path $backupFile -Leaf)" -ForegroundColor Green
    Write-Host "   Lokasi: $backupFile" -ForegroundColor Gray
    Write-Host "   Ukuran: $backupSize" -ForegroundColor Gray
    Write-Host "   Durasi: $($duration.TotalSeconds.ToString('0.00')) detik" -ForegroundColor Gray
    
} catch {
    Write-Host "`n❌ Backup gagal: $_" -ForegroundColor Red
    exit 1
}