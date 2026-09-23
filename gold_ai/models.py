"""Modelos de dados do GOLD AI ENGINE."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


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

    # Emprego, atividade e demanda física (GOLD BIAS ENGINE — docs/DIRETRIZ_BIAS.md §8, §9, §12, §13)
    employment_surprise_sigma: Optional[float] = None  # (resultado − consenso)/desvio; >0 = mercado de trabalho mais forte que o esperado
    jobless_claims_change_pct: Optional[float] = None  # variação dos pedidos de seguro-desemprego (%)
    economy_momentum: Optional[float] = None           # -1 recessão provável .. +1 crescimento forte (GDP/ISM/PMI/varejo)
    china_demand: Optional[float] = None               # -1..+1 demanda física/importações/compras do PBoC
    india_demand: Optional[float] = None               # -1..+1 importações, festivais, casamentos, rupia
    us2y_change_bp: Optional[float] = None

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
    # FLOW ANOMALY ENGINE (5.0): informação implícita — movimento que revela algo que ainda não conhecemos
    flow_score: int = 0
    flow_status: str = "SEM ANOMALIA"       # SEM ANOMALIA | MOVIMENTO EXPLICADO | FLUXO ANÔMALO | REGIME ANÔMALO
    flow_origin: str = "—"                  # A notícia · B macro · C intermarket · D institucional provável · E anômalo
    flow_direction: float = 0.0
    anomalous_regime: bool = False
    flow_chain: str = ""
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
    price_source: str = ""             # "mt5" (corretora) | "yahoo" — no LIVE só se decide com o preço da corretora que executa
    session_start: tuple[int, int] = (22, 0)   # início da sessão (hora, minuto UTC) para o VWAP de sessão — definido pelo motor conforme o mercado

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
    # 5.2: estados do snapshot que o nível/contexto (Edge Bank) precisam ver — copiados pelo cérebro em analyze()
    news_status: str = "UNKNOWN"
    reaction_status: str = "SEM EVENTO"
    reaction_pressure: float = 0.0
    flow_status: str = "SEM ANOMALIA"
    flow_score: int = 0
    flow_origin: str = "—"
    anomalous_regime: bool = False

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
