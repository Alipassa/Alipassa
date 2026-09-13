"""Testes — sweep de piso com seleção in-train."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from gold_ai.evaluation import Backtester, HistoryFrame
from gold_ai.sources.sample import make_candles
from gold_ai.sweep import FloorMetrics, objective, threshold_sweep

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class SweepTests(unittest.TestCase):
    def test_objective(self):
        self.assertEqual(objective(FloorMetrics(20, 3, 10, 1.0, 1.0, None, 0.0, None, None)), 0.0)     # n < 5
        self.assertGreater(objective(FloorMetrics(20, 16, 10, 0.3, 0.6, 1.5, 1.0, None, None)), objective(FloorMetrics(20, 4, 10, 0.9, 1.0, None, 0.0, None, None)))

    def test_sweep_selects_in_train_and_reports_sensitivity(self):
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        bt = Backtester(f, warmup=230, step=6)
        rep = threshold_sweep(bt, floors=(15, 25, 35), n_folds=2)
        self.assertEqual(len(rep.choices), 2)
        for c in rep.choices:
            self.assertIn(c.floor, (15, 25, 35))
            self.assertEqual(len(c.train_table), 3)          # todos os pisos avaliados no treino
        self.assertEqual(len(rep.sensitivity), 3)
        txt = rep.render()
        for key in ("SWEEP DE PISO", "SELEÇÃO IN-TRAIN", "SENSIBILIDADE OOS POR PISO FIXO", "NÃO usar para escolher", "Piso", "Entr/dia", "◀ padrão"):
            self.assertIn(key, txt)
        # sem edge no treino → mantém o padrão em vez de escolher ao acaso
        self.assertTrue(all(c.floor == 25 or objective(c.train) > 0 for c in rep.choices))


if __name__ == "__main__":
    unittest.main()
