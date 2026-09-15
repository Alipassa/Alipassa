"""GOLD AI ENGINE 2.2 — TRADE SIMULATOR · STOP ENGINE · MAX PROFIT ENGINE · GESTÃO DE RISCO.

PRE-MOVE → DIREÇÃO → ENTRADA → STOP → 1R → 2R → 3R → TRAILING → RESULTADO

Pergunta central: quando o motor dá um sinal, quantas vezes o preço atinge 1R, 2R, 3R
antes do stop? E qual estratégia de saída tem a melhor expectativa em R?

Hipótese inicial: RISCO = 1R, ALVO = 3R. O simulador comprova ou rejeita.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional, Sequence

from .models import Assessment, Candle, Direction, EvidenceLevel, MarketSnapshot, Signal, SignalType


# --------------------------------------------------------------------------- modos e limites
class TradingMode(str, Enum):
    PAPER = "PAPER"          # 🟡 simula tudo, não envia ordem
    AUTHORIZE = "AUTHORIZE"  # 🟠 prepara a ordem e espera autorização
    LIVE = "LIVE"            # 🔴 envia automaticamente ao MT5


@dataclass
class RiskLimits:
    """Definidos pelo usuário. A IA pode ter 87 % de confiança, mas NÃO pode aumentar o risco."""

    risk_per_trade_pct: float = 0.5   # % do capital arriscado por operação (= 1R)
    max_daily_loss_pct: float = 2.0
    max_positions: int = 1
    max_lot: float = 0.10
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_spread: float = 0.60          # USD (XAUUSD)
    max_slippage: float = 0.30        # USD
    contract_size: float = 100.0      # onças por lote (XAUUSD padrão)
    min_stop_atr: float = 0.6         # stop nunca mais apertado que isso
    max_stop_atr: float = 2.5         # nem mais largo

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "RiskLimits":
        g = lambda k, d: type(d)(env.get(k, d))  # noqa: E731
        return cls(risk_per_trade_pct=g("RISK_PER_TRADE", 0.5), max_daily_loss_pct=g("MAX_DAILY_LOSS", 2.0),
                   max_positions=g("MAX_POSITIONS", 1), max_lot=g("MAX_LOT", 0.10), max_spread=g("MAX_SPREAD", 0.60),
                   max_slippage=g("MAX_SLIPPAGE", 0.30), contract_size=g("CONTRACT_SIZE", 100.0))


# --------------------------------------------------------------------------- estratégias de saída
@dataclass(frozen=True)
class ExitStrategy:
    name: str
    target_r: Optional[float] = None      # alvo fixo em R (None = sem alvo fixo)
    trail_r: Optional[float] = None       # distância do trailing em R (None = sem trailing)
    trail_activate_r: float = 1.0         # trailing começa após atingir este R
    partial_r: Optional[float] = None     # realiza parcial neste R…
    partial_fraction: float = 0.5         # …desta fração, e move o stop para o zero a zero


STRATEGIES: tuple[ExitStrategy, ...] = (
    ExitStrategy("1R", target_r=1.0),
    ExitStrategy("2R", target_r=2.0),
    ExitStrategy("3R", target_r=3.0),
    ExitStrategy("4R", target_r=4.0),
    ExitStrategy("trailing", trail_r=1.0, trail_activate_r=1.0),
    ExitStrategy("2R+trailing", target_r=None, partial_r=2.0, partial_fraction=0.5, trail_r=1.0, trail_activate_r=2.0),
)
DEFAULT_STRATEGY = "3R"  # hipótese inicial: risco 1R, alvo 3R


# --------------------------------------------------------------------------- plano
@dataclass
class TradePlan:
    direction: Direction
    entry: float
    stop: float
    atr: float
    time: datetime
    targets: dict[str, float] = field(default_factory=dict)             # "1R".."4R" → preço
    structural_target: Optional[float] = None
    volatility_target: Optional[float] = None
    statistical_target_r: Optional[float] = None
    recommended: str = DEFAULT_STRATEGY
    recommended_reason: str = "hipótese inicial (risco 1R, alvo 3R) — ainda sem histórico"
    probabilities: dict[str, Optional[float]] = field(default_factory=dict)  # "1R" → prob. de atingir antes do stop
    lots: Optional[float] = None
    risk_usd: Optional[float] = None
    signal_type: str = ""
    evidence_level: int = 0
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    viable: bool = True                   # False = resistência/suporte forte antes do alvo mínimo
    structure_rr: Optional[float] = None

    @property
    def r_value(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def sign(self) -> float:
        return 1.0 if self.direction == Direction.ALTA else -1.0

    def price_at_r(self, r: float) -> float:
        return self.entry + self.sign * r * self.r_value

    def render(self) -> str:
        side = "COMPRA" if self.direction == Direction.ALTA else "VENDA"
        lines = [f"🧾 PLANO DE OPERAÇÃO — {side} XAU/USD", f"Entrada = {self.entry:.2f}", f"Stop = {self.stop:.2f}  (1R = {self.r_value:.2f} USD)", ""]
        for k in ("1R", "2R", "3R", "4R"):
            p = self.probabilities.get(k)
            lines.append(f"{k} = {self.targets[k]:.2f}" + (f"   prob. {p:.0%}" if p is not None else "   prob. n/d"))
        lines += ["", f"Alvo estrutural: {self.structural_target:.2f}" if self.structural_target else "Alvo estrutural: n/d",
                  f"Alvo de volatilidade: {self.volatility_target:.2f}" if self.volatility_target else "Alvo de volatilidade: n/d",
                  f"Alvo estatístico: {self.statistical_target_r:.1f}R" if self.statistical_target_r else "Alvo estatístico: n/d",
                  "", f"TP ótimo = {self.recommended}  ({self.recommended_reason})",
                  f"R:R = 1:{self.recommended[0] if self.recommended[:1].isdigit() else '3'}"]
        if self.lots is not None:
            lines.append(f"Lote = {self.lots:.2f}  (risco {self.risk_usd:.2f} USD)")
        lines += [f"  • {n}" for n in self.notes]
        return "\n".join(lines)


class StopEngine:
    """Stop = invalidação estrutural (suporte/resistência da zona), limitado a [min, max] ATR."""

    def __init__(self, limits: Optional[RiskLimits] = None) -> None:
        self.limits = limits or RiskLimits()

    def candidates(self, a: Assessment, direction: Direction, atr: float, s: Optional[MarketSnapshot] = None) -> list[tuple[float, str]]:
        """STOP INTELIGENTE: estrutura (invalidação/suporte/resistência), candle anterior, VWAP e banda de ATR."""
        entry = a.price
        sign = 1.0 if direction == Direction.ALTA else -1.0
        out: list[tuple[float, str]] = []
        inval = a.zone.get("invalidation")
        if inval is not None and sign * (entry - inval) > 0:
            out.append((inval, "invalidação estrutural"))
        lvl = a.zone.get("support") if direction == Direction.ALTA else a.zone.get("resistance")
        if lvl is not None and sign * (entry - lvl) > 0:
            out.append((lvl - sign * 0.15 * atr, "suporte/resistência + margem"))
        if s is not None:
            ref = s.candles.get("H1") or s.candles.get("M30") or []
            if len(ref) >= 2:
                prev = ref[-2]
                lvl_c = prev.low if direction == Direction.ALTA else prev.high
                out.append((lvl_c - sign * 0.1 * atr, "candle anterior (H1)"))
        for r in a.technical:
            if r.timeframe == "H1" and r.vwap_position is not None and r.atr:
                vwap = entry - r.vwap_position * r.atr
                band = vwap - sign * 1.0 * r.atr
                if sign * (entry - band) > 0:
                    out.append((band, "banda VWAP −1 ATR"))
        out.append((entry - sign * 1.2 * atr, "1.2 ATR (volatilidade)"))
        return out

    def compute(self, a: Assessment, direction: Direction, atr: float, s: Optional[MarketSnapshot] = None) -> tuple[float, str]:
        """Escolhe o candidato estrutural mais próximo da entrada que respeite MIN_STOP_ATR; limita a MAX_STOP_ATR."""
        entry = a.price
        sign = 1.0 if direction == Direction.ALTA else -1.0
        lo, hi = self.limits.min_stop_atr * atr, self.limits.max_stop_atr * atr
        valid = [(lvl, why) for lvl, why in self.candidates(a, direction, atr, s) if abs(entry - lvl) >= lo]
        if not valid:
            return entry - sign * lo, f"todos os níveis muito próximos → stop em {self.limits.min_stop_atr:.1f} ATR"
        lvl, why = min(valid, key=lambda x: abs(entry - x[0]))
        if abs(entry - lvl) > hi:
            return entry - sign * hi, f"{why} muito distante → stop em {self.limits.max_stop_atr:.1f} ATR"
        return lvl, f"stop: {why} ({abs(entry - lvl) / atr:.2f} ATR)"


# --------------------------------------------------------------------------- simulação
@dataclass
class TradeResult:
    strategy: str
    r_multiple: float
    exit_reason: str          # STOP | TARGET | TRAIL | PARTIAL+TRAIL | HORIZON | OPEN
    bars: int
    exit_time: Optional[datetime]


@dataclass
class ExcursionProfile:
    """Trajetória com o stop INICIAL fixo: até onde o preço foi antes de estopar."""

    max_r_before_stop: float
    mae_r: float
    stopped: bool
    horizon_reached: bool
    bars: int

    def hit(self, r: float) -> bool:
        return self.max_r_before_stop >= r


def _fav_adv(plan: TradePlan, c: Candle) -> tuple[float, float]:
    """(excursão favorável, adversa) do candle em R (adversa positiva = contra)."""
    s, R = plan.sign, plan.r_value or 1e-9
    fav = (c.high - plan.entry) / R if s > 0 else (plan.entry - c.low) / R
    adv = (plan.entry - c.low) / R if s > 0 else (c.high - plan.entry) / R
    return fav, adv


def excursion_profile(plan: TradePlan, candles: Sequence[Candle], horizon_min: int) -> ExcursionProfile:
    max_r, mae, bars = 0.0, 0.0, 0
    end = plan.time + timedelta(minutes=horizon_min)
    for c in candles:
        if c.time <= plan.time:
            continue
        if c.time > end:
            return ExcursionProfile(round(max_r, 3), round(mae, 3), False, True, bars)
        bars += 1
        fav, adv = _fav_adv(plan, c)
        mae = max(mae, adv)
        if adv >= 1.0:  # tocou o stop inicial — conservador: não conta o favorável deste candle
            return ExcursionProfile(round(max_r, 3), round(min(mae, 1.0), 3), True, False, bars)
        max_r = max(max_r, fav)
    return ExcursionProfile(round(max_r, 3), round(mae, 3), False, False, bars)


def simulate_trade(plan: TradePlan, candles: Sequence[Candle], strategy: ExitStrategy, horizon_min: int) -> TradeResult:
    """Simula a operação candle a candle. Conservador: se stop e alvo cabem no mesmo candle, vale o stop."""
    s, R = plan.sign, plan.r_value or 1e-9
    stop_r = -1.0            # nível do stop em R (negativo = abaixo da entrada para compra)
    remaining = 1.0
    realized = 0.0
    peak = 0.0
    bars = 0
    end = plan.time + timedelta(minutes=horizon_min)
    partial_done = strategy.partial_r is None
    last_close_r = 0.0
    for c in candles:
        if c.time <= plan.time:
            continue
        if c.time > end:
            return TradeResult(strategy.name, round(realized + remaining * last_close_r, 3), "HORIZON", bars, c.time)
        bars += 1
        fav, adv = _fav_adv(plan, c)
        low_r = -adv  # extremo adverso em R (negativo)
        # 1) stop (inclui trailing/breakeven) — conservador, antes de qualquer alvo
        if low_r <= stop_r:
            reason = "STOP" if stop_r <= -1.0 + 1e-9 else ("PARTIAL+TRAIL" if not partial_done or strategy.partial_r else "TRAIL")
            return TradeResult(strategy.name, round(realized + remaining * stop_r, 3), reason, bars, c.time)
        # 2) parcial
        if not partial_done and strategy.partial_r is not None and fav >= strategy.partial_r:
            realized += remaining * strategy.partial_fraction * strategy.partial_r
            remaining *= 1.0 - strategy.partial_fraction
            stop_r = max(stop_r, 0.0)  # zero a zero
            partial_done = True
        # 3) alvo fixo
        if strategy.target_r is not None and fav >= strategy.target_r:
            return TradeResult(strategy.name, round(realized + remaining * strategy.target_r, 3), "TARGET", bars, c.time)
        # 4) trailing
        peak = max(peak, fav)
        if strategy.trail_r is not None and peak >= strategy.trail_activate_r:
            stop_r = max(stop_r, peak - strategy.trail_r)
        last_close_r = s * (c.close - plan.entry) / R
    return TradeResult(strategy.name, round(realized + remaining * last_close_r, 3), "OPEN", bars, None)


# --------------------------------------------------------------------------- estatísticas em R
@dataclass
class StrategyStats:
    name: str
    n: int
    expectancy_r: float
    win_rate: float
    profit_factor: Optional[float]
    avg_win_r: float
    avg_loss_r: float


@dataclass
class RStats:
    n: int
    dist: dict[str, int]                       # "stop antes de 1R" | "1R" | "2R" | "3R" | "+3R e continuou"
    reach: dict[str, float]                    # "1R" → fração que atingiu antes do stop
    reach_3r_before_stop: Optional[float]
    stop_before_1r: Optional[float]
    strategies: list[StrategyStats]
    best: Optional[str]
    mfe_r_median: Optional[float]
    mae_r_median: Optional[float]
    by_type: dict[str, dict[str, float]] = field(default_factory=dict)

    def render(self) -> str:
        if not self.n:
            return "🎯 TRADE SIMULATOR — sem operações resolvidas"
        lines = ["🎯 TRADE SIMULATOR — resultado em R", f"Operações: {self.n}", "Distribuição (até onde foi antes do stop):"]
        for k, v in self.dist.items():
            lines.append(f"  {k:<20} {v:>4}  {v / self.n:>5.0%}  {'█' * int(round(20 * v / self.n))}")
        lines.append("Atinge antes do stop: " + " · ".join(f"{k} {v:.0%}" for k, v in self.reach.items()))
        lines.append(f"3R antes do stop: {self.reach_3r_before_stop:.0%} · stop antes de 1R: {self.stop_before_1r:.0%}")
        lines.append(f"MFE mediana: {self.mfe_r_median:.2f}R · MAE mediana: {self.mae_r_median:.2f}R")
        lines.append("Estratégias de saída (expectativa em R por operação; win = resultado > 0, inclui saídas no horizonte):")
        for st in sorted(self.strategies, key=lambda x: -x.expectancy_r):
            pf = f"{st.profit_factor:.2f}" if st.profit_factor is not None else "∞"
            mark = " ◀ melhor" if st.name == self.best else ""
            lines.append(f"  {st.name:<12} E={st.expectancy_r:+.2f}R  win {st.win_rate:.0%}  PF {pf}  ganho médio {st.avg_win_r:+.2f}R  perda média {st.avg_loss_r:+.2f}R{mark}")
        for t, d in self.by_type.items():
            lines.append(f"  por sinal {t}: n={int(d['n'])} 3R antes do stop {d['reach_3r']:.0%} E({DEFAULT_STRATEGY})={d['expectancy']:+.2f}R")
        return "\n".join(lines)


def r_stats(rows: Sequence[dict]) -> RStats:
    """rows: {"type", "profile": ExcursionProfile, "results": {strategy_name: r_multiple}}."""
    n = len(rows)
    if not n:
        return RStats(0, {}, {}, None, None, [], None, None, None)
    dist = {"stop antes de 1R": 0, "expirou < 1R": 0, "1R": 0, "2R": 0, "3R": 0, "+3R e continuou": 0}
    for r in rows:
        m, pr = r["profile"].max_r_before_stop, r["profile"]
        key = ("stop antes de 1R" if pr.stopped else "expirou < 1R") if m < 1 else "1R" if m < 2 else "2R" if m < 3 else "3R" if m < 4 else "+3R e continuou"
        dist[key] += 1
    reach = {f"{k}R": sum(1 for r in rows if r["profile"].hit(k)) / n for k in (1, 2, 3, 4)}
    strategies: list[StrategyStats] = []
    names = sorted({k for r in rows for k in r["results"]})
    for name in names:
        rs = [r["results"][name] for r in rows if name in r["results"]]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        gross_w, gross_l = sum(wins), -sum(losses)
        strategies.append(StrategyStats(name, len(rs), round(statistics.fmean(rs), 3), len(wins) / len(rs),
                                        round(gross_w / gross_l, 2) if gross_l else None,
                                        round(statistics.fmean(wins), 2) if wins else 0.0, round(statistics.fmean(losses), 2) if losses else 0.0))
    best = max(strategies, key=lambda s: s.expectancy_r).name if strategies else None
    by_type: dict[str, dict[str, float]] = {}
    for t in sorted({r["type"] for r in rows}):
        sub = [r for r in rows if r["type"] == t]
        e = [r["results"].get(DEFAULT_STRATEGY, 0.0) for r in sub]
        by_type[t] = {"n": len(sub), "reach_3r": sum(1 for r in sub if r["profile"].hit(3)) / len(sub), "expectancy": statistics.fmean(e) if e else 0.0}
    return RStats(n, dist, reach, reach["3R"], dist["stop antes de 1R"] / n, strategies, best,
                  statistics.median(r["profile"].max_r_before_stop for r in rows), statistics.median(r["profile"].mae_r for r in rows), by_type)


# --------------------------------------------------------------------------- MAX PROFIT ENGINE
class MaxProfitEngine:
    """Não tenta prever o máximo absoluto. Calcula ALVO ESTATÍSTICO (histórico em R), ALVO
    ESTRUTURAL (próximo S/R), ALVO DE VOLATILIDADE (ATR × horizonte) e ALVO POR RISCO/RETORNO,
    estima a probabilidade de cada R e recomenda o TP com melhor expectativa histórica."""

    def __init__(self, stop_engine: Optional[StopEngine] = None, history: Optional[RStats] = None, horizon_min: int = 240,
                 min_rr_to_structure: float = 2.0) -> None:
        self.stop_engine = stop_engine or StopEngine()
        self.history = history
        self.horizon_min = horizon_min
        self.min_rr_to_structure = min_rr_to_structure

    def plan(self, a: Assessment, s: MarketSnapshot, direction: Direction, signal_type: str = "") -> TradePlan:
        atr = s.atr or (a.price * 0.003)
        stop, why = self.stop_engine.compute(a, direction, atr, s)
        plan = TradePlan(direction, a.price, stop, atr, a.time, signal_type=signal_type, evidence_level=int(a.evidence_level), confidence=a.confidence)
        plan.notes.append(why)
        for k in (1, 2, 3, 4):
            plan.targets[f"{k}R"] = round(plan.price_at_r(k), 2)
        # estrutural: próximo nível além da entrada
        lvl = a.zone.get("resistance") if direction == Direction.ALTA else a.zone.get("support")
        if lvl is not None and plan.sign * (lvl - a.price) > 0:
            plan.structural_target = lvl
            rr = plan.sign * (lvl - a.price) / (plan.r_value or 1)
            plan.structure_rr = round(rr, 2)
            plan.notes.append(f"alvo estrutural a {rr:.1f}R")
        # viabilidade do alvo: nível FORTE (H4/D1) antes do R:R mínimo → sem espaço estatístico
        strong = [(r.resistance if direction == Direction.ALTA else r.support) for r in a.technical if r.timeframe in ("H4", "D1")]
        # níveis a menos de 0.5 ATR já estão sendo testados/rompidos — não contam como barreira
        strong = [x for x in strong if x is not None and plan.sign * (x - a.price) > 0.5 * atr]
        if strong:
            near = min(strong, key=lambda x: abs(x - a.price))
            rr_strong = plan.sign * (near - a.price) / (plan.r_value or 1)
            if rr_strong < self.min_rr_to_structure:
                plan.viable = False
                plan.notes.append(f"⚠️ resistência/suporte forte (H4/D1) em {near:.2f} = {rr_strong:.1f}R, antes de {self.min_rr_to_structure:.0f}R → sem espaço estatístico para o alvo")
        # volatilidade: ATR H1 × sqrt(horas do horizonte)
        plan.volatility_target = round(a.price + plan.sign * atr * (self.horizon_min / 60) ** 0.5, 2)
        # estatístico + probabilidades por R (histórico)
        h = self.history
        if h and h.n >= 20:
            plan.statistical_target_r = h.mfe_r_median
            plan.probabilities = {k: h.reach.get(k) for k in ("1R", "2R", "3R", "4R")}
            if h.best:
                plan.recommended = h.best
                best = next(x for x in h.strategies if x.name == h.best)
                plan.recommended_reason = f"melhor expectativa histórica ({best.expectancy_r:+.2f}R em {best.n} operações)"
        else:
            plan.probabilities = {k: None for k in ("1R", "2R", "3R", "4R")}
            plan.notes.append("histórico insuficiente (< 20 operações) — mantida a hipótese 3R")
        return plan


# --------------------------------------------------------------------------- NO TRADE + risco
def no_trade_check(a: Assessment, limits: RiskLimits, spread: Optional[float] = None, min_confidence: float = 60.0,
                   min_level: int = 2, max_spread: Optional[float] = None) -> list[str]:
    """Fatores conflitantes, confiança baixa ou spread alto → 🟡 NÃO OPERAR. Lista vazia = pode operar."""
    reasons: list[str] = []
    if not a.has_edge:
        reasons.append("sem vantagem estatística")
    if a.confidence < min_confidence:
        reasons.append(f"confiança {a.confidence:.0f} < {min_confidence:.0f}")
    if int(a.evidence_level) < min_level:
        reasons.append(f"evidência nível {int(a.evidence_level)} < {min_level}")
    direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
    sign = 1.0 if direction == Direction.ALTA else -1.0
    against = [f.name for f in a.factors if f.available and sign * f.ratio <= -0.3]
    if len(against) >= 2:
        reasons.append(f"fatores conflitantes contra a direção: {', '.join(against)}")
    if a.premove.stage.value == "MOVIMENTO":
        reasons.append("movimento já ocorreu (não perseguir)")
    lim_spread = max_spread if max_spread is not None else limits.max_spread
    if spread is not None and spread > lim_spread:
        reasons.append(f"spread {spread:g} > máximo do ativo {lim_spread:g}")
    return reasons


@dataclass
class RiskManager:
    limits: RiskLimits
    equity: float
    day: Optional[str] = None
    daily_pnl: float = 0.0
    open_positions: int = 0

    def _roll_day(self, t: datetime) -> None:
        d = t.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.daily_pnl = d, 0.0

    def size(self, plan: TradePlan) -> TradePlan:
        """Lote a partir do risco fixo por operação — nunca da confiança."""
        risk_usd = self.equity * self.limits.risk_per_trade_pct / 100.0
        per_lot = plan.r_value * self.limits.contract_size
        lots = risk_usd / per_lot if per_lot > 0 else 0.0
        lots = min(self.limits.max_lot, max(0.0, lots))
        lots = round(int(lots / self.limits.lot_step) * self.limits.lot_step, 2)
        if lots < self.limits.min_lot:
            plan.notes.append(f"risco de {risk_usd:.2f} USD não comporta o lote mínimo com stop de {plan.r_value:.2f} USD")
            lots = 0.0
        plan.lots, plan.risk_usd = lots, round(lots * per_lot, 2)
        return plan

    def check(self, plan: TradePlan) -> list[str]:
        self._roll_day(plan.time)
        reasons: list[str] = []
        if self.open_positions >= self.limits.max_positions:
            reasons.append(f"posições abertas {self.open_positions} ≥ máximo {self.limits.max_positions}")
        if self.daily_pnl <= -self.equity * self.limits.max_daily_loss_pct / 100.0:
            reasons.append(f"perda diária {self.daily_pnl:.2f} USD atingiu o limite de {self.limits.max_daily_loss_pct:.1f}%")
        if not plan.lots:
            reasons.append("lote zero")
        return reasons

    def register(self, plan: TradePlan) -> None:
        self.open_positions += 1

    def close(self, plan: TradePlan, r_multiple: float, t: datetime) -> None:
        self._roll_day(t)
        self.open_positions = max(0, self.open_positions - 1)
        self.daily_pnl += r_multiple * (plan.risk_usd or 0.0)


# --------------------------------------------------------------------------- gestor de posição
@dataclass
class Decision:
    action: str                 # NO_TRADE | PAPER | AWAIT_AUTHORIZATION | SENT | BLOCKED
    plan: Optional[TradePlan]
    reasons: list[str] = field(default_factory=list)
    mode: TradingMode = TradingMode.PAPER

    def render(self) -> str:
        head = {"NO_TRADE": "🟡 NÃO OPERAR", "PAPER": "🟡 PAPER — operação simulada registrada", "AWAIT_AUTHORIZATION": "🟠 AGUARDANDO AUTORIZAÇÃO",
                "SENT": "🔴 LIVE — ordem enviada ao MT5", "BLOCKED": "⛔ BLOQUEADA pelo gestor de risco"}[self.action]
        out = [head] + [f"  • {r}" for r in self.reasons]
        if self.plan is not None and self.action != "NO_TRADE":
            out.append(self.plan.render())
        return "\n".join(out)


class PositionManager:
    def __init__(self, mode: TradingMode, limits: RiskLimits, equity: float, history: Optional[RStats] = None,
                 horizon_min: int = 240, executor=None) -> None:
        self.mode = mode
        self.risk = RiskManager(limits, equity)
        self.mpe = MaxProfitEngine(StopEngine(limits), history, horizon_min)
        self.executor = executor  # data.mt5.MT5Executor (apenas em LIVE / AUTHORIZE)
        self.authorized: bool = False

    EXECUTABLE = {SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL, SignalType.PRE_MOVE}

    def decide(self, sig: Signal, s: MarketSnapshot, spread: Optional[float] = None) -> Decision:
        a = sig.assessment
        if sig.type not in self.EXECUTABLE or sig.direction == Direction.LATERAL:
            return Decision("NO_TRADE", None, [f"sinal {sig.type.value} não é operacional"], self.mode)
        reasons = no_trade_check(a, self.risk.limits, spread)
        if reasons:
            return Decision("NO_TRADE", None, reasons, self.mode)
        plan = self.mpe.plan(a, s, sig.direction, sig.type.value)
        if not plan.viable:
            return Decision("NO_TRADE", plan, [n for n in plan.notes if n.startswith("⚠️")], self.mode)
        self.risk.size(plan)
        blocked = self.risk.check(plan)
        if blocked:
            return Decision("BLOCKED", plan, blocked, self.mode)
        if self.mode == TradingMode.PAPER:
            self.risk.register(plan)
            return Decision("PAPER", plan, [], self.mode)
        if self.mode == TradingMode.AUTHORIZE and not self.authorized:
            return Decision("AWAIT_AUTHORIZATION", plan, ["responda com autorização para enviar"], self.mode)
        if self.executor is None:
            return Decision("BLOCKED", plan, ["sem executor MT5 configurado"], self.mode)
        order = self.executor.send_plan(plan, authorize=True)
        self.risk.register(plan)
        return Decision("SENT", plan, [str(order)], self.mode)


def simulate_all(plan: TradePlan, candles: Sequence[Candle], horizon_min: int, strategies: Iterable[ExitStrategy] = STRATEGIES) -> dict:
    prof = excursion_profile(plan, candles, horizon_min)
    results = {st.name: simulate_trade(plan, candles, st, horizon_min) for st in strategies}
    return {"profile": prof, "results": {k: v.r_multiple for k, v in results.items()}, "details": results}
