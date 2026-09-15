"""Notícias (RSS), interpretação em três níveis (§13) e calendário econômico.

NÍVEL 1 notícia → NÍVEL 2 interpretação → NÍVEL 3 impacto no ouro, com extração de
RESULTADO × CONSENSO quando a manchete traz números ("CPI 2.8% vs 3.0% expected").
O interpretador padrão é por regras; um LLM pode ser plugado via `NewsInterpreter`.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Protocol

from ..events import EVENT_GOLD_SENSITIVITY
from ..models import EconomicEvent, NewsItem
from .http import HttpClient

DEFAULT_FEEDS: tuple[str, ...] = (
    "https://www.fxstreet.com/rss/news",
    "https://www.kitco.com/rss/category/news",
    "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
)

# palavra-chave → (categoria, impacto no ouro -1..+1)
KEYWORDS: list[tuple[str, str, float]] = [
    # Fed / juros
    (r"\b(rate cut|cuts? rates?|dovish|easing|corte de juros)\b", "fed", +0.6),
    (r"\b(rate hike|hikes? rates?|hawkish|tightening|higher for longer|alta de juros)\b", "fed", -0.6),
    (r"\b(yields? (fall|drop|slide|tumble|decline)|treasury rally)\b", "macro", +0.4),
    (r"\b(yields? (rise|jump|surge|climb)|treasury sell-?off)\b", "macro", -0.4),
    # dólar
    (r"\b(dollar (falls|drops|weakens|slides|tumbles)|dxy (falls|drops))\b", "macro", +0.4),
    (r"\b(dollar (rises|gains|strengthens|jumps|surges)|dxy (rises|jumps))\b", "macro", -0.4),
    # inflação / atividade
    (r"\b(inflation (cools|eases|slows|falls|softer)|cpi (falls|cools|misses))\b", "macro", +0.5),
    (r"\b(inflation (heats|accelerates|jumps|hotter|sticky)|cpi (jumps|beats|hotter))\b", "macro", -0.5),
    (r"\b(recession|contraction|slowdown|weak (jobs|payrolls|data)|payrolls miss)\b", "macro", +0.3),
    (r"\b(strong (jobs|payrolls|data)|payrolls beat|blowout jobs)\b", "macro", -0.4),
    # geopolítica
    (r"\b(war|missile|strike|attack|invasion|escalat|conflict|sanction|nuclear|troops|ceasefire collapse)\w*", "geopolitical", +0.5),
    (r"\b(ceasefire|peace (deal|talks)|de-?escalat|truce)\w*", "geopolitical", -0.4),
    # sistêmico
    (r"\b(bank (run|collapse|failure|rescue)|default|contagion|liquidity crisis|credit (stress|crunch)|bailout)\b", "systemic", +0.5),
    # China / bancos centrais / fluxo
    (r"\b(pboc|china central bank|central bank(s)? (buy|purchase|add)|reserves? (rise|increase))\w*", "flow", +0.5),
    (r"\b(etf (inflow|buying)|gold etf holdings rise)\w*", "flow", +0.4),
    (r"\b(etf (outflow|selling)|gold etf holdings fall)\w*", "flow", -0.4),
    (r"\b(china (stimulus|easing|cuts? rrr))\b", "china", +0.3),
    (r"\b(china (slowdown|property crisis|deflation))\b", "china", +0.1),
]

# extração de RESULTADO vs CONSENSO em manchetes
RELEASE_RE = re.compile(
    r"(?P<name>core cpi|cpi|core pce|pce|nonfarm payrolls|payrolls|nfp|unemployment rate|jobless claims|initial claims|"
    r"gdp|ism manufacturing|ism services|retail sales|jolts|consumer confidence|average hourly earnings)"
    r"[^0-9\-+]{0,40}(?P<actual>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>%|k|m)?"
    r"[^0-9\-+]{0,40}(?:vs\.?|versus|against|expected|forecast|consensus|est\.?|exp\.?)[^0-9\-+]{0,25}(?P<consensus>[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
NAME_TO_KIND = {
    "core cpi": "core_cpi", "cpi": "cpi", "core pce": "core_pce", "pce": "pce", "nonfarm payrolls": "nfp", "payrolls": "nfp",
    "nfp": "nfp", "unemployment rate": "unemployment", "jobless claims": "jobless_claims", "initial claims": "jobless_claims",
    "gdp": "gdp", "ism manufacturing": "ism", "ism services": "ism", "retail sales": "retail_sales", "jolts": "jolts",
    "consumer confidence": "consumer_confidence", "average hourly earnings": "earnings",
}


class NewsInterpreter(Protocol):
    def interpret(self, item: NewsItem) -> NewsItem:  # pragma: no cover - interface
        ...


class RuleInterpreter:
    """Interpretação por regras. Preenche category, gold_impact, interpretation e,
    quando há números, cria o EconomicEvent correspondente em `self.events`."""

    def __init__(self) -> None:
        self.events: list[EconomicEvent] = []

    def interpret(self, item: NewsItem) -> NewsItem:
        text = item.headline.lower()
        cat, impact, hits = "generic", 0.0, []
        for pattern, category, val in KEYWORDS:
            if re.search(pattern, text):
                hits.append(f"{category}:{val:+.1f}")
                impact += val
                if cat == "generic" or abs(val) > 0.4:
                    cat = category
        m = RELEASE_RE.search(item.headline)
        if m:
            kind = NAME_TO_KIND.get(m.group("name").lower(), "generic")
            actual, cons = float(m.group("actual")), float(m.group("consensus"))
            sens = EVENT_GOLD_SENSITIVITY.get(kind, 0.0)
            surprise = actual - cons
            impact = max(-1.0, min(1.0, sens * surprise / max(abs(cons) * 0.1, 0.1)))
            cat = "macro"
            self.events.append(EconomicEvent(m.group("name").upper(), item.time, "MUITO ALTO", consensus=cons, actual=actual, kind=kind, unit=m.group("unit") or ""))
            hits.append(f"release {kind}: {actual} vs {cons} (surpresa {surprise:+.2f})")
        item.category = cat
        item.gold_impact = max(-1.0, min(1.0, impact))
        # quanto já estava precificado: manchetes com "as expected"/"in line" → alto
        if re.search(r"\b(as expected|in line|matches? (forecast|expectations)|widely expected)\b", text):
            item.priced_in = 0.8
        item.interpretation = "; ".join(hits) if hits else "sem impacto identificado"
        return item


def parse_rss(xml_text: str, source: str = "") -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        pub = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        try:
            t = parsedate_to_datetime(pub) if pub else datetime.now(timezone.utc)
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            t = datetime.now(timezone.utc)
        items.append(NewsItem(headline=title, source=source or (root.findtext("channel/title") or ""), time=t.astimezone(timezone.utc)))
    return items


class NewsCollector:
    def __init__(self, http: HttpClient, feeds: tuple[str, ...] = DEFAULT_FEEDS, interpreter: Optional[NewsInterpreter] = None,
                 max_age_hours: float = 24.0) -> None:
        self.http = http
        self.feeds = feeds
        self.interpreter = interpreter or RuleInterpreter()
        self.max_age = timedelta(hours=max_age_hours)
        self.errors: dict[str, str] = {}
        self.health: list = []   # news_engine.FeedHealth por feed (fonte, atualização, notícias, válidas, descartadas, erro)

    def collect(self, now: Optional[datetime] = None) -> list[NewsItem]:
        from ..news_engine import FeedHealth

        now = now or datetime.now(timezone.utc)
        out: list[NewsItem] = []
        seen: set[str] = set()
        self.health = []
        if hasattr(self.interpreter, "events"):
            self.interpreter.events = []          # reinterpretação a cada ciclo: sem duplicar eventos já lidos
        self.errors = {}
        for url in self.feeds:
            source = url.split("/")[2]
            try:
                items = parse_rss(self.http.get_text(url, ttl=300), source=source)
            except Exception as e:  # noqa: BLE001 - isolar falha por feed
                self.errors[url] = str(e)
                self.health.append(FeedHealth(source, False, str(e)))
                continue
            valid = discarded = 0
            last = None
            for it in items:
                last = it.time if last is None or it.time > last else last
                key = it.headline.lower()[:80]
                if key in seen or now - it.time > self.max_age:
                    discarded += 1
                    continue
                seen.add(key)
                out.append(self.interpreter.interpret(it))
                valid += 1
            self.health.append(FeedHealth(source, True, "", len(items), valid, discarded, last))
            if not items:
                self.health[-1].ok, self.health[-1].error = False, "feed vazio ou não parseável"
        out.sort(key=lambda n: n.time, reverse=True)
        return out

    def released_events(self) -> list[EconomicEvent]:
        return list(getattr(self.interpreter, "events", []))


def aggregate_sentiment(news: list[NewsItem], now: datetime, half_life_hours: float = 6.0) -> tuple[Optional[float], Optional[float]]:
    """(sentimento -1..+1 ponderado por recência, variação vs. janela anterior)."""
    if not news:
        return None, None
    def weighted(items: list[NewsItem], ref: datetime) -> Optional[float]:
        num = den = 0.0
        for n in items:
            age_h = max(0.0, (ref - n.time).total_seconds() / 3600)
            w = 0.5 ** (age_h / half_life_hours)
            num += w * n.gold_impact * (1 - n.priced_in)
            den += w
        return num / den if den else None
    recent = [n for n in news if now - n.time <= timedelta(hours=12)]
    older = [n for n in news if timedelta(hours=12) < now - n.time <= timedelta(hours=24)]
    s_now, s_prev = weighted(recent, now), weighted(older, now - timedelta(hours=12))
    change = (s_now - s_prev) if (s_now is not None and s_prev is not None) else None
    return (round(s_now, 3) if s_now is not None else None), (round(change, 3) if change is not None else None)


def geopolitical_index(news: list[NewsItem], now: datetime) -> tuple[Optional[float], Optional[float]]:
    """Risco geopolítico 0..100 pela intensidade de manchetes das últimas 24h, e variação vs 24h anteriores."""
    if not news:
        return None, None
    def score(items: list[NewsItem]) -> float:
        geo = [n for n in items if n.category in ("geopolitical", "systemic")]
        return min(100.0, 20.0 * sum(abs(n.gold_impact) for n in geo) + 5.0 * len(geo))
    cur = score([n for n in news if now - n.time <= timedelta(hours=24)])
    prev = score([n for n in news if timedelta(hours=24) < now - n.time <= timedelta(hours=48)])
    return round(cur, 1), round(cur - prev, 1)


def load_calendar(path: str) -> list[EconomicEvent]:
    """Calendário local (JSON): [{"name","time" (ISO UTC),"impact","kind","consensus","previous","actual","unit"}]."""
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    out: list[EconomicEvent] = []
    for r in rows:
        t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        out.append(EconomicEvent(r["name"], t if t.tzinfo else t.replace(tzinfo=timezone.utc), r.get("impact", "ALTO"),
                                 r.get("consensus"), r.get("previous"), r.get("actual"), r.get("kind", "generic"), r.get("unit", "")))
    return out
