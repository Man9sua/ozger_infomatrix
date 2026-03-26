from __future__ import annotations

import json
import os
from typing import Any, Optional
from urllib import error, parse, request


class SupabaseServiceError(RuntimeError):
    """Raised when Supabase REST returns an unexpected response."""


class SupabaseService:
    """Minimal Supabase REST client for assistant-side retrieval and storage."""

    def __init__(self):
        self.base_url = str(os.getenv("SUPABASE_URL") or "").rstrip("/")
        self.anon_key = str(os.getenv("SUPABASE_ANON_KEY") or "").strip()
        self.service_role_key = str(os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    @property
    def available(self) -> bool:
        return bool(self.base_url and (self.service_role_key or self.anon_key))

    def _api_key(self, use_service_role: bool = False) -> str:
        if use_service_role and self.service_role_key:
            return self.service_role_key
        return self.anon_key or self.service_role_key

    def _headers(
        self,
        *,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
        prefer: Optional[str] = None,
        accept: str = "application/json",
    ) -> dict[str, str]:
        api_key = self._api_key(use_service_role=use_service_role)
        headers = {
            "apikey": api_key,
            "Accept": accept,
            "Content-Type": "application/json",
        }
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Optional[dict[str, Any]] = None,
        payload: Optional[Any] = None,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
        prefer: Optional[str] = None,
        timeout: int = 10,
        allow_404: bool = False,
    ) -> Any:
        if not self.available:
            raise SupabaseServiceError("Supabase is not configured")

        base = path if path.startswith("http") else f"{self.base_url}{path}"
        if params:
            query = parse.urlencode(
                {key: value for key, value in params.items() if value is not None},
                doseq=True,
            )
            url = f"{base}?{query}"
        else:
            url = base

        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = request.Request(
            url,
            data=body,
            method=method.upper(),
            headers=self._headers(
                auth_token=auth_token,
                use_service_role=use_service_role,
                prefer=prefer,
            ),
        )

        try:
            with request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="ignore")
            if allow_404 and exc.code == 404:
                return None
            message = raw
            try:
                payload = json.loads(raw)
                message = str(payload.get("message") or payload.get("error") or raw)
            except Exception:
                pass
            raise SupabaseServiceError(
                f"Supabase request failed ({exc.code}): {message.strip() or exc.reason}"
            ) from exc
        except error.URLError as exc:
            raise SupabaseServiceError(f"Supabase network error: {exc.reason}") from exc

    def verify_user(self, access_token: Optional[str]) -> Optional[dict[str, Any]]:
        token = str(access_token or "").strip()
        if not token or not self.available:
            return None
        return self._request_json(
            "/auth/v1/user",
            auth_token=token,
            use_service_role=False,
            timeout=8,
            allow_404=True,
        )

    def select(
        self,
        table: str,
        *,
        params: Optional[dict[str, Any]] = None,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
        timeout: int = 10,
    ) -> list[dict[str, Any]]:
        data = self._request_json(
            f"/rest/v1/{table}",
            params=params or {},
            auth_token=auth_token,
            use_service_role=use_service_role,
            timeout=timeout,
        )
        return data if isinstance(data, list) else []

    def insert(
        self,
        table: str,
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
        prefer: str = "return=representation",
    ) -> list[dict[str, Any]]:
        data = self._request_json(
            f"/rest/v1/{table}",
            method="POST",
            payload=payload,
            auth_token=auth_token,
            use_service_role=use_service_role,
            prefer=prefer,
        )
        return data if isinstance(data, list) else []

    def upsert(
        self,
        table: str,
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        on_conflict: Optional[str] = None,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
    ) -> list[dict[str, Any]]:
        params = {"on_conflict": on_conflict} if on_conflict else None
        data = self._request_json(
            f"/rest/v1/{table}",
            method="POST",
            params=params,
            payload=payload,
            auth_token=auth_token,
            use_service_role=use_service_role,
            prefer="resolution=merge-duplicates,return=representation",
        )
        return data if isinstance(data, list) else []

    def update(
        self,
        table: str,
        filters: dict[str, Any],
        payload: dict[str, Any],
        *,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
        prefer: str = "return=representation",
    ) -> list[dict[str, Any]]:
        data = self._request_json(
            f"/rest/v1/{table}",
            method="PATCH",
            params=filters,
            payload=payload,
            auth_token=auth_token,
            use_service_role=use_service_role,
            prefer=prefer,
        )
        return data if isinstance(data, list) else []

    def delete(
        self,
        table: str,
        filters: dict[str, Any],
        *,
        auth_token: Optional[str] = None,
        use_service_role: bool = False,
        prefer: str = "return=representation",
    ) -> list[dict[str, Any]]:
        data = self._request_json(
            f"/rest/v1/{table}",
            method="DELETE",
            params=filters,
            auth_token=auth_token,
            use_service_role=use_service_role,
            prefer=prefer,
        )
        return data if isinstance(data, list) else []
