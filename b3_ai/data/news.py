"""Notícias reais por RSS: Google Notícias (buscas temáticas, Brasil e exterior), InfoMoney e G1 Economia."""

from __future__ import annotations

import html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .http import DataError, fetch

GN = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"

# (escopo, consulta) — "when:7d" cobre a semana; a recência é ponderada depois
QUERIES_BR = ["ibovespa", "dólar real câmbio", "copom selic banco central", "fiscal governo contas públicas economia",
              "inflação IPCA", "petrobras vale mercado", "analistas recomendação preço-alvo ações",
              "XP OR BTG OR \"Itaú BBA\" OR \"Bradesco BBI\" OR Safra OR Genial recomendação ações", "balanço lucro prejuízo ações B3"]
QUERIES_INT = ["federal reserve interest rates", "wall street stocks", "china economy", "oil prices brent opec",
               "iron ore prices", "us inflation jobs report", "geopolitical risk markets tariffs",
               "Goldman OR JPMorgan OR \"Morgan Stanley\" OR Citi OR UBS Brazil stocks emerging markets", "strategist outlook stocks emerging markets"]
DIRECT = [("BR", "https://www.infomoney.com.br/mercados/feed/", "InfoMoney"),
          ("BR", "https://g1.globo.com/rss/g1/economia/", "G1 Economia")]


def _feeds() -> list[tuple[str, str, str]]:
    out = []
    for q in QUERIES_BR:
        out.append(("BR", GN.format(q=urllib.parse.quote(q + " when:7d"), hl="pt-BR", gl="BR", ceid="BR:pt-419"), ""))
    for q in QUERIES_INT:
        out.append(("INT", GN.format(q=urllib.parse.quote(q + " when:7d"), hl="en-US", gl="US", ceid="US:en"), ""))
    return out + DIRECT


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    if not m:
        return ""
    t = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", m.group(1).strip(), flags=re.S)
    return html.unescape(html.unescape(t)).strip()


def parse_rss(text: str, scope: str, default_source: str = "") -> list[dict]:
    items = []
    for block in re.findall(r"<item>(.*?)</item>", text, re.S):
        title = re.sub(r"<[^>]+>", "", _tag(block, "title"))
        if not title:
            continue
        source = _tag(block, "source") or default_source
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            when = parsedate_to_datetime(_tag(block, "pubDate"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        items.append({"title": title, "source": source, "link": _tag(block, "link"),
                      "ts": when.timestamp(), "scope": scope})
    return items


def dedupe(items: list[dict], max_age_days: float = 7.0) -> list[dict]:
    now = datetime.now(timezone.utc).timestamp()
    seen: set[str] = set()
    out: list[dict] = []
    for it in sorted(items, key=lambda x: -x["ts"]):
        key = re.sub(r"\W+", "", it["title"].lower())[:70]
        if key in seen or now - it["ts"] > max_age_days * 86400 or it["ts"] > now + 3600:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_news(max_age_days: float = 7.0, previous: list[dict] | None = None, pause: float = 0.6) -> tuple[list[dict], list[str]]:
    """Devolve (notícias deduplicadas, mais recentes primeiro; erros por feed).

    As buscas vão UMA por vez, com pausa, porque o Google Notícias recusa rajadas (HTTP 429); ao primeiro 429 as buscas
    restantes do Google são deixadas para a próxima rodada. `previous` (a leitura anterior) é mesclado ao resultado, então
    uma rodada parcial não apaga o que já se tinha.
    """
    import time as _time
    errors: list[str] = []
    items: list[dict] = list(previous or [])
    google_blocked = False
    for scope, url, src in _feeds():
        is_google = "news.google.com" in url
        if is_google and google_blocked:
            continue
        try:
            items += parse_rss(fetch(url, timeout=12, retries=0), scope, src)
        except DataError as e:
            errors.append(str(e))
            if is_google and "429" in str(e):
                google_blocked = True
        _time.sleep(pause if is_google else 0.1)
    for it in items:
        it.pop("_cls", None)              # classificação é refeita sobre o conjunto mesclado
    return dedupe(items, max_age_days), errors
