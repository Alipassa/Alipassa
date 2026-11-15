"""HTTP mínimo (biblioteca padrão): GET/POST com retries e User-Agent de navegador."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class DataError(RuntimeError):
    pass


def fetch(url: str, data: Optional[bytes] = None, headers: Optional[dict[str, str]] = None,
          timeout: float = 20.0, retries: int = 2) -> str:
    h = {"User-Agent": UA, "Accept": "*/*"}
    h.update(headers or {})
    last: Exception = DataError("sem tentativa")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = DataError(f"HTTP {e.code} em {url.split('?')[0]}")
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001 — rede: qualquer falha vira DataError
            last = DataError(f"{type(e).__name__}: {e} em {url.split('?')[0]}")
        time.sleep(0.6 * (attempt + 1))
    raise last


def get_json(url: str, **kw: Any) -> Any:
    try:
        return json.loads(fetch(url, **kw))
    except json.JSONDecodeError as e:
        raise DataError(f"resposta não é JSON em {url.split('?')[0]}: {e}")


def post_json(url: str, payload: dict, **kw: Any) -> Any:
    body = json.dumps(payload).encode()
    try:
        return json.loads(fetch(url, data=body, headers={"Content-Type": "application/json"}, **kw))
    except json.JSONDecodeError as e:
        raise DataError(f"resposta não é JSON em {url}: {e}")
