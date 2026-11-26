"""Ensemble E3 — três sistemas de cruzamento dividindo o MESMO risco total.

    9×20 no H1  +  20×50 no H1  +  9×20 no M30       (cada um com 1/3 do risco por operação)

Validado em 2016–2026 na Pepperstone (regra de adoção fixada antes do teste, 26/11/2026):
    9×20 H1 sozinho: +469 ATR · pior ano −56 · maior queda 94 · lucro÷pior ano 8,4 · lucro÷queda 5,0
    E3             : +412 ATR · pior ano −12 · maior queda 64 · lucro÷pior ano 33,8 · lucro÷queda 6,4
Mesmo risco total, quase o mesmo lucro, quedas bem menores.

Cada sistema é um IASession (sem thread própria) com o seu número mágico — a IA sabe qual posição é de quem e um
sistema nunca fecha a posição do outro. Os limites são da CARTEIRA, pelo patrimônio (equity) da conta:
    • freio diário: perdeu X % do patrimônio do começo do dia (UTC) → nenhum sistema abre posição até o dia seguinte
    • limite da sessão: perdeu Y % do patrimônio do início → a IA para (posições abertas seguem com o stop no servidor)
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, replace
from typing import Any, Optional

from .broker import MAGIC
from .engine import IAConfig, IASession, SessionState
from .forward import ForwardLog
from .notify import TelegramNotifier

SYSTEMS = (("H1", 9, 20), ("H1", 20, 50), ("M30", 9, 20))


class EnsembleSession:
    def __init__(self, broker: Any, poll: float = 1.0, forward: Optional[ForwardLog] = None,
                 notifier: Optional[TelegramNotifier] = None) -> None:
        self.broker = broker
        self.poll = poll
        self.forward = forward if forward is not None else ForwardLog()
        self.notifier = notifier if notifier is not None else TelegramNotifier()
        self.cfg = IAConfig(mode="ensemble")
        self.st = SessionState()                    # running / stopped_reason / dia / log da carteira
        self.children: list[IASession] = []
        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._summary_day = ""

    # ------------------------------------------------------------------ utilidades compartilhadas com o servidor
    def log(self, msg: str, kind: str = "info") -> None:
        with self.lock:
            self.st.log.append({"t": time.time(), "kind": kind, "msg": msg})
            del self.st.log[:-200]

    def notify(self, text: str, dedup_key: Optional[str] = None) -> None:
        try:
            acct = self.broker.info()
            tag = f"{'DEMO' if acct.get('demo') else 'REAL'} {acct.get('login', '')}"
        except Exception:  # noqa: BLE001
            tag = ""
        self.notifier.send(f"🤖 TURBO IA · {tag} · ensemble E3\n{text}", dedup_key)

    def _equity(self) -> float:
        try:
            return float(self.broker.info().get("equity") or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------ controle
    def start(self, cfg: IAConfig) -> dict:
        with self.lock:
            if self.st.running:
                return {"ok": False, "error": "IA já está rodando"}
            self.cfg = replace(cfg, mode="ensemble")
            eq = self._equity()
            self.st = SessionState(running=True, started_at=time.time(), start_equity=eq,
                                   day=time.strftime("%Y-%m-%d", time.gmtime()), day_start_equity=eq)
            self._stop.clear()
            self.children = []
            per = cfg.risk_pct / len(SYSTEMS)
            warnings = []
            for k, (tf, fa, sl) in enumerate(SYSTEMS, start=1):
                child_cfg = replace(cfg, mode="cruzamento", timeframe=tf, fast=fa, slow=sl, risk_pct=per, sl_atr=2.0, rr=0.0,
                                    duration_candles=0, magic=MAGIC + k, label=f"{fa}×{sl} {tf}", target_pct=0.0, loss_pct=0.0,
                                    daily_loss_pct=0.0, target_profit=0.0, loss_limit=0.0, protect=False, stop_block="")
                child = IASession(self.broker, poll=self.poll, forward=self.forward, notifier=self.notifier)
                child.summary_enabled = False
                child.start(child_cfg, spawn=False, quiet=True)
                child._stop.set()                    # o laço é o da carteira
                child.st.running = True
                w = child.min_lot_check()
                if w:
                    warnings.append(w)
                self.children.append(child)
        lim = self.limits()
        self.log(f"ensemble E3 iniciado · {cfg.symbol} · risco total {cfg.risk_pct:g}% ({per:.2f}% por sistema) · patrimônio {eq:.2f} · "
                 f"freio diário {cfg.daily_loss_pct:g}% · para se perder {lim['loss']:.2f}", "start")
        for w in warnings:
            self.log(w, "error")
        self.notify(f"▶️ IA iniciada · ensemble E3 ({', '.join(c.cfg.label for c in self.children)})\n"
                    f"risco total {cfg.risk_pct:g}% ({per:.2f}% por sistema) · patrimônio {eq:.2f}"
                    + ("\n" + "\n".join(dict.fromkeys(warnings)) if warnings else ""))
        self._thread = threading.Thread(target=self._run, name="turbo-ia-ensemble", daemon=True)
        self._thread.start()
        return {"ok": True, "warning": "\n".join(dict.fromkeys(warnings)) or None}

    def stop(self, reason: str = "parada pelo usuário") -> dict:
        self._stop.set()
        with self.lock:
            if self.st.running:
                self.st.running = False
                self.st.stopped_reason = reason
                for c in self.children:
                    c.st.running = False
                self.log(f"IA parada: {reason}", "stop")
                self.notify(f"⏹ IA parada: {reason}\nsessão {sum(c.st.pnl for c in self.children):+.2f}")
        return {"ok": True}

    # ------------------------------------------------------------------ laço
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                self.log(f"erro no ciclo: {e}", "error")
            self._stop.wait(self.poll)

    def step(self) -> None:
        if not self.st.running:
            return
        self._portfolio_limits()
        if not self.st.running:
            return
        for c in self.children:
            try:
                c.step()
            except Exception as e:  # noqa: BLE001
                c.log(f"erro no ciclo: {e}", "error")
        self._daily_summary()

    def limits(self) -> dict:
        c, s = self.cfg, self.st
        return {"target": 0.0, "loss": s.start_equity * c.loss_pct / 100 if c.loss_pct > 0 else 0.0,
                "daily": s.day_start_equity * c.daily_loss_pct / 100 if c.daily_loss_pct > 0 else 0.0,
                "protect_after": 0.0, "protect_keep": 0.0}

    def _portfolio_limits(self) -> None:
        s, c = self.st, self.cfg
        eq = self._equity()
        if eq <= 0:
            return
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.day != today:                                   # novo dia: libera o freio
            if s.day_braked:
                self.log("novo dia: freio diário liberado", "start")
            s.day, s.day_start_equity, s.day_braked = today, eq, False
            for ch in self.children:
                ch.block_entries = False
        s.day_pnl = round(eq - s.day_start_equity, 2)
        if c.daily_loss_pct > 0 and not s.day_braked and eq <= s.day_start_equity * (1 - c.daily_loss_pct / 100):
            s.day_braked = True
            for ch in self.children:
                ch.block_entries = True
            self.log(f"FREIO DIÁRIO da carteira: patrimônio {eq:.2f} ({s.day_pnl:+.2f} hoje, limite {c.daily_loss_pct:g}%) — "
                     "nenhum sistema abre posição até amanhã", "stop")
            self.notify(f"🛑 FREIO DIÁRIO: {s.day_pnl:+.2f} hoje\nnenhum sistema abre posição até amanhã — a IA continua ligada")
        if c.loss_pct > 0 and eq <= s.start_equity * (1 - c.loss_pct / 100):
            self.stop(f"limite de perda da sessão: patrimônio {eq:.2f} (início {s.start_equity:.2f}, limite {c.loss_pct:g}%)")

    def _daily_summary(self) -> None:
        if time.gmtime().tm_hour < 21 or self._summary_day == self.st.day:
            return
        self._summary_day = self.st.day
        parts = [f"{c.cfg.label}: {c.st.day_trades} ops · {c.st.day_pnl:+.2f}" for c in self.children]
        self.notify(f"📋 Resumo do dia · patrimônio {self._equity():.2f} ({self.st.day_pnl:+.2f})\n" + "\n".join(parts))

    # ------------------------------------------------------------------ estado para o painel (mesmo formato do IASession)
    def state(self) -> dict:
        with self.lock:
            kids = self.children
            d = asdict(self.st)
            d["pnl"] = round(sum(c.st.pnl for c in kids), 2)
            d["trades"] = sum(c.st.trades for c in kids)
            d["wins"] = sum(c.st.wins for c in kids)
            d["losses"] = sum(c.st.losses for c in kids)
            opened = [c.st.open_ticket for c in kids if c.st.open_ticket is not None]
            d["open_ticket"] = opened[0] if opened else None
            d["open_tickets"] = opened
            d["last_analysis"] = kids[0].st.last_analysis if kids else None
            logs = list(self.st.log) + [x for c in kids for x in c.st.log]
            d["log"] = sorted(logs, key=lambda x: x["t"])[-200:]
            d["history"] = sorted((h for c in kids for h in c.st.history), key=lambda h: h["t"])
            d["config"] = asdict(self.cfg)
            d["limits"] = self.limits()
            d["systems"] = [{"label": c.cfg.label, "open": c.st.open_ticket, "side": c.st.open_side, "pnl": c.st.pnl,
                             "trades": c.st.trades} for c in kids]
            return d
