# System Architecture - New Features

## Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        FRONTEND (React/Vanilla JS)               │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  Language Selector: 🇰🇿 🇷🇺 🇬🇧 🇮🇳 (NEW)               │ │
│  │  i18n: 80+ Hindi strings (NEW)                            │ │
│  │  Chat Interface: Send message + language                  │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                            ↓
         HTTP Request (message + frontend_lang)
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│                    NODE.js BACKEND (Express)                     │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │  POST /api/ai/chat                                        │ │
│  │  └─ Forwards to Python AI API                             │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                            ↓
         HTTP Request (message + language)
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│               PYTHON AI API (Flask) ← NEW/MODIFIED              │
│                                                                  │
│  ┌─ Language Detection (NEW) ──────────────────────────────┐   │
│  │  detect_message_language(text)                          │   │
│  │  override_language_if_detected(message, frontend_lang)  │   │
│  │  Result: Final language for response                    │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            ↓                                     │
│  ┌─ PDF Knowledge Base (NEW) ──────────────────────────────┐   │
│  │  search_pdf_knowledge(query)                            │   │
│  │  - Loads 11 Kazakh history books (lazy)                 │   │
│  │  - Keyword search (years, names)                        │   │
│  │  - Text similarity fallback                             │   │
│  │  Result: Top 3 relevant excerpts                        │   │
│  └─────────────────────────────────────────────────────────┘   │
│                            ↓                                     │
│  ┌─ Existing RAG System ──────────────────────────────────────┐ │
│  │  knowledge_matches (optional)                            │ │
│  │  Combines with PDF results ↑                             │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            ↓                                     │
│  ┌─ LLM Context Preparation ──────────────────────────────────┐ │
│  │  System Prompt:                                          │ │
│  │  - Language instruction (Kazakh/Russian/English/Hindi)   │ │
│  │  - "Use provided knowledge sources FIRST"               │ │
│  │  - "DO NOT say 'go to library'"                         │ │
│  │  - PDF excerpts merged with RAG matches                 │ │
│  │  - Chat history included                                │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            ↓                                     │
│  ┌─ OpenAI API (gpt-4o / gpt-5.4) ────────────────────────────┐ │
│  │  Generates response in correct language                  │ │
│  │  Using PDF excerpts as primary source                   │ │
│  └────────────────────────────────────────────────────────────┘ │
│                            ↓                                     │
│  ┌─ Response with Language ────────────────────────────────────┐ │
│  │  {                                                       │ │
│  │    "message": "Answer from PDF or GPT",                 │ │
│  │    "language": "ru",  ← Detected/selected language      │ │
│  │    "language_name": "Russian"                           │ │
│  │  }                                                       │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                            ↓
         HTTP Response with language info
                            ↓
┌─────────────────────────────────────────────────────────────────┐
│                        FRONTEND DISPLAY                          │
│  - Response shown in chat                                       │
│  - Language matches detected/selected language                 │
│  - UI updates if language was overridden                       │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow Examples

### Example 1: PDF Knowledge + Language Detection
```
INPUT:
  message: "751 жылы атлах соғысы туралы"
  language: "kk"  (Frontend selected Kazakh)

PROCESSING:
  1. Language Detection:
     detect_message_language("751 жылы атлах...")
     → Detects: Kazakh (95% confidence)
     → Final lang: "kk" (no override needed)
  
  2. PDF Search:
     search_pdf_knowledge("751 жылы атлах...")
     → Found in: Қаз Тарих 9 сынып.pdf
     → Excerpt: "751 жылы батысты қазақ өлімдерінің..."
  
  3. LLM Context:
     system_prompt: "Reply in Kazakh. Use provided knowledge first."
     knowledge_sources: [PDF excerpt, ...]
     chat_history: [...]

OUTPUT:
  message: "751 жылы атлах соғысында... (from PDF)"
  language: "kk"
```

### Example 2: Language Override
```
INPUT:
  message: "What is photosynthesis?"
  language: "kk"  (Frontend selected Kazakh)

PROCESSING:
  1. Language Detection:
     detect_message_language("What is photosynthesis?")
     → Detects: English (98% confidence)
     → Override: Change "kk" → "en"
  
  2. PDF Search:
     search_pdf_knowledge("What is photosynthesis?")
     → No PDF matches (history textbooks, not biology)
     → Falls back to GPT knowledge

  3. LLM Context:
     system_prompt: "Reply in English."
     knowledge_sources: []

OUTPUT:
  message: "Photosynthesis is the process..."
  language: "en"  ← Overridden to English
```

### Example 3: Multi-language Student
```
STUDENT 1:
  Selected: Hindi (🇮🇳)
  Message: "मुझे भारत के बारे में बताएं"
  → Detects: Hindi
  → Responds: In Hindi
  → UI: All in Hindi

STUDENT 2:
  Selected: Russian (🇷🇺)
  Message: "Расскажи про войну 1812"
  → Detects: Russian
  → Searches PDFs for 1812
  → Responds: In Russian with PDF excerpts
  → UI: All in Russian
```

## File Organization

```
backend/
  aiapi/
    services/
      ├── pdf_knowledge_service.py      ← NEW
      │   ├── PDFKnowledgeBase class
      │   ├── load_pdfs()
      │   ├── search()
      │   └── search_pdf_knowledge() function
      │
      ├── language_detector.py          ← NEW
      │   ├── detect_message_language()
      │   ├── get_language_confidence()
      │   └── override_language_if_detected()
      │
      ├── assistant_service.py          ← MODIFIED
      │   ├── Imports both services
      │   ├── In generate_assistant_response():
      │   │   ├── lang = override_language_if_detected(...)
      │   │   ├── pdf_matches = search_pdf_knowledge(...)
      │   │   └── Integrate in knowledge_matches
      │   └── Hindi support in language methods
      │
      └── requirements.txt              ← UPDATED
          ├── PyPDF2>=4.0.0
          ├── pdfplumber>=0.10.0
          └── langdetect>=1.0.9

frontend/
  ├── index.html                       ← MODIFIED
  │   └── Added Hindi button (🇮🇳 नमस्ते)
  │
  └── script.js                        ← MODIFIED
      ├── Added i18n.hi = {...} (80+ strings)
      └── setLanguage() already handles new language
```

## Integration Points

### 1. Language Detection → Assistant Service
```python
# In generate_assistant_response()
from language_detector import override_language_if_detected

lang = override_language_if_detected(message, lang)
# Result: lang is now detected from message content
```

### 2. PDF Search → Assistant Service
```python
# In generate_assistant_response()
from pdf_knowledge_service import search_pdf_knowledge

pdf_matches = search_pdf_knowledge(message, max_results=3)
knowledge_matches = (knowledge_matches or []) + pdf_matches
# Result: PDF excerpts added to knowledge context
```

### 3. Hindi Support → All Language Methods
```python
# In _normalize_lang(), _assistant_language_instruction(), etc.
if lang.startswith("hi"):
    return "hi"  # or appropriate Hindi instruction

# In frontend i18n
i18n.hi = {
    menu: 'मेनू',
    // ... 80+ more strings
}
```

## Performance Characteristics

```
┌─────────────────────────────────────────────┐
│          Request Processing Timeline         │
└─────────────────────────────────────────────┘

0ms   ────── Request arrives
      │
      ├─ Language detection: ~5ms
      │  └─ Checking message text
      │
      ├─ PDF search (if needed): ~50-200ms
      │  ├─ First call: loads PDFs (~5-10s)
      │  └─ Cached: ~50ms
      │
      ├─ LLM preparation: ~100-200ms
      │  └─ Building context
      │
      ├─ OpenAI API call: ~10-15s
      │  └─ Model inference
      │
      └───────── Response ready (~10-25s total)
```

## Security & Safety

- No direct file system access (sandboxed PDF loading)
- Language detection is local (no external API)
- PDF loading only from designated folder
- All operations are read-only (no file modification)
- Error handling prevents crashes

## Scalability Considerations

- PDF index: ~100MB for 11 books (one-time load)
- Language detection: <1MB (library only)
- Search performance: O(n) prefix matching, acceptable for current size
- Can scale to 100+ books with vector embeddings (future)
