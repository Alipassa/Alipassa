"""CICLO DE VIDA DE PARÂMETROS — governança estatística (MARKET AI 5.1).

Um parâmetro (aqui: o "setup" de cada mercado, e qualquer regra que queira virar operacional) nasce e morre por AMOSTRA + SEQUÊNCIA,
nunca por uma sequência isolada:

  amostra OOS   < 10 → 🔴 não vira parâmetro · 10–19 → 🟡 observação · 20 → 🟢 candidato · 30 → operacional provisório
                50+ → validado · 100+ → alta confiança
  sequência     3 perdas → ⚠️ alerta (reduz confiança) · 4 → 🟠 proteção (sem novas entradas até reavaliar) · 5 → 🔴 suspensão + revalidação
  revalidação   últimos 30 + últimos 50 + total: edge continua positivo (expectancy > 0, PF > 1, sem deterioração)? SIM → reativa;
                NÃO → QUEBRADO: fica em SOMBRA (continua avaliado em PAPER) até provar edge de novo. NUNCA é apagado.
  deterioração  expectancy em janelas sucessivas caindo até ≤ 0 é evidência mais forte que 5 perdas seguidas.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .reaction_hires import profit_factor

TIERS = ((100, "alta confiança"), (50, "validado"), (30, "operacional"), (20, "candidato"), (10, "observação"))
ALERT_STREAK, PROTECT_STREAK, SUSPEND_STREAK = 3, 4, 5


def tier(n: int) -> str:
    for k, label in TIERS:
        if n >= k:
            return label
    return "sem amostra"


def tier_icon(n: int) -> str:
    return "🟢" if n >= 20 else "🟡" if n >= 10 else "🔴"


def consecutive_losses(xs: Sequence[float]) -> int:
    k = 0
    for x in reversed(xs):
        if x < 0:
            k += 1
        else:
            break
    return k


@dataclass
class Window:
    label: str
    n: int
    expectancy: float
    pf: Optional[float]
    win_rate: float

    @property
    def positive(self) -> bool:
        return self.n > 0 and self.expectancy > 0 and (self.pf or 0.0) > 1.0


@dataclass
class ParameterState:
    name: str
    n: int
    tier: str
    streak: int
    action: str                     # NORMAL | ALERTA | PROTEÇÃO | SUSPENSO | QUEBRADO | REATIVADO
    windows: list[Window] = field(default_factory=list)
    deteriorating: bool = False
    trend: list[float] = field(default_factory=list)      # expectancy por bloco de 10 (do mais antigo ao mais recente)
    note: str = ""
    edge_ok: bool = False           # expectancy positiva em todas as janelas com n ≥ 10, sem deterioração

    @property
    def grade(self) -> str:
        """HIERARQUIA DE EDGE: A comprovado (≥ 30 OOS, positivo, NORMAL/REATIVADO) · B promissor (10–29 OOS, positivo) ·
        C inconclusivo (< 10 OOS, ou positivo sem confirmação) · D negativo (≥ 10 OOS e expectancy total ≤ 0, ou SUSPENSO/QUEBRADO)."""
        total = next((w for w in self.windows if w.label == "total"), None)
        if self.action in ("SUSPENSO", "QUEBRADO", "PROTEÇÃO"):
            return "D"
        if self.n >= 10 and total is not None and total.expectancy <= 0:
            return "D"
        if self.n >= 30 and self.edge_ok and self.action in ("NORMAL", "REATIVADO", "ALERTA"):
            return "A"
        if self.n >= 10 and self.edge_ok:
            return "B"
        return "C"

    @property
    def grade_text(self) -> str:
        return {"A": "A edge comprovado", "B": "B edge promissor", "C": "C inconclusivo", "D": "D edge negativo"}[self.grade]

    @property
    def allows_entries(self) -> bool:
        return self.action in ("NORMAL", "ALERTA", "REATIVADO")

    @property
    def operational(self) -> bool:
        """Parâmetro com amostra suficiente para operar dinheiro (≥ 30 OOS) — abaixo disso é candidato/observação."""
        return self.n >= 30

    @property
    def confidence_multiplier(self) -> float:
        return 0.85 if self.action == "ALERTA" else 1.0

    def render(self) -> str:
        icon = {"NORMAL": "🟢", "ALERTA": "⚠️", "PROTEÇÃO": "🟠", "SUSPENSO": "🔴", "QUEBRADO": "⛔", "REATIVADO": "♻️"}[self.action]
        w = " · ".join(f"{x.label} n={x.n} {x.expectancy:+.2f}R PF {('∞' if x.pf == float('inf') else f'{x.pf:.2f}') if x.pf is not None else 'n/d'}" for x in self.windows)
        tr = " → ".join(f"{v:+.2f}" for v in self.trend[-5:]) if self.trend else "—"
        return (f"{icon} {self.name:<7} {self.action:<10} amostra {self.n} ({tier_icon(self.n)} {self.tier}) · sequência de perdas {self.streak} · {w} · "
                f"tendência {tr}" + (" · DETERIORAÇÃO" if self.deteriorating else "") + (f" · {self.note}" if self.note else ""))


def _window(label: str, xs: Sequence[float]) -> Window:
    return Window(label, len(xs), statistics.fmean(xs) if xs else 0.0, profit_factor(xs), (sum(1 for x in xs if x > 0) / len(xs)) if xs else 0.0)


def evaluate_parameter(name: str, results: Sequence[float], previous_action: str = "NORMAL") -> ParameterState:
    """`results`: R por operação FECHADA, em ordem cronológica (fora da amostra por construção no live).
    `previous_action`: estado anterior (SUSPENSO/QUEBRADO persistem até a revalidação passar)."""
    xs = list(results)
    n = len(xs)
    windows = [_window("últimos 30", xs[-30:]), _window("últimos 50", xs[-50:]), _window("total", xs)]
    blocks = [statistics.fmean(xs[i:i + 10]) for i in range(0, n - n % 10, 10)] if n >= 20 else []
    deteriorating = len(blocks) >= 3 and blocks[-1] <= 0 and blocks[-2] < blocks[-3]
    streak = consecutive_losses(xs)
    edge_ok = all(w.positive for w in windows if w.n >= 10) and not deteriorating and any(w.n >= 10 for w in windows)
    if n < 10:
        action, note = "NORMAL", "amostra < 10: ainda não é parâmetro — entradas seguem o funil normal (observação)"
    elif previous_action in ("SUSPENSO", "QUEBRADO"):
        # revalidação: só reativa se o edge continua positivo nas janelas
        if edge_ok and streak < SUSPEND_STREAK:
            action, note = "REATIVADO", "revalidação: edge positivo nos últimos 30/50/total → reativado"
        else:
            action, note = "QUEBRADO", "revalidação negativa: continua em SOMBRA (PAPER) até provar edge de novo — nunca apagado"
    elif deteriorating:
        action, note = "QUEBRADO", f"deterioração: expectancy por blocos {' → '.join(f'{b:+.2f}' for b in blocks[-3:])} — sombra (PAPER) até revalidar"
    elif streak >= SUSPEND_STREAK:
        # 5 perdas: suspende e revalida imediatamente pela amostra — sequência normal não mata parâmetro com edge
        action, note = ("REATIVADO", f"{streak} perdas seguidas, mas edge positivo nos últimos 30/50/total → mantido (revalidado)") if edge_ok else \
                       ("SUSPENSO", f"{streak} perdas seguidas e edge não confirmado → suspenso; revalidação a cada ciclo")
    elif streak >= PROTECT_STREAK:
        action, note = "PROTEÇÃO", f"{streak} perdas seguidas: sem novas entradas até a próxima reavaliação"
    elif streak >= ALERT_STREAK:
        action, note = "ALERTA", f"{streak} perdas seguidas: confiança reduzida (×0,85), parâmetro mantido"
    else:
        action, note = "NORMAL", ""
    return ParameterState(name, n, tier(n), streak, action, windows, deteriorating, blocks, note, edge_ok)


# --------------------------------------------------------------------------- ESCADA DE RISCO (decidida antes, nunca depois de ganhos ou perdas)
LADDER_TIERS = (30, 50)          # degraus: operacional (30 casos OOS) · validado (50 casos OOS)


def risk_by_grade(state: "ParameterState", base_pct: float, ladder: Sequence[float], ceiling_pct: float = 5.0,
                  sample_pct: float = 1.0) -> tuple[float, str]:
    """Risco por operação pela HIERARQUIA: A → escada (base/4/5) · B → base · C → risco de amostra (`sample_pct`; 0 = não opera) ·
    D → 0 (bloqueado). A escada só sobe dentro de A; a letra é função da amostra e do edge, nunca de uma boa ou má semana."""
    g = state.grade
    if g == "D":
        return 0.0, f"grau D (edge negativo: {state.n} casos) — bloqueado"
    if g == "C":
        return (float(sample_pct), f"grau C (inconclusivo: {state.n} casos) — risco de amostra {sample_pct:g}%") if sample_pct > 0 else                (0.0, f"grau C (inconclusivo: {state.n} casos) — sem operar (SAMPLE_RISK_PCT=0)")
    if g == "B":
        return min(float(base_pct), float(ceiling_pct)), f"grau B (promissor: {state.n} casos) — base {min(float(base_pct), float(ceiling_pct)):g}%"
    pct, why = risk_ladder_pct(state, base_pct, ladder, ceiling_pct)
    return pct, f"grau A — {why}"


def risk_ladder_pct(state: "ParameterState", base_pct: float, ladder: Sequence[float], ceiling_pct: float = 5.0) -> tuple[float, str]:
    """Risco por operação do mercado: `ladder` = (base, operacional, validado) em % do capital, limitado por `ceiling_pct`.
    Sobe só com amostra (30 / 50 casos OOS) E edge positivo nas janelas E estado NORMAL/REATIVADO. Cai para a base em ALERTA,
    PROTEÇÃO, SUSPENSO, QUEBRADO, deterioração ou expectancy negativa. Nunca sobe por sequência de ganhos nem desce por sequência
    de perdas fora dessas regras — o percentual é função do tier, não do humor."""
    steps = [float(x) for x in ladder] if ladder else [base_pct]
    while len(steps) < 3:
        steps.append(steps[-1])
    cap = float(ceiling_pct)
    base = min(float(base_pct), cap)
    if state.action not in ("NORMAL", "REATIVADO") or not state.edge_ok:
        why = "sem edge confirmado" if state.action in ("NORMAL", "REATIVADO") else state.action
        return base, f"base {base:g}% ({why})"
    if state.n >= LADDER_TIERS[1]:
        return min(steps[2], cap), f"{min(steps[2], cap):g}% (validado: {state.n} casos, edge positivo)"
    if state.n >= LADDER_TIERS[0]:
        return min(steps[1], cap), f"{min(steps[1], cap):g}% (operacional: {state.n} casos, edge positivo)"
    return base, f"base {base:g}% ({state.tier}: {state.n} casos)"


def render_table(states: Sequence[ParameterState]) -> str:
    lines = ["🧬 CICLO DE VIDA DOS PARÂMETROS — amostra OOS + sequência (nunca apaga; suspende e revalida)"]
    lines += ["  " + s.render() for s in states]
    lines.append("  níveis: <10 não é parâmetro · 10–19 observação · 20 candidato · 30 operacional · 50 validado · 100 alta confiança")
    lines.append("  sequência: 3 alerta · 4 proteção · 5 suspensão + revalidação (últimos 30 / 50 / total); deterioração por blocos quebra antes")
    return "\n".join(lines)
