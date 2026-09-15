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
from datetime import datetime, timedelta, timezone
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
        from .opportunity import opportunity_level
        for sym, r in self.results.items():
            a = r.assessment
            if a is None:
                lines.append(f"  {sym:<7} sem dados")
                continue
            level = opportunity_level(a, r.signal, r.decision.startswith(("🟢 PAPER OPEN", "🟢 POSITION OPEN")))
            lines.append(f"  {sym:<7} score {a.score:+4.0f} prob {max(a.prob_up, a.prob_down):.0%} {a.regime:<8} {a.premove.stage.value:<14} "
                         f"nível {level:<11} {'sinal ' + r.signal.type.value if r.signal else 'sem sinal'} → {r.decision}")
        lines.append(f"DECISÃO: {self.decision}")
        return "\n".join(lines)


class MarketAIEngine:
    def __init__(self, mem: PredictionMemory, limits: GuardLimits, symbols: tuple[str, ...], mode: TradingMode = TradingMode.PAPER,
                 equity: float = 10000.0, portfolio: Optional[PortfolioLimits] = None, executors: Optional[dict] = None,
                 sender: Optional[TelegramSender] = None, kill_switch: Optional[KillSwitch] = None, commands: Optional[TelegramCommands] = None,
                 horizon_min: int = 240, log: Callable[[str], None] = print, authorized: bool = False,
                 selector: Optional[AssetSelector] = None, calibrator=None, edge_bank=None, params: Optional[dict] = None) -> None:
        self.mem = mem
        self.params = params or {}                                    # autotune (5.2): {mercado: {"params", "apply", ...}}
        self.edge_bank = edge_bank                                    # edge_bank.EdgeBank (5.2) — opcional
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
            mem.record_equity(datetime.now(timezone.utc), start_equity, None, "capital inicial")
        else:
            fixed = mem.neutralize_baseline_syncs()
            if fixed:
                log(f"[conta] {fixed} registro(s) antigo(s) de 'sync broker' reclassificados como linha de base (não eram resultado do dia)")
            self.perf.restore(mem.account_rows(), datetime.now(timezone.utc))   # reinício não apaga perda do dia, meta nem pico
        self.engines: dict[str, LiveExecutionEngine] = {}
        from .autotune import apply_params
        for sym, spec in self.specs.items():
            cfg = EngineConfig(factor_signs=dict(spec.factor_signs), symbol=sym)
            cfg, note = apply_params(cfg, self.params.get(sym))
            if self.params:
                log(f"🧠 PARÂMETROS {sym}: {note}")
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
        self.flow_engine = FlowAnomalyEngine(ledger=mem.flow_anomaly_rows())     # 5.2: anomalias já medidas alimentam o relógio (histórico decide)
        self.active_flows: dict[str, object] = {}
        self.flow_assessments: dict = {}
        # histórico fino dos líderes (USD/YIELD) ao redor do evento: função (nome, início, fim) → [(t, valor)] (Yahoo M1 / MT5); opcional
        self.lead_history: Optional[Callable[[str, datetime, datetime], list]] = None

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
                # 5.2 — LEDGER: registrar a anomalia (1 por episódio de 60 min) para medir 5/15/30/60 min depois;
                # o mesmo episódio não é anunciado de novo após um reinício (o ledger é a memória, não o processo)
                already = self.mem.recent_flow_anomaly(sym, now - timedelta(minutes=60))
                if not already:
                    prev = getattr(getattr(self, "last_cycle", None), "results", {}).get(sym)
                    regime = str(getattr(getattr(prev, "assessment", None), "regime", "") or "")
                    self.mem.open_flow_anomaly(fa.ledger_record(now, snap, regime))
                hist = f"histórico n={fa.history_n}: continuação {fa.continuation_p:.0%}" if fa.continuation_p is not None else "histórico: ainda sem 5 casos medidos"
                if already:
                    continue
                self.sender.send(f"🟣 FLUXO ANÔMALO — {sym} {'↑' if fa.direction > 0 else '↓'} {fa.move_atr:+.2f} ATR · FLOW SCORE {fa.score} · origem NÃO identificada "
                                 f"(assinatura {fa.signature})\n{fa.chain.splitlines()[1] if len(fa.chain.splitlines()) > 1 else ''}\n"
                                 f"MODO INVESTIGAÇÃO (WATCH): relógio aberto nos demais mercados, procurando continuação e quem está atrasado · {hist}.")
        self._measure_flow_anomalies(snaps)
        # expira fluxos com mais de 2 h ou revertidos
        for sym in list(self.active_flows):
            ev = self.active_flows[sym]
            fa = self.flow_assessments.get(sym)
            if ev.age_min(now) > 120 or (fa is not None and fa.direction != 0 and fa.direction != ev.direction_sign * (1 if ev.kind.endswith("_up") else -1) and fa.score < 40):
                del self.active_flows[sym]
        for ev in self.active_flows.values():
            if all(getattr(x, "kind", None) != ev.kind or getattr(x, "time", None) != ev.time for x in snaps.identified):
                snaps.identified.append(ev)

    def _measure_flow_anomalies(self, snaps: MarketSnapshotSet) -> None:
        """60 min após cada anomalia registrada: MFE/MAE em 5/15/30/60, CONTINUOU / REVERTEU / INDEFINIDO, minutos até confirmação (M1 do broker)."""
        from .flow_anomaly import measure_flow_outcome
        now = snaps.time
        for row in self.mem.pending_flow_anomalies(now, 60):
            snap = snaps.by_symbol.get(row["ativo"])
            candles = (snap.candles.get("M1") or snap.candles.get("M5") or []) if snap is not None else []
            t0 = datetime.fromisoformat(row["hora"])
            if not candles or candles[-1].time < t0 + timedelta(minutes=55):
                if (now - t0) > timedelta(hours=6):                          # sem M1 para medir (fonte sem intradiário): fecha como SEM DADOS
                    self.mem.close_flow_anomaly(row["id"], {"resultado": "SEM DADOS"}, now)
                continue
            m = measure_flow_outcome(row["direcao"], row["preco"], row["atr"], candles, t0)
            self.mem.close_flow_anomaly(row["id"], m, now)
            self.flow_engine.ledger = self.mem.flow_anomaly_rows()
            self.log(f"🟣 ANOMALIA MEDIDA {row['ativo']} {t0:%H:%M} (FLOW {row['flow_score']}, origem {row['origem']}): {m['resultado']} · "
                     f"MFE 5/15/30/60 = {m['mfe5']:.2f}/{m['mfe15']:.2f}/{m['mfe30']:.2f}/{m['mfe60']:.2f} ATR · MAE60 {m['mae60']:.2f} · "
                     f"confirmação {'—' if m['confirm_min'] is None else str(round(m['confirm_min'])) + ' min'}")

    # ------------------------------------------------------------------ comandos Telegram (carteira inteira)
    def _handle_commands(self, snaps: MarketSnapshotSet, pc: PortfolioCycle) -> None:
        if self.commands is None:
            return
        res = CycleResult(None, None)
        pending = [c for c in getattr(self.commands, "last_cmds", []) if c.startswith("/EDGE")]   # /EDGE aplicado fora do ciclo (teste/sob demanda)
        actions = self.commands.apply(self.commands.poll(), self.ks)
        if pending and "EDGE" not in actions:
            actions.append("EDGE")
        for action in actions:
            if action == "EDGE":
                self.daily_edge(snaps.time, pc, force=True)
            elif action == "STATUS":
                self.sender.send(self.status_text())
            elif action == "FLOW":
                from .flow_anomaly import render_flow_stats
                rows = self.mem.flow_anomaly_rows(measured_only=False)
                pend = sum(1 for r in rows if not r.get("resultado"))
                self.sender.send(render_flow_stats(rows) + f"\nregistradas {len(rows)} · medidas {len(rows) - pend} · aguardando medição {pend}")
            elif action in ("STOP", "PAUSE", "RESUME"):
                self.sender.send(f"🔧 comando /{action} aplicado — " + self.ks.new_entries_allowed()[1])
            elif action == "CLOSE_REQUESTED":
                n = sum(len(e.managed) for e in self.engines.values())
                self.sender.send(f"⚠️ /CLOSE solicitado para {n} posição(ões) em {len(self.engines)} mercado(s). Responda /CLOSE CONFIRM para encerrar.")
            elif action == "CLOSE_CONFIRMED":
                for sym, eng in self.engines.items():
                    snap = snaps.by_symbol.get(sym)
                    eng.close_all(snaps.time, res, snap.price if snap is not None else None)
                self.sender.send("🔴 posições encerradas por /CLOSE CONFIRM (todos os mercados)")
        pc.messages += res.messages

    # ------------------------------------------------------------------ autorização única (AUTHORIZE: uma ordem real por --authorize, não uma por mercado)
    def _consume_authorization(self, sym: str) -> None:
        for other, eng in self.engines.items():
            if other != sym:
                eng.authorized = False

    # ------------------------------------------------------------------ REACTION ENGINE (live)
    def _reaction_clock(self, snaps: MarketSnapshotSet) -> None:
        """A cada ciclo: relógio por mercado (evidência para o pré-movimento) + amostragem dos eventos em curso; ao fechar o
        horizonte, mede a reação (alvo e líderes) e grava — o sistema aprende com os eventos que viveu, nunca com o futuro."""
        from .news_engine import TRANSMISSION, expected_direction
        from .reaction import ReactionClock, measure_reaction
        from .history import EXTRA_TRANSMISSION

        now = snaps.time
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
                # T0 REAL: semeia líderes e alvos com histórico M1/M5 já fechado ao redor do evento (não depende do instante em que o loop viu a notícia)
                t_from = ev.time - timedelta(minutes=15)
                if self.lead_history is not None:
                    for name in ("USD", "YIELD"):
                        try:
                            hist = self.lead_history(name, t_from, now) or []
                        except Exception:  # noqa: BLE001
                            hist = []
                        self.pending_reactions[key]["leads"][name] = [(t, v) for t, v in hist if t <= now]
                for sym, snap in snaps.by_symbol.items():
                    exp, _ = expected_direction(ev, sym)
                    if exp != 0.0 and snap.price:
                        seed = []
                        for tf, mins in (("M1", 1), ("M5", 5)):
                            cs = snap.candles.get(tf) or []
                            if cs:
                                seed = [(c.time + timedelta(minutes=mins), c.close) for c in cs if t_from <= c.time + timedelta(minutes=mins) <= now]
                                break
                        self.pending_reactions[key]["targets"][sym] = {"exp": exp, "atr": snap.atr or 0.0, "series": seed}
            pr = self.pending_reactions[key]
            if base.dxy is not None:
                pr["leads"]["USD"].append((now, base.dxy))
            if base.us10y is not None:
                pr["leads"]["YIELD"].append((now, base.us10y))
            for sym, tg in pr["targets"].items():
                snap = snaps.by_symbol.get(sym)
                if snap is not None and snap.price:
                    tg["series"].append((now, snap.price))
        clock = ReactionClock(self.reaction_stats)
        # Δ dos líderes DESDE O EVENTO (não da última hora) a partir das amostras/histórico já guardados
        lead_since: dict[str, float] = {}
        if snaps.identified and self.pending_reactions:
            ev0 = max(snaps.identified, key=lambda e: e.time)
            for pr in self.pending_reactions.values():
                if pr["ev"].time == ev0.time and pr["ev"].kind == ev0.kind:
                    for name, pct in (("USD", True), ("YIELD", False)):
                        ser = pr["leads"].get(name) or []
                        base_pt = next((v for t, v in ser if t <= ev0.time), ser[0][1] if ser else None)
                        if base_pt and ser:
                            last = ser[-1][1]
                            lead_since[name] = ((last / base_pt - 1) * 100) if pct else ((last - base_pt) * 100)
                    break
        for sym, snap in snaps.by_symbol.items():
            ra = clock.assess(sym, snap, snaps.identified, now, lead_since or None)
            snap.reaction_status, snap.reaction_pressure, snap.reaction_probability = ra.status, ra.pressure, ra.probability
            snap.reaction_latency_min, snap.reaction_expected_min, snap.reaction_chain = ra.latency_min, ra.expected_min, ra.chain
        for key in list(self.pending_reactions):
            pr = self.pending_reactions[key]
            ev = pr["ev"]
            if ev.age_min(now) < self.reaction_horizon:
                continue
            for sym, tg in pr["targets"].items():
                cands = snaps.by_symbol[sym].candles if sym in snaps.by_symbol else {}
                fine, res_min = None, 1
                for tf, mins in (("M1", 1), ("M5", 5), ("M15", 15)):
                    if cands.get(tf):
                        fine, res_min = cands[tf], mins
                        break
                # candles carimbados na abertura → série no FECHO; só barras fechadas até agora
                series = [(c.time + timedelta(minutes=res_min), c.close) for c in fine if c.time + timedelta(minutes=res_min) >= ev.time - timedelta(minutes=10)] if fine else list(tg["series"])
                first_price = tg["series"][0][1] if tg["series"] else None      # 1ª amostra observada (alguns minutos após a publicação, nunca antes)
                if first_price is not None and (not series or series[0][0] > ev.time):
                    series = [(ev.time, first_price)] + list(series)
                rec = measure_reaction(key, ev.kind, ev.time, sym, tg["exp"], series, tg["atr"], pr["leads"], pr["lead_dirs"], self.reaction_horizon,
                                       res_min if fine else 1)
                if rec:
                    self.mem.save_reaction(rec)
                    self.reaction_stats.add(rec)
                    self.log(f"⏱️ REACTION registrada: {ev.name[:40]} → {sym}: 1ª reação {rec.time_to_first or 'não'} min · confirmação "
                             f"{rec.time_to_confirmation or 'não'} min · MFE {rec.max_move_atr:.2f} ATR · líderes {rec.lead_times}")
            del self.pending_reactions[key]

    # ------------------------------------------------------------------ histórico por mercado
    def refresh_history(self) -> None:
        from .lifecycle import evaluate_parameter as lifecycle_evaluate
        if not hasattr(self, "lifecycle"):
            self.lifecycle = {}
            self._real_mode = {sym: eng.mode for sym, eng in self.engines.items()}
        for sym in self.specs:
            rs = self.mem.r_stats(sym)
            results = self.mem.results_chrono(sym)
            self.history[sym] = statistical_confidence(results)
            self.engines[sym].mpe.history = self.engines[sym].monitor.history = rs
            # CICLO DE VIDA: estado por mercado (amostra OOS + sequência); SOMBRA = motor do mercado cai para PAPER até revalidar
            prev = self.lifecycle[sym].action if sym in self.lifecycle else "NORMAL"
            st = lifecycle_evaluate(sym, results, prev)
            changed = sym in self.lifecycle and st.action != prev
            self.lifecycle[sym] = st
            eng = self.engines[sym]
            real_mode = self._real_mode.get(sym, eng.mode)
            if st.action == "QUEBRADO" and real_mode != TradingMode.PAPER:
                if eng.mode != TradingMode.PAPER:
                    eng.mode = TradingMode.PAPER
                    self.sender.send(f"⛔ {sym}: parâmetro QUEBRADO — {st.note}. Mercado segue em SOMBRA (PAPER) até revalidar.")
            elif eng.mode == TradingMode.PAPER and real_mode != TradingMode.PAPER and st.action in ("REATIVADO", "NORMAL"):
                eng.mode = real_mode
                self.sender.send(f"♻️ {sym}: parâmetro revalidado — volta ao modo {real_mode.value}.")
            if changed and st.action in ("ALERTA", "PROTEÇÃO", "SUSPENSO", "REATIVADO"):
                self.sender.send({"ALERTA": "⚠️", "PROTEÇÃO": "🟠", "SUSPENSO": "🔴", "REATIVADO": "♻️"}[st.action] + f" {sym}: {st.note}")

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
        # comandos (/STOP /PAUSE /RESUME /STATUS /CLOSE /EDGE) tratados AQUI, para todos os mercados, com o kill switch compartilhado
        self._handle_commands(snaps, pc)
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
            c = Candidate(self.specs[sym], a, sig, snaps.by_symbol[sym], self.history[sym], opp.capture_rate, snaps.data_quality.get(sym, 1.0))
            lc = getattr(self, "lifecycle", {}).get(sym)
            c.lifecycle_multiplier = lc.confidence_multiplier if lc is not None else 1.0
            c.edge_multiplier, c.edge_note = 1.0, ""
            if self.edge_bank is not None:
                from .opportunity import live_context_tags
                c.edge_multiplier, c.edge_note = self.edge_bank.multiplier(sym, live_context_tags(a, getattr(snaps, "identified", []) or [], snaps.time))
                if c.edge_note:
                    self.log(f"🏦 EDGE BANK {sym}: contexto {c.edge_note} → prioridade ×{c.edge_multiplier:.1f}")
            cands.append(c)
        for sym in self.specs:
            if sym not in {c.spec.symbol for c in cands}:
                self.selector.forget(sym)
        # 3) ASSET SELECTOR — ordena; a melhor tenta entrar (Risk Engine + exposição validam depois)
        pc.ranked = self.selector.rank(cands, snaps.time)
        self.log(render_rank(pc.ranked, self.history))
        # 5.2 PORTFOLIO OPPORTUNITY: até N entradas por ciclo, em ordem de prioridade; cada uma passa pelo funil do seu mercado e pelo
        # motor de exposição (risco total, risco correlacionado = mesma tese, posições). Sem N sinais não há N entradas; com N sinais
        # da mesma tese o risco correlacionado barra a partir da segunda.
        entered_syms: list[str] = []
        max_entries = max(1, int(getattr(self.portfolio.limits, "max_entries_per_cycle", 1)))
        for c in pc.ranked:
            sym = c.spec.symbol
            r = pc.results[sym]
            if len(entered_syms) >= max_entries:
                self.engines[sym].enter(r, snaps.by_symbol[sym], veto=f"PRIORIDADE — limite de {max_entries} entrada(s) por ciclo atingido ({', '.join(entered_syms)}; OPP {c.opportunity_score:.1f})")
                continue
            snap_c = snaps.by_symbol[sym]
            lc = getattr(self, "lifecycle", {}).get(sym)
            if lc is not None and not lc.allows_entries and lc.action != "QUEBRADO":
                self.engines[sym].enter(r, snap_c, veto=f"CICLO DE VIDA — {sym} em {lc.action}: {lc.note}")
                continue
            if snap_c.anomalous_regime and snap_c.flow_direction != 0 and ((c.direction == Direction.ALTA) != (snap_c.flow_direction > 0)):
                self.engines[sym].enter(r, snap_c, veto=f"MODO INVESTIGAÇÃO em {sym} — entrada CONTRA o fluxo anômalo adiada (FLOW {snap_c.flow_score}); a favor/atrasado segue o funil normal")
                continue
            was_auth = self.engines[sym].authorized
            self.engines[sym].enter(r, snap_c)
            if was_auth and not self.engines[sym].authorized:
                self._consume_authorization(sym)
            pc.messages += [m for m in r.messages if m not in pc.messages]
            if r.decision.startswith(("🟢 PAPER OPEN", "🟢 POSITION OPEN")):
                entered_syms.append(sym)
                pc.chosen = sym if pc.chosen is None else f"{pc.chosen}+{sym}"
        # mercados sem candidatura: registrar a decisão (regra que bloqueou) para o Opportunity Engine
        for sym, r in pc.results.items():
            if r.assessment is not None and sym not in {c.spec.symbol for c in pc.ranked}:
                self.engines[sym].enter(r, snaps.by_symbol[sym])
        pc.decision = (f"ENTRADA em {pc.chosen}" if pc.chosen else ("melhor oportunidade não passou no Risk Engine/exposição — " + pc.results[pc.ranked[0].spec.symbol].decision
                                                                   if pc.ranked else "nenhuma oportunidade com vantagem neste ciclo"))
        self.log(self.portfolio.render(self.open_exposures(), self.perf.equity))
        # 🚨 LIVE EDGE — o teste definitivo, uma vez por dia (e sob demanda com /EDGE)
        self.daily_edge(snaps.time, pc)
        self.last_cycle = pc
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
        if getattr(self, "lifecycle", None):
            from .lifecycle import render_table
            lines.append(render_table(list(self.lifecycle.values())))
        if self.edge_bank is not None and self.edge_bank.stats:
            lines.append(self.edge_bank.render(list(self.specs), min_n=1, top=5))
        rows = self.mem.flow_anomaly_rows()
        if rows:
            from .flow_anomaly import render_flow_stats
            lines.append(render_flow_stats(rows))
        return "\n".join(lines)
