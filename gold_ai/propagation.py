"""LEADER PROPAGATION ENGINE — "um ativo se move primeiro e puxa os outros; o atraso pode ser monetizado?" (6.0)

Backtest específico, separado do cérebro por ativo:

  DETECTOR   impulso no líder = retorno de 5 min ≥ z desvios da volatilidade de 1 min ANTES do impulso (limiar estatístico,
             não "0,20 %"); um impulso por janela de `cooldown` minutos.
  LAG MAP    para cada par líder → alvo, no MESMO instante: quanto o alvo já andou (atrasado ou não) e o que fez depois
             em 5/15/30/60 min: P(mesma direção), fração transmitida (movimento do alvo ÷ movimento do líder, em σ),
             tempo até reagir (1 σ), MFE/MAE em 60 min.
  APRENDER   na PRIMEIRA metade do período (por líder→alvo e contexto: com/sem notícia): direção esperada (pode ser inversa),
             fração transmitida (alvo adaptativo), expectancy líquida do FOLLOW.
  TESTAR     na SEGUNDA metade, sem tocar no que foi aprendido: FOLLOW = entra no alvo 1 min depois do impulso, stop 1,5 σ,
             alvo = fração transmitida × impulso do líder, saída no horizonte; custo = spread típico do mercado.
  A…E        A = cada ativo sozinho (é a estimativa por ativo, citada); B…E = líder → 1, 2, 3, 4 atrasados por impulso,
             escolhidos pela expectancy líquida aprendida (só com edge > 0): n, win, E, R líquido, capital a 1 %, drawdown.

Nada aqui altera o live. O mapa aprendido vai para dados/propagacao.json para um modo de execução futuro, se a prova sair.
"""
from __future__ import annotations

import json
import math
import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Sequence

HORIZONS = (5, 15, 30, 60)
SESSIONS = (("Ásia", 0, 7), ("Londres", 7, 13), ("NY", 13, 21), ("fecho", 21, 24))
FALLBACK_SPREAD = {"USDX": 0.02, "DXY": 0.02}


def session_name(t: datetime) -> str:
    h = t.astimezone(timezone.utc).hour
    for name, a, b in SESSIONS:
        if a <= h < b:
            return name
    return "fecho"


# --------------------------------------------------------------------------- séries alinhadas por minuto
class MinuteSeries:
    """Candles M1 de um símbolo com σ de 1 minuto (EW-MAD × 1,25, meia-vida 120 min) e busca por instante."""

    def __init__(self, symbol: str, candles: Sequence, halflife_min: int = 120) -> None:
        cs = sorted(candles, key=lambda c: c.time)
        self.symbol = symbol
        self.t = [int(c.time.timestamp() // 60) for c in cs]          # minuto inteiro
        self.close = [float(c.close) for c in cs]
        self.high = [float(c.high) for c in cs]
        self.low = [float(c.low) for c in cs]
        self.time = [c.time for c in cs]
        n = len(cs)
        self.sigma = [0.0] * n
        if n:
            alpha = 1 - 0.5 ** (1.0 / halflife_min)
            mad = None
            for i in range(1, n):
                d = abs(self.close[i] - self.close[i - 1])
                mad = d if mad is None else (1 - alpha) * mad + alpha * d
                self.sigma[i] = 1.25 * mad if mad else 0.0
            for i in range(min(n, halflife_min)):                     # aquecimento: sem σ confiável
                self.sigma[i] = 0.0

    def __len__(self) -> int:
        return len(self.t)

    def index_at(self, minute: int) -> int:
        """Índice do último candle com minuto ≤ `minute` (−1 se não há)."""
        return bisect_right(self.t, minute) - 1

    def close_at(self, minute: int, max_gap: int = 30) -> Optional[float]:
        i = self.index_at(minute)
        if i < 0 or minute - self.t[i] > max_gap:
            return None
        return self.close[i]


# --------------------------------------------------------------------------- impulsos do líder
@dataclass
class Impulse:
    leader: str
    minute: int
    time: datetime
    direction: int            # +1 / −1
    z: float                  # |retorno 5 min| / (σ1 × √5)
    move_sigma: float         # |retorno 5 min| em σ1 do líder (= z × √5)
    session: str
    with_news: bool


def detect_impulses(s: MinuteSeries, z_min: float = 4.0, window: int = 5, cooldown_min: int = 60,
                    news_minutes: Optional[Sequence[int]] = None, news_window: int = 30) -> list[Impulse]:
    out: list[Impulse] = []
    last = -10 ** 9
    news_sorted = sorted(news_minutes or [])

    def near_news(m: int) -> bool:
        j = bisect_right(news_sorted, m + news_window)
        return j > 0 and m - news_sorted[j - 1] <= news_window

    for i in range(window, len(s)):
        if s.t[i] - s.t[i - window] != window:          # buraco na série: não é um impulso de 5 min
            continue
        sig = s.sigma[i - window]
        if sig <= 0:
            continue
        move = s.close[i] - s.close[i - window]
        z = abs(move) / (sig * math.sqrt(window))
        if z < z_min or s.t[i] - last < cooldown_min:
            continue
        last = s.t[i]
        out.append(Impulse(s.symbol, s.t[i], s.time[i], 1 if move > 0 else -1, round(z, 2), round(z * math.sqrt(window), 2),
                           session_name(s.time[i]), near_news(s.t[i])))
    return out


# --------------------------------------------------------------------------- resposta do alvo
@dataclass
class Response:
    leader: str
    target: str
    minute: int
    time: datetime
    leader_dir: int
    leader_move_sigma: float
    with_news: bool
    session: str
    own_move_now: float                       # movimento do alvo nos mesmos 5 min, em σ do alvo (sinal = direção do líder)
    fwd: dict                                 # h → movimento do alvo de t até t+h em σ do alvo, sinal = direção do líder
    ttr_min: Optional[int]                    # minutos até |movimento| ≥ 1 σ (None = não reagiu em 60 min)
    mfe60: float
    mae60: float


def measure_response(imp: Impulse, tgt: MinuteSeries, window: int = 5, horizon: int = 60) -> Optional[Response]:
    i0 = tgt.index_at(imp.minute)
    if i0 < window or imp.minute - tgt.t[i0] > 2:
        return None
    sig = tgt.sigma[i0]
    if sig <= 0:
        return None
    p0 = tgt.close[i0]
    s = imp.direction
    own = s * (p0 - tgt.close[max(0, tgt.index_at(imp.minute - window))]) / sig
    fwd = {}
    for h in HORIZONS:
        c = tgt.close_at(imp.minute + h)
        fwd[h] = (s * (c - p0) / sig) if c is not None else None
    if fwd[max(HORIZONS)] is None:
        return None
    ttr, mfe, mae = None, 0.0, 0.0
    j = i0 + 1
    while j < len(tgt) and tgt.t[j] <= imp.minute + horizon:
        up = s * (tgt.high[j] - p0) / sig if s > 0 else s * (tgt.low[j] - p0) / sig
        dn = s * (tgt.low[j] - p0) / sig if s > 0 else s * (tgt.high[j] - p0) / sig
        mfe = max(mfe, up)
        mae = max(mae, -dn)
        if ttr is None and abs(tgt.close[j] - p0) / sig >= 1.0:
            ttr = tgt.t[j] - imp.minute
        j += 1
    return Response(imp.leader, tgt.symbol, imp.minute, imp.time, s, imp.move_sigma, imp.with_news, imp.session, round(own, 2),
                    {h: (None if v is None else round(v, 3)) for h, v in fwd.items()}, ttr, round(mfe, 2), round(mae, 2))


# --------------------------------------------------------------------------- aprendizado do mapa líder → alvo
@dataclass
class PairStat:
    leader: str
    target: str
    context: str                      # "todas" | "com notícia" | "sem notícia"
    n: int = 0
    p_same_30: float = 0.5            # P(alvo na mesma direção do líder aos 30 min)
    sign: int = 1                     # +1 segue o líder · −1 vai contra (relação inversa aprendida)
    frac30: float = 0.0               # fração transmitida mediana aos 30 min (σ alvo ÷ σ líder), já com o sinal aprendido
    ttr_med: Optional[float] = None
    ttr_p25: Optional[float] = None
    ttr_p75: Optional[float] = None
    mfe60: float = 0.0
    mae60: float = 0.0
    e_train: float = 0.0              # expectancy líquida do FOLLOW no treino (R)
    win_train: float = 0.0
    n_train_trades: int = 0

    @property
    def strength(self) -> float:
        return abs(self.p_same_30 - 0.5)

    @property
    def is_edge(self) -> bool:
        return self.n >= 30 and self.strength >= 0.10 and self.e_train > 0

    def row(self) -> str:
        ttr = "—" if self.ttr_med is None else f"{self.ttr_med:.0f} ({self.ttr_p25:.0f}–{self.ttr_p75:.0f})"
        return (f"{self.leader:<7}→ {self.target:<7} {self.context:<11} n={self.n:<4} mesma dir 30m {self.p_same_30:>4.0%}  "
                f"{'segue' if self.sign > 0 else 'CONTRA':<6} fração {self.frac30:+.2f}  reação {ttr:<14} MFE60 {self.mfe60:.2f}σ MAE60 {self.mae60:.2f}σ  "
                f"FOLLOW treino E {self.e_train:+.2f}R win {self.win_train:.0%} (n={self.n_train_trades})  {'✅ edge' if self.is_edge else '·'}")


def _prop_quantile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    xs = sorted(vals)
    k = (len(xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def learn_pair(resps: list[Response], leader: str, target: str, context: str) -> PairStat:
    st = PairStat(leader, target, context, n=len(resps))
    if not resps:
        return st
    same = [1.0 if (r.fwd[30] or 0.0) > 0 else 0.0 for r in resps]
    st.p_same_30 = round(sum(same) / len(same), 3)
    st.sign = 1 if st.p_same_30 >= 0.5 else -1
    fracs = [st.sign * (r.fwd[30] or 0.0) / max(r.leader_move_sigma, 1e-9) for r in resps]
    st.frac30 = round(statistics.median(fracs), 3)
    ttrs = [float(r.ttr_min) for r in resps if r.ttr_min is not None]
    if ttrs:
        st.ttr_med, st.ttr_p25, st.ttr_p75 = statistics.median(ttrs), _prop_quantile(ttrs, 0.25), _prop_quantile(ttrs, 0.75)
    st.mfe60 = round(statistics.median([r.mfe60 for r in resps]), 2)
    st.mae60 = round(statistics.median([r.mae60 for r in resps]), 2)
    return st


# --------------------------------------------------------------------------- FOLLOW: a operação no atrasado
@dataclass
class FollowTrade:
    leader: str
    target: str
    time: datetime
    direction: int
    r: float                  # líquido de custo
    minutes: int
    reason: str
    with_news: bool


def follow_trade(imp: Impulse, tgt: MinuteSeries, st: PairStat, spread: float, stop_sigma: float = 1.5, horizon: int = 60,
                 delay_min: int = 1, min_target_sigma: float = 0.5) -> Optional[FollowTrade]:
    """Entra no alvo `delay_min` depois do impulso, na direção aprendida; stop `stop_sigma` σ; alvo = fração transmitida ×
    impulso do líder (em σ do alvo), no mínimo `min_target_sigma` σ; sai no horizonte. Custo = spread ÷ distância do stop."""
    i_entry = tgt.index_at(imp.minute + delay_min)
    if i_entry < 0 or (imp.minute + delay_min) - tgt.t[i_entry] > 2:
        return None
    sig = tgt.sigma[i_entry]
    if sig <= 0:
        return None
    d = imp.direction * st.sign
    entry = tgt.close[i_entry]
    stop_dist = stop_sigma * sig
    target_dist = max(min_target_sigma, st.frac30 * imp.move_sigma) * sig
    cost_r = spread / stop_dist if stop_dist > 0 else 0.0
    j = i_entry + 1
    while j < len(tgt) and tgt.t[j] <= imp.minute + horizon:
        hi, lo = tgt.high[j], tgt.low[j]
        adverse = (entry - lo) if d > 0 else (hi - entry)
        favor = (hi - entry) if d > 0 else (entry - lo)
        if adverse >= stop_dist:                        # regra conservadora: stop antes do alvo no mesmo candle
            return FollowTrade(imp.leader, tgt.symbol, imp.time, d, round(-1.0 - cost_r, 3), tgt.t[j] - imp.minute, "STOP", imp.with_news)
        if favor >= target_dist:
            return FollowTrade(imp.leader, tgt.symbol, imp.time, d, round(target_dist / stop_dist - cost_r, 3), tgt.t[j] - imp.minute, "ALVO", imp.with_news)
        j += 1
    last = tgt.close[j - 1] if j - 1 > i_entry else entry
    return FollowTrade(imp.leader, tgt.symbol, imp.time, d, round(d * (last - entry) / stop_dist - cost_r, 3), horizon, "HORIZONTE", imp.with_news)


# --------------------------------------------------------------------------- testes A…E
@dataclass
class KResult:
    k: int
    trades: list = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def e(self) -> float:
        return sum(t.r for t in self.trades) / self.n if self.n else 0.0

    @property
    def win(self) -> float:
        return sum(1 for t in self.trades if t.r > 0) / self.n if self.n else 0.0

    def equity(self, risk_pct: float = 1.0, start: float = 50000.0) -> tuple[float, float]:
        eq, peak, dd = start, start, 0.0
        for t in sorted(self.trades, key=lambda x: x.time):
            eq += eq * risk_pct / 100.0 * t.r
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        return eq, dd

    def row(self, risk_pct: float) -> str:
        eq, dd = self.equity(risk_pct)
        avg_min = sum(t.minutes for t in self.trades) / self.n if self.n else 0.0
        lb = self.e - 1.96 * (statistics.pstdev([t.r for t in self.trades]) / math.sqrt(self.n)) if self.n > 1 else 0.0
        return (f"  {'A cada ativo sozinho' if self.k == 0 else f'{chr(65 + self.k)} líder → {self.k} atrasado(s)':<26} n={self.n:<4} win {self.win:>4.0%}  "
                f"E {self.e:+.2f}R  LB {lb:+.2f}R  Σ {sum(t.r for t in self.trades):+.1f}R  capital {eq:,.0f} ({(eq / 50000 - 1):+.1%})  DD {dd:.1%}  {avg_min:.0f} min/op")


@dataclass
class PropagationReport:
    period: str
    split_at: datetime
    leaders: list
    targets: list
    n_impulses_train: int
    n_impulses_test: int
    pairs: list                       # PairStat (treino), todos os contextos
    k_results: list                   # KResult k=1..4 (teste)
    pair_test: dict                   # (leader, target) → KResult com as operações de teste daquele par
    z_min: float
    risk_pct: float

    def render(self) -> str:
        L = [f"⚡ LEADER PROPAGATION — impulso ≥ {self.z_min:g}σ (5 min) no líder → o que os outros fizeram depois · {self.period}",
             f"aprendido até {self.split_at:%d/%m/%Y} ({self.n_impulses_train} impulsos) · testado depois ({self.n_impulses_test} impulsos) · "
             f"líderes {', '.join(self.leaders)} · alvos {', '.join(self.targets)}",
             "",
             "LAG MAP (treino) — P(mesma direção aos 30 min), direção aprendida, fração transmitida, minutos até 1σ (mediana e P25–P75), FOLLOW no treino:"]
        for st in sorted(self.pairs, key=lambda s: (s.leader, s.target, s.context)):
            if st.n >= 10:
                L.append("  " + st.row())
        L += ["", "TESTE FORA DA AMOSTRA (2ª metade) — FOLLOW nos atrasados com edge aprendido (E treino > 0, n ≥ 30, |P − 50 %| ≥ 10 pp):"]
        for kr in self.k_results:
            L.append(kr.row(self.risk_pct))
        L += ["", "POR PAR (teste) — só pares com edge aprendido:"]
        for (ld, tg), kr in sorted(self.pair_test.items()):
            if kr.n:
                L.append(f"  {ld:<7}→ {tg:<7} n={kr.n:<4} win {kr.win:>4.0%}  E {kr.e:+.2f}R  Σ {sum(t.r for t in kr.trades):+.1f}R  "
                         f"com notícia n={sum(1 for t in kr.trades if t.with_news)} E {(sum(t.r for t in kr.trades if t.with_news) / max(1, sum(1 for t in kr.trades if t.with_news))):+.2f}R · "
                         f"sem n={sum(1 for t in kr.trades if not t.with_news)} E {(sum(t.r for t in kr.trades if not t.with_news) / max(1, sum(1 for t in kr.trades if not t.with_news))):+.2f}R")
        edge_pairs = [s for s in self.pairs if s.is_edge and s.context == "todas"]
        L += ["", "LEITURA:"]
        if not edge_pairs:
            L.append("  nenhum par líder→alvo com edge no treino (n ≥ 30, |P − 50 %| ≥ 10 pp e FOLLOW líquido > 0): a propagação em minutos, "
                     "neste período e com estes custos, não deu vantagem explorável — a hipótese não se confirma aqui.")
        else:
            best = max(self.k_results, key=lambda k: (k.e if k.n >= 20 else -9))
            L.append(f"  {len(edge_pairs)} par(es) com edge no treino; no teste o melhor k foi {best.k} (E {best.e:+.2f}R, n={best.n}). "
                     "Só vale se o E do teste for positivo com n ≥ 30 E com limite inferior > 0 — senão é ruído que sobreviveu ao treino.")
        L.append("  A (cada ativo sozinho) é a ESTIMATIVA por ativo já feita (walk-forward): compare o E líquido de lá com as linhas B…E daqui.")
        L.append("  custos: spread típico de cada mercado dividido pela distância do stop (1,5 σ de 1 min) — em minutos o custo pesa; "
                 "slippage e latência não estão modelados.")
        return "\n".join(L)

    def to_json(self) -> dict:
        return {"period": self.period, "split_at": self.split_at.isoformat(), "z_min": self.z_min,
                "pairs": [{"leader": s.leader, "target": s.target, "context": s.context, "n": s.n, "p_same_30": s.p_same_30, "sign": s.sign,
                           "frac30": s.frac30, "ttr_med": s.ttr_med, "mfe60": s.mfe60, "mae60": s.mae60, "e_train": s.e_train,
                           "win_train": s.win_train, "is_edge": s.is_edge} for s in self.pairs],
                "test": [{"k": k.k, "n": k.n, "e": round(k.e, 3), "win": round(k.win, 3)} for k in self.k_results]}


def run_propagation(series: dict[str, MinuteSeries], leaders: Sequence[str], targets: Sequence[str], spreads: dict[str, float],
                    news_minutes: Sequence[int] = (), z_min: float = 4.0, cooldown_min: int = 60, horizon: int = 60,
                    risk_pct: float = 1.0, split_at: Optional[datetime] = None, log: Optional[Callable[[str], None]] = None) -> PropagationReport:
    all_t = [s.time[0] for s in series.values() if len(s)] + [s.time[-1] for s in series.values() if len(s)]
    t0, t1 = min(all_t), max(all_t)
    split = split_at or (t0 + (t1 - t0) / 2)
    split_min = int(split.timestamp() // 60)
    impulses: dict[str, list[Impulse]] = {}
    for ld in leaders:
        impulses[ld] = detect_impulses(series[ld], z_min, cooldown_min=cooldown_min, news_minutes=news_minutes)
        if log:
            log(f"  {ld}: {len(impulses[ld])} impulsos ≥ {z_min:g}σ ({sum(1 for i in impulses[ld] if i.with_news)} com notícia)")
    # respostas
    resp: dict[tuple[str, str], list[Response]] = {}
    for ld, imps in impulses.items():
        for tg in targets:
            if tg == ld:
                continue
            rows = [r for r in (measure_response(imp, series[tg]) for imp in imps) if r is not None]
            resp[(ld, tg)] = rows
    # aprender no treino (+ FOLLOW no treino para a expectancy líquida)
    pairs: list[PairStat] = []
    learned: dict[tuple[str, str], PairStat] = {}
    for (ld, tg), rows in resp.items():
        train = [r for r in rows if r.minute < split_min]
        for ctx, sel in (("todas", train), ("com notícia", [r for r in train if r.with_news]), ("sem notícia", [r for r in train if not r.with_news])):
            st = learn_pair(sel, ld, tg, ctx)
            if st.n:
                imps_ctx = [i for i in impulses[ld] if i.minute < split_min and (ctx == "todas" or (i.with_news == (ctx == "com notícia")))]
                trades = [t for t in (follow_trade(i, series[tg], st, spreads.get(tg, 0.0), horizon=horizon) for i in imps_ctx) if t is not None]
                st.n_train_trades = len(trades)
                st.e_train = round(sum(t.r for t in trades) / len(trades), 3) if trades else 0.0
                st.win_train = round(sum(1 for t in trades if t.r > 0) / len(trades), 3) if trades else 0.0
            pairs.append(st)
            if ctx == "todas":
                learned[(ld, tg)] = st
    # teste: por impulso, ranquear alvos pelo E de treino (só com edge) e entrar nos top-k
    k_results = [KResult(k) for k in range(1, 5)]
    pair_test: dict[tuple[str, str], KResult] = {key: KResult(0) for key in learned}
    n_test = 0
    for ld, imps in impulses.items():
        for imp in imps:
            if imp.minute < split_min:
                continue
            n_test += 1
            cands = sorted(((learned[(ld, tg)].e_train, tg) for tg in targets if tg != ld and learned.get((ld, tg)) and learned[(ld, tg)].is_edge),
                           reverse=True)
            trades = []
            for _, tg in cands:
                t = follow_trade(imp, series[tg], learned[(ld, tg)], spreads.get(tg, 0.0), horizon=horizon)
                if t is not None:
                    trades.append(t)
                    pair_test[(ld, tg)].trades.append(t)
            for kr in k_results:
                kr.trades += trades[:kr.k]
    n_train = sum(1 for imps in impulses.values() for i in imps if i.minute < split_min)
    return PropagationReport(f"{t0:%d/%m/%Y} → {t1:%d/%m/%Y}", split, list(leaders), list(targets), n_train, n_test, pairs, k_results,
                             pair_test, z_min, risk_pct)
