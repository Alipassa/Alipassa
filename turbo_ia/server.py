"""Servidor local do painel (somente 127.0.0.1). Sem dependências além do pacote MetaTrader5 (opcional)."""

from __future__ import annotations

import argparse
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, b3
from .analysis import analyze, series
from .broker import BrokerError, lot_for_risk, make_broker
from .engine import IAConfig, IASession
from .ensemble import EnsembleSession

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
            return {"version": __version__, "preset": os.environ.get("TURBO_IA_PRESET", ""), "account": info, "session": self.session.state(), "positions": self.broker.positions(),
                    "telegram": self.session.notifier.status()}

    def chart(self, symbol: str, tf: str, n: int, mode: str, z_thr: Optional[float] = None) -> dict:
        with self.lock:
            self.broker.update()
            candles = self.broker.candles(symbol, tf, n)
            bid, ask = self.broker.tick(symbol)
            digits = self.broker.spec(symbol).get("digits", 2)
        s = series(candles, z_thr if z_thr else 2.0)
        a = analyze(candles[:-1], mode, utc_offset=self.broker.utc_offset, z_thr=z_thr) if len(candles) > 1 else None
        return {"symbol": symbol, "tf": tf, "candles": candles, "series": s, "analysis": a.to_dict() if a else None,
                "bid": bid, "ask": ask, "digits": digits, "positions": self._chart_positions(symbol)}

    def _chart_positions(self, symbol: str) -> list:
        root = b3.root_of(symbol)
        if not root:
            return self.broker.positions(symbol)
        return [p for p in self.broker.positions() if b3.root_of(p["symbol"]) == root]   # WIN@ no gráfico, posição no contrato

    def start_ia(self, d: dict) -> dict:
        mode = d.get("mode", "reversao")
        if mode == "b3":                           # WIN M15 swing: o mesmo cruzamento, aprovado na robustez da B3
            if b3.root_of(str(d.get("symbol", ""))) not in ("WIN", "IND"):
                return {"ok": False, "error": "modo B3 só no mini índice: escolha WIN@ (o WDO reprovou nos testes)"}
            d = {**d, "mode": "cruzamento", "timeframe": "M15", "fast": 9, "slow": 20, "sl_atr": 2.0, "rr": 0}
            mode = "cruzamento"
        if mode not in ("reversao", "cruzamento", "ensemble"):   # só estratégias que passaram nos testes
            return {"ok": False, "error": "modo não habilitado (Reversão ouro, Cruzamento 9×20, Ensemble E3 ou B3 WIN M15)"}
        with self.lock:
            if self.session.st.running:
                return {"ok": False, "error": "IA já está rodando"}
            try:
                info = self.broker.info()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"MT5 sem conta logada: {e}"}
            if not info.get("demo") and not info.get("allow_real"):
                return {"ok": False, "error": "conta REAL no MT5 — a IA só liga em conta DEMO (TURBO_IA_ALLOW_REAL=0)"}
            want = EnsembleSession if mode == "ensemble" else IASession
            if not isinstance(self.session, want):          # troca de sistema único ↔ ensemble (IA parada)
                self.session = want(self.broker, forward=self.session.forward, notifier=self.session.notifier)
            r = self.session.start(IAConfig.from_dict(d))
        if r.get("ok") and info.get("algo_trading") is False:
            w = "⚠️ o botão Algo Trading do MetaTrader está DESLIGADO: ligue (fica verde) ou as ordens serão recusadas"
            self.session.log(w, "error")
            r["warning"] = (r.get("warning") + "\n" if r.get("warning") else "") + w
        return r

    def preset_config(self) -> dict:
        """Configuração validada de cada pacote: b3 = WIN M15 cruzamento swing; padrão = ensemble E3 no ouro. Risco 2 %."""
        base = {"risk_pct": 2, "sl_atr": 2, "rr": 0, "duration_candles": 0, "target_pct": 0, "loss_pct": 50,
                "daily_loss_pct": 10, "protect": False, "stop_block": "", "target_profit": 0, "loss_limit": 0}
        try:
            syms = self.broker.symbols()
        except Exception:  # noqa: BLE001
            syms = []
        if os.environ.get("TURBO_IA_PRESET", "") == "b3":
            sym = next((s for s in ("WIN@", "WIN$N", "WIN$", "WIN@N") if s in syms), None)
            if sym is None:                         # fora da Observação do Mercado: tenta habilitar
                for s in ("WIN@", "WIN$N", "WIN$"):
                    try:
                        self.broker.candles(s, "M15", 5)
                        sym = s
                        break
                    except Exception:  # noqa: BLE001
                        continue
            return {**base, "mode": "b3", "symbol": sym or "WIN@", "timeframe": "M15"}
        gold = next((s for s in syms if s.upper().startswith(("XAUUSD", "GOLD"))), "XAUUSD")
        return {**base, "mode": "ensemble", "symbol": gold, "timeframe": "H1"}

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
        root = b3.root_of(symbol or "")
        if root and b3.is_continuous(symbol) and hasattr(self.broker, "contracts"):   # B3: WIN@ não é negociável → contrato vigente
            import time as _time
            tsym = b3.pick_contract(root, self.broker.contracts(root), _time.time())
            bid, ask = self.broker.tick(tsym)
            fill = ask if side == "BUY" else bid
            if sl:
                sl = fill - abs(price - sl) if side == "BUY" else fill + abs(price - sl)
            if tp:
                tp = fill + abs(tp - price) if side == "BUY" else fill - abs(tp - price)
            symbol, price = tsym, fill
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

        def _local_host(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "")

        def do_GET(self):  # noqa: N802
            if not self._local_host():
                return self._json({"error": "acesso só pelo próprio computador"}, 403)
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
                    self._json(app.chart(q.get("symbol", ""), q.get("tf", "M1"), int(q.get("n", 300)), q.get("mode", "veloz"),
                                         float(q["z"]) if q.get("z") else None))
                else:
                    self._json({"error": "não encontrado"}, 404)
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e)}, 500)

        def do_POST(self):  # noqa: N802
            # o painel só aceita chamadas da própria origem (evita outro site disparar ordens no seu MT5)
            if not self._local_host():
                return self._json({"error": "acesso só pelo próprio computador"}, 403)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return self._json({"error": "origem não permitida"}, 403)
            try:
                d = self._body()
                p = urlparse(self.path).path
                if p == "/api/ia/start":
                    self._json(app.start_ia(d))
                elif p == "/api/ia/autostart":                 # botão "LIGAR TUDO": configuração validada do pacote
                    self._json(app.start_ia(app.preset_config()))
                elif p == "/api/telegram/test":
                    n = app.session.notifier
                    if not n.enabled:
                        self._json({"ok": False, "error": "Telegram desligado: coloque TOKEN_TELEGRAM e CHAT_ID no arquivo .env e reinicie o painel"})
                    else:
                        app.session.notify("🔔 Teste: os alertas da TURBO IA estão chegando aqui.")
                        n.flush(12)
                        self._json({"ok": not n.last_error, "error": n.last_error or None})
                elif p == "/api/ia/stop":
                    self._json(app.session.stop())
                elif p == "/api/analyze":
                    with app.lock:
                        candles = app.broker.candles(d["symbol"], d.get("timeframe", "M1"), 300)
                    self._json(analyze(candles[:-1], d.get("mode", "veloz"), utc_offset=app.broker.utc_offset,
                                       z_thr=float(d["z_thr"]) if d.get("z_thr") else None).to_dict())
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
    ap.add_argument("--autostart", action="store_true", help="liga a IA sozinho com a configuração validada (LIGAR TUDO)")
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
    if a.autostart:
        r = app.start_ia(app.preset_config())
        cfg = app.session.cfg
        print(("IA LIGADA · " + f"{cfg.symbol} {cfg.timeframe} · risco {cfg.risk_pct:g}%") if r.get("ok") else f"IA NÃO ligou: {r.get('error')}")
        if r.get("warning"):
            print(r["warning"])
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
