"""SWEEP DE PISO — escolha do |score| mínimo de vantagem DENTRO do treino de cada fold (walk-forward).

Duas saídas, com papéis diferentes:
  1. SELEÇÃO IN-TRAIN (o número honesto): para cada fold, todos os pisos são testados no treino; o melhor
     (por objetivo) é aplicado ao teste. O agregado fora da amostra não viu nenhum resultado de teste.
  2. SENSIBILIDADE OOS POR PISO FIXO (descritiva): o mesmo piso em todos os folds de teste. Serve para
     entender a forma da curva — NUNCA para escolher o piso, porque olha o teste.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional, Sequence

from .config import EngineConfig
from .estimate import TradeR, simulate_equity
from .evaluation import Backtester, BacktestResult

DEFAULT_FLOORS: tuple[float, ...] = (10, 12, 15, 17, 20, 22, 25, 30, 35, 40)


@dataclass
class FloorMetrics:
    floor: float
    n: int
    days: float
    expectancy: float
    win_rate: float
    profit_factor: Optional[float]
    max_dd_pct: float
    capture: Optional[float]
    entry_rate: Optional[float]

    @property
    def per_day(self) -> float:
        return self.n / self.days if self.days else 0.0

    def row(self) -> str:
        pf = "n/d" if self.profit_factor is None else ("∞" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}")
        cap = "n/d" if self.capture is None else f"{self.capture:.0%}"
        return f"{self.floor:>5.0f}{self.n:>10}{self.per_day:>10.2f}{cap:>9}{self.expectancy:>+12.2f}R{pf:>7}{self.max_dd_pct:>7.1f}%{self.win_rate:>6.0%}"


def _metrics(results: list[BacktestResult], floor: float, strategy: str, equity: float, risk_pct: float) -> FloorMetrics:
    rows = [r for res in results for r in res.trade_rows]
    rs = [row["results"].get(strategy, row["results"].get("3R")) for row in rows]
    rs = [x for x in rs if x is not None]
    days = sum(((res.opportunity.period_hours if res.opportunity else 0.0) for res in results)) / 24.0
    wins, losses = [x for x in rs if x > 0], [x for x in rs if x <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins else None)
    trades = [TradeR(row["time"], "", (row["results"].get(strategy, row["results"].get("3R")) or 0.0), 0.0, strategy) for row in rows]
    path = simulate_equity(trades, equity, risk_pct)
    caps = [res.opportunity.capture_rate for res in results if res.opportunity and res.opportunity.capture_rate is not None]
    ents = [res.opportunity.entry_rate for res in results if res.opportunity and res.opportunity.entry_rate is not None]
    return FloorMetrics(floor, len(rs), days, statistics.fmean(rs) if rs else 0.0, (len(wins) / len(rs)) if rs else 0.0, pf, path.max_drawdown_pct,
                        statistics.fmean(caps) if caps else None, statistics.fmean(ents) if ents else None)


def objective(m: FloorMetrics, min_n: int = 5) -> float:
    """Expectancy × √n (t-stat simplificado): premia edge com volume; amostra < min_n vale zero."""
    if m.n < min_n:
        return 0.0
    return m.expectancy * math.sqrt(m.n)


@dataclass
class FoldChoice:
    fold: int
    floor: float
    train: FloorMetrics
    test: FloorMetrics
    train_table: list[FloorMetrics] = field(default_factory=list)


@dataclass
class SweepResult:
    floors: tuple[float, ...]
    choices: list[FoldChoice]
    oos_intrain: FloorMetrics                # agregado OOS com o piso escolhido no treino de cada fold
    sensitivity: list[FloorMetrics]          # piso fixo em todos os folds de teste (descritivo)
    strategy: str
    default_floor: float

    def render(self) -> str:
        hdr = f"{'Piso':>5}{'Entradas':>10}{'Entr/dia':>10}{'Capture':>9}{'Expectancy':>13}{'PF':>7}{'DD':>8}{'Win':>6}"
        lines = [f"🔬 SWEEP DE PISO DE VANTAGEM — walk-forward, estratégia {self.strategy}, pisos {', '.join(f'{f:g}' for f in self.floors)}",
                 "", "1) SELEÇÃO IN-TRAIN (número honesto: o piso de cada fold foi escolhido só no treino)"]
        for c in self.choices:
            lines.append(f"  fold {c.fold}: treino escolheu piso {c.floor:g} (E={c.train.expectancy:+.2f}R, n={c.train.n}) → teste: n={c.test.n}, "
                         f"E={c.test.expectancy:+.2f}R, DD {c.test.max_dd_pct:.1f}%")
        m = self.oos_intrain
        lines.append(f"  OOS agregado: entradas {m.n} ({m.per_day:.2f}/dia) · E={m.expectancy:+.2f}R · win {m.win_rate:.0%} · "
                     f"PF {'n/d' if m.profit_factor is None else ('∞' if m.profit_factor == float('inf') else f'{m.profit_factor:.2f}')} · DD {m.max_dd_pct:.1f}%"
                     + (f" · capture {m.capture:.0%}" if m.capture is not None else ""))
        chosen = [c.floor for c in self.choices]
        if chosen:
            lines.append(f"  pisos escolhidos: {', '.join(f'{f:g}' for f in chosen)} · mediana {statistics.median(chosen):g} (padrão atual {self.default_floor:g})")
        lines += ["", "2) SENSIBILIDADE OOS POR PISO FIXO (descritiva — olha o teste; NÃO usar para escolher o piso)", hdr]
        for fm in self.sensitivity:
            lines.append(fm.row() + ("  ◀ padrão" if fm.floor == self.default_floor else ""))
        best = max((fm for fm in self.sensitivity if fm.n >= 10), key=lambda fm: objective(fm), default=None)
        if best is not None:
            lines.append(f"  maior expectancy×√n com n≥10: piso {best.floor:g} — confirme com a seleção in-train acima antes de adotar")
        return "\n".join(lines)


def threshold_sweep(bt: Backtester, floors: Sequence[float] = DEFAULT_FLOORS, n_folds: int = 4, train_folds: int = 2, strategy: str = "adaptive",
                    equity: float = 10000.0, risk_pct: float = 0.5, base_cfg: Optional[EngineConfig] = None, log=None) -> SweepResult:
    base = base_cfg or bt.cfg
    n = len(bt.frame.xau)
    usable = n - bt.warmup
    fold_len = usable // (n_folds + train_folds)

    def cfg_with(floor: float) -> EngineConfig:
        d = {**base.__dict__, "weights": dict(base.weights), "factor_signs": dict(base.factor_signs), "min_edge_score": float(floor)}
        return EngineConfig(**d)

    choices: list[FoldChoice] = []
    oos_by_floor: dict[float, list[BacktestResult]] = {f: [] for f in floors}
    chosen_results: list[BacktestResult] = []
    for k in range(n_folds):
        train_end = bt.warmup + (train_folds + k) * fold_len
        train_start = train_end - train_folds * fold_len
        test_end = min(n, train_end + fold_len)
        train_table: list[FloorMetrics] = []
        for f in floors:
            r = bt.run(train_start, train_end, cfg_with(f))
            train_table.append(_metrics([r], f, strategy, equity, risk_pct))
            if log:
                log(f"fold {k + 1} treino piso {f:g}: n={train_table[-1].n} E={train_table[-1].expectancy:+.2f}R")
        best = max(train_table, key=objective)
        if objective(best) <= 0:
            best = next((t for t in train_table if t.floor == base.min_edge_score), train_table[0])  # sem edge no treino → mantém o padrão
        test_runs: dict[float, BacktestResult] = {}
        for f in floors:
            test_runs[f] = bt.run(train_end, test_end, cfg_with(f))
            oos_by_floor[f].append(test_runs[f])
        test_m = _metrics([test_runs[best.floor]], best.floor, strategy, equity, risk_pct)
        chosen_results.append(test_runs[best.floor])
        choices.append(FoldChoice(k + 1, best.floor, best, test_m, train_table))
    oos_intrain = _metrics(chosen_results, float("nan"), strategy, equity, risk_pct)
    sensitivity = [_metrics(oos_by_floor[f], f, strategy, equity, risk_pct) for f in floors]
    return SweepResult(tuple(floors), choices, oos_intrain, sensitivity, strategy, base.min_edge_score)
