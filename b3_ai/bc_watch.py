"""Banco Central e movimentos bruscos: alerta e registro (nunca envia ordens).

1. COMUNICADO DO BANCO CENTRAL: a cada 5 min lê fontes públicas (Google Notícias numa busca dedicada e o feed de mercados
   do InfoMoney) atrás de manchetes de intervenção no câmbio — leilão de dólar à vista, swap, linha, compra de dólar — e
   classifica o tipo e o efeito esperado no dólar. Fontes públicas chegam DEPOIS dos terminais profissionais (Sisbacen,
   Broadcast, Bloomberg): o registro guarda o horário da notícia e o da detecção, para medir esse atraso.
2. MOVIMENTO BRUSCO: com o MetaTrader, recebe o último negócio de WIN e WDO a cada segundo e dispara quando o preço anda
   4 desvios (da volatilidade típica de 1 minuto) em 1, 2 ou 3 minutos. Não depende de notícia: é o sinal mais rápido.

Cada evento é gravado com o preço na detecção, quanto o ativo já tinha andado nos 5 minutos anteriores e o que fez depois
(+1, +3, +5, +15, +30 e +60 min). Com dezenas de eventos dá para medir se existe vantagem DEPOIS do aviso público.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import urllib.parse
from collections import deque
from typing import Optional

from . import stats as st

Z_TRIGGER = 4.0                     # desvios de 1 min (escalados por √minutos) para "movimento brusco"
COOLDOWN = 600                      # um alerta de movimento brusco por ativo a cada 10 min
NEWS_EVERY, NEWS_BACKOFF = 300, 900
NEWS_MAX_AGE = 45 * 60              # notícia mais velha que isso na primeira leitura não vira evento (o movimento já passou)
CLUSTER = 24 * 3600                 # janela de leitura das manchetes; o agrupamento é por DIA (horário de Brasília)


def _day(ts: float) -> str:
    """Dia em Brasília. As manchetes do mesmo dia são o mesmo episódio: o Google Notícias às vezes publica horários
    errados, então agrupar por janela de horas parte um episódio em vários."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts - 3 * 3600))
NOMES = {"WIN": "Mini Índice (WIN)", "WDO": "Mini Dólar (WDO)"}

_BC = re.compile(r"(banco central|\bBCB?\b|gal[íi]polo|\bDepin\b)", re.I)
_ACT = re.compile(r"(leil[ãa]o|leil[õo]es|swap|interv[ée]m|interven[çc]|vende d[óo]lar|venda de d[óo]lar|vende US\$|compra de d[óo]lar|"
                  r"compra d[óo]lares|injeta|oferta de d[óo]lar|atua no c[âa]mbio|linha de c[âa]mbio|recompra)", re.I)


def classify(title: str) -> Optional[dict]:
    """Manchete de intervenção do BC no câmbio → {tipo, efeito no dólar (+1 sobe, −1 cai, 0 incerto)}; None se não for."""
    if not (_BC.search(title) and _ACT.search(title)):
        return None
    t = title.lower()
    if "swap reverso" in t:
        return {"tipo": "swap cambial reverso", "efeito": 1}
    if "swap" in t:
        return {"tipo": "swap cambial", "efeito": -1}
    if re.search(r"linha|recompra", t):
        return {"tipo": "leilão de linha (venda com recompra)", "efeito": -1}
    if re.search(r"compra (de )?d[óo]lar|compra d[óo]lares|compra us\$", t):
        return {"tipo": "compra de dólar", "efeito": 1}
    if re.search(r"vend|venda|[àa] vista|spot|injeta|oferta", t) or re.search(r"leil[ãa]o (extraordin[áa]rio )?de (us\$|d[óo]lar)|leil[ãa]o de us\$", t):
        return {"tipo": "venda de dólar à vista", "efeito": -1}
    return {"tipo": "intervenção no câmbio (tipo não identificado)", "efeito": 0}


def _key(*parts: object) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


class Watcher:
    def __init__(self, mem) -> None:
        self.mem = mem
        self.lock = threading.Lock()
        self.buf: dict[str, deque] = {"WIN": deque(), "WDO": deque()}
        self.sig1m: dict[str, float] = {}
        self.price: dict[str, float] = {}
        self.last_trigger: dict[str, float] = {}
        self.open_ticks: list[dict] = []          # eventos esperando +1 e +3 min pelos ticks
        self.news_next, self.news_busy = 0.0, False
        self.news_errors: list[str] = []

    # ------------------------------------------------------------------ preços
    def set_sigma(self, k: str, sigma_5min: float) -> None:
        if sigma_5min and sigma_5min > 0:
            self.sig1m[k] = sigma_5min / math.sqrt(5)

    def set_price(self, k: str, price: float) -> None:
        if price:
            self.price[k] = price

    def _price_ago(self, k: str, secs: float, now: float) -> Optional[float]:
        """Preço de `secs` segundos atrás (o tick mais antigo dentro da janela), se o buffer cobrir esse período."""
        b = self.buf[k]
        if not b or now - b[0][0] < secs - 5:
            return None
        for ts, p in b:
            if ts >= now - secs:
                return p
        return None

    def on_tick(self, k: str, price: float, now: Optional[float] = None) -> Optional[dict]:
        if k not in self.buf or not price:
            return None
        now = now or time.time()
        with self.lock:
            b = self.buf[k]
            b.append((now, price))
            while b and now - b[0][0] > 900:
                b.popleft()
            self.price[k] = price
            self._fill_ticks(now)
            return self._detect(k, price, now)

    def _detect(self, k: str, price: float, now: float) -> Optional[dict]:
        sig = self.sig1m.get(k)
        if not sig or now - self.last_trigger.get(k, 0) < COOLDOWN:
            return None
        for w in (60, 120, 180):
            p0 = self._price_ago(k, w, now)
            if not p0:
                continue
            ch = price / p0 - 1
            z = ch / (sig * math.sqrt(w / 60))
            if abs(z) >= Z_TRIGGER:
                self.last_trigger[k] = now
                p5 = self._price_ago(k, 300, now)
                ev = {"chave": _key("preco", k, int(now)), "ts": now, "ts_pub": now, "origem": "preço", "ativo": k,
                      "tipo": f"movimento brusco de {'alta' if ch > 0 else 'queda'}", "efeito": 1 if ch > 0 else -1,
                      "titulo": f"{NOMES[k]} {ch * 100:+.2f}% em {w // 60} min ({abs(z):.1f} desvios)", "link": "",
                      "preco": price, "antes5": (price / p5 - 1) if p5 else None, "z": z}
                self.mem.add_bc_event(ev)
                self.open_ticks.append(ev)
                return ev
        return None

    def _fill_ticks(self, now: float) -> None:
        keep = []
        for ev in self.open_ticks:
            p = self.price.get(ev["ativo"])
            for mins, col in ((1, "d1"), (3, "d3")):
                if col not in ev and p and now >= ev["ts"] + mins * 60:
                    ev[col] = p / ev["preco"] - 1
                    self.mem.set_bc(ev["chave"], col, ev[col])
            if "d3" not in ev and now - ev["ts"] < 600:
                keep.append(ev)
        self.open_ticks = keep

    # ------------------------------------------------------------------ notícias
    def poll_news(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        if self.news_busy or now < self.news_next:
            return
        self.news_busy, self.news_next = True, now + NEWS_EVERY
        threading.Thread(target=self._news, args=(now,), name="b3-ai-bc", daemon=True).start()

    def _news(self, now: float) -> None:
        from .data import news
        from .data.http import DataError
        q = '"Banco Central" (leilão OR swap OR intervenção OR "vende dólares" OR "venda de dólares") câmbio when:1d'
        feeds = [("BR", news.GN.format(q=urllib.parse.quote(q), hl="pt-BR", gl="BR", ceid="BR:pt-419"), ""),
                 ("BR", "https://www.infomoney.com.br/mercados/feed/", "InfoMoney")]
        errs = []
        try:
            for scope, url, src in feeds:
                if "news.google.com" in url and not news.google_ok():
                    continue                     # o Google recusou há pouco: espera a pausa, usa só o InfoMoney
                try:
                    items = news.parse_rss(news.fetch(url, timeout=10, retries=0), scope, src)
                except DataError as e:
                    errs.append(str(e))
                    if news.google_refused(e):
                        self.news_next = now + NEWS_BACKOFF
                    continue
                for it in sorted(items, key=lambda x: x["ts"]):
                    c = classify(it["title"])
                    if c and now - it["ts"] <= CLUSTER:
                        self._news_event(it, c, now)
        finally:
            self.news_errors, self.news_busy = errs, False

    def _news_event(self, it: dict, c: dict, now: float) -> None:
        """Um evento por episódio: a primeira manchete abre o evento; as seguintes (análises, "dólar sobe mesmo com
        intervenção"...) só completam o tipo do leilão se a primeira não o identificou."""
        for ev in self.mem.bc_events(30):
            if ev["origem"].startswith("Banco Central") and _day(ev["ts_pub"] or ev["ts"]) == _day(it["ts"]):
                if not ev["efeito"] and c["efeito"]:
                    self.mem.update_bc_type(ev["chave"], c["tipo"], c["efeito"])
                return
        if now - it["ts"] > NEWS_MAX_AGE:
            return
        with self.lock:
            p = self.price.get("WDO")
            p5 = self._price_ago("WDO", 300, now)
            ev = {"chave": _key("bc", re.sub(r"\W+", "", it["title"].lower())[:80]), "ts": now, "ts_pub": it["ts"],
                  "origem": "Banco Central (notícia)", "ativo": "WDO", "tipo": c["tipo"], "efeito": c["efeito"],
                  "titulo": it["title"], "link": it.get("link", ""), "fonte": it.get("source", ""),
                  "preco": p, "antes5": (p / p5 - 1) if p and p5 else None, "z": None}
            if self.mem.add_bc_event(ev) and p:
                self.open_ticks.append(ev)

    def record(self, chave: str, origem: str, ativo: str, tipo: str, efeito: int, titulo: str, ts_pub: float,
               now: Optional[float] = None) -> bool:
        """Registra um evento externo (ex.: dado econômico divulgado) com o preço atual do ativo, para medir o que vem depois."""
        now = now or time.time()
        with self.lock:
            p = self.price.get(ativo)
            p5 = self._price_ago(ativo, 300, now)
            ev = {"chave": chave, "ts": now, "ts_pub": ts_pub, "origem": origem, "ativo": ativo, "tipo": tipo, "efeito": efeito,
                  "titulo": titulo, "link": "", "preco": p, "antes5": (p / p5 - 1) if p and p5 else None, "z": None}
            ok = self.mem.add_bc_event(ev)
            if ok and p:
                self.open_ticks.append(ev)
            return ok

    # ------------------------------------------------------------------ acompanhamento por barras
    def fill_from_bars(self, bars: dict[str, list[tuple]]) -> None:
        """Completa +5, +15, +30 e +60 min dos eventos registrados com as barras de 5 min do ativo."""
        from . import model as M
        for ev in self.mem.bc_events(200):
            b = bars.get(ev["ativo"]) or []
            if not b or not ev.get("preco"):
                continue
            last_close_t = b[-1][0] + 300
            for mins, col in ((5, "d5"), (15, "d15"), (30, "d30"), (60, "d60")):
                t = ev["ts"] + mins * 60
                if ev.get(col) is None and last_close_t >= t and t - ev["ts"] <= 8 * 3600:
                    p = M._at(b, t, 300)
                    if p and b[0][0] <= ev["ts"]:
                        self.mem.set_bc(ev["chave"], col, p / ev["preco"] - 1)


def summary(events: list[dict]) -> list[dict]:
    """Por origem: quantos eventos e quanto o ativo andou DEPOIS da detecção, na direção do efeito esperado."""
    out = []
    for origem in sorted({e["origem"] for e in events}):
        evs = [e for e in events if e["origem"] == origem and e.get("efeito")]
        row = {"origem": origem, "n": len(evs)}
        for col in ("d1", "d3", "d5", "d15", "d30", "d60"):
            v = [e["efeito"] * e[col] * 100 for e in evs if e.get(col) is not None]
            row[col] = {"n": len(v), "media": st.mean(v) if v else None, "a_favor": (sum(1 for x in v if x > 0) / len(v)) if v else None}
        lag = [(e["ts"] - e["ts_pub"]) / 60 for e in evs if e.get("ts_pub") and e["ts"] >= e["ts_pub"]]
        row["atraso_min"] = st.mean(lag) if lag else None
        out.append(row)
    return out


def report(events: list[dict]) -> str:
    from datetime import datetime
    from .model import BRT
    pct = lambda v: "—" if v is None else f"{v * 100:+.2f}%".replace(".", ",")  # noqa: E731
    L = ["# Banco Central e movimentos bruscos — registro", "",
         "Cada evento foi detectado pelo painel (notícia pública ou preço) e acompanhado depois. Nenhuma ordem foi enviada.", "",
         "## Resumo: o que o ativo fez DEPOIS da detecção, na direção esperada", "",
         "| Origem | Eventos | +1 min | +3 min | +5 min | +15 min | +30 min | +60 min | Atraso médio da notícia |",
         "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in summary(events):
        cells = []
        for col in ("d1", "d3", "d5", "d15", "d30", "d60"):
            c = r[col]
            cells.append("—" if not c["n"] else f"{c['media']:+.2f}% ({c['a_favor'] * 100:.0f}% a favor, {c['n']})".replace(".", ","))
        lag = "—" if r["atraso_min"] is None else f"{r['atraso_min']:.0f} min"
        L.append(f"| {r['origem']} | {r['n']} | " + " | ".join(cells) + f" | {lag} |")
    L += ["", "## Eventos", "", "| Detectado | Origem | Tipo | Evento | Ativo | Preço | 5 min antes | +1 | +5 | +15 | +60 |",
          "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for e in events:
        when = datetime.fromtimestamp(e["ts"], tz=BRT).strftime("%d/%m %H:%M:%S")
        L.append(f"| {when} | {e['origem']} | {e['tipo']} | {e['titulo'][:90]} | {e['ativo']} | {e['preco'] or '—'} | {pct(e.get('antes5'))} | "
                 f"{pct(e.get('d1'))} | {pct(e.get('d5'))} | {pct(e.get('d15'))} | {pct(e.get('d60'))} |")
    L += ["", "Com poucos eventos, as médias não dizem nada. Só a partir de algumas dezenas dá para concluir se existe vantagem depois "
          "do aviso público — e ela precisa ser maior que o custo da operação (no mini dólar, cerca de R$ 12,60 por contrato).", ""]
    return "\n".join(L)
