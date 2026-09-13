#!/usr/bin/env python3
"""GOLD AI ENGINE — arquivo único (XAU/USD).

Centro Global de Inteligência do Ouro: motor probabilístico de antecipação que
transforma dólar, juros reais, Fed, inflação, geopolítica, fluxo, COT, opções,
sentimento e técnico em SCORE (-100..+100) × PROBABILIDADE × CONFIANÇA, com
detecção de pré-movimento, reversão, risco sistêmico, filtro de 3 confirmações,
anti-spam, alertas Telegram e memória de previsões (SQLite).

Sem dependências externas. Python 3.10+.

Uso:
    python gold_ai_engine.py demo                 # 7 cenários sintéticos + sinais (Telegram dry-run)
    python gold_ai_engine.py demo --db gold.db    # registra previsões
    python gold_ai_engine.py stats --db gold.db   # taxa de acerto / poder dos fatores
    python gold_ai_engine.py event --actual 0.1 --dxy -0.3 --us10y -5 --real -4 --gold 0.4 --flow 0.3
    python gold_ai_engine.py run --once -v        # um ciclo do loop contínuo

Telegram real: exporte TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID e use --send.
Dados reais: implemente uma classe com .snapshot() -> MarketSnapshot e troque SampleSource.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable, Optional, Protocol, Sequence

__version__ = "0.1.0"


# ============================================================================
# CONFIG
# ============================================================================

"""Configuração do motor: pesos, limiares e horizontes (Diretriz §19, §21, §27, §28, §37)."""




# Peso máximo de cada fator no GOLD AI SCORE. A soma é 100, de modo que o
# score final fica naturalmente em -100..+100 (Diretriz §19).
DEFAULT_WEIGHTS: dict[str, int] = {
    "dolar": 15,
    "juros_reais": 18,
    "fed": 12,
    "inflacao": 8,
    "geopolitica": 10,
    "fluxo": 7,
    "cot": 5,
    "opcoes": 6,
    "tecnico": 13,
    "sentimento": 6,
}

# Agrupamento dos fatores em famílias INDEPENDENTES para o filtro de
# confirmações (Diretriz §27): um sinal forte exige >= 3 famílias alinhadas.
FACTOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "macro": ("fed", "inflacao"),
    "juros": ("juros_reais",),
    "dolar": ("dolar",),
    "fluxo": ("fluxo", "cot", "opcoes"),
    "tecnico": ("tecnico",),
    "sentimento": ("sentimento",),
    "geopolitica": ("geopolitica",),
}

# Pesos dos timeframes na nota técnica agregada (Diretriz §18).
TIMEFRAME_WEIGHTS: dict[str, float] = {
    "M1": 0.04,
    "M5": 0.08,
    "M15": 0.10,
    "M30": 0.12,
    "H1": 0.16,
    "H4": 0.20,
    "D1": 0.20,
    "W1": 0.10,
}

TIMEFRAME_GROUPS: dict[str, tuple[str, ...]] = {
    "microestrutura": ("M1", "M5", "M15"),
    "intraday": ("M30", "H1"),
    "swing": ("H4", "D1"),
    "macrotendencia": ("D1", "W1"),
}

# Horizontes de previsão (Diretriz §21).
HORIZONS: dict[str, str] = {
    "curtissimo": "5–30 min",
    "curto": "30 min–4h",
    "intraday": "4–24h",
    "swing": "1–5 dias",
    "macro": "1–4 semanas",
}


@dataclass
class EngineConfig:
    weights: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    # Classificação por score (Diretriz §28).
    strong_buy: int = 70
    buy: int = 50
    sell: int = -50
    strong_sell: int = -70

    # Filtro contra falsos sinais (Diretriz §27).
    min_confirmations: int = 3
    # Contribuição mínima (fração do peso máximo) para uma família "confirmar".
    family_confirmation_ratio: float = 0.35

    # Pré-movimento (Diretriz §14, §22): fundamentos fortes e preço ainda não confirmou.
    premove_fundamental_threshold: float = 35.0  # score fundamental (sem técnico) em -100..+100
    premove_price_confirm_ratio: float = 0.35     # nota técnica abaixo disso = "não confirmou"
    chase_atr_multiple: float = 2.0               # movimento > N ATR = estágio 3 (não perseguir)

    # Reversão (Diretriz §16, §26).
    reversal_risk_threshold: float = 60.0

    # Probabilidades: inclinação da logística que converte score em prob.
    prob_slope: float = 0.045
    lateral_base: float = 0.12

    # Anti-spam (Diretriz §37).
    min_score_change_to_alert: int = 20
    min_seconds_between_alerts: int = 900

    # Risco sistêmico excepcional (Diretriz §11, §37 item 7).
    exceptional_systemic_risk: float = 75.0

    # Janela (min) antes de evento de alto impacto em que a confiança é penalizada.
    event_window_minutes: int = 90


# ============================================================================
# MODELS
# ============================================================================

"""Modelos de dados do GOLD AI ENGINE."""




class Direction(str, Enum):
    ALTA = "ALTA"
    BAIXA = "BAIXA"
    LATERAL = "LATERAL"


class Stage(str, Enum):
    """Sistema de antecipação (Diretriz §22)."""

    PRE_MOVIMENTO = "PRÉ-MOVIMENTO"   # fundamentos mudaram, preço não confirmou
    CONFIRMACAO = "CONFIRMAÇÃO"       # preço começa a acompanhar
    MOVIMENTO = "MOVIMENTO"           # já se moveu — evitar perseguir
    NEUTRO = "NEUTRO"


class SignalType(str, Enum):
    """Classificação de sinais (Diretriz §28)."""

    STRONG_BUY = "GOLD STRONG BUY"
    BUY = "GOLD BUY"
    NEUTRAL = "GOLD NEUTRAL"
    SELL = "GOLD SELL"
    STRONG_SELL = "GOLD STRONG SELL"
    PRE_MOVE = "GOLD PRE-MOVE"
    REVERSAL = "GOLD REVERSAL ALERT"
    RISK = "GOLD SYSTEMIC RISK"


class Sentiment(str, Enum):
    """Sentimento global (Diretriz §12)."""

    MUITO_OTIMISTA = "MUITO OTIMISTA PARA OURO"
    OTIMISTA = "OTIMISTA"
    NEUTRO = "NEUTRO"
    BAIXISTA = "BAIXISTA"
    MUITO_BAIXISTA = "MUITO BAIXISTA"


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class EconomicEvent:
    """Evento de alto impacto (Diretriz §32, §33)."""

    name: str
    time: datetime
    impact: str = "ALTO"  # BAIXO | MÉDIO | ALTO | MUITO ALTO
    consensus: Optional[float] = None
    previous: Optional[float] = None
    actual: Optional[float] = None
    kind: str = "generic"  # cpi | pce | nfp | fomc | unemployment | gdp | pmi | speech | geopolitical | generic
    unit: str = ""

    def surprise(self) -> Optional[float]:
        """Resultado − consenso (na unidade do evento)."""
        if self.actual is None or self.consensus is None:
            return None
        return self.actual - self.consensus


@dataclass
class NewsItem:
    """Notícia em tempo real (Diretriz §13)."""

    headline: str
    source: str = ""
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    category: str = "generic"  # macro | fed | geopolitical | systemic | china | flow | generic
    gold_impact: float = 0.0   # -1..+1 avaliado externamente (ex.: LLM) ou por regra
    priced_in: float = 0.0     # 0..1 — quanto o mercado já havia antecipado
    interpretation: str = ""


@dataclass
class MarketSnapshot:
    """Leitura instantânea das cinco camadas (Diretriz §2).

    Todas as variações são "recentes" (janela do ciclo de análise, por exemplo
    última hora). Valores ausentes (None) reduzem a confiança, não quebram o motor.
    """

    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    price: float = 0.0
    price_change_pct: float = 0.0  # variação recente do XAU/USD (%)
    atr: float = 0.0               # ATR do timeframe de referência (H1)

    # Dólar (§6)
    dxy: Optional[float] = None
    dxy_change_pct: Optional[float] = None

    # Juros (§5)
    us2y: Optional[float] = None
    us10y: Optional[float] = None
    us10y_change_bp: Optional[float] = None
    real_yield_10y: Optional[float] = None
    real_yield_change_bp: Optional[float] = None
    breakeven_10y_change_bp: Optional[float] = None
    fed_cut_prob_change_pp: Optional[float] = None  # variação da prob. de corte na próxima reunião (p.p.)
    fed_tone: Optional[float] = None                # -1 (hawkish) .. +1 (dovish)

    # Inflação (§4)
    inflation_surprise_sigma: Optional[float] = None  # (resultado − consenso) / desvio; >0 = inflação acima
    inflation_trend: Optional[float] = None           # -1 desacelerando .. +1 acelerando

    # Geopolítica (§10) e risco sistêmico (§11)
    geopolitical_risk: Optional[float] = None         # 0..100
    geopolitical_risk_change: Optional[float] = None  # variação recente
    vix: Optional[float] = None
    vix_change_pct: Optional[float] = None
    credit_spread_bp: Optional[float] = None          # HY OAS
    credit_spread_change_bp: Optional[float] = None
    equity_change_pct: Optional[float] = None         # S&P 500
    bank_stress: Optional[float] = None               # 0..100 (CDS bancos, notícias)

    # Fluxo (§7)
    etf_flow_musd: Optional[float] = None             # entradas líquidas ETFs (USD mi)
    futures_volume_ratio: Optional[float] = None      # volume atual / média
    open_interest_change_pct: Optional[float] = None
    central_bank_buying_tonnes: Optional[float] = None
    order_flow_imbalance: Optional[float] = None      # -1..+1 (agressão compradora − vendedora)

    # COT (§8)
    cot_managed_money_net: Optional[float] = None         # contratos líquidos
    cot_managed_money_net_change: Optional[float] = None  # variação semanal
    cot_managed_money_percentile: Optional[float] = None  # 0..100 (extremo = risco de reversão)
    cot_commercial_net_change: Optional[float] = None

    # Opções (§9)
    put_call_ratio: Optional[float] = None
    implied_vol: Optional[float] = None
    implied_vol_change_pct: Optional[float] = None
    gamma_wall_above: Optional[float] = None  # strike com grande concentração acima
    gamma_wall_below: Optional[float] = None

    # Sentimento (§12)
    sentiment: Optional[float] = None       # -1..+1
    sentiment_change: Optional[float] = None

    # Correlatos (§3)
    silver_change_pct: Optional[float] = None
    oil_change_pct: Optional[float] = None
    btc_change_pct: Optional[float] = None
    usdcnh_change_pct: Optional[float] = None

    # Técnico: candles por timeframe (§17, §18)
    candles: dict[str, list[Candle]] = field(default_factory=dict)

    # Notícias e eventos (§13, §32)
    news: list[NewsItem] = field(default_factory=list)
    events: list[EconomicEvent] = field(default_factory=list)


@dataclass
class FactorScore:
    name: str
    score: float
    max_score: float
    rationale: str = ""
    available: bool = True

    @property
    def ratio(self) -> float:
        return self.score / self.max_score if self.max_score else 0.0

    @property
    def emoji(self) -> str:
        if not self.available:
            return "⚪"
        if self.ratio >= 0.3:
            return "🟢"
        if self.ratio <= -0.3:
            return "🔴"
        return "🟡"


@dataclass
class TechnicalReading:
    timeframe: str
    score: float             # -1..+1
    trend: str               # ALTA | BAIXA | LATERAL
    rsi: Optional[float] = None
    macd_hist: Optional[float] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    ema_alignment: Optional[float] = None  # -1..+1
    vwap_position: Optional[float] = None  # (close − vwap)/atr
    support: Optional[float] = None
    resistance: Optional[float] = None
    notes: list[str] = field(default_factory=list)


@dataclass
class PreMoveAnalysis:
    stage: Stage
    direction: Direction
    fundamental_score: float
    price_confirmation: float  # 0..1
    move_in_atr: float
    probability: float         # prob. de o movimento se materializar
    latent_pressure: Optional[str] = None  # "PRESSÃO COMPRADORA LATENTE" etc.
    notes: list[str] = field(default_factory=list)


@dataclass
class ReversalAnalysis:
    risk: float                # 0..100
    current_trend: Direction
    evidence: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    """Saída de um ciclo de análise (Diretriz §36)."""

    time: datetime
    price: float
    score: float
    factors: list[FactorScore]
    prob_up: float
    prob_down: float
    prob_flat: float
    confidence: float
    trend: Direction
    horizon: str
    premove: PreMoveAnalysis
    reversal: ReversalAnalysis
    systemic_risk: float
    sentiment_label: Sentiment
    dominant_pressure: str
    next_event: Optional[EconomicEvent]
    technical: list[TechnicalReading]
    conclusion: str
    confirmations: list[str]
    zone: dict[str, Optional[float]] = field(default_factory=dict)

    @property
    def direction(self) -> Direction:
        if self.prob_up > self.prob_down and self.prob_up >= self.prob_flat:
            return Direction.ALTA
        if self.prob_down > self.prob_up and self.prob_down >= self.prob_flat:
            return Direction.BAIXA
        return Direction.LATERAL

    def factor(self, name: str) -> Optional[FactorScore]:
        for f in self.factors:
            if f.name == name:
                return f
        return None


@dataclass
class Signal:
    """Sinal emitido para o Telegram (Diretriz §23–§26)."""

    type: SignalType
    direction: Direction
    assessment: Assessment
    reasons: list[str]
    trigger: str  # motivo anti-spam que liberou o envio (§37)
    text: str = ""


# ============================================================================
# TECHNICAL
# ============================================================================

"""Análise técnica multi-timeframe (Diretriz §17, §18).

Implementação em Python puro (sem dependências) dos indicadores exigidos:
EMA 9/21/50/200, RSI, MACD, ATR, ADX, Bollinger, VWAP e estrutura de mercado.
Nenhum indicador isolado decide: a nota do timeframe é a média ponderada de
várias evidências, e a nota global é a média ponderada dos timeframes.
"""





def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    if len(closes) < slow + signal:
        return [], [], []
    line = [f - s for f, s in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return trs


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = true_ranges(candles)
    a = sum(trs[1 : period + 1]) / period
    for tr in trs[period + 1 :]:
        a = (a * (period - 1) + tr) / period
    return a


def adx(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [], [], true_ranges(candles)[1:]
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    def wilder(xs: list[float]) -> list[float]:
        out = [sum(xs[:period])]
        for x in xs[period:]:
            out.append(out[-1] - out[-1] / period + x)
        return out

    tr_s, pdm_s, mdm_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dx: list[float] = []
    for t, p, m in zip(tr_s, pdm_s, mdm_s):
        if t == 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
    if len(dx) < period:
        return None
    a = sum(dx[:period]) / period
    for x in dx[period:]:
        a = (a * (period - 1) + x) / period
    return a


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> Optional[tuple[float, float, float]]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    sd = var ** 0.5
    return mid - mult * sd, mid, mid + mult * sd


def vwap(candles: Sequence[Candle]) -> Optional[float]:
    vol = sum(c.volume for c in candles)
    if vol <= 0:
        return None
    return sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles) / vol


def swing_levels(candles: Sequence[Candle], lookback: int = 20) -> tuple[Optional[float], Optional[float]]:
    """Suporte/resistência simples: mínima/máxima do lookback."""
    if not candles:
        return None, None
    win = candles[-lookback:]
    return min(c.low for c in win), max(c.high for c in win)


def analyze_timeframe(tf: str, candles: Sequence[Candle]) -> TechnicalReading:
    """Nota técnica -1..+1 de um timeframe combinando múltiplas evidências."""
    closes = [c.close for c in candles]
    reading = TechnicalReading(timeframe=tf, score=0.0, trend="LATERAL")
    if len(closes) < 30:
        reading.notes.append("dados insuficientes")
        return reading

    close = closes[-1]
    e9, e21, e50 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    _atr = atr(candles) or (max(closes) - min(closes)) / 10 or 1.0
    reading.atr = _atr

    evidences: list[tuple[float, float]] = []  # (valor -1..1, peso)

    # 1) Alinhamento de EMAs (estrutura de tendência)
    align = 0.0
    align += 1 if close > e9 else -1
    align += 1 if e9 > e21 else -1
    align += 1 if e21 > e50 else -1
    if e200 is not None:
        align += 1 if close > e200 else -1
        align /= 4
    else:
        align /= 3
    reading.ema_alignment = align
    evidences.append((align, 0.30))

    # 2) RSI (momentum; extremos penalizam a continuação)
    r = rsi(closes)
    reading.rsi = r
    if r is not None:
        rs = (r - 50) / 25  # 25→-1, 75→+1
        if r > 75:
            rs = 0.4  # sobrecomprado: continuação menos provável
            reading.notes.append("RSI sobrecomprado")
        elif r < 25:
            rs = -0.4
            reading.notes.append("RSI sobrevendido")
        evidences.append((_clip(rs), 0.15))

    # 3) MACD (momentum direcional + divergência simples)
    _, _, hist = macd(closes)
    if hist:
        reading.macd_hist = hist[-1]
        h_norm = _clip(hist[-1] / (_atr * 0.5))
        evidences.append((h_norm, 0.15))
        # perda de momentum: histograma encolhendo enquanto preço estende
        if len(hist) >= 4 and abs(hist[-1]) < abs(hist[-3]) and abs(hist[-1]) < abs(hist[-4]):
            reading.notes.append("momentum perdendo força (MACD)")

    # 4) ADX (força da tendência amplifica ou reduz a leitura direcional)
    a = adx(candles)
    reading.adx = a
    strength = 1.0
    if a is not None:
        strength = 0.6 + 0.4 * _clip((a - 15) / 25, 0.0, 1.0)
        if a < 18:
            reading.notes.append("mercado sem tendência (ADX baixo)")

    # 5) VWAP (posição relativa)
    v = vwap(candles[-60:])
    if v is not None and _atr:
        pos = (close - v) / _atr
        reading.vwap_position = pos
        evidences.append((_clip(pos / 2), 0.15))

    # 6) Bollinger (posição na banda)
    bb = bollinger(closes)
    if bb:
        lo, mid, hi = bb
        width = (hi - lo) or 1.0
        pos = (close - mid) / (width / 2)
        evidences.append((_clip(pos), 0.10))
        if close > hi:
            reading.notes.append("acima da banda superior")
        elif close < lo:
            reading.notes.append("abaixo da banda inferior")

    # 7) Estrutura: máximas/mínimas e rompimentos
    sup, res = swing_levels(candles[:-1])
    reading.support, reading.resistance = sup, res
    if sup is not None and res is not None:
        if close > res:
            evidences.append((0.8, 0.15))
            reading.notes.append("rompimento de resistência")
        elif close < sup:
            evidences.append((-0.8, 0.15))
            reading.notes.append("perda de suporte")
        else:
            rng = (res - sup) or 1.0
            evidences.append((_clip((close - sup) / rng * 2 - 1) * 0.5, 0.15))

    # 8) Falso rompimento: máxima acima da resistência mas fechamento abaixo
    last = candles[-1]
    if res is not None and last.high > res and last.close < res:
        evidences.append((-0.6, 0.10))
        reading.notes.append("falso rompimento (rejeição na resistência)")
    if sup is not None and last.low < sup and last.close > sup:
        evidences.append((0.6, 0.10))
        reading.notes.append("falso rompimento (absorção no suporte)")

    total_w = sum(w for _, w in evidences) or 1.0
    score = sum(v * w for v, w in evidences) / total_w * strength
    reading.score = _clip(score)
    reading.trend = "ALTA" if score > 0.2 else "BAIXA" if score < -0.2 else "LATERAL"
    return reading


def analyze_multi_timeframe(candles_by_tf: dict[str, Sequence[Candle]]) -> tuple[float, list[TechnicalReading]]:
    """Retorna (nota técnica global -1..+1, leituras por timeframe)."""
    readings: list[TechnicalReading] = []
    num, den = 0.0, 0.0
    for tf, w in TIMEFRAME_WEIGHTS.items():
        cs = candles_by_tf.get(tf)
        if not cs:
            continue
        r = analyze_timeframe(tf, cs)
        readings.append(r)
        if "dados insuficientes" in r.notes:
            continue
        num += r.score * w
        den += w
    return (num / den if den else 0.0), readings


def volume_profile_signals(candles: Sequence[Candle], lookback: int = 20) -> dict[str, float]:
    """Sinais de acumulação/distribuição por volume × preço (Diretriz §15, §16).

    Retorna dicionário com:
      volume_ratio   — volume recente / média anterior
      range_ratio    — amplitude recente / amplitude anterior (lateral = < 1)
      absorption     — +1 volume alto com preço lateral (acumulação/distribuição)
      divergence     — preço subindo com volume caindo (>0 = distribuição), inverso <0
    """
    if len(candles) < lookback * 2:
        return {}
    recent, prev = candles[-lookback:], candles[-2 * lookback : -lookback]
    v_recent = sum(c.volume for c in recent) / lookback
    v_prev = sum(c.volume for c in prev) / lookback or 1.0
    r_recent = max(c.high for c in recent) - min(c.low for c in recent)
    r_prev = (max(c.high for c in prev) - min(c.low for c in prev)) or 1.0
    volume_ratio = v_recent / v_prev
    range_ratio = r_recent / r_prev
    absorption = 1.0 if volume_ratio > 1.2 and range_ratio < 0.8 else 0.0
    price_move = recent[-1].close - recent[0].open
    divergence = 0.0
    if price_move > 0 and volume_ratio < 0.8:
        divergence = 1.0
    elif price_move < 0 and volume_ratio < 0.8:
        divergence = -1.0
    return {
        "volume_ratio": volume_ratio,
        "range_ratio": range_ratio,
        "absorption": absorption,
        "divergence": divergence,
    }


# ============================================================================
# FACTORS
# ============================================================================

"""Pontuação dos fatores do GOLD AI SCORE (Diretriz §5–§12, §19).

Cada função recebe o MarketSnapshot e devolve um FactorScore limitado a
[-peso, +peso]. Sinal positivo = favorável à ALTA do ouro.

Princípio (§31): correlação não é causalidade. As funções procuram
*variações* (o que está começando a mudar) e não apenas níveis, e usam
saturação suave para que nenhum fator isolado domine o score.
"""





def _sat(x: float, scale: float) -> float:
    """Saturação suave em -1..+1 (tanh). `scale` é o valor que dá ~0.76."""
    return math.tanh(x / scale) if scale else 0.0


def _factor(name: str, weight: float, ratio: float, rationale: str, available: bool = True) -> FactorScore:
    return FactorScore(name=name, score=round(_clip(ratio, -1, 1) * weight, 1), max_score=weight, rationale=rationale, available=available)


# --------------------------------------------------------------------------- Dólar §6
def score_dolar(s: MarketSnapshot, w: float) -> FactorScore:
    parts: list[float] = []
    notes: list[str] = []
    if s.dxy_change_pct is not None:
        # DXY -0.5% ≈ forte suporte ao ouro
        parts.append(-_sat(s.dxy_change_pct, 0.4))
        notes.append(f"DXY {s.dxy_change_pct:+.2f}%")
    if s.usdcnh_change_pct is not None:
        parts.append(-0.5 * _sat(s.usdcnh_change_pct, 0.4))
        notes.append(f"USD/CNH {s.usdcnh_change_pct:+.2f}%")
    if not parts:
        return _factor("dolar", w, 0.0, "sem dados", available=False)
    ratio = sum(parts) / len(parts)
    verdict = "enfraquecendo → suporte" if ratio > 0.15 else "fortalecendo → pressão" if ratio < -0.15 else "estável"
    return _factor("dolar", w, ratio, f"dólar {verdict} ({', '.join(notes)})")


# --------------------------------------------------------------------------- Juros reais §5
def score_juros_reais(s: MarketSnapshot, w: float) -> FactorScore:
    parts: list[tuple[float, float]] = []  # (valor, peso)
    notes: list[str] = []
    if s.real_yield_change_bp is not None:
        parts.append((-_sat(s.real_yield_change_bp, 6.0), 0.75))  # -6bp ≈ +0.76
        notes.append(f"juros reais 10Y {s.real_yield_change_bp:+.1f}bp")
    elif s.us10y_change_bp is not None:
        # fallback: nominal menos breakeven, se houver
        be = s.breakeven_10y_change_bp or 0.0
        parts.append((-_sat(s.us10y_change_bp - be, 6.0), 0.75))
        notes.append(f"Treasury 10Y {s.us10y_change_bp:+.1f}bp")
    if s.us10y_change_bp is not None and s.real_yield_change_bp is not None:
        parts.append((-_sat(s.us10y_change_bp, 8.0), 0.25))
        notes.append(f"10Y nominal {s.us10y_change_bp:+.1f}bp")
    if not parts:
        return _factor("juros_reais", w, 0.0, "sem dados", available=False)
    ratio = sum(v * wt for v, wt in parts) / sum(wt for _, wt in parts)
    verdict = "caindo → suporte" if ratio > 0.15 else "subindo → pressão" if ratio < -0.15 else "estáveis"
    return _factor("juros_reais", w, ratio, f"juros reais {verdict} ({', '.join(notes)})")


# --------------------------------------------------------------------------- Fed §3, §5
def score_fed(s: MarketSnapshot, w: float) -> FactorScore:
    parts: list[float] = []
    notes: list[str] = []
    if s.fed_cut_prob_change_pp is not None:
        parts.append(_sat(s.fed_cut_prob_change_pp, 10.0))  # +10pp ≈ +0.76
        notes.append(f"prob. corte {s.fed_cut_prob_change_pp:+.0f}pp")
    if s.fed_tone is not None:
        parts.append(_clip(s.fed_tone, -1, 1))
        notes.append("tom dovish" if s.fed_tone > 0.2 else "tom hawkish" if s.fed_tone < -0.2 else "tom neutro")
    if not parts:
        return _factor("fed", w, 0.0, "sem dados", available=False)
    ratio = sum(parts) / len(parts)
    return _factor("fed", w, ratio, f"Fed: {', '.join(notes)}")


# --------------------------------------------------------------------------- Inflação §4
def score_inflacao(s: MarketSnapshot, w: float) -> FactorScore:
    """Inflação abaixo do esperado → mais cortes → ouro sobe (via juros).
    Inflação acelerando de forma persistente é ambígua (hedge vs. Fed hawkish);
    o motor pondera a surpresa mais que a tendência."""
    parts: list[float] = []
    notes: list[str] = []
    if s.inflation_surprise_sigma is not None:
        parts.append(-_sat(s.inflation_surprise_sigma, 1.0))
        notes.append(f"surpresa {s.inflation_surprise_sigma:+.1f}σ")
    if s.inflation_trend is not None:
        parts.append(0.3 * _clip(s.inflation_trend, -1, 1))  # leve: ouro como hedge
        notes.append("inflação acelerando" if s.inflation_trend > 0 else "inflação desacelerando")
    if not parts:
        return _factor("inflacao", w, 0.0, "sem dados", available=False)
    ratio = sum(parts) / len(parts)
    return _factor("inflacao", w, ratio, f"inflação: {', '.join(notes)}")


# --------------------------------------------------------------------------- Geopolítica §10
def score_geopolitica(s: MarketSnapshot, w: float) -> FactorScore:
    """Evento → expectativa → reação → fluxo → ouro. Só a *variação* do risco
    pontua forte; nível alto e estável já está precificado (§35)."""
    if s.geopolitical_risk is None and s.geopolitical_risk_change is None:
        return _factor("geopolitica", w, 0.0, "sem dados", available=False)
    level = (s.geopolitical_risk or 0.0) / 100.0
    change = s.geopolitical_risk_change or 0.0
    ratio = 0.25 * level + 0.75 * _sat(change, 12.0)
    # confirmação pelo comportamento do mercado: VIX/petróleo subindo junto
    if change > 0 and s.vix_change_pct is not None and s.vix_change_pct > 5:
        ratio = min(1.0, ratio + 0.15)
    verdict = "tensão crescente" if change > 3 else "tensão diminuindo" if change < -3 else "estável"
    return _factor("geopolitica", w, ratio, f"geopolítica {verdict} (risco {s.geopolitical_risk or 0:.0f}/100, Δ{change:+.0f})")


# --------------------------------------------------------------------------- Fluxo §7
def score_fluxo(s: MarketSnapshot, w: float) -> FactorScore:
    parts: list[float] = []
    notes: list[str] = []
    if s.etf_flow_musd is not None:
        parts.append(_sat(s.etf_flow_musd, 300.0))
        notes.append(f"ETFs {s.etf_flow_musd:+.0f}M")
    if s.order_flow_imbalance is not None:
        parts.append(_clip(s.order_flow_imbalance, -1, 1))
        notes.append("fluxo comprador" if s.order_flow_imbalance > 0.1 else "fluxo vendedor" if s.order_flow_imbalance < -0.1 else "fluxo equilibrado")
    if s.central_bank_buying_tonnes is not None:
        parts.append(_sat(s.central_bank_buying_tonnes, 30.0))
        notes.append(f"BCs {s.central_bank_buying_tonnes:+.0f}t")
    if s.open_interest_change_pct is not None and s.price_change_pct is not None:
        # OI subindo com preço subindo = posições novas a favor; OI subindo com preço caindo = vendas novas
        sign = 1.0 if s.price_change_pct >= 0 else -1.0
        parts.append(0.5 * sign * _sat(s.open_interest_change_pct, 3.0))
        notes.append(f"OI {s.open_interest_change_pct:+.1f}%")
    if not parts:
        return _factor("fluxo", w, 0.0, "sem dados", available=False)
    ratio = sum(parts) / len(parts)
    return _factor("fluxo", w, ratio, f"fluxo: {', '.join(notes)}")


# --------------------------------------------------------------------------- COT §8
def score_cot(s: MarketSnapshot, w: float) -> FactorScore:
    if s.cot_managed_money_net_change is None and s.cot_managed_money_percentile is None:
        return _factor("cot", w, 0.0, "sem dados", available=False)
    ratio = 0.0
    notes: list[str] = []
    if s.cot_managed_money_net_change is not None:
        ratio += 0.6 * _sat(s.cot_managed_money_net_change, 15000.0)
        notes.append(f"managed money {s.cot_managed_money_net_change:+.0f} contratos/sem")
    if s.cot_managed_money_percentile is not None:
        p = s.cot_managed_money_percentile
        # excesso especulativo: percentil extremo penaliza a continuação (risco de reversão)
        if p >= 90:
            ratio -= 0.5
            notes.append("posicionamento comprado extremo (risco de reversão)")
        elif p <= 10:
            ratio += 0.5
            notes.append("posicionamento vendido extremo (potencial short squeeze)")
    if s.cot_commercial_net_change is not None:
        ratio += 0.2 * _sat(s.cot_commercial_net_change, 15000.0)
    return _factor("cot", w, ratio, f"COT: {', '.join(notes) or 'neutro'}")


# --------------------------------------------------------------------------- Opções §9
def score_opcoes(s: MarketSnapshot, w: float) -> FactorScore:
    if s.put_call_ratio is None and s.implied_vol_change_pct is None:
        return _factor("opcoes", w, 0.0, "sem dados", available=False)
    ratio = 0.0
    notes: list[str] = []
    if s.put_call_ratio is not None:
        # P/C alto = proteção pesada = contrarian levemente altista; P/C muito baixo = euforia
        ratio += 0.6 * _sat(s.put_call_ratio - 0.9, 0.4)
        notes.append(f"put/call {s.put_call_ratio:.2f}")
    if s.implied_vol_change_pct is not None and s.price_change_pct is not None:
        # IV subindo com preço subindo = demanda por calls (alta); IV subindo com preço caindo = medo
        sign = 1.0 if s.price_change_pct >= 0 else -1.0
        ratio += 0.4 * sign * _sat(s.implied_vol_change_pct, 8.0)
        notes.append(f"IV {s.implied_vol_change_pct:+.1f}%")
    if s.gamma_wall_above is not None and s.price and s.gamma_wall_above > s.price:
        dist = (s.gamma_wall_above - s.price) / (s.atr or 1.0)
        if dist < 1.0:
            ratio -= 0.2
            notes.append(f"gamma wall em {s.gamma_wall_above:.0f} (ímã/resistência)")
    if s.gamma_wall_below is not None and s.price and s.gamma_wall_below < s.price:
        dist = (s.price - s.gamma_wall_below) / (s.atr or 1.0)
        if dist < 1.0:
            ratio += 0.2
            notes.append(f"gamma wall em {s.gamma_wall_below:.0f} (suporte)")
    return _factor("opcoes", w, ratio, f"opções: {', '.join(notes) or 'neutro'}")


# --------------------------------------------------------------------------- Sentimento §12
def sentiment_label(value: Optional[float]) -> Sentiment:
    if value is None:
        return Sentiment.NEUTRO
    if value >= 0.6:
        return Sentiment.MUITO_OTIMISTA
    if value >= 0.2:
        return Sentiment.OTIMISTA
    if value <= -0.6:
        return Sentiment.MUITO_BAIXISTA
    if value <= -0.2:
        return Sentiment.BAIXISTA
    return Sentiment.NEUTRO


def score_sentimento(s: MarketSnapshot, w: float) -> FactorScore:
    if s.sentiment is None and not s.news:
        return _factor("sentimento", w, 0.0, "sem dados", available=False)
    parts: list[tuple[float, float]] = []  # (valor, peso)
    notes: list[str] = []
    if s.sentiment is not None:
        parts.append((_clip(s.sentiment, -1, 1), 0.5))
        notes.append(sentiment_label(s.sentiment).value.lower())
    if s.sentiment_change is not None:
        parts.append((_clip(s.sentiment_change, -1, 1), 0.3))
    if s.news:
        # notícias: impacto × (1 − já precificado). Expectativa × resultado × reação (§13).
        eff = [n.gold_impact * (1.0 - _clip(n.priced_in, 0, 1)) for n in s.news]
        news_ratio = _clip(sum(eff) / max(len(eff), 2), -1, 1)
        parts.append((news_ratio, 0.5))
        notes.append(f"{len(s.news)} notícia(s) (efeito líquido {news_ratio:+.2f})")
    ratio = sum(v * wt for v, wt in parts) / sum(wt for _, wt in parts)
    return _factor("sentimento", w, ratio, f"sentimento: {', '.join(notes)}")


# --------------------------------------------------------------------------- Técnico §17
def score_tecnico(s: MarketSnapshot, w: float) -> tuple[FactorScore, list]:
    if not s.candles:
        return _factor("tecnico", w, 0.0, "sem candles", available=False), []
    global_score, readings = analyze_multi_timeframe(s.candles)
    valid = [r for r in readings if "dados insuficientes" not in r.notes]
    if not valid:
        return _factor("tecnico", w, 0.0, "dados insuficientes", available=False), readings
    parts = [f"{r.timeframe}:{r.trend[0]}" for r in valid]
    return _factor("tecnico", w, global_score, f"técnico {'positivo' if global_score > 0.2 else 'negativo' if global_score < -0.2 else 'neutro'} ({' '.join(parts)})"), readings


SCORERS: dict[str, Callable[[MarketSnapshot, float], FactorScore]] = {
    "dolar": score_dolar,
    "juros_reais": score_juros_reais,
    "fed": score_fed,
    "inflacao": score_inflacao,
    "geopolitica": score_geopolitica,
    "fluxo": score_fluxo,
    "cot": score_cot,
    "opcoes": score_opcoes,
    "sentimento": score_sentimento,
}


def systemic_risk_index(s: MarketSnapshot) -> float:
    """ÍNDICE DE RISCO SISTÊMICO 0..100 (Diretriz §11)."""
    parts: list[float] = []
    if s.vix is not None:
        parts.append(_clip((s.vix - 12) / 28, 0, 1) * 100)  # 12→0, 40→100
    if s.vix_change_pct is not None:
        parts.append(_clip(s.vix_change_pct / 40, 0, 1) * 100)
    if s.credit_spread_bp is not None:
        parts.append(_clip((s.credit_spread_bp - 300) / 500, 0, 1) * 100)  # 300→0, 800→100
    if s.credit_spread_change_bp is not None:
        parts.append(_clip(s.credit_spread_change_bp / 50, 0, 1) * 100)
    if s.equity_change_pct is not None:
        parts.append(_clip(-s.equity_change_pct / 4, 0, 1) * 100)
    if s.bank_stress is not None:
        parts.append(_clip(s.bank_stress, 0, 100))
    if not parts:
        return 0.0
    # média com ênfase no pior componente (stress é assimétrico)
    return round(0.5 * sum(parts) / len(parts) + 0.5 * max(parts), 1)


def accumulation_distribution(s: MarketSnapshot) -> dict[str, float]:
    """Sinais de acumulação/distribuição do timeframe de referência (§15, §16)."""
    for tf in ("H1", "M30", "H4", "M15"):
        if tf in s.candles:
            return volume_profile_signals(s.candles[tf])
    return {}


# ============================================================================
# PREMOVE
# ============================================================================

"""Sistema de reação antecipada, estágios e reversão (Diretriz §14, §15, §16, §22, §26).

A pergunta central: "os fundamentos já mudaram e o preço ainda não?"
"""




def fundamental_score(factors: list[FactorScore]) -> float:
    """Score dos fatores NÃO técnicos reescalado para -100..+100."""
    fund = [f for f in factors if f.name != "tecnico" and f.available]
    if not fund:
        return 0.0
    total, max_total = sum(f.score for f in fund), sum(f.max_score for f in fund)
    return 100.0 * total / max_total if max_total else 0.0


def price_confirmation(readings: list[TechnicalReading], direction: Direction) -> float:
    """0..1 — quanto o técnico já confirma a direção dos fundamentos.
    Usa a microestrutura/intraday (M5–H1), que reage primeiro."""
    weights = {"M5": 0.15, "M15": 0.25, "M30": 0.3, "H1": 0.3}
    num, den = 0.0, 0.0
    sign = 1.0 if direction == Direction.ALTA else -1.0
    for r in readings:
        w = weights.get(r.timeframe)
        if w is None or "dados insuficientes" in r.notes:
            continue
        num += max(0.0, sign * r.score) * w
        den += w
    return num / den if den else 0.0


def analyze_premove(
    s: MarketSnapshot,
    factors: list[FactorScore],
    readings: list[TechnicalReading],
    accum: dict[str, float],
    cfg: EngineConfig,
) -> PreMoveAnalysis:
    fund = fundamental_score(factors)
    direction = Direction.ALTA if fund > 0 else Direction.BAIXA if fund < 0 else Direction.LATERAL
    move_in_atr = 0.0
    if s.atr and s.price:
        move_in_atr = (s.price_change_pct / 100.0 * s.price) / s.atr
    confirm = price_confirmation(readings, direction) if direction != Direction.LATERAL else 0.0
    notes: list[str] = []

    if abs(fund) < cfg.premove_fundamental_threshold:
        return PreMoveAnalysis(Stage.NEUTRO, Direction.LATERAL, fund, confirm, move_in_atr, 0.0, None, ["fundamentos sem viés claro"])

    same_sign_move = (move_in_atr > 0) == (direction == Direction.ALTA)
    strong_move = abs(move_in_atr) >= cfg.chase_atr_multiple and same_sign_move

    # Probabilidade de o movimento se materializar: cresce com a força dos fundamentos e
    # com sinais de acumulação/absorção; cai se o preço já andou muito.
    prob = 0.5 + 0.35 * min(abs(fund), 100) / 100
    if accum.get("absorption"):
        prob += 0.06
        notes.append("absorção: volume alto com preço lateral")
    if accum.get("volume_ratio", 1.0) > 1.3:
        prob += 0.04
        notes.append("volume acima da média")
    if s.open_interest_change_pct and s.open_interest_change_pct > 1.5:
        prob += 0.04
        notes.append("open interest crescendo")

    if strong_move:
        stage = Stage.MOVIMENTO
        prob -= 0.20
        notes.append(f"preço já se moveu {move_in_atr:+.1f} ATR — evitar perseguir")
        pressure = None
    elif confirm < cfg.premove_price_confirm_ratio:
        stage = Stage.PRE_MOVIMENTO
        pressure = "PRESSÃO COMPRADORA LATENTE" if direction == Direction.ALTA else "PRESSÃO VENDEDORA LATENTE"
        notes.append("fundamentos mudaram; preço ainda não confirmou")
    else:
        stage = Stage.CONFIRMACAO
        prob += 0.05
        pressure = "PRESSÃO COMPRADORA EM CONFIRMAÇÃO" if direction == Direction.ALTA else "PRESSÃO VENDEDORA EM CONFIRMAÇÃO"
        notes.append("preço começa a acompanhar os fundamentos")

    return PreMoveAnalysis(stage, direction, round(fund, 1), round(confirm, 2), round(move_in_atr, 2), round(max(0.0, min(0.95, prob)), 2), pressure, notes)


def analyze_reversal(
    s: MarketSnapshot,
    factors: list[FactorScore],
    readings: list[TechnicalReading],
    accum: dict[str, float],
) -> ReversalAnalysis:
    """Risco de reversão 0..100: tendência vigente × sinais de distribuição/acumulação contrários."""
    swing = [r for r in readings if r.timeframe in ("H1", "H4", "D1") and "dados insuficientes" not in r.notes]
    if not swing:
        return ReversalAnalysis(0.0, Direction.LATERAL, ["sem leitura de tendência"])
    trend_score = sum(r.score for r in swing) / len(swing)
    trend = Direction.ALTA if trend_score > 0.2 else Direction.BAIXA if trend_score < -0.2 else Direction.LATERAL
    if trend == Direction.LATERAL:
        return ReversalAnalysis(0.0, trend, ["sem tendência definida"])

    sign = 1.0 if trend == Direction.ALTA else -1.0
    risk = 0.0
    evidence: list[str] = []

    fund = fundamental_score(factors)
    if sign * fund < -15:
        risk += min(35.0, abs(fund) * 0.5)
        evidence.append(f"fundamentos contrários à tendência ({fund:+.0f})")

    div = accum.get("divergence", 0.0)
    if sign * div > 0:
        risk += 15
        evidence.append("divergência preço × volume (distribuição)" if sign > 0 else "queda com volume minguando")

    for r in swing:
        if any("momentum perdendo força" in n for n in r.notes):
            risk += 8
            evidence.append(f"perda de momentum em {r.timeframe}")
        if any("falso rompimento" in n for n in r.notes):
            risk += 10
            evidence.append(f"rejeição/falso rompimento em {r.timeframe}")
        if r.rsi is not None and ((sign > 0 and r.rsi > 75) or (sign < 0 and r.rsi < 25)):
            risk += 6
            evidence.append(f"RSI extremo em {r.timeframe}")

    if s.order_flow_imbalance is not None and sign * s.order_flow_imbalance < -0.3:
        risk += 12
        evidence.append("fluxo agressor contrário à tendência")
    if s.cot_managed_money_percentile is not None:
        p = s.cot_managed_money_percentile
        if (sign > 0 and p >= 90) or (sign < 0 and p <= 10):
            risk += 12
            evidence.append("posicionamento especulativo extremo (COT)")
    if s.etf_flow_musd is not None and sign * s.etf_flow_musd < -150:
        risk += 8
        evidence.append("fluxo de ETFs contrário")

    # tendência fraca (ruído) não pode gerar alerta de reversão forte
    strength = min(1.0, abs(trend_score) / 0.45)
    return ReversalAnalysis(round(min(100.0, risk * strength), 1), trend, evidence)


# ============================================================================
# EVENTS
# ============================================================================

"""Calendário de risco, análise pré-evento (árvore de reação) e pós-evento
(Diretriz §32, §33, §34)."""




IMPACT_RANK = {"BAIXO": 1, "MÉDIO": 2, "ALTO": 3, "MUITO ALTO": 4}

# Sensibilidade do ouro ao tipo de evento: +1 = resultado ACIMA do consenso é
# positivo para o ouro; -1 = resultado acima é negativo (via juros/dólar).
EVENT_GOLD_SENSITIVITY: dict[str, float] = {
    "cpi": -1.0,
    "core_cpi": -1.0,
    "pce": -1.0,
    "core_pce": -1.0,
    "nfp": -0.9,
    "unemployment": +0.8,     # desemprego acima → economia fraca → cortes → ouro sobe
    "earnings": -0.6,         # salários acima → inflação → hawkish
    "gdp": -0.5,
    "ism": -0.5,
    "pmi": -0.5,
    "retail_sales": -0.5,
    "jobless_claims": +0.6,   # pedidos acima → mercado de trabalho fraco
    "jolts": -0.5,
    "consumer_confidence": -0.3,
    "michigan": -0.3,
    "housing": -0.3,
    "fomc": 0.0,              # decisão: interpretar pelo tom (fed_tone)
    "speech": 0.0,
    "geopolitical": 0.0,
    "generic": 0.0,
}


@dataclass
class ScenarioTree:
    """ÁRVORE DE REAÇÃO DO OURO para um evento (Diretriz §33)."""

    event: EconomicEvent
    expectation: str
    positioning: str
    scenarios: dict[str, str] = field(default_factory=dict)  # "acima" | "em linha" | "abaixo" → texto

    def render(self) -> str:
        lines = [f"⚠️ EVENTO DE ALTO IMPACTO — {self.event.name} — {self.event.time:%d/%m %H:%M} UTC",
                 f"Impacto possível: {self.event.impact}",
                 f"Expectativa: {self.expectation}",
                 f"Posicionamento: {self.positioning}",
                 "Cenários:"]
        for k, v in self.scenarios.items():
            lines.append(f"  • {k}: {v}")
        return "\n".join(lines)


def next_high_impact_event(events: list[EconomicEvent], now: datetime, window_minutes: int) -> Optional[EconomicEvent]:
    """Próximo evento ALTO/MUITO ALTO dentro da janela (ou None)."""
    horizon = now + timedelta(minutes=window_minutes)
    upcoming = [e for e in events if now <= e.time <= horizon and IMPACT_RANK.get(e.impact, 0) >= 3]
    return min(upcoming, key=lambda e: e.time) if upcoming else None


def upcoming_events(events: list[EconomicEvent], now: datetime, hours: float = 48.0) -> list[EconomicEvent]:
    horizon = now + timedelta(hours=hours)
    return sorted((e for e in events if now <= e.time <= horizon), key=lambda e: e.time)


def build_scenario_tree(event: EconomicEvent, s: MarketSnapshot) -> ScenarioTree:
    sens = EVENT_GOLD_SENSITIVITY.get(event.kind, 0.0)
    cons = f"consenso {event.consensus}{event.unit}" if event.consensus is not None else "sem consenso disponível"
    prev = f", anterior {event.previous}{event.unit}" if event.previous is not None else ""
    expectation = f"{cons}{prev}"

    pos_parts: list[str] = []
    if s.cot_managed_money_percentile is not None:
        p = s.cot_managed_money_percentile
        pos_parts.append("especuladores muito comprados (assimetria de baixa)" if p >= 80 else "especuladores muito vendidos (assimetria de alta)" if p <= 20 else "posicionamento especulativo moderado")
    if s.put_call_ratio is not None:
        pos_parts.append(f"put/call {s.put_call_ratio:.2f}")
    if s.price_change_pct:
        pos_parts.append(f"ouro já {s.price_change_pct:+.2f}% na janela pré-evento")
    positioning = "; ".join(pos_parts) or "sem dados de posicionamento"

    if sens == 0.0:
        scenarios = {
            "tom dovish / risco maior": "Ouro → provável ALTA (juros ↓, dólar ↓, busca por proteção)",
            "tom neutro / em linha": "Ouro → provável LATERAL, reação guiada pelo posicionamento",
            "tom hawkish / risco menor": "Ouro → provável BAIXA (juros ↑, dólar ↑)",
        }
    else:
        up = "ALTA" if sens > 0 else "BAIXA"
        down = "BAIXA" if sens > 0 else "ALTA"
        scenarios = {
            "acima do consenso": f"Ouro → provável {up} (canal: expectativa de juros → Treasury → dólar → ouro)",
            "em linha": "Ouro → provável LATERAL/continuação; observar reação do dólar e dos yields para confirmar",
            "abaixo do consenso": f"Ouro → provável {down}",
        }
    return ScenarioTree(event, expectation, positioning, scenarios)


@dataclass
class PostEventChain:
    """RESULTADO → DÓLAR → TREASURY → JUROS REAIS → OURO → FLUXO → CONFIRMAÇÃO OU REVERSÃO (§34)."""

    event: EconomicEvent
    surprise: Optional[float]
    expected_gold_direction: str     # ALTA | BAIXA | NEUTRO
    dollar_reaction: str
    yields_reaction: str
    real_yields_reaction: str
    gold_reaction: str
    flow_reaction: str
    verdict: str                     # CONFIRMAÇÃO | REVERSÃO | INDEFINIDO
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join([
            f"📋 PÓS-EVENTO — {self.event.name}",
            f"Surpresa: {self.surprise:+.2f}" if self.surprise is not None else "Surpresa: n/d",
            f"Direção esperada para o ouro: {self.expected_gold_direction}",
            f"Dólar: {self.dollar_reaction} → Treasury: {self.yields_reaction} → Juros reais: {self.real_yields_reaction}",
            f"Ouro: {self.gold_reaction} → Fluxo: {self.flow_reaction}",
            f"Veredito: {self.verdict}",
            *[f"  • {n}" for n in self.notes],
        ])


def analyze_post_event(event: EconomicEvent, s: MarketSnapshot) -> PostEventChain:
    sens = EVENT_GOLD_SENSITIVITY.get(event.kind, 0.0)
    surprise = event.surprise()
    if surprise is None or sens == 0.0:
        expected = "NEUTRO" if s.fed_tone is None else ("ALTA" if s.fed_tone > 0.2 else "BAIXA" if s.fed_tone < -0.2 else "NEUTRO")
    else:
        signed = surprise * sens
        expected = "ALTA" if signed > 0 else "BAIXA" if signed < 0 else "NEUTRO"

    def react(x: Optional[float], up: str, down: str, thr: float) -> str:
        if x is None:
            return "n/d"
        return up if x > thr else down if x < -thr else "estável"

    dollar = react(s.dxy_change_pct, "subindo", "caindo", 0.1)
    yields = react(s.us10y_change_bp, "subindo", "caindo", 2.0)
    real = react(s.real_yield_change_bp, "subindo", "caindo", 2.0)
    gold = react(s.price_change_pct, "subindo", "caindo", 0.15)
    flow = react(s.order_flow_imbalance, "comprador", "vendedor", 0.15)

    notes: list[str] = []
    verdict = "INDEFINIDO"
    if expected != "NEUTRO" and gold != "n/d":
        aligned = (expected == "ALTA" and gold == "subindo") or (expected == "BAIXA" and gold == "caindo")
        opposite = (expected == "ALTA" and gold == "caindo") or (expected == "BAIXA" and gold == "subindo")
        if aligned:
            verdict = "CONFIRMAÇÃO"
        elif opposite:
            verdict = "REVERSÃO"
            notes.append("reação contrária ao esperado: provável evento já precificado ou posicionamento saturado (§13, §35)")
        else:
            notes.append("ouro ainda sem reação — observar dólar/yields como precursores")
        chain_ok = (expected == "ALTA" and dollar in ("caindo", "estável") and real in ("caindo", "estável", "n/d")) or \
                   (expected == "BAIXA" and dollar in ("subindo", "estável") and real in ("subindo", "estável", "n/d"))
        if not chain_ok:
            notes.append("cadeia dólar/juros não confirma a direção esperada — reduzir confiança")
    return PostEventChain(event, surprise, expected, dollar, yields, real, gold, flow, verdict, notes)


# ============================================================================
# SIGNALS
# ============================================================================

"""Classificação de sinais, filtro contra falsos sinais e regra anti-spam
(Diretriz §23–§28, §37)."""





def classify(score: float, cfg: EngineConfig) -> SignalType:
    if score >= cfg.strong_buy:
        return SignalType.STRONG_BUY
    if score >= cfg.buy:
        return SignalType.BUY
    if score <= cfg.strong_sell:
        return SignalType.STRONG_SELL
    if score <= cfg.sell:
        return SignalType.SELL
    return SignalType.NEUTRAL


def confirmations(a: Assessment, direction: Direction, cfg: EngineConfig) -> list[str]:
    """Famílias independentes de fatores que confirmam a direção (§27)."""
    sign = 1.0 if direction == Direction.ALTA else -1.0 if direction == Direction.BAIXA else 0.0
    if sign == 0.0:
        return []
    out: list[str] = []
    for family, names in FACTOR_FAMILIES.items():
        fs = [f for f in a.factors if f.name in names and f.available]
        if not fs:
            continue
        total, max_total = sum(f.score for f in fs), sum(f.max_score for f in fs)
        if max_total and sign * total / max_total >= cfg.family_confirmation_ratio:
            out.append(family)
    return out


def reasons_for(a: Assessment, direction: Direction) -> list[str]:
    sign = 1.0 if direction == Direction.ALTA else -1.0
    ranked = sorted((f for f in a.factors if f.available and sign * f.score > 0), key=lambda f: -abs(f.score))
    return [f.rationale for f in ranked[:6]]


@dataclass
class SignalGate:
    """Estado do anti-spam (§37). Só libera envio quando há mudança relevante."""

    cfg: EngineConfig
    last_sent_at: Optional[datetime] = None
    last_score: Optional[float] = None
    last_direction: Optional[Direction] = None
    last_stage: Optional[Stage] = None
    last_type: Optional[SignalType] = None
    last_reversal_alert: bool = False
    last_risk_alert: bool = False
    seen_events: set[str] = field(default_factory=set)

    def evaluate(self, a: Assessment, new_event_key: Optional[str] = None) -> Optional[Signal]:
        base_type = classify(a.score, self.cfg)
        direction = a.direction
        stage = a.premove.stage
        confs = a.confirmations
        trigger: Optional[str] = None
        sig_type: Optional[SignalType] = None

        # 7. risco excepcional — tem prioridade e ignora intervalo mínimo
        if a.systemic_risk >= self.cfg.exceptional_systemic_risk and not self.last_risk_alert:
            self.last_risk_alert = True
            return self._emit(SignalType.RISK, direction, a, "risco sistêmico excepcional")
        if a.systemic_risk < self.cfg.exceptional_systemic_risk - 10:
            self.last_risk_alert = False

        # 6. reversão (o risco já é escalado pela força da tendência, logo um valor
        #    acima do limiar implica tendência definida + evidência de distribuição/acumulação)
        rev_now = a.reversal.risk >= self.cfg.reversal_risk_threshold
        if rev_now and not self.last_reversal_alert:
            self.last_reversal_alert = True
            self.last_stage, self.last_direction, self.last_score = stage, direction, a.score
            return self._emit(SignalType.REVERSAL, a.reversal.current_trend, a, "risco de reversão detectado")
        if not rev_now:
            self.last_reversal_alert = False

        # 4. surgimento de pré-movimento (fundamentos antecipam o preço)
        if stage == Stage.PRE_MOVIMENTO and self.last_stage != Stage.PRE_MOVIMENTO and len(confs) >= self.cfg.min_confirmations:
            sig_type, trigger = SignalType.PRE_MOVE, "surgimento de pré-movimento"
            direction = a.premove.direction
        # 5. confirmação de movimento
        elif stage == Stage.CONFIRMACAO and self.last_stage == Stage.PRE_MOVIMENTO and base_type != SignalType.NEUTRAL:
            sig_type, trigger = base_type, "confirmação de movimento"
        # 1. novo evento relevante
        elif new_event_key and new_event_key not in self.seen_events and base_type != SignalType.NEUTRAL:
            self.seen_events.add(new_event_key)
            sig_type, trigger = base_type, f"novo evento: {new_event_key}"
        # 3. mudança de direção
        elif self.last_direction is not None and direction != self.last_direction and base_type != SignalType.NEUTRAL:
            sig_type, trigger = base_type, "mudança de direção"
        # 2. mudança significativa no score
        elif base_type != SignalType.NEUTRAL and (self.last_score is None or abs(a.score - self.last_score) >= self.cfg.min_score_change_to_alert):
            sig_type, trigger = base_type, "mudança significativa no score"
        # primeiro sinal não-neutro
        elif base_type != SignalType.NEUTRAL and self.last_type in (None, SignalType.NEUTRAL, SignalType.RISK, SignalType.REVERSAL):
            sig_type, trigger = base_type, "primeiro sinal do regime"

        self.last_stage, self.last_direction, self.last_score = stage, direction, a.score
        if sig_type is None:
            self.last_type = base_type
            return None

        # filtro §27: sinal direcional exige >= 3 confirmações independentes
        if sig_type in (SignalType.STRONG_BUY, SignalType.BUY, SignalType.SELL, SignalType.STRONG_SELL, SignalType.PRE_MOVE):
            if len(confs) < self.cfg.min_confirmations:
                self.last_type = SignalType.NEUTRAL
                return None
            if stage == Stage.MOVIMENTO and sig_type != SignalType.PRE_MOVE:
                # §22 estágio 3: não perseguir preço — rebaixa para neutro
                self.last_type = SignalType.NEUTRAL
                return None

        # intervalo mínimo entre alertas do mesmo tipo/direção
        if self.last_sent_at and (a.time - self.last_sent_at).total_seconds() < self.cfg.min_seconds_between_alerts \
                and sig_type == self.last_type and trigger not in ("mudança de direção", "surgimento de pré-movimento"):
            return None
        return self._emit(sig_type, direction, a, trigger or "")

    def _emit(self, sig_type: SignalType, direction: Direction, a: Assessment, trigger: str) -> Signal:
        self.last_sent_at = a.time
        self.last_type = sig_type
        rs = reasons_for(a, direction) if direction != Direction.LATERAL else []
        return Signal(type=sig_type, direction=direction, assessment=a, reasons=rs, trigger=trigger)


# ============================================================================
# MEMORY
# ============================================================================

"""Banco de previsões, feedback e aprendizado (Diretriz §29, §30).

PREVISÃO → MOVIMENTO REAL → COMPARAÇÃO → ERRO → APRENDIZADO.
Implementado em SQLite (stdlib) para funcionar em qualquer ambiente.
"""




SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    hora TEXT NOT NULL,
    sessao TEXT NOT NULL,
    preco REAL NOT NULL,
    previsao TEXT NOT NULL,
    probabilidade REAL NOT NULL,
    confianca REAL NOT NULL,
    score REAL NOT NULL,
    horizonte TEXT NOT NULL,
    estagio TEXT NOT NULL,
    fundamentos TEXT NOT NULL,
    noticias TEXT NOT NULL,
    dolar REAL, juros REAL, fluxo REAL, tecnico REAL,
    evento TEXT,
    sinal_tipo TEXT,
    resultado TEXT,
    tempo_ate_reacao_min REAL,
    maxima_favoravel REAL,
    maxima_adversa REAL,
    preco_final REAL,
    resolvido_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_resultado ON predictions(resultado);
"""


def session_label(t: datetime) -> str:
    """Sessão de mercado pela hora UTC."""
    h = t.astimezone(timezone.utc).hour
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 13:
        return "LONDRES"
    if 13 <= h < 17:
        return "LONDRES/NY"
    if 17 <= h < 22:
        return "NY"
    return "PÓS-NY"


@dataclass
class Outcome:
    result: str                   # ACERTO | ERRO | LATERAL
    time_to_reaction_min: Optional[float]
    mfe: float                    # máxima excursão favorável (USD)
    mae: float                    # máxima excursão adversa (USD)
    final_price: float


class PredictionMemory:
    def __init__(self, path: str = "gold_ai.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ registro
    def record(self, a: Assessment, signal_type: Optional[str] = None) -> int:
        t = a.time.astimezone(timezone.utc)
        direction = a.direction.value
        prob = {"ALTA": a.prob_up, "BAIXA": a.prob_down, "LATERAL": a.prob_flat}[direction]
        fund = {f.name: f.score for f in a.factors}
        cur = self.conn.execute(
            """INSERT INTO predictions (data, hora, sessao, preco, previsao, probabilidade, confianca, score,
               horizonte, estagio, fundamentos, noticias, dolar, juros, fluxo, tecnico, evento, sinal_tipo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), session_label(t), a.price, direction, prob,
                a.confidence, a.score, a.horizon, a.premove.stage.value, json.dumps(fund, ensure_ascii=False),
                json.dumps([], ensure_ascii=False), fund.get("dolar"), fund.get("juros_reais"), fund.get("fluxo"),
                fund.get("tecnico"), a.next_event.name if a.next_event else None, signal_type,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ feedback
    @staticmethod
    def evaluate_path(direction: str, entry: float, path: Iterable[tuple[datetime, float]], start: datetime, threshold: float) -> Outcome:
        """Compara a previsão com o movimento real.

        `threshold` (USD) define o que conta como movimento (ex.: 1 ATR). ACERTO se
        o preço tocar entry ± threshold primeiro na direção prevista; ERRO se tocar
        primeiro na direção contrária; LATERAL se não tocar nenhum.
        """
        sign = 1.0 if direction == "ALTA" else -1.0 if direction == "BAIXA" else 0.0
        mfe, mae, final = 0.0, 0.0, entry
        result, ttr = "LATERAL", None
        for t, p in path:
            final = p
            excursion = (p - entry) * sign if sign else abs(p - entry)
            mfe = max(mfe, excursion)
            mae = max(mae, -excursion if sign else 0.0)
            if sign and result == "LATERAL":
                if excursion >= threshold:
                    result, ttr = "ACERTO", (t - start).total_seconds() / 60
                elif excursion <= -threshold:
                    result, ttr = "ERRO", (t - start).total_seconds() / 60
        if not sign:
            result = "ACERTO" if mfe < threshold else "ERRO"
        return Outcome(result, ttr, round(mfe, 2), round(mae, 2), final)

    def resolve(self, prediction_id: int, path: Iterable[tuple[datetime, float]], threshold: float) -> Outcome:
        row = self.conn.execute("SELECT * FROM predictions WHERE id=?", (prediction_id,)).fetchone()
        if row is None:
            raise KeyError(prediction_id)
        start = datetime.fromisoformat(f"{row['data']}T{row['hora']}").replace(tzinfo=timezone.utc)
        out = self.evaluate_path(row["previsao"], row["preco"], path, start, threshold)
        self.conn.execute(
            """UPDATE predictions SET resultado=?, tempo_ate_reacao_min=?, maxima_favoravel=?, maxima_adversa=?,
               preco_final=?, resolvido_em=? WHERE id=?""",
            (out.result, out.time_to_reaction_min, out.mfe, out.mae, out.final_price,
             datetime.now(timezone.utc).isoformat(), prediction_id),
        )
        self.conn.commit()
        return out

    # ------------------------------------------------------------------ aprendizado
    def accuracy(self, by: str = "sessao") -> list[dict]:
        """TAXA DE ACERTO por: sessao | hora | previsao | horizonte | estagio | evento | score_bucket | sinal_tipo."""
        if by == "score_bucket":
            key = "CASE WHEN score>=70 THEN '>=70' WHEN score>=50 THEN '50-69' WHEN score>-50 THEN '-49..49' WHEN score>-70 THEN '-69..-50' ELSE '<=-70' END"
        elif by == "hora":
            key = "substr(hora,1,2)"
        elif by in ("sessao", "previsao", "horizonte", "estagio", "evento", "sinal_tipo"):
            key = by
        else:
            raise ValueError(by)
        rows = self.conn.execute(
            f"""SELECT {key} AS chave, COUNT(*) AS n,
                       SUM(CASE WHEN resultado='ACERTO' THEN 1 ELSE 0 END) AS acertos,
                       AVG(tempo_ate_reacao_min) AS tempo_medio,
                       AVG(maxima_favoravel) AS mfe_medio, AVG(maxima_adversa) AS mae_medio
                FROM predictions WHERE resultado IS NOT NULL GROUP BY chave ORDER BY n DESC"""
        ).fetchall()
        return [
            {"chave": r["chave"], "n": r["n"], "acertos": r["acertos"], "taxa": round(r["acertos"] / r["n"], 3) if r["n"] else 0.0,
             "tempo_medio_min": r["tempo_medio"], "mfe_medio": r["mfe_medio"], "mae_medio": r["mae_medio"]}
            for r in rows
        ]

    def factor_power(self) -> list[dict]:
        """Quais fatores mais discriminam ACERTO vs ERRO (aprendizado §30).
        Mede a média do score do fator, alinhado à direção prevista, nos acertos e nos erros."""
        rows = self.conn.execute("SELECT previsao, fundamentos, resultado FROM predictions WHERE resultado IN ('ACERTO','ERRO')").fetchall()
        acc: dict[str, dict[str, list[float]]] = {}
        for r in rows:
            sign = 1.0 if r["previsao"] == "ALTA" else -1.0 if r["previsao"] == "BAIXA" else 0.0
            for name, val in json.loads(r["fundamentos"]).items():
                acc.setdefault(name, {"ACERTO": [], "ERRO": []})[r["resultado"]].append(sign * val)
        out = []
        for name, d in acc.items():
            ma = sum(d["ACERTO"]) / len(d["ACERTO"]) if d["ACERTO"] else 0.0
            me = sum(d["ERRO"]) / len(d["ERRO"]) if d["ERRO"] else 0.0
            out.append({"fator": name, "media_acertos": round(ma, 2), "media_erros": round(me, 2), "poder": round(ma - me, 2), "n": len(d["ACERTO"]) + len(d["ERRO"])})
        return sorted(out, key=lambda x: -x["poder"])

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM predictions WHERE resultado IS NULL ORDER BY id").fetchall()

    def close(self) -> None:
        self.conn.close()


# ============================================================================
# TELEGRAM
# ============================================================================

"""Formatação e envio de alertas para o Telegram (Diretriz §23–§26)."""





def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _layer_line(a: Assessment, direction: Direction) -> list[str]:
    sign = 1.0 if direction == Direction.ALTA else -1.0

    def flag(names: tuple[str, ...]) -> str:
        fs = [f for f in a.factors if f.name in names and f.available]
        if not fs:
            return "⚪"
        r = sum(f.score for f in fs) / sum(f.max_score for f in fs)
        return "🟢" if sign * r >= 0.3 else "🔴" if sign * r <= -0.3 else "🟡"

    return [
        f"FUNDAMENTOS → {flag(('dolar', 'juros_reais', 'fed', 'inflacao', 'geopolitica'))}",
        f"FLUXO → {flag(('fluxo', 'cot', 'opcoes'))}",
        f"TÉCNICO → {flag(('tecnico',))}",
        f"SENTIMENTO → {flag(('sentimento',))}",
    ]


def _zone(a: Assessment) -> list[str]:
    z = a.zone
    fmt = lambda v: f"{v:.2f}" if v is not None else "n/d"  # noqa: E731
    lines = []
    if z.get("entry_low") is not None:
        lines.append(f"Entrada: {fmt(z.get('entry_low'))}–{fmt(z.get('entry_high'))}")
    lines.append(f"Suporte: {fmt(z.get('support'))}")
    lines.append(f"Resistência: {fmt(z.get('resistance'))}")
    lines.append(f"Invalidação: {fmt(z.get('invalidation'))}")
    return lines


def format_signal(sig: Signal) -> str:
    a = sig.assessment
    d = sig.direction
    prob = a.prob_up if d == Direction.ALTA else a.prob_down if d == Direction.BAIXA else a.prob_flat
    price = f"Preço: {a.price:.2f}"

    if sig.type == SignalType.PRE_MOVE:
        emoji = "🟢 ALTA" if d == Direction.ALTA else "🔴 BAIXA"
        lines = [f"⚠️ GOLD PRE-MOVE — POSSÍVEL {'ALTA' if d == Direction.ALTA else 'BAIXA'}", "XAU/USD", price, "",
                 "A IA detectou mudança em:", *[f"• {r}" for r in sig.reasons], "",
                 "mas o preço ainda não confirmou.", "",
                 f"Probabilidade de movimento: {_pct(a.premove.probability)}", f"Direção: {emoji}",
                 f"Status: {a.premove.stage.value}", f"Confiança: {a.confidence:.0f}/100", f"Horizonte: {a.horizon}", "",
                 "Antecipação", *_layer_line(a, d), "", "Situação", f"{a.premove.latent_pressure or a.premove.stage.value}", "",
                 "Zona de atenção", *_zone(a)]
        return "\n".join(lines)

    if sig.type == SignalType.REVERSAL:
        trend = a.reversal.current_trend.value
        lines = ["🔄 GOLD REVERSAL ALERT", "XAU/USD", price, "",
                 f"Ouro está em tendência de {trend}, porém foram detectados sinais de {'distribuição' if trend == 'ALTA' else 'acumulação'}.", "",
                 "Indicadores:", *[f"• {e}" for e in a.reversal.evidence], "",
                 f"Resultado: 🔴 RISCO DE REVERSÃO ({a.reversal.risk:.0f}/100)", f"Score atual: {a.score:+.0f}", f"Horizonte: {a.horizon}"]
        if a.premove.stage.value == "PRÉ-MOVIMENTO" and a.premove.direction != a.reversal.current_trend:
            lines += ["", f"⚠️ {a.premove.latent_pressure}: fundamentos já apontam {a.premove.direction.value.lower()} (prob. {a.premove.probability:.0%}); preço ainda não confirmou."]
        lines += ["", "Zona de atenção", *_zone(a)]
        return "\n".join(lines)

    if sig.type == SignalType.RISK:
        lines = ["🚨 GOLD SYSTEMIC RISK", "XAU/USD", price, "",
                 f"RISCO SISTÊMICO: {a.systemic_risk:.0f}/100", "",
                 "Sinais de stress detectados (VIX, spreads, bolsas, bancos).",
                 "Reação do ouro pode ser não-linear: liquidação inicial (venda forçada) seguida de fluxo de proteção.", "",
                 f"Score atual: {a.score:+.0f} | Prob. alta {_pct(a.prob_up)} | Prob. baixa {_pct(a.prob_down)}"]
        return "\n".join(lines)

    buy = sig.type in (SignalType.BUY, SignalType.STRONG_BUY)
    head = "🚨 GOLD AI ALERT" if buy else "🔴 GOLD AI ALERT"
    bias = "🟢 VIÉS: COMPRA" if buy else "🔴 VIÉS: VENDA"
    if sig.type in (SignalType.STRONG_BUY, SignalType.STRONG_SELL):
        bias += " (FORTE)"
    lines = [head, "XAU/USD", price, "", bias, f"Score: {a.score:+.0f}", f"Probabilidade: {_pct(prob)}",
             f"Confiança: {a.confidence:.0f}/100", f"Horizonte: {a.horizon}", "",
             "Motivos", *[f"• {r}" for r in sig.reasons], "",
             "Antecipação", *_layer_line(a, d), "",
             "Situação", f"{a.premove.latent_pressure or a.dominant_pressure}",
             f"Estágio: {a.premove.stage.value}", "",
             "Zona de atenção", *_zone(a)]
    if a.reversal.risk >= 40:
        lines += ["", f"⚠️ Risco de reversão: {a.reversal.risk:.0f}/100"]
    if a.next_event:
        lines += ["", f"⚠️ Evento próximo: {a.next_event.name} — {a.next_event.time:%H:%M} UTC ({a.next_event.impact})"]
    lines += ["", f"Gatilho: {sig.trigger}"]
    return "\n".join(lines)


class TelegramSender:
    """Envio via Bot API (stdlib). Sem token/chat_id, apenas imprime (modo dry-run)."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, dry_run: bool = False) -> None:
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.dry_run = dry_run or not (self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if self.dry_run:
            print("\n[TELEGRAM dry-run]\n" + text + "\n")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))


# ============================================================================
# ENGINE
# ============================================================================

"""GoldAIEngine — ciclo de análise completo (Diretriz §2, §19, §20, §21, §36)."""





class GoldAIEngine:
    def __init__(self, cfg: Optional[EngineConfig] = None) -> None:
        self.cfg = cfg or EngineConfig()
        self.gate = SignalGate(self.cfg)
        self.history: list[Assessment] = []

    # ------------------------------------------------------------------ score
    def score_factors(self, s: MarketSnapshot) -> tuple[list[FactorScore], list[TechnicalReading]]:
        factors: list[FactorScore] = []
        for name, weight in self.cfg.weights.items():
            if name == "tecnico":
                continue
            factors.append(SCORERS[name](s, weight))
        tech, readings = score_tecnico(s, self.cfg.weights["tecnico"])
        factors.append(tech)
        return factors, readings

    @staticmethod
    def total_score(factors: list[FactorScore]) -> float:
        """Soma dos fatores. Fatores sem dados não penalizam: o score é reescalado
        para -100..+100 pelo peso disponível, mas com desconto para evitar que
        poucos fatores produzam extremos."""
        avail = [f for f in factors if f.available]
        if not avail:
            return 0.0
        raw = sum(f.score for f in avail)
        avail_w = sum(f.max_score for f in avail)
        total_w = sum(f.max_score for f in factors)
        coverage = avail_w / total_w if total_w else 1.0
        # reescala parcialmente: cobertura 100% → raw; cobertura 50% → raw × ~1.4 (não ×2)
        scale = (1.0 / coverage) ** 0.5 if coverage else 1.0
        return max(-100.0, min(100.0, round(raw * scale, 1)))

    # ------------------------------------------------------------------ probabilidade
    def probabilities(self, score: float, readings: list[TechnicalReading], systemic: float) -> tuple[float, float, float]:
        """Score → P(alta), P(baixa), P(lateral). Lateral cresce quando ADX baixo (§20)."""
        adxs = [r.adx for r in readings if r.adx is not None and r.timeframe in ("H1", "H4")]
        adx_mean = sum(adxs) / len(adxs) if adxs else 22.0
        lateral = self.cfg.lateral_base + 0.25 * max(0.0, min(1.0, (25 - adx_mean) / 15))
        lateral *= max(0.4, 1 - abs(score) / 120)  # score forte reduz lateral
        p_up_dir = 1.0 / (1.0 + math.exp(-self.cfg.prob_slope * score))
        directional = 1.0 - lateral
        p_up = directional * p_up_dir
        p_down = directional * (1 - p_up_dir)
        return round(p_up, 3), round(p_down, 3), round(lateral, 3)

    # ------------------------------------------------------------------ confiança
    def confidence(self, factors: list[FactorScore], score: float, s: MarketSnapshot, event) -> float:
        avail = [f for f in factors if f.available]
        coverage = sum(f.max_score for f in avail) / sum(f.max_score for f in factors)
        sign = 1.0 if score >= 0 else -1.0
        agree_w = sum(f.max_score for f in avail if sign * f.score > 0.1 * f.max_score)
        disagree_w = sum(f.max_score for f in avail if sign * f.score < -0.1 * f.max_score)
        agreement = (agree_w - disagree_w) / (sum(f.max_score for f in avail) or 1.0)
        conf = 20 + 35 * max(0.0, agreement) + 20 * coverage + 15 * min(1.0, abs(score) / 70)
        if event is not None:
            conf *= 0.75  # janela pré-evento: incerteza binária (§32)
        if s.atr and s.price and abs(s.price_change_pct) / 100 * s.price > 2.5 * s.atr:
            conf *= 0.85  # movimento já esticado
        return round(max(0.0, min(100.0, conf)), 0)

    # ------------------------------------------------------------------ horizonte e zona
    @staticmethod
    def horizon_for(readings: list[TechnicalReading], stage: Stage) -> str:
        """Horizonte pelo timeframe onde o sinal está mais claro (§21)."""
        by_tf = {r.timeframe: r for r in readings if "dados insuficientes" not in r.notes}
        if stage == Stage.PRE_MOVIMENTO:
            return HORIZONS["curto"]
        strengths = {
            "curtissimo": abs(by_tf["M5"].score) if "M5" in by_tf else 0,
            "curto": abs(by_tf["M30"].score) if "M30" in by_tf else 0,
            "intraday": abs(by_tf["H1"].score) if "H1" in by_tf else 0,
            "swing": abs(by_tf["H4"].score) if "H4" in by_tf else 0,
            "macro": abs(by_tf["D1"].score) * 0.8 if "D1" in by_tf else 0,
        }
        best = max(strengths, key=strengths.get) if any(strengths.values()) else "curto"
        return HORIZONS[best]

    @staticmethod
    def zone_for(s: MarketSnapshot, readings: list[TechnicalReading], direction: Direction) -> dict[str, Optional[float]]:
        """Zona de atenção: suporte = maior nível abaixo do preço, resistência = menor nível
        acima, candidatos vindos dos timeframes M30–D1 e das gamma walls (§9, §17)."""
        p = s.price
        ref = next((r for r in readings if r.timeframe == "H1"), None) or next((r for r in readings if r.timeframe in ("M30", "H4")), None)
        atr = (ref.atr if ref and ref.atr else s.atr) or 0.0
        levels: list[float] = []
        for r in readings:
            if r.timeframe in ("M30", "H1", "H4", "D1"):
                levels += [x for x in (r.support, r.resistance) if x is not None]
        levels += [x for x in (s.gamma_wall_below, s.gamma_wall_above) if x is not None]
        below = [x for x in levels if x < p]
        above = [x for x in levels if x > p]
        sup = max(below) if below else None
        res = min(above) if above else None
        if direction == Direction.ALTA:
            inval = sup if sup is not None and p - sup <= 2.0 * atr else p - 1.5 * atr
            return {"entry_low": round(p - 0.5 * atr, 2), "entry_high": round(p + 0.15 * atr, 2), "support": sup, "resistance": res,
                    "invalidation": round(inval - 0.25 * atr, 2)}
        if direction == Direction.BAIXA:
            inval = res if res is not None and res - p <= 2.0 * atr else p + 1.5 * atr
            return {"entry_low": round(p - 0.15 * atr, 2), "entry_high": round(p + 0.5 * atr, 2), "support": sup, "resistance": res,
                    "invalidation": round(inval + 0.25 * atr, 2)}
        return {"entry_low": None, "entry_high": None, "support": sup, "resistance": res, "invalidation": None}

    # ------------------------------------------------------------------ ciclo
    def analyze(self, s: MarketSnapshot) -> Assessment:
        factors, readings = self.score_factors(s)
        score = self.total_score(factors)
        systemic = systemic_risk_index(s)
        accum = accumulation_distribution(s)
        event = next_high_impact_event(s.events, s.time, self.cfg.event_window_minutes)
        p_up, p_down, p_flat = self.probabilities(score, readings, systemic)
        conf = self.confidence(factors, score, s, event)
        premove = analyze_premove(s, factors, readings, accum, self.cfg)
        reversal = analyze_reversal(s, factors, readings, accum)

        d1 = next((r for r in readings if r.timeframe == "D1"), None)
        h4 = next((r for r in readings if r.timeframe == "H4"), None)
        trend_src = d1 or h4
        trend = Direction(trend_src.trend) if trend_src and trend_src.trend in Direction.__members__ else Direction.LATERAL

        direction = Direction.ALTA if p_up > p_down and p_up >= p_flat else Direction.BAIXA if p_down > p_up and p_down >= p_flat else Direction.LATERAL
        horizon = self.horizon_for(readings, premove.stage)
        zone = self.zone_for(s, readings, direction if direction != Direction.LATERAL else premove.direction)

        if premove.latent_pressure:
            dominant = premove.latent_pressure
        elif score >= 30:
            dominant = "PRESSÃO COMPRADORA"
        elif score <= -30:
            dominant = "PRESSÃO VENDEDORA"
        else:
            dominant = "EQUILÍBRIO"

        a = Assessment(
            time=s.time, price=s.price, score=score, factors=factors, prob_up=p_up, prob_down=p_down, prob_flat=p_flat,
            confidence=conf, trend=trend, horizon=horizon, premove=premove, reversal=reversal, systemic_risk=systemic,
            sentiment_label=sentiment_label(s.sentiment), dominant_pressure=dominant, next_event=event, technical=readings,
            conclusion="", confirmations=[], zone=zone,
        )
        a.confirmations = confirmations(a, a.direction if a.direction != Direction.LATERAL else premove.direction, self.cfg)
        a.conclusion = self._conclusion(a)
        self.history.append(a)
        return a

    def _conclusion(self, a: Assessment) -> str:
        cls = classify(a.score, self.cfg).value
        parts = [f"{cls} (score {a.score:+.0f}, {len(a.confirmations)} confirmações: {', '.join(a.confirmations) or 'nenhuma'})."]
        if a.premove.stage == Stage.PRE_MOVIMENTO:
            parts.append(f"{a.premove.latent_pressure}: fundamentos apontam {a.premove.direction.value.lower()} com prob. {a.premove.probability:.0%}, preço ainda não confirmou (confirmação técnica {a.premove.price_confirmation:.0%}).")
        elif a.premove.stage == Stage.CONFIRMACAO:
            parts.append(f"Preço começa a acompanhar os fundamentos ({a.premove.direction.value.lower()}).")
        elif a.premove.stage == Stage.MOVIMENTO:
            parts.append(f"Movimento já ocorreu ({a.premove.move_in_atr:+.1f} ATR): evitar perseguir o preço.")
        if a.reversal.risk >= 40:
            parts.append(f"Risco de reversão {a.reversal.risk:.0f}/100 contra a tendência de {a.reversal.current_trend.value.lower()}.")
        if a.next_event:
            parts.append(f"Evento de alto impacto próximo: {a.next_event.name} às {a.next_event.time:%H:%M} UTC — confiança reduzida.")
        if a.systemic_risk >= 60:
            parts.append(f"Risco sistêmico elevado ({a.systemic_risk:.0f}/100).")
        parts.append("Pergunta-chave: o que o mercado ainda não precificou?")
        return " ".join(parts)

    # ------------------------------------------------------------------ sinal
    def evaluate_signal(self, a: Assessment, new_event_key: Optional[str] = None) -> Optional[Signal]:
        sig = self.gate.evaluate(a, new_event_key)
        if sig is not None:
            sig.text = format_signal(sig)
        return sig

    def run_cycle(self, s: MarketSnapshot, new_event_key: Optional[str] = None) -> tuple[Assessment, Optional[Signal]]:
        a = self.analyze(s)
        return a, self.evaluate_signal(a, new_event_key)


# ============================================================================
# REPORT
# ============================================================================

"""Saída interna de cada ciclo (Diretriz §36)."""




def render_report(a: Assessment) -> str:
    f = {x.name: x for x in a.factors}

    def line(name: str, label: str) -> str:
        x = f.get(name)
        if x is None or not x.available:
            return f"{label}: ⚪ n/d"
        return f"{label}: {x.emoji} {x.score:+.0f}/{x.max_score:.0f} — {x.rationale}"

    tech = " | ".join(f"{r.timeframe}:{r.trend[0]}({r.score:+.2f})" for r in a.technical if "dados insuficientes" not in r.notes)
    lines = [
        "GOLD AI",
        "",
        f"Hora: {a.time:%Y-%m-%d %H:%M} UTC",
        f"Preço: {a.price:.2f}",
        f"Tendência: {a.trend.value}",
        f"Score: {a.score:+.0f}",
        f"Probabilidade de alta: {a.prob_up:.0%}",
        f"Probabilidade de baixa: {a.prob_down:.0%}",
        f"Probabilidade lateral: {a.prob_flat:.0%}",
        f"Confiança: {a.confidence:.0f}/100",
        f"Horizonte: {a.horizon}",
        "",
        line("dolar", "Dólar"),
        line("juros_reais", "Juros reais"),
        line("fed", "Fed"),
        line("inflacao", "Inflação"),
        line("geopolitica", "Geopolítica"),
        line("fluxo", "Fluxo"),
        line("cot", "COT"),
        line("opcoes", "Opções"),
        line("sentimento", "Sentimento"),
        line("tecnico", "Técnico"),
        f"  timeframes: {tech or 'n/d'}",
        "",
        f"Sentimento global: {a.sentiment_label.value}",
        f"Risco sistêmico: {a.systemic_risk:.0f}/100",
        f"Pressão dominante: {a.dominant_pressure}",
        f"Pré-movimento: {a.premove.stage.value} ({a.premove.direction.value}, prob. {a.premove.probability:.0%}, confirmação técnica {a.premove.price_confirmation:.0%}, {a.premove.move_in_atr:+.1f} ATR)",
        f"Risco de reversão: {a.reversal.risk:.0f}/100" + (f" — {'; '.join(a.reversal.evidence)}" if a.reversal.evidence else ""),
        f"Evento próximo: {a.next_event.name + ' ' + a.next_event.time.strftime('%H:%M') + ' UTC' if a.next_event else 'nenhum na janela'}",
        f"Confirmações: {', '.join(a.confirmations) or 'nenhuma'}",
        "",
        f"Conclusão: {a.conclusion}",
    ]
    return "\n".join(lines)


# ============================================================================
# SAMPLE
# ============================================================================

"""Gerador de cenários sintéticos para demonstração/testes (sem dependências)."""




TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}


def make_candles(tf: str, n: int, start_price: float, drift: float, vol: float, end: datetime, seed: int = 7, volume_trend: float = 0.0) -> list[Candle]:
    """Série de candles com deriva (`drift` por candle, em USD) e volatilidade `vol` (USD)."""
    rnd = random.Random(seed + hash(tf) % 1000)
    step = timedelta(minutes=TF_MINUTES[tf])
    price = start_price
    out: list[Candle] = []
    for i in range(n):
        t = end - step * (n - 1 - i)
        o = price
        c = o + drift + rnd.gauss(0, vol)
        h = max(o, c) + abs(rnd.gauss(0, vol * 0.5))
        l = min(o, c) - abs(rnd.gauss(0, vol * 0.5))
        v = max(10.0, 1000 * (1 + volume_trend * (i / n)) + rnd.gauss(0, 150))
        out.append(Candle(t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(v, 0)))
        price = c
    return out


def _candles_all(price: float, end: datetime, drift_per_hour: float, vol: float, seed: int, volume_trend: float = 0.0) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = {}
    for tf, mins in TF_MINUTES.items():
        n = 260 if mins <= 240 else 120
        drift = drift_per_hour * mins / 60
        v = vol * (mins / 60) ** 0.5
        # start so that the series ends near `price`
        start = price - drift * n
        cs = make_candles(tf, n, start, drift, v, end, seed, volume_trend)
        shift = price - cs[-1].close
        for c in cs:
            c.open, c.high, c.low, c.close = c.open + shift, c.high + shift, c.low + shift, c.close + shift
        out[tf] = cs
    return out


class SampleSource:
    """Cenários: 'premove_alta', 'confirmacao_alta', 'venda', 'neutro', 'reversao', 'sistemico', 'pre_evento'."""

    def __init__(self, scenario: str = "premove_alta", price: float = 2650.0, seed: int = 7, now: datetime | None = None) -> None:
        self.scenario = scenario
        self.price = price
        self.seed = seed
        self.now = now or datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)

    def snapshot(self) -> MarketSnapshot:
        now, p = self.now, self.price
        sc = self.scenario
        s = MarketSnapshot(time=now, price=p, atr=9.0)

        if sc == "premove_alta":
            # fundamentos viraram; preço lateral (§14)
            s.candles = _candles_all(p, now, drift_per_hour=0.0, vol=3.0, seed=self.seed, volume_trend=0.6)
            s.price_change_pct = 0.05
            s.dxy, s.dxy_change_pct = 103.2, -0.35
            s.us10y, s.us10y_change_bp, s.real_yield_change_bp = 4.05, -6.0, -5.0
            s.fed_cut_prob_change_pp, s.fed_tone = 12.0, 0.3
            s.inflation_surprise_sigma = -1.0
            s.geopolitical_risk, s.geopolitical_risk_change = 55, 4
            s.etf_flow_musd, s.order_flow_imbalance, s.open_interest_change_pct = 220.0, 0.35, 2.1
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = 8000, 55
            s.put_call_ratio, s.implied_vol_change_pct = 1.05, 3.0
            s.sentiment, s.sentiment_change = 0.35, 0.2
            s.vix, s.vix_change_pct, s.credit_spread_bp = 15.0, 2.0, 330
            s.news = [NewsItem("CPI dos EUA abaixo do esperado; núcleo desacelera", "BLS", now, "macro", gold_impact=0.7, priced_in=0.3,
                               interpretation="aumenta probabilidade de corte do Fed → yields ↓ → dólar ↓ → suporte ao ouro")]
        elif sc == "confirmacao_alta":
            s.candles = _candles_all(p, now, drift_per_hour=1.6, vol=3.0, seed=self.seed, volume_trend=0.5)
            s.price_change_pct = 0.35
            s.dxy_change_pct, s.real_yield_change_bp, s.us10y_change_bp = -0.4, -6.0, -7.0
            s.fed_cut_prob_change_pp, s.fed_tone = 10.0, 0.3
            s.inflation_surprise_sigma = -0.8
            s.geopolitical_risk, s.geopolitical_risk_change = 50, 2
            s.etf_flow_musd, s.order_flow_imbalance, s.open_interest_change_pct = 260.0, 0.4, 2.5
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = 9000, 60
            s.put_call_ratio = 1.0
            s.sentiment = 0.4
            s.vix, s.credit_spread_bp = 14.5, 320
        elif sc == "venda":
            s.candles = _candles_all(p, now, drift_per_hour=-1.4, vol=3.0, seed=self.seed)
            s.price_change_pct = -0.3
            s.dxy_change_pct, s.real_yield_change_bp, s.us10y_change_bp = 0.45, 7.0, 8.0
            s.fed_cut_prob_change_pp, s.fed_tone = -10.0, -0.4
            s.inflation_surprise_sigma = 1.2
            s.geopolitical_risk, s.geopolitical_risk_change = 40, -3
            s.etf_flow_musd, s.order_flow_imbalance = -180.0, -0.35
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = -7000, 45
            s.put_call_ratio = 0.7
            s.sentiment, s.sentiment_change = -0.4, -0.2
            s.vix, s.credit_spread_bp = 13.0, 310
        elif sc == "reversao":
            # tendência de alta com distribuição (§16)
            s.candles = _candles_all(p, now, drift_per_hour=1.2, vol=3.0, seed=self.seed, volume_trend=-0.6)
            s.price_change_pct = 0.2
            s.dxy_change_pct, s.real_yield_change_bp = 0.3, 5.0
            s.fed_cut_prob_change_pp, s.fed_tone = -8.0, -0.3
            s.etf_flow_musd, s.order_flow_imbalance = -200.0, -0.4
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = 2000, 94
            s.sentiment = 0.7
            s.vix, s.credit_spread_bp = 14.0, 310
        elif sc == "sistemico":
            s.candles = _candles_all(p, now, drift_per_hour=-2.0, vol=6.0, seed=self.seed, volume_trend=1.0)
            s.price_change_pct = -0.8
            s.dxy_change_pct, s.real_yield_change_bp = 0.6, -4.0
            s.vix, s.vix_change_pct, s.credit_spread_bp, s.credit_spread_change_bp = 34.0, 45.0, 620, 60
            s.equity_change_pct, s.bank_stress = -3.5, 70
            s.geopolitical_risk, s.geopolitical_risk_change = 60, 5
            s.etf_flow_musd, s.order_flow_imbalance = 80.0, -0.2
            s.sentiment = -0.3
        elif sc == "pre_evento":
            s.candles = _candles_all(p, now, drift_per_hour=0.2, vol=2.5, seed=self.seed)
            s.price_change_pct = 0.05
            s.dxy_change_pct, s.real_yield_change_bp = -0.1, -1.0
            s.fed_cut_prob_change_pp = 2.0
            s.cot_managed_money_percentile, s.put_call_ratio = 85, 0.95
            s.sentiment = 0.2
            s.vix, s.credit_spread_bp = 15.5, 330
            s.events = [EconomicEvent("CPI EUA", now + timedelta(minutes=30), "MUITO ALTO", consensus=0.3, previous=0.2, kind="cpi", unit="%")]
        else:  # neutro
            s.candles = _candles_all(p, now, drift_per_hour=0.0, vol=3.0, seed=self.seed)
            s.price_change_pct = 0.0
            s.dxy_change_pct, s.real_yield_change_bp = 0.05, 0.5
            s.fed_cut_prob_change_pp, s.fed_tone = 0.0, 0.0
            s.etf_flow_musd, s.order_flow_imbalance = 10.0, 0.02
            s.sentiment = 0.0
            s.vix, s.credit_spread_bp = 15.0, 330
        return s


# ============================================================================
# CLI
# ============================================================================

"""CLI: `gold-ai demo`, `gold-ai run`, `gold-ai stats`, `gold-ai event`."""





def cmd_demo(args: argparse.Namespace) -> int:
    engine = GoldAIEngine(EngineConfig())
    sender = TelegramSender(dry_run=not args.send)
    mem = PredictionMemory(args.db) if args.db else None
    scenarios = args.scenarios or ["neutro", "premove_alta", "confirmacao_alta", "reversao", "venda", "sistemico", "pre_evento"]
    src = SampleSource()
    for i, sc in enumerate(scenarios):
        src.scenario = sc
        src.now = src.now + timedelta(minutes=20)
        src.price += {"confirmacao_alta": 6, "venda": -8, "reversao": 4, "sistemico": -18}.get(sc, 0)
        snap = src.snapshot()
        assessment, signal = engine.run_cycle(snap, new_event_key=snap.news[0].headline if snap.news else None)
        print(f"\n{'=' * 78}\nCENÁRIO {i + 1}: {sc}\n{'=' * 78}")
        print(render_report(assessment))
        for ev in upcoming_events(snap.events, snap.time, hours=6):
            print("\n" + build_scenario_tree(ev, snap).render())
        if signal:
            print(f"\n>>> SINAL: {signal.type.value} ({signal.trigger})")
            sender.send(signal.text)
            if mem:
                pid = mem.record(assessment, signal.type.value)
                print(f"[memória] previsão #{pid} registrada")
        else:
            print("\n>>> sem sinal (anti-spam / critérios não atingidos)")
    if mem:
        mem.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Loop contínuo. Substitua SampleSource por uma fonte real (gold_ai.sources.base.DataSource)."""
    engine = GoldAIEngine(EngineConfig())
    sender = TelegramSender(dry_run=not args.send)
    mem = PredictionMemory(args.db)
    src = SampleSource(scenario=args.scenario)
    try:
        while True:
            snap = src.snapshot()
            assessment, signal = engine.run_cycle(snap)
            if args.verbose:
                print(render_report(assessment))
            if signal:
                sender.send(signal.text)
                mem.record(assessment, signal.type.value)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        mem.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    mem = PredictionMemory(args.db)
    pending = mem.pending()
    print(f"Previsões pendentes (sem resultado): {len(pending)} — resolva com PredictionMemory.resolve(id, caminho_de_preço, limiar)")
    for by in args.by:
        print(f"\nTAXA DE ACERTO por {by}:")
        rows = mem.accuracy(by)
        if not rows:
            print("  (nenhuma previsão resolvida)")
        for row in rows:
            print(f"  {row['chave']!s:>12}  n={row['n']:<4} acertos={row['acertos']:<4} taxa={row['taxa']:.1%}")
    print("\nPODER PREDITIVO DOS FATORES (média alinhada nos acertos − nos erros):")
    for row in mem.factor_power():
        print(f"  {row['fator']:<12} poder={row['poder']:+.2f} (n={row['n']})")
    mem.close()
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    src = SampleSource(scenario="pre_evento")
    snap = src.snapshot()
    ev = snap.events[0]
    print(build_scenario_tree(ev, snap).render())
    if args.actual is not None:
        ev.actual = args.actual
        snap.dxy_change_pct, snap.us10y_change_bp, snap.real_yield_change_bp = args.dxy, args.us10y, args.real
        snap.price_change_pct, snap.order_flow_imbalance = args.gold, args.flow
        print("\n" + analyze_post_event(ev, snap).render())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gold-ai", description="GOLD AI ENGINE — inteligência preditiva do ouro (XAU/USD)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="roda cenários sintéticos e mostra relatórios/sinais")
    d.add_argument("--scenarios", nargs="*")
    d.add_argument("--send", action="store_true", help="envia de fato ao Telegram (usa TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
    d.add_argument("--db", default=None, help="caminho do SQLite para registrar previsões")
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="loop contínuo de análise")
    r.add_argument("--interval", type=int, default=60)
    r.add_argument("--scenario", default="neutro")
    r.add_argument("--db", default="gold_ai.db")
    r.add_argument("--send", action="store_true")
    r.add_argument("--once", action="store_true")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("stats", help="taxa de acerto e poder preditivo dos fatores")
    s.add_argument("--db", default="gold_ai.db")
    s.add_argument("--by", nargs="*", default=["sessao", "previsao", "score_bucket", "estagio"])
    s.set_defaults(func=cmd_stats)

    e = sub.add_parser("event", help="árvore de reação pré-evento e cadeia pós-evento (exemplo CPI)")
    e.add_argument("--actual", type=float, default=None)
    e.add_argument("--dxy", type=float, default=0.0)
    e.add_argument("--us10y", type=float, default=0.0)
    e.add_argument("--real", type=float, default=0.0)
    e.add_argument("--gold", type=float, default=0.0)
    e.add_argument("--flow", type=float, default=0.0)
    e.set_defaults(func=cmd_event)

    args = p.parse_args(argv)
    return int(args.func(args))





# ============================================================================
# INTERFACE DE FONTE DE DADOS
# ============================================================================

class DataSource(Protocol):
    """Implemente snapshot() devolvendo um MarketSnapshot com dados reais (MT5, FRED, CFTC, notícias)."""

    def snapshot(self) -> MarketSnapshot:  # pragma: no cover
        ...


if __name__ == "__main__":
    sys.exit(main())
