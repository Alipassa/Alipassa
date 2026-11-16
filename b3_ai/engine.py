"""Motor do B3 AI: coleta dados reais, roda os modelos e monta o estado (JSON) do painel e do relatório."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from . import alerts, market_b3, model as M, news_ai, opinion, stats as st, technical as T
from .data import bcb, calendar as cal, focus, mt5 as mt5feed, news, tradingview as tv, yahoo
from .data.http import DataError
from .memory import Memory
from .universe import ASSETS, BY_KEY, FACTORS, TARGETS

# abertura: líder do exterior durante a noite → direção do gap. (símbolo do líder, sinal, nome)
OPEN_LEADER = {"WIN": ("OANDA:SPX500USD", 1, "S&P 500"), "WDO": ("FX_IDC:USDMXN", 1, "dólar × peso mexicano")}
DAILY_BARS = 1800
INTRA_BARS = 5000
ES = BY_KEY["ES"].tv
WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def bars5_ok(b: list[tuple]) -> bool:
    return len(b) > 10


def brt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=M.BRT)


def next_sessions(last_date: str, n: int) -> str:
    """Data do n-ésimo dia útil depois de `last_date` (feriados não são conhecidos — aproximação por dia útil)."""
    d = datetime.strptime(last_date, "%Y-%m-%d")
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.strftime("%Y-%m-%d")


def fmt_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{WEEKDAYS[d.weekday()]} {d.day:02d}/{d.month:02d}"


class Engine:
    def __init__(self, data_dir: str = "dados", with_news: bool = True, fonte: str = "") -> None:
        import os
        self.fonte = (fonte or os.environ.get("B3AI_FONTE") or "web").lower()
        self.mt5 = mt5feed.MT5Feed() if self.fonte == "mt5" else None
        self.delays: dict[str, int] = {}     # atraso (s) de cada série, informado pelo feed
        self.rt: dict = {}            # dados em tempo real do MetaTrader no ciclo atual (vazio = feed público)
        self.data_dir = data_dir
        self._agenda_cache: tuple[float, dict] = (0.0, {})
        self._open_cache: tuple[float, dict] = (0.0, {})
        self.mem = Memory(f"{data_dir}/b3_ai.db")
        self.with_news = with_news
        self.lock = threading.Lock()
        self.state: dict = {"ok": False, "erro": "primeira leitura em andamento…"}
        self.b3: dict = {"resumo": None, "papeis": []}
        self._daily_cache: dict = {}
        self._intra_cache: dict = {}
        self._news_cache: tuple[float, Optional[dict], list[str]] = (0.0, None, [])
        self._bcb_cache: tuple[float, dict] = (0.0, {})
        self.etapa, self.etapa_ts, self.ciclo_ts, self.ciclos = "iniciando", time.time(), time.time(), 0
        self._b3_ts = 0.0
        self.b3_rt = self.b3_rt_liq = self.b3_liq = 0     # papéis da tabela com preço do MetaTrader (total e entre os líquidos)
        self._b3_rows: list[dict] = []
        self._b3_scan_ts = 0.0
        self._focus_cache: tuple[float, dict] = (0.0, {})
        self._alerts_sent: set[str] = set()
        self.ai: dict = {}           # última análise escrita pelo analista com IA (sob demanda)

    def _step(self, nome: str) -> None:
        """Marca a etapa do ciclo (aparece no painel e na janela preta): mostra se está carregando ou travado, e onde."""
        now = time.time()
        if self.ciclos == 0:
            print(f"[b3-ai] {self.etapa} ... {now - self.etapa_ts:.1f}s", flush=True)
        self.etapa, self.etapa_ts = nome, now

    # ------------------------------------------------------------------ coleta
    def _history(self) -> tuple[dict, list[str]]:
        notes: list[str] = []
        reqs = [(a.tv, "1D", DAILY_BARS) for a in list(TARGETS.values()) + ASSETS]
        reqs += [(TARGETS[k].tv, tf, INTRA_BARS) for k in TARGETS for tf in ("5", "15")] + [(ES, "5", INTRA_BARS), (ES, "15", INTRA_BARS)]
        try:
            self.delays = {}
            h = tv.fetch_history(reqs, delays=self.delays)
            missing = [s for s, tf, _ in reqs if (s, tf) not in h]
            if (TARGETS["WIN"].tv, "1D") not in h:
                raise DataError("histórico do WIN não veio")
            if missing:
                notes.append("sem histórico: " + ", ".join(sorted(set(missing))))
            return h, notes
        except DataError as e:
            notes.append(f"TradingView (histórico) falhou: {e} — usando Yahoo (diário, sem intraday; WIN≈Ibovespa, WDO≈USD/BRL)")
        h = {}
        for a in list(TARGETS.values()) + ASSETS:
            if not a.yahoo:
                continue
            try:
                h[(a.tv, "1D")] = yahoo.fetch_daily(a.yahoo)
            except DataError as e:
                notes.append(f"Yahoo {a.yahoo}: {e}")
        if (TARGETS["WIN"].tv, "1D") not in h:
            raise DataError("; ".join(notes[-3:]) or "sem dados de mercado")
        return h, notes

    def _news(self) -> tuple[Optional[dict], list[str]]:
        if not self.with_news:
            return None, []
        ts, cached, errs = self._news_cache
        if cached is not None and time.time() - ts < 600:
            return cached, errs
        try:
            items, errs = news.fetch_news()
            if items:
                cached = news_ai.analyze(items)
                self._news_cache = (time.time(), cached, errs)
        except Exception as e:  # noqa: BLE001 — notícias nunca derrubam o ciclo
            errs = [f"notícias: {e}"]
        return cached, errs

    def _bcb(self) -> dict:
        ts, cached = self._bcb_cache
        if cached and time.time() - ts < 6 * 3600:
            return cached
        try:
            snap = bcb.snapshot()
            if snap:
                self._bcb_cache = (time.time(), snap)
                return snap
        except Exception:  # noqa: BLE001
            pass
        return cached

    def _opening(self) -> dict:
        """Estudo da abertura (refeito a cada 6 h) + barras recentes de 1 h dos líderes para a leitura ao vivo."""
        ts, cached = self._open_cache
        try:
            if not cached or time.time() - ts > 6 * 3600:
                reqs = [(TARGETS[k].tv, "60", 5000) for k in TARGETS] + [(v[0], "60", 5000) for v in OPEN_LEADER.values()]
                h = tv.fetch_history(reqs)
                cached = {"estudo": {k: M.opening_study(h.get((TARGETS[k].tv, "60"), []), h.get((OPEN_LEADER[k][0], "60"), []), OPEN_LEADER[k][1])
                                     for k in TARGETS}}
                self._open_cache = (time.time(), cached)
            live = tv.fetch_history([(v[0], "60", 120) for v in OPEN_LEADER.values()])
            cached["lider"] = {k: live.get((OPEN_LEADER[k][0], "60"), []) for k in TARGETS}
        except Exception:  # noqa: BLE001 — sem o estudo, o quadro só fica sem a linha de abertura
            cached = cached or {"estudo": {}, "lider": {}}
        return cached

    def _open_row(self, k: str, op: dict, close_ts: float, close_px: float, alvo: str) -> Optional[dict]:
        est, lb = (op.get("estudo") or {}).get(k), (op.get("lider") or {}).get(k) or []
        if not est or len(lb) < 5:
            return None
        a = M._at(lb, close_ts, 3600)
        if not a:
            return None
        x = OPEN_LEADER[k][1] * (lb[-1][4] / a - 1)
        b = next((i for i in est["bins"] if abs(x) < i["ate"] * est["sd"]), est["bins"][-1])
        hit = b["acerto"] if b["n"] >= 25 and b["acerto"] is not None else est["acerto"]
        p = 0.5 if x == 0 else st.clamp(hit if x > 0 else 1 - hit, 0.03, 0.97)
        gap = est["beta"] * x
        dv = est.get("devolve")
        nota = f"{OPEN_LEADER[k][2]} {x * 100:+.2f}% desde o fechamento da B3 → gap esperado {gap * 100:+.2f}%. É a direção da ABERTURA, não do pregão"
        if dv:
            nota += f"; depois de gap grande, o pregão fechou CONTRA o gap em {dv['pct'] * 100:.0f}% de {dv['n']} casos"
        row = {"horizonte": "Abertura", "tipo": "abertura", "alvo": alvo, "p_alta": p, "p_queda": 1 - p, "base": 0.5, "k": None,
               "oos_n": b["n2"], "oos_acerto": b["acerto2"], "oos_acerto_conv": est["acerto2"], "oos_n_conv": est["n2"], "amostra": est["n"],
               "motivos": [], "faixas": M.bands(p, est["gap_sd"]), "sigma_pct": est["gap_sd"] * 100,
               "faixa_min": close_px * (1 + gap - est["gap_sd"]), "faixa_max": close_px * (1 + gap + est["gap_sd"]), "ref": close_px,
               "validado": bool(b["n"] >= 25 and (b["acerto2"] or 0) > 0.6), "nota": nota, "ajuste_exterior": gap * 100,
               "confianca": f"frequência histórica: abertura acompanhou o exterior em {hit * 100:.0f}% de {b['n']} casos desse tamanho"}
        row["sinal"] = "ABERTURA EM ALTA" if p >= 0.55 else "ABERTURA EM QUEDA" if p <= 0.45 else "ABERTURA INDEFINIDA"
        return row

    def _agenda(self, d: "M.DailyData") -> dict:
        """Próximos eventos de alto impacto + o que o mini índice fez nos dias de decisão do Fed (medido)."""
        ts, cached = self._agenda_cache
        if not cached or time.time() - ts > 6 * 3600:
            try:
                cached = cal.build(self.data_dir)
                self._agenda_cache = (time.time(), cached)
            except Exception as e:  # noqa: BLE001 — agenda nunca derruba o ciclo
                cached = cached or {"eventos": [], "fomc_passados": [], "erros": [f"agenda: {e}"]}
        idx = {dt: i for i, dt in enumerate(d.dates)}
        ev = [idx[x] for x in cached.get("fomc_passados", []) if x in idx and 60 < idx[x] < len(d.dates) - 1]
        estudo = None
        if len(ev) >= 12:
            evset = set(ev)
            normal = [abs(d.ret[i]) for i in range(min(ev), len(d.ret)) if i not in evset]
            dia, seg = [d.ret[i] for i in ev], [d.ret[i + 1] for i in ev]
            estudo = {"n": len(ev), "abs_dia": st.mean([abs(x) for x in dia]) * 100, "abs_seguinte": st.mean([abs(x) for x in seg]) * 100,
                      "abs_normal": st.mean(normal) * 100, "pct_alta_dia": sum(1 for x in dia if x > 0) / len(dia),
                      "pct_alta_seguinte": sum(1 for x in seg if x > 0) / len(seg),
                      "maior_queda": min(dia + seg) * 100, "maior_alta": max(dia + seg) * 100}
        today = brt(time.time()).date()
        eventos = []
        for e in cached.get("eventos", []):
            dias = (datetime.strptime(e["data"], "%Y-%m-%d").date() - today).days
            if dias >= 0:
                eventos.append({**e, "dias": dias, "quando": "HOJE" if dias == 0 else "amanhã" if dias == 1 else f"em {dias} dias",
                                "dia_semana": fmt_date(e["data"])})
        return {"eventos": eventos, "fomc_historico": estudo, "erros": cached.get("erros", [])}

    def _delay_text(self) -> str:
        late = sorted({f"{a.name} ({round(self.delays[a.tv] / 60)} min)" for a in ASSETS if self.delays.get(a.tv)})
        intl = ("Mercados internacionais em tempo real" + (", exceto: " + ", ".join(late) if late else "") + ".") if self.delays \
            else "Histórico pelo Yahoo (diário)."
        if self.rt:
            if self.b3_rt:
                acoes = (f" Ações da B3 em tempo real pelo MetaTrader: {self.b3_rt_liq} das {self.b3_liq} ações mais negociadas e "
                         f"{self.b3_rt} papéis no total" + (" (a leitura dos demais continua nas próximas rodadas)."
                                                             if self.b3_rt_liq < self.b3_liq else
                                                             "; os que faltam são papéis sem negócio no último pregão, mostrados com o último dado público."))
            else:
                acoes = " Tabela de ações da B3 com ~15 min de atraso."
            return "WIN e WDO em TEMPO REAL pelo MetaTrader 5 (" + ", ".join(self.rt["simbolos"].values()) + "). " + intl + acoes
        return "WIN, WDO e ações da B3 com ~15 min de atraso (use o MetaTrader 5 para tempo real). " + intl

    def _focus(self) -> dict:
        ts, cached = self._focus_cache
        if cached and time.time() - ts < 6 * 3600:
            return cached
        try:
            f = focus.fetch_focus()
            if f:
                self._focus_cache = (time.time(), f)
                return f
        except Exception:  # noqa: BLE001
            pass
        return cached

    def run_ai(self) -> dict:
        """Opinião do painel sobre os dados atuais (botão "Analisar agora"). Local e gratuita: não chama serviço externo."""
        with self.lock:
            st_now = self.state
        self.ai = {**opinion.build(st_now), "ts": time.time()}
        with self.lock:
            self.state["ia"] = self.ai
        return self.ai

    # ------------------------------------------------------------------ notícias × mercado
    def _enrich_news(self, n: Optional[dict], dd: dict, dres: dict) -> None:
        """Liga cada tema ao mercado: ações atingidas com a variação real do dia e o efeito histórico medido no mini índice."""
        if not n or "WIN" not in dd:
            return
        papeis = {p["t"]: p for p in self.b3.get("papeis", [])}
        d = dd["WIN"]
        cache = self._daily_cache.setdefault("events", {})
        cross = dict(zip(dd["WDO"].dates, dd["WDO"].ret)) if "WDO" in dd else {}
        for w in ("diario", "semanal"):
            for t in n[w]["temas"]:
                imp = news_ai.IMPACT.get(t.get("key", ""))
                if not imp:
                    continue
                ok = tot = 0
                grupos = []
                for lado, sinal in (("ganha", 1), ("perde", -1)):
                    for nome, tickers in imp.get(lado, []):
                        live = [{"t": tk, "c": papeis[tk]["c"]} for tk in tickers if tk in papeis and papeis[tk]["c"] is not None]
                        for x in live:
                            tot += 1
                            ok += 1 if x["c"] * sinal > 0 else 0
                        grupos.append({"lado": lado, "setor": nome, "papeis": live})
                t["impacto"] = {"grupos": grupos, "prazo": imp.get("prazo"), "confirmam": ok, "total": tot}
                px = imp.get("proxy")
                if px:
                    if px not in cache:
                        key, direction = px
                        series = d.ret if key == "SELF" else [cross.get(x, 0.0) for x in d.dates] if key == "CROSS" else d.x.get(key)
                        cache[px] = M.event_study(d, series, direction) if series else None
                    if cache[px]:
                        t["historico"] = {**cache[px], "evento": imp["proxy_nome"]}
        for v in (n.get("analistas") or {}).get("itens", []):
            v["var"] = (papeis.get(v["ticker"]) or {}).get("c") if v.get("ticker") else None
        for c in n.get("empresas", []):
            c["var"] = (papeis.get(c["ticker"]) or {}).get("c")

    @staticmethod
    def _synthesis(s: dict) -> Optional[dict]:
        """Quadro de convergência: quantas frentes independentes apontam para o mesmo lado no mini índice.
        Não é probabilidade — é a contagem do que concorda e do que diverge, para pesar junto com os números."""
        w = s["ativos"].get("WIN")
        if not w:
            return None
        prob = {r["horizonte"]: r for r in w["prob"]}
        P: list[dict] = []

        def add(nome: str, lado: int, detalhe: str) -> None:
            P.append({"pilar": nome, "lado": lado, "detalhe": detalhe})

        for hz in ("Diário", "Semanal"):
            r = prob.get(hz)
            if r:
                add(f"Estatística — {hz.lower()}", 1 if r["p_alta"] >= 0.55 else -1 if r["p_alta"] <= 0.45 else 0,
                    f"{r['p_alta'] * 100:.0f}% alta × {r['p_queda'] * 100:.0f}% queda" + ("" if r["validado"] else " (não validado)"))
        c = w["confronto"]
        novo = bool(c["pos_fechamento"]) and abs(c["pos_fechamento_total"]) >= 0.05
        ext = c["pos_fechamento_total"] if novo else c["implicito"]
        add("Exterior", 1 if ext > 0.15 else -1 if ext < -0.15 else 0,
            f"implica {ext:+.2f}% " + ("desde o fechamento da B3" if novo else "no último pregão"))
        n = s.get("noticias")
        if n:
            sc = n["diario"]["score_ibov"]
            add("Notícias (36 h)", 1 if sc >= 12 else -1 if sc <= -12 else 0, f"score {sc:+d} — {n['diario']['leitura_ibov']}")
            a = n.get("analistas") or {}
            if a.get("total"):
                diff = a["positivas"] - a["negativas"]
                add("Opinião de analistas (manchetes)", 1 if diff >= 3 else -1 if diff <= -3 else 0,
                    f"{a['positivas']} positivas × {a['negativas']} negativas na semana")
        f = s.get("focus") or {}
        rev = {k: (f[k]["itens"][-1].get("revisao") if k in f else None) for k in ("IPCA", "Selic")}
        if rev["IPCA"] is not None and rev["Selic"] is not None:
            lado = -1 if rev["IPCA"] > 0.03 or rev["Selic"] > 0.01 else 1 if rev["IPCA"] < -0.03 and rev["Selic"] <= 0 else 0
            add("Economistas (Focus)", lado, f"em 4 semanas: IPCA {rev['IPCA']:+.2f} p.p., Selic {rev['Selic']:+.2f} p.p. para {f['IPCA']['itens'][-1]['ano']}")
        b = s.get("b3")
        if b and b.get("pct_altas") is not None:
            add("Amplitude da B3", 1 if b["pct_altas"] >= 0.6 else -1 if b["pct_altas"] <= 0.4 else 0,
                f"{b['pct_altas'] * 100:.0f}% das ações em alta; {(b['acima_mm200'] or 0) * 100:.0f}% acima da média de 200")
        lv = w["niveis"]
        if lv["mm20"] and lv["mm200"]:
            up20, up200 = w["preco"] > lv["mm20"], w["preco"] > lv["mm200"]
            add("Tendência técnica", 1 if up20 and up200 else -1 if not up20 and not up200 else 0,
                f"preço {'acima' if up20 else 'abaixo'} da média de 20 e {'acima' if up200 else 'abaixo'} da de 200")
        up, down = sum(1 for x in P if x["lado"] > 0), sum(1 for x in P if x["lado"] < 0)
        if up >= down + 3:
            label, lado = "MAIORIA APONTA ALTA", 1
        elif down >= up + 3:
            label, lado = "MAIORIA APONTA QUEDA", -1
        elif up > down:
            label, lado = "LEVE INCLINAÇÃO DE ALTA", 0
        elif down > up:
            label, lado = "LEVE INCLINAÇÃO DE QUEDA", 0
        else:
            label, lado = "SINAIS DIVIDIDOS", 0
        return {"pilares": P, "alta": up, "queda": down, "neutro": len(P) - up - down, "rotulo": label, "lado": lado}

    def refresh_b3(self) -> list[str]:
        """Tabela da B3: cadastro público (setor, valor de mercado, indicadores; relido a cada ~55 s) com o preço em tempo
        real do MetaTrader por cima, relido a cada ciclo."""
        notes: list[str] = []
        if not self._b3_rows or time.time() - self._b3_scan_ts > 55:
            try:
                self._b3_rows = tv.scan_b3()
                self._b3_scan_ts = time.time()
            except DataError as e:
                if not self._b3_rows:
                    return [f"scanner B3: {e}"]
                notes.append(f"scanner B3: {e} — mantendo o cadastro anterior")
        rows = [dict(r) for r in self._b3_rows]
        self.b3_rt = self.b3_rt_liq = self.b3_liq = 0
        if self.mt5 is not None:
            try:
                liquid = sorted((r for r in rows if r.get("close") is not None and r.get("name")),
                                key=lambda r: -(r.get("Value.Traded") or 0))
                tickers = [r["name"] for r in liquid if r.get("type") == "stock"] + [r["name"] for r in liquid if r.get("type") != "stock"]
                tickers = tickers[:1500]
                seen = sum(1 for t in tickers if t in self.mt5._stocks)
                # enquanto falta ler boa parte da lista, dá mais tempo por rodada (a 1ª leitura de cada papel é a lenta)
                live = self.mt5.stocks(tickers, budget_s=15.0 if seen < 0.8 * len(tickers) else 5.0)
                newest = max((v["ts"] for v in live.values()), default=0)
                for r in rows:
                    is_liq = r.get("type") == "stock" and (r.get("Value.Traded") or 0) >= market_b3.MIN_LIQ
                    self.b3_liq += 1 if is_liq else 0
                    v = live.get(r.get("name"))
                    if not v or newest - v["ts"] > 20 * 3600:      # papel que não negociou no pregão mais recente: mantém o público
                        continue
                    r["close"], r["change"], r["open"], r["high"], r["low"] = v["p"], v["c"], v["o"], v["h"], v["l"]
                    if v["vol"] > 0:
                        r["volume"], r["Value.Traded"] = v["vol"], v["vol"] * v["p"]
                    self.b3_rt += 1
                    self.b3_rt_liq += 1 if is_liq else 0
            except Exception as e:  # noqa: BLE001 — sem MetaTrader a tabela segue com o dado público
                notes.append(f"ações pelo MetaTrader: {e}")
        self.b3 = market_b3.process(rows)
        self.b3["resumo"]["tempo_real"] = self.b3_rt
        self._b3_ts = time.time()
        return notes

    # ------------------------------------------------------------------ blocos do estado
    def _quote(self, a, bars: list[tuple]) -> Optional[dict]:
        if len(bars) < 30:
            return None
        c, pc = bars[-1][4], bars[-2][4]
        chg = (c - pc) * 100 if a.kind == "yield" else (c / pc - 1) * 100   # juros: variação em pontos-base

        def perf(n: int) -> Optional[float]:
            if len(bars) <= n:
                return None
            b = bars[-1 - n][4]
            return (c - b) * 100 if a.kind == "yield" else (c / b - 1) * 100

        return {"key": a.key, "nome": a.name, "grupo": a.group, "tipo": a.kind, "ultimo": c, "var": chg, "var5": perf(5),
                "var21": perf(21), "data": M.bar_date(bars[-1][0]), "fator": a.factor, "porque": a.why,
                "atraso_min": round(self.delays.get(a.tv, 0) / 60),
                "spark": [b[4] for b in bars[-30:]]}

    @staticmethod
    def _levels(bars: list[tuple], complete: bool) -> dict:
        ref = bars[-1] if complete else bars[-2]          # último pregão fechado → pivôs do próximo/atual
        h, l, c = ref[2], ref[3], ref[4]
        p = (h + l + c) / 3
        closes = [b[4] for b in bars]
        sma = lambda n: st.mean(closes[-n:]) if len(closes) >= n else None  # noqa: E731
        return {"pivot": p, "r1": 2 * p - l, "r2": p + (h - l), "s1": 2 * p - h, "s2": p - (h - l),
                "max_ant": h, "min_ant": l, "fech_ant": c, "atr14": st.atr(bars, 14), "rsi14": st.rsi(closes, 14)[-1],
                "mm20": sma(20), "mm50": sma(50), "mm200": sma(200),
                "max20": max(b[2] for b in bars[-20:]), "min20": min(b[3] for b in bars[-20:])}

    @staticmethod
    def _intraday_read(bars: list[tuple]) -> Optional[dict]:
        if len(bars) < 60:
            return None
        closes = [b[4] for b in bars]
        e9, e21 = st.ema(closes[-200:], 9)[-1], st.ema(closes[-200:], 21)[-1]
        i = len(bars) - 1
        while i > 0 and bars[i][0] - bars[i - 1][0] <= 3600:
            i -= 1
        sess = bars[i:]
        vv = sum(b[5] or 1 for b in sess)
        vwap = sum((b[2] + b[3] + b[4]) / 3 * (b[5] or 1) for b in sess) / vv
        c = closes[-1]
        pts = (1 if c > vwap else -1) + (1 if e9 > e21 else -1) + (1 if c > sess[0][1] else -1)
        return {"vwap": vwap, "ema9": e9, "ema21": e21, "rsi": st.rsi(closes[-200:], 14)[-1], "abertura": sess[0][1],
                "max": max(b[2] for b in sess), "min": min(b[3] for b in sess),
                "tendencia": "compradora" if pts >= 2 else "vendedora" if pts <= -2 else "indefinida",
                "acima_vwap": c > vwap, "ema_alta": e9 > e21}

    def _confronto(self, key: str, d: M.DailyData, res: dict) -> dict:
        rows = []
        tot = 0.0
        for a in FACTORS:
            if a.key not in d.x:
                continue
            x, beta = d.x[a.key][-1], res["betas"].get(a.key, 0.0)
            contrib = beta * x
            tot += contrib
            rows.append({"key": a.key, "nome": a.name, "grupo": a.group, "tipo": a.kind,
                         "atraso_min": round(self.delays.get(a.tv, 0) / 60), "var": x * 10000 if a.kind == "yield" else x * 100, "beta": beta, "contrib": contrib * 100,
                         "corr60": st.corr(d.x[a.key][-60:], d.ret[-60:]), "porque": a.why})
        rows.sort(key=lambda r: -abs(r["contrib"]))
        actual = d.ret[-1]
        gap = actual - tot
        z = gap / res["resid_sd"]
        fresh = [{"key": k, "nome": BY_KEY[k].name, "var": v * (10000 if BY_KEY[k].kind == "yield" else 100),
                  "contrib": res["betas"].get(k, 0.0) * v * 100} for k, v in d.fresh.items()]
        nome = "mini índice" if key == "WIN" else "mini dólar"
        if abs(z) < 0.5:
            leitura = f"O {nome} andou em linha com o que o exterior implicava."
        elif gap > 0:
            leitura = (f"O {nome} ficou {gap * 100:+.2f} p.p. ACIMA do que o exterior implicava ({z:+.1f}σ): "
                       "força doméstica (fluxo, notícia local) — ou prêmio que pode ser devolvido se o exterior não acompanhar.")
        else:
            leitura = (f"O {nome} ficou {gap * 100:+.2f} p.p. ABAIXO do que o exterior implicava ({z:+.1f}σ): "
                       "peso doméstico (fiscal, juros, notícia local) — ou atraso que pode ser recuperado.")
        return {"implicito": tot * 100, "realizado": actual * 100, "descolamento": gap * 100, "descolamento_sigma": z,
                "r2": res["r2"], "fatores": rows, "pos_fechamento": fresh,
                "pos_fechamento_total": sum(f["contrib"] for f in fresh), "leitura": leitura, "data": d.dates[-1]}

    def _prob_row(self, name: str, kind: str, model: Optional[dict], row: Optional[list[float]], sigma: float, price: float,
                  alvo: str, shift: float = 0.0, nota: str = "", b80: Optional[dict] = None) -> Optional[dict]:
        if model is None or row is None:
            return None
        ap = M.apply_model(model, row)
        p = ap["p_up"]
        if shift and sigma:
            p = st.clamp(st.phi(st.phi_inv(p) + shift / sigma), 0.03, 0.97)   # informação nova do exterior desloca a média
        ev = model["eval"]
        out = {"horizonte": name, "tipo": kind, "alvo": alvo, "p_alta": p, "p_queda": 1 - p, "base": ap["base"],
               "p_modelo": ap["p_raw"], "k": ev["k"], "oos_n": ev["n"], "oos_acerto": ev["hit"], "oos_skill": ev["skill"],
               "oos_acerto_conv": ev["hit_conv"], "oos_n_conv": ev["n_conv"], "amostra": model["n_train"],
               "motivos": ap["drivers"], "faixas": M.bands(p, sigma), "sigma_pct": sigma * 100,
               "faixa_min": price * (1 - sigma), "faixa_max": price * (1 + sigma), "ref": price, "nota": nota,
               "ajuste_exterior": shift * 100}
        out.update(M.classify(p, ev))
        if b80:
            w = b80["mult"] * sigma
            out.update({"f80_min": price * (1 - w), "f80_max": price * (1 + w), "f80_cobertura": b80["cobertura"], "f80_n": b80["n"]})
        if shift and abs(shift) > 0.25 * sigma:
            out["confianca"] += " · ajustada pelo exterior pós-fechamento"
        return out

    # ------------------------------------------------------------------ ciclo
    def cycle(self) -> dict:
        t0 = self.ciclo_ts = time.time()
        notes: list[str] = []
        self._step("baixando histórico dos mercados internacionais")
        h, n1 = self._history()
        notes += n1
        if self.mt5 is not None or time.time() - self._b3_ts > 55:
            self._step("lendo as ações da B3" + (" no MetaTrader" if self.mt5 is not None else ""))
            notes += self.refresh_b3()
        self._step("lendo notícias")
        news_state, n2 = self._news()
        notes += n2[:2]

        # MetaTrader 5: WIN e WDO em tempo real substituem as séries públicas (que continuam valendo para o exterior)
        self.rt = {}
        if self.mt5 is not None:
            self._step("lendo mini índice e mini dólar no MetaTrader 5")
            try:
                self.rt = self.mt5.snapshot([a.key for a in ASSETS])
                for k, a in TARGETS.items():
                    for tf in ("1D", "5", "15"):
                        h[(a.tv, tf)] = self.rt[(k, tf)]
                for k, bars in self.rt["extras"].items():      # Ibovespa, DI e o que mais a corretora tiver (MT5_<ATIVO>)
                    h[(BY_KEY[k].tv, "1D")] = bars
                    self.delays[BY_KEY[k].tv] = 0
            except Exception as e:  # noqa: BLE001 — sem MetaTrader o painel segue com o dado público
                self.rt = {}
                notes.append(f"MetaTrader 5 indisponível ({e}) — usando cotações públicas com ~15 min de atraso")
        bar_key = (lambda a: f"MT5:{a.key}") if self.rt else (lambda a: a.tv)   # históricos acumulados separados por fonte
        daily = {a.key: h.get((a.tv, "1D"), []) for a in list(TARGETS.values()) + ASSETS}
        intr = {}
        for k, a in TARGETS.items():
            for tf in ("5", "15"):
                bars = h.get((a.tv, tf), [])
                # MetaTrader já entrega 20 mil barras: não precisa acumular no banco local a cada ciclo
                intr[(k, tf)] = bars if self.rt else (self.mem.merge_bars(bar_key(a), tf, bars) if bars else [])
        for tf in ("5", "15"):
            bars = h.get((ES, tf), [])
            sp = (self.rt.get("sp") or {}).get(tf)       # micro S&P da B3 em tempo real, quando o MetaTrader tem
            intr[("ES", tf)] = sp if sp else (self.mem.merge_bars(ES, tf, bars) if bars else [])

        now = time.time()
        last5 = intr[("WIN", "5")][-1][0] if intr[("WIN", "5")] else 0
        # dado público tem ~15 min de atraso: barra de 5 min com menos de 30 min = pregão em andamento (MT5: 12 min)
        is_open = bool(last5 and now - last5 < (12 if self.rt else 30) * 60)
        today = brt(now).strftime("%Y-%m-%d")
        win_last = M.bar_date(daily["WIN"][-1][0])
        complete = not (is_open and win_last == today)

        self._step("treinando os modelos diário e semanal")
        kinds = {a.key: a.kind for a in ASSETS}
        fb = {a.key: daily[a.key] for a in FACTORS}
        dd = {k: M.DailyData(daily[k], fb, kinds) for k in TARGETS if len(daily[k]) > 700}
        if "WIN" not in dd:
            raise DataError("histórico diário insuficiente para o WIN")
        dkey = (win_last, complete, len(dd))
        if self._daily_cache.get("key") != dkey:
            res = {}
            for k, d in dd.items():
                cross = dd.get("WDO" if k == "WIN" else "WIN", d)
                res[k] = M.build_daily(d, daily["VIX"], cross, complete)
            self._daily_cache = {"key": dkey, "res": res}
        elif not complete:   # pregão aberto: mesmos modelos, mas o confronto e os fatores do dia mudam a cada ciclo
            for k, d in dd.items():
                r = self._daily_cache["res"][k]
                cross = dd.get("WDO" if k == "WIN" else "WIN", d)
                r["implied"][-1] = sum(r["betas"][f] * d.x[f][-1] for f in d.factor_keys)
                cmap = dict(zip(cross.dates, cross.ret))
                feats, sig = M.daily_features(d, r["implied"], M.aligned_levels(d, daily["VIX"]), [cmap.get(x, 0.0) for x in d.dates])
                r["feats"][-1], r["sigma"][-1] = feats[-1], sig[-1]
        dres = self._daily_cache["res"]

        self._step("treinando os modelos de 5 min a 1 hora")
        ikey = int(now // 1200)
        if self._intra_cache.get("key") != ikey or not is_open and "res" not in self._intra_cache:
            ires = {}
            for k in TARGETS:
                o = "WDO" if k == "WIN" else "WIN"
                for tf, hz in (("5", {"5 min": 1, "10 min": 2}), ("15", {"30 min": 2, "1 hora": 4})):
                    if intr[(k, tf)]:
                        ires[(k, tf)] = M.build_intraday(intr[(k, tf)], intr[("ES", tf)], intr[(o, tf)], int(tf), hz)
            self._intra_cache = {"key": ikey, "res": ires}
        else:   # modelos em cache: só recalcula os fatores da barra atual
            for (k, tf), r in self._intra_cache["res"].items():
                if r:
                    o = "WDO" if k == "WIN" else "WIN"
                    r["f"] = M.intraday_features(intr[(k, tf)], intr[("ES", tf)], intr[(o, tf)])
        ires = self._intra_cache["res"]

        self._step("montando o quadro de probabilidades")
        opening = self._opening() if not is_open else {}
        ativos = {}
        for k, a in TARGETS.items():
            if k not in dd:
                continue
            d, r = dd[k], dres[k]
            bars = daily[k]
            price = intr[(k, "5")][-1][4] if intr[(k, "5")] else bars[-1][4]
            tick = (self.rt.get("ticks") or {}).get(k)
            if tick and tick["ultimo"]:
                price = tick["ultimo"]
            prev = bars[-2][4]
            conf = self._confronto(k, d, r)
            sig_d = r["sigma"][-1] or st.std(d.ret[-60:])
            shift = conf["pos_fechamento_total"] / 100 if complete else 0.0
            nd, nw = next_sessions(d.dates[-1], 1), next_sessions(d.dates[-1], 5)
            rows = []
            base_ts = intr[(k, "5")][-1][0] if intr[(k, "5")] else now
            for tf in ("5", "15"):
                ir = ires.get((k, tf))
                if not ir:
                    continue
                f = ir["f"]
                for name, (tfm, hb) in M.HORIZONS_I.items():
                    if str(tfm) != tf or name not in ir["models"]:
                        continue
                    sg = (f["sigma"][-1] or 0.0005) * math.sqrt(hb)
                    alvo = brt(f["ts"][-1] + tfm * 60 * (hb + 1)).strftime("%H:%M") if is_open else "próxima sessão"
                    rows.append(self._prob_row(name, "intraday", ir["models"][name], f["feats"][-1], sg, price, alvo,
                                               nota="" if is_open else "mercado fechado: leitura da última barra negociada",
                                               b80=ir["band80"].get(name)))
            if not complete:
                # fecho de hoje × fechamento ANTERIOR: tabela empírica "o dia já andou z → fecha do mesmo lado em x %"
                t = brt(now)
                frac = st.clamp(1 - ((t.hour * 60 + t.minute) - 540) / 565.0, 0.03, 1.0)
                r_now = price / prev - 1
                per = M.close_persistence(intr[(k, "15")])
                z = r_now / ((per["sd"] if per else sig_d) * math.sqrt(frac))
                b = next((x for x in per["bins"] if abs(z) < x["ate"]), None) if per else None
                hit = b["acerto"] if b and b["n"] >= 30 and b["acerto"] is not None else st.phi(abs(z))
                p_close = st.clamp(hit if z >= 0 else 1 - hit, 0.02, 0.98)
                sg = sig_d * math.sqrt(frac)
                rows.append({"horizonte": "Fecho de hoje", "tipo": "sessao", "alvo": "fechar acima/abaixo do fechamento anterior",
                             "p_alta": p_close, "p_queda": 1 - p_close, "base": 0.5, "k": None, "oos_n": b["n"] if b else None,
                             "oos_acerto": b["acerto"] if b else None, "amostra": None, "motivos": [], "faixas": M.bands(p_close, sg),
                             "sigma_pct": sg * 100, "faixa_min": price * (1 - sg), "faixa_max": price * (1 + sg), "ref": prev,
                             "sinal": M.classify(p_close, {})["sinal"], "validado": bool(b and b["n"] >= 30),
                             "confianca": "frequência histórica: quando o dia já andou isso, fechou do mesmo lado nessa proporção",
                             "nota": "referência é o fechamento ANTERIOR, não o preço atual — não indica para onde vai a partir de agora",
                             "ajuste_exterior": 0.0})
            rows.append(self._prob_row("Diário", "diario", r["models"][1], r["feats"][-1], sig_d, bars[-1][4],
                                       fmt_date(nd if complete else next_sessions(today, 1)), shift, b80=r["band80"].get(1)))
            rows.append(self._prob_row("Semanal", "semanal", r["models"][5], r["feats"][-1], sig_d * math.sqrt(5), bars[-1][4],
                                       "até " + fmt_date(nw), shift, b80=r["band80"].get(5)))
            if not is_open and bars5_ok(intr[(k, "5")]):
                orow = self._open_row(k, opening, intr[(k, "5")][-1][0] + 300, intr[(k, "5")][-1][4], "abertura de " + fmt_date(nd) + " × fechamento anterior")
                if orow:
                    rows.insert(0, orow)
            rows = [x for x in rows if x]
            # memória: grava previsões e resolve as vencidas
            self._remember(k, rows, d, intr[(k, "5")], base_ts, is_open, complete)
            lv = self._levels(bars, complete)
            ativos[k] = {
                "key": k, "nome": a.name, "preco": price, "fech_ant": prev if not complete else bars[-2][4],
                "var": (price / (prev if not complete else bars[-2][4]) - 1) * 100,
                "max": bars[-1][2], "min": bars[-1][3], "abertura": bars[-1][1], "data": d.dates[-1],
                "prob": rows, "confronto": conf, "niveis": lv, "intraday": self._intraday_read(intr[(k, "5")]),
                "analogo": r["analog"], "sigma_dia_pct": sig_d * 100,
                "tendencias": T.trends(bars, intr[(k, "5")]), "reversao": T.reversal_read(intr[(k, "5")]),
                "candles": {"D1": [list(b[:5]) for b in bars[-180:]],
                            "M15": [list(b[:5]) for b in intr[(k, "15")][-160:]],
                            "M5": [list(b[:5]) for b in intr[(k, "5")][-160:]]},
            }

        self._step("cruzando notícias, agenda e alertas")
        self._enrich_news(news_state, dd, dres)
        quotes = [q for q in (self._quote(a, daily[a.key]) for a in ASSETS) if q]
        ny = self._brasil_ny(quotes)
        state = {
            "ok": True, "atualizado": now, "duracao": round(time.time() - t0, 2), "mercado_aberto": is_open,
            "pregao": win_last, "pregao_fechado": complete, "ativos": ativos, "cotacoes": quotes, "brasil_ny": ny,
            "b3": self.b3["resumo"], "noticias": news_state, "bcb": self._bcb(), "focus": self._focus(),
            "ia": self.ai, "placar": self.mem.scoreboard(),
            "historico": self.mem.recent(), "avisos": notes,
            "abertura_estudo": (opening.get("estudo") if opening else (self._open_cache[1] or {}).get("estudo")),
            "tempo_real": bool(self.rt), "simbolos_mt5": self.rt.get("simbolos"), "atraso": self._delay_text(),
            "aviso_legal": "Probabilidades estatísticas calculadas sobre histórico real e validadas fora da amostra. "
                           "Não são garantia de movimento nem recomendação de investimento. O painel não envia ordens.",
        }
        self._audit_daily = (daily["WIN"], {**fb}, kinds)
        self._audit_bars = {k: daily[k] for k in TARGETS}      # insumos para a auditoria refazer os estudos de choque
        if "WDO" in dd:      # dólar local como "choque" para o mini índice (não entra na regressão do exterior)
            cm = dict(zip(dd["WDO"].dates, dd["WDO"].ret))
            dd["WIN"].x["CROSS"] = [cm.get(x, 0.0) for x in dd["WIN"].dates]
        al = alerts.from_rows(ativos)
        ev_cache = self._daily_cache.setdefault("shocks", {})
        for k, d in dd.items():
            al += alerts.from_shocks(k, TARGETS[k].name, d, is_open or bool(d.fresh), ev_cache)
        for k, a in ativos.items():
            rv = a.get("reversao")
            h = rv["historico"]["30 min"] if rv else None
            if rv and h["acerto"] is not None and h["n"]:
                rv["comprovado"] = h["acerto"] - 1.96 * math.sqrt(h["acerto"] * (1 - h["acerto"]) / h["n"]) > 0.5
            if rv and rv["sinal"] and is_open and rv.get("comprovado"):
                al.append({"nivel": "REVERSÃO", "lado": rv["sinal"], "ativo": k, "prob": h["acerto"] or 0.0, "horizonte": "reversão intraday",
                           "titulo": f"{a['nome']}: {rv['rotulo']}",
                           "texto": f"RSI {rv['rsi']:.0f}, {rv['vwap_z']:+.1f} desvios do VWAP. Nos últimos {h['n']} sinais iguais, o preço foi na "
                                    f"direção do sinal em {(h['acerto'] or 0) * 100:.0f}% das vezes em 30 min.",
                           "chave": f"{k}|rev|{int(rv['ultimo_ts']) // 1800}"})
        agenda = self._agenda(dd["WIN"])
        notes += agenda["erros"][:2]
        for e in agenda["eventos"]:
            if e["dias"] <= 1 and e["impacto"] == "muito alto":
                h = agenda["fomc_historico"] if e["tipo"] == "fomc" else None
                extra = (f" Nos {h['n']} dias de decisão do Fed desde 2021, o mini índice oscilou em média {h['abs_dia']:.2f}% "
                         f"(contra {h['abs_normal']:.2f}% num dia comum).") if h else ""
                al.append({"nivel": "AGENDA", "lado": 0, "ativo": "WIN", "prob": 0.0, "horizonte": "evento",
                           "titulo": f"{e['quando'].upper()} ({e['dia_semana']}, {e['hora']}): {e['evento']}",
                           "texto": "Evento de impacto muito alto: a direção só se define depois do anúncio e as faixas de preço "
                                    "do quadro tendem a ser estreitas demais nesse dia." + extra,
                           "chave": f"agenda|{e['tipo']}|{e['data']}"})
        state["agenda"] = agenda
        al.sort(key=lambda x: (x["nivel"] != "ALERTA", x["nivel"] != "AGENDA", -x["prob"]))
        for x in al:
            if x["nivel"] == "ALERTA" and x["chave"] not in self._alerts_sent:
                self._alerts_sent.add(x["chave"])
                alerts.send_telegram(f"🚨 B3 AI — {x['titulo']}\n{x['texto']}\nProbabilidade histórica, não garantia.")
        state["alertas"] = al
        state["limiares"] = {"alerta": alerts.ALERTA, "atencao": alerts.ATENCAO, "telegram": all(alerts.telegram_config())}
        state["sintese"] = self._synthesis(state)
        state["leitura"] = self._narrative(state)
        with self.lock:
            self.state = state
        self._step("aguardando o próximo ciclo")
        if self.ciclos == 0:
            print(f"[b3-ai] primeira leitura concluída em {time.time() - t0:.0f}s", flush=True)
        self.ciclos += 1
        return state

    def _brasil_ny(self, quotes: list[dict]) -> list[dict]:
        q = {x["key"]: x for x in quotes}
        fx = q.get("USDBRL", {}).get("var")
        papeis = {p["t"]: p for p in self.b3.get("papeis", [])}
        out = []
        for adr, local, lname in (("EWZ", "IBOV", "Ibovespa"), ("PBR", "PETR3", "PETR3"), ("VALE_ADR", "VALE3", "VALE3")):
            if adr not in q:
                continue
            lv = q["IBOV"]["var"] if local == "IBOV" and "IBOV" in q else (papeis.get(local) or {}).get("c")
            if lv is None or fx is None:
                continue
            usd = ((1 + lv / 100) / (1 + fx / 100) - 1) * 100
            out.append({"ny": q[adr]["nome"], "var_ny": q[adr]["var"], "local": lname, "var_local": lv, "local_em_usd": usd,
                        "diferenca": q[adr]["var"] - usd})
        return out

    def _remember(self, k: str, rows: list[dict], d: M.DailyData, bars5: list[tuple], base_ts: float, is_open: bool, complete: bool) -> None:
        closes = dict(zip(d.dates, d.close))
        last_done = d.dates[-1] if complete else d.dates[-2]
        for row in rows:
            hz = row["horizonte"]
            if row["tipo"] in ("diario", "semanal"):
                n = 1 if hz == "Diário" else 5
                if complete:   # só registra com o pregão-base fechado; atualiza até o próximo abrir
                    self.mem.record(k, hz, d.dates[-1], d.close[-1], row["p_alta"], replace=True)
                done_idx = d.dates.index(last_done)
                for due, ref, _p in self.mem.pending(k, hz):
                    if due in closes and d.dates.index(due) + n <= done_idx:
                        self.mem.resolve(k, hz, due, d.close[d.dates.index(due) + n], ref)
            elif row["tipo"] == "intraday" and is_open and bars5:
                self.mem.record(k, hz, str(int(base_ts)), bars5[-1][4], row["p_alta"])
        if bars5:
            by_ts = {int(b[0]): b[4] for b in bars5[-400:]}
            last_ts = int(bars5[-1][0])
            for hz, (tfm, hb) in M.HORIZONS_I.items():
                for due, ref, _p in self.mem.pending(k, hz):
                    target = int(due) + tfm * 60 * hb
                    if target in by_ts and target < last_ts:
                        self.mem.resolve(k, hz, due, by_ts[target], ref)
                    elif last_ts - target > 6 * 3600:
                        self.mem.resolve(k, hz, due, ref, ref)   # sem barra no alvo (fim de pregão): descarta como empate

    @staticmethod
    def _narrative(s: dict) -> list[str]:
        out: list[str] = []
        w = s["ativos"].get("WIN")
        if not w:
            return out
        prob = {r["horizonte"]: r for r in w["prob"]}
        d, wk = prob.get("Diário"), prob.get("Semanal")
        c = w["confronto"]
        out.append(f"Exterior × Brasil: os mercados internacionais implicavam {c['implicito']:+.2f}% para o mini índice no pregão de "
                   f"{fmt_date(c['data'])}; ele fez {c['realizado']:+.2f}%. {c['leitura']}")
        if c["fatores"]:
            out.append("Maiores pesos do exterior: " + "; ".join(
                (f"{x['nome']} {x['var']:+.1f} pb" if x["tipo"] == "yield" else f"{x['nome']} {x['var']:+.2f}%")
                + f" → {x['contrib']:+.2f} p.p." for x in c["fatores"][:3]) + ".")
        if c["pos_fechamento"] and abs(c["pos_fechamento_total"]) >= 0.05:
            out.append(f"Depois do fechamento da B3 o exterior já andou e implica {c['pos_fechamento_total']:+.2f}% na abertura "
                       "— esse ajuste já está nas probabilidades diária e semanal.")
        if d:
            out.append(f"Mini índice — próximo pregão ({d['alvo']}): {d['p_alta'] * 100:.0f}% de alta × {d['p_queda'] * 100:.0f}% de queda "
                       f"→ {d['sinal']} ({d['confianca']}; acerto fora da amostra {((d['oos_acerto'] or 0) * 100):.1f}% em {d['oos_n']} pregões).")
        if wk:
            out.append(f"Semana ({wk['alvo']}): {wk['p_alta'] * 100:.0f}% de alta × {wk['p_queda'] * 100:.0f}% de queda → {wk['sinal']}.")
        b = s.get("b3")
        if b and b.get("pct_altas") is not None:
            out.append(f"Mercado à vista: {b['altas']} altas × {b['quedas']} quedas entre {b['liquidas']} ações líquidas "
                       f"({b['pct_altas'] * 100:.0f}% subindo); {((b['acima_mm200'] or 0) * 100):.0f}% acima da média de 200 dias. "
                       + (f"Setor mais forte: {b['setores'][0]['setor']} ({b['setores'][0]['var']:+.2f}%); mais fraco: "
                          f"{b['setores'][-1]['setor']} ({b['setores'][-1]['var']:+.2f}%)." if b["setores"] else ""))
        n = s.get("noticias")
        if n:
            dn = n["diario"]
            temas = "; ".join(t["tema"] for t in dn["temas"][:3]) or "sem tema dominante"
            out.append(f"Notícias (últimas 36 h): viés {dn['leitura_ibov']} para a bolsa (score {dn['score_ibov']:+d}) e "
                       f"{dn['leitura_dolar']} para o real. Temas: {temas}.")
        x = s["ativos"].get("WDO")
        if x:
            dx = {r["horizonte"]: r for r in x["prob"]}.get("Diário")
            if dx:
                out.append(f"Mini dólar — próximo pregão: {dx['p_alta'] * 100:.0f}% de alta × {dx['p_queda'] * 100:.0f}% de queda → {dx['sinal']}.")
        return out
