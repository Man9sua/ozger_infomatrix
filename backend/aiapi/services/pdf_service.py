"""
PDF Processing Service
Uses PyMuPDF (fitz) to extract text from PDF files
"""

import io

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

from PyPDF2 import PdfReader


def extract_text_from_pdf(pdf_file) -> str:
    """
    Extract text from a PDF file.
    
    Args:
        pdf_file: File object or bytes containing PDF data
        
    Returns:
        Extracted text as string
    """
    try:
        # If it's a file-like object, read bytes
        if hasattr(pdf_file, 'read'):
            pdf_bytes = pdf_file.read()
            # Best-effort reset (helps if caller reuses the file object)
            try:
                pdf_file.seek(0)
            except Exception:
                pass
        else:
            pdf_bytes = pdf_file
            
        text_content = []

        if fitz is not None:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                text = page.get_text("text")
                if text.strip():
                    # Add page markers so the model can reference locations in large PDFs
                    text_content.append(f"[PAGE {page_num + 1}]\n{text.strip()}")
            doc.close()
        else:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for page_num, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    text_content.append(f"[PAGE {page_num + 1}]\n{text.strip()}")

        return "\n\n".join(text_content)
        
    except Exception as e:
        raise Exception(f"Ошибка при чтении PDF: {str(e)}")


def get_pdf_info(pdf_file) -> dict:
    """
    Get information about PDF file.
    
    Args:
        pdf_file: File object or bytes containing PDF data
        
    Returns:
        Dictionary with PDF metadata
    """
    try:
        if hasattr(pdf_file, 'read'):
            pdf_bytes = pdf_file.read()
            pdf_file.seek(0)  # Reset file pointer
        else:
            pdf_bytes = pdf_file
            
        if fitz is not None:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            info = {
                "page_count": len(doc),
                "metadata": doc.metadata,
            }
            doc.close()
        else:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            info = {
                "page_count": len(reader.pages),
                "metadata": {},
            }
        
        return info
        
    except Exception as e:
        raise Exception(f"Ошибка при получении информации о PDF: {str(e)}")
