"""Testes 2.3 — GOLD TRADE MONITOR + ADAPTIVE EXIT ENGINE."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import Direction, GoldAIEngine
from gold_ai.evaluation import Backtester, HistoryFrame
from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle
from gold_ai.monitor import ManagedTrade, MonitorConfig, Thesis, TradeMonitor, exit_learning, render_evolution, render_monitor
from gold_ai.sources.sample import SampleSource, make_candles
from gold_ai.trading import PositionManager, RiskLimits, TradePlan, TradingMode, r_stats, simulate_all

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def open_long(entry: float = 2650.0, stop: float = 2640.0):
    """Operação de compra com tese do cenário premove_alta."""
    s = SampleSource("premove_alta", price=entry).snapshot()
    a = GoldAIEngine().analyze(s)
    plan = TradePlan(Direction.ALTA, entry, stop, 9.0, s.time)
    return ManagedTrade(127, plan, Thesis.from_assessment(a, Direction.ALTA)), a, s


def scenario_at(name: str, price: float, minutes: int):
    src = SampleSource(name, price=price, now=NOW + timedelta(minutes=minutes))
    s = src.snapshot()
    return GoldAIEngine().analyze(s), s


class ThesisTests(unittest.TestCase):
    def test_thesis_captures_pillars(self):
        tr, a, s = open_long()
        self.assertGreater(tr.thesis.score, 30)
        self.assertIn("dolar", tr.thesis.pillars)
        self.assertIn("juros_reais", tr.thesis.pillars)
        rt = Thesis.from_dict(tr.thesis.to_dict())
        self.assertEqual(rt.pillars, tr.thesis.pillars)

    def test_scores_when_thesis_holds_and_when_it_breaks(self):
        tr, a, s = open_long()
        m = TradeMonitor()
        self.assertGreaterEqual(m.thesis_score(tr, a), 90)
        self.assertLess(m.exit_score(tr, a, s, m.thesis_score(tr, a), m.trade_score(tr, a), 0.0), 35)
        bad, sbad = scenario_at("venda", 2655, 30)   # cenário virou: dólar ↑, juros ↑, fluxo ↓
        th = m.thesis_score(tr, bad)
        self.assertLess(th, 30)
        self.assertGreater(m.exit_score(tr, bad, sbad, th, m.trade_score(tr, bad), 0.5), 70)


class DecisionTests(unittest.TestCase):
    def test_hold_then_protect_then_extend(self):
        tr, a, s = open_long()
        m = TradeMonitor()
        r0 = m.evaluate(tr, a, s)
        self.assertEqual(r0.action, "MANTER")
        a2, s2 = scenario_at("confirmacao_alta", 2672, 20)   # +2.2R com cenário ainda forte
        r1 = m.evaluate(tr, a2, s2)
        self.assertEqual(r1.action, "PROTEGER")
        self.assertTrue(tr.protected)
        self.assertEqual(tr.remaining, 0.5)
        self.assertGreater(tr.realized_r, 1.0)
        self.assertGreaterEqual(tr.stop_r, 0.0)
        # cenário ainda mais forte que a tese → ESTENDER (força trade score alto)
        tr.thesis.score = 30.0
        a3, s3 = scenario_at("confirmacao_alta", 2676, 30)
        a3.zone["resistance"] = 2720.0
        r2 = m.evaluate(tr, a3, s3)
        self.assertIn(r2.action, ("ESTENDER", "MANTER"))
        if r2.action == "ESTENDER":
            self.assertTrue(tr.extending)
            self.assertEqual(tr.trail_r, m.cfg.extend_trail_r)
        self.assertTrue(r2.targets)
        self.assertIn("GOLD TRADE MONITOR", render_monitor(tr, r2))
        self.assertIn("evolução do score", render_evolution(tr))

    def test_thesis_invalidated_closes_even_in_profit(self):
        tr, a, s = open_long()
        m = TradeMonitor()
        m.evaluate(tr, a, s)
        bad, sbad = scenario_at("venda", 2665, 25)   # +1.5R mas tese invalidada
        r = m.evaluate(tr, bad, sbad)
        self.assertEqual(r.action, "ENCERRAR")
        self.assertEqual(tr.status, "CLOSED")
        self.assertEqual(tr.close_reason, "TESE INVALIDADA")
        self.assertGreater(tr.result_r, 1.0)   # fechou com lucro sem esperar o stop
        self.assertIn("TESE INVALIDADA", r.note)
        self.assertIn("SAÍDA", render_evolution(tr))

    def test_reduce_on_deterioration(self):
        tr, a, s = open_long()
        m = TradeMonitor(MonitorConfig(exit_score_reduce=20, exit_score_close=95, thesis_invalidated=0))
        a2, s2 = scenario_at("reversao", 2660, 20)   # +1R, cenário deteriorando mas não invalidado
        r = m.evaluate(tr, a2, s2)
        self.assertIn(r.action, ("REDUZIR", "PROTEGER"))
        self.assertGreaterEqual(tr.stop_r, 0.0)

    def test_check_path_stop_and_trailing(self):
        tr, a, s = open_long()
        m = TradeMonitor()
        m.evaluate(tr, a, s)
        t = s.time
        up = [Candle(t + timedelta(minutes=5 * i), 2650 + 2.5 * (i - 1), 2650 + 2.5 * i, 2650 + 2.5 * (i - 1) - 0.5, 2650 + 2.5 * i, 10) for i in range(1, 9)]  # até +2R, mínimas subindo
        self.assertIsNone(m.check_path(tr, up))
        self.assertGreaterEqual(tr.peak_r, 1.9)
        self.assertAlmostEqual(tr.stop_r, tr.peak_r - tr.trail_r, places=6)  # trailing adaptativo abaixo do pico
        self.assertGreater(tr.stop_r, 0.0)
        down = [Candle(t + timedelta(minutes=60), 2670, 2670, 2655, 2656, 10)]
        r = m.check_path(tr, down)
        self.assertIsNotNone(r)
        self.assertEqual(r.action, "STOP")
        self.assertEqual(tr.status, "CLOSED")
        self.assertGreaterEqual(tr.result_r, 0.5)   # trailing salvou lucro
        tr2, a, s = open_long()
        crash = [Candle(s.time + timedelta(minutes=5), 2650, 2651, 2635, 2636, 10)]
        r2 = TradeMonitor().check_path(tr2, crash)
        self.assertEqual(r2.action, "STOP")
        self.assertEqual(tr2.result_r, -1.0)

    def test_target_labels_with_history(self):
        rows = []
        for i in range(30):
            deltas = [3] * 12 if i % 3 else [-3] * 4
            cs = [Candle(NOW + timedelta(minutes=5 * k), 2650, 2650 + d * k + 0.5, 2650 + d * k - 0.5, 2650 + d * k, 10) for k, d in enumerate(deltas, 1)]
            sim = simulate_all(TradePlan(Direction.ALTA, 2650, 2640, 9.0, NOW), cs, 240)
            rows.append({"type": "GOLD PRE-MOVE", "profile": sim["profile"], "results": sim["results"]})
        m = TradeMonitor(history=r_stats(rows))
        labels = m.target_labels(1.2, 60)
        self.assertEqual(set(labels), {"2R", "3R", "4R"})
        self.assertIn(labels["2R"], ("atingível", "provável", "possível", "improvável"))


class MemoryMonitorTests(unittest.TestCase):
    def test_persist_resume_close_and_learn(self):
        s = SampleSource("premove_alta").snapshot()
        a = GoldAIEngine().analyze(s)
        _, sig = GoldAIEngine().run_cycle(s)
        pm = PositionManager(TradingMode.PAPER, RiskLimits(), 10000)
        d = pm.decide(sig, s)
        self.assertEqual(d.action, "PAPER")
        with tempfile.TemporaryDirectory() as dd:
            mem = PredictionMemory(os.path.join(dd, "t.db"))
            tid = mem.open_trade(d.plan, "PAPER", None, 240)
            thesis = Thesis.from_assessment(a, d.plan.direction)
            tr = ManagedTrade(tid, d.plan, thesis)
            mem.save_thesis(tid, thesis, tr.state_dict())
            m = TradeMonitor()
            r = m.evaluate(tr, a, s)
            mem.log_monitor(tid, r)
            mem.save_state(tid, tr.state_dict())
            # retomada em outro processo
            resumed = mem.managed_trades()
            self.assertEqual(len(resumed), 1)
            self.assertEqual(resumed[0].thesis.pillars, thesis.pillars)
            self.assertEqual(resumed[0].history[-1].time, r.time)
            # tese invalidada → fechamento gerenciado
            bad, sbad = scenario_at("venda", s.price + 12, 30)
            r2 = m.evaluate(resumed[0], bad, sbad)
            self.assertEqual(r2.action, "ENCERRAR")
            mem.log_monitor(tid, r2)
            mem.close_managed(tid, resumed[0].result_r, resumed[0].close_reason, sbad.time, resumed[0].state_dict())
            self.assertEqual(mem.managed_trades(), [])
            self.assertEqual(len(mem.monitor_history(tid)), 2)
            # continua acompanhando até o horizonte para medir o que ficou na mesa
            self.assertEqual(len(mem.open_trades()), 1)
            R = d.plan.r_value
            path = [Candle(s.time + timedelta(minutes=5 * i), s.price, s.price + 0.3 * R * i, s.price, s.price + 0.3 * R * i, 10) for i in range(1, 12)]
            done = mem.auto_resolve_trades(path, s.time + timedelta(minutes=300))
            self.assertEqual(len(done), 1)
            rows = mem.exit_learning_rows()
            self.assertEqual(rows[0]["exit_reason"], "TESE INVALIDADA")
            self.assertIsNotNone(rows[0]["max_r_after"])
            self.assertIsNotNone(rows[0]["drop_at_exit"])
            txt = mem.exit_learning()
            self.assertIn("APRENDIZADO DE SAÍDA", txt)
            self.assertIn("saídas antecipadas", txt)
            rs = mem.r_stats()
            self.assertTrue(any(st.name == "adaptive" for st in rs.strategies))
            mem.close()
        self.assertIn("sem operações", exit_learning([]))


class BacktestAdaptiveTests(unittest.TestCase):
    def test_backtest_compares_adaptive_exit(self):
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        r = Backtester(f, warmup=230, step=2).run()
        if r.trade_rows:
            self.assertTrue(all("adaptive" in row["results"] for row in r.trade_rows))
            self.assertTrue(any(st.name == "adaptive" for st in r.trades.strategies))
            self.assertIn("adaptive", r.render())
        off = Backtester(f, warmup=230, step=2, adaptive_exit=False).run()
        self.assertTrue(all("adaptive" not in row["results"] for row in off.trade_rows))


if __name__ == "__main__":
    unittest.main()
