"""REACTION ENGINE — ALTA RESOLUÇÃO (ticks / M1): segundos, dois horizontes e custo de execução.

Para explorar segundos, H1 não serve. Com ticks (bid/ask) ou M1 o motor mede, por evento e ativo:
  • movimento a T+1s, 5s, 10s, 30s, 60s, 300s (em ATR, na direção esperada);
  • SHORT-TERM REACTION (0–5 min): MFE/MAE;  FOLLOW-THROUGH (5–60 min): MFE/MAE;
  • lead-lag: quanto tempo depois do líder (USD/yields) o alvo reagiu;
e responde à pergunta que precede qualquer operação: "quando A se move assim, após este evento, qual a probabilidade
de B acompanhar, em quanto tempo e com qual magnitude?" (LeadLagStats).
O REACTION TRADE SIM só então pergunta se sobra algo depois de spread + slippage + latência: entrada em T(líder)+atraso,
saída em 5 min (reação) ou até 60 min com stop (continuação). Nada aqui afirma lucro: mede.
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .models import Candle
from .reaction import CONFIRM_ATR, FIRST_ATR, LEAD_THRESHOLDS, ReactionRecord

BUCKETS_SEC = (1, 5, 10, 30, 60, 300)
SHORT_SEC, FOLLOW_SEC = 300, 3600
DELAYS_SEC = (1, 5, 10, 30, 60, 300)
STOP_ATR = 0.5


@dataclass
class Quote:
    time: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


class PricePath:
    """Série bid/ask ordenada (ticks) ou M1 (mid = close, spread fixo)."""

    def __init__(self, quotes: Sequence[Quote], resolution_sec: float) -> None:
        self.q = sorted(quotes, key=lambda x: x.time)
        self.times = [x.time for x in self.q]
        self.resolution_sec = resolution_sec

    @classmethod
    def from_ticks(cls, ticks: Sequence[tuple[datetime, float, float]]) -> "PricePath":
        return cls([Quote(t, b, a) for t, b, a in ticks], 1.0)

    @classmethod
    def from_candles(cls, candles: Sequence[Candle], spread: float, minutes: int = 1) -> "PricePath":
        half = spread / 2
        return cls([Quote(c.time, c.close - half, c.close + half) for c in candles], minutes * 60.0)

    def at_or_before(self, t: datetime) -> Optional[Quote]:
        from bisect import bisect_right
        i = bisect_right(self.times, t)
        return self.q[i - 1] if i else None

    def between(self, t0: datetime, t1: datetime) -> list[Quote]:
        from bisect import bisect_left, bisect_right
        return self.q[bisect_right(self.times, t0): bisect_right(self.times, t1)]

    def atr_at(self, t: datetime, period: int = 14) -> float:
        """ATR aproximado: amplitude (máx−mín do mid) por hora nas últimas `period` horas antes de t."""
        ranges = []
        for k in range(period):
            a, b = t - timedelta(hours=k + 1), t - timedelta(hours=k)
            qs = self.between(a, b)
            if len(qs) >= 2:
                mids = [x.mid for x in qs]
                ranges.append(max(mids) - min(mids))
        return statistics.fmean(ranges) if ranges else 0.0


@dataclass
class HiResRecord:
    base: ReactionRecord
    move_at: dict[int, Optional[float]] = field(default_factory=dict)     # segundos → movimento em ATR (direção esperada)
    short_mfe: float = 0.0
    short_mae: float = 0.0
    follow_mfe: float = 0.0
    follow_mae: float = 0.0
    spread_atr: float = 0.0
    lead_lag_sec: Optional[float] = None       # 1ª reação do alvo − 1ª reação do líder mais rápido (s)
    lead_first_sec: Optional[float] = None

    @property
    def kind(self) -> str:
        return self.base.kind

    @property
    def target(self) -> str:
        return self.base.target


def measure_hires(event_id: str, kind: str, published_at: datetime, target: str, expected_dir: float, path: PricePath, atr: float,
                  leads: Optional[dict[str, PricePath]] = None, lead_dirs: Optional[dict[str, float]] = None) -> Optional[HiResRecord]:
    if expected_dir == 0 or not atr or atr <= 0:
        return None
    q0 = path.at_or_before(published_at)
    if q0 is None:
        return None
    sign = 1.0 if expected_dir > 0 else -1.0
    end = published_at + timedelta(seconds=FOLLOW_SEC)
    quotes = path.between(published_at, end)
    if not quotes:
        return None
    base = ReactionRecord(event_id, kind, published_at, target, sign, horizon_min=FOLLOW_SEC // 60, resolution_min=path.resolution_sec / 60.0)
    rec = HiResRecord(base)
    p0 = q0.mid
    mfe = mae = 0.0
    smfe = smae = fmfe = fmae = 0.0
    t_full = None
    spreads = []
    for q in quotes:
        sec = (q.time - published_at).total_seconds()
        move = sign * (q.mid - p0) / atr
        spreads.append(q.spread / atr)
        if move > mfe:
            mfe, t_full = move, sec / 60
        mae = max(mae, -move)
        if sec <= SHORT_SEC:
            smfe, smae = max(smfe, move), max(smae, -move)
        else:
            fmfe, fmae = max(fmfe, move), max(fmae, -move)
        if base.time_to_first is None and move >= FIRST_ATR:
            base.time_to_first = sec / 60
        if base.time_to_confirmation is None and move >= CONFIRM_ATR:
            base.time_to_confirmation = sec / 60
    for b in BUCKETS_SEC:
        q = path.at_or_before(published_at + timedelta(seconds=b))
        rec.move_at[b] = round(sign * (q.mid - p0) / atr, 3) if q and q.time > published_at else None
    base.max_move_atr, base.max_adverse_atr, base.time_to_full_move = round(mfe, 3), round(mae, 3), (t_full if mfe >= FIRST_ATR else None)
    base.direction_correct = (mfe >= CONFIRM_ATR and mfe > mae) if (mfe or mae) else None
    rec.short_mfe, rec.short_mae, rec.follow_mfe, rec.follow_mae = round(smfe, 3), round(smae, 3), round(fmfe, 3), round(fmae, 3)
    rec.spread_atr = round(statistics.fmean(spreads), 4) if spreads else 0.0
    firsts = []
    for name, lp in (leads or {}).items():
        d = (lead_dirs or {}).get(name, 0.0)
        base.lead_times[name] = None
        if d == 0 or lp is None:
            continue
        l0 = lp.at_or_before(published_at)
        if l0 is None:
            continue
        thr = LEAD_THRESHOLDS.get(name, 0.0)
        for q in lp.between(published_at, end):
            delta = (q.mid / l0.mid - 1) * 100 if name == "USD" else (q.mid - l0.mid) * 100
            if (1.0 if d > 0 else -1.0) * delta >= thr:
                base.lead_times[name] = (q.time - published_at).total_seconds() / 60
                firsts.append((q.time - published_at).total_seconds())
                break
    if firsts:
        rec.lead_first_sec = min(firsts)
        if base.time_to_first is not None:
            rec.lead_lag_sec = round(base.time_to_first * 60 - rec.lead_first_sec, 1)
    return rec


# --------------------------------------------------------------------------- lead-lag: P(B acompanha | A reagiu)
@dataclass
class LeadLagRow:
    kind: str
    target: str
    n: int
    n_lead: int
    p_confirm_given_lead: Optional[float]
    p_confirm_no_lead: Optional[float]
    median_lag_sec: Optional[float]
    median_short_mfe: float
    median_follow_mfe: float
    median_short_mae: float
    median_spread_atr: float
    buckets: dict[int, Optional[float]]

    def row(self) -> str:
        f = lambda v, fmt: "  n/d" if v is None else fmt.format(v)  # noqa: E731
        b = " ".join(f"{('n/d' if self.buckets.get(k) is None else f'{self.buckets[k]:+.2f}'):>6}" for k in BUCKETS_SEC)
        return (f"{self.kind:<20}{self.target:<8}{self.n:>4}{self.n_lead:>6}{f(self.p_confirm_given_lead, '{:>7.0%}'):>8}{f(self.p_confirm_no_lead, '{:>7.0%}'):>8}"
                f"{f(self.median_lag_sec, '{:>7.0f}'):>8}{self.median_short_mfe:>7.2f}{self.median_short_mae:>7.2f}{self.median_follow_mfe:>7.2f}{self.median_spread_atr:>8.3f}  {b}")


class LeadLagStats:
    def __init__(self, records: Sequence[HiResRecord]) -> None:
        self.records = list(records)

    def rows(self) -> list[LeadLagRow]:
        groups: dict[tuple[str, str], list[HiResRecord]] = {}
        for r in self.records:
            groups.setdefault((r.kind, r.target), []).append(r)
        out = []
        for (kind, target), rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            with_lead = [r for r in rs if r.lead_first_sec is not None]
            no_lead = [r for r in rs if r.lead_first_sec is None]
            pc = lambda xs: (sum(1 for r in xs if r.base.direction_correct) / len(xs)) if xs else None  # noqa: E731
            lags = [r.lead_lag_sec for r in with_lead if r.lead_lag_sec is not None]
            med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
            buckets = {}
            for b in BUCKETS_SEC:
                xs = [r.move_at[b] for r in rs if r.move_at.get(b) is not None]
                buckets[b] = statistics.median(xs) if xs else None
            out.append(LeadLagRow(kind, target, len(rs), len(with_lead), pc(with_lead), pc(no_lead), statistics.median(lags) if lags else None,
                                  med([r.short_mfe for r in rs]), med([r.follow_mfe for r in rs]), med([r.short_mae for r in rs]),
                                  med([r.spread_atr for r in rs]), buckets))
        return out

    def render(self) -> str:
        rows = self.rows()
        if not rows:
            return "LEAD-LAG: sem eventos medidos"
        res = min((r.base.resolution_min for r in self.records), default=1.0)
        head = (f"{'evento':<20}{'alvo':<8}{'n':>4}{'c/líd':>6}{'P(B|A)':>8}{'P(B|¬A)':>8}{'lag s':>8}{'MFE5m':>7}{'MAE5m':>7}{'MFE60':>7}{'spread':>8}  " +
                " ".join(f"{'T+' + str(b) + 's':>6}" for b in BUCKETS_SEC))
        lines = ["🔬 LEAD-LAG — quando o líder (USD/yields) se move após o evento, o alvo acompanha? (medianas em ATR)", head] + [r.row() for r in rows]
        lines.append(f"  resolução {res * 60:.0f} s · P(B|A) = P(alvo confirma ≥ 0,40 ATR | líder reagiu) · lag = 1ª reação do alvo − 1ª do líder · spread em ATR")
        lines.append("  amostra < 20 por linha = inconclusivo; T+Ns = movimento mediano do alvo N segundos após a publicação")
        return "\n".join(lines)


# --------------------------------------------------------------------------- REACTION TRADE SIM: sobra algo depois do custo?
@dataclass
class TradeSimRow:
    kind: str
    target: str
    delay_sec: int
    n: int
    gross_short: float      # movimento médio bruto (mid→mid) até 5 min, em ATR
    cost: float             # spread + slippage médio, em ATR (ida e volta)
    net_short: float        # expectancy líquida SHORT (ATR)
    win_short: float
    net_follow: float       # expectancy líquida FOLLOW-THROUGH (stop 0,5 ATR, saída até 60 min) (ATR)
    win_follow: float

    @property
    def net_short_r(self) -> float:
        return self.net_short / STOP_ATR

    @property
    def net_follow_r(self) -> float:
        return self.net_follow / STOP_ATR

    def row(self) -> str:
        tag = "⚪" if self.n < 20 else ("🟢" if max(self.net_short, self.net_follow) > 0.02 else "🔴")
        return (f"{self.kind:<20}{self.target:<8}{self.delay_sec:>6}{self.n:>5}{self.gross_short:>+8.3f}{self.cost:>7.3f}{self.net_short:>+8.3f}{self.net_short_r:>+7.2f}R"
                f"{self.win_short:>6.0%}{self.net_follow:>+8.3f}{self.net_follow_r:>+7.2f}R{self.win_follow:>6.0%}  {tag}")


class ReactionTradeSim:
    """Entra quando o líder reagiu (T_líder + atraso + latência), na direção esperada, pagando ask/bid + slippage.
    SHORT: sai em T_entrada + 5 min. FOLLOW: mantém até 60 min com stop em −0,5 ATR (mid), senão sai no fim."""

    def __init__(self, slippage_atr: float = 0.02, latency_sec: float = 0.5) -> None:
        self.slippage_atr, self.latency_sec = slippage_atr, latency_sec

    def simulate(self, rec: HiResRecord, path: PricePath, atr: float, delay_sec: int) -> Optional[dict]:
        if rec.lead_first_sec is None:
            return None
        t_in = rec.base.published_at + timedelta(seconds=rec.lead_first_sec + delay_sec + self.latency_sec)
        q_in = path.at_or_before(t_in)
        if q_in is None or q_in.time <= rec.base.published_at:
            return None
        sign = rec.base.expected_dir
        entry = (q_in.ask if sign > 0 else q_in.bid) + sign * self.slippage_atr * atr
        t_short = rec.base.published_at + timedelta(seconds=SHORT_SEC) if rec.lead_first_sec + delay_sec < SHORT_SEC else t_in + timedelta(seconds=SHORT_SEC)
        q_s = path.at_or_before(t_short)
        if q_s is None:
            return None
        exit_s = (q_s.bid if sign > 0 else q_s.ask) - sign * self.slippage_atr * atr
        net_short = sign * (exit_s - entry) / atr
        gross_short = sign * (q_s.mid - q_in.mid) / atr
        # follow-through
        end = rec.base.published_at + timedelta(seconds=FOLLOW_SEC)
        stop = entry - sign * STOP_ATR * atr
        exit_f = None
        for q in path.between(t_in, end):
            if (sign > 0 and q.bid <= stop) or (sign < 0 and q.ask >= stop):
                exit_f = stop - sign * self.slippage_atr * atr
                break
        if exit_f is None:
            q_e = path.at_or_before(end)
            exit_f = ((q_e.bid if sign > 0 else q_e.ask) - sign * self.slippage_atr * atr) if q_e else entry
        net_follow = sign * (exit_f - entry) / atr
        return {"gross_short": gross_short, "cost": gross_short - net_short, "net_short": net_short, "net_follow": net_follow}

    def table(self, records: Sequence[tuple[HiResRecord, PricePath, float]], delays: Sequence[int] = DELAYS_SEC) -> list[TradeSimRow]:
        groups: dict[tuple[str, str], list[tuple[HiResRecord, PricePath, float]]] = {}
        for rec, path, atr in records:
            groups.setdefault((rec.kind, rec.target), []).append((rec, path, atr))
        out = []
        for (kind, target), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            for d in delays:
                sims = [s for s in (self.simulate(rec, path, atr, d) for rec, path, atr in items) if s]
                if not sims:
                    continue
                n = len(sims)
                m = lambda k: statistics.fmean(s[k] for s in sims)  # noqa: E731
                out.append(TradeSimRow(kind, target, d, n, m("gross_short"), m("cost"), m("net_short"), sum(1 for s in sims if s["net_short"] > 0) / n,
                                       m("net_follow"), sum(1 for s in sims if s["net_follow"] > 0) / n))
        return out

    def render(self, rows: Sequence[TradeSimRow]) -> str:
        if not rows:
            return "REACTION TRADE SIM: nenhum evento com líder reagido (nada a simular)"
        head = f"{'evento':<20}{'alvo':<8}{'atraso':>6}{'n':>5}{'bruto5m':>8}{'custo':>7}{'líq.5m':>8}{'':>8}{'win':>6}{'líq.60':>8}{'':>8}{'win':>6}"
        lines = [f"💸 REACTION TRADE SIM — entrada em T(líder)+atraso+latência {self.latency_sec:.1f}s, slippage {self.slippage_atr:.2f} ATR, ask/bid reais (ATR e R = 0,5 ATR)",
                 head] + [r.row() for r in rows]
        lines.append("  SHORT = sai 5 min após a publicação (ou após a entrada, se entrou depois); FOLLOW = stop 0,5 ATR, saída até 60 min")
        lines.append("  ⚪ n < 20 inconclusivo · 🟢 líquido > 0,02 ATR · 🔴 custo consome o movimento. Só configurações 🟢 com n ≥ 20 são candidatas a operar.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- CSV (ticks / M1 exportados do MT5)
def save_ticks(ticks: Sequence[tuple[datetime, float, float]], path: str) -> int:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "bid", "ask"])
        for t, b, a in ticks:
            w.writerow([t.isoformat(), f"{b:.5f}", f"{a:.5f}"])
    return len(ticks)


def load_ticks(path: str) -> list[tuple[datetime, float, float]]:
    out = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            out.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["bid"]), float(r["ask"])))
    return out


def save_candles(candles: Sequence[Candle], path: str) -> int:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume])
    return len(candles)
