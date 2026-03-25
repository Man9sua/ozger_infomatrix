"""
Google Gemini API Service
Handles all AI generation for learning content, questions, and tests
"""

import google.generativeai as genai
import json
import os
import time
import datetime
import math
import hashlib
import threading
from collections import OrderedDict
from typing import Optional, Tuple


class GeminiService:
    """Service for interacting with Google Gemini API"""
    
    def __init__(self, api_key: Optional[str] = None):
        """Initialize Gemini service with API key"""
        self.api_keys = self._load_api_keys(api_key)
        if not self.api_keys:
            raise ValueError("GEMINI_API_KEY is required")

        self._key_lock = threading.Lock()
        self._genai_lock = threading.Lock()
        self.rotate_after_requests = self._env_int(
            "GEMINI_ROTATE_AFTER_REQUESTS", default=15, min_value=1, max_value=100000
        )
        self._active_key_index = self._select_initial_key_index(self.api_keys)
        self._requests_on_active_key = 0
        self.api_key = self.api_keys[self._active_key_index]

        genai.configure(api_key=self.api_key)
        
        self.temperature = 0.7
        # Keep outputs very small to fit 30s PaaS budget.
        self.default_max_output_tokens = self._env_int(
            "GEMINI_MAX_OUTPUT_TOKENS", default=6000, min_value=512, max_value=32768
        )
        self.learn_max_output_tokens = self._env_int(
            "GEMINI_LEARN_MAX_OUTPUT_TOKENS",
            default=max(self.default_max_output_tokens, 3500),
            min_value=512,
            max_value=32768,
        )
        self.learn_target_chars = self._env_int(
            "GEMINI_LEARN_TARGET_CHARS", default=20000, min_value=8000, max_value=300000
        )
        self.learn_history_target_chars = self._env_int(
            "GEMINI_LEARN_HISTORY_TARGET_CHARS",
            default=max(self.learn_target_chars, 28000),
            min_value=8000,
            max_value=350000,
        )
        self.learn_history_target_chars = max(
            self.learn_target_chars, self.learn_history_target_chars
        )
        # Hard caps to keep response time within 30s PaaS limits
        self.learn_target_chars = min(self.learn_target_chars, 25000)
        self.learn_history_target_chars = min(self.learn_history_target_chars, 32000)
        self.learn_max_output_tokens = min(self.learn_max_output_tokens, 4000)

        # Model chain for automatic fallback on transient provider failures.
        self.model_names = self._load_model_chain()
        self._active_model_index = 0
        
        # IMPORTANT: keep retries low; frontend expects one generation per click.
        # If Gemini times out, user can retry manually.
        try:
            self.max_retries = max(1, int(os.getenv("GEMINI_MAX_RETRIES", "2")))
        except Exception:
            self.max_retries = 2
        try:
            self.retry_delay = max(0.0, float(os.getenv("GEMINI_RETRY_DELAY", "2")))
        except Exception:
            self.retry_delay = 2.0  # seconds
        
        # Large material processing controls (reduce request count)
        summarize_flag = os.getenv("GEMINI_SUMMARIZE_LARGE", "false").strip().lower()
        self.summarize_large = summarize_flag in ("1", "true", "yes")
        try:
            self.max_chunks = int(os.getenv("GEMINI_MAX_CHUNKS", "8"))
        except Exception:
            self.max_chunks = 8
        self.max_chunks = max(2, min(16, self.max_chunks))
        try:
            self.min_chunk_chars = int(os.getenv("GEMINI_MIN_CHUNK_CHARS", "30000"))
        except Exception:
            self.min_chunk_chars = 30000
        try:
            self.max_chunk_chars = int(os.getenv("GEMINI_MAX_CHUNK_CHARS", "200000"))
        except Exception:
            self.max_chunk_chars = 200000

        # In-memory cache for summarized materials (reduces repeat calls)
        try:
            self.summary_cache_max = int(os.getenv("GEMINI_SUMMARY_CACHE_MAX", "32"))
        except Exception:
            self.summary_cache_max = 32
        self.summary_cache_max = max(0, min(200, self.summary_cache_max))
        self._summary_cache: "OrderedDict[str, str]" = OrderedDict()

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

    def _env_int(self, name: str, default: int, min_value: int, max_value: int) -> int:
        """Parse bounded integer environment variable."""
        try:
            value = int(os.getenv(name, str(default)))
        except Exception:
            value = default
        return max(min_value, min(max_value, value))

    def _load_api_keys(self, explicit_api_key: Optional[str]) -> list[str]:
        """Load API keys from explicit argument or env vars."""
        if explicit_api_key and str(explicit_api_key).strip():
            return [str(explicit_api_key).strip()]

        raw_keys = os.getenv("GEMINI_API_KEYS", "").strip()
        if raw_keys:
            keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
            if keys:
                return keys

        single_key = os.getenv("GEMINI_API_KEY", "").strip()
        return [single_key] if single_key else []

    def _load_model_chain(self) -> list[str]:
        """
        Load model fallback chain.
        Priority:
        1) GEMINI_MODEL_CHAIN (comma separated)
        2) GEMINI_MODEL (single)
        3) sensible defaults
        """
        raw_chain = os.getenv("GEMINI_MODEL_CHAIN", "").strip()
        if raw_chain:
            chain = [m.strip() for m in raw_chain.split(",") if m.strip()]
            if chain:
                return chain

        single_model = os.getenv("GEMINI_MODEL", "").strip()
        if single_model:
            return [single_model]

        return [
            # Default chain: keep gemini-3-flash-preview first as запрошено
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-flash-latest",
        ]

    def _select_initial_key_index(self, keys: list[str]) -> int:
        """Choose starting key index to spread load across restarts."""
        if len(keys) <= 1:
            return 0
        try:
            rotate_hours = max(1, int(os.getenv("GEMINI_ROTATE_HOURS", "1")))
        except Exception:
            rotate_hours = 1
        epoch_hours = int(datetime.datetime.utcnow().timestamp() // 3600)
        return (epoch_hours // rotate_hours) % len(keys)

    def _reserve_api_key(self, force_rotate: bool = False) -> str:
        """
        Reserve API key for a single Gemini request.
        Rotates key after N requests or when forced.
        """
        with self._key_lock:
            if len(self.api_keys) > 1:
                must_rotate = self._requests_on_active_key >= self.rotate_after_requests
                if force_rotate or must_rotate:
                    self._active_key_index = (self._active_key_index + 1) % len(self.api_keys)
                    self._requests_on_active_key = 0

            self.api_key = self.api_keys[self._active_key_index]
            self._requests_on_active_key += 1
            return self.api_key

    @staticmethod
    def _is_quota_or_rate_error(error_text: str) -> bool:
        if not error_text:
            return False
        checks = (
            "429",
            "quota",
            "resource_exhausted",
            "rate limit",
            "too many requests",
            "exceeded",
        )
        return any(token in error_text for token in checks)

    @staticmethod
    def _is_transient_error(error_text: str) -> bool:
        if not error_text:
            return False
        checks = (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "service unavailable",
            "backend write error",
            "varnish",
            "54113",
            "502",
            "503",
            "504",
            "500",
        )
        return any(token in error_text for token in checks)

    @staticmethod
    def _normalize_provider_error(error: Exception) -> str:
        raw = str(error or "").strip()
        text = raw.lower()

        if "backend write error" in text or "varnish" in text or "54113" in text:
            return "AI provider is temporarily unavailable (upstream 503). Please retry in 15-30 seconds."
        if "429" in text or "quota" in text or "resource_exhausted" in text:
            return "AI provider quota/rate limit reached. Please retry in a minute."
        if "<html" in text or "<!doctype" in text:
            return "AI provider returned an invalid HTML error page. Please retry shortly."
        if "503" in text or "502" in text or "504" in text:
            return "AI provider temporary server error (5xx). Please retry shortly."

        if len(raw) > 360:
            return raw[:360] + "..."
        return raw or "AI provider request failed."

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
        """
        Split long text into overlapping chunks (character-based).
        Keeps chunks reasonably aligned to paragraph boundaries when possible.
        """
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

            # If not last chunk, try to cut on a paragraph boundary near the end
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

    def _cache_key(self, material: str, target_chars: int, lang: Optional[str]) -> str:
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        lang_norm = self._normalize_lang(lang)
        return f"{digest}:{target_chars}:{lang_norm}:{'sum' if self.summarize_large else 'trunc'}"

    def _cache_get(self, key: str) -> Optional[str]:
        if self.summary_cache_max <= 0:
            return None
        if key not in self._summary_cache:
            return None
        value = self._summary_cache.pop(key)
        self._summary_cache[key] = value
        return value

    def _cache_set(self, key: str, value: str) -> None:
        if self.summary_cache_max <= 0:
            return
        if key in self._summary_cache:
            self._summary_cache.pop(key)
        self._summary_cache[key] = value
        while len(self._summary_cache) > self.summary_cache_max:
            self._summary_cache.popitem(last=False)

    def _prepare_large_material(self, material: str, *, target_chars: int, lang: Optional[str] = None) -> str:
        """
        For very large PDFs/text, build dense study notes via map-reduce summarization
        so we can still generate strong questions without blunt truncation.
        """
        if not material or len(material) <= target_chars:
            return material

        cache_key = self._cache_key(material, target_chars, lang)
        cached = self._cache_get(cache_key)
        if cached:
            return cached

        if not self.summarize_large:
            truncated = material[:target_chars] + "\n\n[Материал қысқартылды (өте үлкен мәтін)]"
            self._cache_set(cache_key, truncated)
            return truncated

        # Cap number of model calls: we must ensure we don't create hundreds of chunks for big PDFs.
        max_chunks = self.max_chunks
        # Choose chunk size so we end up with ~max_chunks chunks (bounded).
        max_chars = int(math.ceil(len(material) / max_chunks))
        # Keep chunks within reasonable size so Gemini stays stable.
        max_chars = max(self.min_chunk_chars, min(self.max_chunk_chars, max_chars))
        chunks = self._chunk_text(material, max_chars=max_chars, overlap=1200)

        # Hard cap in case of pathological input
        if len(chunks) > max_chunks:
            chunks = chunks[:max_chunks]

        # If chunking didn't help, fall back to a soft truncate with a warning marker
        if len(chunks) <= 1:
            truncated = material[:target_chars] + "\n\n[Материал қысқартылды (өте үлкен мәтін)]"
            self._cache_set(cache_key, truncated)
            return truncated

        notes_parts: list[str] = []

        for idx, chunk in enumerate(chunks, start=1):
            lang_instruction = self._language_instruction(lang)
            prompt = f"""{self.system_prompt}
{lang_instruction}

ТАПСЫРМА: Төмендегі мәтіннің {idx}/{len(chunks)} БӨЛІГІ бойынша өте тығыз, нақты оқу-конспект жаса.
Тек берілген материалдағы фактілерді пайдалан. Егер мәтінде [PAGE N] маркерлері болса, маңызды фактілердің қасына сақта (мысалы: "(PAGE 12)").

ҚҰРЫЛЫМ (қысқа әрі нақты):
- Key facts (bullets)
- Key terms (bullets)
- Timeline (bullets with year/date where possible)
- Potential ENT traps (bullets: шатастыратын, бірақ дәл фактіге негізделген тұстар)

МӘТІН:
{chunk}

КОНСПЕКТ:"""

            part = self._generate_with_retry(prompt).strip()
            if part:
                notes_parts.append(part)

        combined_notes = "\n\n---\n\n".join(notes_parts)

        if len(combined_notes) <= target_chars:
            self._cache_set(cache_key, combined_notes)
            return combined_notes

        # Reduce step: compress the combined notes to a single compact context
        lang_instruction = self._language_instruction(lang)
        reduce_prompt = f"""{self.system_prompt}
{lang_instruction}

ТАПСЫРМА: Төмендегі бірнеше бөлімнен тұратын конспектті бір ТҰТАС, өте ықшам оқу-материалына қысқарт.
Ереже: тек фактілер, артық сөз жоқ. [PAGE N] маркерлері болса, сақта.

Мақсат: нәтиже ұзындығы шамамен {target_chars} таңбадан аспасын.

КОНСПЕКТ:
{combined_notes}

ЫҚШАМ НӘТИЖЕ:"""

        reduced = self._generate_with_retry(reduce_prompt).strip()
        self._cache_set(cache_key, reduced)
        return reduced

    def _clean_json_response(self, text: str) -> str:
        """Clean and extract JSON from response text"""
        # Remove markdown code blocks if present
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        
        # Try to fix truncated JSON
        if text:
            # Count brackets to check if JSON is complete
            open_braces = text.count('{') - text.count('}')
            open_brackets = text.count('[') - text.count(']')
            
            # If JSON is truncated, try to close it
            if open_braces > 0 or open_brackets > 0:
                # Find last complete item and truncate there
                # Try to find last complete question or section
                
                # First, try to close any open strings
                quote_count = text.count('"') 
                if quote_count % 2 != 0:
                    # Find last quote and truncate after previous complete item
                    last_complete = text.rfind('},')
                    if last_complete == -1:
                        last_complete = text.rfind('}]')
                    if last_complete > 0:
                        text = text[:last_complete+1]
                
                # Close remaining brackets
                open_braces = text.count('{') - text.count('}')
                open_brackets = text.count('[') - text.count(']')
                
                text += ']' * open_brackets
                text += '}' * open_braces
        
        return text
    
    def _generate_with_retry(
        self,
        prompt: str,
        max_output_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
    ) -> str:
        """Generate content with retry logic, key rotation and quota fallback."""
        last_error = None
        target_max_output = self.default_max_output_tokens
        if max_output_tokens is not None:
            target_max_output = max(1024, min(32768, int(max_output_tokens)))
        attempt = 0
        force_rotate_key = False
        quota_rotations_left = max(0, len(self.api_keys) - 1)
        model_rotations_left = max(0, len(self.model_names) - 1)
        active_model_index = self._active_model_index

        while attempt < self.max_retries:
            api_key = self._reserve_api_key(force_rotate=force_rotate_key)
            force_rotate_key = False
            model_name = self.model_names[active_model_index]
            try:
                with self._genai_lock:
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel(
                        model_name,
                        generation_config=genai.GenerationConfig(
                            temperature=self.temperature,
                            max_output_tokens=target_max_output,
                        ),
                    )
                    request_opts = {"timeout": timeout_seconds} if timeout_seconds else None
                    response = model.generate_content(prompt, request_options=request_opts)

                text = getattr(response, "text", "")
                if text and str(text).strip():
                    self._active_model_index = active_model_index
                    return text
                raise RuntimeError("AI provider returned an empty response.")
            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                if self._is_quota_or_rate_error(error_str):
                    if quota_rotations_left > 0:
                        quota_rotations_left -= 1
                        force_rotate_key = True
                        continue
                    raise RuntimeError(self._normalize_provider_error(e))

                # Retry on transient provider failures; rotate model and optionally key.
                if self._is_transient_error(error_str):
                    attempt += 1
                    if attempt < self.max_retries:
                        if model_rotations_left > 0:
                            model_rotations_left -= 1
                            active_model_index = (active_model_index + 1) % len(self.model_names)
                        if len(self.api_keys) > 1:
                            force_rotate_key = True
                        # Slightly reduce generation size on retry to improve provider stability.
                        target_max_output = max(2048, int(target_max_output * 0.85))
                        time.sleep(self.retry_delay * attempt)
                        continue

                raise RuntimeError(self._normalize_provider_error(e))

        if last_error is not None:
            raise RuntimeError(self._normalize_provider_error(last_error))
        raise RuntimeError("AI provider request failed.")

    async def generate_learn_content(self, material: str, history_mode: bool = False, lang: Optional[str] = None) -> dict:
        """
        Generate learning plan with content and questions for each section.
        
        Args:
            material: Source material text
            history_mode: If True, use 3-view format (general, summary, timeline)
        """
        # For large PDFs/text: summarize instead of hard truncation
        target_chars = self.learn_history_target_chars if history_mode else self.learn_target_chars
        material = self._prepare_large_material(material, target_chars=target_chars, lang=lang)
        lang_instruction = self._language_instruction(lang)
        
        if history_mode:
            # History Mode - detailed 3-view format
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
- Формат: [{{"period": "1465 жыл", "event": "Керей мен Жәнібек сұлтандар Әбілқайыр ханның қол астынан кетіп, Қазақ хандығын құрды. Олар Моғолстанның батыс бөлігіне - Жетісу өңіріне қоныс аударды."}}]

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
            # Normal Mode - simple format
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
            response_text = self._generate_with_retry(
                prompt,
                max_output_tokens=self.learn_max_output_tokens,
                timeout_seconds=15,
            )
            json_text = self._clean_json_response(response_text)
            return json.loads(json_text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSON форматында қате: {str(e)}")
        except Exception as e:
            raise Exception(f"Gemini API қатесі: {str(e)}")

    async def generate_practice_questions(self, material: str, count: int, exclude_questions: list = None, lang: Optional[str] = None) -> dict:
        """
        Generate practice questions.
        
        Args:
            material: Source material text
            count: Number of questions to generate
            exclude_questions: List of questions to exclude (for "continue with other questions")
            
        Returns:
            Dictionary with questions
        """
        # For large PDFs/text: summarize instead of hard truncation
        material = self._prepare_large_material(material, target_chars=50000, lang=lang)
        lang_instruction = self._language_instruction(lang)

        exclude_text = ""
        if exclude_questions:
            exclude_text = f"\n\nБҰЛ СҰРАҚТАРДЫ ҚАЙТАЛАМА:\n" + "\n".join(exclude_questions)

        prompt = f"""{self.system_prompt}
{lang_instruction}

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
- Қате жауаптардың ұзындығы дұрыс жауаппен шамалас болсын (өте қысқа немесе өте ұзын болмасын)
- Әр сұраққа түсіндірме жаз
{exclude_text}

МАТЕРИАЛ:
{material}

JSON жауап:"""

        try:
            response_text = self._generate_with_retry(prompt)
            json_text = self._clean_json_response(response_text)
            return json.loads(json_text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSON форматында қате: {str(e)}")
        except Exception as e:
            raise Exception(f"Gemini API қатесі: {str(e)}")

    async def generate_realtest_questions(self, material: str, count: int, lang: Optional[str] = None) -> dict:
        """
        Generate real test questions (no explanations, no hints).
        
        Args:
            material: Source material text
            count: Number of questions to generate
            
        Returns:
            Dictionary with test questions
        """
        # For large PDFs/text: summarize instead of hard truncation
        material = self._prepare_large_material(material, target_chars=50000, lang=lang)
        lang_instruction = self._language_instruction(lang)

        prompt = f"""{self.system_prompt}
{lang_instruction}

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
            response_text = self._generate_with_retry(prompt)
            json_text = self._clean_json_response(response_text)
            return json.loads(json_text)
        except json.JSONDecodeError as e:
            raise Exception(f"JSON форматында қате: {str(e)}")
        except Exception as e:
            raise Exception(f"Gemini API қатесі: {str(e)}")


# Singleton instance
_gemini_service = None


def get_gemini_service() -> GeminiService:
    """Get or create Gemini service instance"""
    global _gemini_service
    if _gemini_service is None:
        _gemini_service = GeminiService()
    return _gemini_service
