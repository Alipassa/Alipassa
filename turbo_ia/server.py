"""Servidor local do painel (somente 127.0.0.1). Sem dependências além do pacote MetaTrader5 (opcional)."""

from __future__ import annotations

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .analysis import analyze, series
from .broker import BrokerError, lot_for_risk, make_broker
from .engine import IAConfig, IASession

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class App:
    def __init__(self, broker: Any) -> None:
        self.broker = broker
        self.session = IASession(broker)
        self.lock = threading.RLock()

    def status(self) -> dict:
        with self.lock:
            self.broker.update()
            if not self.session.st.running:          # com a IA parada, ainda confere as entradas pendentes do registro
                self.session.forward.resolve(self.broker)
            try:
                info = self.broker.info()
            except BrokerError as e:
                info = {"broker": self.broker.name, "error": str(e)}
            return {"version": __version__, "account": info, "session": self.session.state(), "positions": self.broker.positions()}

    def chart(self, symbol: str, tf: str, n: int, mode: str) -> dict:
        with self.lock:
            self.broker.update()
            candles = self.broker.candles(symbol, tf, n)
            bid, ask = self.broker.tick(symbol)
            digits = self.broker.spec(symbol).get("digits", 2)
        s = series(candles)
        a = analyze(candles[:-1], mode, utc_offset=self.broker.utc_offset) if len(candles) > 1 else None
        return {"symbol": symbol, "tf": tf, "candles": candles, "series": s, "analysis": a.to_dict() if a else None,
                "bid": bid, "ask": ask, "digits": digits, "positions": self.broker.positions(symbol)}

    def manual_order(self, d: dict) -> dict:
        symbol, side = d.get("symbol"), str(d.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            return {"ok": False, "error": "side deve ser BUY ou SELL"}
        with self.lock:
            candles = self.broker.candles(symbol, d.get("timeframe", "M1"), 300)
        a = analyze(candles[:-1], d.get("mode", "veloz"), float(d.get("rr", 1.5)), float(d.get("sl_atr", 1.5)), self.broker.utc_offset)
        price = candles[-1]["close"]
        dist = float(d.get("sl_atr", 1.5)) * (a.atr or 0)
        sl = (price - dist if side == "BUY" else price + dist) if dist else None
        tp = (price + float(d.get("rr", 1.5)) * dist if side == "BUY" else price - float(d.get("rr", 1.5)) * dist) if dist else None
        volume = float(d.get("volume") or 0.01)
        risk_pct = float(d.get("risk_pct") or 0)
        if risk_pct > 0:                                   # mesmo cálculo da IA: lote para perder risk_pct % no stop
            if not sl:
                return {"ok": False, "error": "sem ATR para calcular o stop"}
            eq = float(self.broker.info().get("equity") or 0)
            volume, _risk, note = lot_for_risk(self.broker.spec(symbol), price, sl, eq, min(risk_pct, 10.0))
            if not volume:
                return {"ok": False, "error": note}
        res = self.broker.open(symbol, side, volume, sl, tp, "TurboIA manual")
        self.session.log(f"manual {side} {symbol}: {'ok #' + str(res.get('ticket')) if res.get('ok') else res.get('error')}", "trade" if res.get("ok") else "error")
        return res


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silencioso
            pass

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/api/status":
                    self._json(app.status())
                elif u.path == "/api/forward":
                    self._json(app.session.forward.stats(q.get("mode") or None, app.broker.name, q.get("kind") or None, float(q.get("rr") or 2)))
                elif u.path == "/api/symbols":
                    self._json({"symbols": app.broker.symbols()})
                elif u.path == "/api/chart":
                    self._json(app.chart(q.get("symbol", ""), q.get("tf", "M1"), int(q.get("n", 300)), q.get("mode", "veloz")))
                else:
                    self._json({"error": "não encontrado"}, 404)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)

        def do_POST(self):  # noqa: N802
            # o painel só aceita chamadas da própria origem (evita outro site disparar ordens no seu MT5)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return self._json({"error": "origem não permitida"}, 403)
            try:
                d = self._body()
                p = urlparse(self.path).path
                if p == "/api/ia/start":
                    self._json(app.session.start(IAConfig.from_dict(d)))
                elif p == "/api/ia/stop":
                    self._json(app.session.stop())
                elif p == "/api/analyze":
                    with app.lock:
                        candles = app.broker.candles(d["symbol"], d.get("timeframe", "M1"), 300)
                    self._json(analyze(candles[:-1], d.get("mode", "veloz"), utc_offset=app.broker.utc_offset).to_dict())
                elif p == "/api/order":
                    self._json(app.manual_order(d))
                elif p == "/api/close":
                    with app.lock:
                        self._json(app.broker.close(int(d["ticket"])))
                else:
                    self._json({"error": "não encontrado"}, 404)
            except Exception as e:  # noqa: BLE001
                self._json({"ok": False, "error": str(e)}, 500)

    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia", description="Painel TURBO IA: análise do gráfico + execução no MT5")
    ap.add_argument("--broker", choices=["auto", "mt5", "paper"], default="auto")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    broker = make_broker(a.broker)
    app = App(broker)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    url = f"http://127.0.0.1:{a.port}"
    info = broker.info()
    print(f"TURBO IA {__version__} · corretora: {broker.name} · conta {info.get('login')} ({'DEMO' if info.get('demo') else 'REAL'})"
          + (f" · terminal {broker.path}" if getattr(broker, "path", None) else ""))
    print(f"painel: {url}   (Ctrl+C para sair)")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.session.stop("servidor encerrado")
        srv.server_close()
    return 0
