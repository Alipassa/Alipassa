"""BANCO HISTÓRICO DE EVENTOS/NOTÍCIAS — point-in-time (MARKET AI 4.0).

O MARKET AI só pode ver, em cada instante do backtest, aquilo que estava publicado naquele momento:
  • `timestamp`     = quando o evento ocorreu/saiu (ex.: CPI 12:30 UTC);
  • `published_at`  = quando a informação ficou disponível (revisões entram como linhas novas, publicadas depois).
Nada com published_at > t é visível em t. É isso que evita look-ahead por revisão.

Esquema do CSV (dados/noticias_historicas.csv):
timestamp,published_at,event_id,event,country,currency,impact,forecast,previous,actual,revised,surprise,category,kind,source,headline,sentiment,
xau_effect,us500_effect,eurusd_effect,usdjpy_effect,wti_effect,effect_source
Os efeitos por ativo nascem de regras macro (news_engine) e são substituídos pelo que o histórico mostrar (`history learn`).
"""

from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Sequence

from .models import EconomicEvent, NewsItem

EFFECT_MARKETS = ("XAUUSD", "US500", "EURUSD", "USDJPY", "WTI")
EFFECT_FIELD = {"XAUUSD": "xau_effect", "US500": "us500_effect", "EURUSD": "eurusd_effect", "USDJPY": "usdjpy_effect", "WTI": "wti_effect"}
IMPACT_MAP = {"HIGH": "MUITO ALTO", "MEDIUM": "MÉDIO", "LOW": "BAIXO", "3": "MUITO ALTO", "2": "MÉDIO", "1": "BAIXO",
              "MUITO ALTO": "MUITO ALTO", "ALTO": "ALTO", "MÉDIO": "MÉDIO", "BAIXO": "BAIXO"}

# nome do evento (normalizado) → kind do motor
KIND_PATTERNS: tuple[tuple[str, str], ...] = (
    ("core cpi", "core_cpi"), ("core inflation", "core_cpi"), ("cpi", "cpi"), ("inflation rate", "cpi"), ("core pce", "core_pce"), ("pce", "pce"),
    ("personal consumption", "pce"), ("ppi", "ppi"), ("producer price", "ppi"), ("interest rate decision", "fomc"), ("non farm", "nfp"), ("nonfarm", "nfp"),
    ("payroll", "nfp"), ("unemployment rate", "unemployment"), ("jobless claims", "jobless_claims"), ("initial claims", "jobless_claims"),
    ("gdp", "gdp"), ("ism manufacturing", "ism"), ("ism services", "ism"), ("ism non-manufacturing", "ism"), ("pmi", "pmi"), ("retail sales", "retail_sales"),
    ("jolts", "jolts"), ("consumer confidence", "consumer_confidence"), ("michigan", "michigan"), ("housing", "housing"), ("building permits", "housing"),
    ("average hourly earnings", "earnings"), ("fomc", "fomc"), ("fed interest rate", "fomc"), ("federal funds", "fomc"), ("fed chair", "speech"),
    ("powell", "speech"), ("ecb", "ecb"), ("boj", "boj"), ("bank of japan", "boj"), ("crude oil inventories", "oil_inventories"), ("eia", "oil_inventories"),
    ("opec", "oil_supply_cut"), ("china", "china"),
)
# eventos sem sensibilidade direta na tabela do motor recebem uma transmissão própria aqui (unidade: acima do consenso)
EXTRA_TRANSMISSION = {
    "ppi": {"yields": +0.7, "dollar": +0.5, "risk": -0.4},
    "oil_inventories": {"oil": -0.8},           # estoques ACIMA do esperado → petróleo cai
    "ecb": {"dollar": -0.5, "yields": +0.2},    # BCE hawkish (acima) → euro sobe → dólar cai
    "boj": {"dollar": -0.3, "yields": +0.2},
    "china": {"risk": +0.4, "oil": +0.3},
}
TYPICAL = {"cpi": 0.1, "core_cpi": 0.1, "pce": 0.1, "core_pce": 0.1, "ppi": 0.2, "nfp": 60.0, "unemployment": 0.1, "jobless_claims": 15.0, "gdp": 0.5,
           "ism": 1.5, "pmi": 1.0, "retail_sales": 0.4, "jolts": 300.0, "consumer_confidence": 3.0, "michigan": 2.0, "housing": 5.0, "earnings": 0.1,
           "oil_inventories": 2.0, "fomc": 0.25, "ecb": 0.25, "boj": 0.1}


def kind_from_name(name: str) -> str:
    n = (name or "").lower()
    for pat, kind in KIND_PATTERNS:
        if pat in n:
            return kind
    return "generic"


def _num(x) -> Optional[float]:
    if x is None or str(x).strip() in ("", "n/d", "nan", "None"):
        return None
    s = str(x).strip().replace("%", "").replace("K", "").replace("k", "").replace("M", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class HistoricalEvent:
    timestamp: datetime
    published_at: datetime
    event_id: str
    event: str
    country: str = "US"
    currency: str = "USD"
    impact: str = "ALTO"
    forecast: Optional[float] = None
    previous: Optional[float] = None
    actual: Optional[float] = None
    revised: Optional[float] = None
    surprise: Optional[float] = None
    category: str = "MACRO"          # MACRO | CENTRAL_BANK | NEWS | GEOPOLITICAL | ENERGY | CHINA
    kind: str = "generic"
    source: str = ""
    headline: str = ""
    sentiment: str = ""              # POSITIVE | NEGATIVE | NEUTRAL (tom da mídia, GDELT)
    xau_effect: Optional[float] = None
    us500_effect: Optional[float] = None
    eurusd_effect: Optional[float] = None
    usdjpy_effect: Optional[float] = None
    wti_effect: Optional[float] = None
    effect_source: str = "rule"      # rule | empirical
    surprise_basis: str = "forecast" # forecast (vs consenso) | previous (sem consenso: vs dado anterior)
    tone: Optional[float] = None     # GDELT tone (−100..+100)
    volume: Optional[float] = None   # GDELT volume (% da cobertura)

    def __post_init__(self) -> None:
        if self.surprise is None and self.actual is not None:
            if self.forecast is not None:
                self.surprise = round(self.actual - self.forecast, 4)
            elif self.previous is not None and self.surprise_basis == "previous":
                self.surprise = round(self.actual - self.previous, 4)
        if self.forecast is None and self.previous is not None and self.surprise is not None and self.surprise_basis == "forecast":
            self.surprise_basis = "previous"
        if self.kind == "generic":
            self.kind = kind_from_name(self.event)

    def effect(self, market: str) -> Optional[float]:
        return getattr(self, EFFECT_FIELD.get(market.upper(), "_"), None)

    def to_economic_event(self) -> EconomicEvent:
        consensus = self.forecast if self.forecast is not None else (self.previous if self.surprise_basis == "previous" else None)
        return EconomicEvent(self.event, self.timestamp, IMPACT_MAP.get(str(self.impact).upper(), self.impact or "ALTO"), consensus, self.previous,
                             self.actual, self.kind, "")

    def to_news_item(self) -> NewsItem:
        cat = {"GEOPOLITICAL": "geopolitical", "CENTRAL_BANK": "fed", "ENERGY": "generic", "CHINA": "china", "NEWS": "generic"}.get(self.category, "macro")
        impact = 0.0
        if self.xau_effect is not None:
            impact = max(-1.0, min(1.0, self.xau_effect))
        elif self.sentiment == "POSITIVE":
            impact = 0.3
        elif self.sentiment == "NEGATIVE":
            impact = -0.3
        return NewsItem(self.headline or self.event, self.source, self.timestamp, cat, impact, 0.0, f"{self.kind} · {self.category}")

    @classmethod
    def columns(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    def to_row(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            d[f.name] = v.isoformat() if isinstance(v, datetime) else ("" if v is None else v)
        return d

    @classmethod
    def from_row(cls, r: dict) -> "HistoricalEvent":
        def dt(x):
            t = datetime.fromisoformat(str(x).replace("Z", "+00:00").replace(" ", "T"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        ts = dt(r["timestamp"])
        pub = dt(r["published_at"]) if r.get("published_at") else ts
        num = lambda k: _num(r.get(k))  # noqa: E731
        return cls(ts, pub, r.get("event_id") or f"{r.get('event', 'EV')}_{ts:%Y%m%d%H%M}", r.get("event", ""), r.get("country", "US"), r.get("currency", "USD"),
                   r.get("impact", "ALTO"), num("forecast"), num("previous"), num("actual"), num("revised"), num("surprise"), r.get("category", "MACRO"),
                   r.get("kind") or "generic", r.get("source", ""), r.get("headline", ""), r.get("sentiment", ""), num("xau_effect"), num("us500_effect"),
                   num("eurusd_effect"), num("usdjpy_effect"), num("wti_effect"), r.get("effect_source") or "rule", r.get("surprise_basis") or "forecast", num("tone"), num("volume"))


class EventHistory:
    def __init__(self, events: Sequence[HistoricalEvent] = ()) -> None:
        self.events: list[HistoricalEvent] = sorted(events, key=lambda e: e.published_at)

    def __len__(self) -> int:
        return len(self.events)

    def add(self, ev: HistoricalEvent) -> None:
        if any(e.event_id == ev.event_id and e.published_at == ev.published_at for e in self.events):
            return
        self.events.append(ev)
        self.events.sort(key=lambda e: e.published_at)

    def available_at(self, t: datetime, lookback_hours: float = 24.0) -> list[HistoricalEvent]:
        """POINT-IN-TIME: só o que estava publicado em t (published_at ≤ t) e ocorreu nas últimas `lookback_hours`."""
        lo = t - timedelta(hours=lookback_hours)
        out = [e for e in self.events if e.published_at <= t and lo <= e.timestamp <= t]
        # revisão: para o mesmo event_id fica a linha publicada mais recentemente até t
        latest: dict[str, HistoricalEvent] = {}
        for e in out:
            latest[e.event_id] = e
        return sorted(latest.values(), key=lambda e: e.timestamp)

    def upcoming_at(self, t: datetime, ahead_hours: float = 48.0) -> list[HistoricalEvent]:
        """Eventos futuros já agendados (consenso conhecido, sem actual) — calendário de risco."""
        hi = t + timedelta(hours=ahead_hours)
        return [HistoricalEvent(e.timestamp, e.published_at, e.event_id, e.event, e.country, e.currency, e.impact, e.forecast, e.previous, None, None, None,
                                e.category, e.kind, e.source) for e in self.events if t < e.timestamp <= hi]

    def snapshot_inputs(self, t: datetime, lookback_hours: float = 24.0) -> tuple[list[EconomicEvent], list[NewsItem]]:
        avail = self.available_at(t, lookback_hours)
        events = [e.to_economic_event() for e in avail if e.actual is not None or e.category in ("MACRO", "CENTRAL_BANK")]
        events += [e.to_economic_event() for e in self.upcoming_at(t)]
        news = [e.to_news_item() for e in avail if e.headline or e.category in ("NEWS", "GEOPOLITICAL", "ENERGY", "CHINA")]
        return events, news

    def stats(self) -> str:
        if not self.events:
            return "histórico vazio"
        by_cat: dict[str, int] = {}
        for e in self.events:
            by_cat[e.category] = by_cat.get(e.category, 0) + 1
        with_actual = sum(1 for e in self.events if e.actual is not None)
        revised = sum(1 for e in self.events if e.revised is not None)
        emp = sum(1 for e in self.events if e.effect_source == "empirical")
        return (f"{len(self.events)} registros · {self.events[0].timestamp:%Y-%m-%d} → {self.events[-1].timestamp:%Y-%m-%d} · com actual {with_actual} · revisões {revised} · "
                f"efeitos empíricos {emp} · por categoria: " + ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items())))


# --------------------------------------------------------------------------- cobertura do período
@dataclass
class Coverage:
    start: date
    end: date
    days: int
    macro_events: int
    news_items: int
    revisions: int
    macro_weeks: int
    weeks: int
    news_days: int
    covered_days: int

    @property
    def macro_pct(self) -> float:
        return self.macro_weeks / self.weeks if self.weeks else 0.0

    @property
    def news_pct(self) -> float:
        return self.news_days / self.days if self.days else 0.0

    @property
    def pct(self) -> float:
        return self.covered_days / self.days if self.days else 0.0

    def render(self) -> str:
        return "\n".join([
            f"HISTÓRICO {self.start:%d/%m/%Y} → {self.end:%d/%m/%Y} ({self.days} dias)",
            f"MACRO     {self.macro_events:>8} eventos    · semanas com macro {self.macro_weeks}/{self.weeks} ({self.macro_pct:.0%})",
            f"NEWS      {self.news_items:>8} manchetes  · dias com manchete {self.news_days}/{self.days} ({self.news_pct:.0%})",
            f"REVISÕES  {self.revisions:>8}",
            f"COBERTURA {self.pct:>8.1%}   (dias com macro na semana ou manchete no dia)",
        ])


MACRO_CATEGORIES = ("MACRO", "CENTRAL_BANK")


def coverage(hist: "EventHistory", start: date, end: date) -> Coverage:
    """Quanto do período o banco cobre. MACRO por semana ISO (há semanas sem release relevante, não dias); NEWS por dia."""
    if end < start:
        start, end = end, start
    days = (end - start).days + 1
    weeks_set = {(start + timedelta(days=i)).isocalendar()[:2] for i in range(days)}
    macro_weeks: set = set()
    news_days: set = set()
    macro_n = news_n = rev_n = 0
    for e in hist.events:
        d = e.timestamp.date()
        if not (start <= d <= end):
            continue
        if e.revised is not None:
            rev_n += 1
            continue
        if e.category in MACRO_CATEGORIES:
            macro_n += 1
            macro_weeks.add(d.isocalendar()[:2])
        else:
            news_n += 1
            news_days.add(d)
    covered = sum(1 for i in range(days) if ((start + timedelta(days=i)).isocalendar()[:2] in macro_weeks) or ((start + timedelta(days=i)) in news_days))
    return Coverage(start, end, days, macro_n, news_n, rev_n, len(macro_weeks), len(weeks_set), len(news_days), covered)


class FetchProgress:
    """Janelas já baixadas (sidecar JSON ao lado do CSV): permite continuar de onde parou após 429/queda de rede."""

    def __init__(self, csv_path: str) -> None:
        import os
        self.path = csv_path + ".progress.json"
        self.done: dict[str, int] = {}
        if os.path.exists(self.path):
            import json
            with open(self.path, encoding="utf-8") as f:
                self.done = json.load(f)

    def has(self, key: str) -> bool:
        return key in self.done

    def mark(self, key: str, n: int) -> None:
        import json
        self.done[key] = n
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.done, f, indent=0, sort_keys=True)


# --------------------------------------------------------------------------- CSV
def load_history(path: str) -> EventHistory:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return EventHistory([HistoricalEvent.from_row(r) for r in rows if r.get("timestamp")])


def save_history(hist: EventHistory, path: str) -> int:
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = HistoricalEvent.columns()
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for e in hist.events:
            w.writerow(e.to_row())
    return len(hist.events)


def merge(a: EventHistory, b: EventHistory) -> EventHistory:
    seen = {(e.event_id, e.published_at) for e in a.events}
    out = list(a.events) + [e for e in b.events if (e.event_id, e.published_at) not in seen]
    return EventHistory(out)


# --------------------------------------------------------------------------- efeitos: regras → empíricos
def rule_direction(kind: str, sign: float, sigma: Optional[float], market: str) -> float:
    """Direção esperada (−1..+1) por regra macro: canais de transmissão do evento × sensibilidade do mercado."""
    from .news_engine import CHANNEL_TO_MARKET, TRANSMISSION

    chans = TRANSMISSION.get(kind) or EXTRA_TRANSMISSION.get(kind)
    if not chans or sign == 0.0:
        return 0.0
    mag = min(1.0, abs(sigma) / 2.0) if sigma is not None else 1.0
    weights = CHANNEL_TO_MARKET.get(market, {})
    total = sum(sign * v * mag * weights.get(c, 0.0) for c, v in chans.items())
    norm = sum(abs(w) for c, w in weights.items() if c in chans) or 1.0
    return max(-1.0, min(1.0, total / norm))


def apply_rule_effects(hist: EventHistory) -> int:
    """Direção esperada por ativo a partir das regras macro (mesmas do NEWS ENGINE). Não toca efeitos empíricos."""
    n = 0
    for e in hist.events:
        if e.effect_source == "empirical":
            continue
        sign, sigma = 0.0, None
        if e.surprise is not None:
            typical = TYPICAL.get(e.kind, max(abs(e.forecast or e.previous or 1.0) * 0.1, 0.1))
            sigma = e.surprise / typical if typical else None
            sign = 1.0 if e.surprise > 0 else -1.0 if e.surprise < 0 else 0.0
        elif e.kind in ("fomc_hawkish", "fomc_dovish", "geopolitical_escalation", "geopolitical_deescalation", "systemic_stress", "oil_supply_cut",
                        "oil_supply_increase", "cb_gold_buying", "china_stimulus"):
            sign = 1.0
        if sign == 0.0:
            continue
        for m in EFFECT_MARKETS:
            setattr(e, EFFECT_FIELD[m], round(rule_direction(e.kind, sign, sigma, m), 3))
        e.effect_source = "rule"
        n += 1
    return n


def learn_effects(hist: EventHistory, prices: dict[str, Sequence[tuple[datetime, float]]], horizon_min: int = 60, min_n: int = 8) -> dict:
    """Deixa o histórico determinar o impacto: para cada (kind, sinal da surpresa) e mercado, mede a direção média do
    preço `horizon_min` depois do evento (em fração de ATR aproximada pelo desvio típico) e a taxa de acerto da regra.
    Substitui os efeitos por valores empíricos quando há amostra suficiente."""
    def move_after(series, t0):
        pts = [(t, p) for t, p in series if t >= t0]
        if not pts:
            return None
        p0 = pts[0][1]
        after = [p for t, p in pts if t <= t0 + timedelta(minutes=horizon_min)]
        return ((after[-1] / p0 - 1.0) * 100.0) if len(after) > 1 and p0 else None
    groups: dict[tuple[str, int, str], list[float]] = {}
    for e in hist.events:
        if e.surprise is None and e.category == "MACRO":
            continue
        sgn = 1 if (e.surprise or 0) > 0 else -1 if (e.surprise or 0) < 0 else 0
        for m, series in prices.items():
            mv = move_after(series, e.timestamp)
            if mv is not None:
                groups.setdefault((e.kind, sgn, m), []).append(mv)
    table: dict = {}
    for (kind, sgn, m), moves in groups.items():
        if len(moves) < min_n:
            continue
        avg = statistics.fmean(moves)
        sd = statistics.pstdev(moves) or 1.0
        up = sum(1 for x in moves if x > 0) / len(moves)
        table[(kind, sgn, m)] = {"n": len(moves), "avg_move_pct": round(avg, 3), "p_up": round(up, 2), "effect": round(max(-1.0, min(1.0, avg / sd)), 3)}
    applied = 0
    for e in hist.events:
        sgn = 1 if (e.surprise or 0) > 0 else -1 if (e.surprise or 0) < 0 else 0
        touched = False
        for m in EFFECT_MARKETS:
            row = table.get((e.kind, sgn, m))
            if row:
                setattr(e, EFFECT_FIELD[m], row["effect"])
                touched = True
        if touched:
            e.effect_source = "empirical"
            applied += 1
    return {"table": table, "applied": applied}


def render_effect_table(table: dict) -> str:
    if not table:
        return "efeitos empíricos: amostra insuficiente (mínimo 8 eventos por tipo/sinal/mercado)"
    lines = ["📚 EFEITO EMPÍRICO POR EVENTO (direção média do preço 60 min após, por mercado)", f"{'evento':<18}{'surpresa':>9}{'mercado':>9}{'n':>5}{'mov.médio':>11}{'P(sobe)':>9}{'efeito':>8}"]
    for (kind, sgn, m), r in sorted(table.items()):
        lines.append(f"{kind:<18}{('acima' if sgn > 0 else 'abaixo' if sgn < 0 else 'em linha'):>9}{m:>9}{r['n']:>5}{r['avg_move_pct']:>+10.2f}%{r['p_up']:>8.0%}{r['effect']:>+8.2f}")
    return "\n".join(lines)
