"""MARKET AI ENGINE 4.0 — LIVE EDGE REPORT (o teste definitivo).

Tabela diária, por mercado, a partir do que o sistema VIVEU (PAPER/AUTHORIZE/LIVE): as previsões foram
feitas antes do resultado, logo tudo aqui é fora da amostra por construção.
    OOS Trades · Expectancy · Probabilidade calibrada (declarada × observada) · Capture Rate · Status
Não precisamos acreditar que EURUSD é melhor. Os dados mostram.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from .selector import StatConfidence, statistical_confidence


@dataclass
class MarketEdge:
    symbol: str
    n_trades: int
    expectancy: float
    win_rate: float
    profit_factor: Optional[float]
    prob_declared: Optional[float]     # média da probabilidade declarada nas previsões direcionais resolvidas
    prob_observed: Optional[float]     # taxa de acerto observada dessas previsões
    n_predictions: int
    capture_rate: Optional[float]
    entry_rate: Optional[float]
    pnl_usd: float
    confidence: StatConfidence
    status: str                        # 🟢 🟡 🔴 ⚪
    status_reason: str

    @property
    def calibration_gap(self) -> Optional[float]:
        if self.prob_declared is None or self.prob_observed is None:
            return None
        return round(self.prob_observed - self.prob_declared, 3)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "n_trades": self.n_trades, "expectancy": self.expectancy, "win_rate": self.win_rate,
                "profit_factor": self.profit_factor, "prob_declared": self.prob_declared, "prob_observed": self.prob_observed,
                "n_predictions": self.n_predictions, "capture_rate": self.capture_rate, "entry_rate": self.entry_rate, "pnl_usd": self.pnl_usd,
                "confidence": self.confidence.level, "shrunk": self.confidence.shrunk, "status": self.status, "status_reason": self.status_reason}


def edge_status_from_stats(conf: StatConfidence, prob_observed: Optional[float], min_trades: int = 30) -> tuple[str, str]:
    if conf.n == 0:
        return "⚪", "sem operações resolvidas"
    if conf.n < min_trades:
        return "⚪", f"amostra insuficiente ({conf.n} < {min_trades})"
    if conf.level in ("HIGH", "MEDIUM") and conf.shrunk > 0.1 and (prob_observed is None or prob_observed >= 0.5):
        return "🟢", f"edge confirmado ({conf.level}, LB {conf.lower_bound:+.2f}R)"
    if conf.shrunk > 0:
        return "🟡", f"positivo mas ainda não confirmado (LB {conf.lower_bound:+.2f}R)"
    return "🔴", f"sem edge (E ajustada {conf.shrunk:+.2f}R)"


def market_edge(mem, symbol: str, min_trades: int = 30) -> MarketEdge:
    rows = mem.conn.execute("SELECT resultado_r, resultado_financeiro FROM trades WHERE ativo=? AND resultado_r IS NOT NULL", (symbol,)).fetchall()
    rs = [r["resultado_r"] for r in rows]
    pnl = sum((r["resultado_financeiro"] or 0.0) for r in rows)
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    conf = statistical_confidence(rs)
    preds = mem.conn.execute("SELECT probabilidade, resultado FROM predictions WHERE ativo=? AND resultado IN ('ACERTO','ERRO') AND previsao IN ('ALTA','BAIXA')", (symbol,)).fetchall()
    prob_decl = statistics.fmean(p["probabilidade"] for p in preds) if preds else None
    prob_obs = (sum(1 for p in preds if p["resultado"] == "ACERTO") / len(preds)) if preds else None
    opp = mem.opportunity_report(symbol=symbol)
    status, why = edge_status_from_stats(conf, prob_obs, min_trades)
    return MarketEdge(symbol, len(rs), round(conf.expectancy, 3), (len(wins) / len(rs)) if rs else 0.0, (round(pf, 2) if pf else None),
                      (round(prob_decl, 3) if prob_decl is not None else None), (round(prob_obs, 3) if prob_obs is not None else None), len(preds),
                      opp.capture_rate, opp.entry_rate, round(pnl, 2), conf, status, why)


@dataclass
class LiveEdgeReport:
    date: str
    rows: list[MarketEdge] = field(default_factory=list)
    equity: Optional[float] = None

    def ranked(self) -> list[MarketEdge]:
        return sorted(self.rows, key=lambda m: -m.confidence.shrunk)

    def render(self, width: int = 44) -> str:
        pct = lambda x: "n/d" if x is None else f"{x:.0%}"  # noqa: E731
        top, mid, bot = "╔" + "═" * width + "╗", "╠" + "═" * width + "╣", "╚" + "═" * width + "╝"
        line = lambda s: "║ " + s[: width - 2].ljust(width - 2) + " ║"  # noqa: E731
        out = [top, "║" + "MARKET AI — LIVE EDGE".center(width) + "║", "║" + f"{self.date} · fora da amostra por construção".center(width) + "║", mid]
        for m in self.ranked():
            out += [line(m.symbol), line(f"OOS Trades: {m.n_trades}"), line(f"Expectancy: {m.expectancy:+.2f}R  (ajustada {m.confidence.shrunk:+.2f}R)"),
                    line(f"Prob. calibrada: {pct(m.prob_observed)}" + (f" (decl. {pct(m.prob_declared)}, n={m.n_predictions})" if m.prob_declared is not None else "")),
                    line(f"Capture Rate: {pct(m.capture_rate)}  · Entry Rate: {pct(m.entry_rate)}"),
                    line(f"Win: {m.win_rate:.0%} · PF: {m.profit_factor if m.profit_factor is not None else 'n/d'} · {m.pnl_usd:+,.2f} USD"),
                    line(f"Status: {m.status}  {m.status_reason}"), mid]
        out[-1] = bot
        best = next((m for m in self.ranked() if m.status == "🟢"), None)
        out.append(f"Melhor edge vivido: {best.symbol} ({best.confidence.render()})" if best else "Nenhum mercado com edge confirmado ainda — continuar em PAPER.")
        if self.equity is not None:
            out.append(f"Capital: {self.equity:,.2f} USD")
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps({"date": self.date, "equity": self.equity, "rows": [m.to_dict() for m in self.rows]}, ensure_ascii=False)


def live_edge_report(mem, symbols: Sequence[str], now: Optional[datetime] = None, equity: Optional[float] = None, min_trades: int = 30) -> LiveEdgeReport:
    now = now or datetime.now(timezone.utc)
    return LiveEdgeReport(now.strftime("%Y-%m-%d"), [market_edge(mem, s, min_trades) for s in symbols], equity)


def edge_trend(history: Sequence[dict], symbol: str) -> str:
    """Evolução da expectancy ajustada de um mercado ao longo dos relatórios diários guardados."""
    pts = []
    for h in history:
        for r in h.get("rows", []):
            if r["symbol"] == symbol:
                pts.append((h["date"], r["shrunk"], r["n_trades"]))
    if not pts:
        return f"{symbol}: sem histórico de edge"
    return f"{symbol}: " + " → ".join(f"{d[5:]} {s:+.2f}R (n={n})" for d, s, n in pts[-8:])
