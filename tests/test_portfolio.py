"""5.2 — oportunidades de carteira: várias entradas por ciclo sob o motor de exposição, e o simulador 1×2×3×4."""
import unittest
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


class PortfolioSimTests(unittest.TestCase):
    def _trades(self):
        from gold_ai.models import Direction
        from gold_ai.portfolio_sim import SimTrade
        out = []
        for i in range(40):
            t = T0 + timedelta(hours=i * 6)
            # duas oportunidades quase simultâneas: ouro (tese USD_SHORT) e euro (mesma tese) + petróleo (tese OIL, independente)
            out.append(SimTrade(t, "XAUUSD", Direction.ALTA, 0.8 if i % 3 else -1.0, t + timedelta(hours=3)))
            out.append(SimTrade(t + timedelta(minutes=5), "EURUSD", Direction.ALTA, 0.6 if i % 4 else -1.0, t + timedelta(hours=3)))
            out.append(SimTrade(t + timedelta(minutes=10), "WTI", Direction.BAIXA, 0.5 if i % 2 else -1.0, t + timedelta(hours=2)))
        return out

    def test_more_positions_admit_more_but_correlation_caps_same_thesis(self):
        from gold_ai.portfolio_sim import render_portfolio_sim, simulate_portfolio
        from gold_ai.selector import PortfolioLimits
        lim = PortfolioLimits(max_total_open_risk_pct=9.0, max_correlated_risk_pct=3.0, max_positions=4)
        trades = self._trades()
        r1 = simulate_portfolio(trades, 1, lim, 10000.0, 3.0)
        r3 = simulate_portfolio(trades, 3, lim, 10000.0, 3.0)
        self.assertEqual(r1.admitted, 40)                     # só a primeira de cada bloco
        self.assertEqual(r1.peak_concurrent, 1)
        self.assertGreater(r3.admitted, r1.admitted)
        self.assertLessEqual(r3.peak_concurrent, 3)
        self.assertTrue(any("correlacionado" in k for k in r3.refused_reasons))   # EURUSD barrado: mesma aposta que XAUUSD
        self.assertEqual(r3.admitted, 80)                     # XAUUSD + WTI a cada bloco; EURUSD recusado por correlação
        txt = render_portfolio_sim([r1, r3], 10000.0, 3.0, lim, len(trades), 10.0)
        self.assertIn("PORTFOLIO SIM", txt)
        self.assertIn("N=3 vs N=1", txt)

    def test_trades_from_rows_uses_exit_times_and_cost(self):
        from gold_ai.portfolio_sim import trades_from_rows
        rows = {"XAUUSD": [{"time": T0, "results": {"adaptive": 1.2, "3R": 3.0}, "exits": {"adaptive": T0 + timedelta(hours=1)}, "direction": "ALTA"},
                           {"time": T0 + timedelta(hours=2), "results": {"3R": -1.0}, "exits": {}, "direction": "BAIXA"}]}
        ts = trades_from_rows(rows, "adaptive", cost_r=0.05)
        self.assertEqual(len(ts), 2)
        self.assertAlmostEqual(ts[0].r, 1.15)
        self.assertEqual(ts[0].exit_time, T0 + timedelta(hours=1))
        self.assertEqual(ts[1].exit_time, T0 + timedelta(hours=6))   # sem exit: horizonte padrão 4 h


class MultiEntryCycleTests(unittest.TestCase):
    def test_limits_from_env_and_default(self):
        from gold_ai.selector import PortfolioLimits
        self.assertEqual(PortfolioLimits().max_entries_per_cycle, 3)
        self.assertEqual(PortfolioLimits.from_env({"MAX_ENTRIES_PER_CYCLE": "2"}).max_entries_per_cycle, 2)

    def test_two_markets_enter_in_same_cycle_when_exposure_allows(self):
        import os
        import tempfile
        from gold_ai.guard import GuardLimits, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        from tests.test_market40 import build_snapset

        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            plim = PortfolioLimits(max_total_open_risk_pct=9.0, max_correlated_risk_pct=9.0, max_positions=4, max_entries_per_cycle=3)
            eng = MarketAIEngine(mem, GuardLimits(risk_per_trade_pct=1.0), ("EURUSD", "XAUUSD", "USDJPY"), TradingMode.PAPER, 10000.0, plim,
                                 sender=TelegramSender(dry_run=True, quiet=True), log=lambda m: None)
            pc = eng.run_cycle(build_snapset({"EURUSD": "venda", "XAUUSD": "venda", "USDJPY": "neutro"}))
            opened = [s for s, r in pc.results.items() if r.decision.startswith("🟢 PAPER OPEN")]
            self.assertGreaterEqual(len(opened), 2, pc.render())
            self.assertIn("+", pc.chosen or "")
            # com 1 por ciclo, o segundo recebe PRIORIDADE
            plim1 = PortfolioLimits(max_total_open_risk_pct=9.0, max_correlated_risk_pct=9.0, max_positions=4, max_entries_per_cycle=1)
            mem2 = PredictionMemory(os.path.join(d, "m2.db"))
            eng1 = MarketAIEngine(mem2, GuardLimits(risk_per_trade_pct=1.0), ("EURUSD", "XAUUSD", "USDJPY"), TradingMode.PAPER, 10000.0, plim1,
                                  sender=TelegramSender(dry_run=True, quiet=True), log=lambda m: None)
            pc1 = eng1.run_cycle(build_snapset({"EURUSD": "venda", "XAUUSD": "venda", "USDJPY": "neutro"}))
            self.assertEqual(sum(1 for r in pc1.results.values() if r.decision.startswith("🟢 PAPER OPEN")), 1)
            self.assertTrue(any("PRIORIDADE" in r.decision for r in pc1.results.values()))
            mem.close(); mem2.close()
