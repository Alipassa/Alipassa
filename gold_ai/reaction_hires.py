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
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

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
        """Candle.time é a ABERTURA: o fecho só existe `minutes` depois → carimbo no fecho (anti look-ahead)."""
        half = spread / 2
        return cls([Quote(c.time + timedelta(minutes=minutes), c.close - half, c.close + half) for c in candles], minutes * 60.0)

    def resample(self, minutes: int = 1) -> "PricePath":
        """Ticks → barras de `minutes` (mid = último, spread = média): para checar se a relação sobrevive à resolução de minuto."""
        buckets: dict[datetime, list[Quote]] = {}
        for q in self.q:
            key = q.time.replace(second=0, microsecond=0)
            key = key.replace(minute=(key.minute // minutes) * minutes)
            buckets.setdefault(key, []).append(q)
        out = []
        for key in sorted(buckets):
            qs = buckets[key]
            sp = statistics.fmean(x.spread for x in qs)
            mid = qs[-1].mid
            out.append(Quote(key + timedelta(minutes=minutes), mid - sp / 2, mid + sp / 2))     # carimbo no FECHO da barra (só passado)
        return PricePath(out, minutes * 60.0)

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


SCHEDULED_KINDS = frozenset({"cpi", "core_cpi", "pce", "core_pce", "ppi", "nfp", "unemployment", "jobless_claims", "gdp", "earnings", "retail_sales",
                             "ism", "ism_services", "oil_inventories", "fomc", "ecb", "boj", "consumer_confidence", "durable_goods", "housing"})


def event_family(kind: str) -> str:
    """Famílias para agregar amostra: 'Σ agendado' (releases com hora exata: NFP, CPI, PPI…) × 'Σ manchete' (GDELT/notícia: geopolítica,
    China, petróleo, banco central…). Um rótulo composto 'nfp+earnings' é agendado se qualquer parte for release."""
    parts = [x.strip().lower() for x in str(kind).split("+")]
    return "Σ agendado" if any(x in SCHEDULED_KINDS for x in parts) else "Σ manchete"


def _grouped(records, key_kind):
    groups: dict[tuple[str, str], list] = {}
    for item in records:
        rec = item[0] if isinstance(item, tuple) else item
        groups.setdefault((key_kind(rec), rec.target), []).append(item)
    return groups


class LeadLagStats:
    def __init__(self, records: Sequence[HiResRecord]) -> None:
        self.records = list(records)

    def rows(self) -> list[LeadLagRow]:
        groups = _grouped(self.records, lambda r: r.kind)
        pooled = _grouped(self.records, lambda r: event_family(r.kind))     # linhas Σ: a mesma publicação conta uma vez (dedupe feito antes)
        groups = {**{k: v for k, v in pooled.items()}, **groups}
        out = []
        for (kind, target), rs in sorted(groups.items(), key=lambda kv: (0 if kv[0][0].startswith("Σ") else 1, -len(kv[1]), kv[0])):
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
        lines.append("  Σ agendado = todos os releases com hora exata (NFP, CPI, PPI, PCE, claims…) somados · Σ manchete = notícias GDELT somadas · uma publicação = um caso")
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
        t_short = max(t_in, rec.base.published_at + timedelta(seconds=SHORT_SEC)) if rec.lead_first_sec + delay_sec < SHORT_SEC else t_in + timedelta(seconds=SHORT_SEC)
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
        groups = _grouped(records, lambda r: r.kind)
        groups = {**_grouped(records, lambda r: event_family(r.kind)), **groups}
        out = []
        for (kind, target), items in sorted(groups.items(), key=lambda kv: (0 if kv[0][0].startswith("Σ") else 1, -len(kv[1]), kv[0])):
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


def parse_time(value: str) -> Optional[datetime]:
    """ISO-8601 tolerante: devolve None para linha truncada/corrompida ('2026-05-1', vazio) em vez de derrubar a etapa."""
    try:
        t = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def load_ticks(path: str, log: Optional[Callable[[str], None]] = None) -> list[tuple[datetime, float, float]]:
    """Ticks de um CSV time,bid,ask. Linhas inválidas (tempo truncado, número faltando — típico de export interrompido) são
    puladas e contadas; o arquivo continua utilizável. Ordena por tempo e remove duplicatas exatas."""
    out, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            t = parse_time(r.get("time"))
            try:
                b, a = float(r.get("bid") or ""), float(r.get("ask") or "")
            except ValueError:
                t = None
            if t is None:
                bad += 1
                continue
            out.append((t, b, a))
    if bad and log:
        log(f"[aviso] {os.path.basename(path)}: {bad} linha(s) inválida(s) ignorada(s) — arquivo de ticks com trecho corrompido/interrompido; {len(out)} ticks válidos")
    out.sort(key=lambda x: x[0])
    return out


def save_candles(candles: Sequence[Candle], path: str) -> int:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume])
    return len(candles)


# --------------------------------------------------------------------------- PROVA: EVENTO → LÍDER → ATRASO → REACTION CLOCK → ENTRADA → SAÍDA RÁPIDA OU EXTENSÃO
QUICK_TAKE_ATR = 0.40      # saída rápida: alvo andou a confirmação a favor
EXTEND_CONFIRM_ATR = 0.15  # aos 5 min, se já anda a favor ≥ 0,15 ATR, estende com trailing
TRAIL_ATR = 0.40


@dataclass
class ClockTrade:
    event_id: str
    kind: str
    target: str
    published_at: datetime
    entry_sec: float
    elapsed_lead_sec: float
    p_hist: float
    n_hist: int
    net_quick: float
    net_extend: float
    net_follow: float
    cost: float


@dataclass
class ClockTestRow:
    kind: str
    target: str
    n_events: int
    n_lead: int
    n_entries: int
    skipped: dict[str, int]
    naive_net_quick: float
    net_quick: float
    net_extend: float
    net_follow: float
    win_quick: float
    win_extend: float
    cost: float
    pf_quick: Optional[float] = None
    pf_extend: Optional[float] = None
    trades_quick: list = field(default_factory=list)
    trades_extend: list = field(default_factory=list)
    trades_naive: list = field(default_factory=list)

    def row(self) -> str:
        tag = "⚪" if self.n_entries < 20 else ("🟢" if max(self.net_quick, self.net_extend, self.net_follow) > 0.02 else "🔴")
        sk = " ".join(f"{k}:{v}" for k, v in self.skipped.items() if v)
        return (f"{self.kind:<18}{self.target:<8}{self.n_events:>5}{self.n_lead:>5}{self.n_entries:>5}{self.naive_net_quick:>+9.3f}{self.net_quick:>+9.3f}"
                f"{self.net_extend:>+9.3f}{self.net_follow:>+9.3f}{self.win_quick:>6.0%}{self.win_extend:>6.0%}{self.cost:>7.3f}  {tag}  {sk}")


def profit_factor(xs: Sequence[float]) -> Optional[float]:
    wins, losses = sum(x for x in xs if x > 0), -sum(x for x in xs if x < 0)
    if not xs:
        return None
    return (wins / losses) if losses > 0 else (float("inf") if wins > 0 else 0.0)


class ClockTradeTest:
    """Percorre os eventos em ordem cronológica. Em cada um, o relógio só conhece os eventos anteriores já concluídos.
    ENTRADA (T_líder + atraso + latência) exige: líder reagiu; alvo ainda não (|mov| < 0,15 ATR); histórico do tipo com
    n ≥ MIN_N e P(alvo confirma | líder) ≥ p_min; tempo decorrido ≤ 2 × mediana do movimento pleno.
    SAÍDA: QUICK (take +0,40 ATR ou 5 min; stop −0,5) · EXTEND (aos 5 min, se ≥ +0,15 ATR, trailing 0,40 até 60 min) · FOLLOW (stop/60 min).
    Compara com a entrada INGÊNUA (toda reação do líder, saída em 5 min) para isolar o valor do relógio."""

    def __init__(self, delay_sec: int = 5, p_min: float = 0.55, min_n: int = 5, slippage_atr: float = 0.02, latency_sec: float = 0.5) -> None:
        self.delay_sec, self.p_min, self.min_n = delay_sec, p_min, min_n
        self.sim = ReactionTradeSim(slippage_atr, latency_sec)
        self.trades: list[ClockTrade] = []
        self.skipped: dict[tuple[str, str], dict[str, int]] = {}
        self.naive: dict[tuple[str, str], list[float]] = {}

    def _exits(self, rec: HiResRecord, path: PricePath, atr: float, t_in: datetime, entry: float, sign: float) -> tuple[float, float, float]:
        slip = self.sim.slippage_atr * atr
        t5 = t_in + timedelta(seconds=SHORT_SEC)
        end = rec.base.published_at + timedelta(seconds=FOLLOW_SEC)
        stop = entry - sign * STOP_ATR * atr
        take = entry + sign * QUICK_TAKE_ATR * atr

        def px(q: Quote) -> float:
            return (q.bid if sign > 0 else q.ask) - sign * slip

        quick = None
        for q in path.between(t_in, t5):
            if (sign > 0 and q.bid <= stop) or (sign < 0 and q.ask >= stop):
                quick = stop - sign * slip
                break
            if (sign > 0 and q.bid >= take) or (sign < 0 and q.ask <= take):
                quick = take - sign * slip
                break
        q5 = path.at_or_before(t5)
        if quick is None:
            quick = px(q5) if q5 else entry
        # EXTEND
        move5 = sign * (q5.mid - entry) / atr if q5 else 0.0
        stopped5 = any((sign > 0 and q.bid <= stop) or (sign < 0 and q.ask >= stop) for q in path.between(t_in, t5))
        if stopped5:
            extend = stop - sign * slip
        elif move5 < EXTEND_CONFIRM_ATR:
            extend = px(q5) if q5 else entry
        else:
            peak = max((sign * (q.mid - entry) for q in path.between(t_in, t5)), default=move5 * atr)
            trail = max(stop, entry + sign * (peak - TRAIL_ATR * atr)) if sign > 0 else min(stop, entry + sign * (peak - TRAIL_ATR * atr))
            extend = None
            for q in path.between(t5, end):
                mv = sign * (q.mid - entry)
                if mv > peak:
                    peak = mv
                    trail = entry + sign * (peak - TRAIL_ATR * atr)
                    if sign > 0:
                        trail = max(trail, stop)
                    else:
                        trail = min(trail, stop)
                if (sign > 0 and q.bid <= trail) or (sign < 0 and q.ask >= trail):
                    extend = trail - sign * slip
                    break
            if extend is None:
                qe = path.at_or_before(end)
                extend = px(qe) if qe else entry
        # FOLLOW
        follow = None
        for q in path.between(t_in, end):
            if (sign > 0 and q.bid <= stop) or (sign < 0 and q.ask >= stop):
                follow = stop - sign * slip
                break
        if follow is None:
            qe = path.at_or_before(end)
            follow = px(qe) if qe else entry
        return (sign * (quick - entry) / atr, sign * (extend - entry) / atr, sign * (follow - entry) / atr)

    def run(self, items: Sequence[tuple[HiResRecord, PricePath, float]]) -> list[ClockTestRow]:
        items = sorted(items, key=lambda x: x[0].base.published_at)
        history: dict[tuple[str, str], list[HiResRecord]] = {}
        counts: dict[tuple[str, str], dict[str, int]] = {}
        for rec, path, atr in items:
            key = (rec.kind, rec.target)
            c = counts.setdefault(key, {"eventos": 0, "líder": 0, "entradas": 0})
            sk = self.skipped.setdefault(key, {"sem_hist": 0, "P_baixa": 0, "alvo_já_reagiu": 0, "fora_janela": 0, "sem_preço": 0})
            c["eventos"] += 1
            # estatística point-in-time: só eventos do mesmo tipo/alvo concluídos antes deste
            prior = [r for r in history.get(key, []) if r.base.published_at + timedelta(seconds=FOLLOW_SEC) <= rec.base.published_at]
            history.setdefault(key, []).append(rec)
            if rec.lead_first_sec is None:
                continue
            c["líder"] += 1
            # baseline INGÊNUA: mesma entrada e mesma saída QUICK, mas em TODA reação do líder (sem relógio)
            t_naive = rec.base.published_at + timedelta(seconds=rec.lead_first_sec + self.delay_sec + self.sim.latency_sec)
            q_n = path.at_or_before(t_naive)
            if q_n is not None and q_n.time > rec.base.published_at:
                sgn = rec.base.expected_dir
                e_n = (q_n.ask if sgn > 0 else q_n.bid) + sgn * self.sim.slippage_atr * atr
                self.naive.setdefault(key, []).append(self._exits(rec, path, atr, t_naive, e_n, sgn)[0])
            with_lead = [r for r in prior if r.lead_first_sec is not None]
            if len(with_lead) < self.min_n:
                sk["sem_hist"] += 1
                continue
            p_hist = sum(1 for r in with_lead if r.base.direction_correct) / len(with_lead)
            if p_hist < self.p_min:
                sk["P_baixa"] += 1
                continue
            fulls = [r.base.time_to_full_move for r in with_lead if r.base.time_to_full_move is not None]
            window_sec = (2 * statistics.median(fulls) * 60) if fulls else float(FOLLOW_SEC)
            t_in = rec.base.published_at + timedelta(seconds=rec.lead_first_sec + self.delay_sec + self.sim.latency_sec)
            elapsed = (t_in - rec.base.published_at).total_seconds()
            if elapsed > window_sec:
                sk["fora_janela"] += 1
                continue
            q_in, q0 = path.at_or_before(t_in), path.at_or_before(rec.base.published_at)
            if q_in is None or q0 is None or q_in.time <= rec.base.published_at:
                sk["sem_preço"] += 1
                continue
            sign = rec.base.expected_dir
            if abs(q_in.mid - q0.mid) / atr >= FIRST_ATR:      # já reagiu (a favor OU contra) → não é "atrasado"
                sk["alvo_já_reagiu"] += 1
                continue
            entry = (q_in.ask if sign > 0 else q_in.bid) + sign * self.sim.slippage_atr * atr
            nq, ne, nf = self._exits(rec, path, atr, t_in, entry, sign)
            gross_q = sign * ((path.at_or_before(t_in + timedelta(seconds=SHORT_SEC)) or q_in).mid - q_in.mid) / atr
            self.trades.append(ClockTrade(rec.base.event_id, rec.kind, rec.target, rec.base.published_at, elapsed, rec.lead_first_sec, p_hist, len(with_lead),
                                          nq, ne, nf, max(0.0, gross_q - nq)))
            c["entradas"] += 1
        rows = []
        for key, c in sorted(counts.items(), key=lambda kv: (-kv[1]["eventos"], kv[0])):
            tr = [t for t in self.trades if (t.kind, t.target) == key]
            m = lambda xs: statistics.fmean(xs) if xs else 0.0  # noqa: E731
            rows.append(ClockTestRow(key[0], key[1], c["eventos"], c["líder"], c["entradas"], self.skipped[key], m(self.naive.get(key, [])),
                                     m([t.net_quick for t in tr]), m([t.net_extend for t in tr]), m([t.net_follow for t in tr]),
                                     (sum(1 for t in tr if t.net_quick > 0) / len(tr)) if tr else 0.0, (sum(1 for t in tr if t.net_extend > 0) / len(tr)) if tr else 0.0,
                                     m([t.cost for t in tr]), profit_factor([t.net_quick for t in tr]), profit_factor([t.net_extend for t in tr]),
                                     [t.net_quick for t in tr], [t.net_extend for t in tr], list(self.naive.get(key, []))))
        return rows

    def render(self, rows: Sequence[ClockTestRow]) -> str:
        head = (f"{'evento':<18}{'alvo':<8}{'evts':>5}{'líd':>5}{'entr':>5}{'ingênua':>9}{'QUICK':>9}{'EXTEND':>9}{'FOLLOW':>9}{'winQ':>6}{'winE':>6}{'custo':>7}")
        lines = [f"🧪 PROVA — EVENTO → LÍDER → ATRASO {self.delay_sec}s → REACTION CLOCK → ENTRADA → SAÍDA RÁPIDA OU EXTENSÃO (líquido em ATR, walk-forward por construção)",
                 f"   relógio exige: líder reagiu · alvo ainda não · histórico n ≥ {self.min_n} com P(alvo|líder) ≥ {self.p_min:.0%} · dentro de 2× mediana do movimento pleno",
                 head] + [r.row() for r in rows]
        tr = self.trades
        if tr:
            tot = lambda k: sum(getattr(t, k) for t in tr)  # noqa: E731
            lines.append(f"   TOTAL {len(tr)} entradas · QUICK {tot('net_quick'):+.2f} ATR ({tot('net_quick') / STOP_ATR:+.1f}R) · EXTEND {tot('net_extend'):+.2f} ATR "
                         f"({tot('net_extend') / STOP_ATR:+.1f}R) · FOLLOW {tot('net_follow'):+.2f} ATR ({tot('net_follow') / STOP_ATR:+.1f}R)")
        lines.append("   'ingênua' = toda reação do líder com a MESMA entrada e saída QUICK, sem relógio; a diferença para QUICK é o valor do filtro temporal")
        lines.append("   ⚪ < 20 entradas inconclusivo · 🟢 líquido > 0,02 ATR · 🔴 custo consome. Colunas de descarte: sem_hist / P_baixa / alvo_já_reagiu / fora_janela")
        return "\n".join(lines)


# --------------------------------------------------------------------------- VEREDITO POR ATIVO → REACTION EDGE (consumível pelo Asset Selector)
VERDICT_SCORE = {"🟢": 0.9, "🟡": 0.65, "🔴": 0.2, "⚪": 0.5}


@dataclass
class AssetVerdict:
    symbol: str
    verdict: str            # 🟢 forte | 🟡 moderado | 🔴 sem edge | ⚪ inconclusivo
    n: int
    net: float              # melhor expectancy líquida (ATR) entre atrasos × saídas
    naive: float
    delay_sec: int
    exit: str
    n_events: int
    kinds: int
    robust: float = 0.0     # mediana do líquido entre as combinações atraso × saída com amostra (base do veredito)

    @property
    def edge_score(self) -> float:
        return VERDICT_SCORE[self.verdict]

    def row(self) -> str:
        label = {"🟢": "forte", "🟡": "moderado", "🔴": "sem edge", "⚪": "inconclusivo"}[self.verdict]
        return (f"{self.symbol:<8}{self.verdict} {label:<13}{self.n_events:>6}{self.n:>7}{self.net:>+9.3f}{self.net / STOP_ATR:>+7.2f}R{self.robust:>+9.3f}{self.naive:>+9.3f}"
                f"{self.delay_sec:>7}s {self.exit:<7}{self.kinds:>6}")


def asset_verdicts(results: dict[int, list[ClockTestRow]], min_n: int = 20) -> list[AssetVerdict]:
    """`results`: atraso → linhas do ClockTradeTest. Por ativo, escolhe a melhor combinação atraso × saída (QUICK/EXTEND/FOLLOW)
    ponderando por entradas; veredito só com n ≥ min_n."""
    per_asset: dict[str, list[tuple[int, str, float, int, float, int, int]]] = {}
    for delay, rows in results.items():
        by_sym: dict[str, list[ClockTestRow]] = {}
        for r in rows:
            by_sym.setdefault(r.target, []).append(r)
        for sym, rs in by_sym.items():
            n = sum(r.n_entries for r in rs)
            n_ev = sum(r.n_events for r in rs)
            if n == 0:
                per_asset.setdefault(sym, []).append((delay, "QUICK", 0.0, 0, 0.0, n_ev, len(rs)))
                continue
            naive_n = sum(r.n_lead for r in rs)
            naive = sum(r.naive_net_quick * r.n_lead for r in rs) / naive_n if naive_n else 0.0
            for ex, attr in (("QUICK", "net_quick"), ("EXTEND", "net_extend"), ("FOLLOW", "net_follow")):
                net = sum(getattr(r, attr) * r.n_entries for r in rs) / n
                per_asset.setdefault(sym, []).append((delay, ex, net, n, naive, n_ev, len(rs)))
    out = []
    for sym, combos in per_asset.items():
        valid = [c for c in combos if c[3] >= min_n]
        if not valid:
            best = max(combos, key=lambda c: (c[3], c[2]))
            out.append(AssetVerdict(sym, "⚪", best[3], best[2], best[4], best[0], best[1], best[5], best[6]))
            continue
        best = max(valid, key=lambda c: c[2])
        delay, ex, net, n, naive, n_ev, kinds = best
        robust = statistics.median([c[2] for c in valid])     # mediana das combinações válidas: evita escolher o melhor por sorte
        if robust >= 0.10 and net > naive:
            v = "🟢"
        elif robust > 0.02:
            v = "🟡"
        else:
            v = "🔴"
        out.append(AssetVerdict(sym, v, n, net, naive, delay, ex, n_ev, kinds, robust))
    return sorted(out, key=lambda v: (-VERDICT_SCORE[v.verdict], -v.net))


def render_verdicts(verdicts: Sequence[AssetVerdict], resolution: str) -> str:
    head = f"{'ativo':<8}{'veredito':<16}{'evts':>6}{'entr':>7}{'melhor':>9}{'':>8}{'mediana':>9}{'ingênua':>9}{'atraso':>8} {'saída':<7}{'tipos':>6}"
    lines = [f"🏁 REACTION EDGE POR ATIVO ({resolution}) — o relógio não funciona igual em todos os mercados", head] + [v.row() for v in verdicts]
    lines.append("   veredito pela MEDIANA das combinações atraso × saída com n ≥ 20 (não pelo melhor caso): 🟢 ≥ 0,10 ATR e melhor > ingênua · 🟡 > 0,02 · 🔴 custo consome · ⚪ n < 20")
    lines.append("   'melhor combinação' atraso × saída por ativo; o Asset Selector usa este veredito (reaction_edge.json) só quando o relógio marca PRESSÃO LATENTE")
    return "\n".join(lines)


def save_reaction_edge(verdicts: Sequence[AssetVerdict], path: str, resolution: str) -> None:
    import json
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {v.symbol: {"verdict": v.verdict, "edge_score": v.edge_score, "n": v.n, "net_atr": round(v.net, 4), "naive_atr": round(v.naive, 4),
                       "delay_sec": v.delay_sec, "exit": v.exit, "n_events": v.n_events, "resolution": resolution,
                       "generated_at": datetime.now(timezone.utc).isoformat()} for v in verdicts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def load_reaction_edge(path: str) -> dict[str, float]:
    """symbol → edge_score (0..1) só para vereditos com amostra (🟢/🟡/🔴); ⚪ é ignorado (o selector fica neutro)."""
    import json
    import os
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {sym: float(v["edge_score"]) for sym, v in data.items() if v.get("verdict") in ("🟢", "🟡", "🔴")}


# --------------------------------------------------------------------------- A TABELA QUE IMPORTA: REACTION CLOCK − INGÊNUA, por ativo × atraso
@dataclass
class DeltaRow:
    symbol: str
    delay_sec: int
    n: int
    n_naive: int
    naive_r: float
    clock_r: float
    extend_r: float
    pf_quick: Optional[float]
    pf_extend: Optional[float]

    @property
    def delta_r(self) -> float:
        return self.clock_r - self.naive_r

    def row(self) -> str:
        pf = lambda v: "  n/d" if v is None else ("    ∞" if v == float("inf") else f"{v:5.2f}")  # noqa: E731
        tag = "⚪" if self.n < 20 else ("🟢" if self.delta_r > 0 and self.clock_r > 0 else "🔴")
        return (f"{self.symbol:<8}{self.delay_sec:>5}s{self.n:>6}{self.n_naive:>7}{self.naive_r:>+10.2f}R{self.clock_r:>+10.2f}R{self.delta_r:>+9.2f}R"
                f"{self.extend_r:>+9.2f}R{pf(self.pf_quick):>7}{pf(self.pf_extend):>7}  {tag}")


def delta_table(results: dict[int, list[ClockTestRow]]) -> list[DeltaRow]:
    out = []
    for delay, rows in sorted(results.items()):
        by_sym: dict[str, list[ClockTestRow]] = {}
        for r in rows:
            by_sym.setdefault(r.target, []).append(r)
        for sym, rs in sorted(by_sym.items()):
            q = [x for r in rs for x in r.trades_quick]
            e = [x for r in rs for x in r.trades_extend]
            nv = [x for r in rs for x in r.trades_naive]
            m = lambda xs: (statistics.fmean(xs) / STOP_ATR) if xs else 0.0  # noqa: E731
            out.append(DeltaRow(sym, delay, len(q), len(nv), m(nv), m(q), m(e), profit_factor(q), profit_factor(e)))
    return sorted(out, key=lambda d: (d.symbol, d.delay_sec))


def render_delta(rows: Sequence[DeltaRow], resolution: str) -> str:
    head = f"{'ativo':<8}{'atraso':>6}{'n':>6}{'n ing.':>7}{'INGÊNUA':>11}{'CLOCK':>11}{'Δ CLOCK−ING':>10}{'EXTEND':>10}{'PF Q':>7}{'PF E':>7}"
    lines = [f"📊 REACTION CLOCK − INGÊNUA por ativo × atraso ({resolution}; R = 0,5 ATR; líquido de custos; walk-forward por construção)", head]
    lines += [r.row() for r in rows]
    lines.append("   Δ > 0 com n ≥ 20 e CLOCK > 0 = o relógio adiciona valor 🟢 · ⚪ amostra insuficiente · 🔴 relógio não ajuda ou perde")
    return "\n".join(lines)


def render_stability(tick: Sequence[AssetVerdict], m1: Sequence[AssetVerdict]) -> str:
    """TICK responde 'existe vantagem em segundos?'; M1 responde 'sobrevive a meses/regimes?'. Só a concordância vale como MUITO FORTE."""
    t = {v.symbol: v for v in tick}
    m = {v.symbol: v for v in m1}
    lines = [f"🧭 ESTABILIDADE TICK × M1 — evidência só é MUITO FORTE quando as duas resoluções apontam na mesma direção",
             f"{'ativo':<8}{'TICK':<16}{'n':>5}{'líq.med':>8}{'M1':<16}{'n':>5}{'líq.med':>8}  conclusão"]
    label = {"🟢": "🟢 forte", "🟡": "🟡 moderado", "🔴": "🔴 sem edge", "⚪": "⚪ inconclusivo"}
    for sym in sorted(set(t) | set(m)):
        a, b = t.get(sym), m.get(sym)
        va, vb = (a.verdict if a else "⚪"), (b.verdict if b else "⚪")
        if va == "🟢" and vb == "🟢":
            concl = "🟢🟢 MUITO FORTE — segundos e meses concordam"
        elif "🟢" in (va, vb) and "🟡" in (va, vb):
            concl = "🟢🟡 forte com reserva"
        elif "⚪" in (va, vb):
            concl = "⚪ falta amostra numa das resoluções"
        elif va == "🔴" or vb == "🔴":
            concl = "🔴 uma resolução nega — não operar"
        else:
            concl = "🟡 moderado nas duas"
        fa = (f"{a.n:>5}{a.robust / STOP_ATR:>+7.2f}R" if a else f"{'':>5}{'':>8}")
        fb = (f"{b.n:>5}{b.robust / STOP_ATR:>+7.2f}R" if b else f"{'':>5}{'':>8}")
        lines.append(f"{sym:<8}{label[va]:<16}{fa}{label[vb]:<16}{fb}  {concl}")
    lines.append("   líq. = MEDIANA das combinações atraso × saída com n ≥ 20 (a base do veredito), não o melhor caso; n = entradas do relógio, 0 = líder nunca reagiu a tempo")
    return "\n".join(lines)


# --------------------------------------------------------------------------- WALK-FORWARD DO ATRASO: escolhe na 1ª metade, mede na 2ª
def walk_forward_choice(tests: dict[int, "ClockTradeTest"]) -> str:
    """`tests`: atraso → ClockTradeTest já executado (com .trades). Para cada ativo, escolhe (atraso, saída) pelo líquido da
    PRIMEIRA metade dos eventos e reporta o líquido na SEGUNDA metade — a escolha nunca vê os dados em que é avaliada."""
    all_tr = [(d, t) for d, ct in tests.items() for t in ct.trades]
    if not all_tr:
        return "🧭 WALK-FORWARD DO ATRASO: sem entradas para avaliar"
    by_sym: dict[str, list] = {}
    for d, t in all_tr:
        by_sym.setdefault(t.target, []).append((d, t))
    lines = ["🧭 WALK-FORWARD DO ATRASO — (atraso × saída) escolhido na 1ª metade dos eventos, resultado medido na 2ª metade (R = 0,5 ATR)",
             f"{'ativo':<8}{'escolha':<14}{'n1':>4}{'líq.1ª':>9}{'n2':>4}{'líq.2ª':>9}{'ingênua 2ª':>12}  leitura"]
    for sym, items in sorted(by_sym.items()):
        times = sorted({t.published_at for _, t in items})
        split = times[len(times) // 2]
        first = [(d, t) for d, t in items if t.published_at < split]
        second = [(d, t) for d, t in items if t.published_at >= split]
        best, best_net = None, None
        for d in tests:
            for ex, attr in (("QUICK", "net_quick"), ("EXTEND", "net_extend"), ("FOLLOW", "net_follow")):
                xs = [getattr(t, attr) for dd, t in first if dd == d]
                if len(xs) >= 5:
                    m = statistics.fmean(xs)
                    if best_net is None or m > best_net:
                        best, best_net = (d, ex, attr), m
        if best is None:
            lines.append(f"{sym:<8}{'—':<14}{len(first):>4}{'':>9}{len(second):>4}{'':>9}{'':>12}  ⚪ 1ª metade sem 5 entradas por combinação")
            continue
        d, ex, attr = best
        xs2 = [getattr(t, attr) for dd, t in second if dd == d]
        naive2 = tests[d].naive
        n2_naive = [v for key, vals in naive2.items() if key[1] == sym for v in vals]
        net2 = statistics.fmean(xs2) / STOP_ATR if xs2 else 0.0
        nv2 = (statistics.fmean(n2_naive) / STOP_ATR) if n2_naive else 0.0
        tag = "⚪ amostra pequena" if len(xs2) < 20 else ("🟢 sobreviveu fora da amostra" if net2 > 0.02 / STOP_ATR and net2 > nv2 else "🔴 não sobreviveu")
        lines.append(f"{sym:<8}{f'{d}s {ex}':<14}{len(first):>4}{best_net / STOP_ATR:>+8.2f}R{len(xs2):>4}{net2:>+8.2f}R{nv2:>+11.2f}R  {tag}")
    lines.append("   só a coluna 'líq.2ª' conta: é o que a escolha feita antes teria rendido depois; se ela desaparece, a 'melhor combinação' era ruído")
    return "\n".join(lines)
