import random
import unittest
from types import SimpleNamespace

from turbo_ia import indicators as ind
from turbo_ia.analysis import analyze
from turbo_ia.broker import MT5Broker, PaperBroker
from turbo_ia.engine import IAConfig, IASession
from turbo_ia.forward import ForwardLog


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
        s = IASession(b, forward=ForwardLog(None))
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
        self.assertGreater(len(s.forward.done) + len(s.forward.pending), 0)    # toda entrada vai para o registro de acerto

    def test_loss_limit(self):
        b = PaperBroker(seed=1)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.cfg = IAConfig(loss_limit=5)
        s.st.running, s.st.pnl = True, -6
        self.assertTrue(s._check_limits())
        self.assertIn("limite de perda", s.st.stopped_reason)


def stretched(hour_utc, up=True, n=80, tf=120):
    """Preço parado e depois uma esticada forte no último candle, que abre na hora pedida."""
    base = (1_700_000_000 // 86400) * 86400 + hour_utc * 3600 - (n - 1) * tf
    out = []
    for i in range(n):
        p = 100.0 + (0.05 if i % 2 else -0.05)
        out.append({"time": base + i * tf, "open": p, "high": p + 0.1, "low": p - 0.1, "close": p})
    last = out[-1]
    last["close"] = last["high"] = 101.0 if up else 99.0
    if not up:
        last["low"] = 99.0
    return out


class TestReversion(unittest.TestCase):
    def test_sells_stretch_up_inside_hours(self):
        from turbo_ia.analysis import analyze
        a = analyze(stretched(16, up=True), "reversao", rr=2, sl_atr=1)
        self.assertEqual(a.signal, "SELL")
        self.assertAlmostEqual(a.sl - a.price, a.atr)                  # perda 1× (1 ATR)
        self.assertAlmostEqual(a.price - a.tp, 2 * a.atr)              # ganho 2×
        a3 = analyze(stretched(16, up=True), "reversao", rr=3, sl_atr=1)
        self.assertAlmostEqual(a3.price - a3.tp, 3 * a3.atr)
        self.assertIsNone(analyze(stretched(16, up=True), "reversao", rr=0).tp)
        self.assertEqual(analyze(stretched(20, up=False), "reversao").signal, "BUY")

    def test_hour_filter_and_server_offset(self):
        from turbo_ia.analysis import analyze
        self.assertEqual(analyze(stretched(12), "reversao").signal, "NONE")
        # MT5 carimba no horário do servidor (UTC+2): candle das 18h do servidor = 16h UTC → opera
        self.assertEqual(analyze(stretched(18), "reversao", utc_offset=2).signal, "SELL")
        self.assertEqual(analyze(stretched(16), "reversao", utc_offset=2).signal, "NONE")


class TestForward(unittest.TestCase):
    def test_resolves_after_expiry(self):
        clk = Clock()
        b = PaperBroker(seed=5, clock=clk)
        b.connect()
        f = ForwardLog(None)
        c = b.candles("XAUUSD", "M2", 3)
        f.add("XAUUSD", "M2", "reversao", "BUY", c[-2]["time"], c[-2]["close"], 10, broker="paper")
        self.assertEqual(f.resolve(b), [])                  # 10 min ainda não passaram
        clk.t += 15 * 60
        b.update()
        r = f.resolve(b)
        self.assertEqual(len(r), 1)
        target = c[-2]["time"] + 600
        exit_ = next(x for x in b.candles("XAUUSD", "M2", 20) if x["time"] == target)["close"]
        self.assertEqual(r[0]["exit"], exit_)
        self.assertEqual(r[0]["win"], exit_ > c[-2]["close"])
        st = f.stats("reversao", "paper")
        self.assertEqual(st["n"], 1)
        self.assertIn("coletando", st["verdict"])
        self.assertEqual(f.stats("reversao", "mt5")["n"], 0)  # simulador não se mistura com o MT5


class StaticBroker:
    def __init__(self, candles):
        self.c = candles

    def candles(self, symbol, tf, n):
        return self.c[-n:]


class TestForwardRR(unittest.TestCase):
    def bars(self, lows_highs, t0=0, tf=120):
        return [{"time": t0 + i * tf, "open": 100, "high": h, "low": l, "close": 100} for i, (l, h) in enumerate(lows_highs)]

    def test_target_first_wins_2r(self):
        f = ForwardLog(None)
        f.add("XAUUSD", "M2", "reversao", "BUY", 0, 100.0, sl=99.0, tp=102.0)
        b = StaticBroker(self.bars([(99.5, 100.5), (99.6, 101), (99.5, 102.1), (98, 100), (99, 100)]))
        r = f.resolve(b)
        self.assertEqual((r[0]["win"], r[0]["r"]), (True, 2.0))
        st = f.stats("reversao")
        self.assertEqual(st["kind"], "rr")
        self.assertAlmostEqual(st["breakeven"], 1 / 3)
        self.assertEqual(st["exp_r"], 2.0)

    def test_both_in_same_candle_counts_stop(self):
        f = ForwardLog(None)
        f.add("XAUUSD", "M2", "reversao", "SELL", 0, 100.0, sl=101.0, tp=97.0)
        r = f.resolve(StaticBroker(self.bars([(99.5, 100.5), (96, 101.5), (99, 100)])))
        self.assertEqual((r[0]["win"], r[0]["r"]), (False, -1.0))

    def test_open_candle_is_not_used(self):
        f = ForwardLog(None)
        f.add("XAUUSD", "M2", "reversao", "BUY", 0, 100.0, sl=99.0, tp=102.0)
        self.assertEqual(f.resolve(StaticBroker(self.bars([(99.5, 100.5), (99.5, 103)]))), [])   # só o candle em formação tocou


class TestRiskSizing(unittest.TestCase):
    GOLD = {"volume_min": 0.01, "volume_step": 0.01, "volume_max": 100, "value_per_unit": 100.0}   # 1 lote = 100 oz

    def test_5pct_of_1000_with_3usd_stop(self):
        from turbo_ia.broker import lot_for_risk
        lot, risk, note = lot_for_risk(self.GOLD, 2650.0, 2647.0, 1000.0, 5)
        self.assertEqual(lot, 0.16)                       # 50 / (3 × 100) = 0,166 → arredonda para baixo
        self.assertAlmostEqual(risk, 48.0)
        self.assertLessEqual(risk, 50.0)

    def test_skips_when_min_lot_is_too_risky(self):
        from turbo_ia.broker import lot_for_risk
        lot, _, note = lot_for_risk(self.GOLD, 2650.0, 2640.0, 50.0, 1)   # 0,01 lote arriscaria 10 de 50
        self.assertEqual(lot, 0.0)
        self.assertIn("pulada", note)

    def test_session_uses_risk_pct(self):
        clk = Clock()
        b = PaperBroker(seed=3, clock=clk, balance=1000)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.start(IAConfig(symbol="Volatility 10 Index", mode="veloz", risk_pct=5, target_profit=0, loss_limit=0, protect=False))
        s._stop.set()
        s.st.running = True
        for _ in range(300):
            clk.t += 30
            s.step()
            if b._pos:
                break
        p = b._pos[0]
        risk = abs(p["price_open"] - p["sl"]) * p["volume"]
        self.assertLessEqual(risk, 1000 * 0.05 + 1.0)    # ~5 % (folga do spread/arredondamento)
        self.assertGreater(risk, 1000 * 0.03)


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


class TestSettings(unittest.TestCase):
    PEP = r"C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe"

    def test_clean_path_variants(self):
        from turbo_ia.settings import clean_path
        self.assertEqual(clean_path(f'"{self.PEP}"'), self.PEP)
        self.assertEqual(clean_path(self.PEP), self.PEP)
        folder = clean_path(r'"C:\Program Files\Pepperstone MetaTrader 5"')
        self.assertTrue(folder.startswith(r"C:\Program Files\Pepperstone MetaTrader 5"))
        self.assertTrue(folder.endswith("terminal64.exe"))

    def test_env_file_and_default(self):
        import os
        import tempfile
        from turbo_ia import settings
        old = {k: os.environ.pop(k, None) for k in ("MT5_PATH", "CAMINHO_MT5", "TIA_TEST")}
        try:
            self.assertEqual(settings.mt5_path(exists=lambda p: p == self.PEP), self.PEP)   # sem .env: padrão da Pepperstone
            self.assertIsNone(settings.mt5_path(exists=lambda p: False))
            with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
                f.write(f'# comentário\nMT5_PATH="{self.PEP}"\nTIA_TEST=1 # nota\n')
            settings.load_env(f.name)
            self.assertEqual(settings.mt5_path(exists=lambda p: False), self.PEP)
            self.assertEqual(os.environ["TIA_TEST"], "1")
            os.unlink(f.name)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class TestServerTime(unittest.TestCase):
    def test_ny_close_server_dst(self):
        import calendar
        from turbo_ia.broker import server_to_utc, us_dst
        summer = calendar.timegm((2026, 7, 1, 19, 0, 0))     # 19h servidor em julho (UTC+3) = 16h UTC
        winter = calendar.timegm((2026, 11, 20, 18, 0, 0))   # 18h servidor em novembro (UTC+2) = 16h UTC
        self.assertTrue(us_dst(summer))
        self.assertFalse(us_dst(winter))
        self.assertEqual((server_to_utc(summer, 2.0) // 3600) % 24, 16)   # fuso de HOJE é +2, mas julho era +3
        self.assertEqual((server_to_utc(winter, 2.0) // 3600) % 24, 16)
        self.assertEqual((server_to_utc(winter, 0.0) // 3600) % 24, 18)   # servidor em UTC: não mexe

    def test_detect_offset(self):
        from turbo_ia.broker import detect_offset
        m = SimpleNamespace(symbol_info_tick=lambda s: SimpleNamespace(time=1_700_000_000 + 2 * 3600 + 30))
        self.assertEqual(detect_offset(m, now=1_700_000_000), 2.0)


class TestManualRisk(unittest.TestCase):
    def test_manual_order_uses_risk_pct(self):
        from turbo_ia.server import App
        b = PaperBroker(seed=2, balance=1000)
        b.connect()
        app = App(b)
        app.session.forward = ForwardLog(None)
        r = app.manual_order({"symbol": "XAUUSD", "timeframe": "M2", "side": "BUY", "volume": 0.5, "risk_pct": 5, "sl_atr": 1, "rr": 3})
        self.assertTrue(r["ok"])
        risk = abs(r["price"] - r["sl"]) * r["volume"]
        self.assertLessEqual(risk, 50 + 1)          # ~5 % de 1000, nunca o lote fixo 0,5
        self.assertNotEqual(r["volume"], 0.5)
