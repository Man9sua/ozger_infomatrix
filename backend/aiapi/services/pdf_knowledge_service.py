"""
PDF Knowledge Base Service
Loads and indexes historical information from PDF textbooks
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from langdetect import detect as detect_lang
except ImportError:
    detect_lang = None

logger = logging.getLogger(__name__)


class PDFKnowledgeBase:
    """Load and search historical information from PDF files"""
    
    def __init__(self):
        self.documents = []
        self.index = {}  # {keyword: [doc_id, ...]}
        self.pdf_dir = Path(r"c:\Users\user\Downloads")
        self.pdf_files = [
            "Қаз Тарих 11 сынып 2 бөлім.pdf",
            "Қаз Тарих 9(8) сынып .pdf",
            "Қаз Тарих 11 сынып 1 бөлім .pdf",
            "Қаз Тарих 10 сынып .pdf",
            "Қаз Тарих 8-9 сынып 2 бөлім .pdf",
            "Қаз Тарих 8-9 сынып 1 бөлім.pdf",
            "Қаз Тарих 9 сынып .pdf",
            "Қаз Тарих 8 сынып.pdf",
            "Қаз Тарих 8(7) сынып.pdf",
            "Қаз Тарих 7(6) сынып .pdf",
            "Қаз Тарих 6 сынып.pdf"
        ]
        self.loaded = False
    
    def load_pdfs(self) -> bool:
        """Load all PDF files and build knowledge base"""
        if self.loaded:
            return True
        
        if not pdfplumber:
            logger.warning("pdfplumber not installed. PDF knowledge base disabled.")
            return False
        
        logger.info("📚 Loading PDF knowledge base...")
        
        for pdf_name in self.pdf_files:
            pdf_path = self.pdf_dir / pdf_name
            
            if not pdf_path.exists():
                logger.warning(f"PDF not found: {pdf_path}")
                continue
            
            try:
                logger.info(f"📖 Processing: {pdf_name}")
                with pdfplumber.open(pdf_path) as pdf:
                    text = ""
                    for page in pdf.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text += page_text + "\n"
                
                # Split into chunks
                chunks = self._chunk_text(text, chunk_size=1000)
                
                for chunk_idx, chunk in enumerate(chunks):
                    doc_id = len(self.documents)
                    self.documents.append({
                        "id": doc_id,
                        "file": pdf_name,
                        "chunk": chunk_idx,
                        "text": chunk,
                        "tokens": len(chunk.split())
                    })
                    
                    # Index keywords
                    self._index_document(doc_id, chunk)
                
                logger.info(f"✅ {pdf_name}: {len(chunks)} chunks indexed")
                
            except Exception as e:
                logger.error(f"❌ Error processing {pdf_name}: {str(e)}")
        
        self.loaded = True
        logger.info(f"📚 Knowledge base ready: {len(self.documents)} documents")
        return bool(self.documents)
    
    def _chunk_text(self, text: str, chunk_size: int = 1000) -> list[str]:
        """Split text into overlapping chunks"""
        sentences = re.split(r'[.!?]\s+', text)
        chunks = []
        current_chunk = ""
        
        for sentence in sentences:
            if len(current_chunk) + len(sentence) < chunk_size:
                current_chunk += sentence + ". "
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = sentence + ". "
        
        if current_chunk:
            chunks.append(current_chunk.strip())
        
        return chunks
    
    def _index_document(self, doc_id: int, text: str):
        """Index document keywords for fast search"""
        # Extract years (1800–1900)
        years = re.findall(r'\b(1[0-9]{3}|2[0-9]{3})\b', text)
        for year in years:
            if year not in self.index:
                self.index[year] = []
            if doc_id not in self.index[year]:
                self.index[year].append(doc_id)
        
        # Extract named entities (capitalized words)
        words = re.findall(r'\b[А-Яа-яҚқҢңҒғҮүІіӘәЗз][а-яәіӘҚқҢңҒғҮүІ]*\b', text)
        for word in set(words[:50]):  # Top 50 unique words
            word_lower = word.lower()
            if word_lower not in self.index:
                self.index[word_lower] = []
            if doc_id not in self.index[word_lower]:
                self.index[word_lower].append(doc_id)
    
    def search(self, query: str, max_results: int = 3) -> list[dict]:
        """Search knowledge base for relevant documents"""
        if not self.loaded:
            self.load_pdfs()
        
        if not self.documents:
            return []
        
        # Extract keywords from query
        years = re.findall(r'\b(1[0-9]{3}|2[0-9]{3})\b', query)
        words = re.findall(r'\b[а-яәіӘҚқҢңҒғҮүІ]{3,}\b', query.lower())
        
        # Find matching documents
        matching_docs = set()
        
        for year in years:
            if year in self.index:
                matching_docs.update(self.index[year])
        
        for word in words:
            if word in self.index:
                matching_docs.update(self.index[word])
        
        if not matching_docs:
            # Fallback: search by text similarity
            matching_docs = self._text_search(query)
        
        # Sort by relevance (how many keywords matched)
        results = []
        for doc_id in list(matching_docs)[:max_results]:
            doc = self.documents[doc_id]
            results.append({
                "file": doc["file"],
                "excerpt": doc["text"][:500],  # First 500 chars
                "full_text": doc["text"]
            })
        
        return results
    
    def _text_search(self, query: str, max_docs: int = 3) -> set:
        """Fallback: search by text similarity"""
        query_lower = query.lower()
        matching = set()
        
        for idx, doc in enumerate(self.documents):
            if query_lower in doc["text"].lower():
                matching.add(idx)
                if len(matching) >= max_docs:
                    break
        
        return matching


# Global instance
_kb_instance: Optional[PDFKnowledgeBase] = None


def get_pdf_knowledge_base() -> PDFKnowledgeBase:
    """Get or create PDF knowledge base instance"""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = PDFKnowledgeBase()
    return _kb_instance


def search_pdf_knowledge(query: str, max_results: int = 3) -> list[dict]:
    """Search PDF knowledge base"""
    kb = get_pdf_knowledge_base()
    return kb.search(query, max_results)
