"""
OpenAI GPT API Service
Handles all AI generation for learning content, questions, and tests
"""

import asyncio
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
MAX_ASSISTANT_RECENT_ROUTES = 8
MAX_ASSISTANT_FACTS = 12
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
    source_title: Optional[str] = Field(None, description="Human-readable quiz topic/title")
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
        weak_topics = _merge_text_lists(
            (experience_summary or {}).get("weak_topics") or [],
            persisted_state.get("weak_topics") or [],
            limit=10,
        )
        strong_topics = _merge_text_lists(
            persisted_state.get("strong_topics") or [],
            (experience_summary or {}).get("strong_topics") or [],
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
                "total_quizzes": int(persisted_state.get("total_quizzes") or 0),
                "successful_quizzes": int(persisted_state.get("successful_quizzes") or 0),
                "average_quiz_percent": float(persisted_state.get("average_quiz_percent") or 0),
            },
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
            "make test", "create test", "generate test", "practice test",
            "\u0441\u0434\u0435\u043b\u0430\u0439 \u0442\u0435\u0441\u0442", "\u0441\u043e\u0437\u0434\u0430\u0439 \u0442\u0435\u0441\u0442", "\u043d\u0430\u0447\u043d\u0438 \u0442\u0435\u0441\u0442", "\u0442\u0435\u0441\u0442", "\u043a\u0432\u0438\u0437",
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
            r"\b(?:сделай|создай|запусти|начни|составь|generate|create|make|start|build|sdelai|sdelay|sozdai|zapusti|nachni|sostav|jasa|kur|basta|daiynda)\b",
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
        asks_quiz = any(token in text for token in quiz_tokens)
        is_command = any(token in text for token in command_tokens)
        if not asks_quiz or not is_command:
            return None

        source_id, source_type = self._pick_quiz_source(
            knowledge_matches=knowledge_matches,
            page_context=page_context,
        )
        question_count = self._extract_requested_question_count(message)
        source_title = self._fallback_quiz_title(message=message, user_profile=user_profile, lang=lang)
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
        fallback_used = bool(assistant_payload.get("_fallback_used"))
        error_code = str(assistant_payload.get("_error_code") or "").strip()
        model_used = str(assistant_payload.get("_model_used") or self.last_model_used or "").strip()
        latency_ms_raw = assistant_payload.get("_latency_ms")
        latency_ms = int(latency_ms_raw) if isinstance(latency_ms_raw, (int, float, str)) and str(latency_ms_raw).strip().isdigit() else None
        route = self._extract_route_from_actions(assistant_actions)
        turn_id = str(uuid.uuid4())

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

        deterministic_quiz_action = self._deterministic_quiz_action(
            message=message,
            lang=lang,
            knowledge_matches=knowledge_matches or [],
            page_context=page_context,
            user_profile=user_profile,
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
            payload = self._build_final_json(deterministic_response, lang)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            return self._attach_internal_meta(
                payload,
                fallback_used=False,
                model_used=self.last_model_used,
                latency_ms=latency_ms,
            )
        
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

RAG context summary:
{context_chunk}

Trap points to avoid:
{json.dumps(trap_points, ensure_ascii=False)}

Student profile snapshot:
{json.dumps(student_profile_snapshot, ensure_ascii=False)}

Current page context:
{json.dumps(page_context or {}, ensure_ascii=False)}

Respond with ANSWER only. Format: {{
  "message": "Your direct answer here - DO NOT suggest library",
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
            result = self._build_final_json(parsed, lang)
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
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        session_id = str(data.get("session_id") or "").strip() or None
        self._close_stale_sessions(user_id=user_id, access_token=access_token)

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

        self._upsert_user_state(
            user_id=user_id,
            access_token=access_token,
            patch={
                "preferred_language": self._normalize_lang(lang),
                "last_active_route": str((page_context or {}).get("route") or ""),
                "recent_routes": [str((page_context or {}).get("route") or "")],
                "total_events_delta": 1,
            },
        )

        # Always include language preference in response
        response["language"] = self._normalize_lang(lang)
        response["language_name"] = {"ru": "Russian", "en": "English", "kk": "Kazakh"}.get(self._normalize_lang(lang), "Kazakh")

        return {key: value for key, value in response.items() if not str(key).startswith("_")}

    async def generate_quiz(self, data: dict) -> dict:
        """Generate quiz questions."""
        material = str(data.get("material") or data.get("material_text") or "").strip()
        count = int(data.get("count") or 10)
        quiz_type = str(data.get("mode") or data.get("type") or "practice").strip().lower()
        lang = data.get("lang", data.get("language", "kk"))
        exclude_questions = data.get("exclude_questions", [])
        source_type = str(data.get("source_type") or "material").strip()
        source_id = str(data.get("source_id") or "").strip()
        source_title = str(data.get("source_title") or "").strip()
        user_profile = data.get("user_profile") if isinstance(data.get("user_profile"), dict) else {}
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None

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
        return result

    def record_experience(self, data: dict) -> dict:
        """Record user experience/action."""
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
        if not user_id:
            return {"success": False, "error": "user_id is required"}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        event_type = str(data.get("event_type") or "").strip() or "assistant_event"
        route = str(payload.get("route") or data.get("route") or "").strip()
        topic = str(payload.get("topic") or data.get("topic") or "").strip()
        source_type = str(payload.get("source_type") or data.get("source_type") or "").strip()
        source_id = str(payload.get("source_id") or data.get("source_id") or "").strip()
        language = str(payload.get("language") or data.get("language") or data.get("lang") or "").strip()
        preferred_difficulty = str(payload.get("preferred_difficulty") or "").strip()

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
        event_name = str(payload.get("event_name") or payload.get("action") or event_type).strip()[:120]

        row = {
            "user_id": user_id,
            "session_id": session_id if _looks_like_uuid(str(session_id or "")) else None,
            "event_type": event_type,
            "event_name": event_name,
            "category": str(payload.get("category") or "").strip() or None,
            "action": payload.get("action") or data.get("action"),
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
            "metadata": payload if payload else {},
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
                if percent is not None:
                    patch["quiz_percent"] = percent
                    if percent >= 70:
                        patch["successful_quizzes_delta"] = 1
                    if topic:
                        if percent < 60:
                            patch["weak_topics"] = [topic]
                        elif percent >= 85:
                            patch["strong_topics"] = [topic]

            self._upsert_user_state(
                user_id=user_id,
                access_token=access_token,
                patch=patch,
            )

            inserted_event_id = ""
            if inserted_rows and isinstance(inserted_rows[0], dict):
                inserted_event_id = str(inserted_rows[0].get("id") or "").strip()

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
            if topic and percent is not None:
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
        user_id = str(data.get("user_id") or "").strip()
        access_token = str(data.get("_access_token") or "").strip() or None
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
