"""5.2 — FALSE SIGNAL FILTER: contextos que perdem fora da amostra e veto no live só com amostra."""
import unittest
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)          # NY 13–21


class _Prof:
    def __init__(self, mfe, mae):
        self.max_r_before_stop, self.mae_r = mfe, mae


def _row(t, r, sym="US500", regime="RANGE", kind="", score=55.0, conf=60.0):
    return {"time": t, "results": {"adaptive": r}, "direction": "BAIXA", "symbol": sym, "regime": regime, "event_kind": kind,
            "score": score, "confidence": conf, "profile": _Prof(0.3 if r <= 0 else 1.5, 1.0 if r <= 0 else 0.3)}


class FalseSignalTests(unittest.TestCase):
    def test_recurrent_losing_context_is_flagged_and_vetoed_only_with_sample(self):
        from gold_ai.false_signal import FS_MIN_N as MIN_N, build_report, live_veto
        rows = {"US500": [_row(T0 + timedelta(days=i), -1.0 if i % 4 else 0.5) for i in range(24)],      # 24 casos em RANGE/NY: 25% acerto
                "XAUUSD": [_row(T0 + timedelta(days=i), 0.9, sym="XAUUSD", regime="BULLISH") for i in range(8)]}   # 8 casos: amostra
        rep = build_report(rows, "adaptive", start="2026-01-01", end="2026-09-01")
        neg = {(s.dimension, s.value) for s in rep.negatives()}
        self.assertIn(("mercado×regime", "US500 RANGE"), neg)
        self.assertIn(("mercado×sessão", "US500 NY 13–21"), neg)
        self.assertNotIn(("mercado", "XAUUSD"), neg)                              # 8 < MIN_N
        self.assertGreaterEqual(MIN_N, 20)
        txt = rep.render()
        self.assertIn("FALSE SIGNAL FILTER", txt)
        self.assertIn("CONTEXTOS VETADOS", txt)
        negs = rep.to_dict()["negatives"]
        veto = live_veto(negs, "US500", T0 + timedelta(days=100), "RANGE")
        self.assertIsNotNone(veto)
        self.assertIn("FALSO SINAL", veto)
        self.assertIsNone(live_veto(negs, "US500", T0.replace(hour=3), "BULLISH"))   # Ásia + BULLISH: fora dos contextos vetados
        self.assertIsNone(live_veto(negs, "XAUUSD", T0, "RANGE"))                    # outro mercado
