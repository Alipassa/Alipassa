"""EXIT LAB (5.2) — a saída com maior expectancy FORA DA AMOSTRA, não o maior alvo.

O sistema já registra, para cada operação simulada, a trajetória com o stop inicial (MFE/MAE em R) e o resultado de cada
estratégia de saída (1R, 2R, 3R, 4R, trailing, 2R+trailing, adaptive). Aqui:
  • distribuição de MFE (mediana, P75, P90) e MAE — até onde o preço costuma ir a favor antes de estopar;
  • expectancy / PF / acerto por estratégia;
  • escolha WALK-FORWARD: em cada bloco cronológico a estratégia é escolhida só com os blocos anteriores e avaliada no bloco —
    a linha "escolhida OOS" é o que uma política de seleção teria rendido de fato;
  • recomendação apenas com n ≥ 20 casos OOS (ciclo de vida: candidato → operacional 30 → validado 50).
Nunca altera a saída ao vivo sozinho: recomenda; adotar é decisão com amostra."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

MIN_OOS = 20


def _percentile(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * p
    i = int(k)
    if i + 1 < len(ys):
        return ys[i] + (ys[i + 1] - ys[i]) * (k - i)
    return ys[i]


@dataclass
class StrategyLine:
    name: str
    n: int
    expectancy: float
    win_rate: float
    profit_factor: Optional[float]

    def row(self) -> str:
        pf = "n/d" if self.profit_factor is None else ("∞" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}")
        return f"  {self.name:<14}{self.n:>5}{self.expectancy:>+9.2f}R{self.win_rate:>8.0%}{pf:>7}"


def strategy_line(name: str, rs: Sequence[float]) -> StrategyLine:
    wins, losses = [x for x in rs if x > 0], [x for x in rs if x <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins else None)
    return StrategyLine(name, len(rs), statistics.fmean(rs) if rs else 0.0, (len(wins) / len(rs)) if rs else 0.0, pf)


@dataclass
class ExitLabReport:
    market: str
    n: int
    mfe_median: Optional[float]
    mfe_p75: Optional[float]
    mfe_p90: Optional[float]
    mae_median: Optional[float]
    reach: dict[str, float] = field(default_factory=dict)          # "1R" → fração que alcançou
    strategies: list[StrategyLine] = field(default_factory=list)
    chosen_oos: Optional[StrategyLine] = None                       # política walk-forward
    choices: list[tuple[int, str, int]] = field(default_factory=list)   # (bloco, estratégia escolhida no treino, n do bloco)
    recommendation: str = ""

    def render(self) -> str:
        f = lambda x: "n/d" if x is None else f"{x:.2f}R"  # noqa: E731
        lines = [f"🔬 EXIT LAB — {self.market} · {self.n} operações (stop 1R)",
                 f"MFE (até onde foi a favor antes do stop): mediana {f(self.mfe_median)} · P75 {f(self.mfe_p75)} · P90 {f(self.mfe_p90)} · MAE mediana {f(self.mae_median)}"]
        if self.reach:
            lines.append("Alcançou: " + " · ".join(f"{k} {v:.0%}" for k, v in self.reach.items()))
        lines.append(f"  {'saída':<14}{'n':>5}{'expect.':>10}{'acerto':>8}{'PF':>7}")
        lines += [s.row() for s in self.strategies]
        if self.chosen_oos is not None:
            lines.append(self.chosen_oos.row() + "   ◀ política walk-forward (escolhida só com o passado)")
            lines.append("  escolhas por bloco: " + ", ".join(f"{b}:{name} (n={n})" for b, name, n in self.choices))
        lines.append(self.recommendation)
        return "\n".join(lines)


def exit_lab(market: str, rows: Sequence[dict], n_blocks: int = 4, default: str = "3R") -> ExitLabReport:
    """rows: {"time", "profile": ExcursionProfile, "results": {estratégia: R}} — ordenadas no tempo aqui."""
    rows = sorted(rows, key=lambda r: r["time"])
    mfe = [float(r["profile"].max_r_before_stop) for r in rows if r.get("profile") is not None]
    mae = [float(getattr(r["profile"], "mae_r", 0.0)) for r in rows if r.get("profile") is not None]
    rep = ExitLabReport(market, len(rows), _percentile(mfe, 0.5), _percentile(mfe, 0.75), _percentile(mfe, 0.9), _percentile(mae, 0.5))
    if mfe:
        rep.reach = {f"{k}R": sum(1 for x in mfe if x >= k) / len(mfe) for k in (1, 2, 3, 4)}
    names = sorted({k for r in rows for k in r["results"]})
    for name in names:
        rep.strategies.append(strategy_line(name, [r["results"][name] for r in rows if name in r["results"]]))
    rep.strategies.sort(key=lambda s: -s.expectancy)
    # walk-forward: bloco k avaliado com a estratégia que venceu nos blocos < k (o 1º bloco usa a hipótese padrão)
    if len(rows) >= 2 * n_blocks and names:
        size = len(rows) // n_blocks
        oos: list[float] = []
        for b in range(n_blocks):
            test = rows[b * size:(b + 1) * size] if b < n_blocks - 1 else rows[b * size:]
            train = rows[:b * size]
            if train:
                best = max(names, key=lambda nm: statistics.fmean([r["results"][nm] for r in train if nm in r["results"]] or [0.0]))
            else:
                best = default if default in names else names[0]
            rep.choices.append((b + 1, best, len(test)))
            oos += [r["results"][best] for r in test if best in r["results"]]
        rep.chosen_oos = strategy_line("escolhida OOS", oos)
    top = rep.strategies[0] if rep.strategies else None
    if top is None or rep.n < MIN_OOS:
        rep.recommendation = f"⚪ amostra {rep.n} < {MIN_OOS}: sem recomendação — a saída padrão ({default}) continua; medir mais."
    else:
        base = next((s for s in rep.strategies if s.name == default), None)
        gain = (top.expectancy - base.expectancy) if base else 0.0
        oos_txt = f" · política walk-forward {rep.chosen_oos.expectancy:+.2f}R" if rep.chosen_oos else ""
        if base is not None and top.name != default and gain > 0.10:
            rep.recommendation = (f"🟢 candidata: {top.name} ({top.expectancy:+.2f}R) supera {default} ({base.expectancy:+.2f}R) em {gain:+.2f}R{oos_txt}. "
                                  f"Adotar só se a política walk-forward também superar {default} (n ≥ {MIN_OOS}).")
        else:
            rep.recommendation = f"🟡 {default} continua adequada (melhor {top.name} {top.expectancy:+.2f}R, diferença {gain:+.2f}R){oos_txt}."
    return rep
