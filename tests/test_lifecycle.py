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
