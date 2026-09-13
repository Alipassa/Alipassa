"""GOLD AI ENGINE 3.0 — LIVE EXECUTION ENGINE (ciclo completo).

DADOS → SNAPSHOT → PREDICTOR → PRE-MOVE → DECISION ENGINE → TRADE PLAN → RISK ENGINE → POSITION SIZE
→ MT5 EXECUTOR → BROKER → CONFIRMAÇÃO → 🔄 TRADE MONITOR → MANTER/PROTEGER/ENCERRAR → RESULTADO
→ SQLITE → PERFORMANCE → NOVO CAPITAL → NOVO POSITION SIZE → PRÓXIMO.

Três cérebros: PREDICTION ENGINE (GoldAIEngine) · TRADE ENGINE (MaxProfitEngine/StopEngine/sizing) ·
TRADE MONITOR (monitor.TradeMonitor). O núcleo preditivo (2.1–2.3) não é alterado.
Nasce em PAPER. LIVE exige autorização explícita.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from .engine import GoldAIEngine
from .guard import GuardLimits, KillSwitch, PerformanceEngine, TelegramCommands, TradingMode, size_lots
from .memory import PredictionMemory
from .models import Assessment, Direction, MarketSnapshot, Signal, SignalType
from .monitor import ManagedTrade, MonitorReading, Thesis, TradeMonitor, render_evolution, render_monitor
from .report import render_dashboard, render_report
from .telegram import (TelegramSender, format_entry, format_protection, format_result, format_scenario_change, format_status)
from .trading import MaxProfitEngine, StopEngine, TradePlan, no_trade_check


@dataclass
class CycleResult:
    assessment: Optional[Assessment]
    signal: Optional[Signal]
    decision: str = ""
    pid: Optional[int] = None
    plan: Optional[TradePlan] = None
    readings: list[tuple[ManagedTrade, MonitorReading]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class LiveExecutionEngine:
    EXECUTABLE = {SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL, SignalType.PRE_MOVE}

    def __init__(self, mem: PredictionMemory, limits: GuardLimits, mode: TradingMode = TradingMode.PAPER, equity: float = 10000.0,
                 executor=None, sender: Optional[TelegramSender] = None, kill_switch: Optional[KillSwitch] = None,
                 commands: Optional[TelegramCommands] = None, horizon_min: int = 240, engine: Optional[GoldAIEngine] = None,
                 log: Callable[[str], None] = print, authorized: bool = False, spec=None, perf: Optional[PerformanceEngine] = None,
                 entry_gate: Optional[Callable] = None) -> None:
        """`spec` (markets.MarketSpec) torna o motor específico de um mercado; `perf` permite capital compartilhado
        entre mercados (4.0); `entry_gate(symbol, direction, risk_usd)` → lista de bloqueios do portfólio (exposição)."""
        from .markets import get_market
        self.spec = spec or get_market("XAUUSD")
        self.symbol = self.spec.symbol
        self.entry_gate = entry_gate
        self.mem, self.limits, self.mode = mem, limits, mode
        self.executor = executor                      # execution.ExecutionEngine (LIVE / SEMI_LIVE / AUTHORIZE com autorização)
        self.sender = sender or TelegramSender(dry_run=True)
        self.ks = kill_switch or KillSwitch()
        self.commands = commands
        self.horizon = horizon_min
        self.engine = engine or GoldAIEngine()
        self.log = log
        self.authorized = authorized                  # AUTHORIZE: autorização dada para a próxima entrada
        start_equity = mem.last_equity() or equity
        self.perf = perf or PerformanceEngine(limits, start_equity)
        if mem.last_equity() is None:
            mem.record_equity(datetime.now(timezone.utc), start_equity, None, "capital inicial")
        if engine is not None and self.spec.factor_signs and not engine.cfg.factor_signs:
            engine.cfg.factor_signs, engine.cfg.symbol = dict(self.spec.factor_signs), self.symbol
        self.monitor = TradeMonitor(history=mem.r_stats(self.symbol))
        self.mpe = MaxProfitEngine(StopEngine(limits), mem.r_stats(self.symbol), horizon_min, limits.min_rr_to_structure)
        self.managed: list[ManagedTrade] = mem.managed_trades(self.symbol)
        self.tickets: dict[int, int] = {}             # trade_id → ticket no broker
        for tr in self.managed:
            row = mem.conn.execute("SELECT ticket FROM trades WHERE id=?", (tr.trade_id,)).fetchone()
            if row and row["ticket"]:
                self.tickets[tr.trade_id] = int(row["ticket"])
        self.pending_close_confirm: list[int] = []

    # ------------------------------------------------------------------ util
    def _send(self, text: str, res: CycleResult) -> None:
        res.messages.append(text)
        self.sender.send(text)

    def status_text(self) -> str:
        return format_status(self.perf, self.ks, self.managed, self.mode.value)

    # ------------------------------------------------------------------ comandos
    def handle_commands(self, res: CycleResult, now: datetime) -> None:
        if self.commands is None:
            return
        for action in self.commands.apply(self.commands.poll(), self.ks):
            if action == "EDGE":
                continue  # tratado pelo MarketAIEngine (LIVE EDGE sob demanda)
            if action == "STATUS":
                self._send(self.status_text(), res)
            elif action in ("STOP", "PAUSE", "RESUME"):
                self._send(f"🔧 comando /{action} aplicado — " + self.ks.new_entries_allowed()[1], res)
            elif action == "CLOSE_REQUESTED":
                self._send(f"⚠️ /CLOSE solicitado para {len(self.managed)} posição(ões). Responda /CLOSE CONFIRM para encerrar.", res)
            elif action == "CLOSE_CONFIRMED":
                for tr in list(self.managed):
                    self._close_trade(tr, tr.r_at(tr.plan.entry), "MANUAL", now, res, price_hint=None)
                self._send("🔴 posições encerradas por /CLOSE CONFIRM", res)

    # ------------------------------------------------------------------ ciclo
    def run_cycle(self, snap: MarketSnapshot, new_event_key: Optional[str] = None, defer_entry: bool = False) -> CycleResult:
        res = CycleResult(None, None)
        now = snap.time
        self.handle_commands(res, now)
        if not snap.candles:
            res.notes.append("sem candles XAU — ciclo abortado")
            return res
        fine = snap.candles.get("M1") or snap.candles.get("M5") or snap.candles.get("M15") or []
        # capital: em LIVE/SEMI_LIVE vem do broker
        if self.executor is not None and self.mode in (TradingMode.LIVE, TradingMode.SEMI_LIVE):
            eq = self.executor.account_equity()
            if eq:
                before = self.perf.equity
                self.perf.sync_equity(eq, now)
                if abs(eq - before) > 0.005:
                    self.mem.record_equity(now, eq, round(eq - before, 2), "sync broker")
        # resolve previsões e operações simuladas pendentes; atualiza histórico
        for pid, out in self.mem.auto_resolve(fine, now, snap.atr or 5.0, self.horizon):
            res.notes.append(f"[memória] previsão #{pid} → {out.result} (lead {out.time_to_reaction_min}, MFE {out.mfe}, MAE {out.mae})")
        for tid, sim in self.mem.auto_resolve_trades(fine, now):
            pr = sim["profile"]
            res.notes.append(f"[trade] operação #{tid} resolvida no horizonte → max {pr.max_r_before_stop:.2f}R, MAE {pr.mae_r:.2f}R")
        self.mpe.history = self.monitor.history = self.mem.r_stats(self.symbol)
        self.engine.expected_lead_min = self.mem.lead_time_stats()["media"]

        self.mem.store_prices(fine)
        self.mem.resolve_hypotheticals(now, self.horizon)
        a, sig = self.engine.run_cycle(snap, new_event_key=new_event_key)
        res.assessment, res.signal = a, sig
        # 🔄 TRADE MONITOR — toda posição aberta é reavaliada antes de qualquer nova decisão
        for tr in list(self.managed):
            self._monitor_trade(tr, a, snap, fine, res)
        res.pid = None
        if sig is not None:
            self._send(sig.text, res)
            res.pid = self.mem.record(a, sig.type.value, atr=snap.atr, horizon_min=self.horizon, symbol=self.symbol)
        if defer_entry:
            res.decision = "ANALISADO — decisão de entrada delegada ao Asset Selector" if sig is not None else "SEM SINAL — " + a.edge_status
            return res
        return self.enter(res, snap)

    def enter(self, res: CycleResult, snap: MarketSnapshot, veto: Optional[str] = None) -> CycleResult:
        """DECISION ENGINE. `veto` = motivo externo (Asset Selector/exposição) para não entrar neste ciclo."""
        a, sig = res.assessment, res.signal
        if a is None:
            return res
        if sig is not None and veto:
            res.decision = veto
        elif sig is not None:
            res.decision = self._decide_entry(sig, a, snap, res.pid or 0, res)
        else:
            res.decision = "SEM SINAL — " + a.edge_status
        # OPPORTUNITY ENGINE + FUNIL: toda análise vira um registro (entrada, ou a primeira etapa em que caiu)
        from .opportunity import DecisionRecord, classify_reason, funnel_stage
        direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
        is_raw, stage = funnel_stage(a, sig, self.engine.gate.last_reason, res.decision, self.engine.cfg)
        self.mem.record_decision(DecisionRecord(snap.time, a.price, a.score, direction.value, classify_reason(res.decision), res.decision,
                                                snap.atr or 0.0, None, int(a.evidence_level), a.confidence), symbol=self.symbol, stage=stage, is_raw=is_raw)
        return res

    # ------------------------------------------------------------------ entrada
    def _decide_entry(self, sig: Signal, a: Assessment, snap: MarketSnapshot, pid: int, res: CycleResult) -> str:
        if sig.type not in self.EXECUTABLE or sig.direction == Direction.LATERAL:
            return f"NO_TRADE — sinal {sig.type.value} não é operacional"
        allowed, why = self.ks.new_entries_allowed()
        if not allowed:
            return f"BLOQUEADA — {why}"
        blocks = self.perf.blocks(a.time)
        if blocks:
            self._send("\n".join(blocks), res)
            return "BLOQUEADA — " + "; ".join(blocks)
        spread = None
        if self.executor is not None:
            try:
                bid, ask = self.executor.client.tick()
                spread = round(ask - bid, 2)
            except Exception:  # noqa: BLE001
                spread = None
        reasons = no_trade_check(a, self.limits, spread)
        if reasons:
            return "🟡 NÃO OPERAR — " + "; ".join(reasons)
        # uma posição por ativo (memória + broker)
        if self.managed or (self.executor is not None and self.executor.positions()):
            return f"BLOQUEADA — já existe posição ativa em {self.symbol} (MAX_POSITIONS)"
        plan = self.mpe.plan(a, snap, sig.direction, sig.type.value)
        res.plan = plan
        if not plan.viable:
            return "🟡 NÃO OPERAR — " + "; ".join(n for n in plan.notes if n.startswith("⚠️"))
        plan.lots, plan.risk_usd = size_lots(self.limits, self.perf.risk_usd, plan.r_value, self.spec.point_value_usd)   # capital + risco + stop + contrato
        if not plan.lots:
            return f"BLOQUEADA — risco de {self.perf.risk_usd:.2f} USD não comporta o lote mínimo com stop de {plan.r_value:.2f}"
        if self.entry_gate is not None:
            blocked = self.entry_gate(self.symbol, sig.direction, plan.risk_usd)
            if blocked:
                return "BLOQUEADA — exposição de carteira: " + "; ".join(blocked)
        self.log(plan.render())
        if self.mode == TradingMode.AUTHORIZE and not self.authorized:
            self._send("🟡 AGUARDANDO AUTORIZAÇÃO\n" + plan.render(), res)
            return "AGUARDANDO AUTORIZAÇÃO"
        execution = None
        if self.mode != TradingMode.PAPER:
            if self.executor is None:
                return "BLOQUEADA — sem executor MT5 configurado"
            execution = self.executor.open(plan, comment=f"GoldAI {sig.type.value}"[:31])
            self.log(execution.render())
            if execution.error:
                self._send("❌ " + execution.render(), res)
                return "FALHA DE EXECUÇÃO — " + execution.error
            if execution.mismatches:
                self._send("⚠️ " + execution.render(), res)
                if any(m.startswith("SL real") for m in execution.mismatches):
                    self.executor.close(execution.ticket)
                    return "EXECUTION MISMATCH — posição sem SL correto foi encerrada por segurança"
            self.authorized = False
        tid = self.mem.open_trade(plan, self.mode.value, pid, self.horizon, symbol=self.symbol)
        thesis = Thesis.from_assessment(a, plan.direction)
        tr = ManagedTrade(tid, plan, thesis)
        if execution is not None:
            self.tickets[tid] = execution.ticket
            tr.plan.entry = execution.fill_price or plan.entry
        self.mem.save_thesis(tid, thesis, tr.state_dict())
        self.mem.save_execution(tid, execution, self.perf.equity, self.limits.risk_per_trade_pct, a)
        self.managed.append(tr)
        self._send(format_entry(plan, a, self.mode.value, execution, self.symbol), res)
        return f"{'🟢 POSITION OPEN' if execution else '🟢 PAPER OPEN'} #{tid:05d}"

    # ------------------------------------------------------------------ monitor
    def _monitor_trade(self, tr: ManagedTrade, a: Assessment, snap: MarketSnapshot, fine, res: CycleResult) -> None:
        now = snap.time
        ticket = self.tickets.get(tr.trade_id)
        # 1) o broker fechou (stop/TP)?
        if ticket and self.executor is not None:
            closed = self.executor.closed_result(ticket)
            if closed is not None:
                r_exit = tr.r_at(closed["price"]) if closed.get("price") else tr.stop_r
                tr.close(r_exit, "BROKER", closed.get("time") or now)
                self._finalize(tr, now, res, pnl_usd=closed.get("profit"))
                return
        # 2) caminho do preço desde a última leitura (stop/trailing) + reavaliação da tese
        before = (tr.remaining, tr.stop_r)
        reading = self.monitor.check_path(tr, fine)
        from_path = reading is not None
        if reading is None and tr.status == "OPEN":
            reading = self.monitor.evaluate(tr, a, snap)
        if reading is None:
            return
        if from_path:
            if tr.status == "CLOSED" and ticket and self.executor is not None and self.executor.position(ticket) is not None:
                self.executor.close(ticket)                    # stop lógico tocado antes de sincronizar com o broker
            elif tr.status == "OPEN" and ticket and self.executor is not None and abs(tr.stop_r - before[1]) > 1e-9:
                self.executor.modify(ticket, tr.price_at_r(tr.stop_r), None if tr.extending else (tr.plan.targets.get(tr.plan.recommended) or None))
        else:
            self._apply_to_broker(tr, reading, before, res)   # ENCERRAR / parcial / trailing / zero a zero chegam ao broker
        self.mem.log_monitor(tr.trade_id, reading)
        res.readings.append((tr, reading))
        self.log(render_monitor(tr, reading))
        if tr.status == "CLOSED":
            self._finalize(tr, now, res)
        else:
            self.mem.save_state(tr.trade_id, tr.state_dict())
            if reading.action == "PROTEGER":
                self._send(format_protection(tr, reading), res)
            elif reading.action in ("REDUZIR", "ESTENDER"):
                self._send("📊 GOLD AI MONITOR\n" + render_monitor(tr, reading), res)

    def _apply_to_broker(self, tr: ManagedTrade, reading: MonitorReading, before: tuple[float, float], res: CycleResult) -> None:
        """Traduz a decisão do monitor em ações no broker (LIVE / SEMI_LIVE)."""
        ticket = self.tickets.get(tr.trade_id)
        if not ticket or self.executor is None:
            return
        remaining_before, stop_before = before
        if reading.action == "ENCERRAR" and tr.status == "CLOSED":
            in_profit = reading.current_r > 0
            if self.mode == TradingMode.SEMI_LIVE and in_profit and not self.ks.paused:
                # ação crítica em SEMI_LIVE: pede confirmação, mas protege com stop no zero a zero
                self.executor.modify(ticket, tr.plan.entry, None)
                tr.status, tr.close_reason, tr.result_r, tr.closed_at = "OPEN", "", None, None
                tr.remaining = remaining_before
                tr.stop_r = max(stop_before, 0.0)
                self.pending_close_confirm.append(tr.trade_id)
                self._send(f"🟠 SEMI-LIVE: monitor pede ENCERRAR #{tr.trade_id:05d} com lucro ({reading.current_r:+.2f}R). Stop movido ao zero a zero. Responda /CLOSE CONFIRM.", res)
                reading.action, reading.note = "PROTEGER", reading.note + " (aguardando confirmação para encerrar)"
                return
            ok, price = self.executor.close(ticket)
            if ok and price is not None:
                tr.result_r = round(tr.realized_r + (remaining_before) * tr.r_at(price), 3) if tr.result_r is None else tr.result_r
            return
        if tr.remaining < remaining_before:  # parcial (PROTEGER/REDUZIR)
            pos = self.executor.position(ticket)
            if pos is not None:
                vol = round(pos.volume * (1 - tr.remaining / remaining_before), 2)
                vol = max(self.limits.min_lot, vol)
                if vol < pos.volume:
                    self.executor.close(ticket, vol, "GoldAI partial")
        if abs(tr.stop_r - stop_before) > 1e-9:
            self.executor.modify(ticket, tr.price_at_r(tr.stop_r), None if tr.extending else (tr.plan.targets.get(tr.plan.recommended) or None))

    def _close_trade(self, tr: ManagedTrade, r_exit: float, reason: str, now: datetime, res: CycleResult, price_hint: Optional[float]) -> None:
        ticket = self.tickets.get(tr.trade_id)
        if ticket and self.executor is not None:
            ok, price = self.executor.close(ticket)
            if ok and price is not None:
                r_exit = tr.r_at(price)
        tr.close(r_exit, reason, now)
        self._finalize(tr, now, res)

    def _finalize(self, tr: ManagedTrade, now: datetime, res: CycleResult, pnl_usd: Optional[float] = None) -> None:
        risk = tr.plan.risk_usd or 0.0
        pnl = pnl_usd if pnl_usd is not None else round((tr.result_r or 0.0) * risk, 2)
        minutes = (now - tr.plan.time).total_seconds() / 60
        self.mem.close_managed(tr.trade_id, tr.result_r or 0.0, tr.close_reason, now, tr.state_dict())
        # previsão correta? lead time?
        correct = (tr.result_r or 0.0) > 0
        lead = next((h.time for h in tr.history if h.current_r >= 1.0), None)
        lead_min = (lead - tr.plan.time).total_seconds() / 60 if lead else None
        self.mem.save_financial_result(tr.trade_id, pnl, round(minutes, 1), lead_min)
        self.perf.record_result(pnl, now, f"trade #{tr.trade_id}")
        self.mem.record_equity(now, self.perf.equity, pnl, f"trade #{tr.trade_id} {tr.close_reason}")
        self.log(render_evolution(tr))
        self.log(self.perf.render())
        if tr.close_reason in ("TESE INVALIDADA", "EXIT SCORE"):
            self._send(format_scenario_change(tr, tr.history[-1]), res)
        self._send(format_result(tr, pnl, correct, lead_min, minutes), res)
        if tr in self.managed:
            self.managed.remove(tr)
        self.tickets.pop(tr.trade_id, None)
        if self.perf.trading_stop:
            self._send("🚨 TRADING STOP — perda diária máxima atingida; sem novas entradas hoje", res)
