"""FLOW ANOMALY ENGINE — informação IMPLÍCITA (MARKET AI 5.0).

O News Engine pergunta "existe uma informação que explica o movimento?". Este motor pergunta o inverso:
"existe um movimento que revela uma informação que ainda não conhecemos?"

FLOW SCORE 0–100 = preço anormal (24) + velocidade (19) + volume/ticks (17) + cross-market não explica (16) + persistência (10)
+ notícia explicativa (penalidade até −30; 0 quando não há). Assinaturas:
  A  líderes explicam (DXY/yields no sentido esperado)       → movimento macro/rates plausível
  B  líderes parados, par confirma (prata p/ ouro), volume ↑ → fluxo específico do ativo / comprador institucional POSSÍVEL
  C  líderes CONTRA o movimento                              → extremamente anômalo → ANOMALOUS FLOW REGIME
Classificação da origem: A notícia conhecida · B macro conhecido · C fluxo intermarket · D fluxo institucional provável · E anômalo sem explicação.
REGRA: nunca chamar de "compra de banco central". Origem = DESCONHECIDA até existir evidência.

Saída: FlowAssessment + evento IMPLÍCITO (IdentifiedEvent kind="flow_<MERCADO>_<up|down>") que entra no mesmo REACTION ENGINE:
relógio, lead-lag e aprendizado de propagação (P(EURUSD acompanha | fluxo anômalo no ouro)) sem código novo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .models import Candle, MarketSnapshot
from .news_engine import CHANNEL_TO_MARKET, IdentifiedEvent, TRANSMISSION, expected_direction

FLOW_THRESHOLD = 70          # ≥ 70 = fluxo anômalo (evento implícito)
REGIME_THRESHOLD = 70        # assinatura C com score ≥ 70 = ANOMALOUS FLOW REGIME
# par de confirmação por mercado (ativo que costuma acompanhar um fluxo específico)
PAIR_FIELD = {"XAUUSD": "silver_change_pct", "WTI": "oil_change_pct"}
# canais que um fluxo anômalo em cada mercado transmite (sem notícia, a direção do líder define o canal)
FLOW_TRANSMISSION: dict[str, dict[str, float]] = {
    "flow_XAUUSD_up": {"safe_haven": +1.0, "dollar": -0.3, "risk": -0.2}, "flow_XAUUSD_down": {"safe_haven": -1.0, "dollar": +0.3, "risk": +0.2},
    "flow_US500_up": {"risk": +1.0, "safe_haven": -0.3}, "flow_US500_down": {"risk": -1.0, "safe_haven": +0.3},
    "flow_EURUSD_up": {"dollar": -1.0}, "flow_EURUSD_down": {"dollar": +1.0},
    "flow_USDJPY_up": {"dollar": +0.7, "risk": +0.3}, "flow_USDJPY_down": {"dollar": -0.7, "risk": -0.3},
    "flow_WTI_up": {"oil": +1.0, "risk": +0.2}, "flow_WTI_down": {"oil": -1.0, "risk": -0.2},
}
for _k, _v in FLOW_TRANSMISSION.items():
    TRANSMISSION.setdefault(_k, _v)      # o REACTION ENGINE passa a conhecer os eventos implícitos


@dataclass
class FlowAssessment:
    market: str
    score: int
    direction: float                       # +1 / −1 / 0
    components: dict[str, int] = field(default_factory=dict)
    signature: str = "—"                   # A | B | C | —
    origin: str = "—"                      # A notícia · B macro · C intermarket · D institucional provável · E anômalo sem explicação · —
    status: str = "SEM ANOMALIA"           # SEM ANOMALIA | MOVIMENTO EXPLICADO | FLUXO ANÔMALO | REGIME ANÔMALO
    anomalous_regime: bool = False
    move_atr: float = 0.0
    minutes: Optional[float] = None
    start_time: Optional[datetime] = None
    leaders: dict[str, str] = field(default_factory=dict)
    chain: str = ""

    @property
    def is_anomalous(self) -> bool:
        return self.score >= FLOW_THRESHOLD and self.origin in ("D", "E")

    def implicit_event(self, now: datetime) -> Optional[IdentifiedEvent]:
        """Evento implícito para o REACTION ENGINE (só quando anômalo). importance ∝ score; sem consenso/surpresa."""
        if not self.is_anomalous or self.direction == 0:
            return None
        kind = f"flow_{self.market}_{'up' if self.direction > 0 else 'down'}"
        t0 = self.start_time or now
        return IdentifiedEvent(kind, f"FLUXO ANÔMALO {self.market} {'↑' if self.direction > 0 else '↓'} ({self.move_atr:+.2f} ATR, origem não identificada)",
                               t0, min(1.0, self.score / 100.0), None, None, None, 1.0, "flow_anomaly", 0.0)


def _move_profile(s: MarketSnapshot, sign: float) -> tuple[float, Optional[float], Optional[datetime], float, float]:
    """(movimento atual em ATR na direção `sign`, minutos desde o início do impulso, instante de início, pico em ATR, retração 0..1)
    a partir dos candles mais finos disponíveis (M1/M5/M15/H1) — só passado."""
    if not s.atr or not s.price:
        return 0.0, None, None, 0.0, 0.0
    for tf, minutes in (("M1", 1), ("M5", 5), ("M15", 15), ("H1", 60)):
        cs = s.candles.get(tf) or []
        if len(cs) >= 6:
            break
    else:
        move = (s.price_change_pct or 0.0) / 100.0 * s.price / s.atr * sign
        return move, None, None, max(move, 0.0), 0.0
    window = cs[-max(6, min(len(cs), 120 // minutes)):]           # até 2 h
    # início do impulso: último mínimo (para alta) / máximo (para baixa) da janela antes do preço atual
    ref_i, ref = 0, None
    for i, c in enumerate(window):
        v = c.low if sign > 0 else c.high
        if ref is None or (sign > 0 and v <= ref) or (sign < 0 and v >= ref):
            ref_i, ref = i, v
    move = sign * (s.price - ref) / s.atr
    peak = max(sign * ((c.high if sign > 0 else c.low) - ref) / s.atr for c in window[ref_i:])
    retrace = max(0.0, (peak - move) / peak) if peak > 0 else 0.0
    minutes_since = (len(window) - ref_i) * minutes
    return move, float(minutes_since), window[ref_i].time, peak, retrace


def _volume_ratio(s: MarketSnapshot) -> Optional[float]:
    for tf in ("M1", "M5", "M15", "H1"):
        cs = s.candles.get(tf) or []
        if len(cs) >= 12 and any(c.volume for c in cs):
            recent, base = cs[-3:], cs[-39:-3] or cs[:-3]
            b = sum(c.volume for c in base) / len(base) if base else 0.0
            r = sum(c.volume for c in recent) / len(recent)
            return (r / b) if b > 0 else None
    return s.futures_volume_ratio


class FlowAnomalyEngine:
    def assess(self, market: str, s: MarketSnapshot, identified: Sequence[IdentifiedEvent] = (), now: Optional[datetime] = None,
               peers: Optional[dict[str, MarketSnapshot]] = None) -> FlowAssessment:
        now = now or s.time
        pct = s.price_change_pct or 0.0
        sign = 1.0 if pct > 0 else -1.0 if pct < 0 else 0.0
        fa = FlowAssessment(market, 0, sign)
        if sign == 0.0 or not s.atr or not s.price:
            fa.chain = f"FLOW → {market}: sem movimento relevante"
            return fa
        move, minutes, t0, peak, retrace = _move_profile(s, sign)
        fa.move_atr, fa.minutes, fa.start_time = round(move, 2), minutes, t0
        comp: dict[str, int] = {}
        # 1) preço anormal: 0,5 ATR/h é normal; 1,5 ATR em poucas horas é extremo
        comp["preço anormal"] = int(round(24 * max(0.0, min(1.0, (move - 0.4) / 1.1))))
        # 2) velocidade: ATR por minuto (1 ATR em 20 min = extremo)
        speed = (move / minutes) if minutes else None
        comp["velocidade"] = int(round(19 * max(0.0, min(1.0, (speed - 0.005) / 0.045)))) if speed is not None else (int(round(19 * min(1.0, move / 1.5))) if move > 0.6 else 0)
        # 3) volume/ticks
        vr = _volume_ratio(s)
        comp["volume/ticks"] = int(round(17 * max(0.0, min(1.0, (vr - 1.2) / 1.8)))) if vr is not None else 0
        # 4) cross-market: quanto do movimento os líderes NÃO explicam
        weights = CHANNEL_TO_MARKET.get(market, {})
        obs = {"dollar": s.dxy_change_pct, "yields": (s.us10y_change_bp / 10.0) if s.us10y_change_bp is not None else None,
               "risk": s.equity_change_pct if market != "US500" else None, "oil": s.oil_change_pct if market != "WTI" else None}
        thr = {"dollar": 0.08, "yields": 0.15, "risk": 0.15, "oil": 0.4}
        explained_w = against_w = total_w = 0.0
        leaders = {}
        for ch, w in weights.items():
            v = obs.get(ch)
            if v is None or ch == "safe_haven":
                continue
            total_w += abs(w)
            if abs(v) < thr[ch]:
                leaders[ch] = "≈ parado"
                continue
            contrib = (1.0 if v > 0 else -1.0) * w * sign      # > 0 = o canal empurra na direção do movimento
            if contrib > 0:
                explained_w += abs(w)
                leaders[ch] = "explica"
            else:
                against_w += abs(w)
                leaders[ch] = "✗ contra"
        unexplained = 1.0 - (explained_w / total_w) if total_w else 0.5
        pair_v = getattr(s, PAIR_FIELD.get(market, ""), None) if market in PAIR_FIELD else None
        pair_confirms = pair_v is not None and (pair_v > 0) == (sign > 0) and abs(pair_v) >= 0.15
        if market in PAIR_FIELD:
            leaders["par"] = "confirma" if pair_confirms else ("n/d" if pair_v is None else "não confirma")
        comp["cross-market"] = int(round(16 * unexplained))
        fa.leaders = leaders
        # 5) persistência: não revertido
        comp["persistência"] = 10 if retrace <= 0.3 else 5 if retrace <= 0.5 else 0
        # 6) notícia explicativa (penalidade): evento identificado com direção esperada igual ao movimento
        explained_by_news = 0.0
        for ev in identified or []:
            if str(ev.kind).startswith("flow_"):
                continue
            exp, _ = expected_direction(ev, market)
            if exp * sign > 0.15 and ev.age_min(now) <= 240:
                explained_by_news = max(explained_by_news, abs(exp) * ev.importance)
        comp["notícia explicativa"] = -int(round(30 * min(1.0, explained_by_news)))
        fa.components = comp
        fa.score = max(0, min(100, sum(comp.values())))
        # assinatura e origem
        if explained_by_news >= 0.4:
            fa.signature, fa.origin = "A", "A"
        elif against_w > 0 and against_w >= explained_w:
            fa.signature, fa.origin = "C", "E"
        elif explained_w > 0 and unexplained < 0.5:
            fa.signature, fa.origin = "A", "B" if s.events else "C"
        elif pair_confirms or (vr or 0) >= 1.5:
            fa.signature, fa.origin = "B", "D"
        else:
            fa.signature, fa.origin = "—", "E"
        if fa.score < 40:
            fa.status = "SEM ANOMALIA"
        elif fa.origin in ("A", "B", "C") or fa.score < FLOW_THRESHOLD:
            fa.status = "MOVIMENTO EXPLICADO" if fa.origin in ("A", "B", "C") else "MOVIMENTO FORTE (abaixo do limiar)"
        elif fa.signature == "C" and fa.score >= REGIME_THRESHOLD:
            fa.status, fa.anomalous_regime = "REGIME ANÔMALO", True
        else:
            fa.status = "FLUXO ANÔMALO"
        origin_label = {"A": "notícia conhecida", "B": "macro conhecido", "C": "fluxo intermarket", "D": "fluxo institucional PROVÁVEL — origem desconhecida",
                        "E": "movimento anômalo sem explicação — origem desconhecida", "—": "—"}[fa.origin]
        icon = {"FLUXO ANÔMALO": "🟣", "REGIME ANÔMALO": "🔴🟣", "MOVIMENTO EXPLICADO": "⚪", "SEM ANOMALIA": "", "MOVIMENTO FORTE (abaixo do limiar)": "🟡"}[fa.status]
        lines = [f"FLOW → {market}: {icon} {fa.status} · FLOW SCORE {fa.score} · {fa.move_atr:+.2f} ATR" + (f" em {minutes:.0f} min" if minutes else "") +
                 f" · assinatura {fa.signature} · origem {fa.origin} ({origin_label})",
                 "  " + " · ".join(f"{k} {v:+d}" for k, v in comp.items()),
                 "  líderes: " + (" · ".join(f"{k} {v}" for k, v in leaders.items()) or "n/d") + (f" · volume ×{vr:.1f}" if vr else "") + f" · retração {retrace:.0%}"]
        if fa.is_anomalous:
            lines.append(f"  → evento IMPLÍCITO flow_{market}_{'up' if sign > 0 else 'down'} aberto no REACTION ENGINE: procurar quem ainda está atrasado")
        if fa.anomalous_regime:
            lines.append(f"  → ANOMALOUS FLOW REGIME em {market}: modelo normal suspenso; entradas contra o fluxo adiadas")
        fa.chain = "\n".join(lines)
        return fa


def render_propagation(assessments: dict[str, FlowAssessment], clocks: dict[str, str]) -> str:
    """Mapa líder → atrasados a partir do fluxo anômalo mais forte e dos relógios de reação de cada mercado."""
    lead = max((a for a in assessments.values() if a.is_anomalous), key=lambda a: a.score, default=None)
    if lead is None:
        return "PROPAGAÇÃO: nenhum fluxo anômalo ativo"
    lines = [f"🟣 PROPAGAÇÃO — líder {lead.market} {'↑' if lead.direction > 0 else '↓'} FLOW {lead.score} (origem {lead.origin}: não identificada)"]
    for sym, chain in clocks.items():
        if sym == lead.market:
            continue
        lines.append("  " + chain.split("\n")[0].replace("REACTION CLOCK → ", ""))
    return "\n".join(lines)
