"""Testes do módulo MT5 com um terminal simulado (o pacote real só existe no Windows)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from gold_ai import Direction, GoldAIEngine, SignalType
from gold_ai.data.mt5 import MT5Client, MT5Config, MT5Executor, MT5Error, MT5Source, rates_to_candles
from gold_ai.sources.sample import SampleSource

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = range(8)
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC, ORDER_FILLING_IOC = 1, 0, 1, 0, 2
    MINUTES = {0: 1, 1: 5, 2: 15, 3: 30, 4: 60, 5: 240, 6: 1440, 7: 10080}

    def __init__(self, fail_init: bool = False) -> None:
        self.fail_init = fail_init
        self.init_kwargs = None
        self.sent: list[dict] = []
        self.shutdown_called = False

    def initialize(self, **kwargs):
        self.init_kwargs = kwargs
        return not self.fail_init

    def last_error(self):
        return (-1, "simulado")

    def symbol_select(self, symbol, enable):
        return symbol == "XAUUSD"

    def shutdown(self):
        self.shutdown_called = True

    def copy_rates_from_pos(self, symbol, tf, start, n):
        step = self.MINUTES[tf] * 60
        from datetime import datetime as _dt, timezone as _tz
        base = int(_dt.now(_tz.utc).timestamp()) - n * step      # termina AGORA (a fonte compara com a hora atual)
        rows = []
        p = 2650.0
        for i in range(n):
            rows.append({"time": base + i * step, "open": p, "high": p + 2, "low": p - 2, "close": p + 0.3, "tick_volume": 100 + i, "spread": 20, "real_volume": 0})
            p += 0.3
        return rows

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=2699.8, ask=2700.2)

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, order=12345, comment="done")


class MT5Tests(unittest.TestCase):
    def test_config_from_env(self):
        cfg = MT5Config.from_env({"MT5_PATH": r"C:\Program Files\Mt5xp\terminal64.exe", "MT5_SYMBOL": "XAUUSD"})
        self.assertEqual(cfg.path, r"C:\Program Files\Mt5xp\terminal64.exe")
        self.assertEqual(cfg.symbol, "XAUUSD")

    def test_rates_to_candles_uses_tick_volume_fallback(self):
        cs = rates_to_candles(FakeMT5().copy_rates_from_pos("XAUUSD", 4, 0, 5))
        self.assertEqual(len(cs), 5)
        self.assertEqual(cs[0].volume, 100.0)
        self.assertEqual(cs[0].time.tzinfo, timezone.utc)

    def test_source_snapshot_from_terminal(self):
        fake = FakeMT5()
        src = MT5Source(MT5Config(path=r"C:\Program Files\Mt5xp\terminal64.exe"), mt5=fake)
        s = src.snapshot()
        self.assertEqual(fake.init_kwargs["path"], r"C:\Program Files\Mt5xp\terminal64.exe")
        self.assertEqual(src.status["mt5"], "ok")
        self.assertEqual(set(s.candles), {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"})
        self.assertAlmostEqual(s.price, 2700.0)
        self.assertGreater(s.atr, 0)
        self.assertIsNotNone(s.order_flow_imbalance)
        a = GoldAIEngine().analyze(s)
        self.assertTrue(-100 <= a.score <= 100)
        src.client.close()
        self.assertTrue(fake.shutdown_called)

    def test_init_failure_is_reported_not_raised(self):
        src = MT5Source(MT5Config(), mt5=FakeMT5(fail_init=True))
        s = src.snapshot()
        self.assertEqual(s.candles, {})
        self.assertTrue(src.status["mt5"].startswith("erro"))

    def test_missing_package(self):
        with self.assertRaises(MT5Error):
            MT5Client(MT5Config(), mt5=None)

    def test_executor_never_sends_without_authorization(self):
        fake = FakeMT5()
        client = MT5Client(MT5Config(), mt5=fake)
        eng = GoldAIEngine()
        _, sig = eng.run_cycle(SampleSource("venda").snapshot())
        self.assertIn(sig.type, (SignalType.SELL, SignalType.STRONG_SELL))
        ex = MT5Executor(client, volume=0.01, min_confidence=0, min_level=0)
        plan = ex.plan(sig)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, Direction.BAIXA)
        self.assertIsNotNone(plan.stop_loss)
        self.assertLess(plan.take_profit, plan.entry)
        ex.execute(plan, authorize=False)
        self.assertEqual(fake.sent, [])
        self.assertIn("SIMULADA", plan.render())
        ex.execute(plan, authorize=True)
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["type"], fake.ORDER_TYPE_SELL)
        self.assertEqual(plan.result["retcode"], 10009)

    def test_executor_blocks_low_evidence_even_if_authorized(self):
        fake = FakeMT5()
        ex = MT5Executor(MT5Client(MT5Config(), mt5=fake), min_confidence=99, min_level=4)
        _, sig = GoldAIEngine().run_cycle(SampleSource("venda").snapshot())
        plan = ex.execute(ex.plan(sig), authorize=True)
        self.assertTrue(plan.notes)
        self.assertIsNone(plan.result)
        self.assertEqual(fake.sent, [])

    def test_executor_ignores_non_executable_signals(self):
        ex = MT5Executor(MT5Client(MT5Config(), mt5=FakeMT5()))
        _, sig = GoldAIEngine().run_cycle(SampleSource("premove_alta").snapshot())
        self.assertEqual(sig.type, SignalType.PRE_MOVE)
        self.assertIsNone(ex.plan(sig))


if __name__ == "__main__":
    unittest.main()


class NumpyLikeRatesTests(unittest.TestCase):
    def test_rates_to_candles_accepts_array_without_truth_value(self):
        """copy_rates_* devolve numpy structured array: `bool(array)` lança ValueError; a conversão não pode depender disso."""
        from gold_ai.data.mt5 import rates_to_candles

        class Row(dict):
            dtype = type("dt", (), {"names": ("time", "open", "high", "low", "close", "tick_volume")})()

        class ArrayLike(list):
            def __bool__(self):
                raise ValueError("The truth value of an array with more than one element is ambiguous")

        rows = ArrayLike([Row(time=1_700_000_000 + 60 * i, open=1.0, high=2.0, low=0.5, close=1.5, tick_volume=10) for i in range(3)])
        cs = rates_to_candles(rows, 3.0)
        self.assertEqual(len(cs), 3)
        self.assertEqual(cs[0].volume, 10.0)
