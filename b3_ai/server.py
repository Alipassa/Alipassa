"""Servidor local do painel (Python puro). Somente leitura: nenhuma rota envia ordens."""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import opinion, report
from .engine import Engine

HTML_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


class Service:
    def __init__(self, engine: Engine, interval: int = 60) -> None:
        self.engine = engine
        self.interval = max(20, interval)
        self.wake = threading.Event()
        self.busy = threading.Lock()
        self.last_error = ""

    def run_once(self) -> bool:
        with self.busy:
            try:
                self.engine.cycle()
                self.last_error = ""
                return True
            except Exception as e:  # noqa: BLE001 — o painel continua no ar com o último estado
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"[b3-ai] ciclo falhou: {self.last_error}")
                if os.environ.get("B3AI_DEBUG"):
                    traceback.print_exc()
                return False

    def loop(self) -> None:
        while True:
            self.run_once()
            self.wake.wait(self.interval)
            self.wake.clear()


def make_handler(svc: Service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # silencioso
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(json.dumps(obj, ensure_ascii=False, allow_nan=False, default=lambda o: None).encode(), "application/json; charset=utf-8", code)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            eng = svc.engine
            if path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as f:
                    self._send(f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                with eng.lock:
                    st = dict(eng.state)
                st["erro_ciclo"] = svc.last_error
                st["intervalo"] = svc.interval
                self._json(_clean(st))
            elif path == "/api/b3":
                self._json(_clean({"papeis": eng.b3.get("papeis", [])}))
            elif path == "/api/prompt":
                with eng.lock:
                    st = eng.state
                if st.get("ok"):
                    self._send(opinion.prompt(st).encode(), "text/plain; charset=utf-8")
                else:
                    self._send("Ainda sem dados.".encode(), "text/plain; charset=utf-8", 503)
            elif path in ("/relatorio", "/relatorio.md"):
                with eng.lock:
                    st = eng.state
                if not st.get("ok"):
                    self._send("Ainda sem dados — aguarde a primeira leitura.".encode(), "text/plain; charset=utf-8", 503)
                elif path.endswith(".md"):
                    self._send(report.markdown(st).encode(), "text/markdown; charset=utf-8")
                else:
                    self._send(report.to_html(st).encode(), "text/html; charset=utf-8")
            else:
                self._send(b"nao encontrado", "text/plain", 404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/refresh":
                svc.wake.set()
                self._json({"ok": True})
            elif self.path == "/api/analise":
                self._json(_clean(svc.engine.run_ai()))   # opinião local, instantânea
            else:
                self._send(b"nao encontrado", "text/plain", 404)

    return Handler


def _clean(o):
    """NaN/inf não existem em JSON: viram null."""
    if isinstance(o, float):
        return o if o == o and abs(o) != float("inf") else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def serve(engine: Engine, host: str = "127.0.0.1", port: int = 8766, interval: int = 60, open_browser: bool = True) -> None:
    svc = Service(engine, interval)
    threading.Thread(target=svc.loop, name="b3-ai-coleta", daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(svc))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"B3 AI — painel em {url}  (Ctrl+C para sair). A primeira leitura leva alguns segundos.")
    if open_browser:
        try:
            import webbrowser
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado.")
    finally:
        httpd.server_close()
        time.sleep(0.1)
