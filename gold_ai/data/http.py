"""Cliente HTTP mínimo (stdlib) com retries, timeout e cache em disco."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


class DataError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, cache_dir: Optional[str] = None, ttl: int = 60, timeout: int = 15, retries: int = 3,
                 user_agent: str = "Mozilla/5.0 (GoldAIEngine/2.0)") -> None:
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".cache")

    def _cache_get(self, url: str, ttl: int) -> Optional[str]:
        p = self._cache_path(url)
        if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
            with open(p, encoding="utf-8") as f:
                return f.read()
        return None

    def _cache_put(self, url: str, text: str) -> None:
        p = self._cache_path(url)
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)

    # ------------------------------------------------------------------ fetch
    def get_text(self, url: str, ttl: Optional[int] = None, headers: Optional[dict[str, str]] = None) -> str:
        ttl = self.ttl if ttl is None else ttl
        cached = self._cache_get(url, ttl)
        if cached is not None:
            return cached
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*", **(headers or {})})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    text = resp.read().decode("utf-8", errors="replace")
                self._cache_put(url, text)
                return text
            except urllib.error.HTTPError as e:  # pragma: no cover - rede
                if e.code == 429:   # limite de requisições: repetir em segundos só prolonga o bloqueio — quem decide a espera é o chamador
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    raise DataError(f"falha ao buscar {url}: HTTP Error 429: Too Many Requests" + (f" (Retry-After {retry_after}s)" if retry_after else "")) from e
                last = e
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
            except (urllib.error.URLError, TimeoutError, OSError) as e:  # pragma: no cover - rede
                last = e
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
        raise DataError(f"falha ao buscar {url}: {last}")

    def get_json(self, url: str, ttl: Optional[int] = None) -> Any:
        try:
            return json.loads(self.get_text(url, ttl))
        except json.JSONDecodeError as e:
            raise DataError(f"JSON inválido em {url}: {e}") from e
