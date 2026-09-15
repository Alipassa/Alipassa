"""Testes — FLOW ANOMALY ENGINE (informação implícita) e propagação pelo REACTION ENGINE."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.flow_anomaly import FLOW_TRANSMISSION, FlowAnomalyEngine
from gold_ai.models import Candle, MarketSnapshot
from gold_ai.news_engine import IdentifiedEvent, TRANSMISSION, expected_direction

UTC = timezone.utc
NOW = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)


def snap(move_pct, dxy=0.0, y_bp=0.0, silver=None, vol_mult=1.0, minutes=20, atr=30.0, price=4300.0, spx=None):
    """Ouro: impulso de `move_pct` nos últimos `minutes` minutos em candles M1, volume ×vol_mult no fim."""
    s = MarketSnapshot(time=NOW, price=price)
    s.atr, s.price_change_pct, s.dxy_change_pct, s.us10y_change_bp, s.silver_change_pct, s.equity_change_pct = atr, move_pct, dxy, y_bp, silver, spx
    p0 = price / (1 + move_pct / 100.0)
    cs = []
    for i in range(120):
        t = NOW - timedelta(minutes=120 - i)
        if i < 120 - minutes:
            p = p0
            v = 100.0
        else:
            k = (i - (120 - minutes) + 1) / minutes
            p = p0 + (price - p0) * k
            v = 100.0 * vol_mult
        cs.append(Candle(t, p, p + 0.5, p - 0.5, p, v))
    cs[-1] = Candle(cs[-1].time, cs[-1].open, price + 0.5, cs[-1].low, price, cs[-1].volume)
    s.candles = {"M1": cs}
    return s


class FlowTests(unittest.TestCase):
    def test_signature_B_institutional_flow_unexplained(self):
        # ouro +1,2% (≈1,7 ATR) em 20 min, DXY e yields parados, prata confirma, volume ×3, sem notícia
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(1.2, dxy=0.02, y_bp=0.5, silver=0.8, vol_mult=3.0), [], NOW)
        self.assertGreaterEqual(fa.score, 70)
        self.assertEqual(fa.signature, "B")
        self.assertEqual(fa.origin, "D")
        self.assertEqual(fa.status, "FLUXO ANÔMALO")
        self.assertTrue(fa.is_anomalous)
        self.assertIn("origem desconhecida", fa.chain)
        self.assertNotIn("banco central", fa.chain.lower())
        ev = fa.implicit_event(NOW)
        self.assertEqual(ev.kind, "flow_XAUUSD_up")
        self.assertEqual(ev.direction_sign, 1.0)
        self.assertLess(ev.time, NOW)                                       # início do impulso, não agora
        # propagação por regra inicial: ouro anômalo ↑ → USDJPY ↓ (refúgio), US500 ↓ leve, EURUSD ↑ (dólar −)
        self.assertLess(expected_direction(ev, "USDJPY")[0], 0)
        self.assertGreater(expected_direction(ev, "EURUSD")[0], 0)
        self.assertGreater(expected_direction(ev, "XAUUSD")[0], 0)

    def test_signature_A_explained_by_leaders_and_by_news(self):
        # DXY −0,5% e yields −6 bp explicam o ouro subir → macro plausível, não anômalo
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(1.2, dxy=-0.5, y_bp=-6.0, silver=0.8, vol_mult=2.0), [], NOW)
        self.assertEqual(fa.signature, "A")
        self.assertFalse(fa.is_anomalous)
        self.assertIn(fa.origin, ("B", "C"))
        # notícia que explica (CPI abaixo → ouro ↑) → origem A, penalidade
        ev = IdentifiedEvent("cpi", "CPI MoM", NOW - timedelta(minutes=30), 1.0, 0.3, 0.1, -2.0, -1.0)
        fa2 = FlowAnomalyEngine().assess("XAUUSD", snap(1.2, dxy=0.0, y_bp=0.0, silver=0.8, vol_mult=3.0), [ev], NOW)
        self.assertEqual(fa2.origin, "A")
        self.assertLess(fa2.components["notícia explicativa"], 0)
        self.assertFalse(fa2.is_anomalous)

    def test_signature_C_regime(self):
        # ouro ↑ forte com DXY ↑ e yields ↑ (contra) → extremamente anômalo → REGIME
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(1.5, dxy=+0.4, y_bp=+5.0, silver=0.3, vol_mult=3.0, minutes=15), [], NOW)
        self.assertEqual(fa.signature, "C")
        self.assertEqual(fa.origin, "E")
        self.assertTrue(fa.anomalous_regime)
        self.assertEqual(fa.status, "REGIME ANÔMALO")
        self.assertIn("ANOMALOUS FLOW REGIME", fa.chain)

    def test_small_move_is_not_anomaly(self):
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(0.2, dxy=0.0, y_bp=0.0, silver=0.1, vol_mult=1.0, minutes=60), [], NOW)
        self.assertLess(fa.score, 40)
        self.assertEqual(fa.status, "SEM ANOMALIA")
        self.assertIsNone(fa.implicit_event(NOW))
        self.assertEqual(FlowAnomalyEngine().assess("XAUUSD", MarketSnapshot(time=NOW, price=4300.0), [], NOW).direction, 0.0)

    def test_flow_kinds_registered_in_transmission(self):
        for k in FLOW_TRANSMISSION:
            self.assertIn(k, TRANSMISSION)


class LivePropagationTests(unittest.TestCase):
    def test_anomaly_opens_implicit_event_and_clocks_other_markets(self):
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        from tests.test_market40 import build_snapset

        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            sender = TelegramSender(dry_run=True, quiet=True)
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD", "USDJPY", "EURUSD"), TradingMode.PAPER, 10000.0, PortfolioLimits(), sender=sender,
                                 kill_switch=KillSwitch(enabled_env=False), log=lambda m: None, horizon_min=60)
            ss = build_snapset({"XAUUSD": "neutro", "USDJPY": "neutro", "EURUSD": "neutro"}, now=NOW)
            gold = snap(1.2, dxy=0.02, y_bp=0.5, silver=0.8, vol_mult=3.0)
            gold.time = NOW
            ss.by_symbol["XAUUSD"] = gold
            for sym in ("USDJPY", "EURUSD"):
                ss.by_symbol[sym].price_change_pct, ss.by_symbol[sym].dxy_change_pct, ss.by_symbol[sym].us10y_change_bp = 0.0, 0.02, 0.5
                ss.by_symbol[sym].candles = {}
            eng.run_cycle(ss)
            self.assertIn("XAUUSD", eng.active_flows)
            self.assertTrue(any("FLUXO ANÔMALO" in m for m in sender.sent))
            self.assertTrue(any(getattr(e, "kind", "") == "flow_XAUUSD_up" for e in ss.identified))
            # os outros mercados ganham relógio de reação para o evento implícito (líder = ouro; eles ainda parados)
            for sym in ("USDJPY", "EURUSD"):
                s = ss.by_symbol[sym]
                self.assertIn("flow_XAUUSD_up", s.reaction_chain)
                self.assertIn(s.reaction_status, ("AGUARDANDO", "PRESSÃO LATENTE"))
            self.assertLess(ss.by_symbol["USDJPY"].reaction_pressure if ss.by_symbol["USDJPY"].reaction_pressure else -0.01, 0)
            mem.close()


if __name__ == "__main__":
    unittest.main()
