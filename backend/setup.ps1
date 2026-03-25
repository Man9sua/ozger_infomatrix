#!/usr/bin/env pwsh

# Quick Start Script for Ozger AI Tutor
# ================================

Write-Host "🚀 Starting Ozger AI Tutor Services..." -ForegroundColor Cyan

# Check if Python venv exists
$venvPath = "c:\Users\user\Desktop\swaga\backend\aiapi\venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "Creating Python virtual environment..." -ForegroundColor Yellow
    python -m venv $venvPath
}

# Activate venv and install requirements
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
& "$venvPath\Scripts\Activate.ps1"
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Check Node.js installation
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Node.js is not installed!" -ForegroundColor Red
    exit 1
}

Write-Host "`n================================" -ForegroundColor Green
Write-Host "✅ Environment Setup Complete!" -ForegroundColor Green
Write-Host "================================`n" -ForegroundColor Green

Write-Host "📝 Next steps:" -ForegroundColor Cyan
Write-Host "1. In Terminal 1, run: npm start" -ForegroundColor Gray
Write-Host "   (from: c:\Users\user\Desktop\swaga\backend)" -ForegroundColor Gray
Write-Host "" -ForegroundColor Gray
Write-Host "2. In Terminal 2, run: python aiapi/app.py" -ForegroundColor Gray
Write-Host "   (from: c:\Users\user\Desktop\swaga\backend)" -ForegroundColor Gray
Write-Host "" -ForegroundColor Gray
Write-Host "3. Open http://localhost:3000 in your browser" -ForegroundColor Gray

Write-Host "`n🎯 Features Now Available:" -ForegroundColor Cyan
Write-Host "  • 📚 PDF-based knowledge base (11 Kazakh history books)" -ForegroundColor Gray
Write-Host "  • 🌐 Automatic language detection" -ForegroundColor Gray
Write-Host "  • 🇮🇳 Hindi language support (NEW)" -ForegroundColor Gray
Write-Host "  • 💬 Multi-language responses (Kazakh/Russian/English/Hindi)" -ForegroundColor Gray

Write-Host "`n" -ForegroundColor Green
