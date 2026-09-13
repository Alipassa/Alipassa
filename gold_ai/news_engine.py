"""NEWS ENGINE — entrada central do cérebro (MARKET AI 4.0).

NEWS → EVENT IDENTIFIER → IMPORTÂNCIA → EXPECTATIVA → SURPRESA → DIREÇÃO ESPERADA (por mercado, via canais
de transmissão) → REAÇÃO REAL (mercado + canais) → DIVERGÊNCIA → PRESSÃO LATENTE → MARKET AI SCORE.

Conceito: NEWS ausente = UNKNOWN (peso reduzido, nunca negativo). NEWS favorável ↑ score, contrária ↓ score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from .markets import MARKETS, MarketSpec, get_market
from .models import EconomicEvent, MarketSnapshot, NewsItem

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
