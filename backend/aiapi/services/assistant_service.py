from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID

from services.openai_service import get_openai_service
from services.supabase_service import SupabaseService, SupabaseServiceError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _shorten(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "..."


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _excerpt(value: Any, limit: int = 420) -> str:
    return _shorten(re.sub(r"\s+", " ", str(value or "")).strip(), limit)


def _normalize_uuid(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(UUID(raw))
    except Exception:
        return ""


class HistoricalKnowledgeBase:
    def __init__(self, data_path: Optional[Path] = None):
        root_dir = Path(__file__).resolve().parents[3]
        self.data_path = data_path or root_dir / "frontend" / "data" / "historical-figures.json"
        self._loaded = False
        self._figures: list[dict[str, Any]] = []

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        self._figures = list(payload.get("figures") or [])
        self._loaded = True

    def _name(self, figure: dict[str, Any], lang: str) -> str:
        names = figure.get("name") or {}
        return str(
            names.get(lang)
            or names.get("kk")
            or names.get("ru")
            or names.get("en")
            or figure.get("id")
            or "Figure"
        )

    def _corpus(self, figure: dict[str, Any]) -> str:
        names = figure.get("name") or {}
        parts = [str(names.get(key) or "") for key in ("kk", "ru", "en")]
        for fact in figure.get("facts") or []:
            text = fact.get("text") or {}
            parts.extend(str(text.get(key) or "") for key in ("kk", "ru", "en"))
        return _normalize_text(" ".join(parts[:24]))

    def to_summary(self, figure: dict[str, Any], lang: str = "kk") -> dict[str, Any]:
        facts: list[str] = []
        for fact in figure.get("facts") or []:
            text = fact.get("text") or {}
            value = str(
                text.get(lang) or text.get("kk") or text.get("ru") or text.get("en") or ""
            ).strip()
            if value:
                facts.append(value)
        facts = facts[:10]
        return {
            "source_type": "historical_figure",
            "id": str(figure.get("id")),
            "title": self._name(figure, lang),
            "facts": facts,
            "excerpt": _excerpt(" ".join(facts)),
            "material_text": f"Topic: {self._name(figure, lang)}\n\n"
            + "\n".join(f"- {item}" for item in facts),
        }

    def search(self, query: str, lang: str = "kk", limit: int = 3) -> list[dict[str, Any]]:
        self._ensure_loaded()
        query_text = _normalize_text(query)
        if not query_text:
            return []
        tokens = [token for token in query_text.split(" ") if len(token) >= 2]
        scored: list[tuple[int, dict[str, Any]]] = []
        for figure in self._figures:
            name = _normalize_text(self._name(figure, lang))
            corpus = self._corpus(figure)
            score = 0
            if query_text == name:
                score += 120
            elif query_text in name:
                score += 80
            for token in tokens:
                if token in name:
                    score += 24
                elif token in corpus:
                    score += 8
            if score > 0:
                scored.append((score, figure))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
        return [self.to_summary(item, lang) for _, item in scored[:limit]]

    def get_by_id(self, figure_id: Any, lang: str = "kk") -> Optional[dict[str, Any]]:
        self._ensure_loaded()
        wanted = str(figure_id or "").strip()
        for figure in self._figures:
            if str(figure.get("id")) == wanted:
                return self.to_summary(figure, lang)
        return None


class AssistantService:
    def __init__(self, material_resolver: Callable[[dict[str, Any]], str]):
        self.material_resolver = material_resolver
        self.knowledge = HistoricalKnowledgeBase()
        self.supabase = SupabaseService()

    def _sanitize_history(self, history: Any) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        if not isinstance(history, list):
            return cleaned
        for item in history[-12:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").strip().lower()
            role = role if role in {"user", "assistant"} else "user"
            content = str(item.get("content") or "").strip()
            if content:
                cleaned.append({"role": role, "content": _shorten(content, 1600)})
        return cleaned

    def _normalize_action(self, action: Any) -> Optional[dict[str, Any]]:
        if not isinstance(action, dict):
            return None
        action_type = str(action.get("type") or "").strip()
        if action_type == "navigate":
            route = str(action.get("route") or "").strip()
            if not route:
                return None
            return {
                "type": "navigate",
                "label": str(action.get("label") or route.replace("_", " ").title()).strip(),
                "route": route,
                "params": action.get("params") if isinstance(action.get("params"), dict) else {},
            }
        if action_type == "start_quiz":
            source_type = str(action.get("source_type") or "historical_figure").strip()
            if source_type not in {"historical_figure", "material"}:
                source_type = "historical_figure"
            return {
                "type": "start_quiz",
                "label": str(action.get("label") or "Start quiz").strip(),
                "mode": str(action.get("mode") or "practice").strip().lower(),
                "count": _to_int(action.get("count"), 10),
                "source_type": source_type,
                "source_id": str(action.get("source_id") or "").strip(),
            }
        if action_type == "prompt":
            prompt = str(action.get("prompt") or "").strip()
            if prompt:
                return {
                    "type": "prompt",
                    "label": str(action.get("label") or prompt).strip(),
                    "prompt": prompt,
                }
        return None

    def _fallback_route(self, message: str) -> Optional[str]:
        checks = (
            ("library", ("library", "material", "book", "библиотек", "материал", "кітапхана")),
            ("favorites", ("favorite", "saved", "избран", "сохран", "таңдаул", "сақтал")),
            ("guess_game", ("guess", "figure", "угадай", "личност", "тұлға")),
            ("upload", ("upload", "create", "загруз", "жүкте", "қосу", "құру")),
            ("ai_learn", ("learn", "study", "изуч", "учить", "үйрен")),
            ("ai_practice", ("practice", "exercise", "практик", "тренир", "жаттығ", "жаттық")),
            ("ai_realtest", ("real test", "mock test", "пробн", "ент", "сынақ")),
            ("profile", ("profile", "account", "профил", "аккаунт", "парақша")),
            ("classmates", ("classmate", "classroom", "однокласс", "сыныптас")),
        )
        text = _normalize_text(message)
        for route, keywords in checks:
            if any(keyword in text for keyword in keywords):
                return route
        return None

    def _sb_auth(self, token: str) -> dict[str, Any]:
        return {
            "use_service_role": bool(self.supabase.service_role_key),
            "auth_token": None if self.supabase.service_role_key else token,
        }

    def _identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        requested = str(payload.get("user_id") or "").strip()
        token = str(payload.get("_access_token") or payload.get("access_token") or "").strip()
        auth_user = None
        if self.supabase.available and token:
            try:
                auth_user = self.supabase.verify_user(token)
            except SupabaseServiceError:
                auth_user = None
        verified = str(auth_user.get("id") or "").strip() if isinstance(auth_user, dict) else ""
        return {
            "user_id": verified or requested or "guest",
            "verified_user_id": verified,
            "access_token": token if verified else "",
            "auth_user": auth_user or {},
        }

    def _profile_row(self, user_id: str, token: str) -> Optional[dict[str, Any]]:
        if not (user_id and token and self.supabase.available):
            return None
        rows = self.supabase.select(
            "profiles",
            params={
                "select": "user_id,username,email,country,city,school,class_number,class_letter,subject_combination,subject1,subject2,avatar_url",
                "user_id": f"eq.{user_id}",
                "limit": "1",
            },
            **self._sb_auth(token),
        )
        return rows[0] if rows else None

    def _stats_row(self, user_id: str, token: str) -> Optional[dict[str, Any]]:
        if not (user_id and token and self.supabase.available):
            return None
        rows = self.supabase.select(
            "user_stats",
            params={
                "select": "user_id,total_tests,guess_best_streak,ent_best_score,ent_tests_completed",
                "user_id": f"eq.{user_id}",
                "limit": "1",
            },
            **self._sb_auth(token),
        )
        return rows[0] if rows else None

    def _materials_rows(self, user_id: str, token: str, limit: int = 10) -> list[dict[str, Any]]:
        if not (user_id and token and self.supabase.available):
            return []
        return self.supabase.select(
            "materials",
            params={
                "select": "id,title,subject,type,content,created_at,updated_at",
                "user_id": f"eq.{user_id}",
                "order": "updated_at.desc",
                "limit": str(limit),
            },
            timeout=12,
            **self._sb_auth(token),
        )

    def _events_rows(self, user_id: str, token: str, limit: int = 120) -> list[dict[str, Any]]:
        if not (user_id and token and self.supabase.available):
            return []
        return self.supabase.select(
            "assistant_events",
            params={
                "select": "event_type,action,route,topic,source_type,source_id,correct,total,percent,message,created_at",
                "user_id": f"eq.{user_id}",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            **self._sb_auth(token),
        )

    def _material_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        content = str(row.get("content") or "")
        return {
            "source_type": "material",
            "id": str(row.get("id") or ""),
            "title": str(row.get("title") or "Material").strip() or "Material",
            "subject": str(row.get("subject") or "").strip(),
            "type": str(row.get("type") or "").strip(),
            "excerpt": _excerpt(content),
            "material_text": _shorten(content, 7000),
            "updated_at": row.get("updated_at"),
        }

    def _search_materials(self, rows: list[dict[str, Any]], query: str, limit: int = 3) -> list[dict[str, Any]]:
        query_text = _normalize_text(query)
        if not query_text:
            return []
        tokens = [token for token in query_text.split(" ") if len(token) >= 2]
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            title = _normalize_text(row.get("title"))
            subject = _normalize_text(row.get("subject"))
            content = _normalize_text(_shorten(row.get("content"), 3200))
            score = 0
            if query_text == title:
                score += 110
            elif query_text in title:
                score += 70
            for token in tokens:
                if token in title:
                    score += 24
                elif token in subject:
                    score += 12
                elif token in content:
                    score += 6
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("updated_at") or "")))
        return [self._material_summary(row) for _, row in scored[:limit]]

    def _payload_sources(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        raw_sources = payload.get("knowledge_sources")
        if not isinstance(raw_sources, list):
            return cleaned
        for item in raw_sources[:16]:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("source_id") or item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            text = str(item.get("text") or item.get("material_text") or item.get("content") or "").strip()
            if not source_id or not title or not text:
                continue
            cleaned.append(
                {
                    "source_type": "material",
                    "id": source_id,
                    "title": title[:160],
                    "subject": str(item.get("subject") or "").strip()[:120],
                    "type": str(item.get("type") or "external").strip()[:80],
                    "content": text[:7000],
                    "updated_at": item.get("updated_at") or item.get("created_at"),
                }
            )
        return cleaned

    def _search_payload_sources(self, rows: list[dict[str, Any]], query: str, limit: int = 3) -> list[dict[str, Any]]:
        return self._search_materials(rows, query, limit=limit)

    def _active_material(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        material_id = str(payload.get("material_id") or "").strip()
        if not material_id:
            return None
        content = self.material_resolver({"material_id": material_id})
        if not content:
            return None
        return {
            "source_type": "material",
            "id": material_id,
            "title": str(payload.get("material_title") or "Current material"),
            "subject": str(payload.get("material_subject") or ""),
            "type": "active_material",
            "excerpt": _excerpt(content),
            "material_text": _shorten(content, 7000),
        }

    def _profile(
        self,
        payload_profile: dict[str, Any],
        auth_user: dict[str, Any],
        row: Optional[dict[str, Any]],
        stats: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = auth_user.get("user_metadata") if isinstance(auth_user, dict) else {}
        metadata = metadata if isinstance(metadata, dict) else {}
        row = row or {}
        stats = stats or {}
        return {
            "id": auth_user.get("id") or payload_profile.get("id") or "",
            "username": row.get("username") or payload_profile.get("username") or metadata.get("username") or "",
            "email": row.get("email") or auth_user.get("email") or payload_profile.get("email") or "",
            "country": row.get("country") or payload_profile.get("country") or metadata.get("country") or "",
            "city": row.get("city") or payload_profile.get("city") or metadata.get("city") or "",
            "school": row.get("school") or payload_profile.get("school") or metadata.get("school") or "",
            "class_number": row.get("class_number") or payload_profile.get("class_number") or payload_profile.get("classNumber") or "",
            "class_letter": row.get("class_letter") or payload_profile.get("class_letter") or payload_profile.get("classLetter") or "",
            "subject_combination": row.get("subject_combination") or payload_profile.get("subject_combination") or payload_profile.get("subjectCombination") or metadata.get("subject_combination") or metadata.get("subjectCombination") or "",
            "subject1": row.get("subject1") or payload_profile.get("subject1") or metadata.get("subject1") or "",
            "subject2": row.get("subject2") or payload_profile.get("subject2") or metadata.get("subject2") or "",
            "stats": {
                "total_tests": _to_int(stats.get("total_tests")),
                "ent_best_score": _to_int(stats.get("ent_best_score")),
                "ent_tests_completed": _to_int(stats.get("ent_tests_completed")),
                "guess_best_streak": _to_int(stats.get("guess_best_streak")),
            },
        }

    def _summary(self, user_id: str, token: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
        events = self._events_rows(user_id, token)
        action_counts: Counter[str] = Counter()
        weak_topics: Counter[str] = Counter()
        recent_topics: list[str] = []
        recent_errors: list[dict[str, Any]] = []
        recent_event_types: list[str] = []
        last_quiz = None
        for row in events:
            recent_event_types.append(str(row.get("event_type") or ""))
            action = str(row.get("action") or row.get("route") or "").strip()
            if action:
                action_counts[action] += 1
            if str(row.get("event_type") or "") == "error":
                recent_errors.append(
                    {
                        "message": str(row.get("message") or "").strip(),
                        "route": str(row.get("route") or "").strip(),
                        "action": str(row.get("action") or "").strip(),
                        "created_at": row.get("created_at"),
                    }
                )
                continue
            if str(row.get("event_type") or "") == "quiz_result":
                topic = str(row.get("topic") or "").strip()
                percent = _to_int(row.get("percent"), 0)
                entry = {
                    "topic": topic,
                    "source_id": row.get("source_id"),
                    "source_type": row.get("source_type"),
                    "correct": _to_int(row.get("correct")),
                    "total": _to_int(row.get("total")),
                    "percent": percent,
                    "created_at": row.get("created_at"),
                }
                if last_quiz is None:
                    last_quiz = entry
                if topic:
                    recent_topics.append(topic)
                if topic and percent < 75:
                    weak_topics[topic] += max(1, (75 - max(percent, 0)) // 10 or 1)
                    recent_errors.append(entry)
        return {
            "top_actions": [name for name, _ in action_counts.most_common(5)],
            "weak_topics": [name for name, _ in weak_topics.most_common(5)],
            "recent_topics": list(dict.fromkeys(recent_topics[:8]))[:5],
            "last_quiz": last_quiz,
            "recent_errors": recent_errors[:5],
            "recent_event_types": recent_event_types[:8],
            "material_sources": [self._material_summary(row) for row in materials[:4]],
        }

    def _session_row(self, user_id: str, token: str, session_id: str) -> Optional[dict[str, Any]]:
        session_id = _normalize_uuid(session_id)
        if not (user_id and token and session_id and self.supabase.available):
            return None
        rows = self.supabase.select(
            "assistant_sessions",
            params={
                "select": "id,title,last_message_preview,last_intent,last_route,created_at,updated_at,last_message_at",
                "id": f"eq.{session_id}",
                "user_id": f"eq.{user_id}",
                "limit": "1",
            },
            **self._sb_auth(token),
        )
        return rows[0] if rows else None

    def _ensure_session(self, user_id: str, token: str, session_id: str, message: str, route: str) -> Optional[dict[str, Any]]:
        session_id = _normalize_uuid(session_id)
        row = self._session_row(user_id, token, session_id)
        if row:
            return row
        rows = self.supabase.insert(
            "assistant_sessions",
            {
                "user_id": user_id,
                "title": _shorten(message or "New chat", 72) or "New chat",
                "last_message_preview": _shorten(message, 180),
                "last_route": route or None,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "last_message_at": _utc_now(),
            },
            **self._sb_auth(token),
        )
        return rows[0] if rows else None

    def _append_message(
        self,
        user_id: str,
        token: str,
        session_id: str,
        role: str,
        content: str,
        intent: str = "",
        actions: Optional[list[dict[str, Any]]] = None,
        citations: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        session_id = _normalize_uuid(session_id)
        if session_id:
            self.supabase.insert(
                "assistant_messages",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": role,
                    "title": None,
                    "content": _shorten(content, 6000),
                    "intent": intent or None,
                    "actions": actions or [],
                    "citations": citations or [],
                    "created_at": _utc_now(),
                },
                prefer="return=minimal",
                **self._sb_auth(token),
            )

    def _update_session(self, user_id: str, token: str, session_id: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        session_id = _normalize_uuid(session_id)
        if not session_id:
            return None
        rows = self.supabase.update(
            "assistant_sessions",
            {"id": f"eq.{session_id}", "user_id": f"eq.{user_id}"},
            payload,
            **self._sb_auth(token),
        )
        return rows[0] if rows else None

    def _record_event(self, user_id: str, token: str, session_id: str, event_type: str, payload: dict[str, Any]) -> None:
        session_id = _normalize_uuid(session_id)
        self.supabase.insert(
            "assistant_events",
            {
                "user_id": user_id,
                "session_id": session_id or None,
                "event_type": event_type,
                "action": str(payload.get("action") or "").strip() or None,
                "route": str(payload.get("route") or "").strip() or None,
                "topic": str(payload.get("topic") or "").strip() or None,
                "source_type": str(payload.get("source_type") or "").strip() or None,
                "source_id": str(payload.get("source_id") or "").strip() or None,
                "correct": _to_int(payload.get("correct"), 0) if payload.get("correct") is not None else None,
                "total": _to_int(payload.get("total"), 0) if payload.get("total") is not None else None,
                "percent": _to_int(payload.get("percent"), 0) if payload.get("percent") is not None else None,
                "message": _shorten(payload.get("message"), 500) or None,
                "created_at": _utc_now(),
            },
            prefer="return=minimal",
            **self._sb_auth(token),
        )

    def _combine_knowledge(
        self,
        active: Optional[dict[str, Any]],
        figures: list[dict[str, Any]],
        payload_materials: list[dict[str, Any]],
        materials: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in ([active] if active else []) + payload_materials + figures + materials:
            key = (str((item or {}).get("source_type") or ""), str((item or {}).get("id") or ""))
            if not item or key in seen:
                continue
            seen.add(key)
            result.append(item)
            if len(result) >= 4:
                break
        return result

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("Message is required")

        lang = str(payload.get("language") or payload.get("lang") or "kk").strip().lower()
        page_context = payload.get("page_context") if isinstance(payload.get("page_context"), dict) else {}
        payload_profile = payload.get("user_profile") if isinstance(payload.get("user_profile"), dict) else {}
        history = self._sanitize_history(payload.get("history"))
        session_id = _normalize_uuid(payload.get("session_id"))

        identity = self._identity(payload)
        user_id = identity["verified_user_id"]
        token = identity["access_token"]

        profile_row = self._profile_row(user_id, token) if user_id else None
        stats_row = self._stats_row(user_id, token) if user_id else None
        material_rows = self._materials_rows(user_id, token) if user_id else []
        payload_sources = self._payload_sources(payload)
        active_material = self._active_material(payload)
        figures = self.knowledge.search(message, lang=lang, limit=3)
        materials = self._search_materials(material_rows, message, limit=3)
        payload_materials = self._search_payload_sources(payload_sources, message, limit=3)
        knowledge = self._combine_knowledge(active_material, figures, payload_materials, materials)
        summary = self._summary(user_id, token, material_rows) if user_id else {
            "top_actions": [],
            "weak_topics": [],
            "recent_topics": [],
            "last_quiz": None,
            "recent_errors": [],
            "recent_event_types": [],
            "material_sources": [],
        }
        if payload_sources:
            summary["material_sources"] = (
                [self._material_summary(row) for row in payload_sources[:4]]
                + list(summary.get("material_sources") or [])
            )[:4]
        profile = self._profile(payload_profile, identity["auth_user"], profile_row, stats_row)

        raw = get_openai_service().generate_assistant_response(
            message=message,
            lang=lang,
            chat_history=history,
            user_profile=profile,
            page_context=page_context,
            experience_summary=summary,
            knowledge_matches=knowledge,
            active_material_excerpt=(active_material or {}).get("material_text", ""),
        )

        response = {
            "message": str(raw.get("message") or "").strip() or "I can help with navigation, quizzes, and study suggestions.",
            "intent": str(raw.get("intent") or "answer").strip().lower() or "answer",
            "actions": [],
            "suggested_prompts": [],
            "citations": [],
            "session": None,
            "summary": summary,
        }

        for item in raw.get("actions") or []:
            normalized = self._normalize_action(item)
            if normalized:
                response["actions"].append(normalized)

        for item in raw.get("suggested_prompts") or []:
            text = str(item or "").strip()
            if text:
                response["suggested_prompts"].append(text[:120])

        for item in raw.get("citations") or []:
            if isinstance(item, dict) and str(item.get("title") or "").strip() and str(item.get("source_id") or "").strip():
                response["citations"].append(
                    {
                        "source_type": str(item.get("source_type") or "historical_figure"),
                        "source_id": str(item.get("source_id") or ""),
                        "title": str(item.get("title") or ""),
                    }
                )

        if not response["actions"]:
            route = self._fallback_route(message)
            if route:
                response["actions"].append(
                    {
                        "type": "navigate",
                        "label": route.replace("_", " ").title(),
                        "route": route,
                        "params": {},
                    }
                )
            elif knowledge and any(token_text in _normalize_text(message) for token_text in ("test", "quiz", "practice", "question")):
                top = knowledge[0]
                response["actions"].append(
                    {
                        "type": "start_quiz",
                        "label": f"Quiz: {top['title']}",
                        "mode": "practice",
                        "count": 10,
                        "source_type": top.get("source_type") or "historical_figure",
                        "source_id": top["id"],
                    }
                )

        if not response["citations"] and knowledge:
            response["citations"] = [
                {
                    "source_type": item.get("source_type", "historical_figure"),
                    "source_id": item["id"],
                    "title": item["title"],
                }
                for item in knowledge[:3]
            ]

        if not response["suggested_prompts"]:
            response["suggested_prompts"] = [
                "Open my library",
                "Create a quiz on my materials",
                "What should I study next?",
            ]

        if user_id:
            session = self._ensure_session(
                user_id,
                token,
                session_id,
                message,
                str(page_context.get("route") or ""),
            )
            if session:
                session_id = str(session.get("id") or "")
                self._append_message(user_id, token, session_id, "user", message)
                self._append_message(
                    user_id,
                    token,
                    session_id,
                    "assistant",
                    response["message"],
                    response["intent"],
                    response["actions"],
                    response["citations"],
                )
                session = self._update_session(
                    user_id,
                    token,
                    session_id,
                    {
                        "title": str(session.get("title") or _shorten(message or "New chat", 72) or "New chat"),
                        "last_message_preview": _shorten(response["message"], 180),
                        "last_intent": response["intent"],
                        "last_route": next((item.get("route") for item in response["actions"] if item.get("type") == "navigate"), str(page_context.get("route") or "") or None),
                        "updated_at": _utc_now(),
                        "last_message_at": _utc_now(),
                    },
                ) or session
                response["session"] = {
                    "id": str(session.get("id") or ""),
                    "title": str(session.get("title") or "New chat"),
                    "preview": str(session.get("last_message_preview") or ""),
                    "last_intent": session.get("last_intent"),
                    "last_route": session.get("last_route"),
                    "created_at": session.get("created_at"),
                    "updated_at": session.get("updated_at"),
                    "last_message_at": session.get("last_message_at"),
                }
                self._record_event(
                    user_id,
                    token,
                    session_id,
                    "assistant_message",
                    {
                        "action": response["intent"],
                        "route": page_context.get("route"),
                        "topic": response["citations"][0]["title"] if response["citations"] else "",
                        "source_type": response["citations"][0]["source_type"] if response["citations"] else "",
                        "source_id": response["citations"][0]["source_id"] if response["citations"] else "",
                    },
                )
                response["summary"] = self._summary(user_id, token, material_rows)

        return response

    async def generate_quiz(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(payload)
        user_id = identity["verified_user_id"]
        token = identity["access_token"]
        lang = str(payload.get("language") or payload.get("lang") or "kk").strip().lower()
        mode = str(payload.get("mode") or "practice").strip().lower()
        count = _to_int(payload.get("count"), 10)
        count = count if count in {10, 15, 20, 25, 30} else 10
        source_type = str(payload.get("source_type") or "historical_figure").strip()

        source = None
        if source_type == "historical_figure":
            source = self.knowledge.get_by_id(payload.get("source_id"), lang=lang)
            if not source and payload.get("query"):
                matches = self.knowledge.search(str(payload.get("query")), lang=lang, limit=1)
                source = matches[0] if matches else None
        elif source_type == "material":
            direct_material = str(payload.get("material_text") or "").strip()
            source_id = str(payload.get("source_id") or "").strip()
            resolved = self.material_resolver({"material_id": source_id}) if source_id else ""
            if direct_material:
                source = {
                    "source_type": "material",
                    "id": source_id or "external-material",
                    "title": str(payload.get("source_title") or payload.get("title") or "User material"),
                    "material_text": _shorten(direct_material, 7000),
                }
            elif resolved:
                source = {
                    "source_type": "material",
                    "id": source_id,
                    "title": str(payload.get("source_title") or payload.get("title") or "Current material"),
                    "material_text": _shorten(resolved, 7000),
                }
            if source is None and user_id:
                rows = self._materials_rows(user_id, token, 12)
                for row in rows:
                    if str(row.get("id") or "") == source_id:
                        source = self._material_summary(row)
                        break
                if source is None and payload.get("query"):
                    matches = self._search_materials(rows, str(payload.get("query")), limit=1)
                    source = matches[0] if matches else None
        else:
            raise ValueError("Unsupported assistant quiz source")

        if not source or not str(source.get("material_text") or "").strip():
            raise ValueError("Knowledge source not found")

        openai_service = get_openai_service()
        result = await (
            openai_service.generate_realtest_questions(source["material_text"], count, lang)
            if mode == "realtest"
            else openai_service.generate_practice_questions(source["material_text"], count, [], lang)
        )
        return {
            "mode": mode,
            "source": {"type": source_type, "id": source["id"], "title": source["title"]},
            "questions": result.get("questions") or [],
            "citations": [
                {
                    "source_type": source_type,
                    "source_id": source["id"],
                    "title": source["title"],
                }
            ],
        }

    def record_experience(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(payload)
        user_id = identity["verified_user_id"]
        token = identity["access_token"]
        event_type = str(payload.get("event_type") or payload.get("type") or "unknown").strip()
        event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        session_id = _normalize_uuid(payload.get("session_id"))
        if user_id:
            self._record_event(user_id, token, session_id, event_type, event_payload)
            return {"ok": True, "summary": self._summary(user_id, token, self._materials_rows(user_id, token, 8)), "updated_at": _utc_now()}
        return {"ok": True, "summary": {}, "updated_at": _utc_now()}

    def list_sessions(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(payload)
        user_id = identity["verified_user_id"]
        token = identity["access_token"]
        if not user_id:
            return {"sessions": [], "summary": {}, "user_profile": None}
        sessions = self.supabase.select(
            "assistant_sessions",
            params={
                "select": "id,title,last_message_preview,last_intent,last_route,created_at,updated_at,last_message_at",
                "user_id": f"eq.{user_id}",
                "order": "last_message_at.desc",
                "limit": str(_to_int(payload.get("limit"), 24) or 24),
            },
            **self._sb_auth(token),
        )
        materials = self._materials_rows(user_id, token, 8)
        return {
            "sessions": [
                {
                    "id": str(item.get("id") or ""),
                    "title": str(item.get("title") or "New chat"),
                    "preview": str(item.get("last_message_preview") or ""),
                    "last_intent": item.get("last_intent"),
                    "last_route": item.get("last_route"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "last_message_at": item.get("last_message_at"),
                }
                for item in sessions
            ],
            "summary": self._summary(user_id, token, materials),
            "user_profile": self._profile({}, identity["auth_user"], self._profile_row(user_id, token), self._stats_row(user_id, token)),
        }

    def get_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(payload)
        user_id = identity["verified_user_id"]
        token = identity["access_token"]
        session_id = _normalize_uuid(payload.get("session_id"))
        session = self._session_row(user_id, token, session_id) if user_id else None
        if not session:
            raise ValueError("Session not found")
        messages = self.supabase.select(
            "assistant_messages",
            params={
                "select": "id,role,title,content,intent,actions,citations,created_at",
                "user_id": f"eq.{user_id}",
                "session_id": f"eq.{session_id}",
                "order": "created_at.asc",
                "limit": str(_to_int(payload.get("limit"), 120) or 120),
            },
            **self._sb_auth(token),
        )
        return {
            "session": {
                "id": str(session.get("id") or ""),
                "title": str(session.get("title") or "New chat"),
                "preview": str(session.get("last_message_preview") or ""),
                "last_intent": session.get("last_intent"),
                "last_route": session.get("last_route"),
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
                "last_message_at": session.get("last_message_at"),
            },
            "messages": [
                {
                    "id": str(item.get("id") or ""),
                    "role": str(item.get("role") or "assistant"),
                    "title": str(item.get("title") or ""),
                    "content": str(item.get("content") or ""),
                    "intent": item.get("intent"),
                    "actions": item.get("actions") if isinstance(item.get("actions"), list) else [],
                    "citations": item.get("citations") if isinstance(item.get("citations"), list) else [],
                    "created_at": item.get("created_at"),
                }
                for item in messages
            ],
        }


_assistant_service: Optional[AssistantService] = None


def get_assistant_service(material_resolver: Callable[[dict[str, Any]], str]) -> AssistantService:
    global _assistant_service
    if _assistant_service is None:
        _assistant_service = AssistantService(material_resolver)
    return _assistant_service
