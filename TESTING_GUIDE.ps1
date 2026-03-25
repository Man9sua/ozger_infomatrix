#!/usr/bin/env pwsh

# Quick Start Guide for Testing New Features
# ==========================================

Write-Host "`n╔════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   OZGER AI TUTOR - NEW FEATURES QUICK START              ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════════╝`n" -ForegroundColor Cyan

# Function to show next step
function Show-NextStep {
    param([string]$Step)
    Write-Host "▶ $Step" -ForegroundColor Yellow
}

# Setup section
Write-Host "📋 SETUP PHASE" -ForegroundColor Green
Write-Host "═" * 60 -ForegroundColor Green

Show-NextStep "Navigate to backend directory"
Write-Host "   cd c:\Users\user\Desktop\swaga\backend`n" -ForegroundColor Gray

Show-NextStep "Activate Python environment"
Write-Host "   aiapi\venv\Scripts\Activate.ps1`n" -ForegroundColor Gray

Show-NextStep "Install dependencies"
Write-Host "   pip install -r aiapi/requirements.txt`n" -ForegroundColor Gray

# Testing section
Write-Host "`n🧪 TESTING PHASE" -ForegroundColor Green
Write-Host "═" * 60 -ForegroundColor Green

Write-Host "`n1️⃣  START SERVICES (need 2 terminals):" -ForegroundColor Cyan
Write-Host "   Terminal 1: npm start" -ForegroundColor Gray
Write-Host "   Terminal 2: python aiapi/app.py`n" -ForegroundColor Gray

Write-Host "2️⃣  OPEN APPLICATION:" -ForegroundColor Cyan
Write-Host "   Browser: http://localhost:3000`n" -ForegroundColor Gray

# Testing features
Write-Host "`n✅ TEST NEW FEATURES:" -ForegroundColor Green
Write-Host "═" * 60 -ForegroundColor Green

Write-Host "`n📚 Test #1: PDF Knowledge Base" -ForegroundColor Yellow
Write-Host "  1. Open the app in browser (http://localhost:3000)" -ForegroundColor Gray
Write-Host "  2. Select 'AI Assistant' or similar chat feature" -ForegroundColor Gray
Write-Host "  3. Ask: 'What was the 751 Atlach war?'" -ForegroundColor Gray
Write-Host "  4. Expected: Response with excerpts from PDF textbooks" -ForegroundColor Gray
Write-Host "  5. Check console: Should see '📚 Found X PDF matches'" -ForegroundColor Gray
Write-Host "     Status: ✅ PASS if excerpts mention '751' or 'Atlach'" -ForegroundColor Green

Write-Host "`n🌐 Test #2: Language Detection" -ForegroundColor Yellow
Write-Host "  1. Select 'Kazakh' in language menu (🇰🇿)" -ForegroundColor Gray
Write-Host "  2. Type a message in Russian (e.g., 'Привет, как дела?')" -ForegroundColor Gray
Write-Host "  3. Send message" -ForegroundColor Gray
Write-Host "  4. Expected: Response in RUSSIAN (detected automatically)" -ForegroundColor Gray
Write-Host "  5. Check console: Should see '[START] Detected language: ru'" -ForegroundColor Gray
Write-Host "     Status: ✅ PASS if response is NOT in Kazakh" -ForegroundColor Green

Write-Host "`n🇮🇳 Test #3: Hindi Language Support" -ForegroundColor Yellow
Write-Host "  1. Click language menu button" -ForegroundColor Gray
Write-Host "  2. Look for Hindi button: '🇮🇳 नमस्ते'" -ForegroundColor Gray
Write-Host "  3. Click Hindi button" -ForegroundColor Gray
Write-Host "  4. Expected: All UI text changes to Hindi" -ForegroundColor Gray
Write-Host "  5. Type a question in Hindi and send" -ForegroundColor Gray
Write-Host "  6. Expected: Response in Hindi" -ForegroundColor Gray
Write-Host "     Status: ✅ PASS if menu shows Hindi and responses are in Hindi" -ForegroundColor Green

Write-Host "`n🔄 Test #4: Multi-language with PDF" -ForegroundColor Yellow
Write-Host "  1. Keep language as English (🇬🇧)" -ForegroundColor Gray
Write-Host "  2. Ask history question: 'Tell me about Seleucida'" -ForegroundColor Gray
Write-Host "  3. Expected: Response in English with PDF excerpts" -ForegroundColor Gray
Write-Host "  4. Now select Hindi language" -ForegroundColor Gray
Write-Host "  5. Ask same question" -ForegroundColor Gray
Write-Host "  6. Expected: Response in Hindi with PDF excerpts" -ForegroundColor Gray
Write-Host "     Status: ✅ PASS if responses match selected language" -ForegroundColor Green

# Troubleshooting
Write-Host "`n⚠️  TROUBLESHOOTING:" -ForegroundColor Red
Write-Host "═" * 60 -ForegroundColor Red

Write-Host "`n❌ If PDF search doesn't work:" -ForegroundColor Yellow
Write-Host "   • Check PDF files exist: ls c:\Users\user\Downloads\Қаз*.pdf" -ForegroundColor Gray
Write-Host "   • Filename should start with 'Қаз Тарих'" -ForegroundColor Gray
Write-Host "   • Run: python -c 'from services.pdf_knowledge_service import *; kb = PDFKnowledgeBase(); kb.load_pdfs()'" -ForegroundColor Gray

Write-Host "`n❌ If language detection doesn't work:" -ForegroundColor Yellow   
Write-Host "   • Check langdetect installed: pip show langdetect" -ForegroundColor Gray
Write-Host "   • Test: python -c 'from langdetect import detect; print(detect(\"Привет\"))'" -ForegroundColor Gray

Write-Host "`n❌ If Hindi doesn't show:" -ForegroundColor Yellow
Write-Host "   • Check browser cache (Ctrl+Shift+Del)" -ForegroundColor Gray
Write-Host "   • Reload page (F5)" -ForegroundColor Gray
Write-Host "   • Check i18n object in script.js has 'hi' key" -ForegroundColor Gray

Write-Host "`n❌ If services won't start:" -ForegroundColor Yellow
Write-Host "   • Check Node.js: node --version" -ForegroundColor Gray
Write-Host "   • Check Python: python --version" -ForegroundColor Gray
Write-Host "   • Run: npm install" -ForegroundColor Gray
Write-Host "   • Check .env file exists with OPENAI_API_KEY" -ForegroundColor Gray

# Summary
Write-Host "`n📊 EXPECTED RESULTS SUMMARY:" -ForegroundColor Cyan
Write-Host "═" * 60 -ForegroundColor Cyan
Write-Host "  ✅ PDF excerpts appear in history questions" -ForegroundColor Green
Write-Host "  ✅ Language auto-detects from message content" -ForegroundColor Green
Write-Host "  ✅ Hindi option available and fully functional" -ForegroundColor Green
Write-Host "  ✅ Responses match selected/detected language" -ForegroundColor Green
Write-Host "  ✅ No performance degradation (<30s per query)" -ForegroundColor Green

Write-Host "`n📝 Don't forget to report:" -ForegroundColor Yellow
Write-Host "  • Which tests passed/failed" -ForegroundColor Gray
Write-Host "  • Response times" -ForegroundColor Gray
Write-Host "  • Any error messages in browser console" -ForegroundColor Gray
Write-Host "  • Clarification on 'Option B variant'" -ForegroundColor Gray

Write-Host "`n" -ForegroundColor Cyan
