# setup.ps1
# Script setup untuk Windows PowerShell
Write-Host "Menyiapkan environment untuk Referral Automation System..." -ForegroundColor Cyan

# Buat file .env jika belum ada
if (-not (Test-Path ..\\.env)) {
    Write-Host "Membuat file .env dari template..." -ForegroundColor Yellow
    if (Test-Path ..\\.env.example) {
        Copy-Item ..\\.env.example ..\\.env
        Write-Host "Silakan edit file .env dan isi dengan konfigurasi yang sesuai" -ForegroundColor Yellow
    } else {
        Write-Host "File .env.example tidak ditemukan" -ForegroundColor Red
    }
}

# Buat virtual environment
if (-not (Test-Path "..\\venv")) {
    Write-Host "Membuat virtual environment..." -ForegroundColor Yellow
    python -m venv ..\venv
}

# Aktifkan virtual environment
Write-Host "Mengaktifkan virtual environment..." -ForegroundColor Yellow
..\venv\Scripts\Activate.ps1

# Install dependencies
Write-Host "Menginstall dependencies..." -ForegroundColor Yellow
pip install -r ..\\requirements.txt

# Inisialisasi database
Write-Host "Menginisialisasi database..." -ForegroundColor Yellow
Set-Location ..
python -c "from src.database.models import Base, engine; Base.metadata.create_all(bind=engine)"

Write-Host "`nSetup selesai!" -ForegroundColor Green
Write-Host "1. Jangan lupa untuk mengisi file .env dengan konfigurasi yang sesuai" -ForegroundColor Green
Write-Host "2. Untuk menjalankan sistem, gunakan perintah: python main.py" -ForegroundColor Green
Write-Host "3. Untuk bantuan, jalankan: .\\scripts\\help.ps1" -ForegroundColor Green
