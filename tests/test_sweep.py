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


class AutotuneTests(unittest.TestCase):
    def test_walk_forward_grid_and_apply_rule(self):
        import json
        import os
        import tempfile
        from gold_ai.autotune import MIN_APPLY, TuneReport, apply_params, autotune_market, cfg_with, load_params
        from gold_ai.config import EngineConfig
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        bt = Backtester(f, warmup=230, step=6)
        tune = autotune_market("XAUUSD", bt, {"min_edge_score": (15.0, 25.0), "min_confirmations": (2, 3)}, n_folds=2)
        self.assertEqual(len(tune.picks), 2)
        self.assertIn(tune.recommended["min_edge_score"], (15.0, 25.0))
        self.assertIn("POLÍTICA OOS", tune.render())
        self.assertIn("SOMBRA", tune.render())              # amostra sintética pequena: nunca 'ADOTAR' sem 20 casos
        rep = TuneReport("2026-01-01", "2026-09-13", [tune])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "parametros.json")
            rep.save(path)
            learned = load_params(path)
            self.assertIn("XAUUSD", learned)
            self.assertFalse(learned["XAUUSD"]["apply"])
            cfg, note = apply_params(EngineConfig(symbol="XAUUSD"), learned["XAUUSD"])
            self.assertEqual(cfg.min_edge_score, 25.0)     # sombra: padrão intacto
            self.assertIn("SOMBRA", note)
            # com amostra e edge, o live adota
            learned["XAUUSD"]["apply"], learned["XAUUSD"]["n_oos"] = True, MIN_APPLY + 5
            learned["XAUUSD"]["params"] = {"min_edge_score": 15.0, "min_confirmations": 2, "signal_score": 40}
            cfg2, note2 = apply_params(EngineConfig(symbol="XAUUSD"), learned["XAUUSD"])
            self.assertEqual((cfg2.min_edge_score, cfg2.min_confirmations, cfg2.buy, cfg2.sell), (15.0, 2, 40, -40))
            self.assertIn("APRENDIDO", note2)
        self.assertEqual(cfg_with(EngineConfig(), {"signal_score": 45}).sell, -45)


class LifecycleSeedTests(unittest.TestCase):
    def test_autotune_oos_results_seed_the_live_lifecycle(self):
        import os
        import tempfile
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        params = {"XAUUSD": {"params": {"min_edge_score": 15.0, "min_confirmations": 2, "signal_score": 40}, "apply": True, "n_oos": 25,
                             "tier": "candidato", "oos_results": [0.5, -1.0, 0.8] * 8 + [0.4]},
                  "US500": {"params": {"min_edge_score": 15.0}, "apply": False, "n_oos": 4, "tier": "sem amostra", "oos_results": [1.0, 1.0, 1.0, 1.0]}}
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD", "US500"), TradingMode.PAPER, 10000.0, PortfolioLimits(), sender=TelegramSender(dry_run=True, quiet=True),
                                 kill_switch=KillSwitch(enabled_env=False), log=lambda m: None, params=params)
            self.assertEqual(eng.lifecycle["XAUUSD"].n, 25)              # semente conta como amostra
            self.assertEqual(eng.lifecycle["XAUUSD"].tier, "candidato")
            self.assertEqual(eng.lifecycle["US500"].n, 0)                # não adotado → não semeia
            self.assertEqual(eng.engines["XAUUSD"].engine.cfg.min_confirmations, 2)
            self.assertEqual(eng.engines["US500"].engine.cfg.min_confirmations, 3)
            mem.close()
