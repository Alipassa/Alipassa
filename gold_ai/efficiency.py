"""EFICIÊNCIA DO DIA (5.2) — o que o robô viu, o que fez e o que deixou na mesa, em números e por mercado.

Fontes: tabela `decisions` (uma linha por análise: nível alcançado, etapa em que caiu, motivo, R hipotético das que não entraram)
e tabela `trades` (entradas, resultado em R e em USD, lote, risco planejado × real, duração, motivo de saída).
Perguntas que responde todo dia: quantas oportunidades existiram (episódios em SETUP/OPPORTUNITY)? quantas viraram entrada?
quanto rendeu? quanto o lote travado ou um bloqueio custou? qual foi o motivo mais comum de NÃO entrar? qual seria o R das
oportunidades não operadas (hipótese 3R / stop 1,2 ATR — proxy, não promessa)?"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

LEVEL_RANK = {"NONE": 0, "WATCH": 1, "SETUP": 2, "OPPORTUNITY": 3, "EXECUTION": 4}


@dataclass
class MarketDay:
    symbol: str
    analyses: int = 0
    episodes_setup: int = 0          # horas distintas com nível ≥ SETUP (vantagem passou)
    episodes_opportunity: int = 0    # horas distintas com nível ≥ OPPORTUNITY (sinal operacional)
    entries: int = 0
    closed: int = 0
    r_sum: float = 0.0
    usd_sum: float = 0.0
    wins: int = 0
    minutes_sum: float = 0.0
    risk_planned: float = 0.0
    risk_real: float = 0.0
    missed_n: int = 0                # SETUP/OPPORTUNITY não operados com R hipotético resolvido
    missed_r: float = 0.0
    reasons: Counter = field(default_factory=Counter)
    exits: Counter = field(default_factory=Counter)

    @property
    def win_rate(self) -> float:
        return self.wins / self.closed if self.closed else 0.0

    @property
    def capture(self) -> Optional[float]:
        return (self.entries / self.episodes_opportunity) if self.episodes_opportunity else None


@dataclass
class DayReport:
    day: datetime
    markets: list[MarketDay]
    equity_start: Optional[float] = None
    equity_end: Optional[float] = None

    def totals(self) -> MarketDay:
        t = MarketDay("TOTAL")
        for m in self.markets:
            for k in ("analyses", "episodes_setup", "episodes_opportunity", "entries", "closed", "r_sum", "usd_sum", "wins", "minutes_sum",
                      "risk_planned", "risk_real", "missed_n", "missed_r"):
                setattr(t, k, getattr(t, k) + getattr(m, k))
            t.reasons.update(m.reasons)
            t.exits.update(m.exits)
        return t

    def render(self) -> str:
        t = self.totals()
        L = [f"📆 EFICIÊNCIA DO DIA {self.day:%d/%m/%Y} (UTC) — o que o robô viu, fez e deixou na mesa"]
        if self.equity_start is not None and self.equity_end is not None:
            L.append(f"   capital {self.equity_start:,.2f} → {self.equity_end:,.2f} USD ({self.equity_end - self.equity_start:+,.2f})")
        L.append(f"  {'ativo':<8}{'anál.':>6}{'SETUP':>7}{'OPORT.':>7}{'entr.':>6}{'captura':>8}{'fech.':>6}{'R':>7}{'USD':>10}{'acerto':>7}{'min/op':>7}{'não op. R':>10}")
        for m in self.markets + [t]:
            cap = "n/d" if m.capture is None else f"{m.capture:.0%}"
            mins = f"{m.minutes_sum / m.closed:.0f}" if m.closed else "—"
            missed = f"{m.missed_r:+.1f} ({m.missed_n})" if m.missed_n else "—"
            L.append(f"  {m.symbol:<8}{m.analyses:>6}{m.episodes_setup:>7}{m.episodes_opportunity:>7}{m.entries:>6}{cap:>8}{m.closed:>6}{m.r_sum:>+7.2f}{m.usd_sum:>+10.2f}"
                     f"{m.win_rate:>7.0%}{mins:>7}{missed:>10}")
        if t.risk_planned > 0:
            ratio = t.risk_real / t.risk_planned
            flag = " ⚠️ lote travado (MAX_LOT / máx. da corretora): o resultado em USD não reflete o risco planejado" if ratio < 0.5 else ""
            L.append(f"  risco planejado {t.risk_planned:,.0f} USD × real {t.risk_real:,.0f} USD ({ratio:.0%}){flag}")
        if t.reasons:
            top = " · ".join(f"{k} ×{v}" for k, v in t.reasons.most_common(4))
            L.append(f"  por que NÃO entrou (mais comuns): {top}")
        if t.exits:
            L.append("  saídas: " + " · ".join(f"{k} ×{v}" for k, v in t.exits.most_common(4)))
        L.append("  leitura: captura = entradas ÷ episódios com sinal operacional · 'não op. R' = R hipotético (3R, stop 1,2 ATR) das oportunidades "
                 "não operadas — proxy para 'deixado na mesa', não promessa · o que muda parâmetro é o autotune, não um dia")
        return "\n".join(L)


def _short_reason(txt: str) -> str:
    t = (txt or "").strip()
    for key in ("mercado lateral", "faltou:", "BLOQUEADA", "exposição", "lote", "kill", "PAUSE", "meta", "perda diária", "confiança", "evidência",
                "conflit", "spread", "risco", "sem vantagem", "não é operacional", "PRIORIDADE"):
        if key.lower() in t.lower():
            i = t.lower().find(key.lower())
            return t[i:i + 44].split("\n")[0].strip(" —·:")
    return (t[:44] or "sem motivo").strip()


def day_report(mem, day: datetime, symbols: Sequence[str]) -> DayReport:
    d0 = day.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    d1 = d0 + timedelta(days=1)
    markets = []
    for sym in symbols:
        m = MarketDay(sym)
        hours_setup, hours_opp = set(), set()
        for d in mem.decisions(since=d0, symbol=sym):
            if d.time >= d1:
                continue
            m.analyses += 1
            level = str(getattr(d, "level", "") or "").upper()
            stage = str(getattr(d, "stage", "") or "").upper()
            rank = LEVEL_RANK.get(level, 0)
            # sem 'level' persistido, inferir pelo que foi gravado: entrada = EXECUTION; etapa NONE/None com |score| alto = pelo menos SETUP
            if d.action == "ENTRADA":
                rank = 4
            elif rank == 0 and stage in ("SINAL", "EXECUCAO", "EXECUÇÃO", "OPORTUNIDADE"):
                rank = 3
            elif rank == 0 and abs(d.score) >= 25:
                rank = 2
            h = d.time.replace(minute=0, second=0, microsecond=0)
            if rank >= 2:
                hours_setup.add(h)
            if rank >= 3:
                hours_opp.add(h)
            if d.action == "ENTRADA":
                m.entries += 1
            else:
                if rank >= 2:
                    m.reasons[_short_reason(d.reason)] += 1
                    if d.hypothetical_r is not None:
                        m.missed_n += 1
                        m.missed_r += float(d.hypothetical_r)
        m.episodes_setup, m.episodes_opportunity = len(hours_setup), len(hours_opp)
        for r in mem.trades_between(d0, d1, sym):
            if r.get("resultado_r") is not None:
                m.closed += 1
                m.r_sum += float(r["resultado_r"])
                m.wins += 1 if float(r["resultado_r"]) > 0 else 0
                m.usd_sum += float(r.get("resultado_financeiro") or 0.0)
                m.minutes_sum += float(r.get("tempo_operacao_min") or 0.0)
                m.exits[str(r.get("motivo_saida") or "?")[:24]] += 1
            cap, pct, risk = r.get("capital"), r.get("risco_pct"), r.get("risco_usd")
            if cap and pct:
                m.risk_planned += float(cap) * float(pct) / 100.0
                m.risk_real += float(risk or 0.0)
        markets.append(m)
    rows = [(t, cap) for t, cap, _, _ in mem.account_rows() if d0 <= t < d1] if hasattr(mem, "account_rows") else []
    rep = DayReport(d0, markets)
    if rows:
        rep.equity_start, rep.equity_end = rows[0][1], rows[-1][1]
    return rep
