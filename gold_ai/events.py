"""Calendário de risco, análise pré-evento (árvore de reação) e pós-evento
(Diretriz §32, §33, §34)."""


from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .models import EconomicEvent, MarketSnapshot


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
