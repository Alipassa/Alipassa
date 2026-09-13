"""MARKET AI ENGINE 4.0 — ESTIMATIVA DE LUCRO sobre um período histórico.

Pergunta: "se o sistema tivesse operado de <início> até <fim>, com este capital e este risco por operação,
quanto teria ganho ou perdido — e com que incerteza?"

Método honesto:
  1. walk-forward FORA DA AMOSTRA (parâmetros escolhidos só no treino) → lista cronológica de operações em R;
  2. custo de execução: spread típico do mercado descontado em R de cada operação;
  3. curva de capital sequencial com risco fixo (RISK_PER_TRADE % do capital corrente — composto);
  4. bootstrap (reamostragem das operações) → percentis 5/50/95 do retorno e do drawdown máximo;
  5. ressalvas explícitas: o histórico H1 gratuito não tem notícias, COT intraday nem FRED intraday,
     logo a cobertura de fatores é menor que no `live`; estimativa ≠ garantia.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from .markets import get_market
from .selector import statistical_confidence


@dataclass
class TradeR:
    time: datetime
    symbol: str
    r: float            # resultado líquido em R (já com custo)
    cost_r: float
    strategy: str


@dataclass
class EquityPath:
    start: float
    end: float
    max_drawdown_pct: float
    curve: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def return_pct(self) -> float:
        return (self.end / self.start - 1.0) * 100.0 if self.start else 0.0


def simulate_equity(trades: Sequence[TradeR], equity: float, risk_pct: float, compound: bool = True) -> EquityPath:
    eq, peak, mdd = equity, equity, 0.0
    curve = []
    base = equity
    for t in sorted(trades, key=lambda x: x.time):
        risk = (eq if compound else base) * risk_pct / 100.0
        eq = round(eq + t.r * risk, 2)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0 if peak else 0.0)
        curve.append((t.time, eq))
    return EquityPath(equity, eq, round(mdd, 2), curve)


def bootstrap(trades: Sequence[TradeR], equity: float, risk_pct: float, n: int = 1000, seed: int = 7) -> dict:
    if not trades:
        return {}
    rnd = random.Random(seed)
    rets, dds = [], []
    for _ in range(n):
        sample = [rnd.choice(trades) for _ in trades]
        # preserva a ordem temporal original para o cálculo do drawdown
        sample = [TradeR(t.time, s.symbol, s.r, s.cost_r, s.strategy) for t, s in zip(sorted(trades, key=lambda x: x.time), sample)]
        p = simulate_equity(sample, equity, risk_pct)
        rets.append(p.return_pct); dds.append(p.max_drawdown_pct)
    rets.sort(); dds.sort()
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"ret_p5": q(rets, 0.05), "ret_p50": q(rets, 0.50), "ret_p95": q(rets, 0.95), "dd_p50": q(dds, 0.50), "dd_p95": q(dds, 0.95),
            "prob_profit": sum(1 for r in rets if r > 0) / len(rets)}


@dataclass
class MarketEstimate:
    symbol: str
    period: str
    n_trades: int
    expectancy_gross_r: float
    expectancy_net_r: float
    avg_cost_r: float
    win_rate: float
    strategy: str
    path: EquityPath
    boot: dict
    confidence: object
    trades: list[TradeR]

    def render(self, equity: float) -> str:
        c = self.confidence
        lines = [f"{self.symbol} · {self.period} · estratégia {self.strategy}",
                 f"  operações OOS: {self.n_trades} · win {self.win_rate:.0%} · E bruta {self.expectancy_gross_r:+.2f}R · custo médio {self.avg_cost_r:.2f}R · E líquida {self.expectancy_net_r:+.2f}R (ajustada {c.shrunk:+.2f}R, conf. {c.level})",
                 f"  capital {equity:,.0f} → {self.path.end:,.2f} USD ({self.path.return_pct:+.1f}%) · drawdown máx {self.path.max_drawdown_pct:.1f}%"]
        if self.boot:
            b = self.boot
            lines.append(f"  bootstrap: retorno p5 {b['ret_p5']:+.1f}% · p50 {b['ret_p50']:+.1f}% · p95 {b['ret_p95']:+.1f}% · P(lucro) {b['prob_profit']:.0%} · DD p95 {b['dd_p95']:.1f}%")
        return "\n".join(lines)


def estimate_market(symbol: str, trade_rows: Sequence[dict], equity: float, risk_pct: float, period: str, strategy: str = "adaptive") -> MarketEstimate:
    spec = get_market(symbol)
    trades: list[TradeR] = []
    for row in trade_rows:
        r = row["results"].get(strategy, row["results"].get("3R"))
        if r is None:
            continue
        rv = row.get("r_value") or 0.0
        cost = round(spec.typical_spread / rv, 3) if rv > 0 else 0.0
        trades.append(TradeR(row["time"], symbol, round(r - cost, 3), cost, strategy))
    rs = [t.r for t in trades]
    gross = [t.r + t.cost_r for t in trades]
    path = simulate_equity(trades, equity, risk_pct)
    return MarketEstimate(symbol, period, len(trades), statistics.fmean(gross) if gross else 0.0, statistics.fmean(rs) if rs else 0.0,
                          statistics.fmean(t.cost_r for t in trades) if trades else 0.0, (sum(1 for x in rs if x > 0) / len(rs)) if rs else 0.0,
                          strategy, path, bootstrap(trades, equity, risk_pct), statistical_confidence(rs), trades)


@dataclass
class ProfitEstimate:
    start: str
    end: str
    equity: float
    risk_pct: float
    markets: list[MarketEstimate]
    portfolio: Optional[EquityPath] = None
    portfolio_boot: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"💰 ESTIMATIVA DE LUCRO — {self.start} → {self.end} · capital {self.equity:,.0f} USD · risco {self.risk_pct}%/operação (composto)",
                 "Método: walk-forward fora da amostra · custo de spread em R · curva de capital sequencial · bootstrap 1000×", ""]
        for m in sorted(self.markets, key=lambda m: -m.confidence.shrunk):
            lines += [m.render(self.equity), ""]
        if self.portfolio is not None:
            b = self.portfolio_boot
            lines.append(f"CARTEIRA (todos os mercados em sequência, capital único): {self.equity:,.0f} → {self.portfolio.end:,.2f} USD "
                         f"({self.portfolio.return_pct:+.1f}%) · drawdown máx {self.portfolio.max_drawdown_pct:.1f}%")
            if b:
                lines.append(f"  bootstrap: p5 {b['ret_p5']:+.1f}% · p50 {b['ret_p50']:+.1f}% · p95 {b['ret_p95']:+.1f}% · P(lucro) {b['prob_profit']:.0%} · DD p95 {b['dd_p95']:.1f}%")
        lines += ["", "⚠️ RESSALVAS"] + [f"  • {c}" for c in self.caveats]
        return "\n".join(lines)


DEFAULT_CAVEATS = [
    "Estimativa histórica fora da amostra, não garantia: o mercado de jan→hoje não se repete.",
    "Histórico H1 gratuito (Yahoo) não inclui notícias, COT semanal alinhado nem FRED intraday: a cobertura de fatores é menor que no `live`, "
    "logo o motor opera com menos evidência do que operaria em tempo real.",
    "Execução simulada a fechamento de candle H1 com regra conservadora de stop; slippage real, gaps e horários sem liquidez não estão modelados além do spread típico.",
    "A carteira soma as operações de todos os mercados em sequência com capital único; a exposição correlacionada do live pode ter bloqueado parte delas.",
    "Amostra < 30 operações por mercado = ⚪ inconclusivo; leia a confiança estatística antes do retorno.",
]


def estimate_profit(frames: dict, start: datetime, end: datetime, equity: float, risk_pct: float, n_folds: int = 4, step: int = 1,
                    warmup: int = 220, horizon_min: int = 240, strategy: str = "adaptive", cfg_factory=None) -> ProfitEstimate:
    from .config import EngineConfig
    from .evaluation import Backtester, walk_forward

    period = f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"
    markets: list[MarketEstimate] = []
    for symbol, frame in frames.items():
        spec = get_market(symbol)
        cfg = cfg_factory(symbol) if cfg_factory else EngineConfig(factor_signs=dict(spec.factor_signs), symbol=symbol)
        bt = Backtester(frame, cfg, warmup=warmup, step=step, horizon_min=horizon_min)
        wf = walk_forward(bt, n_folds=n_folds)
        rows = [r for _, res in wf.folds for r in res.trade_rows]
        markets.append(estimate_market(symbol, rows, equity, risk_pct, period, strategy))
    all_trades = [t for m in markets for t in m.trades]
    portfolio = simulate_equity(all_trades, equity, risk_pct) if all_trades else None
    return ProfitEstimate(f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", equity, risk_pct, markets, portfolio,
                          bootstrap(all_trades, equity, risk_pct) if all_trades else {}, list(DEFAULT_CAVEATS))
