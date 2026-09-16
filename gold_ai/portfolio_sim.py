"""PORTFOLIO SIM (5.2) — 1 × 2 × 3 × 4 posições simultâneas, com as MESMAS operações fora da amostra de cada mercado.

Pergunta: permitir 2–4 posições ao mesmo tempo aumenta o retorno líquido (spread, slippage, correlação) sem drawdown
desproporcional? As operações OOS de cada mercado (walk-forward) são postas em ordem cronológica; para cada N, a carteira
admite uma operação só se, no instante da entrada, houver vaga (< N abertas), o risco total couber e o risco CORRELACIONADO
(mesma tese: dólar, risco, petróleo — correlação assinada pela direção) couber. O que não coube é registrado como "recusada".
Risco fixo em % do capital em cada operação; capital composto. Saída pela estratégia escolhida (exit_time das simulações)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .models import Direction
from .selector import OpenExposure, PortfolioExposureEngine, PortfolioLimits


@dataclass
class SimTrade:
    time: datetime
    symbol: str
    direction: Direction
    r: float
    exit_time: datetime
    cost_r: float = 0.0
    mfe: float = 0.0            # excursão máxima a favor (R, stop inicial)
    mae: float = 0.0            # excursão máxima contra (R)
    kind: str = ""              # tipo do evento identificado ≤ 4 h antes ("" = sem evento)


@dataclass
class PortfolioResult:
    max_positions: int
    admitted: int
    refused: int
    refused_reasons: dict = field(default_factory=dict)
    net_r: float = 0.0
    expectancy: float = 0.0
    win_rate: float = 0.0
    end_equity: float = 0.0
    max_dd_pct: float = 0.0
    peak_concurrent: int = 0
    gross_r: float = 0.0                 # R antes do custo
    costs_usd: float = 0.0               # custo total (spread + slippage) em USD
    max_losing_streak: int = 0
    avg_hours: float = 0.0               # duração média (entrada → saída)
    mfe_avg: float = 0.0
    mae_avg: float = 0.0
    by_symbol: dict = field(default_factory=dict)   # sym → [n, soma R]
    by_kind: dict = field(default_factory=dict)     # tipo de evento → [n, soma R]

    def row(self, start_equity: float) -> str:
        ret = (self.end_equity / start_equity - 1) * 100 if start_equity else 0.0
        return (f"  {self.max_positions:>3}{self.admitted:>10}{self.refused:>10}{self.expectancy:>+10.2f}R{self.net_r:>+10.1f}R{self.win_rate:>8.0%}"
                f"{ret:>+9.1f}%{self.max_dd_pct:>8.1f}%{self.peak_concurrent:>6}")


def trades_from_rows(rows_by_symbol: dict, strategy: str = "adaptive", default: str = "3R", cost_r: float = 0.0) -> list[SimTrade]:
    out = []
    for sym, rows in rows_by_symbol.items():
        for row in rows:
            r = row["results"].get(strategy, row["results"].get(default))
            if r is None:
                continue
            exits = row.get("exits") or {}
            et = exits.get(strategy) or exits.get(default) or (row["time"] + timedelta(hours=4))
            d = Direction.ALTA if str(row.get("direction", "ALTA")).upper().startswith("ALTA") else Direction.BAIXA
            prof = row.get("profile")
            out.append(SimTrade(row["time"], sym, d, float(r) - cost_r, et, cost_r, float(getattr(prof, "max_r_before_stop", 0.0) or 0.0),
                                float(getattr(prof, "mae_r", 0.0) or 0.0), str(row.get("event_kind", "") or "")))
    return sorted(out, key=lambda t: t.time)


def simulate_portfolio(trades: Sequence[SimTrade], max_positions: int, limits: PortfolioLimits, equity: float = 10000.0, risk_pct: float = 3.0,
                       corr_table: Optional[dict] = None) -> PortfolioResult:
    lim = PortfolioLimits(limits.max_total_open_risk_pct, limits.max_correlated_risk_pct, max_positions, limits.max_asset_exposure, limits.correlation_threshold)
    engine = PortfolioExposureEngine(lim, corr_table)
    res = PortfolioResult(max_positions, 0, 0)
    open_: list[tuple[SimTrade, float]] = []          # (trade, risco em USD)
    eq, peak = equity, equity
    streak, hours, mfe, mae = 0, 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x.time):
        # fecha o que já saiu antes desta entrada (resultado realizado no fechamento)
        still = []
        for tr, risk in sorted(open_, key=lambda x: x[0].exit_time):
            if tr.exit_time <= t.time:
                eq = round(eq + tr.r * risk, 2)
                peak = max(peak, eq)
                res.max_dd_pct = max(res.max_dd_pct, (peak - eq) / peak * 100 if peak else 0.0)
            else:
                still.append((tr, risk))
        open_ = still
        risk = eq * risk_pct / 100.0
        reasons = engine.check(t.symbol, t.direction, risk, [OpenExposure(tr.symbol, tr.direction, rk) for tr, rk in open_], eq)
        if reasons:
            res.refused += 1
            key = reasons[0].split(" ")[0] + " " + reasons[0].split(" ")[1]
            res.refused_reasons[key] = res.refused_reasons.get(key, 0) + 1
            continue
        open_.append((t, risk))
        res.admitted += 1
        res.net_r += t.r
        res.gross_r += t.r + t.cost_r
        res.costs_usd += t.cost_r * risk
        res.peak_concurrent = max(res.peak_concurrent, len(open_))
        res.win_rate += 1 if t.r > 0 else 0
        streak = streak + 1 if t.r <= 0 else 0
        res.max_losing_streak = max(res.max_losing_streak, streak)
        hours += max(0.0, (t.exit_time - t.time).total_seconds() / 3600.0)
        mfe += t.mfe
        mae += t.mae
        res.by_symbol.setdefault(t.symbol, [0, 0.0])
        res.by_symbol[t.symbol][0] += 1
        res.by_symbol[t.symbol][1] += t.r
        k = t.kind or "sem evento"
        res.by_kind.setdefault(k, [0, 0.0])
        res.by_kind[k][0] += 1
        res.by_kind[k][1] += t.r
    for tr, risk in sorted(open_, key=lambda x: x[0].exit_time):
        eq = round(eq + tr.r * risk, 2)
        peak = max(peak, eq)
        res.max_dd_pct = max(res.max_dd_pct, (peak - eq) / peak * 100 if peak else 0.0)
    res.end_equity = eq
    res.expectancy = res.net_r / res.admitted if res.admitted else 0.0
    res.win_rate = res.win_rate / res.admitted if res.admitted else 0.0
    if res.admitted:
        res.avg_hours, res.mfe_avg, res.mae_avg = hours / res.admitted, mfe / res.admitted, mae / res.admitted
    return res


def render_portfolio_sim(results: Sequence[PortfolioResult], start_equity: float, risk_pct: float, limits: PortfolioLimits, n_trades: int, days: float) -> str:
    lines = [f"🧺 PORTFOLIO SIM — {n_trades} operações OOS em {days:.0f} dias · risco {risk_pct:g}% por operação, composto · "
             f"risco total ≤ {limits.max_total_open_risk_pct:g}% · correlacionado ≤ {limits.max_correlated_risk_pct:g}% · 1 posição por ativo",
             f"  {'N':>3}{'admitidas':>10}{'recusadas':>10}{'expect.':>11}{'R líq.':>11}{'acerto':>8}{'retorno':>9}{'DD máx':>9}{'pico':>6}"]
    lines += [r.row(start_equity) for r in results]
    base = results[0] if results else None
    for r in results[1:]:
        if base is None or base.admitted == 0:
            break
        d_ret = (r.end_equity - base.end_equity) / start_equity * 100
        d_dd = r.max_dd_pct - base.max_dd_pct
        if r.admitted == base.admitted and abs(d_ret) < 1e-9:
            verdict = "⚪ igual (nenhuma operação simultânea no período)"
        else:
            verdict = "🟢 vale" if d_ret > 0 and d_dd <= max(2.0, base.max_dd_pct * 0.5) else "🟡 ganho sem folga de risco" if d_ret > 0 else "🔴 não vale"
        lines.append(f"  N={r.max_positions} vs N=1: retorno {d_ret:+.1f} pontos · DD {d_dd:+.1f} pontos · {verdict}")
    if results and results[-1].refused_reasons:
        top = sorted(results[-1].refused_reasons.items(), key=lambda kv: -kv[1])[:3]
        lines.append("  recusas (N máximo): " + " · ".join(f"{k} {v}" for k, v in top))
    lines.append(f"  leitura: mais posições só valem se o retorno líquido subir sem o drawdown subir desproporcionalmente; amostra < 20 operações = inconclusivo.")
    return "\n".join(lines)
