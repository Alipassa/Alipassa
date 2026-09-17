"""Classificação de sinais, filtro contra falsos sinais e regra anti-spam
(Diretriz §23–§28, §37)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .config import FACTOR_FAMILIES, EngineConfig
from .models import Assessment, Direction, EvidenceLevel, Signal, SignalType, Stage


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
    last_watch_at: Optional[datetime] = None          # anti-spam do WATCH: mesmo mercado/direção só a cada min_seconds_between_alerts
    last_watch_direction: Optional[Direction] = None

    def missing_for_signal(self, a: Assessment) -> str:
        """O que faltou para o sinal operacional, em números: |score| contra o limiar de sinal e confirmações contra o mínimo.
        Responde à pergunta 'por que não entrou?' na própria tela (SETUP = vantagem passou; OPPORTUNITY exige isto)."""
        need = int(self.cfg.buy)
        have = abs(a.score)
        parts = []
        if have < need:
            parts.append(f"|score| {have:.0f} < {need} (faltam {need - have:.0f})")
        n_conf = len(a.confirmations or [])
        if n_conf < self.cfg.min_confirmations:
            parts.append(f"confirmações {n_conf}/{self.cfg.min_confirmations}")
        return " · ".join(parts) if parts else "limiares atingidos"

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
                and max(a.prob_up, a.prob_down) >= self.cfg.watch_min_probability and self.last_type != SignalType.WATCH \
                and (self.last_watch_at is None or direction != self.last_watch_direction
                     or (a.time - self.last_watch_at).total_seconds() >= self.cfg.min_seconds_between_alerts):
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
        if sig_type == SignalType.WATCH:
            self.last_watch_at, self.last_watch_direction = a.time, direction
        rs = reasons_for(a, direction) if direction != Direction.LATERAL else []
        return Signal(type=sig_type, direction=direction, assessment=a, reasons=rs, trigger=trigger)
