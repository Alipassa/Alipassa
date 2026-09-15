"""LEAD-LAG de um episódio (5.2) — "o ouro caiu, o euro demorou, todos seguiram o ouro": quem se moveu primeiro e quantos
minutos cada mercado levou para acompanhar, medido no M1 do broker em torno de um horário.

Para cada mercado: ATR horário de referência (M1 das 24 h anteriores reamostrado em H1) · preço em t0 · primeiro minuto em que
|Δ| ≥ limiar·ATR (cruzamento) · deslocamento máximo no horizonte · direção. A tabela sai ordenada pelo cruzamento: o primeiro é
o líder observado; o atraso dos demais é a matéria-prima do REACTION CLOCK. Com --record o episódio vira registros
`flow_<LÍDER>_<up|down>` na memória, para o relógio ao vivo usar como histórico (o histórico decide o parâmetro)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .models import Candle
from .technical import atr as _atr_fn


def resample_h1(candles: Sequence[Candle]) -> list[Candle]:
    out: list[Candle] = []
    cur: Optional[Candle] = None
    key = None
    for c in candles:
        k = c.time.replace(minute=0, second=0, microsecond=0)
        if k != key:
            if cur is not None:
                out.append(cur)
            cur, key = Candle(k, c.open, c.high, c.low, c.close, c.volume), k
        else:
            cur = Candle(k, cur.open, max(cur.high, c.high), min(cur.low, c.low), c.close, cur.volume + c.volume)
    if cur is not None:
        out.append(cur)
    return out


@dataclass
class EpisodeRow:
    symbol: str
    atr_h1: float
    p0: float
    direction: float                  # +1 / −1 / 0 no horizonte
    cross_min: Optional[float]        # minutos até |Δ| ≥ limiar·ATR
    max_move_atr: float               # maior deslocamento na direção final (ATR)
    end_move_atr: float               # deslocamento no fim do horizonte (ATR)
    lag_min: Optional[float] = None   # cruzamento − cruzamento do líder

    def row(self, t0: datetime) -> str:
        arrow = "↓" if self.direction < 0 else "↑" if self.direction > 0 else "→"
        cross = "não cruzou" if self.cross_min is None else f"{t0 + timedelta(minutes=self.cross_min):%H:%M} UTC"
        lag = "" if self.lag_min is None else (f"{self.lag_min:+.0f} min" if self.lag_min else "líder")
        return f"  {self.symbol:<8}{arrow:^4}{cross:>14}{lag:>10}{self.max_move_atr:>9.2f} ATR{self.end_move_atr:>+9.2f} ATR"


def lead_lag(candles_by_symbol: dict[str, Sequence[Candle]], t0: datetime, leader: str, threshold_atr: float = 0.5,
             window_min: int = 90) -> list[EpisodeRow]:
    rows: list[EpisodeRow] = []
    for sym, cs in candles_by_symbol.items():
        cs = sorted(cs, key=lambda c: c.time)
        before = [c for c in cs if t0 - timedelta(hours=24) <= c.time < t0]
        after = [c for c in cs if t0 <= c.time <= t0 + timedelta(minutes=window_min)]
        if len(before) < 30 or not after:
            continue
        a = _atr_fn(resample_h1(before)) or 0.0
        if a <= 0:
            continue
        p0 = before[-1].close
        end = (after[-1].close - p0) / a
        direction = 1.0 if end > 0.15 else -1.0 if end < -0.15 else 0.0
        sign = direction if direction else (1.0 if end >= 0 else -1.0)
        cross = None
        mx = 0.0
        for c in after:
            d = (c.close - p0) * sign / a
            mx = max(mx, d)
            if cross is None and abs((c.close - p0) / a) >= threshold_atr:
                cross = (c.time + timedelta(minutes=1) - t0).total_seconds() / 60.0   # barra conta no fechamento
        rows.append(EpisodeRow(sym, a, p0, direction, cross, round(mx, 2), round(end, 2)))
    lead = next((r for r in rows if r.symbol == leader), None)
    if lead is not None and lead.cross_min is not None:
        for r in rows:
            r.lag_min = None if r.cross_min is None else r.cross_min - lead.cross_min
    rows.sort(key=lambda r: (r.cross_min is None, r.cross_min or 0.0))
    return rows


def render_lead_lag(rows: Sequence[EpisodeRow], t0: datetime, leader: str, threshold_atr: float, window_min: int) -> str:
    lines = [f"⏱️ LEAD-LAG do episódio · t0 {t0:%Y-%m-%d %H:%M} UTC · cruzamento = |Δ| ≥ {threshold_atr:g} ATR horário · horizonte {window_min} min",
             f"  {'ativo':<8}{'dir':^4}{'cruzou em':>14}{'vs líder':>10}{'máx':>13}{'fim':>13}"]
    lines += [r.row(t0) for r in rows]
    crossed = [r for r in rows if r.cross_min is not None]
    if crossed:
        first = crossed[0]
        same = [r for r in crossed[1:] if r.direction == first.direction and r.direction != 0]
        lines.append(f"  observado: {first.symbol} cruzou primeiro ({first.cross_min:.0f} min após t0)" +
                     (f"; seguiram na mesma direção: " + ", ".join(f"{r.symbol} +{r.cross_min - first.cross_min:.0f} min" for r in same) if same else "; ninguém seguiu na mesma direção"))
        lead = next((r for r in rows if r.symbol == leader), None)
        if lead is not None and first.symbol != leader and lead.cross_min is not None:
            lines.append(f"  o líder declarado ({leader}) cruzou {lead.cross_min - first.cross_min:+.0f} min depois de {first.symbol} — no M1 o primeiro a cruzar foi {first.symbol}")
    lines.append("  leitura: 1 episódio é anedota; o relógio só confia no atraso com ≥ 5 casos do mesmo tipo (tiers do ciclo de vida).")
    return "\n".join(lines)


def records_from_episode(rows: Sequence[EpisodeRow], t0: datetime, leader: str, window_min: int) -> list:
    """Registros de reação para a memória: evento implícito flow_<líder>_<up|down> → cada seguidor (ponto no tempo: conhecido em t0 + horizonte)."""
    from .reaction import ReactionRecord
    lead = next((r for r in rows if r.symbol == leader), None)
    if lead is None or lead.direction == 0:
        return []
    kind = f"flow_{leader}_{'up' if lead.direction > 0 else 'down'}"
    out = []
    for r in rows:
        if r.symbol == leader:
            continue
        out.append(ReactionRecord(f"leadlag_{leader}_{t0:%Y%m%d%H%M}", kind, t0, r.symbol, lead.direction, r.cross_min, r.cross_min,
                                  None, r.max_move_atr, max(0.0, -r.end_move_atr) if r.direction == lead.direction else r.max_move_atr,
                                  (r.direction == lead.direction) if r.direction != 0 else None, {}, window_min, 1))
    return out
