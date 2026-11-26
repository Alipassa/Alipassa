"""Sessão da IA: ao clicar em "Iniciar IA" roda em segundo plano até bater a meta, o limite de perda ou ser parada.

Ciclo (a cada `poll` segundos):
  1. atualiza a corretora e acompanha a posição aberta (saída por tempo = "duração", SL/TP ficam no servidor)
  2. confere meta de lucro, limite de perda, proteção da sessão e máximo de operações
  3. quando FECHA um candle novo e não há posição aberta, analisa só candles fechados e, havendo sinal, envia a ordem
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import b3
from .analysis import analyze
from .broker import MAGIC, lot_for_risk
from .forward import ForwardLog
from .notify import TelegramNotifier

TF_SECONDS = {"M1": 60, "M2": 120, "M3": 180, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


@dataclass
class IAConfig:
    symbol: str = "Volatility 10 Index"
    timeframe: str = "M1"
    mode: str = "reversao"              # reversao (painel). veloz | equilibrado | preciso só para estudo/backtest: deram prejuízo
    volume: float = 0.5                 # lote fixo por operação (usado quando risk_pct = 0)
    risk_pct: float = 0.0               # > 0: lote calculado a cada operação para perder este % do saldo no stop
    z_thr: float = 2.0                  # reversão: distância mínima da média 20, em ATRs, para entrar
    fast: int = 9                       # cruzamento: média rápida
    slow: int = 20                      # cruzamento: média lenta
    magic: int = MAGIC                  # número mágico das ordens (cada sistema do ensemble tem o seu)
    label: str = ""                     # nome do sistema nos registros/alertas (ensemble)
    stop_block: str = ""                # trava após stop na mesma janela de 1 h: "" (nenhuma) · "dir" (não repete o lado)
                                        # · "2stops" (encerra a janela após 2 stops) · "window" (encerra após 1 stop)
    rr: float = 1.5                     # alvo = rr × distância do stop
    sl_atr: float = 1.5                 # stop padrão em ATRs
    duration_candles: int = 0           # >0 fecha a posição após N candles (estilo Rise/Fall); 0 = só SL/TP
    target_profit: float = 0.0          # meta da sessão em dinheiro (0 = sem) — o painel usa target_pct
    loss_limit: float = 0.0             # perda máxima em dinheiro (0 = sem) — o painel usa loss_pct
    target_pct: float = 0.0             # meta em % do saldo do INÍCIO da sessão (0 = sem meta)
    loss_pct: float = 50.0              # para a IA se a sessão perder este % do saldo do início (0 = sem limite)
    daily_loss_pct: float = 10.0        # freio diário (padrão = 5 × risco de 2 %): perdeu este % do saldo do início do dia (UTC) → sem entradas até amanhã
    max_trades: int = 0                 # 0 = sem limite de operações
    protect: bool = True                # proteção: depois de lucrar `protect_after`, não devolve mais que até `protect_keep`
    protect_after: float = 0.70         # (dinheiro) — o painel usa os campos em %
    protect_keep: float = 0.14
    protect_after_pct: float = 0.0      # proteção em % do saldo inicial: depois de lucrar X %, encerra se voltar a Y %
    protect_keep_pct: float = 0.0
    expiry_min: int = 10                # registro de acerto: confere o preço N minutos depois de cada entrada

    @classmethod
    def from_dict(cls, d: dict) -> "IAConfig":
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k) and v is not None and v != "":
                cur = getattr(c, k)
                if isinstance(cur, bool):
                    setattr(c, k, v in (True, "true", "1", 1, "on"))
                elif isinstance(cur, int):
                    setattr(c, k, int(float(v)))           # "1.5" no campo de velas não derruba o servidor
                else:
                    setattr(c, k, type(cur)(v))
        return c


@dataclass
class SessionState:
    running: bool = False
    started_at: Optional[float] = None
    start_equity: float = 0.0
    day: str = ""                       # dia UTC corrente (AAAA-MM-DD)
    day_start_equity: float = 0.0
    day_pnl: float = 0.0
    day_braked: bool = False
    day_trades: int = 0
    day_wins: int = 0
    summary_sent: str = ""              # dia (UTC) do último resumo enviado ao Telegram
    stopped_reason: str = ""
    pnl: float = 0.0
    peak: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    open_ticket: Optional[int] = None
    open_bar: Optional[int] = None
    open_side: Optional[str] = None
    open_window: Optional[int] = None
    closing: list = field(default_factory=list)           # posições fechadas pela IA aguardando o resultado no histórico
    window_stops: dict = field(default_factory=dict)     # "janela" e "janela|lado" → nº de stops (janela = hora UTC desde 1970)
    last_bar: Optional[int] = None
    last_analysis: Optional[dict] = None
    log: list = field(default_factory=list)
    history: list = field(default_factory=list)


class IASession:
    def __init__(self, broker: Any, poll: float = 1.0, forward: Optional[ForwardLog] = None,
                 notifier: Optional[TelegramNotifier] = None) -> None:
        self.broker = broker
        self.notifier = notifier if notifier is not None else TelegramNotifier()
        self.forward = forward if forward is not None else ForwardLog()
        self.poll = poll
        self.cfg = IAConfig()
        self.st = SessionState()
        self.lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.block_entries = False          # ensemble: freio diário da carteira bloqueia novas entradas
        self.summary_enabled = True         # ensemble: o resumo do dia sai uma vez só, pela carteira
        self._contracts_cache: tuple[float, list] = (0.0, [])

    # ------------------------------------------------------------------ B3: contrato vigente e pregão
    def _now(self) -> float:
        return float(getattr(self.broker, "clock", time.time)())

    def _contracts(self, root: str) -> list:
        t, lst = self._contracts_cache
        if not lst or time.time() - t > 600:
            fn = getattr(self.broker, "contracts", None)
            try:
                lst = list(fn(root)) if fn else []
            except Exception:  # noqa: BLE001
                lst = []
            self._contracts_cache = (time.time(), lst)
        return lst

    def trade_symbol(self) -> str:
        """Onde a ordem vai: o próprio símbolo, ou — na série contínua da B3 (WIN@) — o contrato vigente (ex.: WINZ26)."""
        c = self.cfg
        root = b3.root_of(c.symbol)
        if not root or not b3.is_continuous(c.symbol) or not hasattr(self.broker, "contracts"):
            return c.symbol
        try:
            return b3.pick_contract(root, self._contracts(root), self._now())
        except Exception:  # noqa: BLE001
            return c.symbol

    def _pos_filter(self) -> Optional[str]:
        """Símbolo para listar as posições da IA (None = todos: na B3 a posição está no contrato, não no WIN@)."""
        return None if b3.root_of(self.cfg.symbol) else self.cfg.symbol

    def _b3_roll(self) -> None:
        """Faltando poucos dias para o vencimento: fecha no contrato velho e reabre no novo, mesmo lado e mesma distância de stop."""
        t, c, b = self.st.open_ticket, self.cfg, self.broker
        root = b3.root_of(c.symbol)
        if t is None or not root or not b3.is_continuous(c.symbol):
            return
        pos = next((p for p in b.positions(None) if p["ticket"] == t), None)
        if not pos or not b3.needs_roll(pos["symbol"], self._contracts(root), self._now()):
            return
        new = self.trade_symbol()
        if new.upper() == pos["symbol"].upper():
            return
        side = pos["side"]
        try:
            bid, ask = b.tick(pos["symbol"])
            dist = abs((bid if side == "BUY" else ask) - pos["sl"]) if pos.get("sl") else 0.0
        except Exception:  # noqa: BLE001
            dist = 0.0
        if not dist:
            atr = float((self.st.last_analysis or {}).get("atr") or 0)
            dist = c.sl_atr * atr
        r = b.close(t, "rolagem")
        if not r.get("ok"):
            self.log(f"rolagem: não consegui fechar #{t} em {pos['symbol']}: {r.get('error')}", "error")
            self.notify(f"⚠️ rolagem: não fechou #{t} em {pos['symbol']}: {r.get('error')}", dedup_key=f"roll:{t}")
            return
        with self.lock:
            self.st.closing.append({"ticket": t, "side": side, "window": self.st.open_window})
            self.st.open_ticket = self.st.open_bar = self.st.open_side = None
        nb, na = b.tick(new)
        fill = na if side == "BUY" else nb
        sl = (fill - dist if side == "BUY" else fill + dist) if dist else None
        res = b.open(new, side, pos["volume"], sl, None, "TurboIA rolagem", magic=c.magic)
        if not res.get("ok"):
            self.log(f"rolagem: #{t} fechada em {pos['symbol']}, mas a reabertura em {new} falhou: {res.get('error')} — "
                     "fica fora até o próximo cruzamento", "error")
            self.notify(f"⚠️ rolagem: fechou {pos['symbol']}, não reabriu em {new}: {res.get('error')}", dedup_key=f"roll2:{t}")
            return
        with self.lock:
            self.st.open_ticket, self.st.open_side, self.st.open_bar = res["ticket"], side, self.st.last_bar
        self.log(f"ROLAGEM {pos['symbol']} → {new}: {'COMPRA' if side == 'BUY' else 'VENDA'} #{res['ticket']} {res['volume']} @ "
                 f"{res['price']} · SL {res['sl']}", "trade")
        self.notify(f"🔁 Rolagem {pos['symbol']} → {new}\n{'COMPRA' if side == 'BUY' else 'VENDA'} {res['volume']} @ {res['price']} · stop {res['sl']}")

    # ------------------------------------------------------------------ controle
    def log(self, msg: str, kind: str = "info") -> None:
        if self.cfg.label:
            msg = f"[{self.cfg.label}] {msg}"
        with self.lock:
            self.st.log.append({"t": time.time(), "kind": kind, "msg": msg})
            del self.st.log[:-200]
        if kind == "error":
            self._event({"kind": "error", "msg": msg[:200]})

    def _event(self, rec: dict) -> None:
        """Registro permanente para a validação (critério 7): erros e presença nas janelas de operação."""
        path = getattr(self.forward, "path", None)
        if not path:
            return
        try:
            rec = {"t": time.time(), "login": self._login(), **rec}
            with open(os.path.join(os.path.dirname(path) or ".", "turbo_ia_eventos.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _heartbeat(self) -> None:
        """Uma marca por janela (dia + hora UTC) em que a IA estava ligada e conectada."""
        from .analysis import REVERSION_HOURS
        g = time.gmtime()
        key = f"{g.tm_year:04d}-{g.tm_mon:02d}-{g.tm_mday:02d} {g.tm_hour:02d}"
        if (g.tm_hour in REVERSION_HOURS or self.cfg.mode == "cruzamento") and getattr(self, "_hb", "") != key:
            self._hb = key
            self._event({"kind": "janela", "janela": key})

    def notify(self, text: str, dedup_key: Optional[str] = None) -> None:
        try:
            acct = self.broker.info()
            tag = f"{'DEMO' if acct.get('demo') else 'REAL'} {acct.get('login', '')}"
        except Exception:  # noqa: BLE001
            tag = ""
        lbl = f" · {self.cfg.label}" if self.cfg.label else ""
        self.notifier.send(f"🤖 TURBO IA · {tag}{lbl}\n{text}", dedup_key)

    def min_lot_check(self) -> str:
        """Cruzamento: o lote mínimo cabe no risco? Com stop de 2 ATR no H1, contas pequenas pulam quase tudo."""
        c = self.cfg
        if c.mode != "cruzamento" or c.risk_pct <= 0:
            return ""
        try:
            from . import indicators as ind
            cs = self.broker.candles(c.symbol, c.timeframe, 60)
            a = [x for x in ind.atr(cs, 14) if x][-1]
            spec = self.broker.spec(self.trade_symbol())
            eq = float(self.broker.info().get("equity") or 0)
        except Exception:  # noqa: BLE001
            return ""
        vpu = float(spec.get("value_per_unit") or spec.get("contract_size") or 1.0)
        need = float(spec.get("volume_min") or 0.01) * c.sl_atr * a * vpu
        budget = eq * c.risk_pct / 100.0
        if need > budget:
            return (f"⚠️ CONTA PEQUENA para {c.symbol} {c.timeframe}: o lote mínimo arrisca {need:.2f} no stop de {c.sl_atr:g} ATR, "
                    f"mas o risco por operação é {budget:.2f} ({c.risk_pct:g}% de {eq:.2f}). A IA vai PULAR as entradas até o "
                    f"stop caber — saldo necessário ≈ {need / (c.risk_pct / 100.0):,.0f}.")
        return ""

    def start(self, cfg: IAConfig, spawn: bool = True, quiet: bool = False) -> dict:
        with self.lock:
            if self.st.running:
                return {"ok": False, "error": "IA já está rodando"}
            if cfg.mode == "reversao" and cfg.rr <= 0:   # sem alvo = aposta de direção: sai por tempo, stop largo só de proteção
                cfg.sl_atr = max(cfg.sl_atr, 3.0)
                if cfg.duration_candles <= 0:
                    cfg.duration_candles = max(1, cfg.expiry_min * 60 // TF_SECONDS.get(cfg.timeframe, 60))
            self.cfg = cfg
            try:
                eq = float(self.broker.info().get("equity") or 0)
            except Exception:  # noqa: BLE001
                eq = 0.0
            self.st = SessionState(running=True, started_at=time.time(), start_equity=eq)
            self._stop.clear()
            try:
                mine = sorted([p for p in self.broker.positions(self._pos_filter()) if p.get("magic", MAGIC) == cfg.magic
                               and (p["symbol"] == cfg.symbol or b3.root_of(p["symbol"]) == (b3.root_of(cfg.symbol) or "-"))],
                              key=lambda p: p.get("time", 0))
            except Exception:  # noqa: BLE001
                mine = []
            if mine:                                   # painel reiniciado com operação aberta: acompanha em vez de abrir outra
                p = mine[-1]
                self.st.open_ticket, self.st.open_side = p["ticket"], p["side"]
        lim = self.limits()
        tsym = self.trade_symbol()
        if tsym != cfg.symbol:
            self.log(f"B3: análise em {cfg.symbol}, ordens no contrato vigente {tsym} (rolagem automática {b3.ROLL_DAYS:g} dias antes "
                     f"do vencimento) · ordens só das 09h05 às 18h15 de Brasília", "start")
        self.log(f"IA iniciada · {cfg.symbol} {cfg.timeframe} · risco {cfg.risk_pct:g}%/op · saldo inicial {eq:.2f} · "
                 f"meta {('+%.2f' % lim['target']) if lim['target'] else 'nenhuma'} · "
                 f"limite de perda {('-%.2f' % lim['loss']) if lim['loss'] else 'nenhum'}", "start")
        self._event({"kind": "start", "symbol": cfg.symbol, "tf": cfg.timeframe, "risk": cfg.risk_pct})
        warn = self.min_lot_check()
        if warn:
            self.log(warn, "error")
        brake_txt = f" · freio diário {cfg.daily_loss_pct:g}%" if cfg.daily_loss_pct > 0 else ""
        loss_txt = f" · para se perder {lim['loss']:.2f}" if lim["loss"] else ""
        if not quiet:
            self.notify(f"▶️ IA iniciada · {cfg.symbol} {cfg.timeframe}\nrisco {cfg.risk_pct:g}%/op · saldo {eq:.2f}{brake_txt}{loss_txt}"
                        + (f"\n{warn}" if warn else ""))
        if self.st.open_ticket is not None:
            self.log(f"posição #{self.st.open_ticket} da IA já estava aberta — acompanhando até fechar (nenhuma nova entrada antes disso)", "start")
        if spawn:
            self._thread = threading.Thread(target=self._run, name="turbo-ia", daemon=True)
            self._thread.start()
        return {"ok": True, "warning": warn or None}

    def stop(self, reason: str = "parada pelo usuário") -> dict:
        self._stop.set()
        with self.lock:
            if self.st.running:
                self.st.running = False
                self.st.stopped_reason = reason
                self.log(f"IA parada: {reason}", "stop")
                self._event({"kind": "stop", "reason": reason[:120]})
                self.notify(f"⏹ IA parada: {reason}\nsessão {self.st.pnl:+.2f} · {self.st.trades} operações")
        return {"ok": True}

    def state(self) -> dict:
        with self.lock:
            d = asdict(self.st)
            d["config"] = asdict(self.cfg)
            d["limits"] = self.limits()
            try:
                d["trade_symbol"] = self.trade_symbol() if self.st.running else self.cfg.symbol
            except Exception:  # noqa: BLE001
                d["trade_symbol"] = self.cfg.symbol
            return d

    # ------------------------------------------------------------------ laço
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                self.log(f"erro no ciclo: {e}", "error")
                self.notify(f"⚠️ erro no ciclo: {e}", dedup_key=f"err:{type(e).__name__}:{str(e)[:60]}")
            self._stop.wait(self.poll)

    def step(self) -> None:
        """Um ciclo da IA (público para testes)."""
        b, c = self.broker, self.cfg
        b.update()
        for r in self.forward.resolve(b):
            self.log(f"registro {r['expiry_min']} min: {r['side']} {r['symbol']} {r['entry']} → {r['exit']} = {'ACERTO' if r['win'] else 'ERRO'}",
                     "win" if r["win"] else "loss")
        self._manage_open()
        if not self.st.running:
            return
        self._roll_day()
        self._heartbeat()
        self._daily_summary()
        if self._check_limits():
            return
        if b3.root_of(c.symbol):                          # B3: só no pregão (fora dele o candle não é "gasto")
            if not b3.session_open(self._now()):
                return
            self._b3_roll()
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
        a = analyze(closed, c.mode, c.rr, c.sl_atr, getattr(b, "utc_offset", 0.0), c.z_thr, fast=c.fast, slow=c.slow)
        with self.lock:
            self.st.last_analysis = a.to_dict()
        if c.mode == "cruzamento" and self.st.open_ticket is not None and a.signal not in ("NONE", self.st.open_side):
            t = self.st.open_ticket                       # cruzamento contrário: sai e (abaixo) vira a mão
            r = b.close(t, "cruzamento")
            if not r.get("ok"):
                self.log(f"não consegui fechar #{t} no cruzamento contrário: {r.get('error')}", "error")
                self.notify(f"⚠️ não fechou #{t} no cruzamento contrário: {r.get('error')}", dedup_key=f"close:{t}")
                return
            with self.lock:
                self.st.closing.append({"ticket": t, "side": self.st.open_side, "window": self.st.open_window})
                self.st.open_ticket = self.st.open_bar = self.st.open_side = self.st.open_window = None
            self.log(f"cruzamento contrário: #{t} fechada, virando para {'COMPRA' if a.signal == 'BUY' else 'VENDA'}", "trade")
        if self.st.open_ticket is not None:
            return
        if a.signal == "NONE":
            self.log(f"sem entrada ({a.label}): {'; '.join(a.blocked)}", "skip")
            return
        if self._daily_brake():
            return
        if self.block_entries:
            self.log("sem entrada (freio diário da carteira ativo)", "skip")
            return
        window = int((int(closed[-1]["time"]) - getattr(b, "utc_offset", 0.0) * 3600) // 3600)
        ws = self.st.window_stops
        why = ""
        if c.stop_block == "dir" and ws.get(f"{window}|{a.signal}"):
            why = f"trava: já houve stop {'comprando' if a.signal == 'BUY' else 'vendendo'} nesta janela"
        elif c.stop_block == "window" and ws.get(str(window)):
            why = "trava: já houve stop nesta janela"
        elif c.stop_block == "2stops" and ws.get(str(window), 0) >= 2:
            why = "trava: 2 stops nesta janela"
        if why:
            self.log(f"sem entrada ({why})", "skip")
            return
        tsym = self.trade_symbol()
        if tsym != c.symbol and a.sl:                     # B3: stop com a mesma distância, no preço do contrato
            try:
                tb, ta = b.tick(tsym)
                fill0 = ta if a.signal == "BUY" else tb
                dist = abs(a.price - a.sl)
                a.sl = fill0 - dist if a.signal == "BUY" else fill0 + dist
            except Exception as e:  # noqa: BLE001
                self.log(f"sem preço de {tsym}: {e}", "error")
                return
        fv = getattr(b, "foreign_volume", None)
        if b3.root_of(c.symbol) and fv:
            try:
                other = fv(tsym)
            except Exception:  # noqa: BLE001
                other = 0.0
            if other:
                self.log(f"há {other:g} contrato(s) em {tsym} que não são da IA — conta netting: a ordem se misturaria. Entrada pulada", "error")
                self.notify(f"⚠️ posição manual em {tsym}: a IA não entra (conta netting)", dedup_key=f"foreign:{tsym}")
                return
        volume, risk_note = c.volume, ""
        if c.risk_pct > 0:
            if not a.sl:
                self.log("risco % exige stop — operação pulada", "error")
                return
            eq = float(b.info().get("equity") or 0)
            bid, ask = b.tick(tsym)
            fill = ask if a.signal == "BUY" else bid
            if (a.signal == "BUY" and fill <= a.sl) or (a.signal == "SELL" and fill >= a.sl):
                self.log(f"preço já passou do stop ({fill} × SL {a.sl}) — operação pulada", "skip")
                return
            if (ask - bid) > 0.3 * abs(fill - a.sl):
                self.log(f"spread anormal agora ({ask - bid:.2f} > 30 % do stop {abs(fill - a.sl):.2f}) — operação pulada", "skip")
                return
            volume, risk_money, note = lot_for_risk(b.spec(tsym), fill, a.sl, eq, min(c.risk_pct, 10.0))
            if not volume:
                self.log(note, "skip")
                return
            risk_note = f" · risco {risk_money:.2f} ({risk_money / eq:.1%} do saldo {eq:.2f})"
        try:
            q_bid, q_ask = b.tick(tsym)                     # preço no instante do envio (para medir custo real)
        except Exception:  # noqa: BLE001
            q_bid = q_ask = None
        res = b.open(tsym, a.signal, volume, a.sl, a.tp, f"TurboIA {c.label or c.mode}", magic=c.magic)
        if not res.get("ok"):
            self.log(f"ordem recusada: {res.get('error')}", "error")
            self.notify(f"⚠️ ordem recusada: {res.get('error')}", dedup_key=f"ref:{str(res.get('error'))[:60]}")
            if "REAL" in str(res.get("error", "")):
                self.stop("conta real bloqueada")
            return
        try:
            acct = str(b.info().get("login", ""))
        except Exception:  # noqa: BLE001
            acct = ""
        if c.mode != "cruzamento":                       # cruzamento não tem alvo fixo: o resultado vem do histórico do MT5
            self.forward.add(c.symbol, c.timeframe, c.mode, a.signal, int(closed[-1]["time"]), float(closed[-1]["close"]), c.expiry_min, acct,
                             getattr(b, "name", ""), sl=a.sl, tp=a.tp)
        self._log_execution({"t": time.time(), "ticket": res["ticket"], "symbol": tsym, "tf": c.timeframe, "side": a.signal,
                             "signal_close": float(closed[-1]["close"]), "signal_time": int(closed[-1]["time"]),
                             "bid": q_bid, "ask": q_ask, "fill": res.get("price"), "sl": res.get("sl"), "tp": res.get("tp"),
                             "volume": res.get("volume"), "broker": getattr(b, "name", ""), "mode": c.mode})
        with self.lock:
            self.st.open_ticket, self.st.open_bar = res["ticket"], bar
            self.st.open_side, self.st.open_window = a.signal, window
            self.st.trades += 1
            self.st.day_trades += 1
        self.notify(f"{'🟢 COMPRA' if a.signal == 'BUY' else '🔴 VENDA'} {tsym} {res['volume']} @ {res['price']}\n"
                    f"stop {res['sl']} · alvo {res['tp']}{risk_note.replace(' · ', chr(10), 1) if risk_note else ''}")
        self.log(f"{'COMPRA' if a.signal == 'BUY' else 'VENDA'} #{res['ticket']} {res['volume']} @ {res['price']} · SL {res['sl']} · TP {res['tp']} · "
                 f"força {a.strength:.0f}%{risk_note} · {', '.join(a.reasons[:3])}", "trade")

    def _manage_open(self) -> None:
        for item in list(self.st.closing):                # fechadas pela própria IA (cruzamento contrário)
            p = self.broker.closed_profit(item["ticket"])
            if p is not None:
                self.st.closing.remove(item)
                self._record_result(item["ticket"], p, item["side"], item["window"])
        t = self.st.open_ticket
        if t is None:
            return
        c = self.cfg
        open_now = [p for p in self.broker.positions(self._pos_filter()) if p["ticket"] == t]
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
        side, window = self.st.open_side, self.st.open_window
        with self.lock:
            self.st.open_ticket = self.st.open_bar = None
        self._record_result(t, profit, side, window)

    def _record_result(self, t: int, profit: float, side: Optional[str], window: Optional[int]) -> None:
        with self.lock:
            self.st.pnl = round(self.st.pnl + profit, 2)
            self.st.day_pnl = round(self.st.day_pnl + profit, 2)
            self.st.peak = max(self.st.peak, self.st.pnl)
            if profit >= 0:
                self.st.wins += 1
                self.st.day_wins += 1
            else:
                self.st.losses += 1
                if window is not None:
                    ws = self.st.window_stops
                    ws[str(window)] = ws.get(str(window), 0) + 1
                    ws[f"{window}|{side}"] = ws.get(f"{window}|{side}", 0) + 1
            self.st.history.append({"ticket": t, "profit": round(profit, 2), "t": time.time()})
        self._save_day()
        self.log(f"#{t} fechada: {profit:+.2f} · sessão {self.st.pnl:+.2f}", "win" if profit >= 0 else "loss")
        try:
            bal = f" · saldo {float(self.broker.info().get('balance') or 0):.2f}"
        except Exception:  # noqa: BLE001
            bal = ""
        self.notify(f"{'✅ GANHO' if profit >= 0 else '❌ PERDA'} #{t}: {profit:+.2f}\nhoje {self.st.day_pnl:+.2f} · sessão {self.st.pnl:+.2f}{bal}")

    def _log_execution(self, rec: dict) -> None:
        """Item 6: cada entrada grava preço da vela (o que o backtest supõe) × bid/ask no envio × preço executado."""
        path = getattr(self.forward, "path", None)
        if not path:
            return
        try:
            with open(os.path.join(os.path.dirname(path) or ".", "turbo_ia_execucao.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _day_file(self) -> Optional[str]:
        if getattr(self.forward, "path", None) is None:          # testes sem disco
            return None
        return os.path.join(os.path.dirname(self.forward.path) or ".", ".turbo_ia_dia.json")

    def _login(self) -> str:
        try:
            return str(self.broker.info().get("login", ""))
        except Exception:  # noqa: BLE001
            return ""

    def _save_day(self) -> None:
        f = self._day_file()
        if not f:
            return
        try:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump({"day": self.st.day, "start_equity": self.st.day_start_equity, "pnl": self.st.day_pnl,
                           "symbol": self.cfg.symbol, "broker": getattr(self.broker, "name", ""), "login": self._login()}, fh)
        except Exception:  # noqa: BLE001
            pass

    def _roll_day(self) -> None:
        """Novo dia (UTC): zera o resultado do dia e guarda o saldo de referência do freio diário.
        Se o painel for reiniciado no mesmo dia, retoma o resultado do dia gravado (o freio não "esquece" as perdas)."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.st.day != today:
            try:
                eq = float(self.broker.info().get("equity") or 0)
            except Exception:  # noqa: BLE001
                eq = self.st.start_equity
            pnl = 0.0
            f = self._day_file()
            if f and os.path.exists(f) and not self.st.day:
                try:
                    with open(f, encoding="utf-8") as fh:
                        saved = json.load(fh)
                    if (saved.get("day") == today and saved.get("broker") == getattr(self.broker, "name", "")
                            and str(saved.get("login", "")) == self._login()):   # conta nova não herda o dia da antiga
                        eq, pnl = float(saved.get("start_equity") or eq), float(saved.get("pnl") or 0.0)
                        self.log(f"retomando o dia: resultado de hoje {pnl:+.2f} (saldo de referência {eq:.2f})", "start")
                except Exception:  # noqa: BLE001
                    pass
            with self.lock:
                if self.st.day and self.st.day_braked:
                    self.log("novo dia: freio diário liberado", "start")
                self.st.day, self.st.day_start_equity, self.st.day_pnl, self.st.day_braked = today, eq, pnl, False
                self.st.day_trades = self.st.day_wins = 0
            self._save_day()

    def _daily_summary(self) -> None:
        """Uma vez por dia, depois da última janela (21h UTC = 18h de Brasília), manda o resumo do dia."""
        if not self.summary_enabled:
            return
        s = self.st
        if not s.day or s.summary_sent == s.day or time.gmtime().tm_hour < 21 or s.open_ticket is not None:
            return
        s.summary_sent = s.day
        if not s.day_trades:
            text = "📋 Resumo do dia: nenhuma entrada (o preço não esticou o suficiente nas janelas)"
        else:
            text = (f"📋 Resumo do dia: {s.day_trades} operações · {s.day_wins} ganhos / {s.day_trades - s.day_wins} stops\n"
                    f"resultado {s.day_pnl:+.2f} ({s.day_pnl / s.day_start_equity:+.1%} do saldo do início do dia)"
                    if s.day_start_equity else f"📋 Resumo do dia: {s.day_trades} operações · resultado {s.day_pnl:+.2f}")
        self.notify(text)

    def _daily_brake(self) -> bool:
        c, s = self.cfg, self.st
        if c.daily_loss_pct <= 0 or s.day_start_equity <= 0:
            return False
        lim = s.day_start_equity * c.daily_loss_pct / 100.0
        if s.day_pnl <= -lim:
            if not s.day_braked:
                s.day_braked = True
                self.log(f"FREIO DIÁRIO: perda de {s.day_pnl:.2f} hoje (limite {c.daily_loss_pct:g}% = -{lim:.2f}) — "
                         "sem novas entradas até o próximo dia (00h UTC = 21h de Brasília)", "stop")
                self.notify(f"🛑 FREIO DIÁRIO: {s.day_pnl:+.2f} hoje (limite -{lim:.2f})\nsem novas entradas até amanhã — a IA continua ligada")
            return True
        return False

    def limits(self) -> dict:
        """Limites da sessão em dinheiro. Em % valem sobre o saldo do início da sessão (servem para 1.000 ou 11.000)."""
        c, eq = self.cfg, self.st.start_equity
        pct = lambda p: eq * p / 100.0 if p > 0 and eq > 0 else 0.0  # noqa: E731
        return {"target": pct(c.target_pct) or c.target_profit, "loss": pct(c.loss_pct) or c.loss_limit,
                "daily": self.st.day_start_equity * c.daily_loss_pct / 100.0 if c.daily_loss_pct > 0 else 0.0,
                "protect_after": pct(c.protect_after_pct) or c.protect_after,
                "protect_keep": pct(c.protect_keep_pct) if c.protect_after_pct > 0 else c.protect_keep}

    def _check_limits(self) -> bool:
        c, s = self.cfg, self.st
        if s.open_ticket is not None:
            return False                          # deixa a operação aberta terminar antes de encerrar
        lim = self.limits()
        reason = ""
        if lim["target"] > 0 and s.pnl >= lim["target"]:
            reason = f"meta atingida ({s.pnl:+.2f})"
        elif lim["loss"] > 0 and s.pnl <= -lim["loss"]:
            reason = f"limite de perda atingido ({s.pnl:+.2f} de -{lim['loss']:.2f})"
        elif c.protect and s.peak >= lim["protect_after"] and s.pnl <= lim["protect_keep"]:
            reason = f"proteção da sessão: lucro voltou a {s.pnl:+.2f} após pico {s.peak:+.2f}"
        elif c.max_trades > 0 and s.trades >= c.max_trades:
            reason = f"máximo de {c.max_trades} operações"
        if reason:
            self.stop(reason)
            return True
        return False
