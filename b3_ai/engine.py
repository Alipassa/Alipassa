"""Motor do B3 AI: coleta dados reais, roda os modelos e monta o estado (JSON) do painel e do relatório."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from . import market_b3, model as M, news_ai, stats as st
from .data import bcb, news, tradingview as tv, yahoo
from .data.http import DataError
from .memory import Memory
from .universe import ASSETS, BY_KEY, FACTORS, TARGETS

DAILY_BARS = 1800
INTRA_BARS = 5000
ES = BY_KEY["ES"].tv
WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


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
    def __init__(self, data_dir: str = "dados", with_news: bool = True) -> None:
        self.mem = Memory(f"{data_dir}/b3_ai.db")
        self.with_news = with_news
        self.lock = threading.Lock()
        self.state: dict = {"ok": False, "erro": "primeira leitura em andamento…"}
        self.b3: dict = {"resumo": None, "papeis": []}
        self._daily_cache: dict = {}
        self._intra_cache: dict = {}
        self._news_cache: tuple[float, Optional[dict], list[str]] = (0.0, None, [])
        self._bcb_cache: tuple[float, dict] = (0.0, {})
        self._b3_ts = 0.0

    # ------------------------------------------------------------------ coleta
    def _history(self) -> tuple[dict, list[str]]:
        notes: list[str] = []
        reqs = [(a.tv, "1D", DAILY_BARS) for a in list(TARGETS.values()) + ASSETS]
        reqs += [(TARGETS[k].tv, tf, INTRA_BARS) for k in TARGETS for tf in ("5", "15")] + [(ES, "5", INTRA_BARS), (ES, "15", INTRA_BARS)]
        try:
            h = tv.fetch_history(reqs)
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

    def refresh_b3(self) -> list[str]:
        try:
            self.b3 = market_b3.process(tv.scan_b3())
            self._b3_ts = time.time()
            return []
        except DataError as e:
            return [f"scanner B3: {e}"]

    # ------------------------------------------------------------------ blocos do estado
    @staticmethod
    def _quote(a, bars: list[tuple]) -> Optional[dict]:
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
                         "var": x * 10000 if a.kind == "yield" else x * 100, "beta": beta, "contrib": contrib * 100,
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
                  alvo: str, shift: float = 0.0, nota: str = "") -> Optional[dict]:
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
        if shift and abs(shift) > 0.25 * sigma:
            out["confianca"] += " · ajustada pelo exterior pós-fechamento"
        return out

    # ------------------------------------------------------------------ ciclo
    def cycle(self) -> dict:
        t0 = time.time()
        notes: list[str] = []
        h, n1 = self._history()
        notes += n1
        if time.time() - self._b3_ts > 55:
            notes += self.refresh_b3()
        news_state, n2 = self._news()
        notes += n2[:2]

        daily = {a.key: h.get((a.tv, "1D"), []) for a in list(TARGETS.values()) + ASSETS}
        intr = {}
        for k, a in TARGETS.items():
            for tf in ("5", "15"):
                bars = h.get((a.tv, tf), [])
                intr[(k, tf)] = self.mem.merge_bars(a.tv, tf, bars) if bars else []
        for tf in ("5", "15"):
            bars = h.get((ES, tf), [])
            intr[("ES", tf)] = self.mem.merge_bars(ES, tf, bars) if bars else []

        now = time.time()
        last5 = intr[("WIN", "5")][-1][0] if intr[("WIN", "5")] else 0
        # dado público tem ~15 min de atraso: barra de 5 min com menos de 30 min = pregão em andamento
        is_open = bool(last5 and now - last5 < 30 * 60)
        today = brt(now).strftime("%Y-%m-%d")
        win_last = M.bar_date(daily["WIN"][-1][0])
        complete = not (is_open and win_last == today)

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

        ativos = {}
        for k, a in TARGETS.items():
            if k not in dd:
                continue
            d, r = dd[k], dres[k]
            bars = daily[k]
            price = intr[(k, "5")][-1][4] if intr[(k, "5")] else bars[-1][4]
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
                                               nota="" if is_open else "mercado fechado: leitura da última barra negociada"))
            if not complete:
                # fechamento de hoje: combina o que já andou com o que o exterior implica para o resto do pregão
                t = brt(now)
                frac = st.clamp(1 - ((t.hour * 60 + t.minute) - 540) / 565.0, 0.03, 1.0)
                r_now = price / prev - 1
                mu = r_now + 0.5 * frac * (conf["implicito"] / 100 - r_now)
                p_close = st.clamp(st.phi(mu / (sig_d * math.sqrt(frac))), 0.03, 0.97)
                rows.append({"horizonte": "Fecho de hoje", "tipo": "sessao", "alvo": "18:25 vs. fechamento anterior", "p_alta": p_close,
                             "p_queda": 1 - p_close, "base": 0.5, "k": None, "oos_n": None, "oos_acerto": None, "amostra": None,
                             "motivos": [], "faixas": M.bands(p_close, sig_d * math.sqrt(frac)), "sigma_pct": sig_d * math.sqrt(frac) * 100,
                             "faixa_min": price * (1 - sig_d * math.sqrt(frac)), "faixa_max": price * (1 + sig_d * math.sqrt(frac)),
                             "ref": prev, "sinal": M.classify(p_close, {})["sinal"], "validado": False,
                             "confianca": "cálculo direto: variação do dia + exterior + volatilidade restante (não validado em walk-forward)",
                             "nota": "", "ajuste_exterior": 0.0})
            rows.append(self._prob_row("Diário", "diario", r["models"][1], r["feats"][-1], sig_d, bars[-1][4],
                                       fmt_date(nd if complete else next_sessions(today, 1)), shift))
            rows.append(self._prob_row("Semanal", "semanal", r["models"][5], r["feats"][-1], sig_d * math.sqrt(5), bars[-1][4],
                                       "até " + fmt_date(nw), shift))
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
                "candles": {"D1": [list(b[:5]) for b in bars[-180:]],
                            "M15": [list(b[:5]) for b in intr[(k, "15")][-160:]],
                            "M5": [list(b[:5]) for b in intr[(k, "5")][-160:]]},
            }

        quotes = [q for q in (self._quote(a, daily[a.key]) for a in ASSETS) if q]
        ny = self._brasil_ny(quotes)
        state = {
            "ok": True, "atualizado": now, "duracao": round(time.time() - t0, 2), "mercado_aberto": is_open,
            "pregao": win_last, "pregao_fechado": complete, "ativos": ativos, "cotacoes": quotes, "brasil_ny": ny,
            "b3": self.b3["resumo"], "noticias": news_state, "bcb": self._bcb(), "placar": self.mem.scoreboard(),
            "historico": self.mem.recent(), "avisos": notes,
            "atraso": "Cotações públicas com ~15 min de atraso (B3 e bolsas dos EUA).",
            "aviso_legal": "Probabilidades estatísticas calculadas sobre histórico real e validadas fora da amostra. "
                           "Não são garantia de movimento nem recomendação de investimento. O painel não envia ordens.",
        }
        state["leitura"] = self._narrative(state)
        with self.lock:
            self.state = state
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
        if c["pos_fechamento"]:
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
