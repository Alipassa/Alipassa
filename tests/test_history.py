"""Testes — banco histórico point-in-time de eventos/notícias, importadores e TESTE A/B."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

from gold_ai.ablation import compare_information
from gold_ai.data.history_sources import ALFREDImporter, GDELTImporter, TradingEconomicsImporter, us_release_time
from gold_ai.evaluation import Backtester, HistoryFrame
from gold_ai.history import (EventHistory, FetchProgress, HistoricalEvent, apply_rule_effects, coverage, learn_effects, load_history, merge,
                             render_effect_table, save_history)
from gold_ai.sources.sample import make_candles
from tests.test_v2 import FakeHttp

UTC = timezone.utc
T0 = datetime(2026, 1, 14, 13, 30, tzinfo=UTC)
KEY = "0123456789abcdef0123456789abcdef"


def cpi(actual=0.4, forecast=0.2, published=None, event_id="CPI_202601", **kw) -> HistoricalEvent:
    return HistoricalEvent(T0, published or T0, event_id, "CPI MoM", "US", "USD", "MUITO ALTO", forecast, 0.3, actual, category="MACRO", kind="cpi", **kw)


class PointInTimeTests(unittest.TestCase):
    def test_nothing_visible_before_publication(self):
        h = EventHistory([cpi()])
        self.assertEqual(h.available_at(T0 - timedelta(minutes=1)), [])
        self.assertEqual(len(h.available_at(T0)), 1)
        self.assertEqual(len(h.available_at(T0 + timedelta(hours=23))), 1)
        self.assertEqual(h.available_at(T0 + timedelta(hours=25)), [])

    def test_revision_only_after_its_own_publication(self):
        first = cpi(actual=0.4)
        revised = cpi(actual=0.3, published=T0 + timedelta(days=1, hours=2), revised=0.3)
        h = EventHistory([revised, first])
        self.assertEqual(h.available_at(T0 + timedelta(hours=2))[0].actual, 0.4)      # o valor inicialmente publicado
        later = h.available_at(T0 + timedelta(days=1, hours=3), lookback_hours=48)
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0].actual, 0.3)                                          # a revisão substitui só depois de publicada

    def test_upcoming_events_have_no_actual(self):
        h = EventHistory([cpi()])
        events, news = h.snapshot_inputs(T0 - timedelta(hours=5))
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].actual)                       # calendário futuro: consenso conhecido, resultado não
        self.assertEqual(events[0].consensus, 0.2)
        events, _ = h.snapshot_inputs(T0 + timedelta(minutes=30))
        self.assertEqual(events[0].actual, 0.4)

    def test_surprise_and_rule_effects(self):
        e = cpi()
        self.assertAlmostEqual(e.surprise, 0.2)
        h = EventHistory([e])
        self.assertEqual(apply_rule_effects(h), 1)
        self.assertLess(e.effect("XAUUSD"), 0)        # CPI acima → juros/dólar ↑ → ouro ↓
        self.assertLess(e.effect("US500"), 0)
        self.assertGreater(e.effect("USDJPY"), 0)
        self.assertEqual(e.effect_source, "rule")
        # sem consenso: surpresa vs anterior, explicitada
        e2 = HistoricalEvent(T0, T0, "NFP", "Nonfarm Payrolls", forecast=None, previous=150.0, actual=200.0, kind="nfp", surprise_basis="previous")
        self.assertEqual(e2.surprise, 50.0)
        self.assertEqual(e2.to_economic_event().consensus, 150.0)
        e3 = HistoricalEvent(T0, T0, "X", "CPI MoM", forecast=None, previous=0.3, actual=0.4)
        self.assertIsNone(e3.surprise)                # sem base declarada, nada é inventado
        self.assertIsNone(e3.to_economic_event().consensus)

    def test_csv_roundtrip_and_merge(self):
        h = EventHistory([cpi(), HistoricalEvent(T0 + timedelta(hours=3), T0 + timedelta(hours=3), "G1", "geopolitica: Missile strike", "GLOBAL", "", "MÉDIO",
                                                  category="GEOPOLITICAL", headline="Missile strike escalates conflict", sentiment="NEGATIVE", tone=-4.2, volume=1.3)])
        apply_rule_effects(h)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "noticias_historicas.csv")
            self.assertEqual(save_history(h, path), 2)
            with open(path, encoding="utf-8") as f:
                header = f.readline().strip()
            for col in ("timestamp", "event_id", "forecast", "actual", "surprise", "xau_effect", "us500_effect", "eurusd_effect", "usdjpy_effect", "wti_effect", "headline", "sentiment"):
                self.assertIn(col, header)
            back = load_history(path)
            self.assertEqual(len(back), 2)
            self.assertAlmostEqual(back.events[0].effect("XAUUSD"), h.events[0].effect("XAUUSD"))
            self.assertEqual(back.events[1].tone, -4.2)
            merged = merge(back, EventHistory([cpi()]))
            self.assertEqual(len(merged), 2)            # mesma (event_id, published_at) não duplica
        self.assertIn("2 registros", h.stats())

    def test_learn_effects_replaces_rules_when_history_shows(self):
        events = [cpi(actual=0.4, event_id=f"CPI_{i}") for i in range(10)]
        for i, e in enumerate(events):
            e.timestamp = e.published_at = T0 + timedelta(days=i)
        h = EventHistory(events)
        apply_rule_effects(h)
        # preço do ouro SOBE após cada CPI acima do consenso (contrário à regra) → o histórico manda
        series = []
        for e in events:
            for k in range(0, 61, 15):
                series.append((e.timestamp + timedelta(minutes=k), 2500.0 + k * 0.5))
        res = learn_effects(h, {"XAUUSD": series}, horizon_min=60, min_n=8)
        self.assertEqual(res["applied"], 10)
        self.assertGreater(events[0].effect("XAUUSD"), 0)
        self.assertEqual(events[0].effect_source, "empirical")
        self.assertIn("cpi", render_effect_table(res["table"]))
        self.assertEqual(apply_rule_effects(h), 0)     # regras não sobrescrevem o empírico


class ImporterTests(unittest.TestCase):
    def test_trading_economics_with_revision(self):
        rows = [{"CalendarId": "1", "Date": "2026-01-14T13:30:00", "Country": "United States", "Category": "Inflation Rate", "Event": "Core Inflation Rate MoM",
                 "Actual": "0.4%", "Previous": "0.3%", "Forecast": "0.2%", "Importance": 3, "Currency": "USD", "Revised": "0.2%"},
                {"CalendarId": "2", "Date": "2026-01-16T15:00:00", "Event": "Fed Chair Powell Speech", "Actual": "", "Previous": "", "Forecast": "", "Importance": 2}]
        h = TradingEconomicsImporter(FakeHttp({"tradingeconomics": rows}), "k").fetch(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(len(h), 3)
        main = next(e for e in h.events if e.event_id == "TE_1_202601141330")
        self.assertEqual(main.kind, "core_cpi")
        self.assertAlmostEqual(main.surprise, 0.2)
        rev = next(e for e in h.events if e.event_id.endswith("_REVISAO"))
        self.assertEqual(rev.actual, 0.2)
        speech = next(e for e in h.events if e.kind == "speech")
        self.assertEqual(speech.category, "CENTRAL_BANK")
        self.assertIsNone(speech.actual)

    def test_alfred_initial_print_then_revision(self):
        obs = [
            {"realtime_start": "2026-01-14", "realtime_end": "2026-02-10", "date": "2025-11-01", "value": "320.0"},
            {"realtime_start": "2026-01-14", "realtime_end": "2026-02-10", "date": "2025-12-01", "value": "321.28"},
            {"realtime_start": "2026-02-11", "realtime_end": "9999-12-31", "date": "2025-11-01", "value": "320.0"},
            {"realtime_start": "2026-02-11", "realtime_end": "9999-12-31", "date": "2025-12-01", "value": "320.96"},   # revisão do dezembro
            {"realtime_start": "2026-02-11", "realtime_end": "9999-12-31", "date": "2026-01-01", "value": "322.24"},
        ]
        h = ALFREDImporter(FakeHttp({"CPIAUCSL": {"observations": obs}}), KEY).fetch(date(2026, 1, 1), date(2026, 3, 1), ["CPIAUCSL"])
        dec = [e for e in h.events if e.event_id == "ALFRED_CPIAUCSL_2025-12-01"]
        self.assertEqual(len(dec), 2)
        first, rev = sorted(dec, key=lambda e: e.published_at)
        self.assertAlmostEqual(first.actual, 0.4)                  # 321.28/320 − 1
        self.assertEqual(first.published_at, us_release_time(date(2026, 1, 14)))
        self.assertEqual(first.published_at.hour, 13)              # 8:30 ET em janeiro = 13:30 UTC
        self.assertAlmostEqual(rev.actual, 0.3)
        self.assertEqual(rev.published_at.date(), date(2026, 2, 11))
        self.assertEqual(us_release_time(date(2026, 7, 3)).hour, 12)   # horário de verão
        jan = next(e for e in h.events if e.event_id == "ALFRED_CPIAUCSL_2026-01-01")
        self.assertEqual(jan.surprise_basis, "previous")
        self.assertIsNotNone(jan.surprise)
        # point-in-time: em 20/jan o dezembro vale 0.4; em 12/fev vale 0.3
        pit = EventHistory(h.events)
        self.assertAlmostEqual(pit.available_at(datetime(2026, 1, 15, tzinfo=UTC), 48)[0].actual, 0.4)
        later = [e for e in pit.available_at(datetime(2026, 2, 12, tzinfo=UTC), 24 * 40) if e.event_id.endswith("2025-12-01")]
        self.assertAlmostEqual(later[0].actual, 0.3)

    def test_gdelt_headlines_tone_volume(self):
        arts = {"articles": [{"title": "Missile strike escalates Middle East conflict", "seendate": "20260114T140000Z", "domain": "x.com"},
                             {"title": "Missile strike escalates Middle East conflict", "seendate": "20260114T141500Z", "domain": "y.com"},   # duplicada
                             {"title": "Ceasefire talks resume", "seendate": "20260115T090000Z", "domain": "z.com"}]}
        tone = {"timeline": [{"series": "Average Tone", "data": [{"date": "20260114T120000Z", "value": -5.1}, {"date": "20260115T000000Z", "value": 2.0}]}]}
        vol = {"timeline": [{"series": "Article Count", "data": [{"date": "20260114T120000Z", "value": 1.8}]}]}
        http = FakeHttp({"mode=artlist": arts, "mode=timelinetone": tone, "mode=timelinevolraw": vol})
        slept: list[float] = []
        h = GDELTImporter(http, sleep=slept.append).fetch(date(2026, 1, 14), date(2026, 1, 15), ["geopolitica"], chunk_days=30, enrich=True, mode="artlist")
        self.assertGreaterEqual(len(slept), 1)                 # ritmo: pausa entre chamadas (limite do GDELT)
        self.assertEqual(len(h), 2)
        strike = next(e for e in h.events if "Missile" in e.headline)
        self.assertEqual(strike.kind, "geopolitical_escalation")
        self.assertEqual(strike.sentiment, "NEGATIVE")
        self.assertEqual(strike.category, "GEOPOLITICAL")
        self.assertIsNone(strike.volume)                       # volume vem do modo volinfo
        cease = next(e for e in h.events if "Ceasefire" in e.headline)
        self.assertEqual(cease.kind, "geopolitical_deescalation")
        self.assertEqual(cease.sentiment, "POSITIVE")
        # ids determinísticos entre execuções
        h2 = GDELTImporter(http, sleep=lambda s: None).fetch(date(2026, 1, 14), date(2026, 1, 15), ["geopolitica"], chunk_days=30, enrich=True, mode="artlist")
        self.assertEqual([e.event_id for e in h.events], [e.event_id for e in h2.events])
        # modo econômico (padrão): só artlist — manchete, data, tema, fonte; sem tom/volume
        http.calls.clear()
        h3 = GDELTImporter(http, sleep=lambda s: None).fetch(date(2026, 1, 14), date(2026, 1, 15), ["geopolitica"], chunk_days=30, mode="artlist")
        self.assertEqual(len(h3), 2)
        self.assertTrue(all("artlist" in c for c in http.calls))
        self.assertIsNone(h3.events[0].tone)
        self.assertEqual(h3.events[0].kind, "geopolitical_escalation")   # a identificação qualitativa continua vindo da manchete

    def test_gdelt_retries_on_429_and_checkpoints(self):
        from gold_ai.data.http import DataError

        class Flaky(FakeHttp):
            def __init__(self, routes):
                super().__init__(routes)
                self.fail_left = 1

            def get_json(self, url, ttl=None):
                if "artlist" in url and self.fail_left:
                    self.fail_left -= 1
                    raise DataError("falha ao buscar x: HTTP Error 429: Too Many Requests")
                return super().get_json(url, ttl)
        arts = {"articles": [{"title": "OPEC cuts output", "seendate": "20260114T140000Z", "domain": "x.com"}]}
        http = Flaky({"mode=artlist": arts, "mode=timelinetone": {}, "mode=timelinevolraw": {}})
        slept, parts = [], []
        imp = GDELTImporter(http, retry_wait=60.0, sleep=slept.append, log=lambda m: None)
        h = imp.fetch(date(2026, 1, 1), date(2026, 3, 1), ["petroleo"], chunk_days=30, checkpoint=lambda p: parts.append(len(p)), mode="artlist")
        self.assertIn(60.0, slept)                              # esperou o limite e tentou de novo
        # bloqueio por rajada: espera exponencial; Retry-After do servidor tem prioridade
        http.fail_left = 2
        slept.clear()
        GDELTImporter(http, retry_wait=60.0, sleep=slept.append, log=lambda m: None).fetch(date(2026, 1, 1), date(2026, 1, 10), ["petroleo"], mode="artlist")
        self.assertEqual([w for w in slept if w >= 60.0], [60.0, 120.0])

        class RetryAfter(FakeHttp):
            done = False

            def get_json(self, url, ttl=None):
                if not self.done:
                    self.done = True
                    raise DataError("falha ao buscar x: HTTP Error 429: Too Many Requests (Retry-After 300s)")
                return super().get_json(url, ttl)
        slept.clear()
        GDELTImporter(RetryAfter({"mode=artlist": arts}), sleep=slept.append).fetch(date(2026, 1, 1), date(2026, 1, 10), ["petroleo"], mode="artlist")
        self.assertIn(300.0, slept)
        self.assertEqual(len(h), 1)
        self.assertEqual(len(parts), 2)                         # um checkpoint por janela
        # falha persistente numa janela: registrada em `failed`, execução continua, nada levantado
        http.fail_left = 99
        imp2 = GDELTImporter(http, max_retries=1, sleep=lambda s: None)
        h2 = imp2.fetch(date(2026, 1, 1), date(2026, 1, 10), ["petroleo"], mode="artlist")
        self.assertEqual(len(h2), 0)
        self.assertEqual(len(imp2.failed), 1)

    def test_gdelt_volinfo_spreads_headlines_over_every_day(self):
        data = []
        for d in range(1, 31):
            data.append({"date": f"202601{d:02d}T000000Z", "value": 0.2 + d / 100,
                         "toparts": [{"url": f"https://www.site{d}.com/a", "title": f"Missile strike day {d}"}, {"url": "https://x.org/b", "title": f"Ceasefire talks {d}"}]})
        vol = {"timeline": [{"series": "Volume Intensity", "data": data}]}
        http = FakeHttp({"mode=timelinevolinfo": vol})
        h = GDELTImporter(http, sleep=lambda s: None).fetch(date(2026, 1, 1), date(2026, 1, 30), ["geopolitica"], chunk_days=30)
        self.assertEqual(len(http.calls), 1)                       # 1 chamada por tema e janela
        self.assertEqual(len(h), 60)
        days = {e.timestamp.date() for e in h.events}
        self.assertEqual(len(days), 30)                            # cobertura de todos os dias da janela
        e = next(x for x in h.events if "day 7" in x.headline)
        self.assertEqual((e.published_at.hour, e.published_at.minute), (23, 59))   # só o dia é conhecido → visível do fim do dia
        self.assertEqual(e.source, "gdelt/site7.com")
        self.assertAlmostEqual(e.volume, 0.27)
        self.assertEqual(e.kind, "geopolitical_escalation")
        cov = coverage(h, date(2026, 1, 1), date(2026, 1, 30))
        self.assertAlmostEqual(cov.news_pct, 1.0)

    def test_gdelt_resume_skips_done_windows(self):
        arts = {"articles": [{"title": "OPEC cuts output", "seendate": "20260114T140000Z", "domain": "x.com"}]}
        http = FakeHttp({"mode=artlist": arts})
        with tempfile.TemporaryDirectory() as d:
            prog = FetchProgress(os.path.join(d, "n.csv"))
            GDELTImporter(http, sleep=lambda s: None).fetch(date(2026, 1, 1), date(2026, 3, 1), ["petroleo"], chunk_days=30, progress=prog, mode="artlist")
            self.assertEqual(len(http.calls), 2)
            prog2 = FetchProgress(os.path.join(d, "n.csv"))       # relido do disco
            GDELTImporter(http, sleep=lambda s: None).fetch(date(2026, 1, 1), date(2026, 3, 1), ["petroleo"], chunk_days=30, progress=prog2, mode="artlist")
            self.assertEqual(len(http.calls), 2)                   # nada refeito

    def test_alfred_key_validation_and_400(self):
        from gold_ai.data.http import DataError
        with self.assertRaises(DataError) as ctx:
            ALFREDImporter(FakeHttp({}), "SUA_CHAVE_FRED").fetch(date(2026, 1, 1), date(2026, 3, 1))
        self.assertIn("32 caracteres", str(ctx.exception))
        http = FakeHttp({"CPIAUCSL": DataError("falha ao buscar x: HTTP Error 400: Bad Request"), "UNRATE": {"observations": []}})
        imp = ALFREDImporter(http, KEY, log=lambda m: None)
        h = imp.fetch(date(2026, 1, 1), date(2026, 3, 1), ["CPIAUCSL", "UNRATE"])
        self.assertEqual(len(h), 0)
        self.assertEqual(imp.failed[0][0], "CPIAUCSL")
        self.assertIn("400", imp.failed[0][1])                     # a série seguinte ainda foi buscada
        self.assertEqual(len(http.calls), 2)


class CoverageTests(unittest.TestCase):
    def test_coverage_counts_macro_by_week_and_news_by_day(self):
        evs = []
        for w in range(4):                      # 4 semanas com um CPI cada
            t = datetime(2026, 1, 5, 13, 30, tzinfo=UTC) + timedelta(days=7 * w)
            evs.append(HistoricalEvent(t, t, f"CPI{w}", "CPI MoM", forecast=0.2, actual=0.3, kind="cpi"))
        for d in range(10):                     # 10 dias com manchete
            t = datetime(2026, 1, 5, 9, tzinfo=UTC) + timedelta(days=d)
            evs.append(HistoricalEvent(t, t, f"N{d}", "geopolitica: x", "GLOBAL", "", "MÉDIO", category="GEOPOLITICAL", headline="strike"))
        evs.append(HistoricalEvent(evs[0].timestamp, evs[0].timestamp + timedelta(days=30), "CPI0", "CPI MoM revisado", actual=0.2, revised=0.2, kind="cpi"))
        cov = coverage(EventHistory(evs), date(2026, 1, 5), date(2026, 2, 1))
        self.assertEqual(cov.macro_events, 4)
        self.assertEqual(cov.news_items, 10)
        self.assertEqual(cov.revisions, 1)
        self.assertEqual(cov.macro_weeks, 4)
        self.assertEqual(cov.news_days, 10)
        self.assertEqual(cov.days, 28)
        self.assertAlmostEqual(cov.macro_pct, 1.0)
        self.assertAlmostEqual(cov.pct, 1.0)   # toda semana tem macro
        txt = cov.render()
        for k in ("HISTÓRICO", "MACRO", "NEWS", "REVISÕES", "COBERTURA"):
            self.assertIn(k, txt)
        partial = coverage(EventHistory(evs[:2]), date(2026, 1, 1), date(2026, 3, 31))
        self.assertLess(partial.macro_pct, 0.3)


class BacktestIntegrationTests(unittest.TestCase):
    def _frame(self, end: datetime) -> HistoryFrame:
        return HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, end, seed=3), dxy=make_candles("H1", 900, 104, -0.002, 0.08, end, seed=4),
                            us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, end, seed=5))

    def _history(self, frame: HistoryFrame) -> EventHistory:
        evs = []
        for k, i in enumerate(range(250, len(frame.xau) - 10, 40)):
            t = frame.xau[i].time
            evs.append(HistoricalEvent(t, t, f"CPI_{k}", "CPI MoM", forecast=0.2, previous=0.3, actual=(0.4 if k % 2 else 0.0), kind="cpi", impact="MUITO ALTO"))
            evs.append(HistoricalEvent(t + timedelta(hours=5), t + timedelta(hours=5), f"G_{k}", "geopolitica: strike", "GLOBAL", "", "MÉDIO", category="GEOPOLITICAL",
                                       headline="Missile strike escalates conflict", sentiment="NEGATIVE", tone=-4.0))
        h = EventHistory(evs)
        apply_rule_effects(h)
        return h

    def test_snapshot_sees_only_published_and_modes_differ(self):
        frame = self._frame(datetime(2026, 9, 14, 13, 0, tzinfo=UTC))
        hist = self._history(frame)
        frame.symbol, frame.events = "XAUUSD", hist
        i = 250
        frame.news_mode = "none"
        self.assertEqual(frame.snapshot_at(i).news_status, "UNKNOWN")
        frame.news_mode = "macro"
        s = frame.snapshot_at(i)
        self.assertIn(s.news_status, ("FAVORÁVEL", "CONTRÁRIO", "NEUTRO"))
        self.assertTrue(s.events)
        self.assertIsNone(s.sentiment)
        self.assertEqual(frame.snapshot_at(i - 1).news_status, "UNKNOWN")      # uma hora antes: nada publicado
        frame.news_mode = "full"
        s_full = frame.snapshot_at(i + 5)
        self.assertIsNotNone(s_full.sentiment)                                 # tom GDELT só no modo B
        self.assertLess(s_full.sentiment, 0)

    def test_backtest_runs_with_events_and_ablation_report(self):
        end = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
        frame = self._frame(end)
        hist = self._history(frame)
        frame.symbol, frame.events, frame.news_mode = "XAUUSD", hist, "full"
        res = Backtester(frame, warmup=240, step=6).run()
        self.assertGreater(res.n_steps, 50)
        rep = compare_information({"XAUUSD": frame}, hist, frame.xau[0].time, end, n_folds=2, step=8, warmup=240)
        self.assertEqual(len(rep.results), 3)
        txt = rep.render()
        for label in ("Preço somente", "Preço + Macro (A)", "Preço + Macro + News (B)", "LEITURA", "TESTE A/B"):
            self.assertIn(label, txt)
        macro = next(r for r in rep.results if r.mode == "macro")
        self.assertGreater(macro.steps_with_info, 0)
        self.assertIsNone(frame.events)          # a ablação devolve o frame como estava


if __name__ == "__main__":
    unittest.main()
