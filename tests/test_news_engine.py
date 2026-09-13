"""Testes — NEWS ENGINE: identificação, importância, surpresa, direção esperada por mercado, reação, divergência,
pressão latente, NEWS = UNKNOWN, saúde dos feeds, COT com idade."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import GoldAIEngine
from gold_ai.data.multi import MarketSnapshotSet, derive_market_snapshot
from gold_ai.data.news import NewsCollector
from gold_ai.factors import cot_age_weight, score_cot, score_sentimento
from gold_ai.markets import get_market
from gold_ai.models import EconomicEvent, MarketSnapshot, NewsItem
from gold_ai.news_engine import EventIdentifier, NewsEngine, expected_direction, render_feed_health
from gold_ai.sources.sample import SampleSource, make_candles
from tests.test_v2 import RSS, FakeHttp

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class IdentifyTests(unittest.TestCase):
    def test_release_and_qualitative(self):
        ev = EconomicEvent("CPI", NOW - timedelta(minutes=20), "MUITO ALTO", consensus=3.1, actual=2.8, kind="cpi", unit="%")
        news = [NewsItem("Missile strike escalates Middle East conflict", "x", NOW - timedelta(minutes=40)),
                NewsItem("Fed signals rate cut as inflation cools", "x", NOW - timedelta(minutes=10)),
                NewsItem("Old news", "x", NOW - timedelta(days=3))]
        ids = EventIdentifier().identify(news, [ev], NOW)
        kinds = [e.kind for e in ids]
        self.assertIn("cpi", kinds)
        self.assertIn("geopolitical_escalation", kinds)
        self.assertIn("fomc_dovish", kinds)
        cpi = next(e for e in ids if e.kind == "cpi")
        self.assertAlmostEqual(cpi.surprise_sigma, -3.0)
        self.assertEqual(cpi.direction_sign, -1.0)
        self.assertEqual(cpi.importance, 1.0)
        self.assertEqual(len([e for e in ids if e.kind == "cpi"]), 1)   # sem duplicata

    def test_transmission_per_market(self):
        ev = EconomicEvent("CPI", NOW, "MUITO ALTO", consensus=3.1, actual=2.8, kind="cpi", unit="%")
        cpi = EventIdentifier().identify([], [ev], NOW)[0]
        gold, ch = expected_direction(cpi, "XAUUSD")
        self.assertGreater(gold, 0)                 # inflação ↓ → juros ↓ → dólar ↓ → ouro ↑
        self.assertLess(ch["yields"], 0)
        self.assertLess(ch["dollar"], 0)
        self.assertGreater(expected_direction(cpi, "US500")[0], 0)
        self.assertGreater(expected_direction(cpi, "EURUSD")[0], 0)
        self.assertLess(expected_direction(cpi, "USDJPY")[0], 0)     # dólar ↓ → USDJPY ↓
        geo = EventIdentifier().identify([NewsItem("Missile strike escalates conflict", "x", NOW)], [], NOW)[0]
        self.assertGreater(expected_direction(geo, "XAUUSD")[0], 0)
        self.assertGreater(expected_direction(geo, "WTI")[0], 0)
        self.assertLess(expected_direction(geo, "US500")[0], 0)


class AssessTests(unittest.TestCase):
    def _cpi(self):
        return EventIdentifier().identify([], [EconomicEvent("CPI", NOW - timedelta(minutes=15), "MUITO ALTO", consensus=3.1, actual=2.8, kind="cpi", unit="%")], NOW)

    def test_confirmation_vs_divergence_and_latent_pressure(self):
        eng = NewsEngine()
        # confirmação: canais e ouro reagem na direção esperada
        s = MarketSnapshot(time=NOW, price=2650, price_change_pct=0.4, us10y_change_bp=-5, dxy_change_pct=-0.3)
        a = eng.assess("XAUUSD", s, self._cpi(), NOW)
        self.assertEqual(a.reaction, "CONFIRMAÇÃO")
        self.assertEqual(a.status, "FAVORÁVEL")
        self.assertEqual(a.channels["US10Y"], "confirma")
        # divergência: canais confirmam, ouro caiu → pressão latente cheia na direção esperada
        s2 = MarketSnapshot(time=NOW, price=2650, price_change_pct=-0.4, us10y_change_bp=-5, dxy_change_pct=-0.3)
        b = eng.assess("XAUUSD", s2, self._cpi(), NOW)
        self.assertEqual(b.reaction, "DIVERGÊNCIA")
        self.assertGreater(b.pressure, a.pressure)      # divergência = oportunidade antecipatória
        self.assertIn("PRESSÃO LATENTE", b.chain)
        # sem reação
        s3 = MarketSnapshot(time=NOW, price=2650, price_change_pct=0.0, us10y_change_bp=-5, dxy_change_pct=-0.3)
        c = eng.assess("XAUUSD", s3, self._cpi(), NOW)
        self.assertEqual(c.reaction, "SEM REAÇÃO")
        self.assertGreater(c.pressure, 0)
        # mesmo evento é CONTRÁRIO para USDJPY
        d = eng.assess("USDJPY", s3, self._cpi(), NOW)
        self.assertEqual(d.status, "CONTRÁRIO")

    def test_unknown_is_not_negative(self):
        a = NewsEngine().assess("XAUUSD", MarketSnapshot(time=NOW, price=2650), [], NOW)
        self.assertEqual(a.status, "UNKNOWN")
        self.assertIsNone(a.pressure)
        s = MarketSnapshot(time=NOW, price=2650)
        f = score_sentimento(s, 6)
        self.assertFalse(f.available)
        self.assertEqual(f.score, 0.0)
        self.assertIn("UNKNOWN", f.rationale)
        s.news_pressure, s.news_status = 0.6, "FAVORÁVEL"
        self.assertGreater(score_sentimento(s, 6).score, 0)
        s.news_pressure, s.news_status = -0.6, "CONTRÁRIO"
        self.assertLess(score_sentimento(s, 6).score, 0)

    def test_freshness_and_priced_in_reduce_pressure(self):
        eng = NewsEngine()
        s = MarketSnapshot(time=NOW, price=2650, price_change_pct=0.0)
        fresh = eng.assess("XAUUSD", s, self._cpi(), NOW).pressure
        old_ev = EventIdentifier().identify([], [EconomicEvent("CPI", NOW - timedelta(hours=10), "MUITO ALTO", consensus=3.1, actual=2.8, kind="cpi")], NOW)
        old = eng.assess("XAUUSD", s, old_ev, NOW).pressure
        self.assertAlmostEqual(fresh, old)   # a pressão agregada é normalizada; a idade pesa na combinação entre eventos
        both = eng.assess("XAUUSD", s, self._cpi() + old_ev, NOW)
        self.assertEqual(len(both.drivers), 2)


class CotAndFeedsTests(unittest.TestCase):
    def test_cot_age_decay(self):
        self.assertEqual(cot_age_weight(3), 1.0)
        self.assertAlmostEqual(cot_age_weight(21), 0.3)
        self.assertEqual(cot_age_weight(40), 0.0)
        self.assertGreater(cot_age_weight(15), 0.3)
        s = MarketSnapshot(time=NOW, price=2650, cot_managed_money_net_change=9000, cot_managed_money_percentile=50)
        full = score_cot(s, 5).score
        s.cot_age_days, s.cot_report_date = 21, "2026-08-24"
        aged = score_cot(s, 5)
        self.assertAlmostEqual(aged.score, round(full * 0.3, 1), places=1)
        self.assertIn("21 dias", aged.rationale)
        s.cot_age_days = 40
        self.assertFalse(score_cot(s, 5).available)

    def test_feed_health(self):
        from gold_ai.data.http import DataError
        http = FakeHttp({"fxstreet.com": RSS, "kitco.com": "<rss><channel></channel></rss>", "dowjones.io": DataError("fora do ar")})
        nc = NewsCollector(http)
        items = nc.collect(NOW)
        self.assertTrue(items)
        by = {h.source: h for h in nc.health}
        self.assertTrue(by["www.fxstreet.com"].ok)
        self.assertEqual(by["www.fxstreet.com"].n_items, 5)
        self.assertGreater(by["www.fxstreet.com"].n_discarded, 0)   # a notícia antiga
        self.assertFalse(by["feeds.content.dowjones.io"].ok)
        self.assertIn("fora do ar", by["feeds.content.dowjones.io"].error)
        self.assertFalse(by["www.kitco.com"].ok)
        txt = render_feed_health(nc.health)
        self.assertIn("NEWS FEEDS: 1/3 ok", txt)
        self.assertIn("descartadas", txt)


class IntegrationTests(unittest.TestCase):
    def test_market_snapshot_gets_news_pressure_and_all_markets_use_news(self):
        base = SampleSource("premove_alta").snapshot()
        base.events = [EconomicEvent("CPI", NOW - timedelta(minutes=15), "MUITO ALTO", consensus=3.1, actual=2.8, kind="cpi", unit="%")]
        ids = EventIdentifier().identify(base.news, base.events, NOW)
        self.assertTrue(ids)
        for sym in ("XAUUSD", "EURUSD", "US500", "USDJPY"):
            spec = get_market(sym)
            self.assertEqual(spec.factor_signs["sentimento"], 1)
            cs = base.candles if sym == "XAUUSD" else {"H1": make_candles("H1", 260, 100, 0.01, 0.2, base.time, seed=2)}
            s = derive_market_snapshot(base, spec, cs, base.time, identified=ids)
            self.assertIsNotNone(s.news_pressure)
            self.assertIn(s.news_status, ("FAVORÁVEL", "CONTRÁRIO", "NEUTRO"))
            self.assertIn("NEWS →", s.news_chain)
        s_jpy = derive_market_snapshot(base, get_market("USDJPY"), {"H1": make_candles("H1", 260, 147, 0.01, 0.2, base.time, seed=2)}, base.time, identified=ids)
        self.assertLess(s_jpy.news_pressure, 0)
        # cérebro consome a pressão e a cadeia aparece no relatório
        a = GoldAIEngine().analyze(derive_market_snapshot(base, get_market("XAUUSD"), base.candles, base.time, identified=ids))
        self.assertTrue(a.factor("sentimento").available)
        self.assertIn("NEWS → XAUUSD", a.chain)
        # sem eventos identificados → UNKNOWN, fator indisponível, nunca negativo
        s_unk = derive_market_snapshot(base, get_market("EURUSD"), {"H1": make_candles("H1", 260, 1.1, 0.0001, 0.002, base.time, seed=3)}, base.time, identified=[])
        self.assertEqual(s_unk.news_status, "UNKNOWN")
        self.assertIsNone(s_unk.news_pressure)


if __name__ == "__main__":
    unittest.main()
