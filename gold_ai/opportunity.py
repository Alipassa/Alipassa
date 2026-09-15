"""GOLD AI ENGINE 3.0 — OPPORTUNITY ENGINE (medição, não regras).

🔥 OPPORTUNITY CAPTURE RATE  movimentos relevantes do período × quantos a IA capturou com entrada
   ENTRY RATE                oportunidades analisadas × entradas (⚠️ OVERFILTER abaixo do mínimo)
   FILTER ATTRIBUTION        qual regra bloqueou cada oportunidade e o que teria acontecido (expectancy hipotética)
   THRESHOLD CURVE           entradas × expectancy por limiar de score — a região ideal, não o score mais alto

Filosofia: analisar muito, decidir simples, agir quando existir vantagem. Se o sistema entra pouco,
não se adiciona filtro: identifica-se qual regra elimina oportunidades lucrativas e reduz-se o seu peso.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .evaluation import detect_moves
from .models import Candle, Direction
from .trading import ExitStrategy, TradePlan, simulate_trade

# Categorias de bloqueio (uma por decisão, a primeira que impediu a entrada)
RULES = ("SEM_SINAL", "SEM_VANTAGEM", "CONFIANCA", "EVIDENCIA", "CONFLITO", "ESTAGIO_3", "SPREAD", "VIABILIDADE",
         "KILL_SWITCH", "TRADING_STOP", "POSICAO_ABERTA", "LOTE", "AUTORIZACAO", "ENTRADA")


def classify_reason(decision: str) -> str:
    d = decision.lower()
    if d.startswith(("🟢 paper open", "🟢 position open")):
        return "ENTRADA"
    if "aguardando autoriza" in d:
        return "AUTORIZACAO"
    if "sem sinal" in d or "não é operacional" in d:
        return "SEM_SINAL"
    if "sem vantagem" in d:
        return "SEM_VANTAGEM"
    if "confiança" in d:
        return "CONFIANCA"
    if "evidência" in d:
        return "EVIDENCIA"
    if "conflitantes" in d:
        return "CONFLITO"
    if "perseguir" in d:
        return "ESTAGIO_3"
    if "spread" in d:
        return "SPREAD"
    if "espaço estatístico" in d or "viabil" in d:
        return "VIABILIDADE"
    if "kill switch" in d or "trading_enabled" in d or "/stop" in d or "pausado" in d:
        return "KILL_SWITCH"
    if "trading stop" in d or "drawdown" in d:
        return "TRADING_STOP"
    if "posição ativa" in d or "posições" in d:
        return "POSICAO_ABERTA"
    if "lote" in d:
        return "LOTE"
    return "OUTRO"


@dataclass
class DecisionRecord:
    time: datetime
    price: float
    score: float
    direction: str                 # ALTA | BAIXA | LATERAL
    action: str                    # ENTRADA | bloqueio (RULES)
    reason: str = ""
    atr: float = 0.0
    hypothetical_r: Optional[float] = None   # o que teria acontecido com a hipótese 3R (stop 1.2 ATR)
    evidence_level: int = 0
    confidence: float = 0.0


def hypothetical_trade(rec: DecisionRecord, candles: Sequence[Candle], horizon_min: int, stop_atr: float = 1.2) -> Optional[float]:
    """Resultado em R da operação que a regra bloqueou, com a hipótese padrão (stop 1.2 ATR, alvo 3R)."""
    if rec.direction not in ("ALTA", "BAIXA") or rec.atr <= 0:
        return None
    sign = 1.0 if rec.direction == "ALTA" else -1.0
    plan = TradePlan(Direction(rec.direction), rec.price, rec.price - sign * stop_atr * rec.atr, rec.atr, rec.time)
    fut = [c for c in candles if c.time > rec.time]
    if not fut or (fut[-1].time - rec.time) < timedelta(minutes=min(horizon_min, 30)):
        return None
    res = simulate_trade(plan, fut, ExitStrategy("3R", target_r=3.0), horizon_min)
    return None if res.exit_reason == "OPEN" else res.r_multiple


@dataclass
class OpportunityReport:
    period_hours: float
    n_moves: int
    n_captured: int
    capture_rate: Optional[float]
    n_analyzed: int
    n_entries: int
    entry_rate: Optional[float]
    overfilter: bool
    attribution: list[dict] = field(default_factory=list)     # {rule, n, hyp_n, hyp_expectancy, hyp_win}
    threshold_curve: list[dict] = field(default_factory=list) # {threshold, n, expectancy, win_rate}
    min_entry_rate: float = 0.05
    min_capture_rate: float = 0.25

    def render(self) -> str:
        f = lambda x: "n/d" if x is None else f"{x:.0%}"  # noqa: E731
        lines = ["🔥 OPPORTUNITY ENGINE", f"Período: {self.period_hours:.0f} h",
                 f"OPPORTUNITY CAPTURE RATE: {f(self.capture_rate)}  ({self.n_captured}/{self.n_moves} movimentos relevantes capturados com entrada)",
                 f"ENTRY RATE: {f(self.entry_rate)}  ({self.n_entries} entradas em {self.n_analyzed} oportunidades analisadas)"]
        if self.overfilter:
            lines.append(f"⚠️ OVERFILTER — entry rate < {self.min_entry_rate:.0%} ou captura < {self.min_capture_rate:.0%}: identificar a regra que elimina oportunidades lucrativas")
        if self.attribution:
            lines.append("Atribuição por filtro (o que a regra bloqueou e o que teria acontecido com a hipótese 3R):")
            for a in self.attribution:
                hyp = f"E hipotética {a['hyp_expectancy']:+.2f}R (win {a['hyp_win']:.0%}, n={a['hyp_n']})" if a["hyp_n"] else "sem resultado hipotético ainda"
                flag = " ◀ regra cara: bloqueia oportunidades lucrativas" if (a["hyp_n"] >= 10 and a["hyp_expectancy"] > 0.2) else ""
                lines.append(f"  {a['rule']:<15} n={a['n']:<4} {hyp}{flag}")
        if self.threshold_curve:
            lines.append("Curva limiar × entradas × expectancy (região ideal = maior expectancy com volume suficiente):")
            best = max((r for r in self.threshold_curve if r["n"] >= 10), key=lambda r: r["expectancy"], default=None)
            for r in self.threshold_curve:
                mark = " ◀ região ideal" if best is r else ""
                lines.append(f"  Score ≥ {r['threshold']:<3} entradas {r['n']:<5} E={r['expectancy']:+.2f}R  win {r['win_rate']:.0%}{mark}")
        return "\n".join(lines)


def threshold_curve(rows: Sequence[dict], thresholds: Sequence[int] = (20, 30, 40, 50, 60, 70, 80)) -> list[dict]:
    """rows: {"score": |score| na entrada, "r": resultado em R}."""
    out = []
    for t in thresholds:
        sub = [r["r"] for r in rows if abs(r["score"]) >= t and r["r"] is not None]
        if not sub:
            out.append({"threshold": t, "n": 0, "expectancy": 0.0, "win_rate": 0.0})
            continue
        out.append({"threshold": t, "n": len(sub), "expectancy": round(statistics.fmean(sub), 3), "win_rate": sum(1 for x in sub if x > 0) / len(sub)})
    return out


def attribution(decisions: Sequence[DecisionRecord]) -> list[dict]:
    by: dict[str, list[DecisionRecord]] = {}
    for d in decisions:
        by.setdefault(d.action, []).append(d)
    out = []
    for rule, recs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        hyp = [r.hypothetical_r for r in recs if r.hypothetical_r is not None]
        out.append({"rule": rule, "n": len(recs), "hyp_n": len(hyp), "hyp_expectancy": round(statistics.fmean(hyp), 3) if hyp else 0.0,
                    "hyp_win": (sum(1 for x in hyp if x > 0) / len(hyp)) if hyp else 0.0})
    return out


def opportunity_report(decisions: Sequence[DecisionRecord], prices: Sequence[tuple[datetime, float]], entries: Sequence[tuple[datetime, str]],
                       threshold_usd: float, horizon_min: int = 240, trade_rows: Sequence[dict] = (),
                       min_entry_rate: float = 0.05, min_capture_rate: float = 0.25) -> OpportunityReport:
    """decisions: uma por ciclo analisado; prices: (t, close) do período; entries: (t, direção) das entradas;
    trade_rows: {"score", "r"} das operações resolvidas (curva de limiar)."""
    prices = sorted(prices)
    hours = (prices[-1][0] - prices[0][0]).total_seconds() / 3600 if len(prices) > 1 else 0.0
    moves = detect_moves(prices, threshold_usd, horizon_min) if len(prices) > 1 else []
    captured = 0
    for mv in moves:
        if any(d == mv.direction and mv.evident_at - timedelta(minutes=horizon_min) <= t < mv.evident_at for t, d in entries):
            captured += 1
    analyzed = [d for d in decisions if d.action != "SEM_SINAL"] or list(decisions)   # SEM_SINAL_GATE (com vantagem, barrada) conta como analisada
    n_entries = sum(1 for d in decisions if d.action == "ENTRADA")
    entry_rate = (n_entries / len(analyzed)) if analyzed else None
    capture = (captured / len(moves)) if moves else None
    overfilter = (entry_rate is not None and len(analyzed) >= 20 and entry_rate < min_entry_rate) or (capture is not None and len(moves) >= 10 and capture < min_capture_rate)
    return OpportunityReport(hours, len(moves), captured, capture, len(analyzed), n_entries, entry_rate, overfilter,
                             attribution([d for d in decisions if d.action != "ENTRADA"]), threshold_curve(trade_rows), min_entry_rate, min_capture_rate)


# --------------------------------------------------------------------------- FUNIL DE ENTRADA
# Onde as entradas desaparecem: cada análise recebe a PRIMEIRA etapa em que a oportunidade caiu.
FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("SCORE_MIN", "Score insuficiente (|score| < mínimo de vantagem)"),
    ("PROB_MIN", "Probabilidade insuficiente (< 55 %)"),
    ("CONF_MIN", "Confiança insuficiente (< 50)"),
    ("SCORE_SINAL", "Score abaixo do limiar de sinal (±50)"),
    ("CONFIRMACOES", "Confirmações insuficientes (< 3 famílias)"),
    ("ESTAGIO_3", "Estágio 3 (movimento já ocorreu)"),
    ("ANTI_SPAM", "Anti-spam (sem mudança relevante)"),
    ("INTERVALO_MINIMO", "Intervalo mínimo entre alertas"),
    ("SINAL_NAO_OPERACIONAL", "Sinal não operacional (WATCH/REVERSAL/RISK)"),
    ("CONFIANCA_OPERAR", "Confiança para operar (< 60)"),
    ("EVIDENCIA", "Evidência insuficiente (< nível 2)"),
    ("CONFLITO", "Fatores conflitantes"),
    ("VIABILIDADE", "Alvo inviável (resistência/suporte forte antes de 2R)"),
    ("STOP_LOTE", "Stop inválido / lote zero"),
    ("SPREAD", "Spread"),
    ("KILL_SWITCH", "Kill switch / pausa"),
    ("TRADING_STOP", "Trading stop / drawdown"),
    ("POSICAO_ABERTA", "Posição já aberta no ativo"),
    ("CORRELACAO", "Correlação / exposição de carteira"),
    ("PRIORIDADE", "Prioridade (outro mercado foi melhor)"),
    ("PARAMETRO", "Parâmetro em proteção/suspenso (ciclo de vida)"),
    ("AUTORIZACAO", "Aguardando autorização"),
    ("OUTROS", "Outros filtros"),
)
STAGE_LABEL = dict(FUNNEL_STAGES)


def funnel_stage(a, sig, gate_reason: str, decision: str, cfg, raw_min_score: float = 15.0) -> tuple[bool, Optional[str]]:
    """(é oportunidade bruta?, etapa em que caiu ou None = ENTRADA)."""
    from .models import Direction

    direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
    if direction == Direction.LATERAL or abs(a.score) < raw_min_score:
        return False, None
    d = (decision or "").lower()
    if d.startswith(("🟢 paper open", "🟢 position open")):
        return True, None
    if not a.has_edge:
        if abs(a.score) < cfg.min_edge_score:
            return True, "SCORE_MIN"
        if max(a.prob_up, a.prob_down) < cfg.min_edge_probability:
            return True, "PROB_MIN"
        return True, "CONF_MIN"
    if sig is None:
        return True, gate_reason if gate_reason in STAGE_LABEL else "OUTROS"
    if "não é operacional" in d:
        return True, "SINAL_NAO_OPERACIONAL"
    if "aguardando autoriza" in d:
        return True, "AUTORIZACAO"
    if "confiança" in d:
        return True, "CONFIANCA_OPERAR"
    if "evidência" in d:
        return True, "EVIDENCIA"
    if "conflitantes" in d:
        return True, "CONFLITO"
    if "perseguir" in d:
        return True, "ESTAGIO_3"
    if "espaço estatístico" in d or "viabil" in d:
        return True, "VIABILIDADE"
    if "spread" in d:
        return True, "SPREAD"
    if "kill switch" in d or "trading_enabled" in d or "/stop" in d or "pausado" in d:
        return True, "KILL_SWITCH"
    if "trading stop" in d or "drawdown" in d:
        return True, "TRADING_STOP"
    if "exposição de carteira" in d or "correlacionado" in d or "max_portfolio_positions" in d or "max_total_open_risk" in d:
        return True, "CORRELACAO"
    if "posição ativa" in d or "max_asset_exposure" in d:
        return True, "POSICAO_ABERTA"
    if "prioridade" in d:
        return True, "PRIORIDADE"
    if "ciclo de vida" in d or "parâmetro" in d:
        return True, "PARAMETRO"
    if "lote" in d or "stop" in d:
        return True, "STOP_LOTE"
    if "analisado" in d:
        return True, "OUTROS"
    return True, "OUTROS"


@dataclass
class Funnel:
    analyses: int = 0
    raw: int = 0
    entries: int = 0
    drops: dict[str, int] = field(default_factory=dict)

    def add(self, is_raw: bool, stage: Optional[str]) -> None:
        self.analyses += 1
        if not is_raw:
            return
        self.raw += 1
        if stage is None:
            self.entries += 1
        else:
            self.drops[stage] = self.drops.get(stage, 0) + 1

    def merge(self, other: "Funnel") -> "Funnel":
        f = Funnel(self.analyses + other.analyses, self.raw + other.raw, self.entries + other.entries, dict(self.drops))
        for k, v in other.drops.items():
            f.drops[k] = f.drops.get(k, 0) + v
        return f

    @property
    def qualified(self) -> int:
        """Passaram por todas as regras do motor (vantagem + sinal + regras de operação); só faltou carteira/prioridade/autorização."""
        portfolio = ("KILL_SWITCH", "TRADING_STOP", "POSICAO_ABERTA", "CORRELACAO", "PRIORIDADE", "AUTORIZACAO", "PARAMETRO")
        return self.entries + sum(v for k, v in self.drops.items() if k in portfolio)

    def render(self, title: str = "FUNIL DE ENTRADA") -> str:
        w = 46
        lines = [f"🔻 {title}", f"{'ANÁLISES H1:':<{w}}{self.analyses:>7,}", f"{'OPORTUNIDADES BRUTAS (|score| ≥ 15):':<{w}}{self.raw:>7,}", ""]
        for key, label in FUNNEL_STAGES:
            n = self.drops.get(key, 0)
            if n:
                pct = f"{n / self.raw:>5.0%}" if self.raw else ""
                lines.append(f"  {label:<{w - 2}}{n:>7,}  {pct}")
        lines += ["", f"{'OPORTUNIDADES QUALIFICADAS:':<{w}}{self.qualified:>7,}", f"{'ENTRADAS:':<{w}}{self.entries:>7,}"]
        if self.raw:
            top = max(self.drops.items(), key=lambda kv: kv[1], default=None)
            if top and top[1] / self.raw >= 0.3:
                lines.append(f"→ maior perda: {STAGE_LABEL[top[0]]} ({top[1] / self.raw:.0%} das oportunidades brutas)")
        return "\n".join(lines)
