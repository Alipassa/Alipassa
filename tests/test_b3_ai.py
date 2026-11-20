"""B3 AI — testes offline (sem rede): estatística, modelos, notícias, mercado, relatório e memória."""

import math
import os
import random
import tempfile
import unittest

from b3_ai import market_b3, model as M, news_ai, report, stats as st
from b3_ai.data import news as news_data, yahoo
from b3_ai.memory import Memory

DAY = 86400


def synth_daily(n=900, seed=1, drift=0.0, lead=None, beta=0.0, start=1_600_000_000):
    """Barras diárias sintéticas; com `lead`, o retorno acompanha o de outra série (beta) no mesmo dia."""
    rnd = random.Random(seed)
    bars, c = [], 100.0
    for i in range(n):
        r = rnd.gauss(drift, 0.012)
        if lead is not None and i > 0:
            r += beta * (lead[i][4] / lead[i - 1][4] - 1)
        o = c
        c = c * (1 + r)
        bars.append((start + i * DAY, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1000.0))
    return bars


class StatsTest(unittest.TestCase):
    def test_phi_inverse_roundtrip(self):
        for p in (0.03, 0.2, 0.5, 0.77, 0.97):
            self.assertAlmostEqual(st.phi(st.phi_inv(p)), p, places=6)

    def test_ridge_recovers_coefficients(self):
        rnd = random.Random(3)
        xs = [[rnd.gauss(0, 1), rnd.gauss(0, 1)] for _ in range(500)]
        ys = [2 * a - 3 * b + rnd.gauss(0, 0.01) for a, b in xs]
        b = st.ridge(xs, ys, lam=1e-6)
        self.assertAlmostEqual(b[0], 2, places=2)
        self.assertAlmostEqual(b[1], -3, places=2)

    def test_logistic_learns_signal(self):
        rnd = random.Random(5)
        xs = [[rnd.gauss(0, 1)] for _ in range(1500)]
        ys = [1 if rnd.random() < 1 / (1 + math.exp(-1.5 * x[0])) else 0 for x in xs]
        w = st.logistic_fit(xs, ys, lam=0.1)
        self.assertGreater(w[1], 1.0)
        self.assertGreater(st.logistic_predict(w, [2.0]), 0.85)

    def test_rsi_bounds(self):
        self.assertEqual(st.rsi([float(i) for i in range(1, 40)])[-1], 100.0)
        self.assertLess(st.rsi([float(100 - i) for i in range(40)])[-1], 1.0)


class CalibrationTest(unittest.TestCase):
    def test_noise_model_shrinks_to_base_rate(self):
        """Sem sinal real, a calibração fora da amostra precisa devolver ~taxa-base (não inventar probabilidade)."""
        rnd = random.Random(7)
        xs = [[rnd.gauss(0, 1) for _ in range(6)] for _ in range(900)]
        ys = [1 if rnd.random() < 0.5 else 0 for _ in xs]
        m = M.fit_calibrated(xs, ys, [f"f{i}" for i in range(6)], lam=5.0)
        p = M.apply_model(m, [3, -3, 3, -3, 3, -3])["p_up"]
        self.assertLess(abs(p - m["base"]), 0.06)
        self.assertFalse(M.classify(p, m["eval"])["validado"])

    def test_real_signal_is_kept(self):
        rnd = random.Random(8)
        xs = [[rnd.gauss(0, 1)] for _ in range(1200)]
        ys = [1 if rnd.random() < 1 / (1 + math.exp(-1.2 * x[0])) else 0 for x in xs]
        m = M.fit_calibrated(xs, ys, ["sinal"], lam=1.0)
        self.assertGreater(m["eval"]["k"], 0.6)
        self.assertGreater(m["eval"]["hit"], 0.6)
        self.assertGreater(M.apply_model(m, [2.0])["p_up"], 0.75)
        self.assertTrue(M.classify(0.8, m["eval"])["validado"])

    def test_bands_sum_to_one(self):
        b = M.bands(0.62, 0.01)
        self.assertAlmostEqual(b["alta_forte"] + b["alta"] + b["queda"] + b["queda_forte"], 1.0, places=9)
        self.assertAlmostEqual(b["alta_forte"] + b["alta"], 0.62, places=9)


class DailyModelTest(unittest.TestCase):
    def test_fair_value_recovers_beta_and_date_mapping(self):
        lead = synth_daily(seed=11)
        tgt = synth_daily(seed=12, lead=lead, beta=0.8)
        d = M.DailyData(tgt, {"ES": lead}, {"ES": "pct"})
        implied, betas, r2 = M.rolling_betas(d)
        self.assertAlmostEqual(betas["ES"], 0.8, delta=0.2)
        self.assertGreater(r2, 0.2)
        res = M.build_daily(d, synth_daily(seed=13), d, True)
        self.assertIn(1, res["models"])
        self.assertIsNotNone(res["feats"][-1])
        # futuro americano abre na noite anterior: 23:00 UTC de quinta pertence ao pregão de sexta
        self.assertEqual(M.bar_date(1794524400.0), "2026-11-13")

    def test_fresh_foreign_info_after_local_close(self):
        lead = synth_daily(seed=21)
        tgt = synth_daily(seed=22)[:-1]          # o exterior tem um dia a mais que o alvo
        d = M.DailyData(tgt, {"ES": lead}, {"ES": "pct"})
        self.assertAlmostEqual(d.fresh["ES"], lead[-1][4] / lead[-2][4] - 1, places=9)


class IntradayModelTest(unittest.TestCase):
    def test_builds_models_per_horizon(self):
        rnd = random.Random(31)
        bars, c, ts = [], 100000.0, 1_700_000_000
        for day in range(12):
            for i in range(108):
                o = c
                c *= 1 + rnd.gauss(0, 0.0006)
                bars.append((ts + day * DAY + i * 300, o, max(o, c) + 5, min(o, c) - 5, c, 100.0))
        r = M.build_intraday(bars, bars, bars, 5, {"5 min": 1, "10 min": 2})
        self.assertEqual(set(r["models"]), {"5 min", "10 min"})
        self.assertEqual(len(r["f"]["feats"][-1]), len(M.FEATURES_I))
        p = M.apply_model(r["models"]["5 min"], r["f"]["feats"][-1])["p_up"]
        self.assertTrue(0.3 < p < 0.7)       # passeio aleatório: nada de probabilidade extrema


class NewsTest(unittest.TestCase):
    def test_classify_direction(self):
        k = [c[0] for c in news_ai.classify("Fed signals rate hike in December as inflation stays hot")]
        self.assertIn("fed_duro", k)
        k = [c[0] for c in news_ai.classify("Copom corta Selic em 0,5 ponto")]
        self.assertIn("copom_corte", k)
        self.assertEqual(news_ai.classify("Mega-Sena acumula e vai a R$ 34 milhões"), [])

    def test_opposite_polarity_cancels(self):
        k = [c[0] for c in news_ai.classify("Petróleo sobe após cair na véspera")]
        self.assertNotIn("petroleo_alta", k)
        self.assertNotIn("petroleo_queda", k)

    def test_scores_follow_headlines(self):
        now = 1_800_000_000.0
        mk = lambda t, sc: {"title": t, "source": "x", "link": "", "ts": now - 3600, "scope": sc}  # noqa: E731
        bad = news_ai.analyze([mk("Fed raises rates again, hawkish Powell", "INT"), mk("Risco fiscal aumenta com rombo nas contas públicas", "BR")], now)
        good = news_ai.analyze([mk("Fed cuts rates, dovish Powell", "INT"), mk("Estrangeiros: entrada recorde de fluxo na bolsa", "BR")], now)
        self.assertLess(bad["diario"]["score_ibov"], 0)
        self.assertGreater(bad["diario"]["score_dolar"], 0)
        self.assertGreater(good["diario"]["score_ibov"], 0)

    def test_rss_parser(self):
        xml = ("<rss><channel><item><title>Ibovespa sobe 1% - Valor</title><link>https://x/y</link>"
               "<pubDate>Sun, 15 Nov 2026 11:36:52 GMT</pubDate><source url='u'>Valor</source></item></channel></rss>")
        it = news_data.parse_rss(xml, "BR")
        self.assertEqual(it[0]["title"], "Ibovespa sobe 1%")
        self.assertEqual(it[0]["source"], "Valor")


class MarketTest(unittest.TestCase):
    def test_breadth_and_fractional_dedup(self):
        row = lambda n, c, v, sec="Finance": {"name": n, "description": n, "close": 10.0, "change": c, "Value.Traded": v,  # noqa: E731
                                              "sector": sec, "type": "stock", "subtype": "common", "SMA50": 9.0, "SMA200": 11.0}
        out = market_b3.process([row("AAAA3", 2.0, 5e6), row("AAAA3F", 2.0, 5e6), row("BBBB4", -1.0, 9e6), row("CCCC3", 5.0, 100.0)])
        s = out["resumo"]
        self.assertEqual(len(out["papeis"]), 3)             # fracionário removido
        self.assertEqual((s["altas"], s["quedas"], s["liquidas"]), (1, 1, 2))   # ilíquido fora da amplitude
        self.assertEqual(s["setores"][0]["setor"], "Financeiro")


class MemoryTest(unittest.TestCase):
    def test_bars_accumulate_and_scoreboard(self):
        with tempfile.TemporaryDirectory() as d:
            m = Memory(os.path.join(d, "t.db"))
            m.merge_bars("W", "5", [(1.0, 1, 1, 1, 1, 1), (2.0, 1, 1, 1, 2, 1)])
            self.assertEqual(len(m.merge_bars("W", "5", [(2.0, 1, 1, 1, 3, 1), (3.0, 1, 1, 1, 4, 1)])), 3)
            m.record("WIN", "Diário", "2026-01-02", 100.0, 0.6)
            m.record("WIN", "Diário", "2026-01-03", 100.0, 0.4)
            m.resolve("WIN", "Diário", "2026-01-02", 101.0, 100.0)   # previu alta, subiu → acerto
            m.resolve("WIN", "Diário", "2026-01-03", 101.0, 100.0)   # previu queda, subiu → erro
            sb = m.scoreboard()[0]
            self.assertEqual((sb["n"], sb["acerto"]), (2, 0.5))
            m.close()


class YahooTest(unittest.TestCase):
    def test_parse_chart_skips_nulls(self):
        p = {"chart": {"result": [{"timestamp": [1, 2], "indicators": {"quote": [{"open": [1, None], "high": [2, 2], "low": [1, 1],
                                                                                       "close": [1.5, 2], "volume": [10, 10]}]}}]}}
        self.assertEqual(yahoo.parse_chart(p), [(1.0, 1.0, 2.0, 1.0, 1.5, 10.0)])


class ReportTest(unittest.TestCase):
    def test_report_renders_big_numbers(self):
        row = {"horizonte": "Diário", "tipo": "diario", "alvo": "seg 16/11", "p_alta": 0.58, "p_queda": 0.42, "base": 0.51, "k": 0.7,
               "oos_n": 800, "oos_acerto": 0.54, "oos_acerto_conv": 0.56, "oos_n_conv": 100, "amostra": 1400, "motivos": [],
               "faixas": M.bands(0.58, 0.02), "sigma_pct": 2.0, "faixa_min": 170000.0, "faixa_max": 178000.0, "ref": 174000.0,
               "nota": "", "ajuste_exterior": 0.0, "sinal": "VIÉS DE ALTA", "validado": True, "confianca": "validada"}
        lv = {k: 1.0 for k in ("pivot", "r1", "r2", "s1", "s2", "max_ant", "min_ant", "fech_ant", "atr14", "rsi14", "mm20", "mm50", "mm200", "max20", "min20")}
        conf = {"implicito": 0.1, "realizado": 0.7, "descolamento": 0.6, "descolamento_sigma": 0.4, "r2": 0.12, "fatores": [],
                "pos_fechamento": [], "pos_fechamento_total": 0.0, "leitura": "ok", "data": "2026-11-13"}
        s = {"atualizado": 1794756548.0, "pregao": "2026-11-13", "mercado_aberto": False, "atraso": "15 min", "leitura": ["x"],
             "ativos": {"WIN": {"nome": "Mini Índice (WIN)", "preco": 174230.0, "var": 0.76, "max": 1.0, "min": 1.0, "sigma_dia_pct": 2.4,
                                "prob": [row], "confronto": conf, "niveis": lv, "intraday": None}},
             "cotacoes": [], "brasil_ny": [], "b3": None, "bcb": {}, "noticias": None, "placar": [], "aviso_legal": "aviso"}
        md = report.markdown(s)
        self.assertIn("**58%**", md)
        self.assertIn("**42%**", md)
        self.assertIn("<table>", report.to_html(s))


if __name__ == "__main__":
    unittest.main()


class NewFeaturesTest(unittest.TestCase):
    """Faixa de 80 %, persistência do dia, estudo de evento, alertas, agenda, tendência e reversão."""

    def test_band80_covers_about_80_percent(self):
        rnd = random.Random(41)
        b = M.band80([abs(rnd.gauss(0, 1)) for _ in range(4000)])
        self.assertAlmostEqual(b["mult"], 1.2816, delta=0.08)
        self.assertAlmostEqual(b["cobertura"], 0.80, delta=0.04)

    def test_event_study_measures_shock_days(self):
        lead = synth_daily(seed=51)
        tgt = synth_daily(seed=52, lead=lead, beta=1.5)
        d = M.DailyData(tgt, {"ES": lead}, {"ES": "pct"})
        ev = M.event_study(d, d.x["ES"], -1)
        self.assertGreater(ev["n"], 15)
        self.assertLess(ev["dia_pct_alta"], 0.25)       # exterior desaba → alvo cai no mesmo dia
        self.assertLess(ev["dia_media"], 0)

    def test_alert_fires_only_above_threshold(self):
        from b3_ai import alerts
        mk = lambda p, tipo="abertura": {"WIN": {"nome": "Mini Índice", "data": "2026-11-13", "prob": [  # noqa: E731
            {"p_alta": 1 - p, "p_queda": p, "horizonte": "Abertura", "tipo": tipo, "alvo": "a", "confianca": "c", "nota": ""}]}}
        self.assertEqual(alerts.from_rows(mk(0.95, "sessao")), [])            # "Fecho de hoje" não vira aviso no topo
        many = [{"ativo": "WIN", "prob": 0.84, "titulo": "Mini Índice: emergentes em queda — x", "texto": "t."},
                {"ativo": "WIN", "prob": 0.93, "titulo": "Mini Índice: dólar disparando — y", "texto": "t."},
                {"ativo": "WDO", "prob": 0.88, "titulo": "Mini Dólar: emergentes em queda — z", "texto": "t."}]
        best = alerts.strongest_shock(many)
        self.assertEqual(sorted((b["ativo"], b["prob"]) for b in best), [("WDO", 0.88), ("WIN", 0.93)])
        self.assertIn("emergentes em queda", next(b for b in best if b["ativo"] == "WIN")["texto"])
        self.assertEqual(alerts.from_rows(mk(0.60)), [])
        self.assertEqual(alerts.from_rows(mk(0.80))[0]["nivel"], "ATENÇÃO")
        a = alerts.from_rows(mk(0.93))[0]
        self.assertEqual((a["nivel"], a["lado"]), ("ALERTA", -1))

    def test_fomc_calendar_parser(self):
        from b3_ai.data import calendar as cal
        html_text = ('<h4>2026 FOMC Meetings</h4><div class="fomc-meeting__month col"><strong>December</strong></div>'
                     '<div class="fomc-meeting__date col">8-9*</div><div class="fomc-meeting__month"><strong>Apr/May</strong></div>'
                     '<div class="fomc-meeting__date">30-1</div><h4>2027 FOMC Meetings</h4>')
        self.assertEqual(cal.parse_fomc(html_text), [{"data": "2026-05-01", "projecoes": False}, {"data": "2026-12-09", "projecoes": True}])
        self.assertEqual(cal.first_fridays(__import__("datetime").date(2026, 11, 15), 2), ["2026-12-04"])

    def test_analyst_and_company_headlines(self):
        v = news_ai.analyst_view({"title": "BTG eleva recomendação de Vale (VALE3) para compra", "source": "Valor", "link": "", "ts": 0})
        self.assertEqual((v["casa"], v["tom"], v["ticker"]), ("BTG", 1, "VALE3"))
        c = news_ai.company_news({"title": "CSN (CSNA3) despenca 12% após balanço", "source": "x", "link": "", "ts": 0})
        self.assertEqual((c["ticker"], c["tom"]), ("CSNA3", -1))
        self.assertEqual(news_ai.classify("AMD soars amid Nvidia turmoil"), [])       # "turmoil" não é petróleo
        self.assertLess(news_ai.headline_weight("Guru warns stocks could crash?", "Blog"), news_ai.headline_weight("Fed raises rates", "Reuters"))

    def test_trend_and_reversal(self):
        from b3_ai import technical as T
        up = [(1_600_000_000 + i * DAY, 100 + i, 101 + i, 99 + i, 100.5 + i, 1.0) for i in range(80)]
        tr = {t["prazo"]: t["tendencia"] for t in T.trends(up, [])}
        self.assertEqual((tr["Semana"], tr["Mês"]), ("ALTA", "ALTA"))
        # esticada forte para cima e depois uma barra que fecha abaixo da mínima anterior → reversão para baixo
        bars, c = [], 1000.0
        for i in range(60):
            o = c
            c += 0.2 if i < 40 else 6.0
            bars.append((1_700_000_000 + i * 300, o, c + 0.5, o - 0.5, c, 10.0))
        bars.append((bars[-1][0] + 300, c, c + 0.5, c - 12, c - 10, 10.0))
        x = T.extremes(bars)
        self.assertEqual(x["state"][-2], 1)
        self.assertEqual(x["signal"][-1], -1)
        self.assertIn("PARA BAIXO", T.reversal_read(bars)["rotulo"])


class OpinionTest(unittest.TestCase):
    def test_opinion_is_local_and_reads_trend(self):
        from b3_ai import opinion
        lv = {k: 100.0 for k in ("pivot", "r1", "r2", "s1", "s2", "max_ant", "min_ant", "fech_ant", "atr14", "rsi14", "mm20", "mm50", "mm200", "max20", "min20")}
        row = {"horizonte": "Diário", "alvo": "seg", "p_alta": 0.5, "p_queda": 0.5, "sinal": "NEUTRO", "validado": True,
               "f80_min": 90.0, "f80_max": 110.0, "f80_cobertura": 0.82}
        conf = {"leitura": "em linha.", "pos_fechamento": [], "pos_fechamento_total": 0.0, "implicito": 0, "realizado": 0,
                "descolamento": 0, "descolamento_sigma": 0, "r2": 0.1, "fatores": []}
        s = {"ok": True, "atualizado": 1794756548.0, "cotacoes": [], "sintese": {"rotulo": "SINAIS DIVIDIDOS", "alta": 1, "queda": 3, "neutro": 1, "lado": 0},
             "ativos": {"WIN": {"nome": "Mini Índice", "preco": 100.0, "var": 0.5, "max": 1, "min": 1, "sigma_dia_pct": 1, "niveis": lv,
                                "prob": [row], "confronto": conf,
                                "tendencias": [{"prazo": "Semana", "tendencia": "ALTA"}, {"prazo": "Mês", "tendencia": "BAIXA"}]}}}
        r = opinion.build(s)
        self.assertTrue(r["ok"])
        self.assertIn("repique dentro de tendência de baixa", r["texto"])
        self.assertIn("cara-ou-coroa", r["texto"])
        self.assertIn("lado vendedor", r["texto"])
        self.assertIn("DADOS DO PAINEL", opinion.prompt(s))
        self.assertFalse(opinion.build({"ok": False})["ok"])


class FakeMT5:
    """Imitação mínima do módulo MetaTrader5 para testar a leitura sem o terminal."""
    TIMEFRAME_D1, TIMEFRAME_M5, TIMEFRAME_M15 = "D1", "M5", "M15"

    def __init__(self, names=("WIN@", "WDO@", "WINZ26", "IBOV"), ok=True):
        self.names, self.ok, self.shut = set(names), ok, False

    def initialize(self, **kw):
        return self.ok

    def last_error(self):
        return (-6, "Authorization failed")

    def symbol_select(self, sym, enable):
        return sym in self.names

    def symbols_get(self, group=""):
        return [type("I", (), {"name": n})() for n in self.names if n.startswith(group.rstrip("*"))]

    def symbol_info_tick(self, sym):
        return type("T", (), {"time": 1_700_000_000 - 3 * 3600, "last": 174500.0, "bid": 174495.0, "ask": 174505.0})()

    def copy_rates_from_pos(self, sym, tf, start, n):
        if sym not in self.names:
            return None
        return [{"time": 1_700_000_000 - 3 * 3600 + i * 300, "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i,
                 "tick_volume": 7, "real_volume": 0} for i in range(3)]

    def shutdown(self):
        self.shut = True


class MT5Test(unittest.TestCase):
    def test_reads_bars_in_utc_and_last_price(self):
        from b3_ai.data.mt5 import MT5Feed
        f = MT5Feed(FakeMT5(), env={"MT5_UTC_OFFSET_HOURS": "-3"})
        snap = f.snapshot()
        self.assertEqual(snap["simbolos"], {"WIN": "WIN@", "WDO": "WDO@"})     # nomes do MetaTrader da XP
        self.assertEqual(snap[("WIN", "5")][0], (1_700_000_000.0, 100.0, 101.0, 99.0, 100.5, 7.0))   # servidor (Brasília) → UTC
        self.assertEqual(snap["ticks"]["WIN"]["ultimo"], 174500.0)

    def test_discovers_contract_when_known_names_missing(self):
        from b3_ai.data.mt5 import MT5Feed
        f = MT5Feed(FakeMT5(names=("WINZ26", "WDOZ26")), env={"MT5_UTC_OFFSET_HOURS": "-3"})
        f.connect()
        self.assertEqual(f.symbols, {"WIN": "WINZ26", "WDO": "WDOZ26"})

    def test_clear_error_when_terminal_is_closed(self):
        from b3_ai.data.http import DataError
        from b3_ai.data.mt5 import MT5Feed
        with self.assertRaises(DataError) as cm:
            MT5Feed(FakeMT5(ok=False), env={}).connect()
        self.assertIn("abra o terminal", str(cm.exception))
        with self.assertRaises(DataError):
            MT5Feed(None, env={}).connect() if __import__("b3_ai.data.mt5", fromlist=["_mt5"])._mt5 is None else (_ for _ in ()).throw(DataError("x"))


class AuditTest(unittest.TestCase):
    def test_interval_and_verdict(self):
        from b3_ai import audit
        lo, hi = audit.interval(0.54, 825)
        self.assertAlmostEqual(lo, 0.506, places=3)
        self.assertIn("comprovada", audit.verdict(0.54, 825))
        self.assertIn("não distinguível", audit.verdict(0.52, 800))
        self.assertIn("pior", audit.verdict(0.45, 2000))


class MT5ExtrasTest(unittest.TestCase):
    def test_extra_series_only_when_symbol_exists_with_enough_history(self):
        from b3_ai.data.mt5 import MT5Feed

        class Deep(FakeMT5):
            def copy_rates_from_pos(self, sym, tf, start, n):
                if sym not in self.names:
                    return None
                k = 400 if sym in ("IBOV", "FERRO") else 3
                return [{"time": 1_700_000_000 + i * 86400, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_volume": 1, "real_volume": 5}
                        for i in range(k)]

        f = MT5Feed(Deep(names=("WIN$N", "WDO$N", "IBOV", "FERRO", "DI1$N")), env={"MT5_UTC_OFFSET_HOURS": "-3", "MT5_IRON": "FERRO"})
        snap = f.snapshot(["IBOV", "DI", "IRON", "BRENT"])
        self.assertEqual(sorted(snap["extras"]), ["IBOV", "IRON"])     # DI tem histórico curto; BRENT não existe no terminal
        self.assertEqual(snap["simbolos"]["IRON"], "FERRO")
        self.assertNotIn("DI", snap["simbolos"])


class MT5StocksTest(unittest.TestCase):
    def _feed(self, info):
        from b3_ai.data.mt5 import MT5Feed

        class Quotes(FakeMT5):
            def symbol_info(self, sym):
                return info.get(sym)

            def copy_rates_from_pos(self, sym, tf, start, n):
                if sym in ("WIN@", "WDO@"):
                    return FakeMT5.copy_rates_from_pos(self, sym, tf, start, n)
                if sym == "XXXX3":
                    return None                                   # papel que o terminal não tem
                day = 86400
                return [{"time": 1_700_000_000, "open": 10.0, "high": 10.5, "low": 9.9, "close": 10.0, "tick_volume": 1, "real_volume": 100},
                        {"time": 1_700_000_000 + day, "open": 10.1, "high": 10.9, "low": 10.0, "close": 10.5, "tick_volume": 1, "real_volume": 2000}]

        return MT5Feed(Quotes(), env={"MT5_UTC_OFFSET_HOURS": "-3"})

    def test_daily_bar_fallback_change_and_rotation(self):
        f = self._feed({})
        live = f.stocks(["PETR4", "XXXX3", "VALE3"])
        self.assertEqual(sorted(live), ["PETR4", "VALE3"])
        self.assertAlmostEqual(live["PETR4"]["c"], 5.0)           # 10,00 → 10,50
        self.assertEqual((live["PETR4"]["p"], live["PETR4"]["vol"], live["PETR4"]["fonte"]), (10.5, 2000.0, "barra diária"))
        self.assertEqual(f.stocks([], 1), live)                   # sem lista: devolve o que já tem
        f.stocks(["PETR4", "VALE3"], budget_s=0)                  # orçamento zero: não trava nem perde o que já leu
        self.assertEqual(sorted(f._stocks), ["PETR4", "VALE3"])

    def test_terminal_quote_wins_over_stale_daily_bar(self):
        """Caso real (PETR4 na XP): a barra diária veio antiga; o último negócio do terminal é o que vale."""
        newer = 1_700_000_000 + 40 * 86400
        info = {"PETR4": type("I", (), {"last": 45.27, "time": newer, "price_change": 1.34, "session_close": 44.67})(),
                "VALE3": type("I", (), {"last": 0.0, "time": 0})()}
        live = self._feed(info).stocks(["PETR4", "VALE3"])
        self.assertEqual((live["PETR4"]["p"], live["PETR4"]["c"], live["PETR4"]["fonte"]), (45.27, 1.34, "último negócio"))
        self.assertEqual(live["PETR4"]["h"], 45.27)               # barra de outro dia não empresta máxima/mínima
        self.assertEqual(live["VALE3"]["fonte"], "barra diária")  # sem cotação no terminal: cai para a barra
        self.assertGreater(live["PETR4"]["ts"], live["VALE3"]["ts"])


class OpeningTest(unittest.TestCase):
    def test_opening_follows_overnight_leader(self):
        rnd = random.Random(61)
        H = 3600
        tgt, lead, px, lp, t = [], [], 100000.0, 5000.0, 1_700_000_000
        for day in range(140):
            base = t + day * 86400
            for hh in range(9):                                   # pregão do alvo: 9 barras de 1 h
                o = px
                px *= 1 + rnd.gauss(0, 0.001)
                tgt.append((base + hh * H, o, max(o, px), min(o, px), px, 1.0))
            night = rnd.gauss(0, 0.006)                           # o líder anda à noite e o alvo abre seguindo
            for hh in range(24):
                o = lp
                lp *= 1 + (night / 15 if hh >= 9 else rnd.gauss(0, 0.0003))
                lead.append((base + hh * H, o, max(o, lp), min(o, lp), lp, 1.0))
            px *= 1 + 0.8 * night + rnd.gauss(0, 0.0005)
        e = M.opening_study(tgt, lead)
        self.assertGreater(e["n"], 100)
        self.assertGreater(e["acerto"], 0.8)
        self.assertGreater(e["bins"][-1]["acerto"], 0.95)         # movimento grande do líder → abertura quase sempre acompanha
        self.assertAlmostEqual(e["beta"], 0.8, delta=0.15)
        self.assertLess(M.opening_study(tgt, lead, sign=-1)["acerto"], 0.2)


class SimulationTest(unittest.TestCase):
    def test_costs_turn_small_edge_into_loss(self):
        from b3_ai import simulate
        self.assertAlmostEqual(simulate.cost("WDO"), 2 * 0.5 * 10 + simulate.FEE["WDO"])
        r = simulate.summarize("x", "WDO", [1.0, -1.0, 1.0, 1.0] * 20)      # 75 % de acerto, 1 ponto por operação
        self.assertEqual(r["n"], 80)
        self.assertAlmostEqual(r["acerto_bruto"], 0.75)
        self.assertLess(r["liq_medio"], 0)                                   # +R$ 5 de média bruta não paga R$ 12,60 de custo
        self.assertIn("prejuízo", r["veredito"])
        good = simulate.summarize("y", "WIN", [300.0, -100.0] * 40)
        self.assertIn("lucro comprovado", good["veredito"])
        self.assertEqual(good["seq_perdas"], 1)
        self.assertEqual(simulate.summarize("z", "WIN", [])["n"], 0)

    def test_gap_fade_enters_against_the_gap(self):
        from b3_ai import simulate
        rnd = random.Random(71)
        bars, px, t = [], 100000.0, 1_700_000_000
        for day in range(80):
            big = day % 5 == 0                                        # sempre há gaps grandes na janela: os pequenos ficam de fora
            op = px * (1 + (0.02 if big else rnd.gauss(0, 0.001)))           # gap grande para cima...
            cl = op * (1 - 0.01) if big else op * (1 + rnd.gauss(0, 0.002))  # ...que devolve metade até o fechamento
            for i in range(8):
                p0 = op + (cl - op) * i / 8
                p1 = op + (cl - op) * (i + 1) / 8
                bars.append((t + day * 86400 + i * 900, p0, max(p0, p1), min(p0, p1), p1, 1.0))
            px = cl
        res = simulate.gap_fade(bars)
        self.assertGreaterEqual(len(res), 5)
        self.assertTrue(all(x > 0 for x in res))                             # vendeu na abertura, recomprou mais baixo


class PatternsTest(unittest.TestCase):
    def _data(self, planted: bool):
        """Passeio aleatório; com `planted`, depois de 3 quedas seguidas o pregão seguinte sobe em ~80 % das vezes."""
        from b3_ai import patterns
        rnd = random.Random(81)
        bars, c, streak = [], 100.0, 0
        for i in range(1900):
            r = rnd.gauss(0, 0.012)
            if planted and streak >= 3:
                r = abs(r) if rnd.random() < 0.8 else -abs(r)
            streak = streak + 1 if r < 0 else 0
            o = c
            c *= 1 + r
            bars.append((1_500_000_000 + i * DAY, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1.0))
        d = M.DailyData(bars, {}, {})
        res = M.build_daily(d, [], d, True)
        return patterns.mine(d, res, [b[1] for b in bars], "dólar", True)

    def test_planted_pattern_is_found_and_confirmed(self):
        out = self._data(True)
        hit = [p for p in out["padroes"] if p["condicoes"] == ["3 quedas seguidas"] and p["alvo"] == "fech"]
        self.assertTrue(hit, "o padrão plantado deveria ser confirmado")
        self.assertEqual(hit[0]["direcao"], "alta")
        self.assertGreater(hit[0]["chance"], 0.7)

    def test_pure_noise_confirms_almost_nothing(self):
        out = self._data(False)
        self.assertGreater(out["testados"], 50)
        self.assertLessEqual(out["confirmados"], 3)          # ruído puro: no máximo um punhado por acaso


class GapStateTest(unittest.TestCase):
    def test_gap_today_and_fade_side(self):
        from b3_ai import simulate
        rnd = random.Random(91)
        bars, px, t = [], 100000.0, 1_700_000_000
        for day in range(60):
            op = px * (1 + rnd.gauss(0, 0.002))
            for i in range(8):
                c = op * (1 + rnd.gauss(0, 0.001))
                bars.append((t + day * 86400 + i * 900, op if i == 0 else c, max(op, c), min(op, c), c, 1.0))
            px = bars[-1][4]
        today = bars + [(t + 60 * 86400, px * 1.02, px * 1.021, px * 1.019, px * 1.02, 1.0)]     # hoje abriu 2 % acima
        g = simulate.gap_state(today, True)
        self.assertTrue(g["grande"])
        self.assertEqual(g["lado"], -1)                               # gap de alta → devolução = venda
        self.assertAlmostEqual(g["gap_pct"], 2.0, places=1)
        self.assertTrue(g["fora_da_faixa"])                           # 2 % acima: fora da faixa de ontem
        self.assertIsNone(simulate.gap_state(bars, False))            # pregão fechado: não há gap de hoje
        calm = bars + [(t + 60 * 86400, px * 1.0001, px * 1.0002, px, px * 1.0001, 1.0)]
        self.assertEqual(simulate.gap_state(calm, True)["lado"], 0)   # gap pequeno: sem operação


class OpenSessionTest(unittest.TestCase):
    """Virada da abertura: um alvo ganha a barra do dia antes do outro e o ciclo seguinte não pode estourar."""

    def test_cycle_survives_targets_gaining_today_bar_at_different_times(self):
        import contextlib
        import io
        import tempfile as tf
        import time as _t
        from b3_ai import engine as E

        day0 = int((_t.time() - 3 * 3600) // DAY) * DAY - 899 * DAY + 12 * 3600     # a última barra do WIN é a de HOJE (Brasília)
        full = {"WIN": synth_daily(900, seed=101, start=day0), "WDO": synth_daily(900, seed=102, start=day0)}
        facs = {a.tv: synth_daily(900, seed=200 + i, start=day0) for i, a in enumerate(E.ASSETS)}

        def intraday(seed, upto):
            rnd, out, px = random.Random(seed), [], 1000.0
            for dd_ in range(60, 0, -1):
                base = upto - dd_ * DAY
                for i in range(100):
                    o = px
                    px *= 1 + rnd.gauss(0, 0.0005)
                    out.append((base + i * 300, o, max(o, px), min(o, px), px, 5.0))
            return out

        now = _t.time()
        state = {"WIN": 900, "WDO": 899}                      # o WDO ainda sem a barra de hoje

        def fake_history(reqs, timeout=45.0, delays=None, chunk=12):
            h = {}
            for sym, tfm, _n in reqs:
                key = next((k for k, a in E.TARGETS.items() if a.tv == sym), None)
                if tfm == "1D":
                    h[(sym, tfm)] = full[key][:state[key]] if key else facs.get(sym, [])
                elif tfm in ("5", "15"):
                    bars = intraday(hash(sym) % 1000, now)
                    h[(sym, tfm)] = bars + [(now - 600, bars[-1][4], bars[-1][4], bars[-1][4], bars[-1][4], 1.0)]   # barra fresca: pregão aberto
                else:
                    h[(sym, tfm)] = []
            return h

        orig = (E.tv.fetch_history, E.tv.scan_b3, E.Engine._agenda, E.Engine._focus, E.Engine._bcb)
        E.tv.fetch_history, E.tv.scan_b3 = fake_history, lambda: []
        E.Engine._agenda = lambda self, d: {"eventos": [], "fomc_historico": None, "erros": []}
        E.Engine._focus = E.Engine._bcb = lambda self: {}
        try:
            with tf.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                e = E.Engine(tmp, with_news=False)
                self.assertTrue(e.cycle()["ok"])
                state["WDO"] = 900                              # agora o WDO também tem a barra do dia
                e._tv_try = 0
                self.assertTrue(e.cycle()["ok"])
                e.mem.close()
        finally:
            E.tv.fetch_history, E.tv.scan_b3, E.Engine._agenda, E.Engine._focus, E.Engine._bcb = orig


class LiveTickTest(unittest.TestCase):
    def test_ticks_are_light_and_never_block(self):
        from b3_ai.data.mt5 import MT5Feed
        f = MT5Feed(FakeMT5(), env={"MT5_UTC_OFFSET_HOURS": "-3"})
        self.assertEqual(f.ticks(), {})                       # ainda não conectou: nada, sem erro
        f.snapshot()
        t = f.ticks()
        self.assertEqual(t["WIN"]["ultimo"], 174500.0)
        self.assertEqual((t["WIN"]["bid"], t["WIN"]["ask"]), (174495.0, 174505.0))
        import threading
        held = threading.Event()
        release = threading.Event()

        def hold():
            with f.lock:
                held.set()
                release.wait(3)

        th = threading.Thread(target=hold)
        th.start()
        held.wait(2)
        self.assertEqual(f.ticks(), {})                       # terminal ocupado por outra leitura: devolve vazio em ~0,5 s
        release.set()
        th.join()


class GapFiltersTest(unittest.TestCase):
    def test_filter_is_chosen_on_first_half_and_checked_on_second(self):
        """Gaps que abrem FORA da faixa da véspera devolvem; os de dentro não. O estudo precisa achar isso sozinho."""
        from b3_ai import simulate
        rnd = random.Random(111)
        bars, px, t = [], 100000.0, 1_700_000_000
        for day in range(330):
            kind = day % 3 if day >= 45 else 9
            wide = day >= 44 and (day + 1) % 3 == 1               # véspera de gap "de dentro": pregão de faixa larga
            if kind == 0:
                op = px * 1.012                                  # gap grande para FORA da faixa de ontem → devolve
                cl = op * (1 - 0.006 + rnd.gauss(0, 0.001))
            elif kind == 1:
                op = px * (1 + rnd.choice((-1, 1)) * 0.010)      # gap do mesmo tamanho, mas DENTRO da faixa larga de ontem → aleatório
                cl = op * (1 + rnd.gauss(0, 0.006))
            else:
                op = px * (1 + rnd.gauss(0, 0.0003))
                cl = op * (1 + rnd.gauss(0, 0.002))
            for i in range(8):
                p0, p1 = op + (cl - op) * i / 8, op + (cl - op) * (i + 1) / 8
                hi, lo = max(p0, p1), min(p0, p1)
                if wide and i == 3:
                    hi, lo = hi * 1.02, lo * 0.98
                bars.append((t + day * 86400 + i * 900, p0, hi, lo, p1, 1.0))
            px = cl
        S = simulate.gap_study("WIN", bars)
        F = S["filtros"]
        self.assertEqual(F["escolhido"], "abriu FORA da faixa da véspera")
        self.assertTrue(F["melhorou"])
        self.assertGreater(F["resultado"]["acerto_bruto"], 0.9)


class GapAssetsTest(unittest.TestCase):
    def test_portfolio_counts_days_not_trades(self):
        """Dez ações com o mesmo resultado no mesmo dia valem como UM dia, não como dez operações independentes."""
        from b3_ai import simulate
        day = lambda i: {"ts": i * 86400.0, "dia": i, "ret": 0.01 if i % 2 else -0.01, "ctx": 2}   # noqa: E731
        assets = [{"custo": 0.1, "corte": 0.0, "rows": [day(i) for i in range(40)]} for _ in range(10)]
        p = simulate.portfolio(assets, lambda c: c == 2)
        self.assertEqual((p["dias"], p["ops"]), (40, 400))
        self.assertAlmostEqual(p["liq"], -0.1, places=6)                 # média zero menos o custo
        self.assertIn("não distinguível", p["veredito"])
        r = simulate.summarize_pct("x", [0.01] * 40, 0.1)
        self.assertAlmostEqual(r["liq"], 0.9)
        self.assertEqual(simulate.portfolio([], lambda c: True)["dias"], 0)


class GapPreopenTest(unittest.TestCase):
    def test_trigger_price_follows_last_session_direction(self):
        from b3_ai import simulate
        rnd = random.Random(121)
        bars, px, t = [], 100000.0, 1_700_000_000
        for day in range(60):
            op = px * (1 + rnd.gauss(0, 0.002))
            cl = op * (1.01 if day == 59 else 1 + rnd.gauss(0, 0.002))        # último pregão: alta de 1 %
            for i in range(8):
                p0, p1 = op + (cl - op) * i / 8, op + (cl - op) * (i + 1) / 8
                bars.append((t + day * 86400 + i * 900, p0, max(p0, p1), min(p0, p1), p1, 1.0))
            px = cl
        p = simulate.gap_preopen(bars)
        self.assertTrue(p["dia_de_alta"])
        self.assertEqual(p["lado"], -1)                                       # dia de alta → só gap de alta dá sinal → venda
        self.assertGreaterEqual(p["gatilho"], p["maxima"])                    # precisa abrir fora da faixa
        self.assertGreater(p["gap_necessario_pct"], 0)
        # abrir exatamente no gatilho satisfaz as três condições do sinal forte
        today = bars + [(t + 60 * 86400, p["gatilho"] * 1.0001, p["gatilho"] * 1.0002, p["gatilho"], p["gatilho"] * 1.0001, 1.0)]
        g = simulate.gap_state(today, True)
        self.assertTrue(g["grande"] and g["fora_da_faixa"] and g["vespera_na_direcao"])


class NewsThrottleTest(unittest.TestCase):
    def test_google_refusal_keeps_previous_news_and_stops_asking(self):
        from b3_ai.data import news as N
        from b3_ai.data.http import DataError
        calls = []

        def fake(url, **kw):
            calls.append(url)
            raise DataError("HTTP 429 em https://news.google.com/rss/search")

        prev = [{"title": "Ibovespa sobe 1%", "source": "Valor", "link": "", "ts": __import__("time").time() - 3600, "scope": "BR"}]
        orig = N.fetch
        N.fetch = fake
        try:
            items, errs = N.fetch_news(previous=prev, pause=0)
        finally:
            N.fetch = orig
        self.assertEqual([i["title"] for i in items], ["Ibovespa sobe 1%"])          # a leitura anterior não se perde
        self.assertEqual(sum("news.google.com" in c for c in calls), 1)              # parou no primeiro 429 do Google


class BCWatchTest(unittest.TestCase):
    def _mem(self):
        d = tempfile.mkdtemp()
        return Memory(os.path.join(d, "t.db"))

    def test_classify_headlines(self):
        from b3_ai import bc_watch as B
        self.assertEqual(B.classify("Banco Central fará leilão de venda de dólar à vista de US$ 2 bi")["efeito"], -1)
        self.assertEqual(B.classify("BC anuncia leilão de swap cambial tradicional")["tipo"], "swap cambial")
        self.assertEqual(B.classify("BC fará leilão de swap reverso")["efeito"], 1)
        self.assertEqual(B.classify("Banco Central faz leilão de linha com compromisso de recompra")["efeito"], -1)
        self.assertEqual(B.classify("Banco Central anuncia compra de dólares no mercado à vista")["efeito"], 1)
        self.assertIsNone(B.classify("Copom mantém Selic em 13,75%"))
        self.assertIsNone(B.classify("Dólar sobe com cenário fiscal"))
        self.assertEqual(B.classify("Dólar dispara e Banco Central faz leilão de dólares")["efeito"], -1)
        self.assertEqual(B.classify("BC intervém, mas dólar fica acima de R$ 5,90")["efeito"], 0)

    def test_sudden_move_triggers_once_and_is_recorded(self):
        from b3_ai import bc_watch as B
        m = self._mem()
        w = B.Watcher(m)
        w.set_sigma("WDO", 0.0005)                       # 0,05 % por barra de 5 min → ~0,022 % por minuto
        t0 = 1_800_000_000.0
        rnd = random.Random(5)
        for i in range(400):                             # ruído normal: nada dispara
            self.assertIsNone(w.on_tick("WDO", 5700 * (1 + rnd.gauss(0, 0.00003)), t0 + i))
        ev = None
        for j in range(60):                              # queda de 0,3 % em 1 minuto (~13 desvios)
            ev = ev or w.on_tick("WDO", 5700 * (1 - 0.003 * (j + 1) / 60), t0 + 400 + j)
        self.assertIsNotNone(ev)
        self.assertEqual((ev["efeito"], ev["ativo"]), (-1, "WDO"))
        self.assertIsNone(w.on_tick("WDO", 5600, t0 + 470))                       # cooldown: não repete
        for j in range(200):                                                       # +1 e +3 min preenchidos pelos ticks
            w.on_tick("WDO", 5682.9, t0 + 470 + j)
        rec = m.bc_events()[0]
        self.assertIsNotNone(rec["d1"])
        self.assertIsNotNone(rec["d3"])
        m.close()

    def test_news_event_dedupe_and_bar_followup(self):
        from b3_ai import bc_watch as B
        m = self._mem()
        w = B.Watcher(m)
        now = 1_800_000_000.0
        w.set_price("WDO", 5700.0)
        it = {"title": "Banco Central fará leilão de venda de dólar à vista", "ts": now - 120, "source": "Valor", "link": "x"}
        w._news_event(it, B.classify(it["title"]), now)
        w._news_event(it, B.classify(it["title"]), now + 180)                      # mesma notícia lida de novo
        later = {"title": "Dólar sobe mesmo com intervenção do BC", "ts": now + 3600, "source": "G1", "link": "y"}
        w._news_event(later, B.classify(later["title"]), now + 3700)               # outra matéria do mesmo episódio
        self.assertEqual(len(m.bc_events()), 1)
        old = {"title": "BC vende US$ 1 bilhão em leilão à vista", "ts": now - 30 * 3600, "source": "x", "link": "z"}
        w._news_event(old, B.classify(old["title"]), now)                          # notícia velha: não vira evento
        self.assertEqual(len(m.bc_events()), 1)
        bars = [(now - 600 + i * 300, 5700, 5701, 5650, 5700 - 5 * i, 1.0) for i in range(30)]
        w.fill_from_bars({"WDO": bars})
        ev = m.bc_events()[0]
        self.assertLess(ev["d15"], 0)                                              # o dólar caiu depois
        s = B.summary(m.bc_events())[0]
        self.assertGreater(s["d15"]["media"], 0)                                   # a favor do efeito esperado (queda)
        self.assertAlmostEqual(s["atraso_min"], 2.0)
        self.assertIn("Banco Central", B.report(m.bc_events()))
        m.close()


class HolidayTest(unittest.TestCase):
    def test_b3_calendar(self):
        from datetime import date
        from b3_ai.data import calendar as cal
        from b3_ai.engine import next_sessions
        h = cal.b3_holidays(2026)
        for d in ("2026-11-20", "2026-02-16", "2026-02-17", "2026-04-03", "2026-06-04", "2026-12-24", "2026-12-31"):
            self.assertIn(d, h)                                   # Consciência Negra, Carnaval, Sexta Santa, Corpus Christi, 24 e 31/12
        self.assertFalse(cal.is_b3_day(date(2026, 11, 20)))
        self.assertTrue(cal.is_b3_day(date(2026, 11, 19)))
        self.assertEqual(next_sessions("2026-11-19", 1), "2026-11-23")      # quinta → pula o feriado de sexta e o fim de semana
        self.assertEqual(next_sessions("2026-11-19", 5), "2026-11-27")
        self.assertNotIn("2023-11-20", cal.b3_holidays(2023))      # feriado nacional só a partir de 2024
