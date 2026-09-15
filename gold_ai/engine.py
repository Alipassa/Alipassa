"""GoldAIEngine — ciclo de análise completo (Diretriz §2, §19, §20, §21, §36)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable, Optional

from .config import HORIZONS, EngineConfig
from .factors import SCORERS, accumulation_distribution, score_tecnico, sentiment_label, systemic_risk_index
from .events import next_high_impact_event
from .evidence import edge_status, event_chain, evidence_level
from .models import Assessment, Direction, FactorScore, MarketSnapshot, Signal, Stage, TechnicalReading
from .premove import analyze_premove, analyze_reversal
from .signals import SignalGate, classify, confirmations
from .telegram import format_signal


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
        s.session_start = self._session_start()          # VWAP de sessão conforme o mercado (NY para índices; 22:00 UTC nos demais)
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
            news_status=str(getattr(s, "news_status", "UNKNOWN") or "UNKNOWN"), reaction_status=str(getattr(s, "reaction_status", "SEM EVENTO") or "SEM EVENTO"),
            reaction_pressure=float(getattr(s, "reaction_pressure", 0.0) or 0.0), flow_status=str(getattr(s, "flow_status", "SEM ANOMALIA") or "SEM ANOMALIA"),
            flow_score=int(getattr(s, "flow_score", 0) or 0), flow_origin=str(getattr(s, "flow_origin", "—") or "—"),
            anomalous_regime=bool(getattr(s, "anomalous_regime", False)),
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

    def _session_start(self) -> tuple[int, int]:
        """Início da sessão para o VWAP: abertura de NY (13:30) em índices; 22:00 UTC (CME/forex) nos demais."""
        try:
            from .markets import MARKETS
            spec = MARKETS.get(self.cfg.symbol)
            if spec is not None and spec.session_hours_utc != (0, 24):
                return (spec.session_hours_utc[0], 30 if spec.session_hours_utc[0] == 13 else 0)
        except Exception:  # noqa: BLE001
            pass
        return (22, 0)

    def run_cycle(self, s: MarketSnapshot, new_event_key: Optional[str] = None) -> tuple[Assessment, Optional[Signal]]:
        a = self.analyze(s)
        return a, self.evaluate_signal(a, new_event_key)
