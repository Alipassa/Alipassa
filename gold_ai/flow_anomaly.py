"""FLOW ANOMALY ENGINE — informação IMPLÍCITA (MARKET AI 5.0).

O News Engine pergunta "existe uma informação que explica o movimento?". Este motor pergunta o inverso:
"existe um movimento que revela uma informação que ainda não conhecemos?"

FLOW SCORE 0–100 = preço anormal (28) + velocidade (22) + volume/ticks (20) + cross-market não explica (18) + persistência (12)
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
    history_n: int = 0                     # anomalias medidas no histórico para este ativo/origem
    continuation_p: Optional[float] = None # P(continuou aos 60 min) no histórico

    def ledger_record(self, now: datetime, s: MarketSnapshot, regime: str = "") -> dict:
        """Campos do registro de anomalia (5.2): o que se sabia NO MOMENTO da detecção."""
        vr = _volume_ratio(s)
        leader = ", ".join(k for k, v in self.leaders.items() if v in ("explica", "confirma")) or "nenhum"
        return {"event_id": f"flow_{self.market}_{'up' if self.direction > 0 else 'down'}_{now:%Y%m%d%H%M}", "ativo": self.market, "hora": now.isoformat(),
                "flow_score": int(self.score), "atr_move": float(self.move_atr), "minutos": self.minutes, "volume_ratio": vr,
                "persistencia": int(self.components.get("persistência", 0)), "cross_market": int(self.components.get("cross-market", 0)),
                "origem": self.origin, "assinatura": self.signature, "leader": leader, "regime": regime, "direcao": float(self.direction),
                "preco": float(s.price), "atr": float(s.atr)}

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


# --------------------------------------------------------------------------- 5.2: cada anomalia é registrada e MEDIDA (o histórico decide o parâmetro)
HORIZONS_MIN = (5, 15, 30, 60)
CONTINUE_ATR = 0.3          # aos 60 min: ≥ +0,3 ATR além do preço de detecção = CONTINUOU · ≤ −0,3 = REVERTEU · senão INDEFINIDO
FLOW_CONFIRM_ATR = 0.5           # tempo até confirmação: 1º fechamento ≥ +0,5 ATR a favor
MIN_STAT = 5                # mostra a estatística histórica no relógio a partir de 5 casos (informação); edge só com tiers do ciclo de vida


def measure_flow_outcome(direction: float, price0: float, atr: float, candles: Sequence, t0: datetime, horizons: Sequence[int] = HORIZONS_MIN) -> dict:
    """MFE/MAE (em ATR, a favor do fluxo) em cada horizonte após a detecção, resultado aos 60 min e minutos até confirmação.
    candles: M1 (ou M5) com carimbo de ABERTURA; só barras fechadas depois de t0 contam (ponto no tempo)."""
    sign = 1.0 if direction > 0 else -1.0
    out: dict = {}
    confirm = None
    last_close = None
    for h in horizons:
        mfe = mae = 0.0
        for c in candles:
            if c.time <= t0 or c.time > t0 + timedelta(minutes=h):
                continue
            fav = (c.high - price0) * sign if sign > 0 else (price0 - c.low)
            adv = (price0 - c.low) if sign > 0 else (c.high - price0)
            mfe, mae = max(mfe, fav / atr), max(mae, adv / atr)
            if h == max(horizons):
                last_close = (c.close - price0) * sign / atr
                if confirm is None and last_close >= FLOW_CONFIRM_ATR:
                    confirm = (c.time - t0).total_seconds() / 60.0
        out[f"mfe{h}"], out[f"mae{h}"] = round(mfe, 3), round(mae, 3)
    if last_close is None:
        out["resultado"] = "SEM DADOS"
    elif last_close >= CONTINUE_ATR:
        out["resultado"] = "CONTINUOU"
    elif last_close <= -CONTINUE_ATR:
        out["resultado"] = "REVERTEU"
    else:
        out["resultado"] = "INDEFINIDO"
    out["fechamento60"] = None if last_close is None else round(last_close, 3)
    out["confirm_min"] = None if confirm is None else round(confirm, 1)
    return out


def _median(xs: Sequence[float]) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


@dataclass
class FlowGroupStat:
    asset: str
    origin: str
    n: int
    continued: int
    reversed_: int
    undefined: int
    mfe15_med: Optional[float]
    mfe60_med: Optional[float]
    mae60_med: Optional[float]
    confirm_med: Optional[float]

    @property
    def p_continue(self) -> float:
        return self.continued / self.n if self.n else 0.0

    def row(self) -> str:
        f = lambda x, u="": "n/d" if x is None else f"{x:.2f}{u}"  # noqa: E731
        return (f"  {self.asset:<8}{self.origin:<8}{self.n:>4}{self.p_continue:>8.0%}{(self.reversed_ / self.n if self.n else 0):>8.0%}"
                f"{(self.undefined / self.n if self.n else 0):>8.0%}{f(self.mfe15_med, ' ATR'):>11}{f(self.mfe60_med, ' ATR'):>11}{f(self.mae60_med, ' ATR'):>11}"
                f"{('n/d' if self.confirm_med is None else f'{self.confirm_med:.0f} min'):>10}")


def flow_stats(rows: Sequence[dict], min_score: int = FLOW_THRESHOLD) -> list[FlowGroupStat]:
    """rows: registros MEDIDOS do ledger ({ativo, origem, flow_score, mfe15, mfe60, mae60, resultado, confirm_min}). Grupos por ativo × origem + total."""
    rows = [r for r in rows if r.get("resultado") in ("CONTINUOU", "REVERTEU", "INDEFINIDO") and (r.get("flow_score") or 0) >= min_score]
    out: list[FlowGroupStat] = []
    keys = sorted({(r["ativo"], r["origem"]) for r in rows}) + sorted({(r["ativo"], "todas") for r in rows})
    for asset, origin in keys:
        sub = [r for r in rows if r["ativo"] == asset and (origin == "todas" or r["origem"] == origin)]
        if not sub:
            continue
        out.append(FlowGroupStat(asset, origin, len(sub), sum(1 for r in sub if r["resultado"] == "CONTINUOU"), sum(1 for r in sub if r["resultado"] == "REVERTEU"),
                                 sum(1 for r in sub if r["resultado"] == "INDEFINIDO"), _median([r.get("mfe15") for r in sub]), _median([r.get("mfe60") for r in sub]),
                                 _median([r.get("mae60") for r in sub]), _median([r.get("confirm_min") for r in sub])))
    return out


def _bucket_rows(rows: Sequence[dict], key: str) -> list[tuple[str, list[dict]]]:
    """Quebras: faixa de FLOW SCORE (70–79 · 80–89 · 90+), tamanho do movimento (ATR) e sessão (hora UTC da detecção)."""
    def score_b(r):
        v = int(r.get("flow_score") or 0)
        return "FLOW 70–79" if v < 80 else "FLOW 80–89" if v < 90 else "FLOW 90+"

    def move_b(r):
        v = abs(float(r.get("atr_move") or 0.0))
        return "mov < 1,5 ATR" if v < 1.5 else "mov 1,5–2,5 ATR" if v < 2.5 else "mov ≥ 2,5 ATR"

    def session_b(r):
        try:
            h = datetime.fromisoformat(r["hora"]).hour
        except Exception:  # noqa: BLE001
            return "sessão ?"
        return "Ásia 00–07 UTC" if h < 7 else "Londres 07–13 UTC" if h < 13 else "NY 13–21 UTC" if h < 21 else "fecho 21–24 UTC"
    fn = {"score": score_b, "move": move_b, "session": session_b}[key]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(fn(r), []).append(r)
    return sorted(groups.items())


def render_flow_breakdown(rows: Sequence[dict], min_score: int = FLOW_THRESHOLD) -> str:
    rows = [r for r in rows if r.get("resultado") in ("CONTINUOU", "REVERTEU", "INDEFINIDO") and (r.get("flow_score") or 0) >= min_score]
    if len(rows) < 20:
        return ""
    lines = ["  QUEBRAS (todos os ativos): onde a continuação muda?",
             f"  {'grupo':<20}{'n':>5}{'contin.':>8}{'revert.':>8}{'MFE60':>11}{'MAE60':>11}{'MFE−MAE':>9}"]
    for key in ("score", "move", "session"):
        for label, sub in _bucket_rows(rows, key):
            n = len(sub)
            c = sum(1 for r in sub if r["resultado"] == "CONTINUOU") / n
            v = sum(1 for r in sub if r["resultado"] == "REVERTEU") / n
            mfe, mae = _median([r.get("mfe60") for r in sub]) or 0.0, _median([r.get("mae60") for r in sub]) or 0.0
            lines.append(f"  {label:<20}{n:>5}{c:>8.0%}{v:>8.0%}{mfe:>8.2f} ATR{mae:>8.2f} ATR{mfe - mae:>+9.2f}")
        lines.append("")
    lines.append("  leitura: um grupo só é candidato se continuação ≥ 60% E MFE60 − MAE60 > 0 com n ≥ 20; igual ao total = a quebra não separa nada.")
    return "\n".join(lines)


def render_flow_stats(rows: Sequence[dict]) -> str:
    stats = flow_stats(rows)
    lines = [f"🟣 FLOW ANOMALY — o que aconteceu DEPOIS de cada anomalia (FLOW ≥ {FLOW_THRESHOLD}; MFE/MAE em ATR a favor do fluxo; resultado aos 60 min)",
             f"  {'ativo':<8}{'origem':<8}{'n':>4}{'contin.':>8}{'revert.':>8}{'indef.':>8}{'MFE15':>11}{'MFE60':>11}{'MAE60':>11}{'confirm.':>10}"]
    if not stats:
        lines.append("  (nenhuma anomalia medida ainda — o live registra cada FLOW ≥ 70 e mede 60 min depois)")
    lines += [g.row() for g in stats]
    lines.append(f"  leitura: continuação ≥ 60% com n ≥ 20 e MFE60 mediano ≥ {FLOW_CONFIRM_ATR} ATR = candidato a edge (tiers do ciclo de vida); abaixo disso é observação.")
    bd = render_flow_breakdown(rows)
    if bd:
        lines += ["", bd]
    return "\n".join(lines)


def learned_thresholds(ledger: Sequence[dict], min_n: int = 20, min_continuation: float = 0.60) -> dict[str, tuple[int, str]]:
    """LIMIAR APRENDIDO por mercado: o menor degrau (70 / 80 / 90) em que a continuação ≥ 60% E MFE60 − MAE60 > 0 com n ≥ 20.
    Se nenhum degrau prova, o 70 fica como gatilho de INVESTIGAÇÃO (informação para o relógio), não de operação."""
    out: dict[str, tuple[int, str]] = {}
    by: dict[str, list[dict]] = {}
    for r in ledger:
        if r.get("resultado"):
            by.setdefault(str(r.get("ativo", "")), []).append(r)
    for sym, rows in by.items():
        chosen = None
        for low in (70, 80, 90):
            grp = [r for r in rows if int(r.get("flow_score") or 0) >= low]
            n = len(grp)
            if n < min_n:
                continue
            cont = sum(1 for r in grp if str(r.get("resultado")) == "CONTINUOU") / n
            mfe = [float(r.get("mfe60") or 0.0) for r in grp]
            mae = [float(r.get("mae60") or 0.0) for r in grp]
            edge = (sum(mfe) / n) - (sum(mae) / n)
            if cont >= min_continuation and edge > 0:
                chosen = (low, f"aprendido: FLOW ≥ {low} continuou {cont:.0%} em {n} casos (MFE−MAE {edge:+.2f} ATR)")
                break
        out[sym] = chosen or (FLOW_THRESHOLD, f"padrão {FLOW_THRESHOLD}: nenhum degrau provou continuação ≥ {min_continuation:.0%} com n ≥ {min_n} ({len(rows)} medidas) — só investigação")
    return out


class FlowAnomalyEngine:
    def __init__(self, ledger: Optional[Sequence[dict]] = None) -> None:
        self.ledger: list[dict] = list(ledger or [])      # anomalias já MEDIDAS (para a estatística histórica no relógio)
        self.thresholds = learned_thresholds(self.ledger)  # mercado → (limiar, motivo): o histórico calibra o gatilho, não um 70 fixo

    def threshold_for(self, market: str) -> int:
        return int(self.thresholds.get(market, (FLOW_THRESHOLD, ""))[0])

    def history_stat(self, market: str, origin: str) -> Optional[FlowGroupStat]:
        for g in flow_stats(self.ledger):
            if g.asset == market and g.origin == origin and g.n >= MIN_STAT:
                return g
        for g in flow_stats(self.ledger):
            if g.asset == market and g.origin == "todas" and g.n >= MIN_STAT:
                return g
        return None

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
        comp["preço anormal"] = int(round(28 * max(0.0, min(1.0, (move - 0.4) / 1.1))))
        # 2) velocidade: ATR por minuto (1 ATR em 20 min = extremo)
        speed = (move / minutes) if minutes else None
        comp["velocidade"] = int(round(22 * max(0.0, min(1.0, (speed - 0.005) / 0.045)))) if speed is not None else (int(round(22 * min(1.0, move / 1.5))) if move > 0.6 else 0)
        # 3) volume/ticks
        vr = _volume_ratio(s)
        comp["volume/ticks"] = int(round(20 * max(0.0, min(1.0, (vr - 1.2) / 1.8)))) if vr is not None else 0
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
        comp["cross-market"] = int(round(18 * unexplained))
        fa.leaders = leaders
        # 5) persistência: não revertido
        comp["persistência"] = 12 if retrace <= 0.3 else 6 if retrace <= 0.5 else 0
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
            released = [e for e in s.events if e.actual is not None and (now - e.time) <= timedelta(hours=6)]
            fa.signature, fa.origin = "A", "B" if released else "C"
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
            g = self.history_stat(market, fa.origin)
            if g is not None:
                fa.history_n, fa.continuation_p = g.n, g.p_continue
                lines.append(f"  histórico ({market} origem {g.origin}, n={g.n}): continuação {g.p_continue:.0%} · reversão {g.reversed_ / g.n:.0%} · "
                             f"MFE60 mediano {g.mfe60_med if g.mfe60_med is not None else 0:.2f} ATR · confirmação mediana "
                             f"{'n/d' if g.confirm_med is None else f'{g.confirm_med:.0f} min'}")
            else:
                lines.append(f"  histórico: ainda sem {MIN_STAT} anomalias medidas para {market} — esta será registrada e medida em 5/15/30/60 min")
        if fa.anomalous_regime:
            lines.append(f"  → MODO INVESTIGAÇÃO em {market} (WATCH): origem desconhecida não é 'não operar' — entradas CONTRA o fluxo adiadas; "
                         "a favor do fluxo ou no ativo atrasado só com vantagem validada (relógio + histórico)")
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


# --------------------------------------------------------------------------- 5.2: REPLAY no histórico (M1) — o ledger nasce com meses de casos
def replay_flow_anomalies(market: str, m1: Sequence, leaders: Optional[dict] = None, history=None, step_min: int = 5,
                          window_min: int = 60, episode_min: int = 60, log: Optional[callable] = None) -> list[dict]:
    """Percorre o M1 como se fosse ao vivo: em cada passo o detector só vê o passado (candles ≤ t, eventos publicados ≤ t);
    a medição usa os 60 min SEGUINTES e é carimbada como conhecida em t+60. leaders: {"USD": candles M1 do índice do dólar}.
    Devolve registros prontos para o ledger (event_id 'hist_…'), 1 por episódio de `episode_min`."""
    from .leadlag import resample_h1
    from .models import MarketSnapshot
    from .technical import atr as _atr_fn
    m1 = sorted(m1, key=lambda c: c.time)
    if len(m1) < 26 * 60:
        return []
    usd = sorted((leaders or {}).get("USD") or [], key=lambda c: c.time)
    usd_i = 0
    engine = FlowAnomalyEngine()
    out: list[dict] = []
    last_episode: Optional[datetime] = None
    atr_cache: dict = {}
    identify = None
    if history is not None and len(history) > 0:
        from .news_engine import EventIdentifier
        ident = EventIdentifier()

        def identify(t):
            events, news = history.snapshot_inputs(t)
            return events, ident.identify(news, events, t)
    start = 24 * 60
    for i in range(start, len(m1), step_min):
        c = m1[i]
        t = c.time + timedelta(minutes=1)                       # barra conta no fechamento
        hour_key = t.replace(minute=0, second=0, microsecond=0)
        if hour_key not in atr_cache:
            atr_cache[hour_key] = _atr_fn(resample_h1(m1[max(0, i - 48 * 60):i + 1])) or 0.0
        a = atr_cache[hour_key]
        if a <= 0:
            continue
        j = max(0, i - window_min)
        s = MarketSnapshot(time=t, price=c.close)
        s.atr = a
        s.price_change_pct = (c.close / m1[j].close - 1) * 100 if m1[j].close else 0.0
        s.candles = {"M1": m1[max(0, i - 120):i + 1]}
        if usd:
            while usd_i + 1 < len(usd) and usd[usd_i + 1].time <= c.time:
                usd_i += 1
            k = usd_i
            while k > 0 and usd[k].time > c.time - timedelta(minutes=window_min):
                k -= 1
            if usd[usd_i].time <= c.time and usd[k].close:
                s.dxy_change_pct = (usd[usd_i].close / usd[k].close - 1) * 100
        identified = []
        if identify is not None:
            s.events, identified = identify(t)
        fa = engine.assess(market, s, identified, t)
        if not fa.is_anomalous:
            continue
        if last_episode is not None and (t - last_episode) < timedelta(minutes=episode_min):
            continue
        last_episode = t
        rec = fa.ledger_record(t, s, "")
        rec["event_id"] = "hist_" + rec["event_id"]
        after = m1[i + 1:i + 1 + window_min + 5]
        rec.update(measure_flow_outcome(fa.direction, c.close, a, after, t))
        rec["medido_em"] = (t + timedelta(minutes=window_min)).isoformat()
        out.append(rec)
        if log and len(out) % 25 == 0:
            log(f"  {market}: {len(out)} anomalias até {t:%d/%m %H:%M}")
    return out
