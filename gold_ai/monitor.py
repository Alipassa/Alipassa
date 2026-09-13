"""GOLD AI ENGINE 2.3 — GOLD TRADE MONITOR + ADAPTIVE EXIT ENGINE.

Regra central: "A abertura de uma operação não encerra o processo de análise. Enquanto existir
posição aberta, o GOLD AI ENGINE deverá continuar recebendo dados de mercado, notícias,
macroeconomia, fluxo e indicadores técnicos, comparar o cenário atual com a tese original e
decidir continuamente entre MANTER, PROTEGER, REDUZIR ou ENCERRAR a posição."

                 OPERAÇÃO ABERTA → GOLD TRADE MONITOR → NOVO SCORE → COMPARAR COM TESE ORIGINAL
                 → MANTER (trailing) · PROTEGER (parcial) · REDUZIR · ESTENDER · ENCERRAR (invalidação)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from .config import DEFAULT_WEIGHTS
from .models import Assessment, Candle, Direction, MarketSnapshot, Stage
from .trading import RStats, TradePlan


# --------------------------------------------------------------------------- tese e estado
@dataclass
class Thesis:
    """Fotografia da tese na entrada."""

    direction: Direction
    score: float                      # score assinado na direção da operação (-100..+100)
    pillars: dict[str, float]         # fatores alinhados (ratio ≥ 0.3) → ratio na entrada
    evidence_level: int
    confidence: float
    time: datetime

    @classmethod
    def from_assessment(cls, a: Assessment, direction: Direction) -> "Thesis":
        sign = 1.0 if direction == Direction.ALTA else -1.0
        pillars = {f.name: round(f.ratio, 3) for f in a.factors if f.available and sign * f.ratio >= 0.3}
        return cls(direction, sign * a.score, pillars, int(a.evidence_level), a.confidence, a.time)

    def to_dict(self) -> dict:
        return {"direction": self.direction.value, "score": self.score, "pillars": self.pillars, "evidence_level": self.evidence_level,
                "confidence": self.confidence, "time": self.time.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "Thesis":
        return cls(Direction(d["direction"]), d["score"], dict(d["pillars"]), d["evidence_level"], d["confidence"], datetime.fromisoformat(d["time"]))


@dataclass
class MonitorReading:
    time: datetime
    price: float
    current_r: float
    trade_score: float        # estado atual do mercado, assinado na direção da operação (-100..+100)
    thesis_score: float       # 0..100 — quanto da tese original permanece válida
    exit_score: float         # 0..100 — necessidade de encerrar
    profit_potential: float   # 0..100 — espaço estatisticamente favorável restante
    action: str               # MANTER | PROTEGER | REDUZIR | ESTENDER | ENCERRAR | STOP
    note: str = ""
    targets: dict[str, str] = field(default_factory=dict)  # "3R" → atingível/provável/possível/improvável


@dataclass
class ManagedTrade:
    trade_id: int
    plan: TradePlan
    thesis: Thesis
    remaining: float = 1.0
    realized_r: float = 0.0
    stop_r: float = -1.0
    peak_r: float = 0.0
    protected: bool = False
    extending: bool = False
    trail_r: float = 1.0
    status: str = "OPEN"          # OPEN | CLOSED
    close_reason: str = ""
    result_r: Optional[float] = None
    closed_at: Optional[datetime] = None
    history: list[MonitorReading] = field(default_factory=list)

    @property
    def sign(self) -> float:
        return self.plan.sign

    def r_at(self, price: float) -> float:
        return self.sign * (price - self.plan.entry) / (self.plan.r_value or 1e-9)

    def price_at_r(self, r: float) -> float:
        return self.plan.price_at_r(r)

    def state_dict(self) -> dict:
        return {"remaining": self.remaining, "realized_r": self.realized_r, "stop_r": self.stop_r, "peak_r": self.peak_r,
                "protected": self.protected, "extending": self.extending, "trail_r": self.trail_r}

    def load_state(self, d: dict) -> "ManagedTrade":
        for k, v in d.items():
            if hasattr(self, k):
                setattr(self, k, v)
        return self

    def close(self, r_exit: float, reason: str, t: datetime) -> float:
        self.result_r = round(self.realized_r + self.remaining * r_exit, 3)
        self.remaining, self.status, self.close_reason, self.closed_at = 0.0, "CLOSED", reason, t
        return self.result_r


# --------------------------------------------------------------------------- monitor
@dataclass
class MonitorConfig:
    exit_score_close: float = 70.0      # ≥ → ENCERRAR
    thesis_invalidated: float = 30.0    # THESIS SCORE < → ENCERRAR (mesmo com lucro)
    exit_score_reduce: float = 50.0     # ≥ e em lucro → REDUZIR (parcial + zero a zero)
    exit_score_protect: float = 35.0    # ≥ e ≥ 1R → stop no zero a zero
    protect_r: float = 2.0              # PROTEGER: parcial 50 % + trailing
    partial_fraction: float = 0.5
    trail_r: float = 1.0
    extend_trail_r: float = 1.5
    extend_min_potential: float = 60.0
    extend_score_gain: float = 5.0      # trade score ≥ tese + isto → ESTENDER


class TradeMonitor:
    def __init__(self, cfg: Optional[MonitorConfig] = None, history: Optional[RStats] = None) -> None:
        self.cfg = cfg or MonitorConfig()
        self.history = history

    # ------------------------------------------------------------------ 1. caminho do preço (stop/trailing)
    def check_path(self, tr: ManagedTrade, candles: Sequence[Candle]) -> Optional[MonitorReading]:
        """Verifica, candle a candle desde a última leitura, se o stop atual foi tocado (conservador)."""
        last = tr.history[-1].time if tr.history else tr.plan.time
        for c in candles:
            if c.time <= last:
                continue
            fav = tr.r_at(c.high) if tr.sign > 0 else tr.r_at(c.low)
            adv = tr.r_at(c.low) if tr.sign > 0 else tr.r_at(c.high)
            if adv <= tr.stop_r:
                reason = "STOP" if tr.stop_r <= -1.0 + 1e-9 else "TRAILING/PROTEÇÃO"
                res = tr.close(tr.stop_r, reason, c.time)
                reading = MonitorReading(c.time, tr.price_at_r(tr.stop_r), tr.stop_r, 0.0, 0.0, 100.0, 0.0, "STOP", f"{reason} tocado → resultado {res:+.2f}R")
                tr.history.append(reading)
                return reading
            tr.peak_r = max(tr.peak_r, fav)
            if tr.peak_r >= 1.0:  # trailing sempre ativo após 1R
                tr.stop_r = max(tr.stop_r, tr.peak_r - tr.trail_r)
        return None

    # ------------------------------------------------------------------ 2. scores
    def thesis_score(self, tr: ManagedTrade, a: Assessment) -> float:
        sign = tr.sign
        now = {f.name: f.ratio for f in a.factors if f.available}
        if not tr.thesis.pillars:
            base = 50.0
        else:
            tot = sum(DEFAULT_WEIGHTS.get(n, 5) for n in tr.thesis.pillars)
            kept = sum(DEFAULT_WEIGHTS.get(n, 5) for n in tr.thesis.pillars if sign * now.get(n, 0.0) >= 0.15)
            base = 100.0 * kept / tot if tot else 50.0
        if sign * a.score < 0:
            base -= 25.0
        return round(max(0.0, min(100.0, base)), 1)

    def trade_score(self, tr: ManagedTrade, a: Assessment) -> float:
        return round(tr.sign * a.score, 1)

    def exit_score(self, tr: ManagedTrade, a: Assessment, s: MarketSnapshot, thesis: float, trade: float, current_r: float) -> float:
        e = (100.0 - thesis) * 0.35
        e += max(0.0, -trade) * 0.30
        drop = tr.thesis.score - trade
        e += max(0.0, drop) / 100.0 * 25.0
        if a.reversal.current_trend == tr.thesis.direction:
            e += a.reversal.risk * 0.20
        if a.systemic_risk >= 75:
            e += 10.0
        if a.next_event is not None and current_r > 0:
            e += 10.0
        if a.premove.stage == Stage.PRE_MOVIMENTO and a.premove.direction != tr.thesis.direction and a.premove.direction != Direction.LATERAL:
            e += 15.0
        return round(max(0.0, min(100.0, e)), 1)

    def _cond_prob(self, current_r: float, target_r: float) -> Optional[float]:
        """P(atingir target | já atingiu floor(current)) a partir do histórico."""
        h = self.history
        if not h or h.n < 20:
            return None
        base_k = max(0, min(4, int(current_r)))
        p_base = 1.0 if base_k == 0 else h.reach.get(f"{base_k}R", 0.0)
        k = int(min(4, max(1, round(target_r))))
        p_t = h.reach.get(f"{k}R", 0.0)
        if p_base <= 0:
            return 0.0
        return max(0.0, min(1.0, p_t / p_base))

    def profit_potential(self, tr: ManagedTrade, a: Assessment, s: MarketSnapshot, trade: float, thesis: float, current_r: float) -> float:
        R = tr.plan.r_value or 1e-9
        lvl = a.zone.get("resistance") if tr.sign > 0 else a.zone.get("support")
        if lvl is not None and tr.sign * (lvl - a.price) > 0:
            room_r = tr.sign * (lvl - a.price) / R
        else:
            room_r = (s.atr or R) * 2.0 / R
        pot = min(1.0, room_r / 2.0) * 40.0
        pot += max(0.0, trade) / 100.0 * 30.0
        p_next = self._cond_prob(current_r, int(current_r) + 1)
        pot += (p_next if p_next is not None else 0.5 * thesis / 100.0) * 30.0
        return round(max(0.0, min(100.0, pot)), 1)

    def target_labels(self, current_r: float, potential: float) -> dict[str, str]:
        out: dict[str, str] = {}
        base = int(current_r) + 1
        for k in range(base, base + 3):
            p = self._cond_prob(current_r, k)
            if p is None:
                p = potential / 100.0 * (0.9 ** (k - base))
            out[f"{k}R"] = "atingível" if p >= 0.6 else "provável" if p >= 0.4 else "possível" if p >= 0.2 else "improvável"
        return out

    # ------------------------------------------------------------------ 3. decisão
    def evaluate(self, tr: ManagedTrade, a: Assessment, s: MarketSnapshot) -> MonitorReading:
        c = self.cfg
        price = a.price
        cur = tr.r_at(price)
        tr.peak_r = max(tr.peak_r, cur)
        thesis = self.thesis_score(tr, a)
        trade = self.trade_score(tr, a)
        ex = self.exit_score(tr, a, s, thesis, trade, cur)
        pot = self.profit_potential(tr, a, s, trade, thesis, cur)
        note, action = "", "MANTER"

        if ex >= c.exit_score_close or thesis < c.thesis_invalidated:
            action = "ENCERRAR"
            res = tr.close(cur, "TESE INVALIDADA" if thesis < c.thesis_invalidated else "EXIT SCORE", a.time)
            note = f"🔴 TESE INVALIDADA (thesis {thesis:.0f}, exit {ex:.0f}) → fechada a {cur:+.2f}R, resultado {res:+.2f}R"
        elif ex >= c.exit_score_reduce and cur >= 0.5 and tr.remaining >= 1.0 - 1e-9:
            action = "REDUZIR"
            tr.realized_r += tr.remaining * c.partial_fraction * cur
            tr.remaining *= 1.0 - c.partial_fraction
            tr.stop_r = max(tr.stop_r, 0.0)
            note = f"cenário deteriorando (exit {ex:.0f}) → {c.partial_fraction:.0%} realizado a {cur:+.2f}R, stop no zero a zero"
        elif ex >= c.exit_score_reduce and tr.remaining < 1.0:
            action = "REDUZIR"
            tr.stop_r = max(tr.stop_r, tr.peak_r - 0.5)
            note = f"exit {ex:.0f} com posição já reduzida → trailing apertado ({tr.stop_r:+.2f}R)"
        elif cur >= c.protect_r and not tr.protected:
            action = "PROTEGER"
            tr.realized_r += tr.remaining * c.partial_fraction * cur
            tr.remaining *= 1.0 - c.partial_fraction
            tr.stop_r = max(tr.stop_r, 0.0)
            tr.protected = True
            note = f"+{cur:.1f}R → {c.partial_fraction:.0%} protegido, {1 - c.partial_fraction:.0%} em trailing"
        elif cur >= 1.0 and tr.stop_r < 0 and ex >= c.exit_score_protect:
            action = "PROTEGER"
            tr.stop_r = 0.0
            note = f"exit {ex:.0f} com lucro → stop no zero a zero"
        elif tr.protected and trade >= tr.thesis.score + c.extend_score_gain and pot >= c.extend_min_potential:
            action = "ESTENDER"
            tr.extending, tr.trail_r = True, c.extend_trail_r
            note = f"cenário mais forte que a tese ({trade:+.0f} vs {tr.thesis.score:+.0f}) e potencial {pot:.0f} → buscar {int(cur) + 2}R/{int(cur) + 3}R com trailing {c.extend_trail_r:.1f}R"
        else:
            if tr.peak_r >= 1.0:
                tr.stop_r = max(tr.stop_r, tr.peak_r - tr.trail_r)
            note = "tese preservada" if thesis >= 60 else "tese parcialmente preservada — observar"
        reading = MonitorReading(a.time, price, round(cur, 3), trade, thesis, ex, pot, action, note, self.target_labels(cur, pot))
        tr.history.append(reading)
        return reading


# --------------------------------------------------------------------------- relatório
def render_monitor(tr: ManagedTrade, r: MonitorReading) -> str:
    side = "BUY" if tr.thesis.direction == Direction.ALTA else "SELL"
    pnl = tr.realized_r + tr.remaining * r.current_r if tr.status == "OPEN" else (tr.result_r or 0.0)
    status = {"MANTER": "🟢 MANTER", "PROTEGER": "🟡 PROTEGER", "REDUZIR": "🟠 REDUZIR", "ESTENDER": "🟢 ESTENDER", "ENCERRAR": "🔴 ENCERRAR", "STOP": "⛔ STOP"}[r.action]
    lines = ["GOLD TRADE MONITOR", f"Trade: #{tr.trade_id:05d}", f"{side} XAU/USD", f"Entrada {tr.plan.entry:.2f} · Stop atual {tr.price_at_r(tr.stop_r):.2f} ({tr.stop_r:+.2f}R)", "",
             f"Lucro: {pnl:+.2f}R", "", f"TRADE SCORE:       {r.trade_score:+.0f}", f"THESIS SCORE:      {r.thesis_score:.0f}/100",
             f"EXIT SCORE:        {r.exit_score:.0f}/100", f"PROFIT POTENTIAL:  {r.profit_potential:.0f}/100", "", "Status:", status]
    if r.targets and tr.status == "OPEN":
        lines += ["", "Alvo:"] + [f"{k} → {v}" for k, v in r.targets.items()]
    lines += ["", "Ação:", f"{tr.remaining:.0%} em posição" + (f" · {1 - tr.remaining:.0%} realizado ({tr.realized_r:+.2f}R)" if tr.remaining < 1 else "")]
    if r.note:
        lines.append(r.note)
    return "\n".join(lines)


def render_evolution(tr: ManagedTrade) -> str:
    lines = [f"Trade #{tr.trade_id:05d} — evolução do score"]
    t0 = tr.plan.time
    for h in tr.history:
        mins = (h.time - t0).total_seconds() / 60
        lines.append(f"  +{mins:.0f} min  score {h.trade_score:+.0f}  tese {h.thesis_score:.0f}  exit {h.exit_score:.0f}  {h.current_r:+.2f}R  {h.action}")
    if tr.status == "CLOSED":
        lines.append(f"  SAÍDA: {tr.close_reason} → {tr.result_r:+.2f}R")
    return "\n".join(lines)


# --------------------------------------------------------------------------- aprendizado empírico
def exit_learning(rows: Sequence[dict]) -> str:
    """rows: {"exit_reason", "result_r", "max_r_after" (até onde o preço foi depois, se conhecido),
    "thesis_at_exit", "drop_at_exit"}. Responde: qual deterioração do score realmente indica sair?"""
    if not rows:
        return "🧠 APRENDIZADO DE SAÍDA — sem operações gerenciadas encerradas"
    lines = ["🧠 APRENDIZADO DE SAÍDA — deterioração do score × resultado"]
    buckets = [("queda < 20", lambda d: d < 20), ("queda 20–40", lambda d: 20 <= d < 40), ("queda 40–60", lambda d: 40 <= d < 60), ("queda ≥ 60", lambda d: d >= 60)]
    for name, fn in buckets:
        sub = [r for r in rows if r.get("drop_at_exit") is not None and fn(r["drop_at_exit"])]
        if not sub:
            continue
        avg = sum(r["result_r"] for r in sub) / len(sub)
        after = [r["max_r_after"] - r["result_r"] for r in sub if r.get("max_r_after") is not None]
        left = f", deixado na mesa {sum(after) / len(after):+.2f}R" if after else ""
        lines.append(f"  {name:<12} n={len(sub):<3} resultado médio {avg:+.2f}R{left}")
    early = [r for r in rows if r.get("exit_reason") in ("TESE INVALIDADA", "EXIT SCORE")]
    if early:
        avg = sum(r["result_r"] for r in early) / len(early)
        saved = [r["result_r"] - r["min_r_after"] for r in early if r.get("min_r_after") is not None]
        lines.append(f"  saídas antecipadas: n={len(early)} resultado médio {avg:+.2f}R" + (f", evitado {sum(saved) / len(saved):+.2f}R de queda posterior" if saved else ""))
    return "\n".join(lines)
