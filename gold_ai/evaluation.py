"""Avaliação honesta do sistema (GOLD AI 2.0).

Não basta "quantas vezes o ouro subiu depois do sinal". Mede-se:
  PRECISÃO   dos sinais de compra e de venda
  RECALL     quantos movimentos relevantes o sistema detectou (antes de ficarem evidentes)
  MFE / MAE  máxima excursão favorável / adversa após o sinal
  LEAD TIME  minutos entre o sinal e o momento em que o movimento ficou evidente
  ⏱️ GOLD LEAD SCORE — antecipação média dos acertos e fração de acertos com antecedência útil

Inclui um Backtester que reconstrói MarketSnapshots a partir de séries históricas
alinhadas e um walk-forward (calibração no treino, avaliação fora da amostra).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from .config import EngineConfig
from .engine import GoldAIEngine
from .models import Candle, Direction, MarketSnapshot, SignalType
from .technical import atr as _atr


# --------------------------------------------------------------------------- estruturas
@dataclass
class SignalRecord:
    time: datetime
    direction: str            # ALTA | BAIXA
    type: str                 # GOLD BUY, GOLD PRE-MOVE, GOLD WATCH...
    price: float
    atr: float
    evidence_level: int = 0
    probability: float = 0.0
    confidence: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)     # ratio -1..+1 por fator
    technical: dict[str, float] = field(default_factory=dict)   # indicadores assinados -1..+1


def technical_details(a) -> dict[str, float]:
    """Indicadores técnicos assinados (-1..+1) do H1/H4 para o scoreboard: RSI, VWAP, EMA, MACD, ADX-tendência."""
    out: dict[str, float] = {}
    for r in a.technical:
        if r.timeframe not in ("H1", "H4") or "dados insuficientes" in r.notes:
            continue
        tf = r.timeframe
        if r.rsi is not None:
            out[f"RSI_{tf}"] = max(-1.0, min(1.0, (r.rsi - 50) / 25))
        if r.vwap_position is not None:
            out[f"VWAP_{tf}"] = max(-1.0, min(1.0, r.vwap_position / 2))
        if r.ema_alignment is not None:
            out[f"EMA_{tf}"] = r.ema_alignment
        if r.macd_hist is not None and r.atr:
            out[f"MACD_{tf}"] = max(-1.0, min(1.0, r.macd_hist / (r.atr * 0.5)))
    return out


def record_from(a, sig, atr: float) -> "SignalRecord":
    return SignalRecord(a.time, sig.direction.value, sig.type.value, a.price, atr, int(a.evidence_level),
                        max(a.prob_up, a.prob_down), a.confidence,
                        {f.name: f.ratio for f in a.factors if f.available}, technical_details(a))


@dataclass
class Move:
    start: datetime
    direction: str
    evident_at: datetime      # quando o preço andou ≥ threshold na direção
    magnitude: float
    detected_by: Optional[SignalRecord] = None


@dataclass
class SignalOutcome:
    signal: SignalRecord
    result: str               # ACERTO | ERRO | LATERAL
    mfe: float
    mae: float
    lead_time_min: Optional[float]
    evident_at: Optional[datetime]


@dataclass
class Metrics:
    n_signals: int = 0
    n_buy: int = 0
    n_sell: int = 0
    precision_buy: Optional[float] = None
    precision_sell: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None
    n_moves: int = 0
    n_moves_detected: int = 0
    mfe_avg: Optional[float] = None
    mae_avg: Optional[float] = None
    mfe_mae_ratio: Optional[float] = None
    lead_time_avg: Optional[float] = None
    lead_time_median: Optional[float] = None
    lead_times: list[float] = field(default_factory=list)
    gold_lead_score: Optional[float] = None
    by_level: dict[int, dict[str, float]] = field(default_factory=dict)
    outcomes: list[SignalOutcome] = field(default_factory=list)

    def render(self) -> str:
        f = lambda x, s="": ("n/d" if x is None else f"{x:.1%}" if s == "%" else f"{x:.1f}{s}")  # noqa: E731
        lines = [
            "📊 AVALIAÇÃO — GOLD AI",
            f"Sinais: {self.n_signals} (compra {self.n_buy}, venda {self.n_sell})",
            f"Precisão: total {f(self.precision, '%')} · compra {f(self.precision_buy, '%')} · venda {f(self.precision_sell, '%')}",
            f"Recall: {f(self.recall, '%')} ({self.n_moves_detected}/{self.n_moves} movimentos relevantes detectados antes de ficarem evidentes)",
            f"MFE médio: {f(self.mfe_avg)} · MAE médio: {f(self.mae_avg)} · MFE/MAE: {f(self.mfe_mae_ratio)}",
            f"Lead time (acertos): média {f(self.lead_time_avg, ' min')} · mediana {f(self.lead_time_median, ' min')}",
            f"⏱️ GOLD LEAD SCORE: {f(self.gold_lead_score)}/100",
        ]
        if self.lead_times:
            lines.append("  Antecedência por sinal: " + ", ".join(f"{x:.0f} min" for x in self.lead_times[:12]) + (" …" if len(self.lead_times) > 12 else ""))
        for lvl in sorted(self.by_level):
            d = self.by_level[lvl]
            lines.append(f"  Nível {lvl}: n={int(d['n'])} precisão={d['precision']:.0%} lead médio={d['lead']:.0f} min")
        return "\n".join(lines)


# --------------------------------------------------------------------------- movimentos relevantes
def detect_moves(path: Sequence[tuple[datetime, float]], threshold: float, horizon_min: int = 240) -> list[Move]:
    """Movimento relevante = deslocamento ≥ threshold (ex.: 1 ATR) dentro do horizonte, sem antes
    andar ≥ threshold na direção contrária. Movimentos sobrepostos na mesma direção são fundidos."""
    moves: list[Move] = []
    n = len(path)
    i = 0
    while i < n:
        t0, p0 = path[i]
        found = None
        for j in range(i + 1, n):
            tj, pj = path[j]
            if (tj - t0) > timedelta(minutes=horizon_min):
                break
            if pj - p0 >= threshold:
                found = Move(t0, "ALTA", tj, pj - p0)
                break
            if p0 - pj >= threshold:
                found = Move(t0, "BAIXA", tj, p0 - pj)
                break
        if found:
            if moves and moves[-1].direction == found.direction and found.start <= moves[-1].evident_at:
                moves[-1].magnitude = max(moves[-1].magnitude, found.magnitude)
            else:
                moves.append(found)
            # pula até o momento em que ficou evidente
            while i < n and path[i][0] < found.evident_at:
                i += 1
        else:
            i += 1
    return moves


# --------------------------------------------------------------------------- avaliação
def evaluate_signal(sig: SignalRecord, path: Sequence[tuple[datetime, float]], threshold: float, horizon_min: int) -> SignalOutcome:
    sign = 1.0 if sig.direction == "ALTA" else -1.0
    mfe = mae = 0.0
    result, evident, lead = "LATERAL", None, None
    for t, p in path:
        if t < sig.time:
            continue
        if t - sig.time > timedelta(minutes=horizon_min):
            break
        exc = (p - sig.price) * sign
        mfe, mae = max(mfe, exc), max(mae, -exc)
        if result == "LATERAL":
            if exc >= threshold:
                result, evident, lead = "ACERTO", t, (t - sig.time).total_seconds() / 60
            elif exc <= -threshold:
                result, evident = "ERRO", t
    return SignalOutcome(sig, result, round(mfe, 2), round(mae, 2), lead, evident)


def evaluate(signals: Iterable[SignalRecord], path: Sequence[tuple[datetime, float]], threshold: float,
             horizon_min: int = 240, useful_lead_min: float = 5.0) -> Metrics:
    sigs = sorted(signals, key=lambda s: s.time)
    m = Metrics(n_signals=len(sigs))
    outcomes = [evaluate_signal(s, path, threshold, horizon_min) for s in sigs]
    m.outcomes = outcomes
    buys = [o for o in outcomes if o.signal.direction == "ALTA"]
    sells = [o for o in outcomes if o.signal.direction == "BAIXA"]
    m.n_buy, m.n_sell = len(buys), len(sells)
    hit = lambda os_: (sum(1 for o in os_ if o.result == "ACERTO") / len(os_)) if os_ else None  # noqa: E731
    m.precision_buy, m.precision_sell, m.precision = hit(buys), hit(sells), hit(outcomes)
    if outcomes:
        m.mfe_avg = statistics.fmean(o.mfe for o in outcomes)
        m.mae_avg = statistics.fmean(o.mae for o in outcomes)
        m.mfe_mae_ratio = (m.mfe_avg / m.mae_avg) if m.mae_avg else None
    leads = [o.lead_time_min for o in outcomes if o.result == "ACERTO" and o.lead_time_min is not None]
    m.lead_times = leads
    if leads:
        m.lead_time_avg, m.lead_time_median = statistics.fmean(leads), statistics.median(leads)
    # recall: movimento relevante detectado se houve sinal na mesma direção entre (start − horizonte) e evident_at
    moves = detect_moves(path, threshold, horizon_min)
    m.n_moves = len(moves)
    for mv in moves:
        for s in sigs:
            if s.direction == mv.direction and mv.evident_at - timedelta(minutes=horizon_min) <= s.time < mv.evident_at:
                mv.detected_by = s
                break
    m.n_moves_detected = sum(1 for mv in moves if mv.detected_by)
    m.recall = (m.n_moves_detected / m.n_moves) if m.n_moves else None
    # GOLD LEAD SCORE: acertos com antecedência útil ÷ total de sinais, escalado pela antecedência média (satura em 30 min)
    if outcomes:
        useful = sum(1 for o in outcomes if o.result == "ACERTO" and (o.lead_time_min or 0) >= useful_lead_min)
        lead_factor = min(1.0, (m.lead_time_avg or 0) / 30.0)
        m.gold_lead_score = round(100.0 * (useful / len(outcomes)) * (0.5 + 0.5 * lead_factor), 1)
    for lvl in sorted({o.signal.evidence_level for o in outcomes}):
        os_ = [o for o in outcomes if o.signal.evidence_level == lvl]
        ls = [o.lead_time_min for o in os_ if o.result == "ACERTO" and o.lead_time_min is not None]
        m.by_level[lvl] = {"n": float(len(os_)), "precision": hit(os_) or 0.0, "lead": statistics.fmean(ls) if ls else 0.0}
    return m


# --------------------------------------------------------------------------- histórico e backtest
@dataclass
class HistoryFrame:
    """Séries H1 alinhadas por timestamp. Apenas `xau` é obrigatória."""

    xau: list[Candle]
    dxy: list[Candle] = field(default_factory=list)
    us10y: list[Candle] = field(default_factory=list)          # em % (ex.: ^TNX)
    vix: list[Candle] = field(default_factory=list)
    spx: list[Candle] = field(default_factory=list)
    real_yield_daily: list[tuple[datetime, float]] = field(default_factory=list)  # FRED DFII10 (%)

    @staticmethod
    def _at(series: list[Candle], t: datetime) -> Optional[int]:
        lo, hi = 0, len(series) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if series[mid].time <= t:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        return best

    def snapshot_at(self, i: int, window_bars: int = 1, lookback: int = 300) -> MarketSnapshot:
        """Snapshot no índice i da série XAU, com candles H1/H4/D1/W1 reamostrados e variações
        recentes calculadas na janela de `window_bars` horas (sem olhar o futuro)."""
        from .data.yahoo import resample  # import local para manter o pacote leve

        t = self.xau[i].time
        h1 = self.xau[max(0, i - lookback + 1): i + 1]
        h4 = resample(h1, 240)
        d1 = resample(self.xau[max(0, i - lookback * 8 + 1): i + 1], 1440)
        w1 = resample(d1, 10080)
        s = MarketSnapshot(time=t, price=h1[-1].close, candles={"H1": h1, "H4": h4, "D1": d1, "W1": w1})
        s.atr = _atr(h1) or 0.0
        ref = h1[-1 - window_bars] if len(h1) > window_bars else h1[0]
        s.price_change_pct = (h1[-1].close / ref.close - 1) * 100 if ref.close else 0.0
        vol = sum(c.volume for c in h1[-window_bars:])
        if vol > 0:
            s.order_flow_imbalance = (sum(c.volume for c in h1[-window_bars:] if c.close > c.open) - sum(c.volume for c in h1[-window_bars:] if c.close < c.open)) / vol

        def change(series: list[Candle], pct: bool) -> tuple[Optional[float], Optional[float]]:
            j = self._at(series, t)
            if j is None or j - window_bars < 0:
                return None, None
            a, b = series[j].close, series[j - window_bars].close
            return a, ((a / b - 1) * 100 if pct else a - b) if b else None

        s.dxy, s.dxy_change_pct = change(self.dxy, True)
        y, dy = change(self.us10y, False)
        s.us10y, s.us10y_change_bp = y, (dy * 100 if dy is not None else None)
        s.vix, s.vix_change_pct = change(self.vix, True)
        _, s.equity_change_pct = change(self.spx, True)
        if self.real_yield_daily:
            pts = [(d, v) for d, v in self.real_yield_daily if d <= t]
            if len(pts) >= 2:
                s.real_yield_10y = pts[-1][1]
                s.real_yield_change_bp = (pts[-1][1] - pts[-2][1]) * 100
        elif s.us10y_change_bp is not None:
            s.real_yield_change_bp = s.us10y_change_bp  # aproximação: sem breakeven, usa nominal
        return s


@dataclass
class BacktestResult:
    metrics: Metrics
    signals: list[SignalRecord]
    n_steps: int
    cfg: EngineConfig
    trades: Optional[object] = None   # trading.RStats (2.2)
    trade_rows: list[dict] = field(default_factory=list)
    opportunity: Optional[object] = None  # opportunity.OpportunityReport (3.0)
    decisions: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    funnel: Optional[object] = None       # opportunity.Funnel

    def render(self) -> str:
        out = f"BACKTEST — {self.n_steps} passos, {len(self.signals)} sinais\n" + self.metrics.render()
        if self.trades is not None:
            out += "\n\n" + self.trades.render()
        if self.opportunity is not None:
            out += "\n\n" + self.opportunity.render()
        if self.funnel is not None:
            out += "\n\n" + self.funnel.render()
        return out


class Backtester:
    def __init__(self, frame: HistoryFrame, cfg: Optional[EngineConfig] = None, warmup: int = 220, step: int = 1,
                 threshold_atr: float = 1.0, horizon_min: int = 240, include_watch: bool = False, simulate_trades: bool = True,
                 adaptive_exit: bool = True) -> None:
        self.frame = frame
        self.simulate_trades = simulate_trades
        self.adaptive_exit = adaptive_exit
        self.cfg = cfg or EngineConfig()
        self.warmup, self.step = warmup, step
        self.threshold_atr, self.horizon_min = threshold_atr, horizon_min
        self.include_watch = include_watch

    def run(self, start: Optional[int] = None, end: Optional[int] = None, cfg: Optional[EngineConfig] = None) -> BacktestResult:
        cfg = cfg or self.cfg
        engine = GoldAIEngine(cfg)
        xau = self.frame.xau
        start = max(self.warmup, start or self.warmup)
        end = min(len(xau), end or len(xau))
        from .monitor import ManagedTrade, Thesis, TradeMonitor
        from .trading import MaxProfitEngine, r_stats, simulate_all

        mpe = MaxProfitEngine(horizon_min=self.horizon_min)
        monitor = TradeMonitor()
        signals: list[SignalRecord] = []
        trade_rows: list[dict] = []
        managed: list[tuple[ManagedTrade, dict, int]] = []   # (trade, row, índice de abertura)
        horizon_bars = self.horizon_min // 60
        prev_i = start - 1
        from .opportunity import DecisionRecord, Funnel, funnel_stage, hypothetical_trade
        decisions: list[DecisionRecord] = []
        entries: list[tuple] = []
        funnel = Funnel()
        for i in range(start, end, self.step):
            snap = self.frame.snapshot_at(i)
            a, sig = engine.run_cycle(snap)
            # OPPORTUNITY ENGINE: cada passo é uma oportunidade analisada
            d_dir = a.direction if a.direction != Direction.LATERAL else a.premove.direction
            entered = sig is not None and sig.type not in (SignalType.RISK, SignalType.REVERSAL, SignalType.WATCH) and sig.direction != Direction.LATERAL
            rule = "ENTRADA" if entered else ("SEM_VANTAGEM" if not a.has_edge else "SEM_SINAL" if sig is None else "SEM_SINAL")
            # FUNIL: primeira etapa em que a oportunidade caiu (no backtest a entrada = sinal operacional)
            decision_text = "🟢 PAPER OPEN" if entered else ("" if sig is None else f"NO_TRADE — sinal {sig.type.value} não é operacional")
            funnel.add(*funnel_stage(a, sig, engine.gate.last_reason, decision_text, cfg))
            rec = DecisionRecord(a.time, a.price, a.score, d_dir.value, rule, "", snap.atr or 0.0, None, int(a.evidence_level), a.confidence)
            if abs(a.score) >= 15 and d_dir != Direction.LATERAL:
                rec.hypothetical_r = hypothetical_trade(rec, xau[i + 1: i + 1 + self.horizon_min // 60 + 2], self.horizon_min)
            decisions.append(rec)
            if entered:
                entries.append((a.time, sig.direction.value))
            # 2.3: operações abertas continuam sendo analisadas a cada passo (ADAPTIVE EXIT)
            if self.adaptive_exit:
                for tr, row, i0 in list(managed):
                    if monitor.check_path(tr, xau[prev_i + 1: i + 1]) is None and tr.status == "OPEN":
                        if i - i0 >= horizon_bars:
                            tr.close(tr.r_at(xau[i].close), "HORIZON", xau[i].time)
                        else:
                            monitor.evaluate(tr, a, snap)
                    if tr.status == "CLOSED":
                        row["results"]["adaptive"] = tr.result_r
                        row["adaptive_reason"] = tr.close_reason
                        managed.remove((tr, row, i0))
            prev_i = i
            if sig is None or sig.type in (SignalType.RISK, SignalType.REVERSAL):
                continue
            if sig.type == SignalType.WATCH and not self.include_watch:
                continue
            if sig.direction == Direction.LATERAL:
                continue
            signals.append(record_from(a, sig, snap.atr))
            if self.simulate_trades and sig.type != SignalType.WATCH:
                plan = mpe.plan(a, snap, sig.direction, sig.type.value)
                sim = simulate_all(plan, xau[i + 1: i + 1 + horizon_bars + 2], self.horizon_min)
                row = {"type": sig.type.value, "profile": sim["profile"], "results": sim["results"], "time": a.time, "r_value": plan.r_value,
                       "score": a.score, "direction": sig.direction.value, "entry": a.price}
                trade_rows.append(row)
                if self.adaptive_exit:
                    managed.append((ManagedTrade(len(trade_rows), plan, Thesis.from_assessment(a, sig.direction)), row, i))
        for tr, row, i0 in managed:  # ainda abertas no fim do período
            tr.close(tr.r_at(xau[min(end, len(xau)) - 1].close), "FIM", xau[min(end, len(xau)) - 1].time)
            row["results"]["adaptive"] = tr.result_r
        path = [(c.time, c.close) for c in xau[start:end]]
        atrs = [s.atr for s in signals if s.atr] or [_atr(xau[start - 20:end]) or 1.0]
        threshold = self.threshold_atr * statistics.fmean(atrs)
        from .opportunity import opportunity_report
        curve_rows = [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
        opp = opportunity_report(decisions, path, entries, threshold, self.horizon_min, curve_rows)
        return BacktestResult(evaluate(signals, path, threshold, self.horizon_min), signals, len(range(start, end, self.step)), cfg,
                              r_stats(trade_rows) if self.simulate_trades else None, trade_rows, opp, decisions, entries, funnel)


@dataclass
class WalkForwardResult:
    folds: list[tuple[EngineConfig, BacktestResult]]
    oos: Metrics
    oos_trades: Optional[object] = None  # trading.RStats fora da amostra
    oos_opportunity: Optional[object] = None  # opportunity.OpportunityReport agregado OOS
    oos_funnel: Optional[object] = None       # opportunity.Funnel agregado OOS

    def render(self) -> str:
        lines = ["🔁 WALK-FORWARD (fora da amostra) — treina → testa → avança → treina → testa"]
        for k, (cfg, r) in enumerate(self.folds, 1):
            lines.append(f"  fold {k}: buy≥{cfg.buy} sell≤{cfg.sell} conf≥{cfg.min_confirmations} → sinais={r.metrics.n_signals} "
                         f"precisão={'n/d' if r.metrics.precision is None else f'{r.metrics.precision:.0%}'} lead={'n/d' if r.metrics.lead_time_avg is None else f'{r.metrics.lead_time_avg:.0f} min'}")
        lines.append("AGREGADO OOS:")
        lines.append(self.oos.render())
        if self.oos_trades is not None:
            lines.append("")
            lines.append(self.oos_trades.render())
        if self.oos_opportunity is not None:
            lines.append("")
            lines.append(self.oos_opportunity.render())
        if self.oos_funnel is not None:
            lines.append("")
            lines.append(self.oos_funnel.render("FUNIL DE ENTRADA (fora da amostra, todos os folds de teste)"))
        return "\n".join(lines)


def _objective(m: Metrics) -> float:
    """Precisão × recall (F1) ponderada pelo GOLD LEAD SCORE; penaliza ausência de sinais."""
    if m.n_signals == 0 or m.precision is None:
        return 0.0
    p, r = m.precision, m.recall or 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return f1 * (0.5 + 0.5 * (m.gold_lead_score or 0) / 100)


def walk_forward(bt: Backtester, n_folds: int = 4, grid: Optional[list[dict]] = None, mode: str = "rolling",
                 train_folds: int = 2) -> WalkForwardResult:
    """Treina → testa → avança a janela → treina de novo → testa.

    mode="rolling": janela de treino de tamanho fixo (`train_folds` folds) que avança;
    mode="anchored": treino sempre desde o início. O teste nunca se sobrepõe ao treino e
    nunca é usado para escolher parâmetros (calibração só no treino)."""
    grid = grid or [{"buy": b, "sell": -b, "min_confirmations": c} for b in (40, 50, 60) for c in (2, 3)]
    n = len(bt.frame.xau)
    usable = n - bt.warmup
    fold_len = usable // (n_folds + train_folds)
    folds: list[tuple[EngineConfig, BacktestResult]] = []
    for k in range(n_folds):
        train_end = bt.warmup + (train_folds + k) * fold_len
        train_start = bt.warmup if mode == "anchored" else train_end - train_folds * fold_len
        test_end = min(n, train_end + fold_len)
        best_cfg, best_obj = None, -1.0
        for params in grid:
            cfg = EngineConfig(**{**bt.cfg.__dict__, **params, "weights": dict(bt.cfg.weights)})
            r = bt.run(train_start, train_end, cfg)
            obj = _objective(r.metrics)
            if obj > best_obj:
                best_cfg, best_obj = cfg, obj
        assert best_cfg is not None
        folds.append((best_cfg, bt.run(train_end, test_end, best_cfg)))
    # agrega OOS
    all_sigs = [s for _, r in folds for s in r.signals]
    path = [(c.time, c.close) for c in bt.frame.xau[bt.warmup + train_folds * fold_len:]]
    atrs = [s.atr for s in all_sigs if s.atr] or [1.0]
    oos = evaluate(all_sigs, path, bt.threshold_atr * statistics.fmean(atrs), bt.horizon_min)
    from .trading import r_stats
    rows = [r for _, res in folds for r in res.trade_rows]
    # oportunidades OOS agregadas: decisões, entradas e curva de limiar de todos os folds de teste
    from .opportunity import opportunity_report
    decisions = [d for _, res in folds if res.opportunity for d in res.decisions]
    entries = [e for _, res in folds if res.opportunity for e in res.entries]
    curve_rows = [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
    opp = opportunity_report(decisions, path, entries, bt.threshold_atr * statistics.fmean(atrs), bt.horizon_min, curve_rows) if decisions else None
    from .opportunity import Funnel
    fun = Funnel()
    for _, res in folds:
        if res.funnel is not None:
            fun = fun.merge(res.funnel)
    return WalkForwardResult(folds, oos, r_stats(rows) if rows else None, opp, fun if fun.analyses else None)


# --------------------------------------------------------------------------- 2.1 validação consolidada
def validate(frame: HistoryFrame, cfg: Optional[EngineConfig] = None, n_folds: int = 4, step: int = 1, warmup: int = 220,
             threshold_atr: float = 1.0, horizon_min: int = 240, mode: str = "rolling", audit_every: int = 25):
    """Backtest + walk-forward rolante + calibração + score por fator + auditoria anti look-ahead."""
    from .validation import ValidationReport, calibration_table, factor_scoreboard, lookahead_audit

    bt = Backtester(frame, cfg, warmup=warmup, step=step, threshold_atr=threshold_atr, horizon_min=horizon_min)
    violations: list[str] = []
    audited = 0
    for i in range(warmup, len(frame.xau), max(1, audit_every)):
        audited += 1
        violations += [f"i={i}: {v}" for v in lookahead_audit(frame.snapshot_at(i))]
    full = bt.run()
    wf = walk_forward(bt, n_folds=n_folds, mode=mode)
    oos = wf.oos.outcomes
    calib = calibration_table((o.signal.probability, o.result == "ACERTO") for o in oos if o.result != "LATERAL")
    board = factor_scoreboard([{"direction": o.signal.direction, "hit": o.result == "ACERTO", "factors": o.signal.factors,
                                "technical": o.signal.technical} for o in oos if o.result != "LATERAL"])
    rep = ValidationReport(full.render(), wf.render(), calib, board, violations, audited)
    # oportunidades fora da amostra: agrega os folds de teste
    from .opportunity import opportunity_report
    rep.opportunity_text = "OOS por fold:\n" + "\n".join(f"  fold {k}: captura {('n/d' if r.opportunity.capture_rate is None else f'{r.opportunity.capture_rate:.0%}')} · "
                                                         f"entry rate {('n/d' if r.opportunity.entry_rate is None else f'{r.opportunity.entry_rate:.0%}')}"
                                                         + (" ⚠️ OVERFILTER" if r.opportunity.overfilter else "") for k, (_, r) in enumerate(wf.folds, 1))
    return rep


# --------------------------------------------------------------------------- 4.0: validação multi-mercado
@dataclass
class MarketValidation:
    symbol: str
    n_trades: int
    expectancy: float
    profit_factor: Optional[float]
    win_rate: float
    capture_rate: Optional[float]
    entry_rate: Optional[float]
    confidence: object            # selector.StatConfidence
    status: str                   # 🟢 🟡 🔴
    report: object                # ValidationReport


def validate_markets(frames: dict, cfg_factory=None, n_folds: int = 4, step: int = 1, warmup: int = 220, horizon_min: int = 240,
                     strategy: str = "adaptive") -> list[MarketValidation]:
    """Responde: qual mercado apresenta melhor expectativa FORA DA AMOSTRA, ponderada pelo tamanho da amostra?
    O ranking usa a expectancy encolhida pela confiança estatística — 37 trades a +0.9R não vencem 487 a +0.42R."""
    from .config import EngineConfig
    from .markets import get_market
    from .selector import statistical_confidence

    out: list[MarketValidation] = []
    for symbol, frame in frames.items():
        spec = get_market(symbol)
        cfg = cfg_factory(symbol) if cfg_factory else EngineConfig(factor_signs=dict(spec.factor_signs), symbol=symbol)
        rep = validate(frame, cfg, n_folds=n_folds, step=step, warmup=warmup, horizon_min=horizon_min, audit_every=200)
        bt = Backtester(frame, cfg, warmup=warmup, step=step, horizon_min=horizon_min)
        wf = walk_forward(bt, n_folds=n_folds)
        rows = [r for _, res in wf.folds for r in res.trade_rows]
        rs = [row["results"].get(strategy, row["results"].get("3R", 0.0)) for row in rows]
        conf = statistical_confidence(rs)
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (None if not wins else float("inf"))
        caps = [res.opportunity.capture_rate for _, res in wf.folds if res.opportunity and res.opportunity.capture_rate is not None]
        ents = [res.opportunity.entry_rate for _, res in wf.folds if res.opportunity and res.opportunity.entry_rate is not None]
        status = "🟢" if (conf.level in ("HIGH", "MEDIUM") and conf.shrunk > 0.1) else "🟡" if conf.shrunk > 0 else "🔴"
        out.append(MarketValidation(symbol, len(rs), conf.expectancy, (round(pf, 2) if pf not in (None, float("inf")) else pf), (len(wins) / len(rs)) if rs else 0.0,
                                    (statistics.fmean(caps) if caps else None), (statistics.fmean(ents) if ents else None), conf, status, rep))
    return sorted(out, key=lambda m: -m.confidence.shrunk)


def render_market_validation(rows: Sequence[MarketValidation]) -> str:
    lines = ["🧪 VALIDAÇÃO MULTI-MERCADO (fora da amostra, walk-forward) — ranking pela expectancy ajustada à amostra",
             f"{'Ativo':<8}{'Trades':>7}{'Expect.':>9}{'Ajust.':>8}{'PF':>7}{'Win':>6}{'Capture':>9}{'Entry':>7}  Conf.   Status"]
    for m in rows:
        pf = "n/d" if m.profit_factor is None else ("∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}")
        cap = "n/d" if m.capture_rate is None else f"{m.capture_rate:.0%}"
        ent = "n/d" if m.entry_rate is None else f"{m.entry_rate:.0%}"
        lines.append(f"{m.symbol:<8}{m.n_trades:>7}{m.expectancy:>+9.2f}{m.confidence.shrunk:>+8.2f}{pf:>7}{m.win_rate:>6.0%}{cap:>9}{ent:>7}  {m.confidence.level:<7} {m.status}")
    if rows:
        best = rows[0]
        lines.append(f"Melhor expectativa OOS ajustada: {best.symbol} ({best.confidence.render()})")
        low = [m.symbol for m in rows if m.confidence.level == "LOW" and m.expectancy > rows[0].expectancy]
        if low:
            lines.append(f"⚠️ {', '.join(low)}: expectancy maior mas amostra pequena — NÃO escolher automaticamente")
    return "\n".join(lines)
