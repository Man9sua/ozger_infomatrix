"""
OpenAI GPT API Service
Handles all AI generation for learning content, questions, and tests
"""

import json
import logging
import os
import math
import re
from functools import lru_cache
from typing import Optional
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - handled at runtime
    OpenAI = None


logger = logging.getLogger(__name__)

MAX_ACTIVE_MATERIAL_EXCERPT_CHARS = 3000
MAX_MATERIAL_CHARS_HISTORY = 70_000
MAX_MATERIAL_CHARS_DEFAULT = 50_000
MAX_SUMMARY_CHUNKS = 16
MAX_ASSISTANT_HISTORY_MESSAGES = 6
MAX_ASSISTANT_MESSAGE_CHARS = 4000
MAX_JSON_LOG_PREVIEW_CHARS = 700
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60.0
DEFAULT_OPENAI_SDK_RETRIES = 3


def _normalize_free_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


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
                float(os.getenv("OPENAI_TIMEOUT_SECONDS", str(DEFAULT_OPENAI_TIMEOUT_SECONDS))),
            )
        except Exception:
            self.request_timeout = DEFAULT_OPENAI_TIMEOUT_SECONDS
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
        return "kk"

    def _language_instruction(self, lang: Optional[str]) -> str:
        lang = self._normalize_lang(lang)
        if lang == "ru":
            return "Ответь строго на русском языке."
        if lang == "en":
            return "Respond strictly in English."
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

    def _assistant_route_from_message(self, message: str) -> str:
        text = _normalize_free_text(message)
        checks = (
            ("library", ("library", "materials", "material", "библиотек", "материал", "кітапхана")),
            ("favorites", ("favorites", "favorite", "saved", "избран", "сохран", "таңдаул", "сақтал")),
            ("guess_game", ("guess", "figure", "historical figure", "угадай", "личност", "тұлға")),
            ("upload", ("upload", "create material", "create test", "загруз", "добав", "жүкте", "қосу", "құру")),
            ("ai_learn", ("learn", "study", "lesson", "изуч", "учить", "үйрен")),
            ("ai_practice", ("practice", "exercise", "тренир", "практик", "жаттық", "жаттығ")),
            ("ai_realtest", ("real test", "mock test", "пробн", "ент", "сынақ", "байқау тест")),
            ("assistant", ("assistant", "bot", "ai", "ассистент", "бот")),
            ("profile", ("profile", "account", "профил", "аккаунт", "парақша")),
            ("classmates", ("classmate", "classmates", "classroom", "однокласс", "сыныптас")),
        )
        for route, keywords in checks:
            if any(keyword in text for keyword in keywords):
                return route
        return ""

    def _assistant_is_quiz_request(self, message: str) -> bool:
        text = _normalize_free_text(message)
        keywords = (
            "quiz", "test", "practice", "question", "questions",
            "тест", "квиз", "вопрос", "вопросы", "практика", "тренировка",
            "сұрақ", "сұрақтар", "тест жаса", "жаттығ", "практик",
        )
        return any(keyword in text for keyword in keywords)

    def _assistant_is_plan_request(self, message: str) -> bool:
        text = _normalize_free_text(message)
        keywords = (
            "what should i study", "study next", "next step", "plan",
            "what do i study next",
            "что учить", "что мне учить", "учить дальше", "с чего начать", "план", "следующий шаг", "что дальше",
            "неден бастау", "не оқу", "маған не оқу", "жоспар", "келесі қадам", "ары қарай не оқу",
        )
        return any(keyword in text for keyword in keywords)

    def _assistant_route_label(self, route: str, lang: Optional[str]) -> str:
        labels = {
            "library": ("Кітапхананы ашу", "Открыть библиотеку", "Open library"),
            "favorites": ("Таңдаулыларды ашу", "Открыть избранное", "Open favorites"),
            "guess_game": ("Guess Game ашу", "Открыть Guess Game", "Open Guess Game"),
            "upload": ("Жүктеу бөлімін ашу", "Открыть загрузку", "Open upload"),
            "ai_learn": ("AI Learn ашу", "Открыть AI Learn", "Open AI Learn"),
            "ai_practice": ("AI Practice ашу", "Открыть AI Practice", "Open AI Practice"),
            "ai_realtest": ("Real Test ашу", "Открыть Real Test", "Open Real Test"),
            "assistant": ("Ассистентті ашу", "Открыть ассистента", "Open assistant"),
            "profile": ("Профильді ашу", "Открыть профиль", "Open profile"),
            "classmates": ("Сыныптастарды ашу", "Открыть classmates", "Open classmates"),
        }
        kk, ru, en = labels.get(route, ("Ашу", "Открыть", "Open"))
        return self._assistant_localized(lang, kk, ru, en)

    def _assistant_route_name(self, route: str, lang: Optional[str]) -> str:
        names = {
            "library": ("кітапхана", "библиотека", "the library"),
            "favorites": ("таңдаулылар", "избранное", "favorites"),
            "guess_game": ("Guess Game", "Guess Game", "Guess Game"),
            "upload": ("жүктеу бөлімі", "раздел загрузки", "the upload section"),
            "ai_learn": ("AI Learn", "AI Learn", "AI Learn"),
            "ai_practice": ("AI Practice", "AI Practice", "AI Practice"),
            "ai_realtest": ("Real Test", "Real Test", "Real Test"),
            "assistant": ("ассистент", "ассистент", "the assistant"),
            "profile": ("профиль", "профиль", "your profile"),
            "classmates": ("сыныптастар бөлімі", "раздел classmates", "the classmates section"),
        }
        kk, ru, en = names.get(route, ("бөлім", "раздел", "the section"))
        return self._assistant_localized(lang, kk, ru, en)

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

    def _assistant_fallback_response(
        self,
        *,
        message: str,
        lang: Optional[str],
        user_profile: Optional[dict],
        experience_summary: Optional[dict],
        knowledge_matches: list[dict],
    ) -> dict:
        route = self._assistant_route_from_message(message)
        quiz_request = self._assistant_is_quiz_request(message)
        plan_request = self._assistant_is_plan_request(message)
        top_match = knowledge_matches[0] if knowledge_matches else {}
        top_title = str(top_match.get("title") or "").strip()
        top_source_id = str(top_match.get("id") or "").strip()
        top_source_type = str(top_match.get("source_type") or "historical_figure").strip() or "historical_figure"
        top_excerpt = str(top_match.get("excerpt") or top_match.get("material_text") or "").strip()
        weak_topics = [str(item).strip() for item in (experience_summary or {}).get("weak_topics") or [] if str(item).strip()]
        recent_errors = (experience_summary or {}).get("recent_errors") or []
        material_sources = (experience_summary or {}).get("material_sources") or []
        subject_parts = [
            str((user_profile or {}).get("subject_combination") or "").strip(),
            str((user_profile or {}).get("subject1") or "").strip(),
            str((user_profile or {}).get("subject2") or "").strip(),
        ]
        subjects = ", ".join([item for item in subject_parts if item])

        citations = []
        if top_source_id and top_title:
            citations.append(
                {
                    "source_type": top_source_type,
                    "source_id": top_source_id,
                    "title": top_title,
                }
            )

        if quiz_request and top_source_id and top_title:
            source_kind = self._assistant_localized(lang, "материал", "материал", "material") if top_source_type == "material" else self._assistant_localized(lang, "тарихи тұлға", "историческую фигуру", "historical figure")
            message_text = self._assistant_localized(
                lang,
                f"Сәйкес {source_kind} таптым: {top_title}. Қаласаң, осы дереккөз бойынша бірден 10 сұрақтық practice-тест бастаймын.",
                f"Нашёл подходящий {source_kind}: {top_title}. Могу сразу запустить practice-квиз на 10 вопросов по этому источнику.",
                f"I found a relevant {source_kind}: {top_title}. I can immediately start a 10-question practice quiz on it.",
            )
            return {
                "message": message_text,
                "intent": "quiz",
                "actions": [
                    {
                        "type": "start_quiz",
                        "label": self._assistant_localized(lang, "Practice тесті", "Practice-квиз", "Practice quiz"),
                        "mode": "practice",
                        "count": 10,
                        "source_type": top_source_type,
                        "source_id": top_source_id,
                    }
                ],
                "suggested_prompts": self._assistant_default_prompts(lang),
                "citations": citations,
            }

        if plan_request:
            if weak_topics:
                topics = ", ".join(weak_topics[:3])
                message_text = self._assistant_localized(
                    lang,
                    f"Қазір алдымен мына әлсіз тақырыптардан бастаған дұрыс: {topics}. Қаласаң, солардың біріне жоспар не тест құрып беремін.",
                    f"Сейчас лучше начать с этих слабых тем: {topics}. Если хочешь, я сразу составлю план или подберу квиз по одной из них.",
                    f"You should start with these weak topics first: {topics}. If you want, I can turn one of them into a study plan or a quiz.",
                )
            elif recent_errors:
                last_error = recent_errors[0]
                error_topic = str(last_error.get("topic") or last_error.get("action") or "").strip()
                message_text = self._assistant_localized(
                    lang,
                    f"Соңғы қателер бойынша келесі фокус ретінде {error_topic or 'соңғы әлсіз жерлерді'} қарауды ұсынамын. Кейін материал не тестке өте аламыз.",
                    f"По последним ошибкам я бы предложил следующим фокусом взять {error_topic or 'последние слабые места'}. Потом можно сразу перейти к материалу или квизу.",
                    f"Based on recent errors, I would focus next on {error_topic or 'your latest weak areas'}. After that, we can jump into materials or a quiz.",
                )
            else:
                message_text = self._assistant_localized(
                    lang,
                    f"Алдымен {subjects or 'негізгі пәндерің'} бойынша кітапханадағы материалдарды қарап, содан кейін қысқа practice-тестпен бекіткен дұрыс.",
                    f"Я бы начал с материалов в библиотеке по {subjects or 'твоим основным предметам'}, а потом закрепил коротким practice-квизом.",
                    f"I would start with library materials for {subjects or 'your main subjects'}, then reinforce them with a short practice quiz.",
                )
            return {
                "message": message_text,
                "intent": "plan",
                "actions": [],
                "suggested_prompts": self._assistant_default_prompts(lang),
                "citations": citations,
            }

        if route:
            message_text = self._assistant_localized(
                lang,
                f"Сені {self._assistant_route_name(route, lang)} бөліміне бағыттай аламын. Қажет болса, ашқаннан кейін неден бастау керегін де айтып беремін.",
                f"Могу перевести тебя в раздел «{self._assistant_route_name(route, lang)}». После перехода подскажу, с чего лучше начать.",
                f"I can take you to {self._assistant_route_name(route, lang)}. After that, I can suggest the best next step there.",
            )
            return {
                "message": message_text,
                "intent": "navigate",
                "actions": [
                    {
                        "type": "navigate",
                        "label": self._assistant_route_label(route, lang),
                        "route": route,
                        "params": {},
                    }
                ],
                "suggested_prompts": self._assistant_default_prompts(lang),
                "citations": citations,
            }

        if top_title:
            short_excerpt = re.sub(r"\s+", " ", top_excerpt).strip()
            if len(short_excerpt) > 220:
                short_excerpt = short_excerpt[:219].rstrip() + "..."
            if short_excerpt:
                message_text = self._assistant_localized(
                    lang,
                    f"Ең жақын дереккөз: {top_title}. Қысқаша: {short_excerpt}",
                    f"Самый релевантный источник: {top_title}. Коротко: {short_excerpt}",
                    f"The most relevant source I found is {top_title}. In short: {short_excerpt}",
                )
            else:
                message_text = self._assistant_localized(
                    lang,
                    f"Сұрағыңа ең жақын дереккөз: {top_title}. Қаласаң, осы материал бойынша қысқа жауап, жоспар немесе тест жасап беремін.",
                    f"Самый близкий к вопросу источник: {top_title}. Если хочешь, я могу сделать по нему короткий ответ, план или квиз.",
                    f"The closest source to your question is {top_title}. If you want, I can turn it into a short answer, plan, or quiz.",
                )
            return {
                "message": message_text,
                "intent": "answer",
                "actions": [],
                "suggested_prompts": self._assistant_default_prompts(lang),
                "citations": citations,
            }

        materials_count = len(material_sources)
        message_text = self._assistant_localized(
            lang,
            f"Мен сайт бөлімдерін аша аламын, әлсіз тақырыптарды қарап келесі қадамды ұсына аламын және {materials_count} материал көзіне сүйеніп тест не жауап дайындай аламын.",
            f"Я могу открывать разделы сайта, смотреть слабые темы и предлагать следующий шаг, а также опираться на {materials_count} источников материалов при ответе или квизе.",
            f"I can open site sections, look at weak topics, suggest your next step, and use {materials_count} material sources for answers or quizzes.",
        )
        return {
            "message": message_text,
            "intent": "answer",
            "actions": [],
            "suggested_prompts": self._assistant_default_prompts(lang),
            "citations": citations,
        }

    def _assistant_language_instruction(self, lang: Optional[str]) -> str:
        lang = self._normalize_lang(lang)
        if lang == "ru":
            return "Reply in Russian."
        if lang == "en":
            return "Reply in English."
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

    def _candidate_models(self) -> list[str]:
        return [self.model, *self.fallback_models]

    def _is_model_access_error(self, error_str: str) -> bool:
        text = str(error_str or "").lower()
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

        for model_name in candidate_models:
            self.last_model_used = model_name
            try:
                response = self.client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
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
    ) -> dict:
        """Generate structured assistant response JSON."""
        system_prompt = """
You are Ozger Assistant, a concise AI study companion for ENT preparation.

Your job:
- help users navigate the site
- answer using only the provided knowledge snippets for factual questions
- personalize suggestions using the experience summary
- suggest actions instead of claiming you already opened pages
- be practical, direct, and useful

Allowed intents:
- answer
- navigate
- quiz
- plan
- clarify

Allowed actions:
1. navigate
   route must be one of:
   home, library, upload, favorites, guess_game, ai_learn, ai_practice, ai_realtest, assistant, profile, classmates
2. start_quiz
   source_type must be historical_figure or material
   mode must be practice or realtest
   count must be one of 10, 15, 20, 25, 30
3. prompt
   use only for a useful follow-up prompt suggestion

Rules:
- If the user asks for a quiz/test/practice on a known figure, prefer a start_quiz action.
- If the user asks for a quiz on their own material and material matches are provided, you may use source_type material.
- If a material match is more relevant than a historical figure, cite the material instead of the figure.
- If the user asks where something is or how to open it, prefer a navigate action.
- If the provided knowledge is insufficient, say that clearly.
- Never invent citations.
- Return valid JSON only.
"""

        history_messages = self._history_messages(chat_history)

        prompt = f"""
{self._assistant_language_instruction(lang)}

User message:
{message}

User profile:
{json.dumps(user_profile or {}, ensure_ascii=False)}

Current page context:
{json.dumps(page_context or {}, ensure_ascii=False)}

Experience summary:
{json.dumps(experience_summary or {}, ensure_ascii=False)}

Relevant knowledge matches:
{json.dumps(knowledge_matches or [], ensure_ascii=False)}

Active material excerpt:
{active_material_excerpt[:MAX_ACTIVE_MATERIAL_EXCERPT_CHARS]}

Return JSON with this shape:
{{
  "message": "short helpful answer",
  "intent": "answer|navigate|quiz|plan|clarify",
  "actions": [
    {{
      "type": "navigate",
      "label": "Open library",
      "route": "library",
      "params": {{}}
    }},
    {{
      "type": "start_quiz",
      "label": "Start quiz on Kenesary",
      "mode": "practice",
      "count": 10,
      "source_type": "historical_figure",
      "source_id": "2"
    }}
  ],
  "suggested_prompts": ["...", "...", "..."],
  "citations": [
    {{
      "source_type": "historical_figure",
      "source_id": "2",
      "title": "Kenesary Kasymov"
    }}
  ]
}}

Constraints:
- actions: maximum 3
- suggested_prompts: maximum 3
- citations: maximum 3
- message should be under 120 words
"""

        try:
            return self._generate_json_object(
                prompt,
                system_prompt=system_prompt,
                conversation_messages=history_messages,
                temperature=0.25,
                max_tokens=2200,
            )
        except Exception as exc:
            logger.warning("Assistant response fell back to local planner: %s", exc)
            return self._assistant_fallback_response(
                message=message,
                lang=lang,
                user_profile=user_profile,
                experience_summary=experience_summary,
                knowledge_matches=knowledge_matches,
            )

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

@lru_cache(maxsize=1)
def get_openai_service() -> OpenAIService:
    """Get or create OpenAI service instance."""
    return OpenAIService()

# Временно не используется
