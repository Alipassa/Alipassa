"""IMPORTADORES do banco histórico de eventos/notícias (point-in-time).

  • TradingEconomicsImporter — calendário macro com actual/forecast/previous/revised (exige chave: TE_API_KEY).
  • ALFREDImporter           — vintages do FRED (ALFRED): o valor INICIALMENTE publicado e as revisões, cada uma com a data
                               em que passou a existir (exige chave gratuita: FRED_API_KEY). Sem consenso → surpresa vs anterior.
  • GDELTImporter            — manchetes + tom + volume por tema (aberto, sem chave) → TESTE B (news/geopolítica/China/petróleo).

Nada aqui inventa efeito: o efeito por ativo nasce das regras macro (history.apply_rule_effects) e é substituído
pelo que o histórico mostrar (history.learn_effects).
"""

from __future__ import annotations

import re
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from ..history import EventHistory, HistoricalEvent, _num, kind_from_name

TE_BASE = "https://api.tradingeconomics.com"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"

TE_IMPORTANCE = {3: "MUITO ALTO", 2: "MÉDIO", 1: "BAIXO"}
CATEGORY_BY_KIND = {"fomc": "CENTRAL_BANK", "speech": "CENTRAL_BANK", "ecb": "CENTRAL_BANK", "boj": "CENTRAL_BANK", "oil_inventories": "ENERGY",
                    "oil_supply_cut": "ENERGY", "china": "CHINA"}


def _iso(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def us_release_time(d: date, hour_et: int = 8, minute: int = 30) -> datetime:
    """Horário UTC de uma divulgação às hour_et:minute (hora de Nova York), respeitando o horário de verão dos EUA."""
    def nth_sunday(year: int, month: int, n: int) -> date:
        first = date(year, month, 1)
        off = (6 - first.weekday()) % 7
        return first + timedelta(days=off + 7 * (n - 1))
    dst_start, dst_end = nth_sunday(d.year, 3, 2), nth_sunday(d.year, 11, 1)
    offset = 4 if dst_start <= d < dst_end else 5
    return datetime(d.year, d.month, d.day, hour_et + offset, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- Trading Economics
class TradingEconomicsImporter:
    def __init__(self, http, api_key: str) -> None:
        self.http, self.key = http, api_key

    def fetch(self, start: date, end: date, country: str = "united states") -> EventHistory:
        url = f"{TE_BASE}/calendar/country/{quote(country)}/{start:%Y-%m-%d}/{end:%Y-%m-%d}?c={self.key}&f=json"
        rows = self.http.get_json(url, ttl=24 * 3600)
        return self.parse(rows)

    @staticmethod
    def parse(rows: list[dict]) -> EventHistory:
        out: list[HistoricalEvent] = []
        for r in rows or []:
            try:
                ts = _iso(datetime.fromisoformat(str(r.get("Date")).replace("Z", "")))
            except (TypeError, ValueError):
                continue
            name = str(r.get("Event") or r.get("Category") or "")
            kind = kind_from_name(name) if kind_from_name(name) != "generic" else kind_from_name(str(r.get("Category") or ""))
            imp = TE_IMPORTANCE.get(int(r.get("Importance") or 0), "BAIXO")
            actual, forecast, previous, revised = _num(r.get("Actual")), _num(r.get("Forecast") or r.get("TEForecast")), _num(r.get("Previous")), _num(r.get("Revised"))
            ev_id = f"TE_{r.get('CalendarId') or name.replace(' ', '_')}_{ts:%Y%m%d%H%M}"
            cat = CATEGORY_BY_KIND.get(kind, "MACRO")
            out.append(HistoricalEvent(ts, ts, ev_id, name, str(r.get("Country") or "US"), str(r.get("Currency") or "USD"), imp, forecast, previous, actual, None,
                                       None, cat, kind, "tradingeconomics"))
            # REVISÃO: o valor anterior foi revisto nesta divulgação → linha nova, publicada AGORA, para o evento anterior
            if revised is not None and previous is not None and revised != previous:
                out.append(HistoricalEvent(ts, ts, f"{ev_id}_REVISAO", f"{name} (revisão do anterior)", str(r.get("Country") or "US"), str(r.get("Currency") or "USD"),
                                           "BAIXO", previous, previous, revised, revised, None, cat, kind, "tradingeconomics"))
        return EventHistory(out)


# --------------------------------------------------------------------------- ALFRED (FRED vintages)
ALFRED_SERIES: dict[str, tuple[str, str, str]] = {
    # série: (nome do evento, kind, transformação para a unidade que o mercado lê)
    "CPIAUCSL": ("CPI MoM", "cpi", "pct"),
    "CPILFESL": ("Core CPI MoM", "core_cpi", "pct"),
    "PCEPILFE": ("Core PCE MoM", "core_pce", "pct"),
    "PPIFIS": ("PPI MoM", "ppi", "pct"),
    "PAYEMS": ("Nonfarm Payrolls (K)", "nfp", "diff"),
    "UNRATE": ("Unemployment Rate", "unemployment", "level"),
    "ICSA": ("Initial Jobless Claims (K)", "jobless_claims", "level_k"),
    "RSAFS": ("Retail Sales MoM", "retail_sales", "pct"),
    "GDPC1": ("GDP QoQ ann.", "gdp", "pct_ann"),
    "CES0500000003": ("Average Hourly Earnings MoM", "earnings", "pct"),
}


class ALFREDImporter:
    KEY_RE = re.compile(r"^[a-f0-9]{32}$")

    def __init__(self, http, api_key: str, log=None) -> None:
        self.http, self.key, self._log = http, (api_key or "").strip(), log
        self.failed: list[tuple[str, str]] = []

    def validate_key(self) -> None:
        from .http import DataError
        if not self.KEY_RE.match(self.key):
            raise DataError(f"FRED_API_KEY inválida ('{self.key[:12]}…'): a chave do FRED tem 32 caracteres hexadecimais minúsculos. "
                            "Gere a sua (gratuita) em https://fred.stlouisfed.org/docs/api/api_key.html e coloque FRED_API_KEY=... no .env")

    def fetch(self, start: date, end: date, series: Optional[list[str]] = None, progress=None) -> EventHistory:
        from .http import DataError
        self.validate_key()
        hist = EventHistory()
        # O FRED rejeita (400) realtime_end "no futuro" — e o dia dele é o de St. Louis (UTC−5/−6). Se a data UTC de hoje
        # ainda não chegou lá, o pedido cai; nesse caso repete-se com o dia anterior.
        rt_end = end
        for sid in series or list(ALFRED_SERIES):
            key = f"alfred:v3:{sid}:{start:%Y-%m-%d}:{end:%Y-%m-%d}"
            if progress is not None and progress.has(key):
                continue
            obs_start = start - timedelta(days=400)     # trimestral (PIB) precisa do trimestre anterior; mensal ganha folga
            rt_start = start - timedelta(days=60)       # a 1ª vintage é recortada pelo FRED na data pedida: fica ANTES do período = só fundo

            def url_for(rte: date) -> str:
                return (f"{FRED_API}?series_id={sid}&api_key={self.key}&file_type=json&observation_start={obs_start:%Y-%m-%d}"
                        f"&realtime_start={rt_start:%Y-%m-%d}&realtime_end={rte:%Y-%m-%d}")
            try:
                try:
                    payload = self.http.get_json(url_for(rt_end), ttl=24 * 3600)
                except DataError as e:
                    if "400" in str(e) and rt_end == end:
                        rt_end = end - timedelta(days=1)
                        if self._log:
                            self._log(f"  ALFRED: {end} ainda é 'futuro' em St. Louis — repetindo com realtime_end={rt_end}")
                        payload = self.http.get_json(url_for(rt_end), ttl=24 * 3600)
                    else:
                        raise
            except DataError as e:
                msg = str(e)
                reason = ("HTTP 400: chave rejeitada ou parâmetros inválidos (confira FRED_API_KEY)" if "400" in msg else
                          "HTTP 429: limite de requisições do FRED" if "429" in msg else msg[-160:])
                self.failed.append((sid, reason))
                if self._log:
                    self._log(f"  ALFRED {sid}: FALHOU — {reason}")
                continue
            part = self.parse(sid, payload.get("observations", []), start)
            for e in part.events:
                hist.add(e)
            if self._log:
                self._log(f"  ALFRED {sid} ({ALFRED_SERIES.get(sid, (sid,))[0]}): {len(part)} publicações/revisões")
            if progress is not None:
                progress.mark(key, len(part))
        return hist

    @staticmethod
    def parse(sid: str, observations: list[dict], start: date) -> EventHistory:
        name, kind, transform = ALFRED_SERIES.get(sid, (sid, "generic", "level"))
        # O FRED devolve UMA linha por (data, valor) cobrindo o intervalo realtime_start..realtime_end em que aquele valor
        # vigorou. Reconstruímos a série COMPLETA de cada vintage: valor de d na vintage V = linha com rs ≤ V ≤ re.
        rows = []
        for o in observations:
            v = _num(o.get("value"))
            if v is None:
                continue
            rows.append((o["date"], o["realtime_start"], o.get("realtime_end") or "9999-12-31", v))
        vintages: dict[str, dict[str, float]] = {}
        for vint in sorted({rs for _, rs, _, _ in rows}):
            vintages[vint] = {d: v for d, rs, re_, v in rows if rs <= vint <= re_}
        first_seen: dict[str, tuple[str, float]] = {}      # data da observação → (vintage inicial, valor transformado)
        out: list[HistoricalEvent] = []
        for vint in sorted(vintages):
            series = vintages[vint]
            dates = sorted(series)
            for i, d in enumerate(dates):
                if i == 0 and transform in ("pct", "diff", "pct_ann"):
                    continue
                v, prev = series[d], series[dates[i - 1]] if i else None
                if transform == "pct":
                    val = round((v / prev - 1) * 100, 2) if prev else None
                elif transform == "pct_ann":
                    val = round(((v / prev) ** 4 - 1) * 100, 1) if prev else None
                elif transform == "diff":
                    val = round(v - prev, 0) if prev is not None else None
                elif transform == "level_k":
                    val = round(v / 1000.0, 0)
                else:
                    val = v
                if val is None:
                    continue
                pub = datetime.fromisoformat(vint).date()
                if pub < start:
                    if d not in first_seen:
                        first_seen[d] = ("", val)      # fundo: conhecido antes do período, não é evento nem gera revisão
                    else:
                        first_seen[d] = (first_seen[d][0], val)
                    continue
                if d not in first_seen:
                    first_seen[d] = (vint, val)
                    prev_val = first_seen.get(dates[i - 1], (None, None))[1] if i else None
                    out.append(HistoricalEvent(us_release_time(pub), us_release_time(pub), f"ALFRED_{sid}_{d}", f"{name} ({d[:7]})", "US", "USD",
                                               "MUITO ALTO" if kind in ("cpi", "core_cpi", "nfp", "core_pce") else "ALTO", None, prev_val, val, None,
                                               (round(val - prev_val, 3) if prev_val is not None else None), "MACRO", kind, "alfred", surprise_basis="previous"))
                elif first_seen[d][0] and first_seen[d][1] != val:   # revisão só de publicação ocorrida dentro do período
                    # revisão: publicada na data desta vintage, mantém o event_id → substitui só a partir de published_at
                    out.append(HistoricalEvent(us_release_time(datetime.fromisoformat(first_seen[d][0]).date()), us_release_time(pub), f"ALFRED_{sid}_{d}",
                                               f"{name} ({d[:7]}) revisado", "US", "USD", "BAIXO", None, first_seen[d][1], val, val, None, "MACRO", kind, "alfred"))
                    first_seen[d] = (first_seen[d][0], val)
        return EventHistory(out)


# --------------------------------------------------------------------------- GDELT
GDELT_TOPICS: dict[str, tuple[str, str]] = {
    # tema: (query GDELT, categoria)
    "geopolitica": ('(war OR missile OR ceasefire OR sanctions OR "Middle East" OR Iran OR Israel OR Ukraine)', "GEOPOLITICAL"),
    "petroleo": ('(OPEC OR "crude oil" OR "oil supply" OR "oil prices")', "ENERGY"),
    "china": ('("China economy" OR PBOC OR "China stimulus" OR "Chinese exports")', "CHINA"),
    "fed": ('("Federal Reserve" OR Powell OR FOMC OR "rate cut" OR "rate hike")', "CENTRAL_BANK"),
    "risco": ('(recession OR "bank failure" OR "credit stress" OR "debt default" OR tariffs)', "NEWS"),
}


class GDELTImporter:
    """GDELT limita a ~1 requisição a cada 5 s (HTTP 429 acima disso): as chamadas são espaçadas por `min_interval`,
    um 429 espera e tenta de novo, e `checkpoint` recebe o parcial após cada janela (nada se perde se cair no meio)."""

    def __init__(self, http, min_interval: float = 8.0, retry_wait: float = 60.0, max_retries: int = 5, sleep=time.sleep, log=None) -> None:
        self.http, self.min_interval, self.retry_wait, self.max_retries = http, min_interval, retry_wait, max_retries
        self._sleep, self._log, self._last = sleep, log, 0.0

    def _get(self, url: str):
        from .http import DataError
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            try:
                out = self.http.get_json(url, ttl=24 * 3600)
                self._last = time.monotonic()
                return out
            except DataError as e:
                self._last = time.monotonic()
                if attempt == self.max_retries:
                    raise
                rate = "429" in str(e)
                m = re.search(r"Retry-After (\d+)s", str(e))
                # 429 é bloqueio por rajada: espera exponencial (60 s, 2, 4, 8, 16 min) ou o Retry-After do servidor
                wait = (float(m.group(1)) if m else self.retry_wait * (2 ** attempt)) if rate else min(self.retry_wait, 20.0)
                if self._log:
                    self._log(f"GDELT {'429 (bloqueio por excesso de requisições)' if rate else 'falha de rede'}: aguardando {wait / 60:.1f} min e tentando de novo ({attempt + 1}/{self.max_retries})")
                self._sleep(wait)
        raise DataError("GDELT: falha persistente")

    def _url(self, query: str, mode: str, start: datetime, end: datetime, extra: str = "") -> str:
        return (f"{GDELT_DOC}?query={quote(query + ' sourcelang:english')}&mode={mode}&format=json"
                f"&startdatetime={start:%Y%m%d%H%M%S}&enddatetime={end:%Y%m%d%H%M%S}{extra}")

    def fetch(self, start: date, end: date, topics: Optional[list[str]] = None, chunk_days: int = 30, max_records: int = 250,
              checkpoint=None, enrich: bool = False, progress=None, mode: str = "volinfo") -> EventHistory:
        """1 chamada por tema e janela. `mode="volinfo"` (padrão): `timelinevolinfo` devolve, DIA A DIA, o volume e as manchetes
        mais relevantes de cada dia — cobertura uniforme da janela. `mode="artlist"`: as `max_records` manchetes mais recentes
        com hora exata (concentram-se no fim da janela; use janelas curtas). `enrich=True` acrescenta o tom (timelinetone).
        Janelas já feitas (progress) são puladas; janela que falhar após as tentativas fica em `self.failed` e a execução continua."""
        from .http import DataError
        hist = EventHistory()
        self.failed: list[tuple[str, str, str]] = []
        t0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        t_end = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)
        topics = topics or list(GDELT_TOPICS)
        n_chunks = max(1, -(-(t_end - t0).days // chunk_days))
        calls = 2 if enrich else 1
        if self._log:
            self._log(f"GDELT ({mode}): {len(topics)} temas × {n_chunks} janelas de {chunk_days} dias × {calls} chamada(s) ≈ "
                      f"{len(topics) * n_chunks * calls * self.min_interval / 60:.0f} min (limite do GDELT: ~1 requisição a cada 5 s)")
        for topic in topics:
            query, cat = GDELT_TOPICS[topic]
            t = t0
            while t < t_end:
                t1 = min(t + timedelta(days=chunk_days), t_end)
                key = f"gdelt:{topic}:{t:%Y%m%d}:{t1:%Y%m%d}:{mode}{':enrich' if enrich else ''}"
                if progress is not None and progress.has(key):
                    t = t1
                    continue
                try:
                    if mode == "artlist":
                        arts = self._get(self._url(query, "artlist", t, t1, f"&maxrecords={max_records}&sort=datedesc"))
                        vol = None
                    else:
                        arts = vol = self._get(self._url(query, "timelinevolinfo", t, t1))
                    tone = self._get(self._url(query, "timelinetone", t, t1)) if enrich else None
                except DataError as e:
                    self.failed.append((topic, f"{t:%Y-%m-%d}→{t1:%Y-%m-%d}", str(e)[-120:]))
                    if self._log:
                        self._log(f"  {topic} {t:%Y-%m-%d}→{t1:%Y-%m-%d}: FALHOU (rode de novo para continuar daqui)")
                    t = t1
                    continue
                part = self.parse(topic, cat, arts, tone, vol)
                for e in part.events:
                    hist.add(e)
                if self._log:
                    self._log(f"  {topic} {t:%Y-%m-%d}→{t1:%Y-%m-%d}: {len(part)} manchetes")
                if checkpoint:
                    checkpoint(hist)
                if progress is not None:
                    progress.mark(key, len(part))
                t = t1
        return hist

    @staticmethod
    def _timeline(payload: dict) -> list[tuple[datetime, float]]:
        out = []
        for series in (payload or {}).get("timeline", []):
            for pt in series.get("data", []):
                try:
                    out.append((datetime.strptime(pt["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), float(pt.get("value", 0.0))))
                except (KeyError, ValueError):
                    continue
        return sorted(out)

    @staticmethod
    def _articles(payload: dict) -> list[tuple[str, datetime, str]]:
        """(título, instante, domínio) tanto de `artlist` (seendate exato) quanto de `timelinevolinfo` (toparts por dia).
        No volinfo só se conhece o DIA: published_at = fim do dia (conservador — o cérebro nunca vê antes de existir)."""
        out = []
        for a in (payload or {}).get("articles", []):
            try:
                ts = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            out.append((str(a.get("title") or "").strip(), ts, str(a.get("domain") or "")))
        for series in (payload or {}).get("timeline", []):
            for pt in series.get("data", []):
                try:
                    day = datetime.strptime(pt["date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except (KeyError, ValueError):
                    continue
                ts = day.replace(hour=23, minute=59) if day.hour == 0 and day.minute == 0 else day
                for a in pt.get("toparts", []) or []:
                    url = str(a.get("url") or "")
                    dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
                    out.append((str(a.get("title") or "").strip(), ts, dom))
        return out

    @staticmethod
    def parse(topic: str, category: str, artlist: dict, tone: Optional[dict] = None, volume: Optional[dict] = None) -> EventHistory:
        from ..news_engine import QUALITATIVE
        tones = GDELTImporter._timeline(tone or {})
        vols = GDELTImporter._timeline(volume or {})

        def nearest(series, t):
            best = None
            for ts, v in series:
                if ts <= t:
                    best = v
                else:
                    break
            return best
        out: list[HistoricalEvent] = []
        seen: set[str] = set()
        for title, ts, domain in GDELTImporter._articles(artlist):
            if not title:
                continue
            key = re.sub(r"\W+", " ", title.lower())[:60]
            if key in seen:
                continue
            seen.add(key)
            kind = next((k for pat, k, _ in QUALITATIVE if re.search(pat, title.lower())), "generic")
            tn, vl = nearest(tones, ts), nearest(vols, ts)
            sentiment = "" if tn is None else ("POSITIVE" if tn > 1.0 else "NEGATIVE" if tn < -1.0 else "NEUTRAL")
            out.append(HistoricalEvent(ts, ts, f"GDELT_{topic}_{ts:%Y%m%d%H%M}_{zlib.crc32(key.encode()) % 10000:04d}", f"{topic}: {title[:60]}", "GLOBAL", "", "MÉDIO",
                                       None, None, None, None, None, category, kind, f"gdelt/{domain}", title[:160], sentiment, tone=tn, volume=vl))
        return EventHistory(out)
