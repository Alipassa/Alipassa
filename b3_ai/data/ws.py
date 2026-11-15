"""Cliente WebSocket mínimo (RFC 6455) só com a biblioteca padrão — TLS, proxy HTTP CONNECT, ping/pong.

Suficiente para ler o feed de histórico da TradingView; não é um cliente WebSocket genérico completo.
"""

from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Optional


class WsError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, origin: Optional[str] = None, timeout: float = 20.0,
                 user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36") -> None:
        u = urllib.parse.urlparse(url)
        if u.scheme != "wss":
            raise WsError("apenas wss:// é suportado")
        host, port = u.hostname or "", u.port or 443
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock = self._connect(host, port, timeout)
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", f"User-Agent: {user_agent}"]
        if origin:
            lines.append(f"Origin: {origin}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise WsError(f"handshake recusado: {status}")

    @staticmethod
    def _connect(host: str, port: int, timeout: float) -> ssl.SSLSocket:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            p = urllib.parse.urlparse(proxy)
            raw = socket.create_connection((p.hostname, p.port or 8080), timeout=timeout)
            req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            if p.username:
                cred = base64.b64encode(f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}".encode()).decode()
                req += f"Proxy-Authorization: Basic {cred}\r\n"
            raw.sendall((req + "\r\n").encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                resp += chunk
            if b" 200" not in resp.split(b"\r\n", 1)[0]:
                raw.close()
                raise WsError(f"proxy recusou o túnel: {resp.split(b'\r\n', 1)[0].decode(errors='replace')}")
        else:
            raw = socket.create_connection((host, port), timeout=timeout)
        raw.settimeout(timeout)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)

    # ------------------------------------------------------------------ leitura
    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WsError("conexão encerrada pelo servidor")
        self.buf += chunk

    def _read_until(self, mark: bytes) -> bytes:
        while mark not in self.buf:
            self._fill()
        i = self.buf.index(mark) + len(mark)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> str:
        """Próxima mensagem de texto completa (responde pings; junta fragmentos)."""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if op == 0x8:
                raise WsError("servidor fechou o WebSocket")
            if op == 0x9:
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ escrita
    def _send(self, op: int, data: bytes) -> None:
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def send(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
