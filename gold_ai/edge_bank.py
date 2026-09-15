"""EDGE BANK (5.2) — memória estatística do que funciona, onde funciona e quanto pode ser emprestado a outro ativo.

Cada situação (contexto) recebe uma etiqueta: regime, evento (cpi, fomc_hawkish…), banda VWAP × regime, estado do relógio de
reação, estado do fluxo. Para cada (ativo, etiqueta) o banco guarda os R:
  • OPERADO  — resultado real das entradas (backtest OOS ou vivido em PAPER/LIVE);
  • POTENCIAL — o que a hipótese padrão teria rendido nas oportunidades ≥ SETUP (para aprender quando NÃO operar).
Aprendizado cruzado: CASOS PRÓPRIOS + EVIDÊNCIA TRANSFERIDA PONDERADA (similaridade entre ativos × 0,5) — o n próprio fica
separado e é ele que define o tier do ciclo de vida. 50 XAU + 30 US500 nunca viram 80 casos de EURUSD.
Regra de uso ao vivo: só contextos com n PRÓPRIO ≥ 30 (operacional) ajustam a prioridade do Asset Selector (×0,9 / ×1,1); abaixo
disso o banco é conhecimento exibido, não regra."""
from __future__ import annotations

import json
import math
import os
import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .lifecycle import TIERS, tier
from .markets import MARKETS

TRANSFER_WEIGHT = 0.5      # fração da evidência alheia que pode ser emprestada (× similaridade)
MIN_OWN_NEGATIVE = 20      # "quando NÃO operar": só com n próprio ≥ 20 e expectancy < 0
MIN_OWN_LIVE = 30          # ajuste de prioridade ao vivo: só tier operacional


def similarity(a: str, b: str) -> float:
    """Cosseno dos vetores de sinais de fatores (MarketSpec.factor_signs), truncado em 0: ativos que reagem ao mesmo conjunto
    de forças no mesmo sentido são parecidos; sentido oposto (USDJPY × XAUUSD) vale 0, nunca negativo."""
    if a == b:
        return 1.0
    sa, sb = MARKETS.get(a), MARKETS.get(b)
    if sa is None or sb is None:
        return 0.0
    keys = sorted(set(sa.factor_signs) | set(sb.factor_signs))
    va, vb = [float(sa.factor_signs.get(k, 0)) for k in keys], [float(sb.factor_signs.get(k, 0)) for k in keys]
    na, nb = math.sqrt(sum(x * x for x in va)), math.sqrt(sum(x * x for x in vb))
    if not na or not nb:
        return 0.0
    return max(0.0, sum(x * y for x, y in zip(va, vb)) / (na * nb))


@dataclass
class ContextStat:
    asset: str
    tag: str
    own: list[float] = field(default_factory=list)          # OPERADO
    potential: list[float] = field(default_factory=list)    # POTENCIAL (hipótese padrão nas oportunidades ≥ SETUP)
    transfer_n: float = 0.0                                  # n efetivo emprestado
    transfer_sum: float = 0.0                                # Σ w·n·média dos outros
    sources: list[str] = field(default_factory=list)

    @property
    def n_own(self) -> int:
        return len(self.own)

    @property
    def expectancy(self) -> Optional[float]:
        return statistics.fmean(self.own) if self.own else None

    @property
    def potential_expectancy(self) -> Optional[float]:
        return statistics.fmean(self.potential) if self.potential else None

    @property
    def blended(self) -> Optional[float]:
        """Casos próprios + evidência transferida ponderada; None se não há nada."""
        tot = self.n_own + self.transfer_n
        if tot <= 0:
            return None
        return (sum(self.own) + self.transfer_sum) / tot

    @property
    def tier(self) -> str:
        return tier(self.n_own)

    @property
    def profit_factor(self) -> Optional[float]:
        wins, losses = [x for x in self.own if x > 0], [x for x in self.own if x <= 0]
        if not self.own:
            return None
        return (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins else 0.0)

    def row(self) -> str:
        e = "n/d" if self.expectancy is None else f"{self.expectancy:+.2f}R"
        b = "" if (self.blended is None or self.transfer_n <= 0) else f"  c/ transferência {self.blended:+.2f}R (n_ef +{self.transfer_n:.1f} de {', '.join(self.sources)})"
        p = "" if self.potential_expectancy is None else f"  potencial {self.potential_expectancy:+.2f}R (n={len(self.potential)})"
        return f"  {self.tag:<32}{self.n_own:>4}  {e:>8}  {self.tier:<14}{b}{p}"


class EdgeBank:
    def __init__(self) -> None:
        self.stats: dict[tuple[str, str], ContextStat] = {}
        self.note: str = ""

    # ------------------------------------------------------------------ alimentação
    def get(self, asset: str, tag: str) -> ContextStat:
        key = (asset, tag)
        if key not in self.stats:
            self.stats[key] = ContextStat(asset, tag)
        return self.stats[key]

    def add_trade(self, asset: str, tags: Sequence[str], r: float) -> None:
        for tag in tags:
            self.get(asset, tag).own.append(float(r))

    def add_potential(self, asset: str, tags: Sequence[str], r: float) -> None:
        for tag in tags:
            self.get(asset, tag).potential.append(float(r))

    def add_backtest(self, asset: str, decisions: Sequence, trade_rows: Sequence[dict], strategy: str = "adaptive", default: str = "3R",
                     min_level: str = "SETUP") -> int:
        """decisions: opportunity.DecisionRecord (com level/context_tags); trade_rows: operações simuladas (R por estratégia)."""
        from .opportunity import LEVEL_RANK
        by_time = {row["time"]: row["results"].get(strategy, row["results"].get(default)) for row in trade_rows}
        n = 0
        for d in decisions:
            tags = d.context_tags
            if not tags:
                continue
            if d.action == "ENTRADA":
                r = by_time.get(d.time)
                if r is not None:
                    self.add_trade(asset, tags, r)
                    n += 1
            elif LEVEL_RANK.get(d.level, 0) >= LEVEL_RANK[min_level] and d.hypothetical_r is not None:
                self.add_potential(asset, tags, d.hypothetical_r)
        return n

    # ------------------------------------------------------------------ aprendizado cruzado
    def apply_transfer(self, weight: float = TRANSFER_WEIGHT) -> None:
        assets = sorted({a for a, _ in self.stats})
        for (asset, tag), st in self.stats.items():
            st.transfer_n, st.transfer_sum, st.sources = 0.0, 0.0, []
            for other in assets:
                if other == asset:
                    continue
                o = self.stats.get((other, tag))
                if o is None or not o.own:
                    continue
                w = weight * similarity(asset, other)
                if w <= 0:
                    continue
                st.transfer_n += w * o.n_own
                st.transfer_sum += w * sum(o.own)
                st.sources.append(f"{other}×{w:.2f}")

    # ------------------------------------------------------------------ leitura
    def for_asset(self, asset: str, min_n: int = 1) -> list[ContextStat]:
        rows = [s for (a, _), s in self.stats.items() if a == asset and (s.n_own >= min_n or s.transfer_n > 0 or s.potential)]
        return sorted(rows, key=lambda s: (-(s.expectancy if s.expectancy is not None else -9.0), -s.n_own))

    def negative_contexts(self, min_own: int = MIN_OWN_NEGATIVE) -> list[ContextStat]:
        """Aprender quando NÃO operar: o próprio histórico decide (n próprio ≥ 20 e expectancy < 0)."""
        out = [s for s in self.stats.values() if s.n_own >= min_own and (s.expectancy or 0.0) < 0]
        out += [s for s in self.stats.values() if s.n_own < min_own and len(s.potential) >= min_own and (s.potential_expectancy or 0.0) < -0.1 and s not in out]
        return sorted(out, key=lambda s: (s.asset, s.expectancy if s.expectancy is not None else s.potential_expectancy or 0.0))

    def multiplier(self, asset: str, tags: Sequence[str], min_own: int = MIN_OWN_LIVE) -> tuple[float, str]:
        """Ajuste de prioridade no Asset Selector: ×1,1 se o contexto atual tem edge operacional positivo, ×0,9 se negativo;
        1,0 sem amostra própria suficiente (o banco informa, não decide)."""
        best, note = 1.0, ""
        for tag in tags:
            st = self.stats.get((asset, tag))
            if st is None or st.n_own < min_own or st.expectancy is None:
                continue
            m = 1.1 if st.expectancy > 0.1 else 0.9 if st.expectancy < -0.1 else 1.0
            if m != 1.0 and (best == 1.0 or abs(m - 1) > abs(best - 1)):
                best, note = m, f"{tag} {st.expectancy:+.2f}R (n={st.n_own})"
        return best, note

    def render(self, assets: Optional[Sequence[str]] = None, min_n: int = 1, top: int = 12) -> str:
        assets = list(assets) if assets else sorted({a for a, _ in self.stats})
        lines = ["🏦 EDGE BANK — o que funciona, onde funciona (R por operação; n = casos PRÓPRIOS do ativo)",
                 "tiers: " + " · ".join(f"{k}={v}" for k, v in TIERS)]
        for a in assets:
            rows = self.for_asset(a, min_n)[:top]
            lines.append(f"{a}")
            lines.append("─" * 60)
            if not rows:
                lines.append("  (sem casos)")
            lines += [r.row() for r in rows]
        neg = self.negative_contexts()
        if neg:
            lines.append("")
            lines.append("🚫 QUANDO NÃO OPERAR (o histórico decide — n próprio ≥ 20 com expectancy < 0, ou potencial negativo com amostra):")
            for s in neg:
                e = s.expectancy if s.expectancy is not None else s.potential_expectancy
                lines.append(f"  {s.asset:<8}{s.tag:<32} {e:+.2f}R  (n={s.n_own}, potencial n={len(s.potential)})")
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)

    # ------------------------------------------------------------------ persistência
    def to_dict(self) -> dict:
        return {"note": self.note, "stats": [{"asset": s.asset, "tag": s.tag, "own": s.own, "potential": s.potential} for s in self.stats.values()]}

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "EdgeBank":
        bank = cls()
        if not os.path.exists(path):
            return bank
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        bank.note = data.get("note", "")
        for row in data.get("stats", []):
            st = bank.get(row["asset"], row["tag"])
            st.own, st.potential = [float(x) for x in row.get("own", [])], [float(x) for x in row.get("potential", [])]
        bank.apply_transfer()
        return bank
