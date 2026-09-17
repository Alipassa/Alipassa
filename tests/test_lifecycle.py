"""Testes — ciclo de vida de parâmetros: amostra + sequência, suspensão, revalidação, sombra; nunca apaga."""

from __future__ import annotations

import os
import tempfile
import unittest

from gold_ai.lifecycle import consecutive_losses, evaluate_parameter as evaluate, render_table, tier


class TierTests(unittest.TestCase):
    def test_tiers_and_streak(self):
        self.assertEqual(tier(5), "sem amostra")
        self.assertEqual(tier(10), "observação")
        self.assertEqual(tier(20), "candidato")
        self.assertEqual(tier(30), "operacional")
        self.assertEqual(tier(50), "validado")
        self.assertEqual(tier(100), "alta confiança")
        self.assertEqual(consecutive_losses([1, -1, -1, -1]), 3)
        self.assertEqual(consecutive_losses([-1, 1]), 0)


class SequenceTests(unittest.TestCase):
    def good(self, n=50):
        # +0,35R de expectancy com PF > 1: 60% de acerto a +1,5R, 40% a −1R
        return [(1.5 if i % 5 in (2, 3, 4) else -1.0) for i in range(n)]      # termina em ganho

    def test_alert_protection_suspension(self):
        base = self.good(40)
        self.assertEqual(evaluate("X", base + [-1, -1, -1]).action, "ALERTA")
        self.assertEqual(evaluate("X", base + [-1] * 3).confidence_multiplier, 0.85)
        self.assertEqual(evaluate("X", base + [-1] * 4).action, "PROTEÇÃO")
        self.assertFalse(evaluate("X", base + [-1] * 4).allows_entries)
        st = evaluate("X", base + [-1] * 5)
        # 5 perdas depois de N=40 com edge forte: suspende e REVALIDA na hora → mantido (não apaga)
        self.assertEqual(st.action, "REATIVADO")
        self.assertIn("mantido", st.note)
        self.assertTrue(st.allows_entries)

    def test_five_losses_without_edge_suspends_then_shadow(self):
        weak = [(1.0 if i % 2 else -1.0) for i in range(20)] + [-1] * 5      # expectancy negativa nas janelas
        st = evaluate("X", weak)
        self.assertEqual(st.action, "SUSPENSO")
        self.assertFalse(st.allows_entries)
        st2 = evaluate("X", weak, previous_action="SUSPENSO")
        self.assertEqual(st2.action, "QUEBRADO")                                  # revalidação negativa → sombra
        # sombra acumula resultados bons → revalida e reativa
        st3 = evaluate("X", weak + self.good(30), previous_action="QUEBRADO")
        self.assertEqual(st3.action, "REATIVADO")

    def test_deterioration_beats_streak(self):
        # blocos de 10: +0,4 → +0,3 → +0,1 → −0,05 → −0,15: quebra mesmo sem 5 perdas seguidas
        blocks = [[0.4] * 10, [0.3] * 10, [0.1] * 10, [0.05, -0.15] * 5, [-0.15] * 10]
        xs = [x for b in blocks for x in b]
        st = evaluate("X", xs)
        self.assertTrue(st.deteriorating)
        self.assertEqual(st.action, "QUEBRADO")
        self.assertIn("deterioração", st.note)
        # amostra pequena nunca vira parâmetro nem quebra
        self.assertEqual(evaluate("X", [-1] * 9).action, "NORMAL")
        self.assertIn("amostra < 10", evaluate("X", [-1] * 9).note)
        self.assertIn("CICLO DE VIDA", render_table([st]))


class LiveIntegrationTests(unittest.TestCase):
    def test_market_engine_vetoes_and_shadows(self):
        from datetime import datetime, timedelta, timezone
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        from gold_ai.trading import TradePlan
        from gold_ai.models import Direction
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
            weak = [(1.0 if i % 2 else -1.0) for i in range(20)] + [-1] * 5
            for i, r in enumerate(weak):
                tid = mem.open_trade(TradePlan(Direction.ALTA, 2500.0, 2490.0, 10.0, t0 + timedelta(hours=i), {"3R": 2530.0}, recommended="3R"), "PAPER", symbol="XAUUSD")
                mem.conn.execute("UPDATE trades SET status='CLOSED', resultado_r=?, fechada_em=? WHERE id=?", (r, (t0 + timedelta(hours=i + 4)).isoformat(), tid))
            mem.conn.commit()
            sender = TelegramSender(dry_run=True, quiet=True)
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD", "USDJPY"), TradingMode.PAPER, 10000.0, PortfolioLimits(), sender=sender,
                                 kill_switch=KillSwitch(enabled_env=False), log=lambda m: None)
            self.assertEqual(eng.lifecycle["XAUUSD"].action, "SUSPENSO")
            self.assertEqual(eng.lifecycle["USDJPY"].action, "NORMAL")
            self.assertIn("CICLO DE VIDA", eng.status_text())
            eng.refresh_history()
            self.assertEqual(eng.lifecycle["XAUUSD"].action, "QUEBRADO")          # revalidação negativa → sombra (já é PAPER)
            mem.close()


if __name__ == "__main__":
    unittest.main()


class RiskLadderTests(unittest.TestCase):
    def test_ladder_by_tier_edge_and_state(self):
        from gold_ai.lifecycle import evaluate_parameter, risk_ladder_pct
        good = [0.6, -1.0, 0.8, 0.5] * 13                                   # 52 casos, expectancy positiva
        st50 = evaluate_parameter("X", good)
        self.assertTrue(st50.edge_ok)
        self.assertEqual(risk_ladder_pct(st50, 3.0, (3, 4, 5), 5.0)[0], 5.0)
        st30 = evaluate_parameter("X", good[:32])
        self.assertEqual(risk_ladder_pct(st30, 3.0, (3, 4, 5), 5.0)[0], 4.0)
        st20 = evaluate_parameter("X", good[:22])
        self.assertEqual(risk_ladder_pct(st20, 3.0, (3, 4, 5), 5.0)[0], 3.0)
        # teto absoluto
        self.assertEqual(risk_ladder_pct(st50, 3.0, (3, 6, 10), 5.0)[0], 5.0)
        # 3 perdas seguidas → ALERTA → volta à base mesmo com 52 casos
        st_alert = evaluate_parameter("X", good + [-1.0, -1.0, -1.0])
        self.assertEqual(st_alert.action, "ALERTA")
        self.assertEqual(risk_ladder_pct(st_alert, 3.0, (3, 4, 5), 5.0)[0], 3.0)
        # expectancy negativa → base
        bad = [0.3, -1.0] * 26
        st_bad = evaluate_parameter("X", bad)
        self.assertFalse(st_bad.edge_ok)
        self.assertEqual(risk_ladder_pct(st_bad, 3.0, (3, 4, 5), 5.0)[0], 3.0)

    def test_guard_limits_ladder_from_env_and_proportional_default(self):
        from gold_ai.guard import GuardLimits
        g = GuardLimits.from_env({"RISK_PER_TRADE": "3", "RISK_LADDER": "3,4,5", "RISK_LADDER_MAX": "5"})
        self.assertEqual(g.ladder(), (3.0, 4.0, 5.0))
        g2 = GuardLimits.from_env({"RISK_PER_TRADE": "3"})
        self.assertEqual(g2.ladder(), (3.0, 4.0, 5.0))                     # proporcional ×1, ×4/3, ×5/3, teto 5
        g3 = GuardLimits.from_env({"RISK_PER_TRADE": "0.6"})
        self.assertEqual(g3.ladder(), (0.6, 0.8, 1.0))
        g4 = GuardLimits.from_env({"RISK_PER_TRADE": "3", "RISK_LADDER": "3,6,10"})
        self.assertEqual(g4.ladder(), (3.0, 5.0, 5.0))                     # nunca acima do teto

    def test_live_engine_uses_ladder_risk(self):
        import os
        import tempfile
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        params = {"XAUUSD": {"params": {"min_edge_score": 25.0}, "apply": True, "n_oos": 52, "tier": "validado", "oos_results": [0.6, -1.0, 0.8, 0.5] * 13}}
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            eng = MarketAIEngine(mem, GuardLimits(risk_per_trade_pct=3.0), ("XAUUSD", "US500"), TradingMode.PAPER, 50000.0, PortfolioLimits(),
                                 sender=TelegramSender(dry_run=True, quiet=True), kill_switch=KillSwitch(enabled_env=False), log=lambda m: None, params=params)
            self.assertEqual(eng.engines["XAUUSD"].risk_pct, 5.0)         # validado com edge → topo da escada
            self.assertEqual(eng.engines["XAUUSD"].risk_usd(), 2500.0)
            self.assertEqual(eng.engines["US500"].risk_pct, 1.0)          # sem amostra → grau C: risco de amostra (SAMPLE_RISK_PCT)
            self.assertEqual(eng.engines["US500"].grade, "C")
            self.assertEqual(eng.engines["US500"].risk_usd(), 500.0)
            self.assertEqual(eng.engines["XAUUSD"].grade, "A")
            self.assertIn("HIERARQUIA DE EDGE E RISCO", eng.status_text())
            mem.close()


class SignalLabelDecisionTests(unittest.TestCase):
    def test_signal_label_uses_market_name(self):
        from gold_ai.models import SignalType
        from gold_ai.telegram import signal_label
        self.assertEqual(signal_label(SignalType.WATCH, "EURUSD"), "EURUSD WATCH")
        self.assertEqual(signal_label(SignalType.WATCH, "XAUUSD"), "GOLD WATCH")
        self.assertEqual(signal_label(SignalType.PRE_MOVE, "US500"), "US500 PRE-MOVE")

    def test_live_edge_probability_line_fits_width(self):
        from gold_ai.edge_report import MarketEdge, LiveEdgeReport
        from gold_ai.selector import StatConfidence
        conf = StatConfidence(n=3, expectancy=1.0, std=0.5, level="LOW", lower_bound=0.0, shrunk=0.1)
        m = MarketEdge("WTI", 3, 1.0, 1.0, None, 0.65, 1.0, 3, None, None, 0.0, conf, "⚪", "amostra pequena")
        txt = LiveEdgeReport("2026-09-16", [m]).render()
        row = [l for l in txt.splitlines() if "Prob." in l][0]
        self.assertIn("n=3", row)
        self.assertIn("decl. 65%", row)


class EdgeGradeTests(unittest.TestCase):
    def test_grades_and_risk_by_grade(self):
        from gold_ai.lifecycle import evaluate_parameter, risk_by_grade
        good = [0.6, -1.0, 0.8, 0.5] * 13
        self.assertEqual(evaluate_parameter("X", good).grade, "A")
        self.assertEqual(evaluate_parameter("X", good[:16]).grade, "B")
        self.assertEqual(evaluate_parameter("X", good[:6]).grade, "C")
        self.assertEqual(evaluate_parameter("X", [0.3, -1.0] * 8).grade, "D")
        self.assertEqual(risk_by_grade(evaluate_parameter("X", good), 3.0, (3, 4, 5))[0], 5.0)
        self.assertEqual(risk_by_grade(evaluate_parameter("X", good[:16]), 3.0, (3, 4, 5))[0], 3.0)
        self.assertEqual(risk_by_grade(evaluate_parameter("X", good[:6]), 3.0, (3, 4, 5), sample_pct=1.0)[0], 1.0)
        self.assertEqual(risk_by_grade(evaluate_parameter("X", good[:6]), 3.0, (3, 4, 5), sample_pct=0.0)[0], 0.0)
        self.assertEqual(risk_by_grade(evaluate_parameter("X", [0.3, -1.0] * 8), 3.0, (3, 4, 5))[0], 0.0)

    def test_allowed_risk_splits_correlated_budget(self):
        from gold_ai.models import Direction
        from gold_ai.selector import OpenExposure, PortfolioExposureEngine, PortfolioLimits
        eng = PortfolioExposureEngine(PortfolioLimits(max_total_open_risk_pct=6.0, max_correlated_risk_pct=3.0, max_positions=4))
        room, why = eng.allowed_risk_usd("XAUUSD", Direction.ALTA, [], 50000.0)
        self.assertEqual(room, 1500.0)
        open_ = [OpenExposure("EURUSD", Direction.ALTA, 1500.0)]          # mesma aposta (dólar fraco)
        room2, why2 = eng.allowed_risk_usd("XAUUSD", Direction.ALTA, open_, 50000.0)
        self.assertLess(room2, 1500.0)
        self.assertIn("correlacionado", why2)
        room3, _ = eng.allowed_risk_usd("XAUUSD", Direction.BAIXA, open_, 50000.0)   # aposta oposta: não consome o correlacionado
        self.assertGreaterEqual(room3, room2)
