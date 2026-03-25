"""
OpenAI GPT API Service
Handles all AI generation for learning content, questions, and tests
"""

import json
import logging
import os
import math
import re
import time
from functools import lru_cache
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled at runtime
    OpenAI = None
from .supabase_service import SupabaseService, SupabaseServiceError
from .pdf_knowledge_service import search_pdf_knowledge
from .language_detector import override_language_if_detected


logger = logging.getLogger(__name__)

MAX_ACTIVE_MATERIAL_EXCERPT_CHARS = 3000
MAX_MATERIAL_CHARS_HISTORY = 70_000
MAX_MATERIAL_CHARS_DEFAULT = 50_000
MAX_SUMMARY_CHUNKS = 16
MAX_ASSISTANT_HISTORY_MESSAGES = 30
MAX_ASSISTANT_SHORT_TERM_MESSAGES = 10
MAX_ASSISTANT_MESSAGE_CHARS = 4000
MAX_JSON_LOG_PREVIEW_CHARS = 700
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
DEFAULT_OPENAI_SDK_RETRIES = 3
MAX_ASSISTANT_RECENT_ERRORS = 10
MAX_ASSISTANT_ACTIONS = 3
MAX_ASSISTANT_CITATIONS = 3
TOOL_RETRY_ATTEMPTS = 3
ASSISTANT_MAX_PIPELINE_SECONDS = 28.0
ASSISTANT_FAST_MODE_DEFAULT = "true"

ASSISTANT_ROUTES = [
    "ai_learn",
    "ai_practice",
    "library",
    "ai_realtest",
    "profile",
]

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
    question_count: Optional[int] = Field(None, description="Question count for quiz")


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
            open_braces = text.count('{') - text.count('}')
            open_brackets = text.count('[') - text.count(']')
            
            if open_braces > 0 or open_brackets > 0:
                quote_count = text.count('"') 
                if quote_count % 2 != 0:
                    last_complete = text.rfind('},')
                    if last_complete == -1:
                        last_complete = text.rfind('}]')
                    if last_complete > 0:
                        text = text[:last_complete+1]
                
                open_braces = text.count('{') - text.count('}')
                open_brackets = text.count('[') - text.count(']')
                
                text += ']' * open_brackets
                text += '}' * open_braces
        
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
                "Сделай квиз по моим материалам",
                "Что мне учить дальше?",
            ]
        if self._normalize_lang(lang) == "en":
            return [
                "Open my library",
                "Create a quiz on my materials",
                "What should I study next?",
            ]
        return [
            "Кітапханамды аш",
            "Материалдарым бойынша тест жаса",
            "Маған келесі не оқу керек?",
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
    ) -> dict[str, Any]:
        normalized = self._history_messages(chat_history)
        short_term_messages = normalized[-MAX_ASSISTANT_SHORT_TERM_MESSAGES:]
        older_messages = normalized[:-MAX_ASSISTANT_SHORT_TERM_MESSAGES]
        current_domain = self._infer_domain(message)

        previous_domain = "general"
        for item in reversed(short_term_messages):
            if item.get("role") != "user":
                continue
            previous_domain = self._infer_domain(str(item.get("content") or ""))
            break
        domain_shift = previous_domain != current_domain and previous_domain != "general"

        incremental_summary = ""
        if len(older_messages) >= 8:
            chunk = older_messages[-12:]
            chunk_text = "\n".join(f"{item['role']}: {item['content']}" for item in chunk)
            summary_prompt = f"""
{self._assistant_language_instruction(lang)}
Update memory summary for ENT tutor.
Keep only high-signal facts: goals, weak topics, errors, unresolved asks.
Current subject domain: {current_domain}. If old details are from another subject, compress them.

Previous summary:
{incremental_summary or '[none]'}

New chunk:
{chunk_text}

Return concise bullet summary only.
"""
            try:
                incremental_summary = self._generate_with_retry(
                    summary_prompt,
                    system_prompt="You maintain an incremental tutoring memory summary.",
                    temperature=0.1,
                    max_tokens=260,
                ).strip()
            except Exception:
                incremental_summary = ""

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
        if any(token in value for token in ("histor", "tarikh", "khan", "empire", "revolution")):
            return "history"
        if any(token in value for token in ("math", "algebra", "geometr", "equation", "integral")):
            return "math"
        if any(token in value for token in ("law", "constitution", "quqyq", "pravo")):
            return "law"
        return "general"

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
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.3,  # Lower for consistency and speed
                max_tokens=80,  # REDUCED from 150 for speed
                timeout=15.0,
            )
            
            answer = str(response.choices[0].message.content or "").strip()
            
            return {
                "message": answer,
                "reasoning": "Fast path gpt-4o response",
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
        """Determine if message is simple enough for fast lightweight response."""
        if not message or not isinstance(message, str):
            return False
        
        text = message.strip()
        # Length check: very short messages
        if len(text) > 200:
            return False
        
        # Word count check: simple messages have few words
        words = text.split()
        if len(words) > 25:
            return False
        
        # Content check: avoid complex topics that need full context
        complex_keywords = {
            "объясн", "explain", "құтықта", "анализ", "analyze", "қалтаңды", "расчет", "calculate",
            "план", "plan", "сеңдіктің", "задача", "task", "проблем", "problem", "қызмет",
            "помощь", "help", "өз", "совет", "advice", "ноқаты",
            "история", "history", "тарихы", "закон", "law", "заң", "тест", "test", "сынақ",
            "материал", "material", "ресурс", "resource", "қайнар",
        }
        
        normalized = _normalize_free_text(text)
        if any(keyword in normalized for keyword in complex_keywords):
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
                errors.append(
                    {
                        "topic": row.get("topic") or row.get("action") or "",
                        "student_answer": row.get("message") or "",
                        "correct_answer": "",
                        "percent": row.get("percent"),
                        "created_at": row.get("created_at"),
                    }
                )
            return errors[:MAX_ASSISTANT_RECENT_ERRORS]
        except SupabaseServiceError:
            return []

    def _build_student_profile_snapshot(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        user_profile: Optional[dict],
        experience_summary: Optional[dict],
    ) -> dict[str, Any]:
        weak_topics = []
        for value in (experience_summary or {}).get("weak_topics") or []:
            text = str(value).strip()
            if text and text not in weak_topics:
                weak_topics.append(text)
        return {
            "subject_combination": (user_profile or {}).get("subject_combination"),
            "weak_topics": weak_topics[:10],
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
            "quiz", "test", "practice", "start test",
            "открой", "перейди", "раздел", "профиль", "библиотек", "квиз", "тест",
            "аш", "өту", "бөлім", "профиль", "кітапхана", "тест", "квиз",
        )
        return any(token in text for token in keywords)

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
        tool_system_prompt = f"""
You are a routing agent for an ENT tutor assistant.
{self._assistant_language_instruction(lang)}
Use tool calls when navigation or quiz action is useful.
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
                if source_id and source_type in {"material", "historical_figure"}:
                    question_count = int(args.get("question_count") or 10)
                    actions.append(
                        ActionButton(
                            label=self._assistant_localized(lang, "Start quiz", "Nachat quiz", "Start quiz"),
                            type="start_quiz",
                            payload=ActionButtonPayload(
                                source_id=source_id,
                                source_type=source_type,
                                question_count=max(5, min(30, question_count)),
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
                        "mode": "practice",
                        "count": payload.question_count or 10,
                        "source_type": payload.source_type,
                        "source_id": payload.source_id,
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
                if source_type not in {"material", "historical_figure"}:
                    source_type = "material"
                if source_id:
                    result.append(
                        ActionButton(
                            label=str(action.get("label") or self._assistant_localized(lang, "Test bastau", "Nachat test", "Start quiz")),
                            type="start_quiz",
                            payload=ActionButtonPayload(
                                source_id=source_id,
                                source_type=source_type,  # type: ignore[arg-type]
                                question_count=max(5, min(30, int(action.get("count") or 10))),
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
        return self._build_final_json(fallback, lang)

    def _build_final_json(self, parsed: TutorResponse, lang: Optional[str]) -> dict[str, Any]:
        action_buttons = parsed.action_buttons[:MAX_ASSISTANT_ACTIONS]
        citations = parsed.citations[:MAX_ASSISTANT_CITATIONS]
        legacy_actions = self._map_new_to_legacy_actions(action_buttons)
        plan_steps = parsed.plan_steps if parsed.intent == "plan" else None
        return {
            "message": parsed.message,
            "intent": parsed.intent,
            "action_buttons": [self._model_dump(item) for item in action_buttons],
            "citations": [self._model_dump(item) for item in citations],
            "plan_steps": plan_steps,
            "actions": legacy_actions,
            "suggested_prompts": self._assistant_default_prompts(lang),
            "old_format_actions": legacy_actions,
        }

    def _extract_route_from_actions(self, actions: list[dict[str, Any]]) -> Optional[str]:
        for action in actions or []:
            if not isinstance(action, dict):
                continue
            if action.get("type") == "navigate" and action.get("route"):
                return str(action.get("route"))
        return None

    def _persist_chat_exchange(
        self,
        *,
        user_id: str,
        access_token: Optional[str],
        session_id: Optional[str],
        user_message: str,
        assistant_payload: dict[str, Any],
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
        route = self._extract_route_from_actions(assistant_actions)

        effective_session_id = str(session_id or "").strip()
        existing = None
        if _looks_like_uuid(effective_session_id):
            try:
                rows = self.supabase.select(
                    "assistant_sessions",
                    params={
                        "id": f"eq.{effective_session_id}",
                        "user_id": f"eq.{user_id}",
                        "limit": "1",
                    },
                    auth_token=token,
                    use_service_role=use_service_role,
                )
                existing = rows[0] if rows else None
            except SupabaseServiceError:
                existing = None

        if not existing:
            title = clean_user_message[:72] if clean_user_message else "New chat"
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
            try:
                updated = self.supabase.update(
                    "assistant_sessions",
                    {"id": f"eq.{effective_session_id}", "user_id": f"eq.{user_id}"},
                    {
                        "last_message_preview": (assistant_message or clean_user_message)[:220],
                        "last_intent": assistant_intent,
                        "last_route": route,
                        "last_message_at": now_iso,
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
        }

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
        system_prompt: str = None,
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
    ) -> dict:
        """Generate assistant response with smart routing: fast (gpt-4o) or full (gpt-4o)."""
        started_at = time.perf_counter()
        started_wall = time.time()
        lang = self._normalize_lang(lang)
        
        # Detect language from message content (override if different from frontend setting)
        lang = override_language_if_detected(message, lang)
        
        logger.info("=" * 80)
        logger.info(f"[START] User message: {str(message or '')[:100]}")
        logger.info(f"[START] Chat history length: {len(chat_history or [])}")
        logger.info(f"[START] Knowledge matches: {len(knowledge_matches or [])}")
        logger.info(f"[START] Detected language: {lang}")
        
        # Search PDF knowledge base for historical queries
        pdf_matches = []
        try:
            pdf_matches = search_pdf_knowledge(message, max_results=3)
            if pdf_matches:
                logger.info(f"📚 Found {len(pdf_matches)} PDF matches")
                knowledge_matches = (knowledge_matches or []) + pdf_matches
        except Exception as e:
            logger.warning(f"PDF search error: {str(e)}")
        
        # === FAST PATH for simple messages ===
        if self._is_simple_message(message):
            logger.info("⚡ [FAST PATH] Simple message detected")
            elapsed = time.perf_counter() - started_at
            logger.info(f"⚡ [FAST PATH] Detection took {elapsed:.2f}s")
            
            fast_response = self._generate_fast_simple_response(message, lang)
            elapsed = time.perf_counter() - started_at
            logger.info(f"⚡ [FAST PATH] Total response time: {elapsed:.2f}s")
            
            return {
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
        )
        short_term_messages = prepared_messages["short_term_messages"]
        long_term_summary = prepared_messages["incremental_summary"]
        current_domain = prepared_messages["current_domain"]
        domain_shift = bool(prepared_messages["domain_shift"])
        trap_instructions = str(prepared_messages.get("trap_instructions") or "")
        if len(chat_history or []) < 2:
            short_term_messages = []
            long_term_summary = ""
        student_profile_snapshot = self._build_student_profile_snapshot(
            user_id=str(user_id or ""),
            access_token=access_token,
            user_profile=user_profile,
            experience_summary=experience_summary,
        )

        try:
            # Hybrid Search: one-pass RAG context. If empty, fallback to model knowledge.
            rag_matches = (knowledge_matches or [])[:3]
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
            if (time.perf_counter() - started_at) > ASSISTANT_MAX_PIPELINE_SECONDS:
                return self._safe_fallback_response(lang=lang)

            system_prompt = f"""You are an ENT AI Tutor for Kazakhstan students.
{self._assistant_language_instruction(lang)}

CRITICAL INSTRUCTIONS:
1. ALWAYS search provided knowledge sources first - DO NOT say "go to library"
2. If knowledge_matches are available, USE THEM as primary source
3. User language preference: {self._normalize_lang(lang)}
4. Answer briefly and accurately
5. Never tell user to search - provide answers directly from available sources"""
            
            prompt = f"""User question: {message}

Available knowledge materials:
{json.dumps(rag_matches[:2], ensure_ascii=False) if rag_matches else "No external materials - use your knowledge"}

Respond with ANSWER only. Format: {{
  "message": "Your direct answer here - DO NOT suggest library",
  "intent": "answer",
  "action_buttons": [],
  "citations": [],
  "plan_steps": []
}}"""
            if fast_mode:
                logger.info("💪 [FULL] Fast mode enabled - reduced tokens")
                raw_response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt + "\n\nReturn strict JSON object only."},
                    ],
                    temperature=0.2,
                    max_completion_tokens=600,  # REDUCED from 800 for speed
                    timeout=20.0,  # REDUCED timeout
                )
                raw_text = str(raw_response.choices[0].message.content or "")
                raw_dict = self._parse_json_response(raw_text)
                parsed = self._coerce_tutor_response(raw_dict, lang)
            else:
                parsed_response = self.client.beta.chat.completions.parse(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
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
            return self._build_final_json(parsed, lang)
        except Exception as exc:
            logger.warning("Assistant response switched to safe fallback: %s", exc)
            logger.info("assistant.generate_assistant_response failed after %.2fs", time.time() - started_wall)
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
            response_text = self._generate_with_retry(prompt, self.system_prompt)
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
{exclude_text}

МАТЕРИАЛ:
{material}

JSON жауап:"""

        try:
            response_text = self._generate_with_retry(prompt, self.system_prompt)
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

МАТЕРИАЛ:
{material}

JSON жауап:"""

        try:
            response_text = self._generate_with_retry(prompt, self.system_prompt)
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
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        session_id = str(data.get("session_id") or "").strip() or None
        
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
        )
        persisted_session = self._persist_chat_exchange(
            user_id=user_id,
            access_token=access_token,
            session_id=session_id,
            user_message=message,
            assistant_payload=response,
        )
        if persisted_session:
            response["session"] = persisted_session
        
        # Always include language preference in response
        response["language"] = self._normalize_lang(lang)
        response["language_name"] = {"ru": "Russian", "en": "English", "kk": "Kazakh"}.get(self._normalize_lang(lang), "Kazakh")
        
        return response

    async def generate_quiz(self, data: dict) -> dict:
        """Generate quiz questions."""
        material = data.get("material", "")
        count = data.get("count", 10)
        quiz_type = data.get("type", "practice")  # practice or realtest
        lang = data.get("lang", "kk")
        exclude_questions = data.get("exclude_questions", [])
        
        if quiz_type == "realtest":
            return await self.generate_realtest_questions(material, count, lang)
        else:
            return await self.generate_practice_questions(material, count, exclude_questions, lang)

    def record_experience(self, data: dict) -> dict:
        """Record user experience/action."""
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        if not user_id:
            return {"success": False, "error": "user_id is required"}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        row = {
            "user_id": user_id,
            "session_id": data.get("session_id"),
            "event_type": str(data.get("event_type") or "").strip() or "assistant_event",
            "action": payload.get("action") or data.get("action"),
            "route": payload.get("route") or data.get("route"),
            "topic": payload.get("topic") or data.get("topic"),
            "source_type": payload.get("source_type") or data.get("source_type"),
            "source_id": payload.get("source_id") or data.get("source_id"),
            "correct": payload.get("correct"),
            "total": payload.get("total"),
            "percent": payload.get("percent"),
            "message": payload.get("message") or data.get("message"),
        }
        try:
            if self.supabase.available:
                self.supabase.insert(
                    "assistant_events",
                    row,
                    auth_token=access_token,
                    use_service_role=not bool(access_token),
                )
            return {"success": True, "message": "Experience recorded"}
        except SupabaseServiceError as exc:
            logger.warning("Failed to record assistant event: %s", exc)
            return {"success": False, "error": str(exc)}

    def list_sessions(self, data: dict) -> dict:
        """List user chat sessions."""
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        if not user_id:
            return {"sessions": [], "total": 0}
        try:
            sessions = self.supabase.select(
                "assistant_sessions",
                params={
                    "user_id": f"eq.{user_id}",
                    "order": "last_message_at.desc.nullslast,updated_at.desc",
                    "limit": "50",
                },
                auth_token=access_token,
                use_service_role=not bool(access_token),
            ) if self.supabase.available else []
            return {"sessions": sessions, "total": len(sessions)}
        except SupabaseServiceError as exc:
            logger.warning("Failed to list assistant sessions: %s", exc)
            return {"sessions": [], "total": 0}

    def get_session(self, data: dict) -> dict:
        """Get a specific chat session."""
        session_id = str(data.get("session_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        if not session_id:
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

@lru_cache(maxsize=1)
def get_openai_service() -> OpenAIService:
    """Get or create OpenAI service instance."""
    return OpenAIService()


def get_assistant_service(resolve_material=None) -> OpenAIService:
    """Get OpenAI service instance for assistant use."""
    return get_openai_service()

# Временно не используется, но может пригодиться для отладки и экспериментов с ассистентом в будущем.
