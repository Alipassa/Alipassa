"""GOLD BIAS ENGINE — IA Preditiva do Ouro, viés direcional do XAU/USD (docs/DIRETRIZ_BIAS.md).

"Leia o mercado inteiro antes de formar uma opinião sobre o ouro."

INFORMAR → ANALISAR → CRUZAR → CALCULAR → EXPLICAR → ALERTAR → APRENDER

Usa o mesmo MarketSnapshot do motor (MT5 da corretora + DataEngine: Yahoo, FRED, CFTC, RSS, calendário)
e o mesmo TelegramSender. Não opera: produz o GOLD BIAS SCORE (−100..+100), a classificação
(FORTE ALTA … FORTE BAIXA), três horizontes (horas · 1 dia · 5 dias), a confiança, as contradições,
a opinião da IA e as mensagens do Telegram (relatório, atualização, alerta, reversão, manhã, fechamento).
Cada leitura é gravada no SQLite e resolvida depois contra o preço real (acerto × erro por fator).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .events import EVENT_GOLD_SENSITIVITY
from .factors import score_dolar, score_fed, score_fluxo, score_geopolitica, score_inflacao, score_juros_reais, score_tecnico
from .models import Candle, EconomicEvent, FactorScore, MarketSnapshot, NewsItem, TechnicalReading

# §23 — pesos do GOLD BIAS SCORE (soma 100). Ajustáveis por backtesting (BiasMemory.suggest_weights).
BIAS_WEIGHTS: dict[str, int] = {
    "dolar": 15,
    "juros_reais": 20,
    "fed": 15,
    "inflacao": 10,
    "emprego": 5,
    "geopolitica": 10,
    "fluxo": 10,
    "china_india": 5,
    "tecnico": 10,
}

BIAS_FACTOR_LABELS: dict[str, str] = {
    "dolar": "Dólar", "juros_reais": "Juros reais", "fed": "FED", "inflacao": "Inflação", "emprego": "Emprego",
    "geopolitica": "Geopolítica", "fluxo": "Fluxo institucional", "china_india": "China/Índia", "tecnico": "Técnico",
}

# §20/§21 — notícia → fator que ela alimenta; importância da categoria; credibilidade da fonte.
BIAS_NEWS_ROUTE: dict[str, str] = {"fed": "fed", "macro": "inflacao", "employment": "emprego", "geopolitical": "geopolitica",
                                   "systemic": "geopolitica", "flow": "fluxo", "china": "china_india", "india": "china_india", "dollar": "dolar"}
BIAS_NEWS_IMPORTANCE: dict[str, float] = {"fed": 1.0, "macro": 0.9, "employment": 0.85, "geopolitical": 0.8, "systemic": 0.9,
                                          "flow": 0.7, "china": 0.6, "india": 0.5, "dollar": 0.8, "generic": 0.3}
BIAS_SOURCE_TIERS: tuple[tuple[str, float], ...] = (
    (r"federal ?reserve|\bfed\b|fomc|ecb|bce|boj|pboc|banco central|central bank|treasury|bls|bea|census|world gold council|wgc|cftc", 1.0),
    (r"reuters|bloomberg|dow ?jones|wsj|financial times|\bft\b|cnbc|marketwatch|associated press|\bap\b|nikkei|valor", 0.85),
    (r"kitco|fxstreet|investing|forexlive|yahoo|comex|cme|spdr|lbma", 0.75),
    (r"twitter|\bx\.com\b|reddit|telegram|tiktok|youtube|forum", 0.25),
)
BIAS_RUMOR_RE = re.compile(r"\b(rumou?r|unconfirmed|sources say|reportedly|could|may|speculat|boato|rumor|não confirmad)\w*", re.IGNORECASE)
BIAS_EMPLOYMENT_RE = re.compile(r"\b(payrolls?|nfp|jobs|jobless|unemployment|desemprego|emprego|adp|jolts|hourly earnings)\b", re.IGNORECASE)
BIAS_NEWS_HALF_LIFE_MIN = 180.0

BIAS_CLASSES: tuple[tuple[float, str, str], ...] = ((70, "FORTE ALTA", "🟢"), (40, "ALTA", "🟢"), (-39.999, "NEUTRO", "🟡"),
                                                    (-69.999, "BAIXA", "🔴"), (-1000, "FORTE BAIXA", "🔴"))
BIAS_HORIZON_HOURS: dict[str, float] = {"horas": 4.0, "1d": 24.0, "5d": 120.0}
BIAS_HORIZON_LABELS: dict[str, str] = {"horas": "Próximas horas", "1d": "Próximo dia", "5d": "Próximos 5 dias"}
BIAS_MOVE_THRESHOLD_PCT: dict[str, float] = {"horas": 0.15, "1d": 0.30, "5d": 0.80}   # |movimento| abaixo disso = lateral
BIAS_EVENT_SWING: dict[str, str] = {"MUITO ALTO": "alta", "ALTO": "moderada a alta", "MÉDIO": "moderada", "BAIXO": "baixa"}


def bias_clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def bias_classify(score: float) -> tuple[str, str]:
    """§24 — (rótulo, emoji). ≥+70 FORTE ALTA · +40..+69 ALTA · −39..+39 NEUTRO · −40..−69 BAIXA · ≤−70 FORTE BAIXA."""
    for floor, label, emoji in BIAS_CLASSES:
        if score >= floor:
            return label, emoji
    return "FORTE BAIXA", "🔴"


def bias_direction(label: str) -> int:
    return 1 if "ALTA" in label else -1 if "BAIXA" in label else 0


def bias_arrow(x: Optional[float], eps: float = 0.0) -> str:
    if x is None:
        return "—"
    return "↑" if x > eps else "↓" if x < -eps else "→"


# --------------------------------------------------------------------------- §20/§21 notícias
@dataclass
class BiasNewsScore:
    headline: str
    source: str
    time: datetime
    category: str
    factor: str
    impact: int            # −3..+3 (§20)
    importance: float      # 0..1
    credibility: float     # 0..1
    recency: float         # 0..1 (meia-vida BIAS_NEWS_HALF_LIFE_MIN)

    @property
    def weight(self) -> float:
        """IMPORTÂNCIA × CREDIBILIDADE × RECÊNCIA."""
        return self.importance * self.credibility * self.recency

    @property
    def effect(self) -> float:
        """Contribuição −1..+1 no fator: impacto/3 × peso."""
        return self.impact / 3.0 * self.weight


def bias_source_credibility(source: str, headline: str = "") -> float:
    src = (source or "").lower()
    cred = 0.6
    for pattern, value in BIAS_SOURCE_TIERS:
        if re.search(pattern, src):
            cred = value
            break
    if BIAS_RUMOR_RE.search(headline or ""):
        cred *= 0.5            # §21: rumor, fonte anônima, sem confirmação
    return round(cred, 3)


def bias_score_news(item: NewsItem, now: datetime) -> BiasNewsScore:
    cat = item.category or "generic"
    if cat == "macro" and BIAS_EMPLOYMENT_RE.search(item.headline):
        cat = "employment"
    effective = item.gold_impact * (1.0 - bias_clip(item.priced_in, 0.0, 1.0))
    impact = int(max(-3, min(3, round(effective * 3))))
    age_min = max(0.0, (now - item.time).total_seconds() / 60.0)
    recency = 0.5 ** (age_min / BIAS_NEWS_HALF_LIFE_MIN)
    return BiasNewsScore(item.headline, item.source, item.time, cat, BIAS_NEWS_ROUTE.get(cat, ""), impact,
                         BIAS_NEWS_IMPORTANCE.get(cat, 0.3), bias_source_credibility(item.source, item.headline), round(recency, 3))


# --------------------------------------------------------------------------- fatores novos (§8, §12, §13)
def bias_event_surprise(events: Sequence[EconomicEvent], kinds: tuple[str, ...], now: datetime, max_age_h: float = 36.0) -> Optional[float]:
    """Surpresa relativa (−1..+1) do evento divulgado mais recente dos tipos pedidos, já no sentido do OURO."""
    done = [e for e in events if e.kind in kinds and e.actual is not None and e.consensus is not None and 0 <= (now - e.time).total_seconds() <= max_age_h * 3600]
    if not done:
        return None
    ev = max(done, key=lambda e: e.time)
    scale = max(abs(ev.consensus or 0.0) * 0.1, 0.1)
    return bias_clip(EVENT_GOLD_SENSITIVITY.get(ev.kind, 0.0) * (ev.surprise() or 0.0) / scale)


def bias_score_emprego(s: MarketSnapshot, w: float) -> FactorScore:
    """EMPREGOS → INFLAÇÃO → FED → JUROS → DÓLAR → OURO: emprego mais forte que o esperado pressiona o ouro."""
    parts: list[float] = []
    notes: list[str] = []
    if s.employment_surprise_sigma is not None:
        parts.append(-math.tanh(s.employment_surprise_sigma / 1.0))
        notes.append(f"surpresa emprego {s.employment_surprise_sigma:+.1f}σ")
    if s.jobless_claims_change_pct is not None:
        parts.append(0.5 * math.tanh(s.jobless_claims_change_pct / 8.0))
        notes.append(f"pedidos de seguro {s.jobless_claims_change_pct:+.1f}%")
    ev = bias_event_surprise(s.events, ("nfp", "unemployment", "jobless_claims", "earnings", "jolts"), s.time)
    if ev is not None:
        parts.append(ev)
        notes.append("último dado de emprego " + ("favorável" if ev > 0.1 else "desfavorável" if ev < -0.1 else "em linha"))
    if not parts:
        return FactorScore("emprego", 0.0, w, "sem dados", available=False)
    ratio = bias_clip(sum(parts) / len(parts))
    verdict = "mercado de trabalho fraco → suporte" if ratio > 0.15 else "mercado de trabalho forte → pressão" if ratio < -0.15 else "em linha"
    return FactorScore("emprego", round(ratio * w, 1), w, f"emprego: {verdict} ({', '.join(notes)})")


def bias_score_china_india(s: MarketSnapshot, w: float) -> FactorScore:
    parts: list[tuple[float, float]] = []
    notes: list[str] = []
    if s.china_demand is not None:
        parts.append((bias_clip(s.china_demand), 0.65))      # China pesa mais na demanda global
        notes.append(f"China {s.china_demand:+.2f}")
    if s.india_demand is not None:
        parts.append((bias_clip(s.india_demand), 0.35))
        notes.append(f"Índia {s.india_demand:+.2f}")
    if s.usdcnh_change_pct is not None and not parts:
        parts.append((-0.4 * math.tanh(s.usdcnh_change_pct / 0.4), 0.3))   # yuan fraco encarece o ouro em CNY
        notes.append(f"USD/CNH {s.usdcnh_change_pct:+.2f}%")
    if not parts:
        return FactorScore("china_india", 0.0, w, "sem dados", available=False)
    ratio = bias_clip(sum(v * wt for v, wt in parts) / sum(wt for _, wt in parts))
    verdict = "demanda física forte" if ratio > 0.15 else "demanda física fraca" if ratio < -0.15 else "demanda estável"
    return FactorScore("china_india", round(ratio * w, 1), w, f"China/Índia: {verdict} ({', '.join(notes)})")


def bias_score_inflacao(s: MarketSnapshot, w: float) -> FactorScore:
    """§7 — DADO REAL × EXPECTATIVA × ANTERIOR. Sem surpresa no snapshot, usa o CPI/PCE/PPI divulgado."""
    f = score_inflacao(s, w)
    if f.available:
        return f
    ev = bias_event_surprise(s.events, ("cpi", "core_cpi", "pce", "core_pce", "ppi"), s.time)
    if ev is None:
        return f
    return FactorScore("inflacao", round(ev * w, 1), w, "inflação: último dado " + ("abaixo do esperado → suporte" if ev > 0 else "acima do esperado → pressão"))


def bias_score_fluxo(s: MarketSnapshot, w: float) -> FactorScore:
    """Fluxo institucional: ETFs + bancos centrais + agressão + OI (+ COT semanal quando houver)."""
    f = score_fluxo(s, w)
    if s.cot_managed_money_net_change is None:
        return f
    cot = math.tanh(s.cot_managed_money_net_change / 15000.0)
    if not f.available:
        return FactorScore("fluxo", round(cot * 0.6 * w, 1), w, f"fluxo: só COT ({s.cot_managed_money_net_change:+.0f} contratos/sem)")
    ratio = bias_clip(0.75 * f.ratio + 0.25 * cot)
    return FactorScore("fluxo", round(ratio * w, 1), w, f.rationale + f", COT {s.cot_managed_money_net_change:+.0f}")


def bias_blend_news(f: FactorScore, news: Sequence[BiasNewsScore]) -> FactorScore:
    """§2 DECISÃO = MACRO + NOTÍCIAS: cada notícia alimenta o fator da sua categoria (25 %; 60 % do peso se o fator não tem dado)."""
    mine = [n for n in news if n.factor == f.name and n.weight > 0.05]
    if not mine:
        return f
    total_w = sum(n.weight for n in mine)
    news_ratio = bias_clip(sum(n.effect for n in mine) / max(total_w, 0.5))
    note = f"{len(mine)} notícia(s) {news_ratio:+.2f}"
    if not f.available:
        return FactorScore(f.name, round(0.6 * news_ratio * f.max_score, 1), f.max_score, f"{BIAS_FACTOR_LABELS[f.name].lower()}: só notícias ({note})")
    ratio = bias_clip(0.75 * f.ratio + 0.25 * news_ratio)
    return FactorScore(f.name, round(ratio * f.max_score, 1), f.max_score, f"{f.rationale}; {note}")


# --------------------------------------------------------------------------- leitura
@dataclass
class BiasHorizon:
    key: str
    score: float
    label: str
    emoji: str


@dataclass
class BiasEventWatch:
    name: str
    time: datetime
    minutes: float
    consensus: Optional[float]
    previous: Optional[float]
    unit: str
    if_above: str
    if_below: str
    volatility: str


@dataclass
class BiasReading:
    time: datetime
    price: float
    score: float
    label: str
    emoji: str
    confidence: float
    factors: list[FactorScore]
    horizons: dict[str, BiasHorizon]
    news: list[BiasNewsScore]
    contradictions: list[str]
    fed_stance: str
    fed_shift: str
    geo_level: str
    geo_emoji: str
    economy: str
    risk_mode: str
    oil_read: str
    crypto_read: str
    structure: dict[str, str]
    vwap: str
    ema_cross: str
    levels: dict[str, Optional[float]]
    event: Optional[BiasEventWatch]
    opinion: str
    coverage: float
    raw: dict = field(default_factory=dict)

    @property
    def direction(self) -> int:
        return bias_direction(self.label)

    def factor(self, name: str) -> Optional[FactorScore]:
        return next((f for f in self.factors if f.name == name), None)


def bias_fed_stance(tone: Optional[float], cut_change_pp: Optional[float]) -> str:
    """§6 — HAWKISH · DOVISH · NEUTRO."""
    parts = [x for x in (tone, math.tanh(cut_change_pp / 10.0) if cut_change_pp is not None else None) if x is not None]
    if not parts:
        return "INDISPONÍVEL"
    v = sum(parts) / len(parts)
    return "DOVISH" if v > 0.2 else "HAWKISH" if v < -0.2 else "NEUTRO"


def bias_geo_level(risk: Optional[float], change: Optional[float]) -> tuple[str, str]:
    """§16 — BAIXO · MODERADO · ALTO · EXTREMO."""
    if risk is None and change is None:
        return "INDISPONÍVEL", "⚪"
    lvl = (risk or 0.0) + 1.5 * max(0.0, change or 0.0)
    if lvl >= 80:
        return "EXTREMO", "🔴"
    if lvl >= 60:
        return "ALTO", "🟠"
    if lvl >= 35:
        return "MODERADO", "🟡"
    return "BAIXO", "🟢"


def bias_economy(s: MarketSnapshot) -> str:
    """§9 — crescimento forte · moderado · desaceleração · recessão provável."""
    m = s.economy_momentum
    if m is None:
        ev = bias_event_surprise(s.events, ("gdp", "ism", "pmi", "retail_sales"), s.time, max_age_h=24 * 20)
        m = -ev if ev is not None else None      # sensibilidade do ouro é negativa: devolve o sentido da economia
    if m is None:
        return "INDISPONÍVEL"
    if m >= 0.5:
        return "crescimento forte"
    if m >= 0.0:
        return "crescimento moderado"
    if m > -0.5:
        return "desaceleração"
    return "recessão provável"


def bias_risk_mode(s: MarketSnapshot) -> str:
    """§15/§17 — Risk-on × Risk-off (bolsas, VIX, cripto)."""
    pts: list[float] = []
    if s.equity_change_pct is not None:
        pts.append(math.tanh(s.equity_change_pct / 1.0))
    if s.vix_change_pct is not None:
        pts.append(-math.tanh(s.vix_change_pct / 10.0))
    if s.btc_change_pct is not None:
        pts.append(0.5 * math.tanh(s.btc_change_pct / 3.0))
    if not pts:
        return "INDISPONÍVEL"
    v = sum(pts) / len(pts)
    return "RISK-ON" if v > 0.2 else "RISK-OFF" if v < -0.2 else "misto"


def bias_structure(candles: Sequence[Candle], k: int = 3) -> str:
    """§19 — HH+HL = alta · LH+LL = baixa; senão consolidação. Pivôs de k candles de cada lado."""
    if len(candles) < 4 * k + 2:
        return "—"
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        win = candles[i - k:i + k + 1]
        if candles[i].high == max(c.high for c in win):
            highs.append(candles[i].high)
        if candles[i].low == min(c.low for c in win):
            lows.append(candles[i].low)
    if len(highs) < 2 or len(lows) < 2:
        return "consolidação"
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    if hh and hl:
        return "ALTA (HH+HL)"
    if not hh and not hl:
        return "BAIXA (LH+LL)"
    last = candles[-1].close
    if last > highs[-1]:
        return "rompimento de alta"
    if last < lows[-1]:
        return "rompimento de baixa"
    return "consolidação"


def bias_ema(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def bias_upcoming_event(s: MarketSnapshot, hours: float = 12.0) -> Optional[BiasEventWatch]:
    """§22 — próximo evento de ALTO impacto: expectativa, anterior, impacto provável e volatilidade esperada."""
    fut = [e for e in s.events if e.impact in ("ALTO", "MUITO ALTO") and e.actual is None and 0 <= (e.time - s.time).total_seconds() <= hours * 3600]
    if not fut:
        return None
    e = min(fut, key=lambda x: x.time)
    sens = EVENT_GOLD_SENSITIVITY.get(e.kind, 0.0)
    if e.kind in ("fomc", "speech"):
        above, below = "tom HAWKISH → 🔴 pressão sobre o ouro", "tom DOVISH → 🟢 favorece o ouro"
    elif sens < 0:
        above, below = "🔴 possível pressão (juros/dólar ↑)", "🟢 possível alta (juros/dólar ↓)"
    elif sens > 0:
        above, below = "🟢 favorece o ouro (economia mais fraca)", "🔴 pressiona o ouro (economia mais forte)"
    else:
        above = below = "impacto incerto — ler a reação do dólar e dos juros"
    return BiasEventWatch(e.name, e.time, round((e.time - s.time).total_seconds() / 60.0), e.consensus, e.previous, e.unit,
                          above, below, BIAS_EVENT_SWING.get(e.impact, "moderada"))


class GoldBiasEngine:
    """MACRO + NOTÍCIAS + FLUXO + SENTIMENTO + TÉCNICO + CONTEXTO → GOLD BIAS SCORE."""

    def __init__(self, weights: Optional[dict[str, float]] = None, event_window_min: float = 90.0) -> None:
        self.weights = dict(weights or BIAS_WEIGHTS)
        self.event_window_min = event_window_min
        self.previous_fed: Optional[str] = None

    # ---- fatores
    def factors(self, s: MarketSnapshot, news: Sequence[BiasNewsScore]) -> tuple[list[FactorScore], list[TechnicalReading]]:
        w = self.weights
        fs = [score_dolar(s, w["dolar"]), score_juros_reais(s, w["juros_reais"]), score_fed(s, w["fed"]), bias_score_inflacao(s, w["inflacao"]),
              bias_score_emprego(s, w["emprego"]), score_geopolitica(s, w["geopolitica"]), bias_score_fluxo(s, w["fluxo"]),
              bias_score_china_india(s, w["china_india"])]
        tec, readings = score_tecnico(s, w["tecnico"])
        fs.append(tec)
        return [bias_blend_news(f, news) for f in fs], readings

    @staticmethod
    def total(factors: Sequence[FactorScore]) -> tuple[float, float]:
        """Score −100..+100 normalizado pelos fatores disponíveis, encolhido pela cobertura (com < 70 % dos pesos
        disponíveis o score perde força — nunca uma decisão com um indicador só, §39)."""
        avail = [f for f in factors if f.available]
        wa = sum(f.max_score for f in avail)
        wt = sum(f.max_score for f in factors) or 1.0
        if not wa:
            return 0.0, 0.0
        coverage = wa / wt
        return round(sum(f.score for f in avail) / wa * 100 * min(1.0, coverage / 0.7), 1), coverage

    @staticmethod
    def horizon_scores(factors: Sequence[FactorScore], readings: Sequence[TechnicalReading], news: Sequence[BiasNewsScore], score: float) -> dict[str, float]:
        """§25 — curto (M5–H1 + notícias recentes) · 1 dia (H4/D1 + score) · 5 dias (macro + D1/W1)."""
        def tf(names: tuple[str, ...]) -> Optional[float]:
            v = [r.score for r in readings if r.timeframe in names and "dados insuficientes" not in r.notes]
            return sum(v) / len(v) if v else None

        def mix(parts: list[tuple[Optional[float], float]]) -> float:
            p = [(v, w) for v, w in parts if v is not None]
            return round(sum(v * w for v, w in p) / sum(w for _, w in p) * 100, 1) if p else 0.0

        recent = [n for n in news if n.recency > 0.5]
        news_now = bias_clip(sum(n.effect for n in recent) / max(sum(n.weight for n in recent), 0.5)) if recent else None
        macro = [f for f in factors if f.available and f.name not in ("tecnico",)]
        macro_ratio = sum(f.score for f in macro) / sum(f.max_score for f in macro) if macro else None
        return {
            "horas": mix([(tf(("M5", "M15", "M30", "H1")), 0.5), (news_now, 0.2), (score / 100, 0.3)]),
            "1d": mix([(tf(("H4", "D1")), 0.35), (score / 100, 0.65)]),
            "5d": mix([(macro_ratio, 0.7), (tf(("D1", "W1")), 0.3)]),
        }

    @staticmethod
    def contradictions(s: MarketSnapshot, factors: Sequence[FactorScore], readings: Sequence[TechnicalReading], news: Sequence[BiasNewsScore]) -> list[str]:
        """§27 — procurar ativamente sinais contrários."""
        g = {f.name: f for f in factors}
        out: list[str] = []

        def r(name: str) -> float:
            f = g.get(name)
            return f.ratio if f and f.available else 0.0

        macro = (r("dolar") * 15 + r("juros_reais") * 20 + r("fed") * 15) / 50
        tec = r("tecnico")
        if tec > 0.25 and macro < -0.2:
            out.append("Existe divergência entre técnico e macro: gráfico em alta, mas dólar/juros/FED pressionam o ouro.")
        elif tec < -0.25 and macro > 0.2:
            out.append("Existe divergência entre técnico e macro: gráfico em baixa, mas dólar/juros/FED favorecem o ouro.")
        if r("dolar") > 0.3 and r("juros_reais") < -0.3:
            out.append("Dólar favorece, mas juros reais sobem — sinais macro opostos.")
        elif r("dolar") < -0.3 and r("juros_reais") > 0.3:
            out.append("Juros reais caem, mas o dólar se fortalece — sinais macro opostos.")
        if s.etf_flow_musd is not None and s.price_change_pct:
            if s.price_change_pct > 0.15 and s.etf_flow_musd < -50:
                out.append(f"Preço sobe com saída dos ETFs ({s.etf_flow_musd:+.0f}M) — alta sem fluxo institucional.")
            elif s.price_change_pct < -0.15 and s.etf_flow_musd > 50:
                out.append(f"Preço cai com entrada nos ETFs ({s.etf_flow_musd:+.0f}M) — possível acumulação.")
        news_sum = sum(n.effect for n in news if n.recency > 0.5)
        if news_sum > 0.4 and s.price_change_pct < -0.2:
            out.append("Notícias favoráveis, mas o preço cai — mercado não está reagindo às notícias.")
        elif news_sum < -0.4 and s.price_change_pct > 0.2:
            out.append("Notícias desfavoráveis, mas o preço sobe — algo mais forte sustenta o ouro.")
        if bias_risk_mode(s) == "RISK-OFF" and s.price_change_pct < -0.3:
            out.append("Risk-off sem demanda por proteção: ouro caindo junto (possível venda por liquidez).")
        if s.cot_managed_money_percentile is not None and s.cot_managed_money_percentile >= 90 and tec > 0.25:
            out.append("Especuladores muito comprados (COT ≥ p90) — risco de realização técnica.")
        return out

    @staticmethod
    def confidence(factors: Sequence[FactorScore], score: float, contradictions: Sequence[str], event: Optional[BiasEventWatch],
                   coverage: float, window_min: float) -> float:
        """§26 — concordância entre fatores + cobertura; penaliza divergências, evento próximo e mercado lateral. Teto de 90 %: nunca certeza (§37)."""
        avail = [f for f in factors if f.available and abs(f.ratio) > 0.1]
        sign = 1 if score > 0 else -1 if score < 0 else 0
        if not avail or sign == 0:
            agree = 0.5
        else:
            aligned = sum(f.max_score * abs(f.ratio) for f in avail if f.score * sign > 0)
            total = sum(f.max_score * abs(f.ratio) for f in avail)
            agree = aligned / total if total else 0.5
        conf = 20 + 35 * agree + 15 * coverage + 25 * min(1.0, abs(score) / 80)
        conf -= 8 * len(contradictions)
        if event is not None and event.minutes <= window_min:
            conf -= 12
        if abs(score) < 20:
            conf = min(conf, 55)
        return round(max(5.0, min(90.0, conf)), 0)

    def opinion(self, label: str, factors: Sequence[FactorScore], contradictions: Sequence[str], event: Optional[BiasEventWatch]) -> str:
        """§29 — "Minha leitura atual é de ALTA/BAIXA/NEUTRO para o ouro porque..."."""
        d = bias_direction(label)
        word = "ALTA" if d > 0 else "BAIXA" if d < 0 else "NEUTRO"
        avail = sorted([f for f in factors if f.available], key=lambda f: abs(f.score), reverse=True)
        if d == 0:
            pro = [f for f in avail if f.score > 0.5][:2]
            con = [f for f in avail if f.score < -0.5][:2]
            why = "os fatores se equilibram"
            if pro or con:
                why += f": a favor, {', '.join(BIAS_FACTOR_LABELS[f.name].lower() for f in pro) or 'nada relevante'}; contra, {', '.join(BIAS_FACTOR_LABELS[f.name].lower() for f in con) or 'nada relevante'}"
        else:
            drivers = [f for f in avail if f.score * d > 0.5][:3]
            against = [f for f in avail if f.score * d < -0.5][:1]
            why = " + ".join(f.rationale for f in drivers) or "o conjunto dos fatores"
            if against:
                why += f". O principal fator contrário é {BIAS_FACTOR_LABELS[against[0].name].lower()} ({against[0].rationale})"
        txt = f"Minha leitura atual é de {word} para o ouro porque {why}."
        if contradictions:
            txt += " Atenção: " + contradictions[0]
        if event is not None:
            txt += f" A leitura pode mudar com {event.name} em {event.minutes:.0f} min."
        return txt

    def analyze(self, s: MarketSnapshot) -> BiasReading:
        news = sorted((bias_score_news(n, s.time) for n in s.news), key=lambda n: abs(n.effect), reverse=True)
        factors, readings = self.factors(s, news)
        score, coverage = self.total(factors)
        label, emoji = bias_classify(score)
        hz = {k: BiasHorizon(k, v, *bias_classify(v)) for k, v in self.horizon_scores(factors, readings, news, score).items()}
        contra = self.contradictions(s, factors, readings, news)
        event = bias_upcoming_event(s)
        conf = self.confidence(factors, score, contra, event, coverage, self.event_window_min)
        fed = bias_fed_stance(s.fed_tone, s.fed_cut_prob_change_pp)
        shift = "" if self.previous_fed in (None, fed) or fed == "INDISPONÍVEL" else f"{self.previous_fed} → {fed}"
        self.previous_fed = fed if fed != "INDISPONÍVEL" else self.previous_fed
        geo, geo_emoji = bias_geo_level(s.geopolitical_risk, s.geopolitical_risk_change)
        structure = {tf: bias_structure(s.candles[tf]) for tf in ("H1", "H4", "D1") if s.candles.get(tf)}
        by_tf = {r.timeframe: r for r in readings}
        ref = by_tf.get("H1") or by_tf.get("M30") or by_tf.get("M15")
        vwap = "—" if ref is None or ref.vwap_position is None else ("acima" if ref.vwap_position > 0 else "abaixo")
        ema_cross = "—"
        h1 = s.candles.get("H1") or []
        if len(h1) > 30:
            closes = [c.close for c in h1]
            e9, e21 = bias_ema(closes, 9), bias_ema(closes, 21)
            p9, p21 = bias_ema(closes[:-3], 9), bias_ema(closes[:-3], 21)
            if None not in (e9, e21, p9, p21):
                if e9 > e21 and p9 <= p21:
                    ema_cross = "cruzamento de alta"
                elif e9 < e21 and p9 >= p21:
                    ema_cross = "cruzamento de baixa"
                else:
                    ema_cross = "9 acima da 21" if e9 > e21 else "9 abaixo da 21"
        levels = {"suporte": ref.support if ref else None, "resistencia": ref.resistance if ref else None}
        oil = "—" if s.oil_change_pct is None else (f"petróleo {s.oil_change_pct:+.1f}% → " + ("pressão inflacionária (Fed mais duro)" if s.oil_change_pct > 1.5
                                                                                           else "alívio inflacionário" if s.oil_change_pct < -1.5 else "neutro"))
        crypto = "—" if s.btc_change_pct is None else f"BTC {s.btc_change_pct:+.1f}% ({'apetite a risco' if s.btc_change_pct > 2 else 'aversão a risco' if s.btc_change_pct < -2 else 'estável'})"
        return BiasReading(s.time, s.price, score, label, emoji, conf, factors, hz, news, contra, fed, shift, geo, geo_emoji, bias_economy(s),
                           bias_risk_mode(s), oil, crypto, structure, vwap, ema_cross, levels, event, self.opinion(label, factors, contra, event), round(coverage, 2),
                           raw={"dxy": s.dxy_change_pct, "us10y": s.us10y_change_bp, "us2y": s.us2y_change_bp, "real": s.real_yield_change_bp,
                                "etf": s.etf_flow_musd, "price_change_pct": s.price_change_pct})


# --------------------------------------------------------------------------- Telegram (§28, §30, §31, §33)
def bias_fmt_price(p: float) -> str:
    return f"US$ {p:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if p else "—"


def bias_factor_word(f: Optional[FactorScore]) -> str:
    if f is None or not f.available:
        return "sem dado"
    return "favorável" if f.ratio > 0.15 else "desfavorável" if f.ratio < -0.15 else "neutro"


def format_bias_message(r: BiasReading, title: str = "🥇 GOLD MARKET AI", local_tz_hours: float = -3.0) -> str:
    """§33 — mensagem curta e objetiva; começa pelo resumo executivo (§28)."""
    local = r.time + timedelta(hours=local_tz_hours)
    raw = r.raw
    f = r.factor
    hz = " · ".join(f"{BIAS_HORIZON_LABELS[k]}: {h.emoji} {h.label.title()}" for k, h in r.horizons.items())
    lines = [title, "", f"⏰ {local:%d/%m %H:%M} (UTC{local_tz_hours:+.0f})", f"XAU/USD: {bias_fmt_price(r.price)}", "",
             f"VIÉS: {r.emoji} {r.label}", f"CONFIANÇA: {r.confidence:.0f}%", f"SCORE: {r.score:+.0f}", hz, "",
             "RESUMO",
             f"• Dólar: {bias_arrow(raw.get('dxy'), 0.02)} {bias_factor_word(f('dolar'))}",
             f"• Treasury 10Y: {bias_arrow(raw.get('us10y'), 0.5)} {bias_factor_word(f('juros_reais')) if raw.get('us10y') is not None else 'sem dado'}",
             f"• Juros reais: {bias_arrow(raw.get('real'), 0.5)} {bias_factor_word(f('juros_reais'))}",
             f"• FED: {r.fed_stance.lower()}" + (f" (mudou: {r.fed_shift})" if r.fed_shift else ""),
             f"• Inflação: {bias_factor_word(f('inflacao'))} · Emprego: {bias_factor_word(f('emprego'))}",
             f"• Economia: {r.economy} · Mercado: {r.risk_mode}",
             f"• Fluxo institucional: {bias_factor_word(f('fluxo'))}" + (f" (ETFs {raw['etf']:+.0f}M)" if raw.get("etf") is not None else ""),
             f"• China/Índia: {bias_factor_word(f('china_india'))}",
             f"• Geopolítica: {r.geo_emoji} {r.geo_level}"]
    if r.oil_read != "—" or r.crypto_read != "—":
        lines.append("• " + " · ".join(x for x in (r.oil_read, r.crypto_read) if x != "—"))
    top_news = [n for n in r.news if n.impact != 0][:3]
    if top_news:
        lines += ["", "NOTÍCIAS"] + [f"• [{n.impact:+d}] {n.headline[:110]}" for n in top_news]
    lines += ["", "TÉCNICO"]
    lines += [f"• {tf}: {st}" for tf, st in r.structure.items()]
    lines.append(f"• VWAP: {r.vwap} · EMA 9/21: {r.ema_cross}")
    lv = r.levels
    if lv.get("suporte") or lv.get("resistencia"):
        lines.append(f"• Suporte {lv.get('suporte') or 0:.2f} · Resistência {lv.get('resistencia') or 0:.2f}")
    if r.contradictions:
        lines += ["", "⚠️ CONTRADIÇÕES"] + [f"• {c}" for c in r.contradictions[:3]]
    lines += ["", "LEITURA DA IA", f"\"{r.opinion}\""]
    main = "Alta" if r.direction > 0 else "Baixa" if r.direction < 0 else "Lateral/incerto"
    lines += ["", f"🎯 CENÁRIO PRINCIPAL: {main}"]
    if r.event is not None:
        e = r.event
        lines += [f"⚠️ RISCO: {e.name} em {e.minutes:.0f} min (volatilidade {e.volatility})",
                  f"   Se vier ACIMA do esperado: {e.if_above}", f"   Se vier ABAIXO do esperado: {e.if_below}"]
    else:
        lines.append("⚠️ RISCO: reversão se dólar e juros reais virarem contra o cenário")
    lines += ["", "Viés probabilístico, não garantia de movimento."]
    return "\n".join(lines)


def format_bias_event_warning(e: BiasEventWatch, r: BiasReading) -> str:
    """§22 — EVENTO DE ALTO IMPACTO."""
    fmt = lambda v: "—" if v is None else f"{v}{e.unit}"  # noqa: E731
    return "\n".join(["⏳ EVENTO DE ALTO IMPACTO", "", f"{e.name} em {e.minutes:.0f} min ({e.time:%H:%M} UTC)",
                      f"Expectativa: {fmt(e.consensus)} · Anterior: {fmt(e.previous)}",
                      f"Acima do esperado: {e.if_above}", f"Abaixo do esperado: {e.if_below}",
                      f"Volatilidade esperada: {e.volatility}", "", f"Viés atual: {r.emoji} {r.label} — {r.confidence:.0f}% (confiança reduzida até o dado)"])


def format_bias_alert(r: BiasReading, reason: str, prev: Optional[dict]) -> str:
    """§30 — 🚨 GOLD ALERT."""
    move = ""
    if prev and prev.get("price"):
        move = f"{(r.price / prev['price'] - 1) * 100:+.2f}% desde a última leitura"
    impact = "🟢 Favorável ao ouro" if r.score > (prev or {}).get("score", 0) else "🔴 Desfavorável ao ouro"
    return "\n".join(["🚨 GOLD ALERT", "", f"Evento: {reason}", f"Impacto esperado: {impact}",
                      f"Motivo: {r.opinion.split(' porque ', 1)[-1] if ' porque ' in r.opinion else r.opinion}",
                      f"XAU/USD: {bias_fmt_price(r.price)}" + (f" ({move})" if move else ""), f"Novo viés: {r.emoji} {r.label}", f"Confiança: {r.confidence:.0f}%"])


def format_bias_reversal(r: BiasReading, prev: dict) -> str:
    """§31 — 🚨 REVERSÃO DE CENÁRIO."""
    moves = []
    for f in r.factors:
        before = (prev.get("factors") or {}).get(f.name)
        if before is not None and f.available and abs(f.score - before) >= 0.25 * f.max_score:
            moves.append((abs(f.score - before), f"{BIAS_FACTOR_LABELS[f.name]} virou {'a favor' if f.score > before else 'contra'} o ouro"))
    changed = [txt for _, txt in sorted(moves, reverse=True)[:4]]
    return "\n".join(["🚨 REVERSÃO DE CENÁRIO", "", f"Antes: {prev.get('emoji', '')} {prev.get('label')} — {prev.get('confidence', 0):.0f}%",
                      f"Agora: {r.emoji} {r.label} — {r.confidence:.0f}%", "", "Motivo: " + (" + ".join(changed) if changed else r.opinion)])


class BiasNotifier:
    """§34 — decide o que enviar: relatório, alerta extraordinário, reversão, aviso de evento ou nada (sem mensagens repetitivas).
    Estado persistido em JSON para sobreviver a reinícios."""

    def __init__(self, state_path: Optional[str] = None, min_score_change: float = 15.0, alert_score_jump: float = 25.0,
                 min_seconds_between_updates: int = 1800) -> None:
        self.state_path = state_path
        self.min_score_change = min_score_change
        self.alert_score_jump = alert_score_jump
        self.min_seconds = min_seconds_between_updates
        self.state: dict = {}
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path, encoding="utf-8") as fh:
                    self.state = json.load(fh)
            except (OSError, ValueError):
                self.state = {}

    def _save(self) -> None:
        if not self.state_path:
            return
        d = os.path.dirname(self.state_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=1)

    @staticmethod
    def snapshot_of(r: BiasReading) -> dict:
        return {"time": r.time.isoformat(), "price": r.price, "score": r.score, "label": r.label, "emoji": r.emoji, "confidence": r.confidence,
                "fed": r.fed_stance, "geo": r.geo_level, "structure": r.structure, "factors": {f.name: f.score for f in r.factors if f.available},
                "news": [n.headline for n in r.news[:10]]}

    def decide(self, r: BiasReading) -> list[tuple[str, str]]:
        """Lista de (tipo, texto). Tipos: relatorio · reversao · alerta · evento · atualizacao."""
        prev = self.state.get("last")
        out: list[tuple[str, str]] = []
        if prev is None:
            out.append(("relatorio", format_bias_message(r)))
        else:
            d_prev, d_now = bias_direction(prev.get("label", "NEUTRO")), r.direction
            jump = abs(r.score - prev.get("score", 0.0))
            new_news = [n for n in r.news if n.headline not in prev.get("news", []) and abs(n.impact) >= 2 and n.credibility >= 0.7 and n.recency > 0.7]
            reasons = []
            if new_news:
                reasons.append(f"notícia relevante — {new_news[0].headline[:100]}")
            if r.fed_stance != prev.get("fed") and prev.get("fed") not in (None, "INDISPONÍVEL") and r.fed_stance != "INDISPONÍVEL":
                reasons.append(f"FED mudou de {prev.get('fed')} para {r.fed_stance}")
            if r.geo_level == "EXTREMO" and prev.get("geo") != "EXTREMO":
                reasons.append("risco geopolítico EXTREMO")
            broke = [tf for tf, st in r.structure.items() if "rompimento" in st and (prev.get("structure") or {}).get(tf) != st]
            if broke:
                reasons.append(f"rompimento no {', '.join(broke)}")
            if d_prev and d_now and d_prev != d_now:
                out.append(("reversao", format_bias_reversal(r, prev)))
            elif jump >= self.alert_score_jump or (reasons and jump >= self.min_score_change / 2):
                out.append(("alerta", format_bias_alert(r, "; ".join(reasons) or f"score mudou {r.score - prev.get('score', 0):+.0f} pontos", prev)))
            elif r.label != prev.get("label") or jump >= self.min_score_change:
                last_t = datetime.fromisoformat(self.state.get("last_sent", prev["time"]))
                if (r.time - last_t).total_seconds() >= self.min_seconds or r.label != prev.get("label"):
                    out.append(("atualizacao", format_bias_message(r, title="🥇 GOLD MARKET AI — ATUALIZAÇÃO")))
        if r.event is not None and r.event.minutes <= 60 and self.state.get("warned_event") != f"{r.event.name}@{r.event.time.isoformat()}":
            out.append(("evento", format_bias_event_warning(r.event, r)))
            self.state["warned_event"] = f"{r.event.name}@{r.event.time.isoformat()}"
        self.state["last"] = self.snapshot_of(r)
        if out:
            self.state["last_sent"] = r.time.isoformat()
        self._save()
        return out


# --------------------------------------------------------------------------- memória e aprendizado (§35, §36)
class BiasMemory:
    """SQLite: previsão, horário, preço, score, confiança, notícias e indicadores → resultado posterior por horizonte."""

    def __init__(self, path: str = "dados/gold_bias.db") -> None:
        d = os.path.dirname(path)
        if d and path != ":memory:":
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS bias_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, price REAL, score REAL, label TEXT, confidence REAL,
            horizons TEXT, factors TEXT, news TEXT, contradictions TEXT, kind TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS bias_outcomes (
            prediction_id INTEGER, horizon TEXT, predicted TEXT, price_after REAL, move_pct REAL, hit INTEGER,
            PRIMARY KEY (prediction_id, horizon))""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record(self, r: BiasReading, kind: str = "leitura") -> int:
        cur = self.db.execute(
            "INSERT INTO bias_predictions (time, price, score, label, confidence, horizons, factors, news, contradictions, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r.time.isoformat(), r.price, r.score, r.label, r.confidence, json.dumps({k: h.label for k, h in r.horizons.items()}),
             json.dumps({f.name: f.score for f in r.factors if f.available}),
             json.dumps([{"h": n.headline, "i": n.impact, "w": round(n.weight, 3)} for n in r.news[:10]], ensure_ascii=False),
             json.dumps(r.contradictions, ensure_ascii=False), kind))
        self.db.commit()
        return int(cur.lastrowid)

    @staticmethod
    def price_at(candles: Sequence[Candle], t: datetime) -> Optional[float]:
        """Fechamento do primeiro candle que termina em/depois de t (sem olhar o futuro além do alvo)."""
        for c in candles:
            if c.time >= t:
                return c.close
        return None

    def resolve(self, candles: Sequence[Candle], now: datetime) -> int:
        """Resolve previsões cujo horizonte já passou: ACERTO se a direção bateu (ou lateral ficou dentro da faixa)."""
        rows = self.db.execute("SELECT id, time, price, horizons FROM bias_predictions").fetchall()
        done = {(pid, h) for pid, h in self.db.execute("SELECT prediction_id, horizon FROM bias_outcomes")}
        n = 0
        for pid, t, price, hz in rows:
            t0 = datetime.fromisoformat(t)
            for h, label in json.loads(hz).items():
                if (pid, h) in done or not price:
                    continue
                target = t0 + timedelta(hours=BIAS_HORIZON_HOURS.get(h, 4.0))
                if target > now:
                    continue
                after = self.price_at(candles, target)
                if after is None:
                    continue
                move = (after / price - 1) * 100
                thr = BIAS_MOVE_THRESHOLD_PCT.get(h, 0.2)
                real = 1 if move > thr else -1 if move < -thr else 0
                hit = int(real == bias_direction(label))
                self.db.execute("INSERT INTO bias_outcomes VALUES (?,?,?,?,?,?)", (pid, h, label, after, round(move, 4), hit))
                n += 1
        self.db.commit()
        return n

    def stats(self) -> dict:
        """Acerto por horizonte, por classe, por hora do dia e poder preditivo de cada fator (sinal do fator × movimento real)."""
        out: dict = {"horizontes": {}, "classes": {}, "horas": {}, "fatores": {}}
        rows = self.db.execute("""SELECT p.time, p.label, p.factors, o.horizon, o.predicted, o.move_pct, o.hit
                                  FROM bias_outcomes o JOIN bias_predictions p ON p.id = o.prediction_id""").fetchall()
        for t, _label, factors, h, predicted, move, hit in rows:
            for key, bucket in ((h, "horizontes"), (predicted, "classes"), (f"{datetime.fromisoformat(t).hour:02d}h", "horas")):
                a = out[bucket].setdefault(key, [0, 0])
                a[0] += hit
                a[1] += 1
            thr = BIAS_MOVE_THRESHOLD_PCT.get(h, 0.2)
            if abs(move) <= thr:
                continue
            for name, sc in json.loads(factors).items():
                if abs(sc) < 0.5:
                    continue
                a = out["fatores"].setdefault(name, [0, 0])
                a[0] += int((sc > 0) == (move > 0))
                a[1] += 1
        return out

    def suggest_weights(self, base: Optional[dict[str, float]] = None, min_n: int = 30, strength: float = 1.5) -> dict[str, float]:
        """§36 — ajusta pesos pelo acerto de cada fator, encolhido pela amostra (n / (n + min_n)); soma volta a 100."""
        base = dict(base or BIAS_WEIGHTS)
        fat = self.stats()["fatores"]
        raw = {}
        for name, w in base.items():
            hits, n = fat.get(name, (0, 0))
            edge = (hits / n - 0.5) if n else 0.0
            shrink = n / (n + min_n)
            raw[name] = max(1.0, w * (1 + strength * edge * shrink * 2))
        tot = sum(raw.values())
        return {k: round(v * 100 / tot, 1) for k, v in raw.items()}

    def today(self, day: datetime) -> list[tuple]:
        d = day.date().isoformat()
        return self.db.execute("SELECT id, time, price, score, label, confidence, news FROM bias_predictions WHERE substr(time, 1, 10) = ? ORDER BY time", (d,)).fetchall()


def render_bias_stats(mem: BiasMemory) -> str:
    st = mem.stats()

    def rate(d: dict) -> list[str]:
        return [f"  {k:<14} {h}/{n} = {h / n:.0%}" for k, (h, n) in sorted(d.items()) if n]

    lines = ["GOLD BIAS — PREVISÃO × RESULTADO REAL", "", "Por horizonte:"] + (rate(st["horizontes"]) or ["  (nada resolvido ainda)"])
    lines += ["", "Por classe:"] + (rate(st["classes"]) or ["  —"])
    lines += ["", "Poder preditivo por fator (sinal do fator × direção real):"] + (rate(st["fatores"]) or ["  —"])
    lines += ["", "Por hora (UTC):"] + (rate(st["horas"]) or ["  —"])
    sw = mem.suggest_weights()
    lines += ["", "Pesos sugeridos (encolhidos pela amostra):"] + [f"  {k:<12} {BIAS_WEIGHTS[k]:>3} → {v:>5}" for k, v in sw.items()]
    return "\n".join(lines)


def format_bias_morning(r: BiasReading) -> str:
    """§32 — RELATÓRIO DA MANHÃ: macro + notícias + técnico + direção provável."""
    return format_bias_message(r, title="🌅 GOLD MARKET AI — RELATÓRIO DA MANHÃ")


def format_bias_closing(r: BiasReading, mem: Optional[BiasMemory], candles: Sequence[Candle] = ()) -> str:
    """§32 — RELATÓRIO DE FECHAMENTO: o que aconteceu, notícias, fatores que acertaram/falharam, previsão × resultado."""
    rows = mem.today(r.time) if mem else []
    lines = ["🌙 GOLD MARKET AI — FECHAMENTO", "", f"XAU/USD: {bias_fmt_price(r.price)}"]
    if rows:
        first = rows[0]
        move = (r.price / first[2] - 1) * 100 if first[2] else 0.0
        real = 1 if move > BIAS_MOVE_THRESHOLD_PCT["1d"] else -1 if move < -BIAS_MOVE_THRESHOLD_PCT["1d"] else 0
        lines += [f"Dia: {move:+.2f}% desde a 1ª leitura ({bias_fmt_price(first[2])})",
                  f"Previsão da manhã: {first[4]} ({first[5]:.0f}%) → resultado: {'ALTA' if real > 0 else 'BAIXA' if real < 0 else 'LATERAL'} "
                  f"{'✅ ACERTO' if bias_direction(first[4]) == real else '❌ ERRO'}",
                  f"Leituras no dia: {len(rows)} · viés final {r.emoji} {r.label}"]
        news = {}
        for row in rows:
            for n in json.loads(row[6] or "[]"):
                if abs(n.get("i", 0)) >= 2:
                    news[n["h"]] = n["i"]
        if news:
            lines += ["", "Notícias que movimentaram:"] + [f"• [{i:+d}] {h[:100]}" for h, i in list(news.items())[:5]]
        ok = [BIAS_FACTOR_LABELS[f.name] for f in r.factors if f.available and abs(f.ratio) > 0.2 and (f.score > 0) == (move > 0) and real != 0]
        bad = [BIAS_FACTOR_LABELS[f.name] for f in r.factors if f.available and abs(f.ratio) > 0.2 and (f.score > 0) != (move > 0) and real != 0]
        lines += ["", "Fatores que acertaram: " + (", ".join(ok) or "—"), "Fatores que falharam: " + (", ".join(bad) or "—")]
    else:
        lines.append("Sem leituras gravadas hoje (rode com --db para comparar previsão × resultado).")
    lines += ["", "LEITURA DA IA", f"\"{r.opinion}\""]
    return "\n".join(lines)
