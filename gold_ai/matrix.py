"""MATRIZ DE DECISÃO (5.2) — confirmações 1 → 2 → 3 → 4 → 5  ×  posições simultâneas 1 → 2 → 3 → 4.

Não se escolhe antecipadamente quantas confirmações nem quantas posições: o teste descobre. Para cada nível de confirmações,
cada mercado é reprocessado com o mesmo motor do backtest (nada mais muda); as operações de todos os mercados vão para a
carteira simulada com 1, 2, 3 e 4 posições simultâneas (limites de risco total e correlacionado do .env, custo por operação).
Métricas por célula: operações, acerto, R bruto e líquido, expectancy, lucro líquido, custos, MFE, MAE, drawdown, maior
sequência de perdas, duração média, resultado por ativo, por tipo de evento e nas duas metades do período.

Dentro × fora da amostra, sem truque: a célula "vencedora" é escolhida SÓ na 1ª metade do período (retorno − drawdown,
n ≥ MIN_N); o que ela rendeu na 2ª metade é o número que conta. A regra permanece: o histórico decide o parâmetro; e o
live só adota o que passar pelo autotune/ciclo de vida com amostra — a matriz é o mapa, não o gatilho."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from .autotune import cfg_with
from .config import EngineConfig
from .evaluation import Backtester
from .portfolio_sim import PortfolioResult, SimTrade, simulate_portfolio, trades_from_rows
from .selector import PortfolioLimits

CONFIRMATIONS = (1, 2, 3, 4, 5)
POSITIONS = (1, 2, 3, 4)
MIN_N = 20


@dataclass
class Cell:
    confirmations: int
    positions: int
    full: PortfolioResult
    first: PortfolioResult          # 1ª metade do período (onde se escolhe)
    second: PortfolioResult         # 2ª metade (fora da amostra: onde se confere)
    n_candidates: int = 0           # operações geradas antes da carteira (denominador das recusas)

    def ret_pct(self, equity: float, which: str = "full") -> float:
        r = getattr(self, which)
        return (r.end_equity / equity - 1) * 100 if equity else 0.0

    def score(self, equity: float, which: str = "first") -> Optional[float]:
        """Retorno − drawdown na metade indicada; None se a amostra é pequena demais para escolher."""
        r = getattr(self, which)
        if r.admitted < MIN_N:
            return None
        return self.ret_pct(equity, which) - r.max_dd_pct


@dataclass
class MatrixReport:
    start: str
    end: str
    equity: float
    risk_pct: float
    cost_r: float
    limits: PortfolioLimits
    confirmations: Sequence[int] = CONFIRMATIONS
    positions: Sequence[int] = POSITIONS
    cells: list[Cell] = field(default_factory=list)
    default_confirmations: int = 3
    split_time: Optional[datetime] = None

    def cell(self, c: int, n: int) -> Optional[Cell]:
        return next((x for x in self.cells if x.confirmations == c and x.positions == n), None)

    def winner(self) -> Optional[Cell]:
        scored = [(x.score(self.equity, "first"), x) for x in self.cells]
        scored = [(s, x) for s, x in scored if s is not None]
        if not scored:
            return None
        return max(scored, key=lambda t: (t[0], -t[1].positions, t[1].confirmations))[1]

    # ------------------------------------------------------------------ texto
    def render(self) -> str:
        eq = self.equity
        L = [f"🧮 MATRIZ DE DECISÃO — confirmações {min(self.confirmations)}→{max(self.confirmations)} × posições simultâneas "
             f"{min(self.positions)}→{max(self.positions)} · {self.start} → {self.end}",
             f"   capital {eq:,.0f} USD · risco {self.risk_pct:g}% por operação, composto · custo {self.cost_r:g}R/operação · "
             f"risco total ≤ {self.limits.max_total_open_risk_pct:g}% · correlacionado ≤ {self.limits.max_correlated_risk_pct:g}%"
             + (f" · corte 1ª/2ª metade em {self.split_time:%d/%m/%Y}" if self.split_time else ""), ""]
        # ---- quadro 1: retorno / DD por célula (período inteiro)
        L.append("QUADRO 1 — retorno líquido (DD máx) no período inteiro · linhas = confirmações mínimas · colunas = posições simultâneas")
        L.append("  conf " + "".join(f"{'N=' + str(n):>22}" for n in self.positions))
        for c in self.confirmations:
            row = f"  {c:>4} "
            for n in self.positions:
                x = self.cell(c, n)
                row += f"{'—':>22}" if x is None else f"{x.ret_pct(eq):>+8.1f}% ({x.full.max_dd_pct:>4.1f}%) n={x.full.admitted:<3}".rjust(22)
            L.append(row)
        L.append("")
        # ---- quadro 2: detalhe por confirmações (N = máximo testado; o funil sem limite de vagas)
        nmax = max(self.positions)
        L.append(f"QUADRO 2 — detalhe por nível de confirmações (carteira com N={nmax})")
        L.append(f"  {'conf':>4}{'oper.':>7}{'recus.':>7}{'acerto':>8}{'R bruto':>9}{'R líq.':>9}{'expect.':>9}{'lucro USD':>12}{'custos':>9}"
                 f"{'MFE':>7}{'MAE':>7}{'DD':>7}{'seq.perd':>9}{'h/oper':>8}")
        for c in self.confirmations:
            x = self.cell(c, nmax)
            if x is None:
                continue
            f = x.full
            L.append(f"  {c:>4}{f.admitted:>7}{f.refused:>7}{f.win_rate:>8.0%}{f.gross_r:>+9.1f}{f.net_r:>+9.1f}{f.expectancy:>+9.2f}"
                     f"{f.end_equity - eq:>+12,.0f}{f.costs_usd:>9,.0f}{f.mfe_avg:>7.2f}{f.mae_avg:>7.2f}{f.max_dd_pct:>6.1f}%{f.max_losing_streak:>9}{f.avg_hours:>8.1f}")
        L.append("")
        # ---- quadro 3: 1ª × 2ª metade por confirmações (N = nmax)
        L.append(f"QUADRO 3 — 1ª metade × 2ª metade (N={nmax}) · o parâmetro que só funciona numa metade não é parâmetro, é coincidência")
        L.append(f"  {'conf':>4}{'n 1ª':>7}{'E 1ª':>8}{'ret 1ª':>9}{'DD 1ª':>8}{'n 2ª':>7}{'E 2ª':>8}{'ret 2ª':>9}{'DD 2ª':>8}  leitura")
        for c in self.confirmations:
            x = self.cell(c, nmax)
            if x is None:
                continue
            a, b = x.first, x.second
            if a.admitted < MIN_N // 2 or b.admitted < MIN_N // 2:
                read = "amostra pequena"
            elif a.expectancy > 0 and b.expectancy > 0:
                read = "🟢 estável"
            elif a.expectancy > 0 > b.expectancy or b.expectancy > 0 > a.expectancy:
                read = "🟡 muda de sinal"
            else:
                read = "🔴 negativo nas duas"
            L.append(f"  {c:>4}{a.admitted:>7}{a.expectancy:>+8.2f}{x.ret_pct(eq, 'first'):>+8.1f}%{a.max_dd_pct:>7.1f}%"
                     f"{b.admitted:>7}{b.expectancy:>+8.2f}{x.ret_pct(eq, 'second'):>+8.1f}%{b.max_dd_pct:>7.1f}%  {read}")
        L.append("")
        # ---- quadro 4: por ativo e por tipo de evento (N = nmax), por confirmações
        L.append(f"QUADRO 4 — por ativo e por tipo de evento (N={nmax}) · n e expectancy")
        for c in self.confirmations:
            x = self.cell(c, nmax)
            if x is None or not x.full.admitted:
                continue
            syms = " · ".join(f"{k} n={v[0]} E={v[1] / v[0]:+.2f}" for k, v in sorted(x.full.by_symbol.items(), key=lambda kv: -kv[1][0]))
            kinds = " · ".join(f"{k} n={v[0]} E={v[1] / v[0]:+.2f}" for k, v in sorted(x.full.by_kind.items(), key=lambda kv: -kv[1][0])[:6])
            L.append(f"  conf {c}: ativos → {syms}")
            L.append(f"          eventos → {kinds}")
        L.append("")
        # ---- posições: para cada conf, N vale?
        L.append("POSIÇÕES SIMULTÂNEAS — para cada nível de confirmações, N=2/3/4 contra N=1 (período inteiro)")
        for c in self.confirmations:
            base = self.cell(c, min(self.positions))
            if base is None or base.full.admitted == 0:
                continue
            parts = []
            for n in self.positions[1:]:
                x = self.cell(c, n)
                if x is None:
                    continue
                d_ret = x.ret_pct(eq) - base.ret_pct(eq)
                d_dd = x.full.max_dd_pct - base.full.max_dd_pct
                if x.full.admitted == base.full.admitted and abs(d_ret) < 1e-9:
                    v = "⚪ igual"
                elif d_ret > 0 and d_dd <= max(2.0, base.full.max_dd_pct * 0.5):
                    v = "🟢 vale"
                elif d_ret > 0:
                    v = "🟡 ganha, DD cresce demais"
                else:
                    v = "🔴 não vale"
                parts.append(f"N={n}: {d_ret:+.1f}pts ret · {d_dd:+.1f}pts DD · {v}")
            L.append(f"  conf {c}: " + " | ".join(parts))
        L.append("")
        # ---- veredito
        w = self.winner()
        dflt = self.cell(self.default_confirmations, min(self.positions))
        L.append("VEREDITO (escolhido SÓ na 1ª metade por retorno − drawdown com n ≥ %d; conferido na 2ª metade):" % MIN_N)
        if w is None:
            L.append(f"  ⚪ nenhuma célula chegou a {MIN_N} operações na 1ª metade — a matriz ainda não tem amostra para escolher. "
                     "Mantém-se o padrão do autotune/ciclo de vida; a frequência tem de vir da camada de reação (tick/M1), não de afrouxar o funil.")
        else:
            s2 = w.second
            L.append(f"  1ª metade escolheu: {w.confirmations} confirmações + {w.positions} posições → ret {w.ret_pct(eq, 'first'):+.1f}% DD {w.first.max_dd_pct:.1f}% "
                     f"n={w.first.admitted}")
            L.append(f"  2ª metade (fora da amostra): ret {w.ret_pct(eq, 'second'):+.1f}% DD {s2.max_dd_pct:.1f}% n={s2.admitted} E={s2.expectancy:+.2f}R "
                     f"acerto {s2.win_rate:.0%} seq.perdas {s2.max_losing_streak}")
            if dflt is not None:
                L.append(f"  padrão ({self.default_confirmations} conf + 1 posição) na 2ª metade: ret {dflt.ret_pct(eq, 'second'):+.1f}% "
                         f"DD {dflt.second.max_dd_pct:.1f}% n={dflt.second.admitted} E={dflt.second.expectancy:+.2f}R")
            if s2.admitted < MIN_N:
                L.append(f"  ⚪ INCONCLUSIVO: {s2.admitted} operações fora da amostra (< {MIN_N}). Não muda nada no live.")
            elif s2.expectancy > 0 and (dflt is None or w.ret_pct(eq, "second") - s2.max_dd_pct >= dflt.ret_pct(eq, "second") - dflt.second.max_dd_pct):
                L.append(f"  🟢 SOBREVIVEU fora da amostra e supera o padrão. Candidato a: MIN_CONFIRMATIONS={w.confirmations} · MAX_ENTRIES_PER_CYCLE={w.positions}. "
                         "Ainda assim: entra em SOMBRA no autotune e sobe pelo ciclo de vida (20/30/50), nunca por este quadro sozinho.")
            elif s2.expectancy > 0:
                L.append("  🟡 positivo fora da amostra, mas não supera o padrão com folga de risco. Mantém o padrão; repete a matriz com mais história.")
            else:
                L.append("  🔴 a escolha da 1ª metade NÃO sobreviveu à 2ª. Sinal clássico de ajuste ao passado: não adotar.")
        L.append("")
        L.append("REGRAS: risco por operação fixo (escada por tier, nunca por este quadro) · custos reais descontados · mais posições só valem se o retorno "
                 "líquido sobe sem o DD subir desproporcionalmente · amostra < 20 = inconclusivo · H1 é seletivo por natureza — a frequência vem da camada TICK → M1 → Reaction Clock → lead/lag.")
        return "\n".join(L)

    def to_dict(self) -> dict:
        def pr(r: PortfolioResult) -> dict:
            return {"admitted": r.admitted, "refused": r.refused, "net_r": round(r.net_r, 3), "gross_r": round(r.gross_r, 3), "expectancy": round(r.expectancy, 3),
                    "win_rate": round(r.win_rate, 3), "end_equity": round(r.end_equity, 2), "costs_usd": round(r.costs_usd, 2), "max_dd_pct": round(r.max_dd_pct, 2),
                    "max_losing_streak": r.max_losing_streak, "avg_hours": round(r.avg_hours, 2), "mfe_avg": round(r.mfe_avg, 3), "mae_avg": round(r.mae_avg, 3),
                    "by_symbol": {k: [v[0], round(v[1], 3)] for k, v in r.by_symbol.items()}, "by_kind": {k: [v[0], round(v[1], 3)] for k, v in r.by_kind.items()}}
        w = self.winner()
        return {"generated": datetime.now(timezone.utc).isoformat(), "start": self.start, "end": self.end, "equity": self.equity, "risk_pct": self.risk_pct,
                "cost_r": self.cost_r, "min_n": MIN_N, "split_time": self.split_time.isoformat() if self.split_time else None,
                "winner": None if w is None else {"confirmations": w.confirmations, "positions": w.positions},
                "cells": [{"confirmations": x.confirmations, "positions": x.positions, "n_candidates": x.n_candidates,
                           "full": pr(x.full), "first": pr(x.first), "second": pr(x.second)} for x in self.cells]}

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)


def _split(trades: Sequence[SimTrade], split_time: datetime) -> tuple[list[SimTrade], list[SimTrade]]:
    return [t for t in trades if t.time < split_time], [t for t in trades if t.time >= split_time]


def build_matrix(backtesters: dict[str, Backtester], limits: PortfolioLimits, equity: float = 10000.0, risk_pct: float = 3.0, cost_r: float = 0.05,
                 confirmations: Sequence[int] = CONFIRMATIONS, positions: Sequence[int] = POSITIONS, strategy: str = "adaptive",
                 start: str = "", end: str = "", log: Optional[Callable[[str], None]] = None) -> MatrixReport:
    """Reprocessa cada mercado para cada nível de confirmações (só esse parâmetro muda) e monta a matriz conf × posições."""
    any_bt = next(iter(backtesters.values()))
    rep = MatrixReport(start, end, equity, risk_pct, cost_r, limits, tuple(confirmations), tuple(positions), default_confirmations=int(any_bt.cfg.min_confirmations))
    # corte temporal: meio do trecho utilizável do mercado mais longo (mesmo instante para todos)
    times = []
    for bt in backtesters.values():
        xau = bt.frame.xau
        if len(xau) > bt.warmup:
            times.append((xau[bt.warmup].time, xau[-1].time))
    t0, t1 = min(t[0] for t in times), max(t[1] for t in times)
    rep.split_time = t0 + (t1 - t0) / 2
    for c in confirmations:
        rows_by: dict[str, list] = {}
        for sym, bt in backtesters.items():
            cfg = cfg_with(bt.cfg, {"min_confirmations": int(c)})
            res = bt.run(None, None, cfg)
            rows_by[sym] = list(res.trade_rows)
            if log:
                log(f"conf {c} · {sym}: {len(res.trade_rows)} operações")
        trades = trades_from_rows(rows_by, strategy, cost_r=cost_r)
        first, second = _split(trades, rep.split_time)
        for n in positions:
            cell = Cell(int(c), int(n), simulate_portfolio(trades, n, limits, equity, risk_pct),
                        simulate_portfolio(first, n, limits, equity, risk_pct), simulate_portfolio(second, n, limits, equity, risk_pct), len(trades))
            rep.cells.append(cell)
            if log:
                log(f"conf {c} × N={n}: admitidas {cell.full.admitted} · ret {cell.ret_pct(equity):+.1f}% · DD {cell.full.max_dd_pct:.1f}%")
    return rep
