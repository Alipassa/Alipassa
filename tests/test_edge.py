"""Testes — LIVE EDGE REPORT (o teste definitivo da 4.0)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import GoldAIEngine
from gold_ai.edge_report import LiveEdgeReport, edge_status_from_stats as edge_status, edge_trend, live_edge_report, market_edge
from gold_ai.guard import GuardLimits, TelegramCommands, KillSwitch, TradingMode
from gold_ai.market_engine import MarketAIEngine
from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle, Direction
from gold_ai.selector import PortfolioLimits, statistical_confidence
from gold_ai.sources.sample import SampleSource
from gold_ai.telegram import TelegramSender
from gold_ai.trading import TradePlan
from tests.test_market40 import build_snapset

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def seed_trades(mem: PredictionMemory, symbol: str, results: list[float], prob: float = 0.7):
    """Grava operações e previsões resolvidas como se tivessem sido vividas."""
    s = SampleSource("venda").snapshot()
    a = GoldAIEngine().analyze(s)
    for i, r in enumerate(results):
        plan = TradePlan(Direction.BAIXA, 2650.0, 2659.0, 9.0, NOW + timedelta(minutes=i), lots=0.05, risk_usd=45.0, signal_type="GOLD SELL")
        tid = mem.open_trade(plan, "PAPER", None, 240, symbol=symbol)
        mem.conn.execute("UPDATE trades SET status='CLOSED', resultado_r=?, resultado_financeiro=?, score_entrada=-60 WHERE id=?", (r, r * 45.0, tid))
        pid = mem.record(a, "GOLD SELL", atr=9.0, symbol=symbol)
        mem.conn.execute("UPDATE predictions SET probabilidade=?, resultado=? WHERE id=?", (prob, "ACERTO" if r > 0 else "ERRO", pid))
    mem.conn.commit()


class EdgeTests(unittest.TestCase):
    def test_status_rules(self):
        self.assertEqual(edge_status(statistical_confidence([]), None)[0], "⚪")
        self.assertEqual(edge_status(statistical_confidence([0.5] * 10), 0.6)[0], "⚪")            # amostra insuficiente
        good = [0.42 + (0.8 if i % 3 == 0 else -0.25) for i in range(300)]
        self.assertEqual(edge_status(statistical_confidence(good), 0.61)[0], "🟢")
        bad = [(-0.3 if i % 2 else 0.1) for i in range(120)]
        self.assertEqual(edge_status(statistical_confidence(bad), 0.5)[0], "🔴")

    def test_report_from_lived_data(self):
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "t.db"))
            seed_trades(mem, "EURUSD", [0.9 if i % 3 else -1.0 for i in range(120)], prob=0.72)
            seed_trades(mem, "XAUUSD", [0.5 if i % 2 else -1.0 for i in range(40)], prob=0.8)
            rep = live_edge_report(mem, ("EURUSD", "XAUUSD", "WTI"), NOW, equity=10500.0)
            by = {m.symbol: m for m in rep.rows}
            self.assertEqual(by["EURUSD"].n_trades, 120)
            self.assertGreater(by["EURUSD"].expectancy, 0.2)
            self.assertEqual(by["EURUSD"].status, "🟢")
            self.assertAlmostEqual(by["EURUSD"].prob_observed, 80 / 120, places=3)
            self.assertAlmostEqual(by["EURUSD"].prob_declared, 0.72, places=3)
            self.assertIsNotNone(by["EURUSD"].calibration_gap)
            self.assertEqual(by["XAUUSD"].n_trades, 40)
            self.assertIn(by["XAUUSD"].status, ("🔴", "🟡"))   # expectancy −0.25R → sem edge
            self.assertEqual(by["WTI"].status, "⚪")
            self.assertEqual(rep.ranked()[0].symbol, "EURUSD")
            txt = rep.render()
            for key in ("MARKET AI — LIVE EDGE", "OOS Trades: 120", "Expectancy:", "Prob. calibrada:", "Capture Rate:", "Status: 🟢", "Melhor edge vivido: EURUSD", "Capital: 10,500.00"):
                self.assertIn(key, txt)
            self.assertTrue(all(len(l) == len(txt.splitlines()[0]) for l in txt.splitlines()[:-2]))   # caixa alinhada
            # persistência e evolução
            mem.save_edge_report(rep)
            self.assertEqual(mem.last_edge_date(), "2026-09-14")
            rep2 = LiveEdgeReport("2026-09-15", rep.rows, 10600.0)
            mem.save_edge_report(rep2)
            hist = mem.edge_history()
            self.assertEqual(len(hist), 2)
            self.assertIn("EURUSD:", edge_trend(hist, "EURUSD"))
            self.assertIn("09-14", edge_trend(hist, "EURUSD"))
            self.assertIn("sem histórico", edge_trend(hist, "US500"))
            mem.close()

    def test_daily_generation_once_per_day_and_on_demand(self):
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "t.db"))
            sender = TelegramSender(dry_run=True, quiet=True)
            eng = MarketAIEngine(mem, GuardLimits(), ("EURUSD", "XAUUSD"), TradingMode.PAPER, 10000.0, PortfolioLimits(),
                                 sender=sender, kill_switch=KillSwitch(enabled_env=False), log=lambda s: None)
            pc1 = eng.run_cycle(build_snapset({"EURUSD": "neutro", "XAUUSD": "neutro"}))
            self.assertTrue(any("LIVE EDGE" in m for m in pc1.messages))
            n_sent = sum(1 for m in sender.sent if "LIVE EDGE" in m)
            pc2 = eng.run_cycle(build_snapset({"EURUSD": "neutro", "XAUUSD": "neutro"}, now=NOW + timedelta(hours=2)))
            self.assertFalse(any("LIVE EDGE" in m for m in pc2.messages))          # mesma data: não repete
            self.assertEqual(sum(1 for m in sender.sent if "LIVE EDGE" in m), n_sent)
            pc3 = eng.run_cycle(build_snapset({"EURUSD": "neutro", "XAUUSD": "neutro"}, now=NOW + timedelta(days=1)))
            self.assertTrue(any("LIVE EDGE" in m for m in pc3.messages))           # novo dia: gera
            self.assertEqual(len(mem.edge_history()), 2)
            # sob demanda via /EDGE
            cmds = TelegramCommands(None, None)
            self.assertEqual(cmds.apply(["/EDGE"], eng.ks), ["EDGE"])
            eng.commands = cmds
            pc4 = eng.run_cycle(build_snapset({"EURUSD": "neutro", "XAUUSD": "neutro"}, now=NOW + timedelta(days=1, hours=1)))
            self.assertTrue(any("LIVE EDGE" in m for m in pc4.messages))
            mem.close()


if __name__ == "__main__":
    unittest.main()
