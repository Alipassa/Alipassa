"""Testes — estimativa de lucro (curva de capital, custo, bootstrap, carteira) e histórico por datas."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.data.yahoo import YahooCollector
from gold_ai.estimate import TradeR, bootstrap, estimate_market, estimate_profit, simulate_equity
from gold_ai.evaluation import HistoryFrame
from gold_ai.sources.sample import make_candles
from tests.test_v2 import FakeHttp, yahoo_payload

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class EquityTests(unittest.TestCase):
    def test_compound_path_and_drawdown(self):
        trades = [TradeR(NOW + timedelta(hours=i), "XAUUSD", r, 0.0, "3R") for i, r in enumerate([1.0, -1.0, 2.0, -1.0, -1.0, 3.0])]
        p = simulate_equity(trades, 10000, 1.0)
        self.assertAlmostEqual(p.curve[0][1], 10100.0)
        self.assertGreater(p.end, 10000)
        self.assertGreater(p.max_drawdown_pct, 1.5)
        flat = simulate_equity(trades, 10000, 1.0, compound=False)
        self.assertAlmostEqual(flat.end, 10000 + 300.0)
        self.assertEqual(simulate_equity([], 10000, 1.0).return_pct, 0.0)

    def test_bootstrap_percentiles(self):
        trades = [TradeR(NOW + timedelta(hours=i), "XAUUSD", (2.0 if i % 3 else -1.0), 0.0, "3R") for i in range(60)]
        b = bootstrap(trades, 10000, 0.5, n=200)
        self.assertLessEqual(b["ret_p5"], b["ret_p50"])
        self.assertLessEqual(b["ret_p50"], b["ret_p95"])
        self.assertGreater(b["prob_profit"], 0.9)
        self.assertEqual(bootstrap([], 10000, 0.5), {})

    def test_estimate_market_applies_spread_cost(self):
        rows = [{"results": {"adaptive": 1.5, "3R": 3.0}, "time": NOW + timedelta(hours=i), "r_value": 3.0} for i in range(40)]
        m = estimate_market("XAUUSD", rows, 10000, 0.5, "p")
        self.assertEqual(m.n_trades, 40)
        self.assertAlmostEqual(m.avg_cost_r, 0.1)          # spread 0.30 / R 3.0
        self.assertAlmostEqual(m.expectancy_net_r, 1.4)
        self.assertAlmostEqual(m.expectancy_gross_r, 1.5)
        self.assertIn("E líquida", m.render(10000))
        m3 = estimate_market("XAUUSD", rows, 10000, 0.5, "p", strategy="3R")
        self.assertAlmostEqual(m3.expectancy_net_r, 2.9)


class EstimateProfitTests(unittest.TestCase):
    def test_end_to_end_on_synthetic_history(self):
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        rep = estimate_profit({"XAUUSD": f}, NOW - timedelta(days=37), NOW, 10000, 0.5, n_folds=2, step=4)
        txt = rep.render()
        for key in ("ESTIMATIVA DE LUCRO", "walk-forward fora da amostra", "XAUUSD", "RESSALVAS", "não garantia"):
            self.assertIn(key, txt)
        self.assertEqual(len(rep.markets), 1)
        if rep.markets[0].n_trades:
            self.assertIsNotNone(rep.portfolio)
            self.assertIn("CARTEIRA", txt)


class YahooBetweenTests(unittest.TestCase):
    def test_candles_between_concatenates_windows(self):
        start = NOW - timedelta(days=100)
        ts0 = int(start.timestamp())
        routes = {"GC%3DF?interval=1h&period1=": yahoo_payload(200, ts0, 3600, 2600, 0.1)}
        y = YahooCollector(FakeHttp(routes))
        cs = y.candles_between("GC=F", "H1", start, NOW)
        self.assertGreater(len(cs), 100)
        self.assertEqual(len({c.time for c in cs}), len(cs))          # sem duplicatas entre janelas
        self.assertTrue(all(a.time < b.time for a, b in zip(cs, cs[1:])))
        h4 = y.candles_between("GC=F", "H4", start, NOW)
        self.assertLess(len(h4), len(cs))


if __name__ == "__main__":
    unittest.main()
