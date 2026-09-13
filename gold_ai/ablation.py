"""TESTE A/B — o que a informação adiciona ao cérebro (MARKET AI 4.0).

Mesmo período, mesmo walk-forward, mesmo piso; muda só o que o cérebro pode ver:
  • Preço somente            — sem banco histórico (news_mode = none)
  • Preço + Macro (TESTE A)  — calendário macro/bancos centrais point-in-time (news_mode = macro)
  • Preço + Macro + News (B) — + manchetes, tom e intensidade (GDELT) (news_mode = full)

Se a informação cria a oportunidade, entradas e expectancy sobem SEM mexer no funil. Só depois recalibra-se o piso.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .config import EngineConfig
from .evaluation import Backtester, HistoryFrame, walk_forward
from .history import EventHistory
from .markets import get_market
from .sweep import FloorMetrics, _metrics

MODES: tuple[tuple[str, str], ...] = (("none", "Preço somente"), ("macro", "Preço + Macro (A)"), ("full", "Preço + Macro + News (B)"))


@dataclass
class ModeResult:
    market: str
    mode: str
    label: str
    metrics: FloorMetrics
    steps: int
    steps_with_info: int
    news_known_steps: int

    @property
    def info_share(self) -> float:
        return self.steps_with_info / self.steps if self.steps else 0.0

    def row(self) -> str:
        m = self.metrics
        pf = "n/d" if m.profit_factor is None else ("∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}")
        cap = "n/d" if m.capture is None else f"{m.capture:.0%}"
        return (f"{self.market:<8}{self.label:<27}{self.steps:>7}{self.info_share:>8.0%}{m.n:>9}{m.per_day:>8.2f}{cap:>9}{m.expectancy:>+12.2f}R{pf:>7}"
                f"{m.max_dd_pct:>7.1f}%{m.win_rate:>7.0%}")


@dataclass
class AblationReport:
    start: str
    end: str
    history_stats: str
    results: list[ModeResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_market(self) -> dict[str, dict[str, ModeResult]]:
        out: dict[str, dict[str, ModeResult]] = {}
        for r in self.results:
            out.setdefault(r.market, {})[r.mode] = r
        return out

    def verdicts(self) -> list[str]:
        lines = []
        for mkt, modes in self.by_market().items():
            base = modes.get("none")
            if base is None:
                continue
            for mode, label in MODES[1:]:
                r = modes.get(mode)
                if r is None:
                    continue
                if r.steps_with_info == 0:
                    lines.append(f"{mkt} · {label}: banco sem cobertura neste período (0 passos com informação) — nada a concluir; rode `history stats`.")
                    continue
                d_exp = r.metrics.expectancy - base.metrics.expectancy
                d_n = r.metrics.n - base.metrics.n
                n = min(r.metrics.n, base.metrics.n)
                strength = "⚪ inconclusivo (amostra < 30)" if n < 30 else ("🟢 melhora" if d_exp > 0.05 else "🔴 piora" if d_exp < -0.05 else "🟡 sem diferença")
                lines.append(f"{mkt} · {label}: entradas {base.metrics.n} → {r.metrics.n} ({d_n:+d}), expectancy {base.metrics.expectancy:+.2f}R → "
                             f"{r.metrics.expectancy:+.2f}R ({d_exp:+.2f}R), cobertura {r.info_share:.0%} dos passos · {strength}")
        return lines

    def render(self) -> str:
        head = (f"🧪 TESTE A/B — O QUE A INFORMAÇÃO ADICIONA · {self.start} → {self.end} (walk-forward OOS, mesmo piso, mesma janela)\n"
                f"banco histórico: {self.history_stats}\n\n"
                f"{'mercado':<8}{'modo':<27}{'passos':>7}{'c/info':>8}{'entradas':>9}{'ent/dia':>8}{'capture':>9}{'expectancy':>13}{'PF':>7}{'DD':>8}{'acerto':>7}")
        rows = [r.row() for r in self.results]
        out = [head] + rows + ["", "LEITURA (Preço somente = referência):"] + [f"  • {v}" for v in self.verdicts()]
        if self.notes:
            out += ["", "NOTAS:"] + [f"  • {n}" for n in self.notes]
        out += ["", "REGRA: se a informação cria entradas com expectancy ≥ referência, ela fica; o funil só é recalibrado depois (`sweep`).",
                "       Efeitos por ativo: regras macro até `history learn` substituí-los pelo que o histórico mostrou (nunca inventados)."]
        return "\n".join(out)


def _info_steps(results, hist: EventHistory, mode: str) -> tuple[int, int]:
    """Passos OOS (um por decisão do backtest) e quantos deles tinham evento/notícia publicada naquele instante."""
    total, with_info = 0, 0
    for res in results:
        for d in res.decisions:
            total += 1
            if mode == "none":
                continue
            avail = hist.available_at(d.time)
            if mode == "macro":
                avail = [e for e in avail if e.category in ("MACRO", "CENTRAL_BANK")]
            if avail:
                with_info += 1
    return total, with_info


def compare_information(frames: dict[str, HistoryFrame], hist: EventHistory, start: datetime, end: datetime, equity: float = 10000.0, risk_pct: float = 0.5,
                        n_folds: int = 4, step: int = 1, warmup: int = 220, horizon_min: int = 240, strategy: str = "adaptive",
                        cfg_factory: Optional[Callable[[str], EngineConfig]] = None, modes: tuple[str, ...] = ("none", "macro", "full"),
                        log: Optional[Callable[[str], None]] = None) -> AblationReport:
    rep = AblationReport(f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", hist.stats())
    labels = dict(MODES)
    for symbol, frame in frames.items():
        spec = get_market(symbol)
        for mode in modes:
            cfg = cfg_factory(symbol) if cfg_factory else EngineConfig(factor_signs=dict(spec.factor_signs), symbol=symbol)
            frame.symbol, frame.events, frame.news_mode = symbol, (hist if mode != "none" else None), mode
            bt = Backtester(frame, cfg, warmup=warmup, step=step, horizon_min=horizon_min)
            wf = walk_forward(bt, n_folds=n_folds)
            results = [res for _, res in wf.folds]
            m = _metrics(results, cfg.min_edge_score, strategy, equity, risk_pct)
            steps, with_info = _info_steps(results, hist, mode)
            rep.results.append(ModeResult(symbol, mode, labels[mode], m, steps, with_info, with_info))
            if log:
                log(f"{symbol} · {labels[mode]}: {m.n} entradas OOS, expectancy {m.expectancy:+.2f}R")
        frame.events, frame.news_mode = None, "full"
    if not any(r.steps_with_info for r in rep.results if r.mode != "none"):
        rep.notes.append("Nenhum passo do backtest teve evento/notícia disponível: o banco não cobre o período ou não foi carregado.")
    rep.notes.append("Reação real medida no fechamento do candle H1 seguinte ao evento (12:30 → 13:00); a sequência 12:29→12:31→12:35 exige histórico M1/M5.")
    rep.notes.append("Sem consenso (ALFRED) a surpresa é vs. o dado anterior; com Trading Economics é vs. o consenso — a regra macro lê a surpresa, não o nível.")
    return rep
