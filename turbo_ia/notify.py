"""Alertas no Telegram: entradas, saídas, freio diário, erros e resumo do dia.

Configuração no `.env` da pasta (as mesmas chaves do gold_ai):
    TOKEN_TELEGRAM=123456789:AA...        (ou TELEGRAM_BOT_TOKEN)
    CHAT_ID=123456789                     (ou TELEGRAM_CHAT_ID)
Sem token/chat_id não envia nada (o painel mostra "Telegram: desligado").
O envio roda numa fila em segundo plano: uma falha de internet nunca atrasa nem derruba a IA.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None,
                 post: Optional[Callable[[str, dict], dict]] = None) -> None:
        env = os.environ
        self.token = token if token is not None else (env.get("TELEGRAM_BOT_TOKEN") or env.get("TOKEN_TELEGRAM") or "")
        self.chat_id = str(chat_id if chat_id is not None else (env.get("TELEGRAM_CHAT_ID") or env.get("CHAT_ID") or ""))
        self._post = post or self._http_post
        self.q: "queue.Queue[str]" = queue.Queue(maxsize=200)
        self.last_error = ""
        self.sent = 0
        self._recent: dict[str, float] = {}
        self._worker: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict:
        return {"enabled": self.enabled, "sent": self.sent, "last_error": self.last_error,
                "chat": (self.chat_id[:3] + "…" + self.chat_id[-2:]) if len(self.chat_id) > 5 else self.chat_id}

    def send(self, text: str, dedup_key: Optional[str] = None, dedup_s: float = 1800) -> bool:
        """Põe na fila. `dedup_key`: a mesma chave não repete dentro de `dedup_s` (ex.: o mesmo erro em loop)."""
        if not self.enabled:
            return False
        now = time.time()
        if dedup_key:
            if now - self._recent.get(dedup_key, 0) < dedup_s:
                return False
            self._recent[dedup_key] = now
        try:
            self.q.put_nowait(text[:3900])
        except queue.Full:
            return False
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run, name="telegram", daemon=True)
            self._worker.start()
        return True

    def flush(self, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.05)

    def _run(self) -> None:
        while True:
            try:
                text = self.q.get(timeout=30)
            except queue.Empty:
                return
            for attempt in range(3):
                try:
                    r = self._post(self.API.format(token=self.token), {"chat_id": self.chat_id, "text": text,
                                                                       "disable_web_page_preview": "true"})
                    if r.get("ok"):
                        self.sent += 1
                        self.last_error = ""
                        break
                    self.last_error = str(r.get("description") or r)[:200]
                    if r.get("error_code") in (400, 401, 403, 404):
                        break                              # token/chat errado: tentar de novo não resolve
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)[:200]
                time.sleep(2 * (attempt + 1))

    @staticmethod
    def _http_post(url: str, data: dict) -> dict:
        body = urllib.parse.urlencode(data).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, body), timeout=15) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:                # 400/401/403 vêm com JSON explicando o motivo
            try:
                return json.loads(e.read() or b"{}")
            except Exception:  # noqa: BLE001
                return {"ok": False, "error_code": e.code, "description": str(e)}
