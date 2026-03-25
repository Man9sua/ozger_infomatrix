"""
Language Detection Service
Detect message language for proper response handling
"""

import logging
from typing import Optional

try:
    from langdetect import detect as detect_lang
    from langdetect import detect_langs
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False

logger = logging.getLogger(__name__)


def detect_message_language(text: str) -> Optional[str]:
    """
    Detect language of message text
    Returns: 'kk' (Kazakh), 'ru' (Russian), 'en' (English), 'hi' (Hindi), or None
    """
    if not text or len(text.strip()) < 3:
        return None
    
    if not LANGDETECT_AVAILABLE:
        return None
    
    try:
        # Get top language with confidence
        detected = detect_lang(text)
        
        # Map to our supported languages
        lang_map = {
            'kk': 'kk',      # Kazakh
            'ka': 'kk',      # Sometimes detected as Georgian, but for Cyrillic Kazakh
            'ru': 'ru',      # Russian
            'en': 'en',      # English
            'hi': 'hi',      # Hindi
        }
        
        normalized = lang_map.get(detected, detected)
        logger.debug(f"Detected language: {detected} -> {normalized}")
        return normalized
        
    except Exception as e:
        logger.debug(f"Language detection error: {str(e)}")
        return None


def get_language_confidence(text: str) -> dict:
    """
    Get confidence scores for all detected languages
    Returns: {language: confidence, ...}
    """
    if not text or len(text.strip()) < 3:
        return {}
    
    if not LANGDETECT_AVAILABLE:
        return {}
    
    try:
        probs = detect_langs(text)
        result = {}
        for prob in probs:
            result[prob.lang] = round(float(prob.prob), 2)
        return result
    except Exception as e:
        logger.debug(f"Language confidence error: {str(e)}")
        return {}


def override_language_if_detected(user_message: str, frontend_language: str) -> str:
    """
    If message is clearly in different language than frontend setting, use detected language
    This handles case like: user selected "Kazakh" in UI but writes in Russian
    """
    detected = detect_message_language(user_message)
    
    if detected and detected != frontend_language:
        confidence = get_language_confidence(user_message)
        
        # Only override if high confidence (>70%)
        if confidence.get(detected, 0) > 0.7:
            logger.info(f"Language override: {frontend_language} -> {detected} "
                       f"(confidence: {confidence[detected]})")
            return detected
    
    return frontend_language
