"""GOLD AI ENGINE 2.1 — VALIDATION ENGINE.

Missão: provar (ou refutar) que o 2.0 antecipa o XAU/USD.
  1. Auditoria anti look-ahead   → lookahead_audit
  2. Walk-forward rolante         → evaluation.walk_forward(mode="rolling")
  3. Lead time / 4. MFE-MAE       → evaluation.evaluate
  5. Probabilidade calibrada      → calibration_table, Brier, IsotonicCalibrator
  6. Score por fator              → factor_scoreboard (quais informações realmente preveem)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .models import Candle, MarketSnapshot


# --------------------------------------------------------------------------- 1. anti look-ahead
def lookahead_audit(snapshot: MarketSnapshot) -> list[str]:
    """Lista violações: qualquer candle, notícia ou evento *realizado* com timestamp posterior ao snapshot."""
    t = snapshot.time
    bad: list[str] = []
    for tf, cs in snapshot.candles.items():
        late = [c for c in cs if c.time > t]
        if late:
            bad.append(f"{tf}: {len(late)} candle(s) após {t:%Y-%m-%d %H:%M}")
        if any(cs[i].time > cs[i + 1].time for i in range(len(cs) - 1)):
            bad.append(f"{tf}: candles fora de ordem")
    for n in snapshot.news:
        if n.time > t:
            bad.append(f"notícia futura: {n.headline[:50]}")
    for e in snapshot.events:
        if e.actual is not None and e.time > t:
            bad.append(f"resultado de evento futuro: {e.name}")
    return bad


# --------------------------------------------------------------------------- 5. calibração
@dataclass
class CalibrationBin:
    lo: float
    hi: float
    n: int
    predicted: float   # média da probabilidade prevista
    observed: float    # taxa de acerto observada

    @property
    def gap(self) -> float:
        return self.observed - self.predicted


@dataclass
class CalibrationReport:
    bins: list[CalibrationBin]
    brier: Optional[float]
    brier_reference: Optional[float]  # Brier de prever sempre a taxa-base
    ece: Optional[float]              # expected calibration error
    n: int

    def render(self) -> str:
        lines = ["🎯 CALIBRAÇÃO DA PROBABILIDADE"]
        if not self.n:
            return "\n".join(lines + ["  (sem previsões resolvidas)"])
        lines.append(f"  n={self.n} · Brier={self.brier:.3f} (referência {self.brier_reference:.3f}; menor é melhor) · ECE={self.ece:.3f}")
        lines.append("  previsto → observado")
        for b in self.bins:
            bar = "█" * int(round(b.observed * 20))
            lines.append(f"  {b.lo:.0%}–{b.hi:.0%}: prev {b.predicted:.0%} obs {b.observed:.0%} (n={b.n}) {bar} {'+' if b.gap > 0 else ''}{b.gap:+.0%}")
        verdict = "bem calibrado" if self.ece < 0.05 else "moderadamente calibrado" if self.ece < 0.10 else "MAL calibrado — usar IsotonicCalibrator"
        lines.append(f"  Veredito: {verdict}")
        return "\n".join(lines)


def calibration_table(pairs: Iterable[tuple[float, bool]], n_bins: int = 5, lo: float = 0.5, hi: float = 1.0) -> CalibrationReport:
    """pairs: (probabilidade prevista na direção sinalizada, acertou?)."""
    data = [(p, 1.0 if hit else 0.0) for p, hit in pairs]
    if not data:
        return CalibrationReport([], None, None, None, 0)
    width = (hi - lo) / n_bins
    bins: list[CalibrationBin] = []
    ece = 0.0
    for k in range(n_bins):
        a, b = lo + k * width, lo + (k + 1) * width
        inb = [(p, y) for p, y in data if (a <= p < b) or (k == n_bins - 1 and p == b)]
        if not inb:
            continue
        pred = statistics.fmean(p for p, _ in inb)
        obs = statistics.fmean(y for _, y in inb)
        bins.append(CalibrationBin(a, b, len(inb), pred, obs))
        ece += len(inb) / len(data) * abs(obs - pred)
    brier = statistics.fmean((p - y) ** 2 for p, y in data)
    base = statistics.fmean(y for _, y in data)
    brier_ref = statistics.fmean((base - y) ** 2 for _, y in data)
    return CalibrationReport(bins, round(brier, 4), round(brier_ref, 4), round(ece, 4), len(data))


class IsotonicCalibrator:
    """Regressão isotônica (pool-adjacent-violators): mapeia probabilidade prevista → observada,
    monotônica. Aplicável ao motor via GoldAIEngine.calibrator."""

    def __init__(self) -> None:
        self.xs: list[float] = []
        self.ys: list[float] = []

    def fit(self, pairs: Iterable[tuple[float, bool]]) -> "IsotonicCalibrator":
        data = sorted((p, 1.0 if h else 0.0) for p, h in pairs)
        if not data:
            return self
        # agrupa empates de x (mesma probabilidade) antes do PAV
        grouped: list[list[float]] = []  # [x, y médio, peso]
        for x, y in data:
            if grouped and grouped[-1][0] == x:
                g = grouped[-1]
                g[1] = (g[1] * g[2] + y) / (g[2] + 1)
                g[2] += 1
            else:
                grouped.append([x, y, 1])
        blocks = grouped  # [x médio, y médio, peso]
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][1] > blocks[i + 1][1]:
                a, b = blocks[i], blocks[i + 1]
                w = a[2] + b[2]
                merged = [(a[0] * a[2] + b[0] * b[2]) / w, (a[1] * a[2] + b[1] * b[2]) / w, w]
                blocks[i:i + 2] = [merged]
                i = max(0, i - 1)
            else:
                i += 1
        self.xs = [b[0] for b in blocks]
        self.ys = [b[1] for b in blocks]
        return self

    def __call__(self, p: float) -> float:
        if not self.xs:
            return p
        if p <= self.xs[0]:
            return self.ys[0]
        if p >= self.xs[-1]:
            return self.ys[-1]
        for i in range(len(self.xs) - 1):
            if self.xs[i] <= p <= self.xs[i + 1]:
                span = self.xs[i + 1] - self.xs[i]
                w = (p - self.xs[i]) / span if span else 0.0
                return self.ys[i] + w * (self.ys[i + 1] - self.ys[i])
        return p

    def to_dict(self) -> dict:
        return {"xs": self.xs, "ys": self.ys}

    @classmethod
    def from_dict(cls, d: dict) -> "IsotonicCalibrator":
        c = cls()
        c.xs, c.ys = list(d.get("xs", [])), list(d.get("ys", []))
        return c


# --------------------------------------------------------------------------- 6. score por fator
@dataclass
class FactorRow:
    name: str
    n: int                 # previsões em que o fator estava alinhado com a direção prevista
    hit_rate: float        # acertos / n quando alinhado
    n_against: int         # previsões em que o fator apontava contra
    hit_rate_against: Optional[float]
    lift: Optional[float]  # hit_rate − hit_rate_against (poder discriminante)

    def bar(self, width: int = 10) -> str:
        return "█" * int(round(self.hit_rate * width)) + "░" * (width - int(round(self.hit_rate * width)))


@dataclass
class Scoreboard:
    rows: list[FactorRow]
    base_rate: Optional[float]
    n: int

    def render(self) -> str:
        lines = ["📈 SCORE POR FATOR — taxa de acerto quando o fator apontava na direção do sinal"]
        if not self.n:
            return "\n".join(lines + ["  (sem previsões resolvidas)"])
        lines.append(f"  taxa-base (todos os sinais): {self.base_rate:.0%} · n={self.n}")
        w = max(len(r.name) for r in self.rows) if self.rows else 10
        for r in sorted(self.rows, key=lambda r: -(r.lift if r.lift is not None else r.hit_rate)):
            lift = f"lift {r.lift:+.0%}" if r.lift is not None else "lift n/d"
            lines.append(f"  {r.name:<{w}} {r.bar()} {r.hit_rate:>4.0%}  (n={r.n:<3} {lift})")
        return "\n".join(lines)


def factor_scoreboard(records: Sequence[dict], min_ratio: float = 0.2) -> Scoreboard:
    """records: dicts com 'direction' (ALTA|BAIXA), 'hit' (bool) e 'factors' {nome: valor assinado
    (+ = altista)}, opcionalmente 'technical' {indicador: valor assinado}. Um fator conta como
    'alinhado' se sinal(valor) == sinal(direção) e |valor| >= min_ratio (valores em -1..+1)."""
    names: dict[str, tuple[list[bool], list[bool]]] = {}
    hits_all: list[bool] = []
    for r in records:
        sign = 1.0 if r["direction"] == "ALTA" else -1.0
        hit = bool(r["hit"])
        hits_all.append(hit)
        allf = {**r.get("factors", {}), **{f"tec:{k}": v for k, v in (r.get("technical") or {}).items()}}
        for name, val in allf.items():
            if val is None or abs(val) < min_ratio:
                continue
            aligned, against = names.setdefault(name, ([], []))
            (aligned if sign * val > 0 else against).append(hit)
    rows: list[FactorRow] = []
    for name, (al, ag) in names.items():
        if not al:
            continue
        hr = sum(al) / len(al)
        hra = (sum(ag) / len(ag)) if ag else None
        rows.append(FactorRow(name, len(al), hr, len(ag), hra, (hr - hra) if hra is not None else None))
    base = (sum(hits_all) / len(hits_all)) if hits_all else None
    return Scoreboard(rows, base, len(hits_all))


# --------------------------------------------------------------------------- relatório consolidado
@dataclass
class ValidationReport:
    backtest_text: str
    walk_forward_text: str
    calibration: CalibrationReport
    scoreboard: Scoreboard
    audit_violations: list[str] = field(default_factory=list)
    n_audited: int = 0
    min_signals: int = 20  # abaixo disso nenhuma conclusão estatística é honesta
    opportunity_text: str = ""

    def verdict(self) -> str:
        import re
        m = re.search(r"Lead time \(acertos\): média ([\d.]+) min", self.walk_forward_text)
        lead = float(m.group(1)) if m else None
        p = re.search(r"Precisão: total (\d+)\.?\d*%", self.walk_forward_text)
        prec = int(p.group(1)) / 100 if p else None
        if self.audit_violations:
            return "❌ REPROVADO — violações de look-ahead"
        n = self.calibration.n
        if prec is None or lead is None or n < self.min_signals:
            return f"⚪ INCONCLUSIVO — {n} sinal(is) resolvido(s) fora da amostra; mínimo {self.min_signals}. Rode `live` por mais tempo ou use histórico com DXY/juros."
        if prec >= 0.6 and lead >= 10 and (self.calibration.ece or 1) < 0.10:
            return f"🟢 EVIDÊNCIA DE ANTECIPAÇÃO — precisão OOS {prec:.0%}, lead médio {lead:.0f} min, calibração ok"
        if prec >= 0.5:
            return f"🟡 PARCIAL — precisão OOS {prec:.0%}, lead médio {lead:.0f} min; ainda não comprova antecipação consistente"
        return f"🔴 SEM EVIDÊNCIA — precisão OOS {prec:.0%}"

    def render(self) -> str:
        audit = f"🔍 AUDITORIA ANTI LOOK-AHEAD: {self.n_audited} snapshots, {len(self.audit_violations)} violação(ões)"
        if self.audit_violations:
            audit += "\n" + "\n".join(f"  ✗ {v}" for v in self.audit_violations[:10])
        parts = ["🧪 GOLD AI ENGINE — VALIDATION ENGINE", audit, self.backtest_text, self.walk_forward_text,
                 self.calibration.render(), self.scoreboard.render()]
        if self.opportunity_text:
            parts.append(self.opportunity_text)
        parts.append(f"VEREDITO: {self.verdict()}")
        return "\n\n".join(parts)
