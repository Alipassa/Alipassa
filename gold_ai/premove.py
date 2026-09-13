"""Sistema de reação antecipada, estágios e reversão (Diretriz §14, §15, §16, §22, §26).

A pergunta central: "os fundamentos já mudaram e o preço ainda não?"
"""

from __future__ import annotations

from .config import EngineConfig
from .models import Direction, FactorScore, MarketSnapshot, PreMoveAnalysis, ReversalAnalysis, Stage, TechnicalReading


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
