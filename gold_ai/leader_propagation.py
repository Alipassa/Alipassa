"""LEADER PROPAGATION ENGINE (6.0) — o robô não prevê o primeiro movimento: detecta que ele JÁ começou no ativo líder e entra nos
ativos que o histórico diz que costumam acompanhar depois.

    MOVIMENTO BRUSCO no líder → detectar em segundos (M1/ticks) → direção → magnitude do impulso → procurar ATRASADOS
    → entrar SÓ onde existe edge líquido comprovado (P calibrada × movimento esperado − spread − slippage − comissão)
    → capturar a parte estatisticamente previsível da propagação → SAIR.

Peças (cada uma só olha o passado — walk-forward por construção):
  1. DETECTOR      impulso = variação em `impulse_min` minutos ≥ z × desvio-padrão das variações das últimas `lookback` horas
                   (limiar ESTATÍSTICO: 0,20 % é enorme num regime e normal noutro) e ≥ `min_move_atr` ATR horário.
  2. LEADER        quem se mexeu primeiro: no instante da detecção cada outro ativo é classificado como ATRASADO (|z| < lag_z),
                   PARCIAL (já andou) ou SIMULTÂNEO (também rompeu o limiar — não é atrasado, é co-líder).
  3. LAG MAP       líder × direção × alvo → resultados medidos no horizonte (direção final, tempo até reagir, MFE, MAE,
                   fração do impulso do líder transmitida). Cada medição só é conhecida em t + horizonte.
  4. REACTION CLOCK  P(alvo sobe | líder subiu) × P(desce): a direção é a que o histórico mostrar (mesma ou oposta),
                   encolhida para 50 % conforme a amostra; tempo até reação (mediana/P25/P75); continuação; magnitude.
  5. OPORTUNIDADE  alvo adaptativo = quantil da fração transmitida × impulso do líder (em ATR do alvo); operações-sombra
                   simuladas com os mesmos custos dão a expectancy líquida do par ANTES de qualquer entrada real.
  6. EXECUÇÃO      entra só com n ≥ min_n, P ≥ p_min e expectancy-sombra líquida > edge_min; nunca dois trades no mesmo alvo.
  7. SAÍDA         take adaptativo · stop 0,5 ATR · tempo limite = 2 × mediana do tempo de reação (não reagiu → sai).

TESTES: A líder opera o próprio impulso (cada ativo sozinho) · B→E líder → 1, 2, 3, 4 atrasados (melhor edge primeiro) ·
INGÊNUA todo atrasado na direção do líder, sem relógio. Métricas: n, acerto, expectancy, R líquido, PF, lucro, custo, DD,
minutos por operação, 1ª × 2ª metade, por alvo e por líder. Nada aqui afirma lucro: mede."""
from __future__ import annotations

import json
import math
import statistics
from bisect import bisect_left, bisect_right
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence

from .false_signal import session_of as propagation_session
from .reaction import FIRST_ATR, shrink                      # "reagiu" = ≥ 0,15 ATR na direção prevista
from .reaction_hires import STOP_ATR, PricePath, Quote, profit_factor   # 1R = 0,5 ATR horário (mesma convenção da prova do REACTION CLOCK)

TAKE_MIN_ATR, TAKE_MAX_ATR = 0.15, 2.0
TIMEOUT_MIN_MIN = 5.0
TESTS = (("A", 0), ("B", 1), ("C", 2), ("D", 3), ("E", 4))


def quantile(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# --------------------------------------------------------------------------- SÉRIE: quotes + volatilidade rolante + ATR horário
class Series:
    """PricePath de um ativo + desvio-padrão rolante da variação em `impulse_min` minutos (só passado) + ATR horário (só passado)."""

    def __init__(self, symbol: str, path: PricePath, impulse_min: int = 3, lookback_hours: float = 24.0, min_samples: int = 120,
                 atr_period: int = 14) -> None:
        self.symbol, self.path = symbol, path
        self.k = max(1, int(impulse_min))
        self.times = path.times
        self.mids = [q.mid for q in path.q]
        n = len(self.mids)
        self.delta: list[Optional[float]] = [None] * n       # variação em k minutos (None se atravessa lacuna)
        self.vol: list[Optional[float]] = [None] * n         # desvio-padrão das variações anteriores (janela em amostras)
        window = max(min_samples, int(lookback_hours * 60 / self.k))
        max_gap = timedelta(minutes=self.k * 3)
        buf: deque = deque()
        s1 = s2 = 0.0
        for i in range(n):
            if i >= self.k and self.times[i] - self.times[i - self.k] <= max_gap:
                self.delta[i] = self.mids[i] - self.mids[i - self.k]
            if len(buf) >= min_samples:
                var = max(0.0, s2 / len(buf) - (s1 / len(buf)) ** 2)
                self.vol[i] = math.sqrt(var)
            d = self.delta[i]
            if d is not None:                              # a variação de i só entra na janela DEPOIS de i (só passado)
                buf.append(d)
                s1 += d
                s2 += d * d
                if len(buf) > window:
                    old = buf.popleft()
                    s1 -= old
                    s2 -= old * old
        # ATR horário: amplitude (máx − mín do mid) por hora cheia, média das últimas `atr_period` horas ANTES de t
        hours: dict[datetime, tuple[float, float]] = {}
        for t, m in zip(self.times, self.mids):
            k = t.replace(minute=0, second=0, microsecond=0)
            lo, hi = hours.get(k, (m, m))
            hours[k] = (min(lo, m), max(hi, m))
        self._hour_keys = sorted(hours)
        self._hour_range = [hours[k][1] - hours[k][0] for k in self._hour_keys]
        self._atr_period = atr_period

    def index_at_or_before(self, t: datetime) -> int:
        return bisect_right(self.times, t) - 1

    def atr_at(self, t: datetime) -> float:
        hk = t.replace(minute=0, second=0, microsecond=0)
        j = bisect_left(self._hour_keys, hk)           # horas cheias estritamente anteriores à hora de t
        rng = [r for r in self._hour_range[max(0, j - self._atr_period):j] if r > 0]
        return statistics.fmean(rng) if rng else 0.0

    def z_at(self, i: int) -> Optional[float]:
        d, v = self.delta[i], self.vol[i]
        if d is None or not v:
            return None
        return d / v


# --------------------------------------------------------------------------- 1–3. DETECTOR · LEADER · LAG MAP
@dataclass
class Impulse:
    leader: str
    time: datetime                  # instante em que o impulso ficou conhecido (fecho da barra / tick)
    direction: float                # +1 / −1
    z: float
    move_atr: float                 # |Δ| em ATR horário do líder
    move_pct: float
    session: str = ""
    news: str = ""                  # kind do release macro nos ±30 min ("" = sem notícia)
    co_movers: list = field(default_factory=list)      # ativos que romperam o limiar no mesmo instante (simultâneos)


@dataclass
class PropOutcome:
    """O que o alvo fez após o impulso do líder — conhecido só em `known_at`."""
    leader: str
    direction: float
    target: str
    time: datetime
    known_at: datetime
    leader_move_atr: float
    lag_z: float
    max_up: float = 0.0             # ATR do alvo
    max_down: float = 0.0
    end_eval: float = 0.0           # movimento assinado (ATR) no minuto de avaliação
    end_h: float = 0.0              # movimento assinado (ATR) no fim do horizonte
    first_up_min: Optional[float] = None
    first_down_min: Optional[float] = None

    def mfe(self, s: float) -> float:
        return self.max_up if s > 0 else self.max_down

    def mae(self, s: float) -> float:
        return self.max_down if s > 0 else self.max_up

    def first_min(self, s: float) -> Optional[float]:
        return self.first_up_min if s > 0 else self.first_down_min


def measure_outcome(imp: Impulse, target: str, ser: Series, t: datetime, horizon_min: int, eval_min: int, lag_z: float,
                    atr: float) -> Optional[PropOutcome]:
    i0 = ser.index_at_or_before(t)
    if i0 < 0 or ser.times[i0] < t - timedelta(minutes=3) or atr <= 0:
        return None
    p0 = ser.mids[i0]
    end = t + timedelta(minutes=horizon_min)
    i1 = bisect_right(ser.times, end)
    if i1 - i0 < 2:
        return None
    o = PropOutcome(imp.leader, imp.direction, target, t, end, imp.move_atr, lag_z)
    t_eval = t + timedelta(minutes=eval_min)
    for j in range(i0 + 1, i1):
        d = (ser.mids[j] - p0) / atr
        o.max_up, o.max_down = max(o.max_up, d), max(o.max_down, -d)
        mins = (ser.times[j] - t).total_seconds() / 60.0
        if o.first_up_min is None and d >= FIRST_ATR:
            o.first_up_min = mins
        if o.first_down_min is None and -d >= FIRST_ATR:
            o.first_down_min = mins
    je = bisect_right(ser.times, t_eval) - 1
    o.end_eval = (ser.mids[je] - p0) / atr if je > i0 else 0.0
    o.end_h = (ser.mids[i1 - 1] - p0) / atr
    return o


# --------------------------------------------------------------------------- 4. REACTION CLOCK (estatística point-in-time por líder × direção × alvo)
@dataclass
class RelationStats:
    leader: str
    direction: float
    target: str
    n: int = 0
    p_same: float = 0.0
    p_opp: float = 0.0
    sign: float = 0.0               # direção prevista no ALVO (+1 / −1) — pode ser oposta à do líder
    p: float = 0.5                  # probabilidade encolhida da direção prevista
    react_med: Optional[float] = None
    react_p25: Optional[float] = None
    react_p75: Optional[float] = None
    continuation: float = 0.0       # fração que andou ≥ 0,15 ATR na direção prevista dentro do horizonte
    mfe_med: float = 0.0
    mae_med: float = 0.0
    ratio_q: float = 0.0            # quantil da fração do impulso do líder transmitida ao alvo (ATR/ATR)
    shadow_n: int = 0
    shadow_net: float = 0.0         # expectancy líquida (ATR) das operações-sombra concluídas
    shadow_pf: Optional[float] = None

    @property
    def relation(self) -> str:
        return "mesma" if self.sign == self.direction else "oposta"

    def take_atr(self, leader_move_atr: float) -> float:
        return min(TAKE_MAX_ATR, max(TAKE_MIN_ATR, self.ratio_q * leader_move_atr))

    def timeout_min(self, horizon_min: int) -> float:
        base = 2.0 * self.react_med if self.react_med else float(horizon_min)
        return min(float(horizon_min), max(TIMEOUT_MIN_MIN, base))


def relation_stats(key: tuple, outcomes: Sequence[PropOutcome], shadows: Sequence[float], take_quantile: float, shrink_k: float = 10.0) -> RelationStats:
    leader, direction, target = key
    st = RelationStats(leader, direction, target, len(outcomes))
    if not outcomes:
        return st
    same = sum(1 for o in outcomes if o.end_eval * direction > 0)
    opp = sum(1 for o in outcomes if o.end_eval * direction < 0)
    st.p_same, st.p_opp = same / len(outcomes), opp / len(outcomes)
    st.sign = direction if same >= opp else -direction
    st.p = shrink(max(st.p_same, st.p_opp), len(outcomes), shrink_k)
    s = st.sign
    reacts = [o.first_min(s) for o in outcomes if o.first_min(s) is not None]
    if reacts:
        st.react_med, st.react_p25, st.react_p75 = statistics.median(reacts), quantile(reacts, 0.25), quantile(reacts, 0.75)
    st.continuation = len(reacts) / len(outcomes)
    st.mfe_med = statistics.median(o.mfe(s) for o in outcomes)
    st.mae_med = statistics.median(o.mae(s) for o in outcomes)
    ratios = [o.mfe(s) / o.leader_move_atr for o in outcomes if o.leader_move_atr > 0]
    st.ratio_q = quantile(ratios, take_quantile) if ratios else 0.0
    st.shadow_n = len(shadows)
    st.shadow_net = statistics.fmean(shadows) if shadows else 0.0
    st.shadow_pf = profit_factor(list(shadows)) if shadows else None
    return st


# --------------------------------------------------------------------------- 5–7. OPORTUNIDADE · EXECUÇÃO · SAÍDA (simulação a bid/ask)
@dataclass
class PropTrade:
    leader: str
    target: str
    time: datetime                  # entrada
    exit_time: datetime
    sign: float
    net_atr: float                  # ADAPT: take adaptativo · stop · tempo limite
    follow_atr: float               # FOLLOW: só stop · tempo limite (mede se o take corta lucro)
    cost_atr: float                 # spread + 2 × slippage + comissão, em ATR do alvo
    take_atr: float
    timeout_min: float
    exit_reason: str                # take | stop | tempo
    p: float
    edge_atr: float                 # expectancy-sombra líquida conhecida na entrada
    z: float
    session: str
    news: str
    mfe_atr: float = 0.0
    mae_atr: float = 0.0

    @property
    def net_r(self) -> float:
        return self.net_atr / STOP_ATR

    @property
    def cost_r(self) -> float:
        return self.cost_atr / STOP_ATR

    @property
    def minutes(self) -> float:
        return (self.exit_time - self.time).total_seconds() / 60.0


def simulate_propagation_trade(path: PricePath, atr: float, t_in: datetime, sign: float, take_atr: float, timeout_min: float, slippage_atr: float,
                   commission_atr: float, t_min_quote: datetime) -> Optional[PropTrade]:
    """Entrada a ask/bid + slippage na cotação disponível em t_in; percorre as cotações até take, stop ou tempo limite (stop checado antes)."""
    q_in = path.at_or_before(t_in)
    if q_in is None or q_in.time <= t_min_quote or atr <= 0:
        return None
    slip = slippage_atr * atr
    entry = (q_in.ask if sign > 0 else q_in.bid) + sign * slip
    stop = entry - sign * STOP_ATR * atr
    take = entry + sign * take_atr * atr
    end = t_in + timedelta(minutes=timeout_min)
    px_exit = lambda q: (q.bid if sign > 0 else q.ask) - sign * slip  # noqa: E731
    adapt: Optional[float] = None
    follow: Optional[float] = None
    reason, t_exit = "tempo", end
    mfe = mae = 0.0
    for q in path.between(t_in, end):
        if adapt is None:                              # excursões só enquanto a operação ADAPT está aberta
            d = sign * (q.mid - entry) / atr
            mfe, mae = max(mfe, d), max(mae, -d)
        hit_stop = (sign > 0 and q.bid <= stop) or (sign < 0 and q.ask >= stop)
        hit_take = (sign > 0 and q.bid >= take) or (sign < 0 and q.ask <= take)
        if follow is None and hit_stop:
            follow = stop - sign * slip
        if adapt is None:
            if hit_stop:
                adapt, reason, t_exit = stop - sign * slip, "stop", q.time
            elif hit_take:
                adapt, reason, t_exit = take - sign * slip, "take", q.time
        if adapt is not None and follow is not None:
            break
    q_end = path.at_or_before(end)
    if q_end is None:
        return None
    if adapt is None:
        adapt = px_exit(q_end)
    if follow is None:
        follow = px_exit(q_end)
    cost = (q_in.spread + 2 * slip) / atr + commission_atr
    return PropTrade("", "", q_in.time, t_exit, sign, sign * (adapt - entry) / atr, sign * (follow - entry) / atr, cost, take_atr, timeout_min,
                     reason, 0.0, 0.0, 0.0, "", "", mfe, mae)


# --------------------------------------------------------------------------- MÉTRICAS por teste
@dataclass
class TestResult:
    name: str
    label: str
    trades: list = field(default_factory=list)
    equity: float = 10000.0
    risk_pct: float = 1.0
    end_equity: float = 10000.0
    max_dd_pct: float = 0.0
    peak_concurrent: int = 0

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def net_r(self) -> float:
        return sum(t.net_r for t in self.trades)

    @property
    def follow_r(self) -> float:
        return sum(t.follow_atr for t in self.trades) / STOP_ATR

    @property
    def expectancy(self) -> float:
        return self.net_r / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return (sum(1 for t in self.trades if t.net_r > 0) / self.n) if self.n else 0.0

    @property
    def cost_r(self) -> float:
        return sum(t.cost_r for t in self.trades)

    @property
    def pf(self) -> Optional[float]:
        return profit_factor([t.net_r for t in self.trades])

    @property
    def max_dd_r(self) -> float:
        cum = peak = dd = 0.0
        for t in sorted(self.trades, key=lambda x: x.exit_time):
            cum += t.net_r
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        return dd

    @property
    def avg_minutes(self) -> float:
        return (sum(t.minutes for t in self.trades) / self.n) if self.n else 0.0

    @property
    def ret_pct(self) -> float:
        return (self.end_equity / self.equity - 1) * 100 if self.equity else 0.0

    def halves(self, split: datetime) -> tuple["TestResult", "TestResult"]:
        a = TestResult(self.name, "1ª metade", [t for t in self.trades if t.time < split], self.equity, self.risk_pct)
        b = TestResult(self.name, "2ª metade", [t for t in self.trades if t.time >= split], self.equity, self.risk_pct)
        a.run_equity()
        b.run_equity()
        return a, b

    def by(self, key: Callable[[PropTrade], str]) -> dict[str, tuple[int, float, float]]:
        out: dict[str, list] = {}
        for t in self.trades:
            k = key(t)
            acc = out.setdefault(k, [0, 0.0, 0])
            acc[0] += 1
            acc[1] += t.net_r
            acc[2] += 1 if t.net_r > 0 else 0
        return {k: (v[0], v[1], (v[2] / v[0]) if v[0] else 0.0) for k, v in out.items()}

    def run_equity(self) -> None:
        """Capital composto: cada operação arrisca risk_pct % do capital no momento da entrada (1R = stop)."""
        eq, peak = self.equity, self.equity
        open_: list[tuple[datetime, float]] = []
        self.max_dd_pct, self.peak_concurrent = 0.0, 0
        for t in sorted(self.trades, key=lambda x: x.time):
            still = []
            for et, pnl in sorted(open_):
                if et <= t.time:
                    eq += pnl
                    peak = max(peak, eq)
                    self.max_dd_pct = max(self.max_dd_pct, (peak - eq) / peak * 100 if peak else 0.0)
                else:
                    still.append((et, pnl))
            open_ = still
            risk = eq * self.risk_pct / 100.0
            open_.append((t.exit_time, t.net_r * risk))
            self.peak_concurrent = max(self.peak_concurrent, len(open_))
        for et, pnl in sorted(open_):
            eq += pnl
            peak = max(peak, eq)
            self.max_dd_pct = max(self.max_dd_pct, (peak - eq) / peak * 100 if peak else 0.0)
        self.end_equity = round(eq, 2)

    def verdict(self, min_n: int = 20) -> str:
        if self.n < min_n:
            return "⚪"
        return "🟢" if self.expectancy > 0.05 and (self.pf or 0) > 1.1 else "🟡" if self.expectancy > 0 else "🔴"

    def row(self, split: Optional[datetime] = None) -> str:
        pf = self.pf
        pf_s = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
        s = (f"  {self.name:<8}{self.label:<26}{self.n:>5}{self.win_rate:>7.0%}{self.expectancy:>+9.2f}R{self.net_r:>+9.1f}R{pf_s:>6}"
             f"{self.ret_pct:>+8.1f}%{self.max_dd_pct:>7.1f}%{self.cost_r:>8.1f}R{self.avg_minutes:>7.0f}m{self.peak_concurrent:>5}  {self.verdict()}")
        if split is not None and self.n:
            a, b = self.halves(split)
            s += f"   1ª {a.n} op {a.expectancy:+.2f}R · 2ª {b.n} op {b.expectancy:+.2f}R"
        return s


# --------------------------------------------------------------------------- O MOTOR
@dataclass
class PropagationConfig:
    impulse_min: int = 3            # janela do impulso (minutos)
    z: float = 3.0                  # limiar estatístico: |Δ| ≥ z × desvio-padrão
    min_move_atr: float = 0.25      # e ≥ este tanto do ATR horário (evita disparar em madrugada morta)
    lookback_hours: float = 24.0    # janela do desvio-padrão
    lag_z: float = 1.0              # alvo "atrasado" = |z do alvo| < lag_z
    horizon_min: int = 60           # medição/limite máximo da operação
    eval_min: int = 30              # minuto em que a direção do alvo é julgada
    cooldown_min: int = 60          # sem novo impulso do mesmo líder dentro deste prazo
    min_n: int = 10                 # casos concluídos mínimos (relação e sombras) para entrar
    p_min: float = 0.55             # probabilidade encolhida mínima
    edge_min_atr: float = 0.0       # expectancy-sombra líquida mínima (ATR)
    take_quantile: float = 0.35     # quantil da fração transmitida usado como alvo
    delay_sec: float = 60.0         # do instante conhecido até a ordem (M1: próxima barra)
    latency_sec: float = 0.5
    slippage_atr: float = 0.02      # por perna
    commission_atr: float = 0.0     # ida e volta, em ATR
    max_targets: int = 4
    equity: float = 10000.0
    risk_pct: float = 1.0
    shrink_k: float = 10.0


class LeaderPropagationEngine:
    """Percorre os impulsos em ordem cronológica; em cada um só conhece resultados (medições e sombras) já concluídos."""

    def __init__(self, series: dict[str, Series], cfg: PropagationConfig, leaders: Optional[Sequence[str]] = None,
                 news_times: Optional[Sequence[tuple[datetime, str]]] = None, log: Optional[Callable[[str], None]] = None) -> None:
        self.series = series
        self.cfg = cfg
        self.leaders = [s for s in (leaders or list(series)) if s in series]
        self.news = sorted(news_times or [], key=lambda x: x[0])
        self._news_t = [t for t, _ in self.news]
        self.log = log or (lambda s: None)
        self.impulses: list[Impulse] = []
        self.outcomes: dict[tuple, list[PropOutcome]] = {}
        self.shadows: dict[tuple, list[tuple[datetime, float]]] = {}      # (known_at, net ATR)
        self.stats_at_entry: dict[tuple, RelationStats] = {}
        self.classification: dict[str, int] = {"atrasado": 0, "parcial": 0, "simultâneo": 0, "sem_dados": 0, "já_posicionado": 0}
        self.results: dict[str, TestResult] = {}
        self.naive: TestResult = TestResult("INGÊNUA", "todo atrasado, direção do líder")
        self.leader_board: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------------ detector
    def detect(self, start: Optional[datetime] = None, end: Optional[datetime] = None) -> list[Impulse]:
        c = self.cfg
        out: list[Impulse] = []
        for sym in self.leaders:
            ser = self.series[sym]
            last: Optional[datetime] = None
            for i in range(len(ser.times)):
                t = ser.times[i]
                if (start and t < start) or (end and t > end):
                    continue
                zval = ser.z_at(i)
                if zval is None or abs(zval) < c.z:
                    continue
                if last is not None and t < last + timedelta(minutes=c.cooldown_min):
                    continue
                atr = ser.atr_at(t)
                if atr <= 0:
                    continue
                move_atr = abs(ser.delta[i]) / atr
                if move_atr < c.min_move_atr:
                    continue
                last = t
                p_prev = ser.mids[i - ser.k]
                out.append(Impulse(sym, t, 1.0 if zval > 0 else -1.0, abs(zval), move_atr, (ser.delta[i] / p_prev * 100) if p_prev else 0.0,
                                   propagation_session(t), self._news_near(t)))
        out.sort(key=lambda x: (x.time, -x.z))
        self.impulses = out
        return out

    def _news_near(self, t: datetime, minutes: float = 30.0) -> str:
        if not self._news_t:
            return ""
        lo = bisect_left(self._news_t, t - timedelta(minutes=minutes))
        hi = bisect_right(self._news_t, t + timedelta(minutes=minutes))
        return self.news[lo][1] if hi > lo else ""

    # ------------------------------------------------------------------ estatística point-in-time
    def stats_for(self, key: tuple, now: datetime) -> RelationStats:
        prior = [o for o in self.outcomes.get(key, []) if o.known_at <= now]
        sh = [v for k, v in self.shadows.get(key, []) if k <= now]
        return relation_stats(key, prior, sh, self.cfg.take_quantile, self.cfg.shrink_k)

    def _classify(self, imp: Impulse, target: str) -> tuple[str, float]:
        ser = self.series[target]
        j = ser.index_at_or_before(imp.time)
        if j < 0 or ser.times[j] < imp.time - timedelta(minutes=3):
            return "sem_dados", 0.0
        zt = ser.z_at(j)
        if zt is None:
            return "sem_dados", 0.0
        if abs(zt) >= self.cfg.z:
            return "simultâneo", zt
        if abs(zt) >= self.cfg.lag_z:
            return "parcial", zt
        return "atrasado", zt

    # ------------------------------------------------------------------ o percurso
    def run(self, start: Optional[datetime] = None, end: Optional[datetime] = None) -> dict[str, TestResult]:
        c = self.cfg
        if not self.impulses:
            self.detect(start, end)
        tests = {name: TestResult(name, ("líder opera o próprio impulso" if k == 0 else f"líder → {k} atrasado{'s' if k > 1 else ''}"), [], c.equity, c.risk_pct)
                 for name, k in TESTS}
        self.naive = TestResult("INGÊNUA", "todo atrasado, direção do líder", [], c.equity, c.risk_pct)
        open_until_by_test: dict[str, dict[str, datetime]] = {name: {} for name, _ in TESTS}
        for imp in self.impulses:
            self.leader_board.setdefault(imp.leader, {"impulsos": 0, "primeiro": 0, "simultâneo": 0})
            self.leader_board[imp.leader]["impulsos"] += 1
            now = imp.time
            t_in = now + timedelta(seconds=c.delay_sec + c.latency_sec)
            candidates: list[tuple[float, str, PropTrade]] = []
            # ---- TESTE A: o líder opera o próprio impulso (continuação × reversão decidida pelo histórico)
            self_tr = self._trade_for(imp, imp.leader, now, t_in, self_trade=True)
            if self_tr is not None and self._free("A", imp.leader, now, open_until_by_test):
                tests["A"].trades.append(self_tr)
                open_until_by_test["A"][imp.leader] = self_tr.exit_time
            # ---- atrasados
            co = []
            for target in self.series:
                if target == imp.leader:
                    continue
                cls, zt = self._classify(imp, target)
                self.classification[cls] += 1
                if cls == "simultâneo":
                    co.append(target)
                    continue
                if cls != "atrasado":
                    continue
                naive_tr = self._trade_for(imp, target, now, t_in, naive=True)
                if naive_tr is not None:
                    self.naive.trades.append(naive_tr)
                tr = self._trade_for(imp, target, now, t_in)
                if tr is not None:
                    candidates.append((tr.edge_atr, target, tr))
            imp.co_movers = co
            if co:
                self.leader_board[imp.leader]["simultâneo"] += 1
            else:
                self.leader_board[imp.leader]["primeiro"] += 1
            candidates.sort(key=lambda x: (-x[0], x[1]))
            for name, k in TESTS:
                if k == 0:
                    continue
                taken = 0
                for edge, target, tr in candidates:
                    if taken >= k:
                        break
                    if not self._free(name, target, now, open_until_by_test):
                        continue
                    tests[name].trades.append(tr)
                    open_until_by_test[name][target] = tr.exit_time
                    taken += 1
        for t in tests.values():
            t.run_equity()
        self.naive.run_equity()
        self.results = tests
        return tests

    def _free(self, test: str, target: str, now: datetime, open_until_by_test: dict) -> bool:
        until = open_until_by_test[test].get(target)
        if until is not None and now < until:
            self.classification["já_posicionado"] += 1
            return False
        return True

    def _trade_for(self, imp: Impulse, target: str, now: datetime, t_in: datetime, naive: bool = False, self_trade: bool = False) -> Optional[PropTrade]:
        """Mede o resultado (para o futuro), simula a sombra (para o futuro) e devolve a operação real se o edge estiver comprovado agora."""
        c = self.cfg
        ser = self.series[target]
        atr = ser.atr_at(now)
        if atr <= 0:
            return None
        key = (imp.leader, imp.direction, target)
        # medição do alvo (conhecida em t + horizonte) — uma vez por impulso × alvo
        if not naive:
            j = ser.index_at_or_before(now)
            lag_z = ser.z_at(j) or 0.0
            o = measure_outcome(imp, target, ser, now, c.horizon_min, c.eval_min, lag_z, atr)
            if o is None:
                return None
            self.outcomes.setdefault(key, []).append(o)
        if naive:
            tr = simulate_propagation_trade(ser.path, atr, t_in, imp.direction, TAKE_MAX_ATR, float(c.horizon_min), c.slippage_atr, c.commission_atr, now)
            if tr is None:
                return None
            tr.leader, tr.target, tr.p, tr.z, tr.session, tr.news = imp.leader, target, 0.5, imp.z, imp.session, imp.news
            return tr
        st = self.stats_for(key, now)
        if st.n < c.min_n or st.sign == 0:
            return None
        take = st.take_atr(imp.move_atr)
        timeout = st.timeout_min(c.horizon_min)
        tr = simulate_propagation_trade(ser.path, atr, t_in, st.sign, take, timeout, c.slippage_atr, c.commission_atr, now)
        if tr is None:
            return None
        tr.leader, tr.target, tr.p, tr.edge_atr, tr.z, tr.session, tr.news = imp.leader, target, st.p, st.shadow_net, imp.z, imp.session, imp.news
        # sombra: conhecida no fecho da operação — é ela que prova (ou não) o edge líquido antes de qualquer entrada real
        self.shadows.setdefault(key, []).append((tr.exit_time, tr.net_atr))
        if st.p < c.p_min or st.shadow_n < c.min_n or st.shadow_net <= c.edge_min_atr:
            return None
        self.stats_at_entry[key] = st
        return tr

    # ------------------------------------------------------------------ relatório
    def final_stats(self) -> list[RelationStats]:
        last = [s.times[-1] for s in self.series.values() if s.times]
        far = (max(last) if last else datetime(2100, 1, 1)) + timedelta(days=3650)
        keys = sorted(set(self.outcomes), key=lambda k: (k[0], -k[1], k[2]))
        return [self.stats_for(k, far) for k in keys]

    def render(self, split: Optional[datetime] = None, resolution: str = "M1") -> str:
        c = self.cfg
        L = [f"⚡ LEADER PROPAGATION ENGINE 6.0 — impulso no líder → atrasados que o histórico diz que acompanham · {resolution} · walk-forward por construção",
             f"   detector: |Δ{c.impulse_min}min| ≥ {c.z:g}σ (σ das últimas {c.lookback_hours:g} h) e ≥ {c.min_move_atr:g} ATR · atrasado = |z| < {c.lag_z:g} · "
             f"horizonte {c.horizon_min} min · direção julgada aos {c.eval_min} min · entrada {c.delay_sec:g}s+{c.latency_sec:g}s após o instante conhecido",
             f"   entra só com n ≥ {c.min_n}, P encolhida ≥ {c.p_min:.0%} e expectancy-sombra líquida > {c.edge_min_atr:g} ATR · take = P{c.take_quantile * 100:.0f} da fração "
             f"transmitida × impulso · stop {STOP_ATR:g} ATR (1R) · tempo limite 2× mediana da reação · slippage {c.slippage_atr:g} ATR/perna · risco {c.risk_pct:g}%"]
        # detector / leader board
        L.append("")
        L.append("🔎 DETECTOR · LEADER BOARD (quem se mexeu primeiro)")
        L.append(f"  {'líder':<8}{'impulsos':>9}{'↑':>5}{'↓':>5}{'primeiro':>10}{'simult.':>9}{'z med':>7}{'|Δ| ATR':>9}{'|Δ| %':>8}{'c/ notícia':>11}  sessões")
        for sym in self.leaders:
            imps = [i for i in self.impulses if i.leader == sym]
            if not imps:
                L.append(f"  {sym:<8}{0:>9}   (nenhum impulso acima do limiar)")
                continue
            lb = self.leader_board.get(sym, {"primeiro": 0, "simultâneo": 0})
            sess: dict[str, int] = {}
            for i in imps:
                sess[i.session] = sess.get(i.session, 0) + 1
            L.append(f"  {sym:<8}{len(imps):>9}{sum(1 for i in imps if i.direction > 0):>5}{sum(1 for i in imps if i.direction < 0):>5}{lb['primeiro']:>10}{lb['simultâneo']:>9}"
                     f"{statistics.median(i.z for i in imps):>7.1f}{statistics.median(i.move_atr for i in imps):>9.2f}{statistics.median(abs(i.move_pct) for i in imps):>8.2f}"
                     f"{sum(1 for i in imps if i.news):>11}  " + " ".join(f"{k} {v}" for k, v in sorted(sess.items(), key=lambda kv: -kv[1])))
        cl = self.classification
        L.append(f"  alvos no instante do impulso: atrasados {cl['atrasado']} · parciais (já andaram) {cl['parcial']} · simultâneos {cl['simultâneo']} · sem dados {cl['sem_dados']}"
                 f" · recusados por já estar posicionado {cl['já_posicionado']}")
        # lag map / clock
        L.append("")
        L.append("🕒 LAG MAP · REACTION CLOCK — líder × direção → alvo (todo o período; a decisão em cada impulso usou só o passado)")
        L.append(f"  {'líder':<8}{'dir':^4}{'alvo':<8}{'n':>4}{'P mesma':>8}{'P opos.':>8}{'prev':>6}{'P̂':>6}{'reação med/P25/P75':>21}{'cont.':>7}{'MFE':>7}{'MAE':>7}{'fração':>8}"
                 f"{'sombras':>8}{'exp.líq':>9}{'PF':>6}  veredito")
        for st in self.final_stats():
            react = (f"{st.react_med:.0f}/{st.react_p25:.0f}/{st.react_p75:.0f} min" if st.react_med is not None else "não reagiu")
            pf = "∞" if st.shadow_pf == float("inf") else (f"{st.shadow_pf:.2f}" if st.shadow_pf is not None else "—")
            if st.n < c.min_n or st.shadow_n < c.min_n:
                ver = "⚪ amostra"
            elif st.p >= c.p_min and st.shadow_net > c.edge_min_atr:
                ver = "🟢 edge líquido" if st.shadow_net >= 0.05 else "🟡 edge fino"
            elif st.p >= c.p_min:
                ver = "🔴 custo consome"
            else:
                ver = "🔴 sem relação"
            self_tag = " (próprio)" if st.target == st.leader else ""
            L.append(f"  {st.leader:<8}{'↑' if st.direction > 0 else '↓':^4}{st.target:<8}{st.n:>4}{st.p_same:>8.0%}{st.p_opp:>8.0%}{'↑' if st.sign > 0 else '↓':>6}{st.p:>6.0%}"
                     f"{react:>21}{st.continuation:>7.0%}{st.mfe_med:>7.2f}{st.mae_med:>7.2f}{st.ratio_q:>8.2f}{st.shadow_n:>8}{st.shadow_net:>+9.3f}{pf:>6}  {ver}{self_tag}")
        L.append("  P̂ = probabilidade da direção prevista encolhida para 50 % conforme n · fração = quantil da parte do impulso do líder transmitida ao alvo (ATR/ATR)"
                 " · exp.líq = expectancy líquida (ATR) das operações-sombra")
        # testes
        L.append("")
        L.append("🧪 TESTES A → E (mesmos impulsos, mesmos custos; B→E escolhem os atrasados de maior edge-sombra)")
        L.append(f"  {'teste':<8}{'':<26}{'n':>5}{'acerto':>7}{'expect.':>10}{'R líq.':>10}{'PF':>6}{'retorno':>9}{'DD':>8}{'custo':>9}{'dur.':>8}{'pico':>5}")
        for name, _ in TESTS:
            L.append(self.results[name].row(split))
        L.append(self.naive.row(split))
        best = max((r for r in self.results.values() if r.n >= 20), key=lambda r: r.net_r - r.max_dd_r, default=None)
        if best is not None:
            L.append(f"  melhor (R líquido − DD, n ≥ 20): TESTE {best.name} — {best.label}: {best.n} operações, expectancy {best.expectancy:+.2f}R, {best.net_r:+.1f}R, retorno {best.ret_pct:+.1f}%")
        else:
            L.append("  nenhum teste com n ≥ 20 — amostra insuficiente para escolher (afrouxe --z / --min-n ou amplie o período)")
        L.append("  1R = stop de 0,5 ATR · custo = spread + 2× slippage + comissão · DD em % do capital composto · pico = posições simultâneas · ⚪ n < 20 · 🟢 expectancy > 0,05R e PF > 1,1")
        # por alvo / por líder (teste E, o mais amplo)
        e = self.results["E"]
        if e.n:
            L.append("")
            L.append("📊 TESTE E por alvo, por líder, por sessão e com/sem notícia (n · R líquido · acerto)")
            for title, fn in (("alvo", lambda t: t.target), ("líder", lambda t: t.leader), ("sessão", lambda t: t.session), ("notícia", lambda t: "c/ notícia" if t.news else "sem notícia")):
                parts = " · ".join(f"{k} {n} {r:+.1f}R {w:.0%}" for k, (n, r, w) in sorted(e.by(fn).items(), key=lambda kv: -kv[1][1]))
                L.append(f"  {title:<8} {parts}")
            reasons = e.by(lambda t: t.exit_reason)
            L.append("  saídas   " + " · ".join(f"{k} {n} ({r:+.1f}R)" for k, (n, r, _) in sorted(reasons.items(), key=lambda kv: -kv[1][0])))
            L.append(f"  FOLLOW (só stop/tempo, sem take) no mesmo conjunto: {e.follow_r:+.1f}R vs ADAPT {e.net_r:+.1f}R — se FOLLOW for maior, o take está cortando lucro")
        L.append("")
        L.append("  leitura: a hipótese 'um ativo se move primeiro e puxa os outros; o atraso pode ser monetizado' só está provada onde o teste B→E supera o A e a INGÊNUA"
                 " com n ≥ 20 e expectancy líquida positiva nas duas metades. Nada aqui é gatilho para o live sem passar pelo ciclo de vida.")
        return "\n".join(L)

    def to_json(self, split: Optional[datetime] = None) -> dict:
        def tr(t: PropTrade) -> dict:
            return {"leader": t.leader, "target": t.target, "time": t.time.isoformat(), "exit_time": t.exit_time.isoformat(), "sign": t.sign, "net_r": round(t.net_r, 4),
                    "follow_r": round(t.follow_atr / STOP_ATR, 4), "cost_r": round(t.cost_r, 4), "take_atr": round(t.take_atr, 3), "timeout_min": t.timeout_min,
                    "exit": t.exit_reason, "p": round(t.p, 3), "edge_atr": round(t.edge_atr, 4), "z": round(t.z, 2), "session": t.session, "news": t.news,
                    "mfe_atr": round(t.mfe_atr, 3), "mae_atr": round(t.mae_atr, 3)}

        def res(r: TestResult) -> dict:
            d = {"n": r.n, "win_rate": round(r.win_rate, 4), "expectancy_r": round(r.expectancy, 4), "net_r": round(r.net_r, 3), "follow_r": round(r.follow_r, 3),
                 "profit_factor": (None if r.pf is None or r.pf == float("inf") else round(r.pf, 3)), "return_pct": round(r.ret_pct, 3), "end_equity": r.end_equity,
                 "max_dd_pct": round(r.max_dd_pct, 3), "max_dd_r": round(r.max_dd_r, 3), "cost_r": round(r.cost_r, 3), "avg_minutes": round(r.avg_minutes, 1),
                 "peak_concurrent": r.peak_concurrent, "verdict": r.verdict(), "by_target": r.by(lambda t: t.target), "by_leader": r.by(lambda t: t.leader)}
            if split is not None:
                a, b = r.halves(split)
                d["first_half"] = {"n": a.n, "expectancy_r": round(a.expectancy, 4), "net_r": round(a.net_r, 3)}
                d["second_half"] = {"n": b.n, "expectancy_r": round(b.expectancy, 4), "net_r": round(b.net_r, 3)}
            d["trades"] = [tr(t) for t in r.trades]
            return d

        cfg = {k: v for k, v in self.cfg.__dict__.items()}
        return {"engine": "LEADER_PROPAGATION_ENGINE", "version": "6.0", "config": cfg, "leaders": self.leaders,
                "impulses": [{"leader": i.leader, "time": i.time.isoformat(), "direction": i.direction, "z": round(i.z, 2), "move_atr": round(i.move_atr, 3),
                              "move_pct": round(i.move_pct, 4), "session": i.session, "news": i.news, "co_movers": i.co_movers} for i in self.impulses],
                "classification": self.classification, "leader_board": self.leader_board,
                "lag_map": [{"leader": s.leader, "direction": s.direction, "target": s.target, "n": s.n, "p_same": round(s.p_same, 4), "p_opp": round(s.p_opp, 4),
                             "sign": s.sign, "p": round(s.p, 4), "react_med": s.react_med, "react_p25": s.react_p25, "react_p75": s.react_p75,
                             "continuation": round(s.continuation, 4), "mfe_med": round(s.mfe_med, 4), "mae_med": round(s.mae_med, 4), "ratio_q": round(s.ratio_q, 4),
                             "shadow_n": s.shadow_n, "shadow_net_atr": round(s.shadow_net, 4)} for s in self.final_stats()],
                "tests": {name: res(r) for name, r in self.results.items()}, "naive": res(self.naive)}


def split_time(impulses: Sequence[Impulse]) -> Optional[datetime]:
    if not impulses:
        return None
    ts = sorted(i.time for i in impulses)
    return ts[len(ts) // 2]


def save_json(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
