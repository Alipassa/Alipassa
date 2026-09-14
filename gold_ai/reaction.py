"""REACTION ENGINE — EVENTO → REAÇÃO → TEMPO → PREVISÃO (MARKET AI 4.0).

Quando uma informação sai, o cronômetro começa. O motor mede, por evento e por ativo:
  TIME_TO_FIRST_REACTION · TIME_TO_CONFIRMATION · TIME_TO_FULL_MOVE · MAX_MOVE · MAX_ADVERSE_MOVE
e, para os canais líderes (USD, YIELDS), quanto tempo demoraram a reagir. Com a estatística por tipo de evento
("depois de CPI, o ouro leva em mediana X min para começar a reagir"), o RELÓGIO DE REAÇÃO detecta a assimetria
temporal: líderes já reagiram, alvo ainda não, tempo decorrido dentro da janela histórica → PRESSÃO LATENTE.

Regra: só aprende com eventos já CONCLUÍDOS antes do instante avaliado (known_at = published_at + horizonte).
Nunca usa a reação do próprio evento para decidir sobre ele. Não toca no Prediction Engine: entrega evidência.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from .models import MarketSnapshot
from .news_engine import IdentifiedEvent, expected_direction

FIRST_ATR = 0.15          # 1ª reação: movimento ≥ 0,15 ATR na direção esperada
CONFIRM_ATR = 0.40        # confirmação: ≥ 0,40 ATR
LEAD_THRESHOLDS = {"USD": 0.08, "YIELD": 1.5}   # DXY em %, US10Y em bp
MIN_N = 3                 # amostra mínima para usar a mediana de um tipo de evento


@dataclass
class ReactionRecord:
    event_id: str
    kind: str
    published_at: datetime
    target: str
    expected_dir: float                       # +1 / −1
    time_to_first: Optional[float] = None     # minutos; None = não reagiu no horizonte
    time_to_confirmation: Optional[float] = None
    time_to_full_move: Optional[float] = None
    max_move_atr: float = 0.0
    max_adverse_atr: float = 0.0
    direction_correct: Optional[bool] = None
    lead_times: dict[str, Optional[float]] = field(default_factory=dict)   # USD / YIELD → minutos
    horizon_min: int = 240
    resolution_min: int = 60                  # resolução da série usada (60 = H1; 5 = M5)

    @property
    def known_at(self) -> datetime:
        return self.published_at + timedelta(minutes=self.horizon_min)


def _at_or_before(series: Sequence[tuple[datetime, float]], t: datetime) -> Optional[float]:
    best = None
    for ts, v in series:
        if ts <= t:
            best = v
        else:
            break
    return best


def measure_reaction(event_id: str, kind: str, published_at: datetime, target: str, expected_dir: float,
                     target_series: Sequence[tuple[datetime, float]], atr: float,
                     leads: Optional[dict[str, Sequence[tuple[datetime, float]]]] = None, lead_dirs: Optional[dict[str, float]] = None,
                     horizon_min: int = 240, resolution_min: int = 60) -> Optional[ReactionRecord]:
    """Cronômetro do evento: a partir de published_at, quando o alvo (em ATR, na direção esperada) e os líderes reagiram."""
    if expected_dir == 0 or not atr or atr <= 0:
        return None
    p0 = _at_or_before(target_series, published_at)
    if p0 is None:
        return None
    end = published_at + timedelta(minutes=horizon_min)
    rec = ReactionRecord(event_id, kind, published_at, target, 1.0 if expected_dir > 0 else -1.0, horizon_min=horizon_min, resolution_min=resolution_min)
    sign = rec.expected_dir
    mfe, mae, t_full = 0.0, 0.0, None
    for ts, p in target_series:
        if ts <= published_at:
            continue
        if ts > end:
            break
        move = sign * (p - p0) / atr
        minutes = (ts - published_at).total_seconds() / 60
        if move > mfe:
            mfe, t_full = move, minutes
        if -move > mae:
            mae = -move
        if rec.time_to_first is None and move >= FIRST_ATR:
            rec.time_to_first = minutes
        if rec.time_to_confirmation is None and move >= CONFIRM_ATR:
            rec.time_to_confirmation = minutes
    rec.max_move_atr, rec.max_adverse_atr = round(mfe, 2), round(mae, 2)
    rec.time_to_full_move = t_full if mfe >= FIRST_ATR else None
    rec.direction_correct = (mfe >= CONFIRM_ATR and mfe > mae) if (mfe or mae) else None
    for name, series in (leads or {}).items():
        d = (lead_dirs or {}).get(name, 0.0)
        thr = LEAD_THRESHOLDS.get(name, 0.0)
        rec.lead_times[name] = None
        if d == 0 or not series:
            continue
        v0 = _at_or_before(series, published_at)
        if v0 is None:
            continue
        for ts, v in series:
            if ts <= published_at:
                continue
            if ts > end:
                break
            delta = (v / v0 - 1) * 100 if name == "USD" else (v - v0) * 100   # DXY em %, US10Y (em %) → bp
            if (1.0 if d > 0 else -1.0) * delta >= thr:
                rec.lead_times[name] = (ts - published_at).total_seconds() / 60
                break
    return rec


@dataclass
class KindStats:
    kind: str
    target: str
    n: int
    median_first: Optional[float]
    median_confirmation: Optional[float]
    median_full: Optional[float]
    p_first: float                 # fração que teve 1ª reação
    p_correct: float               # fração com direção confirmada
    median_mfe: float
    median_mae: float
    median_lead: dict[str, Optional[float]]
    resolution_min: int

    def row(self) -> str:
        f = lambda v: "  n/d" if v is None else f"{v:>5.0f}"  # noqa: E731
        leads = " ".join(f"{k} {f(v).strip()}" for k, v in self.median_lead.items())
        return (f"{self.kind:<22}{self.target:<8}{self.n:>4}{f(self.median_first):>8}{f(self.median_confirmation):>8}{f(self.median_full):>8}"
                f"{self.p_first:>8.0%}{self.p_correct:>8.0%}{self.median_mfe:>7.2f}{self.median_mae:>7.2f}  {leads}")


class ReactionStats:
    """Estatística point-in-time: `at(t)` só usa registros com known_at ≤ t."""

    def __init__(self, records: Sequence[ReactionRecord] = ()) -> None:
        self.records = sorted(records, key=lambda r: r.known_at)
        self._known = [r.known_at for r in self.records]
        self._cache: dict[int, dict[tuple[str, str], KindStats]] = {}

    def add(self, rec: ReactionRecord) -> None:
        self.records.append(rec)
        self.records.sort(key=lambda r: r.known_at)
        self._known = [r.known_at for r in self.records]
        self._cache.clear()

    @staticmethod
    def _aggregate(recs: Sequence[ReactionRecord]) -> dict[tuple[str, str], KindStats]:
        groups: dict[tuple[str, str], list[ReactionRecord]] = {}
        for r in recs:
            groups.setdefault((r.kind, r.target), []).append(r)
        out = {}
        for key, rs in groups.items():
            med = lambda xs: (statistics.median(xs) if xs else None)  # noqa: E731
            firsts = [r.time_to_first for r in rs if r.time_to_first is not None]
            confs = [r.time_to_confirmation for r in rs if r.time_to_confirmation is not None]
            fulls = [r.time_to_full_move for r in rs if r.time_to_full_move is not None]
            leads: dict[str, Optional[float]] = {}
            for name in sorted({k for r in rs for k in r.lead_times}):
                xs = [r.lead_times[name] for r in rs if r.lead_times.get(name) is not None]
                leads[name] = med(xs)
            out[key] = KindStats(key[0], key[1], len(rs), med(firsts), med(confs), med(fulls), len(firsts) / len(rs),
                                 sum(1 for r in rs if r.direction_correct) / len(rs), statistics.median([r.max_move_atr for r in rs]),
                                 statistics.median([r.max_adverse_atr for r in rs]), leads, max(r.resolution_min for r in rs))
        return out

    def at(self, t: datetime) -> dict[tuple[str, str], KindStats]:
        n = bisect_right(self._known, t)
        if n not in self._cache:
            self._cache[n] = self._aggregate(self.records[:n])
        return self._cache[n]

    def get(self, kind: str, target: str, t: datetime) -> Optional[KindStats]:
        return self.at(t).get((kind, target))

    def render(self, t: Optional[datetime] = None, title: str = "⏱️ REACTION ENGINE — tempo de reação por tipo de evento (medianas, minutos)") -> str:
        table = self.at(t) if t else self._aggregate(self.records)
        if not table:
            return f"{title}\n  sem eventos concluídos ainda"
        head = f"{'evento':<22}{'alvo':<8}{'n':>4}{'1ªreaç':>8}{'confirm':>8}{'pleno':>8}{'P(reag)':>8}{'P(dir)':>8}{'MFE':>7}{'MAE':>7}  líderes (min)"
        rows = [v.row() for _, v in sorted(table.items(), key=lambda kv: (-kv[1].n, kv[0]))]
        res = max(v.resolution_min for v in table.values())
        note = f"  resolução {res} min: tempos são múltiplos do candle usado (H1 = horas; para minutos use histórico M5/M1 ou o live)"
        return "\n".join([title, head] + rows + [note, "  MFE/MAE em ATR no horizonte; P(dir) = fração com confirmação ≥ 0,40 ATR na direção esperada"])


@dataclass
class ReactionAssessment:
    market: str
    status: str                          # SEM EVENTO | AGUARDANDO | PRESSÃO LATENTE | REAGIU | DIVERGÊNCIA | EXPIRADO | SEM HISTÓRICO
    pressure: float = 0.0                # −1..+1
    probability: Optional[float] = None  # prob. estimada de o alvo confirmar a direção esperada
    latency_min: Optional[float] = None  # tempo decorrido desde o evento
    expected_min: Optional[float] = None # mediana histórica da 1ª reação do alvo
    full_move_min: Optional[float] = None
    lead: dict[str, str] = field(default_factory=dict)
    target_reaction: str = ""
    event: str = ""
    n_history: int = 0
    chain: str = ""


class ReactionClock:
    def __init__(self, stats: Optional[ReactionStats] = None) -> None:
        self.stats = stats or ReactionStats()

    def assess(self, market: str, s: MarketSnapshot, events: Sequence[IdentifiedEvent], now: datetime) -> ReactionAssessment:
        cands = []
        for ev in events:
            exp, chans = expected_direction(ev, market)
            if exp == 0.0:
                continue
            cands.append((ev, exp, chans))
        if not cands:
            return ReactionAssessment(market, "SEM EVENTO", chain="REACTION: sem evento com direção esperada na janela")
        ev, exp, chans = max(cands, key=lambda c: (c[0].importance * (1 if c[0].surprise_sigma is None else min(2.0, abs(c[0].surprise_sigma))), c[0].time))
        sign = 1.0 if exp > 0 else -1.0
        elapsed = ev.age_min(now)
        ks = self.stats.get(ev.kind, market, now)
        n_hist = ks.n if ks else 0
        expected_min = ks.median_first if ks and ks.n >= MIN_N else None
        full_min = ks.median_full if ks and ks.n >= MIN_N else None
        window = (2.0 * full_min) if full_min else 240.0
        # líderes: canais dollar/yields esperados × observados
        lead: dict[str, str] = {}
        exp_usd, exp_y = chans.get("dollar", 0.0), chans.get("yields", 0.0)
        for name, e, obs, thr in (("USD", exp_usd, s.dxy_change_pct, LEAD_THRESHOLDS["USD"]), ("YIELD", exp_y, s.us10y_change_bp, LEAD_THRESHOLDS["YIELD"])):
            if e == 0.0:
                continue
            if obs is None:
                lead[name] = "n/d"
            elif abs(obs) < thr:
                lead[name] = "ainda não"
            elif (obs > 0) == (e > 0):
                lead[name] = "✓ reagiu"
            else:
                lead[name] = "✗ contra"
        # alvo
        move_atr = ((s.price_change_pct or 0.0) / 100.0 * (s.price or 0.0) / s.atr) * sign if (s.atr and s.price) else 0.0
        if move_atr >= CONFIRM_ATR:
            target = "confirmou"
        elif move_atr >= FIRST_ATR:
            target = "iniciou"
        elif move_atr <= -FIRST_ATR:
            target = "contra"
        else:
            target = "ainda não"
        leads_ok = sum(1 for v in lead.values() if v == "✓ reagiu")
        leads_against = sum(1 for v in lead.values() if v == "✗ contra")
        # estado
        if target == "confirmou":
            status, strength = "REAGIU", 0.3
        elif target == "contra" or (leads_against and not leads_ok):
            status, strength = "DIVERGÊNCIA", 0.0
        elif leads_ok and elapsed <= window:
            status, strength = "PRESSÃO LATENTE", 1.0 if target == "ainda não" else 0.8
        elif leads_ok:
            status, strength = "EXPIRADO", 0.0
        elif elapsed <= window:
            status, strength = "AGUARDANDO", 0.4
        else:
            status, strength = "EXPIRADO", 0.0
        if not ks or ks.n < MIN_N:
            hist_note = f"sem histórico suficiente para {ev.kind}→{market} (n={n_hist}; mínimo {MIN_N}) — janela padrão 240 min"
        else:
            hist_note = f"histórico {ev.kind}→{market}: 1ª reação mediana {ks.median_first:.0f} min, pleno {ks.median_full:.0f} min, P(dir) {ks.p_correct:.0%} (n={ks.n})" \
                if ks.median_first is not None and ks.median_full is not None else f"histórico {ev.kind}→{market}: n={ks.n}, P(reação) {ks.p_first:.0%}, P(dir) {ks.p_correct:.0%}"
        # probabilidade estimada: base histórica ajustada pela evidência atual (nunca inventada: sem histórico, 0,5 ± evidência)
        base = ks.p_correct if ks and ks.n >= MIN_N else 0.5
        prob = base + 0.08 * leads_ok - 0.15 * leads_against + (0.05 if status == "PRESSÃO LATENTE" else 0.0) - (0.25 if target == "contra" else 0.0)
        if status == "EXPIRADO":
            prob = min(prob, 0.45)
        prob = max(0.05, min(0.95, prob))
        pressure = round(sign * strength * (0.6 + 0.4 * (prob if prob else 0.5)), 3) if strength else 0.0
        icon = {"PRESSÃO LATENTE": "🔥", "AGUARDANDO": "⏳", "REAGIU": "✅", "DIVERGÊNCIA": "⚠️", "EXPIRADO": "⌛"}.get(status, "")
        sp = f" surpresa {ev.surprise_sigma:+.1f}σ" if ev.surprise_sigma is not None else ""
        lines = [f"REACTION CLOCK → {market}: {icon} {status} · T+{elapsed:.0f} min · {ev.name[:50]}{sp} → {market} {'↑' if sign > 0 else '↓'}",
                 "  líderes: " + (" · ".join(f"{k} {v}" for k, v in lead.items()) or "sem canal líder") + f" · alvo {market}: {target} ({move_atr:+.2f} ATR)",
                 f"  {hist_note}"]
        if status == "PRESSÃO LATENTE":
            lines.append(f"  assimetria temporal: líderes reagiram, {market} não; decorrido {elapsed:.0f} min" +
                         (f" vs mediana {expected_min:.0f} min" if expected_min else "") + f" · prob. estimada {prob:.0%}")
        return ReactionAssessment(market, status, pressure, round(prob, 2), round(elapsed, 1), expected_min, full_min, lead, target, ev.name, n_hist, "\n".join(lines))


# --------------------------------------------------------------------------- aprendizado a partir do banco histórico
def records_from_history(hist, symbol: str, target_series: Sequence[tuple[datetime, float]], atr_at, leads: Optional[dict] = None,
                         horizon_min: int = 240, resolution_min: int = 60) -> list[ReactionRecord]:
    """Um ReactionRecord por evento do banco com direção esperada para `symbol` (regra macro). `atr_at(t)` → ATR do alvo em t."""
    from .history import rule_direction
    from .news_engine import TRANSMISSION
    from .history import EXTRA_TRANSMISSION

    out = []
    for e in hist.events:
        if e.revised is not None:
            continue
        sign, sigma = 0.0, None
        if e.surprise is not None:
            sign = 1.0 if e.surprise > 0 else -1.0 if e.surprise < 0 else 0.0
            from .history import TYPICAL
            typ = TYPICAL.get(e.kind, max(abs(e.forecast or e.previous or 1.0) * 0.1, 0.1))
            sigma = e.surprise / typ if typ else None
        elif e.kind in TRANSMISSION and e.actual is None:
            sign = 1.0
        if sign == 0.0:
            continue
        exp = rule_direction(e.kind, sign, sigma, symbol)
        if exp == 0.0:
            continue
        chans = TRANSMISSION.get(e.kind) or EXTRA_TRANSMISSION.get(e.kind) or {}
        lead_dirs = {"USD": sign * chans.get("dollar", 0.0), "YIELD": sign * chans.get("yields", 0.0)}
        atr = atr_at(e.published_at)
        if not atr:
            continue
        rec = measure_reaction(e.event_id, e.kind, e.published_at, symbol, exp, target_series, atr, leads, lead_dirs, horizon_min, resolution_min)
        if rec:
            out.append(rec)
    return out
