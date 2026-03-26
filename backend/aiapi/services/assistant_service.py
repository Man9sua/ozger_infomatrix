"""
OpenAI GPT API Service
Handles all AI generation for learning content, questions, and tests
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
import json
import logging
import os
import math
import re
import time
import uuid
from functools import lru_cache
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled at runtime
    OpenAI = None
from .gemini_service import get_gemini_service
from .supabase_service import SupabaseService, SupabaseServiceError
from .pdf_knowledge_service import search_pdf_knowledge
from .language_detector import override_language_if_detected


logger = logging.getLogger(__name__)
_PDF_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assistant_pdf_search")

MAX_ACTIVE_MATERIAL_EXCERPT_CHARS = 3000
MAX_MATERIAL_CHARS_HISTORY = 70_000
MAX_MATERIAL_CHARS_DEFAULT = 50_000
MAX_SUMMARY_CHUNKS = 16
MAX_ASSISTANT_HISTORY_MESSAGES = 40
MAX_ASSISTANT_SHORT_TERM_MESSAGES = 12
MAX_ASSISTANT_MESSAGE_CHARS = 4000
MAX_JSON_LOG_PREVIEW_CHARS = 700
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
DEFAULT_OPENAI_SDK_RETRIES = 3
MAX_ASSISTANT_RECENT_ERRORS = 10
MAX_ASSISTANT_ACTIONS = 3
MAX_ASSISTANT_CITATIONS = 3
MAX_ASSISTANT_RECENT_ROUTES = 8
MAX_ASSISTANT_FACTS = 12
MAX_ASSISTANT_RECENT_QUIZ_RESULTS = 8
TOOL_RETRY_ATTEMPTS = 3
ASSISTANT_MAX_PIPELINE_SECONDS = 28.0
ASSISTANT_FAST_MODE_DEFAULT = "true"
MAX_ASSISTANT_RECENT_CONTEXT_MESSAGES = 8
MAX_ASSISTANT_SESSION_SUMMARY_LINES = 8
MAX_ASSISTANT_SESSION_SUMMARY_CHARS = 1400

ASSISTANT_ROUTE_META = {
    "home": {
        "labels": {"kk": "Басты бет", "ru": "Главная", "en": "Home"},
        "keywords": ("home", "главн", "домой", "басты", "асты бет"),
        "description": "Main dashboard with entry points to the site's core tools.",
    },
    "library": {
        "labels": {"kk": "Кітапхана", "ru": "Библиотека", "en": "Library"},
        "keywords": ("library", "библиотек", "кітапхана", "community", "материал"),
        "description": "Community/library area for browsing saved learning materials and tests.",
    },
    "upload": {
        "labels": {"kk": "Жүктеу", "ru": "Загрузка", "en": "Upload"},
        "keywords": ("upload", "загруз", "жүкт", "добавь материал", "add material"),
        "description": "Upload area for adding new text or PDF learning materials.",
    },
    "favorites": {
        "labels": {"kk": "Таңдаулылар", "ru": "Избранное", "en": "Favorites"},
        "keywords": ("favorite", "favorites", "избран", "таңдаул"),
        "description": "Saved materials and tests the student marked as favorites.",
    },
    "guess_game": {
        "labels": {"kk": "Guess ойыны", "ru": "Игра Guess", "en": "Guess Game"},
        "keywords": ("guess", "guess game", "угадай", "ойын", "game"),
        "description": "Historical figure guessing game based on facts and hints.",
    },
    "ai_learn": {
        "labels": {"kk": "Learn", "ru": "Learn", "en": "Learn"},
        "keywords": ("learn", "оқу", "изуч", "оқып", "learning"),
        "description": "AI Learn section for theory explanations and structured studying.",
    },
    "ai_practice": {
        "labels": {"kk": "Practice", "ru": "Practice", "en": "Practice"},
        "keywords": ("practice", "практи", "жаттығ", "exercise"),
        "description": "AI Practice section for practice quizzes and targeted drills.",
    },
    "ai_realtest": {
        "labels": {"kk": "Real Test", "ru": "Real Test", "en": "Real Test"},
        "keywords": ("real test", "realtest", "ент", "экзамен", "сынақ", "mock exam"),
        "description": "Timed real-test style practice for exam simulation.",
    },
    "assistant": {
        "labels": {"kk": "Ассистент", "ru": "Ассистент", "en": "Assistant"},
        "keywords": ("assistant", "ассистент", "navigator", "навига", "ai assistant"),
        "description": "Assistant workspace for chat, study guidance, retrieval, and navigation.",
    },
    "profile": {
        "labels": {"kk": "Профиль", "ru": "Профиль", "en": "Profile"},
        "keywords": ("profile", "профил", "аккаунт"),
        "description": "Student profile with personal and academic context.",
    },
    "classmates": {
        "labels": {"kk": "Сыныптастар", "ru": "Одноклассники", "en": "Classmates"},
        "keywords": ("classmate", "classmates", "однокласс", "сыныптас", "class"),
        "description": "Classmates/community section showing schoolmates and class context.",
    },
}

ASSISTANT_ROUTES = list(ASSISTANT_ROUTE_META.keys())

ASSISTANT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "navigate_to_section",
            "description": "Navigates the user to a specific app section",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {
                        "type": "string",
                        "enum": ASSISTANT_ROUTES,
                        "description": "Technical route name for app navigation",
                    }
                },
                "required": ["route"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_educational_quiz",
            "description": "Starts a quiz from a material or historical figure source",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "source_type": {
                        "type": "string",
                        "enum": ["material", "historical_figure"],
                    },
                    "question_count": {"type": "integer", "default": 10},
                },
                "required": ["source_id", "source_type"],
            },
        },
    },
]


class ActionButtonPayload(BaseModel):
    route: Optional[str] = Field(None, description="Technical route name for navigation")
    source_id: Optional[str] = Field(None, description="Source id for quiz start")
    source_type: Optional[Literal["material", "historical_figure"]] = Field(
        None,
        description="Source type for quiz start",
    )
    source_title: Optional[str] = Field(None, description="Human-readable quiz topic/title")
    assistant_prompt: Optional[str] = Field(
        None,
        description="Original user prompt that triggered the quiz action",
    )
    language: Optional[str] = Field(
        None,
        description="Preferred quiz output language for this action",
    )
    question_count: Optional[int] = Field(None, description="Question count for quiz")
    mode: Optional[Literal["practice", "realtest"]] = Field(
        "practice",
        description="Quiz mode",
    )


class ActionButton(BaseModel):
    label: str = Field(..., description="Student-visible action label")
    type: Literal["navigate", "start_quiz"] = Field(..., description="Action type")
    payload: ActionButtonPayload


class Citation(BaseModel):
    id: str
    title: str
    excerpt: str = Field(..., description="Short source excerpt")


class TutorResponse(BaseModel):
    reasoning: str = Field(..., description="Internal reasoning not shown in UI")
    message: str = Field(..., description="Final student-facing message")
    intent: Literal["answer", "navigate", "quiz", "plan"] = Field(...)
    action_buttons: list[ActionButton] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    plan_steps: Optional[list[str]] = Field(
        default=None,
        description="Deterministic study steps for intent=plan",
    )
    old_format_actions: Optional[list[dict[str, Any]]] = None


def _normalize_free_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", str(value or "").strip()))


def _merge_text_lists(*values: Any, limit: int = 12) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, list):
            for item in value:
                text = str(item or "").strip()
                if text and text not in result:
                    result.append(text)
    return result[:limit]


class OpenAIService:
    """Service for interacting with OpenAI GPT API"""
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize OpenAI service with API key"""
        if OpenAI is None:
            raise ImportError("openai package is not installed. Run: pip install -r requirements.txt")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")

        try:
            self.request_timeout = max(
                10.0,
                float(os.getenv("OPENAI_TIMEOUT_SECONDS", "30.0")),
            )
        except Exception:
            self.request_timeout = 30.0  # Reduced from 60 to 30 seconds
        try:
            self.sdk_max_retries = max(
                0,
                int(os.getenv("OPENAI_SDK_MAX_RETRIES", str(DEFAULT_OPENAI_SDK_RETRIES))),
            )
        except Exception:
            self.sdk_max_retries = DEFAULT_OPENAI_SDK_RETRIES

        self.client = OpenAI(
            api_key=self.api_key,
            max_retries=self.sdk_max_retries,
            timeout=self.request_timeout,
        )
        self.supabase = SupabaseService()

        # Model to use
        self.model = os.getenv("OPENAI_MODEL", "gpt-5.4")
        fallback_env = os.getenv("OPENAI_MODEL_FALLBACKS", "gpt-4o")
        self.fallback_models: list[str] = []
        for item in fallback_env.split(","):
            model_name = str(item or "").strip()
            if model_name and model_name != self.model and model_name not in self.fallback_models:
                self.fallback_models.append(model_name)
        self.fast_model = os.getenv(
            "OPENAI_FAST_MODEL",
            self.fallback_models[0] if self.fallback_models else self.model,
        ).strip() or self.model
        self.last_model_used = self.model
        
        logger.info(f"🤖 AI Service initialized with model: {self.model}, fallbacks: {self.fallback_models}")
        
        # Base system prompt for ENT preparation
        self.system_prompt = """
Сен - ЕНТ дайындық үшін AI оқытушысың (Қазақстандағы мектеп түлектерінің бірыңғай ұлттық тестілеуі).

МАҢЫЗДЫ ЕРЕЖЕЛЕР:
1. Деңгей: Мектеп деңгейі, ЕНТ форматы
2. ТЕКСТІ БЕРІЛГЕН МАТЕРИАЛДАН ҒАНА пайдалан
3. Күндер, есімдер, оқиғалар - дәл болуы керек
4. Интерпретация немесе пікірлерден аулақ бол
5. Жалған жауаптар шатастыратын, бірақ қате болуы керек

IMPORTANT RULES:
1. Level: School level, ENT format
2. Use ONLY the provided material
3. Dates, names, events must be exact
4. Avoid interpretation or opinions
5. Wrong answers should be confusing but incorrect
"""

    def _normalize_lang(self, lang: Optional[str]) -> str:
        if not lang:
            return "kk"
        lang = lang.strip().lower()
        if lang.startswith("ru"):
            return "ru"
        if lang.startswith("en"):
            return "en"
        if lang.startswith("hi"):
            return "hi"
        return "kk"

    def _language_instruction(self, lang: Optional[str]) -> str:
        lang = self._normalize_lang(lang)
        if lang == "ru":
            return "Ответь строго на русском языке."
        if lang == "en":
            return "Respond strictly in English."
        if lang == "hi":
            return "हिंदी में सख्ती से जवाब दें।"
        return "Тек қазақ тілінде жауап бер."

    def _extract_requested_output_language(self, message: str) -> Optional[str]:
        text = _normalize_free_text(message)
        if not text:
            return None

        language_patterns = {
            "kk": (
                r"\bқазақша\b",
                r"\bқазақ тілінде\b",
                r"\bkazaksha\b",
                r"\bkazakhsha\b",
                r"\bqazaqsha\b",
                r"\bqazaq tilinde\b",
                r"\bна казахском\b",
                r"\bпо[- ]казахски\b",
                r"\bin kazakh\b",
                r"\bwrite in kazakh\b",
                r"\brespond in kazakh\b",
            ),
            "ru": (
                r"\bорысша\b",
                r"\bорыс тілінде\b",
                r"\bна русском\b",
                r"\bпо[- ]русски\b",
                r"\bin russian\b",
                r"\bwrite in russian\b",
                r"\brespond in russian\b",
            ),
            "en": (
                r"\bағылшынша\b",
                r"\bағылшын тілінде\b",
                r"\bна английском\b",
                r"\bпо[- ]английски\b",
                r"\bin english\b",
                r"\bwrite in english\b",
                r"\brespond in english\b",
            ),
            "hi": (
                r"\bहिंदी में\b",
                r"\bна хинди\b",
                r"\bin hindi\b",
                r"\bwrite in hindi\b",
                r"\brespond in hindi\b",
            ),
        }

        for lang_code, patterns in language_patterns.items():
            if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns):
                return lang_code
        return None

    def _resolve_requested_output_language(self, message: str, fallback_lang: Optional[str]) -> str:
        explicit_language = self._extract_requested_output_language(message)
        if explicit_language:
            return self._normalize_lang(explicit_language)
        detected_or_fallback = override_language_if_detected(message, self._normalize_lang(fallback_lang))
        return self._normalize_lang(detected_or_fallback)

    def _chunk_text(self, text: str, *, max_chars: int, overlap: int = 800) -> list[str]:
        """Split long text into overlapping chunks."""
        if not text:
            return []

        text = text.replace("\r\n", "\n")
        max_chars = max(2000, int(max_chars))
        overlap = max(0, int(overlap))
        if overlap >= max_chars:
            overlap = 0

        chunks: list[str] = []
        start = 0
        n = len(text)

        while start < n:
            end = min(n, start + max_chars)
            chunk = text[start:end]

            if end < n:
                search_from = max(0, len(chunk) - 2500)
                cut = chunk.rfind("\n\n", search_from)
                if cut > 0 and cut > len(chunk) * 0.5:
                    end = start + cut
                    chunk = text[start:end]

            chunk = chunk.strip()
            if chunk:
                chunks.append(chunk)

            if end >= n:
                break

            start = max(0, end - overlap)

        return chunks

    def _prepare_large_material(self, material: str, *, target_chars: int, lang: Optional[str] = None) -> str:
        """For very large PDFs/text, build dense study notes via map-reduce summarization."""
        if not material or len(material) <= target_chars:
            return material

        max_chunks = MAX_SUMMARY_CHUNKS
        max_chars = int(math.ceil(len(material) / max_chunks))
        max_chars = max(20000, min(250000, max_chars))
        chunks = self._chunk_text(material, max_chars=max_chars, overlap=1200)

        if len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]

        if len(chunks) <= 1:
            return material[:target_chars] + "\n\n[Материал қысқартылды (өте үлкен мәтін)]"

        notes_parts: list[str] = []

        for idx, chunk in enumerate(chunks, start=1):
            lang_instruction = self._language_instruction(lang)
            prompt = f"""{self.system_prompt}
{lang_instruction}

ТАПСЫРМА: Төмендегі мәтіннің {idx}/{len(chunks)} БӨЛІГІ бойынша өте тығыз, нақты оқу-конспект жаса.
Тек берілген материалдағы фактілерді пайдалан.

ҚҰРЫЛЫМ:
- Key facts (bullets)
- Key terms (bullets)
- Timeline (bullets with year/date where possible)
- Potential ENT traps

МӘТІН:
{chunk}

КОНСПЕКТ:"""

            part = self._generate_with_retry(prompt).strip()
            if part:
                notes_parts.append(part)

        combined_notes = "\n\n---\n\n".join(notes_parts)

        if len(combined_notes) <= target_chars:
            return combined_notes

        lang_instruction = self._language_instruction(lang)
        reduce_prompt = f"""{self.system_prompt}
{lang_instruction}

ТАПСЫРМА: Төмендегі бірнеше бөлімнен тұратын конспектті бір ТҰТАС, өте ықшам оқу-материалына қысқарт.
Ереже: тек фактілер, артық сөз жоқ.

Мақсат: нәтиже ұзындығы шамамен {target_chars} таңбадан аспасын.

КОНСПЕКТ:
{combined_notes}

ЫҚШАМ НӘТИЖЕ:"""

        return self._generate_with_retry(reduce_prompt).strip()

    def _clean_json_response(self, text: str) -> str:
        """Clean and extract JSON from response text"""
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        if text:
            # Recover truncated JSON by tracking open structures in order.
            stack: list[str] = []
            in_string = False
            escaped = False

            for char in text:
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue

                if char == '"':
                    in_string = True
                elif char == "{":
                    stack.append("}")
                elif char == "[":
                    stack.append("]")
                elif char == "}" and stack and stack[-1] == "}":
                    stack.pop()
                elif char == "]" and stack and stack[-1] == "]":
                    stack.pop()

            # If string got cut mid-token, close quote first.
            if in_string:
                text += '"'

            # Close structures in strict reverse-open order.
            while stack:
                text += stack.pop()

        return text

    def _extract_first_json_object(self, text: str) -> str:
        text = str(text or "").strip()
        start = text.find("{")
        if start < 0:
            return text

        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]

        return text[start:]

    def _strip_trailing_commas(self, text: str) -> str:
        return re.sub(r",(\s*[}\]])", r"\1", str(text or ""))

    def _assistant_localized(self, lang: Optional[str], kk: str, ru: str, en: str) -> str:
        lang = self._normalize_lang(lang)
        if lang == "ru":
            return ru
        if lang == "en":
            return en
        return kk
    def _assistant_default_prompts(self, lang: Optional[str]) -> list[str]:
        if self._normalize_lang(lang) == "ru":
            return [
                "Открой мою библиотеку",
                "Покажи результат последнего теста",
                "Сделай practice-тест на 10 вопросов",
                "Ответь по моим материалам",
            ]
        if self._normalize_lang(lang) == "en":
            return [
                "Open my library",
                "Show my latest test result",
                "Create a 10-question practice quiz",
                "Answer using my materials",
            ]
        if self._normalize_lang(lang) == "hi":
            return [
                "मेरी लाइब्रेरी खोलो",
                "मेरा आख़िरी टेस्ट रिज़ल्ट दिखाओ",
                "10 सवालों का practice test बनाओ",
                "मेरी materials के आधार पर जवाब दो",
            ]
        return [
            "Кітапханамды аш",
            "Соңғы тест нәтижемді көрсет",
            "10 сұрақтық practice test жаса",
            "Жауапты менің материалдарымнан бер",
        ]
    def _assistant_language_instruction(self, lang: Optional[str]) -> str:
        lang = self._normalize_lang(lang)
        if lang == "ru":
            return "Reply in Russian."
        if lang == "en":
            return "Reply in English."
        if lang == "hi":
            return "Reply in Hindi."
        return "Reply in Kazakh."

    def _resolve_authenticated_user_id(
        self,
        *,
        requested_user_id: str,
        access_token: Optional[str],
    ) -> tuple[str, Optional[str]]:
        requested = str(requested_user_id or "").strip()
        token = str(access_token or "").strip() or None

        # Never use service-role fallback for user-scoped assistant data
        # unless the user's Supabase token was verified first.
        if not token:
            return "", None

        if not self.supabase.available:
            return requested, token

        try:
            verified_user = self.supabase.verify_user(token)
        except SupabaseServiceError as exc:
            logger.warning("assistant auth verification failed: %s", exc)
            return "", None

        verified_user_id = str((verified_user or {}).get("id") or "").strip()
        if not verified_user_id:
            return "", None
        if requested and requested != verified_user_id:
            raise ValueError("Authenticated user does not match user_id")
        return verified_user_id, token

    def _history_messages(self, chat_history: Optional[list[dict]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        if not isinstance(chat_history, list):
            return normalized
        for item in chat_history[-MAX_ASSISTANT_HISTORY_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            normalized.append(
                {
                    "role": role,
                    "content": content[:MAX_ASSISTANT_MESSAGE_CHARS],
                }
            )
        return normalized

    def _history_without_current_message(
        self,
        history_messages: list[dict[str, str]],
        current_message: str,
    ) -> list[dict[str, str]]:
        if not history_messages:
            return []
        normalized_current = _normalize_free_text(current_message)
        trimmed = list(history_messages)
        while trimmed:
            last = trimmed[-1]
            if last.get("role") != "user":
                break
            if _normalize_free_text(str(last.get("content") or "")) != normalized_current:
                break
            trimmed.pop()
            break
        return trimmed

    def _clean_memory_fragment(self, value: Optional[str], *, limit: int = 160) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        text = text.strip(" \"'`.,:;!?-")
        if not text:
            return ""
        parts = re.split(r"[.!?\n]", text, maxsplit=1)
        text = parts[0].strip() if parts else text
        return text[:limit].strip()

    def _extract_first_pattern_group(self, message: str, patterns: list[str], *, limit: int = 120) -> str:
        raw = str(message or "").strip()
        if not raw:
            return ""
        for pattern in patterns:
            match = re.search(pattern, raw, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = self._clean_memory_fragment(match.group(1), limit=limit)
            if candidate:
                return candidate
        return ""

    def _extract_focus_topic(self, message: str) -> str:
        normalized = _normalize_free_text(message)
        for prefix in (
            "не понял тему ",
            "не понимаю тему ",
            "тему ",
            "тема ",
            "about ",
            "on ",
            "про ",
            "о ",
            "об ",
            "по теме ",
            "туралы ",
            "жөнінде ",
        ):
            if prefix in normalized:
                candidate = self._clean_memory_fragment(normalized.split(prefix, 1)[1], limit=100)
                if candidate:
                    return candidate
        period_match = re.search(r"\b(?:1[0-9]|20)\s*(?:век\w*|ғасыр\w*)\b[^,.!?\n]*", normalized)
        if period_match:
            candidate = self._clean_memory_fragment(period_match.group(0), limit=100)
            if candidate:
                return candidate
        topic = self._extract_first_pattern_group(
            message,
            [
                r"\b(?:about|on)\b\s+([^,.!?\n]+)",
                r"\b(?:про|о|об|по)\b\s+([^,.!?\n]+)",
                r"\b(?:туралы|жөнінде)\b\s+([^,.!?\n]+)",
                r"\b(?:topic|theme|тема|тему)\b\s+([^,.!?\n]+)",
            ],
            limit=100,
        )
        if topic:
            return topic
        cleaned = self._clean_memory_fragment(message, limit=100)
        if len(cleaned.split()) <= 8:
            return cleaned
        return ""

    def _resolve_memory_nickname(
        self,
        *,
        preferred_name: str,
        user_profile: Optional[dict],
    ) -> str:
        profile = user_profile if isinstance(user_profile, dict) else {}
        for candidate in (
            preferred_name,
            profile.get("nickname"),
            profile.get("username"),
            profile.get("name"),
        ):
            clean = self._clean_memory_fragment(candidate, limit=80)
            if clean:
                return clean
        return "Student"

    def _extract_specific_memory_facts(
        self,
        *,
        message: str,
        lang: Optional[str],
        user_profile: Optional[dict],
        preferred_name: str,
        ) -> list[dict[str, Any]]:
        nickname = self._resolve_memory_nickname(
            preferred_name=preferred_name,
            user_profile=user_profile,
        )

        def clean_detail(value: str) -> str:
            detail = re.sub(r"\s+", " ", str(value or "").strip())
            detail = re.sub(
                r"\b(?:and|и|және)\b\s+(?:"
                r"prefer(?:s)?|like(?:s)?|am interested in|interested in|"
                r"предпочита(?:ю|ет)|люб(?:лю|ит)|интересу(?:юсь|ется)|увлека(?:юсь|ется)|"
                r"учу|изучаю|учит|изучает|"
                r"қалаймын|ұната(?:мын|ды)|жақсы көремін|қызыға(?:мын|ды)|"
                r"оқып жүрмін|үйреніп жүрмін|оқиды"
                r")\b.*$",
                "",
                detail,
                flags=re.IGNORECASE,
            )
            detail = re.sub(r"^(?:что|to|the|это)\s+", "", detail, flags=re.IGNORECASE).strip()
            return self._clean_memory_fragment(detail, limit=160)

        fact_specs = [
            (
                "student_studies",
                [
                    r"(?:\bя\b\s+(?:учу|изучаю)\s+)([^,.!?\n]+)",
                    r"(?:\bi\b\s+(?:study|am studying)\s+)([^,.!?\n]+)",
                    r"(?:\bмен\b\s+(?:оқып жүрмін|үйреніп жүрмін)\s+)([^,.!?\n]+)",
                    r"(?:\b[\w\-]{2,}\b\s+(?:учит|изучает|studies|оқиды)\s+)([^,.!?\n]+)",
                ],
                {
                    "kk": f"{nickname} оқып жүр: {{detail}}",
                    "ru": f"{nickname} учит {{detail}}",
                    "en": f"{nickname} studies {{detail}}",
                },
                0.83,
            ),
            (
                "student_interest",
                [
                    r"(?:\bя\b\s+(?:интересуюсь|увлекаюсь)\s+)([^,.!?\n]+)",
                    r"(?:\bi\b\s+(?:am interested in|interested in)\s+)([^,.!?\n]+)",
                    r"(?:\bмен\b\s+(?:қызығамын|қызығушылық танытамын)\s+)([^,.!?\n]+)",
                    r"(?:\b[\w\-]{2,}\b\s+(?:интересуется|увлекается|is interested in|қызығады)\s+)([^,.!?\n]+)",
                ],
                {
                    "kk": f"{nickname} қызығады: {{detail}}",
                    "ru": f"{nickname} интересуется {{detail}}",
                    "en": f"{nickname} is interested in {{detail}}",
                },
                0.8,
            ),
            (
                "student_preference",
                [
                    r"(?:\bя\b\s+(?:предпочитаю|люблю)\s+)([^,.!?\n]+)",
                    r"(?:\bi\b\s+(?:prefer|like)\s+)([^,.!?\n]+)",
                    r"(?:\bмен\b\s+(?:қалаймын|ұнатамын|жақсы көремін)\s+)([^,.!?\n]+)",
                    r"(?:\b[\w\-]{2,}\b\s+(?:предпочитает|любит|prefers|ұнатады)\s+)([^,.!?\n]+)",
                ],
                {
                    "kk": f"{nickname} мынаны қалайды: {{detail}}",
                    "ru": f"{nickname} предпочитает {{detail}}",
                    "en": f"{nickname} prefers {{detail}}",
                },
                0.79,
            ),
        ]

        language_key = self._normalize_lang(lang)
        facts: list[dict[str, Any]] = []
        for fact_key, patterns, templates, confidence in fact_specs:
            detail = clean_detail(self._extract_first_pattern_group(message, patterns, limit=160))
            if not detail:
                continue
            template = templates.get(language_key) or templates["ru"]
            facts.append(
                {
                    "fact_key": fact_key,
                    "fact_value": template.format(detail=detail),
                    "confidence": confidence,
                }
            )
        return facts

    def _collect_chat_memory_signals(
        self,
        *,
        message: str,
        lang: Optional[str],
        user_profile: Optional[dict],
    ) -> dict[str, Any]:
        text = _normalize_free_text(message)
        preferred_language = self._normalize_lang(lang)
        response_style = ""
        preferred_difficulty = ""

        if any(token in text for token in ("кратко", "коротко", "brief", "short", "concise", "қысқа")):
            response_style = "concise"
        elif any(token in text for token in ("подроб", "detail", "step by step", "deep", "толық", "егжей")):
            response_style = "detailed"

        if any(token in text for token in ("проще", "легче", "easy", "easier", "оңай", "жеңіл")):
            preferred_difficulty = "easy"
        elif any(token in text for token in ("сложнее", "harder", "advanced", "қиын", "күрделі")):
            preferred_difficulty = "hard"

        learning_goal = self._extract_first_pattern_group(
            message,
            [
                r"(?:my goal is|i want to|i need to|i'm trying to)\s+([^,.!?\n]+)",
                r"(?:хочу|мне нужно|моя цель|я хочу)\s+([^,.!?\n]+)",
                r"(?:мақсатым|маған керек|қалаймын)\s+([^,.!?\n]+)",
            ],
            limit=140,
        )
        weak_topic = self._extract_first_pattern_group(
            message,
            [
                r"(?:i struggle with|i am weak at|i'm weak at|help me with)\s+([^,.!?\n]+)",
                r"(?:мне трудно|я путаюсь в|я слаб[а-я]* в|помоги с)\s+([^,.!?\n]+)",
                r"(?:не понял(?:а)?|не понимаю|не понял тему|не понимаю тему)\s+([^,.!?\n]+)",
                r"(?:маған қиын|мен қателесемін|көмектес)\s+([^,.!?\n]+)",
                r"(?:түсінбедім|тусінбедім|тусінбедим)\s+([^,.!?\n]+)",
            ],
            limit=100,
        )
        if weak_topic:
            weak_topic = re.sub(r"^(?:topic|theme|тема|тему)\s+", "", weak_topic, flags=re.IGNORECASE).strip()
        strong_topic = self._extract_first_pattern_group(
            message,
            [
                r"(?:i'm good at|i am good at|i know well)\s+([^,.!?\n]+)",
                r"(?:я хорошо знаю|я сил[её]н в)\s+([^,.!?\n]+)",
                r"(?:мен жақсы білемін|мен мықтымын)\s+([^,.!?\n]+)",
            ],
            limit=100,
        )
        preferred_name = self._extract_first_pattern_group(
            message,
            [
                r"(?:call me|my name is)\s+([^,.!?\n]+)",
                r"(?:зови меня|меня зовут)\s+([^,.!?\n]+)",
                r"(?:мені|менің атым)\s+([^,.!?\n]+)",
            ],
            limit=80,
        )
        memory_facts = self._extract_specific_memory_facts(
            message=message,
            lang=lang,
            user_profile=user_profile if isinstance(user_profile, dict) else {},
            preferred_name=preferred_name,
        )
        focus_topic = self._extract_focus_topic(message)
        if not focus_topic and weak_topic:
            focus_topic = weak_topic
        if not weak_topic and self._is_explanation_request(message) and focus_topic:
            weak_topic = focus_topic

        signals = {
            "preferred_language": preferred_language,
            "response_style": response_style,
            "preferred_difficulty": preferred_difficulty,
            "learning_goals": [learning_goal] if learning_goal else [],
            "weak_topics": [weak_topic] if weak_topic else [],
            "strong_topics": [strong_topic] if strong_topic else [],
            "focus_topic": focus_topic,
            "preferred_name": preferred_name,
            "memory_facts": memory_facts,
            "subject_combination": str((user_profile or {}).get("subject_combination") or "").strip(),
        }
        return signals

    def _merge_chat_memory_into_snapshot(
        self,
        snapshot: dict[str, Any],
        signals: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        merged = dict(snapshot or {})
        signal_map = signals if isinstance(signals, dict) else {}
        merged["preferred_language"] = str(
            signal_map.get("preferred_language")
            or merged.get("preferred_language")
            or ""
        ).strip()
        merged["preferred_difficulty"] = str(
            signal_map.get("preferred_difficulty")
            or merged.get("preferred_difficulty")
            or "medium"
        ).strip()
        if signal_map.get("response_style"):
            merged["response_style"] = str(signal_map.get("response_style") or "").strip()
        merged["learning_goals"] = _merge_text_lists(
            signal_map.get("learning_goals") or [],
            merged.get("learning_goals") or [],
            limit=8,
        )
        merged["weak_topics"] = _merge_text_lists(
            signal_map.get("weak_topics") or [],
            merged.get("weak_topics") or [],
            limit=10,
        )
        merged["strong_topics"] = _merge_text_lists(
            signal_map.get("strong_topics") or [],
            merged.get("strong_topics") or [],
            limit=10,
        )

        facts = list(merged.get("facts") or [])
        extra_facts: list[dict[str, Any]] = []
        preferred_name = str(signal_map.get("preferred_name") or "").strip()
        focus_topic = str(signal_map.get("focus_topic") or "").strip()
        if preferred_name:
            extra_facts.append(
                {"fact_key": "preferred_name", "fact_value": preferred_name, "confidence": 0.86}
            )
        if focus_topic:
            extra_facts.append(
                {"fact_key": "focus_topic", "fact_value": focus_topic, "confidence": 0.72}
            )
        if signal_map.get("response_style"):
            extra_facts.append(
                {
                    "fact_key": "response_style",
                    "fact_value": str(signal_map.get("response_style") or "").strip(),
                    "confidence": 0.78,
                }
            )
        if signal_map.get("preferred_difficulty"):
            extra_facts.append(
                {
                    "fact_key": "preferred_difficulty",
                    "fact_value": str(signal_map.get("preferred_difficulty") or "").strip(),
                    "confidence": 0.74,
                }
            )
        for item in signal_map.get("memory_facts") or []:
            if not isinstance(item, dict):
                continue
            fact_key = str(item.get("fact_key") or "").strip()
            fact_value = str(item.get("fact_value") or "").strip()
            if not fact_key or not fact_value:
                continue
            extra_facts.append(
                {
                    "fact_key": fact_key,
                    "fact_value": fact_value,
                    "confidence": item.get("confidence"),
                }
            )

        merged_facts: list[dict[str, Any]] = []
        seen_fact_keys: set[str] = set()
        for item in extra_facts + facts:
            if not isinstance(item, dict):
                continue
            fact_key = str(item.get("fact_key") or "").strip()
            fact_value = str(item.get("fact_value") or "").strip()
            if not fact_key or not fact_value or fact_key in seen_fact_keys:
                continue
            seen_fact_keys.add(fact_key)
            merged_facts.append(item)
        merged["facts"] = merged_facts[:MAX_ASSISTANT_FACTS]
        return merged

    def _summary_lines_from_text(self, summary: Optional[str]) -> list[str]:
        lines: list[str] = []
        for raw_line in str(summary or "").splitlines():
            clean = self._clean_memory_fragment(raw_line.lstrip("-* "), limit=180)
            if clean and clean not in lines:
                lines.append(clean)
        return lines

    def _build_session_summary(
        self,
        *,
        existing_summary: str,
        signals: Optional[dict[str, Any]],
        user_profile: Optional[dict],
        page_context: Optional[dict],
        assistant_payload: Optional[dict] = None,
    ) -> str:
        lines = self._summary_lines_from_text(existing_summary)
        signal_map = signals if isinstance(signals, dict) else {}
        additions: list[str] = []

        preferred_language = str(signal_map.get("preferred_language") or "").strip()
        if preferred_language:
            additions.append(f"Preferred language: {preferred_language}")
        response_style = str(signal_map.get("response_style") or "").strip()
        if response_style:
            additions.append(f"Response style: {response_style}")
        preferred_difficulty = str(signal_map.get("preferred_difficulty") or "").strip()
        if preferred_difficulty:
            additions.append(f"Preferred difficulty: {preferred_difficulty}")

        subject_combination = str(
            signal_map.get("subject_combination")
            or (user_profile or {}).get("subject_combination")
            or ""
        ).strip()
        if subject_combination:
            additions.append(f"Subjects: {subject_combination}")

        for goal in (signal_map.get("learning_goals") or [])[:2]:
            clean_goal = self._clean_memory_fragment(goal, limit=160)
            if clean_goal:
                additions.append(f"Learning goal: {clean_goal}")
        for topic in (signal_map.get("weak_topics") or [])[:2]:
            clean_topic = self._clean_memory_fragment(topic, limit=120)
            if clean_topic:
                additions.append(f"Weak topic: {clean_topic}")
        for topic in (signal_map.get("strong_topics") or [])[:2]:
            clean_topic = self._clean_memory_fragment(topic, limit=120)
            if clean_topic:
                additions.append(f"Strong topic: {clean_topic}")

        focus_topic = self._clean_memory_fragment(signal_map.get("focus_topic"), limit=120)
        if focus_topic:
            additions.append(f"Current focus: {focus_topic}")

        route = str((page_context or {}).get("route") or "").strip()
        if route:
            additions.append(f"Recent route: {route}")

        if isinstance(assistant_payload, dict):
            intent = str(assistant_payload.get("intent") or "").strip()
            if intent:
                additions.append(f"Latest assistant intent: {intent}")

        merged: list[str] = []
        seen: set[str] = set()
        for line in additions + lines:
            clean = self._clean_memory_fragment(line, limit=180)
            if not clean:
                continue
            normalized = _normalize_free_text(clean)
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(clean)
            if len(merged) >= MAX_ASSISTANT_SESSION_SUMMARY_LINES:
                break

        summary = "\n".join(f"- {line}" for line in merged)
        return summary[:MAX_ASSISTANT_SESSION_SUMMARY_CHARS].strip()

    def _build_recent_history_summary(self, history_messages: list[dict[str, str]]) -> str:
        if not history_messages:
            return ""
        lines: list[str] = []
        for item in history_messages[-MAX_ASSISTANT_RECENT_CONTEXT_MESSAGES:]:
            role = str(item.get("role") or "").strip().lower()
            content = self._clean_memory_fragment(item.get("content"), limit=180)
            if not content:
                continue
            if role == "user":
                lines.append(f"- User: {content}")
            elif role == "assistant":
                lines.append(f"- Assistant: {content}")
        return "\n".join(lines[:MAX_ASSISTANT_RECENT_CONTEXT_MESSAGES])

    def _is_explanation_request(self, message: str) -> bool:
        text = _normalize_free_text(message)
        explanation_tokens = (
            "не понял",
            "не понимаю",
            "объясни",
            "объяснить",
            "помоги понять",
            "простыми словами",
            "разбери",
            "түсіндір",
            "тусиндир",
            "қарапайым",
            "тусінбедім",
            "тусінбедим",
            "understand",
            "explain",
            "simpler",
            "simple words",
        )
        return any(token in text for token in explanation_tokens)

    def _should_search_pdf_knowledge(self, message: str) -> bool:
        text = _normalize_free_text(message)
        if not text:
            return False
        has_specific_year = bool(re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", text))
        if self._is_explanation_request(message) and not has_specific_year:
            return False
        history_tokens = (
            "истор",
            "тарих",
            "history",
            "xix",
            "19 век",
            "19 ғасыр",
            "реформа",
            "хан",
            "khan",
            "revolution",
            "war",
            "соғыс",
            "войн",
        )
        return has_specific_year or any(token in text for token in history_tokens)

    def _search_pdf_knowledge_with_timeout(self, message: str, max_results: int = 3) -> list[dict]:
        if not self._should_search_pdf_knowledge(message):
            return []
        future = _PDF_SEARCH_EXECUTOR.submit(search_pdf_knowledge, message, max_results)
        try:
            result = future.result(timeout=1.2)
        except FuturesTimeoutError:
            logger.info("PDF knowledge search timed out; skipping for this turn.")
            return []
        except Exception as exc:
            logger.warning("PDF search error: %s", exc)
            return []
        return result if isinstance(result, list) else []

    def _compact_recent_errors(self, recent_errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in recent_errors[:4]:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "topic": self._clean_memory_fragment(item.get("topic") or item.get("action"), limit=80),
                    "message": self._clean_memory_fragment(item.get("message"), limit=160),
                    "percent": item.get("percent"),
                }
            )
        return compact

    def _build_quiz_assistant_context(
        self,
        *,
        message: str,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
        user_profile: Optional[dict],
        page_context: Optional[dict],
        assistant_summary: Optional[dict],
        lang: Optional[str],
        requested_language: Optional[str],
        source_title: str,
        source_type: str,
        quiz_type: str,
        count: int,
    ) -> dict[str, Any]:
        student_profile_snapshot = self._build_student_profile_snapshot(
            user_id=user_id,
            access_token=access_token,
            user_profile=user_profile,
            experience_summary=None,
        )
        chat_memory_signals = self._collect_chat_memory_signals(
            message=message or source_title,
            lang=lang,
            user_profile=user_profile if isinstance(user_profile, dict) else {},
        )
        student_profile_snapshot = self._merge_chat_memory_into_snapshot(
            student_profile_snapshot,
            chat_memory_signals,
        )
        assistant_summary_map = assistant_summary if isinstance(assistant_summary, dict) else {}
        summary_user_state = assistant_summary_map.get("userState") if isinstance(assistant_summary_map.get("userState"), dict) else {}
        summary_weak_topics = _merge_text_lists(
            assistant_summary_map.get("weakTopics") or [],
            summary_user_state.get("weak_topics") or [],
            limit=5,
        )
        summary_learning_goals = _merge_text_lists(
            assistant_summary_map.get("learningGoals") or [],
            summary_user_state.get("learning_goals") or [],
            limit=4,
        )
        if summary_user_state.get("preferred_language"):
            student_profile_snapshot["preferred_language"] = str(summary_user_state.get("preferred_language") or "").strip()
        if summary_user_state.get("preferred_difficulty"):
            student_profile_snapshot["preferred_difficulty"] = str(summary_user_state.get("preferred_difficulty") or "").strip()
        if summary_user_state.get("response_style"):
            student_profile_snapshot["response_style"] = str(summary_user_state.get("response_style") or "").strip()
        normalized_requested_language = self._normalize_lang(requested_language or lang)
        if normalized_requested_language:
            student_profile_snapshot["preferred_language"] = normalized_requested_language
        student_profile_snapshot["weak_topics"] = _merge_text_lists(
            summary_weak_topics,
            student_profile_snapshot.get("weak_topics") or [],
            limit=10,
        )
        student_profile_snapshot["learning_goals"] = _merge_text_lists(
            summary_learning_goals,
            student_profile_snapshot.get("learning_goals") or [],
            limit=8,
        )
        merged_recent_errors: list[dict[str, Any]] = []
        seen_error_keys: set[str] = set()
        for item in self._compact_recent_errors(list(assistant_summary_map.get("recentErrors") or [])) + self._compact_recent_errors(list(student_profile_snapshot.get("recent_errors") or [])):
            topic = self._clean_memory_fragment(item.get("topic"), limit=80)
            message_text = self._clean_memory_fragment(item.get("message"), limit=160)
            error_key = f"{_normalize_free_text(topic)}|{_normalize_free_text(message_text)}"
            if error_key in seen_error_keys:
                continue
            seen_error_keys.add(error_key)
            merged_recent_errors.append(
                {
                    "topic": topic,
                    "message": message_text,
                    "percent": item.get("percent"),
                }
            )
            if len(merged_recent_errors) >= 4:
                break
        student_profile_snapshot["recent_errors"] = merged_recent_errors
        session_record = self._load_session_record(
            user_id=user_id,
            access_token=access_token,
            session_id=session_id,
        )
        session_summary = str((session_record or {}).get("summary") or "").strip()
        return {
            "quiz_type": quiz_type,
            "question_count": count,
            "source_title": source_title,
            "source_type": source_type,
            "preferred_language": student_profile_snapshot.get("preferred_language") or normalized_requested_language,
            "preferred_difficulty": student_profile_snapshot.get("preferred_difficulty") or "medium",
            "response_style": student_profile_snapshot.get("response_style") or "concise",
            "learning_goals": list(student_profile_snapshot.get("learning_goals") or [])[:4],
            "weak_topics": list(student_profile_snapshot.get("weak_topics") or [])[:5],
            "strong_topics": list(student_profile_snapshot.get("strong_topics") or [])[:4],
            "recent_errors": self._compact_recent_errors(list(student_profile_snapshot.get("recent_errors") or [])),
            "facts": list(student_profile_snapshot.get("facts") or [])[:6],
            "recent_routes": list(student_profile_snapshot.get("recent_routes") or [])[:4],
            "quiz_performance": student_profile_snapshot.get("quiz_performance") or {},
            "session_summary": session_summary,
            "page_context": page_context if isinstance(page_context, dict) else {},
        }

    def _append_session_summary_line(self, existing_summary: str, line: str) -> str:
        clean_line = self._clean_memory_fragment(line, limit=180)
        if not clean_line:
            return str(existing_summary or "").strip()
        existing_lines = self._summary_lines_from_text(existing_summary)
        merged = [clean_line]
        seen = {_normalize_free_text(clean_line)}
        for item in existing_lines:
            normalized = _normalize_free_text(item)
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(item)
            if len(merged) >= MAX_ASSISTANT_SESSION_SUMMARY_LINES:
                break
        summary = "\n".join(f"- {item}" for item in merged)
        return summary[:MAX_ASSISTANT_SESSION_SUMMARY_CHARS].strip()

    def _normalize_quiz_attempt_items(
        self,
        *,
        details: Optional[dict[str, Any]],
        fallback_topic: str,
    ) -> list[dict[str, Any]]:
        details_map = details if isinstance(details, dict) else {}
        raw_items = details_map.get("attempt_items")
        if not isinstance(raw_items, list):
            raw_items = details_map.get("question_results")
        if not isinstance(raw_items, list):
            raw_items = details_map.get("answers")
        if not isinstance(raw_items, list):
            return []

        normalized_items: list[dict[str, Any]] = []
        fallback_topic_clean = self._clean_memory_fragment(fallback_topic, limit=160)
        for idx, item in enumerate(raw_items[:60], start=1):
            if not isinstance(item, dict):
                continue
            question_text = re.sub(r"\s+", " ", str(item.get("question_text") or item.get("question") or "").strip())[:2000]
            if not question_text:
                continue
            try:
                question_index = int(item.get("question_index") or item.get("number") or idx)
            except Exception:
                question_index = idx
            selected_answer = re.sub(r"\s+", " ", str(item.get("selected_answer") or item.get("userAnswer") or "").strip())[:1000]
            correct_answer = re.sub(r"\s+", " ", str(item.get("correct_answer") or item.get("correctAnswer") or "").strip())[:1000]
            explanation = re.sub(r"\s+", " ", str(item.get("explanation") or "").strip())[:2000]
            topic_hint = self._clean_memory_fragment(
                item.get("topic_hint") or item.get("topic") or fallback_topic_clean,
                limit=160,
            )
            source_question = item.get("source_question") if isinstance(item.get("source_question"), dict) else {}
            normalized_items.append(
                {
                    "question_index": max(1, question_index),
                    "question_id": str(item.get("question_id") or item.get("id") or "").strip()[:120] or None,
                    "question_text": question_text,
                    "selected_answer": selected_answer or None,
                    "correct_answer": correct_answer or None,
                    "is_correct": bool(item.get("is_correct") if item.get("is_correct") is not None else selected_answer and correct_answer and selected_answer == correct_answer),
                    "explanation": explanation or None,
                    "topic_hint": topic_hint or None,
                    "source_question": source_question,
                }
            )
        return normalized_items

    def _build_quiz_attempt_analysis(
        self,
        *,
        normalized_items: list[dict[str, Any]],
        fallback_topic: str,
    ) -> dict[str, Any]:
        incorrect_items = [item for item in normalized_items if not bool(item.get("is_correct"))]
        skipped_count = 0
        topic_counts: dict[str, int] = {}
        mistake_examples: list[dict[str, Any]] = []
        fallback_topic_clean = self._clean_memory_fragment(fallback_topic, limit=160)

        for item in normalized_items:
            selected_answer = str(item.get("selected_answer") or "").strip()
            if not selected_answer:
                skipped_count += 1

        for item in incorrect_items:
            topic_hint = self._clean_memory_fragment(
                item.get("topic_hint") or fallback_topic_clean,
                limit=160,
            )
            if topic_hint:
                topic_counts[topic_hint] = topic_counts.get(topic_hint, 0) + 1
            if len(mistake_examples) < 3:
                mistake_examples.append(
                    {
                        "question": self._clean_memory_fragment(item.get("question_text"), limit=220),
                        "topic": topic_hint or fallback_topic_clean,
                        "student_answer": self._clean_memory_fragment(item.get("selected_answer"), limit=160),
                        "correct_answer": self._clean_memory_fragment(item.get("correct_answer"), limit=160),
                    }
                )

        focus_topics = [
            topic
            for topic, _count in sorted(
                topic_counts.items(),
                key=lambda pair: (-pair[1], pair[0]),
            )
        ][:4]

        return {
            "focus_topics": focus_topics,
            "mistake_count": len(incorrect_items),
            "skipped_count": skipped_count,
            "mistake_examples": mistake_examples,
        }

    def _persist_quiz_attempt(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
        assistant_event_id: Optional[str],
        event_type: str,
        route: str,
        quiz_mode: str,
        topic: str,
        source_type: str,
        source_id: str,
        language: str,
        page_context: Optional[dict[str, Any]],
        details: Optional[dict[str, Any]],
        payload: Optional[dict[str, Any]],
        correct: Optional[int],
        total: Optional[int],
        percent: Optional[int],
        assistant_origin: bool = False,
    ) -> Optional[str]:
        if not user_id or not self.supabase.available:
            return None

        normalized_mode = str(quiz_mode or "").strip().lower()
        if normalized_mode not in {"practice", "realtest"}:
            normalized_mode = "practice"

        normalized_items = self._normalize_quiz_attempt_items(
            details=details,
            fallback_topic=topic,
        )
        analysis = self._build_quiz_attempt_analysis(
            normalized_items=normalized_items,
            fallback_topic=topic,
        )
        total_questions = total if total is not None else len(normalized_items)
        correct_answers = correct
        if correct_answers is None and normalized_items:
            correct_answers = sum(1 for item in normalized_items if item.get("is_correct"))
        source_title = self._clean_memory_fragment(
            (payload or {}).get("source_title") or topic,
            limit=180,
        )
        attempt_row = {
            "user_id": user_id,
            "session_id": session_id if _looks_like_uuid(str(session_id or "")) else None,
            "assistant_event_id": assistant_event_id if _looks_like_uuid(str(assistant_event_id or "")) else None,
            "mode": normalized_mode,
            "route": route or None,
            "topic": self._clean_memory_fragment(topic, limit=180) or None,
            "source_type": source_type or None,
            "source_id": source_id or None,
            "source_title": source_title or None,
            "correct": int(correct_answers or 0),
            "total": int(total_questions or 0),
            "percent": percent,
            "language": self._normalize_lang(language) if language else None,
            "page_context": page_context if isinstance(page_context, dict) else {},
            "metadata": {
                "event_type": event_type,
                "learning_goals": list((payload or {}).get("learning_goals") or [])[:4] if isinstance((payload or {}).get("learning_goals"), list) else [],
                "assistant_origin": bool(assistant_origin),
                "item_count": len(normalized_items),
                "source_action": str((payload or {}).get("action") or "").strip(),
                "focus_topics": list(analysis.get("focus_topics") or [])[:4],
                "mistake_count": int(analysis.get("mistake_count") or 0),
                "skipped_count": int(analysis.get("skipped_count") or 0),
                "mistake_examples": list(analysis.get("mistake_examples") or [])[:3],
            },
        }

        try:
            inserted_attempts = self.supabase.insert(
                "assistant_quiz_attempts",
                attempt_row,
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError as exc:
            logger.warning("assistant_quiz_attempts insert failed: %s", exc)
            return None

        attempt_id = ""
        if inserted_attempts and isinstance(inserted_attempts[0], dict):
            attempt_id = str(inserted_attempts[0].get("id") or "").strip()
        if not attempt_id or not normalized_items:
            return attempt_id or None

        item_rows: list[dict[str, Any]] = []
        for item in normalized_items:
            item_rows.append(
                {
                    "attempt_id": attempt_id,
                    "user_id": user_id,
                    "question_index": item.get("question_index"),
                    "question_id": item.get("question_id"),
                    "question_text": item.get("question_text"),
                    "selected_answer": item.get("selected_answer"),
                    "correct_answer": item.get("correct_answer"),
                    "is_correct": bool(item.get("is_correct")),
                    "explanation": item.get("explanation"),
                    "topic_hint": item.get("topic_hint"),
                    "source_question": item.get("source_question") if isinstance(item.get("source_question"), dict) else {},
                }
            )

        try:
            self.supabase.insert(
                "assistant_quiz_attempt_items",
                item_rows,
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError as exc:
            logger.warning("assistant_quiz_attempt_items insert failed: %s", exc)
        return attempt_id or None

    def _sync_user_stats_from_quiz_attempts(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
    ) -> None:
        if not user_id or not self.supabase.available:
            return

        token = access_token if access_token else None
        use_service_role = not bool(token)

        try:
            attempt_rows = self.supabase.select(
                "assistant_quiz_attempts",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "percent,created_at",
                    "order": "created_at.desc",
                    "limit": "500",
                },
                auth_token=token,
                use_service_role=use_service_role,
            )
        except SupabaseServiceError as exc:
            logger.warning("assistant_quiz_attempts stats sync read failed: %s", exc)
            return

        total_completed = 0
        percent_values: list[float] = []
        latest_attempt_at = ""

        for row in attempt_rows:
            if not isinstance(row, dict):
                continue
            total_completed += 1
            if not latest_attempt_at:
                latest_attempt_at = str(row.get("created_at") or "").strip()
            try:
                if row.get("percent") is not None:
                    percent_values.append(float(row.get("percent")))
            except Exception:
                continue

        average_score = round(sum(percent_values) / len(percent_values), 2) if percent_values else 0.0

        existing_total_tests = 0
        existing_total_completed = 0
        existing_average_score = 0.0
        existing_last_test_date = ""
        try:
            existing_rows = self.supabase.select(
                "user_stats",
                params={
                    "user_id": f"eq.{user_id}",
                    "select": "total_tests,total_tests_completed,average_score,last_test_date",
                    "limit": "1",
                },
                auth_token=token,
                use_service_role=use_service_role,
            )
            if existing_rows and isinstance(existing_rows[0], dict):
                existing_row = existing_rows[0]
                try:
                    existing_total_tests = int(float(existing_row.get("total_tests") or 0))
                except Exception:
                    existing_total_tests = 0
                try:
                    existing_total_completed = int(float(existing_row.get("total_tests_completed") or 0))
                except Exception:
                    existing_total_completed = 0
                try:
                    existing_average_score = float(existing_row.get("average_score") or 0)
                except Exception:
                    existing_average_score = 0.0
                existing_last_test_date = str(existing_row.get("last_test_date") or "").strip()
        except SupabaseServiceError as exc:
            logger.warning("user_stats sync read failed: %s", exc)

        effective_total_completed = max(existing_total_completed, total_completed)
        effective_average_score = average_score
        if total_completed < existing_total_completed and existing_average_score > 0:
            effective_average_score = existing_average_score
        effective_last_test_date = max(
            existing_last_test_date,
            latest_attempt_at,
        )

        payload: dict[str, Any] = {
            "user_id": user_id,
            "total_tests_completed": effective_total_completed,
            "average_score": effective_average_score,
            "last_test_date": effective_last_test_date or None,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        if existing_total_tests < total_completed:
            payload["total_tests"] = total_completed

        try:
            self.supabase.upsert(
                "user_stats",
                payload,
                on_conflict="user_id",
                auth_token=token,
                use_service_role=use_service_role,
            )
        except SupabaseServiceError as exc:
            logger.warning("user_stats sync write failed: %s", exc)

    def _remember_quiz_result(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
        topic: str,
        percent: Optional[int],
        mode: str = "",
        event_id: Optional[str] = None,
    ) -> None:
        if not user_id or percent is None:
            return
        clean_topic = self._clean_memory_fragment(topic, limit=100)
        score_label = f"{percent}%"
        self._upsert_user_fact(
            user_id=user_id,
            access_token=access_token,
            fact_key="last_quiz_score",
            fact_value=score_label,
            confidence=0.9,
            source_event_id=event_id,
            source_session_id=session_id,
        )
        if clean_topic:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="last_quiz_topic",
                fact_value=clean_topic,
                confidence=0.86,
                source_event_id=event_id,
                source_session_id=session_id,
            )
        clean_mode = str(mode or "").strip().lower()
        if clean_mode in {"practice", "realtest"}:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="last_quiz_mode",
                fact_value=clean_mode,
                confidence=0.84,
                source_event_id=event_id,
                source_session_id=session_id,
            )

    def _extract_ent_trap_points(
        self,
        *,
        knowledge_matches: list[dict],
        student_profile_snapshot: dict[str, Any],
    ) -> list[str]:
        weak_topics = [str(item).strip() for item in (student_profile_snapshot.get("weak_topics") or []) if str(item).strip()]
        traps: list[str] = []
        if weak_topics:
            traps.append(f"Weak-topic traps: {', '.join(weak_topics[:3])}")

        years: list[str] = []
        for item in knowledge_matches[:6]:
            text = str(item.get("excerpt") or item.get("material_text") or "")
            years.extend(re.findall(r"\b(1[0-9]{3}|20[0-9]{2})\b", text))
        if years:
            top_years = ", ".join(sorted(set(years))[:5])
            traps.append(f"Confusable historical years to double-check: {top_years}")

        traps.append("Common ENT trap: similar person names/titles in Kazakhstan history and law contexts.")
        return traps[:4]

    def _prepare_assistant_messages(
        self,
        *,
        chat_history: Optional[list[dict]],
        message: str,
        lang: Optional[str],
        persisted_session_summary: str = "",
    ) -> dict[str, Any]:
        normalized = self._history_messages(chat_history)
        prior_messages = self._history_without_current_message(normalized, message)
        short_term_messages = prior_messages[-MAX_ASSISTANT_SHORT_TERM_MESSAGES:]
        older_messages = prior_messages[:-MAX_ASSISTANT_SHORT_TERM_MESSAGES]
        current_domain = self._infer_domain(message)

        previous_domain = "general"
        for item in reversed(short_term_messages):
            if item.get("role") != "user":
                continue
            previous_domain = self._infer_domain(str(item.get("content") or ""))
            break
        domain_shift = previous_domain != current_domain and previous_domain != "general"

        incremental_summary = str(persisted_session_summary or "").strip()
        if not incremental_summary and older_messages:
            incremental_summary = self._build_recent_history_summary(older_messages[-MAX_ASSISTANT_RECENT_CONTEXT_MESSAGES:])

        return {
            "short_term_messages": short_term_messages,
            "incremental_summary": incremental_summary,
            "current_domain": current_domain,
            "domain_shift": domain_shift,
            "trap_instructions": (
                "Identify ENT Trap Points proactively: confusable dates, similar historical names/titles, "
                "and legal-term mixups in Kazakhstan history/law."
            ),
        }

    def _model_dump(self, value: BaseModel) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        return value.dict()

    def _infer_domain(self, text: str) -> str:
        value = _normalize_free_text(text)
        history_tokens = (
            "histor",
            "history",
            "истор",
            "тарих",
            "tarikh",
            "хан",
            "khan",
            "empire",
            "revolution",
            "xix",
            "казах",
        )
        has_history_period = bool(re.search(r"\b(?:1[0-9]|20)\s*(?:век\w*|ғасыр\w*)\b", value))
        if any(token in value for token in history_tokens) or has_history_period:
            return "history"
        if any(token in value for token in ("math", "algebra", "geometr", "equation", "integral")):
            return "math"
        if any(token in value for token in ("law", "constitution", "quqyq", "pravo", "закон", "құқық", "кукык")):
            return "law"
        return "general"

    def _smalltalk_intent(self, message: str) -> str:
        text = _normalize_free_text(message)
        text = text.strip(" .,!?:;")
        if not text:
            return ""

        greeting_tokens = (
            "привет",
            "здравствуй",
            "здравствуйте",
            "сәлем",
            "салам",
            "hello",
            "hi",
            "hey",
        )
        if text in greeting_tokens or any(text.startswith(f"{token} ") for token in greeting_tokens):
            return "greeting"

        identity_tokens = (
            "кто ты",
            "ты кто",
            "кто ты такой",
            "что ты умеешь",
            "что ты можешь",
            "who are you",
            "what are you",
            "what can you do",
            "сен кімсің",
            "сен не істей аласың",
        )
        if any(token in text for token in identity_tokens):
            return "identity"

        thanks_tokens = (
            "спасибо",
            "thanks",
            "thank you",
            "рахмет",
            "рақмет",
        )
        if text in thanks_tokens or any(text.startswith(f"{token} ") for token in thanks_tokens):
            return "thanks"

        return ""

    def _build_smalltalk_response(self, message: str, lang: Optional[str]) -> Optional[dict[str, Any]]:
        intent = self._smalltalk_intent(message)
        if not intent:
            return None
        lang = self._normalize_lang(lang)

        if intent == "greeting":
            text = self._assistant_localized(
                lang,
                "Сәлем! Мен оқу ассистентімін: тақырыпты түсіндіремін, материалдарыңнан жауап табамын, соңғы тесттеріңді талдаймын және practice не real test бастай аламын.",
                "Привет! Я учебный ассистент: могу объяснить тему, ответить по твоим материалам, разобрать последние тесты и запустить practice или real test.",
                "Hi! I am your study assistant. I can explain topics, answer from your materials, review recent tests, and start practice or real tests.",
            )
        elif intent == "identity":
            text = self._assistant_localized(
                lang,
                "Мен ЕНТ дайындығына арналған толық көмекші ассистентпін. Мен материалдарыңды, диалог контекстін және тест нәтижелеріңді ескеріп, түсіндіру, оқу жоспары, тест талдауы және жаңа quiz бере аламын.",
                "Я полноценный ассистент для подготовки к ЕНТ. Я учитываю материалы, контекст диалога и результаты тестов и могу объяснять тему, строить план, разбирать ошибки и запускать новые квизы.",
                "I am a full ENT prep assistant. I use your materials, conversation context, and test results to explain topics, build study plans, review mistakes, and launch new quizzes.",
            )
        else:
            text = self._assistant_localized(
                lang,
                "Әрқашан көмектесемін. Қаласаң, қазір бірге келесі оқу қадамын таңдайық.",
                "Всегда пожалуйста. Если хочешь, можем сразу выбрать следующий учебный шаг.",
                "Anytime. If you want, we can pick the next study step right now.",
            )

        return {
            "message": text,
            "reasoning": "Deterministic smalltalk response",
            "intent": "answer",
            "action_buttons": [],
            "plan_steps": [],
            "citations": [],
        }

    def _is_last_quiz_lookup_request(self, message: str) -> bool:
        text = _normalize_free_text(message)
        if not text:
            return False

        test_tokens = ("тест", "квиз", "quiz", "test", "сынақ")
        last_tokens = ("последн", "latest", "recent", "last", "соңғы")
        review_tokens = (
            "посмотри",
            "покажи",
            "скажи",
            "результ",
            "итог",
            "score",
            "result",
            "пройден",
            "прошел",
            "прошёл",
            "completed",
            "finished",
            "тапсырған",
            "өткен",
        )
        return (
            any(token in text for token in test_tokens)
            and any(token in text for token in last_tokens)
            and any(token in text for token in review_tokens)
        )

    def _format_assistant_timestamp(self, raw_value: Optional[str]) -> str:
        raw = str(raw_value or "").strip()
        if not raw:
            return ""
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return raw.replace("T", " ")[:16]

    def _load_latest_quiz_attempt(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not user_id or not self.supabase.available:
            return None

        try:
            rows = self.supabase.select(
                "assistant_quiz_attempts",
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": "1",
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError:
            rows = []

        if rows and isinstance(rows[0], dict):
            row = rows[0]
            attempt_id = str(row.get("id") or "").strip()
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            items: list[dict[str, Any]] = []
            if attempt_id:
                try:
                    items = self.supabase.select(
                        "assistant_quiz_attempt_items",
                        params={
                            "attempt_id": f"eq.{attempt_id}",
                            "user_id": f"eq.{user_id}",
                            "order": "question_index.asc",
                            "limit": "60",
                        },
                        auth_token=access_token,
                        use_service_role=not bool(access_token),
                    )
                except SupabaseServiceError:
                    items = []

            incorrect_topics: list[str] = []
            incorrect_count = 0
            skipped_count = 0
            mistake_examples: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not str(item.get("selected_answer") or "").strip():
                    skipped_count += 1
                if bool(item.get("is_correct")):
                    continue
                incorrect_count += 1
                topic_hint = self._clean_memory_fragment(item.get("topic_hint"), limit=100)
                if topic_hint and topic_hint not in incorrect_topics:
                    incorrect_topics.append(topic_hint)
                if len(mistake_examples) < 3:
                    mistake_examples.append(
                        {
                            "topic": topic_hint or self._clean_memory_fragment(row.get("source_title") or row.get("topic"), limit=100),
                            "question": self._clean_memory_fragment(item.get("question_text"), limit=220),
                            "student_answer": self._clean_memory_fragment(item.get("selected_answer"), limit=160),
                            "correct_answer": self._clean_memory_fragment(item.get("correct_answer"), limit=160),
                        }
                    )

            if not incorrect_topics:
                incorrect_topics = [
                    str(item).strip()
                    for item in (metadata.get("focus_topics") or [])
                    if str(item).strip()
                ][:3]
            if not mistake_examples:
                for item in (metadata.get("mistake_examples") or [])[:3]:
                    if not isinstance(item, dict):
                        continue
                    mistake_examples.append(
                        {
                            "topic": self._clean_memory_fragment(item.get("topic"), limit=100),
                            "question": self._clean_memory_fragment(item.get("question"), limit=220),
                            "student_answer": self._clean_memory_fragment(item.get("student_answer"), limit=160),
                            "correct_answer": self._clean_memory_fragment(item.get("correct_answer"), limit=160),
                        }
                    )

            return {
                "topic": self._clean_memory_fragment(
                    row.get("source_title") or row.get("topic") or row.get("source_id"),
                    limit=140,
                ),
                "source_id": str(row.get("source_id") or "").strip(),
                "source_type": str(row.get("source_type") or "").strip(),
                "source_title": self._clean_memory_fragment(row.get("source_title") or row.get("topic"), limit=140),
                "mode": str(row.get("mode") or "practice").strip().lower() or "practice",
                "percent": row.get("percent"),
                "correct": row.get("correct"),
                "total": row.get("total"),
                "created_at": row.get("created_at"),
                "incorrect_topics": incorrect_topics[:3],
                "incorrect_count": incorrect_count,
                "skipped_count": skipped_count or int(metadata.get("skipped_count") or 0),
                "mistake_examples": mistake_examples[:3],
                "language": str(row.get("language") or "").strip(),
                "assistant_origin": bool(metadata.get("assistant_origin")),
            }

        try:
            rows = self.supabase.select(
                "assistant_events",
                params={
                    "user_id": f"eq.{user_id}",
                    "event_type": "in.(quiz_result,assistant_quiz_result)",
                    "order": "created_at.desc",
                    "limit": "1",
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError:
            rows = []

        if not rows or not isinstance(rows[0], dict):
            return None

        row = rows[0]
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        mistakes = details.get("mistakes") if isinstance(details.get("mistakes"), list) else []
        attempt_items = details.get("attempt_items") if isinstance(details.get("attempt_items"), list) else []
        incorrect_topics: list[str] = []
        mistake_examples: list[dict[str, Any]] = []
        for mistake in mistakes:
            if not isinstance(mistake, dict):
                continue
            topic_hint = self._clean_memory_fragment(
                mistake.get("topic") or row.get("topic"),
                limit=100,
            )
            if topic_hint and topic_hint not in incorrect_topics:
                incorrect_topics.append(topic_hint)
            if len(mistake_examples) < 3:
                mistake_examples.append(
                    {
                        "topic": topic_hint,
                        "question": self._clean_memory_fragment(mistake.get("question"), limit=220),
                        "student_answer": self._clean_memory_fragment(mistake.get("userAnswer"), limit=160),
                        "correct_answer": self._clean_memory_fragment(mistake.get("correctAnswer"), limit=160),
                    }
                )

        if not incorrect_topics:
            incorrect_topics = [
                str(item).strip()
                for item in (metadata.get("focus_topics") or [])
                if str(item).strip()
            ][:3]
        if not mistake_examples:
            for item in (metadata.get("mistake_examples") or [])[:3]:
                if not isinstance(item, dict):
                    continue
                mistake_examples.append(
                    {
                        "topic": self._clean_memory_fragment(item.get("topic"), limit=100),
                        "question": self._clean_memory_fragment(item.get("question"), limit=220),
                        "student_answer": self._clean_memory_fragment(item.get("student_answer"), limit=160),
                        "correct_answer": self._clean_memory_fragment(item.get("correct_answer"), limit=160),
                    }
                )

        action_name = str(row.get("action") or row.get("event_name") or "").strip().lower()
        skipped_count = 0
        for item in attempt_items:
            if not isinstance(item, dict):
                continue
            if not str(item.get("selected_answer") or item.get("userAnswer") or "").strip():
                skipped_count += 1
        return {
            "topic": self._clean_memory_fragment(
                row.get("topic") or metadata.get("source_title") or row.get("message"),
                limit=140,
            ),
            "source_id": str(row.get("source_id") or metadata.get("source_id") or "").strip(),
            "source_type": str(row.get("source_type") or metadata.get("source_type") or "").strip(),
            "source_title": self._clean_memory_fragment(metadata.get("source_title") or row.get("topic"), limit=140),
            "mode": str(metadata.get("mode") or "practice").strip().lower() or "practice",
            "percent": row.get("percent"),
            "correct": row.get("correct"),
            "total": row.get("total"),
            "created_at": row.get("created_at"),
            "incorrect_topics": incorrect_topics[:3],
            "incorrect_count": len(mistakes),
            "skipped_count": skipped_count,
            "mistake_examples": mistake_examples[:3],
            "language": str(metadata.get("language") or "").strip(),
            "assistant_origin": bool(metadata.get("assistant_origin")) or action_name == "assistant_quiz_result",
        }

    def _build_latest_quiz_recommendation_text(
        self,
        *,
        lang: Optional[str],
        percent: Optional[int],
        incorrect_topics: list[str],
    ) -> str:
        topics_text = ", ".join(incorrect_topics[:3])
        topics_hint = ""
        if topics_text:
            topics_hint = self._assistant_localized(
                lang,
                f"Негізгі назар аударатын тақырыптар: {topics_text}.",
                f"Главные темы для повторения: {topics_text}.",
                f"Main topics to review: {topics_text}.",
            )

        if percent is None:
            base_text = self._assistant_localized(
                lang,
                "Ұсыныс: қысқа practice-тест өтіп, жаңа статистика жинап алыңыз.",
                "Рекомендация: пройдите короткий practice-тест, чтобы обновить статистику.",
                "Recommendation: take a short practice test to refresh your stats.",
            )
        elif percent >= 85:
            base_text = self._assistant_localized(
                lang,
                "Ұсыныс: нәтиже өте жақсы. Қалған қателерді бекітіп, real test көріңіз.",
                "Рекомендация: результат сильный. Закрепите оставшиеся ошибки и попробуйте real test.",
                "Recommendation: strong result. Review the remaining mistakes and try a real test.",
            )
        elif percent >= 60:
            base_text = self._assistant_localized(
                lang,
                "Ұсыныс: нәтиже жаман емес. Қателерді талдап, 10-15 сұрақтық practice-тестті қайта өтіңіз.",
                "Рекомендация: результат неплохой. Разберите ошибки и пройдите ещё один practice-тест на 10-15 вопросов.",
                "Recommendation: solid result. Review the mistakes and take another 10-15 question practice test.",
            )
        else:
            base_text = self._assistant_localized(
                lang,
                "Ұсыныс: алдымен теорияны қайталап, әлсіз тұстарды қарап шығыңыз, содан кейін қысқа practice-тест өтіңіз.",
                "Рекомендация: сначала повторите теорию и слабые темы, затем пройдите короткий practice-тест.",
                "Recommendation: review the theory and weak topics first, then take a short practice test.",
            )

        return " ".join(part for part in [base_text, topics_hint] if part).strip()

    def _build_quiz_coaching_snapshot(
        self,
        *,
        lang: Optional[str],
        latest_attempt: Optional[dict[str, Any]],
        quiz_performance: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        latest = latest_attempt if isinstance(latest_attempt, dict) else {}
        performance = quiz_performance if isinstance(quiz_performance, dict) else {}

        focus_topics = _merge_text_lists(
            latest.get("incorrect_topics") or [],
            performance.get("weak_topics") or [],
            limit=4,
        )
        strong_topics = _merge_text_lists(
            performance.get("strong_topics") or [],
            limit=3,
        )

        try:
            percent = int(float(latest.get("percent"))) if latest.get("percent") is not None else None
        except Exception:
            percent = None

        if percent is None:
            recommended_route = "ai_practice"
        elif percent < 60:
            recommended_route = "ai_learn"
        elif percent < 85:
            recommended_route = "ai_practice"
        else:
            recommended_route = "ai_realtest"

        study_plan: list[str] = []
        if focus_topics:
            primary_topic = focus_topics[0]
            study_plan.append(
                self._assistant_localized(
                    lang,
                    f"{primary_topic} тақырыбы бойынша Learn арқылы теорияны қайталау.",
                    f"Повторить теорию по теме «{primary_topic}» через Learn.",
                    f"Review the theory for '{primary_topic}' in Learn.",
                )
            )
            study_plan.append(
                self._assistant_localized(
                    lang,
                    f"{primary_topic} бойынша 10-15 сұрақтық practice-тест орындау.",
                    f"Решить practice-тест на 10-15 вопросов по теме «{primary_topic}».",
                    f"Take a 10-15 question practice quiz on '{primary_topic}'.",
                )
            )
        else:
            study_plan.append(
                self._assistant_localized(
                    lang,
                    "Қысқа practice-тест өтіп, әлсіз тақырыптарды жаңартыңыз.",
                    "Пройдите короткий practice-тест, чтобы обновить слабые темы.",
                    "Take a short practice quiz to refresh weak topics.",
                )
            )

        if percent is not None and percent >= 85:
            study_plan.append(
                self._assistant_localized(
                    lang,
                    "Нәтиже тұрақты болса, real test режиміне өтіңіз.",
                    "Если результат держится стабильно, переходите в real test.",
                    "If the score stays stable, move on to real test.",
                )
            )
        else:
            study_plan.append(
                self._assistant_localized(
                    lang,
                    "Қайта тестілеуден кейін қателерді қысқаша қорытындылап шығыңыз.",
                    "После повторного теста коротко зафиксируйте причины ошибок.",
                    "After the retest, briefly note why each mistake happened.",
                )
            )

        return {
            "latest_topic": self._clean_memory_fragment(latest.get("topic"), limit=140),
            "latest_percent": percent,
            "focus_topics": focus_topics,
            "strong_topics": strong_topics,
            "mistake_examples": list(latest.get("mistake_examples") or [])[:3],
            "skipped_count": int(latest.get("skipped_count") or 0),
            "recommended_route": recommended_route,
            "study_plan": study_plan[:4],
        }

    def _build_supabase_lookup_response(
        self,
        *,
        message: str,
        lang: Optional[str],
        user_id: str,
        access_token: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not self._is_last_quiz_lookup_request(message):
            return None

        if not user_id or not self.supabase.available:
            payload = TutorResponse(
                reasoning="Deterministic Supabase lookup could not run due to missing auth or configuration.",
                message=self._assistant_localized(
                    lang,
                    "Соңғы тестті тексеру үшін аккаунт пен дерекқорға қолжетімділік керек.",
                    "Чтобы посмотреть последний тест, мне нужен доступ к аккаунту и Supabase.",
                    "I need account and Supabase access to inspect your last test.",
                ),
                intent="answer",
                action_buttons=[],
                citations=[],
                plan_steps=None,
            )
            return self._attach_internal_meta(
                self._build_final_json(payload, lang),
                model_used="deterministic-supabase",
            )

        latest_attempt = self._load_latest_quiz_attempt(
            user_id=user_id,
            access_token=access_token,
        )
        if not latest_attempt:
            payload = TutorResponse(
                reasoning="Deterministic Supabase lookup found no quiz history.",
                message=self._assistant_localized(
                    lang,
                    "Сіз бұл сайтта әлі тест тапсырмағансыз.",
                    "Вы до этого не проходили тесты на этом сайте.",
                    "You have not completed any tests on this site yet.",
                ),
                intent="answer",
                action_buttons=[],
                citations=[],
                plan_steps=None,
            )
            return self._attach_internal_meta(
                self._build_final_json(payload, lang),
                model_used="deterministic-supabase",
            )

        topic = str(latest_attempt.get("topic") or "").strip() or self._assistant_localized(
            lang,
            "Атаусыз тест",
            "Тест без названия",
            "Untitled test",
        )
        mode = str(latest_attempt.get("mode") or "practice").strip().lower()
        mode_label = self._assistant_localized(
            lang,
            "practice тест",
            "practice-тест",
            "practice test",
        )
        if mode == "realtest":
            mode_label = self._assistant_localized(
                lang,
                "real test",
                "real test",
                "real test",
            )

        try:
            percent = int(float(latest_attempt.get("percent"))) if latest_attempt.get("percent") is not None else None
        except Exception:
            percent = None
        try:
            correct = int(float(latest_attempt.get("correct"))) if latest_attempt.get("correct") is not None else None
        except Exception:
            correct = None
        try:
            total = int(float(latest_attempt.get("total"))) if latest_attempt.get("total") is not None else None
        except Exception:
            total = None

        score_text = ""
        if correct is not None and total is not None and total > 0:
            score_text = f"{correct}/{total}"
            if percent is not None:
                score_text = f"{score_text} ({percent}%)"
        elif percent is not None:
            score_text = f"{percent}%"

        completed_at = self._format_assistant_timestamp(latest_attempt.get("created_at"))
        incorrect_topics = [
            str(item).strip()
            for item in (latest_attempt.get("incorrect_topics") or [])
            if str(item).strip()
        ]

        lines = [
            self._assistant_localized(
                lang,
                f"Сіздің соңғы тестіңіз: {topic} ({mode_label}).",
                f"Ваш последний тест: {topic} ({mode_label}).",
                f"Your last test: {topic} ({mode_label}).",
            )
        ]
        if score_text:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Нәтиже: {score_text}.",
                    f"Результат: {score_text}.",
                    f"Result: {score_text}.",
                )
            )
        if completed_at:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Өткен уақыты: {completed_at}.",
                    f"Пройден: {completed_at}.",
                    f"Completed: {completed_at}.",
                )
            )
        if latest_attempt.get("assistant_origin"):
            lines.append(
                self._assistant_localized(
                    lang,
                    "Бұл тестті ассистент ұсынған.",
                    "Этот тест был предложен ассистентом.",
                    "This test was suggested by the assistant.",
                )
            )
        if incorrect_topics:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Қателер көбірек кездескен тақырыптар: {', '.join(incorrect_topics[:3])}.",
                    f"Больше всего ошибок было по темам: {', '.join(incorrect_topics[:3])}.",
                    f"Most mistakes were in: {', '.join(incorrect_topics[:3])}.",
                )
            )
        lines.append(
            self._build_latest_quiz_recommendation_text(
                lang=lang,
                percent=percent,
                incorrect_topics=incorrect_topics,
            )
        )

        payload = TutorResponse(
            reasoning="Deterministic Supabase lookup returned the latest quiz attempt.",
            message="\n".join(line for line in lines if line).strip(),
            intent="answer",
            action_buttons=[],
            citations=[],
            plan_steps=None,
        )
        return self._attach_internal_meta(
            self._build_final_json(payload, lang),
            model_used="deterministic-supabase",
        )

    def _is_quiz_coaching_request(self, message: str) -> bool:
        text = _normalize_free_text(message)
        if not text:
            return False

        direct_patterns = (
            "разбери ошибки",
            "проанализируй ошибки",
            "мои ошибки",
            "что повторить",
            "что пройти дальше",
            "что учить дальше",
            "какие темы пройти",
            "какие темы повторить",
            "analyze my mistakes",
            "review my mistakes",
            "what should i review",
            "what should i study next",
            "next step after quiz",
            "қателерімді талда",
            "не қайталауым керек",
            "қандай тақырыптарды қайталау",
            "келесі қадам",
            "әлсіз тақырыптарым",
        )
        if any(pattern in text for pattern in direct_patterns):
            return True

        quiz_tokens = ("quiz", "test", "practice", "real test", "тест", "квиз", "сынақ")
        coaching_tokens = ("mistake", "error", "weak", "review", "repeat", "plan", "ошиб", "слаб", "повтор", "қате", "әлсіз", "қайтала")
        return any(token in text for token in quiz_tokens) and any(token in text for token in coaching_tokens)

    def _build_quiz_coaching_response(
        self,
        *,
        message: str,
        lang: Optional[str],
        user_id: str,
        access_token: Optional[str],
    ) -> Optional[dict[str, Any]]:
        if not self._is_quiz_coaching_request(message):
            return None

        if not user_id or not self.supabase.available:
            return None

        latest_attempt = self._load_latest_quiz_attempt(
            user_id=user_id,
            access_token=access_token,
        )
        if not latest_attempt:
            return None

        quiz_performance = self._load_quiz_performance_snapshot(
            user_id=user_id,
            access_token=access_token,
            user_profile={},
        )
        coaching = self._build_quiz_coaching_snapshot(
            lang=lang,
            latest_attempt=latest_attempt,
            quiz_performance=quiz_performance,
        )

        topic = str(coaching.get("latest_topic") or latest_attempt.get("topic") or "").strip() or self._assistant_localized(
            lang,
            "соңғы тест",
            "последний тест",
            "your latest quiz",
        )
        percent = coaching.get("latest_percent")
        focus_topics = [str(item).strip() for item in (coaching.get("focus_topics") or []) if str(item).strip()]
        strong_topics = [str(item).strip() for item in (coaching.get("strong_topics") or []) if str(item).strip()]
        skipped_count = int(coaching.get("skipped_count") or 0)
        mistake_examples = [item for item in (coaching.get("mistake_examples") or []) if isinstance(item, dict)]

        lines = [
            self._assistant_localized(
                lang,
                f"{topic} бойынша терең талдау:",
                f"Глубокий разбор по теме «{topic}»:",
                f"Deep analysis for '{topic}':",
            )
        ]

        if percent is not None:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Нәтиже: {percent}%.",
                    f"Результат: {percent}%.",
                    f"Score: {percent}%.",
                )
            )
        if focus_topics:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Қайта өту керек негізгі тақырыптар: {', '.join(focus_topics[:3])}.",
                    f"Главные темы для повтора: {', '.join(focus_topics[:3])}.",
                    f"Main topics to revisit: {', '.join(focus_topics[:3])}.",
                )
            )
        if strong_topics:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Қазір салыстырмалы түрде мықты тақырыптар: {', '.join(strong_topics[:2])}.",
                    f"Сейчас относительно сильные темы: {', '.join(strong_topics[:2])}.",
                    f"Current stronger topics: {', '.join(strong_topics[:2])}.",
                )
            )
        if skipped_count > 0:
            lines.append(
                self._assistant_localized(
                    lang,
                    f"Жауап берілмеген сұрақтар саны: {skipped_count}. Бұл жерде уақытты бөлу мен сенімділікке жұмыс істеу керек.",
                    f"Вопросов без ответа: {skipped_count}. Здесь стоит отдельно поработать над темпом и уверенностью.",
                    f"Unanswered questions: {skipped_count}. This points to pacing and confidence issues.",
                )
            )
        if mistake_examples:
            example_lines = []
            for item in mistake_examples[:3]:
                topic_hint = str(item.get("topic") or "").strip()
                question = str(item.get("question") or "").strip()
                student_answer = str(item.get("student_answer") or "").strip() or self._assistant_localized(
                    lang,
                    "жауап жоқ",
                    "нет ответа",
                    "no answer",
                )
                correct_answer = str(item.get("correct_answer") or "").strip()
                example_lines.append(
                    self._assistant_localized(
                        lang,
                        f"- {topic_hint or 'Тақырып'}: {question} | сіздің жауабыңыз: {student_answer} | дұрысы: {correct_answer}",
                        f"- {topic_hint or 'Тема'}: {question} | ваш ответ: {student_answer} | правильно: {correct_answer}",
                        f"- {topic_hint or 'Topic'}: {question} | your answer: {student_answer} | correct: {correct_answer}",
                    )
                )
            if example_lines:
                lines.append(
                    self._assistant_localized(
                        lang,
                        "Қате мысалдары:\n" + "\n".join(example_lines),
                        "Примеры ошибок:\n" + "\n".join(example_lines),
                        "Mistake examples:\n" + "\n".join(example_lines),
                    )
                )

        study_plan = [str(step).strip() for step in (coaching.get("study_plan") or []) if str(step).strip()]
        if study_plan:
            numbered_plan = "\n".join(f"{idx + 1}. {step}" for idx, step in enumerate(study_plan[:4]))
            lines.append(
                self._assistant_localized(
                    lang,
                    "Келесі нақты жоспар:\n" + numbered_plan,
                    "Конкретный следующий план:\n" + numbered_plan,
                    "Concrete next plan:\n" + numbered_plan,
                )
            )

        recommended_route = str(coaching.get("recommended_route") or "ai_practice").strip() or "ai_practice"
        action_buttons: list[ActionButton] = [
            ActionButton(
                label=self._assistant_localized(lang, "Learn ашу", "Открыть Learn", "Open Learn"),
                type="navigate",
                payload=ActionButtonPayload(route="ai_learn"),
            )
        ]

        source_id = str(latest_attempt.get("source_id") or "").strip()
        source_type = str(latest_attempt.get("source_type") or "").strip()
        source_title = str(latest_attempt.get("source_title") or latest_attempt.get("topic") or "").strip()
        if source_id and source_type in {"material", "historical_figure"}:
            action_buttons.append(
                ActionButton(
                    label=self._assistant_localized(lang, "Қайталау тесті", "Тест на повтор", "Repeat quiz"),
                    type="start_quiz",
                    payload=ActionButtonPayload(
                        source_id=source_id,
                        source_type=source_type,  # type: ignore[arg-type]
                        source_title=source_title or None,
                        assistant_prompt=message.strip() or None,
                        language=self._normalize_lang(lang),
                        question_count=10 if recommended_route != "ai_realtest" else 15,
                        mode="practice" if recommended_route != "ai_realtest" else "realtest",
                    ),
                )
            )
        else:
            action_buttons.append(
                ActionButton(
                    label=self._assistant_localized(lang, "Practice ашу", "Открыть Practice", "Open Practice"),
                    type="navigate",
                    payload=ActionButtonPayload(route="ai_practice"),
                )
            )

        target_route = recommended_route if recommended_route in ASSISTANT_ROUTES else "ai_practice"
        if all((button.payload.route or "") != target_route for button in action_buttons if button.type == "navigate"):
            action_buttons.append(
                ActionButton(
                    label=self._assistant_localized(
                        lang,
                        f"{self._route_label(target_route, lang)} ашу",
                        f"Открыть {self._route_label(target_route, lang)}",
                        f"Open {self._route_label(target_route, lang)}",
                    ),
                    type="navigate",
                    payload=ActionButtonPayload(route=target_route),
                )
            )

        payload = TutorResponse(
            reasoning="Deterministic quiz coaching response based on saved attempt analysis.",
            message="\n".join(line for line in lines if line).strip(),
            intent="answer",
            action_buttons=action_buttons[:MAX_ASSISTANT_ACTIONS],
            citations=[],
            plan_steps=None,
        )
        return self._attach_internal_meta(
            self._build_final_json(payload, lang),
            model_used="deterministic-quiz-coaching",
        )

    def _generate_fast_simple_response(self, message: str, lang: Optional[str]) -> dict:
        """
        FAST PATH: Quick response for simple messages with minimal load.
        Returns JSON with strict format to prevent frontend crashes.
        """
        lang = self._normalize_lang(lang)
        
        # Minimalist system prompt for fast API
        system_prompt = f"""Кратко ответь на вопрос. Максимум 2 предложения.
{self._assistant_language_instruction(lang)}"""
        
        try:
            # Minimal load: low temp, few tokens for speed
            logger.info("⚡ FAST PATH: Sending minimal request to API")
            response = self.client.chat.completions.create(
                model=self.fast_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.3,  # Lower for consistency and speed
                max_tokens=80,  # REDUCED from 150 for speed
                timeout=15.0,
            )
            
            answer = str(response.choices[0].message.content or "").strip()
            self.last_model_used = self.fast_model
            
            return {
                "message": answer,
                "reasoning": f"Fast path {self.fast_model} response",
                "intent": "answer",
                "action_buttons": [],
                "plan_steps": [],
                "citations": [],
            }
            
        except Exception as e:
            logger.error(f"Fast path error: {str(e)[:200]}")
            # Ultimate safe fallback
            return {
                "message": self._assistant_localized(
                    lang,
                    "Сәлем! Мен дайынмын. Не талқылаймыз?",
                    "Привет! Я готов к работе. О чем хочешь поговорить?",
                    "Hi! I'm ready. What do you want to discuss?",
                ),
                "reasoning": "Safe fallback due to error",
                "intent": "answer",
                "action_buttons": [],
                "plan_steps": [],
                "citations": [],
            }
    def _is_simple_message(self, message: str) -> bool:
        """Determine if message is simple enough for fast lightweight response."""
        if not message or not isinstance(message, str):
            return False

        text = message.strip()
        if len(text) > 200:
            return False

        words = text.split()
        if len(words) > 25:
            return False

        normalized = _normalize_free_text(text)
        # Any explicit question should go through full pipeline.
        if "?" in text:
            return False

        complex_keywords = {
            "explain",
            "analyze",
            "analysis",
            "calculate",
            "plan",
            "task",
            "problem",
            "help",
            "advice",
            "history",
            "law",
            "test",
            "material",
            "resource",
            "расскажи",
            "объясни",
            "почему",
            "как",
            "что",
            "когда",
            "где",
            "кто",
            "история",
            "война",
            "закон",
            "тест",
            "материал",
            "ресурс",
            "айт",
            "тусіндір",
            "неге",
            "қалай",
            "қашан",
            "қайда",
            "кім",
            "тарих",
            "соғыс",
            "заң",
        }
        if any(keyword in normalized for keyword in complex_keywords):
            return False

        # Keep fast-path only for very short chit-chat.
        if len(words) > 8:
            return False

        return True

    def _build_long_term_summary(
        self,
        *,
        lang: Optional[str],
        history_messages: list[dict[str, str]],
        current_domain: str,
    ) -> str:
        if len(history_messages) <= MAX_ASSISTANT_SHORT_TERM_MESSAGES:
            return ""
        older = history_messages[:-MAX_ASSISTANT_SHORT_TERM_MESSAGES]
        if not older:
            return ""
        older_text = "\n".join(f"{item['role']}: {item['content']}" for item in older)
        prompt = f"""
{self._assistant_language_instruction(lang)}
Summarize old dialogue context for a tutoring assistant in 6-10 bullets.
Keep: student goals, weak topics, mistakes, unresolved asks, preferred language.
Current domain: {current_domain}. If old context is from another domain, compress it into one bullet.

Dialogue:
{older_text}

Return plain bullets only.
"""
        try:
            return self._generate_with_retry(
                prompt,
                system_prompt="You create concise memory summaries for tutoring assistants.",
                temperature=0.1,
                max_tokens=700,
            ).strip()
        except Exception:
            return ""

    def _extract_recent_errors(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        experience_summary: Optional[dict],
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        summary_errors = (experience_summary or {}).get("recent_errors") or []
        if isinstance(summary_errors, list):
            for item in summary_errors[:MAX_ASSISTANT_RECENT_ERRORS]:
                if isinstance(item, dict):
                    errors.append(item)
        if errors:
            return errors[:MAX_ASSISTANT_RECENT_ERRORS]
        if not user_id or not self.supabase.available:
            return []
        try:
            item_rows = self.supabase.select(
                "assistant_quiz_attempt_items",
                params={
                    "user_id": f"eq.{user_id}",
                    "is_correct": "eq.false",
                    "order": "created_at.desc,question_index.asc",
                    "limit": str(MAX_ASSISTANT_RECENT_ERRORS),
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
            for row in item_rows:
                if not isinstance(row, dict):
                    continue
                errors.append(
                    {
                        "topic": row.get("topic_hint") or "",
                        "message": row.get("question_text") or "",
                        "student_answer": row.get("selected_answer") or "",
                        "correct_answer": row.get("correct_answer") or "",
                        "percent": None,
                        "created_at": row.get("created_at"),
                    }
                )
                if len(errors) >= MAX_ASSISTANT_RECENT_ERRORS:
                    return errors[:MAX_ASSISTANT_RECENT_ERRORS]
            if errors:
                return errors[:MAX_ASSISTANT_RECENT_ERRORS]
        except SupabaseServiceError:
            pass
        try:
            rows = self.supabase.select(
                "assistant_events",
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(MAX_ASSISTANT_RECENT_ERRORS),
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                details = row.get("details") if isinstance(row.get("details"), dict) else {}
                mistakes = details.get("mistakes") if isinstance(details.get("mistakes"), list) else []
                if mistakes:
                    for mistake in mistakes[:3]:
                        if not isinstance(mistake, dict):
                            continue
                        errors.append(
                            {
                                "topic": row.get("topic") or row.get("action") or "",
                                "message": mistake.get("question") or "",
                                "student_answer": mistake.get("userAnswer") or "",
                                "correct_answer": mistake.get("correctAnswer") or "",
                                "percent": row.get("percent"),
                                "created_at": row.get("created_at"),
                            }
                        )
                        if len(errors) >= MAX_ASSISTANT_RECENT_ERRORS:
                            return errors[:MAX_ASSISTANT_RECENT_ERRORS]
                    continue
                errors.append(
                    {
                        "topic": row.get("topic") or row.get("action") or "",
                        "message": row.get("message") or "",
                        "student_answer": row.get("message") or "",
                        "correct_answer": "",
                        "percent": row.get("percent"),
                        "created_at": row.get("created_at"),
                    }
                )
            return errors[:MAX_ASSISTANT_RECENT_ERRORS]
        except SupabaseServiceError:
            return []

    def _load_quiz_performance_snapshot(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        user_profile: Optional[dict],
    ) -> dict[str, Any]:
        profile = user_profile if isinstance(user_profile, dict) else {}
        baseline_total = 0
        baseline_best = 0.0
        try:
            baseline_total = int(float(profile.get("ent_tests_completed") or 0))
        except Exception:
            baseline_total = 0
        try:
            baseline_best = float(profile.get("ent_best_score") or 0)
        except Exception:
            baseline_best = 0.0

        snapshot: dict[str, Any] = {
            "total_quizzes": baseline_total,
            "practice_count": 0,
            "realtest_count": 0,
            "average_percent": 0.0,
            "best_percent": baseline_best,
            "latest_percent": None,
            "latest_mode": "",
            "recent_results": [],
            "weak_topics": [],
            "strong_topics": [],
        }
        if not user_id or not self.supabase.available:
            return snapshot

        try:
            attempt_rows = self.supabase.select(
                "assistant_quiz_attempts",
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": "60",
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
            if attempt_rows:
                recent_results: list[dict[str, Any]] = []
                percents: list[int] = []
                practice_count = 0
                realtest_count = 0
                best_percent = baseline_best
                latest_percent: Optional[int] = None
                latest_mode = ""
                weak_topic_scores: dict[str, list[int]] = {}
                strong_topic_scores: dict[str, list[int]] = {}

                for row in attempt_rows:
                    if not isinstance(row, dict):
                        continue
                    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                    mode = str(row.get("mode") or "").strip().lower()
                    if mode not in {"practice", "realtest"}:
                        mode = "practice"
                    try:
                        percent = int(float(row.get("percent"))) if row.get("percent") is not None else None
                    except Exception:
                        percent = None
                    try:
                        correct = int(float(row.get("correct"))) if row.get("correct") is not None else None
                    except Exception:
                        correct = None
                    try:
                        total = int(float(row.get("total"))) if row.get("total") is not None else None
                    except Exception:
                        total = None

                    topic = self._clean_memory_fragment(
                        row.get("topic") or row.get("source_title") or row.get("source_id"),
                        limit=120,
                    )
                    if mode == "realtest":
                        realtest_count += 1
                    else:
                        practice_count += 1

                    if percent is not None:
                        percents.append(percent)
                        best_percent = max(best_percent, float(percent))
                        if latest_percent is None:
                            latest_percent = percent
                            latest_mode = mode
                        focus_topics = [
                            str(item).strip()
                            for item in (metadata.get("focus_topics") or [])
                            if str(item).strip()
                        ]
                        topic_targets = focus_topics or ([topic] if topic else [])
                        for topic_name in topic_targets:
                            if percent < 60:
                                weak_topic_scores.setdefault(topic_name, []).append(percent)
                            elif percent >= 85:
                                strong_topic_scores.setdefault(topic_name, []).append(percent)

                    if len(recent_results) < MAX_ASSISTANT_RECENT_QUIZ_RESULTS:
                        recent_results.append(
                            {
                                "mode": mode,
                                "topic": topic,
                                "percent": percent,
                                "correct": correct,
                                "total": total,
                                "created_at": row.get("created_at"),
                            }
                        )

                weak_topics = [
                    topic
                    for topic, _scores in sorted(
                        weak_topic_scores.items(),
                        key=lambda item: (-len(item[1]), sum(item[1]) / max(1, len(item[1]))),
                    )
                ][:4]
                strong_topics = [
                    topic
                    for topic, _scores in sorted(
                        strong_topic_scores.items(),
                        key=lambda item: (-len(item[1]), -(sum(item[1]) / max(1, len(item[1])))),
                    )
                ][:4]

                total_quizzes = max(baseline_total, practice_count + realtest_count)
                average_percent = round(sum(percents) / len(percents), 2) if percents else 0.0
                return {
                    "total_quizzes": total_quizzes,
                    "practice_count": practice_count,
                    "realtest_count": realtest_count,
                    "average_percent": average_percent,
                    "best_percent": round(best_percent, 2),
                    "latest_percent": latest_percent,
                    "latest_mode": latest_mode,
                    "recent_results": recent_results,
                    "weak_topics": weak_topics,
                    "strong_topics": strong_topics,
                }
        except SupabaseServiceError:
            pass

        try:
            rows = self.supabase.select(
                "assistant_events",
                params={
                    "user_id": f"eq.{user_id}",
                    "event_type": "in.(quiz_result,assistant_quiz_result)",
                    "order": "created_at.desc",
                    "limit": "60",
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError:
            return snapshot

        recent_results: list[dict[str, Any]] = []
        percents: list[int] = []
        practice_count = 0
        realtest_count = 0
        best_percent = baseline_best
        latest_percent: Optional[int] = None
        latest_mode = ""
        weak_topic_scores: dict[str, list[int]] = {}
        strong_topic_scores: dict[str, list[int]] = {}

        for row in rows:
            if not isinstance(row, dict):
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            mode = str(metadata.get("mode") or "").strip().lower()
            route = str(row.get("route") or "").strip().lower()
            action = str(row.get("action") or row.get("event_name") or "").strip().lower()
            if mode not in {"practice", "realtest"}:
                if "realtest" in route or "realtest" in action or "real test" in action:
                    mode = "realtest"
                else:
                    mode = "practice"

            percent: Optional[int]
            try:
                percent = int(float(row.get("percent"))) if row.get("percent") is not None else None
            except Exception:
                percent = None
            try:
                correct = int(float(row.get("correct"))) if row.get("correct") is not None else None
            except Exception:
                correct = None
            try:
                total = int(float(row.get("total"))) if row.get("total") is not None else None
            except Exception:
                total = None

            topic = self._clean_memory_fragment(
                row.get("topic") or metadata.get("topic") or row.get("message"),
                limit=120,
            )
            if mode == "realtest":
                realtest_count += 1
            else:
                practice_count += 1

            if percent is not None:
                percents.append(percent)
                best_percent = max(best_percent, float(percent))
                if latest_percent is None:
                    latest_percent = percent
                    latest_mode = mode
                focus_topics = [
                    str(item).strip()
                    for item in (metadata.get("focus_topics") or [])
                    if str(item).strip()
                ]
                topic_targets = focus_topics or ([topic] if topic else [])
                for topic_name in topic_targets:
                    if percent < 60:
                        weak_topic_scores.setdefault(topic_name, []).append(percent)
                    elif percent >= 85:
                        strong_topic_scores.setdefault(topic_name, []).append(percent)

            if len(recent_results) < MAX_ASSISTANT_RECENT_QUIZ_RESULTS:
                recent_results.append(
                    {
                        "mode": mode,
                        "topic": topic,
                        "percent": percent,
                        "correct": correct,
                        "total": total,
                        "created_at": row.get("created_at"),
                    }
                )

        weak_topics = [
            topic
            for topic, _scores in sorted(
                weak_topic_scores.items(),
                key=lambda item: (-len(item[1]), sum(item[1]) / max(1, len(item[1]))),
            )
        ][:4]
        strong_topics = [
            topic
            for topic, _scores in sorted(
                strong_topic_scores.items(),
                key=lambda item: (-len(item[1]), -(sum(item[1]) / max(1, len(item[1])))),
            )
        ][:4]

        total_quizzes = max(baseline_total, practice_count + realtest_count)
        average_percent = round(sum(percents) / len(percents), 2) if percents else 0.0
        return {
            "total_quizzes": total_quizzes,
            "practice_count": practice_count,
            "realtest_count": realtest_count,
            "average_percent": average_percent,
            "best_percent": round(best_percent, 2),
            "latest_percent": latest_percent,
            "latest_mode": latest_mode,
            "recent_results": recent_results,
            "weak_topics": weak_topics,
            "strong_topics": strong_topics,
        }

    def _load_user_state(self, *, user_id: str, access_token: Optional[str]) -> dict[str, Any]:
        if not user_id or not self.supabase.available:
            return {}
        try:
            rows = self.supabase.select(
                "assistant_user_state",
                params={"user_id": f"eq.{user_id}", "limit": "1"},
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
            if rows and isinstance(rows[0], dict):
                return rows[0]
        except SupabaseServiceError:
            return {}
        return {}

    def _load_user_facts(self, *, user_id: str, access_token: Optional[str]) -> list[dict[str, Any]]:
        if not user_id or not self.supabase.available:
            return []
        try:
            rows = self.supabase.select(
                "assistant_user_facts",
                params={
                    "user_id": f"eq.{user_id}",
                    "active": "eq.true",
                    "order": "confidence.desc,updated_at.desc",
                    "limit": str(MAX_ASSISTANT_FACTS),
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError:
            return []
        facts: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            facts.append(
                {
                    "fact_key": str(row.get("fact_key") or "").strip(),
                    "fact_value": str(row.get("fact_value") or "").strip(),
                    "confidence": row.get("confidence"),
                }
            )
        return [item for item in facts if item.get("fact_key") and item.get("fact_value")][:MAX_ASSISTANT_FACTS]

    def _extract_recent_routes(self, *, user_id: str, access_token: Optional[str]) -> list[str]:
        if not user_id or not self.supabase.available:
            return []
        try:
            rows = self.supabase.select(
                "assistant_events",
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "created_at.desc",
                    "limit": str(MAX_ASSISTANT_RECENT_ROUTES * 2),
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError:
            return []
        routes: list[str] = []
        for row in rows:
            route = str((row or {}).get("route") or "").strip()
            if route and route not in routes:
                routes.append(route)
            if len(routes) >= MAX_ASSISTANT_RECENT_ROUTES:
                break
        return routes

    def _build_student_profile_snapshot(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        user_profile: Optional[dict],
        experience_summary: Optional[dict],
    ) -> dict[str, Any]:
        persisted_state = self._load_user_state(user_id=user_id, access_token=access_token)
        latest_attempt = self._load_latest_quiz_attempt(
            user_id=user_id,
            access_token=access_token,
        )
        quiz_performance = self._load_quiz_performance_snapshot(
            user_id=user_id,
            access_token=access_token,
            user_profile=user_profile,
        )
        weak_topics = _merge_text_lists(
            (experience_summary or {}).get("weak_topics") or [],
            persisted_state.get("weak_topics") or [],
            quiz_performance.get("weak_topics") or [],
            limit=10,
        )
        strong_topics = _merge_text_lists(
            persisted_state.get("strong_topics") or [],
            (experience_summary or {}).get("strong_topics") or [],
            quiz_performance.get("strong_topics") or [],
            limit=10,
        )
        learning_goals = _merge_text_lists(
            (experience_summary or {}).get("learning_goals") or [],
            persisted_state.get("learning_goals") or [],
            limit=8,
        )
        preferred_language = str(
            persisted_state.get("preferred_language")
            or (user_profile or {}).get("preferred_language")
            or ""
        ).strip()
        preferred_difficulty = str(
            persisted_state.get("preferred_difficulty")
            or (user_profile or {}).get("preferred_difficulty")
            or "medium"
        ).strip()
        recent_routes = _merge_text_lists(
            persisted_state.get("recent_routes") or [],
            self._extract_recent_routes(user_id=user_id, access_token=access_token),
            limit=MAX_ASSISTANT_RECENT_ROUTES,
        )
        user_facts = self._load_user_facts(user_id=user_id, access_token=access_token)
        quiz_coaching = self._build_quiz_coaching_snapshot(
            lang=preferred_language or "kk",
            latest_attempt=latest_attempt,
            quiz_performance=quiz_performance,
        )
        return {
            "subject_combination": (user_profile or {}).get("subject_combination"),
            "weak_topics": weak_topics,
            "strong_topics": strong_topics,
            "learning_goals": learning_goals,
            "preferred_language": preferred_language,
            "preferred_difficulty": preferred_difficulty,
            "recent_routes": recent_routes,
            "facts": user_facts,
            "state_metrics": {
                "total_events": int(persisted_state.get("total_events") or 0),
                "total_quizzes": max(
                    int(persisted_state.get("total_quizzes") or 0),
                    int(quiz_performance.get("total_quizzes") or 0),
                ),
                "successful_quizzes": int(persisted_state.get("successful_quizzes") or 0),
                "average_quiz_percent": float(
                    quiz_performance.get("average_percent")
                    or persisted_state.get("average_quiz_percent")
                    or 0
                ),
                "best_quiz_percent": float(quiz_performance.get("best_percent") or 0),
                "practice_quizzes": int(quiz_performance.get("practice_count") or 0),
                "realtest_quizzes": int(quiz_performance.get("realtest_count") or 0),
            },
            "quiz_performance": quiz_performance,
            "quiz_coaching": quiz_coaching,
            "recent_errors": self._extract_recent_errors(
                user_id=user_id,
                access_token=access_token,
                experience_summary=experience_summary,
            ),
        }

    def _normalize_citations(self, knowledge_matches: list[dict]) -> list[Citation]:
        normalized: list[Citation] = []
        for item in knowledge_matches[:MAX_ASSISTANT_CITATIONS]:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("id") or item.get("source_id") or "").strip()
            title = str(item.get("title") or "").strip()
            excerpt = str(item.get("excerpt") or item.get("material_text") or "").strip()
            if not source_id or not title:
                continue
            normalized.append(Citation(id=source_id, title=title, excerpt=excerpt[:280]))
        return normalized

    def _tool_actions_with_retry(
        self,
        *,
        message: str,
        lang: Optional[str],
        student_profile_snapshot: dict[str, Any],
        knowledge_matches: list[dict],
        short_term_messages: list[dict[str, str]],
        long_term_summary: str,
    ) -> list[ActionButton]:
        last_error: Optional[Exception] = None
        for _ in range(min(2, TOOL_RETRY_ATTEMPTS)):
            try:
                return self._tool_actions_once(
                    message=message,
                    lang=lang,
                    student_profile_snapshot=student_profile_snapshot,
                    knowledge_matches=knowledge_matches,
                    short_term_messages=short_term_messages,
                    long_term_summary=long_term_summary,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.15)
        if last_error:
            logger.warning("Tool-calling failed after retries: %s", last_error)
        return []

    def _should_use_tools_for_message(self, message: str) -> bool:
        text = _normalize_free_text(message)
        keywords = (
            "open", "go to", "navigate", "section", "library", "profile",
            "upload", "guess", "game", "classmates", "favorites", "home",
            "quiz", "test", "practice", "start test",
            "make test", "create test", "generate test", "practice test",
            "\u043e\u0442\u043a\u0440\u043e\u0439", "\u043f\u0435\u0440\u0435\u0439\u0434\u0438", "\u043f\u043e\u043a\u0430\u0436\u0438", "\u0431\u0438\u0431\u043b\u0438\u043e\u0442\u0435\u043a", "\u0437\u0430\u0433\u0440\u0443\u0437", "\u043e\u0434\u043d\u043e\u043a\u043b\u0430\u0441\u0441", "\u0443\u0433\u0430\u0434\u0430\u0439", "\u0438\u0437\u0431\u0440\u0430\u043d",
            "\u0441\u0434\u0435\u043b\u0430\u0439 \u0442\u0435\u0441\u0442", "\u0441\u043e\u0437\u0434\u0430\u0439 \u0442\u0435\u0441\u0442", "\u043d\u0430\u0447\u043d\u0438 \u0442\u0435\u0441\u0442", "\u0442\u0435\u0441\u0442", "\u043a\u0432\u0438\u0437",
            "\u0430\u0448", "\u043a\u04e9\u0440\u0441\u0435\u0442", "\u0436\u04af\u043a\u0442\u0435\u0443", "\u0441\u044b\u043d\u044b\u043f\u0442\u0430\u0441", "\u0442\u0430\u04a3\u0434\u0430\u0443\u043b\u044b", "\u0431\u0430\u0441\u0442\u044b \u0431\u0435\u0442",
            "\u0442\u0435\u0441\u0442 \u0436\u0430\u0441\u0430", "\u0441\u044b\u043d\u0430\u049b \u0436\u0430\u0441\u0430", "\u0441\u044b\u043d\u0430\u049b",
        )
        return any(token in text for token in keywords)

    def _extract_requested_question_count(self, message: str) -> int:
        text = _normalize_free_text(message)
        matches = re.findall(r"\b([0-9]{1,2})\b", text)
        requested = 10
        for raw in matches:
            value = int(raw)
            if 3 <= value <= 40:
                requested = value
                break
        requested = max(5, min(30, requested))
        allowed_counts = [5, 10, 15, 20, 25, 30]
        return min(allowed_counts, key=lambda candidate: abs(candidate - requested))

    def _extract_quiz_topic(self, message: str) -> str:
        raw = str(message or "").strip()
        if not raw:
            return ""

        patterns = [
            r"(?:\btopic\b|\btheme\b)\s*[:\-]\s*(.+)$",
            r"(?:\babout\b|\bon\b)\s+(.+)$",
            r"(?:\bпро\b|\bо\b|\bоб\b|\bпо\b|\bна\b)\s+(.+)$",
        ]
        candidate = ""
        for pattern in patterns:
            found = re.search(pattern, raw, flags=re.IGNORECASE)
            if found:
                candidate = str(found.group(1) or "").strip()
                break

        if not candidate:
            candidate = raw

        candidate = re.sub(
            r"\b(?:на|по|about|for|of|na)\s+\d{1,2}\s*(?:вопрос(?:ов|а)?|сұрақ(?:тар)?|questions?|vopros(?:ov|a)?)\b",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"^\d{1,2}\s*(?:вопрос(?:ов|а)?|сұрақ(?:тар)?|questions?|vopros(?:ov|a)?)\b.*$",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"\b(?:сделай|сдела\w*|создай|созда\w*|запусти|запуст\w*|начни|начн\w*|составь|состав\w*|generate|create|make|start|build|sdelai|sdelay|sozdai|zapusti|nachni|sostav|jasa|kur|basta|daiynda|жаса\w*|құр\w*|баста\w*|дайында\w*)\b",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"\b(?:можешь|сможешь|can you|could you|please|бере аласыңба|бере аласың ба|жасап бере аласыңба|жасап бере аласың ба)\b",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"\b(?:тест|квиз|практик[ауи]|quiz|test|practice)\b",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"\b(?:туралы|жайлы|бойынша|about|on|про|о|об|по|на)\b",
            "",
            candidate,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(r"\s+", " ", candidate)
        candidate = re.sub(r"[\"'`“”«»]+", "", candidate).strip(" .,:;!?-")
        return candidate[:120].strip()

    def _fallback_quiz_title(self, *, message: str, user_profile: Optional[dict], lang: Optional[str]) -> str:
        topic_from_message = self._extract_quiz_topic(message)
        if topic_from_message:
            return topic_from_message

        profile = user_profile if isinstance(user_profile, dict) else {}
        subject_combo = str(profile.get("subject_combination") or "").strip()
        if subject_combo:
            return subject_combo
        subject1 = str(profile.get("subject1") or "").strip()
        subject2 = str(profile.get("subject2") or "").strip()
        subjects = " / ".join([item for item in [subject1, subject2] if item])
        if subjects:
            return subjects

        return self._assistant_localized(
            lang,
            "ENT aralas taqyryptary",
            "Смешанные темы ЕНТ",
            "Mixed ENT topics",
        )

    def _route_label(self, route: str, lang: Optional[str]) -> str:
        route_meta = ASSISTANT_ROUTE_META.get(str(route or "").strip(), {})
        labels = route_meta.get("labels") if isinstance(route_meta.get("labels"), dict) else {}
        normalized_lang = self._normalize_lang(lang)
        return str(
            labels.get(normalized_lang)
            or labels.get("ru")
            or route
        ).strip() or route

    def _deterministic_navigation_action(
        self,
        *,
        message: str,
        lang: Optional[str],
    ) -> Optional[ActionButton]:
        text = _normalize_free_text(message)
        if not text:
            return None

        command_tokens = (
            "open",
            "go to",
            "navigate",
            "take me",
            "show",
            "перейди",
            "открой",
            "покажи",
            "зайди",
            "веди",
            "аш",
            "көрсет",
            "бар",
            "өт",
        )
        looks_like_direct_route_request = len(text.split()) <= 6
        if not any(token in text for token in command_tokens) and not looks_like_direct_route_request:
            return None

        for route, meta in ASSISTANT_ROUTE_META.items():
            keywords = meta.get("keywords") if isinstance(meta.get("keywords"), tuple) else ()
            if not any(keyword in text for keyword in keywords):
                continue
            route_label = self._route_label(route, lang)
            return ActionButton(
                label=self._assistant_localized(
                    lang,
                    f"{route_label} ашу",
                    f"Открыть: {route_label}",
                    f"Open {route_label}",
                ),
                type="navigate",
                payload=ActionButtonPayload(route=route),
            )

        return None

    def _pick_quiz_source(
        self,
        *,
        knowledge_matches: list[dict],
        page_context: Optional[dict],
    ) -> tuple[str, str]:
        context = page_context if isinstance(page_context, dict) else {}
        source_from_context = str(
            context.get("active_material_id")
            or context.get("material_id")
            or ""
        ).strip()
        if source_from_context:
            return source_from_context, "material"

        for item in knowledge_matches or []:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or item.get("id") or "").strip()
            source_type = str(item.get("source_type") or "material").strip()
            if source_type not in {"material", "historical_figure"}:
                source_type = "material"
            if source_id:
                return source_id, source_type

        return "", "material"

    def _deterministic_quiz_action(
        self,
        *,
        message: str,
        lang: Optional[str],
        knowledge_matches: list[dict],
        page_context: Optional[dict],
        user_profile: Optional[dict],
    ) -> Optional[ActionButton]:
        text = _normalize_free_text(message)
        quiz_tokens = (
            "quiz", "test", "practice", "practice test",
            "kviz", "praktika", "praktik", "synaq",
            "\u0442\u0435\u0441\u0442", "\u043a\u0432\u0438\u0437", "\u043f\u0440\u0430\u043a\u0442\u0438\u043a", "\u0441\u044b\u043d\u0430\u049b",
        )
        command_tokens = (
            "make", "create", "start", "generate", "build",
            "sdelai", "sdelay", "sozdai", "zapusti", "nachni", "sostav",
            "jasa", "kur", "basta", "daiynda",
            "\u0441\u0434\u0435\u043b\u0430\u0439", "\u0441\u043e\u0437\u0434\u0430\u0439", "\u0437\u0430\u043f\u0443\u0441\u0442\u0438", "\u0441\u043e\u0441\u0442\u0430\u0432\u044c", "\u043d\u0430\u0447\u043d\u0438",
            "\u0436\u0430\u0441\u0430", "\u049b\u04b1\u0440", "\u0431\u0430\u0441\u0442\u0430", "\u0434\u0430\u0439\u044b\u043d\u0434\u0430",
        )
        question_tokens = (
            "what", "which", "why", "how", "where", "when",
            "что", "какой", "какая", "какие", "почему", "зачем", "как", "где", "когда",
            "неге", "қалай", "қай", "қайсы", "қайда", "қашан",
        )
        lookup_tokens = (
            "result", "results", "score", "mistake", "mistakes", "error", "errors",
            "review", "repeat", "analysis", "analyze", "latest", "last", "recent",
            "результ", "итог", "ошиб", "қате", "талда", "соңғы", "последн",
        )
        asks_quiz = any(token in text for token in quiz_tokens)
        is_command = any(token in text for token in command_tokens)
        if not asks_quiz:
            return None

        if self._is_last_quiz_lookup_request(message) or self._is_quiz_coaching_request(message):
            return None

        words = [word for word in re.split(r"\s+", text) if word]
        looks_like_direct_quiz_prompt = (
            len(words) <= 8
            and not any(token in words for token in question_tokens)
            and not any(token in text for token in lookup_tokens)
        )
        if not is_command and not looks_like_direct_quiz_prompt:
            return None

        source_id, source_type = self._pick_quiz_source(
            knowledge_matches=knowledge_matches,
            page_context=page_context,
        )
        question_count = self._extract_requested_question_count(message)
        source_title = self._fallback_quiz_title(message=message, user_profile=user_profile, lang=lang)
        quiz_language = self._resolve_requested_output_language(message, lang)
        if not source_id:
            source_type = "material"

        return ActionButton(
            label=self._assistant_localized(
                lang,
                f"{question_count} suraqtyq practice test bastau",
                f"\u0417\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c practice-\u0442\u0435\u0441\u0442 ({question_count} \u0432\u043e\u043f\u0440\u043e\u0441\u043e\u0432)",
                f"Start {question_count}-question practice test",
            ),
            type="start_quiz",
            payload=ActionButtonPayload(
                source_id=source_id or None,
                source_type=source_type,  # type: ignore[arg-type]
                source_title=source_title or None,
                assistant_prompt=message.strip() or None,
                language=quiz_language,
                question_count=question_count,
                mode="practice",
            ),
        )

    def _tool_actions_once(
        self,
        *,
        message: str,
        lang: Optional[str],
        student_profile_snapshot: dict[str, Any],
        knowledge_matches: list[dict],
        short_term_messages: list[dict[str, str]],
        long_term_summary: str,
    ) -> list[ActionButton]:
        project_route_map = {
            route: str((meta or {}).get("description") or "").strip()
            for route, meta in ASSISTANT_ROUTE_META.items()
        }
        tool_system_prompt = f"""
You are a routing agent for an ENT tutor assistant.
{self._assistant_language_instruction(lang)}
Use tool calls when navigation or quiz action is useful.
You know the whole product and may route the student to any supported section.
Do not call tools if not needed.
"""
        tool_messages = [
            {"role": "system", "content": tool_system_prompt},
            *short_term_messages,
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message": message,
                        "student_profile_snapshot": student_profile_snapshot,
                        "long_term_summary": long_term_summary,
                        "knowledge_matches": knowledge_matches[:5],
                        "project_route_map": project_route_map,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=tool_messages,
            tools=ASSISTANT_TOOLS,
            tool_choice="auto",
            temperature=0.1,
            max_completion_tokens=220,
        )
        tool_calls = response.choices[0].message.tool_calls or []
        actions: list[ActionButton] = []
        for call in tool_calls[:MAX_ASSISTANT_ACTIONS]:
            try:
                args = json.loads(call.function.arguments or "{}")
            except Exception:
                args = {}
            if call.function.name == "navigate_to_section":
                route = str(args.get("route") or "").strip()
                if route:
                    actions.append(
                        ActionButton(
                            label=self._assistant_localized(lang, "Open section", "Otkryt razdel", "Open section"),
                            type="navigate",
                            payload=ActionButtonPayload(route=route),
                        )
                    )
            elif call.function.name == "start_educational_quiz":
                source_id = str(args.get("source_id") or "").strip()
                source_type = str(args.get("source_type") or "").strip()
                source_title = str(args.get("source_title") or args.get("topic") or "").strip()
                if source_id and source_type in {"material", "historical_figure"}:
                    question_count = int(args.get("question_count") or 10)
                    actions.append(
                        ActionButton(
                            label=self._assistant_localized(lang, "Start quiz", "Nachat quiz", "Start quiz"),
                            type="start_quiz",
                            payload=ActionButtonPayload(
                                source_id=source_id,
                                source_type=source_type,
                                source_title=source_title or None,
                                assistant_prompt=message.strip() or None,
                                language=self._resolve_requested_output_language(message, lang),
                                question_count=max(5, min(30, question_count)),
                                mode="practice",
                            ),
                        )
                    )
        return actions[:MAX_ASSISTANT_ACTIONS]

    def _verify_message(
        self,
        *,
        lang: Optional[str],
        draft_message: str,
        knowledge_matches: list[dict],
        current_domain: str = "general",
    ) -> str:
        if not draft_message.strip():
            return draft_message
        if not knowledge_matches:
            return draft_message
        # Keep latency low: run verifier only for riskier fact-heavy domains.
        if current_domain not in {"history", "law"}:
            return draft_message
        verifier_prompt = f"""
{self._assistant_language_instruction(lang)}
Verify dates, names, and factual claims in this tutor message using only provided sources.
If a core fact is doubtful or unsupported, replace with a safe sentence that data is being verified.
Keep the message concise and educational. Return only the revised message.

Draft message:
{draft_message}

Sources:
{json.dumps(knowledge_matches[:5], ensure_ascii=False)}
"""
        try:
            verified = self._generate_with_retry(
                verifier_prompt,
                system_prompt="You are a strict fact verifier for ENT prep content.",
                temperature=0.0,
                max_tokens=260,
            ).strip()
            return verified or draft_message
        except Exception:
            return draft_message

    def _map_new_to_legacy_actions(self, action_buttons: list[ActionButton]) -> list[dict[str, Any]]:
        legacy: list[dict[str, Any]] = []
        for action in action_buttons:
            payload = action.payload
            if action.type == "navigate":
                legacy.append(
                    {
                        "type": "navigate",
                        "label": action.label,
                        "route": payload.route,
                        "params": {},
                    }
                )
            elif action.type == "start_quiz":
                legacy.append(
                    {
                        "type": "start_quiz",
                        "label": action.label,
                        "mode": payload.mode or "practice",
                        "count": payload.question_count or 10,
                        "source_type": payload.source_type,
                        "source_id": payload.source_id,
                        "source_title": payload.source_title,
                        "assistant_prompt": payload.assistant_prompt,
                        "language": payload.language,
                        "topic": payload.source_title,
                    }
                )
        return legacy

    def _action_buttons_from_legacy_actions(self, actions: list[dict[str, Any]], lang: Optional[str]) -> list[ActionButton]:
        result: list[ActionButton] = []
        for action in actions[:MAX_ASSISTANT_ACTIONS]:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type") or "").strip()
            if action_type == "navigate":
                route = str(action.get("route") or "").strip()
                if route not in ASSISTANT_ROUTES:
                    route = "library"
                result.append(
                    ActionButton(
                        label=str(action.get("label") or self._assistant_localized(lang, "Bolimdi ashu", "Otkryt razdel", "Open section")),
                        type="navigate",
                        payload=ActionButtonPayload(route=route),
                    )
                )
            elif action_type == "start_quiz":
                source_id = str(action.get("source_id") or "").strip()
                source_type = str(action.get("source_type") or "material").strip()
                source_title = str(action.get("source_title") or action.get("topic") or "").strip()
                assistant_prompt = str(action.get("assistant_prompt") or action.get("prompt") or "").strip()
                action_language = str(action.get("language") or "").strip()
                mode = str(action.get("mode") or "practice").strip().lower()
                if source_type not in {"material", "historical_figure"}:
                    source_type = "material"
                if mode not in {"practice", "realtest"}:
                    mode = "practice"
                if source_id or source_title:
                    result.append(
                        ActionButton(
                            label=str(action.get("label") or self._assistant_localized(lang, "Test bastau", "Nachat test", "Start quiz")),
                            type="start_quiz",
                            payload=ActionButtonPayload(
                                source_id=source_id or None,
                                source_type=source_type,  # type: ignore[arg-type]
                                source_title=source_title or None,
                                assistant_prompt=assistant_prompt or None,
                                language=self._normalize_lang(action_language) if action_language else None,
                                question_count=max(5, min(30, int(action.get("count") or 10))),
                                mode=mode,  # type: ignore[arg-type]
                            ),
                        )
                    )
        return result

    def _coerce_tutor_response(self, payload: dict[str, Any], lang: Optional[str]) -> TutorResponse:
        default_message = self._assistant_localized(
            lang,
            "Qysqa jauap beremin: suragyngyzdy naktylap jiberiniz.",
            "Даю краткий ответ: уточните, пожалуйста, вопрос.",
            "Quick answer: please clarify your question.",
        )
        intent = str(payload.get("intent") or "answer").strip().lower()
        if intent not in {"answer", "navigate", "quiz", "plan"}:
            intent = "answer"

        action_buttons_raw = payload.get("action_buttons")
        action_buttons: list[ActionButton] = []
        if isinstance(action_buttons_raw, list):
            for item in action_buttons_raw[:MAX_ASSISTANT_ACTIONS]:
                if isinstance(item, dict):
                    try:
                        action_buttons.append(ActionButton.model_validate(item))
                    except Exception:
                        continue
        elif isinstance(payload.get("actions"), list):
            action_buttons = self._action_buttons_from_legacy_actions(payload.get("actions") or [], lang)

        citations_raw = payload.get("citations")
        citations: list[Citation] = []
        if isinstance(citations_raw, list):
            for item in citations_raw[:MAX_ASSISTANT_CITATIONS]:
                if isinstance(item, dict):
                    try:
                        citations.append(Citation.model_validate(item))
                    except Exception:
                        continue

        plan_steps = payload.get("plan_steps")
        if not isinstance(plan_steps, list):
            plan_steps = None
        else:
            plan_steps = [str(step).strip() for step in plan_steps if str(step).strip()][:7]

        return TutorResponse(
            reasoning=str(payload.get("reasoning") or "Fast-mode structured response."),
            message=str(payload.get("message") or payload.get("answer") or default_message),
            intent=intent,  # type: ignore[arg-type]
            action_buttons=action_buttons,
            citations=citations,
            plan_steps=plan_steps,
        )

    def _safe_fallback_response(self, *, lang: Optional[str]) -> dict[str, Any]:
        message = self._assistant_localized(
            lang,
            "Suraqty tusindim. Qazir tolyq jauap dayyndap jatyrmyn, al qazir kitaphana ne qysqa practice test bastaimiz.",
            "Ponjal zapros. Gotovlu polnyy otvet; poka mozhno nachat s biblioteki ili korotkogo practice quiz.",
            "I understood your request. I am preparing a full answer; meanwhile, start with the library or a short practice quiz.",
        )
        fallback = TutorResponse(
            reasoning="Fallback mode due to transient tool/parse failure.",
            message=message,
            intent="answer",
            action_buttons=[],
            citations=[],
            plan_steps=None,
        )
        payload = self._build_final_json(fallback, lang)
        return self._attach_internal_meta(payload, fallback_used=True, error_code="safe_fallback")

    def _rescue_answer_response(
        self,
        *,
        message: str,
        lang: Optional[str],
        knowledge_matches: list[dict],
    ) -> Optional[dict[str, Any]]:
        """Best-effort plain answer when structured pipeline fails."""
        try:
            lang_instruction = self._assistant_language_instruction(lang)
            context_hint = json.dumps((knowledge_matches or [])[:2], ensure_ascii=False)
            rescue_prompt = f"""{lang_instruction}
Answer briefly and directly (2-5 sentences).
If context is present, prioritize it; otherwise use internal knowledge.

Question: {message}
Context: {context_hint}
"""
            answer = self._generate_with_retry(
                rescue_prompt,
                system_prompt="You are a concise tutoring assistant. Reply with plain text only.",
                temperature=0.2,
                max_tokens=300,
            ).strip()
            if not answer:
                return None
            payload = TutorResponse(
                reasoning="Rescue plain-text answer after structured pipeline failure.",
                message=answer,
                intent="answer",
                action_buttons=[],
                citations=self._normalize_citations(knowledge_matches),
                plan_steps=None,
            )
            result = self._build_final_json(payload, lang)
            return self._attach_internal_meta(result, fallback_used=True, error_code="rescue_answer")
        except Exception:
            return None

    def _attach_internal_meta(
        self,
        payload: dict[str, Any],
        *,
        fallback_used: bool = False,
        error_code: str = "",
        model_used: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        result = dict(payload or {})
        result["_fallback_used"] = bool(fallback_used)
        if error_code:
            result["_error_code"] = error_code
        if model_used:
            result["_model_used"] = model_used
        if latency_ms is not None:
            result["_latency_ms"] = max(0, int(latency_ms))
        return result

    def _build_final_json(
        self,
        parsed: TutorResponse,
        lang: Optional[str],
        summary: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        action_buttons = parsed.action_buttons[:MAX_ASSISTANT_ACTIONS]
        citations = parsed.citations[:MAX_ASSISTANT_CITATIONS]
        legacy_actions = self._map_new_to_legacy_actions(action_buttons)
        plan_steps = parsed.plan_steps if parsed.intent == "plan" else None
        payload = {
            "message": parsed.message,
            "intent": parsed.intent,
            "action_buttons": [self._model_dump(item) for item in action_buttons],
            "citations": [self._model_dump(item) for item in citations],
            "plan_steps": plan_steps,
            "actions": legacy_actions,
            "suggested_prompts": self._assistant_default_prompts(lang),
            "old_format_actions": legacy_actions,
        }
        if isinstance(summary, dict) and summary:
            payload["summary"] = summary
        return payload

    def _extract_route_from_actions(self, actions: list[dict[str, Any]]) -> Optional[str]:
        for action in actions or []:
            if not isinstance(action, dict):
                continue
            if action.get("type") == "navigate" and action.get("route"):
                return str(action.get("route"))
        return None

    def _load_session_record(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
    ) -> Optional[dict[str, Any]]:
        effective_session_id = str(session_id or "").strip()
        if not user_id or not self.supabase.available or not _looks_like_uuid(effective_session_id):
            return None
        try:
            rows = self.supabase.select(
                "assistant_sessions",
                params={
                    "id": f"eq.{effective_session_id}",
                    "user_id": f"eq.{user_id}",
                    "limit": "1",
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            )
        except SupabaseServiceError:
            return None
        if rows and isinstance(rows[0], dict):
            return rows[0]
        return None

    def _build_response_summary(
        self,
        *,
        student_profile_snapshot: dict[str, Any],
        knowledge_matches: list[dict],
        actions: list[dict[str, Any]],
        session_summary: str,
    ) -> dict[str, Any]:
        material_sources: list[dict[str, Any]] = []
        for item in knowledge_matches[:MAX_ASSISTANT_CITATIONS]:
            if not isinstance(item, dict):
                continue
            material_sources.append(
                {
                    "id": str(item.get("source_id") or item.get("id") or "").strip(),
                    "source_id": str(item.get("source_id") or item.get("id") or "").strip(),
                    "source_type": str(item.get("source_type") or "material").strip() or "material",
                    "title": str(item.get("title") or "").strip(),
                    "subject": str(item.get("subject") or "").strip(),
                    "material_text": str(
                        item.get("excerpt")
                        or item.get("material_text")
                        or item.get("text")
                        or ""
                    ).strip()[:280],
                    "updated_at": item.get("updated_at"),
                }
            )

        top_actions: list[dict[str, Any]] = []
        for action in actions[:MAX_ASSISTANT_ACTIONS]:
            if not isinstance(action, dict):
                continue
            top_actions.append(
                {
                    "type": str(action.get("type") or "").strip(),
                    "label": str(action.get("label") or "").strip(),
                    "route": str(action.get("route") or "").strip(),
                    "source_id": str(action.get("source_id") or "").strip(),
                    "source_title": str(action.get("source_title") or action.get("topic") or "").strip(),
                }
            )

        return {
            "weak_topics": _merge_text_lists(student_profile_snapshot.get("weak_topics") or [], limit=6),
            "learning_goals": _merge_text_lists(student_profile_snapshot.get("learning_goals") or [], limit=4),
            "recent_errors": list(student_profile_snapshot.get("recent_errors") or [])[:4],
            "quiz_performance": {
                "total_quizzes": int(((student_profile_snapshot.get("quiz_performance") or {}) if isinstance(student_profile_snapshot.get("quiz_performance"), dict) else {}).get("total_quizzes") or 0),
                "average_percent": float(((student_profile_snapshot.get("quiz_performance") or {}) if isinstance(student_profile_snapshot.get("quiz_performance"), dict) else {}).get("average_percent") or 0),
                "best_percent": float(((student_profile_snapshot.get("quiz_performance") or {}) if isinstance(student_profile_snapshot.get("quiz_performance"), dict) else {}).get("best_percent") or 0),
                "practice_count": int(((student_profile_snapshot.get("quiz_performance") or {}) if isinstance(student_profile_snapshot.get("quiz_performance"), dict) else {}).get("practice_count") or 0),
                "realtest_count": int(((student_profile_snapshot.get("quiz_performance") or {}) if isinstance(student_profile_snapshot.get("quiz_performance"), dict) else {}).get("realtest_count") or 0),
                "recent_results": list(((student_profile_snapshot.get("quiz_performance") or {}) if isinstance(student_profile_snapshot.get("quiz_performance"), dict) else {}).get("recent_results") or [])[:3],
            },
            "material_sources": material_sources,
            "top_actions": top_actions,
            "session_summary": str(session_summary or "").strip(),
        }

    def _remember_chat_memory(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
        signals: Optional[dict[str, Any]],
        user_profile: Optional[dict],
    ) -> None:
        if not user_id:
            return
        signal_map = signals if isinstance(signals, dict) else {}
        effective_session_id = str(session_id or "").strip() if _looks_like_uuid(str(session_id or "").strip()) else None

        preferred_name = str(signal_map.get("preferred_name") or "").strip()
        if preferred_name:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="preferred_name",
                fact_value=preferred_name,
                confidence=0.86,
                source_session_id=effective_session_id,
            )

        latest_request = self._clean_memory_fragment(signal_map.get("focus_topic"), limit=120)
        if latest_request:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="last_user_request",
                fact_value=latest_request,
                confidence=0.7,
                source_session_id=effective_session_id,
            )

        response_style = str(signal_map.get("response_style") or "").strip()
        if response_style:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="response_style",
                fact_value=response_style,
                confidence=0.78,
                source_session_id=effective_session_id,
            )

        preferred_difficulty = str(signal_map.get("preferred_difficulty") or "").strip()
        if preferred_difficulty:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="preferred_difficulty",
                fact_value=preferred_difficulty,
                confidence=0.76,
                source_session_id=effective_session_id,
            )

        focus_topic = self._clean_memory_fragment(signal_map.get("focus_topic"), limit=100)
        if focus_topic:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="focus_topic",
                fact_value=focus_topic,
                confidence=0.72,
                source_session_id=effective_session_id,
            )

        subject_combination = str(
            signal_map.get("subject_combination")
            or (user_profile or {}).get("subject_combination")
            or ""
        ).strip()
        if subject_combination:
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key="subject_combination",
                fact_value=subject_combination,
                confidence=0.82,
                source_session_id=effective_session_id,
            )

        for goal in (signal_map.get("learning_goals") or [])[:3]:
            clean_goal = self._clean_memory_fragment(goal, limit=140)
            if not clean_goal:
                continue
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key=f"learning_goal:{_normalize_free_text(clean_goal)[:80]}",
                fact_value=clean_goal,
                confidence=0.8,
                source_session_id=effective_session_id,
            )

        for topic in (signal_map.get("weak_topics") or [])[:3]:
            clean_topic = self._clean_memory_fragment(topic, limit=100)
            if not clean_topic:
                continue
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key=f"weak_topic:{_normalize_free_text(clean_topic)[:80]}",
                fact_value=f"Needs review on {clean_topic}",
                confidence=0.82,
                source_session_id=effective_session_id,
            )

        for topic in (signal_map.get("strong_topics") or [])[:3]:
            clean_topic = self._clean_memory_fragment(topic, limit=100)
            if not clean_topic:
                continue
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key=f"strong_topic:{_normalize_free_text(clean_topic)[:80]}",
                fact_value=f"Confident on {clean_topic}",
                confidence=0.78,
                source_session_id=effective_session_id,
            )

        for fact in signal_map.get("memory_facts") or []:
            if not isinstance(fact, dict):
                continue
            fact_key = str(fact.get("fact_key") or "").strip()
            fact_value = str(fact.get("fact_value") or "").strip()
            if not fact_key or not fact_value:
                continue
            self._upsert_user_fact(
                user_id=user_id,
                access_token=access_token,
                fact_key=fact_key,
                fact_value=fact_value,
                confidence=float(fact.get("confidence") or 0.75),
                source_session_id=effective_session_id,
            )

    def _persist_chat_exchange(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
        user_message: str,
        assistant_payload: dict[str, Any],
        existing_session: Optional[dict[str, Any]] = None,
        session_summary: str = "",
    ) -> Optional[dict[str, Any]]:
        if not self.supabase.available or not user_id:
            return None

        token = access_token if access_token else None
        use_service_role = not bool(token)
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        clean_user_message = str(user_message or "").strip()
        assistant_message = str(assistant_payload.get("message") or "").strip()
        assistant_intent = str(assistant_payload.get("intent") or "answer")
        assistant_actions = assistant_payload.get("actions") if isinstance(assistant_payload.get("actions"), list) else []
        assistant_citations = assistant_payload.get("citations") if isinstance(assistant_payload.get("citations"), list) else []
        fallback_used = bool(assistant_payload.get("_fallback_used"))
        error_code = str(assistant_payload.get("_error_code") or "").strip()
        model_used = str(assistant_payload.get("_model_used") or self.last_model_used or "").strip()
        latency_ms_raw = assistant_payload.get("_latency_ms")
        latency_ms = int(latency_ms_raw) if isinstance(latency_ms_raw, (int, float, str)) and str(latency_ms_raw).strip().isdigit() else None
        route = self._extract_route_from_actions(assistant_actions)
        turn_id = str(uuid.uuid4())

        effective_session_id = str(session_id or "").strip()
        existing = existing_session if isinstance(existing_session, dict) else None
        if not existing and _looks_like_uuid(effective_session_id):
            existing = self._load_session_record(
                user_id=user_id,
                access_token=access_token,
                session_id=effective_session_id,
            )

        if not existing:
            title = clean_user_message[:72] if clean_user_message else "New chat"
            quality_score = min(
                100,
                max(
                    0,
                    40
                    + (12 if assistant_message else 0)
                    + (8 if assistant_actions else 0)
                    + (8 if assistant_citations else 0)
                    - (25 if fallback_used else 0),
                ),
            )
            try:
                inserted = self.supabase.insert(
                    "assistant_sessions",
                    {
                        "user_id": user_id,
                        "title": title,
                        "last_message_preview": (assistant_message or clean_user_message)[:220],
                        "last_intent": assistant_intent,
                        "last_route": route,
                        "last_message_at": now_iso,
                        "status": "active",
                        "quality_score": quality_score,
                        "conversation_turns": 1,
                        "fallback_count": 1 if fallback_used else 0,
                        "last_error_code": error_code or None,
                        "last_model": model_used or None,
                        "summary": str(session_summary or "").strip()[:MAX_ASSISTANT_SESSION_SUMMARY_CHARS],
                    },
                    auth_token=token,
                    use_service_role=use_service_role,
                )
                existing = inserted[0] if inserted else None
            except SupabaseServiceError as exc:
                logger.warning("assistant session create failed: %s", exc)
                return None
        else:
            effective_session_id = str(existing.get("id") or effective_session_id)
            turns = int(existing.get("conversation_turns") or 0) + 1
            fallback_count = int(existing.get("fallback_count") or 0) + (1 if fallback_used else 0)
            quality_score = min(
                100,
                max(
                    0,
                    int(existing.get("quality_score") or 45)
                    + (6 if assistant_message else -2)
                    + (4 if assistant_actions else 0)
                    + (3 if assistant_citations else 0)
                    - (12 if fallback_used else 0),
                ),
            )
            try:
                updated = self.supabase.update(
                    "assistant_sessions",
                    {"id": f"eq.{effective_session_id}", "user_id": f"eq.{user_id}"},
                    {
                        "last_message_preview": (assistant_message or clean_user_message)[:220],
                        "last_intent": assistant_intent,
                        "last_route": route,
                        "last_message_at": now_iso,
                        "status": "active",
                        "quality_score": quality_score,
                        "conversation_turns": turns,
                        "fallback_count": fallback_count,
                        "last_error_code": error_code or None,
                        "last_model": model_used or None,
                        "abandoned_at": None,
                        "closed_at": None,
                        "summary": str(
                            session_summary
                            or existing.get("summary")
                            or ""
                        ).strip()[:MAX_ASSISTANT_SESSION_SUMMARY_CHARS],
                    },
                    auth_token=token,
                    use_service_role=use_service_role,
                )
                if updated:
                    existing = updated[0]
            except SupabaseServiceError as exc:
                logger.warning("assistant session update failed: %s", exc)

        effective_session_id = str((existing or {}).get("id") or effective_session_id).strip()
        if not _looks_like_uuid(effective_session_id):
            return None

        # Persist user + assistant messages so switching sessions shows full history.
        message_rows = []
        if clean_user_message:
            message_rows.append(
                {
                    "session_id": effective_session_id,
                    "user_id": user_id,
                    "role": "user",
                    "title": "",
                    "content": clean_user_message,
                    "intent": None,
                    "actions": [],
                    "citations": [],
                    "turn_id": turn_id,
                    "latency_ms": None,
                    "model_used": model_used or None,
                    "fallback_used": False,
                    "error_code": None,
                    "metadata": {"source": "assistant_chat", "latency_ms": None},
                }
            )
        if assistant_message:
            message_rows.append(
                {
                    "session_id": effective_session_id,
                    "user_id": user_id,
                    "role": "assistant",
                    "title": "",
                    "content": assistant_message,
                    "intent": assistant_intent,
                    "actions": assistant_actions,
                    "citations": assistant_citations,
                    "turn_id": turn_id,
                    "latency_ms": latency_ms,
                    "model_used": model_used or None,
                    "fallback_used": fallback_used,
                    "error_code": error_code or None,
                    "metadata": {
                        "source": "assistant_chat",
                        "fallback_used": fallback_used,
                        "route": route or "",
                    },
                }
            )
        if message_rows:
            try:
                self.supabase.insert(
                    "assistant_messages",
                    message_rows,
                    auth_token=token,
                    use_service_role=use_service_role,
                )
            except SupabaseServiceError as exc:
                logger.warning("assistant messages insert failed: %s", exc)

        return {
            "id": effective_session_id,
            "title": str((existing or {}).get("title") or clean_user_message[:72] or "New chat"),
            "preview": (assistant_message or clean_user_message)[:220],
            "created_at": (existing or {}).get("created_at"),
            "updated_at": (existing or {}).get("updated_at") or now_iso,
            "last_message_at": (existing or {}).get("last_message_at") or now_iso,
            "status": (existing or {}).get("status") or "active",
            "quality_score": int((existing or {}).get("quality_score") or 0),
            "conversation_turns": int((existing or {}).get("conversation_turns") or 0),
        }

    def _upsert_user_state(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        patch: dict[str, Any],
    ) -> None:
        if not user_id or not self.supabase.available:
            return
        token = access_token if access_token else None
        use_service_role = not bool(token)
        existing = self._load_user_state(user_id=user_id, access_token=access_token)

        current_routes = _merge_text_lists(existing.get("recent_routes") or [], limit=MAX_ASSISTANT_RECENT_ROUTES)
        patch_routes = _merge_text_lists(patch.get("recent_routes") or [], limit=MAX_ASSISTANT_RECENT_ROUTES)
        merged_routes = _merge_text_lists(current_routes, patch_routes, limit=MAX_ASSISTANT_RECENT_ROUTES)

        current_weak = _merge_text_lists(existing.get("weak_topics") or [], limit=10)
        current_strong = _merge_text_lists(existing.get("strong_topics") or [], limit=10)
        current_goals = _merge_text_lists(existing.get("learning_goals") or [], limit=8)

        payload = {
            "preferred_language": str(patch.get("preferred_language") or existing.get("preferred_language") or "kk"),
            "preferred_difficulty": str(patch.get("preferred_difficulty") or existing.get("preferred_difficulty") or "medium"),
            "response_style": str(patch.get("response_style") or existing.get("response_style") or "concise"),
            "learning_goals": _merge_text_lists(current_goals, patch.get("learning_goals") or [], limit=8),
            "weak_topics": _merge_text_lists(current_weak, patch.get("weak_topics") or [], limit=10),
            "strong_topics": _merge_text_lists(current_strong, patch.get("strong_topics") or [], limit=10),
            "recent_routes": merged_routes,
            "last_active_route": str(patch.get("last_active_route") or existing.get("last_active_route") or ""),
            "last_seen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_events": int(existing.get("total_events") or 0) + int(patch.get("total_events_delta") or 0),
            "total_quizzes": int(existing.get("total_quizzes") or 0) + int(patch.get("total_quizzes_delta") or 0),
            "successful_quizzes": int(existing.get("successful_quizzes") or 0) + int(patch.get("successful_quizzes_delta") or 0),
        }

        if patch.get("average_quiz_percent") is not None:
            payload["average_quiz_percent"] = float(patch.get("average_quiz_percent") or 0)
        elif patch.get("quiz_percent") is not None:
            previous_count = int(existing.get("total_quizzes") or 0)
            previous_avg = float(existing.get("average_quiz_percent") or 0)
            new_percent = float(patch.get("quiz_percent") or 0)
            payload["average_quiz_percent"] = round(
                ((previous_avg * previous_count) + new_percent) / max(1, previous_count + 1),
                2,
            )
        else:
            payload["average_quiz_percent"] = float(existing.get("average_quiz_percent") or 0)

        try:
            if existing and existing.get("id"):
                self.supabase.update(
                    "assistant_user_state",
                    {"id": f"eq.{existing['id']}", "user_id": f"eq.{user_id}"},
                    payload,
                    auth_token=token,
                    use_service_role=use_service_role,
                )
            else:
                payload["user_id"] = user_id
                self.supabase.insert(
                    "assistant_user_state",
                    payload,
                    auth_token=token,
                    use_service_role=use_service_role,
                )
        except SupabaseServiceError as exc:
            logger.warning("assistant_user_state upsert failed: %s", exc)

    def _upsert_user_fact(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        fact_key: str,
        fact_value: str,
        confidence: float = 0.6,
        source_event_id: Optional[str] = None,
        source_session_id: Optional[str] = None,
        active: bool = True,
        expires_at: Optional[str] = None,
    ) -> None:
        if not user_id or not self.supabase.available:
            return
        key = str(fact_key or "").strip()
        value = str(fact_value or "").strip()
        if not key or not value:
            return
        token = access_token if access_token else None
        use_service_role = not bool(token)
        payload = {
            "user_id": user_id,
            "fact_key": key[:160],
            "fact_value": value[:1200],
            "confidence": round(max(0.0, min(1.0, float(confidence))), 3),
            "source_event_id": source_event_id if _looks_like_uuid(str(source_event_id or "")) else None,
            "source_session_id": source_session_id if _looks_like_uuid(str(source_session_id or "")) else None,
            "active": bool(active),
            "expires_at": expires_at,
        }
        try:
            self.supabase.upsert(
                "assistant_user_facts",
                payload,
                on_conflict="user_id,fact_key",
                auth_token=token,
                use_service_role=use_service_role,
            )
        except SupabaseServiceError as exc:
            logger.warning("assistant_user_facts upsert failed: %s", exc)

    def _close_stale_sessions(self, *, user_id: str, access_token: Optional[str]) -> None:
        if not user_id or not self.supabase.available:
            return
        token = access_token if access_token else None
        use_service_role = not bool(token)
        stale_threshold = time.time() - (60 * 60 * 8)
        stale_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stale_threshold))
        try:
            self.supabase.update(
                "assistant_sessions",
                {
                    "user_id": f"eq.{user_id}",
                    "status": "eq.active",
                    "last_message_at": f"lt.{stale_iso}",
                    "conversation_turns": "lt.2",
                },
                {
                    "status": "abandoned",
                    "abandoned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                auth_token=token,
                use_service_role=use_service_role,
            )
            self.supabase.update(
                "assistant_sessions",
                {
                    "user_id": f"eq.{user_id}",
                    "status": "eq.active",
                    "last_message_at": f"lt.{stale_iso}",
                    "conversation_turns": "gte.2",
                },
                {
                    "status": "closed",
                    "closed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                auth_token=token,
                use_service_role=use_service_role,
            )
        except SupabaseServiceError:
            return

    def _candidate_models(self) -> list[str]:
        return [self.model, *self.fallback_models]

    def _is_model_access_error(self, error_str: str) -> bool:
        text = str(error_str or "").lower()
        # unsupported_parameter means this model doesn't support the call shape —
        # treat as a reason to try the next fallback model
        if "unsupported_parameter" in text or "unsupported parameter" in text:
            return True
        if "model" not in text:
            return False
        return any(
            token in text
            for token in (
                "does not exist",
                "not found",
                "unknown model",
                "do not have access",
                "access to",
                "permission",
                "unsupported model",
            )
        )

    def _json_candidates(self, raw: str) -> list[str]:
        candidates: list[str] = []
        for candidate in (
            raw,
            self._clean_json_response(raw),
            self._extract_first_json_object(raw),
            self._strip_trailing_commas(self._clean_json_response(raw)),
            self._strip_trailing_commas(self._extract_first_json_object(raw)),
        ):
            candidate = str(candidate or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _parse_json_response(self, raw: str) -> dict:
        last_error: Optional[Exception] = None
        for candidate in self._json_candidates(raw):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception as exc:
                last_error = exc

        if last_error:
            raw_preview = str(raw or "").replace("\n", "\\n")[:MAX_JSON_LOG_PREVIEW_CHARS]
            logger.warning(
                "Failed to parse OpenAI JSON from model %s: %s | raw=%r",
                self.last_model_used,
                last_error,
                raw_preview,
            )
            raise last_error
        raise ValueError("OpenAI response was not valid JSON")

    def _generate_json_object(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        conversation_messages: Optional[list[dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2400,
    ) -> dict:
        raw = self._generate_with_retry(
            prompt,
            system_prompt=system_prompt,
            conversation_messages=conversation_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return self._parse_json_response(raw)
    
    def _generate_with_retry(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        *,
        conversation_messages: Optional[list[dict[str, str]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ) -> str:
        """Generate content with SDK retries and model fallback."""
        last_error = None
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if conversation_messages:
            messages.extend(conversation_messages)
        messages.append({"role": "user", "content": prompt})

        candidate_models = self._candidate_models()
        last_candidate = candidate_models[-1] if candidate_models else self.model

        # Track which token param works per model to avoid repeated probing
        _use_legacy_max_tokens: dict[str, bool] = {}

        for model_name in candidate_models:
            self.last_model_used = model_name
            try:
                # Newer models (o-series, gpt-5.x) require max_completion_tokens;
                # older models (gpt-4o, gpt-3.5-turbo) use max_tokens.
                # We try max_completion_tokens first, then fall back automatically.
                use_legacy = _use_legacy_max_tokens.get(model_name, False)
                token_kwargs = (
                    {"max_tokens": max_tokens}
                    if use_legacy
                    else {"max_completion_tokens": max_tokens}
                )
                try:
                    response = self.client.chat.completions.create(
                        model=model_name,
                        messages=messages,
                        temperature=temperature,
                        timeout=self.request_timeout,
                        **token_kwargs,
                    )
                except Exception as inner_e:
                    inner_str = str(inner_e).lower()
                    # If max_completion_tokens is unsupported, retry with max_tokens
                    if (
                        not use_legacy
                        and "max_completion_tokens" in inner_str
                        and "unsupported" in inner_str
                    ):
                        _use_legacy_max_tokens[model_name] = True
                        response = self.client.chat.completions.create(
                            model=model_name,
                            messages=messages,
                            temperature=temperature,
                            timeout=self.request_timeout,
                            max_tokens=max_tokens,
                        )
                    # If max_tokens is unsupported, retry with max_completion_tokens
                    elif (
                        use_legacy
                        and "max_tokens" in inner_str
                        and "unsupported" in inner_str
                    ):
                        _use_legacy_max_tokens[model_name] = False
                        response = self.client.chat.completions.create(
                            model=model_name,
                            messages=messages,
                            temperature=temperature,
                            timeout=self.request_timeout,
                            max_completion_tokens=max_tokens,
                        )
                    else:
                        raise

                return str(response.choices[0].message.content or "")
            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                if self._is_model_access_error(error_str) and model_name != last_candidate:
                    logger.warning(
                        "OpenAI model %s is unavailable for this project, trying fallback model. Error: %s",
                        model_name,
                        e,
                    )
                    continue

                raise
        
        raise last_error

    def generate_assistant_response(
        self,
        *,
        message: str,
        lang: Optional[str],
        chat_history: list[dict],
        user_profile: Optional[dict],
        page_context: Optional[dict],
        experience_summary: Optional[dict],
        knowledge_matches: list[dict],
        active_material_excerpt: str = "",
        user_id: str = "",
        access_token: Optional[str] = None,
        persisted_session_summary: str = "",
        chat_memory_signals: Optional[dict[str, Any]] = None,
    ) -> dict:
        """Generate assistant response with smart routing: fast (gpt-4o) or full (gpt-4o)."""
        started_at = time.perf_counter()
        started_wall = time.time()
        lang = self._normalize_lang(lang)
        
        # Detect language from message content (override if different from frontend setting)
        lang = override_language_if_detected(message, lang)

        deterministic_navigation_action = self._deterministic_navigation_action(
            message=message,
            lang=lang,
        )
        deterministic_quiz_action = self._deterministic_quiz_action(
            message=message,
            lang=lang,
            knowledge_matches=knowledge_matches or [],
            page_context=page_context,
            user_profile=user_profile,
        )
        if deterministic_navigation_action:
            deterministic_response = TutorResponse(
                reasoning="Deterministic command parser matched navigation intent.",
                message=self._assistant_localized(
                    lang,
                    f"Төмендегі батырма арқылы {self._route_label(str(deterministic_navigation_action.payload.route or ''), lang)} бөліміне өте аласыз.",
                    f"Нажмите кнопку ниже, чтобы перейти в раздел: {self._route_label(str(deterministic_navigation_action.payload.route or ''), lang)}.",
                    f"Use the button below to open {self._route_label(str(deterministic_navigation_action.payload.route or ''), lang)}.",
                ),
                intent="navigate",
                action_buttons=[deterministic_navigation_action],
                citations=[],
                plan_steps=None,
            )
            deterministic_profile_snapshot = self._merge_chat_memory_into_snapshot(
                self._build_student_profile_snapshot(
                    user_id=str(user_id or ""),
                    access_token=access_token,
                    user_profile=user_profile,
                    experience_summary=experience_summary,
                ),
                chat_memory_signals,
            )
            deterministic_summary = self._build_response_summary(
                student_profile_snapshot=deterministic_profile_snapshot,
                knowledge_matches=[],
                actions=self._map_new_to_legacy_actions([deterministic_navigation_action]),
                session_summary=str(persisted_session_summary or "").strip(),
            )
            payload = self._build_final_json(deterministic_response, lang, summary=deterministic_summary)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            return self._attach_internal_meta(
                payload,
                fallback_used=False,
                model_used="deterministic-navigation",
                latency_ms=latency_ms,
            )

        if deterministic_quiz_action:
            deterministic_intent = "quiz" if deterministic_quiz_action.type == "start_quiz" else "navigate"
            deterministic_message = (
                self._assistant_localized(
                    lang,
                    "Practice test dayyn. Tomendegi batyrmany basyp bastanyz.",
                    "Готово: practice-тест подготовлен. Нажмите кнопку ниже, чтобы запустить.",
                    "Done: your practice test is ready. Use the button below to start.",
                )
                if deterministic_quiz_action.type == "start_quiz"
                else self._assistant_localized(
                    lang,
                    "Practice bolimine otu ushin tomendegi batyrmany basynyz.",
                    "Источник для теста не найден. Откройте practice-раздел по кнопке ниже.",
                    "I couldn't find a quiz source yet. Open the practice section with the button below.",
                )
            )
            deterministic_response = TutorResponse(
                reasoning="Deterministic command parser matched quiz intent.",
                message=deterministic_message,
                intent=deterministic_intent,  # type: ignore[arg-type]
                action_buttons=[deterministic_quiz_action],
                citations=self._normalize_citations((knowledge_matches or [])[:3]),
                plan_steps=None,
            )
            deterministic_profile_snapshot = self._merge_chat_memory_into_snapshot(
                self._build_student_profile_snapshot(
                    user_id=str(user_id or ""),
                    access_token=access_token,
                    user_profile=user_profile,
                    experience_summary=experience_summary,
                ),
                chat_memory_signals,
            )
            deterministic_summary = self._build_response_summary(
                student_profile_snapshot=deterministic_profile_snapshot,
                knowledge_matches=(knowledge_matches or [])[:3],
                actions=self._map_new_to_legacy_actions([deterministic_quiz_action]),
                session_summary=str(persisted_session_summary or "").strip(),
            )
            payload = self._build_final_json(deterministic_response, lang, summary=deterministic_summary)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            return self._attach_internal_meta(
                payload,
                fallback_used=False,
                model_used=self.last_model_used,
                latency_ms=latency_ms,
            )

        smalltalk_response = self._build_smalltalk_response(message, lang)
        if smalltalk_response:
            payload = {
                "message": smalltalk_response.get("message", ""),
                "reasoning": smalltalk_response.get("reasoning", ""),
                "intent": smalltalk_response.get("intent", "answer"),
                "action_buttons": smalltalk_response.get("action_buttons", []),
                "citations": smalltalk_response.get("citations", []),
                "plan_steps": smalltalk_response.get("plan_steps"),
                "suggested_prompts": self._assistant_default_prompts(lang),
                "actions": [],
                "old_format_actions": [],
            }
            return self._attach_internal_meta(
                payload,
                fallback_used=False,
                model_used="deterministic-smalltalk",
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )

        supabase_lookup_response = self._build_supabase_lookup_response(
            message=message,
            lang=lang,
            user_id=str(user_id or ""),
            access_token=access_token,
        )
        if supabase_lookup_response:
            supabase_lookup_response.setdefault("_latency_ms", int((time.perf_counter() - started_at) * 1000))
            return supabase_lookup_response

        quiz_coaching_response = self._build_quiz_coaching_response(
            message=message,
            lang=lang,
            user_id=str(user_id or ""),
            access_token=access_token,
        )
        if quiz_coaching_response:
            quiz_coaching_response.setdefault("_latency_ms", int((time.perf_counter() - started_at) * 1000))
            return quiz_coaching_response
        
        logger.info("=" * 80)
        logger.info(f"[START] User message: {str(message or '')[:100]}")
        logger.info(f"[START] Chat history length: {len(chat_history or [])}")
        logger.info(f"[START] Knowledge matches: {len(knowledge_matches or [])}")
        logger.info(f"[START] Detected language: {lang}")
        
        # === FAST PATH for simple messages ===
        if self._is_simple_message(message):
            logger.info("⚡ [FAST PATH] Simple message detected")
            elapsed = time.perf_counter() - started_at
            logger.info(f"⚡ [FAST PATH] Detection took {elapsed:.2f}s")
            
            fast_response = self._generate_fast_simple_response(message, lang)
            elapsed = time.perf_counter() - started_at
            logger.info(f"⚡ [FAST PATH] Total response time: {elapsed:.2f}s")
            
            payload = {
                "message": fast_response.get("message", ""),
                "reasoning": fast_response.get("reasoning", ""),
                "intent": fast_response.get("intent", "answer"),
                "action_buttons": fast_response.get("action_buttons", []),
                "citations": fast_response.get("citations", []),
                "plan_steps": fast_response.get("plan_steps"),
                "suggested_prompts": self._assistant_default_prompts(lang),
                "actions": [],
                "old_format_actions": [],
            }
            return self._attach_internal_meta(
                payload,
                fallback_used=False,
                model_used=self.last_model_used,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )

        # Search PDF knowledge base only for full educational answers.
        pdf_matches = self._search_pdf_knowledge_with_timeout(message, max_results=3)
        if pdf_matches:
            logger.info(f"📚 Found {len(pdf_matches)} PDF matches")
            knowledge_matches = (knowledge_matches or []) + pdf_matches
        
        # === FULL PATH for complex messages ===
        logger.info("💪 [FULL PATH] Complex message detected")
        elapsed = time.perf_counter() - started_at
        logger.info(f"💪 [FULL PATH] Detection took {elapsed:.2f}s")
        
        normalized_message = _normalize_free_text(message)
        logger.info(f"💪 [FULL PATH] Using model: {self.model}")
        logger.info(f"💪 [FULL PATH] Timeout: {self.request_timeout}s")

        fast_mode = str(os.getenv("ASSISTANT_FAST_MODE", "true")).strip().lower() in {"1", "true", "yes"}
        logger.info("START GENERATION: %s", str(message or "")[:50])
        prepared_messages = self._prepare_assistant_messages(
            chat_history=chat_history,
            message=message,
            lang=lang,
            persisted_session_summary=persisted_session_summary,
        )
        short_term_messages = prepared_messages["short_term_messages"]
        long_term_summary = prepared_messages["incremental_summary"]
        current_domain = prepared_messages["current_domain"]
        domain_shift = bool(prepared_messages["domain_shift"])
        trap_instructions = str(prepared_messages.get("trap_instructions") or "")
        if not short_term_messages:
            short_term_messages = []
        if len(chat_history or []) < 2 and not persisted_session_summary:
            long_term_summary = ""
        student_profile_snapshot = self._build_student_profile_snapshot(
            user_id=str(user_id or ""),
            access_token=access_token,
            user_profile=user_profile,
            experience_summary=experience_summary,
        )
        student_profile_snapshot = self._merge_chat_memory_into_snapshot(
            student_profile_snapshot,
            chat_memory_signals,
        )
        recent_conversation_messages = short_term_messages[-MAX_ASSISTANT_RECENT_CONTEXT_MESSAGES:]
        recent_conversation_summary = self._build_recent_history_summary(recent_conversation_messages)

        rag_matches = (knowledge_matches or [])[:3]
        try:
            # Hybrid Search: one-pass RAG context. If empty, fallback to model knowledge.
            if rag_matches:
                context_chunk = f"Contextual Knowledge (RAG): {json.dumps(rag_matches, ensure_ascii=False)}"
            else:
                logger.info("RAG sources empty. Switching to LLM internal knowledge.")
                context_chunk = "Note: No specific database matches found. Use your internal expert training."

            should_use_tools = self._should_use_tools_for_message(message)
            tool_actions = []
            if should_use_tools and not fast_mode:
                tool_actions = self._tool_actions_with_retry(
                    message=message,
                    lang=lang,
                    student_profile_snapshot=student_profile_snapshot,
                    knowledge_matches=rag_matches,
                    short_term_messages=short_term_messages,
                    long_term_summary=long_term_summary,
                )
            trap_points = self._extract_ent_trap_points(
                knowledge_matches=rag_matches,
                student_profile_snapshot=student_profile_snapshot,
            )
            project_route_map = {
                route: {
                    "label": self._route_label(route, lang),
                    "description": str((meta or {}).get("description") or "").strip(),
                }
                for route, meta in ASSISTANT_ROUTE_META.items()
            }
            if (time.perf_counter() - started_at) > ASSISTANT_MAX_PIPELINE_SECONDS:
                return self._safe_fallback_response(lang=lang)

            system_prompt = f"""You are an ENT AI Tutor and full study copilot for Kazakhstan students.
{self._assistant_language_instruction(lang)}

CRITICAL INSTRUCTIONS:
1. ALWAYS search provided knowledge sources first - DO NOT say "go to library"
2. If knowledge_matches are available, USE THEM as primary source
3. User language preference: {self._normalize_lang(lang)}
4. Answer briefly and accurately
5. Never tell user to search - provide answers directly from available sources
6. Respect recent conversation context and stored student memory when relevant
7. If the user refers to earlier discussion, use the recent chat messages provided above
8. You know the full product map and should offer route buttons whenever the user asks to open or reach a section
9. If quiz mistakes, weak topics, or quiz_coaching data exist, analyze them concretely instead of giving generic motivation
10. Prefer actionable help: what to repeat, which topic to review in Learn, when to drill with Practice, and when to move to Real Test
11. When the student asks what to do next, give a strong next step, not a vague suggestion
12. Behave like a full academic helper: explain concepts, summarize materials, answer from user materials, build study plans, review last tests, and create quizzes when useful
13. Be human and practical: answer the exact ask first, then give the most useful next step
14. If the user asks to answer from their materials and relevant material exists, explicitly ground the answer in those materials
15. If evidence is limited, say what is known and avoid pretending certainty"""
            
            prompt = f"""User question: {message}

Stored tutoring memory summary:
{long_term_summary or "No stored session memory yet."}

Recent conversation recap:
{recent_conversation_summary or "No recent dialogue context."}

Active material excerpt:
{str(active_material_excerpt or "").strip()[:MAX_ACTIVE_MATERIAL_EXCERPT_CHARS] or "No active material excerpt."}

Available knowledge materials:
{json.dumps(rag_matches[:2], ensure_ascii=False) if rag_matches else "No external materials - use your knowledge"}

RAG context summary:
{context_chunk}

Trap points to avoid:
{json.dumps(trap_points, ensure_ascii=False)}
{trap_instructions}

Student profile snapshot:
{json.dumps(student_profile_snapshot, ensure_ascii=False)}

Current page context:
{json.dumps(page_context or {}, ensure_ascii=False)}

Project route map:
{json.dumps(project_route_map, ensure_ascii=False)}

Respond with ANSWER only. Format: {{
  "message": "Your direct answer here. Start with the answer, then add the best next step if useful. DO NOT suggest library as a fallback.",
  "intent": "answer",
  "action_buttons": [],
  "citations": [],
  "plan_steps": []
}}"""
            if fast_mode:
                logger.info("💪 [FULL] Fast mode enabled - reduced tokens")
                raw_text = self._generate_with_retry(
                    prompt=prompt + "\n\nReturn strict JSON object only.",
                    system_prompt=system_prompt,
                    conversation_messages=recent_conversation_messages,
                    temperature=0.2,
                    max_tokens=600,
                )
                raw_dict = self._parse_json_response(raw_text)
                parsed = self._coerce_tutor_response(raw_dict, lang)
            else:
                parsed_response = self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *recent_conversation_messages,
                        {"role": "user", "content": prompt},
                    ],
                    response_format=TutorResponse,
                    temperature=0.2,
                    max_completion_tokens=1000,
                )
                message_obj = parsed_response.choices[0].message
                parsed = getattr(message_obj, "parsed", None)
                if parsed is None:
                    raise ValueError("Parsed structured response is empty")

            if tool_actions:
                parsed.action_buttons = tool_actions

            parsed.citations = self._normalize_citations(rag_matches)
            remaining_budget = ASSISTANT_MAX_PIPELINE_SECONDS - (time.perf_counter() - started_at)
            if remaining_budget > 6.0:
                parsed.message = self._verify_message(
                    lang=lang,
                    draft_message=parsed.message,
                    knowledge_matches=rag_matches,
                    current_domain=current_domain,
                )
            if parsed.intent == "plan" and not parsed.plan_steps:
                weak = [str(x).strip() for x in student_profile_snapshot.get("weak_topics") or [] if str(x).strip()]
                topic = weak[0] if weak else "priority weak topic"
                parsed.plan_steps = [
                    f"Review core theory for {topic} (20-30 min).",
                    "Solve 10-15 focused ENT practice questions.",
                    "Write down every mistake and why it happened.",
                    "Re-test with a short timed quiz and compare accuracy.",
                ]
            logger.info("assistant.generate_assistant_response took %.2fs", time.time() - started_wall)
            result = self._build_final_json(
                parsed,
                lang,
                summary=self._build_response_summary(
                    student_profile_snapshot=student_profile_snapshot,
                    knowledge_matches=rag_matches,
                    actions=self._map_new_to_legacy_actions(parsed.action_buttons[:MAX_ASSISTANT_ACTIONS]),
                    session_summary=long_term_summary,
                ),
            )
            return self._attach_internal_meta(
                result,
                fallback_used=False,
                model_used=self.last_model_used,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except Exception as exc:
            logger.warning("Assistant response switched to safe fallback: %s", exc)
            logger.info("assistant.generate_assistant_response failed after %.2fs", time.time() - started_wall)
            rescue = self._rescue_answer_response(
                message=message,
                lang=lang,
                knowledge_matches=rag_matches,
            )
            if rescue:
                return rescue
            return self._safe_fallback_response(lang=lang)

    async def generate_learn_content(self, material: str, history_mode: bool = False, lang: Optional[str] = None) -> dict:
        """Generate learning plan with content and questions."""
        target_chars = MAX_MATERIAL_CHARS_HISTORY if history_mode else MAX_MATERIAL_CHARS_DEFAULT
        material = self._prepare_large_material(material, target_chars=target_chars, lang=lang)
        lang_instruction = self._language_instruction(lang)
        
        if history_mode:
            prompt = f"""Сен - тарих оқытушы AI. Тек берілген материалды пайдалан.
{lang_instruction}

ТАПСЫРМА: Материалды 3-5 тарихи бөлімге бөл. Әр бөлімге 3 ТОЛЫҚ көрініс жаса:

1) "general" - ТОЛЫҚ БАЯНДАУ:
- Тарихи оқиғаларды толық сипатта
- Себептерін, барысын, нәтижелерін жаз
- Тарихи тұлғалар туралы мәлімет бер
- 5-10 сөйлем болсын

2) "summary" - КОНСПЕКТ:
- Негізгі фактілер тізімі
- Есімдер, орындар, оқиғалар
- 5-8 пункт болсын

3) "timeline" - ХРОНОЛОГИЯ (МАҢЫЗДЫ!):
- Жыл нақты көрсетілсін
- Әр жылға ТОЛЫҚ оқиға сипаттамасы
- Барлық күндерді қамту
- Формат: [{{"period": "1465 жыл", "event": "Керей мен Жәнібек сұлтандар Әбілқайыр ханның қол астынан кетіп, Қазақ хандығын құрды."}}]

Әр бөлімге 3 ЕНТ деңгейіндегі сұрақ құр.

JSON ФОРМАТ:
{{
  "plan": [
    {{
      "title": "Бөлім атауы",
      "content": {{
        "general": "Толық тарихи баяндау...",
        "summary": ["Факт 1", "Факт 2", "Факт 3", "Факт 4", "Факт 5"],
        "timeline": [
          {{"period": "1465 жыл", "event": "Толық оқиға сипаттамасы..."}},
          {{"period": "1480 жыл", "event": "Келесі маңызды оқиға..."}}
        ]
      }},
      "questions": [
        {{"question": "Сұрақ?", "correct": "Дұрыс жауап", "wrong": ["Қате 1", "Қате 2", "Қате 3"], "explanation": "Түсіндірме"}}
      ]
    }}
  ]
}}

МАТЕРИАЛ:
{material}

JSON:"""
        else:
            prompt = f"""Сен - оқу AI. Тек берілген материалды пайдалан.
{lang_instruction}

ТАПСЫРМА: Материалды 2-4 бөлімге бөл. Әр бөлімге:
- content: оқу материалы (type: text/list/table)
- questions: 3 сұрақ

JSON:
{{
  "plan": [
    {{
      "title": "Бөлім атауы",
      "content": {{
        "type": "text",
        "data": "Оқу материалы мәтіні..."
      }},
      "questions": [
        {{"question": "Сұрақ?", "correct": "Дұрыс жауап", "wrong": ["Қате 1", "Қате 2", "Қате 3"], "explanation": "Түсіндірме"}}
      ]
    }}
  ]
}}

МАТЕРИАЛ:
{material}

JSON:"""

        try:
            response_text = await asyncio.to_thread(
                self._generate_with_retry,
                prompt,
                self.system_prompt,
            )
            return self._parse_json_response(response_text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSON форматында қате: {str(e)}")
        except Exception as e:
            raise Exception(f"OpenAI API қатесі: {str(e)}")

    async def generate_practice_questions(self, material: str, count: int, exclude_questions: list = None, lang: Optional[str] = None) -> dict:
        """Generate practice questions."""
        material = self._prepare_large_material(material, target_chars=MAX_MATERIAL_CHARS_DEFAULT, lang=lang)
        lang_instruction = self._language_instruction(lang)

        exclude_text = ""
        if exclude_questions:
            exclude_text = f"\n\nБҰЛ СҰРАҚТАРДЫ ҚАЙТАЛАМА:\n" + "\n".join(exclude_questions)

        prompt = f"""{lang_instruction}

ТАПСЫРМА: Материал бойынша {count} практика сұрақтарын құр.

ФОРМАТ (JSON):
{{
  "questions": [
    {{
      "id": 1,
      "question": "Сұрақ мәтіні",
      "correct": "Дұрыс жауап",
      "wrong": ["Қате жауап 1", "Қате жауап 2", "Қате жауап 3"],
      "explanation": "Түсіндірме (неге бұл жауап дұрыс)"
    }}
  ]
}}

ЕРЕЖЕЛЕР:
- Нақты {count} сұрақ құр
- Қате жауаптар шатастыратын болсын (ЕНТ стилінде)
- Қате жауаптардың ұзындығы дұрыс жауаппен шамалас болсын
- Әр сұраққа түсіндірме жаз
- JSON ішіндегі question, correct, wrong, explanation өрістерінің бәрі де тек сұралған тілде болсын
- Ешбір сұрақта немесе жауап нұсқасында басқа тілге ауыспа
{exclude_text}

МАТЕРИАЛ:
{material}

JSON жауап:"""

        try:
            response_text = await asyncio.to_thread(
                self._generate_with_retry,
                prompt,
                self.system_prompt,
            )
            return self._parse_json_response(response_text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSON форматында қате: {str(e)}")
        except Exception as e:
            raise Exception(f"OpenAI API қатесі: {str(e)}")

    async def generate_realtest_questions(self, material: str, count: int, lang: Optional[str] = None) -> dict:
        """Generate real test questions."""
        material = self._prepare_large_material(material, target_chars=MAX_MATERIAL_CHARS_DEFAULT, lang=lang)
        lang_instruction = self._language_instruction(lang)

        prompt = f"""{lang_instruction}

ТАПСЫРМА: Материал бойынша {count} тест сұрақтарын құр (нақты ЕНТ форматында).

ФОРМАТ (JSON):
{{
  "questions": [
    {{
      "id": 1,
      "question": "Сұрақ мәтіні",
      "correct": "Дұрыс жауап",
      "wrong": ["Қате жауап 1", "Қате жауап 2", "Қате жауап 3"]
    }}
  ]
}}

ЕРЕЖЕЛЕР:
- Нақты {count} сұрақ құр
- Сұрақтар ЕНТ деңгейінде болсын (күрделі)
- Қате жауаптар өте шатастыратын болсын
- Қате жауаптардың ұзындығы дұрыс жауаппен шамалас болсын
- Тек материалдағы фактілерді пайдалан
- JSON ішіндегі question, correct, wrong өрістерінің бәрі де тек сұралған тілде болсын
- Басқа тілмен араластырма

МАТЕРИАЛ:
{material}

JSON жауап:"""

        try:
            response_text = await asyncio.to_thread(
                self._generate_with_retry,
                prompt,
                self.system_prompt,
            )
            return self._parse_json_response(response_text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSON форматында қате: {str(e)}")
        except Exception as e:
            raise Exception(f"OpenAI API қатесі: {str(e)}")

    def chat(self, data: dict) -> dict:
        """Handle assistant chat messages."""
        message = data.get("message", "")
        lang = data.get("lang", data.get("language", "kk"))
        chat_history = data.get("chat_history", data.get("history", []))
        user_profile = data.get("user_profile", data.get("profile"))
        page_context = data.get("page_context")
        experience_summary = data.get("experience_summary", data.get("user_diagnostics"))
        knowledge_matches = data.get("knowledge_matches", data.get("knowledge_sources", []))
        if not isinstance(knowledge_matches, list):
            knowledge_matches = []
        knowledge_matches = knowledge_matches[:3]
        active_material_excerpt = data.get("active_material_excerpt", data.get("material_text", ""))
        user_id, access_token = self._resolve_authenticated_user_id(
            requested_user_id=str(data.get("user_id") or "").strip(),
            access_token=str(data.get("_access_token") or "").strip() or None,
        )
        session_id = str(data.get("session_id") or "").strip() or None
        self._close_stale_sessions(user_id=user_id, access_token=access_token)
        existing_session = self._load_session_record(
            user_id=user_id,
            access_token=access_token,
            session_id=session_id,
        )
        chat_memory_signals = self._collect_chat_memory_signals(
            message=message,
            lang=lang,
            user_profile=user_profile if isinstance(user_profile, dict) else {},
        )
        session_memory_summary = self._build_session_summary(
            existing_summary=str((existing_session or {}).get("summary") or ""),
            signals=chat_memory_signals,
            user_profile=user_profile if isinstance(user_profile, dict) else {},
            page_context=page_context if isinstance(page_context, dict) else {},
        )

        response = self.generate_assistant_response(
            message=message,
            lang=lang,
            chat_history=chat_history,
            user_profile=user_profile,
            page_context=page_context,
            experience_summary=experience_summary,
            knowledge_matches=knowledge_matches,
            active_material_excerpt=active_material_excerpt,
            user_id=user_id,
            access_token=access_token,
            persisted_session_summary=session_memory_summary,
            chat_memory_signals=chat_memory_signals,
        )
        final_session_summary = self._build_session_summary(
            existing_summary=str((existing_session or {}).get("summary") or ""),
            signals=chat_memory_signals,
            user_profile=user_profile if isinstance(user_profile, dict) else {},
            page_context=page_context if isinstance(page_context, dict) else {},
            assistant_payload=response,
        )
        persisted_session = self._persist_chat_exchange(
            user_id=user_id,
            access_token=access_token,
            session_id=session_id,
            user_message=message,
            assistant_payload=response,
            existing_session=existing_session,
            session_summary=final_session_summary,
        )
        if persisted_session:
            response["session"] = persisted_session

        user_state_patch = {
            "preferred_language": self._normalize_lang(lang),
            "preferred_difficulty": str(chat_memory_signals.get("preferred_difficulty") or "").strip(),
            "response_style": str(chat_memory_signals.get("response_style") or "").strip(),
            "learning_goals": chat_memory_signals.get("learning_goals") or [],
            "weak_topics": chat_memory_signals.get("weak_topics") or [],
            "strong_topics": chat_memory_signals.get("strong_topics") or [],
            "last_active_route": str((page_context or {}).get("route") or ""),
            "recent_routes": [str((page_context or {}).get("route") or "")],
            "total_events_delta": 1,
        }
        self._upsert_user_state(
            user_id=user_id,
            access_token=access_token,
            patch=user_state_patch,
        )
        self._remember_chat_memory(
            user_id=user_id,
            access_token=access_token,
            session_id=(persisted_session or {}).get("id") or session_id,
            signals=chat_memory_signals,
            user_profile=user_profile if isinstance(user_profile, dict) else {},
        )

        # Always include language preference in response
        response["language"] = self._normalize_lang(lang)
        response["language_name"] = {"ru": "Russian", "en": "English", "kk": "Kazakh", "hi": "Hindi"}.get(self._normalize_lang(lang), "Kazakh")

        return {key: value for key, value in response.items() if not str(key).startswith("_")}

    async def generate_quiz(self, data: dict) -> dict:
        """Generate quiz questions."""
        material = str(data.get("material") or data.get("material_text") or "").strip()
        has_grounding_material = bool(material)
        count = int(data.get("count") or 10)
        quiz_type = str(data.get("mode") or data.get("type") or "practice").strip().lower()
        lang = self._normalize_lang(data.get("lang", data.get("language", "kk")))
        exclude_questions = data.get("exclude_questions", [])
        source_type = str(data.get("source_type") or "material").strip()
        source_id = str(data.get("source_id") or "").strip()
        source_title = str(data.get("source_title") or "").strip()
        user_profile = data.get("user_profile") if isinstance(data.get("user_profile"), dict) else {}
        user_id, access_token = self._resolve_authenticated_user_id(
            requested_user_id=str(data.get("user_id") or "").strip(),
            access_token=str(data.get("_access_token") or "").strip() or None,
        )
        session_id = str(data.get("session_id") or "").strip() or None
        page_context = data.get("page_context") if isinstance(data.get("page_context"), dict) else {}
        assistant_summary = data.get("assistant_summary") if isinstance(data.get("assistant_summary"), dict) else {}
        assistant_prompt = str(data.get("assistant_prompt") or data.get("message") or source_title or "").strip()
        lang = self._resolve_requested_output_language(assistant_prompt or source_title or material[:160], lang)

        count = max(5, min(30, count))
        if source_type not in {"material", "historical_figure"}:
            source_type = "material"
        if quiz_type not in {"practice", "realtest"}:
            quiz_type = "practice"

        if not material and source_type == "material" and source_id and self.supabase.available:
            try:
                filters = {"id": f"eq.{source_id}", "limit": "1"}
                if user_id:
                    filters["user_id"] = f"eq.{user_id}"
                materials_rows = self.supabase.select(
                    "materials",
                    params=filters,
                    auth_token=access_token,
                    use_service_role=not bool(access_token),
                )
                if materials_rows and isinstance(materials_rows[0], dict):
                    row = materials_rows[0]
                    material = str(row.get("content") or "").strip()
                    has_grounding_material = bool(material)
                    source_title = source_title or str(row.get("title") or "").strip()
                if not material:
                    tests_rows = self.supabase.select(
                        "tests",
                        params=filters,
                        auth_token=access_token,
                        use_service_role=not bool(access_token),
                    )
                    if tests_rows and isinstance(tests_rows[0], dict):
                        row = tests_rows[0]
                        content = str(row.get("content") or "").strip()
                        questions = row.get("questions")
                        question_blob = json.dumps(questions, ensure_ascii=False) if questions is not None else ""
                        material = (content + "\n\n" + question_blob).strip()
                        has_grounding_material = bool(material)
                        source_title = source_title or str(row.get("title") or "").strip()
            except SupabaseServiceError:
                pass

        if not source_title:
            subject_combo = str(user_profile.get("subject_combination") or "").strip()
            subject1 = str(user_profile.get("subject1") or "").strip()
            subject2 = str(user_profile.get("subject2") or "").strip()
            if subject_combo:
                source_title = subject_combo
            elif subject1 or subject2:
                source_title = " / ".join(item for item in [subject1, subject2] if item)

        if not material and source_title:
            material = f"Topic: {source_title}\nCreate a focused {quiz_type} quiz for ENT preparation."

        if not material:
            source_title = source_title or self._assistant_localized(
                lang,
                "ENT aralas taqyryptary",
                "Смешанные темы ЕНТ",
                "Mixed ENT topics",
            )
            material = f"Topic: {source_title}\nCreate a focused {quiz_type} quiz for ENT preparation."

        assistant_context = self._build_quiz_assistant_context(
            message=assistant_prompt or source_title or material[:160],
            user_id=user_id,
            access_token=access_token,
            session_id=session_id,
            user_profile=user_profile,
            page_context=page_context,
            assistant_summary=assistant_summary,
            lang=lang,
            requested_language=lang,
            source_title=source_title,
            source_type=source_type,
            quiz_type=quiz_type,
            count=count,
        )
        if not has_grounding_material:
            logger.info(
                "assistant.generate_quiz using direct OpenAI for synthetic topic quiz. mode=%s title=%s",
                quiz_type,
                source_title,
            )
            if quiz_type == "realtest":
                result = await self.generate_realtest_questions(material, count, lang)
            else:
                result = await self.generate_practice_questions(material, count, exclude_questions, lang)
        else:
            gemini = get_gemini_service()
            try:
                if quiz_type == "realtest":
                    result = await gemini.generate_realtest_questions(
                        material,
                        count,
                        lang,
                        assistant_context=assistant_context,
                    )
                else:
                    result = await gemini.generate_practice_questions(
                        material,
                        count,
                        exclude_questions,
                        lang,
                        assistant_context=assistant_context,
                    )
            except Exception as exc:
                logger.warning(
                    "Gemini quiz generation failed; falling back to OpenAI. mode=%s title=%s error=%s",
                    quiz_type,
                    source_title,
                    exc,
                )
                if quiz_type == "realtest":
                    result = await self.generate_realtest_questions(material, count, lang)
                else:
                    result = await self.generate_practice_questions(material, count, exclude_questions, lang)

        if isinstance(result, dict):
            result["source"] = {
                "type": source_type,
                "id": source_id,
                "title": source_title,
            }
            result["assistant_context"] = {
                "weak_topics": assistant_context.get("weak_topics") or [],
                "learning_goals": assistant_context.get("learning_goals") or [],
                "preferred_difficulty": assistant_context.get("preferred_difficulty") or "medium",
                "session_summary": assistant_context.get("session_summary") or "",
                "quiz_performance": assistant_context.get("quiz_performance") or {},
            }
        return result

    def record_experience(self, data: dict) -> dict:
        """Record user experience/action."""
        user_id, access_token = self._resolve_authenticated_user_id(
            requested_user_id=str(data.get("user_id") or "").strip(),
            access_token=str(data.get("_access_token") or "").strip() or None,
        )
        if not user_id:
            return {"success": False, "error": "authenticated user is required"}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        event_type = str(data.get("event_type") or "").strip() or "assistant_event"
        route = str(payload.get("route") or data.get("route") or "").strip()
        topic = str(payload.get("topic") or data.get("topic") or "").strip()
        source_type = str(payload.get("source_type") or data.get("source_type") or "").strip()
        source_id = str(payload.get("source_id") or data.get("source_id") or "").strip()
        quiz_mode = str(payload.get("mode") or payload.get("quiz_mode") or "").strip().lower()
        language = str(payload.get("language") or data.get("language") or data.get("lang") or "").strip()
        preferred_difficulty = str(payload.get("preferred_difficulty") or "").strip()
        action_name = str(payload.get("action") or data.get("action") or "").strip().lower()
        assistant_origin = bool(payload.get("assistant_origin")) or action_name == "assistant_quiz_result" or event_type == "assistant_quiz_result"

        percent_raw = payload.get("percent") if payload.get("percent") is not None else data.get("percent")
        try:
            percent = int(float(percent_raw)) if percent_raw is not None else None
        except Exception:
            percent = None

        correct_raw = payload.get("correct") if payload.get("correct") is not None else data.get("correct")
        total_raw = payload.get("total") if payload.get("total") is not None else data.get("total")
        try:
            correct = int(float(correct_raw)) if correct_raw is not None else None
        except Exception:
            correct = None
        try:
            total = int(float(total_raw)) if total_raw is not None else None
        except Exception:
            total = None

        session_id = str(data.get("session_id") or "").strip() or None
        page_context = payload.get("page_context") if isinstance(payload.get("page_context"), dict) else {}
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        normalized_attempt_items = self._normalize_quiz_attempt_items(
            details=details,
            fallback_topic=topic,
        )
        attempt_analysis = self._build_quiz_attempt_analysis(
            normalized_items=normalized_attempt_items,
            fallback_topic=topic,
        )
        event_name = str(payload.get("event_name") or payload.get("action") or event_type).strip()[:120]
        metadata = dict(payload) if payload else {}
        metadata.setdefault("assistant_origin", assistant_origin)
        metadata.setdefault("action", action_name or event_name)
        if event_type in {"quiz_result", "assistant_quiz_result"}:
            metadata.setdefault("focus_topics", list(attempt_analysis.get("focus_topics") or [])[:4])
            metadata.setdefault("mistake_count", int(attempt_analysis.get("mistake_count") or 0))
            metadata.setdefault("skipped_count", int(attempt_analysis.get("skipped_count") or 0))
            metadata.setdefault("mistake_examples", list(attempt_analysis.get("mistake_examples") or [])[:3])

        row = {
            "user_id": user_id,
            "session_id": session_id if _looks_like_uuid(str(session_id or "")) else None,
            "event_type": event_type,
            "event_name": event_name,
            "category": str(payload.get("category") or "").strip() or None,
            "action": action_name or None,
            "route": route or None,
            "topic": topic or None,
            "source_type": source_type or None,
            "source_id": source_id or None,
            "correct": correct,
            "total": total,
            "percent": percent,
            "message": payload.get("message") or data.get("message"),
            "page_context": page_context,
            "details": details,
            "metadata": metadata,
            "duration_ms": payload.get("duration_ms"),
            "severity": str(payload.get("severity") or "").strip() or None,
            "confidence": payload.get("confidence"),
            "client_ts": payload.get("client_ts"),
        }
        try:
            inserted_rows: list[dict[str, Any]] = []
            if self.supabase.available:
                inserted_rows = self.supabase.insert(
                    "assistant_events",
                    row,
                    auth_token=access_token,
                    use_service_role=not bool(access_token),
                )

            patch: dict[str, Any] = {
                "total_events_delta": 1,
                "last_active_route": route,
                "recent_routes": [route] if route else [],
            }
            if language:
                patch["preferred_language"] = self._normalize_lang(language)
            if preferred_difficulty:
                patch["preferred_difficulty"] = preferred_difficulty
            goals = payload.get("learning_goals")
            if isinstance(goals, list):
                patch["learning_goals"] = goals

            if event_type in {"quiz_result", "assistant_quiz_result"}:
                patch["total_quizzes_delta"] = 1
                focus_topics = list(attempt_analysis.get("focus_topics") or [])[:4]
                if percent is not None:
                    patch["quiz_percent"] = percent
                    if percent >= 70:
                        patch["successful_quizzes_delta"] = 1
                    if percent < 60:
                        patch["weak_topics"] = focus_topics or ([topic] if topic else [])
                    elif percent >= 85:
                        patch["strong_topics"] = focus_topics[:2] or ([topic] if topic else [])
                elif focus_topics:
                    patch["weak_topics"] = focus_topics

            self._upsert_user_state(
                user_id=user_id,
                access_token=access_token,
                patch=patch,
            )

            inserted_event_id = ""
            if inserted_rows and isinstance(inserted_rows[0], dict):
                inserted_event_id = str(inserted_rows[0].get("id") or "").strip()

            if event_type in {"quiz_result", "assistant_quiz_result"}:
                attempt_id = self._persist_quiz_attempt(
                    user_id=user_id,
                    access_token=access_token,
                    session_id=session_id,
                    assistant_event_id=inserted_event_id or None,
                    event_type=event_type,
                    route=route,
                    quiz_mode=quiz_mode,
                    topic=topic,
                    source_type=source_type,
                    source_id=source_id,
                    language=language,
                    page_context=page_context,
                    details=details,
                    payload=payload,
                    correct=correct,
                    total=total,
                    percent=percent,
                    assistant_origin=assistant_origin,
                )
                if attempt_id:
                    self._sync_user_stats_from_quiz_attempts(
                        user_id=user_id,
                        access_token=access_token,
                    )

            if route:
                self._upsert_user_fact(
                    user_id=user_id,
                    access_token=access_token,
                    fact_key="last_route",
                    fact_value=route,
                    confidence=0.7,
                    source_event_id=inserted_event_id or None,
                    source_session_id=session_id,
                )
            if language:
                self._upsert_user_fact(
                    user_id=user_id,
                    access_token=access_token,
                    fact_key="preferred_language",
                    fact_value=self._normalize_lang(language),
                    confidence=0.85,
                    source_event_id=inserted_event_id or None,
                    source_session_id=session_id,
                )
            if event_type in {"quiz_result", "assistant_quiz_result"} and session_id and _looks_like_uuid(str(session_id)):
                session_record = self._load_session_record(
                    user_id=user_id,
                    access_token=access_token,
                    session_id=session_id,
                )
                if session_record:
                    quiz_line = self._assistant_localized(
                        language or "kk",
                        f"Quiz result: {topic or 'practice'} - {percent if percent is not None else 0}%",
                        f"Результат теста: {topic or 'practice'} - {percent if percent is not None else 0}%",
                        f"Quiz result: {topic or 'practice'} - {percent if percent is not None else 0}%",
                    )
                    try:
                        self.supabase.update(
                            "assistant_sessions",
                            {"id": f"eq.{session_id}", "user_id": f"eq.{user_id}"},
                            {
                                "summary": self._append_session_summary_line(
                                    str(session_record.get("summary") or ""),
                                    quiz_line,
                                )
                            },
                            auth_token=access_token,
                            use_service_role=not bool(access_token),
                        )
                    except SupabaseServiceError:
                        pass
                self._remember_quiz_result(
                    user_id=user_id,
                    access_token=access_token,
                    session_id=session_id,
                    topic=topic,
                    percent=percent,
                    mode=quiz_mode,
                    event_id=inserted_event_id or None,
                )
            if topic and percent is not None:
                for focus_topic in list(attempt_analysis.get("focus_topics") or [])[:3]:
                    self._upsert_user_fact(
                        user_id=user_id,
                        access_token=access_token,
                        fact_key=f"weak_topic:{_normalize_free_text(focus_topic)}",
                        fact_value=f"Needs review on {focus_topic}",
                        confidence=0.84,
                        source_event_id=inserted_event_id or None,
                        source_session_id=session_id,
                    )
                if percent < 60:
                    self._upsert_user_fact(
                        user_id=user_id,
                        access_token=access_token,
                        fact_key=f"weak_topic:{_normalize_free_text(topic)}",
                        fact_value=f"Needs review on {topic}",
                        confidence=0.82,
                        source_event_id=inserted_event_id or None,
                        source_session_id=session_id,
                    )
                elif percent >= 85:
                    self._upsert_user_fact(
                        user_id=user_id,
                        access_token=access_token,
                        fact_key=f"strong_topic:{_normalize_free_text(topic)}",
                        fact_value=f"Confident on {topic}",
                        confidence=0.78,
                        source_event_id=inserted_event_id or None,
                        source_session_id=session_id,
                    )
            return {"success": True, "message": "Experience recorded"}
        except SupabaseServiceError as exc:
            logger.warning("Failed to record assistant event: %s", exc)
            return {"success": False, "error": str(exc)}

    def list_sessions(self, data: dict) -> dict:
        """List user chat sessions."""
        user_id, access_token = self._resolve_authenticated_user_id(
            requested_user_id=str(data.get("user_id") or "").strip(),
            access_token=str(data.get("_access_token") or "").strip() or None,
        )
        if not user_id:
            return {"sessions": [], "total": 0}
        self._close_stale_sessions(user_id=user_id, access_token=access_token)
        search_query = _normalize_free_text(
            data.get("q") or data.get("query") or data.get("search")
        )
        try:
            limit = int(data.get("limit") or 50)
        except Exception:
            limit = 50
        limit = min(max(limit, 1), 120)
        try:
            sessions = self.supabase.select(
                "assistant_sessions",
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "last_message_at.desc.nullslast,updated_at.desc",
                    "limit": str(limit),
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            ) if self.supabase.available else []
            hide_raw = str(data.get("hide_raw_sessions", "true")).strip().lower() not in {"0", "false", "no"}
            if hide_raw:
                filtered: list[dict[str, Any]] = []
                for session in sessions:
                    turns = int((session or {}).get("conversation_turns") or 0)
                    quality = int((session or {}).get("quality_score") or 0)
                    status = str((session or {}).get("status") or "")
                    if turns <= 1 and quality < 30 and status in {"active", "abandoned"}:
                        continue
                    filtered.append(session)
                sessions = filtered
            if search_query:
                searched: list[dict[str, Any]] = []
                for session in sessions:
                    haystack = _normalize_free_text(
                        " ".join(
                            [
                                str((session or {}).get("title") or ""),
                                str((session or {}).get("last_message_preview") or ""),
                                str((session or {}).get("summary") or ""),
                            ]
                        )
                    )
                    if search_query in haystack:
                        searched.append(session)
                sessions = searched
            return {"sessions": sessions, "total": len(sessions)}
        except SupabaseServiceError as exc:
            logger.warning("Failed to list assistant sessions: %s", exc)
            return {"sessions": [], "total": 0}

    def get_session(self, data: dict) -> dict:
        """Get a specific chat session."""
        session_id = str(data.get("session_id") or "").strip()
        user_id, access_token = self._resolve_authenticated_user_id(
            requested_user_id=str(data.get("user_id") or "").strip(),
            access_token=str(data.get("_access_token") or "").strip() or None,
        )
        if not session_id:
            return {"session_id": session_id, "messages": []}
        if not user_id:
            return {"session_id": session_id, "messages": []}
        try:
            params = {"session_id": f"eq.{session_id}", "order": "created_at.asc"}
            if user_id:
                params["user_id"] = f"eq.{user_id}"
            messages = self.supabase.select(
                "assistant_messages",
                params=params,
                auth_token=access_token,
                use_service_role=not bool(access_token),
            ) if self.supabase.available else []
            return {"session_id": session_id, "messages": messages}
        except SupabaseServiceError as exc:
            logger.warning("Failed to get assistant session %s: %s", session_id, exc)
            return {"session_id": session_id, "messages": []}

    def rename_session(self, data: dict) -> dict:
        """Rename a specific chat session."""
        session_id = str(data.get("session_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        raw_title = str(data.get("title") or data.get("new_title") or "").strip()
        new_title = re.sub(r"\s+", " ", raw_title).strip()[:120]

        if not user_id:
            return {"success": False, "error": "user_id is required"}
        if not _looks_like_uuid(session_id):
            return {"success": False, "error": "invalid session_id"}
        if not new_title:
            return {"success": False, "error": "title is required"}

        try:
            updated = self.supabase.update(
                "assistant_sessions",
                {"id": f"eq.{session_id}", "user_id": f"eq.{user_id}"},
                {"title": new_title},
                auth_token=access_token,
                use_service_role=not bool(access_token),
            ) if self.supabase.available else []

            if not updated:
                return {"success": False, "error": "session not found"}

            return {"success": True, "session": updated[0]}
        except SupabaseServiceError as exc:
            logger.warning("Failed to rename assistant session %s: %s", session_id, exc)
            return {"success": False, "error": str(exc)}

    def delete_session(self, data: dict) -> dict:
        """Delete chat session and related messages for a user."""
        session_id = str(data.get("session_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None

        if not user_id:
            return {"success": False, "error": "user_id is required"}
        if not _looks_like_uuid(session_id):
            return {"success": False, "error": "invalid session_id"}

        try:
            deleted = self.supabase.delete(
                "assistant_sessions",
                {"id": f"eq.{session_id}", "user_id": f"eq.{user_id}"},
                auth_token=access_token,
                use_service_role=not bool(access_token),
            ) if self.supabase.available else []

            if not deleted:
                return {"success": False, "error": "session not found"}

            return {"success": True, "session_id": session_id}
        except SupabaseServiceError as exc:
            logger.warning("Failed to delete assistant session %s: %s", session_id, exc)
            return {"success": False, "error": str(exc)}

@lru_cache(maxsize=1)
def get_openai_service() -> OpenAIService:
    """Get or create OpenAI service instance."""
    return OpenAIService()


def get_assistant_service(resolve_material=None) -> OpenAIService:
    """Get OpenAI service instance for assistant use."""
    return get_openai_service()

# Временно не используется, но может пригодиться для отладки и экспериментов с ассистентом в будущем.
