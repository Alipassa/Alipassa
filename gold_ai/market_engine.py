"""MARKET AI ENGINE 4.0 — cérebro único · múltiplos mercados · seleção dinâmica da melhor oportunidade.

Objetivo: "Analisar vários mercados simultaneamente e operar somente aquele que apresentar a melhor
vantagem estatística disponível naquele momento, respeitando risco, correlação, qualidade dos dados
e custo de execução." A IA não precisa operar ouro; precisa encontrar onde existe vantagem.

Preserva integralmente os motores do 3.0 (um LiveExecutionEngine por mercado, capital compartilhado).
O Asset Selector NÃO cria entradas: só ordena as que o Prediction/Opportunity Engine já produziu.
Nenhum filtro de entrada novo: os vetos do 4.0 são exclusivamente de PORTFÓLIO (exposição/correlação)
e de PRIORIDADE (um ciclo, uma entrada: a melhor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from .config import EngineConfig
from .data.multi import MarketSnapshotSet
from .engine import GoldAIEngine
from .guard import GuardLimits, KillSwitch, PerformanceEngine, TelegramCommands, TradingMode
from .live_engine import CycleResult, LiveExecutionEngine
from .markets import MarketSpec, get_market
from .memory import PredictionMemory
from .models import Direction, SignalType
from .selector import (AssetSelector, Candidate, OpenExposure, PortfolioExposureEngine, PortfolioLimits, StatConfidence, render_rank,
                       statistical_confidence)
from .telegram import TelegramSender


@dataclass
class PortfolioCycle:
    time: datetime
    results: dict[str, CycleResult] = field(default_factory=dict)
    ranked: list[Candidate] = field(default_factory=list)
    chosen: Optional[str] = None
    decision: str = ""
    messages: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"🌎 MARKET AI — ciclo {self.time:%Y-%m-%d %H:%M} UTC"]
        for sym, r in self.results.items():
            a = r.assessment
            if a is None:
                lines.append(f"  {sym:<7} sem dados")
                continue
            lines.append(f"  {sym:<7} score {a.score:+4.0f} prob {max(a.prob_up, a.prob_down):.0%} {a.regime:<8} {a.premove.stage.value:<14} "
                         f"{'sinal ' + r.signal.type.value if r.signal else 'sem sinal'} → {r.decision}")
        lines.append(f"DECISÃO: {self.decision}")
        return "\n".join(lines)


class MarketAIEngine:
    def __init__(self, mem: PredictionMemory, limits: GuardLimits, symbols: tuple[str, ...], mode: TradingMode = TradingMode.PAPER,
                 equity: float = 10000.0, portfolio: Optional[PortfolioLimits] = None, executors: Optional[dict] = None,
                 sender: Optional[TelegramSender] = None, kill_switch: Optional[KillSwitch] = None, commands: Optional[TelegramCommands] = None,
                 horizon_min: int = 240, log: Callable[[str], None] = print, authorized: bool = False,
                 selector: Optional[AssetSelector] = None, calibrator=None) -> None:
        self.mem = mem
        self.specs: dict[str, MarketSpec] = {s: get_market(s) for s in symbols}
        self.mode, self.limits = mode, limits
        self.portfolio = PortfolioExposureEngine(portfolio or PortfolioLimits())
        self.sender = sender or TelegramSender(dry_run=True, quiet=True)
        self.ks = kill_switch or KillSwitch()
        self.commands = commands
        self.log = log
        self.selector = selector or AssetSelector()
        start_equity = mem.last_equity() or equity
        self.perf = PerformanceEngine(limits, start_equity)   # capital ÚNICO compartilhado
        if mem.last_equity() is None:
            mem.record_equity(datetime.now(), start_equity, None, "capital inicial")
        self.engines: dict[str, LiveExecutionEngine] = {}
        for sym, spec in self.specs.items():
            cfg = EngineConfig(factor_signs=dict(spec.factor_signs), symbol=sym)
            brain = GoldAIEngine(cfg, calibrator=calibrator)
            self.engines[sym] = LiveExecutionEngine(mem, limits, mode, equity, (executors or {}).get(sym), self.sender, self.ks, None,
                                                    horizon_min, brain, log, authorized, spec=spec, perf=self.perf, entry_gate=self._portfolio_gate)
        self.history: dict[str, StatConfidence] = {}
        self.refresh_history()
        # REACTION ENGINE (live): estatística do que foi vivido + cronômetros dos eventos em curso
        from .reaction import ReactionStats
        self.reaction_stats = ReactionStats(mem.reaction_records())
        self.pending_reactions: dict[str, dict] = {}     # event_id → {ev, exp/lead_dirs por ativo, série do alvo, séries líderes}
        self.reaction_horizon = min(horizon_min, 240)
        # FLOW ANOMALY ENGINE (5.0): eventos implícitos ativos (mercado → IdentifiedEvent) e assinaturas do ciclo
        from .flow_anomaly import FlowAnomalyEngine
        self.flow_engine = FlowAnomalyEngine()
        self.active_flows: dict[str, object] = {}
        self.flow_assessments: dict = {}

    def _flow_anomaly(self, snaps: MarketSnapshotSet) -> None:
        """Informação implícita: movimento anormal sem explicação vira evento IMPLÍCITO no REACTION ENGINE (líder → atrasados)."""
        now = snaps.time
        self.flow_assessments = {}
        for sym, snap in snaps.by_symbol.items():
            fa = self.flow_engine.assess(sym, snap, snaps.identified, now, snaps.by_symbol)
            self.flow_assessments[sym] = fa
            snap.flow_score, snap.flow_status, snap.flow_origin, snap.flow_direction = fa.score, fa.status, fa.origin, fa.direction
            snap.anomalous_regime, snap.flow_chain = fa.anomalous_regime, fa.chain
            ev = fa.implicit_event(now)
            if ev is not None and sym not in self.active_flows:
                self.active_flows[sym] = ev
                self.log(fa.chain)
                self.sender.send(f"🟣 FLUXO ANÔMALO — {sym} {'↑' if fa.direction > 0 else '↓'} {fa.move_atr:+.2f} ATR · FLOW SCORE {fa.score} · origem NÃO identificada "
                                 f"(assinatura {fa.signature})\n{fa.chain.splitlines()[1] if len(fa.chain.splitlines()) > 1 else ''}\n"
                                 "Relógio de reação aberto nos demais mercados: procurando quem ainda está atrasado.")
        # expira fluxos com mais de 2 h ou revertidos
        for sym in list(self.active_flows):
            ev = self.active_flows[sym]
            fa = self.flow_assessments.get(sym)
            if ev.age_min(now) > 120 or (fa is not None and fa.direction != 0 and fa.direction != ev.direction_sign * (1 if ev.kind.endswith("_up") else -1) and fa.score < 40):
                del self.active_flows[sym]
        for ev in self.active_flows.values():
            if all(getattr(x, "kind", None) != ev.kind or getattr(x, "time", None) != ev.time for x in snaps.identified):
                snaps.identified.append(ev)

    # ------------------------------------------------------------------ REACTION ENGINE (live)
    def _reaction_clock(self, snaps: MarketSnapshotSet) -> None:
        """A cada ciclo: relógio por mercado (evidência para o pré-movimento) + amostragem dos eventos em curso; ao fechar o
        horizonte, mede a reação (alvo e líderes) e grava — o sistema aprende com os eventos que viveu, nunca com o futuro."""
        from .news_engine import TRANSMISSION, expected_direction
        from .reaction import ReactionClock, measure_reaction
        from .history import EXTRA_TRANSMISSION

        now = snaps.time
        clock = ReactionClock(self.reaction_stats)
        for sym, snap in snaps.by_symbol.items():
            ra = clock.assess(sym, snap, snaps.identified, now)
            snap.reaction_status, snap.reaction_pressure, snap.reaction_probability = ra.status, ra.pressure, ra.probability
            snap.reaction_latency_min, snap.reaction_expected_min, snap.reaction_chain = ra.latency_min, ra.expected_min, ra.chain
        base = snaps.base
        for ev in snaps.identified:
            key = f"{ev.kind}:{ev.time:%Y%m%d%H%M}:{ev.name[:30]}"
            if key not in self.pending_reactions:
                if any(self.mem.has_reaction(key, sym) for sym in self.specs):
                    continue
                chans = TRANSMISSION.get(ev.kind) or EXTRA_TRANSMISSION.get(ev.kind) or {}
                sign = ev.direction_sign or 1.0
                self.pending_reactions[key] = {"ev": ev, "leads": {"USD": [], "YIELD": []}, "targets": {},
                                               "lead_dirs": {"USD": sign * chans.get("dollar", 0.0), "YIELD": sign * chans.get("yields", 0.0)}}
                for sym, snap in snaps.by_symbol.items():
                    exp, _ = expected_direction(ev, sym)
                    if exp != 0.0 and snap.price:
                        self.pending_reactions[key]["targets"][sym] = {"exp": exp, "atr": snap.atr or 0.0, "series": []}
            pr = self.pending_reactions[key]
            if base.dxy is not None:
                pr["leads"]["USD"].append((now, base.dxy))
            if base.us10y is not None:
                pr["leads"]["YIELD"].append((now, base.us10y))
            for sym, tg in pr["targets"].items():
                snap = snaps.by_symbol.get(sym)
                if snap is not None and snap.price:
                    tg["series"].append((now, snap.price))
        for key in list(self.pending_reactions):
            pr = self.pending_reactions[key]
            ev = pr["ev"]
            if ev.age_min(now) < self.reaction_horizon:
                continue
            for sym, tg in pr["targets"].items():
                fine = snaps.by_symbol[sym].candles.get("M5") if sym in snaps.by_symbol else None
                series = [(c.time, c.close) for c in fine if c.time >= ev.time - timedelta(minutes=10)] if fine else tg["series"]
                first_price = next((p for t, p in tg["series"] if t <= ev.time), None)
                if first_price is not None and (not series or series[0][0] > ev.time):
                    series = [(ev.time, first_price)] + list(series)
                rec = measure_reaction(key, ev.kind, ev.time, sym, tg["exp"], series, tg["atr"], pr["leads"], pr["lead_dirs"], self.reaction_horizon,
                                       5 if fine else 1)
                if rec:
                    self.mem.save_reaction(rec)
                    self.reaction_stats.add(rec)
                    self.log(f"⏱️ REACTION registrada: {ev.name[:40]} → {sym}: 1ª reação {rec.time_to_first or 'não'} min · confirmação "
                             f"{rec.time_to_confirmation or 'não'} min · MFE {rec.max_move_atr:.2f} ATR · líderes {rec.lead_times}")
            del self.pending_reactions[key]

    # ------------------------------------------------------------------ histórico por mercado
    def refresh_history(self) -> None:
        for sym in self.specs:
            rs = self.mem.r_stats(sym)
            results = []
            for row in self.mem.conn.execute("SELECT resultado_r FROM trades WHERE ativo=? AND resultado_r IS NOT NULL", (sym,)).fetchall():
                results.append(row["resultado_r"])
            self.history[sym] = statistical_confidence(results)
            self.engines[sym].mpe.history = self.engines[sym].monitor.history = rs

    def open_exposures(self) -> list[OpenExposure]:
        out = []
        for sym, eng in self.engines.items():
            for tr in eng.managed:
                out.append(OpenExposure(sym, tr.thesis.direction, (tr.plan.risk_usd or 0.0) * tr.remaining))
        return out

    def _portfolio_gate(self, symbol: str, direction: Direction, risk_usd: float) -> list[str]:
        return self.portfolio.check(symbol, direction, risk_usd, self.open_exposures(), self.perf.equity)

    # ------------------------------------------------------------------ ciclo de carteira
    def run_cycle(self, snaps: MarketSnapshotSet) -> PortfolioCycle:
        pc = PortfolioCycle(snaps.time)
        # comandos (/STOP /PAUSE /STATUS /CLOSE) tratados pelo primeiro motor, com o kill switch compartilhado
        first = next(iter(self.engines.values()))
        first.commands = self.commands
        if self.commands is not None and any(c.startswith("/EDGE") for c in getattr(self.commands, "last_cmds", [])):
            self.daily_edge(snaps.time, pc, force=True)
        # 0) FLOW ANOMALY (informação implícita) → REACTION ENGINE (relógio por mercado + aprendizado dos eventos concluídos)
        self._flow_anomaly(snaps)
        self._reaction_clock(snaps)
        # 1) cada mercado: monitor das posições abertas + predição (entrada adiada)
        for sym, eng in self.engines.items():
            snap = snaps.by_symbol.get(sym)
            if snap is None:
                pc.results[sym] = CycleResult(None, None, decision="sem dados")
                continue
            r = eng.run_cycle(snap, new_event_key=(snap.news[0].headline if snap.news else None), defer_entry=True)
            pc.results[sym] = r
            pc.messages += r.messages
        self.refresh_history()
        # 2) candidatos = oportunidades já produzidas (sinal operacional + vantagem estatística)
        cands: list[Candidate] = []
        for sym, r in pc.results.items():
            a, sig = r.assessment, r.signal
            if a is None or sig is None or sig.type not in LiveExecutionEngine.EXECUTABLE or sig.direction == Direction.LATERAL or not a.has_edge:
                continue
            opp = self.mem.opportunity_report(symbol=sym)
            cands.append(Candidate(self.specs[sym], a, sig, snaps.by_symbol[sym], self.history[sym], opp.capture_rate, snaps.data_quality.get(sym, 1.0)))
        for sym in self.specs:
            if sym not in {c.spec.symbol for c in cands}:
                self.selector.forget(sym)
        # 3) ASSET SELECTOR — ordena; a melhor tenta entrar (Risk Engine + exposição validam depois)
        pc.ranked = self.selector.rank(cands, snaps.time)
        self.log(render_rank(pc.ranked, self.history))
        entered = False
        for c in pc.ranked:
            sym = c.spec.symbol
            r = pc.results[sym]
            if entered:
                self.engines[sym].enter(r, snaps.by_symbol[sym], veto=f"PRIORIDADE — {pc.chosen} foi a melhor oportunidade do ciclo (OPP {pc.ranked[0].opportunity_score:.1f} vs {c.opportunity_score:.1f})")
                continue
            snap_c = snaps.by_symbol[sym]
            if snap_c.anomalous_regime and snap_c.flow_direction != 0 and ((c.direction == Direction.ALTA) != (snap_c.flow_direction > 0)):
                self.engines[sym].enter(r, snap_c, veto=f"REGIME ANÔMALO em {sym} — entrada contra o fluxo anômalo adiada (FLOW {snap_c.flow_score})")
                continue
            self.engines[sym].enter(r, snap_c)
            pc.messages += [m for m in r.messages if m not in pc.messages]
            if r.decision.startswith(("🟢 PAPER OPEN", "🟢 POSITION OPEN")):
                entered, pc.chosen = True, sym
        # mercados sem candidatura: registrar a decisão (regra que bloqueou) para o Opportunity Engine
        for sym, r in pc.results.items():
            if r.assessment is not None and sym not in {c.spec.symbol for c in pc.ranked}:
                self.engines[sym].enter(r, snaps.by_symbol[sym])
        pc.decision = (f"ENTRADA em {pc.chosen}" if pc.chosen else ("melhor oportunidade não passou no Risk Engine/exposição — " + pc.results[pc.ranked[0].spec.symbol].decision
                                                                   if pc.ranked else "nenhuma oportunidade com vantagem neste ciclo"))
        self.log(self.portfolio.render(self.open_exposures(), self.perf.equity))
        # 🚨 LIVE EDGE — o teste definitivo, uma vez por dia (e sob demanda com /EDGE)
        self.daily_edge(snaps.time, pc)
        return pc

    def edge_report(self, now: Optional[datetime] = None):
        from .edge_report import live_edge_report
        return live_edge_report(self.mem, tuple(self.specs), now, self.perf.equity)

    def daily_edge(self, now: datetime, pc: Optional[PortfolioCycle] = None, force: bool = False) -> Optional[str]:
        today = now.strftime("%Y-%m-%d")
        if not force and self.mem.last_edge_date() == today:
            return None
        rep = self.edge_report(now)
        self.mem.save_edge_report(rep)
        text = rep.render()
        self.log(text)
        self.sender.send("📊 " + text)
        if pc is not None:
            pc.messages.append(text)
        return text

    def status_text(self) -> str:
        lines = [f"📋 MARKET AI STATUS · modo {self.mode.value} · mercados {', '.join(self.specs)}", self.perf.render(),
                 self.portfolio.render(self.open_exposures(), self.perf.equity), "Histórico por mercado:"]
        for sym, h in self.history.items():
            lines.append(f"  {sym:<7} {h.render()}")
        return "\n".join(lines)
