"""Sessão da IA: ao clicar em "Iniciar IA" roda em segundo plano até bater a meta, o limite de perda ou ser parada.

Ciclo (a cada `poll` segundos):
  1. atualiza a corretora e acompanha a posição aberta (saída por tempo = "duração", SL/TP ficam no servidor)
  2. confere meta de lucro, limite de perda, proteção da sessão e máximo de operações
  3. quando FECHA um candle novo e não há posição aberta, analisa só candles fechados e, havendo sinal, envia a ordem
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .analysis import analyze

TF_SECONDS = {"M1": 60, "M2": 120, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


@dataclass
class IAConfig:
    symbol: str = "Volatility 10 Index"
    timeframe: str = "M1"
    mode: str = "veloz"                 # veloz | equilibrado | preciso
    volume: float = 0.5                 # lote por operação
    rr: float = 1.5                     # alvo = rr × distância do stop
    sl_atr: float = 1.5                 # stop padrão em ATRs
    duration_candles: int = 0           # >0 fecha a posição após N candles (estilo Rise/Fall); 0 = só SL/TP
    target_profit: float = 1.0          # meta da sessão (moeda da conta)
    loss_limit: float = 100.0           # perda máxima da sessão
    max_trades: int = 50
    protect: bool = True                # proteção: depois de lucrar `protect_after`, não devolve mais que até `protect_keep`
    protect_after: float = 0.70
    protect_keep: float = 0.14

    @classmethod
    def from_dict(cls, d: dict) -> "IAConfig":
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k) and v is not None and v != "":
                cur = getattr(c, k)
                setattr(c, k, type(cur)(v) if not isinstance(cur, bool) else (v in (True, "true", "1", 1, "on")))
        return c


@dataclass
class SessionState:
    running: bool = False
    started_at: Optional[float] = None
    stopped_reason: str = ""
    pnl: float = 0.0
    peak: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    open_ticket: Optional[int] = None
    open_bar: Optional[int] = None
    last_bar: Optional[int] = None
    last_analysis: Optional[dict] = None
    log: list = field(default_factory=list)
    history: list = field(default_factory=list)


class IASession:
    def __init__(self, broker: Any, poll: float = 1.0) -> None:
        self.broker = broker
        self.poll = poll
        self.cfg = IAConfig()
        self.st = SessionState()
        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ controle
    def log(self, msg: str, kind: str = "info") -> None:
        with self.lock:
            self.st.log.append({"t": time.time(), "kind": kind, "msg": msg})
            del self.st.log[:-200]

    def start(self, cfg: IAConfig) -> dict:
        with self.lock:
            if self.st.running:
                return {"ok": False, "error": "IA já está rodando"}
            self.cfg = cfg
            self.st = SessionState(running=True, started_at=time.time())
            self._stop.clear()
        self.log(f"IA iniciada · {cfg.symbol} {cfg.timeframe} · modo {cfg.mode} · lote {cfg.volume} · meta {cfg.target_profit} · limite {cfg.loss_limit}", "start")
        self._thread = threading.Thread(target=self._run, name="turbo-ia", daemon=True)
        self._thread.start()
        return {"ok": True}

    def stop(self, reason: str = "parada pelo usuário") -> dict:
        self._stop.set()
        with self.lock:
            if self.st.running:
                self.st.running = False
                self.st.stopped_reason = reason
                self.log(f"IA parada: {reason}", "stop")
        return {"ok": True}

    def state(self) -> dict:
        with self.lock:
            d = asdict(self.st)
            d["config"] = asdict(self.cfg)
            return d

    # ------------------------------------------------------------------ laço
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                self.log(f"erro no ciclo: {e}", "error")
            self._stop.wait(self.poll)

    def step(self) -> None:
        """Um ciclo da IA (público para testes)."""
        b, c = self.broker, self.cfg
        b.update()
        self._manage_open()
        if not self.st.running:
            return
        if self._check_limits():
            return
        candles = b.candles(c.symbol, c.timeframe, 300)
        if len(candles) < 2:
            return
        bar = int(candles[-1]["time"])
        if self.st.last_bar is None:
            self.st.last_bar = bar
            self.log("aguardando o fechamento do próximo candle para analisar…")
            return
        if bar == self.st.last_bar:
            return
        self.st.last_bar = bar
        closed = candles[:-1]                     # só candles fechados: sem repintura
        a = analyze(closed, c.mode, c.rr, c.sl_atr)
        with self.lock:
            self.st.last_analysis = a.to_dict()
        if self.st.open_ticket is not None:
            return
        if a.signal == "NONE":
            self.log(f"sem entrada ({a.label}): {'; '.join(a.blocked)}", "skip")
            return
        res = b.open(c.symbol, a.signal, c.volume, a.sl, a.tp, f"TurboIA {c.mode}")
        if not res.get("ok"):
            self.log(f"ordem recusada: {res.get('error')}", "error")
            if "REAL" in str(res.get("error", "")):
                self.stop("conta real bloqueada")
            return
        with self.lock:
            self.st.open_ticket, self.st.open_bar = res["ticket"], bar
            self.st.trades += 1
        self.log(f"{'COMPRA' if a.signal == 'BUY' else 'VENDA'} #{res['ticket']} {res['volume']} @ {res['price']} · SL {res['sl']} · TP {res['tp']} · "
                 f"força {a.strength:.0f}% · {', '.join(a.reasons[:3])}", "trade")

    def _manage_open(self) -> None:
        t = self.st.open_ticket
        if t is None:
            return
        c = self.cfg
        open_now = [p for p in self.broker.positions(c.symbol) if p["ticket"] == t]
        if open_now and c.duration_candles > 0 and self.st.open_bar is not None:
            sec = TF_SECONDS.get(c.timeframe, 60)
            last = self.broker.candles(c.symbol, c.timeframe, 2)[-1]["time"]
            if (int(last) - self.st.open_bar) // sec >= c.duration_candles:
                self.broker.close(t, "tempo")
                open_now = []
        if open_now:
            return
        profit = self.broker.closed_profit(t)
        if profit is None:
            return                                # histórico ainda não chegou: tenta no próximo ciclo
        with self.lock:
            self.st.open_ticket = self.st.open_bar = None
            self.st.pnl = round(self.st.pnl + profit, 2)
            self.st.peak = max(self.st.peak, self.st.pnl)
            if profit >= 0:
                self.st.wins += 1
            else:
                self.st.losses += 1
            self.st.history.append({"ticket": t, "profit": round(profit, 2), "t": time.time()})
        self.log(f"#{t} fechada: {profit:+.2f} · sessão {self.st.pnl:+.2f}", "win" if profit >= 0 else "loss")

    def _check_limits(self) -> bool:
        c, s = self.cfg, self.st
        if s.open_ticket is not None:
            return False                          # deixa a operação aberta terminar antes de encerrar
        reason = ""
        if c.target_profit > 0 and s.pnl >= c.target_profit:
            reason = f"meta atingida ({s.pnl:+.2f})"
        elif c.loss_limit > 0 and s.pnl <= -c.loss_limit:
            reason = f"limite de perda atingido ({s.pnl:+.2f})"
        elif c.protect and s.peak >= c.protect_after and s.pnl <= c.protect_keep:
            reason = f"proteção da sessão: lucro voltou a {s.pnl:+.2f} após pico {s.peak:+.2f}"
        elif s.trades >= c.max_trades:
            reason = f"máximo de {c.max_trades} operações"
        if reason:
            self.stop(reason)
            return True
        return False
