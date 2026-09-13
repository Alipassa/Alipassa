"""GOLD AI 2.0 — nível de evidência, cadeia de raciocínio do evento e vantagem estatística.

O sistema não faz "notícia → sentimento → compra". Ele percorre:
1. O que aconteceu?  2. O que o mercado esperava?  3. Qual foi a surpresa?
4. Juros?  5. Dólar?  6. Ouro?  7. Fluxo?  8. Pressão latente?  9. Só então: sinal.
E precisa saber dizer "NÃO SEI" (sem vantagem estatística → não enviar).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from .config import EngineConfig
from .events import EVENT_GOLD_SENSITIVITY
from .models import Assessment, Direction, EconomicEvent, EvidenceLevel, MarketSnapshot, Stage


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
    return "\n".join(lines)
