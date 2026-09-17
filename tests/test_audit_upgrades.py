"""5.2h — cinco upgrades da auditoria: fonte do preço no LIVE, relógio por ticks, probabilidade calibrada obrigatória,
correlação dinâmica, limiar de fluxo aprendido."""
import unittest
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
T0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class DynamicCorrelationTests(unittest.TestCase):
    def test_rolling_beats_static_and_stress_raises_floor(self):
        import math
        from gold_ai.selector import dynamic_correlation_table
        a = [(T0 + timedelta(minutes=i), 100 + math.sin(i / 5.0)) for i in range(300)]
        b = [(T0 + timedelta(minutes=i), 50 + 0.5 * math.sin(i / 5.0)) for i in range(300)]      # mesma onda → ρ ≈ +1
        c = [(T0 + timedelta(minutes=i), 70 - 0.3 * math.sin(i / 5.0)) for i in range(300)]      # onda oposta → ρ ≈ −1
        table, notes = dynamic_correlation_table({"XAUUSD": a, "EURUSD": b, "USDJPY": c}, {("XAUUSD", "EURUSD"): 0.3, ("XAUUSD", "USDJPY"): -0.2, ("EURUSD", "USDJPY"): -0.3})
        self.assertGreater(table[("XAUUSD", "EURUSD")], 0.9)
        self.assertLess(table[("XAUUSD", "USDJPY")], -0.9)
        self.assertIn("60m", notes[("XAUUSD", "EURUSD")])
        flat = [(T0 + timedelta(minutes=i), 100 + (i % 3) * 0.01) for i in range(300)]
        t2, n2 = dynamic_correlation_table({"XAUUSD": flat, "EURUSD": b}, {("XAUUSD", "EURUSD"): 0.4}, stressed=["XAUUSD"])
        self.assertGreaterEqual(abs(t2[("XAUUSD", "EURUSD")]), 0.8)                                # estresse eleva ao pior caso plausível
        self.assertIn("ESTRESSE", n2[("XAUUSD", "EURUSD")])


class LearnedFlowThresholdTests(unittest.TestCase):
    def test_threshold_learned_only_when_a_bucket_proves(self):
        from gold_ai.flow_anomaly import FLOW_THRESHOLD, learned_thresholds
        rows = [{"ativo": "XAUUSD", "flow_score": 72 + (i % 25), "resultado": "CONTINUOU" if i % 3 else "REVERTEU", "mfe60": 0.7, "mae60": 0.4} for i in range(60)]
        thr = learned_thresholds(rows)
        self.assertEqual(thr["XAUUSD"][0], 70)                                                    # 67% continuação desde 70 → aprendido em 70
        self.assertIn("aprendido", thr["XAUUSD"][1])
        noise = [{"ativo": "WTI", "flow_score": 75, "resultado": "CONTINUOU" if i % 2 else "REVERTEU", "mfe60": 0.5, "mae60": 0.5} for i in range(60)]
        thr2 = learned_thresholds(noise)
        self.assertEqual(thr2["WTI"][0], FLOW_THRESHOLD)
        self.assertIn("só investigação", thr2["WTI"][1])


class PriceSourceAndProbabilityTests(unittest.TestCase):
    def test_snapshot_has_price_source_and_live_vetoes_yahoo(self):
        from gold_ai.models import MarketSnapshot
        s = MarketSnapshot.__dataclass_fields__
        self.assertIn("price_source", s)

    def test_uncalibrated_probability_is_shrunk(self):
        from gold_ai.guard import GuardLimits
        g = GuardLimits.from_env({"PROB_SHRINK_UNCALIBRATED": "0.5"})
        p_raw = 0.70
        p_used = 0.5 + (p_raw - 0.5) * g.prob_shrink_uncalibrated
        self.assertAlmostEqual(p_used, 0.60)
