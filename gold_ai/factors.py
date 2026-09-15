"""Pontuação dos fatores do GOLD AI SCORE (Diretriz §5–§12, §19).

Cada função recebe o MarketSnapshot e devolve um FactorScore limitado a
[-peso, +peso]. Sinal positivo = favorável à ALTA do ouro.

Princípio (§31): correlação não é causalidade. As funções procuram
*variações* (o que está começando a mudar) e não apenas níveis, e usam
saturação suave para que nenhum fator isolado domine o score.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from .models import FactorScore, MarketSnapshot, Sentiment
from .technical import analyze_multi_timeframe, volume_profile_signals


def _sat(x: float, scale: float) -> float:
    """Saturação suave em -1..+1 (tanh). `scale` é o valor que dá ~0.76."""
    return math.tanh(x / scale) if scale else 0.0


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


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
    global_score, readings = analyze_multi_timeframe(s.candles, getattr(s, "session_start", (22, 0)))
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
