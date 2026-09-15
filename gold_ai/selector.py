"""MARKET AI ENGINE 4.0 — ASSET SELECTOR · MARKET OPPORTUNITY SCORE · OPPORTUNITY DECAY · PORTFOLIO EXPOSURE.

Regra: o Asset Selector NÃO cria entradas. Ele só ordena oportunidades que o Prediction/Opportunity
Engine já produziu (sinal operacional + vantagem estatística) e o Risk Engine ainda valida depois.

MARKET OPPORTUNITY SCORE (pesos iniciais, a serem testados pelo Validation Engine):
    25% expectancy histórica (encolhida pela confiança estatística)
    20% probabilidade calibrada
    15% score atual
    15% qualidade do pré-movimento
    10% compatibilidade de regime
     5% captura histórica de oportunidades
     5% liquidez / custo de execução
     5% qualidade dos dados
Três dimensões separadas: QUALIDADE HISTÓRICA · OPORTUNIDADE ATUAL · DECAY (ainda existe vantagem AGORA?).
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .markets import MarketSpec, correlation
from .models import Assessment, Direction, MarketSnapshot, Signal, Stage

WEIGHTS = {"expectancy": 0.25, "probability": 0.20, "score": 0.15, "premove": 0.15, "regime": 0.10, "capture": 0.05, "execution": 0.05, "data": 0.05}


# --------------------------------------------------------------------------- confiança estatística
@dataclass
class StatConfidence:
    n: int
    expectancy: float
    std: float
    level: str            # HIGH | MEDIUM | LOW | NONE
    lower_bound: float    # limite inferior ~90% da expectancy
    shrunk: float         # expectancy encolhida para 0 conforme a amostra

    def render(self) -> str:
        return f"E={self.expectancy:+.2f}R n={self.n} conf={self.level} (LB {self.lower_bound:+.2f}R, ajustada {self.shrunk:+.2f}R)"


def statistical_confidence(results_r: Sequence[float], k: int = 30) -> StatConfidence:
    """Encolhimento bayesiano simples E×n/(n+k) e limite inferior E − 1.28·σ/√n. HIGH exige n≥100 e LB>0."""
    n = len(results_r)
    if n == 0:
        return StatConfidence(0, 0.0, 0.0, "NONE", 0.0, 0.0)
    e = statistics.fmean(results_r)
    sd = statistics.pstdev(results_r) if n > 1 else 1.0
    lb = e - 1.28 * sd / math.sqrt(n)
    shrunk = e * n / (n + k)
    level = "HIGH" if (n >= 100 and lb > 0) else "MEDIUM" if (n >= 30 and lb > -0.05) else "LOW"
    return StatConfidence(n, round(e, 3), round(sd, 3), level, round(lb, 3), round(shrunk, 3))


# --------------------------------------------------------------------------- decay
def opportunity_decay(a: Assessment, first_seen: Optional[datetime], now: datetime, half_life_min: float = 45.0) -> tuple[float, str]:
    """0..1 — quanto da vantagem ainda existe para entrar AGORA: estágio × preço já percorrido × idade do sinal."""
    stage = {Stage.PRE_MOVIMENTO: 1.0, Stage.CONFIRMACAO: 0.85, Stage.NEUTRO: 0.6, Stage.MOVIMENTO: 0.2}[a.premove.stage]
    moved = max(0.0, 1.0 - abs(a.premove.move_in_atr) / 2.0)
    age_min = (now - first_seen).total_seconds() / 60 if first_seen else 0.0
    age = 0.5 ** (age_min / half_life_min)
    decay = round(stage * moved * (0.4 + 0.6 * age), 3)
    why = f"estágio {a.premove.stage.value} · {abs(a.premove.move_in_atr):.1f} ATR percorridos · idade {age_min:.0f} min"
    return decay, why


# --------------------------------------------------------------------------- candidato
@dataclass
class Candidate:
    spec: MarketSpec
    assessment: Assessment
    signal: Signal
    snapshot: MarketSnapshot
    history: StatConfidence
    capture_rate: Optional[float] = None
    data_quality: float = 1.0          # fração de fatores disponíveis
    first_seen: Optional[datetime] = None
    components: dict[str, float] = field(default_factory=dict)
    decay: float = 1.0
    decay_note: str = ""
    opportunity_score: float = 0.0
    status: str = "🟠"

    @property
    def direction(self) -> Direction:
        return self.signal.direction

    def render_row(self) -> str:
        a = self.assessment
        return (f"{self.spec.symbol:<7} {self.status} OPP {self.opportunity_score:>5.1f} · hist {self.history.shrunk:+.2f}R ({self.history.level}, n={self.history.n}) · "
                f"agora score {a.score:+.0f} prob {max(a.prob_up, a.prob_down):.0%} {a.premove.stage.value} · decay {self.decay:.2f} · "
                f"{'BUY' if self.direction == Direction.ALTA else 'SELL'}")


def regime_compatibility(a: Assessment, direction: Direction) -> float:
    r = a.regime
    if direction == Direction.ALTA:
        return 1.0 if r.startswith("BULLISH") else 0.6 if r == "RANGE" else 0.3 if r == "VOLATILE" else 0.2
    return 1.0 if r.startswith("BEARISH") else 0.6 if r == "RANGE" else 0.3 if r == "VOLATILE" else 0.2


def premove_quality(a: Assessment) -> float:
    base = {Stage.PRE_MOVIMENTO: 1.0, Stage.CONFIRMACAO: 0.8, Stage.NEUTRO: 0.4, Stage.MOVIMENTO: 0.1}[a.premove.stage]
    return base * (0.5 + 0.5 * a.premove.probability) * (0.6 + 0.4 * int(a.evidence_level) / 4)


def execution_quality(spec: MarketSpec, snap: MarketSnapshot, spread: Optional[float]) -> float:
    atr = snap.atr or 0.0
    sp = spread if spread is not None else spec.typical_spread
    if atr <= 0:
        return 0.5
    ratio = sp / atr  # spread como fração do ATR
    q = max(0.0, 1.0 - ratio / 0.15)
    h = snap.time.hour
    lo, hi = spec.session_hours_utc
    in_session = lo <= h < hi if lo < hi else (h >= lo or h < hi)
    return round(q * (1.0 if in_session else 0.6), 3)


class AssetSelector:
    """`reaction_edge` (símbolo → 0..1, de reaction_edge.json): dimensão opcional "o relógio de reação tem edge provado neste
    mercado". Só pesa quando o snapshot marca PRESSÃO LATENTE; fora disso é neutra (0,5). Não cria entradas: só ordena."""

    REACTION_WEIGHT = 0.10

    def __init__(self, weights: Optional[dict[str, float]] = None, reaction_edge: Optional[dict[str, float]] = None) -> None:
        self.weights = weights or dict(WEIGHTS)
        self.reaction_edge = dict(reaction_edge or {})
        self.first_seen: dict[str, tuple[Direction, datetime]] = {}

    def score(self, c: Candidate, now: datetime, spread: Optional[float] = None) -> Candidate:
        a = c.assessment
        key = c.spec.symbol
        seen = self.first_seen.get(key)
        if seen is None or seen[0] != c.direction:
            self.first_seen[key] = (c.direction, now)
            seen = self.first_seen[key]
        c.first_seen = seen[1]
        c.decay, c.decay_note = opportunity_decay(a, c.first_seen, now)
        comp = {
            "expectancy": max(0.0, min(1.0, 0.5 + c.history.shrunk)),          # +0.5R → 1.0 ; 0 → 0.5 ; −0.5R → 0
            "probability": max(0.0, min(1.0, (max(a.prob_up, a.prob_down) - 0.5) * 2)),
            "score": min(1.0, abs(a.score) / 100.0),
            "premove": premove_quality(a),
            "regime": regime_compatibility(a, c.direction),
            "capture": c.capture_rate if c.capture_rate is not None else 0.5,
            "execution": execution_quality(c.spec, c.snapshot, spread),
            "data": c.data_quality,
        }
        raw = sum(self.weights[k] * v for k, v in comp.items()) * 100.0
        if key in self.reaction_edge:
            latent = getattr(c.snapshot, "reaction_status", "") == "PRESSÃO LATENTE"
            comp["reaction"] = self.reaction_edge[key] if latent else 0.5
            raw = raw * (1.0 - self.REACTION_WEIGHT) + comp["reaction"] * self.REACTION_WEIGHT * 100.0
        c.components = {k: round(v, 3) for k, v in comp.items()}
        raw *= getattr(c, "lifecycle_multiplier", 1.0)      # ALERTA (3 perdas seguidas) reduz confiança, não quebra o parâmetro
        c.opportunity_score = round(raw * (0.5 + 0.5 * c.decay), 1)
        c.status = "🟢" if c.opportunity_score >= 60 else "🟡" if c.opportunity_score >= 45 else "🟠"
        return c

    def rank(self, candidates: Sequence[Candidate], now: datetime, spreads: Optional[dict[str, float]] = None) -> list[Candidate]:
        scored = [self.score(c, now, (spreads or {}).get(c.spec.symbol)) for c in candidates]
        return sorted(scored, key=lambda c: -c.opportunity_score)

    def forget(self, symbol: str) -> None:
        self.first_seen.pop(symbol, None)


def render_rank(ranked: Sequence[Candidate], history: Optional[dict[str, StatConfidence]] = None) -> str:
    lines = ["🏆 MARKET OPPORTUNITY RANK", "        HISTÓRICO            AGORA"]
    for c in ranked:
        a = c.assessment
        now_flag = "🟢" if (a.has_edge and c.decay >= 0.5) else "🟡" if a.has_edge else "🔴"
        lines.append(f"{c.spec.symbol:<7} {c.history.shrunk:+.2f}R {c.history.level:<6}  {now_flag} score {a.score:+.0f} prob {max(a.prob_up, a.prob_down):.0%} "
                     f"decay {c.decay:.2f} → OPP {c.opportunity_score:.1f} {c.status}")
    if history:
        rest = [s for s in history if s not in {c.spec.symbol for c in ranked}]
        for s in rest:
            h = history[s]
            lines.append(f"{s:<7} {h.shrunk:+.2f}R {h.level:<6}  🔴 sem oportunidade agora")
    if ranked:
        lines.append(f"MELHOR OPORTUNIDADE: {ranked[0].spec.symbol} ({'BUY' if ranked[0].direction == Direction.ALTA else 'SELL'}) — {ranked[0].decay_note}")
    else:
        lines.append("Nenhuma oportunidade com vantagem estatística neste ciclo.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- exposição de carteira
@dataclass
class OpenExposure:
    symbol: str
    direction: Direction
    risk_usd: float


@dataclass
class PortfolioLimits:
    max_total_open_risk_pct: float = 1.5
    max_correlated_risk_pct: float = 1.0
    max_positions: int = 3
    max_asset_exposure: int = 1
    correlation_threshold: float = 0.5   # acima disto, duas posições são "a mesma aposta"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PortfolioLimits":
        g = lambda k, d: type(d)(env.get(k, d))  # noqa: E731
        return cls(g("MAX_TOTAL_OPEN_RISK", 1.5), g("MAX_CORRELATED_RISK", 1.0), g("MAX_PORTFOLIO_POSITIONS", 3), g("MAX_ASSET_EXPOSURE", 1), g("CORRELATION_THRESHOLD", 0.5))


class PortfolioExposureEngine:
    """"Estou diversificando ou fazendo a mesma aposta três vezes?" Risco agregado e correlacionado."""

    def __init__(self, limits: PortfolioLimits, corr_table: Optional[dict[tuple[str, str], float]] = None) -> None:
        self.limits = limits
        self.corr_table = corr_table

    def correlated_risk(self, symbol: str, direction: Direction, risk_usd: float, open_: Sequence[OpenExposure]) -> float:
        """Risco da nova posição + risco das abertas na mesma aposta (correlação assinada pela direção)."""
        total = risk_usd
        for o in open_:
            rho = correlation(symbol, o.symbol, self.corr_table)
            same = 1.0 if o.direction == direction else -1.0
            signed = rho * same
            if signed > 0:
                total += o.risk_usd * signed
        return round(total, 2)

    def check(self, symbol: str, direction: Direction, risk_usd: float, open_: Sequence[OpenExposure], equity: float) -> list[str]:
        reasons: list[str] = []
        if len(open_) >= self.limits.max_positions:
            reasons.append(f"posições abertas {len(open_)} ≥ MAX_PORTFOLIO_POSITIONS {self.limits.max_positions}")
        if sum(1 for o in open_ if o.symbol == symbol) >= self.limits.max_asset_exposure:
            reasons.append(f"já existe posição em {symbol} (MAX_ASSET_EXPOSURE)")
        total = sum(o.risk_usd for o in open_) + risk_usd
        if total > equity * self.limits.max_total_open_risk_pct / 100.0:
            reasons.append(f"risco total aberto {total / equity:.2%} > MAX_TOTAL_OPEN_RISK {self.limits.max_total_open_risk_pct}%")
        corr = self.correlated_risk(symbol, direction, risk_usd, open_)
        if corr > equity * self.limits.max_correlated_risk_pct / 100.0:
            same = [o.symbol for o in open_ if correlation(symbol, o.symbol, self.corr_table) * (1 if o.direction == direction else -1) >= self.limits.correlation_threshold]
            reasons.append(f"risco correlacionado {corr / equity:.2%} > MAX_CORRELATED_RISK {self.limits.max_correlated_risk_pct}% (mesma aposta: {', '.join(same) or 'parcial'})")
        return reasons

    def render(self, open_: Sequence[OpenExposure], equity: float) -> str:
        if not open_:
            return "📐 EXPOSIÇÃO: nenhuma posição aberta"
        total = sum(o.risk_usd for o in open_)
        lines = [f"📐 EXPOSIÇÃO: {len(open_)} posição(ões) · risco total {total / equity:.2%} do capital"]
        for o in open_:
            lines.append(f"  {o.symbol} {'BUY' if o.direction == Direction.ALTA else 'SELL'} risco {o.risk_usd:.2f} USD")
        return "\n".join(lines)
