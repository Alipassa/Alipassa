#!/usr/bin/env python3
"""MARKET AI ENGINE 4.0 — cérebro único · múltiplos mercados · seleção dinâmica da melhor oportunidade.

Gerado por tools/build_single_file.py a partir do pacote gold_ai/ (versão 4.0.0).
Equivalente a `python -m gold_ai`. Não existem outros bundles suportados (v1/v2/v3 removidos).

REGRA CENTRAL: maximizar o aproveitamento das oportunidades estatisticamente válidas, a expectancy e o potencial de ganho,
mantendo o risco controlado — sem sacrificar captura de oportunidades em busca de uma taxa de acerto artificialmente alta.
Alvo = acerto + captura + expectancy + ganho + controle de drawdown (docs/MISSAO.md).

"Analisar vários mercados simultaneamente e operar somente aquele que apresentar a melhor vantagem
estatística disponível naquele momento, respeitando risco, correlação, qualidade dos dados e custo de
execução." A IA não precisa operar ouro; precisa encontrar onde existe vantagem.

XAUUSD · EURUSD · US500 · USDJPY · WTI (fase 1) → 📡 DATA ENGINE (macro uma vez + candles por mercado)
→ 🧠 PREDICTION ENGINE (cérebro único; cada mercado declara o sinal de cada fator)
→ 🔥 OPPORTUNITY ENGINE → 🏆 ASSET SELECTOR (histórico ajustado à amostra × oportunidade atual × decay)
→ 📐 PORTFOLIO EXPOSURE (correlação; mesma aposta três vezes ≠ diversificação) → RISK ENGINE (capital único)
→ TRADE ENGINE → MT5 → confirmação → 🔄 TRADE MONITOR 24/7 → ADAPTIVE EXIT → resultado → capital

🌎 MUNDO → 📡 DATA ENGINE (Yahoo · FRED · CFTC · RSS · calendário · MetaTrader 5)
→ MARKET SNAPSHOT → 🧠 PREDICTION ENGINE (score · probabilidade · confiança · pré-movimento)
→ CADEIA DE RACIOCÍNIO · NÍVEL DE EVIDÊNCIA · VANTAGEM ESTATÍSTICA ("NÃO SEI")
→ DECISION ENGINE → 🧾 TRADE ENGINE (stop inteligente · 1R–4R · MAX PROFIT ENGINE · viabilidade)
→ RISK ENGINE (capital → risco % fixo → lote; TRADING STOP; drawdown; kill switch)
→ EXECUTION ENGINE (MT5 → broker → confirmação real · EXECUTION MISMATCH)
→ 🔄 TRADE MONITOR (TRADE/THESIS/EXIT SCORE · PROFIT POTENTIAL · MANTER/PROTEGER/REDUZIR/ESTENDER/ENCERRAR)
→ RESULTADO → SQLite → PERFORMANCE → NOVO CAPITAL → NOVO LOTE
→ VALIDATION (anti look-ahead · walk-forward · calibração · score por fator · lead time · MFE/MAE)

Modos: 🟢 PAPER (padrão) · 🟡 AUTHORIZE · 🟠 SEMI-LIVE · 🔴 LIVE (exige --authorize)
Comandos Telegram: /STOP /PAUSE /RESUME /STATUS /CLOSE (com /CLOSE CONFIRM)

Uso (4.0, multi-mercado):
    python market_ai_engine_v4.py markets                                              # ranking agora, não opera
    python market_ai_engine_v4.py edge                                                 # 🚨 LIVE EDGE — o teste definitivo (o que foi vivido)
    python market_ai_engine_v4.py estimate --start 2026-01-01 --markets EURUSD,US500,XAUUSD,USDJPY,WTI --equity 10000   # estimativa de lucro OOS
    python market_ai_engine_v4.py sweep --start 2026-01-01 --market US500        # piso de vantagem escolhido no treino de cada fold
    python market_ai_engine_v4.py history fetch-alfred|fetch-te|fetch-gdelt      # banco histórico point-in-time de eventos/notícias
    python market_ai_engine_v4.py compare-news --start 2026-01-01 --markets US500,XAUUSD   # Preço só × +Macro (A) × +Macro+News (B)
    python market_ai_engine_v4.py live --markets EURUSD,US500,XAUUSD,USDJPY,WTI --source mt5 --mode paper --send
    python market_ai_engine_v4.py validate --markets EURUSD,US500,XAUUSD,USDJPY,WTI [--csv-dir dados/]
Uso (3.0, um mercado):
    python market_ai_engine_v4.py live --source mt5 --mode paper|authorize|semi-live|live [--authorize] --send
    python market_ai_engine_v4.py status | stats | validate | simulate | calibrate | backtest | metrics | demo | event

Credenciais e limites no .env (ver .env.example). Sem dependências externas (MetaTrader5 opcional, Windows).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

try:  # MetaTrader5 só existe no Windows com o terminal instalado
    import MetaTrader5 as _mt5  # type: ignore
except Exception:  # noqa: BLE001
    _mt5 = None

__version__ = "4.0.0"


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

    # 4.0: sinal com que cada fator (calculado na convenção do ouro) afeta o mercado analisado.
    # +1 mesma direção, −1 oposta, 0 sem relação conhecida (fator marcado como indisponível).
    factor_signs: dict[str, int] = field(default_factory=dict)
    symbol: str = "XAUUSD"

    # Vantagem estatística (GOLD AI 2.0 — "saber dizer NÃO SEI").
    min_edge_probability: float = 0.55
    min_edge_confidence: float = 50.0
    min_edge_score: float = 25.0

    # GOLD WATCH: alerta de observação (nível 2, ainda sem sinal operacional).
    watch_min_probability: float = 0.60


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

    WATCH = "GOLD WATCH"
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
    # NEWS ENGINE (4.0): pressão específica do mercado; None = UNKNOWN (peso reduzido, nunca negativo)
    news_pressure: Optional[float] = None   # -1..+1
    news_status: str = "UNKNOWN"            # UNKNOWN | FAVORÁVEL | CONTRÁRIO | NEUTRO
    news_chain: str = ""
    # REACTION ENGINE (4.0): relógio de reação do evento mais relevante (assimetria temporal líderes × alvo)
    reaction_status: str = "SEM EVENTO"     # SEM EVENTO | AGUARDANDO | PRESSÃO LATENTE | REAGIU | DIVERGÊNCIA | EXPIRADO
    reaction_pressure: float = 0.0          # −1..+1
    reaction_probability: Optional[float] = None
    reaction_latency_min: Optional[float] = None
    reaction_expected_min: Optional[float] = None
    reaction_chain: str = ""
    # COT: último dado válido conhecido + idade (semanal; o peso decai com a idade)
    cot_age_days: Optional[float] = None
    cot_report_date: Optional[str] = None

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


class EvidenceLevel(int, Enum):
    """Nível de evidência (GOLD AI 2.0)."""

    NONE = 0
    L1_OBSERVACAO = 1   # 1–2 fatores
    L2_ALERTA = 2       # 3 fatores independentes
    L3_SINAL = 3        # macro + fluxo + técnico
    L4_PREMOVE_FORTE = 4  # macro + fluxo + técnico + notícia/evento + divergência preço/fundamento

    @property
    def label(self) -> str:
        return {0: "SEM EVIDÊNCIA", 1: "NÍVEL 1 — OBSERVAÇÃO", 2: "NÍVEL 2 — ALERTA", 3: "NÍVEL 3 — SINAL", 4: "🔥 NÍVEL 4 — PRE-MOVE FORTE"}[int(self)]


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
    evidence_level: "EvidenceLevel" = EvidenceLevel.NONE
    edge_status: str = ""          # "VANTAGEM ESTATÍSTICA: ALTA" | "🟡 SEM VANTAGEM ESTATÍSTICA"
    has_edge: bool = False
    chain: str = ""                # raciocínio em cadeia do evento (9 passos)
    regime: str = "INDEFINIDO"     # BULLISH | BEARISH | RANGE | VOLATILE

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


_atr = atr  # alias usado pelo Data Engine, MT5 e avaliação


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
def cot_age_weight(age_days: Optional[float]) -> float:
    """COT é semanal: até 10 dias peso cheio; decai linearmente até 0.3 em 21 dias; > 35 dias indisponível (0)."""
    if age_days is None:
        return 1.0
    if age_days <= 10:
        return 1.0
    if age_days >= 35:
        return 0.0
    if age_days <= 21:
        return 1.0 - 0.7 * (age_days - 10) / 11
    return 0.3


def score_cot(s: MarketSnapshot, w: float) -> FactorScore:
    if s.cot_managed_money_net_change is None and s.cot_managed_money_percentile is None:
        return _factor("cot", w, 0.0, "sem dados", available=False)
    aw = cot_age_weight(s.cot_age_days)
    if aw == 0.0:
        return _factor("cot", w, 0.0, f"COT antigo demais ({s.cot_age_days:.0f} dias) — indisponível", available=False)
    ratio = 0.0
    notes: list[str] = []
    if s.cot_age_days is not None:
        notes.append(f"relatório {s.cot_report_date or ''} há {s.cot_age_days:.0f} dias (peso {aw:.0%})".strip())
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
    return _factor("cot", w, ratio * aw, f"COT: {', '.join(notes) or 'neutro'}")


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
    """NEWS ENGINE: ausência = UNKNOWN (fator indisponível), nunca negativo. Com pressão de notícias
    específica do mercado, ela domina; o sentimento agregado entra como complemento."""
    if s.news_pressure is not None:
        parts = [(_clip(s.news_pressure, -1, 1), 0.7)]
        notes = [f"NEWS {s.news_status} (pressão {s.news_pressure:+.2f})"]
        if s.sentiment is not None:
            parts.append((_clip(s.sentiment, -1, 1), 0.3))
            notes.append(sentiment_label(s.sentiment).value.lower())
        ratio = sum(v * wt for v, wt in parts) / sum(wt for _, wt in parts)
        return _factor("sentimento", w, ratio, "notícias: " + ", ".join(notes))
    if s.news_status == "UNKNOWN" and s.sentiment is None and not s.news:
        return _factor("sentimento", w, 0.0, "NEWS UNKNOWN — fonte indisponível (peso reduzido, não negativo)", available=False)
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
    # REACTION ENGINE: assimetria temporal (líderes reagiram, alvo ainda não) é evidência a favor; divergência reduz. Nunca veta.
    if s.reaction_status == "PRESSÃO LATENTE" and (s.reaction_pressure > 0) == (direction == Direction.ALTA):
        prob += 0.08
        lat = f"{s.reaction_latency_min:.0f} min" if s.reaction_latency_min is not None else "n/d"
        med = f" vs mediana {s.reaction_expected_min:.0f} min" if s.reaction_expected_min else ""
        notes.append(f"relógio de reação: líderes já reagiram, preço ainda não (T+{lat}{med})")
    elif s.reaction_status == "DIVERGÊNCIA":
        prob -= 0.06
        notes.append("relógio de reação: mercado diverge da notícia")

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
# EVIDENCE
# ============================================================================

"""GOLD AI 2.0 — nível de evidência, cadeia de raciocínio do evento e vantagem estatística.

O sistema não faz "notícia → sentimento → compra". Ele percorre:
1. O que aconteceu?  2. O que o mercado esperava?  3. Qual foi a surpresa?
4. Juros?  5. Dólar?  6. Ouro?  7. Fluxo?  8. Pressão latente?  9. Só então: sinal.
E precisa saber dizer "NÃO SEI" (sem vantagem estatística → não enviar).
"""





# --------------------------------------------------------------------------- nível de evidência
def evidence_level(a: Assessment, s: MarketSnapshot) -> EvidenceLevel:
    fam = set(a.confirmations)
    n = len(fam)
    if n == 0:
        return EvidenceLevel.NONE
    macro_ok = bool(fam & {"macro", "juros", "dolar"})
    core3 = macro_ok and "fluxo" in fam and "tecnico" in fam
    direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
    sign = 1.0 if direction == Direction.ALTA else -1.0
    news_support = any(sign * n_.gold_impact * (1 - n_.priced_in) >= 0.3 for n_ in s.news) or any(
        _event_supports(e, sign) for e in s.events if e.actual is not None
    )
    divergence = a.premove.stage == Stage.PRE_MOVIMENTO and a.premove.direction == direction
    if core3 and news_support and divergence:
        return EvidenceLevel.L4_PREMOVE_FORTE
    if core3:
        return EvidenceLevel.L3_SINAL
    if n >= 3:
        return EvidenceLevel.L2_ALERTA
    return EvidenceLevel.L1_OBSERVACAO


def _event_supports(e: EconomicEvent, sign: float) -> bool:
    sens = EVENT_GOLD_SENSITIVITY.get(e.kind, 0.0)
    sp = e.surprise()
    return sp is not None and sens != 0.0 and sign * sens * sp > 0


# --------------------------------------------------------------------------- vantagem estatística
def edge_status(a: Assessment, cfg: EngineConfig) -> tuple[bool, str]:
    """Só há vantagem quando a probabilidade dominante, a confiança e o score superam os mínimos."""
    p_max = max(a.prob_up, a.prob_down)
    if p_max < cfg.min_edge_probability or a.confidence < cfg.min_edge_confidence or abs(a.score) < cfg.min_edge_score:
        return False, "🟡 SEM VANTAGEM ESTATÍSTICA — NÃO ENVIAR SINAL"
    d = "ALTA" if a.prob_up > a.prob_down else "BAIXA"
    return True, f"🟢 VANTAGEM ESTATÍSTICA: {d} ({p_max:.0%}, confiança {a.confidence:.0f}/100)"


# --------------------------------------------------------------------------- cadeia do evento
def _react(x: Optional[float], up: str, down: str, thr: float, unit: str = "") -> str:
    if x is None:
        return "sem dado"
    if x > thr:
        return f"{up} ({x:+.2f}{unit})"
    if x < -thr:
        return f"{down} ({x:+.2f}{unit})"
    return f"estável ({x:+.2f}{unit})"


def event_chain(a: Assessment, s: MarketSnapshot) -> str:
    """Raciocínio em 9 passos a partir do evento/notícia mais relevante da janela."""
    released = [e for e in s.events if e.actual is not None and s.time - e.time <= timedelta(hours=6)]
    ev = max(released, key=lambda e: e.time) if released else None
    top_news = max(s.news, key=lambda n: abs(n.gold_impact) * (1 - n.priced_in), default=None) if s.news else None

    lines: list[str] = ["CADEIA DE RACIOCÍNIO"]
    if ev is not None:
        sp = ev.surprise()
        lines.append(f"1. O que aconteceu? {ev.name} = {ev.actual}{ev.unit}")
        lines.append(f"2. O que o mercado esperava? consenso = {ev.consensus}{ev.unit}" if ev.consensus is not None else "2. Sem consenso disponível")
        lines.append(f"3. Surpresa: {sp:+.2f}{ev.unit}" if sp is not None else "3. Surpresa: n/d")
    elif top_news is not None:
        lines.append(f"1. O que aconteceu? \"{top_news.headline}\"")
        lines.append(f"2. Interpretação: {top_news.interpretation or 'n/d'}")
        lines.append(f"3. Já precificado: {top_news.priced_in:.0%} → impacto líquido {top_news.gold_impact * (1 - top_news.priced_in):+.2f}")
    else:
        lines.append("1–3. Sem evento ou notícia relevante na janela (leitura puramente de mercado)")
    lines.append(f"4. Juros: Treasury 10Y {_react(s.us10y_change_bp, 'subindo', 'caindo', 1.5, 'bp')}; reais {_react(s.real_yield_change_bp, 'subindo', 'caindo', 1.5, 'bp')}")
    lines.append(f"5. Dólar: DXY {_react(s.dxy_change_pct, 'subindo', 'caindo', 0.08, '%')}")
    lines.append(f"6. Ouro: {_react(s.price_change_pct, 'subindo', 'caindo', 0.12, '%')} — {'ainda não reagiu' if abs(s.price_change_pct) < 0.12 else 'já reagindo'}")
    lines.append(f"7. Fluxo: {_react(s.order_flow_imbalance, 'compradores aparecendo', 'vendedores aparecendo', 0.12)}; ETFs {f'{s.etf_flow_musd:+.0f}M' if s.etf_flow_musd is not None else 'n/d'}")
    if a.premove.latent_pressure:
        lines.append(f"8. O modelo identifica: {a.premove.latent_pressure} ({a.premove.stage.value})")
    else:
        lines.append(f"8. O modelo identifica: {a.dominant_pressure} ({a.premove.stage.value})")
    if a.has_edge:
        d = "ALTA" if a.prob_up > a.prob_down else "BAIXA"
        p = max(a.prob_up, a.prob_down)
        lines.append(f"9. Veredito: {a.evidence_level.label} → {d} {p:.0%} · confiança {a.confidence:.0f}/100")
    else:
        lines.append(f"9. Veredito: {a.edge_status}")
    if s.news_chain:
        lines.append(s.news_chain)
    elif s.news_status == "UNKNOWN":
        lines.append("NEWS: UNKNOWN — sem notícias/eventos identificados (peso reduzido, não negativo)")
    if s.reaction_chain and s.reaction_status != "SEM EVENTO":
        lines.append(s.reaction_chain)
    return "\n".join(lines)


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
    last_reason: str = ""   # motivo do último None (funil de entrada)

    def evaluate(self, a: Assessment, new_event_key: Optional[str] = None) -> Optional[Signal]:
        self.last_reason = ""
        base_type = classify(a.score, self.cfg)
        direction = a.direction
        stage = a.premove.stage
        confs = a.confirmations
        trigger: Optional[str] = None
        sig_type: Optional[SignalType] = None

        # 0. "NÃO SEI": sem vantagem estatística não há sinal direcional (risco/reversão continuam passando)
        directional_allowed = a.has_edge

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

        if not directional_allowed:
            self.last_stage, self.last_direction, self.last_score = stage, direction, a.score
            self.last_type = SignalType.NEUTRAL
            self.last_reason = "SEM_VANTAGEM"
            return None

        # 4. surgimento de pré-movimento (fundamentos antecipam o preço)
        if stage == Stage.PRE_MOVIMENTO and self.last_stage != Stage.PRE_MOVIMENTO and len(confs) >= self.cfg.min_confirmations:
            sig_type, trigger = SignalType.PRE_MOVE, "surgimento de pré-movimento"
            direction = a.premove.direction
        # 4b. GOLD WATCH: nível 2 de evidência, ainda sem sinal operacional
        elif base_type == SignalType.NEUTRAL and a.evidence_level >= EvidenceLevel.L2_ALERTA \
                and max(a.prob_up, a.prob_down) >= self.cfg.watch_min_probability and self.last_type != SignalType.WATCH:
            sig_type, trigger = SignalType.WATCH, "evidência nível 2 — observação"
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
            self.last_reason = "SCORE_SINAL" if base_type == SignalType.NEUTRAL else "ANTI_SPAM"
            return None

        # filtro §27: sinal direcional exige >= 3 confirmações independentes
        if sig_type in (SignalType.STRONG_BUY, SignalType.BUY, SignalType.SELL, SignalType.STRONG_SELL, SignalType.PRE_MOVE, SignalType.WATCH):
            if len(confs) < self.cfg.min_confirmations:
                self.last_type = SignalType.NEUTRAL
                self.last_reason = "CONFIRMACOES"
                return None
            if stage == Stage.MOVIMENTO and sig_type != SignalType.PRE_MOVE:
                # §22 estágio 3: não perseguir preço — rebaixa para neutro
                self.last_type = SignalType.NEUTRAL
                self.last_reason = "ESTAGIO_3"
                return None

        # intervalo mínimo entre alertas do mesmo tipo/direção
        if self.last_sent_at and (a.time - self.last_sent_at).total_seconds() < self.cfg.min_seconds_between_alerts \
                and sig_type == self.last_type and trigger not in ("mudança de direção", "surgimento de pré-movimento"):
            self.last_reason = "INTERVALO_MINIMO"
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
    nivel_evidencia INTEGER DEFAULT 0,
    fatores_ratio TEXT,
    tecnico_detalhe TEXT,
    atr REAL,
    horizonte_min INTEGER DEFAULT 240,
    resultado TEXT,
    tempo_ate_reacao_min REAL,
    maxima_favoravel REAL,
    maxima_adversa REAL,
    preco_final REAL,
    resolvido_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_resultado ON predictions(resultado);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER,
    aberta_em TEXT NOT NULL,
    modo TEXT NOT NULL,
    sinal_tipo TEXT,
    direcao TEXT NOT NULL,
    entrada REAL NOT NULL,
    stop REAL NOT NULL,
    atr REAL,
    lote REAL,
    risco_usd REAL,
    alvos TEXT,
    estrategia TEXT,
    horizonte_min INTEGER DEFAULT 240,
    status TEXT DEFAULT 'OPEN',
    max_r REAL, mae_r REAL,
    hit_1r INTEGER, hit_2r INTEGER, hit_3r INTEGER, hit_4r INTEGER,
    estopada INTEGER,
    resultados TEXT,
    fechada_em TEXT,
    tese TEXT,
    estado TEXT,
    motivo_saida TEXT,
    resultado_r REAL,
    gerenciada_em TEXT,
    capital REAL,
    risco_pct REAL,
    ticket INTEGER,
    preco_execucao REAL,
    sl_real REAL,
    tp_real REAL,
    slippage REAL,
    execucao TEXT,
    resultado_financeiro REAL,
    tempo_operacao_min REAL,
    lead_time_min REAL,
    score_entrada REAL,
    probabilidade REAL,
    confianca REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hora TEXT NOT NULL,
    preco REAL, score REAL, direcao TEXT, acao TEXT, motivo TEXT, atr REAL,
    nivel_evidencia INTEGER, confianca REAL,
    r_hipotetico REAL, resolvido INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prices (
    hora TEXT PRIMARY KEY,
    close REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_reports (
    data TEXT PRIMARY KEY,
    hora TEXT NOT NULL,
    texto TEXT NOT NULL,
    dados TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hora TEXT NOT NULL,
    capital REAL NOT NULL,
    pnl REAL,
    nota TEXT
);
CREATE TABLE IF NOT EXISTS reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id TEXT, ativo TEXT, tipo TEXT, publicado TEXT, direcao_esperada REAL,
    t_primeira REAL, t_confirmacao REAL, t_pleno REAL, mfe REAL, mae REAL, direcao_ok INTEGER,
    lead_usd REAL, lead_yield REAL, horizonte INTEGER, resolucao INTEGER, conhecido_em TEXT,
    UNIQUE(evento_id, ativo)
);
CREATE TABLE IF NOT EXISTS trade_monitor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    hora TEXT NOT NULL,
    preco REAL, r_atual REAL,
    trade_score REAL, thesis_score REAL, exit_score REAL, profit_potential REAL,
    acao TEXT, nota TEXT
);
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
        self._migrate()

    def _migrate(self) -> None:
        """4.0: coluna `ativo` (símbolo) nas tabelas por mercado; bancos antigos = XAUUSD."""
        for table in ("predictions", "trades", "decisions"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "ativo" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN ativo TEXT DEFAULT 'XAUUSD'")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "etapa" not in cols:
            self.conn.execute("ALTER TABLE decisions ADD COLUMN etapa TEXT")
        if "bruta" not in cols:
            self.conn.execute("ALTER TABLE decisions ADD COLUMN bruta INTEGER DEFAULT 0")
        self.conn.commit()

    @staticmethod
    def _where_symbol(symbol: Optional[str], prefix: str = "WHERE") -> tuple[str, tuple]:
        return (f" {prefix} ativo=?", (symbol,)) if symbol else ("", ())

    # ------------------------------------------------------------------ registro
    def record(self, a: Assessment, signal_type: Optional[str] = None, atr: Optional[float] = None, horizon_min: int = 240,
               symbol: str = "XAUUSD") -> int:

        t = a.time.astimezone(timezone.utc)
        direction = a.direction.value
        prob = {"ALTA": a.prob_up, "BAIXA": a.prob_down, "LATERAL": a.prob_flat}[direction]
        fund = {f.name: f.score for f in a.factors}
        cur = self.conn.execute(
            """INSERT INTO predictions (data, hora, sessao, preco, previsao, probabilidade, confianca, score,
               horizonte, estagio, fundamentos, noticias, dolar, juros, fluxo, tecnico, evento, sinal_tipo, nivel_evidencia,
               fatores_ratio, tecnico_detalhe, atr, horizonte_min, ativo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), session_label(t), a.price, direction, prob,
                a.confidence, a.score, a.horizon, a.premove.stage.value, json.dumps(fund, ensure_ascii=False),
                json.dumps([], ensure_ascii=False), fund.get("dolar"), fund.get("juros_reais"), fund.get("fluxo"),
                fund.get("tecnico"), a.next_event.name if a.next_event else None, signal_type, int(a.evidence_level),
                json.dumps({f.name: round(f.ratio, 3) for f in a.factors if f.available}), json.dumps(technical_details(a)),
                atr, horizon_min, symbol,
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
        elif by in ("sessao", "previsao", "horizonte", "estagio", "evento", "sinal_tipo", "nivel_evidencia"):
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

    # ------------------------------------------------------------------ 2.1: resolução automática no loop live
    def auto_resolve(self, candles: Iterable, now: datetime, default_threshold: float, horizon_min: int = 240) -> list[tuple[int, Outcome]]:
        """Resolve previsões pendentes usando os candles mais recentes (M1/M5): ACERTO/ERRO quando o preço
        tocar ±limiar (1 ATR da previsão, ou `default_threshold`) dentro do horizonte; LATERAL ao expirar."""
        cs = sorted(candles, key=lambda c: c.time)
        done: list[tuple[int, Outcome]] = []
        for row in self.pending():
            start = datetime.fromisoformat(f"{row['data']}T{row['hora']}").replace(tzinfo=timezone.utc)
            horizon = row["horizonte_min"] or horizon_min
            path = [(c.time, c.close) for c in cs if start < c.time <= start + timedelta(minutes=horizon)]
            if not path:
                continue
            thr = row["atr"] or default_threshold
            out = self.evaluate_path(row["previsao"], row["preco"], path, start, thr)
            expired = now >= start + timedelta(minutes=horizon)
            if out.result in ("ACERTO", "ERRO") or expired:
                self.conn.execute(
                    """UPDATE predictions SET resultado=?, tempo_ate_reacao_min=?, maxima_favoravel=?, maxima_adversa=?,
                       preco_final=?, resolvido_em=? WHERE id=?""",
                    (out.result, out.time_to_reaction_min, out.mfe, out.mae, out.final_price, now.isoformat(), row["id"]))
                done.append((row["id"], out))
        if done:
            self.conn.commit()
        return done

    # ------------------------------------------------------------------ 2.2: operações simuladas
    def open_trade(self, plan, mode: str, prediction_id: Optional[int] = None, horizon_min: int = 240, symbol: str = "XAUUSD") -> int:
        cur = self.conn.execute(
            """INSERT INTO trades (prediction_id, aberta_em, modo, sinal_tipo, direcao, entrada, stop, atr, lote, risco_usd, alvos,
               estrategia, horizonte_min, ativo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, plan.time.astimezone(timezone.utc).isoformat(), mode, plan.signal_type, plan.direction.value, plan.entry,
             plan.stop, plan.atr, plan.lots, plan.risk_usd, json.dumps(plan.targets), plan.recommended, horizon_min, symbol))
        self.conn.commit()
        return int(cur.lastrowid)

    def open_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades WHERE status IN ('OPEN','MANAGED_CLOSED') ORDER BY id").fetchall()

    # ------------------------------------------------------------------ 3.0: execução, capital, resultado
    def save_execution(self, trade_id: int, report, capital: float, risk_pct: float, assessment=None) -> None:
        self.conn.execute(
            """UPDATE trades SET ticket=?, preco_execucao=?, sl_real=?, tp_real=?, slippage=?, execucao=?, capital=?, risco_pct=?,
               score_entrada=?, probabilidade=?, confianca=? WHERE id=?""",
            (report.ticket if report else None, report.fill_price if report else None, report.real_sl if report else None,
             report.real_tp if report else None, report.slippage if report else None, report.render() if report else None, capital, risk_pct,
             assessment.score if assessment else None, max(assessment.prob_up, assessment.prob_down) if assessment else None,
             assessment.confidence if assessment else None, trade_id))
        self.conn.commit()

    def save_financial_result(self, trade_id: int, pnl_usd: float, minutes: Optional[float], lead_time_min: Optional[float] = None) -> None:
        self.conn.execute("UPDATE trades SET resultado_financeiro=?, tempo_operacao_min=?, lead_time_min=? WHERE id=?", (pnl_usd, minutes, lead_time_min, trade_id))
        self.conn.commit()

    def record_equity(self, t: datetime, equity: float, pnl: Optional[float] = None, note: str = "") -> None:
        self.conn.execute("INSERT INTO account (hora, capital, pnl, nota) VALUES (?,?,?,?)", (t.isoformat(), equity, pnl, note))
        self.conn.commit()

    def last_equity(self) -> Optional[float]:
        r = self.conn.execute("SELECT capital FROM account ORDER BY id DESC LIMIT 1").fetchone()
        return float(r["capital"]) if r else None

    def equity_curve(self) -> list[tuple[datetime, float]]:
        return [(datetime.fromisoformat(r["hora"]), r["capital"]) for r in self.conn.execute("SELECT hora, capital FROM account ORDER BY id").fetchall()]

    # ------------------------------------------------------------------ 4.0: LIVE EDGE diário
    def save_edge_report(self, report) -> None:
        self.conn.execute("INSERT OR REPLACE INTO edge_reports (data, hora, texto, dados) VALUES (?,?,?,?)",
                          (report.date, datetime.now(timezone.utc).isoformat(), report.render(), report.to_json()))
        self.conn.commit()

    def last_edge_date(self) -> Optional[str]:
        r = self.conn.execute("SELECT data FROM edge_reports ORDER BY data DESC LIMIT 1").fetchone()
        return r["data"] if r else None

    def edge_history(self) -> list[dict]:
        return [json.loads(r["dados"]) for r in self.conn.execute("SELECT dados FROM edge_reports ORDER BY data").fetchall()]

    def per_market_summary(self) -> list[dict]:
        """4.0: operações, expectancy e win rate por ativo (o que foi vivido)."""
        rows = self.conn.execute("SELECT ativo, COUNT(*) n, AVG(resultado_r) e, SUM(CASE WHEN resultado_r>0 THEN 1 ELSE 0 END) w, SUM(resultado_financeiro) p "
                                 "FROM trades WHERE resultado_r IS NOT NULL GROUP BY ativo ORDER BY e DESC").fetchall()
        return [{"symbol": r["ativo"], "n": r["n"], "expectancy": r["e"] or 0.0, "win_rate": (r["w"] / r["n"]) if r["n"] else 0.0, "pnl": r["p"] or 0.0} for r in rows]

    def performance_summary(self) -> str:
        rows = self.conn.execute("SELECT resultado_financeiro AS p, resultado_r AS r, modo FROM trades WHERE resultado_financeiro IS NOT NULL").fetchall()
        curve = self.equity_curve()
        if not rows and not curve:
            return "📈 PERFORMANCE — sem operações com resultado financeiro"
        pnl = sum(r["p"] for r in rows)
        wins = [r["p"] for r in rows if r["p"] > 0]
        lines = ["📈 PERFORMANCE ENGINE"]
        if curve:
            lines.append(f"  capital inicial {curve[0][1]:,.2f} → atual {curve[-1][1]:,.2f} USD ({(curve[-1][1] / curve[0][1] - 1) * 100:+.2f}%)")
        if rows:
            lines.append(f"  operações {len(rows)} · resultado {pnl:+,.2f} USD · win rate {len(wins) / len(rows):.0%} · média {pnl / len(rows):+,.2f} USD")
            by_mode = {}
            for r in rows:
                by_mode.setdefault(r["modo"], []).append(r["p"])
            for m, v in by_mode.items():
                lines.append(f"    {m}: n={len(v)} {sum(v):+,.2f} USD")
            for m in self.per_market_summary():
                lines.append(f"    {m['symbol']}: n={m['n']} E={m['expectancy']:+.2f}R win {m['win_rate']:.0%} {m['pnl']:+,.2f} USD")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 2.3: trade monitor
    def save_thesis(self, trade_id: int, thesis, state: dict) -> None:
        self.conn.execute("UPDATE trades SET tese=?, estado=? WHERE id=?", (json.dumps(thesis.to_dict()), json.dumps(state), trade_id))
        self.conn.commit()

    def save_state(self, trade_id: int, state: dict) -> None:
        self.conn.execute("UPDATE trades SET estado=? WHERE id=?", (json.dumps(state), trade_id))
        self.conn.commit()

    def log_monitor(self, trade_id: int, reading) -> None:
        self.conn.execute(
            "INSERT INTO trade_monitor (trade_id, hora, preco, r_atual, trade_score, thesis_score, exit_score, profit_potential, acao, nota) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trade_id, reading.time.isoformat(), reading.price, reading.current_r, reading.trade_score, reading.thesis_score,
             reading.exit_score, reading.profit_potential, reading.action, reading.note))
        self.conn.commit()

    def close_managed(self, trade_id: int, result_r: float, reason: str, t: datetime, state: dict) -> None:
        """Fechada pelo monitor; continua sendo acompanhada até o horizonte para medir o que ficou na mesa."""
        self.conn.execute("UPDATE trades SET status='MANAGED_CLOSED', resultado_r=?, motivo_saida=?, gerenciada_em=?, estado=? WHERE id=?",
                          (result_r, reason, t.isoformat(), json.dumps(state), trade_id))
        self.conn.commit()

    def managed_trades(self, symbol: Optional[str] = None) -> list:
        """Reconstrói as operações abertas gerenciadas pelo monitor (ManagedTrade)."""

        out = []
        w, args = self._where_symbol(symbol, "AND")
        for r in self.conn.execute(f"SELECT * FROM trades WHERE status='OPEN' AND tese IS NOT NULL{w} ORDER BY id", args).fetchall():
            plan = TradePlan(Direction(r["direcao"]), r["entrada"], r["stop"], r["atr"] or 0.0, datetime.fromisoformat(r["aberta_em"]),
                             targets=json.loads(r["alvos"] or "{}"), recommended=r["estrategia"] or "3R", signal_type=r["sinal_tipo"] or "", lots=r["lote"], risk_usd=r["risco_usd"])
            tr = ManagedTrade(r["id"], plan, Thesis.from_dict(json.loads(r["tese"]))).load_state(json.loads(r["estado"] or "{}"))
            last = self.conn.execute("SELECT hora FROM trade_monitor WHERE trade_id=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            if last:
                tr.history.append(MonitorReading(datetime.fromisoformat(last["hora"]), plan.entry, 0.0, 0.0, 0.0, 0.0, 0.0, "MANTER"))
            out.append(tr)
        return out

    def monitor_history(self, trade_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trade_monitor WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()

    def exit_learning_rows(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades WHERE motivo_saida IS NOT NULL").fetchall()
        out = []
        for r in rows:
            hist = self.monitor_history(r["id"])
            thesis0 = json.loads(r["tese"])["score"] if r["tese"] else None
            last = hist[-1] if hist else None
            out.append({"exit_reason": r["motivo_saida"], "result_r": r["resultado_r"] or 0.0,
                        "max_r_after": r["max_r"] if r["status"] == "CLOSED" else None,
                        "min_r_after": (-(r["mae_r"] or 0.0)) if r["status"] == "CLOSED" else None,
                        "thesis_at_exit": last["thesis_score"] if last else None,
                        "drop_at_exit": (thesis0 - last["trade_score"]) if (last and thesis0 is not None) else None})
        return out

    def exit_learning(self) -> str:
        return exit_learning(self.exit_learning_rows())

    def auto_resolve_trades(self, candles: Iterable, now: datetime) -> list[tuple[int, dict]]:
        """Simula cada operação aberta com os candles reais (todas as estratégias). Fecha quando o stop
        inicial é tocado, quando todas as estratégias saíram, ou ao expirar o horizonte."""

        cs = sorted(candles, key=lambda c: c.time)
        done: list[tuple[int, dict]] = []
        for row in self.open_trades():
            t0 = datetime.fromisoformat(row["aberta_em"])
            horizon = row["horizonte_min"] or 240
            if not any(c.time > t0 for c in cs):
                continue
            plan = TradePlan(Direction(row["direcao"]), row["entrada"], row["stop"], row["atr"] or 0.0, t0)
            sim = simulate_all(plan, cs, horizon)
            prof = sim["profile"]
            expired = now >= t0 + timedelta(minutes=horizon) or prof.horizon_reached
            all_closed = all(r.exit_reason != "OPEN" for r in sim["details"].values())
            if prof.stopped or expired or all_closed:
                if row["status"] == "MANAGED_CLOSED" and not (prof.stopped or expired):
                    continue  # segue acompanhando até o stop inicial ou o horizonte
                self.conn.execute(
                    """UPDATE trades SET status='CLOSED', max_r=?, mae_r=?, hit_1r=?, hit_2r=?, hit_3r=?, hit_4r=?, estopada=?, resultados=?, fechada_em=? WHERE id=?""",
                    (prof.max_r_before_stop, prof.mae_r, int(prof.hit(1)), int(prof.hit(2)), int(prof.hit(3)), int(prof.hit(4)),
                     int(prof.stopped), json.dumps(sim["results"]), now.isoformat(), row["id"]))
                done.append((row["id"], sim))
        if done:
            self.conn.commit()
        return done

    def r_stats(self, symbol: Optional[str] = None):

        w, args = self._where_symbol(symbol, "AND")
        rows = self.conn.execute(f"SELECT * FROM trades WHERE status='CLOSED'{w}", args).fetchall()
        recs = []
        for r in rows:
            results = json.loads(r["resultados"] or "{}")
            if r["resultado_r"] is not None:
                results["adaptive"] = r["resultado_r"]
            recs.append({"type": r["sinal_tipo"] or "?", "results": results,
                         "profile": ExcursionProfile(r["max_r"] or 0.0, r["mae_r"] or 0.0, bool(r["estopada"]), False, 0)})
        return r_stats(recs)

    # ------------------------------------------------------------------ 4.0: REACTION ENGINE
    def save_reaction(self, rec) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reactions (evento_id, ativo, tipo, publicado, direcao_esperada, t_primeira, t_confirmacao, t_pleno, mfe, mae, direcao_ok, "
            "lead_usd, lead_yield, horizonte, resolucao, conhecido_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.event_id, rec.target, rec.kind, rec.published_at.isoformat(), rec.expected_dir, rec.time_to_first, rec.time_to_confirmation, rec.time_to_full_move,
             rec.max_move_atr, rec.max_adverse_atr, None if rec.direction_correct is None else int(rec.direction_correct), rec.lead_times.get("USD"),
             rec.lead_times.get("YIELD"), rec.horizon_min, rec.resolution_min, rec.known_at.isoformat()))
        self.conn.commit()

    def reaction_records(self, symbol: Optional[str] = None) -> list:
        where, params = self._where_symbol(symbol)
        out = []
        for r in self.conn.execute(f"SELECT * FROM reactions {where} ORDER BY publicado", params).fetchall():
            rec = ReactionRecord(r["evento_id"], r["tipo"], datetime.fromisoformat(r["publicado"]), r["ativo"], r["direcao_esperada"], r["t_primeira"],
                                 r["t_confirmacao"], r["t_pleno"], r["mfe"] or 0.0, r["mae"] or 0.0, None if r["direcao_ok"] is None else bool(r["direcao_ok"]),
                                 {"USD": r["lead_usd"], "YIELD": r["lead_yield"]}, r["horizonte"] or 240, r["resolucao"] or 5)
            out.append(rec)
        return out

    def has_reaction(self, event_id: str, symbol: str) -> bool:
        return self.conn.execute("SELECT 1 FROM reactions WHERE evento_id=? AND ativo=?", (event_id, symbol)).fetchone() is not None

    # ------------------------------------------------------------------ 3.0: OPPORTUNITY ENGINE
    def record_decision(self, rec, symbol: str = "XAUUSD", stage: Optional[str] = None, is_raw: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions (hora, preco, score, direcao, acao, motivo, atr, nivel_evidencia, confianca, ativo, etapa, bruta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.time.isoformat(), rec.price, rec.score, rec.direction, rec.action, rec.reason[:300], rec.atr, rec.evidence_level, rec.confidence, symbol,
             stage, int(is_raw)))
        self.conn.commit()
        return int(cur.lastrowid)

    def store_prices(self, candles: Iterable) -> int:
        rows = [(c.time.astimezone(timezone.utc).isoformat(), c.close) for c in candles]
        if not rows:
            return 0
        self.conn.executemany("INSERT OR IGNORE INTO prices (hora, close) VALUES (?,?)", rows)
        self.conn.commit()
        return len(rows)

    def prices(self, since: Optional[datetime] = None) -> list[tuple[datetime, float]]:
        q = "SELECT hora, close FROM prices" + (" WHERE hora >= ?" if since else "") + " ORDER BY hora"
        rows = self.conn.execute(q, (since.isoformat(),) if since else ()).fetchall()
        return [(datetime.fromisoformat(r["hora"]), r["close"]) for r in rows]

    def resolve_hypotheticals(self, now: datetime, horizon_min: int = 240) -> int:
        """Preenche o resultado hipotético (3R, stop 1.2 ATR) das decisões bloqueadas com os preços gravados."""

        rows = self.conn.execute("SELECT * FROM decisions WHERE resolvido=0 AND acao NOT IN ('ENTRADA','SEM_SINAL') AND direcao IN ('ALTA','BAIXA')").fetchall()
        if not rows:
            return 0
        prices = self.prices()
        candles = [Candle(t, p, p, p, p, 0.0) for t, p in prices]
        n = 0
        for r in rows:
            t0 = datetime.fromisoformat(r["hora"])
            rec = DecisionRecord(t0, r["preco"], r["score"], r["direcao"], r["acao"], r["motivo"] or "", r["atr"] or 0.0)
            expired = now >= t0 + timedelta(minutes=horizon_min)
            res = hypothetical_trade(rec, candles, horizon_min)
            if res is not None or expired:
                self.conn.execute("UPDATE decisions SET r_hipotetico=?, resolvido=1 WHERE id=?", (res, r["id"]))
                n += 1
        self.conn.commit()
        return n

    def decisions(self, since: Optional[datetime] = None, symbol: Optional[str] = None) -> list:

        conds, args = [], []
        if since:
            conds.append("hora >= ?"); args.append(since.isoformat())
        if symbol:
            conds.append("ativo = ?"); args.append(symbol)
        q = "SELECT * FROM decisions" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY id"
        rows = self.conn.execute(q, tuple(args)).fetchall()
        return [DecisionRecord(datetime.fromisoformat(r["hora"]), r["preco"], r["score"] or 0.0, r["direcao"] or "LATERAL", r["acao"], r["motivo"] or "",
                               r["atr"] or 0.0, r["r_hipotetico"], r["nivel_evidencia"] or 0, r["confianca"] or 0.0) for r in rows]

    def funnel(self, symbol: Optional[str] = None, since: Optional[datetime] = None):
        """FUNIL DE ENTRADA do que foi vivido (uma linha por análise gravada pelo live)."""

        conds, args = [], []
        if since:
            conds.append("hora >= ?"); args.append(since.isoformat())
        if symbol:
            conds.append("ativo = ?"); args.append(symbol)
        q = "SELECT bruta, etapa, acao FROM decisions" + (" WHERE " + " AND ".join(conds) if conds else "")
        f = Funnel()
        for r in self.conn.execute(q, tuple(args)).fetchall():
            is_raw = bool(r["bruta"]) or r["acao"] == "ENTRADA"
            stage = None if r["acao"] == "ENTRADA" else (r["etapa"] or ("OUTROS" if is_raw else None))
            f.add(is_raw, stage)
        return f

    def opportunity_report(self, horizon_min: int = 240, since: Optional[datetime] = None, symbol: Optional[str] = None):

        decisions = self.decisions(since, symbol)
        prices = self.prices(since)
        w, args = self._where_symbol(symbol)
        entries = [(datetime.fromisoformat(r["aberta_em"]), r["direcao"]) for r in self.conn.execute(f"SELECT aberta_em, direcao FROM trades{w}", args).fetchall()]
        w2, args2 = self._where_symbol(symbol, "AND")
        atrs = [r["atr"] for r in self.conn.execute(f"SELECT atr FROM trades WHERE atr IS NOT NULL{w2}", args2).fetchall()] or [d.atr for d in decisions if d.atr]
        threshold = (sum(atrs) / len(atrs)) if atrs else 9.0
        trade_rows = [{"score": r["score_entrada"] or 0.0, "r": r["resultado_r"]} for r in
                      self.conn.execute(f"SELECT score_entrada, resultado_r FROM trades WHERE resultado_r IS NOT NULL{w2}", args2).fetchall()]
        # decisões bloqueadas com resultado hipotético também alimentam a curva de limiar
        trade_rows += [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
        return opportunity_report(decisions, prices, entries, threshold, horizon_min, trade_rows)

    def resolved_records(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM predictions WHERE resultado IN ('ACERTO','ERRO') AND previsao IN ('ALTA','BAIXA')").fetchall()
        return [{"direction": r["previsao"], "hit": r["resultado"] == "ACERTO", "probability": r["probabilidade"],
                 "factors": json.loads(r["fatores_ratio"] or "{}"), "technical": json.loads(r["tecnico_detalhe"] or "{}"),
                 "lead": r["tempo_ate_reacao_min"], "type": r["sinal_tipo"]} for r in rows]

    def calibration(self):
        return calibration_table((r["probability"], r["hit"]) for r in self.resolved_records())

    def scoreboard(self):
        return factor_scoreboard(self.resolved_records())

    def fit_calibrator(self):
        return IsotonicCalibrator().fit((r["probability"], r["hit"]) for r in self.resolved_records())

    def lead_time_stats(self) -> dict:
        """⏱️ lead time das previsões que acertaram, por tipo de sinal."""
        rows = self.conn.execute("SELECT sinal_tipo, tempo_ate_reacao_min FROM predictions WHERE resultado='ACERTO' AND tempo_ate_reacao_min IS NOT NULL").fetchall()
        by: dict[str, list[float]] = {}
        for r in rows:
            by.setdefault(r["sinal_tipo"] or "?", []).append(r["tempo_ate_reacao_min"])
        allv = [v for vs in by.values() for v in vs]
        return {"media": (sum(allv) / len(allv)) if allv else None, "n": len(allv),
                "por_tipo": {k: sum(v) / len(v) for k, v in by.items()}}

    def metrics(self, path: Iterable[tuple[datetime, float]], threshold: float, horizon_min: int = 240):
        """Precisão, recall, MFE/MAE, lead time e GOLD LEAD SCORE das previsões direcionais registradas,
        confrontadas com o caminho real do preço (evaluation.evaluate)."""

        rows = self.conn.execute("SELECT * FROM predictions WHERE previsao IN ('ALTA','BAIXA') ORDER BY id").fetchall()
        sigs = [SignalRecord(datetime.fromisoformat(f"{r['data']}T{r['hora']}").replace(tzinfo=timezone.utc), r["previsao"],
                             r["sinal_tipo"] or "", r["preco"], threshold, r["nivel_evidencia"] or 0, r["probabilidade"], r["confianca"]) for r in rows]
        return evaluate(sigs, list(path), threshold, horizon_min)

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

    if sig.type == SignalType.WATCH:
        lines = ["⚠️ GOLD WATCH", "XAU/USD", price, "",
                 f"Possível movimento de {'ALTA' if d == Direction.ALTA else 'BAIXA'}.", "",
                 f"Probabilidade: {_pct(prob)}", f"Confiança: {a.confidence:.0f}/100", f"Evidência: {a.evidence_level.label}",
                 f"Horizonte: {a.horizon}", "", "Fatores em observação:", *[f"• {r}" for r in sig.reasons[:4]], "",
                 "Ainda não há operação. Aguardando confirmação."]
        return "\n".join(lines)

    if sig.type == SignalType.PRE_MOVE:
        emoji = "🟢 ALTA" if d == Direction.ALTA else "🔴 BAIXA"
        lines = [f"⚠️ GOLD PRE-MOVE — POSSÍVEL {'ALTA' if d == Direction.ALTA else 'BAIXA'}", "XAU/USD", price, "",
                 "A IA detectou mudança em:", *[f"• {r}" for r in sig.reasons], "",
                 "mas o preço ainda não confirmou.", "",
                 f"Probabilidade de movimento: {_pct(a.premove.probability)}", f"Direção: {emoji}",
                 f"Status: {a.premove.stage.value}", f"Confiança: {a.confidence:.0f}/100", f"Horizonte: {a.horizon}", "",
                 f"Evidência: {a.evidence_level.label}", "",
                 "Antecipação", *_layer_line(a, d), "", "Situação", f"{a.premove.latent_pressure or a.premove.stage.value}", "",
                 "Zona de atenção", *_zone(a), "", a.chain]
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
    confirmed = sig.trigger == "confirmação de movimento"
    head = ("🟢 GOLD SIGNAL" if buy else "🔴 GOLD SIGNAL") if confirmed else ("🚨 GOLD AI ALERT" if buy else "🔴 GOLD AI ALERT")
    bias = "🟢 COMPRA" if buy else "🔴 VENDA"
    if sig.type in (SignalType.STRONG_BUY, SignalType.STRONG_SELL):
        bias += " (FORTE)"
    lines = [head, "XAU/USD", price, "", bias, *(["PRE-MOVE CONFIRMADO"] if confirmed else []),
             f"Score: {a.score:+.0f}", f"Probabilidade: {_pct(prob)}",
             f"Confiança: {a.confidence:.0f}/100", f"Evidência: {a.evidence_level.label}", f"Horizonte: {a.horizon}", "",
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


ENV_CANDIDATES = (".env", ".env.txt", "env", "env.txt")


def env_file_candidates(path: str = ".env") -> list[str]:
    """Onde o .env é procurado: pasta atual e pasta do script, com os nomes que o Windows costuma dar ao arquivo."""
    if path != ".env":
        return [path]
    dirs = [os.getcwd()]
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else ""
    if script_dir and script_dir != dirs[0]:
        dirs.append(script_dir)
    return [os.path.join(d, name) for d in dirs for name in ENV_CANDIDATES]


def find_env_file(path: str = ".env") -> Optional[str]:
    for cand in env_file_candidates(path):
        if os.path.isfile(cand):
            return cand
    return None


def load_env_file(path: str = ".env") -> dict[str, str]:
    """Lê um .env simples (CHAVE=valor, aspas opcionais). Nunca versionar esse arquivo.
    Aceita .env / .env.txt / env / env.txt na pasta atual ou na pasta do script."""
    out: dict[str, str] = {}
    found = find_env_file(path)
    if not found:
        return out
    with open(found, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class TelegramSender:
    """Envio via Bot API (stdlib). Credenciais por argumento, variável de ambiente
    (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, ou TOKEN_TELEGRAM / CHAT_ID) ou arquivo .env.
    Sem token/chat_id, apenas imprime (modo dry-run)."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, dry_run: bool = False, env_file: str = ".env",
                 quiet: bool = False) -> None:
        env = {**load_env_file(env_file), **os.environ}
        self.token = token or env.get("TELEGRAM_BOT_TOKEN") or env.get("TOKEN_TELEGRAM")
        self.chat_id = str(chat_id or env.get("TELEGRAM_CHAT_ID") or env.get("CHAT_ID") or "") or None
        self.dry_run = dry_run or not (self.token and self.chat_id)
        self.quiet = quiet
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        if self.dry_run:
            self.sent.append(text)
            if not self.quiet:
                print("\n[TELEGRAM dry-run]\n" + text + "\n")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))


def format_decision(decision) -> str:
    """Mensagem do TRADE SIMULATOR / gestor de posição (2.2)."""
    return "🧾 GOLD AI TRADE\n" + decision.render()


def format_monitor(tr, reading) -> str:
    return "📡 " + render_monitor(tr, reading)


# --------------------------------------------------------------------------- 3.0: TELEGRAM TRADE MANAGER
def format_entry(plan, assessment, mode: str, execution=None, symbol: str = "XAUUSD") -> str:
    side = f"🟢 BUY {symbol}" if plan.direction.value == "ALTA" else f"🔴 SELL {symbol}"
    tp = plan.targets.get(plan.recommended) or plan.targets.get("3R")
    rr = plan.recommended[0] if plan.recommended[:1].isdigit() else "3"
    lines = ["🚨 MARKET AI", "", side, "", f"Score: {assessment.score:+.0f}", f"Probabilidade: {max(assessment.prob_up, assessment.prob_down):.0%}",
             f"Confiança: {assessment.confidence:.0f}", "", f"Entrada: {plan.entry:.2f}", f"Stop: {plan.stop:.2f}", f"TP: {tp:.2f}" if tp else "TP: trailing",
             "", f"R:R = 1:{rr}", "", f"Lote: {plan.lots:.2f}" if plan.lots else "Lote: n/d", f"Risco: {plan.risk_usd:.2f} USD" if plan.risk_usd else "",
             "", "PRE-MOVE CONFIRMADO" if "CONFIRM" in plan.signal_type.upper() or "BUY" in plan.signal_type or "SELL" in plan.signal_type else plan.signal_type,
             f"Modo: {mode}"]
    if execution is not None:
        lines += ["", execution.render()]
    return "\n".join(x for x in lines if x is not None)


def format_protection(tr, reading) -> str:
    return "\n".join(["🛡️ GOLD AI", "", f"+{reading.current_r:.1f}R atingido", "", f"{1 - tr.remaining:.0%} realizado", "",
                       f"Stop: {'BREAK EVEN' if abs(tr.stop_r) < 1e-9 else f'{tr.stop_r:+.2f}R'}", "", f"{tr.remaining:.0%} restante:", "TRAILING", "", reading.note])


def format_scenario_change(tr, reading) -> str:
    first = tr.history[0] if len(tr.history) > 1 else SimpleNamespace(thesis_score=100.0, exit_score=0.0)
    return "\n".join(["⚠️ GOLD AI", "", "CENÁRIO ALTERADO", "", f"Score:\n{tr.thesis.score:+.0f} → {reading.trade_score:+.0f}", "",
                       f"Thesis:\n{first.thesis_score:.0f} → {reading.thesis_score:.0f}", "", f"Exit:\n{first.exit_score:.0f} → {reading.exit_score:.0f}", "",
                       "AÇÃO:", "🔴 ENCERRAR" if reading.action == "ENCERRAR" else f"🟠 {reading.action}", "", f"Motivo:\n{tr.close_reason or reading.note}"])


def format_result(tr, pnl_usd=None, prediction_correct=None, lead_time_min=None, minutes=None) -> str:
    lines = ["🏆 GOLD AI" if (tr.result_r or 0) > 0 else "📉 GOLD AI", "", "TRADE ENCERRADO", "", f"Resultado:\n{tr.result_r:+.2f}R"]
    if pnl_usd is not None:
        lines.append(f"{pnl_usd:+,.2f} USD")
    reason = {"TESE INVALIDADA": "Saída adaptativa (tese invalidada)", "EXIT SCORE": "Saída adaptativa", "STOP": "Stop", "TRAILING/PROTEÇÃO": "Trailing / proteção",
              "HORIZON": "Horizonte", "FIM": "Fim do período", "BROKER": "Fechada no broker", "MANUAL": "Encerramento manual (/CLOSE)"}.get(tr.close_reason, tr.close_reason)
    lines += ["", f"Motivo:\n{reason}"]
    if prediction_correct is not None:
        lines += ["", f"Previsão:\n{'CORRETA' if prediction_correct else 'INCORRETA'}"]
    if lead_time_min is not None:
        lines += ["", f"Lead time:\n{lead_time_min:.0f} minutos"]
    if minutes is not None:
        lines += ["", f"Duração:\n{minutes:.0f} minutos"]
    return "\n".join(lines)


def format_status(perf, ks, managed, mode: str) -> str:
    allowed, why = ks.new_entries_allowed()
    lines = ["📋 GOLD AI STATUS", f"Modo: {mode}", f"Novas entradas: {'✅' if allowed else '⛔ ' + why}", perf.render(), f"Posições sob monitor: {len(managed)}"]
    for tr in managed:
        last = tr.history[-1] if tr.history else None
        lines.append(f"  #{tr.trade_id:05d} {tr.thesis.direction.value} entrada {tr.plan.entry:.2f} stop {tr.price_at_r(tr.stop_r):.2f} "
                     + (f"{last.current_r:+.2f}R {last.action}" if last else ""))
    return "\n".join(lines)


# ============================================================================
# ENGINE
# ============================================================================

"""GoldAIEngine — ciclo de análise completo (Diretriz §2, §19, §20, §21, §36)."""





class GoldAIEngine:
    def __init__(self, cfg: Optional[EngineConfig] = None, calibrator: Optional[Callable[[float], float]] = None) -> None:
        self.cfg = cfg or EngineConfig()
        self.gate = SignalGate(self.cfg)
        self.history: list[Assessment] = []
        self.calibrator = calibrator  # mapa prob. prevista → observada (validation.IsotonicCalibrator)
        self.expected_lead_min: Optional[float] = None  # lead time histórico (métricas), exibido no painel

    # ------------------------------------------------------------------ score
    def score_factors(self, s: MarketSnapshot) -> tuple[list[FactorScore], list[TechnicalReading]]:
        factors: list[FactorScore] = []
        for name, weight in self.cfg.weights.items():
            if name == "tecnico":
                continue
            factors.append(SCORERS[name](s, weight))
        tech, readings = score_tecnico(s, self.cfg.weights["tecnico"])
        factors.append(tech)
        if self.cfg.factor_signs:
            for f in factors:
                sign = self.cfg.factor_signs.get(f.name, 1)
                if sign == 0:
                    f.score, f.available, f.rationale = 0.0, False, f"sem relação conhecida com {self.cfg.symbol}"
                elif sign < 0:
                    f.score = -f.score
                    f.rationale = f"(sinal invertido para {self.cfg.symbol}) " + f.rationale
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

    # ------------------------------------------------------------------ regime
    @staticmethod
    def regime_for(readings: list[TechnicalReading]) -> str:
        """BULLISH / BEARISH / RANGE / VOLATILE a partir de H4+D1 (tendência × ADX)."""
        swing = [r for r in readings if r.timeframe in ("H4", "D1") and "dados insuficientes" not in r.notes]
        if not swing:
            return "INDEFINIDO"
        score = sum(r.score for r in swing) / len(swing)
        adxs = [r.adx for r in swing if r.adx is not None]
        adx = sum(adxs) / len(adxs) if adxs else 20.0
        if adx < 18 and abs(score) < 0.35:
            return "RANGE"
        if score > 0.25:
            return "BULLISH" if adx >= 18 else "BULLISH (fraco)"
        if score < -0.25:
            return "BEARISH" if adx >= 18 else "BEARISH (fraco)"
        return "VOLATILE" if adx >= 30 else "RANGE"

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
        if self.calibrator is not None and (p_up + p_down) > 0:
            # calibra a probabilidade da direção dominante e redistribui o restante
            dom_up = p_up >= p_down
            p_dom = self.calibrator(max(p_up, p_down) / (p_up + p_down)) * (p_up + p_down)
            p_dom = max(0.0, min(p_up + p_down, p_dom))
            p_up, p_down = (round(p_dom, 3), round(p_up + p_down - p_dom, 3)) if dom_up else (round(p_up + p_down - p_dom, 3), round(p_dom, 3))
        conf = self.confidence(factors, score, s, event)
        premove = analyze_premove(s, factors, readings, accum, self.cfg)
        reversal = analyze_reversal(s, factors, readings, accum)

        d1 = next((r for r in readings if r.timeframe == "D1"), None)
        h4 = next((r for r in readings if r.timeframe == "H4"), None)
        trend_src = d1 or h4
        trend = Direction(trend_src.trend) if trend_src and trend_src.trend in Direction.__members__ else Direction.LATERAL
        regime = self.regime_for(readings)

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
            conclusion="", confirmations=[], zone=zone, regime=regime,
        )
        a.confirmations = confirmations(a, a.direction if a.direction != Direction.LATERAL else premove.direction, self.cfg)
        a.evidence_level = evidence_level(a, s)
        a.has_edge, a.edge_status = edge_status(a, self.cfg)
        a.chain = event_chain(a, s)
        a.conclusion = self._conclusion(a)
        self.history.append(a)
        return a

    def _conclusion(self, a: Assessment) -> str:
        cls = classify(a.score, self.cfg).value
        parts = [f"{cls} (score {a.score:+.0f}, {len(a.confirmations)} confirmações: {', '.join(a.confirmations) or 'nenhuma'}). {a.evidence_level.label}. {a.edge_status}."]
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
        f"Evidência: {a.evidence_level.label}",
        "",
        f"STATUS: {a.edge_status}",
        "",
        a.chain,
        "",
        f"Conclusão: {a.conclusion}",
    ]
    return "\n".join(lines)


def render_dashboard(a: Assessment, expected_lead_min: Optional[float] = None) -> str:
    """Painel GOLD MARKET PREDICTION SYSTEM (caixa de largura fixa)."""

    f = {x.name: x for x in a.factors}

    def lab(name: str) -> str:
        x = f.get(name)
        if x is None or not x.available:
            return "N/D"
        return "ALTISTA" if x.ratio >= 0.3 else "BAIXISTA" if x.ratio <= -0.3 else "NEUTRO"

    p_dom = max(a.prob_up, a.prob_down)
    decision = classify(a.score, EngineConfig()).value
    if a.premove.stage.value == "PRÉ-MOVIMENTO" and len(a.confirmations) >= 3:
        decision = "GOLD PRE-MOVE"
    if not a.has_edge:
        decision = "NÃO SEI — SEM SINAL"
    status = a.premove.latent_pressure or a.dominant_pressure
    status_emoji = "🟢" if "COMPRADORA" in status else "🔴" if "VENDEDORA" in status else "🟡"
    lead = f"{expected_lead_min:.0f} min (histórico)" if expected_lead_min else "n/d (sem histórico)"
    rows = [
        ("REGIME", a.regime), ("SCORE", f"{a.score:+.0f}"), ("PROBABILIDADE", f"{p_dom:.0%}"), ("CONFIANÇA", f"{a.confidence:.0f}"),
        None,
        ("PRE-MOVE", a.premove.direction.value if a.premove.stage.value == "PRÉ-MOVIMENTO" else a.premove.stage.value),
        ("LEAD TIME", lead), ("EVIDÊNCIA", f"NÍVEL {int(a.evidence_level)}"),
        None,
        ("DXY", lab("dolar")), ("REAL YIELD", lab("juros_reais")), ("FED", lab("fed")), ("FLOW", lab("fluxo")),
        ("COT", lab("cot")), ("TECHNICAL", lab("tecnico")), ("NEWS", lab("sentimento") if f.get("sentimento") and f["sentimento"].available else "UNKNOWN"), ("GEO", lab("geopolitica")),
        None,
        ("STATUS", f"{status_emoji} {status}"),
        None,
        ("DECISÃO", decision),
    ]
    width = 44
    out = ["┌" + "─" * width + "┐", "│" + "GOLD AI ENGINE".center(width) + "│", "├" + "─" * width + "┤"]
    for r in rows:
        if r is None:
            out.append("│" + " " * width + "│")
            continue
        k, v = r
        line = f" {k:<13}→ {v}"
        out.append("│" + line[:width].ljust(width) + "│")
    out.append("└" + "─" * width + "┘")
    return "\n".join(out)


# ============================================================================
# SAMPLE
# ============================================================================

"""Gerador de cenários sintéticos para demonstração/testes (sem dependências)."""




TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}


def make_candles(tf: str, n: int, start_price: float, drift: float, vol: float, end: datetime, seed: int = 7, volume_trend: float = 0.0) -> list[Candle]:
    """Série de candles com deriva (`drift` por candle, em USD) e volatilidade `vol` (USD)."""
    rnd = random.Random(seed + sum(ord(ch) * (i + 1) for i, ch in enumerate(tf)))  # determinístico entre processos
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
# DATA · HTTP
# ============================================================================

"""Cliente HTTP mínimo (stdlib) com retries, timeout e cache em disco."""




class DataError(RuntimeError):
    pass


class HttpClient:
    def __init__(self, cache_dir: Optional[str] = None, ttl: int = 60, timeout: int = 15, retries: int = 3,
                 user_agent: str = "Mozilla/5.0 (GoldAIEngine/2.0)") -> None:
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest() + ".cache")

    def _cache_get(self, url: str, ttl: int) -> Optional[str]:
        p = self._cache_path(url)
        if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
            with open(p, encoding="utf-8") as f:
                return f.read()
        return None

    def _cache_put(self, url: str, text: str) -> None:
        p = self._cache_path(url)
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)

    # ------------------------------------------------------------------ fetch
    def get_text(self, url: str, ttl: Optional[int] = None, headers: Optional[dict[str, str]] = None) -> str:
        ttl = self.ttl if ttl is None else ttl
        cached = self._cache_get(url, ttl)
        if cached is not None:
            return cached
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*", **(headers or {})})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    text = resp.read().decode("utf-8", errors="replace")
                self._cache_put(url, text)
                return text
            except urllib.error.HTTPError as e:  # pragma: no cover - rede
                if e.code == 429:   # limite de requisições: repetir em segundos só prolonga o bloqueio — quem decide a espera é o chamador
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    raise DataError(f"falha ao buscar {url}: HTTP Error 429: Too Many Requests" + (f" (Retry-After {retry_after}s)" if retry_after else "")) from e
                last = e
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
            except (urllib.error.URLError, TimeoutError, OSError) as e:  # pragma: no cover - rede
                last = e
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
        raise DataError(f"falha ao buscar {url}: {last}")

    def get_bytes(self, url: str, ttl: Optional[int] = None, allow_404: bool = False) -> bytes:
        """Download binário (ex.: ticks .bi5 do Dukascopy). 404 com allow_404 → b'' (hora sem dados). Cache em disco por URL."""
        ttl = self.ttl if ttl is None else ttl
        p = self._cache_path(url)
        if p:
            pb = p + ".bin"
            if os.path.exists(pb) and time.time() - os.path.getmtime(pb) < ttl:
                with open(pb, "rb") as f:
                    return f.read()
        last: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    data = resp.read()
                if p:
                    with open(p + ".bin", "wb") as f:
                        f.write(data)
                return data
            except urllib.error.HTTPError as e:  # pragma: no cover - rede
                if e.code == 404 and allow_404:
                    if p:
                        with open(p + ".bin", "wb") as f:
                            f.write(b"")
                    return b""
                if e.code == 429:
                    raise DataError(f"falha ao buscar {url}: HTTP Error 429: Too Many Requests") from e
                last = e
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
            except (urllib.error.URLError, TimeoutError, OSError) as e:  # pragma: no cover - rede
                last = e
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
        raise DataError(f"falha ao buscar {url}: {last}")

    def get_json(self, url: str, ttl: Optional[int] = None) -> Any:
        try:
            return json.loads(self.get_text(url, ttl))
        except json.JSONDecodeError as e:
            raise DataError(f"JSON inválido em {url}: {e}") from e


# ============================================================================
# DATA · YAHOO
# ============================================================================

"""Coletor Yahoo Finance (chart API v8) — candles multi-timeframe e variações.

Sem chave. Símbolos úteis: GC=F (ouro futuro), XAUUSD=X (spot), DX-Y.NYB (DXY),
^TNX (10Y %), ^FVX (5Y), ^TYX (30Y), 2YY=F (2Y), ZQ=F (Fed Funds futuro),
^VIX, ^GSPC, SI=F (prata), CL=F (petróleo), BTC-USD, CNH=X (USD/CNH).
"""




YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# timeframe interno → (intervalo Yahoo, range Yahoo)
TF_MAP: dict[str, tuple[str, str]] = {
    "M1": ("1m", "5d"),
    "M5": ("5m", "5d"),
    "M15": ("15m", "1mo"),
    "M30": ("30m", "1mo"),
    "H1": ("1h", "3mo"),
    "D1": ("1d", "2y"),
    "W1": ("1wk", "5y"),
}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}


def parse_chart(payload: dict) -> list[Candle]:
    """Converte a resposta da chart API em candles (ignora barras nulas)."""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as e:
        err = (payload or {}).get("chart", {}).get("error") if isinstance(payload, dict) else None
        raise DataError(f"resposta Yahoo inválida: {err or e}")
    ts = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("quote") or [{}])[0]
    out: list[Candle] = []
    for i, t in enumerate(ts):
        o, h, l, c = q.get("open", [None])[i], q.get("high", [None])[i], q.get("low", [None])[i], q.get("close", [None])[i]
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        out.append(Candle(datetime.fromtimestamp(t, tz=timezone.utc), float(o), float(h), float(l), float(c), float(v)))
    return out


def resample(candles: list[Candle], minutes: int) -> list[Candle]:
    """Agrega candles em blocos de `minutes` alinhados à época (ex.: H1 → H4, D1 → W1)."""
    out: list[Candle] = []
    bucket: list[Candle] = []
    key: Optional[int] = None
    for c in candles:
        k = int(c.time.timestamp() // (minutes * 60))
        if key is not None and k != key and bucket:
            out.append(_merge(bucket))
            bucket = []
        key = k
        bucket.append(c)
    if bucket:
        out.append(_merge(bucket))
    return out


def _merge(b: list[Candle]) -> Candle:
    return Candle(b[0].time, b[0].open, max(c.high for c in b), min(c.low for c in b), b[-1].close, sum(c.volume for c in b))


class YahooCollector:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def candles(self, symbol: str, tf: str, ttl: int = 60) -> list[Candle]:
        if tf == "H4":
            return resample(self.candles(symbol, "H1", ttl), 240)
        interval, rng = TF_MAP[tf]
        url = f"{YAHOO_BASE}{symbol.replace('=', '%3D').replace('^', '%5E')}?interval={interval}&range={rng}&includePrePost=false"
        return parse_chart(self.http.get_json(url, ttl))

    def candles_between(self, symbol: str, tf: str, start: datetime, end: datetime, ttl: int = 3600) -> list[Candle]:
        """Histórico entre datas (period1/period2). Yahoo limita 1h a ~730 dias e 1m a 7 dias; para períodos longos
        de H1 a API devolve em blocos — pedimos em janelas de 60 dias e concatenamos."""
        if tf == "H4":
            return resample(self.candles_between(symbol, "H1", start, end, ttl), 240)
        interval = TF_MAP.get(tf, ("1h", ""))[0]
        sym = symbol.replace("=", "%3D").replace("^", "%5E")
        out: list[Candle] = []
        step = timedelta(days=60 if interval in ("1h", "60m", "30m", "15m") else 365 * 5)
        cur = start
        while cur < end:
            nxt = min(end, cur + step)
            url = f"{YAHOO_BASE}{sym}?interval={interval}&period1={int(cur.timestamp())}&period2={int(nxt.timestamp())}&includePrePost=false"
            try:
                out += parse_chart(self.http.get_json(url, ttl))
            except DataError:
                pass
            cur = nxt
        seen, dedup = set(), []
        for c in sorted(out, key=lambda c: c.time):
            if c.time not in seen:
                seen.add(c.time); dedup.append(c)
        return dedup

    def all_timeframes(self, symbol: str, tfs: tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")) -> dict[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        for tf in tfs:
            cs = self.candles(symbol, tf, ttl=60 if TF_MINUTES[tf] <= 60 else 900)
            if cs:
                out[tf] = cs
        return out

    def last_price(self, symbol: str) -> Optional[float]:
        cs = self.candles(symbol, "M5")
        return cs[-1].close if cs else None

    @staticmethod
    def change_over(candles: list[Candle], window_minutes: int, pct: bool = True) -> Optional[float]:
        """Variação do fechamento na janela (em % ou absoluta) usando o timestamp dos candles."""
        if len(candles) < 2:
            return None
        last = candles[-1]
        cutoff = last.time - timedelta(minutes=window_minutes)
        ref = None
        for c in reversed(candles[:-1]):
            if c.time <= cutoff:
                ref = c
                break
        if ref is None:
            ref = candles[0]
        if pct:
            return (last.close / ref.close - 1.0) * 100.0 if ref.close else None
        return last.close - ref.close


# ============================================================================
# DATA · FRED
# ============================================================================

"""Coletor FRED (CSV público, sem chave): juros reais, breakeven, spreads, Fed Funds.

Séries: DFII10 (TIPS 10Y real, %), T10YIE (breakeven 10Y, %), DGS10, DGS2 (%),
BAMLH0A0HYM2 (HY OAS, %), DFF (Fed Funds efetiva, %). Frequência diária, com defasagem
de um dia útil — usar para nível/tendência; variações intraday vêm do Yahoo.
"""




FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


def parse_csv(text: str) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for ln in text.strip().splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 2 or parts[1].strip() in (".", ""):
            continue
        try:
            out.append((date.fromisoformat(parts[0].strip()), float(parts[1])))
        except ValueError:
            continue
    return out


class FredCollector:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def series(self, series_id: str, ttl: int = 3600) -> list[tuple[date, float]]:
        text = self.http.get_text(FRED_BASE + series_id, ttl)
        data = parse_csv(text)
        if not data:
            raise DataError(f"série FRED vazia: {series_id}")
        return data

    def last_and_change(self, series_id: str, lookback: int = 1) -> tuple[Optional[float], Optional[float]]:
        """(último valor, variação vs `lookback` observações atrás) na unidade da série."""
        data = self.series(series_id)
        if len(data) <= lookback:
            return (data[-1][1] if data else None), None
        return data[-1][1], data[-1][1] - data[-1 - lookback][1]


# ============================================================================
# DATA · CFTC
# ============================================================================

"""Coletor CFTC — Commitment of Traders (Disaggregated, Futures Only) via API pública Socrata.

Dataset 72hh-3qpy; ouro COMEX = código 088691. Campos usados:
m_money_positions_long_all / short_all (Managed Money), prod_merc_* + swap_* (Commercials).
"""




CFTC_URL = ("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?cftc_contract_market_code={code}"
       "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit={limit}")
GOLD_CODE = "088691"


@dataclass
class CotReading:
    report_date: date
    managed_money_net: float
    managed_money_net_change: float
    managed_money_percentile: float  # 0..100 sobre a janela histórica
    commercial_net: float
    commercial_net_change: float
    history_weeks: int


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_rows(rows: list[dict]) -> CotReading:
    if len(rows) < 2:
        raise DataError("COT: menos de duas semanas de dados")
    rows = sorted(rows, key=lambda r: r.get("report_date_as_yyyy_mm_dd", ""))
    mm = [_f(r, "m_money_positions_long_all") - _f(r, "m_money_positions_short_all") for r in rows]
    com = [(_f(r, "prod_merc_positions_long_all") + _f(r, "swap_positions_long_all"))
           - (_f(r, "prod_merc_positions_short_all") + _f(r, "swap_positions_short_all")) for r in rows]
    last = mm[-1]
    pct = 100.0 * sum(1 for x in mm if x <= last) / len(mm)
    d = date.fromisoformat(rows[-1]["report_date_as_yyyy_mm_dd"][:10])
    return CotReading(d, last, mm[-1] - mm[-2], round(pct, 1), com[-1], com[-1] - com[-2], len(rows))


class CftcCollector:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def gold(self, weeks: int = 156, code: str = GOLD_CODE) -> CotReading:
        rows = self.http.get_json(CFTC_URL.format(code=code, limit=weeks), ttl=6 * 3600)
        if not isinstance(rows, list):
            raise DataError("COT: resposta inesperada")
        return parse_rows(rows)


# ============================================================================
# DATA · NEWS
# ============================================================================

"""Notícias (RSS), interpretação em três níveis (§13) e calendário econômico.

NÍVEL 1 notícia → NÍVEL 2 interpretação → NÍVEL 3 impacto no ouro, com extração de
RESULTADO × CONSENSO quando a manchete traz números ("CPI 2.8% vs 3.0% expected").
O interpretador padrão é por regras; um LLM pode ser plugado via `NewsInterpreter`.
"""




DEFAULT_FEEDS: tuple[str, ...] = (
    "https://www.fxstreet.com/rss/news",
    "https://www.kitco.com/rss/category/news",
    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
)

# palavra-chave → (categoria, impacto no ouro -1..+1)
KEYWORDS: list[tuple[str, str, float]] = [
    # Fed / juros
    (r"\b(rate cut|cuts? rates?|dovish|easing|corte de juros)\b", "fed", +0.6),
    (r"\b(rate hike|hikes? rates?|hawkish|tightening|higher for longer|alta de juros)\b", "fed", -0.6),
    (r"\b(yields? (fall|drop|slide|tumble|decline)|treasury rally)\b", "macro", +0.4),
    (r"\b(yields? (rise|jump|surge|climb)|treasury sell-?off)\b", "macro", -0.4),
    # dólar
    (r"\b(dollar (falls|drops|weakens|slides|tumbles)|dxy (falls|drops))\b", "macro", +0.4),
    (r"\b(dollar (rises|gains|strengthens|jumps|surges)|dxy (rises|jumps))\b", "macro", -0.4),
    # inflação / atividade
    (r"\b(inflation (cools|eases|slows|falls|softer)|cpi (falls|cools|misses))\b", "macro", +0.5),
    (r"\b(inflation (heats|accelerates|jumps|hotter|sticky)|cpi (jumps|beats|hotter))\b", "macro", -0.5),
    (r"\b(recession|contraction|slowdown|weak (jobs|payrolls|data)|payrolls miss)\b", "macro", +0.3),
    (r"\b(strong (jobs|payrolls|data)|payrolls beat|blowout jobs)\b", "macro", -0.4),
    # geopolítica
    (r"\b(war|missile|strike|attack|invasion|escalat|conflict|sanction|nuclear|troops|ceasefire collapse)\w*", "geopolitical", +0.5),
    (r"\b(ceasefire|peace (deal|talks)|de-?escalat|truce)\w*", "geopolitical", -0.4),
    # sistêmico
    (r"\b(bank (run|collapse|failure|rescue)|default|contagion|liquidity crisis|credit (stress|crunch)|bailout)\b", "systemic", +0.5),
    # China / bancos centrais / fluxo
    (r"\b(pboc|china central bank|central bank(s)? (buy|purchase|add)|reserves? (rise|increase))\w*", "flow", +0.5),
    (r"\b(etf (inflow|buying)|gold etf holdings rise)\w*", "flow", +0.4),
    (r"\b(etf (outflow|selling)|gold etf holdings fall)\w*", "flow", -0.4),
    (r"\b(china (stimulus|easing|cuts? rrr))\b", "china", +0.3),
    (r"\b(china (slowdown|property crisis|deflation))\b", "china", +0.1),
]

# extração de RESULTADO vs CONSENSO em manchetes
RELEASE_RE = re.compile(
    r"(?P<name>core cpi|cpi|core pce|pce|nonfarm payrolls|payrolls|nfp|unemployment rate|jobless claims|initial claims|"
    r"gdp|ism manufacturing|ism services|retail sales|jolts|consumer confidence|average hourly earnings)"
    r"[^0-9\-+]{0,40}(?P<actual>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>%|k|m)?"
    r"[^0-9\-+]{0,40}(?:vs\.?|versus|against|expected|forecast|consensus|est\.?|exp\.?)[^0-9\-+]{0,25}(?P<consensus>[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
NAME_TO_KIND = {
    "core cpi": "core_cpi", "cpi": "cpi", "core pce": "core_pce", "pce": "pce", "nonfarm payrolls": "nfp", "payrolls": "nfp",
    "nfp": "nfp", "unemployment rate": "unemployment", "jobless claims": "jobless_claims", "initial claims": "jobless_claims",
    "gdp": "gdp", "ism manufacturing": "ism", "ism services": "ism", "retail sales": "retail_sales", "jolts": "jolts",
    "consumer confidence": "consumer_confidence", "average hourly earnings": "earnings",
}


class NewsInterpreter(Protocol):
    def interpret(self, item: NewsItem) -> NewsItem:  # pragma: no cover - interface
        ...


class RuleInterpreter:
    """Interpretação por regras. Preenche category, gold_impact, interpretation e,
    quando há números, cria o EconomicEvent correspondente em `self.events`."""

    def __init__(self) -> None:
        self.events: list[EconomicEvent] = []

    def interpret(self, item: NewsItem) -> NewsItem:
        text = item.headline.lower()
        cat, impact, hits = "generic", 0.0, []
        for pattern, category, val in KEYWORDS:
            if re.search(pattern, text):
                hits.append(f"{category}:{val:+.1f}")
                impact += val
                if cat == "generic" or abs(val) > 0.4:
                    cat = category
        m = RELEASE_RE.search(item.headline)
        if m:
            kind = NAME_TO_KIND.get(m.group("name").lower(), "generic")
            actual, cons = float(m.group("actual")), float(m.group("consensus"))
            sens = EVENT_GOLD_SENSITIVITY.get(kind, 0.0)
            surprise = actual - cons
            impact = max(-1.0, min(1.0, sens * surprise / max(abs(cons) * 0.1, 0.1)))
            cat = "macro"
            self.events.append(EconomicEvent(m.group("name").upper(), item.time, "MUITO ALTO", consensus=cons, actual=actual, kind=kind, unit=m.group("unit") or ""))
            hits.append(f"release {kind}: {actual} vs {cons} (surpresa {surprise:+.2f})")
        item.category = cat
        item.gold_impact = max(-1.0, min(1.0, impact))
        # quanto já estava precificado: manchetes com "as expected"/"in line" → alto
        if re.search(r"\b(as expected|in line|matches? (forecast|expectations)|widely expected)\b", text):
            item.priced_in = 0.8
        item.interpretation = "; ".join(hits) if hits else "sem impacto identificado"
        return item


def parse_rss(xml_text: str, source: str = "") -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        try:
            t = parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            t = datetime.now(timezone.utc)
        items.append(NewsItem(headline=title, source=source or (root.findtext("channel/title") or ""), time=t.astimezone(timezone.utc)))
    return items


class NewsCollector:
    def __init__(self, http: HttpClient, feeds: tuple[str, ...] = DEFAULT_FEEDS, interpreter: Optional[NewsInterpreter] = None,
                 max_age_hours: float = 24.0) -> None:
        self.http = http
        self.feeds = feeds
        self.interpreter = interpreter or RuleInterpreter()
        self.max_age = timedelta(hours=max_age_hours)
        self.errors: dict[str, str] = {}
        self.health: list = []   # news_engine.FeedHealth por feed (fonte, atualização, notícias, válidas, descartadas, erro)

    def collect(self, now: Optional[datetime] = None) -> list[NewsItem]:

        now = now or datetime.now(timezone.utc)
        out: list[NewsItem] = []
        seen: set[str] = set()
        self.health = []
        self.errors = {}
        for url in self.feeds:
            source = url.split("/")[2]
            try:
                items = parse_rss(self.http.get_text(url, ttl=300), source=source)
            except Exception as e:  # noqa: BLE001 - isolar falha por feed
                self.errors[url] = str(e)
                self.health.append(FeedHealth(source, False, str(e)))
                continue
            valid = discarded = 0
            last = None
            for it in items:
                last = it.time if last is None or it.time > last else last
                key = it.headline.lower()[:80]
                if key in seen or now - it.time > self.max_age:
                    discarded += 1
                    continue
                seen.add(key)
                out.append(self.interpreter.interpret(it))
                valid += 1
            self.health.append(FeedHealth(source, True, "", len(items), valid, discarded, last))
            if not items:
                self.health[-1].ok, self.health[-1].error = False, "feed vazio ou não parseável"
        out.sort(key=lambda n: n.time, reverse=True)
        return out

    def released_events(self) -> list[EconomicEvent]:
        return list(getattr(self.interpreter, "events", []))


def aggregate_sentiment(news: list[NewsItem], now: datetime, half_life_hours: float = 6.0) -> tuple[Optional[float], Optional[float]]:
    """(sentimento -1..+1 ponderado por recência, variação vs. janela anterior)."""
    if not news:
        return None, None
    def weighted(items: list[NewsItem], ref: datetime) -> Optional[float]:
        num = den = 0.0
        for n in items:
            age_h = max(0.0, (ref - n.time).total_seconds() / 3600)
            w = 0.5 ** (age_h / half_life_hours)
            num += w * n.gold_impact * (1 - n.priced_in)
            den += w
        return num / den if den else None
    recent = [n for n in news if now - n.time <= timedelta(hours=12)]
    older = [n for n in news if timedelta(hours=12) < now - n.time <= timedelta(hours=24)]
    s_now, s_prev = weighted(recent, now), weighted(older, now - timedelta(hours=12))
    change = (s_now - s_prev) if (s_now is not None and s_prev is not None) else None
    return (round(s_now, 3) if s_now is not None else None), (round(change, 3) if change is not None else None)


def geopolitical_index(news: list[NewsItem], now: datetime) -> tuple[Optional[float], Optional[float]]:
    """Risco geopolítico 0..100 pela intensidade de manchetes das últimas 24h, e variação vs 24h anteriores."""
    if not news:
        return None, None
    def score(items: list[NewsItem]) -> float:
        geo = [n for n in items if n.category in ("geopolitical", "systemic")]
        return min(100.0, 20.0 * sum(abs(n.gold_impact) for n in geo) + 5.0 * len(geo))
    cur = score([n for n in news if now - n.time <= timedelta(hours=24)])
    prev = score([n for n in news if timedelta(hours=24) < now - n.time <= timedelta(hours=48)])
    return round(cur, 1), round(cur - prev, 1)


def load_calendar(path: str) -> list[EconomicEvent]:
    """Calendário local (JSON): [{"name","time" (ISO UTC),"impact","kind","consensus","previous","actual","unit"}]."""
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    out: list[EconomicEvent] = []
    for r in rows:
        t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        out.append(EconomicEvent(r["name"], t if t.tzinfo else t.replace(tzinfo=timezone.utc), r.get("impact", "ALTO"),
                                 r.get("consensus"), r.get("previous"), r.get("actual"), r.get("kind", "generic"), r.get("unit", "")))
    return out


# ============================================================================
# DATA · ENGINE
# ============================================================================

"""DATA ENGINE — coleta, normaliza e entrega um MarketSnapshot com dados reais.

Cada coletor é isolado: uma falha vira `status[fonte] = erro` e o campo fica None;
o GOLD AI ENGINE reduz a confiança em vez de quebrar. `LiveSource` implementa
`DataSource.snapshot()` e substitui `SampleSource`.
"""





@dataclass
class DataEngineConfig:
    xau_symbol: str = "GC=F"          # ou "XAUUSD=X"
    dxy_symbol: str = "DX-Y.NYB"
    us10y_symbol: str = "^TNX"
    us2y_symbol: str = "2YY=F"
    fedfunds_symbol: str = "ZQ=F"     # 30-day Fed Funds futures (100 − preço = taxa implícita)
    vix_symbol: str = "^VIX"
    spx_symbol: str = "^GSPC"
    silver_symbol: str = "SI=F"
    oil_symbol: str = "CL=F"
    btc_symbol: str = "BTC-USD"
    usdcnh_symbol: str = "CNH=X"
    window_minutes: int = 60          # janela das "variações recentes"
    cache_dir: Optional[str] = os.path.join(os.path.expanduser("~"), ".gold_ai_cache")
    feeds: Optional[tuple[str, ...]] = None
    calendar_path: Optional[str] = None
    enable_fred: bool = True
    enable_cot: bool = True
    enable_news: bool = True
    market_symbol: str = "XAUUSD"     # mercado para o NEWS ENGINE no modo de mercado único


class DataEngine:
    def __init__(self, cfg: Optional[DataEngineConfig] = None, http: Optional[HttpClient] = None) -> None:
        self.cfg = cfg or DataEngineConfig()
        self.http = http or HttpClient(cache_dir=self.cfg.cache_dir)
        self.yahoo = YahooCollector(self.http)
        self.fred = FredCollector(self.http)
        self.cftc = CftcCollector(self.http)
        self.news = NewsCollector(self.http, self.cfg.feeds) if self.cfg.feeds else NewsCollector(self.http)
        self.status: dict[str, str] = {}

    # ------------------------------------------------------------------ util
    def _try(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            self.status[name] = "ok"
        except Exception as e:  # noqa: BLE001 - isolar cada fonte
            self.status[name] = f"erro: {e}"

    def _change(self, symbol: str, tf: str = "M15", pct: bool = True) -> Optional[float]:
        return self.yahoo.change_over(self.yahoo.candles(symbol, tf), self.cfg.window_minutes, pct)

    # ------------------------------------------------------------------ coleta
    def collect(self, now: Optional[datetime] = None) -> MarketSnapshot:
        now = now or datetime.now(timezone.utc)
        s = MarketSnapshot(time=now)
        w = self.cfg.window_minutes

        def xau() -> None:
            s.candles = self.yahoo.all_timeframes(self.cfg.xau_symbol)
            ref = s.candles.get("H1") or s.candles.get("M15") or []
            if not ref:
                raise RuntimeError("sem candles XAU")
            s.price = ref[-1].close
            s.atr = _atr(s.candles["H1"]) or 0.0 if "H1" in s.candles else 0.0
            m5 = s.candles.get("M5") or ref
            s.price_change_pct = self.yahoo.change_over(m5, w) or 0.0
            # proxy de fluxo agressor: volume em candles de alta − de baixa na janela
            recent = [c for c in m5 if (now - c.time).total_seconds() <= w * 60]
            vol = sum(c.volume for c in recent)
            if vol > 0:
                s.order_flow_imbalance = round((sum(c.volume for c in recent if c.close > c.open) - sum(c.volume for c in recent if c.close < c.open)) / vol, 3)
            h1 = s.candles.get("H1") or []
            if len(h1) > 30:
                avg = sum(c.volume for c in h1[-30:-1]) / 29
                s.futures_volume_ratio = round(h1[-1].volume / avg, 2) if avg else None
        self._try("xau", xau)

        def dxy() -> None:
            cs = self.yahoo.candles(self.cfg.dxy_symbol, "M15")
            s.dxy, s.dxy_change_pct = cs[-1].close, self.yahoo.change_over(cs, w)
        self._try("dxy", dxy)

        def yields_() -> None:
            cs = self.yahoo.candles(self.cfg.us10y_symbol, "M15")
            s.us10y = cs[-1].close
            d = self.yahoo.change_over(cs, w, pct=False)
            s.us10y_change_bp = d * 100 if d is not None else None
        self._try("us10y", yields_)

        def us2y() -> None:
            cs = self.yahoo.candles(self.cfg.us2y_symbol, "M15")
            s.us2y = cs[-1].close
        self._try("us2y", us2y)

        def fed() -> None:
            cs = self.yahoo.candles(self.cfg.fedfunds_symbol, "M15")
            d = self.yahoo.change_over(cs, w, pct=False)  # Δpreço; taxa implícita = 100 − preço
            if d is not None:
                implied_bp = -d * 100
                # 25 bp de corte ≈ 100 p.p.: queda de 5 bp na taxa implícita ≈ +20 p.p. de prob. de corte
                s.fed_cut_prob_change_pp = round(max(-100.0, min(100.0, -implied_bp * 4)), 1)
        self._try("fed_funds", fed)

        def fred() -> None:
            if not self.cfg.enable_fred:
                return
            real, d_real = self.fred.last_and_change("DFII10")
            _, d_be = self.fred.last_and_change("T10YIE")
            s.real_yield_10y = real
            s.breakeven_10y_change_bp = d_be * 100 if d_be is not None else None
            if s.us10y_change_bp is not None and d_be is not None:
                # intraday: nominal (Yahoo) − breakeven (FRED, diário)
                s.real_yield_change_bp = round(s.us10y_change_bp - d_be * 100 * (w / 1440), 2)
            elif d_real is not None:
                s.real_yield_change_bp = round(d_real * 100, 2)
            hy, d_hy = self.fred.last_and_change("BAMLH0A0HYM2")
            s.credit_spread_bp = hy * 100 if hy is not None else None
            s.credit_spread_change_bp = d_hy * 100 if d_hy is not None else None
        self._try("fred", fred)

        def risk() -> None:
            cs = self.yahoo.candles(self.cfg.vix_symbol, "M15")
            s.vix, s.vix_change_pct = cs[-1].close, self.yahoo.change_over(cs, w)
            s.equity_change_pct = self._change(self.cfg.spx_symbol)
        self._try("vix_spx", risk)

        def intermarket() -> None:
            s.silver_change_pct = self._change(self.cfg.silver_symbol)
            s.oil_change_pct = self._change(self.cfg.oil_symbol)
            s.btc_change_pct = self._change(self.cfg.btc_symbol)
            s.usdcnh_change_pct = self._change(self.cfg.usdcnh_symbol)
        self._try("intermarket", intermarket)

        def cot() -> None:
            if not self.cfg.enable_cot:
                return
            data = self.cot_with_cache("088691", now)
            if data is None:
                raise RuntimeError("COT indisponível e sem cache")
            s.cot_managed_money_net, s.cot_managed_money_net_change = data["net"], data["change"]
            s.cot_managed_money_percentile, s.cot_commercial_net_change = data["percentile"], data["commercial_change"]
            s.cot_age_days, s.cot_report_date = data["age_days"], data["report_date"]
            if data.get("from_cache"):
                raise RuntimeError(f"usando último COT válido ({data['report_date']}, {data['age_days']:.0f} dias)")
        self._try("cot", cot)

        def news() -> None:
            if not self.cfg.enable_news:
                return
            items = self.news.collect(now)
            s.news = items[:50]
            s.sentiment, s.sentiment_change = aggregate_sentiment(items, now)
            s.geopolitical_risk, s.geopolitical_risk_change = geopolitical_index(items, now)
            s.events += self.news.released_events()
            if self.news.errors:
                raise RuntimeError(f"{len(items)} notícias; feeds com erro: {len(self.news.errors)}")
        self._try("news", news)

        def calendar() -> None:
            if self.cfg.calendar_path and os.path.exists(self.cfg.calendar_path):
                s.events += load_calendar(self.cfg.calendar_path)
        self._try("calendar", calendar)

        def news_engine() -> None:
            if not self.cfg.enable_news:
                return
            identified = EventIdentifier().identify(s.news, s.events, now)
            na = NewsEngine().assess(self.cfg.market_symbol, s, identified, now)
            s.news_pressure, s.news_status, s.news_chain = na.pressure, na.status, na.chain
            if not identified:
                raise RuntimeError("nenhum evento identificado → NEWS UNKNOWN (peso reduzido, não negativo)")
        self._try("news_engine", news_engine)

        return s

    # ------------------------------------------------------------------ COT: último dado válido conhecido + idade
    def _cot_cache_path(self) -> Optional[str]:
        return os.path.join(self.cfg.cache_dir, "cot_last_valid.json") if self.cfg.cache_dir else None

    def cot_with_cache(self, code: str, now: datetime) -> Optional[dict]:
        path = self._cot_cache_path()
        cache: dict = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:  # noqa: BLE001
                cache = {}
        try:
            r = self.cftc.gold(code=code)
            data = {"net": r.managed_money_net, "change": r.managed_money_net_change, "percentile": r.managed_money_percentile,
                    "commercial_change": r.commercial_net_change, "report_date": r.report_date.isoformat()}
            cache[code] = data
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
            data = dict(data); data["from_cache"] = False
        except Exception:  # noqa: BLE001
            if code not in cache:
                return None
            data = dict(cache[code]); data["from_cache"] = True
        rd = datetime.fromisoformat(data["report_date"]).replace(tzinfo=timezone.utc)
        data["age_days"] = round((now - rd).total_seconds() / 86400, 1)
        return data

    def coverage(self) -> str:
        ok = [k for k, v in self.status.items() if v == "ok"]
        bad = {k: v for k, v in self.status.items() if v != "ok"}
        lines = [f"DATA ENGINE — fontes ok: {', '.join(ok) or 'nenhuma'}"]
        for k, v in bad.items():
            lines.append(f"  ✗ {k}: {v}")
        if self.cfg.enable_news:
            lines.append(render_feed_health(self.news.health))
        return "\n".join(lines)


class LiveSource:
    """DataSource real: substitui SampleSource."""

    def __init__(self, cfg: Optional[DataEngineConfig] = None, http: Optional[HttpClient] = None) -> None:
        self.engine = DataEngine(cfg, http)

    def snapshot(self) -> MarketSnapshot:
        return self.engine.collect()


# ============================================================================
# DATA · MT5
# ============================================================================

"""MetaTrader 5 — fonte de candles reais do broker e executor com autorização explícita.

Requer o pacote `MetaTrader5` (Windows) e o terminal instalado:
    pip install MetaTrader5

`MT5Source` lê XAU/USD (M1…W1, tick volume, bid/ask) direto do terminal e completa o
resto do MarketSnapshot com o DataEngine (DXY, juros, FRED, COT, notícias).
`MT5Executor` NUNCA envia ordem sem `authorize=True` — por padrão apenas simula.
"""





TF_TO_MT5 = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
             "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1"}
BARS = {"M1": 300, "M5": 300, "M15": 300, "M30": 300, "H1": 300, "H4": 300, "D1": 300, "W1": 160}


@dataclass
class MT5Config:
    path: Optional[str] = None            # ex.: r"C:\Program Files\MetaTrader 5\terminal64.exe"
    symbol: str = "XAUUSD"
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    window_minutes: int = 60

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "MT5Config":
        e = {**(env or {}), **os.environ}
        login = e.get("MT5_LOGIN")
        return cls(path=e.get("MT5_PATH") or e.get("CAMINHO_MT5"), symbol=e.get("MT5_SYMBOL", "XAUUSD"),
                   login=int(login) if login else None, password=e.get("MT5_PASSWORD"), server=e.get("MT5_SERVER"))


class MT5Error(RuntimeError):
    pass


def rates_to_candles(rates: Any, server_offset_hours: float = 0.0) -> list[Candle]:
    """Converte o array de `copy_rates_from_pos` (time, open, high, low, close, tick_volume, spread, real_volume).
    O MT5 carimba no horário do SERVIDOR da corretora (Pepperstone: GMT+2/+3): `server_offset_hours` converte para UTC."""
    out: list[Candle] = []
    for r in rates or []:
        t = datetime.fromtimestamp(int(r["time"]) - int(server_offset_hours * 3600), tz=timezone.utc)
        vol = float(r["real_volume"]) if _has(r, "real_volume") else 0.0
        if vol <= 0:
            vol = float(r["tick_volume"]) if _has(r, "tick_volume") else 0.0
        out.append(Candle(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), vol))
    return out


def _has(row: Any, key: str) -> bool:
    try:
        names = row.dtype.names
        return key in names
    except AttributeError:
        try:
            return key in row
        except TypeError:
            return False


class MT5Client:
    """Wrapper fino sobre o módulo MetaTrader5 (injetável para testes)."""

    def __init__(self, cfg: MT5Config, mt5: Any = None) -> None:
        self.cfg = cfg
        self.mt5 = mt5 or _mt5
        if self.mt5 is None:
            raise MT5Error("pacote MetaTrader5 não disponível (instale no Windows: pip install MetaTrader5)")
        self.connected = False

    def connect(self) -> None:
        kwargs: dict[str, Any] = {}
        if self.cfg.path:
            kwargs["path"] = self.cfg.path
        if self.cfg.login:
            kwargs.update(login=self.cfg.login, password=self.cfg.password, server=self.cfg.server)
        if not self.mt5.initialize(**kwargs):
            err = self.mt5.last_error()
            code = err[0] if isinstance(err, (tuple, list)) and err else None
            hints = {
                -6: "Authorization failed: o terminal abriu mas não há conta autorizada. Abra o terminal da corretora, faça login na conta (demo ou real) e "
                    "deixe-o aberto; ou defina MT5_LOGIN, MT5_PASSWORD e MT5_SERVER no .env (ex.: MT5_SERVER=Pepperstone-Demo).",
                -10003: "IPC initialize failed: caminho do terminal64.exe incorreto em MT5_PATH ou terminal de outro usuário do Windows.",
                -10004: "IPC timeout: o terminal demorou a responder; abra-o manualmente e tente de novo.",
                -2: "Invalid params: confira MT5_PATH (use barras invertidas) e MT5_LOGIN numérico.",
            }
            raise MT5Error(f"initialize falhou: {err}. {hints.get(code, 'Confira MT5_PATH, se o terminal está aberto e logado, e se o pacote MetaTrader5 é da mesma arquitetura (64 bits) do Python.')}")
        if not self.mt5.symbol_select(self.cfg.symbol, True):
            raise MT5Error(f"símbolo {self.cfg.symbol} indisponível: {self.mt5.last_error()}")
        self.connected = True
        self.server_offset_hours = self._detect_server_offset()

    def _detect_server_offset(self) -> float:
        """Fuso do servidor da corretora: compara o carimbo do último tick com o relógio UTC local (arredondado à hora).
        MT5_UTC_OFFSET_HOURS no .env força um valor. Sem tick recente (fim de semana), usa 0 e avisa."""
        forced = os.environ.get("MT5_UTC_OFFSET_HOURS") or getattr(self.cfg, "utc_offset_hours", None)
        if forced not in (None, ""):
            return float(forced)
        try:
            t = self.mt5.symbol_info_tick(self.cfg.symbol)
            ts = float(getattr(t, "time", 0) or 0)
        except Exception:  # noqa: BLE001
            ts = 0.0
        if not ts:
            return 0.0
        now = datetime.now(timezone.utc).timestamp()
        if abs(now - ts) > 3 * 86400:      # tick velho (mercado fechado há dias): não dá para inferir
            return 0.0
        return float(round((ts - now) / 3600.0))

    def close(self) -> None:
        if self.connected:
            self.mt5.shutdown()
            self.connected = False

    def candles(self, tf: str, n: Optional[int] = None) -> list[Candle]:
        rates = self.mt5.copy_rates_from_pos(self.cfg.symbol, getattr(self.mt5, TF_TO_MT5[tf]), 0, n or BARS[tf])
        if rates is None:
            raise MT5Error(f"copy_rates_from_pos({tf}) falhou: {self.mt5.last_error()}")
        return rates_to_candles(rates, getattr(self, "server_offset_hours", 0.0))

    def rates_range(self, symbol: str, tf: str, start: datetime, end: datetime) -> list[Candle]:
        """Histórico por intervalo (copy_rates_range) — M1 costuma existir por anos na corretora."""
        if not self.mt5.symbol_select(symbol, True):
            raise MT5Error(f"símbolo {symbol} indisponível: {self.mt5.last_error()}")
        off = getattr(self, "server_offset_hours", 0.0)
        shift = timedelta(hours=off)
        rates = self.mt5.copy_rates_range(symbol, getattr(self.mt5, TF_TO_MT5[tf]), start + shift, end + shift)   # pedido em hora do servidor
        if rates is None:
            raise MT5Error(f"copy_rates_range({symbol},{tf}) falhou: {self.mt5.last_error()}")
        return rates_to_candles(rates, off)

    def ticks_range(self, symbol: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float]]:
        """Ticks (time_msc, bid, ask) por intervalo (copy_ticks_range, COPY_TICKS_INFO) — a corretora guarda semanas/meses."""
        if not self.mt5.symbol_select(symbol, True):
            raise MT5Error(f"símbolo {symbol} indisponível: {self.mt5.last_error()}")
        flags = getattr(self.mt5, "COPY_TICKS_INFO", 1)
        off = getattr(self, "server_offset_hours", 0.0)
        shift = timedelta(hours=off)
        ticks = self.mt5.copy_ticks_range(symbol, start + shift, end + shift, flags)
        if ticks is None:
            raise MT5Error(f"copy_ticks_range({symbol}) falhou: {self.mt5.last_error()}")
        out = []
        last_bid = last_ask = None
        for r in ticks:
            ms = (int(r["time_msc"]) if _has(r, "time_msc") else int(r["time"]) * 1000) - int(off * 3600 * 1000)
            bid = float(r["bid"]) if _has(r, "bid") and r["bid"] else last_bid
            ask = float(r["ask"]) if _has(r, "ask") and r["ask"] else last_ask
            if bid is None or ask is None:
                continue
            last_bid, last_ask = bid, ask
            out.append((datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc), bid, ask))
        return out

    def tick(self) -> tuple[float, float]:
        t = self.mt5.symbol_info_tick(self.cfg.symbol)
        if t is None:
            raise MT5Error(f"tick indisponível: {self.mt5.last_error()}")
        return float(t.bid), float(t.ask)


class MT5Source:
    """DataSource: XAU do MT5 + demais camadas do DataEngine (opcional)."""

    def __init__(self, cfg: Optional[MT5Config] = None, data_engine: Any = None, mt5: Any = None) -> None:
        self.cfg = cfg or MT5Config.from_env()
        self.client = MT5Client(self.cfg, mt5)
        self.data_engine = data_engine
        self.status: dict[str, str] = {}

    def snapshot(self) -> MarketSnapshot:
        now = datetime.now(timezone.utc)
        s = self.data_engine.collect(now) if self.data_engine is not None else MarketSnapshot(time=now)
        if self.data_engine is not None:
            self.status.update(self.data_engine.status)
        try:
            if not self.client.connected:
                self.client.connect()
            candles = {tf: self.client.candles(tf) for tf in TF_TO_MT5}
            candles = {k: v for k, v in candles.items() if v}
            if not candles.get("H1"):
                raise MT5Error("sem candles H1")
            s.candles = candles  # o broker é a fonte primária do preço
            bid, ask = self.client.tick()
            s.price = (bid + ask) / 2
            s.atr = _atr(candles["H1"]) or s.atr
            m5 = candles.get("M5") or candles["H1"]
            w = self.cfg.window_minutes
            ref = next((c for c in reversed(m5[:-1]) if (m5[-1].time - c.time).total_seconds() >= w * 60), m5[0])
            s.price_change_pct = (m5[-1].close / ref.close - 1) * 100 if ref.close else 0.0
            recent = [c for c in m5 if (now - c.time).total_seconds() <= w * 60]
            vol = sum(c.volume for c in recent)
            if vol > 0:
                s.order_flow_imbalance = round((sum(c.volume for c in recent if c.close > c.open) - sum(c.volume for c in recent if c.close < c.open)) / vol, 3)
            self.status["mt5"] = "ok"
        except MT5Error as e:
            self.status["mt5"] = f"erro: {e}"
        return s


@dataclass
class OrderPlan:
    direction: Direction
    volume: float
    entry: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    comment: str
    authorized: bool = False
    result: Optional[dict] = None
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        side = "COMPRA" if self.direction == Direction.ALTA else "VENDA"
        st = "ENVIADA" if self.result else ("AUTORIZADA — não enviada" if self.authorized else "SIMULADA (sem autorização)")
        return (f"ORDEM {side} {self.volume} lote(s) @ {self.entry:.2f} | SL {self.stop_loss} | TP {self.take_profit} | {st}"
                + (f" | {self.result}" if self.result else "") + ("".join(f"\n  • {n}" for n in self.notes)))


class MT5Executor:
    """Converte um Signal em plano de ordem. Só envia ao broker com `authorize=True`
    (por padrão apenas simula) e nunca para sinais WATCH/PRE-MOVE/REVERSAL/RISK."""

    EXECUTABLE = {SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL}

    def __init__(self, client: MT5Client, volume: float = 0.01, rr: float = 2.0, min_confidence: float = 70.0, min_level: int = 3) -> None:
        self.client = client
        self.volume, self.rr = volume, rr
        self.min_confidence, self.min_level = min_confidence, min_level

    def plan(self, sig: Signal) -> Optional[OrderPlan]:
        if sig.type not in self.EXECUTABLE or sig.direction == Direction.LATERAL:
            return None
        a = sig.assessment
        notes: list[str] = []
        if a.confidence < self.min_confidence:
            notes.append(f"confiança {a.confidence:.0f} < {self.min_confidence:.0f}")
        if int(a.evidence_level) < self.min_level:
            notes.append(f"evidência nível {int(a.evidence_level)} < {self.min_level}")
        if not a.has_edge:
            notes.append("sem vantagem estatística")
        inval = a.zone.get("invalidation")
        entry = a.price
        sl = inval
        tp = None
        if sl is not None:
            risk = abs(entry - sl)
            tp = entry + risk * self.rr if sig.direction == Direction.ALTA else entry - risk * self.rr
        plan = OrderPlan(sig.direction, self.volume, entry, sl, round(tp, 2) if tp else None, f"GoldAI {sig.type.value}", notes=notes)
        return plan

    def send_plan(self, plan, authorize: bool = False) -> OrderPlan:
        """Executa um trading.TradePlan (2.2): stop do Stop Engine, TP da estratégia recomendada, lote do gestor de risco."""

        st = next((s for s in STRATEGIES if s.name == plan.recommended), None)
        tp = plan.price_at_r(st.target_r) if st and st.target_r else (plan.price_at_r(st.partial_r) if st and st.partial_r else None)
        order = OrderPlan(plan.direction, plan.lots or self.volume, plan.entry, round(plan.stop, 2), round(tp, 2) if tp else None,
                          f"GoldAI {plan.signal_type} {plan.recommended}", notes=[])
        if not plan.lots:
            order.notes.append("lote zero — gestor de risco")
        if plan.confidence < self.min_confidence:
            order.notes.append(f"confiança {plan.confidence:.0f} < {self.min_confidence:.0f}")
        if plan.evidence_level < self.min_level:
            order.notes.append(f"evidência nível {plan.evidence_level} < {self.min_level}")
        return self.execute(order, authorize=authorize)

    def execute(self, plan: OrderPlan, authorize: bool = False) -> OrderPlan:
        plan.authorized = authorize
        if not authorize or plan.notes:
            return plan  # simulação: nada é enviado
        mt5 = self.client.mt5
        bid, ask = self.client.tick()
        buy = plan.direction == Direction.ALTA
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.client.cfg.symbol, "volume": plan.volume,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": ask if buy else bid,
            "sl": plan.stop_loss or 0.0, "tp": plan.take_profit or 0.0, "deviation": 20, "magic": 20260914,
            "comment": plan.comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        plan.result = {"retcode": getattr(res, "retcode", None), "order": getattr(res, "order", None), "comment": getattr(res, "comment", "")}
        return plan


# ============================================================================
# DATA · DUKASCOPY
# ============================================================================

"""DUKASCOPY — ticks bid/ask históricos, gratuitos e sem chave (fonte alternativa ao MT5 para o REACTION ENGINE).

URL por hora: https://datafeed.dukascopy.com/datafeed/<INSTRUMENTO>/<ANO>/<MÊS-1 com 2 dígitos>/<DIA>/<HORA>h_ticks.bi5
Cada arquivo é LZMA com registros big-endian de 20 bytes: (ms desde o início da hora: uint32, ask: uint32, bid: uint32,
volume ask: float32, volume bid: float32). Preços inteiros divididos pela escala do instrumento (confira o primeiro tick).
Hora sem dados (fim de semana, feriado) = 404 → vazio. Meses no caminho começam em 00 (janeiro).
"""


import lzma
import struct

DUKA_BASE = "https://datafeed.dukascopy.com/datafeed"

# mercado do MARKET AI → (instrumento Dukascopy, escala de preço)
DUKA_INSTRUMENTS: dict[str, tuple[str, float]] = {
    "XAUUSD": ("XAUUSD", 1000.0),
    "EURUSD": ("EURUSD", 100000.0),
    "GBPUSD": ("GBPUSD", 100000.0),
    "USDJPY": ("USDJPY", 1000.0),
    "US500": ("USA500IDXUSD", 1000.0),
    "NAS100": ("USATECHIDXUSD", 1000.0),
    "WTI": ("LIGHTCMDUSD", 1000.0),
    "USDX": ("DOLLARIDXUSD", 1000.0),      # índice do dólar: líder USD
    "US10Y": ("USTBONDTRUSD", 1000.0),     # T-Bond futuro (proxy inverso de yields; use --lead-yield com cautela)
    "BTCUSD": ("BTCUSD", 10.0),
}


def parse_bi5(data: bytes, hour_start: datetime, scale: float) -> list[tuple[datetime, float, float]]:
    if not data:
        return []
    raw = lzma.decompress(data)
    out = []
    for off in range(0, len(raw) - len(raw) % 20, 20):
        ms, ask, bid, _va, _vb = struct.unpack(">IIIff", raw[off:off + 20])
        if bid <= 0 or ask <= 0:
            continue
        out.append((hour_start + timedelta(milliseconds=ms), bid / scale, ask / scale))
    return out


def hour_url(instrument: str, t: datetime) -> str:
    return f"{DUKA_BASE}/{instrument}/{t.year}/{t.month - 1:02d}/{t.day:02d}/{t.hour:02d}h_ticks.bi5"


class DukascopyImporter:
    """Uma hora que falhe (timeout, 503) é tentada de novo com espera crescente; se persistir, é anotada em `failed` e pulada —
    o download continua. Horas baixadas ficam em cache (HttpClient), então repetir o comando refaz só o que faltou."""

    def __init__(self, http, log: Optional[Callable[[str], None]] = None, retries: int = 4, sleep=None, pace: float = 0.15) -> None:
        import time as _time
        self.http, self._log, self.retries, self.pace = http, log, retries, pace
        self._sleep = sleep or _time.sleep
        self.failed: list[tuple[str, datetime, str]] = []

    def _fetch_hour(self, inst: str, h: datetime) -> Optional[bytes]:
        for attempt in range(self.retries + 1):
            try:
                data = self.http.get_bytes(hour_url(inst, h), ttl=365 * 24 * 3600, allow_404=True)
                if self.pace:
                    self._sleep(self.pace)
                return data
            except (DataError, OSError, EOFError) as e:
                if attempt == self.retries:
                    self.failed.append((inst, h, str(e)[-80:]))
                    if self._log:
                        self._log(f"  {inst} {h:%Y-%m-%d %H}h: FALHOU após {self.retries + 1} tentativas — pulada (repita o comando para completar)")
                    return None
                self._sleep(min(60.0, 3.0 * (2 ** attempt)))
        return None

    def hours(self, market: str, hours: Sequence[datetime], scale: Optional[float] = None) -> list[tuple[datetime, float, float]]:
        inst, sc = DUKA_INSTRUMENTS.get(market.upper(), (market.upper(), 1000.0))
        sc = scale or sc
        out = []
        for h in sorted(set(x.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc) for x in hours)):
            if h.weekday() == 5 or (h.weekday() == 6 and h.hour < 22):
                continue                                              # mercado fechado: nem pede
            data = self._fetch_hour(inst, h)
            if data is None:
                continue
            try:
                out += parse_bi5(data, h, sc)
            except Exception as e:  # noqa: BLE001 — arquivo corrompido/incompleto
                self.failed.append((inst, h, f"bi5 inválido: {e}"))
        out.sort()
        return out

    def around_events(self, market: str, event_times: Sequence[datetime], before_h: int = 4, after_h: int = 1, scale: Optional[float] = None,
                      checkpoint: Optional[Callable[[list], None]] = None) -> list[tuple[datetime, float, float]]:
        """Só as horas ao redor de cada evento (−before_h … +after_h): amostra grande sem baixar o ano inteiro."""
        hours: list[datetime] = []
        for t in event_times:
            t = t.astimezone(timezone.utc)
            for k in range(-before_h, after_h + 1):
                hours.append(t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=k))
        uniq = sorted(set(hours))
        if self._log:
            self._log(f"Dukascopy {market}: {len(uniq)} horas ao redor de {len(event_times)} eventos")
        out = []
        for i in range(0, len(uniq), 24):
            out += self.hours(market, uniq[i:i + 24], scale)
            if checkpoint:
                checkpoint(out)
            if self._log and (i // 24) % 10 == 9:
                self._log(f"  {market}: {min(i + 24, len(uniq))}/{len(uniq)} horas · {len(out)} ticks")
        return out

    def range(self, market: str, start: date, end: date, scale: Optional[float] = None, checkpoint: Optional[Callable[[list], None]] = None) -> list:
        hours = []
        t = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        t_end = datetime(end.year, end.month, end.day, 23, tzinfo=timezone.utc)
        while t <= t_end:
            if t.weekday() < 5 or (t.weekday() == 6 and t.hour >= 22):
                hours.append(t)
            t += timedelta(hours=1)
        if self._log:
            self._log(f"Dukascopy {market}: {len(hours)} horas ({start} → {end})")
        out = []
        for i in range(0, len(hours), 24):
            out += self.hours(market, hours[i:i + 24], scale)
            if checkpoint:
                checkpoint(out)
        return out


# ============================================================================
# TRADING
# ============================================================================

"""GOLD AI ENGINE 2.2 — TRADE SIMULATOR · STOP ENGINE · MAX PROFIT ENGINE · GESTÃO DE RISCO.

PRE-MOVE → DIREÇÃO → ENTRADA → STOP → 1R → 2R → 3R → TRAILING → RESULTADO

Pergunta central: quando o motor dá um sinal, quantas vezes o preço atinge 1R, 2R, 3R
antes do stop? E qual estratégia de saída tem a melhor expectativa em R?

Hipótese inicial: RISCO = 1R, ALVO = 3R. O simulador comprova ou rejeita.
"""





# --------------------------------------------------------------------------- modos e limites
class TradingMode22(str, Enum):  # modos do 2.2 (PositionManager); o 3.0 usa guard.TradingMode
    PAPER = "PAPER"          # 🟡 simula tudo, não envia ordem
    AUTHORIZE = "AUTHORIZE"  # 🟠 prepara a ordem e espera autorização
    LIVE = "LIVE"            # 🔴 envia automaticamente ao MT5


@dataclass
class RiskLimits:
    """Definidos pelo usuário. A IA pode ter 87 % de confiança, mas NÃO pode aumentar o risco."""

    risk_per_trade_pct: float = 0.5   # % do capital arriscado por operação (= 1R)
    max_daily_loss_pct: float = 2.0
    max_positions: int = 1
    max_lot: float = 0.10
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_spread: float = 0.60          # USD (XAUUSD)
    max_slippage: float = 0.30        # USD
    contract_size: float = 100.0      # onças por lote (XAUUSD padrão)
    min_stop_atr: float = 0.6         # stop nunca mais apertado que isso
    max_stop_atr: float = 2.5         # nem mais largo

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "RiskLimits":
        g = lambda k, d: type(d)(env.get(k, d))  # noqa: E731
        return cls(risk_per_trade_pct=g("RISK_PER_TRADE", 0.5), max_daily_loss_pct=g("MAX_DAILY_LOSS", 2.0),
                   max_positions=g("MAX_POSITIONS", 1), max_lot=g("MAX_LOT", 0.10), max_spread=g("MAX_SPREAD", 0.60),
                   max_slippage=g("MAX_SLIPPAGE", 0.30), contract_size=g("CONTRACT_SIZE", 100.0))


# --------------------------------------------------------------------------- estratégias de saída
@dataclass(frozen=True)
class ExitStrategy:
    name: str
    target_r: Optional[float] = None      # alvo fixo em R (None = sem alvo fixo)
    trail_r: Optional[float] = None       # distância do trailing em R (None = sem trailing)
    trail_activate_r: float = 1.0         # trailing começa após atingir este R
    partial_r: Optional[float] = None     # realiza parcial neste R…
    partial_fraction: float = 0.5         # …desta fração, e move o stop para o zero a zero


STRATEGIES: tuple[ExitStrategy, ...] = (
    ExitStrategy("1R", target_r=1.0),
    ExitStrategy("2R", target_r=2.0),
    ExitStrategy("3R", target_r=3.0),
    ExitStrategy("4R", target_r=4.0),
    ExitStrategy("trailing", trail_r=1.0, trail_activate_r=1.0),
    ExitStrategy("2R+trailing", target_r=None, partial_r=2.0, partial_fraction=0.5, trail_r=1.0, trail_activate_r=2.0),
)
DEFAULT_STRATEGY = "3R"  # hipótese inicial: risco 1R, alvo 3R


# --------------------------------------------------------------------------- plano
@dataclass
class TradePlan:
    direction: Direction
    entry: float
    stop: float
    atr: float
    time: datetime
    targets: dict[str, float] = field(default_factory=dict)             # "1R".."4R" → preço
    structural_target: Optional[float] = None
    volatility_target: Optional[float] = None
    statistical_target_r: Optional[float] = None
    recommended: str = DEFAULT_STRATEGY
    recommended_reason: str = "hipótese inicial (risco 1R, alvo 3R) — ainda sem histórico"
    probabilities: dict[str, Optional[float]] = field(default_factory=dict)  # "1R" → prob. de atingir antes do stop
    lots: Optional[float] = None
    risk_usd: Optional[float] = None
    signal_type: str = ""
    evidence_level: int = 0
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    viable: bool = True                   # False = resistência/suporte forte antes do alvo mínimo
    structure_rr: Optional[float] = None

    @property
    def r_value(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def sign(self) -> float:
        return 1.0 if self.direction == Direction.ALTA else -1.0

    def price_at_r(self, r: float) -> float:
        return self.entry + self.sign * r * self.r_value

    def render(self) -> str:
        side = "COMPRA" if self.direction == Direction.ALTA else "VENDA"
        lines = [f"🧾 PLANO DE OPERAÇÃO — {side} XAU/USD", f"Entrada = {self.entry:.2f}", f"Stop = {self.stop:.2f}  (1R = {self.r_value:.2f} USD)", ""]
        for k in ("1R", "2R", "3R", "4R"):
            p = self.probabilities.get(k)
            lines.append(f"{k} = {self.targets[k]:.2f}" + (f"   prob. {p:.0%}" if p is not None else "   prob. n/d"))
        lines += ["", f"Alvo estrutural: {self.structural_target:.2f}" if self.structural_target else "Alvo estrutural: n/d",
                  f"Alvo de volatilidade: {self.volatility_target:.2f}" if self.volatility_target else "Alvo de volatilidade: n/d",
                  f"Alvo estatístico: {self.statistical_target_r:.1f}R" if self.statistical_target_r else "Alvo estatístico: n/d",
                  "", f"TP ótimo = {self.recommended}  ({self.recommended_reason})",
                  f"R:R = 1:{self.recommended[0] if self.recommended[:1].isdigit() else '3'}"]
        if self.lots is not None:
            lines.append(f"Lote = {self.lots:.2f}  (risco {self.risk_usd:.2f} USD)")
        lines += [f"  • {n}" for n in self.notes]
        return "\n".join(lines)


class StopEngine:
    """Stop = invalidação estrutural (suporte/resistência da zona), limitado a [min, max] ATR."""

    def __init__(self, limits: Optional[RiskLimits] = None) -> None:
        self.limits = limits or RiskLimits()

    def candidates(self, a: Assessment, direction: Direction, atr: float, s: Optional[MarketSnapshot] = None) -> list[tuple[float, str]]:
        """STOP INTELIGENTE: estrutura (invalidação/suporte/resistência), candle anterior, VWAP e banda de ATR."""
        entry = a.price
        sign = 1.0 if direction == Direction.ALTA else -1.0
        out: list[tuple[float, str]] = []
        inval = a.zone.get("invalidation")
        if inval is not None and sign * (entry - inval) > 0:
            out.append((inval, "invalidação estrutural"))
        lvl = a.zone.get("support") if direction == Direction.ALTA else a.zone.get("resistance")
        if lvl is not None and sign * (entry - lvl) > 0:
            out.append((lvl - sign * 0.15 * atr, "suporte/resistência + margem"))
        if s is not None:
            ref = s.candles.get("H1") or s.candles.get("M30") or []
            if len(ref) >= 2:
                prev = ref[-2]
                lvl_c = prev.low if direction == Direction.ALTA else prev.high
                out.append((lvl_c - sign * 0.1 * atr, "candle anterior (H1)"))
        for r in a.technical:
            if r.timeframe == "H1" and r.vwap_position is not None and r.atr:
                vwap = entry - r.vwap_position * r.atr
                band = vwap - sign * 1.0 * r.atr
                if sign * (entry - band) > 0:
                    out.append((band, "banda VWAP −1 ATR"))
        out.append((entry - sign * 1.2 * atr, "1.2 ATR (volatilidade)"))
        return out

    def compute(self, a: Assessment, direction: Direction, atr: float, s: Optional[MarketSnapshot] = None) -> tuple[float, str]:
        """Escolhe o candidato estrutural mais próximo da entrada que respeite MIN_STOP_ATR; limita a MAX_STOP_ATR."""
        entry = a.price
        sign = 1.0 if direction == Direction.ALTA else -1.0
        lo, hi = self.limits.min_stop_atr * atr, self.limits.max_stop_atr * atr
        valid = [(lvl, why) for lvl, why in self.candidates(a, direction, atr, s) if abs(entry - lvl) >= lo]
        if not valid:
            return entry - sign * lo, f"todos os níveis muito próximos → stop em {self.limits.min_stop_atr:.1f} ATR"
        lvl, why = min(valid, key=lambda x: abs(entry - x[0]))
        if abs(entry - lvl) > hi:
            return entry - sign * hi, f"{why} muito distante → stop em {self.limits.max_stop_atr:.1f} ATR"
        return lvl, f"stop: {why} ({abs(entry - lvl) / atr:.2f} ATR)"


# --------------------------------------------------------------------------- simulação
@dataclass
class TradeResult:
    strategy: str
    r_multiple: float
    exit_reason: str          # STOP | TARGET | TRAIL | PARTIAL+TRAIL | HORIZON | OPEN
    bars: int
    exit_time: Optional[datetime]


@dataclass
class ExcursionProfile:
    """Trajetória com o stop INICIAL fixo: até onde o preço foi antes de estopar."""

    max_r_before_stop: float
    mae_r: float
    stopped: bool
    horizon_reached: bool
    bars: int

    def hit(self, r: float) -> bool:
        return self.max_r_before_stop >= r


def _fav_adv(plan: TradePlan, c: Candle) -> tuple[float, float]:
    """(excursão favorável, adversa) do candle em R (adversa positiva = contra)."""
    s, R = plan.sign, plan.r_value or 1e-9
    fav = (c.high - plan.entry) / R if s > 0 else (plan.entry - c.low) / R
    adv = (plan.entry - c.low) / R if s > 0 else (c.high - plan.entry) / R
    return fav, adv


def excursion_profile(plan: TradePlan, candles: Sequence[Candle], horizon_min: int) -> ExcursionProfile:
    max_r, mae, bars = 0.0, 0.0, 0
    end = plan.time + timedelta(minutes=horizon_min)
    for c in candles:
        if c.time <= plan.time:
            continue
        if c.time > end:
            return ExcursionProfile(round(max_r, 3), round(mae, 3), False, True, bars)
        bars += 1
        fav, adv = _fav_adv(plan, c)
        mae = max(mae, adv)
        if adv >= 1.0:  # tocou o stop inicial — conservador: não conta o favorável deste candle
            return ExcursionProfile(round(max_r, 3), round(min(mae, 1.0), 3), True, False, bars)
        max_r = max(max_r, fav)
    return ExcursionProfile(round(max_r, 3), round(mae, 3), False, False, bars)


def simulate_trade(plan: TradePlan, candles: Sequence[Candle], strategy: ExitStrategy, horizon_min: int) -> TradeResult:
    """Simula a operação candle a candle. Conservador: se stop e alvo cabem no mesmo candle, vale o stop."""
    s, R = plan.sign, plan.r_value or 1e-9
    stop_r = -1.0            # nível do stop em R (negativo = abaixo da entrada para compra)
    remaining = 1.0
    realized = 0.0
    peak = 0.0
    bars = 0
    end = plan.time + timedelta(minutes=horizon_min)
    partial_done = strategy.partial_r is None
    last_close_r = 0.0
    for c in candles:
        if c.time <= plan.time:
            continue
        if c.time > end:
            return TradeResult(strategy.name, round(realized + remaining * last_close_r, 3), "HORIZON", bars, c.time)
        bars += 1
        fav, adv = _fav_adv(plan, c)
        low_r = -adv  # extremo adverso em R (negativo)
        # 1) stop (inclui trailing/breakeven) — conservador, antes de qualquer alvo
        if low_r <= stop_r:
            reason = "STOP" if stop_r <= -1.0 + 1e-9 else ("PARTIAL+TRAIL" if not partial_done or strategy.partial_r else "TRAIL")
            return TradeResult(strategy.name, round(realized + remaining * stop_r, 3), reason, bars, c.time)
        # 2) parcial
        if not partial_done and strategy.partial_r is not None and fav >= strategy.partial_r:
            realized += remaining * strategy.partial_fraction * strategy.partial_r
            remaining *= 1.0 - strategy.partial_fraction
            stop_r = max(stop_r, 0.0)  # zero a zero
            partial_done = True
        # 3) alvo fixo
        if strategy.target_r is not None and fav >= strategy.target_r:
            return TradeResult(strategy.name, round(realized + remaining * strategy.target_r, 3), "TARGET", bars, c.time)
        # 4) trailing
        peak = max(peak, fav)
        if strategy.trail_r is not None and peak >= strategy.trail_activate_r:
            stop_r = max(stop_r, peak - strategy.trail_r)
        last_close_r = s * (c.close - plan.entry) / R
    return TradeResult(strategy.name, round(realized + remaining * last_close_r, 3), "OPEN", bars, None)


# --------------------------------------------------------------------------- estatísticas em R
@dataclass
class StrategyStats:
    name: str
    n: int
    expectancy_r: float
    win_rate: float
    profit_factor: Optional[float]
    avg_win_r: float
    avg_loss_r: float


@dataclass
class RStats:
    n: int
    dist: dict[str, int]                       # "stop antes de 1R" | "1R" | "2R" | "3R" | "+3R e continuou"
    reach: dict[str, float]                    # "1R" → fração que atingiu antes do stop
    reach_3r_before_stop: Optional[float]
    stop_before_1r: Optional[float]
    strategies: list[StrategyStats]
    best: Optional[str]
    mfe_r_median: Optional[float]
    mae_r_median: Optional[float]
    by_type: dict[str, dict[str, float]] = field(default_factory=dict)

    def render(self) -> str:
        if not self.n:
            return "🎯 TRADE SIMULATOR — sem operações resolvidas"
        lines = ["🎯 TRADE SIMULATOR — resultado em R", f"Operações: {self.n}", "Distribuição (até onde foi antes do stop):"]
        for k, v in self.dist.items():
            lines.append(f"  {k:<20} {v:>4}  {v / self.n:>5.0%}  {'█' * int(round(20 * v / self.n))}")
        lines.append("Atinge antes do stop: " + " · ".join(f"{k} {v:.0%}" for k, v in self.reach.items()))
        lines.append(f"3R antes do stop: {self.reach_3r_before_stop:.0%} · stop antes de 1R: {self.stop_before_1r:.0%}")
        lines.append(f"MFE mediana: {self.mfe_r_median:.2f}R · MAE mediana: {self.mae_r_median:.2f}R")
        lines.append("Estratégias de saída (expectativa em R por operação; win = resultado > 0, inclui saídas no horizonte):")
        for st in sorted(self.strategies, key=lambda x: -x.expectancy_r):
            pf = f"{st.profit_factor:.2f}" if st.profit_factor is not None else "∞"
            mark = " ◀ melhor" if st.name == self.best else ""
            lines.append(f"  {st.name:<12} E={st.expectancy_r:+.2f}R  win {st.win_rate:.0%}  PF {pf}  ganho médio {st.avg_win_r:+.2f}R  perda média {st.avg_loss_r:+.2f}R{mark}")
        for t, d in self.by_type.items():
            lines.append(f"  por sinal {t}: n={int(d['n'])} 3R antes do stop {d['reach_3r']:.0%} E({DEFAULT_STRATEGY})={d['expectancy']:+.2f}R")
        return "\n".join(lines)


def r_stats(rows: Sequence[dict]) -> RStats:
    """rows: {"type", "profile": ExcursionProfile, "results": {strategy_name: r_multiple}}."""
    n = len(rows)
    if not n:
        return RStats(0, {}, {}, None, None, [], None, None, None)
    dist = {"stop antes de 1R": 0, "expirou < 1R": 0, "1R": 0, "2R": 0, "3R": 0, "+3R e continuou": 0}
    for r in rows:
        m, pr = r["profile"].max_r_before_stop, r["profile"]
        key = ("stop antes de 1R" if pr.stopped else "expirou < 1R") if m < 1 else "1R" if m < 2 else "2R" if m < 3 else "3R" if m < 4 else "+3R e continuou"
        dist[key] += 1
    reach = {f"{k}R": sum(1 for r in rows if r["profile"].hit(k)) / n for k in (1, 2, 3, 4)}
    strategies: list[StrategyStats] = []
    names = sorted({k for r in rows for k in r["results"]})
    for name in names:
        rs = [r["results"][name] for r in rows if name in r["results"]]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        gross_w, gross_l = sum(wins), -sum(losses)
        strategies.append(StrategyStats(name, len(rs), round(statistics.fmean(rs), 3), len(wins) / len(rs),
                                        round(gross_w / gross_l, 2) if gross_l else None,
                                        round(statistics.fmean(wins), 2) if wins else 0.0, round(statistics.fmean(losses), 2) if losses else 0.0))
    best = max(strategies, key=lambda s: s.expectancy_r).name if strategies else None
    by_type: dict[str, dict[str, float]] = {}
    for t in sorted({r["type"] for r in rows}):
        sub = [r for r in rows if r["type"] == t]
        e = [r["results"].get(DEFAULT_STRATEGY, 0.0) for r in sub]
        by_type[t] = {"n": len(sub), "reach_3r": sum(1 for r in sub if r["profile"].hit(3)) / len(sub), "expectancy": statistics.fmean(e) if e else 0.0}
    return RStats(n, dist, reach, reach["3R"], dist["stop antes de 1R"] / n, strategies, best,
                  statistics.median(r["profile"].max_r_before_stop for r in rows), statistics.median(r["profile"].mae_r for r in rows), by_type)


# --------------------------------------------------------------------------- MAX PROFIT ENGINE
class MaxProfitEngine:
    """Não tenta prever o máximo absoluto. Calcula ALVO ESTATÍSTICO (histórico em R), ALVO
    ESTRUTURAL (próximo S/R), ALVO DE VOLATILIDADE (ATR × horizonte) e ALVO POR RISCO/RETORNO,
    estima a probabilidade de cada R e recomenda o TP com melhor expectativa histórica."""

    def __init__(self, stop_engine: Optional[StopEngine] = None, history: Optional[RStats] = None, horizon_min: int = 240,
                 min_rr_to_structure: float = 2.0) -> None:
        self.stop_engine = stop_engine or StopEngine()
        self.history = history
        self.horizon_min = horizon_min
        self.min_rr_to_structure = min_rr_to_structure

    def plan(self, a: Assessment, s: MarketSnapshot, direction: Direction, signal_type: str = "") -> TradePlan:
        atr = s.atr or (a.price * 0.003)
        stop, why = self.stop_engine.compute(a, direction, atr, s)
        plan = TradePlan(direction, a.price, stop, atr, a.time, signal_type=signal_type, evidence_level=int(a.evidence_level), confidence=a.confidence)
        plan.notes.append(why)
        for k in (1, 2, 3, 4):
            plan.targets[f"{k}R"] = round(plan.price_at_r(k), 2)
        # estrutural: próximo nível além da entrada
        lvl = a.zone.get("resistance") if direction == Direction.ALTA else a.zone.get("support")
        if lvl is not None and plan.sign * (lvl - a.price) > 0:
            plan.structural_target = lvl
            rr = plan.sign * (lvl - a.price) / (plan.r_value or 1)
            plan.structure_rr = round(rr, 2)
            plan.notes.append(f"alvo estrutural a {rr:.1f}R")
        # viabilidade do alvo: nível FORTE (H4/D1) antes do R:R mínimo → sem espaço estatístico
        strong = [(r.resistance if direction == Direction.ALTA else r.support) for r in a.technical if r.timeframe in ("H4", "D1")]
        # níveis a menos de 0.5 ATR já estão sendo testados/rompidos — não contam como barreira
        strong = [x for x in strong if x is not None and plan.sign * (x - a.price) > 0.5 * atr]
        if strong:
            near = min(strong, key=lambda x: abs(x - a.price))
            rr_strong = plan.sign * (near - a.price) / (plan.r_value or 1)
            if rr_strong < self.min_rr_to_structure:
                plan.viable = False
                plan.notes.append(f"⚠️ resistência/suporte forte (H4/D1) em {near:.2f} = {rr_strong:.1f}R, antes de {self.min_rr_to_structure:.0f}R → sem espaço estatístico para o alvo")
        # volatilidade: ATR H1 × sqrt(horas do horizonte)
        plan.volatility_target = round(a.price + plan.sign * atr * (self.horizon_min / 60) ** 0.5, 2)
        # estatístico + probabilidades por R (histórico)
        h = self.history
        if h and h.n >= 20:
            plan.statistical_target_r = h.mfe_r_median
            plan.probabilities = {k: h.reach.get(k) for k in ("1R", "2R", "3R", "4R")}
            if h.best:
                plan.recommended = h.best
                best = next(x for x in h.strategies if x.name == h.best)
                plan.recommended_reason = f"melhor expectativa histórica ({best.expectancy_r:+.2f}R em {best.n} operações)"
        else:
            plan.probabilities = {k: None for k in ("1R", "2R", "3R", "4R")}
            plan.notes.append("histórico insuficiente (< 20 operações) — mantida a hipótese 3R")
        return plan


# --------------------------------------------------------------------------- NO TRADE + risco
def no_trade_check(a: Assessment, limits: RiskLimits, spread: Optional[float] = None, min_confidence: float = 60.0,
                   min_level: int = 2) -> list[str]:
    """Fatores conflitantes, confiança baixa ou spread alto → 🟡 NÃO OPERAR. Lista vazia = pode operar."""
    reasons: list[str] = []
    if not a.has_edge:
        reasons.append("sem vantagem estatística")
    if a.confidence < min_confidence:
        reasons.append(f"confiança {a.confidence:.0f} < {min_confidence:.0f}")
    if int(a.evidence_level) < min_level:
        reasons.append(f"evidência nível {int(a.evidence_level)} < {min_level}")
    direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
    sign = 1.0 if direction == Direction.ALTA else -1.0
    against = [f.name for f in a.factors if f.available and sign * f.ratio <= -0.3]
    if len(against) >= 2:
        reasons.append(f"fatores conflitantes contra a direção: {', '.join(against)}")
    if a.premove.stage.value == "MOVIMENTO":
        reasons.append("movimento já ocorreu (não perseguir)")
    if spread is not None and spread > limits.max_spread:
        reasons.append(f"spread {spread:.2f} > máximo {limits.max_spread:.2f}")
    return reasons


@dataclass
class RiskManager:
    limits: RiskLimits
    equity: float
    day: Optional[str] = None
    daily_pnl: float = 0.0
    open_positions: int = 0

    def _roll_day(self, t: datetime) -> None:
        d = t.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.daily_pnl = d, 0.0

    def size(self, plan: TradePlan) -> TradePlan:
        """Lote a partir do risco fixo por operação — nunca da confiança."""
        risk_usd = self.equity * self.limits.risk_per_trade_pct / 100.0
        per_lot = plan.r_value * self.limits.contract_size
        lots = risk_usd / per_lot if per_lot > 0 else 0.0
        lots = min(self.limits.max_lot, max(0.0, lots))
        lots = round(int(lots / self.limits.lot_step) * self.limits.lot_step, 2)
        if lots < self.limits.min_lot:
            plan.notes.append(f"risco de {risk_usd:.2f} USD não comporta o lote mínimo com stop de {plan.r_value:.2f} USD")
            lots = 0.0
        plan.lots, plan.risk_usd = lots, round(lots * per_lot, 2)
        return plan

    def check(self, plan: TradePlan) -> list[str]:
        self._roll_day(plan.time)
        reasons: list[str] = []
        if self.open_positions >= self.limits.max_positions:
            reasons.append(f"posições abertas {self.open_positions} ≥ máximo {self.limits.max_positions}")
        if self.daily_pnl <= -self.equity * self.limits.max_daily_loss_pct / 100.0:
            reasons.append(f"perda diária {self.daily_pnl:.2f} USD atingiu o limite de {self.limits.max_daily_loss_pct:.1f}%")
        if not plan.lots:
            reasons.append("lote zero")
        return reasons

    def register(self, plan: TradePlan) -> None:
        self.open_positions += 1

    def close(self, plan: TradePlan, r_multiple: float, t: datetime) -> None:
        self._roll_day(t)
        self.open_positions = max(0, self.open_positions - 1)
        self.daily_pnl += r_multiple * (plan.risk_usd or 0.0)


# --------------------------------------------------------------------------- gestor de posição
@dataclass
class Decision:
    action: str                 # NO_TRADE | PAPER | AWAIT_AUTHORIZATION | SENT | BLOCKED
    plan: Optional[TradePlan]
    reasons: list[str] = field(default_factory=list)
    mode: TradingMode22 = TradingMode22.PAPER

    def render(self) -> str:
        head = {"NO_TRADE": "🟡 NÃO OPERAR", "PAPER": "🟡 PAPER — operação simulada registrada", "AWAIT_AUTHORIZATION": "🟠 AGUARDANDO AUTORIZAÇÃO",
                "SENT": "🔴 LIVE — ordem enviada ao MT5", "BLOCKED": "⛔ BLOQUEADA pelo gestor de risco"}[self.action]
        out = [head] + [f"  • {r}" for r in self.reasons]
        if self.plan is not None and self.action != "NO_TRADE":
            out.append(self.plan.render())
        return "\n".join(out)


class PositionManager:
    def __init__(self, mode: TradingMode22, limits: RiskLimits, equity: float, history: Optional[RStats] = None,
                 horizon_min: int = 240, executor=None) -> None:
        self.mode = mode
        self.risk = RiskManager(limits, equity)
        self.mpe = MaxProfitEngine(StopEngine(limits), history, horizon_min)
        self.executor = executor  # data.mt5.MT5Executor (apenas em LIVE / AUTHORIZE)
        self.authorized: bool = False

    EXECUTABLE = {SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL, SignalType.PRE_MOVE}

    def decide(self, sig: Signal, s: MarketSnapshot, spread: Optional[float] = None) -> Decision:
        a = sig.assessment
        if sig.type not in self.EXECUTABLE or sig.direction == Direction.LATERAL:
            return Decision("NO_TRADE", None, [f"sinal {sig.type.value} não é operacional"], self.mode)
        reasons = no_trade_check(a, self.risk.limits, spread)
        if reasons:
            return Decision("NO_TRADE", None, reasons, self.mode)
        plan = self.mpe.plan(a, s, sig.direction, sig.type.value)
        if not plan.viable:
            return Decision("NO_TRADE", plan, [n for n in plan.notes if n.startswith("⚠️")], self.mode)
        self.risk.size(plan)
        blocked = self.risk.check(plan)
        if blocked:
            return Decision("BLOCKED", plan, blocked, self.mode)
        if self.mode == TradingMode22.PAPER:
            self.risk.register(plan)
            return Decision("PAPER", plan, [], self.mode)
        if self.mode == TradingMode22.AUTHORIZE and not self.authorized:
            return Decision("AWAIT_AUTHORIZATION", plan, ["responda com autorização para enviar"], self.mode)
        if self.executor is None:
            return Decision("BLOCKED", plan, ["sem executor MT5 configurado"], self.mode)
        order = self.executor.send_plan(plan, authorize=True)
        self.risk.register(plan)
        return Decision("SENT", plan, [str(order)], self.mode)


def simulate_all(plan: TradePlan, candles: Sequence[Candle], horizon_min: int, strategies: Iterable[ExitStrategy] = STRATEGIES) -> dict:
    prof = excursion_profile(plan, candles, horizon_min)
    results = {st.name: simulate_trade(plan, candles, st, horizon_min) for st in strategies}
    return {"profile": prof, "results": {k: v.r_multiple for k, v in results.items()}, "details": results}


# ============================================================================
# MONITOR
# ============================================================================

"""GOLD AI ENGINE 2.3 — GOLD TRADE MONITOR + ADAPTIVE EXIT ENGINE.

Regra central: "A abertura de uma operação não encerra o processo de análise. Enquanto existir
posição aberta, o GOLD AI ENGINE deverá continuar recebendo dados de mercado, notícias,
macroeconomia, fluxo e indicadores técnicos, comparar o cenário atual com a tese original e
decidir continuamente entre MANTER, PROTEGER, REDUZIR ou ENCERRAR a posição."

                 OPERAÇÃO ABERTA → GOLD TRADE MONITOR → NOVO SCORE → COMPARAR COM TESE ORIGINAL
                 → MANTER (trailing) · PROTEGER (parcial) · REDUZIR · ESTENDER · ENCERRAR (invalidação)
"""





# --------------------------------------------------------------------------- tese e estado
@dataclass
class Thesis:
    """Fotografia da tese na entrada."""

    direction: Direction
    score: float                      # score assinado na direção da operação (-100..+100)
    pillars: dict[str, float]         # fatores alinhados (ratio ≥ 0.3) → ratio na entrada
    evidence_level: int
    confidence: float
    time: datetime

    @classmethod
    def from_assessment(cls, a: Assessment, direction: Direction) -> "Thesis":
        sign = 1.0 if direction == Direction.ALTA else -1.0
        pillars = {f.name: round(f.ratio, 3) for f in a.factors if f.available and sign * f.ratio >= 0.3}
        return cls(direction, sign * a.score, pillars, int(a.evidence_level), a.confidence, a.time)

    def to_dict(self) -> dict:
        return {"direction": self.direction.value, "score": self.score, "pillars": self.pillars, "evidence_level": self.evidence_level,
                "confidence": self.confidence, "time": self.time.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "Thesis":
        return cls(Direction(d["direction"]), d["score"], dict(d["pillars"]), d["evidence_level"], d["confidence"], datetime.fromisoformat(d["time"]))


@dataclass
class MonitorReading:
    time: datetime
    price: float
    current_r: float
    trade_score: float        # estado atual do mercado, assinado na direção da operação (-100..+100)
    thesis_score: float       # 0..100 — quanto da tese original permanece válida
    exit_score: float         # 0..100 — necessidade de encerrar
    profit_potential: float   # 0..100 — espaço estatisticamente favorável restante
    action: str               # MANTER | PROTEGER | REDUZIR | ESTENDER | ENCERRAR | STOP
    note: str = ""
    targets: dict[str, str] = field(default_factory=dict)  # "3R" → atingível/provável/possível/improvável


@dataclass
class ManagedTrade:
    trade_id: int
    plan: TradePlan
    thesis: Thesis
    remaining: float = 1.0
    realized_r: float = 0.0
    stop_r: float = -1.0
    peak_r: float = 0.0
    protected: bool = False
    extending: bool = False
    trail_r: float = 1.0
    status: str = "OPEN"          # OPEN | CLOSED
    close_reason: str = ""
    result_r: Optional[float] = None
    closed_at: Optional[datetime] = None
    history: list[MonitorReading] = field(default_factory=list)

    @property
    def sign(self) -> float:
        return self.plan.sign

    def r_at(self, price: float) -> float:
        return self.sign * (price - self.plan.entry) / (self.plan.r_value or 1e-9)

    def price_at_r(self, r: float) -> float:
        return self.plan.price_at_r(r)

    def state_dict(self) -> dict:
        return {"remaining": self.remaining, "realized_r": self.realized_r, "stop_r": self.stop_r, "peak_r": self.peak_r,
                "protected": self.protected, "extending": self.extending, "trail_r": self.trail_r}

    def load_state(self, d: dict) -> "ManagedTrade":
        for k, v in d.items():
            if hasattr(self, k):
                setattr(self, k, v)
        return self

    def close(self, r_exit: float, reason: str, t: datetime) -> float:
        self.result_r = round(self.realized_r + self.remaining * r_exit, 3)
        self.remaining, self.status, self.close_reason, self.closed_at = 0.0, "CLOSED", reason, t
        return self.result_r


# --------------------------------------------------------------------------- monitor
@dataclass
class MonitorConfig:
    exit_score_close: float = 70.0      # ≥ → ENCERRAR
    thesis_invalidated: float = 30.0    # THESIS SCORE < → ENCERRAR (mesmo com lucro)
    exit_score_reduce: float = 50.0     # ≥ e em lucro → REDUZIR (parcial + zero a zero)
    exit_score_protect: float = 35.0    # ≥ e ≥ 1R → stop no zero a zero
    protect_r: float = 2.0              # PROTEGER: parcial 50 % + trailing
    partial_fraction: float = 0.5
    trail_r: float = 1.0
    extend_trail_r: float = 1.5
    extend_min_potential: float = 60.0
    extend_score_gain: float = 5.0      # trade score ≥ tese + isto → ESTENDER


def adaptive_trail_r(trade_score: float, thesis_score: float, base_r: float = 1.0) -> float:
    """TRAILING INTELIGENTE: mercado forte → trailing mais largo; perdendo força → mais apertado."""
    strength = (max(-100.0, min(100.0, trade_score)) / 100.0 + thesis_score / 100.0) / 2.0  # -0.5..1
    if strength >= 0.7:
        return round(base_r * 1.5, 2)
    if strength >= 0.4:
        return base_r
    if strength >= 0.2:
        return round(base_r * 0.75, 2)
    return round(base_r * 0.5, 2)


class TradeMonitor:
    def __init__(self, cfg: Optional[MonitorConfig] = None, history: Optional[RStats] = None, adaptive_trailing: bool = True) -> None:
        self.cfg = cfg or MonitorConfig()
        self.history = history
        self.adaptive_trailing = adaptive_trailing

    # ------------------------------------------------------------------ 1. caminho do preço (stop/trailing)
    def check_path(self, tr: ManagedTrade, candles: Sequence[Candle]) -> Optional[MonitorReading]:
        """Verifica, candle a candle desde a última leitura, se o stop atual foi tocado (conservador)."""
        last = tr.history[-1].time if tr.history else tr.plan.time
        for c in candles:
            if c.time <= last:
                continue
            fav = tr.r_at(c.high) if tr.sign > 0 else tr.r_at(c.low)
            adv = tr.r_at(c.low) if tr.sign > 0 else tr.r_at(c.high)
            if adv <= tr.stop_r:
                reason = "STOP" if tr.stop_r <= -1.0 + 1e-9 else "TRAILING/PROTEÇÃO"
                res = tr.close(tr.stop_r, reason, c.time)
                reading = MonitorReading(c.time, tr.price_at_r(tr.stop_r), tr.stop_r, 0.0, 0.0, 100.0, 0.0, "STOP", f"{reason} tocado → resultado {res:+.2f}R")
                tr.history.append(reading)
                return reading
            tr.peak_r = max(tr.peak_r, fav)
            if tr.peak_r >= 1.0:  # trailing sempre ativo após 1R
                tr.stop_r = max(tr.stop_r, tr.peak_r - tr.trail_r)
        return None

    # ------------------------------------------------------------------ 2. scores
    def thesis_score(self, tr: ManagedTrade, a: Assessment) -> float:
        sign = tr.sign
        now = {f.name: f.ratio for f in a.factors if f.available}
        if not tr.thesis.pillars:
            base = 50.0
        else:
            tot = sum(DEFAULT_WEIGHTS.get(n, 5) for n in tr.thesis.pillars)
            kept = sum(DEFAULT_WEIGHTS.get(n, 5) for n in tr.thesis.pillars if sign * now.get(n, 0.0) >= 0.15)
            base = 100.0 * kept / tot if tot else 50.0
        if sign * a.score < 0:
            base -= 25.0
        return round(max(0.0, min(100.0, base)), 1)

    def trade_score(self, tr: ManagedTrade, a: Assessment) -> float:
        return round(tr.sign * a.score, 1)

    def exit_score(self, tr: ManagedTrade, a: Assessment, s: MarketSnapshot, thesis: float, trade: float, current_r: float) -> float:
        e = (100.0 - thesis) * 0.35
        e += max(0.0, -trade) * 0.30
        drop = tr.thesis.score - trade
        e += max(0.0, drop) / 100.0 * 25.0
        if a.reversal.current_trend == tr.thesis.direction:
            e += a.reversal.risk * 0.20
        if a.systemic_risk >= 75:
            e += 10.0
        if a.next_event is not None and current_r > 0:
            e += 10.0
        if a.premove.stage == Stage.PRE_MOVIMENTO and a.premove.direction != tr.thesis.direction and a.premove.direction != Direction.LATERAL:
            e += 15.0
        return round(max(0.0, min(100.0, e)), 1)

    def _cond_prob(self, current_r: float, target_r: float) -> Optional[float]:
        """P(atingir target | já atingiu floor(current)) a partir do histórico."""
        h = self.history
        if not h or h.n < 20:
            return None
        base_k = max(0, min(4, int(current_r)))
        p_base = 1.0 if base_k == 0 else h.reach.get(f"{base_k}R", 0.0)
        k = int(min(4, max(1, round(target_r))))
        p_t = h.reach.get(f"{k}R", 0.0)
        if p_base <= 0:
            return 0.0
        return max(0.0, min(1.0, p_t / p_base))

    def profit_potential(self, tr: ManagedTrade, a: Assessment, s: MarketSnapshot, trade: float, thesis: float, current_r: float) -> float:
        R = tr.plan.r_value or 1e-9
        lvl = a.zone.get("resistance") if tr.sign > 0 else a.zone.get("support")
        if lvl is not None and tr.sign * (lvl - a.price) > 0:
            room_r = tr.sign * (lvl - a.price) / R
        else:
            room_r = (s.atr or R) * 2.0 / R
        pot = min(1.0, room_r / 2.0) * 40.0
        pot += max(0.0, trade) / 100.0 * 30.0
        p_next = self._cond_prob(current_r, int(current_r) + 1)
        pot += (p_next if p_next is not None else 0.5 * thesis / 100.0) * 30.0
        return round(max(0.0, min(100.0, pot)), 1)

    def target_labels(self, current_r: float, potential: float) -> dict[str, str]:
        out: dict[str, str] = {}
        base = int(current_r) + 1
        for k in range(base, base + 3):
            p = self._cond_prob(current_r, k)
            if p is None:
                p = potential / 100.0 * (0.9 ** (k - base))
            out[f"{k}R"] = "atingível" if p >= 0.6 else "provável" if p >= 0.4 else "possível" if p >= 0.2 else "improvável"
        return out

    # ------------------------------------------------------------------ 3. decisão
    def evaluate(self, tr: ManagedTrade, a: Assessment, s: MarketSnapshot) -> MonitorReading:
        c = self.cfg
        price = a.price
        cur = tr.r_at(price)
        tr.peak_r = max(tr.peak_r, cur)
        thesis = self.thesis_score(tr, a)
        trade = self.trade_score(tr, a)
        ex = self.exit_score(tr, a, s, thesis, trade, cur)
        pot = self.profit_potential(tr, a, s, trade, thesis, cur)
        note, action = "", "MANTER"

        if ex >= c.exit_score_close or thesis < c.thesis_invalidated:
            action = "ENCERRAR"
            res = tr.close(cur, "TESE INVALIDADA" if thesis < c.thesis_invalidated else "EXIT SCORE", a.time)
            note = f"🔴 TESE INVALIDADA (thesis {thesis:.0f}, exit {ex:.0f}) → fechada a {cur:+.2f}R, resultado {res:+.2f}R"
        elif ex >= c.exit_score_reduce and cur >= 0.5 and tr.remaining >= 1.0 - 1e-9:
            action = "REDUZIR"
            tr.realized_r += tr.remaining * c.partial_fraction * cur
            tr.remaining *= 1.0 - c.partial_fraction
            tr.stop_r = max(tr.stop_r, 0.0)
            note = f"cenário deteriorando (exit {ex:.0f}) → {c.partial_fraction:.0%} realizado a {cur:+.2f}R, stop no zero a zero"
        elif ex >= c.exit_score_reduce and tr.remaining < 1.0:
            action = "REDUZIR"
            tr.stop_r = max(tr.stop_r, tr.peak_r - 0.5)
            note = f"exit {ex:.0f} com posição já reduzida → trailing apertado ({tr.stop_r:+.2f}R)"
        elif cur >= c.protect_r and not tr.protected:
            action = "PROTEGER"
            tr.realized_r += tr.remaining * c.partial_fraction * cur
            tr.remaining *= 1.0 - c.partial_fraction
            tr.stop_r = max(tr.stop_r, 0.0)
            tr.protected = True
            note = f"+{cur:.1f}R → {c.partial_fraction:.0%} protegido, {1 - c.partial_fraction:.0%} em trailing"
        elif cur >= 1.0 and tr.stop_r < 0 and ex >= c.exit_score_protect:
            action = "PROTEGER"
            tr.stop_r = 0.0
            note = f"exit {ex:.0f} com lucro → stop no zero a zero"
        elif tr.protected and trade >= tr.thesis.score + c.extend_score_gain and pot >= c.extend_min_potential:
            action = "ESTENDER"
            tr.extending, tr.trail_r = True, c.extend_trail_r
            note = f"cenário mais forte que a tese ({trade:+.0f} vs {tr.thesis.score:+.0f}) e potencial {pot:.0f} → buscar {int(cur) + 2}R/{int(cur) + 3}R com trailing {c.extend_trail_r:.1f}R"
        else:
            if self.adaptive_trailing and not tr.extending:
                tr.trail_r = adaptive_trail_r(trade, thesis, c.trail_r)
            if tr.peak_r >= 1.0:
                tr.stop_r = max(tr.stop_r, tr.peak_r - tr.trail_r)
            note = ("tese preservada" if thesis >= 60 else "tese parcialmente preservada — observar") + f" · trailing {tr.trail_r:.2f}R"
        reading = MonitorReading(a.time, price, round(cur, 3), trade, thesis, ex, pot, action, note, self.target_labels(cur, pot))
        tr.history.append(reading)
        return reading


# --------------------------------------------------------------------------- relatório
def render_monitor(tr: ManagedTrade, r: MonitorReading) -> str:
    side = "BUY" if tr.thesis.direction == Direction.ALTA else "SELL"
    pnl = tr.realized_r + tr.remaining * r.current_r if tr.status == "OPEN" else (tr.result_r or 0.0)
    status = {"MANTER": "🟢 MANTER", "PROTEGER": "🟡 PROTEGER", "REDUZIR": "🟠 REDUZIR", "ESTENDER": "🟢 ESTENDER", "ENCERRAR": "🔴 ENCERRAR", "STOP": "⛔ STOP"}[r.action]
    lines = ["GOLD TRADE MONITOR", f"Trade: #{tr.trade_id:05d}", f"{side} XAU/USD", f"Entrada {tr.plan.entry:.2f} · Stop atual {tr.price_at_r(tr.stop_r):.2f} ({tr.stop_r:+.2f}R)", "",
             f"Lucro: {pnl:+.2f}R", "", f"TRADE SCORE:       {r.trade_score:+.0f}", f"THESIS SCORE:      {r.thesis_score:.0f}/100",
             f"EXIT SCORE:        {r.exit_score:.0f}/100", f"PROFIT POTENTIAL:  {r.profit_potential:.0f}/100", "", "Status:", status]
    if r.targets and tr.status == "OPEN":
        lines += ["", "Alvo:"] + [f"{k} → {v}" for k, v in r.targets.items()]
    lines += ["", "Ação:", f"{tr.remaining:.0%} em posição" + (f" · {1 - tr.remaining:.0%} realizado ({tr.realized_r:+.2f}R)" if tr.remaining < 1 else "")]
    if r.note:
        lines.append(r.note)
    return "\n".join(lines)


def render_evolution(tr: ManagedTrade) -> str:
    lines = [f"Trade #{tr.trade_id:05d} — evolução do score"]
    t0 = tr.plan.time
    for h in tr.history:
        mins = (h.time - t0).total_seconds() / 60
        lines.append(f"  +{mins:.0f} min  score {h.trade_score:+.0f}  tese {h.thesis_score:.0f}  exit {h.exit_score:.0f}  {h.current_r:+.2f}R  {h.action}")
    if tr.status == "CLOSED":
        lines.append(f"  SAÍDA: {tr.close_reason} → {tr.result_r:+.2f}R")
    return "\n".join(lines)


# --------------------------------------------------------------------------- aprendizado empírico
def exit_learning(rows: Sequence[dict]) -> str:
    """rows: {"exit_reason", "result_r", "max_r_after" (até onde o preço foi depois, se conhecido),
    "thesis_at_exit", "drop_at_exit"}. Responde: qual deterioração do score realmente indica sair?"""
    if not rows:
        return "🧠 APRENDIZADO DE SAÍDA — sem operações gerenciadas encerradas"
    lines = ["🧠 APRENDIZADO DE SAÍDA — deterioração do score × resultado"]
    buckets = [("queda < 20", lambda d: d < 20), ("queda 20–40", lambda d: 20 <= d < 40), ("queda 40–60", lambda d: 40 <= d < 60), ("queda ≥ 60", lambda d: d >= 60)]
    for name, fn in buckets:
        sub = [r for r in rows if r.get("drop_at_exit") is not None and fn(r["drop_at_exit"])]
        if not sub:
            continue
        avg = sum(r["result_r"] for r in sub) / len(sub)
        after = [r["max_r_after"] - r["result_r"] for r in sub if r.get("max_r_after") is not None]
        left = f", deixado na mesa {sum(after) / len(after):+.2f}R" if after else ""
        lines.append(f"  {name:<12} n={len(sub):<3} resultado médio {avg:+.2f}R{left}")
    early = [r for r in rows if r.get("exit_reason") in ("TESE INVALIDADA", "EXIT SCORE")]
    if early:
        avg = sum(r["result_r"] for r in early) / len(early)
        saved = [r["result_r"] - r["min_r_after"] for r in early if r.get("min_r_after") is not None]
        lines.append(f"  saídas antecipadas: n={len(early)} resultado médio {avg:+.2f}R" + (f", evitado {sum(saved) / len(saved):+.2f}R de queda posterior" if saved else ""))
    return "\n".join(lines)


# ============================================================================
# MARKETS
# ============================================================================

"""MARKET AI ENGINE 4.0 — registro de mercados.

Cérebro único, múltiplos mercados: os fatores macro são calculados uma vez (na convenção do ouro,
"+ = favorável à alta") e cada mercado declara o SINAL com que cada fator o afeta.
    XAUUSD: dólar ↓ → +, juros reais ↓ → +, geopolítica ↑ → +
    EURUSD: dólar ↓ → +, juros reais ↓ → +, geopolítica ↑ (risk-off, USD) → −
    US500 : juros ↓ → +, Fed dovish → +, geopolítica ↑ → −, risco sistêmico → −
    USDJPY: dólar ↑ → +, juros ↑ → + (o oposto do ouro), geopolítica ↑ (JPY refúgio) → −
    WTI   : dólar ↓ → +, geopolítica ↑ → +, crescimento → +
Fatores sem relação conhecida ficam com sinal 0 (indisponíveis para aquele mercado) — nada de
inventar edge. O técnico e o fluxo vêm sempre dos candles do próprio mercado.
"""



# Grupos de exposição: (bucket, direção da exposição quando o mercado SOBE)
# USD_SHORT = o mercado sobe quando o dólar cai; RISK_ON = sobe com apetite a risco; OIL = petróleo.


@dataclass(frozen=True)
class MarketSpec:
    symbol: str                        # nome canônico (XAUUSD, EURUSD, US500, USDJPY, WTI)
    yahoo: str                         # símbolo Yahoo para candles
    mt5: str                           # símbolo típico no MT5 (ajustável por .env: MT5_SYMBOL_<symbol>)
    contract_size: float               # unidades por lote (USD por 1.0 de preço por lote = contract_size × valor do ponto)
    point_value_usd: float             # USD por 1.0 de variação de preço por lote padrão
    factor_signs: dict[str, int]       # fator → +1 / −1 / 0
    exposures: dict[str, float]        # bucket → peso (+ = mesma direção do mercado)
    cftc_code: Optional[str] = None    # COT (disaggregated/legacy) do próprio mercado
    typical_spread: float = 0.0        # em unidades de preço (custo de execução)
    min_stop_atr: float = 0.6
    max_stop_atr: float = 2.5
    session_hours_utc: tuple[int, int] = (0, 24)   # janela de liquidez principal
    phase: int = 1

    def risk_usd_per_lot(self, stop_distance: float) -> float:
        return stop_distance * self.point_value_usd


GOLD_SIGNS = {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": 1, "fluxo": 1, "cot": 1, "opcoes": 1, "sentimento": 1, "tecnico": 1}

MARKETS: dict[str, MarketSpec] = {
    "XAUUSD": MarketSpec("XAUUSD", "GC=F", "XAUUSD", 100.0, 100.0, GOLD_SIGNS, {"USD_SHORT": 0.6, "SAFE_HAVEN": 0.8}, "088691", 0.30),
    "EURUSD": MarketSpec("EURUSD", "EURUSD=X", "EURUSD", 100000.0, 100000.0,
                         {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                         {"USD_SHORT": 1.0, "RISK_ON": 0.3}, "099741", 0.00008),
    "US500": MarketSpec("US500", "ES=F", "US500", 1.0, 1.0,
                        {"dolar": 0, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 0, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                        {"RISK_ON": 1.0}, "13874A", 0.4, session_hours_utc=(13, 21)),
    "USDJPY": MarketSpec("USDJPY", "JPY=X", "USDJPY", 100000.0, 680.0,   # ≈ 100000 / 147 USD por 1.0 de preço
                         {"dolar": -1, "juros_reais": -1, "fed": -1, "inflacao": -1, "geopolitica": -1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                         {"USD_SHORT": -1.0, "RISK_ON": 0.5}, "097741", 0.012),
    "WTI": MarketSpec("WTI", "CL=F", "XTIUSD", 1000.0, 1000.0,
                      {"dolar": 1, "juros_reais": 0, "fed": 1, "inflacao": 0, "geopolitica": 1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                      {"OIL": 1.0, "USD_SHORT": 0.3, "RISK_ON": 0.3}, "067651", 0.03),
    # FASE 2
    "NAS100": MarketSpec("NAS100", "NQ=F", "NAS100", 1.0, 1.0,
                         {"dolar": 0, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 0, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                         {"RISK_ON": 1.0}, None, 1.5, session_hours_utc=(13, 21), phase=2),
    "GBPUSD": MarketSpec("GBPUSD", "GBPUSD=X", "GBPUSD", 100000.0, 100000.0,
                         {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                         {"USD_SHORT": 1.0, "RISK_ON": 0.4}, "096742", 0.00012, phase=2),
    # FASE 3 (não provar volatilidade como falso edge antes de generalizar)
    "BTCUSD": MarketSpec("BTCUSD", "BTC-USD", "BTCUSD", 1.0, 1.0,
                         {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 0, "geopolitica": 0, "fluxo": 1, "cot": 0, "opcoes": 0, "sentimento": 1, "tecnico": 1},
                         {"RISK_ON": 1.0, "USD_SHORT": 0.3}, None, 30.0, phase=3),
}

PHASE1 = ("EURUSD", "US500", "XAUUSD", "USDJPY", "WTI")

# Correlação estática de referência entre retornos (usada até haver correlação empírica suficiente).
DEFAULT_CORRELATION: dict[tuple[str, str], float] = {
    ("EURUSD", "GBPUSD"): 0.75, ("EURUSD", "XAUUSD"): 0.35, ("EURUSD", "USDJPY"): -0.30, ("EURUSD", "US500"): 0.20, ("EURUSD", "WTI"): 0.15,
    ("GBPUSD", "XAUUSD"): 0.30, ("GBPUSD", "USDJPY"): -0.25, ("GBPUSD", "US500"): 0.25,
    ("XAUUSD", "USDJPY"): -0.35, ("XAUUSD", "US500"): 0.05, ("XAUUSD", "WTI"): 0.20,
    ("USDJPY", "US500"): 0.35, ("USDJPY", "WTI"): 0.10,
    ("US500", "WTI"): 0.30, ("US500", "NAS100"): 0.90, ("US500", "BTCUSD"): 0.40, ("NAS100", "BTCUSD"): 0.45,
}


def correlation(a: str, b: str, table: Optional[dict[tuple[str, str], float]] = None) -> float:
    if a == b:
        return 1.0
    table = table or DEFAULT_CORRELATION
    return table.get((a, b), table.get((b, a), 0.0))


def get_market(symbol: str) -> MarketSpec:
    try:
        return MARKETS[symbol.upper()]
    except KeyError as e:
        raise KeyError(f"mercado desconhecido: {symbol} (disponíveis: {', '.join(MARKETS)})") from e


# ============================================================================
# NEWS_ENGINE
# ============================================================================

"""NEWS ENGINE — entrada central do cérebro (MARKET AI 4.0).

NEWS → EVENT IDENTIFIER → IMPORTÂNCIA → EXPECTATIVA → SURPRESA → DIREÇÃO ESPERADA (por mercado, via canais
de transmissão) → REAÇÃO REAL (mercado + canais) → DIVERGÊNCIA → PRESSÃO LATENTE → MARKET AI SCORE.

Conceito: NEWS ausente = UNKNOWN (peso reduzido, nunca negativo). NEWS favorável ↑ score, contrária ↓ score.
"""




# Canais de transmissão de cada tipo de evento quando o RESULTADO SUPERA a expectativa (+1 = canal sobe).
# Canais: yields (juros nominais/reais), dollar (DXY), risk (apetite a risco), oil (petróleo), safe_haven (refúgio).
TRANSMISSION: dict[str, dict[str, float]] = {
    "cpi":         {"yields": +1.0, "dollar": +0.8, "risk": -0.6},
    "core_cpi":    {"yields": +1.0, "dollar": +0.8, "risk": -0.6},
    "pce":         {"yields": +0.9, "dollar": +0.7, "risk": -0.5},
    "core_pce":    {"yields": +0.9, "dollar": +0.7, "risk": -0.5},
    "nfp":         {"yields": +0.8, "dollar": +0.7, "risk": +0.3},
    "earnings":    {"yields": +0.6, "dollar": +0.5, "risk": -0.2},
    "unemployment": {"yields": -0.7, "dollar": -0.6, "risk": -0.4},   # desemprego acima → economia fraca
    "jobless_claims": {"yields": -0.5, "dollar": -0.4, "risk": -0.3},
    "gdp":         {"yields": +0.5, "dollar": +0.4, "risk": +0.5},
    "ism":         {"yields": +0.5, "dollar": +0.4, "risk": +0.5},
    "pmi":         {"yields": +0.4, "dollar": +0.3, "risk": +0.5},
    "retail_sales": {"yields": +0.5, "dollar": +0.4, "risk": +0.4},
    "jolts":       {"yields": +0.4, "dollar": +0.3, "risk": +0.2},
    "consumer_confidence": {"yields": +0.2, "dollar": +0.2, "risk": +0.4},
    "michigan":    {"yields": +0.2, "dollar": +0.2, "risk": +0.3},
    "housing":     {"yields": +0.2, "dollar": +0.1, "risk": +0.2},
    "fomc_hawkish": {"yields": +1.0, "dollar": +0.9, "risk": -0.7},
    "fomc_dovish": {"yields": -1.0, "dollar": -0.9, "risk": +0.7},
    "geopolitical_escalation": {"yields": -0.3, "dollar": +0.5, "risk": -0.9, "oil": +0.7, "safe_haven": +1.0},
    "geopolitical_deescalation": {"yields": +0.2, "dollar": -0.3, "risk": +0.7, "oil": -0.5, "safe_haven": -0.8},
    "systemic_stress": {"yields": -0.8, "dollar": +0.4, "risk": -1.0, "safe_haven": +0.8},
    "oil_supply_cut": {"oil": +1.0, "yields": +0.2, "risk": -0.2},
    "oil_supply_increase": {"oil": -1.0, "risk": +0.1},
    "cb_gold_buying": {"safe_haven": +0.6},
    "china_stimulus": {"risk": +0.6, "oil": +0.4, "dollar": -0.2},
}

# Como cada canal afeta cada mercado (+1 = mercado sobe quando o canal sobe).
CHANNEL_TO_MARKET: dict[str, dict[str, float]] = {
    "XAUUSD": {"yields": -1.0, "dollar": -0.8, "risk": -0.2, "safe_haven": +1.0, "oil": +0.1},
    "EURUSD": {"yields": -0.6, "dollar": -1.0, "risk": +0.3},
    "GBPUSD": {"yields": -0.6, "dollar": -1.0, "risk": +0.4},
    "US500":  {"yields": -0.7, "dollar": -0.2, "risk": +1.0, "safe_haven": -0.3},
    "NAS100": {"yields": -0.9, "dollar": -0.2, "risk": +1.0, "safe_haven": -0.3},
    "USDJPY": {"yields": +1.0, "dollar": +1.0, "risk": +0.5, "safe_haven": -0.6},
    "WTI":    {"oil": +1.0, "dollar": -0.4, "risk": +0.4},
    "BTCUSD": {"yields": -0.6, "dollar": -0.4, "risk": +1.0},
}

IMPORTANCE = {"MUITO ALTO": 1.0, "ALTO": 0.8, "MÉDIO": 0.5, "BAIXO": 0.25}


@dataclass
class IdentifiedEvent:
    kind: str
    name: str
    time: datetime
    importance: float                 # 0..1
    expectation: Optional[float]      # consenso
    actual: Optional[float]
    surprise_sigma: Optional[float]   # surpresa normalizada (unidades de "desvio típico")
    direction_sign: float             # +1 resultado acima / evento "positivo" no canal; −1 abaixo; 0 neutro
    source: str = ""
    priced_in: float = 0.0
    headline: str = ""

    def age_min(self, now: datetime) -> float:
        return max(0.0, (now - self.time).total_seconds() / 60)


TYPICAL_SURPRISE = {"cpi": 0.1, "core_cpi": 0.1, "pce": 0.1, "core_pce": 0.1, "nfp": 60.0, "unemployment": 0.1, "earnings": 0.1, "gdp": 0.5,
                    "ism": 1.5, "pmi": 1.0, "retail_sales": 0.4, "jolts": 300.0, "jobless_claims": 15.0, "consumer_confidence": 3.0, "michigan": 2.0, "housing": 5.0}

QUALITATIVE = [
    (r"\b(hawkish|higher for longer|rate hike|hikes? rates?|tightening)\b", "fomc_hawkish", 0.8),
    (r"\b(dovish|rate cut|cuts? rates?|easing|pause)\b", "fomc_dovish", 0.8),
    (r"\b(war|missile|strike|attack|invasion|escalat\w*|sanction|nuclear|troops)\b", "geopolitical_escalation", 0.7),
    (r"\b(ceasefire|peace (deal|talks)|de-?escalat\w*|truce)\b", "geopolitical_deescalation", 0.6),
    (r"\b(bank (run|collapse|failure|rescue)|default|contagion|liquidity crisis|credit (stress|crunch)|bailout)\b", "systemic_stress", 0.8),
    (r"\b(opec\+? (cut|cuts)|supply (cut|disruption)|pipeline (attack|outage)|production cut)\b", "oil_supply_cut", 0.6),
    (r"\b(opec\+? (raise|increase|hike)|output increase|production increase|supply glut)\b", "oil_supply_increase", 0.6),
    (r"\b(pboc|central bank(s)? (buy|purchase|add)|reserves? (rise|increase))\w*", "cb_gold_buying", 0.5),
    (r"\b(china (stimulus|easing|cuts? rrr))\b", "china_stimulus", 0.5),
]


class EventIdentifier:
    """Transforma notícias/eventos em eventos identificados com importância, expectativa e surpresa."""

    def identify(self, news: Sequence[NewsItem], events: Sequence[EconomicEvent], now: datetime, max_age_hours: float = 12.0) -> list[IdentifiedEvent]:
        out: list[IdentifiedEvent] = []
        seen: set[str] = set()
        for e in events:
            if e.actual is None or now - e.time > timedelta(hours=max_age_hours) or e.time > now:
                continue
            sp = e.surprise()
            typical = TYPICAL_SURPRISE.get(e.kind, max(abs(e.consensus or 1.0) * 0.1, 0.1))
            sigma = (sp / typical) if sp is not None and typical else None
            key = f"{e.kind}:{e.time:%Y%m%d%H}"
            if key in seen:
                continue
            seen.add(key)
            out.append(IdentifiedEvent(e.kind, e.name, e.time, IMPORTANCE.get(e.impact, 0.6), e.consensus, e.actual, sigma,
                                       0.0 if sigma is None else (1.0 if sigma > 0 else -1.0 if sigma < 0 else 0.0), "calendário/release"))
        for n in news:
            if now - n.time > timedelta(hours=max_age_hours) or n.time > now:
                continue
            text = n.headline.lower()
            for pattern, kind, imp in QUALITATIVE:
                if re.search(pattern, text):
                    key = f"{kind}:{n.headline[:40].lower()}"
                    if key in seen:
                        break
                    seen.add(key)
                    out.append(IdentifiedEvent(kind, n.headline[:80], n.time, imp, None, None, None, 1.0, n.source, n.priced_in, n.headline))
                    break
        return sorted(out, key=lambda e: e.time, reverse=True)


def expected_direction(ev: IdentifiedEvent, market: str) -> tuple[float, dict[str, float]]:
    """Direção esperada (−1..+1) do mercado dado o evento, via canais de transmissão; devolve também os canais."""
    chans = TRANSMISSION.get(ev.kind, {})
    sign = ev.direction_sign if ev.direction_sign else 0.0
    if not chans or sign == 0.0:
        return 0.0, {}
    # magnitude: surpresa normalizada saturada (releases) ou 1 (qualitativos)
    mag = min(1.0, abs(ev.surprise_sigma) / 2.0) if ev.surprise_sigma is not None else 1.0
    channel_moves = {c: sign * v * mag for c, v in chans.items()}
    weights = CHANNEL_TO_MARKET.get(market, {})
    total = sum(channel_moves.get(c, 0.0) * w for c, w in weights.items())
    norm = sum(abs(w) for c, w in weights.items() if c in channel_moves) or 1.0
    return max(-1.0, min(1.0, total / norm)), channel_moves


@dataclass
class NewsAssessment:
    market: str
    status: str                        # UNKNOWN | FAVORÁVEL | CONTRÁRIO | NEUTRO
    pressure: Optional[float]          # −1..+1 (None = UNKNOWN)
    expected: float = 0.0              # direção esperada agregada
    reaction: str = ""                 # CONFIRMAÇÃO | DIVERGÊNCIA | SEM REAÇÃO | n/d
    channels: dict[str, str] = field(default_factory=dict)
    drivers: list[str] = field(default_factory=list)
    chain: str = ""


class NewsEngine:
    def __init__(self, half_life_min: float = 180.0) -> None:
        self.identifier = EventIdentifier()
        self.half_life_min = half_life_min

    def assess(self, market: str, s: MarketSnapshot, events: Sequence[IdentifiedEvent], now: datetime) -> NewsAssessment:
        if not events:
            return NewsAssessment(market, "UNKNOWN", None, 0.0, "n/d", {}, [], "NEWS: UNKNOWN — sem notícias/eventos identificados na janela (peso reduzido, não negativo)")
        total, wsum = 0.0, 0.0
        drivers: list[str] = []
        for ev in events:
            exp, chans = expected_direction(ev, market)
            if exp == 0.0:
                continue
            fresh = 0.5 ** (ev.age_min(now) / self.half_life_min)
            w = ev.importance * fresh * (1.0 - ev.priced_in)
            total += exp * w
            wsum += w
            arrow = "↑" if exp > 0 else "↓"
            sp = f" surpresa {ev.surprise_sigma:+.1f}σ" if ev.surprise_sigma is not None else ""
            drivers.append(f"{ev.name}{sp} → {market} {arrow} ({exp:+.2f}, imp {ev.importance:.1f}, {ev.age_min(now):.0f} min)")
        if wsum == 0.0:
            return NewsAssessment(market, "NEUTRO", 0.0, 0.0, "n/d", {}, drivers, f"NEWS: NEUTRO — eventos sem canal de transmissão para {market}")
        expected = max(-1.0, min(1.0, total / wsum))
        # reação real: mercado e canais
        def react(x: Optional[float], thr: float) -> Optional[float]:
            return None if x is None else (1.0 if x > thr else -1.0 if x < -thr else 0.0)
        mkt = react(s.price_change_pct, 0.12)
        chans = {"US10Y": react(s.us10y_change_bp, 1.5), "DXY": react(s.dxy_change_pct, 0.08)}
        exp_sign = 1.0 if expected > 0 else -1.0
        # canais esperados: sinal do canal 'yields'/'dollar' agregado
        exp_ch = {"US10Y": 0.0, "DXY": 0.0}
        for ev in events:
            _, cm = expected_direction(ev, market)
            exp_ch["US10Y"] += cm.get("yields", 0.0)
            exp_ch["DXY"] += cm.get("dollar", 0.0)
        ch_status = {}
        for k, v in chans.items():
            e = exp_ch[k]
            ch_status[k] = "n/d" if v is None else ("sem reação" if v == 0 else ("confirma" if (e == 0 or (v > 0) == (e > 0)) else "diverge"))
        if mkt is None:
            reaction = "n/d"
        elif mkt == 0:
            reaction = "SEM REAÇÃO"
        elif (mkt > 0) == (exp_sign > 0):
            reaction = "CONFIRMAÇÃO"
        else:
            reaction = "DIVERGÊNCIA"
        # pressão latente: canais confirmam e o mercado ainda não reagiu (ou diverge) → pressão na direção esperada
        channels_confirm = any(v == "confirma" for v in ch_status.values())
        if reaction in ("SEM REAÇÃO", "DIVERGÊNCIA", "n/d"):
            pressure = expected * (1.0 if channels_confirm else 0.7)
            note = "PRESSÃO LATENTE: canais " + ("confirmam" if channels_confirm else "ainda não confirmam") + f" e {market} {'não reagiu' if reaction != 'DIVERGÊNCIA' else 'diverge'}"
        else:
            pressure = expected * 0.5   # já reagiu: parte do efeito consumida
            note = f"{market} já reagiu na direção esperada (efeito parcialmente consumido)"
        status = "FAVORÁVEL" if pressure > 0.15 else "CONTRÁRIO" if pressure < -0.15 else "NEUTRO"
        chain = "\n".join([f"NEWS → {market}: {status} (pressão {pressure:+.2f}, esperado {expected:+.2f})"] + [f"  • {d}" for d in drivers[:5]] +
                          [f"  reação real: {market} {reaction} · " + " · ".join(f"{k} {v}" for k, v in ch_status.items()), f"  {note}"])
        return NewsAssessment(market, status, round(pressure, 3), round(expected, 3), reaction, ch_status, drivers, chain)


@dataclass
class FeedHealth:
    source: str
    ok: bool
    error: str = ""
    n_items: int = 0
    n_valid: int = 0
    n_discarded: int = 0
    last_update: Optional[datetime] = None

    def row(self) -> str:
        upd = self.last_update.strftime("%d/%m %H:%M") if self.last_update else "n/d"
        st = "✅" if self.ok else "✗"
        return f"  {st} {self.source:<32} atualização {upd:<12} notícias {self.n_items:>4} válidas {self.n_valid:>4} descartadas {self.n_discarded:>4}" + (f"  {self.error[:60]}" if self.error else "")


def render_feed_health(rows: Sequence[FeedHealth]) -> str:
    if not rows:
        return "📰 NEWS: nenhum feed configurado"
    ok = sum(1 for r in rows if r.ok)
    return "\n".join([f"📰 NEWS FEEDS: {ok}/{len(rows)} ok"] + [r.row() for r in rows])


# ============================================================================
# REACTION
# ============================================================================

"""REACTION ENGINE — EVENTO → REAÇÃO → TEMPO → PREVISÃO (MARKET AI 4.0).

Quando uma informação sai, o cronômetro começa. O motor mede, por evento e por ativo:
  TIME_TO_FIRST_REACTION · TIME_TO_CONFIRMATION · TIME_TO_FULL_MOVE · MAX_MOVE · MAX_ADVERSE_MOVE
e, para os canais líderes (USD, YIELDS), quanto tempo demoraram a reagir. Com a estatística por tipo de evento
("depois de CPI, o ouro leva em mediana X min para começar a reagir"), o RELÓGIO DE REAÇÃO detecta a assimetria
temporal: líderes já reagiram, alvo ainda não, tempo decorrido dentro da janela histórica → PRESSÃO LATENTE.

Regra: só aprende com eventos já CONCLUÍDOS antes do instante avaliado (known_at = published_at + horizonte).
Nunca usa a reação do próprio evento para decidir sobre ele. Não toca no Prediction Engine: entrega evidência.
"""


from bisect import bisect_right


FIRST_ATR = 0.15          # 1ª reação: movimento ≥ 0,15 ATR na direção esperada
CONFIRM_ATR = 0.40        # confirmação: ≥ 0,40 ATR
LEAD_THRESHOLDS = {"USD": 0.08, "YIELD": 1.5}   # DXY em %, US10Y em bp
MIN_N = 3                 # amostra mínima para usar a mediana de um tipo de evento


@dataclass
class ReactionRecord:
    event_id: str
    kind: str
    published_at: datetime
    target: str
    expected_dir: float                       # +1 / −1
    time_to_first: Optional[float] = None     # minutos; None = não reagiu no horizonte
    time_to_confirmation: Optional[float] = None
    time_to_full_move: Optional[float] = None
    max_move_atr: float = 0.0
    max_adverse_atr: float = 0.0
    direction_correct: Optional[bool] = None
    lead_times: dict[str, Optional[float]] = field(default_factory=dict)   # USD / YIELD → minutos
    horizon_min: int = 240
    resolution_min: int = 60                  # resolução da série usada (60 = H1; 5 = M5)

    @property
    def known_at(self) -> datetime:
        return self.published_at + timedelta(minutes=self.horizon_min)


def _at_or_before(series: Sequence[tuple[datetime, float]], t: datetime) -> Optional[float]:
    best = None
    for ts, v in series:
        if ts <= t:
            best = v
        else:
            break
    return best


def measure_reaction(event_id: str, kind: str, published_at: datetime, target: str, expected_dir: float,
                     target_series: Sequence[tuple[datetime, float]], atr: float,
                     leads: Optional[dict[str, Sequence[tuple[datetime, float]]]] = None, lead_dirs: Optional[dict[str, float]] = None,
                     horizon_min: int = 240, resolution_min: int = 60) -> Optional[ReactionRecord]:
    """Cronômetro do evento: a partir de published_at, quando o alvo (em ATR, na direção esperada) e os líderes reagiram."""
    if expected_dir == 0 or not atr or atr <= 0:
        return None
    p0 = _at_or_before(target_series, published_at)
    if p0 is None:
        return None
    end = published_at + timedelta(minutes=horizon_min)
    rec = ReactionRecord(event_id, kind, published_at, target, 1.0 if expected_dir > 0 else -1.0, horizon_min=horizon_min, resolution_min=resolution_min)
    sign = rec.expected_dir
    mfe, mae, t_full = 0.0, 0.0, None
    for ts, p in target_series:
        if ts <= published_at:
            continue
        if ts > end:
            break
        move = sign * (p - p0) / atr
        minutes = (ts - published_at).total_seconds() / 60
        if move > mfe:
            mfe, t_full = move, minutes
        if -move > mae:
            mae = -move
        if rec.time_to_first is None and move >= FIRST_ATR:
            rec.time_to_first = minutes
        if rec.time_to_confirmation is None and move >= CONFIRM_ATR:
            rec.time_to_confirmation = minutes
    rec.max_move_atr, rec.max_adverse_atr = round(mfe, 2), round(mae, 2)
    rec.time_to_full_move = t_full if mfe >= FIRST_ATR else None
    rec.direction_correct = (mfe >= CONFIRM_ATR and mfe > mae) if (mfe or mae) else None
    for name, series in (leads or {}).items():
        d = (lead_dirs or {}).get(name, 0.0)
        thr = LEAD_THRESHOLDS.get(name, 0.0)
        rec.lead_times[name] = None
        if d == 0 or not series:
            continue
        v0 = _at_or_before(series, published_at)
        if v0 is None:
            continue
        for ts, v in series:
            if ts <= published_at:
                continue
            if ts > end:
                break
            delta = (v / v0 - 1) * 100 if name == "USD" else (v - v0) * 100   # DXY em %, US10Y (em %) → bp
            if (1.0 if d > 0 else -1.0) * delta >= thr:
                rec.lead_times[name] = (ts - published_at).total_seconds() / 60
                break
    return rec


@dataclass
class KindStats:
    kind: str
    target: str
    n: int
    median_first: Optional[float]
    median_confirmation: Optional[float]
    median_full: Optional[float]
    p_first: float                 # fração que teve 1ª reação
    p_correct: float               # fração com direção confirmada
    median_mfe: float
    median_mae: float
    median_lead: dict[str, Optional[float]]
    resolution_min: int

    def row(self) -> str:
        f = lambda v: "  n/d" if v is None else f"{v:>5.0f}"  # noqa: E731
        leads = " ".join(f"{k} {f(v).strip()}" for k, v in self.median_lead.items())
        return (f"{self.kind:<22}{self.target:<8}{self.n:>4}{f(self.median_first):>8}{f(self.median_confirmation):>8}{f(self.median_full):>8}"
                f"{self.p_first:>8.0%}{self.p_correct:>8.0%}{self.median_mfe:>7.2f}{self.median_mae:>7.2f}  {leads}")


class ReactionStats:
    """Estatística point-in-time: `at(t)` só usa registros com known_at ≤ t."""

    def __init__(self, records: Sequence[ReactionRecord] = ()) -> None:
        self.records = sorted(records, key=lambda r: r.known_at)
        self._known = [r.known_at for r in self.records]
        self._cache: dict[int, dict[tuple[str, str], KindStats]] = {}

    def add(self, rec: ReactionRecord) -> None:
        self.records.append(rec)
        self.records.sort(key=lambda r: r.known_at)
        self._known = [r.known_at for r in self.records]
        self._cache.clear()

    @staticmethod
    def _aggregate(recs: Sequence[ReactionRecord]) -> dict[tuple[str, str], KindStats]:
        groups: dict[tuple[str, str], list[ReactionRecord]] = {}
        for r in recs:
            groups.setdefault((r.kind, r.target), []).append(r)
        out = {}
        for key, rs in groups.items():
            med = lambda xs: (statistics.median(xs) if xs else None)  # noqa: E731
            firsts = [r.time_to_first for r in rs if r.time_to_first is not None]
            confs = [r.time_to_confirmation for r in rs if r.time_to_confirmation is not None]
            fulls = [r.time_to_full_move for r in rs if r.time_to_full_move is not None]
            leads: dict[str, Optional[float]] = {}
            for name in sorted({k for r in rs for k in r.lead_times}):
                xs = [r.lead_times[name] for r in rs if r.lead_times.get(name) is not None]
                leads[name] = med(xs)
            out[key] = KindStats(key[0], key[1], len(rs), med(firsts), med(confs), med(fulls), len(firsts) / len(rs),
                                 sum(1 for r in rs if r.direction_correct) / len(rs), statistics.median([r.max_move_atr for r in rs]),
                                 statistics.median([r.max_adverse_atr for r in rs]), leads, max(r.resolution_min for r in rs))
        return out

    def at(self, t: datetime) -> dict[tuple[str, str], KindStats]:
        n = bisect_right(self._known, t)
        if n not in self._cache:
            self._cache[n] = self._aggregate(self.records[:n])
        return self._cache[n]

    def get(self, kind: str, target: str, t: datetime) -> Optional[KindStats]:
        return self.at(t).get((kind, target))

    def render(self, t: Optional[datetime] = None, title: str = "⏱️ REACTION ENGINE — tempo de reação por tipo de evento (medianas, minutos)") -> str:
        table = self.at(t) if t else self._aggregate(self.records)
        if not table:
            return f"{title}\n  sem eventos concluídos ainda"
        head = f"{'evento':<22}{'alvo':<8}{'n':>4}{'1ªreaç':>8}{'confirm':>8}{'pleno':>8}{'P(reag)':>8}{'P(dir)':>8}{'MFE':>7}{'MAE':>7}  líderes (min)"
        rows = [v.row() for _, v in sorted(table.items(), key=lambda kv: (-kv[1].n, kv[0]))]
        res = max(v.resolution_min for v in table.values())
        note = f"  resolução {res} min: tempos são múltiplos do candle usado (H1 = horas; para minutos use histórico M5/M1 ou o live)"
        return "\n".join([title, head] + rows + [note, "  MFE/MAE em ATR no horizonte; P(dir) = fração com confirmação ≥ 0,40 ATR na direção esperada"])


@dataclass
class ReactionAssessment:
    market: str
    status: str                          # SEM EVENTO | AGUARDANDO | PRESSÃO LATENTE | REAGIU | DIVERGÊNCIA | EXPIRADO | SEM HISTÓRICO
    pressure: float = 0.0                # −1..+1
    probability: Optional[float] = None  # prob. estimada de o alvo confirmar a direção esperada
    latency_min: Optional[float] = None  # tempo decorrido desde o evento
    expected_min: Optional[float] = None # mediana histórica da 1ª reação do alvo
    full_move_min: Optional[float] = None
    lead: dict[str, str] = field(default_factory=dict)
    target_reaction: str = ""
    event: str = ""
    n_history: int = 0
    chain: str = ""


class ReactionClock:
    def __init__(self, stats: Optional[ReactionStats] = None) -> None:
        self.stats = stats or ReactionStats()

    def assess(self, market: str, s: MarketSnapshot, events: Sequence[IdentifiedEvent], now: datetime) -> ReactionAssessment:
        cands = []
        for ev in events:
            exp, chans = expected_direction(ev, market)
            if exp == 0.0:
                continue
            cands.append((ev, exp, chans))
        if not cands:
            return ReactionAssessment(market, "SEM EVENTO", chain="REACTION: sem evento com direção esperada na janela")
        ev, exp, chans = max(cands, key=lambda c: (c[0].importance * (1 if c[0].surprise_sigma is None else min(2.0, abs(c[0].surprise_sigma))), c[0].time))
        sign = 1.0 if exp > 0 else -1.0
        elapsed = ev.age_min(now)
        ks = self.stats.get(ev.kind, market, now)
        n_hist = ks.n if ks else 0
        expected_min = ks.median_first if ks and ks.n >= MIN_N else None
        full_min = ks.median_full if ks and ks.n >= MIN_N else None
        window = (2.0 * full_min) if full_min else 240.0
        # líderes: canais dollar/yields esperados × observados
        lead: dict[str, str] = {}
        exp_usd, exp_y = chans.get("dollar", 0.0), chans.get("yields", 0.0)
        for name, e, obs, thr in (("USD", exp_usd, s.dxy_change_pct, LEAD_THRESHOLDS["USD"]), ("YIELD", exp_y, s.us10y_change_bp, LEAD_THRESHOLDS["YIELD"])):
            if e == 0.0:
                continue
            if obs is None:
                lead[name] = "n/d"
            elif abs(obs) < thr:
                lead[name] = "ainda não"
            elif (obs > 0) == (e > 0):
                lead[name] = "✓ reagiu"
            else:
                lead[name] = "✗ contra"
        # alvo: movimento DESDE O EVENTO (preço no último candle ≤ instante do evento, só passado); sem candles, usa a variação da janela
        move_atr = 0.0
        if s.atr and s.price:
            p0 = None
            for tf in ("M5", "M15", "H1"):
                cs = s.candles.get(tf) or []
                past = [c for c in cs if c.time <= ev.time]
                if past:
                    p0 = past[-1].close
                    break
            move_atr = ((s.price - p0) / s.atr * sign) if p0 else ((s.price_change_pct or 0.0) / 100.0 * s.price / s.atr) * sign
        if move_atr >= CONFIRM_ATR:
            target = "confirmou"
        elif move_atr >= FIRST_ATR:
            target = "iniciou"
        elif move_atr <= -FIRST_ATR:
            target = "contra"
        else:
            target = "ainda não"
        leads_ok = sum(1 for v in lead.values() if v == "✓ reagiu")
        leads_against = sum(1 for v in lead.values() if v == "✗ contra")
        # estado
        if target == "confirmou":
            status, strength = "REAGIU", 0.3
        elif target == "contra" or (leads_against and not leads_ok):
            status, strength = "DIVERGÊNCIA", 0.0
        elif leads_ok and elapsed <= window:
            status, strength = "PRESSÃO LATENTE", 1.0 if target == "ainda não" else 0.8
        elif leads_ok:
            status, strength = "EXPIRADO", 0.0
        elif elapsed <= window:
            status, strength = "AGUARDANDO", 0.4
        else:
            status, strength = "EXPIRADO", 0.0
        if not ks or ks.n < MIN_N:
            hist_note = f"sem histórico suficiente para {ev.kind}→{market} (n={n_hist}; mínimo {MIN_N}) — janela padrão 240 min"
        else:
            hist_note = f"histórico {ev.kind}→{market}: 1ª reação mediana {ks.median_first:.0f} min, pleno {ks.median_full:.0f} min, P(dir) {ks.p_correct:.0%} (n={ks.n})" \
                if ks.median_first is not None and ks.median_full is not None else f"histórico {ev.kind}→{market}: n={ks.n}, P(reação) {ks.p_first:.0%}, P(dir) {ks.p_correct:.0%}"
        # probabilidade estimada: base histórica ajustada pela evidência atual (nunca inventada: sem histórico, 0,5 ± evidência)
        base = ks.p_correct if ks and ks.n >= MIN_N else 0.5
        prob = base + 0.08 * leads_ok - 0.15 * leads_against + (0.05 if status == "PRESSÃO LATENTE" else 0.0) - (0.25 if target == "contra" else 0.0)
        if status == "EXPIRADO":
            prob = min(prob, 0.45)
        prob = max(0.05, min(0.95, prob))
        pressure = round(sign * strength * (0.6 + 0.4 * (prob if prob else 0.5)), 3) if strength else 0.0
        icon = {"PRESSÃO LATENTE": "🔥", "AGUARDANDO": "⏳", "REAGIU": "✅", "DIVERGÊNCIA": "⚠️", "EXPIRADO": "⌛"}.get(status, "")
        sp = f" surpresa {ev.surprise_sigma:+.1f}σ" if ev.surprise_sigma is not None else ""
        lines = [f"REACTION CLOCK → {market}: {icon} {status} · T+{elapsed:.0f} min · {ev.name[:50]}{sp} → {market} {'↑' if sign > 0 else '↓'}",
                 "  líderes: " + (" · ".join(f"{k} {v}" for k, v in lead.items()) or "sem canal líder") + f" · alvo {market}: {target} ({move_atr:+.2f} ATR)",
                 f"  {hist_note}"]
        if status == "PRESSÃO LATENTE":
            lines.append(f"  assimetria temporal: líderes reagiram, {market} não; decorrido {elapsed:.0f} min" +
                         (f" vs mediana {expected_min:.0f} min" if expected_min else "") + f" · prob. estimada {prob:.0%}")
        return ReactionAssessment(market, status, pressure, round(prob, 2), round(elapsed, 1), expected_min, full_min, lead, target, ev.name, n_hist, "\n".join(lines))


# --------------------------------------------------------------------------- aprendizado a partir do banco histórico
def records_from_history(hist, symbol: str, target_series: Sequence[tuple[datetime, float]], atr_at, leads: Optional[dict] = None,
                         horizon_min: int = 240, resolution_min: int = 60) -> list[ReactionRecord]:
    """Um ReactionRecord por evento do banco com direção esperada para `symbol` (regra macro). `atr_at(t)` → ATR do alvo em t."""

    out = []
    for e in hist.events:
        if e.revised is not None:
            continue
        sign, sigma = 0.0, None
        if e.surprise is not None:
            sign = 1.0 if e.surprise > 0 else -1.0 if e.surprise < 0 else 0.0
            typ = TYPICAL.get(e.kind, max(abs(e.forecast or e.previous or 1.0) * 0.1, 0.1))
            sigma = e.surprise / typ if typ else None
        elif e.kind in TRANSMISSION and e.actual is None:
            sign = 1.0
        if sign == 0.0:
            continue
        exp = rule_direction(e.kind, sign, sigma, symbol)
        if exp == 0.0:
            continue
        chans = TRANSMISSION.get(e.kind) or EXTRA_TRANSMISSION.get(e.kind) or {}
        lead_dirs = {"USD": sign * chans.get("dollar", 0.0), "YIELD": sign * chans.get("yields", 0.0)}
        atr = atr_at(e.published_at)
        if not atr:
            continue
        rec = measure_reaction(e.event_id, e.kind, e.published_at, symbol, exp, target_series, atr, leads, lead_dirs, horizon_min, resolution_min)
        if rec:
            out.append(rec)
    return out


# ============================================================================
# REACTION_HIRES
# ============================================================================

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

    def __init__(self, delay_sec: int = 5, p_min: float = 0.55, min_n: int = 3, slippage_atr: float = 0.02, latency_sec: float = 0.5) -> None:
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
            if sign * (q_in.mid - q0.mid) / atr >= FIRST_ATR:
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

    @property
    def edge_score(self) -> float:
        return VERDICT_SCORE[self.verdict]

    def row(self) -> str:
        label = {"🟢": "forte", "🟡": "moderado", "🔴": "sem edge", "⚪": "inconclusivo"}[self.verdict]
        return (f"{self.symbol:<8}{self.verdict} {label:<13}{self.n_events:>6}{self.n:>7}{self.net:>+9.3f}{self.net / STOP_ATR:>+7.2f}R{self.naive:>+9.3f}"
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
        if net >= 0.10 and net > naive:
            v = "🟢"
        elif net > 0.02:
            v = "🟡"
        else:
            v = "🔴"
        out.append(AssetVerdict(sym, v, n, net, naive, delay, ex, n_ev, kinds))
    return sorted(out, key=lambda v: (-VERDICT_SCORE[v.verdict], -v.net))


def render_verdicts(verdicts: Sequence[AssetVerdict], resolution: str) -> str:
    head = f"{'ativo':<8}{'veredito':<16}{'evts':>6}{'entr':>7}{'líquido':>9}{'':>8}{'ingênua':>9}{'atraso':>8} {'saída':<7}{'tipos':>6}"
    lines = [f"🏁 REACTION EDGE POR ATIVO ({resolution}) — o relógio não funciona igual em todos os mercados", head] + [v.row() for v in verdicts]
    lines.append("   🟢 forte: n ≥ 20, líquido ≥ 0,10 ATR (0,2R) e acima da ingênua · 🟡 moderado: líquido > 0,02 ATR · 🔴 custo consome · ⚪ n < 20")
    lines.append("   'melhor combinação' atraso × saída por ativo; o Asset Selector usa este veredito (reaction_edge.json) só quando o relógio marca PRESSÃO LATENTE")
    return "\n".join(lines)


def save_reaction_edge(verdicts: Sequence[AssetVerdict], path: str, resolution: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {v.symbol: {"verdict": v.verdict, "edge_score": v.edge_score, "n": v.n, "net_atr": round(v.net, 4), "naive_atr": round(v.naive, 4),
                       "delay_sec": v.delay_sec, "exit": v.exit, "n_events": v.n_events, "resolution": resolution,
                       "generated_at": datetime.now(timezone.utc).isoformat()} for v in verdicts}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def load_reaction_edge(path: str) -> dict[str, float]:
    """symbol → edge_score (0..1) só para vereditos com amostra (🟢/🟡/🔴); ⚪ é ignorado (o selector fica neutro)."""
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
             f"{'ativo':<8}{'TICK':<16}{'n':>5}{'líq.':>8}{'M1':<16}{'n':>5}{'líq.':>8}  conclusão"]
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
        fa = (f"{a.n:>5}{a.net / STOP_ATR:>+7.2f}R" if a else f"{'':>5}{'':>8}")
        fb = (f"{b.n:>5}{b.net / STOP_ATR:>+7.2f}R" if b else f"{'':>5}{'':>8}")
        lines.append(f"{sym:<8}{label[va]:<16}{fa}{label[vb]:<16}{fb}  {concl}")
    return "\n".join(lines)


# ============================================================================
# HISTORY
# ============================================================================

"""BANCO HISTÓRICO DE EVENTOS/NOTÍCIAS — point-in-time (MARKET AI 4.0).

O MARKET AI só pode ver, em cada instante do backtest, aquilo que estava publicado naquele momento:
  • `timestamp`     = quando o evento ocorreu/saiu (ex.: CPI 12:30 UTC);
  • `published_at`  = quando a informação ficou disponível (revisões entram como linhas novas, publicadas depois).
Nada com published_at > t é visível em t. É isso que evita look-ahead por revisão.

Esquema do CSV (dados/noticias_historicas.csv):
timestamp,published_at,event_id,event,country,currency,impact,forecast,previous,actual,revised,surprise,category,kind,source,headline,sentiment,
xau_effect,us500_effect,eurusd_effect,usdjpy_effect,wti_effect,effect_source
Os efeitos por ativo nascem de regras macro (news_engine) e são substituídos pelo que o histórico mostrar (`history learn`).
"""




EFFECT_MARKETS = ("XAUUSD", "US500", "EURUSD", "USDJPY", "WTI")
EFFECT_FIELD = {"XAUUSD": "xau_effect", "US500": "us500_effect", "EURUSD": "eurusd_effect", "USDJPY": "usdjpy_effect", "WTI": "wti_effect"}
IMPACT_MAP = {"HIGH": "MUITO ALTO", "MEDIUM": "MÉDIO", "LOW": "BAIXO", "3": "MUITO ALTO", "2": "MÉDIO", "1": "BAIXO",
              "MUITO ALTO": "MUITO ALTO", "ALTO": "ALTO", "MÉDIO": "MÉDIO", "BAIXO": "BAIXO"}

# nome do evento (normalizado) → kind do motor
KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("core cpi", "core_cpi"), ("core inflation", "core_cpi"), ("cpi", "cpi"), ("inflation rate", "cpi"), ("core pce", "core_pce"), ("pce", "pce"),
    ("personal consumption", "pce"), ("ppi", "ppi"), ("producer price", "ppi"), ("interest rate decision", "fomc"), ("non farm", "nfp"), ("nonfarm", "nfp"),
    ("payroll", "nfp"), ("unemployment rate", "unemployment"), ("jobless claims", "jobless_claims"), ("initial claims", "jobless_claims"),
    ("gdp", "gdp"), ("ism manufacturing", "ism"), ("ism services", "ism"), ("ism non-manufacturing", "ism"), ("pmi", "pmi"), ("retail sales", "retail_sales"),
    ("jolts", "jolts"), ("consumer confidence", "consumer_confidence"), ("michigan", "michigan"), ("housing", "housing"), ("building permits", "housing"),
    ("average hourly earnings", "earnings"), ("fomc", "fomc"), ("fed interest rate", "fomc"), ("federal funds", "fomc"), ("fed chair", "speech"),
    ("powell", "speech"), ("ecb", "ecb"), ("boj", "boj"), ("bank of japan", "boj"), ("crude oil inventories", "oil_inventories"), ("eia", "oil_inventories"),
    ("opec", "oil_supply_cut"), ("china", "china"),
)
# eventos sem sensibilidade direta na tabela do motor recebem uma transmissão própria aqui (unidade: acima do consenso)
EXTRA_TRANSMISSION = {
    "ppi": {"yields": +0.7, "dollar": +0.5, "risk": -0.4},
    "oil_inventories": {"oil": -0.8},           # estoques ACIMA do esperado → petróleo cai
    "ecb": {"dollar": -0.5, "yields": +0.2},    # BCE hawkish (acima) → euro sobe → dólar cai
    "boj": {"dollar": -0.3, "yields": +0.2},
    "china": {"risk": +0.4, "oil": +0.3},
}
TYPICAL = {"cpi": 0.1, "core_cpi": 0.1, "pce": 0.1, "core_pce": 0.1, "ppi": 0.2, "nfp": 60.0, "unemployment": 0.1, "jobless_claims": 15.0, "gdp": 0.5,
           "ism": 1.5, "pmi": 1.0, "retail_sales": 0.4, "jolts": 300.0, "consumer_confidence": 3.0, "michigan": 2.0, "housing": 5.0, "earnings": 0.1,
           "oil_inventories": 2.0, "fomc": 0.25, "ecb": 0.25, "boj": 0.1}


def kind_from_name(name: str) -> str:
    n = (name or "").lower()
    for pat, kind in KIND_PATTERNS:
        if pat in n:
            return kind
    return "generic"


def _num(x) -> Optional[float]:
    if x is None or str(x).strip() in ("", "n/d", "nan", "None"):
        return None
    s = str(x).strip().replace("%", "").replace("K", "").replace("k", "").replace("M", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class HistoricalEvent:
    timestamp: datetime
    published_at: datetime
    event_id: str
    event: str
    country: str = "US"
    currency: str = "USD"
    impact: str = "ALTO"
    forecast: Optional[float] = None
    previous: Optional[float] = None
    actual: Optional[float] = None
    revised: Optional[float] = None
    surprise: Optional[float] = None
    category: str = "MACRO"          # MACRO | CENTRAL_BANK | NEWS | GEOPOLITICAL | ENERGY | CHINA
    kind: str = "generic"
    source: str = ""
    headline: str = ""
    sentiment: str = ""              # POSITIVE | NEGATIVE | NEUTRAL (tom da mídia, GDELT)
    xau_effect: Optional[float] = None
    us500_effect: Optional[float] = None
    eurusd_effect: Optional[float] = None
    usdjpy_effect: Optional[float] = None
    wti_effect: Optional[float] = None
    effect_source: str = "rule"      # rule | empirical
    surprise_basis: str = "forecast" # forecast (vs consenso) | previous (sem consenso: vs dado anterior)
    tone: Optional[float] = None     # GDELT tone (−100..+100)
    volume: Optional[float] = None   # GDELT volume (% da cobertura)

    def __post_init__(self) -> None:
        if self.surprise is None and self.actual is not None:
            if self.forecast is not None:
                self.surprise = round(self.actual - self.forecast, 4)
            elif self.previous is not None and self.surprise_basis == "previous":
                self.surprise = round(self.actual - self.previous, 4)
        if self.forecast is None and self.previous is not None and self.surprise is not None and self.surprise_basis == "forecast":
            self.surprise_basis = "previous"
        if self.kind == "generic" and self.category in ("MACRO", "CENTRAL_BANK"):
            self.kind = kind_from_name(self.event)     # manchete (NEWS/GEOPOLITICAL/…) nunca vira release macro

    def effect(self, market: str) -> Optional[float]:
        return getattr(self, EFFECT_FIELD.get(market.upper(), "_"), None)

    def to_economic_event(self) -> EconomicEvent:
        consensus = self.forecast if self.forecast is not None else (self.previous if self.surprise_basis == "previous" else None)
        return EconomicEvent(self.event, self.timestamp, IMPACT_MAP.get(str(self.impact).upper(), self.impact or "ALTO"), consensus, self.previous,
                             self.actual, self.kind, "")

    def to_news_item(self) -> NewsItem:
        cat = {"GEOPOLITICAL": "geopolitical", "CENTRAL_BANK": "fed", "ENERGY": "generic", "CHINA": "china", "NEWS": "generic"}.get(self.category, "macro")
        impact = 0.0
        if self.xau_effect is not None:
            impact = max(-1.0, min(1.0, self.xau_effect))
        elif self.sentiment == "POSITIVE":
            impact = 0.3
        elif self.sentiment == "NEGATIVE":
            impact = -0.3
        return NewsItem(self.headline or self.event, self.source, self.timestamp, cat, impact, 0.0, f"{self.kind} · {self.category}")

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_row(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            d[f.name] = v.isoformat() if isinstance(v, datetime) else ("" if v is None else v)
        return d

    @classmethod
    def from_row(cls, r: dict) -> "HistoricalEvent":
        def dt(x):
            t = datetime.fromisoformat(str(x).replace("Z", "+00:00").replace(" ", "T"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        ts = dt(r["timestamp"])
        pub = dt(r["published_at"]) if r.get("published_at") else ts
        num = lambda k: _num(r.get(k))  # noqa: E731
        return cls(ts, pub, r.get("event_id") or f"{r.get('event', 'EV')}_{ts:%Y%m%d%H%M}", r.get("event", ""), r.get("country", "US"), r.get("currency", "USD"),
                   r.get("impact", "ALTO"), num("forecast"), num("previous"), num("actual"), num("revised"), num("surprise"), r.get("category", "MACRO"),
                   r.get("kind") or "generic", r.get("source", ""), r.get("headline", ""), r.get("sentiment", ""), num("xau_effect"), num("us500_effect"),
                   num("eurusd_effect"), num("usdjpy_effect"), num("wti_effect"), r.get("effect_source") or "rule", r.get("surprise_basis") or "forecast", num("tone"), num("volume"))


class EventHistory:
    def __init__(self, events: Sequence[HistoricalEvent] = ()) -> None:
        self.events: list[HistoricalEvent] = sorted(events, key=lambda e: e.published_at)

    def __len__(self) -> int:
        return len(self.events)

    def add(self, ev: HistoricalEvent) -> None:
        if any(e.event_id == ev.event_id and e.published_at == ev.published_at for e in self.events):
            return
        self.events.append(ev)
        self.events.sort(key=lambda e: e.published_at)

    def available_at(self, t: datetime, lookback_hours: float = 24.0) -> list[HistoricalEvent]:
        """POINT-IN-TIME: só o que estava publicado em t (published_at ≤ t) e ocorreu nas últimas `lookback_hours`."""
        lo = t - timedelta(hours=lookback_hours)
        out = [e for e in self.events if e.published_at <= t and lo <= e.timestamp <= t]
        # revisão: para o mesmo event_id fica a linha publicada mais recentemente até t
        latest: dict[str, HistoricalEvent] = {}
        for e in out:
            latest[e.event_id] = e
        return sorted(latest.values(), key=lambda e: e.timestamp)

    def upcoming_at(self, t: datetime, ahead_hours: float = 48.0) -> list[HistoricalEvent]:
        """Eventos futuros já agendados (consenso conhecido, sem actual) — calendário de risco."""
        hi = t + timedelta(hours=ahead_hours)
        return [HistoricalEvent(e.timestamp, e.published_at, e.event_id, e.event, e.country, e.currency, e.impact, e.forecast, e.previous, None, None, None,
                                e.category, e.kind, e.source) for e in self.events if t < e.timestamp <= hi]

    def snapshot_inputs(self, t: datetime, lookback_hours: float = 24.0) -> tuple[list[EconomicEvent], list[NewsItem]]:
        avail = self.available_at(t, lookback_hours)
        events = [e.to_economic_event() for e in avail if e.actual is not None or e.category in ("MACRO", "CENTRAL_BANK")]
        events += [e.to_economic_event() for e in self.upcoming_at(t)]
        news = [e.to_news_item() for e in avail if e.headline or e.category in ("NEWS", "GEOPOLITICAL", "ENERGY", "CHINA")]
        return events, news

    def stats(self) -> str:
        if not self.events:
            return "histórico vazio"
        by_cat: dict[str, int] = {}
        for e in self.events:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        with_actual = sum(1 for e in self.events if e.actual is not None)
        revised = sum(1 for e in self.events if e.revised is not None)
        emp = sum(1 for e in self.events if e.effect_source == "empirical")
        return (f"{len(self.events)} registros · {self.events[0].timestamp:%Y-%m-%d} → {self.events[-1].timestamp:%Y-%m-%d} · com actual {with_actual} · revisões {revised} · "
                f"efeitos empíricos {emp} · por categoria: " + ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items())))


# --------------------------------------------------------------------------- cobertura do período
@dataclass
class Coverage:
    start: date
    end: date
    days: int
    macro_events: int
    news_items: int
    revisions: int
    macro_weeks: int
    weeks: int
    news_days: int
    covered_days: int

    @property
    def macro_pct(self) -> float:
        return self.macro_weeks / self.weeks if self.weeks else 0.0

    @property
    def news_pct(self) -> float:
        return self.news_days / self.days if self.days else 0.0

    @property
    def pct(self) -> float:
        return self.covered_days / self.days if self.days else 0.0

    def render(self) -> str:
        return "\n".join([
            f"HISTÓRICO {self.start:%d/%m/%Y} → {self.end:%d/%m/%Y} ({self.days} dias)",
            f"MACRO     {self.macro_events:>8} eventos    · semanas com macro {self.macro_weeks}/{self.weeks} ({self.macro_pct:.0%})",
            f"NEWS      {self.news_items:>8} manchetes  · dias com manchete {self.news_days}/{self.days} ({self.news_pct:.0%})",
            f"REVISÕES  {self.revisions:>8}",
            f"COBERTURA {self.pct:>8.1%}   (dias com macro na semana ou manchete no dia)",
        ])


MACRO_CATEGORIES = ("MACRO", "CENTRAL_BANK")


def coverage(hist: "EventHistory", start: date, end: date) -> Coverage:
    """Quanto do período o banco cobre. MACRO por semana ISO (há semanas sem release relevante, não dias); NEWS por dia."""
    if end < start:
        start, end = end, start
    days = (end - start).days + 1
    weeks_set = {(start + timedelta(days=i)).isocalendar()[:2] for i in range(days)}
    macro_weeks: set = set()
    news_days: set = set()
    macro_n = news_n = rev_n = 0
    for e in hist.events:
        d = e.timestamp.date()
        if not (start <= d <= end):
            continue
        if e.revised is not None:
            rev_n += 1
            continue
        if e.category in MACRO_CATEGORIES:
            macro_n += 1
            macro_weeks.add(d.isocalendar()[:2])
        else:
            news_n += 1
            news_days.add(d)
    covered = sum(1 for i in range(days) if ((start + timedelta(days=i)).isocalendar()[:2] in macro_weeks) or ((start + timedelta(days=i)) in news_days))
    return Coverage(start, end, days, macro_n, news_n, rev_n, len(macro_weeks), len(weeks_set), len(news_days), covered)


class FetchProgress:
    """Janelas já baixadas (sidecar JSON ao lado do CSV): permite continuar de onde parou após 429/queda de rede."""

    def __init__(self, csv_path: str) -> None:
        self.path = csv_path + ".progress.json"
        self.done: dict[str, int] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.done = json.load(f)

    def has(self, key: str) -> bool:
        return key in self.done

    def mark(self, key: str, n: int) -> None:
        self.done[key] = n
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.done, f, indent=0, sort_keys=True)


# --------------------------------------------------------------------------- CSV
def load_history(path: str) -> EventHistory:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return EventHistory([HistoricalEvent.from_row(r) for r in rows if r.get("timestamp")])


def save_history(hist: EventHistory, path: str) -> int:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = HistoricalEvent.columns()
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for e in hist.events:
            w.writerow(e.to_row())
    return len(hist.events)


def merge(a: EventHistory, b: EventHistory) -> EventHistory:
    seen = {(e.event_id, e.published_at) for e in a.events}
    out = list(a.events) + [e for e in b.events if (e.event_id, e.published_at) not in seen]
    return EventHistory(out)


# --------------------------------------------------------------------------- efeitos: regras → empíricos
def rule_direction(kind: str, sign: float, sigma: Optional[float], market: str) -> float:
    """Direção esperada (−1..+1) por regra macro: canais de transmissão do evento × sensibilidade do mercado."""

    chans = TRANSMISSION.get(kind) or EXTRA_TRANSMISSION.get(kind)
    if not chans or sign == 0.0:
        return 0.0
    mag = min(1.0, abs(sigma) / 2.0) if sigma is not None else 1.0
    weights = CHANNEL_TO_MARKET.get(market, {})
    total = sum(sign * v * mag * weights.get(c, 0.0) for c, v in chans.items())
    norm = sum(abs(w) for c, w in weights.items() if c in chans) or 1.0
    return max(-1.0, min(1.0, total / norm))


def apply_rule_effects(hist: EventHistory) -> int:
    """Direção esperada por ativo a partir das regras macro (mesmas do NEWS ENGINE). Não toca efeitos empíricos."""
    n = 0
    for e in hist.events:
        if e.effect_source == "empirical":
            continue
        sign, sigma = 0.0, None
        if e.surprise is not None:
            typical = TYPICAL.get(e.kind, max(abs(e.forecast or e.previous or 1.0) * 0.1, 0.1))
            sigma = e.surprise / typical if typical else None
            sign = 1.0 if e.surprise > 0 else -1.0 if e.surprise < 0 else 0.0
        elif e.kind in ("fomc_hawkish", "fomc_dovish", "geopolitical_escalation", "geopolitical_deescalation", "systemic_stress", "oil_supply_cut",
                        "oil_supply_increase", "cb_gold_buying", "china_stimulus"):
            sign = 1.0
        if sign == 0.0:
            continue
        for m in EFFECT_MARKETS:
            setattr(e, EFFECT_FIELD[m], round(rule_direction(e.kind, sign, sigma, m), 3))
        e.effect_source = "rule"
        n += 1
    return n


def learn_effects(hist: EventHistory, prices: dict[str, Sequence[tuple[datetime, float]]], horizon_min: int = 60, min_n: int = 8) -> dict:
    """Deixa o histórico determinar o impacto: para cada (kind, sinal da surpresa) e mercado, mede a direção média do
    preço `horizon_min` depois do evento (em fração de ATR aproximada pelo desvio típico) e a taxa de acerto da regra.
    Substitui os efeitos por valores empíricos quando há amostra suficiente."""
    def move_after(series, t0):
        pts = [(t, p) for t, p in series if t >= t0]
        if not pts:
            return None
        p0 = pts[0][1]
        after = [p for t, p in pts if t <= t0 + timedelta(minutes=horizon_min)]
        return ((after[-1] / p0 - 1.0) * 100.0) if len(after) > 1 and p0 else None
    groups: dict[tuple[str, int, str], list[float]] = {}
    for e in hist.events:
        if e.surprise is None and e.category == "MACRO":
            continue
        sgn = 1 if (e.surprise or 0) > 0 else -1 if (e.surprise or 0) < 0 else 0
        for m, series in prices.items():
            mv = move_after(series, e.timestamp)
            if mv is not None:
                groups.setdefault((e.kind, sgn, m), []).append(mv)
    table: dict = {}
    for (kind, sgn, m), moves in groups.items():
        if len(moves) < min_n:
            continue
        avg = statistics.fmean(moves)
        sd = statistics.pstdev(moves) or 1.0
        up = sum(1 for x in moves if x > 0) / len(moves)
        table[(kind, sgn, m)] = {"n": len(moves), "avg_move_pct": round(avg, 3), "p_up": round(up, 2), "effect": round(max(-1.0, min(1.0, avg / sd)), 3)}
    applied = 0
    for e in hist.events:
        sgn = 1 if (e.surprise or 0) > 0 else -1 if (e.surprise or 0) < 0 else 0
        touched = False
        for m in EFFECT_MARKETS:
            row = table.get((e.kind, sgn, m))
            if row:
                setattr(e, EFFECT_FIELD[m], row["effect"])
                touched = True
        if touched:
            e.effect_source = "empirical"
            applied += 1
    return {"table": table, "applied": applied}


def render_effect_table(table: dict) -> str:
    if not table:
        return "efeitos empíricos: amostra insuficiente (mínimo 8 eventos por tipo/sinal/mercado)"
    lines = ["📚 EFEITO EMPÍRICO POR EVENTO (direção média do preço 60 min após, por mercado)", f"{'evento':<18}{'surpresa':>9}{'mercado':>9}{'n':>5}{'mov.médio':>11}{'P(sobe)':>9}{'efeito':>8}"]
    for (kind, sgn, m), r in sorted(table.items()):
        lines.append(f"{kind:<18}{('acima' if sgn > 0 else 'abaixo' if sgn < 0 else 'em linha'):>9}{m:>9}{r['n']:>5}{r['avg_move_pct']:>+10.2f}%{r['p_up']:>8.0%}{r['effect']:>+8.2f}")
    return "\n".join(lines)


# ============================================================================
# DATA · HISTORY_SOURCES
# ============================================================================

"""IMPORTADORES do banco histórico de eventos/notícias (point-in-time).

  • TradingEconomicsImporter — calendário macro com actual/forecast/previous/revised (exige chave: TE_API_KEY).
  • ALFREDImporter           — vintages do FRED (ALFRED): o valor INICIALMENTE publicado e as revisões, cada uma com a data
                               em que passou a existir (exige chave gratuita: FRED_API_KEY). Sem consenso → surpresa vs anterior.
  • GDELTImporter            — manchetes + tom + volume por tema (aberto, sem chave) → TESTE B (news/geopolítica/China/petróleo).

Nada aqui inventa efeito: o efeito por ativo nasce das regras macro (history.apply_rule_effects) e é substituído
pelo que o histórico mostrar (history.learn_effects).
"""


import zlib
from urllib.parse import quote


TE_BASE = "https://api.tradingeconomics.com"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"

TE_IMPORTANCE = {3: "MUITO ALTO", 2: "MÉDIO", 1: "BAIXO"}
CATEGORY_BY_KIND = {"fomc": "CENTRAL_BANK", "speech": "CENTRAL_BANK", "ecb": "CENTRAL_BANK", "boj": "CENTRAL_BANK", "oil_inventories": "ENERGY",
                    "oil_supply_cut": "ENERGY", "china": "CHINA"}


def _iso(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def us_release_time(d: date, hour_et: int = 8, minute: int = 30) -> datetime:
    """Horário UTC de uma divulgação às hour_et:minute (hora de Nova York), respeitando o horário de verão dos EUA."""
    def nth_sunday(year: int, month: int, n: int) -> date:
        first = date(year, month, 1)
        off = (6 - first.weekday()) % 7
        return first + timedelta(days=off + 7 * (n - 1))
    dst_start, dst_end = nth_sunday(d.year, 3, 2), nth_sunday(d.year, 11, 1)
    offset = 4 if dst_start <= d < dst_end else 5
    return datetime(d.year, d.month, d.day, hour_et + offset, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- Trading Economics
class TradingEconomicsImporter:
    def __init__(self, http, api_key: str) -> None:
        self.http, self.key = http, api_key

    def fetch(self, start: date, end: date, country: str = "united states") -> EventHistory:
        url = f"{TE_BASE}/calendar/country/{quote(country)}/{start:%Y-%m-%d}/{end:%Y-%m-%d}?c={self.key}&f=json"
        rows = self.http.get_json(url, ttl=24 * 3600)
        return self.parse(rows)

    @staticmethod
    def parse(rows: list[dict]) -> EventHistory:
        out: list[HistoricalEvent] = []
        for r in rows or []:
            try:
                ts = _iso(datetime.fromisoformat(str(r.get("Date")).replace("Z", "")))
            except (TypeError, ValueError):
                continue
            name = str(r.get("Event") or r.get("Category") or "")
            kind = kind_from_name(name) if kind_from_name(name) != "generic" else kind_from_name(str(r.get("Category") or ""))
            imp = TE_IMPORTANCE.get(int(r.get("Importance") or 0), "BAIXO")
            actual, forecast, previous, revised = _num(r.get("Actual")), _num(r.get("Forecast") or r.get("TEForecast")), _num(r.get("Previous")), _num(r.get("Revised"))
            ev_id = f"TE_{r.get('CalendarId') or name.replace(' ', '_')}_{ts:%Y%m%d%H%M}"
            cat = CATEGORY_BY_KIND.get(kind, "MACRO")
            out.append(HistoricalEvent(ts, ts, ev_id, name, str(r.get("Country") or "US"), str(r.get("Currency") or "USD"), imp, forecast, previous, actual, None,
                                       None, cat, kind, "tradingeconomics"))
            # REVISÃO: o valor anterior foi revisto nesta divulgação → linha nova, publicada AGORA, para o evento anterior
            if revised is not None and previous is not None and revised != previous:
                out.append(HistoricalEvent(ts, ts, f"{ev_id}_REVISAO", f"{name} (revisão do anterior)", str(r.get("Country") or "US"), str(r.get("Currency") or "USD"),
                                           "BAIXO", previous, previous, revised, revised, None, cat, kind, "tradingeconomics"))
        return EventHistory(out)


# --------------------------------------------------------------------------- ALFRED (FRED vintages)
ALFRED_SERIES: dict[str, tuple[str, str, str]] = {
    # série: (nome do evento, kind, transformação para a unidade que o mercado lê)
    "CPIAUCSL": ("CPI MoM", "cpi", "pct"),
    "CPILFESL": ("Core CPI MoM", "core_cpi", "pct"),
    "PCEPILFE": ("Core PCE MoM", "core_pce", "pct"),
    "PPIFIS": ("PPI MoM", "ppi", "pct"),
    "PAYEMS": ("Nonfarm Payrolls (K)", "nfp", "diff"),
    "UNRATE": ("Unemployment Rate", "unemployment", "level"),
    "ICSA": ("Initial Jobless Claims (K)", "jobless_claims", "level_k"),
    "RSAFS": ("Retail Sales MoM", "retail_sales", "pct"),
    "GDPC1": ("GDP QoQ ann.", "gdp", "pct_ann"),
    "CES0500000003": ("Average Hourly Earnings MoM", "earnings", "pct"),
}


class ALFREDImporter:
    KEY_RE = re.compile(r"^[a-f0-9]{32}$")

    def __init__(self, http, api_key: str, log=None) -> None:
        self.http, self.key, self._log = http, (api_key or "").strip(), log
        self.failed: list[tuple[str, str]] = []

    def validate_key(self) -> None:
        if not self.KEY_RE.match(self.key):
            raise DataError(f"FRED_API_KEY inválida ('{self.key[:12]}…'): a chave do FRED tem 32 caracteres hexadecimais minúsculos. "
                            "Gere a sua (gratuita) em https://fred.stlouisfed.org/docs/api/api_key.html e coloque FRED_API_KEY=... no .env")

    def fetch(self, start: date, end: date, series: Optional[list[str]] = None, progress=None) -> EventHistory:
        self.validate_key()
        hist = EventHistory()
        for sid in series or list(ALFRED_SERIES):
            key = f"alfred:v3:{sid}:{start:%Y-%m-%d}:{end:%Y-%m-%d}"
            if progress is not None and progress.has(key):
                continue
            obs_start = start - timedelta(days=400)     # trimestral (PIB) precisa do trimestre anterior; mensal ganha folga
            rt_start = start - timedelta(days=60)       # a 1ª vintage é recortada pelo FRED na data pedida: fica ANTES do período = só fundo
            url = (f"{FRED_API}?series_id={sid}&api_key={self.key}&file_type=json&observation_start={obs_start:%Y-%m-%d}"
                   f"&realtime_start={rt_start:%Y-%m-%d}&realtime_end={end:%Y-%m-%d}")
            try:
                payload = self.http.get_json(url, ttl=24 * 3600)
            except DataError as e:
                msg = str(e)
                reason = ("HTTP 400: chave rejeitada ou parâmetros inválidos (confira FRED_API_KEY)" if "400" in msg else
                          "HTTP 429: limite de requisições do FRED" if "429" in msg else msg[-160:])
                self.failed.append((sid, reason))
                if self._log:
                    self._log(f"  ALFRED {sid}: FALHOU — {reason}")
                continue
            part = self.parse(sid, payload.get("observations", []), start)
            for e in part.events:
                hist.add(e)
            if self._log:
                self._log(f"  ALFRED {sid} ({ALFRED_SERIES.get(sid, (sid,))[0]}): {len(part)} publicações/revisões")
            if progress is not None:
                progress.mark(key, len(part))
        return hist

    @staticmethod
    def parse(sid: str, observations: list[dict], start: date) -> EventHistory:
        name, kind, transform = ALFRED_SERIES.get(sid, (sid, "generic", "level"))
        # O FRED devolve UMA linha por (data, valor) cobrindo o intervalo realtime_start..realtime_end em que aquele valor
        # vigorou. Reconstruímos a série COMPLETA de cada vintage: valor de d na vintage V = linha com rs ≤ V ≤ re.
        rows = []
        for o in observations:
            v = _num(o.get("value"))
            if v is None:
                continue
            rows.append((o["date"], o["realtime_start"], o.get("realtime_end") or "9999-12-31", v))
        vintages: dict[str, dict[str, float]] = {}
        for vint in sorted({rs for _, rs, _, _ in rows}):
            vintages[vint] = {d: v for d, rs, re_, v in rows if rs <= vint <= re_}
        first_seen: dict[str, tuple[str, float]] = {}      # data da observação → (vintage inicial, valor transformado)
        out: list[HistoricalEvent] = []
        for vint in sorted(vintages):
            series = vintages[vint]
            dates = sorted(series)
            for i, d in enumerate(dates):
                if i == 0 and transform in ("pct", "diff", "pct_ann"):
                    continue
                v, prev = series[d], series[dates[i - 1]] if i else None
                if transform == "pct":
                    val = round((v / prev - 1) * 100, 2) if prev else None
                elif transform == "pct_ann":
                    val = round(((v / prev) ** 4 - 1) * 100, 1) if prev else None
                elif transform == "diff":
                    val = round(v - prev, 0) if prev is not None else None
                elif transform == "level_k":
                    val = round(v / 1000.0, 0)
                else:
                    val = v
                if val is None:
                    continue
                pub = datetime.fromisoformat(vint).date()
                if pub < start:
                    if d not in first_seen:
                        first_seen[d] = ("", val)      # fundo: conhecido antes do período, não é evento nem gera revisão
                    else:
                        first_seen[d] = (first_seen[d][0], val)
                    continue
                if d not in first_seen:
                    first_seen[d] = (vint, val)
                    prev_val = first_seen.get(dates[i - 1], (None, None))[1] if i else None
                    out.append(HistoricalEvent(us_release_time(pub), us_release_time(pub), f"ALFRED_{sid}_{d}", f"{name} ({d[:7]})", "US", "USD",
                                               "MUITO ALTO" if kind in ("cpi", "core_cpi", "nfp", "core_pce") else "ALTO", None, prev_val, val, None,
                                               (round(val - prev_val, 3) if prev_val is not None else None), "MACRO", kind, "alfred", surprise_basis="previous"))
                elif first_seen[d][0] and first_seen[d][1] != val:   # revisão só de publicação ocorrida dentro do período
                    # revisão: publicada na data desta vintage, mantém o event_id → substitui só a partir de published_at
                    out.append(HistoricalEvent(us_release_time(datetime.fromisoformat(first_seen[d][0]).date()), us_release_time(pub), f"ALFRED_{sid}_{d}",
                                               f"{name} ({d[:7]}) revisado", "US", "USD", "BAIXO", None, first_seen[d][1], val, val, None, "MACRO", kind, "alfred"))
                    first_seen[d] = (first_seen[d][0], val)
        return EventHistory(out)


# --------------------------------------------------------------------------- GDELT
GDELT_TOPICS: dict[str, tuple[str, str]] = {
    # tema: (query GDELT, categoria)
    "geopolitica": ('(war OR missile OR ceasefire OR sanctions OR "Middle East" OR Iran OR Israel OR Ukraine)', "GEOPOLITICAL"),
    "petroleo": ('(OPEC OR "crude oil" OR "oil supply" OR "oil prices")', "ENERGY"),
    "china": ('("China economy" OR PBOC OR "China stimulus" OR "Chinese exports")', "CHINA"),
    "fed": ('("Federal Reserve" OR Powell OR FOMC OR "rate cut" OR "rate hike")', "CENTRAL_BANK"),
    "risco": ('(recession OR "bank failure" OR "credit stress" OR "debt default" OR tariffs)', "NEWS"),
}


class GDELTImporter:
    """GDELT limita a ~1 requisição a cada 5 s (HTTP 429 acima disso): as chamadas são espaçadas por `min_interval`,
    um 429 espera e tenta de novo, e `checkpoint` recebe o parcial após cada janela (nada se perde se cair no meio)."""

    def __init__(self, http, min_interval: float = 8.0, retry_wait: float = 60.0, max_retries: int = 5, sleep=time.sleep, log=None) -> None:
        self.http, self.min_interval, self.retry_wait, self.max_retries = http, min_interval, retry_wait, max_retries
        self._sleep, self._log, self._last = sleep, log, 0.0

    def _get(self, url: str):
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            try:
                out = self.http.get_json(url, ttl=24 * 3600)
                self._last = time.monotonic()
                return out
            except DataError as e:
                self._last = time.monotonic()
                if attempt == self.max_retries:
                    raise
                rate = "429" in str(e)
                m = re.search(r"Retry-After (\d+)s", str(e))
                # 429 é bloqueio por rajada: espera exponencial (60 s, 2, 4, 8, 16 min) ou o Retry-After do servidor
                wait = (float(m.group(1)) if m else self.retry_wait * (2 ** attempt)) if rate else min(self.retry_wait, 20.0)
                if self._log:
                    self._log(f"GDELT {'429 (bloqueio por excesso de requisições)' if rate else 'falha de rede'}: aguardando {wait / 60:.1f} min e tentando de novo ({attempt + 1}/{self.max_retries})")
                self._sleep(wait)
        raise DataError("GDELT: falha persistente")

    def _url(self, query: str, mode: str, start: datetime, end: datetime, extra: str = "") -> str:
        return (f"{GDELT_DOC}?query={quote(query + ' sourcelang:english')}&mode={mode}&format=json"
                f"&startdatetime={start:%Y%m%d%H%M%S}&enddatetime={end:%Y%m%d%H%M%S}{extra}")

    def fetch(self, start: date, end: date, topics: Optional[list[str]] = None, chunk_days: int = 30, max_records: int = 250,
              checkpoint=None, enrich: bool = False, progress=None, mode: str = "volinfo") -> EventHistory:
        """1 chamada por tema e janela. `mode="volinfo"` (padrão): `timelinevolinfo` devolve, DIA A DIA, o volume e as manchetes
        mais relevantes de cada dia — cobertura uniforme da janela. `mode="artlist"`: as `max_records` manchetes mais recentes
        com hora exata (concentram-se no fim da janela; use janelas curtas). `enrich=True` acrescenta o tom (timelinetone).
        Janelas já feitas (progress) são puladas; janela que falhar após as tentativas fica em `self.failed` e a execução continua."""
        hist = EventHistory()
        self.failed: list[tuple[str, str, str]] = []
        t0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        t_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
        topics = topics or list(GDELT_TOPICS)
        n_chunks = max(1, -(-(t_end - t0).days // chunk_days))
        calls = 2 if enrich else 1
        if self._log:
            self._log(f"GDELT ({mode}): {len(topics)} temas × {n_chunks} janelas de {chunk_days} dias × {calls} chamada(s) ≈ "
                      f"{len(topics) * n_chunks * calls * self.min_interval / 60:.0f} min (limite do GDELT: ~1 requisição a cada 5 s)")
        for topic in topics:
            query, cat = GDELT_TOPICS[topic]
            t = t0
            while t < t_end:
                t1 = min(t + timedelta(days=chunk_days), t_end)
                key = f"gdelt:{topic}:{t:%Y%m%d}:{t1:%Y%m%d}:{mode}{':enrich' if enrich else ''}"
                if progress is not None and progress.has(key):
                    t = t1
                    continue
                try:
                    if mode == "artlist":
                        arts = self._get(self._url(query, "artlist", t, t1, f"&maxrecords={max_records}&sort=datedesc"))
                        vol = None
                    else:
                        arts = vol = self._get(self._url(query, "timelinevolinfo", t, t1))
                    tone = self._get(self._url(query, "timelinetone", t, t1)) if enrich else None
                except DataError as e:
                    self.failed.append((topic, f"{t:%Y-%m-%d}→{t1:%Y-%m-%d}", str(e)[-120:]))
                    if self._log:
                        self._log(f"  {topic} {t:%Y-%m-%d}→{t1:%Y-%m-%d}: FALHOU (rode de novo para continuar daqui)")
                    t = t1
                    continue
                part = self.parse(topic, cat, arts, tone, vol)
                for e in part.events:
                    hist.add(e)
                if self._log:
                    self._log(f"  {topic} {t:%Y-%m-%d}→{t1:%Y-%m-%d}: {len(part)} manchetes")
                if checkpoint:
                    checkpoint(hist)
                if progress is not None:
                    progress.mark(key, len(part))
                t = t1
        return hist

    @staticmethod
    def _timeline(payload: dict) -> list[tuple[datetime, float]]:
        out = []
        for series in (payload or {}).get("timeline", []):
            for pt in series.get("data", []):
                try:
                    out.append((datetime.strptime(pt["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), float(pt.get("value", 0.0))))
                except (KeyError, ValueError):
                    continue
        return sorted(out)

    @staticmethod
    def _articles(payload: dict) -> list[tuple[str, datetime, str]]:
        """(título, instante, domínio) tanto de `artlist` (seendate exato) quanto de `timelinevolinfo` (toparts por dia).
        No volinfo só se conhece o DIA: published_at = fim do dia (conservador — o cérebro nunca vê antes de existir)."""
        out = []
        for a in (payload or {}).get("articles", []):
            try:
                ts = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            out.append((str(a.get("title") or "").strip(), ts, str(a.get("domain") or "")))
        for series in (payload or {}).get("timeline", []):
            for pt in series.get("data", []):
                try:
                    day = datetime.strptime(pt["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    continue
                ts = day.replace(hour=23, minute=59) if day.hour == 0 and day.minute == 0 else day
                for a in pt.get("toparts", []) or []:
                    url = str(a.get("url") or "")
                    dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
                    out.append((str(a.get("title") or "").strip(), ts, dom))
        return out

    @staticmethod
    def parse(topic: str, category: str, artlist: dict, tone: Optional[dict] = None, volume: Optional[dict] = None) -> EventHistory:
        tones = GDELTImporter._timeline(tone or {})
        vols = GDELTImporter._timeline(volume or {})

        def nearest(series, t):
            best = None
            for ts, v in series:
                if ts <= t:
                    best = v
                else:
                    break
            return best
        out: list[HistoricalEvent] = []
        seen: set[str] = set()
        for title, ts, domain in GDELTImporter._articles(artlist):
            if not title:
                continue
            key = re.sub(r"\W+", " ", title.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            kind = next((k for pat, k, _ in QUALITATIVE if re.search(pat, title.lower())), "generic")
            tn, vl = nearest(tones, ts), nearest(vols, ts)
            sentiment = "" if tn is None else ("POSITIVE" if tn > 1.0 else "NEGATIVE" if tn < -1.0 else "NEUTRAL")
            out.append(HistoricalEvent(ts, ts, f"GDELT_{topic}_{ts:%Y%m%d%H%M}_{zlib.crc32(key.encode()) % 10000:04d}", f"{topic}: {title[:60]}", "GLOBAL", "", "MÉDIO",
                                       None, None, None, None, None, category, kind, f"gdelt/{domain}", title[:160], sentiment, tone=tn, volume=vl))
        return EventHistory(out)


# ============================================================================
# EXECUTION
# ============================================================================

"""GOLD AI ENGINE 3.0 — EXECUTION ENGINE + BROKER CONFIRMATION.

REQUEST → MT5 → BROKER → TICKET → POSITION → PREÇO REAL → SL REAL → TP REAL.
Uma ordem só é considerada executada depois de confirmada no broker; qualquer divergência
entre o que foi pedido e o que foi aberto é ⚠️ EXECUTION MISMATCH.
"""




MAGIC = 20260914


@dataclass
class ExecutionReport:
    requested_volume: float
    requested_sl: float
    requested_tp: Optional[float]
    requested_price: float
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    ticket: Optional[int] = None          # ticket da posição no broker
    fill_price: Optional[float] = None
    real_volume: Optional[float] = None
    real_sl: Optional[float] = None
    real_tp: Optional[float] = None
    slippage: Optional[float] = None
    confirmed: bool = False
    mismatches: list[str] = field(default_factory=list)
    corrected: bool = False
    error: str = ""
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return self.confirmed and not self.mismatches

    def render(self) -> str:
        if self.error:
            return f"❌ EXECUÇÃO FALHOU: {self.error} (retcode {self.retcode})"
        lines = [f"{'✅ EXECUÇÃO CONFIRMADA' if self.ok else '⚠️ EXECUTION MISMATCH'} — ticket {self.ticket}",
                 f"pedido: {self.requested_volume} @ {self.requested_price:.2f} SL {self.requested_sl:.2f} TP {self.requested_tp}",
                 f"real:   {self.real_volume} @ {self.fill_price} SL {self.real_sl} TP {self.real_tp} · slippage {self.slippage}"]
        lines += [f"  • {m}" for m in self.mismatches]
        if self.corrected:
            lines.append("  • SL/TP corrigidos automaticamente no broker")
        return "\n".join(lines)


@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    time: datetime


class ExecutionEngine:
    """Executa e confirma ordens no MT5 (o objeto `mt5` é injetável para testes)."""

    def __init__(self, client, price_tol: float = 0.05, sl_tol: float = 0.05, max_slippage: float = 0.30, deviation: int = 20) -> None:
        self.client = client
        self.mt5 = client.mt5
        self.price_tol, self.sl_tol, self.max_slippage, self.deviation = price_tol, sl_tol, max_slippage, deviation

    # ------------------------------------------------------------------ leitura
    def positions(self, symbol: Optional[str] = None) -> list[BrokerPosition]:
        symbol = symbol or self.client.cfg.symbol
        raw = self.mt5.positions_get(symbol=symbol) or []
        out = []
        for p in raw:
            direction = Direction.ALTA if getattr(p, "type", 0) == getattr(self.mt5, "POSITION_TYPE_BUY", 0) else Direction.BAIXA
            out.append(BrokerPosition(int(p.ticket), p.symbol, direction, float(p.volume), float(p.price_open), float(p.sl or 0.0),
                                      float(p.tp or 0.0), float(getattr(p, "profit", 0.0)), datetime.fromtimestamp(int(p.time), tz=timezone.utc)))
        return out

    def position(self, ticket: int) -> Optional[BrokerPosition]:
        return next((p for p in self.positions() if p.ticket == ticket), None)

    def account_equity(self) -> Optional[float]:
        info = self.mt5.account_info()
        return float(info.equity) if info is not None else None

    # ------------------------------------------------------------------ envio + confirmação
    def open(self, plan: TradePlan, comment: str = "GoldAI") -> ExecutionReport:
        mt5 = self.mt5
        bid, ask = self.client.tick()
        buy = plan.direction == Direction.ALTA
        price = ask if buy else bid
        tp = plan.targets.get(plan.recommended) if plan.recommended in plan.targets else plan.targets.get("3R")
        rep = ExecutionReport(plan.lots or 0.0, round(plan.stop, 2), round(tp, 2) if tp else None, price)
        if not plan.lots:
            rep.error = "lote zero"
            return rep
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.client.cfg.symbol, "volume": plan.lots,
               "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price, "sl": rep.requested_sl, "tp": rep.requested_tp or 0.0,
               "deviation": self.deviation, "magic": MAGIC, "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        if res is None:
            rep.error = f"order_send devolveu None: {mt5.last_error()}"
            return rep
        rep.retcode, rep.order, rep.deal = getattr(res, "retcode", None), getattr(res, "order", None), getattr(res, "deal", None)
        if rep.retcode != getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            rep.error = f"broker recusou: {getattr(res, 'comment', '')}"
            return rep
        return self.confirm(rep, plan)

    def confirm(self, rep: ExecutionReport, plan: TradePlan) -> ExecutionReport:
        """Confirma a POSIÇÃO no broker (não a requisição) e compara com o pedido."""
        pos = None
        for p in self.positions():
            if (rep.order and p.ticket == rep.order) or abs(p.volume - rep.requested_volume) < 1e-9 and p.direction == plan.direction:
                pos = p
                break
        if pos is None:
            rep.error = "posição não encontrada no broker após o envio"
            return rep
        rep.ticket, rep.fill_price, rep.real_volume, rep.real_sl, rep.real_tp = pos.ticket, pos.price_open, pos.volume, pos.sl, pos.tp
        rep.slippage = round(abs(pos.price_open - rep.requested_price), 2)
        rep.confirmed = True
        if abs(pos.volume - rep.requested_volume) > 1e-9:
            rep.mismatches.append(f"volume {pos.volume} ≠ pedido {rep.requested_volume}")
        if rep.slippage > self.max_slippage:
            rep.mismatches.append(f"slippage {rep.slippage} > máximo {self.max_slippage}")
        sl_bad = abs((pos.sl or 0.0) - rep.requested_sl) > self.sl_tol
        tp_bad = rep.requested_tp is not None and abs((pos.tp or 0.0) - rep.requested_tp) > self.sl_tol
        if sl_bad:
            rep.mismatches.append(f"SL real {pos.sl} ≠ pedido {rep.requested_sl}")
        if tp_bad:
            rep.mismatches.append(f"TP real {pos.tp} ≠ pedido {rep.requested_tp}")
        if sl_bad or tp_bad:
            if self.modify(pos.ticket, rep.requested_sl, rep.requested_tp):
                again = self.position(pos.ticket)
                if again and abs((again.sl or 0.0) - rep.requested_sl) <= self.sl_tol and (rep.requested_tp is None or abs((again.tp or 0.0) - rep.requested_tp) <= self.sl_tol):
                    rep.real_sl, rep.real_tp, rep.corrected = again.sl, again.tp, True
                    rep.mismatches = [m for m in rep.mismatches if not m.startswith(("SL real", "TP real"))]
        return rep

    # ------------------------------------------------------------------ gestão no broker
    def modify(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        mt5 = self.mt5
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": self.client.cfg.symbol, "sl": round(sl, 2) if sl else 0.0, "tp": round(tp, 2) if tp else 0.0}
        res = mt5.order_send(req)
        return res is not None and getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)

    def close(self, ticket: int, volume: Optional[float] = None, comment: str = "GoldAI close") -> tuple[bool, Optional[float]]:
        """Fecha total ou parcialmente. Devolve (ok, preço de fechamento)."""
        mt5 = self.mt5
        pos = self.position(ticket)
        if pos is None:
            return False, None
        bid, ask = self.client.tick()
        buy = pos.direction == Direction.ALTA
        vol = round(min(volume or pos.volume, pos.volume), 2)
        req = {"action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": pos.symbol, "volume": vol,
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": bid if buy else ask, "deviation": self.deviation,
               "magic": MAGIC, "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC}
        res = mt5.order_send(req)
        ok = res is not None and getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        return ok, (float(getattr(res, "price", 0.0)) or (bid if buy else ask)) if ok else None

    def closed_result(self, ticket: int) -> Optional[dict]:
        """Se a posição sumiu do broker (stop/TP), busca o resultado nos deals do histórico."""
        if self.position(ticket) is not None:
            return None
        deals = self.mt5.history_deals_get(position=ticket) or []
        if not deals:
            return None
        profit = sum(float(getattr(d, "profit", 0.0)) for d in deals)
        exits = [d for d in deals if getattr(d, "entry", 1) == getattr(self.mt5, "DEAL_ENTRY_OUT", 1)]
        price = float(exits[-1].price) if exits else None
        t = datetime.fromtimestamp(int(exits[-1].time), tz=timezone.utc) if exits else None
        return {"profit": profit, "price": price, "time": t, "deals": len(deals)}


# ============================================================================
# GUARD
# ============================================================================

"""GOLD AI ENGINE 3.0 — RISK GUARD · KILL SWITCH · PERFORMANCE ENGINE · COMANDOS.

Regras fundamentais (literalmente na especificação):
  • A IA nunca poderá aumentar o risco percentual da conta para recuperar perdas.
  • Nenhuma nova posição será aberta enquanto existir posição ativa no mesmo ativo.
  • O lote nasce de CAPITAL + RISCO + STOP + CONTRATO — nunca da confiança.
  • Perda diária ≥ MAX_DAILY_LOSS ou drawdown ≥ MAX_DRAWDOWN → 🚨 TRADING STOP.
  • TRADING_ENABLED=false (ou arquivo kill switch, ou /STOP) bloqueia novas entradas.
"""





class TradingMode(str, Enum):
    PAPER = "PAPER"           # 🟢 tudo simulado (padrão)
    AUTHORIZE = "AUTHORIZE"   # 🟡 monta a operação e pede autorização
    SEMI_LIVE = "SEMI_LIVE"   # 🟠 entra por regras pré-autorizadas; ações críticas pedem confirmação
    LIVE = "LIVE"             # 🔴 execução totalmente automática


@dataclass
class GuardLimits(RiskLimits):
    max_drawdown_pct: float = 10.0
    min_rr_to_structure: float = 2.0     # se a resistência/suporte forte estiver antes disto (em R), não há expectativa
    daily_target_pct: float = 0.0        # META DIÁRIA (0 = desligada): ao atingir, sem novas entradas até o dia seguinte — trava, não obrigação

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "GuardLimits":
        base = RiskLimits.from_env(env)
        g = cls(**base.__dict__)
        g.max_drawdown_pct = float(env.get("MAX_DRAWDOWN", g.max_drawdown_pct))
        g.min_rr_to_structure = float(env.get("MIN_RR_TO_STRUCTURE", g.min_rr_to_structure))
        g.daily_target_pct = float(env.get("DAILY_TARGET", g.daily_target_pct) or 0.0)
        return g


@dataclass
class KillSwitch:
    """TRADING_ENABLED no ambiente/.env, arquivo sentinela e comandos /STOP /PAUSE /RESUME."""

    enabled_env: bool = True
    file_path: Optional[str] = None
    stopped: bool = False     # /STOP → sem novas entradas
    paused: bool = False      # /PAUSE → sem novas entradas nem gestão automática crítica

    @classmethod
    def from_env(cls, env: dict[str, str], file_path: Optional[str] = None) -> "KillSwitch":
        val = str(env.get("TRADING_ENABLED", "true")).strip().lower()
        return cls(enabled_env=val in ("1", "true", "yes", "on"), file_path=file_path)

    def new_entries_allowed(self) -> tuple[bool, str]:
        if not self.enabled_env:
            return False, "TRADING_ENABLED=false"
        if self.file_path and os.path.exists(self.file_path):
            return False, f"kill switch ativo ({self.file_path})"
        if self.paused:
            return False, "sistema pausado (/PAUSE)"
        if self.stopped:
            return False, "novas entradas bloqueadas (/STOP)"
        return True, "ok"


@dataclass
class PerformanceEngine:
    """Capital → risco financeiro permitido → lote. O percentual nunca muda; o valor cresce com o capital."""

    limits: GuardLimits
    equity: float
    peak_equity: float = 0.0
    day: Optional[str] = None
    daily_pnl: float = 0.0
    trading_stop: bool = False
    target_reached: bool = False      # 🎯 meta diária atingida: protege o ganho (sem novas entradas hoje)
    history: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.peak_equity = max(self.peak_equity, self.equity)

    def roll_day(self, t: datetime) -> None:
        d = t.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.daily_pnl, self.trading_stop, self.target_reached = d, 0.0, False, False

    @property
    def daily_target_usd(self) -> Optional[float]:
        return round(self.equity_start_of_day() * self.limits.daily_target_pct / 100.0, 2) if self.limits.daily_target_pct > 0 else None

    @property
    def daily_pct(self) -> float:
        base = self.equity_start_of_day()
        return round(100.0 * self.daily_pnl / base, 2) if base else 0.0

    def r_to_target(self) -> Optional[float]:
        """Quantos R líquidos faltam para a meta com o risco atual (informativo — a meta nunca força entrada)."""
        if self.daily_target_usd is None or self.risk_usd <= 0:
            return None
        return round(max(0.0, (self.daily_target_usd - self.daily_pnl) / self.risk_usd), 2)

    @property
    def risk_usd(self) -> float:
        """Risco financeiro por operação = capital × RISK_PER_TRADE. Único ponto de cálculo — nunca ajustado por confiança ou perdas."""
        return round(self.equity * self.limits.risk_per_trade_pct / 100.0, 2)

    @property
    def drawdown_pct(self) -> float:
        return round(100.0 * (self.peak_equity - self.equity) / self.peak_equity, 2) if self.peak_equity else 0.0

    def record_result(self, pnl_usd: float, t: datetime, note: str = "") -> None:
        self.roll_day(t)
        self.equity = round(self.equity + pnl_usd, 2)
        self.peak_equity = max(self.peak_equity, self.equity)
        self.daily_pnl = round(self.daily_pnl + pnl_usd, 2)
        self.history.append({"time": t.isoformat(), "pnl": pnl_usd, "equity": self.equity, "note": note})
        if self.daily_pnl <= -self.equity_start_of_day() * self.limits.max_daily_loss_pct / 100.0:
            self.trading_stop = True
        if self.daily_target_usd is not None and self.daily_pnl >= self.daily_target_usd:
            self.target_reached = True

    def equity_start_of_day(self) -> float:
        return self.equity - self.daily_pnl

    def sync_equity(self, broker_equity: float, t: datetime) -> None:
        """Em LIVE o capital vem do broker; a variação entra como resultado do dia."""
        self.roll_day(t)
        delta = round(broker_equity - self.equity, 2)
        if abs(delta) > 0.005:
            self.record_result(delta, t, "sync broker")

    def blocks(self, t: datetime) -> list[str]:
        self.roll_day(t)
        out: list[str] = []
        if self.trading_stop or self.daily_pnl <= -self.equity_start_of_day() * self.limits.max_daily_loss_pct / 100.0:
            self.trading_stop = True
            out.append(f"🚨 TRADING STOP — perda diária {self.daily_pnl:+.2f} USD atingiu {self.limits.max_daily_loss_pct:.1f}% do capital")
        if self.drawdown_pct >= self.limits.max_drawdown_pct:
            out.append(f"🚨 drawdown {self.drawdown_pct:.1f}% ≥ MAX_DRAWDOWN {self.limits.max_drawdown_pct:.1f}%")
        if self.daily_target_usd is not None and (self.target_reached or self.daily_pnl >= self.daily_target_usd):
            self.target_reached = True
            out.append(f"🎯 META DIÁRIA ATINGIDA — dia {self.daily_pnl:+.2f} USD ({self.daily_pct:+.1f}% ≥ {self.limits.daily_target_pct:.0f}%): "
                       "sem novas entradas hoje; posições abertas seguem com o monitor")
        return out

    def render(self) -> str:
        meta = ""
        if self.daily_target_usd is not None:
            r = self.r_to_target()
            meta = (f" · 🎯 meta {self.limits.daily_target_pct:.0f}% = {self.daily_target_usd:,.2f} USD ({self.daily_pct:+.1f}% hoje"
                    + (f", faltam ≈ {r:.2f}R" if r else ", ATINGIDA") + ")")
        return (f"💼 CAPITAL {self.equity:,.2f} USD · pico {self.peak_equity:,.2f} · drawdown {self.drawdown_pct:.1f}% · "
                f"dia {self.daily_pnl:+.2f} · risco/operação {self.limits.risk_per_trade_pct}% = {self.risk_usd:.2f} USD" + meta
                + (" · 🚨 TRADING STOP" if self.trading_stop else "") + (" · 🎯 META ATINGIDA" if self.target_reached else ""))


def size_lots(limits: GuardLimits, risk_usd: float, stop_distance: float, point_value_usd: Optional[float] = None) -> tuple[float, float]:
    """(lote, risco real em USD). CAPITAL + RISCO + STOP + CONTRATO — nada mais.
    `point_value_usd` (USD por 1.0 de preço por lote) vem do MarketSpec no 4.0; padrão = contract_size (XAUUSD)."""
    per_lot = stop_distance * (point_value_usd if point_value_usd else limits.contract_size)
    if per_lot <= 0:
        return 0.0, 0.0
    lots = min(limits.max_lot, risk_usd / per_lot)
    lots = round(int(lots / limits.lot_step + 1e-9) * limits.lot_step, 2)
    if lots < limits.min_lot:
        return 0.0, 0.0
    return lots, round(lots * per_lot, 2)


@dataclass
class TelegramCommands:
    """Lê /STOP /PAUSE /RESUME /STATUS /CLOSE (com confirmação) via getUpdates. Sem token → inativo."""

    token: Optional[str]
    chat_id: Optional[str]
    offset: int = 0
    pending_close: bool = False
    last_cmds: list[str] = field(default_factory=list)

    def poll(self) -> list[str]:  # pragma: no cover - rede
        if not self.token:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?" + urllib.parse.urlencode({"offset": self.offset, "timeout": 0})
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
        except Exception:  # noqa: BLE001
            return []
        cmds: list[str] = []
        for upd in data.get("result", []):
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                continue
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                cmds.append(text.upper())
        return cmds

    def apply(self, cmds: list[str], ks: KillSwitch) -> list[str]:
        """Aplica ao kill switch; devolve ações que o loop deve executar: STATUS, CLOSE_CONFIRMED."""
        actions: list[str] = []
        self.last_cmds = list(cmds)
        for c in cmds:
            if c.startswith("/EDGE"):
                actions.append("EDGE")
            elif c.startswith("/STOP"):
                ks.stopped = True
                actions.append("STOP")
            elif c.startswith("/PAUSE"):
                ks.paused = True
                actions.append("PAUSE")
            elif c.startswith("/RESUME"):
                ks.stopped = ks.paused = False
                actions.append("RESUME")
            elif c.startswith("/STATUS"):
                actions.append("STATUS")
            elif c.startswith("/CLOSE"):
                if "CONFIRM" in c or self.pending_close:
                    self.pending_close = False
                    actions.append("CLOSE_CONFIRMED")
                else:
                    self.pending_close = True
                    actions.append("CLOSE_REQUESTED")
        return actions


# ============================================================================
# VALIDATION
# ============================================================================

"""GOLD AI ENGINE 2.1 — VALIDATION ENGINE.

Missão: provar (ou refutar) que o 2.0 antecipa o XAU/USD.
  1. Auditoria anti look-ahead   → lookahead_audit
  2. Walk-forward rolante         → evaluation.walk_forward(mode="rolling")
  3. Lead time / 4. MFE-MAE       → evaluation.evaluate
  5. Probabilidade calibrada      → calibration_table, Brier, IsotonicCalibrator
  6. Score por fator              → factor_scoreboard (quais informações realmente preveem)
"""





# --------------------------------------------------------------------------- 1. anti look-ahead
def lookahead_audit(snapshot: MarketSnapshot) -> list[str]:
    """Lista violações: qualquer candle, notícia ou evento *realizado* com timestamp posterior ao snapshot."""
    t = snapshot.time
    bad: list[str] = []
    for tf, cs in snapshot.candles.items():
        late = [c for c in cs if c.time > t]
        if late:
            bad.append(f"{tf}: {len(late)} candle(s) após {t:%Y-%m-%d %H:%M}")
        if any(cs[i].time > cs[i + 1].time for i in range(len(cs) - 1)):
            bad.append(f"{tf}: candles fora de ordem")
    for n in snapshot.news:
        if n.time > t:
            bad.append(f"notícia futura: {n.headline[:50]}")
    for e in snapshot.events:
        if e.actual is not None and e.time > t:
            bad.append(f"resultado de evento futuro: {e.name}")
    return bad


# --------------------------------------------------------------------------- 5. calibração
@dataclass
class CalibrationBin:
    lo: float
    hi: float
    n: int
    predicted: float   # média da probabilidade prevista
    observed: float    # taxa de acerto observada

    @property
    def gap(self) -> float:
        return self.observed - self.predicted


@dataclass
class CalibrationReport:
    bins: list[CalibrationBin]
    brier: Optional[float]
    brier_reference: Optional[float]  # Brier de prever sempre a taxa-base
    ece: Optional[float]              # expected calibration error
    n: int

    def render(self) -> str:
        lines = ["🎯 CALIBRAÇÃO DA PROBABILIDADE"]
        if not self.n:
            return "\n".join(lines + ["  (sem previsões resolvidas)"])
        lines.append(f"  n={self.n} · Brier={self.brier:.3f} (referência {self.brier_reference:.3f}; menor é melhor) · ECE={self.ece:.3f}")
        lines.append("  previsto → observado")
        for b in self.bins:
            bar = "█" * int(round(b.observed * 20))
            lines.append(f"  {b.lo:.0%}–{b.hi:.0%}: prev {b.predicted:.0%} obs {b.observed:.0%} (n={b.n}) {bar} {'+' if b.gap > 0 else ''}{b.gap:+.0%}")
        verdict = "bem calibrado" if self.ece < 0.05 else "moderadamente calibrado" if self.ece < 0.10 else "MAL calibrado — usar IsotonicCalibrator"
        lines.append(f"  Veredito: {verdict}")
        return "\n".join(lines)


def calibration_table(pairs: Iterable[tuple[float, bool]], n_bins: int = 5, lo: float = 0.5, hi: float = 1.0) -> CalibrationReport:
    """pairs: (probabilidade prevista na direção sinalizada, acertou?)."""
    data = [(p, 1.0 if hit else 0.0) for p, hit in pairs]
    if not data:
        return CalibrationReport([], None, None, None, 0)
    width = (hi - lo) / n_bins
    bins: list[CalibrationBin] = []
    ece = 0.0
    for k in range(n_bins):
        a, b = lo + k * width, lo + (k + 1) * width
        inb = [(p, y) for p, y in data if (a <= p < b) or (k == n_bins - 1 and p == b)]
        if not inb:
            continue
        pred = statistics.fmean(p for p, _ in inb)
        obs = statistics.fmean(y for _, y in inb)
        bins.append(CalibrationBin(a, b, len(inb), pred, obs))
        ece += len(inb) / len(data) * abs(obs - pred)
    brier = statistics.fmean((p - y) ** 2 for p, y in data)
    base = statistics.fmean(y for _, y in data)
    brier_ref = statistics.fmean((base - y) ** 2 for _, y in data)
    return CalibrationReport(bins, round(brier, 4), round(brier_ref, 4), round(ece, 4), len(data))


class IsotonicCalibrator:
    """Regressão isotônica (pool-adjacent-violators): mapeia probabilidade prevista → observada,
    monotônica. Aplicável ao motor via GoldAIEngine.calibrator."""

    def __init__(self) -> None:
        self.xs: list[float] = []
        self.ys: list[float] = []

    def fit(self, pairs: Iterable[tuple[float, bool]]) -> "IsotonicCalibrator":
        data = sorted((p, 1.0 if h else 0.0) for p, h in pairs)
        if not data:
            return self
        # agrupa empates de x (mesma probabilidade) antes do PAV
        grouped: list[list[float]] = []  # [x, y médio, peso]
        for x, y in data:
            if grouped and grouped[-1][0] == x:
                g = grouped[-1]
                g[1] = (g[1] * g[2] + y) / (g[2] + 1)
                g[2] += 1
            else:
                grouped.append([x, y, 1])
        blocks = grouped  # [x médio, y médio, peso]
        i = 0
        while i < len(blocks) - 1:
            if blocks[i][1] > blocks[i + 1][1]:
                a, b = blocks[i], blocks[i + 1]
                w = a[2] + b[2]
                merged = [(a[0] * a[2] + b[0] * b[2]) / w, (a[1] * a[2] + b[1] * b[2]) / w, w]
                blocks[i:i + 2] = [merged]
                i = max(0, i - 1)
            else:
                i += 1
        self.xs = [b[0] for b in blocks]
        self.ys = [b[1] for b in blocks]
        return self

    def __call__(self, p: float) -> float:
        if not self.xs:
            return p
        if p <= self.xs[0]:
            return self.ys[0]
        if p >= self.xs[-1]:
            return self.ys[-1]
        for i in range(len(self.xs) - 1):
            if self.xs[i] <= p <= self.xs[i + 1]:
                span = self.xs[i + 1] - self.xs[i]
                w = (p - self.xs[i]) / span if span else 0.0
                return self.ys[i] + w * (self.ys[i + 1] - self.ys[i])
        return p

    def to_dict(self) -> dict:
        return {"xs": self.xs, "ys": self.ys}

    @classmethod
    def from_dict(cls, d: dict) -> "IsotonicCalibrator":
        c = cls()
        c.xs, c.ys = list(d.get("xs", [])), list(d.get("ys", []))
        return c


# --------------------------------------------------------------------------- 6. score por fator
@dataclass
class FactorRow:
    name: str
    n: int                 # previsões em que o fator estava alinhado com a direção prevista
    hit_rate: float        # acertos / n quando alinhado
    n_against: int         # previsões em que o fator apontava contra
    hit_rate_against: Optional[float]
    lift: Optional[float]  # hit_rate − hit_rate_against (poder discriminante)

    def bar(self, width: int = 10) -> str:
        return "█" * int(round(self.hit_rate * width)) + "░" * (width - int(round(self.hit_rate * width)))


@dataclass
class Scoreboard:
    rows: list[FactorRow]
    base_rate: Optional[float]
    n: int

    def render(self) -> str:
        lines = ["📈 SCORE POR FATOR — taxa de acerto quando o fator apontava na direção do sinal"]
        if not self.n:
            return "\n".join(lines + ["  (sem previsões resolvidas)"])
        lines.append(f"  taxa-base (todos os sinais): {self.base_rate:.0%} · n={self.n}")
        w = max(len(r.name) for r in self.rows) if self.rows else 10
        for r in sorted(self.rows, key=lambda r: -(r.lift if r.lift is not None else r.hit_rate)):
            lift = f"lift {r.lift:+.0%}" if r.lift is not None else "lift n/d"
            lines.append(f"  {r.name:<{w}} {r.bar()} {r.hit_rate:>4.0%}  (n={r.n:<3} {lift})")
        return "\n".join(lines)


def factor_scoreboard(records: Sequence[dict], min_ratio: float = 0.2) -> Scoreboard:
    """records: dicts com 'direction' (ALTA|BAIXA), 'hit' (bool) e 'factors' {nome: valor assinado
    (+ = altista)}, opcionalmente 'technical' {indicador: valor assinado}. Um fator conta como
    'alinhado' se sinal(valor) == sinal(direção) e |valor| >= min_ratio (valores em -1..+1)."""
    names: dict[str, tuple[list[bool], list[bool]]] = {}
    hits_all: list[bool] = []
    for r in records:
        sign = 1.0 if r["direction"] == "ALTA" else -1.0
        hit = bool(r["hit"])
        hits_all.append(hit)
        allf = {**r.get("factors", {}), **{f"tec:{k}": v for k, v in (r.get("technical") or {}).items()}}
        for name, val in allf.items():
            if val is None or abs(val) < min_ratio:
                continue
            aligned, against = names.setdefault(name, ([], []))
            (aligned if sign * val > 0 else against).append(hit)
    rows: list[FactorRow] = []
    for name, (al, ag) in names.items():
        if not al:
            continue
        hr = sum(al) / len(al)
        hra = (sum(ag) / len(ag)) if ag else None
        rows.append(FactorRow(name, len(al), hr, len(ag), hra, (hr - hra) if hra is not None else None))
    base = (sum(hits_all) / len(hits_all)) if hits_all else None
    return Scoreboard(rows, base, len(hits_all))


# --------------------------------------------------------------------------- relatório consolidado
@dataclass
class ValidationReport:
    backtest_text: str
    walk_forward_text: str
    calibration: CalibrationReport
    scoreboard: Scoreboard
    audit_violations: list[str] = field(default_factory=list)
    n_audited: int = 0
    min_signals: int = 20  # abaixo disso nenhuma conclusão estatística é honesta
    opportunity_text: str = ""

    def verdict(self) -> str:
        m = re.search(r"Lead time \(acertos\): média ([\d.]+) min", self.walk_forward_text)
        lead = float(m.group(1)) if m else None
        p = re.search(r"Precisão: total (\d+)\.?\d*%", self.walk_forward_text)
        prec = int(p.group(1)) / 100 if p else None
        if self.audit_violations:
            return "❌ REPROVADO — violações de look-ahead"
        n = self.calibration.n
        if prec is None or lead is None or n < self.min_signals:
            return f"⚪ INCONCLUSIVO — {n} sinal(is) resolvido(s) fora da amostra; mínimo {self.min_signals}. Rode `live` por mais tempo ou use histórico com DXY/juros."
        if prec >= 0.6 and lead >= 10 and (self.calibration.ece or 1) < 0.10:
            return f"🟢 EVIDÊNCIA DE ANTECIPAÇÃO — precisão OOS {prec:.0%}, lead médio {lead:.0f} min, calibração ok"
        if prec >= 0.5:
            return f"🟡 PARCIAL — precisão OOS {prec:.0%}, lead médio {lead:.0f} min; ainda não comprova antecipação consistente"
        return f"🔴 SEM EVIDÊNCIA — precisão OOS {prec:.0%}"

    def render(self) -> str:
        audit = f"🔍 AUDITORIA ANTI LOOK-AHEAD: {self.n_audited} snapshots, {len(self.audit_violations)} violação(ões)"
        if self.audit_violations:
            audit += "\n" + "\n".join(f"  ✗ {v}" for v in self.audit_violations[:10])
        parts = ["🧪 GOLD AI ENGINE — VALIDATION ENGINE", audit, self.backtest_text, self.walk_forward_text,
                 self.calibration.render(), self.scoreboard.render()]
        if self.opportunity_text:
            parts.append(self.opportunity_text)
        parts.append(f"VEREDITO: {self.verdict()}")
        return "\n\n".join(parts)


# ============================================================================
# EVALUATION
# ============================================================================

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
    fedfunds: list[Candle] = field(default_factory=list)      # ZQ=F (30-day Fed Funds futures): 100 − preço = taxa implícita
    breakeven_daily: list[tuple[datetime, float]] = field(default_factory=list)   # FRED T10YIE (%)
    symbol: str = "XAUUSD"                                     # mercado (para o NEWS ENGINE por mercado)
    events: Optional[object] = None                            # history.EventHistory (banco point-in-time de eventos/notícias)
    news_mode: str = "full"                                    # none | macro | full — o que do banco o cérebro pode ver

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
        _, d_zq = change(self.fedfunds, False)
        if d_zq is not None:
            s.fed_cut_prob_change_pp = round(max(-100.0, min(100.0, d_zq * 100 * 4)), 1)   # Δpreço → −Δtaxa implícita (bp) → p.p. de corte
        be_pts = [(d, v) for d, v in self.breakeven_daily if d <= t] if self.breakeven_daily else []
        if self.real_yield_daily:
            pts = [(d, v) for d, v in self.real_yield_daily if d <= t]
            if len(pts) >= 2:
                s.real_yield_10y = pts[-1][1]
                if s.us10y_change_bp is not None and len(be_pts) >= 2:
                    s.real_yield_change_bp = round(s.us10y_change_bp - (be_pts[-1][1] - be_pts[-2][1]) * 100 * (window_bars / 24), 2)
                else:
                    s.real_yield_change_bp = (pts[-1][1] - pts[-2][1]) * 100
        elif s.us10y_change_bp is not None:
            s.real_yield_change_bp = s.us10y_change_bp  # aproximação: sem breakeven, usa nominal
        self._attach_events(s, t)
        return s

    def reaction_stats(self):
        """ReactionRecords de todos os eventos do banco, medidos nas séries H1 do frame (cada um só fica visível após known_at)."""
        key = (id(self.events), self.symbol)
        cached = getattr(self, "_reaction_stats", None)
        if cached is not None and getattr(self, "_reaction_key", None) == key:
            return cached

        series = [(c.time, c.close) for c in self.xau]
        leads = {"USD": [(c.time, c.close) for c in self.dxy], "YIELD": [(c.time, c.close) for c in self.us10y]}
        closes = [c.close for c in self.xau]

        def atr_at(t: datetime) -> float:
            j = self._at(self.xau, t)
            if j is None or j < 20:
                return 0.0
            return _atr(self.xau[max(0, j - 60): j + 1]) or 0.0
        recs = records_from_history(self.events, self.symbol, series, atr_at, leads, 240, 60) if self.events is not None else []
        self._reaction_stats, self._reaction_key = ReactionStats(recs), key
        return self._reaction_stats

    def _attach_events(self, s: MarketSnapshot, t: datetime) -> None:
        """BANCO HISTÓRICO point-in-time: só o que estava publicado em t. Modo none = preço somente;
        macro = calendário (releases/bancos centrais); full = calendário + manchetes/tom (GDELT)."""
        if self.events is None or self.news_mode == "none" or len(self.events) == 0:
            return

        events, news = self.events.snapshot_inputs(t)
        if self.news_mode == "macro":
            news = []
        s.events = events
        identified = EventIdentifier().identify(news, events, t)
        na = NewsEngine().assess(self.symbol, s, identified, t)
        s.news_pressure, s.news_status, s.news_chain = na.pressure, na.status, na.chain
        # REACTION ENGINE: relógio com estatística point-in-time (só eventos concluídos antes de t)
        ra = ReactionClock(self.reaction_stats()).assess(self.symbol, s, identified, t)
        s.reaction_status, s.reaction_pressure, s.reaction_probability = ra.status, ra.pressure, ra.probability
        s.reaction_latency_min, s.reaction_expected_min, s.reaction_chain = ra.latency_min, ra.expected_min, ra.chain
        if self.news_mode == "full":
            tones = [e.tone for e in self.events.available_at(t, 6.0) if e.tone is not None]
            if tones:
                s.sentiment = max(-1.0, min(1.0, sum(tones) / len(tones) / 10.0))


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
    factor_coverage: Optional[float] = None   # fração média do peso dos fatores com dado disponível

    def render(self) -> str:
        cov = f" · cobertura de fatores {self.factor_coverage:.0%}" if self.factor_coverage is not None else ""
        out = f"BACKTEST — {self.n_steps} passos, {len(self.signals)} sinais{cov}\n" + self.metrics.render()
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

        mpe = MaxProfitEngine(horizon_min=self.horizon_min)
        monitor = TradeMonitor()
        signals: list[SignalRecord] = []
        trade_rows: list[dict] = []
        managed: list[tuple[ManagedTrade, dict, int]] = []   # (trade, row, índice de abertura)
        horizon_bars = self.horizon_min // 60
        prev_i = start - 1
        decisions: list[DecisionRecord] = []
        entries: list[tuple] = []
        funnel = Funnel()
        coverage_sum, coverage_n = 0.0, 0
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
            coverage_sum += sum(f.max_score for f in a.factors if f.available) / max(1.0, sum(f.max_score for f in a.factors))
            coverage_n += 1
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
        curve_rows = [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
        opp = opportunity_report(decisions, path, entries, threshold, self.horizon_min, curve_rows)
        res = BacktestResult(evaluate(signals, path, threshold, self.horizon_min), signals, len(range(start, end, self.step)), cfg,
                             r_stats(trade_rows) if self.simulate_trades else None, trade_rows, opp, decisions, entries, funnel)
        res.factor_coverage = round(coverage_sum / coverage_n, 3) if coverage_n else None
        return res


@dataclass
class WalkForwardResult:
    folds: list[tuple[EngineConfig, BacktestResult]]
    oos: Metrics
    oos_trades: Optional[object] = None  # trading.RStats fora da amostra
    oos_opportunity: Optional[object] = None  # opportunity.OpportunityReport agregado OOS
    oos_funnel: Optional[object] = None       # opportunity.Funnel agregado OOS

    def render(self) -> str:
        covs = [r.factor_coverage for _, r in self.folds if r.factor_coverage is not None]
        cov = f" · cobertura de fatores {statistics.fmean(covs):.0%} (o score reescala pelo peso disponível: cobertura baixa = scores baixos)" if covs else ""
        lines = ["🔁 WALK-FORWARD (fora da amostra) — treina → testa → avança → treina → testa" + cov]
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
    rows = [r for _, res in folds for r in res.trade_rows]
    # oportunidades OOS agregadas: decisões, entradas e curva de limiar de todos os folds de teste
    decisions = [d for _, res in folds if res.opportunity for d in res.decisions]
    entries = [e for _, res in folds if res.opportunity for e in res.entries]
    curve_rows = [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
    opp = opportunity_report(decisions, path, entries, bt.threshold_atr * statistics.fmean(atrs), bt.horizon_min, curve_rows) if decisions else None
    fun = Funnel()
    for _, res in folds:
        if res.funnel is not None:
            fun = fun.merge(res.funnel)
    return WalkForwardResult(folds, oos, r_stats(rows) if rows else None, opp, fun if fun.analyses else None)


# --------------------------------------------------------------------------- 2.1 validação consolidada
def validate(frame: HistoryFrame, cfg: Optional[EngineConfig] = None, n_folds: int = 4, step: int = 1, warmup: int = 220,
             threshold_atr: float = 1.0, horizon_min: int = 240, mode: str = "rolling", audit_every: int = 25):
    """Backtest + walk-forward rolante + calibração + score por fator + auditoria anti look-ahead."""

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


# ============================================================================
# OPPORTUNITY
# ============================================================================

"""GOLD AI ENGINE 3.0 — OPPORTUNITY ENGINE (medição, não regras).

🔥 OPPORTUNITY CAPTURE RATE  movimentos relevantes do período × quantos a IA capturou com entrada
   ENTRY RATE                oportunidades analisadas × entradas (⚠️ OVERFILTER abaixo do mínimo)
   FILTER ATTRIBUTION        qual regra bloqueou cada oportunidade e o que teria acontecido (expectancy hipotética)
   THRESHOLD CURVE           entradas × expectancy por limiar de score — a região ideal, não o score mais alto

Filosofia: analisar muito, decidir simples, agir quando existir vantagem. Se o sistema entra pouco,
não se adiciona filtro: identifica-se qual regra elimina oportunidades lucrativas e reduz-se o seu peso.
"""




# Categorias de bloqueio (uma por decisão, a primeira que impediu a entrada)
RULES = ("SEM_SINAL", "SEM_VANTAGEM", "CONFIANCA", "EVIDENCIA", "CONFLITO", "ESTAGIO_3", "SPREAD", "VIABILIDADE",
         "KILL_SWITCH", "TRADING_STOP", "POSICAO_ABERTA", "LOTE", "AUTORIZACAO", "ENTRADA")


def classify_reason(decision: str) -> str:
    d = decision.lower()
    if d.startswith(("🟢 paper open", "🟢 position open")):
        return "ENTRADA"
    if "aguardando autoriza" in d:
        return "AUTORIZACAO"
    if "sem sinal" in d or "não é operacional" in d:
        return "SEM_SINAL"
    if "sem vantagem" in d:
        return "SEM_VANTAGEM"
    if "confiança" in d:
        return "CONFIANCA"
    if "evidência" in d:
        return "EVIDENCIA"
    if "conflitantes" in d:
        return "CONFLITO"
    if "perseguir" in d:
        return "ESTAGIO_3"
    if "spread" in d:
        return "SPREAD"
    if "espaço estatístico" in d or "viabil" in d:
        return "VIABILIDADE"
    if "kill switch" in d or "trading_enabled" in d or "/stop" in d or "pausado" in d:
        return "KILL_SWITCH"
    if "trading stop" in d or "drawdown" in d:
        return "TRADING_STOP"
    if "posição ativa" in d or "posições" in d:
        return "POSICAO_ABERTA"
    if "lote" in d:
        return "LOTE"
    return "OUTRO"


@dataclass
class DecisionRecord:
    time: datetime
    price: float
    score: float
    direction: str                 # ALTA | BAIXA | LATERAL
    action: str                    # ENTRADA | bloqueio (RULES)
    reason: str = ""
    atr: float = 0.0
    hypothetical_r: Optional[float] = None   # o que teria acontecido com a hipótese 3R (stop 1.2 ATR)
    evidence_level: int = 0
    confidence: float = 0.0


def hypothetical_trade(rec: DecisionRecord, candles: Sequence[Candle], horizon_min: int, stop_atr: float = 1.2) -> Optional[float]:
    """Resultado em R da operação que a regra bloqueou, com a hipótese padrão (stop 1.2 ATR, alvo 3R)."""
    if rec.direction not in ("ALTA", "BAIXA") or rec.atr <= 0:
        return None
    sign = 1.0 if rec.direction == "ALTA" else -1.0
    plan = TradePlan(Direction(rec.direction), rec.price, rec.price - sign * stop_atr * rec.atr, rec.atr, rec.time)
    fut = [c for c in candles if c.time > rec.time]
    if not fut or (fut[-1].time - rec.time) < timedelta(minutes=min(horizon_min, 30)):
        return None
    res = simulate_trade(plan, fut, ExitStrategy("3R", target_r=3.0), horizon_min)
    return None if res.exit_reason == "OPEN" else res.r_multiple


@dataclass
class OpportunityReport:
    period_hours: float
    n_moves: int
    n_captured: int
    capture_rate: Optional[float]
    n_analyzed: int
    n_entries: int
    entry_rate: Optional[float]
    overfilter: bool
    attribution: list[dict] = field(default_factory=list)     # {rule, n, hyp_n, hyp_expectancy, hyp_win}
    threshold_curve: list[dict] = field(default_factory=list) # {threshold, n, expectancy, win_rate}
    min_entry_rate: float = 0.05
    min_capture_rate: float = 0.25

    def render(self) -> str:
        f = lambda x: "n/d" if x is None else f"{x:.0%}"  # noqa: E731
        lines = ["🔥 OPPORTUNITY ENGINE", f"Período: {self.period_hours:.0f} h",
                 f"OPPORTUNITY CAPTURE RATE: {f(self.capture_rate)}  ({self.n_captured}/{self.n_moves} movimentos relevantes capturados com entrada)",
                 f"ENTRY RATE: {f(self.entry_rate)}  ({self.n_entries} entradas em {self.n_analyzed} oportunidades analisadas)"]
        if self.overfilter:
            lines.append(f"⚠️ OVERFILTER — entry rate < {self.min_entry_rate:.0%} ou captura < {self.min_capture_rate:.0%}: identificar a regra que elimina oportunidades lucrativas")
        if self.attribution:
            lines.append("Atribuição por filtro (o que a regra bloqueou e o que teria acontecido com a hipótese 3R):")
            for a in self.attribution:
                hyp = f"E hipotética {a['hyp_expectancy']:+.2f}R (win {a['hyp_win']:.0%}, n={a['hyp_n']})" if a["hyp_n"] else "sem resultado hipotético ainda"
                flag = " ◀ regra cara: bloqueia oportunidades lucrativas" if (a["hyp_n"] >= 10 and a["hyp_expectancy"] > 0.2) else ""
                lines.append(f"  {a['rule']:<15} n={a['n']:<4} {hyp}{flag}")
        if self.threshold_curve:
            lines.append("Curva limiar × entradas × expectancy (região ideal = maior expectancy com volume suficiente):")
            best = max((r for r in self.threshold_curve if r["n"] >= 10), key=lambda r: r["expectancy"], default=None)
            for r in self.threshold_curve:
                mark = " ◀ região ideal" if best is r else ""
                lines.append(f"  Score ≥ {r['threshold']:<3} entradas {r['n']:<5} E={r['expectancy']:+.2f}R  win {r['win_rate']:.0%}{mark}")
        return "\n".join(lines)


def threshold_curve(rows: Sequence[dict], thresholds: Sequence[int] = (20, 30, 40, 50, 60, 70, 80)) -> list[dict]:
    """rows: {"score": |score| na entrada, "r": resultado em R}."""
    out = []
    for t in thresholds:
        sub = [r["r"] for r in rows if abs(r["score"]) >= t and r["r"] is not None]
        if not sub:
            out.append({"threshold": t, "n": 0, "expectancy": 0.0, "win_rate": 0.0})
            continue
        out.append({"threshold": t, "n": len(sub), "expectancy": round(statistics.fmean(sub), 3), "win_rate": sum(1 for x in sub if x > 0) / len(sub)})
    return out


def attribution(decisions: Sequence[DecisionRecord]) -> list[dict]:
    by: dict[str, list[DecisionRecord]] = {}
    for d in decisions:
        by.setdefault(d.action, []).append(d)
    out = []
    for rule, recs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        hyp = [r.hypothetical_r for r in recs if r.hypothetical_r is not None]
        out.append({"rule": rule, "n": len(recs), "hyp_n": len(hyp), "hyp_expectancy": round(statistics.fmean(hyp), 3) if hyp else 0.0,
                    "hyp_win": (sum(1 for x in hyp if x > 0) / len(hyp)) if hyp else 0.0})
    return out


def opportunity_report(decisions: Sequence[DecisionRecord], prices: Sequence[tuple[datetime, float]], entries: Sequence[tuple[datetime, str]],
                       threshold_usd: float, horizon_min: int = 240, trade_rows: Sequence[dict] = (),
                       min_entry_rate: float = 0.05, min_capture_rate: float = 0.25) -> OpportunityReport:
    """decisions: uma por ciclo analisado; prices: (t, close) do período; entries: (t, direção) das entradas;
    trade_rows: {"score", "r"} das operações resolvidas (curva de limiar)."""
    prices = sorted(prices)
    hours = (prices[-1][0] - prices[0][0]).total_seconds() / 3600 if len(prices) > 1 else 0.0
    moves = detect_moves(prices, threshold_usd, horizon_min) if len(prices) > 1 else []
    captured = 0
    for mv in moves:
        if any(d == mv.direction and mv.evident_at - timedelta(minutes=horizon_min) <= t < mv.evident_at for t, d in entries):
            captured += 1
    analyzed = [d for d in decisions if d.action != "SEM_SINAL"] or list(decisions)
    n_entries = sum(1 for d in decisions if d.action == "ENTRADA")
    entry_rate = (n_entries / len(analyzed)) if analyzed else None
    capture = (captured / len(moves)) if moves else None
    overfilter = (entry_rate is not None and len(analyzed) >= 20 and entry_rate < min_entry_rate) or (capture is not None and len(moves) >= 10 and capture < min_capture_rate)
    return OpportunityReport(hours, len(moves), captured, capture, len(analyzed), n_entries, entry_rate, overfilter,
                             attribution([d for d in decisions if d.action != "ENTRADA"]), threshold_curve(trade_rows), min_entry_rate, min_capture_rate)


# --------------------------------------------------------------------------- FUNIL DE ENTRADA
# Onde as entradas desaparecem: cada análise recebe a PRIMEIRA etapa em que a oportunidade caiu.
FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("SCORE_MIN", "Score insuficiente (|score| < mínimo de vantagem)"),
    ("PROB_MIN", "Probabilidade insuficiente (< 55 %)"),
    ("CONF_MIN", "Confiança insuficiente (< 50)"),
    ("SCORE_SINAL", "Score abaixo do limiar de sinal (±50)"),
    ("CONFIRMACOES", "Confirmações insuficientes (< 3 famílias)"),
    ("ESTAGIO_3", "Estágio 3 (movimento já ocorreu)"),
    ("ANTI_SPAM", "Anti-spam (sem mudança relevante)"),
    ("INTERVALO_MINIMO", "Intervalo mínimo entre alertas"),
    ("SINAL_NAO_OPERACIONAL", "Sinal não operacional (WATCH/REVERSAL/RISK)"),
    ("CONFIANCA_OPERAR", "Confiança para operar (< 60)"),
    ("EVIDENCIA", "Evidência insuficiente (< nível 2)"),
    ("CONFLITO", "Fatores conflitantes"),
    ("VIABILIDADE", "Alvo inviável (resistência/suporte forte antes de 2R)"),
    ("STOP_LOTE", "Stop inválido / lote zero"),
    ("SPREAD", "Spread"),
    ("KILL_SWITCH", "Kill switch / pausa"),
    ("TRADING_STOP", "Trading stop / drawdown"),
    ("POSICAO_ABERTA", "Posição já aberta no ativo"),
    ("CORRELACAO", "Correlação / exposição de carteira"),
    ("PRIORIDADE", "Prioridade (outro mercado foi melhor)"),
    ("AUTORIZACAO", "Aguardando autorização"),
    ("OUTROS", "Outros filtros"),
)
STAGE_LABEL = dict(FUNNEL_STAGES)


def funnel_stage(a, sig, gate_reason: str, decision: str, cfg, raw_min_score: float = 15.0) -> tuple[bool, Optional[str]]:
    """(é oportunidade bruta?, etapa em que caiu ou None = ENTRADA)."""

    direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
    if direction == Direction.LATERAL or abs(a.score) < raw_min_score:
        return False, None
    d = (decision or "").lower()
    if d.startswith(("🟢 paper open", "🟢 position open")):
        return True, None
    if not a.has_edge:
        if abs(a.score) < cfg.min_edge_score:
            return True, "SCORE_MIN"
        if max(a.prob_up, a.prob_down) < cfg.min_edge_probability:
            return True, "PROB_MIN"
        return True, "CONF_MIN"
    if sig is None:
        return True, gate_reason if gate_reason in STAGE_LABEL else "OUTROS"
    if "não é operacional" in d:
        return True, "SINAL_NAO_OPERACIONAL"
    if "aguardando autoriza" in d:
        return True, "AUTORIZACAO"
    if "confiança" in d:
        return True, "CONFIANCA_OPERAR"
    if "evidência" in d:
        return True, "EVIDENCIA"
    if "conflitantes" in d:
        return True, "CONFLITO"
    if "perseguir" in d:
        return True, "ESTAGIO_3"
    if "espaço estatístico" in d or "viabil" in d:
        return True, "VIABILIDADE"
    if "spread" in d:
        return True, "SPREAD"
    if "kill switch" in d or "trading_enabled" in d or "/stop" in d or "pausado" in d:
        return True, "KILL_SWITCH"
    if "trading stop" in d or "drawdown" in d:
        return True, "TRADING_STOP"
    if "exposição de carteira" in d or "correlacionado" in d or "max_portfolio_positions" in d or "max_total_open_risk" in d:
        return True, "CORRELACAO"
    if "posição ativa" in d or "max_asset_exposure" in d:
        return True, "POSICAO_ABERTA"
    if "prioridade" in d:
        return True, "PRIORIDADE"
    if "lote" in d or "stop" in d:
        return True, "STOP_LOTE"
    if "analisado" in d:
        return True, "OUTROS"
    return True, "OUTROS"


@dataclass
class Funnel:
    analyses: int = 0
    raw: int = 0
    entries: int = 0
    drops: dict[str, int] = field(default_factory=dict)

    def add(self, is_raw: bool, stage: Optional[str]) -> None:
        self.analyses += 1
        if not is_raw:
            return
        self.raw += 1
        if stage is None:
            self.entries += 1
        else:
            self.drops[stage] = self.drops.get(stage, 0) + 1

    def merge(self, other: "Funnel") -> "Funnel":
        f = Funnel(self.analyses + other.analyses, self.raw + other.raw, self.entries + other.entries, dict(self.drops))
        for k, v in other.drops.items():
            f.drops[k] = f.drops.get(k, 0) + v
        return f

    @property
    def qualified(self) -> int:
        """Passaram por todas as regras do motor (vantagem + sinal + regras de operação); só faltou carteira/prioridade/autorização."""
        portfolio = ("KILL_SWITCH", "TRADING_STOP", "POSICAO_ABERTA", "CORRELACAO", "PRIORIDADE", "AUTORIZACAO")
        return self.entries + sum(v for k, v in self.drops.items() if k in portfolio)

    def render(self, title: str = "FUNIL DE ENTRADA") -> str:
        w = 46
        lines = [f"🔻 {title}", f"{'ANÁLISES H1:':<{w}}{self.analyses:>7,}", f"{'OPORTUNIDADES BRUTAS (|score| ≥ 15):':<{w}}{self.raw:>7,}", ""]
        for key, label in FUNNEL_STAGES:
            n = self.drops.get(key, 0)
            if n:
                pct = f"{n / self.raw:>5.0%}" if self.raw else ""
                lines.append(f"  {label:<{w - 2}}{n:>7,}  {pct}")
        lines += ["", f"{'OPORTUNIDADES QUALIFICADAS:':<{w}}{self.qualified:>7,}", f"{'ENTRADAS:':<{w}}{self.entries:>7,}"]
        if self.raw:
            top = max(self.drops.items(), key=lambda kv: kv[1], default=None)
            if top and top[1] / self.raw >= 0.3:
                lines.append(f"→ maior perda: {STAGE_LABEL[top[0]]} ({top[1] / self.raw:.0%} das oportunidades brutas)")
        return "\n".join(lines)


# ============================================================================
# SELECTOR
# ============================================================================

"""MARKET AI ENGINE 4.0 — ASSET SELECTOR · MARKET OPPORTUNITY SCORE · OPPORTUNITY DECAY · PORTFOLIO EXPOSURE.

Regra: o Asset Selector NÃO cria entradas. Ele só ordena oportunidades que o Prediction/Opportunity
Engine já produziu (sinal operacional + vantagem estatística) e o Risk Engine ainda valida depois.

MARKET OPPORTUNITY SCORE (pesos iniciais, a serem testados pelo Validation Engine):
    25% expectancy histórica (encolhida pela confiança estatística)
    20% probabilidade calibrada
    15% score atual
    15% qualidade do pré-movimento
    10% compatibilidade de regime
     5% captura histórica de oportunidades
     5% liquidez / custo de execução
     5% qualidade dos dados
Três dimensões separadas: QUALIDADE HISTÓRICA · OPORTUNIDADE ATUAL · DECAY (ainda existe vantagem AGORA?).
"""




WEIGHTS = {"expectancy": 0.25, "probability": 0.20, "score": 0.15, "premove": 0.15, "regime": 0.10, "capture": 0.05, "execution": 0.05, "data": 0.05}


# --------------------------------------------------------------------------- confiança estatística
@dataclass
class StatConfidence:
    n: int
    expectancy: float
    std: float
    level: str            # HIGH | MEDIUM | LOW | NONE
    lower_bound: float    # limite inferior ~90% da expectancy
    shrunk: float         # expectancy encolhida para 0 conforme a amostra

    def render(self) -> str:
        return f"E={self.expectancy:+.2f}R n={self.n} conf={self.level} (LB {self.lower_bound:+.2f}R, ajustada {self.shrunk:+.2f}R)"


def statistical_confidence(results_r: Sequence[float], k: int = 30) -> StatConfidence:
    """Encolhimento bayesiano simples E×n/(n+k) e limite inferior E − 1.28·σ/√n. HIGH exige n≥100 e LB>0."""
    n = len(results_r)
    if n == 0:
        return StatConfidence(0, 0.0, 0.0, "NONE", 0.0, 0.0)
    e = statistics.fmean(results_r)
    sd = statistics.pstdev(results_r) if n > 1 else 1.0
    lb = e - 1.28 * sd / math.sqrt(n)
    shrunk = e * n / (n + k)
    level = "HIGH" if (n >= 100 and lb > 0) else "MEDIUM" if (n >= 30 and lb > -0.05) else "LOW"
    return StatConfidence(n, round(e, 3), round(sd, 3), level, round(lb, 3), round(shrunk, 3))


# --------------------------------------------------------------------------- decay
def opportunity_decay(a: Assessment, first_seen: Optional[datetime], now: datetime, half_life_min: float = 45.0) -> tuple[float, str]:
    """0..1 — quanto da vantagem ainda existe para entrar AGORA: estágio × preço já percorrido × idade do sinal."""
    stage = {Stage.PRE_MOVIMENTO: 1.0, Stage.CONFIRMACAO: 0.85, Stage.NEUTRO: 0.6, Stage.MOVIMENTO: 0.2}[a.premove.stage]
    moved = max(0.0, 1.0 - abs(a.premove.move_in_atr) / 2.0)
    age_min = (now - first_seen).total_seconds() / 60 if first_seen else 0.0
    age = 0.5 ** (age_min / half_life_min)
    decay = round(stage * moved * (0.4 + 0.6 * age), 3)
    why = f"estágio {a.premove.stage.value} · {abs(a.premove.move_in_atr):.1f} ATR percorridos · idade {age_min:.0f} min"
    return decay, why


# --------------------------------------------------------------------------- candidato
@dataclass
class Candidate:
    spec: MarketSpec
    assessment: Assessment
    signal: Signal
    snapshot: MarketSnapshot
    history: StatConfidence
    capture_rate: Optional[float] = None
    data_quality: float = 1.0          # fração de fatores disponíveis
    first_seen: Optional[datetime] = None
    components: dict[str, float] = field(default_factory=dict)
    decay: float = 1.0
    decay_note: str = ""
    opportunity_score: float = 0.0
    status: str = "🟠"

    @property
    def direction(self) -> Direction:
        return self.signal.direction

    def render_row(self) -> str:
        a = self.assessment
        return (f"{self.spec.symbol:<7} {self.status} OPP {self.opportunity_score:>5.1f} · hist {self.history.shrunk:+.2f}R ({self.history.level}, n={self.history.n}) · "
                f"agora score {a.score:+.0f} prob {max(a.prob_up, a.prob_down):.0%} {a.premove.stage.value} · decay {self.decay:.2f} · "
                f"{'BUY' if self.direction == Direction.ALTA else 'SELL'}")


def regime_compatibility(a: Assessment, direction: Direction) -> float:
    r = a.regime
    if direction == Direction.ALTA:
        return 1.0 if r.startswith("BULLISH") else 0.6 if r == "RANGE" else 0.3 if r == "VOLATILE" else 0.2
    return 1.0 if r.startswith("BEARISH") else 0.6 if r == "RANGE" else 0.3 if r == "VOLATILE" else 0.2


def premove_quality(a: Assessment) -> float:
    base = {Stage.PRE_MOVIMENTO: 1.0, Stage.CONFIRMACAO: 0.8, Stage.NEUTRO: 0.4, Stage.MOVIMENTO: 0.1}[a.premove.stage]
    return base * (0.5 + 0.5 * a.premove.probability) * (0.6 + 0.4 * int(a.evidence_level) / 4)


def execution_quality(spec: MarketSpec, snap: MarketSnapshot, spread: Optional[float]) -> float:
    atr = snap.atr or 0.0
    sp = spread if spread is not None else spec.typical_spread
    if atr <= 0:
        return 0.5
    ratio = sp / atr  # spread como fração do ATR
    q = max(0.0, 1.0 - ratio / 0.15)
    h = snap.time.hour
    lo, hi = spec.session_hours_utc
    in_session = lo <= h < hi if lo < hi else (h >= lo or h < hi)
    return round(q * (1.0 if in_session else 0.6), 3)


class AssetSelector:
    """`reaction_edge` (símbolo → 0..1, de reaction_edge.json): dimensão opcional "o relógio de reação tem edge provado neste
    mercado". Só pesa quando o snapshot marca PRESSÃO LATENTE; fora disso é neutra (0,5). Não cria entradas: só ordena."""

    REACTION_WEIGHT = 0.10

    def __init__(self, weights: Optional[dict[str, float]] = None, reaction_edge: Optional[dict[str, float]] = None) -> None:
        self.weights = weights or dict(WEIGHTS)
        self.reaction_edge = dict(reaction_edge or {})
        self.first_seen: dict[str, tuple[Direction, datetime]] = {}

    def score(self, c: Candidate, now: datetime, spread: Optional[float] = None) -> Candidate:
        a = c.assessment
        key = c.spec.symbol
        seen = self.first_seen.get(key)
        if seen is None or seen[0] != c.direction:
            self.first_seen[key] = (c.direction, now)
            seen = self.first_seen[key]
        c.first_seen = seen[1]
        c.decay, c.decay_note = opportunity_decay(a, c.first_seen, now)
        comp = {
            "expectancy": max(0.0, min(1.0, 0.5 + c.history.shrunk)),          # +0.5R → 1.0 ; 0 → 0.5 ; −0.5R → 0
            "probability": max(0.0, min(1.0, (max(a.prob_up, a.prob_down) - 0.5) * 2)),
            "score": min(1.0, abs(a.score) / 100.0),
            "premove": premove_quality(a),
            "regime": regime_compatibility(a, c.direction),
            "capture": c.capture_rate if c.capture_rate is not None else 0.5,
            "execution": execution_quality(c.spec, c.snapshot, spread),
            "data": c.data_quality,
        }
        raw = sum(self.weights[k] * v for k, v in comp.items()) * 100.0
        if key in self.reaction_edge:
            latent = getattr(c.snapshot, "reaction_status", "") == "PRESSÃO LATENTE"
            comp["reaction"] = self.reaction_edge[key] if latent else 0.5
            raw = raw * (1.0 - self.REACTION_WEIGHT) + comp["reaction"] * self.REACTION_WEIGHT * 100.0
        c.components = {k: round(v, 3) for k, v in comp.items()}
        c.opportunity_score = round(raw * (0.5 + 0.5 * c.decay), 1)
        c.status = "🟢" if c.opportunity_score >= 60 else "🟡" if c.opportunity_score >= 45 else "🟠"
        return c

    def rank(self, candidates: Sequence[Candidate], now: datetime, spreads: Optional[dict[str, float]] = None) -> list[Candidate]:
        scored = [self.score(c, now, (spreads or {}).get(c.spec.symbol)) for c in candidates]
        return sorted(scored, key=lambda c: -c.opportunity_score)

    def forget(self, symbol: str) -> None:
        self.first_seen.pop(symbol, None)


def render_rank(ranked: Sequence[Candidate], history: Optional[dict[str, StatConfidence]] = None) -> str:
    lines = ["🏆 MARKET OPPORTUNITY RANK", "        HISTÓRICO            AGORA"]
    for c in ranked:
        a = c.assessment
        now_flag = "🟢" if (a.has_edge and c.decay >= 0.5) else "🟡" if a.has_edge else "🔴"
        lines.append(f"{c.spec.symbol:<7} {c.history.shrunk:+.2f}R {c.history.level:<6}  {now_flag} score {a.score:+.0f} prob {max(a.prob_up, a.prob_down):.0%} "
                     f"decay {c.decay:.2f} → OPP {c.opportunity_score:.1f} {c.status}")
    if history:
        rest = [s for s in history if s not in {c.spec.symbol for c in ranked}]
        for s in rest:
            h = history[s]
            lines.append(f"{s:<7} {h.shrunk:+.2f}R {h.level:<6}  🔴 sem oportunidade agora")
    if ranked:
        lines.append(f"MELHOR OPORTUNIDADE: {ranked[0].spec.symbol} ({'BUY' if ranked[0].direction == Direction.ALTA else 'SELL'}) — {ranked[0].decay_note}")
    else:
        lines.append("Nenhuma oportunidade com vantagem estatística neste ciclo.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- exposição de carteira
@dataclass
class OpenExposure:
    symbol: str
    direction: Direction
    risk_usd: float


@dataclass
class PortfolioLimits:
    max_total_open_risk_pct: float = 1.5
    max_correlated_risk_pct: float = 1.0
    max_positions: int = 3
    max_asset_exposure: int = 1
    correlation_threshold: float = 0.5   # acima disto, duas posições são "a mesma aposta"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PortfolioLimits":
        g = lambda k, d: type(d)(env.get(k, d))  # noqa: E731
        return cls(g("MAX_TOTAL_OPEN_RISK", 1.5), g("MAX_CORRELATED_RISK", 1.0), g("MAX_PORTFOLIO_POSITIONS", 3), g("MAX_ASSET_EXPOSURE", 1), g("CORRELATION_THRESHOLD", 0.5))


class PortfolioExposureEngine:
    """"Estou diversificando ou fazendo a mesma aposta três vezes?" Risco agregado e correlacionado."""

    def __init__(self, limits: PortfolioLimits, corr_table: Optional[dict[tuple[str, str], float]] = None) -> None:
        self.limits = limits
        self.corr_table = corr_table

    def correlated_risk(self, symbol: str, direction: Direction, risk_usd: float, open_: Sequence[OpenExposure]) -> float:
        """Risco da nova posição + risco das abertas na mesma aposta (correlação assinada pela direção)."""
        total = risk_usd
        for o in open_:
            rho = correlation(symbol, o.symbol, self.corr_table)
            same = 1.0 if o.direction == direction else -1.0
            signed = rho * same
            if signed > 0:
                total += o.risk_usd * signed
        return round(total, 2)

    def check(self, symbol: str, direction: Direction, risk_usd: float, open_: Sequence[OpenExposure], equity: float) -> list[str]:
        reasons: list[str] = []
        if len(open_) >= self.limits.max_positions:
            reasons.append(f"posições abertas {len(open_)} ≥ MAX_PORTFOLIO_POSITIONS {self.limits.max_positions}")
        if sum(1 for o in open_ if o.symbol == symbol) >= self.limits.max_asset_exposure:
            reasons.append(f"já existe posição em {symbol} (MAX_ASSET_EXPOSURE)")
        total = sum(o.risk_usd for o in open_) + risk_usd
        if total > equity * self.limits.max_total_open_risk_pct / 100.0:
            reasons.append(f"risco total aberto {total / equity:.2%} > MAX_TOTAL_OPEN_RISK {self.limits.max_total_open_risk_pct}%")
        corr = self.correlated_risk(symbol, direction, risk_usd, open_)
        if corr > equity * self.limits.max_correlated_risk_pct / 100.0:
            same = [o.symbol for o in open_ if correlation(symbol, o.symbol, self.corr_table) * (1 if o.direction == direction else -1) >= self.limits.correlation_threshold]
            reasons.append(f"risco correlacionado {corr / equity:.2%} > MAX_CORRELATED_RISK {self.limits.max_correlated_risk_pct}% (mesma aposta: {', '.join(same) or 'parcial'})")
        return reasons

    def render(self, open_: Sequence[OpenExposure], equity: float) -> str:
        if not open_:
            return "📐 EXPOSIÇÃO: nenhuma posição aberta"
        total = sum(o.risk_usd for o in open_)
        lines = [f"📐 EXPOSIÇÃO: {len(open_)} posição(ões) · risco total {total / equity:.2%} do capital"]
        for o in open_:
            lines.append(f"  {o.symbol} {'BUY' if o.direction == Direction.ALTA else 'SELL'} risco {o.risk_usd:.2f} USD")
        return "\n".join(lines)


# ============================================================================
# EDGE_REPORT
# ============================================================================

"""MARKET AI ENGINE 4.0 — LIVE EDGE REPORT (o teste definitivo).

Tabela diária, por mercado, a partir do que o sistema VIVEU (PAPER/AUTHORIZE/LIVE): as previsões foram
feitas antes do resultado, logo tudo aqui é fora da amostra por construção.
    OOS Trades · Expectancy · Probabilidade calibrada (declarada × observada) · Capture Rate · Status
Não precisamos acreditar que EURUSD é melhor. Os dados mostram.
"""





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
                    line(f"Probabilidade calibrada: {pct(m.prob_observed)}" + (f"  (declarada {pct(m.prob_declared)}, n={m.n_predictions})" if m.prob_declared is not None else "")),
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


# ============================================================================
# ESTIMATE
# ============================================================================

"""MARKET AI ENGINE 4.0 — ESTIMATIVA DE LUCRO sobre um período histórico.

Pergunta: "se o sistema tivesse operado de <início> até <fim>, com este capital e este risco por operação,
quanto teria ganho ou perdido — e com que incerteza?"

Método honesto:
  1. walk-forward FORA DA AMOSTRA (parâmetros escolhidos só no treino) → lista cronológica de operações em R;
  2. custo de execução: spread típico do mercado descontado em R de cada operação;
  3. curva de capital sequencial com risco fixo (RISK_PER_TRADE % do capital corrente — composto);
  4. bootstrap (reamostragem das operações) → percentis 5/50/95 do retorno e do drawdown máximo;
  5. ressalvas explícitas: o histórico H1 gratuito não tem notícias, COT intraday nem FRED intraday,
     logo a cobertura de fatores é menor que no `live`; estimativa ≠ garantia.
"""





@dataclass
class TradeR:
    time: datetime
    symbol: str
    r: float            # resultado líquido em R (já com custo)
    cost_r: float
    strategy: str


@dataclass
class EquityPath:
    start: float
    end: float
    max_drawdown_pct: float
    curve: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def return_pct(self) -> float:
        return (self.end / self.start - 1.0) * 100.0 if self.start else 0.0


def simulate_equity(trades: Sequence[TradeR], equity: float, risk_pct: float, compound: bool = True) -> EquityPath:
    eq, peak, mdd = equity, equity, 0.0
    curve = []
    base = equity
    for t in sorted(trades, key=lambda x: x.time):
        risk = (eq if compound else base) * risk_pct / 100.0
        eq = round(eq + t.r * risk, 2)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0 if peak else 0.0)
        curve.append((t.time, eq))
    return EquityPath(equity, eq, round(mdd, 2), curve)


def bootstrap(trades: Sequence[TradeR], equity: float, risk_pct: float, n: int = 1000, seed: int = 7) -> dict:
    if not trades:
        return {}
    rnd = random.Random(seed)
    rets, dds = [], []
    for _ in range(n):
        sample = [rnd.choice(trades) for _ in trades]
        # preserva a ordem temporal original para o cálculo do drawdown
        sample = [TradeR(t.time, s.symbol, s.r, s.cost_r, s.strategy) for t, s in zip(sorted(trades, key=lambda x: x.time), sample)]
        p = simulate_equity(sample, equity, risk_pct)
        rets.append(p.return_pct); dds.append(p.max_drawdown_pct)
    rets.sort(); dds.sort()
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"ret_p5": q(rets, 0.05), "ret_p50": q(rets, 0.50), "ret_p95": q(rets, 0.95), "dd_p50": q(dds, 0.50), "dd_p95": q(dds, 0.95),
            "prob_profit": sum(1 for r in rets if r > 0) / len(rets)}


@dataclass
class MarketEstimate:
    symbol: str
    period: str
    n_trades: int
    expectancy_gross_r: float
    expectancy_net_r: float
    avg_cost_r: float
    win_rate: float
    strategy: str
    path: EquityPath
    boot: dict
    confidence: object
    trades: list[TradeR]

    def render(self, equity: float) -> str:
        c = self.confidence
        lines = [f"{self.symbol} · {self.period} · estratégia {self.strategy}",
                 f"  operações OOS: {self.n_trades} · win {self.win_rate:.0%} · E bruta {self.expectancy_gross_r:+.2f}R · custo médio {self.avg_cost_r:.2f}R · E líquida {self.expectancy_net_r:+.2f}R (ajustada {c.shrunk:+.2f}R, conf. {c.level})",
                 f"  capital {equity:,.0f} → {self.path.end:,.2f} USD ({self.path.return_pct:+.1f}%) · drawdown máx {self.path.max_drawdown_pct:.1f}%"]
        if self.boot:
            b = self.boot
            lines.append(f"  bootstrap: retorno p5 {b['ret_p5']:+.1f}% · p50 {b['ret_p50']:+.1f}% · p95 {b['ret_p95']:+.1f}% · P(lucro) {b['prob_profit']:.0%} · DD p95 {b['dd_p95']:.1f}%")
        return "\n".join(lines)


def estimate_market(symbol: str, trade_rows: Sequence[dict], equity: float, risk_pct: float, period: str, strategy: str = "adaptive") -> MarketEstimate:
    spec = get_market(symbol)
    trades: list[TradeR] = []
    for row in trade_rows:
        r = row["results"].get(strategy, row["results"].get("3R"))
        if r is None:
            continue
        rv = row.get("r_value") or 0.0
        cost = round(spec.typical_spread / rv, 3) if rv > 0 else 0.0
        trades.append(TradeR(row["time"], symbol, round(r - cost, 3), cost, strategy))
    rs = [t.r for t in trades]
    gross = [t.r + t.cost_r for t in trades]
    path = simulate_equity(trades, equity, risk_pct)
    return MarketEstimate(symbol, period, len(trades), statistics.fmean(gross) if gross else 0.0, statistics.fmean(rs) if rs else 0.0,
                          statistics.fmean(t.cost_r for t in trades) if trades else 0.0, (sum(1 for x in rs if x > 0) / len(rs)) if rs else 0.0,
                          strategy, path, bootstrap(trades, equity, risk_pct), statistical_confidence(rs), trades)


@dataclass
class ProfitEstimate:
    start: str
    end: str
    equity: float
    risk_pct: float
    markets: list[MarketEstimate]
    portfolio: Optional[EquityPath] = None
    portfolio_boot: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"💰 ESTIMATIVA DE LUCRO — {self.start} → {self.end} · capital {self.equity:,.0f} USD · risco {self.risk_pct}%/operação (composto)",
                 "Método: walk-forward fora da amostra · custo de spread em R · curva de capital sequencial · bootstrap 1000×", ""]
        for m in sorted(self.markets, key=lambda m: -m.confidence.shrunk):
            lines += [m.render(self.equity), ""]
        if self.portfolio is not None:
            b = self.portfolio_boot
            lines.append(f"CARTEIRA (todos os mercados em sequência, capital único): {self.equity:,.0f} → {self.portfolio.end:,.2f} USD "
                         f"({self.portfolio.return_pct:+.1f}%) · drawdown máx {self.portfolio.max_drawdown_pct:.1f}%")
            if b:
                lines.append(f"  bootstrap: p5 {b['ret_p5']:+.1f}% · p50 {b['ret_p50']:+.1f}% · p95 {b['ret_p95']:+.1f}% · P(lucro) {b['prob_profit']:.0%} · DD p95 {b['dd_p95']:.1f}%")
        lines += ["", "⚠️ RESSALVAS"] + [f"  • {c}" for c in self.caveats]
        return "\n".join(lines)


DEFAULT_CAVEATS = [
    "Estimativa histórica fora da amostra, não garantia: o mercado de jan→hoje não se repete.",
    "Histórico H1 gratuito (Yahoo) não inclui notícias, COT semanal alinhado nem FRED intraday: a cobertura de fatores é menor que no `live`, "
    "logo o motor opera com menos evidência do que operaria em tempo real.",
    "Execução simulada a fechamento de candle H1 com regra conservadora de stop; slippage real, gaps e horários sem liquidez não estão modelados além do spread típico.",
    "A carteira soma as operações de todos os mercados em sequência com capital único; a exposição correlacionada do live pode ter bloqueado parte delas.",
    "Amostra < 30 operações por mercado = ⚪ inconclusivo; leia a confiança estatística antes do retorno.",
]


def estimate_profit(frames: dict, start: datetime, end: datetime, equity: float, risk_pct: float, n_folds: int = 4, step: int = 1,
                    warmup: int = 220, horizon_min: int = 240, strategy: str = "adaptive", cfg_factory=None) -> ProfitEstimate:

    period = f"{start:%Y-%m-%d} → {end:%Y-%m-%d}"
    markets: list[MarketEstimate] = []
    for symbol, frame in frames.items():
        spec = get_market(symbol)
        cfg = cfg_factory(symbol) if cfg_factory else EngineConfig(factor_signs=dict(spec.factor_signs), symbol=symbol)
        bt = Backtester(frame, cfg, warmup=warmup, step=step, horizon_min=horizon_min)
        wf = walk_forward(bt, n_folds=n_folds)
        rows = [r for _, res in wf.folds for r in res.trade_rows]
        markets.append(estimate_market(symbol, rows, equity, risk_pct, period, strategy))
    all_trades = [t for m in markets for t in m.trades]
    portfolio = simulate_equity(all_trades, equity, risk_pct) if all_trades else None
    return ProfitEstimate(f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", equity, risk_pct, markets, portfolio,
                          bootstrap(all_trades, equity, risk_pct) if all_trades else {}, list(DEFAULT_CAVEATS))


# ============================================================================
# SWEEP
# ============================================================================

"""SWEEP DE PISO — escolha do |score| mínimo de vantagem DENTRO do treino de cada fold (walk-forward).

Duas saídas, com papéis diferentes:
  1. SELEÇÃO IN-TRAIN (o número honesto): para cada fold, todos os pisos são testados no treino; o melhor
     (por objetivo) é aplicado ao teste. O agregado fora da amostra não viu nenhum resultado de teste.
  2. SENSIBILIDADE OOS POR PISO FIXO (descritiva): o mesmo piso em todos os folds de teste. Serve para
     entender a forma da curva — NUNCA para escolher o piso, porque olha o teste.
"""




DEFAULT_FLOORS: tuple[float, ...] = (10, 12, 15, 17, 20, 22, 25, 30, 35, 40)


@dataclass
class FloorMetrics:
    floor: float
    n: int
    days: float
    expectancy: float
    win_rate: float
    profit_factor: Optional[float]
    max_dd_pct: float
    capture: Optional[float]
    entry_rate: Optional[float]

    @property
    def per_day(self) -> float:
        return self.n / self.days if self.days else 0.0

    def row(self) -> str:
        pf = "n/d" if self.profit_factor is None else ("∞" if self.profit_factor == float("inf") else f"{self.profit_factor:.2f}")
        cap = "n/d" if self.capture is None else f"{self.capture:.0%}"
        return f"{self.floor:>5.0f}{self.n:>10}{self.per_day:>10.2f}{cap:>9}{self.expectancy:>+12.2f}R{pf:>7}{self.max_dd_pct:>7.1f}%{self.win_rate:>6.0%}"


def _metrics(results: list[BacktestResult], floor: float, strategy: str, equity: float, risk_pct: float) -> FloorMetrics:
    rows = [r for res in results for r in res.trade_rows]
    rs = [row["results"].get(strategy, row["results"].get("3R")) for row in rows]
    rs = [x for x in rs if x is not None]
    days = sum(((res.opportunity.period_hours if res.opportunity else 0.0) for res in results)) / 24.0
    wins, losses = [x for x in rs if x > 0], [x for x in rs if x <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else (float("inf") if wins else None)
    trades = [TradeR(row["time"], "", (row["results"].get(strategy, row["results"].get("3R")) or 0.0), 0.0, strategy) for row in rows]
    path = simulate_equity(trades, equity, risk_pct)
    caps = [res.opportunity.capture_rate for res in results if res.opportunity and res.opportunity.capture_rate is not None]
    ents = [res.opportunity.entry_rate for res in results if res.opportunity and res.opportunity.entry_rate is not None]
    return FloorMetrics(floor, len(rs), days, statistics.fmean(rs) if rs else 0.0, (len(wins) / len(rs)) if rs else 0.0, pf, path.max_drawdown_pct,
                        statistics.fmean(caps) if caps else None, statistics.fmean(ents) if ents else None)


def objective(m: FloorMetrics, min_n: int = 5) -> float:
    """Expectancy × √n (t-stat simplificado): premia edge com volume; amostra < min_n vale zero."""
    if m.n < min_n:
        return 0.0
    return m.expectancy * math.sqrt(m.n)


@dataclass
class FoldChoice:
    fold: int
    floor: float
    train: FloorMetrics
    test: FloorMetrics
    train_table: list[FloorMetrics] = field(default_factory=list)


@dataclass
class SweepResult:
    floors: tuple[float, ...]
    choices: list[FoldChoice]
    oos_intrain: FloorMetrics                # agregado OOS com o piso escolhido no treino de cada fold
    sensitivity: list[FloorMetrics]          # piso fixo em todos os folds de teste (descritivo)
    strategy: str
    default_floor: float

    def render(self) -> str:
        hdr = f"{'Piso':>5}{'Entradas':>10}{'Entr/dia':>10}{'Capture':>9}{'Expectancy':>13}{'PF':>7}{'DD':>8}{'Win':>6}"
        lines = [f"🔬 SWEEP DE PISO DE VANTAGEM — walk-forward, estratégia {self.strategy}, pisos {', '.join(f'{f:g}' for f in self.floors)}",
                 "", "1) SELEÇÃO IN-TRAIN (número honesto: o piso de cada fold foi escolhido só no treino)"]
        for c in self.choices:
            lines.append(f"  fold {c.fold}: treino escolheu piso {c.floor:g} (E={c.train.expectancy:+.2f}R, n={c.train.n}) → teste: n={c.test.n}, "
                         f"E={c.test.expectancy:+.2f}R, DD {c.test.max_dd_pct:.1f}%")
        m = self.oos_intrain
        lines.append(f"  OOS agregado: entradas {m.n} ({m.per_day:.2f}/dia) · E={m.expectancy:+.2f}R · win {m.win_rate:.0%} · "
                     f"PF {'n/d' if m.profit_factor is None else ('∞' if m.profit_factor == float('inf') else f'{m.profit_factor:.2f}')} · DD {m.max_dd_pct:.1f}%"
                     + (f" · capture {m.capture:.0%}" if m.capture is not None else ""))
        chosen = [c.floor for c in self.choices]
        if chosen:
            lines.append(f"  pisos escolhidos: {', '.join(f'{f:g}' for f in chosen)} · mediana {statistics.median(chosen):g} (padrão atual {self.default_floor:g})")
        lines += ["", "2) SENSIBILIDADE OOS POR PISO FIXO (descritiva — olha o teste; NÃO usar para escolher o piso)", hdr]
        for fm in self.sensitivity:
            lines.append(fm.row() + ("  ◀ padrão" if fm.floor == self.default_floor else ""))
        best = max((fm for fm in self.sensitivity if fm.n >= 10), key=lambda fm: objective(fm), default=None)
        if best is not None:
            lines.append(f"  maior expectancy×√n com n≥10: piso {best.floor:g} — confirme com a seleção in-train acima antes de adotar")
        return "\n".join(lines)


def threshold_sweep(bt: Backtester, floors: Sequence[float] = DEFAULT_FLOORS, n_folds: int = 4, train_folds: int = 2, strategy: str = "adaptive",
                    equity: float = 10000.0, risk_pct: float = 0.5, base_cfg: Optional[EngineConfig] = None, log=None) -> SweepResult:
    base = base_cfg or bt.cfg
    n = len(bt.frame.xau)
    usable = n - bt.warmup
    fold_len = usable // (n_folds + train_folds)

    def cfg_with(floor: float) -> EngineConfig:
        d = {**base.__dict__, "weights": dict(base.weights), "factor_signs": dict(base.factor_signs), "min_edge_score": float(floor)}
        return EngineConfig(**d)

    choices: list[FoldChoice] = []
    oos_by_floor: dict[float, list[BacktestResult]] = {f: [] for f in floors}
    chosen_results: list[BacktestResult] = []
    for k in range(n_folds):
        train_end = bt.warmup + (train_folds + k) * fold_len
        train_start = train_end - train_folds * fold_len
        test_end = min(n, train_end + fold_len)
        train_table: list[FloorMetrics] = []
        for f in floors:
            r = bt.run(train_start, train_end, cfg_with(f))
            train_table.append(_metrics([r], f, strategy, equity, risk_pct))
            if log:
                log(f"fold {k + 1} treino piso {f:g}: n={train_table[-1].n} E={train_table[-1].expectancy:+.2f}R")
        best = max(train_table, key=objective)
        if objective(best) <= 0:
            best = next((t for t in train_table if t.floor == base.min_edge_score), train_table[0])  # sem edge no treino → mantém o padrão
        test_runs: dict[float, BacktestResult] = {}
        for f in floors:
            test_runs[f] = bt.run(train_end, test_end, cfg_with(f))
            oos_by_floor[f].append(test_runs[f])
        test_m = _metrics([test_runs[best.floor]], best.floor, strategy, equity, risk_pct)
        chosen_results.append(test_runs[best.floor])
        choices.append(FoldChoice(k + 1, best.floor, best, test_m, train_table))
    oos_intrain = _metrics(chosen_results, float("nan"), strategy, equity, risk_pct)
    sensitivity = [_metrics(oos_by_floor[f], f, strategy, equity, risk_pct) for f in floors]
    return SweepResult(tuple(floors), choices, oos_intrain, sensitivity, strategy, base.min_edge_score)


# ============================================================================
# ABLATION
# ============================================================================

"""TESTE A/B — o que a informação adiciona ao cérebro (MARKET AI 4.0).

Mesmo período, mesmo walk-forward, mesmo piso; muda só o que o cérebro pode ver:
  • Preço somente            — sem banco histórico (news_mode = none)
  • Preço + Macro (TESTE A)  — calendário macro/bancos centrais point-in-time (news_mode = macro)
  • Preço + Macro + News (B) — + manchetes, tom e intensidade (GDELT) (news_mode = full)

Se a informação cria a oportunidade, entradas e expectancy sobem SEM mexer no funil. Só depois recalibra-se o piso.
"""




MODES: tuple[tuple[str, str], ...] = (("none", "Preço somente"), ("macro", "Preço + Macro (A)"), ("full", "Preço + Macro + News (B)"))


@dataclass
class ModeResult:
    market: str
    mode: str
    label: str
    metrics: FloorMetrics
    steps: int
    steps_with_info: int
    news_known_steps: int

    @property
    def info_share(self) -> float:
        return self.steps_with_info / self.steps if self.steps else 0.0

    def row(self) -> str:
        m = self.metrics
        pf = "n/d" if m.profit_factor is None else ("∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}")
        cap = "n/d" if m.capture is None else f"{m.capture:.0%}"
        return (f"{self.market:<8}{self.label:<27}{self.steps:>7}{self.info_share:>8.0%}{m.n:>9}{m.per_day:>8.2f}{cap:>9}{m.expectancy:>+12.2f}R{pf:>7}"
                f"{m.max_dd_pct:>7.1f}%{m.win_rate:>7.0%}")


@dataclass
class AblationReport:
    start: str
    end: str
    history_stats: str
    results: list[ModeResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_market(self) -> dict[str, dict[str, ModeResult]]:
        out: dict[str, dict[str, ModeResult]] = {}
        for r in self.results:
            out.setdefault(r.market, {})[r.mode] = r
        return out

    def verdicts(self) -> list[str]:
        lines = []
        for mkt, modes in self.by_market().items():
            base = modes.get("none")
            if base is None:
                continue
            for mode, label in MODES[1:]:
                r = modes.get(mode)
                if r is None:
                    continue
                if r.steps_with_info == 0:
                    lines.append(f"{mkt} · {label}: banco sem cobertura neste período (0 passos com informação) — nada a concluir; rode `history stats`.")
                    continue
                d_exp = r.metrics.expectancy - base.metrics.expectancy
                d_n = r.metrics.n - base.metrics.n
                n = min(r.metrics.n, base.metrics.n)
                strength = "⚪ inconclusivo (amostra < 30)" if n < 30 else ("🟢 melhora" if d_exp > 0.05 else "🔴 piora" if d_exp < -0.05 else "🟡 sem diferença")
                lines.append(f"{mkt} · {label}: entradas {base.metrics.n} → {r.metrics.n} ({d_n:+d}), expectancy {base.metrics.expectancy:+.2f}R → "
                             f"{r.metrics.expectancy:+.2f}R ({d_exp:+.2f}R), cobertura {r.info_share:.0%} dos passos · {strength}")
        return lines

    def render(self) -> str:
        head = (f"🧪 TESTE A/B — O QUE A INFORMAÇÃO ADICIONA · {self.start} → {self.end} (walk-forward OOS, mesmo piso, mesma janela)\n"
                f"banco histórico: {self.history_stats}\n\n"
                f"{'mercado':<8}{'modo':<27}{'passos':>7}{'c/info':>8}{'entradas':>9}{'ent/dia':>8}{'capture':>9}{'expectancy':>13}{'PF':>7}{'DD':>8}{'acerto':>7}")
        rows = [r.row() for r in self.results]
        out = [head] + rows + ["", "LEITURA (Preço somente = referência):"] + [f"  • {v}" for v in self.verdicts()]
        if self.notes:
            out += ["", "NOTAS:"] + [f"  • {n}" for n in self.notes]
        out += ["", "REGRA: se a informação cria entradas com expectancy ≥ referência, ela fica; o funil só é recalibrado depois (`sweep`).",
                "       Efeitos por ativo: regras macro até `history learn` substituí-los pelo que o histórico mostrou (nunca inventados)."]
        return "\n".join(out)


def _info_steps(results, hist: EventHistory, mode: str) -> tuple[int, int]:
    """Passos OOS (um por decisão do backtest) e quantos deles tinham evento/notícia publicada naquele instante."""
    total, with_info = 0, 0
    for res in results:
        for d in res.decisions:
            total += 1
            if mode == "none":
                continue
            avail = hist.available_at(d.time)
            if mode == "macro":
                avail = [e for e in avail if e.category in ("MACRO", "CENTRAL_BANK")]
            if avail:
                with_info += 1
    return total, with_info


def compare_information(frames: dict[str, HistoryFrame], hist: EventHistory, start: datetime, end: datetime, equity: float = 10000.0, risk_pct: float = 0.5,
                        n_folds: int = 4, step: int = 1, warmup: int = 220, horizon_min: int = 240, strategy: str = "adaptive",
                        cfg_factory: Optional[Callable[[str], EngineConfig]] = None, modes: tuple[str, ...] = ("none", "macro", "full"),
                        log: Optional[Callable[[str], None]] = None) -> AblationReport:
    rep = AblationReport(f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}", hist.stats())
    labels = dict(MODES)
    for symbol, frame in frames.items():
        spec = get_market(symbol)
        for mode in modes:
            cfg = cfg_factory(symbol) if cfg_factory else EngineConfig(factor_signs=dict(spec.factor_signs), symbol=symbol)
            frame.symbol, frame.events, frame.news_mode = symbol, (hist if mode != "none" else None), mode
            bt = Backtester(frame, cfg, warmup=warmup, step=step, horizon_min=horizon_min)
            wf = walk_forward(bt, n_folds=n_folds)
            results = [res for _, res in wf.folds]
            m = _metrics(results, cfg.min_edge_score, strategy, equity, risk_pct)
            steps, with_info = _info_steps(results, hist, mode)
            rep.results.append(ModeResult(symbol, mode, labels[mode], m, steps, with_info, with_info))
            if log:
                log(f"{symbol} · {labels[mode]}: {m.n} entradas OOS, expectancy {m.expectancy:+.2f}R")
        frame.events, frame.news_mode = None, "full"
    if not any(r.steps_with_info for r in rep.results if r.mode != "none"):
        rep.notes.append("Nenhum passo do backtest teve evento/notícia disponível: o banco não cobre o período ou não foi carregado.")
    rep.notes.append("Reação real medida no fechamento do candle H1 seguinte ao evento (12:30 → 13:00); a sequência 12:29→12:31→12:35 exige histórico M1/M5.")
    rep.notes.append("Sem consenso (ALFRED) a surpresa é vs. o dado anterior; com Trading Economics é vs. o consenso — a regra macro lê a surpresa, não o nível.")
    return rep


# ============================================================================
# LIVE_ENGINE
# ============================================================================

"""GOLD AI ENGINE 3.0 — LIVE EXECUTION ENGINE (ciclo completo).

DADOS → SNAPSHOT → PREDICTOR → PRE-MOVE → DECISION ENGINE → TRADE PLAN → RISK ENGINE → POSITION SIZE
→ MT5 EXECUTOR → BROKER → CONFIRMAÇÃO → 🔄 TRADE MONITOR → MANTER/PROTEGER/ENCERRAR → RESULTADO
→ SQLITE → PERFORMANCE → NOVO CAPITAL → NOVO POSITION SIZE → PRÓXIMO.

Três cérebros: PREDICTION ENGINE (GoldAIEngine) · TRADE ENGINE (MaxProfitEngine/StopEngine/sizing) ·
TRADE MONITOR (monitor.TradeMonitor). O núcleo preditivo (2.1–2.3) não é alterado.
Nasce em PAPER. LIVE exige autorização explícita.
"""





@dataclass
class CycleResult:
    assessment: Optional[Assessment]
    signal: Optional[Signal]
    decision: str = ""
    pid: Optional[int] = None
    plan: Optional[TradePlan] = None
    readings: list[tuple[ManagedTrade, MonitorReading]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class LiveExecutionEngine:
    EXECUTABLE = {SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL, SignalType.PRE_MOVE}

    def __init__(self, mem: PredictionMemory, limits: GuardLimits, mode: TradingMode = TradingMode.PAPER, equity: float = 10000.0,
                 executor=None, sender: Optional[TelegramSender] = None, kill_switch: Optional[KillSwitch] = None,
                 commands: Optional[TelegramCommands] = None, horizon_min: int = 240, engine: Optional[GoldAIEngine] = None,
                 log: Callable[[str], None] = print, authorized: bool = False, spec=None, perf: Optional[PerformanceEngine] = None,
                 entry_gate: Optional[Callable] = None) -> None:
        """`spec` (markets.MarketSpec) torna o motor específico de um mercado; `perf` permite capital compartilhado
        entre mercados (4.0); `entry_gate(symbol, direction, risk_usd)` → lista de bloqueios do portfólio (exposição)."""
        self.spec = spec or get_market("XAUUSD")
        self.symbol = self.spec.symbol
        self.entry_gate = entry_gate
        self.mem, self.limits, self.mode = mem, limits, mode
        self.executor = executor                      # execution.ExecutionEngine (LIVE / SEMI_LIVE / AUTHORIZE com autorização)
        self.sender = sender or TelegramSender(dry_run=True)
        self.ks = kill_switch or KillSwitch()
        self.commands = commands
        self.horizon = horizon_min
        self.engine = engine or GoldAIEngine()
        self.log = log
        self.authorized = authorized                  # AUTHORIZE: autorização dada para a próxima entrada
        start_equity = mem.last_equity() or equity
        self.perf = perf or PerformanceEngine(limits, start_equity)
        if mem.last_equity() is None:
            mem.record_equity(datetime.now(timezone.utc), start_equity, None, "capital inicial")
        if engine is not None and self.spec.factor_signs and not engine.cfg.factor_signs:
            engine.cfg.factor_signs, engine.cfg.symbol = dict(self.spec.factor_signs), self.symbol
        self.monitor = TradeMonitor(history=mem.r_stats(self.symbol))
        self.mpe = MaxProfitEngine(StopEngine(limits), mem.r_stats(self.symbol), horizon_min, limits.min_rr_to_structure)
        self.managed: list[ManagedTrade] = mem.managed_trades(self.symbol)
        self.tickets: dict[int, int] = {}             # trade_id → ticket no broker
        for tr in self.managed:
            row = mem.conn.execute("SELECT ticket FROM trades WHERE id=?", (tr.trade_id,)).fetchone()
            if row and row["ticket"]:
                self.tickets[tr.trade_id] = int(row["ticket"])
        self.pending_close_confirm: list[int] = []

    # ------------------------------------------------------------------ util
    def _send(self, text: str, res: CycleResult) -> None:
        res.messages.append(text)
        self.sender.send(text)

    def status_text(self) -> str:
        return format_status(self.perf, self.ks, self.managed, self.mode.value)

    # ------------------------------------------------------------------ comandos
    def handle_commands(self, res: CycleResult, now: datetime) -> None:
        if self.commands is None:
            return
        for action in self.commands.apply(self.commands.poll(), self.ks):
            if action == "EDGE":
                continue  # tratado pelo MarketAIEngine (LIVE EDGE sob demanda)
            if action == "STATUS":
                self._send(self.status_text(), res)
            elif action in ("STOP", "PAUSE", "RESUME"):
                self._send(f"🔧 comando /{action} aplicado — " + self.ks.new_entries_allowed()[1], res)
            elif action == "CLOSE_REQUESTED":
                self._send(f"⚠️ /CLOSE solicitado para {len(self.managed)} posição(ões). Responda /CLOSE CONFIRM para encerrar.", res)
            elif action == "CLOSE_CONFIRMED":
                for tr in list(self.managed):
                    self._close_trade(tr, tr.r_at(tr.plan.entry), "MANUAL", now, res, price_hint=None)
                self._send("🔴 posições encerradas por /CLOSE CONFIRM", res)

    # ------------------------------------------------------------------ ciclo
    def run_cycle(self, snap: MarketSnapshot, new_event_key: Optional[str] = None, defer_entry: bool = False) -> CycleResult:
        res = CycleResult(None, None)
        now = snap.time
        self.handle_commands(res, now)
        if not snap.candles:
            res.notes.append("sem candles XAU — ciclo abortado")
            return res
        fine = snap.candles.get("M1") or snap.candles.get("M5") or snap.candles.get("M15") or []
        # capital: em LIVE/SEMI_LIVE vem do broker
        if self.executor is not None and self.mode in (TradingMode.LIVE, TradingMode.SEMI_LIVE):
            eq = self.executor.account_equity()
            if eq:
                before = self.perf.equity
                self.perf.sync_equity(eq, now)
                if abs(eq - before) > 0.005:
                    self.mem.record_equity(now, eq, round(eq - before, 2), "sync broker")
        # resolve previsões e operações simuladas pendentes; atualiza histórico
        for pid, out in self.mem.auto_resolve(fine, now, snap.atr or 5.0, self.horizon):
            res.notes.append(f"[memória] previsão #{pid} → {out.result} (lead {out.time_to_reaction_min}, MFE {out.mfe}, MAE {out.mae})")
        for tid, sim in self.mem.auto_resolve_trades(fine, now):
            pr = sim["profile"]
            res.notes.append(f"[trade] operação #{tid} resolvida no horizonte → max {pr.max_r_before_stop:.2f}R, MAE {pr.mae_r:.2f}R")
        self.mpe.history = self.monitor.history = self.mem.r_stats(self.symbol)
        self.engine.expected_lead_min = self.mem.lead_time_stats()["media"]

        self.mem.store_prices(fine)
        self.mem.resolve_hypotheticals(now, self.horizon)
        a, sig = self.engine.run_cycle(snap, new_event_key=new_event_key)
        res.assessment, res.signal = a, sig
        # 🔄 TRADE MONITOR — toda posição aberta é reavaliada antes de qualquer nova decisão
        for tr in list(self.managed):
            self._monitor_trade(tr, a, snap, fine, res)
        res.pid = None
        if sig is not None:
            self._send(sig.text, res)
            res.pid = self.mem.record(a, sig.type.value, atr=snap.atr, horizon_min=self.horizon, symbol=self.symbol)
        if defer_entry:
            res.decision = "ANALISADO — decisão de entrada delegada ao Asset Selector" if sig is not None else "SEM SINAL — " + a.edge_status
            return res
        return self.enter(res, snap)

    def enter(self, res: CycleResult, snap: MarketSnapshot, veto: Optional[str] = None) -> CycleResult:
        """DECISION ENGINE. `veto` = motivo externo (Asset Selector/exposição) para não entrar neste ciclo."""
        a, sig = res.assessment, res.signal
        if a is None:
            return res
        if sig is not None and veto:
            res.decision = veto
        elif sig is not None:
            res.decision = self._decide_entry(sig, a, snap, res.pid or 0, res)
        else:
            res.decision = "SEM SINAL — " + a.edge_status
        # OPPORTUNITY ENGINE + FUNIL: toda análise vira um registro (entrada, ou a primeira etapa em que caiu)
        direction = a.direction if a.direction != Direction.LATERAL else a.premove.direction
        is_raw, stage = funnel_stage(a, sig, self.engine.gate.last_reason, res.decision, self.engine.cfg)
        self.mem.record_decision(DecisionRecord(snap.time, a.price, a.score, direction.value, classify_reason(res.decision), res.decision,
                                                snap.atr or 0.0, None, int(a.evidence_level), a.confidence), symbol=self.symbol, stage=stage, is_raw=is_raw)
        return res

    # ------------------------------------------------------------------ entrada
    def _decide_entry(self, sig: Signal, a: Assessment, snap: MarketSnapshot, pid: int, res: CycleResult) -> str:
        if sig.type not in self.EXECUTABLE or sig.direction == Direction.LATERAL:
            return f"NO_TRADE — sinal {sig.type.value} não é operacional"
        allowed, why = self.ks.new_entries_allowed()
        if not allowed:
            return f"BLOQUEADA — {why}"
        blocks = self.perf.blocks(a.time)
        if blocks:
            self._send("\n".join(blocks), res)
            return "BLOQUEADA — " + "; ".join(blocks)
        spread = None
        if self.executor is not None:
            try:
                bid, ask = self.executor.client.tick()
                spread = round(ask - bid, 2)
            except Exception:  # noqa: BLE001
                spread = None
        reasons = no_trade_check(a, self.limits, spread)
        if reasons:
            return "🟡 NÃO OPERAR — " + "; ".join(reasons)
        # uma posição por ativo (memória + broker)
        if self.managed or (self.executor is not None and self.executor.positions()):
            return f"BLOQUEADA — já existe posição ativa em {self.symbol} (MAX_POSITIONS)"
        plan = self.mpe.plan(a, snap, sig.direction, sig.type.value)
        res.plan = plan
        if not plan.viable:
            return "🟡 NÃO OPERAR — " + "; ".join(n for n in plan.notes if n.startswith("⚠️"))
        plan.lots, plan.risk_usd = size_lots(self.limits, self.perf.risk_usd, plan.r_value, self.spec.point_value_usd)   # capital + risco + stop + contrato
        if not plan.lots:
            return f"BLOQUEADA — risco de {self.perf.risk_usd:.2f} USD não comporta o lote mínimo com stop de {plan.r_value:.2f}"
        if self.entry_gate is not None:
            blocked = self.entry_gate(self.symbol, sig.direction, plan.risk_usd)
            if blocked:
                return "BLOQUEADA — exposição de carteira: " + "; ".join(blocked)
        self.log(plan.render())
        if self.mode == TradingMode.AUTHORIZE and not self.authorized:
            self._send("🟡 AGUARDANDO AUTORIZAÇÃO\n" + plan.render(), res)
            return "AGUARDANDO AUTORIZAÇÃO"
        execution = None
        if self.mode != TradingMode.PAPER:
            if self.executor is None:
                return "BLOQUEADA — sem executor MT5 configurado"
            execution = self.executor.open(plan, comment=f"GoldAI {sig.type.value}"[:31])
            self.log(execution.render())
            if execution.error:
                self._send("❌ " + execution.render(), res)
                return "FALHA DE EXECUÇÃO — " + execution.error
            if execution.mismatches:
                self._send("⚠️ " + execution.render(), res)
                if any(m.startswith("SL real") for m in execution.mismatches):
                    self.executor.close(execution.ticket)
                    return "EXECUTION MISMATCH — posição sem SL correto foi encerrada por segurança"
            self.authorized = False
        tid = self.mem.open_trade(plan, self.mode.value, pid, self.horizon, symbol=self.symbol)
        thesis = Thesis.from_assessment(a, plan.direction)
        tr = ManagedTrade(tid, plan, thesis)
        if execution is not None:
            self.tickets[tid] = execution.ticket
            tr.plan.entry = execution.fill_price or plan.entry
        self.mem.save_thesis(tid, thesis, tr.state_dict())
        self.mem.save_execution(tid, execution, self.perf.equity, self.limits.risk_per_trade_pct, a)
        self.managed.append(tr)
        self._send(format_entry(plan, a, self.mode.value, execution, self.symbol), res)
        return f"{'🟢 POSITION OPEN' if execution else '🟢 PAPER OPEN'} #{tid:05d}"

    # ------------------------------------------------------------------ monitor
    def _monitor_trade(self, tr: ManagedTrade, a: Assessment, snap: MarketSnapshot, fine, res: CycleResult) -> None:
        now = snap.time
        ticket = self.tickets.get(tr.trade_id)
        # 1) o broker fechou (stop/TP)?
        if ticket and self.executor is not None:
            closed = self.executor.closed_result(ticket)
            if closed is not None:
                r_exit = tr.r_at(closed["price"]) if closed.get("price") else tr.stop_r
                tr.close(r_exit, "BROKER", closed.get("time") or now)
                self._finalize(tr, now, res, pnl_usd=closed.get("profit"))
                return
        # 2) caminho do preço desde a última leitura (stop/trailing) + reavaliação da tese
        before = (tr.remaining, tr.stop_r)
        reading = self.monitor.check_path(tr, fine)
        from_path = reading is not None
        if reading is None and tr.status == "OPEN":
            reading = self.monitor.evaluate(tr, a, snap)
        if reading is None:
            return
        if from_path:
            if tr.status == "CLOSED" and ticket and self.executor is not None and self.executor.position(ticket) is not None:
                self.executor.close(ticket)                    # stop lógico tocado antes de sincronizar com o broker
            elif tr.status == "OPEN" and ticket and self.executor is not None and abs(tr.stop_r - before[1]) > 1e-9:
                self.executor.modify(ticket, tr.price_at_r(tr.stop_r), None if tr.extending else (tr.plan.targets.get(tr.plan.recommended) or None))
        else:
            self._apply_to_broker(tr, reading, before, res)   # ENCERRAR / parcial / trailing / zero a zero chegam ao broker
        self.mem.log_monitor(tr.trade_id, reading)
        res.readings.append((tr, reading))
        self.log(render_monitor(tr, reading))
        if tr.status == "CLOSED":
            self._finalize(tr, now, res)
        else:
            self.mem.save_state(tr.trade_id, tr.state_dict())
            if reading.action == "PROTEGER":
                self._send(format_protection(tr, reading), res)
            elif reading.action in ("REDUZIR", "ESTENDER"):
                self._send("📊 GOLD AI MONITOR\n" + render_monitor(tr, reading), res)

    def _apply_to_broker(self, tr: ManagedTrade, reading: MonitorReading, before: tuple[float, float], res: CycleResult) -> None:
        """Traduz a decisão do monitor em ações no broker (LIVE / SEMI_LIVE)."""
        ticket = self.tickets.get(tr.trade_id)
        if not ticket or self.executor is None:
            return
        remaining_before, stop_before = before
        if reading.action == "ENCERRAR" and tr.status == "CLOSED":
            in_profit = reading.current_r > 0
            if self.mode == TradingMode.SEMI_LIVE and in_profit and not self.ks.paused:
                # ação crítica em SEMI_LIVE: pede confirmação, mas protege com stop no zero a zero
                self.executor.modify(ticket, tr.plan.entry, None)
                tr.status, tr.close_reason, tr.result_r, tr.closed_at = "OPEN", "", None, None
                tr.remaining = remaining_before
                tr.stop_r = max(stop_before, 0.0)
                self.pending_close_confirm.append(tr.trade_id)
                self._send(f"🟠 SEMI-LIVE: monitor pede ENCERRAR #{tr.trade_id:05d} com lucro ({reading.current_r:+.2f}R). Stop movido ao zero a zero. Responda /CLOSE CONFIRM.", res)
                reading.action, reading.note = "PROTEGER", reading.note + " (aguardando confirmação para encerrar)"
                return
            ok, price = self.executor.close(ticket)
            if ok and price is not None:
                tr.result_r = round(tr.realized_r + (remaining_before) * tr.r_at(price), 3) if tr.result_r is None else tr.result_r
            return
        if tr.remaining < remaining_before:  # parcial (PROTEGER/REDUZIR)
            pos = self.executor.position(ticket)
            if pos is not None:
                vol = round(pos.volume * (1 - tr.remaining / remaining_before), 2)
                vol = max(self.limits.min_lot, vol)
                if vol < pos.volume:
                    self.executor.close(ticket, vol, "GoldAI partial")
        if abs(tr.stop_r - stop_before) > 1e-9:
            self.executor.modify(ticket, tr.price_at_r(tr.stop_r), None if tr.extending else (tr.plan.targets.get(tr.plan.recommended) or None))

    def _close_trade(self, tr: ManagedTrade, r_exit: float, reason: str, now: datetime, res: CycleResult, price_hint: Optional[float]) -> None:
        ticket = self.tickets.get(tr.trade_id)
        if ticket and self.executor is not None:
            ok, price = self.executor.close(ticket)
            if ok and price is not None:
                r_exit = tr.r_at(price)
        tr.close(r_exit, reason, now)
        self._finalize(tr, now, res)

    def _finalize(self, tr: ManagedTrade, now: datetime, res: CycleResult, pnl_usd: Optional[float] = None) -> None:
        risk = tr.plan.risk_usd or 0.0
        pnl = pnl_usd if pnl_usd is not None else round((tr.result_r or 0.0) * risk, 2)
        minutes = (now - tr.plan.time).total_seconds() / 60
        self.mem.close_managed(tr.trade_id, tr.result_r or 0.0, tr.close_reason, now, tr.state_dict())
        # previsão correta? lead time?
        correct = (tr.result_r or 0.0) > 0
        lead = next((h.time for h in tr.history if h.current_r >= 1.0), None)
        lead_min = (lead - tr.plan.time).total_seconds() / 60 if lead else None
        self.mem.save_financial_result(tr.trade_id, pnl, round(minutes, 1), lead_min)
        self.perf.record_result(pnl, now, f"trade #{tr.trade_id}")
        self.mem.record_equity(now, self.perf.equity, pnl, f"trade #{tr.trade_id} {tr.close_reason}")
        self.log(render_evolution(tr))
        self.log(self.perf.render())
        if tr.close_reason in ("TESE INVALIDADA", "EXIT SCORE"):
            self._send(format_scenario_change(tr, tr.history[-1]), res)
        self._send(format_result(tr, pnl, correct, lead_min, minutes), res)
        if tr in self.managed:
            self.managed.remove(tr)
        self.tickets.pop(tr.trade_id, None)
        if self.perf.trading_stop:
            self._send("🚨 TRADING STOP — perda diária máxima atingida; sem novas entradas hoje", res)
        if self.perf.target_reached and not getattr(self, "_target_announced", None) == self.perf.day:
            self._target_announced = self.perf.day
            self._send(f"🎯 META DIÁRIA ATINGIDA — {self.perf.daily_pct:+.1f}% hoje ({self.perf.daily_pnl:+.2f} USD). Ganho protegido: sem novas entradas até amanhã; "
                       "posições abertas seguem com o monitor.", res)


# ============================================================================
# DATA · MULTI
# ============================================================================

"""MARKET AI ENGINE 4.0 — dados multi-mercado sobre o Data Engine existente.

Macro (dólar, juros, Fed, inflação, geopolítica, risco sistêmico, notícias, COT do ouro) é coletada UMA vez;
cada mercado recebe os próprios candles/preço/ATR/fluxo (Yahoo ou MT5) e o COT do próprio contrato quando houver.
"""


import copy



@dataclass
class MarketSnapshotSet:
    time: datetime
    base: MarketSnapshot                       # snapshot macro (convenção do ouro)
    by_symbol: dict[str, MarketSnapshot] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    data_quality: dict[str, float] = field(default_factory=dict)
    identified: list = field(default_factory=list)     # eventos identificados pelo NEWS ENGINE neste ciclo


MACRO_FIELDS = ("dxy", "dxy_change_pct", "us2y", "us10y", "us10y_change_bp", "real_yield_10y", "real_yield_change_bp", "breakeven_10y_change_bp",
                "fed_cut_prob_change_pp", "fed_tone", "inflation_surprise_sigma", "inflation_trend", "geopolitical_risk", "geopolitical_risk_change",
                "vix", "vix_change_pct", "credit_spread_bp", "credit_spread_change_bp", "equity_change_pct", "bank_stress", "sentiment", "sentiment_change",
                "silver_change_pct", "oil_change_pct", "btc_change_pct", "usdcnh_change_pct", "news", "events")


def derive_market_snapshot(base: MarketSnapshot, spec: MarketSpec, candles: dict, now: datetime, window_minutes: int = 60,
                           cot: Optional[dict] = None, identified=None) -> MarketSnapshot:
    """Snapshot do mercado: macro compartilhada + candles/preço/ATR/fluxo próprios."""
    s = MarketSnapshot(time=now)
    for f in MACRO_FIELDS:
        setattr(s, f, copy.copy(getattr(base, f)))
    s.candles = candles
    ref = candles.get("H1") or candles.get("M15") or []
    if ref:
        s.price = ref[-1].close
        s.atr = (_atr(candles["H1"]) or 0.0) if "H1" in candles else 0.0
        fine = candles.get("M5") or ref
        cutoff = fine[-1].time.timestamp() - window_minutes * 60
        prev = next((c for c in reversed(fine[:-1]) if c.time.timestamp() <= cutoff), fine[0])
        s.price_change_pct = (fine[-1].close / prev.close - 1) * 100 if prev.close else 0.0
        recent = [c for c in fine if (now - c.time).total_seconds() <= window_minutes * 60]
        vol = sum(c.volume for c in recent)
        if vol > 0:
            s.order_flow_imbalance = round((sum(c.volume for c in recent if c.close > c.open) - sum(c.volume for c in recent if c.close < c.open)) / vol, 3)
    if spec.symbol == "XAUUSD":
        s.cot_managed_money_net, s.cot_managed_money_net_change = base.cot_managed_money_net, base.cot_managed_money_net_change
        s.cot_managed_money_percentile, s.cot_commercial_net_change = base.cot_managed_money_percentile, base.cot_commercial_net_change
        s.etf_flow_musd, s.put_call_ratio, s.implied_vol_change_pct = base.etf_flow_musd, base.put_call_ratio, base.implied_vol_change_pct
        s.gamma_wall_above, s.gamma_wall_below = base.gamma_wall_above, base.gamma_wall_below
    elif cot:
        s.cot_managed_money_net, s.cot_managed_money_net_change = cot.get("net"), cot.get("change")
        s.cot_managed_money_percentile, s.cot_commercial_net_change = cot.get("percentile"), cot.get("commercial_change")
        s.cot_age_days, s.cot_report_date = cot.get("age_days"), cot.get("report_date")
    if spec.symbol == "XAUUSD":
        s.cot_age_days, s.cot_report_date = base.cot_age_days, base.cot_report_date
    # NEWS ENGINE por mercado: identificação → importância → surpresa → direção esperada → reação → pressão latente
    if identified is not None:
        na = NewsEngine().assess(spec.symbol, s, identified, now)
        s.news_pressure, s.news_status, s.news_chain = na.pressure, na.status, na.chain
    return s


def data_quality(s: MarketSnapshot, spec: MarketSpec) -> float:
    """Fração dos fatores relevantes para o mercado com dado disponível."""
    checks = {
        "dolar": s.dxy_change_pct is not None, "juros_reais": s.real_yield_change_bp is not None or s.us10y_change_bp is not None,
        "fed": s.fed_cut_prob_change_pp is not None or s.fed_tone is not None, "inflacao": s.inflation_surprise_sigma is not None,
        "geopolitica": s.geopolitical_risk is not None, "fluxo": s.order_flow_imbalance is not None, "cot": s.cot_managed_money_net_change is not None,
        "opcoes": s.put_call_ratio is not None, "sentimento": s.sentiment is not None, "tecnico": bool(s.candles.get("H1")),
    }
    relevant = [k for k, sign in spec.factor_signs.items() if sign != 0]
    if not relevant:
        return 0.0
    return round(sum(1 for k in relevant if checks.get(k)) / len(relevant), 2)


class MultiMarketData:
    """Coleta macro uma vez e candles por mercado (Yahoo por padrão; MT5 quando `mt5_client` é fornecido)."""

    def __init__(self, symbols: tuple[str, ...], cfg: Optional[DataEngineConfig] = None, http: Optional[HttpClient] = None,
                 mt5_client: Any = None, mt5_symbol_map: Optional[dict[str, str]] = None) -> None:
        self.specs = [get_market(s) for s in symbols]
        self.engine = DataEngine(cfg, http)
        self.yahoo = YahooCollector(self.engine.http)
        self.mt5 = mt5_client
        self.mt5_symbol_map = mt5_symbol_map or {}
        self.status: dict[str, str] = {}

    @classmethod
    def symbol_map_from_env(cls, env: dict[str, str]) -> dict[str, str]:
        return {sym: env[f"MT5_SYMBOL_{sym}"] for sym in MARKETS if f"MT5_SYMBOL_{sym}" in env}

    def market_candles(self, spec: MarketSpec) -> dict:
        if self.mt5 is not None:
            orig = self.mt5.cfg.symbol
            self.mt5.cfg.symbol = self.mt5_symbol_map.get(spec.symbol, spec.mt5)
            try:
                if not self.mt5.connected:
                    self.mt5.connect()
                self.mt5.mt5.symbol_select(self.mt5.cfg.symbol, True)
                return {tf: cs for tf in TF_TO_MT5 if (cs := self.mt5.candles(tf))}
            finally:
                self.mt5.cfg.symbol = orig
        return self.yahoo.all_timeframes(spec.yahoo)

    def market_cot(self, spec: MarketSpec, now: datetime) -> Optional[dict]:
        if not spec.cftc_code or spec.symbol == "XAUUSD" or not self.engine.cfg.enable_cot:
            return None
        data = self.engine.cot_with_cache(spec.cftc_code, now)
        if data is None:
            self.status[f"cot:{spec.symbol}"] = "erro: indisponível e sem cache"
            return None
        if data.get("from_cache"):
            self.status[f"cot:{spec.symbol}"] = f"último válido {data['report_date']} ({data['age_days']:.0f} dias)"
        return data

    def collect(self, now: Optional[datetime] = None) -> MarketSnapshotSet:
        now = now or datetime.now(timezone.utc)
        base = self.engine.collect(now)          # macro + XAU
        self.status = dict(self.engine.status)
        out = MarketSnapshotSet(now, base)
        identified = EventIdentifier().identify(base.news, base.events, now)
        self.identified = identified
        out.identified = identified
        for spec in self.specs:
            try:
                candles = base.candles if spec.symbol == "XAUUSD" and base.candles else self.market_candles(spec)
                if not candles:
                    raise RuntimeError("sem candles")
                s = derive_market_snapshot(base, spec, candles, now, self.engine.cfg.window_minutes, self.market_cot(spec, now), identified)
                out.by_symbol[spec.symbol] = s
                out.data_quality[spec.symbol] = data_quality(s, spec)
                self.status[spec.symbol] = "ok"
            except Exception as e:  # noqa: BLE001
                self.status[spec.symbol] = f"erro: {e}"
        out.status = dict(self.status)
        return out

    def coverage(self) -> str:
        ok = [k for k, v in self.status.items() if v == "ok"]
        bad = [f"  ✗ {k}: {v}" for k, v in self.status.items() if v != "ok"]
        lines = [f"MULTI-MARKET DATA — ok: {', '.join(ok) or 'nenhum'}"] + bad
        if self.engine.cfg.enable_news:
            lines.append(render_feed_health(self.engine.news.health))
            n = len(getattr(self, "identified", []))
            lines.append(f"NEWS ENGINE: {n} evento(s) identificado(s) na janela" + ("" if n else " → NEWS = UNKNOWN em todos os mercados (peso reduzido, não negativo)"))
        return "\n".join(lines)


# ============================================================================
# MARKET_ENGINE
# ============================================================================

"""MARKET AI ENGINE 4.0 — cérebro único · múltiplos mercados · seleção dinâmica da melhor oportunidade.

Objetivo: "Analisar vários mercados simultaneamente e operar somente aquele que apresentar a melhor
vantagem estatística disponível naquele momento, respeitando risco, correlação, qualidade dos dados
e custo de execução." A IA não precisa operar ouro; precisa encontrar onde existe vantagem.

Preserva integralmente os motores do 3.0 (um LiveExecutionEngine por mercado, capital compartilhado).
O Asset Selector NÃO cria entradas: só ordena as que o Prediction/Opportunity Engine já produziu.
Nenhum filtro de entrada novo: os vetos do 4.0 são exclusivamente de PORTFÓLIO (exposição/correlação)
e de PRIORIDADE (um ciclo, uma entrada: a melhor).
"""





@dataclass
class PortfolioCycle:
    time: datetime
    results: dict[str, CycleResult] = field(default_factory=dict)
    ranked: list[Candidate] = field(default_factory=list)
    chosen: Optional[str] = None
    decision: str = ""
    messages: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"🌎 MARKET AI — ciclo {self.time:%Y-%m-%d %H:%M} UTC"]
        for sym, r in self.results.items():
            a = r.assessment
            if a is None:
                lines.append(f"  {sym:<7} sem dados")
                continue
            lines.append(f"  {sym:<7} score {a.score:+4.0f} prob {max(a.prob_up, a.prob_down):.0%} {a.regime:<8} {a.premove.stage.value:<14} "
                         f"{'sinal ' + r.signal.type.value if r.signal else 'sem sinal'} → {r.decision}")
        lines.append(f"DECISÃO: {self.decision}")
        return "\n".join(lines)


class MarketAIEngine:
    def __init__(self, mem: PredictionMemory, limits: GuardLimits, symbols: tuple[str, ...], mode: TradingMode = TradingMode.PAPER,
                 equity: float = 10000.0, portfolio: Optional[PortfolioLimits] = None, executors: Optional[dict] = None,
                 sender: Optional[TelegramSender] = None, kill_switch: Optional[KillSwitch] = None, commands: Optional[TelegramCommands] = None,
                 horizon_min: int = 240, log: Callable[[str], None] = print, authorized: bool = False,
                 selector: Optional[AssetSelector] = None, calibrator=None) -> None:
        self.mem = mem
        self.specs: dict[str, MarketSpec] = {s: get_market(s) for s in symbols}
        self.mode, self.limits = mode, limits
        self.portfolio = PortfolioExposureEngine(portfolio or PortfolioLimits())
        self.sender = sender or TelegramSender(dry_run=True, quiet=True)
        self.ks = kill_switch or KillSwitch()
        self.commands = commands
        self.log = log
        self.selector = selector or AssetSelector()
        start_equity = mem.last_equity() or equity
        self.perf = PerformanceEngine(limits, start_equity)   # capital ÚNICO compartilhado
        if mem.last_equity() is None:
            mem.record_equity(datetime.now(), start_equity, None, "capital inicial")
        self.engines: dict[str, LiveExecutionEngine] = {}
        for sym, spec in self.specs.items():
            cfg = EngineConfig(factor_signs=dict(spec.factor_signs), symbol=sym)
            brain = GoldAIEngine(cfg, calibrator=calibrator)
            self.engines[sym] = LiveExecutionEngine(mem, limits, mode, equity, (executors or {}).get(sym), self.sender, self.ks, None,
                                                    horizon_min, brain, log, authorized, spec=spec, perf=self.perf, entry_gate=self._portfolio_gate)
        self.history: dict[str, StatConfidence] = {}
        self.refresh_history()
        # REACTION ENGINE (live): estatística do que foi vivido + cronômetros dos eventos em curso
        self.reaction_stats = ReactionStats(mem.reaction_records())
        self.pending_reactions: dict[str, dict] = {}     # event_id → {ev, exp/lead_dirs por ativo, série do alvo, séries líderes}
        self.reaction_horizon = min(horizon_min, 240)

    # ------------------------------------------------------------------ REACTION ENGINE (live)
    def _reaction_clock(self, snaps: MarketSnapshotSet) -> None:
        """A cada ciclo: relógio por mercado (evidência para o pré-movimento) + amostragem dos eventos em curso; ao fechar o
        horizonte, mede a reação (alvo e líderes) e grava — o sistema aprende com os eventos que viveu, nunca com o futuro."""

        now = snaps.time
        clock = ReactionClock(self.reaction_stats)
        for sym, snap in snaps.by_symbol.items():
            ra = clock.assess(sym, snap, snaps.identified, now)
            snap.reaction_status, snap.reaction_pressure, snap.reaction_probability = ra.status, ra.pressure, ra.probability
            snap.reaction_latency_min, snap.reaction_expected_min, snap.reaction_chain = ra.latency_min, ra.expected_min, ra.chain
        base = snaps.base
        for ev in snaps.identified:
            key = f"{ev.kind}:{ev.time:%Y%m%d%H%M}:{ev.name[:30]}"
            if key not in self.pending_reactions:
                if any(self.mem.has_reaction(key, sym) for sym in self.specs):
                    continue
                chans = TRANSMISSION.get(ev.kind) or EXTRA_TRANSMISSION.get(ev.kind) or {}
                sign = ev.direction_sign or 1.0
                self.pending_reactions[key] = {"ev": ev, "leads": {"USD": [], "YIELD": []}, "targets": {},
                                               "lead_dirs": {"USD": sign * chans.get("dollar", 0.0), "YIELD": sign * chans.get("yields", 0.0)}}
                for sym, snap in snaps.by_symbol.items():
                    exp, _ = expected_direction(ev, sym)
                    if exp != 0.0 and snap.price:
                        self.pending_reactions[key]["targets"][sym] = {"exp": exp, "atr": snap.atr or 0.0, "series": []}
            pr = self.pending_reactions[key]
            if base.dxy is not None:
                pr["leads"]["USD"].append((now, base.dxy))
            if base.us10y is not None:
                pr["leads"]["YIELD"].append((now, base.us10y))
            for sym, tg in pr["targets"].items():
                snap = snaps.by_symbol.get(sym)
                if snap is not None and snap.price:
                    tg["series"].append((now, snap.price))
        for key in list(self.pending_reactions):
            pr = self.pending_reactions[key]
            ev = pr["ev"]
            if ev.age_min(now) < self.reaction_horizon:
                continue
            for sym, tg in pr["targets"].items():
                fine = snaps.by_symbol[sym].candles.get("M5") if sym in snaps.by_symbol else None
                series = [(c.time, c.close) for c in fine if c.time >= ev.time - timedelta(minutes=10)] if fine else tg["series"]
                first_price = next((p for t, p in tg["series"] if t <= ev.time), None)
                if first_price is not None and (not series or series[0][0] > ev.time):
                    series = [(ev.time, first_price)] + list(series)
                rec = measure_reaction(key, ev.kind, ev.time, sym, tg["exp"], series, tg["atr"], pr["leads"], pr["lead_dirs"], self.reaction_horizon,
                                       5 if fine else 1)
                if rec:
                    self.mem.save_reaction(rec)
                    self.reaction_stats.add(rec)
                    self.log(f"⏱️ REACTION registrada: {ev.name[:40]} → {sym}: 1ª reação {rec.time_to_first or 'não'} min · confirmação "
                             f"{rec.time_to_confirmation or 'não'} min · MFE {rec.max_move_atr:.2f} ATR · líderes {rec.lead_times}")
            del self.pending_reactions[key]

    # ------------------------------------------------------------------ histórico por mercado
    def refresh_history(self) -> None:
        for sym in self.specs:
            rs = self.mem.r_stats(sym)
            results = []
            for row in self.mem.conn.execute("SELECT resultado_r FROM trades WHERE ativo=? AND resultado_r IS NOT NULL", (sym,)).fetchall():
                results.append(row["resultado_r"])
            self.history[sym] = statistical_confidence(results)
            self.engines[sym].mpe.history = self.engines[sym].monitor.history = rs

    def open_exposures(self) -> list[OpenExposure]:
        out = []
        for sym, eng in self.engines.items():
            for tr in eng.managed:
                out.append(OpenExposure(sym, tr.thesis.direction, (tr.plan.risk_usd or 0.0) * tr.remaining))
        return out

    def _portfolio_gate(self, symbol: str, direction: Direction, risk_usd: float) -> list[str]:
        return self.portfolio.check(symbol, direction, risk_usd, self.open_exposures(), self.perf.equity)

    # ------------------------------------------------------------------ ciclo de carteira
    def run_cycle(self, snaps: MarketSnapshotSet) -> PortfolioCycle:
        pc = PortfolioCycle(snaps.time)
        # comandos (/STOP /PAUSE /STATUS /CLOSE) tratados pelo primeiro motor, com o kill switch compartilhado
        first = next(iter(self.engines.values()))
        first.commands = self.commands
        if self.commands is not None and any(c.startswith("/EDGE") for c in getattr(self.commands, "last_cmds", [])):
            self.daily_edge(snaps.time, pc, force=True)
        # 0) REACTION ENGINE: relógio de reação por mercado + aprendizado dos eventos concluídos
        self._reaction_clock(snaps)
        # 1) cada mercado: monitor das posições abertas + predição (entrada adiada)
        for sym, eng in self.engines.items():
            snap = snaps.by_symbol.get(sym)
            if snap is None:
                pc.results[sym] = CycleResult(None, None, decision="sem dados")
                continue
            r = eng.run_cycle(snap, new_event_key=(snap.news[0].headline if snap.news else None), defer_entry=True)
            pc.results[sym] = r
            pc.messages += r.messages
        self.refresh_history()
        # 2) candidatos = oportunidades já produzidas (sinal operacional + vantagem estatística)
        cands: list[Candidate] = []
        for sym, r in pc.results.items():
            a, sig = r.assessment, r.signal
            if a is None or sig is None or sig.type not in LiveExecutionEngine.EXECUTABLE or sig.direction == Direction.LATERAL or not a.has_edge:
                continue
            opp = self.mem.opportunity_report(symbol=sym)
            cands.append(Candidate(self.specs[sym], a, sig, snaps.by_symbol[sym], self.history[sym], opp.capture_rate, snaps.data_quality.get(sym, 1.0)))
        for sym in self.specs:
            if sym not in {c.spec.symbol for c in cands}:
                self.selector.forget(sym)
        # 3) ASSET SELECTOR — ordena; a melhor tenta entrar (Risk Engine + exposição validam depois)
        pc.ranked = self.selector.rank(cands, snaps.time)
        self.log(render_rank(pc.ranked, self.history))
        entered = False
        for c in pc.ranked:
            sym = c.spec.symbol
            r = pc.results[sym]
            if entered:
                self.engines[sym].enter(r, snaps.by_symbol[sym], veto=f"PRIORIDADE — {pc.chosen} foi a melhor oportunidade do ciclo (OPP {pc.ranked[0].opportunity_score:.1f} vs {c.opportunity_score:.1f})")
                continue
            self.engines[sym].enter(r, snaps.by_symbol[sym])
            pc.messages += [m for m in r.messages if m not in pc.messages]
            if r.decision.startswith(("🟢 PAPER OPEN", "🟢 POSITION OPEN")):
                entered, pc.chosen = True, sym
        # mercados sem candidatura: registrar a decisão (regra que bloqueou) para o Opportunity Engine
        for sym, r in pc.results.items():
            if r.assessment is not None and sym not in {c.spec.symbol for c in pc.ranked}:
                self.engines[sym].enter(r, snaps.by_symbol[sym])
        pc.decision = (f"ENTRADA em {pc.chosen}" if pc.chosen else ("melhor oportunidade não passou no Risk Engine/exposição — " + pc.results[pc.ranked[0].spec.symbol].decision
                                                                   if pc.ranked else "nenhuma oportunidade com vantagem neste ciclo"))
        self.log(self.portfolio.render(self.open_exposures(), self.perf.equity))
        # 🚨 LIVE EDGE — o teste definitivo, uma vez por dia (e sob demanda com /EDGE)
        self.daily_edge(snaps.time, pc)
        return pc

    def edge_report(self, now: Optional[datetime] = None):
        return live_edge_report(self.mem, tuple(self.specs), now, self.perf.equity)

    def daily_edge(self, now: datetime, pc: Optional[PortfolioCycle] = None, force: bool = False) -> Optional[str]:
        today = now.strftime("%Y-%m-%d")
        if not force and self.mem.last_edge_date() == today:
            return None
        rep = self.edge_report(now)
        self.mem.save_edge_report(rep)
        text = rep.render()
        self.log(text)
        self.sender.send("📊 " + text)
        if pc is not None:
            pc.messages.append(text)
        return text

    def status_text(self) -> str:
        lines = [f"📋 MARKET AI STATUS · modo {self.mode.value} · mercados {', '.join(self.specs)}", self.perf.render(),
                 self.portfolio.render(self.open_exposures(), self.perf.equity), "Histórico por mercado:"]
        for sym, h in self.history.items():
            lines.append(f"  {sym:<7} {h.render()}")
        return "\n".join(lines)


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


def _load_frame(args: argparse.Namespace):
    """HistoryFrame de CSV (time,open,high,low,close,volume) ou do Yahoo (H1, até ~3 meses)."""

    def read_csv(path: str) -> list[Candle]:
        out = []
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
                out.append(Candle(t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0)))
        return sorted(out, key=lambda c: c.time)

    def attach(frame):
        """BANCO HISTÓRICO point-in-time (--events dados/noticias_historicas.csv): o cérebro só vê o que estava publicado em cada passo."""
        frame.symbol = (getattr(args, "market", None) or getattr(args, "market_symbol", None) or "XAUUSD").upper()
        path = getattr(args, "events", None)
        if path:
            frame.events = load_history(path)
            frame.news_mode = getattr(args, "news_mode", None) or "full"
            print(f"banco histórico: {frame.events.stats()} · modo {frame.news_mode}")
        return frame

    if args.csv:
        return attach(HistoryFrame(xau=read_csv(args.csv), dxy=read_csv(args.dxy_csv) if args.dxy_csv else [],
                                   us10y=read_csv(args.us10y_csv) if args.us10y_csv else []))
    y = YahooCollector(HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=900))
    start, end = getattr(args, "start", None), getattr(args, "end", None)
    if start:
        s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        e = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else datetime.now(timezone.utc)
        get = lambda sym: y.candles_between(sym, "H1", s, e)  # noqa: E731
    else:
        get = lambda sym: y.candles(sym, "H1")  # noqa: E731
    frame = HistoryFrame(xau=get(args.symbol), dxy=get("DX-Y.NYB"), us10y=get("^TNX"), vix=get("^VIX"), spx=get("^GSPC"))
    try:
        frame.fedfunds = get("ZQ=F")
    except Exception as e:  # noqa: BLE001
        print(f"(ZQ=F indisponível: {e})")
    if not getattr(args, "no_fred", False):
        try:
            fred = FredCollector(HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=6 * 3600))
            frame.real_yield_daily = [(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), v) for d, v in fred.series("DFII10")]
            frame.breakeven_daily = [(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), v) for d, v in fred.series("T10YIE")]
        except Exception as e:  # noqa: BLE001
            print(f"(FRED indisponível: {e})")
    return attach(frame)


def cmd_live_markets(args: argparse.Namespace) -> int:
    """4.0 MARKET AI ENGINE: vários mercados → cérebro único → Asset Selector → melhor oportunidade → risco/exposição → execução → monitor."""

    env = load_env_file()
    limits, plim = GuardLimits.from_env(env), PortfolioLimits.from_env(env)
    mode = TradingMode(args.mode.upper().replace("-", "_"))
    if mode == TradingMode.LIVE and not args.authorize:
        print("modo LIVE exige --authorize explícito; rebaixando para SEMI_LIVE")
        mode = TradingMode.SEMI_LIVE
    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    ks = KillSwitch.from_env(env, file_path=args.kill_switch_file)
    dcfg = DataEngineConfig(xau_symbol=args.symbol, calendar_path=args.calendar, enable_cot=not args.no_cot, enable_fred=not args.no_fred, enable_news=not args.no_news)
    mt5_client, executors = None, {}
    if args.source == "mt5":

        mcfg = MT5Config.from_env(env)
        if args.mt5_path:
            mcfg.path = args.mt5_path
        mt5_client = MT5Client(mcfg)
        try:
            mt5_client.connect()
        except Exception as e:  # noqa: BLE001
            print(f"MT5 indisponível: {e}")
            if mode != TradingMode.PAPER:
                return 1
            print("modo PAPER: continuando com dados web (Yahoo) — o MT5 só é obrigatório para executar ordens")
            mt5_client = None
        if mt5_client is not None and mode != TradingMode.PAPER:
            symbol_map = MultiMarketData.symbol_map_from_env(env)
            for sym in symbols:
                c = MT5Client(MT5Config(path=mcfg.path, symbol=symbol_map.get(sym, get_market(sym).mt5), login=mcfg.login, password=mcfg.password, server=mcfg.server))
                c.mt5, c.connected = mt5_client.mt5, True
                executors[sym] = ExecutionEngine(c, max_slippage=limits.max_slippage)
    elif mode != TradingMode.PAPER:
        print("execução real exige --source mt5; rebaixando para PAPER")
        mode = TradingMode.PAPER
    data = MultiMarketData(symbols, dcfg, mt5_client=mt5_client, mt5_symbol_map=MultiMarketData.symbol_map_from_env(env))
    sender = TelegramSender(dry_run=not args.send)
    commands = TelegramCommands(sender.token, sender.chat_id) if (args.send and not sender.dry_run) else None
    mem = PredictionMemory(args.db)
    edge = load_reaction_edge(args.reaction_edge) if getattr(args, "reaction_edge", None) else {}
    if edge:
        print("REACTION EDGE carregado (Asset Selector): " + ", ".join(f"{k} {v:.2f}" for k, v in edge.items()))
    engine = MarketAIEngine(mem, limits, symbols, mode, args.equity, plim, executors, sender, ks, commands, args.horizon, print, args.authorize,
                            selector=AssetSelector(reaction_edge=edge))
    print(f"MARKET AI ENGINE {__version__} · modo {mode.value} · mercados {', '.join(symbols)} · {engine.perf.render()}")
    print(f"portfólio: risco total {plim.max_total_open_risk_pct}% · correlacionado {plim.max_correlated_risk_pct}% · posições {plim.max_positions} · por ativo {plim.max_asset_exposure}")
    try:
        while True:
            snaps = data.collect()
            print(data.coverage())
            pc = engine.run_cycle(snaps)
            print(pc.render())
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(engine.status_text())
        mem.close()
        if mt5_client is not None:
            mt5_client.close()
    return 0


def cmd_markets(args: argparse.Namespace) -> int:
    """Ranking de oportunidades AGORA (sem operar) + histórico por mercado no SQLite."""

    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    data = MultiMarketData(symbols, DataEngineConfig(enable_cot=not args.no_cot, enable_fred=not args.no_fred, enable_news=not args.no_news))
    mem = PredictionMemory(args.db)
    edge = load_reaction_edge(args.reaction_edge) if args.reaction_edge else {}
    engine = MarketAIEngine(mem, GuardLimits.from_env(load_env_file()), symbols, TradingMode.PAPER, 10000.0, PortfolioLimits(),
                            kill_switch=KillSwitch(enabled_env=False), log=print, selector=AssetSelector(reaction_edge=edge))   # kill switch: só ranqueia, nunca entra
    if edge:
        print("REACTION EDGE carregado: " + ", ".join(f"{k} {v:.2f}" for k, v in edge.items()))
    snaps = data.collect()
    print(data.coverage())
    pc = engine.run_cycle(snaps)
    print(pc.render())
    print(engine.status_text())
    mem.close()
    return 0


def _frames_for_markets(args: argparse.Namespace, markets: str) -> dict:

    frames = {}
    for sym in (s.strip().upper() for s in markets.split(",") if s.strip()):
        ns = argparse.Namespace(**vars(args))
        ns.symbol = get_market(sym).yahoo
        ns.market_symbol = sym
        csv_dir = getattr(args, "csv_dir", None)
        ns.csv = os.path.join(csv_dir, f"{sym}_h1.csv") if csv_dir else None
        ns.dxy_csv = os.path.join(csv_dir, "DXY_h1.csv") if csv_dir and os.path.exists(os.path.join(csv_dir, "DXY_h1.csv")) else None
        ns.us10y_csv = os.path.join(csv_dir, "US10Y_h1.csv") if csv_dir and os.path.exists(os.path.join(csv_dir, "US10Y_h1.csv")) else None
        frames[sym] = _load_frame(ns)
        print(f"{sym}: {len(frames[sym].xau)} candles H1 carregados")
    return frames


def cmd_estimate(args: argparse.Namespace) -> int:
    """ESTIMATIVA DE LUCRO: histórico <start>→<end> (Yahoo ou CSV) → walk-forward OOS → capital, retorno, drawdown, bootstrap."""

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    frames = _frames_for_markets(args, args.markets)
    frames = {k: v for k, v in frames.items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado). Verifique a rede/Yahoo ou use --csv-dir.")
        return 1
    factory = lambda sym: _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)  # noqa: E731
    rep = estimate_profit(frames, start, end, args.equity, risk, n_folds=args.folds, step=args.step, horizon_min=args.horizon, strategy=args.strategy,
                          cfg_factory=factory)
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """BANCO HISTÓRICO DE EVENTOS/NOTÍCIAS (point-in-time): template · fetch-te · fetch-alfred · fetch-gdelt · rules · learn · stats."""

    env = load_env_file()
    path = args.file
    exists = os.path.exists(path)
    hist = load_history(path) if exists else EventHistory()
    start = date.fromisoformat(args.start) if args.start else date(2026, 1, 1)
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    if args.action == "prices" and args.source == "dukascopy":
        # ticks bid/ask gratuitos (sem chave, sem MT5): por padrão só as horas ao redor dos eventos do banco
        http = HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=365 * 24 * 3600, timeout=90, retries=2)   # arquivos de hora podem ter MBs
        imp = DukascopyImporter(http, log=print)
        out_dir = args.out_dir or "dados"
        os.makedirs(out_dir, exist_ok=True)
        symbols = [x.strip().upper() for x in (args.markets + ("," + args.extra if args.extra else "")).split(",") if x.strip()]
        ev_times = []
        if not args.full:
            if not exists:
                print(f"{path} não existe — sem eventos para delimitar as horas; use --full para baixar o período inteiro")
                return 1
            ev_times = [e.published_at for e in hist.events if e.revised is None and e.category in ("MACRO", "CENTRAL_BANK") and start <= e.published_at.date() <= end]
            print(f"{len(ev_times)} eventos macro com hora exata entre {start} e {end}")
        for sym in symbols:
            inst, sc = DUKA_INSTRUMENTS.get(sym, (sym, 1000.0))
            dest = os.path.join(out_dir, f"{sym}_ticks.csv")
            try:
                ticks = imp.range(sym, start, end, args.scale, checkpoint=lambda t, d=dest: save_ticks(t, d)) if args.full else \
                    imp.around_events(sym, ev_times, args.before, args.after, args.scale, checkpoint=lambda t, d=dest: save_ticks(t, d))
            except Exception as e:  # noqa: BLE001
                print(f"{sym} ({inst}): FALHOU — {e}")
                continue
            n = save_ticks(ticks, dest)
            first = f" · 1º tick {ticks[0][0]:%Y-%m-%d %H:%M} bid {ticks[0][1]:g} ask {ticks[0][2]:g} (confira a escala!)" if ticks else " · nenhum tick (instrumento/escala/período?)"
            print(f"{sym} ({inst}, escala {args.scale or sc:g}): {n} ticks → {dest}{first}")
        if imp.failed:
            print(f"\n{len(imp.failed)} hora(s) falharam (timeout/503 do Dukascopy). Repita o mesmo comando: as horas já baixadas estão em cache e só as que faltam são pedidas.")
            return 2      # código 2 = incompleto (o .bat repete até 0)
        return 0
    if args.action == "prices":
        # exportação de M1 / ticks do MT5 para CSV (a corretora guarda M1 por anos e ticks por semanas/meses)
        cfg = MT5Config.from_env(env)
        symbol_map = MultiMarketData.symbol_map_from_env(env)
        out_dir = args.out_dir or "dados"
        os.makedirs(out_dir, exist_ok=True)
        t0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        t1 = datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc)
        try:
            client = MT5Client(cfg)
            client.connect()
        except MT5Error as e:
            print(f"MT5 indisponível: {e}")
            return 1
        print(f"fuso do servidor da corretora: UTC{client.server_offset_hours:+.0f}h (carimbos convertidos para UTC; force com MT5_UTC_OFFSET_HOURS)")
        symbols = [x.strip().upper() for x in (args.markets + ("," + args.extra if args.extra else "")).split(",") if x.strip()]
        for sym in symbols:
            broker = symbol_map.get(sym) or (get_market(sym).mt5 if sym in MARKETS else sym)
            try:
                if args.tf.upper() == "TICK":
                    n = 0
                    rows = []
                    t = t0
                    while t < t1:                      # ticks em blocos de 1 dia (volume grande)
                        tt = min(t + timedelta(days=1), t1)
                        rows += client.ticks_range(broker, t, tt)
                        t = tt
                    n = save_ticks(rows, os.path.join(out_dir, f"{sym}_ticks.csv"))
                    print(f"{sym} ({broker}): {n} ticks → {out_dir}/{sym}_ticks.csv")
                else:
                    cs = client.rates_range(broker, args.tf.upper(), t0, t1)
                    n = save_candles(cs, os.path.join(out_dir, f"{sym}_{args.tf.lower()}.csv"))
                    print(f"{sym} ({broker}): {n} candles {args.tf.upper()} → {out_dir}/{sym}_{args.tf.lower()}.csv")
            except MT5Error as e:
                print(f"{sym} ({broker}): FALHOU — {e}")
        client.close()
        return 0
    if args.action == "template":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ex = HistoricalEvent(datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc), datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc), "EXEMPLO_CPI_202601",
                             "CPI MoM (exemplo — apague)", "US", "USD", "MUITO ALTO", 0.2, 0.3, 0.4, None, None, "MACRO", "cpi", "manual", "", "")
        n = save_history(merge(hist, EventHistory([ex])) if not exists else hist, path)
        print(f"template salvo em {path} ({n} linhas). Colunas: {', '.join(HistoricalEvent.columns())}")
        print("Preencha com calendário (Investing/TE export) ou use fetch-te / fetch-alfred / fetch-gdelt. Datas em UTC (ISO 8601).")
        return 0
    if args.action == "stats":
        print(hist.stats() if exists else f"{path} não existe — use `history template` ou um fetch-*")
        if exists:
            print(coverage(hist, start, end).render())
            for m in EFFECT_MARKETS:
                vals = [e.effect(m) for e in hist.events if e.effect(m) is not None]
                print(f"  {m}: {len(vals)} eventos com efeito · favoráveis(↑) {sum(1 for v in vals if v > 0)} · contrários(↓) {sum(1 for v in vals if v < 0)}")
        return 0
    if args.action in ("fetch-te", "fetch-alfred", "fetch-gdelt"):
        http = HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=24 * 3600)
        progress = FetchProgress(path)
        if args.action == "fetch-te":
            key = args.key or env.get("TE_API_KEY") or os.environ.get("TE_API_KEY")
            if not key:
                print("Trading Economics exige chave: --key ou TE_API_KEY no .env (https://tradingeconomics.com/api)")
                return 1
            new = TradingEconomicsImporter(http, key).fetch(start, end, args.country)
        elif args.action == "fetch-alfred":
            key = args.key or env.get("FRED_API_KEY") or os.environ.get("FRED_API_KEY")
            if not key:
                found = find_env_file()
                print("ALFRED/FRED exige chave gratuita: --key ou FRED_API_KEY no .env (https://fred.stlouisfed.org/docs/api/api_key.html)")
                print(f"  .env lido: {found}" if found else "  nenhum .env encontrado; procurei em: " + ", ".join(env_file_candidates()))
                return 1
            imp = ALFREDImporter(http, key, log=print)
            try:
                new = imp.fetch(start, end, progress=progress)
            except DataError as e:
                print(str(e))
                return 1
            if imp.failed:
                print(f"séries com falha ({len(imp.failed)}): " + "; ".join(f"{sid}: {r}" for sid, r in imp.failed))
        else:
            topics = [t.strip() for t in args.topics.split(",")] if args.topics else None

            def checkpoint(partial):   # salva o parcial a cada janela: um 429 ou queda de rede não perde o que já veio
                save_history(merge(hist, partial), path)
            imp = GDELTImporter(http, min_interval=args.pace, log=print)
            new = imp.fetch(start, end, topics, chunk_days=args.chunk_days, max_records=args.max_records, checkpoint=checkpoint, enrich=args.enrich,
                            progress=progress, mode=args.gdelt_mode)
            if imp.failed:
                print(f"janelas com falha ({len(imp.failed)}) — rode o mesmo comando de novo para completá-las: " +
                      "; ".join(f"{t} {w}" for t, w, _ in imp.failed))
        print(f"{args.action}: {len(new)} registros novos ({new.stats()})")
        if args.action in ("fetch-te", "fetch-alfred") and len(new):
            src = "tradingeconomics" if args.action == "fetch-te" else "alfred"
            old = [e for e in hist.events if e.source == src]
            if old:
                print(f"substituindo {len(old)} registros anteriores da fonte {src} (reimportação é a versão definitiva)")
            hist = EventHistory([e for e in hist.events if e.source != src])
        hist = merge(hist, new)
        apply_rule_effects(hist)
        n = save_history(hist, path)
        print(f"salvo: {path} · {n} linhas\n" + coverage(hist, start, end).render())
        return 0
    if not exists:
        print(f"{path} não existe — use `history template` ou um fetch-*")
        return 1
    if args.action == "list":
        rows = [e for e in hist.events if (not args.kind or e.kind == args.kind) and (not args.category or e.category == args.category)]
        rows = [e for e in rows if start <= e.timestamp.date() <= end]
        print(f"{'evento (UTC)':<17}{'publicado':<17}{'tipo':<14}{'evento':<34}{'anterior':>9}{'consenso':>9}{'real':>8}{'surpresa':>9}  efeito XAU/US500/USDJPY")
        for e in rows[-args.limit:]:
            fmt = lambda v: "" if v is None else f"{v:g}"  # noqa: E731
            eff = "/".join("" if e.effect(m) is None else f"{e.effect(m):+.2f}" for m in ("XAUUSD", "US500", "USDJPY"))
            print(f"{e.timestamp:%Y-%m-%d %H:%M}  {e.published_at:%Y-%m-%d %H:%M}  {e.kind:<14}{(e.headline or e.event)[:33]:<34}{fmt(e.previous):>9}{fmt(e.forecast):>9}"
                  f"{fmt(e.actual):>8}{fmt(e.surprise):>9}  {eff}")
        print(f"{len(rows)} registros" + (f" (últimos {args.limit})" if len(rows) > args.limit else ""))
        return 0
    if args.action == "rules":
        n = apply_rule_effects(hist)
        save_history(hist, path)
        print(f"efeitos por regra macro aplicados a {n} eventos (efeitos empíricos preservados) · salvo em {path}")
        return 0
    if args.action == "learn":
        # efeitos empíricos: o histórico de preço (Yahoo/CSV) decide a direção média 60 min após cada evento
        frames = _frames_for_markets(args, args.markets)
        prices = {sym: [(c.time, c.close) for c in fr.xau] for sym, fr in frames.items()}
        res = learn_effects(hist, prices, horizon_min=args.horizon, min_n=args.min_n)
        print(render_effect_table(res["table"]))
        print(f"\n{res['applied']} eventos passaram a usar efeito empírico (os demais mantêm a regra macro)")
        save_history(hist, path)
        print(f"salvo em {path}")
        return 0
    print(f"ação desconhecida: {args.action}")
    return 1


def _reaction_learn_hires(args: argparse.Namespace, tf: Optional[str] = None, edge_out: Optional[str] = None):
    """Alta resolução: ticks/M1 (`history prices`) × banco de eventos → segundos, dois horizontes, lead-lag, custo, prova do relógio,
    tabela CLOCK − INGÊNUA e veredito por ativo. Devolve a lista de vereditos (ou 1 em erro)."""

    if not os.path.exists(args.events):
        print(f"banco histórico não encontrado: {args.events}")
        return 1
    hist = load_history(args.events)
    csv_dir = args.csv_dir or "dados"
    tf = (tf or args.tf).upper()
    edge_out = args.edge_out if edge_out is None else edge_out

    def read_candles(path):
        import csv as _csv
        out = []
        with open(path, encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
                out.append(Candle(t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0)))
        return out

    def load_path(sym: str, spread: float):
        p_t = os.path.join(csv_dir, f"{sym}_ticks.csv")
        p_c = os.path.join(csv_dir, f"{sym}_{tf.lower()}.csv")
        if tf == "TICK" and os.path.exists(p_t):
            return PricePath.from_ticks(load_ticks(p_t)), "ticks"
        if os.path.exists(p_c):
            return PricePath.from_candles(read_candles(p_c), spread, 1 if tf == "M1" else 5), tf
        if os.path.exists(p_t):
            if tf in ("M1", "M5"):
                return PricePath.from_ticks(load_ticks(p_t)).resample(1 if tf == "M1" else 5), f"{tf} (reamostrado dos ticks)"
            return PricePath.from_ticks(load_ticks(p_t)), "ticks"
        return None, ""

    leads = {}
    if args.lead_usd:
        lp, kind = load_path(args.lead_usd.upper(), 0.0)
        if lp:
            leads["USD"] = lp
            print(f"líder USD: {args.lead_usd} ({kind})")
        else:
            print(f"líder USD {args.lead_usd}: arquivo não encontrado em {csv_dir} (sem líder USD → lead-lag e trade sim ficam vazios)")
    if args.lead_yield:
        lp, kind = load_path(args.lead_yield.upper(), 0.0)
        if lp:
            leads["YIELD"] = lp
            print(f"líder YIELD: {args.lead_yield} ({kind})")
    all_recs, sim_items = [], []
    for sym in (x.strip().upper() for x in args.markets.split(",") if x.strip()):
        spec = get_market(sym)
        path, kind = load_path(sym, spec.typical_spread)
        if path is None:
            print(f"{sym}: sem {sym}_{tf.lower()}.csv / {sym}_ticks.csv em {csv_dir} — exporte com `history prices --tf {tf} --markets {sym}`")
            continue
        n = 0
        for e in hist.events:
            if e.revised is not None:
                continue
            sign, sigma = 0.0, None
            if e.surprise is not None:
                sign = 1.0 if e.surprise > 0 else -1.0 if e.surprise < 0 else 0.0
                typ = TYPICAL.get(e.kind, max(abs(e.forecast or e.previous or 1.0) * 0.1, 0.1))
                sigma = e.surprise / typ if typ else None
            elif e.kind in TRANSMISSION and e.actual is None:
                sign = 1.0
            if sign == 0.0:
                continue
            exp = rule_direction(e.kind, sign, sigma, sym)
            if exp == 0.0:
                continue
            chans = TRANSMISSION.get(e.kind) or EXTRA_TRANSMISSION.get(e.kind) or {}
            lead_dirs = {"USD": sign * chans.get("dollar", 0.0), "YIELD": sign * chans.get("yields", 0.0)}
            atr = path.atr_at(e.published_at)
            if not atr:
                continue
            rec = measure_hires(e.event_id, e.kind, e.published_at, sym, exp, path, atr, leads, lead_dirs)
            if rec:
                all_recs.append(rec)
                sim_items.append((rec, path, atr))
                n += 1
        print(f"{sym}: {n} eventos medidos em {kind} (resolução {path.resolution_sec:.0f} s)")
    ll = LeadLagStats(all_recs)
    sim = ReactionTradeSim(slippage_atr=args.slippage, latency_sec=args.latency)
    delays_default = "1,5,10,30,60" if tf == "TICK" else "60,120,300"
    delays = [int(x) for x in (args.delays or delays_default).split(",")]
    sim_rows = sim.table(sim_items, delays=delays)
    txt = ll.render() + "\n\n" + sim.render(sim_rows)
    results = {}
    for d in delays:
        ct = ClockTradeTest(delay_sec=d, p_min=args.p_min, slippage_atr=args.slippage, latency_sec=args.latency)
        results[d] = ct.run(sim_items)
        txt += "\n\n" + ct.render(results[d])
    txt += "\n\n" + render_delta(delta_table(results), tf)
    verdicts = asset_verdicts(results)
    txt += "\n\n" + render_verdicts(verdicts, tf)
    if edge_out:
        save_reaction_edge(verdicts, edge_out, tf)
        txt += f"\n   veredito salvo em {edge_out} (o `live --markets` e o `markets` leem este arquivo)"
    txt += "\n\nLIMITES: manchetes GDELT (volinfo) têm published_at no fim do dia — só releases (ALFRED/TE) têm hora exata para segundos; "
    txt += "custo = ask/bid reais dos ticks (ou spread típico no M1) + slippage + latência; liquidez fora do horário e gaps não modelados."
    print(txt)
    if args.out:
        with open(args.out, "a" if tf == "M1" and args.tf.upper() == "BOTH" else "w", encoding="utf-8") as f:
            f.write(("\n\n" if tf == "M1" and args.tf.upper() == "BOTH" else "") + txt)
    return verdicts


def cmd_reaction(args: argparse.Namespace) -> int:
    """REACTION ENGINE: learn (banco histórico × preço → tempo de reação por evento e ativo) · stats (o que o live viveu) · clock (agora)."""

    if args.action == "learn" and args.tf.upper() == "BOTH":
        outs = {}
        for tf in ("TICK", "M1"):
            print(f"\n{'=' * 100}\n{tf}\n{'=' * 100}")
            outs[tf] = _reaction_learn_hires(args, tf=tf, edge_out=(args.edge_out if tf == "TICK" else ""))
        if isinstance(outs["TICK"], list) and isinstance(outs["M1"], list):
            txt = render_stability(outs["TICK"], outs["M1"])
            print("\n" + txt)
            if args.out:
                with open(args.out, "a", encoding="utf-8") as f:
                    f.write("\n\n" + txt)
        return 0
    if args.action == "learn" and args.tf.upper() in ("M1", "M5", "TICK"):
        return 0 if isinstance(_reaction_learn_hires(args), list) else 1
    if args.action == "learn":
        if not os.path.exists(args.events):
            print(f"banco histórico não encontrado: {args.events}")
            return 1
        hist = load_history(args.events)
        frames = _frames_for_markets(args, args.markets)
        all_recs = []
        for sym, frame in frames.items():
            frame.symbol, frame.events = sym, hist
            st = frame.reaction_stats()
            all_recs += st.records
            print(f"{sym}: {len(st.records)} eventos medidos (H1)")
        st = ReactionStats(all_recs)
        txt = st.render()
        print(txt)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(txt)
        return 0
    if args.action == "stats":
        mem = PredictionMemory(args.db)
        st = ReactionStats(mem.reaction_records())
        print(st.render(title="⏱️ REACTION ENGINE — o que o live viveu (resolução por ciclo/M5)"))
        mem.close()
        return 0
    if args.action == "clock":
        symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
        mem = PredictionMemory(args.db)
        data = MultiMarketData(symbols, DataEngineConfig(enable_cot=False, enable_fred=not args.no_fred, enable_news=True))
        snaps = data.collect()
        clock = ReactionClock(ReactionStats(mem.reaction_records()))
        print(data.coverage())
        for sym, snap in snaps.by_symbol.items():
            print(clock.assess(sym, snap, snaps.identified, snaps.time).chain)
        mem.close()
        return 0
    return 1


def cmd_compare_news(args: argparse.Namespace) -> int:
    """TESTE A/B: Preço somente × Preço + Macro (A) × Preço + Macro + News (B) — mesma janela, mesmo piso, walk-forward OOS."""

    if not os.path.exists(args.events):
        print(f"banco histórico não encontrado: {args.events} — crie com `history template` / `history fetch-*`")
        return 1
    hist = load_history(args.events)
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    modes = tuple(m.strip() for m in args.modes.split(",")) if args.modes else ("none", "macro", "full")
    cov = coverage(hist, start.date(), end.date())
    print(cov.render() + "\n")
    gaps = []
    if "macro" in modes or "full" in modes:
        if cov.macro_pct < args.min_coverage:
            gaps.append(f"MACRO cobre {cov.macro_pct:.0%} das semanas (mínimo {args.min_coverage:.0%}) — rode `history fetch-alfred` / `fetch-te`")
    if "full" in modes and cov.news_pct < args.min_coverage:
        gaps.append(f"NEWS cobre {cov.news_pct:.0%} dos dias (mínimo {args.min_coverage:.0%}) — rode `history fetch-gdelt` até completar")
    if gaps:
        for g in gaps:
            print("⚠️ " + g)
        if not args.allow_partial:
            print("Banco incompleto para o período: um TESTE A/B sobre fração do histórico não vale como resultado. Complete o banco ou use --allow-partial para um ensaio.")
            return 1
        print("(--allow-partial: resultado é ENSAIO, não conclusão)\n")
    args.events_path, args.events = args.events, None    # os frames são carregados SEM banco; a ablação liga/desliga por modo
    frames = {k: v for k, v in _frames_for_markets(args, args.markets).items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    factory = lambda sym: _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)  # noqa: E731
    rep = compare_information(frames, hist, start, end, args.equity, risk, n_folds=args.folds, step=args.step, horizon_min=args.horizon,
                              strategy=args.strategy, cfg_factory=factory, modes=modes, log=(print if args.verbose else None))
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """SWEEP DE PISO: testa vários |score| mínimos de vantagem no walk-forward, escolhendo o piso NO TREINO de cada fold."""

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    floors = tuple(float(x) for x in args.floors.split(",")) if args.floors else DEFAULT_FLOORS
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    bt = Backtester(frame, cfg, step=args.step, horizon_min=args.horizon)
    rep = threshold_sweep(bt, floors, n_folds=args.folds, strategy=args.strategy, equity=args.equity, risk_pct=risk,
                          log=(print if args.verbose else None))
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
    return 0


def cmd_edge(args: argparse.Namespace) -> int:
    """LIVE EDGE: tabela por mercado a partir do que o sistema viveu (fora da amostra por construção) + evolução diária."""

    mem = PredictionMemory(args.db)
    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    rep = live_edge_report(mem, symbols, equity=mem.last_equity(), min_trades=args.min_trades)
    print(rep.render())
    if args.save:
        mem.save_edge_report(rep)
        print("relatório salvo")
    hist = mem.edge_history()
    if hist:
        print("\nEvolução (expectancy ajustada por relatório diário):")
        for s in symbols:
            print("  " + edge_trend(hist, s))
    mem.close()
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """3.0 LIVE EXECUTION ENGINE: dados reais → predição → decisão → plano → risco → lote → MT5 → confirmação → monitor → resultado → capital."""
    if args.markets:
        return cmd_live_markets(args)

    env = load_env_file()
    limits = GuardLimits.from_env(env)
    mode = TradingMode(args.mode.upper().replace("-", "_"))
    if mode == TradingMode.LIVE and not args.authorize:
        print("modo LIVE exige --authorize explícito; rebaixando para SEMI_LIVE")
        mode = TradingMode.SEMI_LIVE
    ks = KillSwitch.from_env(env, file_path=args.kill_switch_file)

    dcfg = DataEngineConfig(xau_symbol=args.symbol, calendar_path=args.calendar, enable_cot=not args.no_cot,
                            enable_fred=not args.no_fred, enable_news=not args.no_news)
    data = DataEngine(dcfg)
    source = data
    executor = None
    if args.source == "mt5":

        mcfg = MT5Config.from_env(env)
        if args.mt5_path:
            mcfg.path = args.mt5_path
        source = MT5Source(mcfg, data_engine=data)
        if mode != TradingMode.PAPER:
            source.client.connect()
            executor = ExecutionEngine(source.client, max_slippage=limits.max_slippage)
    elif mode != TradingMode.PAPER:
        print("execução real exige --source mt5; rebaixando para PAPER")
        mode = TradingMode.PAPER

    calibrator = None
    if args.calibrator and os.path.exists(args.calibrator):
        with open(args.calibrator, encoding="utf-8") as f:
            calibrator = IsotonicCalibrator.from_dict(json.load(f))
        print(f"calibrador carregado: {args.calibrator}")
    sender = TelegramSender(dry_run=not args.send)
    commands = TelegramCommands(sender.token, sender.chat_id) if (args.send and not sender.dry_run) else None
    mem = PredictionMemory(args.db)
    live = LiveExecutionEngine(mem, limits, mode, args.equity, executor, sender, ks, commands, args.horizon,
                               GoldAIEngine(EngineConfig(), calibrator=calibrator), authorized=args.authorize)
    print(f"GOLD AI ENGINE {__version__} · modo {mode.value} · {live.perf.render()}")
    print(f"limites: risco/trade {limits.risk_per_trade_pct}% · perda diária {limits.max_daily_loss_pct}% · drawdown {limits.max_drawdown_pct}% · "
          f"posições {limits.max_positions} · lote máx {limits.max_lot} · spread máx {limits.max_spread} · kill switch: {ks.new_entries_allowed()[1]}")
    if live.managed:
        print(f"{len(live.managed)} operação(ões) aberta(s) retomada(s) pelo GOLD TRADE MONITOR")
    try:
        while True:
            snap = source.collect() if source is data else source.snapshot()
            print(data.coverage())
            if source is not data:
                print(f"MT5: {source.status.get('mt5', 'n/d')}")
            res = live.run_cycle(snap, new_event_key=snap.news[0].headline if snap.news else None)
            for n in res.notes:
                print(n)
            if res.assessment is not None:
                print(render_dashboard(res.assessment, live.engine.expected_lead_min))
                if args.verbose:
                    print(render_report(res.assessment))
            print(f">>> {res.decision}")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(live.status_text())
        mem.close()
        if source is not data:
            source.client.close()
    return 0


def _apply_experiment(cfg: EngineConfig, args: argparse.Namespace) -> EngineConfig:
    """Limiares como parâmetros de EXPERIMENTO (para testar fora da amostra, nunca para 'fazer entrar')."""
    changed = []
    if getattr(args, "edge_score", None) is not None:
        cfg.min_edge_score = args.edge_score; changed.append(f"vantagem |score| ≥ {args.edge_score:g}")
    if getattr(args, "signal_score", None) is not None:
        cfg.buy, cfg.sell = args.signal_score, -args.signal_score; changed.append(f"sinal |score| ≥ {args.signal_score:g}")
    if getattr(args, "min_confirmations", None) is not None:
        cfg.min_confirmations = args.min_confirmations; changed.append(f"confirmações ≥ {args.min_confirmations}")
    if getattr(args, "edge_confidence", None) is not None:
        cfg.min_edge_confidence = args.edge_confidence; changed.append(f"confiança ≥ {args.edge_confidence:g}")
    if changed:
        print("experimento: " + " · ".join(changed) + "  (compare a expectancy OOS com o padrão antes de adotar)")
    return cfg


def _cfg_for(args: argparse.Namespace) -> EngineConfig:
    """EngineConfig com os sinais de fator do mercado (--market); sem --market usa o cérebro do ouro."""

    market = getattr(args, "market", None)
    if market:
        spec = get_market(market)
        if getattr(args, "symbol", None) in (None, "GC=F") and not getattr(args, "csv", None):
            args.symbol = spec.yahoo
        print(f"cérebro: {spec.symbol} (sinais por fator do mercado) · candles {args.symbol}")
        return _apply_experiment(EngineConfig(factor_signs=dict(spec.factor_signs), symbol=spec.symbol), args)
    print("cérebro: XAUUSD (padrão) — use --market EURUSD|US500|USDJPY|WTI para aplicar os sinais do mercado")
    return _apply_experiment(EngineConfig(), args)


def cmd_backtest(args: argparse.Namespace) -> int:

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    bt = Backtester(frame, cfg, threshold_atr=args.threshold_atr, horizon_min=args.horizon, include_watch=args.include_watch)
    if args.walk_forward:
        print(walk_forward(bt, n_folds=args.folds).render())
    else:
        print(bt.run().render())
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Confronta as previsões gravadas no SQLite com o caminho real do preço (CSV time,close)."""

    path = []
    with open(args.path_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            path.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["close"])))
    mem = PredictionMemory(args.db)
    print(mem.metrics(path, args.threshold, args.horizon).render())
    mem.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    mem = PredictionMemory(args.db)
    print(mem.performance_summary())
    curve = mem.equity_curve()
    if curve:
        print("  últimos pontos: " + " → ".join(f"{v:,.0f}" for _, v in curve[-8:]))
    open_ = mem.managed_trades()
    print(f"operações abertas sob monitor: {len(open_)}")
    for tr in open_:
        print(f"  #{tr.trade_id:05d} {tr.thesis.direction.value} entrada {tr.plan.entry:.2f} stop {tr.price_at_r(tr.stop_r):.2f} restante {tr.remaining:.0%}")
    print(mem.r_stats().render())
    print(mem.exit_learning())
    mem.close()
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """2.1 VALIDATION ENGINE: backtest + walk-forward rolante + calibração + score por fator + auditoria.
    Com --markets: validação multi-mercado (4.0) — qual mercado tem melhor expectativa fora da amostra, ajustada à amostra."""

    if args.markets:

        frames = {}
        for sym in (s.strip().upper() for s in args.markets.split(",") if s.strip()):
            ns = argparse.Namespace(**vars(args))
            ns.symbol = get_market(sym).yahoo
            ns.csv = os.path.join(args.csv_dir, f"{sym}_h1.csv") if args.csv_dir else None
            ns.dxy_csv = os.path.join(args.csv_dir, "DXY_h1.csv") if args.csv_dir and os.path.exists(os.path.join(args.csv_dir, "DXY_h1.csv")) else None
            ns.us10y_csv = os.path.join(args.csv_dir, "US10Y_h1.csv") if args.csv_dir and os.path.exists(os.path.join(args.csv_dir, "US10Y_h1.csv")) else None
            frames[sym] = _load_frame(ns)
        rows = validate_markets(frames, n_folds=args.folds, step=args.step, horizon_min=args.horizon)
        print(render_market_validation(rows))
        if args.verbose_markets:
            for m in rows:
                print(f"\n{'=' * 30} {m.symbol} {'=' * 30}\n" + m.report.render())
        return 0

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    rep = validate(frame, cfg, n_folds=args.folds, step=args.step, threshold_atr=args.threshold_atr,
                   horizon_min=args.horizon, mode=args.mode)
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """2.2 TRADE SIMULATOR sobre histórico: 1R/2R/3R/4R antes do stop, estratégias de saída, expectancy em R."""

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    bt = Backtester(frame, cfg, step=args.step, threshold_atr=args.threshold_atr, horizon_min=args.horizon)
    if args.walk_forward:
        wf = walk_forward(bt, n_folds=args.folds)
        print(wf.render())
        rs = wf.oos_trades
    else:
        r = bt.run()
        print(r.render())
        rs = r.trades
    if rs and rs.n:
        print(f"\nRESPOSTA: com {rs.n} operações, 3R é atingido antes do stop em {rs.reach_3r_before_stop:.0%} dos casos; "
              f"melhor estratégia de saída: {rs.best} (E={max(s.expectancy_r for s in rs.strategies):+.2f}R).")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Ajusta o calibrador isotônico com as previsões resolvidas no SQLite e salva em JSON (usado por `live --calibrator`)."""
    mem = PredictionMemory(args.db)
    rep = mem.calibration()
    print(rep.render())
    if rep.n < args.min_n:
        print(f"\nsó {rep.n} previsões resolvidas (mínimo {args.min_n}) — calibrador NÃO salvo")
        mem.close()
        return 1
    cal = mem.fit_calibrator()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cal.to_dict(), f)
    print(f"\ncalibrador salvo em {args.out}: " + ", ".join(f"{x:.2f}→{y:.2f}" for x, y in zip(cal.xs, cal.ys)))
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
    lt = mem.lead_time_stats()
    print(f"\n⏱️ LEAD TIME médio dos acertos: {lt['media']:.0f} min (n={lt['n']})" if lt["media"] else "\n⏱️ LEAD TIME: sem acertos resolvidos ainda")
    for k, v in lt["por_tipo"].items():
        print(f"  {k}: {v:.0f} min")
    print()
    print(mem.calibration().render())
    print()
    print(mem.scoreboard().render())
    print()
    rs = mem.r_stats()
    print(rs.render())
    print()
    print(mem.exit_learning())
    print()
    mem.resolve_hypotheticals(datetime.now(timezone.utc))
    print(mem.opportunity_report().render())
    print()
    print(mem.funnel().render("FUNIL DE ENTRADA (vivido)"))
    for m in mem.per_market_summary():
        print()
        print(mem.funnel(m["symbol"]).render(f"FUNIL — {m['symbol']}"))
    if rs.n:
        best = next((s for s in rs.strategies if s.name == rs.best), None)
        print(f"\nExpectancy em R ({rs.best}): {best.expectancy_r:+.2f}R por operação · "
              f"1R {rs.reach['1R']:.0%} · 2R {rs.reach['2R']:.0%} · 3R {rs.reach['3R']:.0%} · 3R antes do stop {rs.reach_3r_before_stop:.0%}")
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
    s.add_argument("--by", nargs="*", default=["sessao", "previsao", "score_bucket", "estagio", "nivel_evidencia"])
    s.set_defaults(func=cmd_stats)

    e = sub.add_parser("event", help="árvore de reação pré-evento e cadeia pós-evento (exemplo CPI)")
    e.add_argument("--actual", type=float, default=None)
    e.add_argument("--dxy", type=float, default=0.0)
    e.add_argument("--us10y", type=float, default=0.0)
    e.add_argument("--real", type=float, default=0.0)
    e.add_argument("--gold", type=float, default=0.0)
    e.add_argument("--flow", type=float, default=0.0)
    e.set_defaults(func=cmd_event)

    lv = sub.add_parser("live", help="3.0 LIVE EXECUTION ENGINE — dados reais, decisão, execução no MT5 e gestão da posição")
    lv.add_argument("--symbol", default="GC=F", help="GC=F (futuro) ou XAUUSD=X (spot) para o Data Engine web")
    lv.add_argument("--calendar", default=None, help="JSON de eventos econômicos")
    lv.add_argument("--interval", type=int, default=300)
    lv.add_argument("--db", default="gold_ai.db")
    lv.add_argument("--send", action="store_true", help="envia ao Telegram e habilita comandos /STOP /PAUSE /RESUME /STATUS /CLOSE")
    lv.add_argument("--once", action="store_true")
    lv.add_argument("--no-cot", action="store_true")
    lv.add_argument("--no-fred", action="store_true")
    lv.add_argument("--no-news", action="store_true")
    lv.add_argument("--source", choices=["web", "mt5"], default="web", help="mt5 = preço/candles e execução no MetaTrader 5")
    lv.add_argument("--mt5-path", default=None, help="caminho do terminal64.exe (ou MT5_PATH no .env)")
    lv.add_argument("--mode", choices=["paper", "authorize", "semi-live", "live"], default="paper",
                    help="🟢 paper (padrão) · 🟡 authorize · 🟠 semi-live · 🔴 live (exige --authorize)")
    lv.add_argument("--authorize", action="store_true", help="autoriza a próxima entrada (AUTHORIZE) / habilita LIVE")
    lv.add_argument("--equity", type=float, default=10000.0, help="capital inicial (PAPER); em LIVE vem do broker")
    lv.add_argument("--horizon", type=int, default=240, help="minutos para resolver cada previsão/operação")
    lv.add_argument("--calibrator", default="calibrator.json", help="JSON gerado por `calibrate` (ignorado se não existir)")
    lv.add_argument("--kill-switch-file", default="STOP_TRADING", help="se o arquivo existir, nenhuma entrada nova")
    lv.add_argument("-v", "--verbose", action="store_true")
    lv.add_argument("--markets", default=None, help="4.0: lista de mercados, ex.: EURUSD,US500,XAUUSD,USDJPY,WTI (Asset Selector escolhe a melhor)")
    lv.add_argument("--reaction-edge", default=os.path.join("dados", "reaction_edge.json"), help="veredito do REACTION EDGE por ativo ('' = ignorar)")
    lv.set_defaults(func=cmd_live)

    es = sub.add_parser("estimate", help="estimativa de lucro num período histórico (walk-forward OOS, custo, bootstrap)")
    es.add_argument("--start", default="2026-01-01", help="data inicial (YYYY-MM-DD)")
    es.add_argument("--end", default=None, help="data final (padrão: agora)")
    es.add_argument("--markets", default="XAUUSD", help="ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    es.add_argument("--equity", type=float, default=10000.0)
    es.add_argument("--risk", type=float, default=None, help="%% por operação (padrão: RISK_PER_TRADE do .env ou 0.5)")
    es.add_argument("--strategy", default="adaptive", help="adaptive | 3R | 2R+trailing | trailing | 1R | 2R | 4R")
    es.add_argument("--folds", type=int, default=4)
    es.add_argument("--step", type=int, default=1)
    es.add_argument("--horizon", type=int, default=240)
    es.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    es.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    es.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    es.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    es.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    es.add_argument("--csv-dir", default=None, help="alternativa ao Yahoo: pasta com <SYMBOL>_h1.csv (+ DXY_h1.csv, US10Y_h1.csv)")
    es.add_argument("--out", default=None)
    es.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    es.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    es.set_defaults(func=cmd_estimate)

    sw = sub.add_parser("sweep", help="sweep de piso de vantagem no walk-forward (piso escolhido no treino de cada fold) + sensibilidade OOS")
    sw.add_argument("--csv", default=None)
    sw.add_argument("--dxy-csv", default=None)
    sw.add_argument("--us10y-csv", default=None)
    sw.add_argument("--symbol", default="GC=F")
    sw.add_argument("--market", default=None, help="EURUSD, US500, XAUUSD, USDJPY, WTI")
    sw.add_argument("--start", default=None)
    sw.add_argument("--end", default=None)
    sw.add_argument("--floors", default=None, help="ex.: 10,12,15,17,20,22,25,30,35,40 (padrão)")
    sw.add_argument("--strategy", default="adaptive")
    sw.add_argument("--folds", type=int, default=4)
    sw.add_argument("--step", type=int, default=1)
    sw.add_argument("--horizon", type=int, default=240)
    sw.add_argument("--equity", type=float, default=10000.0)
    sw.add_argument("--risk", type=float, default=None)
    sw.add_argument("--no-fred", action="store_true")
    sw.add_argument("--out", default=None)
    sw.add_argument("-v", "--verbose", action="store_true")
    sw.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    sw.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    sw.set_defaults(func=cmd_sweep)

    hi = sub.add_parser("history", help="BANCO HISTÓRICO point-in-time: template | fetch-te | fetch-alfred | fetch-gdelt | rules | learn | stats | list | prices (M1/ticks do MT5)")
    hi.add_argument("action", choices=["template", "fetch-te", "fetch-alfred", "fetch-gdelt", "rules", "learn", "stats", "list", "prices"])
    hi.add_argument("--tf", default="M1", help="prices: M1 | M5 | TICK (ticks bid/ask, blocos diários)")
    hi.add_argument("--source", choices=["mt5", "dukascopy"], default="dukascopy", help="prices: dukascopy (ticks gratuitos, sem chave) | mt5 (terminal logado)")
    hi.add_argument("--full", action="store_true", help="prices dukascopy: período inteiro (padrão: só horas ao redor dos eventos macro)")
    hi.add_argument("--before", type=int, default=4, help="prices dukascopy: horas antes de cada evento")
    hi.add_argument("--after", type=int, default=1, help="prices dukascopy: horas depois de cada evento")
    hi.add_argument("--scale", type=float, default=None, help="prices dukascopy: escala de preço do instrumento (padrão por mercado)")
    hi.add_argument("--extra", default=None, help="prices: símbolos extras da corretora para líderes, ex.: USDX,USTNOTE")
    hi.add_argument("--out-dir", default="dados")
    hi.add_argument("--kind", default=None, help="list: cpi, core_cpi, nfp, unemployment, jobless_claims, gdp, ppi, retail_sales, earnings, core_pce…")
    hi.add_argument("--category", default=None, help="list: MACRO, CENTRAL_BANK, GEOPOLITICAL, ENERGY, CHINA, NEWS")
    hi.add_argument("--limit", type=int, default=40)
    hi.add_argument("--file", default=os.path.join("dados", "noticias_historicas.csv"))
    hi.add_argument("--start", default="2026-01-01")
    hi.add_argument("--end", default=None)
    hi.add_argument("--key", default=None, help="chave da API (ou TE_API_KEY / FRED_API_KEY no .env)")
    hi.add_argument("--country", default="united states")
    hi.add_argument("--topics", default=None, help="GDELT: geopolitica,petroleo,china,fed,risco (padrão: todos)")
    hi.add_argument("--chunk-days", type=int, default=30, help="GDELT: dias por janela (menos janelas = menos chamadas; o GDELT limita a 1 a cada ~5 s)")
    hi.add_argument("--max-records", type=int, default=250, help="GDELT: manchetes por tema por janela (máx. 250)")
    hi.add_argument("--enrich", action="store_true", help="GDELT: além das manchetes/volume, baixar o tom (2× chamadas)")
    hi.add_argument("--gdelt-mode", choices=["volinfo", "artlist"], default="volinfo",
                    help="volinfo (padrão): manchetes mais relevantes de CADA DIA + volume, 1 chamada/janela; artlist: as mais recentes com hora exata")
    hi.add_argument("--pace", type=float, default=8.0, help="GDELT: segundos entre chamadas (aumente se receber 429 repetidos)")
    hi.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI", help="learn: mercados cujo preço define o efeito empírico")
    hi.add_argument("--horizon", type=int, default=60, help="learn: minutos após o evento para medir a direção")
    hi.add_argument("--min-n", type=int, default=8, help="learn: amostra mínima por tipo/sinal/mercado")
    hi.add_argument("--csv-dir", default=None)
    hi.add_argument("--no-fred", action="store_true")
    hi.set_defaults(func=cmd_history)

    rc = sub.add_parser("reaction", help="REACTION ENGINE: learn (tempo de reação por evento/ativo no histórico) | stats (vivido) | clock (agora)")
    rc.add_argument("action", choices=["learn", "stats", "clock"])
    rc.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    rc.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    rc.add_argument("--start", default="2026-01-01")
    rc.add_argument("--end", default=None)
    rc.add_argument("--db", default="gold_ai.db")
    rc.add_argument("--csv-dir", default=None)
    rc.add_argument("--no-fred", action="store_true")
    rc.add_argument("--tf", default="H1", help="learn: H1 (Yahoo/CSV) | M1 | M5 | TICK | BOTH (TICK + M1 reamostrado + tabela de estabilidade)")
    rc.add_argument("--lead-usd", default="USDX", help="learn M1/TICK: símbolo exportado do líder USD (ex.: USDX); vazio = sem líder")
    rc.add_argument("--lead-yield", default=None, help="learn M1/TICK: símbolo exportado do líder de juros (ex.: USTNOTE)")
    rc.add_argument("--slippage", type=float, default=0.02, help="trade sim: slippage em ATR por perna")
    rc.add_argument("--latency", type=float, default=0.5, help="trade sim: latência de execução em segundos")
    rc.add_argument("--delays", default=None, help="prova do relógio: atrasos (s) após a reação do líder (padrão TICK 1,5,10,30,60; M1 60,120,300)")
    rc.add_argument("--edge-out", default=os.path.join("dados", "reaction_edge.json"), help="veredito por ativo para o Asset Selector ('' = não salvar)")
    rc.add_argument("--p-min", type=float, default=0.55, help="prova do relógio: P(alvo confirma | líder) mínima no histórico anterior")
    rc.add_argument("--out", default=None)
    rc.set_defaults(func=cmd_reaction)

    cn = sub.add_parser("compare-news", help="TESTE A/B: Preço somente × Preço + Macro (A) × Preço + Macro + News (B), walk-forward OOS, mesmo piso")
    cn.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    cn.add_argument("--start", default="2026-01-01")
    cn.add_argument("--end", default=None)
    cn.add_argument("--markets", default="US500,XAUUSD", help="ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    cn.add_argument("--modes", default=None, help="none,macro,full (padrão: os três)")
    cn.add_argument("--min-coverage", type=float, default=0.8, help="cobertura mínima do banco no período (macro por semana, news por dia)")
    cn.add_argument("--allow-partial", action="store_true", help="roda mesmo com banco incompleto (resultado é ensaio, não conclusão)")
    cn.add_argument("--equity", type=float, default=10000.0)
    cn.add_argument("--risk", type=float, default=None)
    cn.add_argument("--strategy", default="adaptive")
    cn.add_argument("--folds", type=int, default=4)
    cn.add_argument("--step", type=int, default=1)
    cn.add_argument("--horizon", type=int, default=240)
    cn.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    cn.add_argument("--signal-score", type=float, default=None)
    cn.add_argument("--min-confirmations", type=int, default=None)
    cn.add_argument("--edge-confidence", type=float, default=None)
    cn.add_argument("--no-fred", action="store_true")
    cn.add_argument("--csv-dir", default=None)
    cn.add_argument("--out", default=None)
    cn.add_argument("-v", "--verbose", action="store_true")
    cn.set_defaults(func=cmd_compare_news)

    ed = sub.add_parser("edge", help="4.0: LIVE EDGE — tabela diária por mercado a partir do que foi vivido (o teste definitivo)")
    ed.add_argument("--markets", default="EURUSD,US500,XAUUSD,USDJPY,WTI")
    ed.add_argument("--db", default="gold_ai.db")
    ed.add_argument("--min-trades", type=int, default=30)
    ed.add_argument("--save", action="store_true", help="guarda o relatório de hoje no SQLite")
    ed.set_defaults(func=cmd_edge)

    mk = sub.add_parser("markets", help="4.0: ranking de oportunidades agora (não opera) + histórico por mercado")
    mk.add_argument("--markets", default="EURUSD,US500,XAUUSD,USDJPY,WTI")
    mk.add_argument("--reaction-edge", default=os.path.join("dados", "reaction_edge.json"), help="veredito do REACTION EDGE por ativo ('' = ignorar)")
    mk.add_argument("--db", default="gold_ai.db")
    mk.add_argument("--no-cot", action="store_true")
    mk.add_argument("--no-fred", action="store_true")
    mk.add_argument("--no-news", action="store_true")
    mk.set_defaults(func=cmd_markets)

    st = sub.add_parser("status", help="capital, performance, operações abertas, aprendizado")
    st.add_argument("--db", default="gold_ai.db")
    st.set_defaults(func=cmd_status)

    bt = sub.add_parser("backtest", help="backtest / walk-forward sobre histórico H1 (CSV ou Yahoo)")
    bt.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    bt.add_argument("--dxy-csv", default=None)
    bt.add_argument("--us10y-csv", default=None)
    bt.add_argument("--symbol", default="GC=F")
    bt.add_argument("--market", default=None, help="aplica os sinais por fator do mercado (EURUSD, US500, XAUUSD, USDJPY, WTI) e escolhe o símbolo Yahoo")
    bt.add_argument("--start", default=None, help="histórico Yahoo a partir desta data (YYYY-MM-DD)")
    bt.add_argument("--end", default=None)
    bt.add_argument("--threshold-atr", type=float, default=1.0)
    bt.add_argument("--horizon", type=int, default=240, help="minutos")
    bt.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    bt.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    bt.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    bt.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    bt.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--folds", type=int, default=4)
    bt.add_argument("--include-watch", action="store_true")
    bt.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    bt.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    bt.set_defaults(func=cmd_backtest)

    mt = sub.add_parser("metrics", help="precisão/recall/MFE/MAE/lead time das previsões gravadas vs. preço real")
    mt.add_argument("--db", default="gold_ai.db")
    mt.add_argument("--path-csv", required=True, help="CSV time,close com o caminho real do preço")
    mt.add_argument("--threshold", type=float, default=9.0, help="USD (ex.: 1 ATR)")
    mt.add_argument("--horizon", type=int, default=240)
    mt.set_defaults(func=cmd_metrics)

    va = sub.add_parser("validate", help="2.1 VALIDATION ENGINE: auditoria + walk-forward + calibração + score por fator + oportunidades")
    va.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    va.add_argument("--dxy-csv", default=None)
    va.add_argument("--us10y-csv", default=None)
    va.add_argument("--symbol", default="GC=F")
    va.add_argument("--market", default=None, help="aplica os sinais por fator do mercado (EURUSD, US500, XAUUSD, USDJPY, WTI) e escolhe o símbolo Yahoo")
    va.add_argument("--start", default=None, help="histórico Yahoo a partir desta data (YYYY-MM-DD)")
    va.add_argument("--end", default=None)
    va.add_argument("--folds", type=int, default=4)
    va.add_argument("--step", type=int, default=1)
    va.add_argument("--mode", choices=["rolling", "anchored"], default="rolling")
    va.add_argument("--threshold-atr", type=float, default=1.0)
    va.add_argument("--horizon", type=int, default=240)
    va.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    va.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    va.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    va.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    va.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    va.add_argument("--out", default=None, help="salva o relatório em arquivo")
    va.add_argument("--markets", default=None, help="4.0: validação multi-mercado, ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    va.add_argument("--csv-dir", default=None, help="pasta com <SYMBOL>_h1.csv (+ DXY_h1.csv, US10Y_h1.csv opcionais)")
    va.add_argument("--verbose-markets", action="store_true", help="imprime o relatório completo de cada mercado")
    va.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    va.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    va.set_defaults(func=cmd_validate)

    si = sub.add_parser("simulate", help="2.2 TRADE SIMULATOR: 1R/2R/3R/4R antes do stop, estratégias de saída, expectancy, oportunidades")
    si.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    si.add_argument("--dxy-csv", default=None)
    si.add_argument("--us10y-csv", default=None)
    si.add_argument("--symbol", default="GC=F")
    si.add_argument("--market", default=None, help="aplica os sinais por fator do mercado (EURUSD, US500, XAUUSD, USDJPY, WTI) e escolhe o símbolo Yahoo")
    si.add_argument("--start", default=None, help="histórico Yahoo a partir desta data (YYYY-MM-DD)")
    si.add_argument("--end", default=None)
    si.add_argument("--step", type=int, default=1)
    si.add_argument("--folds", type=int, default=4)
    si.add_argument("--threshold-atr", type=float, default=1.0)
    si.add_argument("--horizon", type=int, default=240)
    si.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    si.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    si.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    si.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    si.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    si.add_argument("--walk-forward", action="store_true")
    si.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    si.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    si.set_defaults(func=cmd_simulate)

    ca = sub.add_parser("calibrate", help="ajusta e salva o calibrador de probabilidade a partir do SQLite")
    ca.add_argument("--db", default="gold_ai.db")
    ca.add_argument("--out", default="calibrator.json")
    ca.add_argument("--min-n", type=int, default=30)
    ca.set_defaults(func=cmd_calibrate)

    args = p.parse_args(argv)
    return int(args.func(args))





# ============================================================================
# INTERFACE DE FONTE DE DADOS
# ============================================================================

class DataSource(Protocol):
    """Qualquer objeto com snapshot() -> MarketSnapshot (LiveSource, MT5Source, SampleSource ou o seu)."""

    def snapshot(self) -> MarketSnapshot:  # pragma: no cover
        ...


if __name__ == "__main__":
    sys.exit(main())
