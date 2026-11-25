import random
import unittest
from types import SimpleNamespace

from turbo_ia import indicators as ind
from turbo_ia.analysis import analyze
from turbo_ia.broker import MT5Broker, PaperBroker
from turbo_ia.engine import IAConfig, IASession


def trend(n=200, drift=0.5, seed=1):
    rng = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        o = p
        p += drift + rng.gauss(0, 0.3)
        out.append({"time": 60 * i, "open": o, "high": max(o, p) + 0.2, "low": min(o, p) - 0.2, "close": p})
    return out


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestIndicators(unittest.TestCase):
    def test_lengths_and_direction(self):
        c = trend()
        line, d = ind.supertrend(c)
        self.assertEqual(len(line), len(c))
        self.assertEqual(d[-1], 1)
        self.assertAlmostEqual(ind.lsma([float(i) for i in range(30)], 25)[-1], 29.0)
        sar, sd = ind.parabolic_sar(c)
        self.assertEqual(sd[-1], 1)
        self.assertLess(sar[-1], c[-1]["close"])

    def test_choch_uses_only_confirmed_pivots(self):
        c = trend(80, 0.6) + [dict(x, time=x["time"] + 80 * 60) for x in trend(80, -0.6, 2)]
        piv = ind.zigzag_pivots(c, 5)
        for e in ind.choch(c, piv):
            used = [p for p in piv if p["price"] == e["price"]]
            self.assertTrue(all(p["confirm"] <= e["i"] for p in used))


class TestAnalysis(unittest.TestCase):
    def test_uptrend_buys_downtrend_sells(self):
        a = analyze(trend(200, 0.5), "veloz")
        self.assertEqual(a.signal, "BUY")
        self.assertLess(a.sl, a.price)
        self.assertGreater(a.tp, a.price)
        b = analyze(trend(200, -0.5), "veloz")
        self.assertEqual(b.signal, "SELL")
        self.assertGreater(b.sl, b.price)

    def test_exhaustion_blocks_in_stricter_modes(self):
        a = analyze(trend(200, 0.5), "equilibrado")      # tendência esticada: RSI ~99
        self.assertEqual(a.signal, "NONE")
        self.assertTrue(any("RSI" in x for x in a.blocked))

    def test_too_few(self):
        self.assertEqual(analyze(trend(20)).signal, "NONE")


class TestSession(unittest.TestCase):
    def test_trades_on_new_bar_and_stops_on_target(self):
        clk = Clock()
        b = PaperBroker(seed=3, clock=clk)
        b.connect()
        s = IASession(b)
        s.start(IAConfig(symbol="Volatility 10 Index", mode="veloz", volume=1, target_profit=0.01, loss_limit=10_000,
                         duration_candles=1, max_trades=100))
        s._stop.set()                      # sem thread: dirigimos os ciclos à mão
        s.st.running = True
        for _ in range(400):
            clk.t += 30
            s.step()
            if not s.st.running:
                break
        self.assertGreater(s.st.trades, 0)
        self.assertFalse(s.st.running)
        self.assertTrue(any(l["kind"] == "trade" for l in s.st.log))

    def test_loss_limit(self):
        b = PaperBroker(seed=1)
        b.connect()
        s = IASession(b)
        s.cfg = IAConfig(loss_limit=5)
        s.st.running, s.st.pnl = True, -6
        self.assertTrue(s._check_limits())
        self.assertIn("limite de perda", s.st.stopped_reason)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1, 0, 1
    ORDER_TIME_GTC, ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN, TRADE_RETCODE_DONE, POSITION_TYPE_BUY = 0, 0, 1, 2, 10009, 0

    def __init__(self, mode=0):
        self.mode, self.sent = mode, []

    def account_info(self):
        return SimpleNamespace(trade_mode=self.mode, login=1, server="Deriv-Demo", balance=100, equity=100, currency="USD")

    def symbol_select(self, s, on):
        return True

    def symbol_info(self, s):
        return SimpleNamespace(digits=3, volume_min=0.5, volume_step=0.01, volume_max=100, trade_contract_size=1, point=0.001,
                               trade_stops_level=10, filling_mode=1)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=100.0, ask=100.1)

    def order_send(self, req):
        self.sent.append(req)
        return SimpleNamespace(retcode=10009, order=42, price=req["price"], comment="done")

    def last_error(self):
        return (0, "")


class TestMT5Broker(unittest.TestCase):
    def test_demo_order_normalized(self):
        m = FakeMT5()
        r = MT5Broker(mt5=m).open("Volatility 10 Index", "BUY", 0.123, sl=99.99, tp=105)
        self.assertTrue(r["ok"])
        req = m.sent[0]
        self.assertEqual(req["volume"], 0.5)              # mínimo da corretora
        self.assertLessEqual(req["sl"], 100.0 - 0.1)       # SL respeita spread/stops level
        self.assertEqual(req["price"], 100.1)

    def test_real_account_blocked(self):
        m = FakeMT5(mode=2)
        r = MT5Broker(mt5=m, allow_real=False).open("X", "SELL", 1)
        self.assertFalse(r["ok"])
        self.assertEqual(m.sent, [])


if __name__ == "__main__":
    unittest.main()


class TestBacktest(unittest.TestCase):
    def test_expiry_and_no_overlap(self):
        from turbo_ia.backtest import run
        c = [dict(x, time=x["time"] * 2) for x in trend(500, 0.5)]        # candles de 2 min
        r = run(c, "M2", "veloz", expiry_min=10)
        self.assertGreater(r["trades"], 0)
        self.assertGreater(r["rate"], 0.9)                                    # tendência limpa: compra acerta
        times = [t["time"] for t in r["list"]]
        self.assertTrue(all(b - a >= 600 for a, b in zip(times, times[1:])))   # de 10 em 10 min, sem sobrepor

    def test_expiry_must_fit_timeframe(self):
        from turbo_ia.backtest import run
        with self.assertRaises(ValueError):
            run(trend(400), "M5", "veloz", expiry_min=7)
