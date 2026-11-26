import random
import urllib.error
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


class TestCandleSpread(unittest.TestCase):
    def test_uses_candle_spread_and_commission(self):
        from turbo_ia.backtest import simulate_rr, WINDOW
        c = [{"time": 120 * i, "open": 100, "high": 100.5, "low": 99.5, "close": 100} for i in range(WINDOW + 5)]
        c[WINDOW]["high"] = 104                                    # alvo 3 × 1 atingido no candle seguinte ao sinal
        sig = [(WINDOW - 1, "BUY", 1.0)]
        r0 = simulate_rr(c, sig, 3, 1.0, spread=0.30)
        c[WINDOW - 1]["spread"] = 0.10
        r1 = simulate_rr(c, sig, 3, 1.0, spread=0.30)
        r2 = simulate_rr(c, sig, 3, 1.0, spread=0.30, commission=0.07)
        self.assertAlmostEqual(r0["total_r"], 3 - 0.30)
        self.assertAlmostEqual(r1["total_r"], 3 - 0.10)            # spread real da vela ganha do padrão
        self.assertAlmostEqual(r2["total_r"], 3 - 0.17)


class TestResearch(unittest.TestCase):
    def test_variants_run_and_time_exit(self):
        from turbo_ia import research
        clk = Clock()
        b = PaperBroker(seed=9, clock=clk)
        b.connect()
        c = b.candles("XAUUSD", "M2", 1500)
        f = research.features(c)
        for ex in research.EXITS:
            r = research.simulate(c, f, dict(research.BASE, exit=ex, z=0.5), 0, len(c), 0.0, 0.0)
            self.assertTrue(all(x >= -1.0 - 1e-9 for x in r))          # nunca perde mais que o stop (sem custo)
            if ex == "4R":
                self.assertTrue(all(x <= 4 + 1e-9 for x in r))


class TestZThreshold(unittest.TestCase):
    def test_default_is_2_and_configurable(self):
        from turbo_ia.analysis import REVERSION_Z, analyze
        self.assertEqual(REVERSION_Z, 2.0)
        c = stretched(16, up=True)
        z = analyze(c, "reversao", z_thr=0.5).reasons[0]          # "preço +X ATR acima…"
        zval = float(z.split()[1])
        self.assertEqual(analyze(c, "reversao", z_thr=zval - 0.1).signal, "SELL")
        self.assertEqual(analyze(c, "reversao", z_thr=zval + 0.1).signal, "NONE")
        self.assertEqual(IAConfig.from_dict({"z_thr": "2.5"}).z_thr, 2.5)


class TestOnlyReversion(unittest.TestCase):
    def test_server_rejects_other_modes(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from turbo_ia.server import App, make_handler
        b = PaperBroker(seed=1)
        b.connect()
        app = App(b)
        app.session.forward = ForwardLog(None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def post(body):
                req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ia/start", json.dumps(body).encode(),
                                             {"Content-Type": "application/json"})
                return json.loads(urllib.request.urlopen(req).read())
            self.assertFalse(post({"mode": "veloz"})["ok"])
            self.assertTrue(post({"mode": "reversao", "symbol": "XAUUSD", "timeframe": "M2"})["ok"])
            self.assertEqual(app.session.cfg.mode, "reversao")
        finally:
            app.session.stop()
            srv.shutdown()
            srv.server_close()


class TestSweep(unittest.TestCase):
    def test_sweep_runs_on_paper_data(self):
        import io
        from contextlib import redirect_stdout
        from turbo_ia import research
        b = PaperBroker(seed=4)
        b.connect()
        buf = io.StringIO()
        with redirect_stdout(buf):
            research.sweep(lambda tf: b.candles("XAUUSD", tf, 3000), ["M1", "M2"], 0.0)
        out = buf.getvalue()
        self.assertIn("M1", out)
        self.assertIn("regra atual", out)


class TestM3(unittest.TestCase):
    def test_paper_m3_candles(self):
        b = PaperBroker(seed=1)
        b.connect()
        c = b.candles("XAUUSD", "M3", 50)
        self.assertTrue(all((y["time"] - x["time"]) % 180 == 0 for x, y in zip(c, c[1:])))
        self.assertEqual(MT5Broker.TF["M3"], "TIMEFRAME_M3")


class TestStopBlock(unittest.TestCase):
    def _session(self, block):
        clk = Clock()
        b = PaperBroker(seed=3, clock=clk)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.start(IAConfig(symbol="Volatility 10 Index", mode="veloz", volume=1, target_profit=0, loss_limit=0, protect=False,
                         stop_block=block))
        s._stop.set()
        s.st.running = True
        return s, clk

    def test_window_block_skips_after_stop(self):
        s, clk = self._session("window")
        s.st.last_bar = 0
        # simula um stop registrado na janela atual
        t = s.broker.candles(s.cfg.symbol, s.cfg.timeframe, 3)[-2]["time"]
        s.st.window_stops[str(int(t // 3600))] = 1
        clk.t += 61
        s.step()
        self.assertTrue(any("trava" in l["msg"] for l in s.st.log) or s.st.last_analysis["signal"] == "NONE")

    def test_loss_is_counted_per_window_and_side(self):
        s, _ = self._session("dir")
        s.st.open_ticket, s.st.open_side, s.st.open_window = 999, "SELL", 123
        s.broker.closed_profit = lambda t: -10.0
        s.broker.positions = lambda sym=None: []
        s._manage_open()
        self.assertEqual(s.st.window_stops, {"123": 1, "123|SELL": 1})


class TestPercentLimits(unittest.TestCase):
    def _s(self, balance, **kw):
        b = PaperBroker(seed=1, balance=balance)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.start(IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2", **kw))
        s._stop.set()
        s.st.running = True
        return s

    def test_loss_pct_scales_with_balance(self):
        for bal in (1000, 11000):
            s = self._s(bal, loss_pct=50)
            self.assertAlmostEqual(s.limits()["loss"], bal * 0.5, places=0)
            s.st.pnl = -bal * 0.49
            self.assertFalse(s._check_limits())
            s.st.pnl = -bal * 0.51
            self.assertTrue(s._check_limits())
            self.assertIn("limite de perda", s.st.stopped_reason)

    def test_target_and_protection_pct_and_no_trade_cap(self):
        s = self._s(11000, target_pct=10, loss_pct=0, protect=True, protect_after_pct=20, protect_keep_pct=10)
        self.assertEqual(s.limits()["loss"], 0)
        s.st.trades = 500                                     # sem limite de operações por padrão
        s.st.pnl, s.st.peak = 1000, 1000
        self.assertFalse(s._check_limits())
        s.st.pnl = 1100
        self.assertTrue(s._check_limits())                    # meta de 10 % de 11.000
        s2 = self._s(11000, protect=True, protect_after_pct=20, protect_keep_pct=10)
        s2.st.peak, s2.st.pnl = 2300, 1000                    # passou de +20 % e voltou abaixo de +10 %
        self.assertTrue(s2._check_limits())
        self.assertIn("proteção", s2.st.stopped_reason)


class TestDailyBrake(unittest.TestCase):
    def test_brake_blocks_rest_of_day_and_releases_next_day(self):
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.start(IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2", daily_loss_pct=10))
        s._stop.set()
        s.st.running = True
        s._roll_day()
        self.assertAlmostEqual(s.st.day_start_equity, 1000, places=0)
        s.st.day_pnl = -99
        self.assertFalse(s._daily_brake())
        s.st.day_pnl = -101
        self.assertTrue(s._daily_brake())
        self.assertTrue(any("FREIO DIÁRIO" in l["msg"] for l in s.st.log))
        s.st.day = "2000-01-01"                       # virou o dia
        s._roll_day()
        self.assertFalse(s.st.day_braked)
        self.assertEqual(s.st.day_pnl, 0.0)
        self.assertFalse(s._daily_brake())
        self.assertTrue(s.st.running)                 # o freio não encerra a sessão


class TestAudit(unittest.TestCase):
    """Auditoria 26/11/2026: um teste por falha corrigida."""

    def test_offset_ignores_stale_tick_and_uses_cache(self):
        import os
        import tempfile
        from turbo_ia import broker as br
        old = br.OFFSET_CACHE
        br.OFFSET_CACHE = os.path.join(tempfile.mkdtemp(), "off.json")
        try:
            now = 1_700_000_000
            fresh = SimpleNamespace(symbol_info_tick=lambda s: SimpleNamespace(time=now + 2 * 3600 + 20))
            stale = SimpleNamespace(symbol_info_tick=lambda s: SimpleNamespace(time=now - 40 * 3600 + 7 * 60))  # fim de semana
            self.assertIsNone(br.fresh_offset(stale, now))
            self.assertEqual(br.detect_offset(stale, now), 0.0)        # sem cache ainda
            self.assertEqual(br.detect_offset(fresh, now), 2.0)        # grava o bom
            self.assertEqual(br.detect_offset(stale, now), 2.0)        # fim de semana: usa o último bom, não 0
        finally:
            br.OFFSET_CACHE = old

    def test_mt5_calls_are_locked_and_constants_pass(self):
        from turbo_ia.broker import _LockedMT5
        import threading
        lk = threading.RLock()
        m = _LockedMT5(FakeMT5(), lk)
        self.assertEqual(m.TRADE_RETCODE_DONE, 10009)
        self.assertEqual(m.symbol_info_tick("X").bid, 100.0)

    def test_closed_profit_waits_for_exit_deal(self):
        fm = FakeMT5()
        fm.DEAL_ENTRY_IN, fm.DEAL_ENTRY_OUT = 0, 1
        deals = [SimpleNamespace(entry=0, profit=0.0, commission=-0.7, swap=0.0)]
        fm.history_deals_get = lambda position=None: deals
        b = MT5Broker(mt5=fm)
        self.assertIsNone(b.closed_profit(1))                      # antes: devolvia -0,70 e contava como perda
        deals.append(SimpleNamespace(entry=1, profit=150.0, commission=-0.7, swap=0.0))
        self.assertAlmostEqual(b.closed_profit(1), 148.6)

    def test_from_dict_int_fields_accept_decimals(self):
        self.assertEqual(IAConfig.from_dict({"duration_candles": "1.5", "max_trades": "3"}).duration_candles, 1)

    def test_start_adopts_open_ai_position(self):
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        r = b.open("XAUUSD", "SELL", 0.05, None, None)
        s = IASession(b, forward=ForwardLog(None))
        s.start(IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2"))
        s.stop()
        self.assertEqual(s.st.open_ticket, r["ticket"])
        self.assertTrue(any("já estava aberta" in l["msg"] for l in s.st.log))

    def _forced_signal_session(self, spread):
        from turbo_ia.analysis import Analysis
        import turbo_ia.engine as eng
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        price = b.tick("XAUUSD")[0]
        b.tick = lambda sym: (price, price + spread)
        s = IASession(b, forward=ForwardLog(None))
        s.start(IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2", risk_pct=2, daily_loss_pct=0))
        s._stop.set()
        s.st.running, s.st.last_bar = True, 0
        orig = eng.analyze
        eng.analyze = lambda *a, **k: Analysis("SELL", 80, price, 1.0, price + 1.0, price - 3.0, mode="reversao", label="x")
        try:
            s.step()
        finally:
            eng.analyze = orig
        return s, b

    def test_normal_spread_trades_with_live_price(self):
        s, b = self._forced_signal_session(0.05)
        self.assertEqual(len(b._pos), 1)
        risk = abs(b._pos[0]["sl"] - b._pos[0]["price_open"]) * b._pos[0]["volume"]
        self.assertLessEqual(risk, 1000 * 0.02 + 0.5)

    def test_abnormal_spread_skips(self):
        s, b = self._forced_signal_session(0.5)                    # spread 0,5 com stop de 1,0
        self.assertEqual(b._pos, [])
        self.assertTrue(any("spread anormal" in l["msg"] for l in s.st.log))

    def test_daily_result_survives_restart(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        f = ForwardLog(os.path.join(d, "reg.jsonl"))
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        s1 = IASession(b, forward=f)
        s1.cfg = IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2")
        s1._roll_day()
        s1.st.day_pnl = -80.0
        s1._save_day()
        s2 = IASession(b, forward=f)                               # painel reiniciado no mesmo dia
        s2.cfg = IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2", daily_loss_pct=10)
        s2._roll_day()
        self.assertEqual(s2.st.day_pnl, -80.0)
        s2.st.day_pnl -= 25
        self.assertTrue(s2._daily_brake())                         # −105 de 1000 passa do freio de 10 %

    def test_server_rejects_foreign_host_header(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from turbo_ia.server import App, make_handler
        b = PaperBroker(seed=1)
        b.connect()
        app = App(b)
        app.session.forward = ForwardLog(None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/api/status"
            self.assertEqual(urllib.request.urlopen(url).status, 200)
            req = urllib.request.Request(url, headers={"Host": "evil.example.com"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            self.assertEqual(e.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()


class TestTelegram(unittest.TestCase):
    def _n(self):
        from turbo_ia.notify import TelegramNotifier
        sent = []
        n = TelegramNotifier("TOKEN", "12345", post=lambda url, data: (sent.append((url, data)), {"ok": True})[1])
        return n, sent

    def test_disabled_without_credentials(self):
        from turbo_ia.notify import TelegramNotifier
        n = TelegramNotifier("", "")
        self.assertFalse(n.enabled)
        self.assertFalse(n.send("x"))

    def test_sends_in_background_and_dedups(self):
        n, sent = self._n()
        self.assertTrue(n.send("a", dedup_key="e1"))
        self.assertFalse(n.send("a de novo", dedup_key="e1"))   # mesmo erro em loop: não repete
        n.flush()
        self.assertEqual(len(sent), 1)
        self.assertIn("botTOKEN/sendMessage", sent[0][0])
        self.assertEqual(sent[0][1]["chat_id"], "12345")

    def test_bad_token_does_not_retry_forever(self):
        from turbo_ia.notify import TelegramNotifier
        calls = []
        n = TelegramNotifier("T", "1", post=lambda u, d: (calls.append(1), {"ok": False, "error_code": 401, "description": "Unauthorized"})[1])
        n.send("x")
        n.flush()
        self.assertEqual(len(calls), 1)
        self.assertIn("Unauthorized", n.last_error)

    def test_session_notifies_trade_close_and_brake(self):
        n, sent = self._n()
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        s = IASession(b, forward=ForwardLog(None), notifier=n)
        s.cfg = IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2", daily_loss_pct=10)
        s._roll_day()
        s.st.open_ticket, s.st.open_side, s.st.open_window = 77, "SELL", 1
        b.closed_profit = lambda t: -120.0
        b.positions = lambda sym=None: []
        s._manage_open()
        self.assertTrue(s._daily_brake())
        n.flush()
        texts = "\n".join(d["text"] for _, d in sent)
        self.assertIn("❌ PERDA #77: -120.00", texts)
        self.assertIn("FREIO DIÁRIO", texts)
        self.assertIn("DEMO PAPER", texts)


class TestProgram(unittest.TestCase):
    """Itens 2–5 e 7: cada variação faz o que diz, sem olhar o futuro, e o cofre fica antes do corte."""

    def bars(self, closes, t0=0, tf=120, rng=0.2):
        out, prev = [], closes[0]
        for i, cl in enumerate(closes):
            out.append({"time": t0 + i * tf, "open": prev, "high": max(prev, cl) + rng, "low": min(prev, cl) - rng, "close": cl})
            prev = cl
        return out

    def test_impulse_rules(self):
        from turbo_ia.program import impulsive
        c = self.bars([100] * 10 + [101, 102, 103, 104, 105])
        self.assertTrue(impulsive(c, 1.0, 14, False, ("move", 5, 3.0)))     # subiu 5 em 5 velas → venda bloqueada
        self.assertFalse(impulsive(c, 1.0, 14, True, ("move", 5, 3.0)))     # o filtro só olha o lado da entrada
        self.assertTrue(impulsive(c, 1.0, 14, False, ("streak", 5)))
        self.assertFalse(impulsive(c, 1.0, 14, False, ("streak", 6)))
        self.assertFalse(impulsive(c, 1.0, 14, False, ("bar", 2.0)))

    def _sim(self, v, path):
        from turbo_ia import program as pg
        hour16 = 16 * 3600
        closes = [100.0] * 40 + path
        c = self.bars(closes, t0=hour16 - 40 * 120, rng=0.0)
        f = {"sma": [100.0] * len(c), "atr": [1.0] * len(c)}
        c[40]["close"] = 103.0                                 # esticada de +3 ATR às 16h → VENDA
        c[40]["high"] = max(c[40]["high"], 103.0)
        return pg.simulate(c, f, {**pg.BASE, **v}, 0, len(c), 0.0)

    def test_fixed_3r_and_partial(self):
        down = [103.0, 102.0, 100.5, 99.0, 98.0, 97.0]         # cai 6 → alvo 3R (3 abaixo de 103)
        self.assertAlmostEqual(self._sim({}, down)[0][1], 3.0)
        r = self._sim({"exit": ("partial", 1.0)}, [103.0, 101.8, 102.5, 104.5])  # +1R parcial, volta e passa do zero a zero
        self.assertAlmostEqual(r[0][1], 0.5)                   # 50 % × 1R; o resto saiu no zero a zero (0)

    def test_rejection_waits_for_reversal_candle(self):
        r_now = self._sim({}, [104.5, 104.5])                   # entra a 103 e é stopado a 104
        self.assertAlmostEqual(r_now[0][1], -1.0)
        r_rej = self._sim({"entry": "reject"}, [104.0, 105.0, 106.0, 107.0])  # nunca rejeita → não entra
        self.assertEqual(r_rej, [])

    def test_cofre_is_before_cutoff(self):
        from turbo_ia import program as pg
        c = self.bars([100 + (i % 7) * 0.3 for i in range(4000)], t0=pg.CUTOFF - 2000 * 120)
        res = pg.evaluate(c, 0.0, pg.CUTOFF)
        self.assertAlmostEqual(res["cofre_dias"], 2000 * 120 / 86400, delta=0.01)
        self.assertIn("COFRE", res["rows"]["regra atual"])


class TestCosts(unittest.TestCase):
    def test_real_stop_from_25_11(self):
        from turbo_ia.costs import analyze_positions, report
        from turbo_ia.broker import MAGIC
        D = lambda **k: SimpleNamespace(magic=MAGIC, position_id=433283554, commission=0.0, swap=0.0, fee=0.0, **k)  # noqa: E731
        deals = [D(entry=0, type=1, order=433283554, price=3552.45, volume=0.12, profit=0.0, time=1764108120),
                 D(entry=1, type=0, order=433283600, price=3556.89, volume=0.12, profit=-53.28, time=1764108420)]
        orders = [SimpleNamespace(ticket=433283554, sl=3556.89, tp=3541.14, price_open=3552.50)]
        mt5 = SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_TYPE_BUY=0,
                              history_deals_get=lambda a, b: deals, history_orders_get=lambda position=None: orders)
        for d in deals:
            d.comment = ""
        ex = {433283554: {"signal_close": 3552.60, "bid": 3552.50, "ask": 3552.60, "sl": 3556.89, "tp": 3541.14}}
        rows = analyze_positions(mt5, 30, ex, now=1764108420)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["kind"], "stop")
        self.assertAlmostEqual(r["exit_slip"], 0.0)            # saiu exatamente no stop
        self.assertAlmostEqual(r["entry_slip_req"], 0.05, places=6)   # vendeu 5 centavos ABAIXO do pedido = pior
        self.assertAlmostEqual(r["entry_slip_bar"], 0.15, places=6)   # e 15 centavos abaixo do fechamento da vela
        self.assertAlmostEqual(r["spread"], 0.10, places=6)
        txt = report(rows)
        self.assertIn("CUSTO TOTAL REAL", txt)


class TestCostsFallbacks(unittest.TestCase):
    def test_exit_deal_without_magic_and_sl_from_comment(self):
        from turbo_ia.costs import analyze_positions
        from turbo_ia.broker import MAGIC
        deals = [SimpleNamespace(magic=MAGIC, position_id=9, entry=0, type=1, order=9, price=3552.45, volume=0.12, profit=0.0,
                                 time=1, commission=0, swap=0, fee=0, comment="TurboIA reversao"),
                 SimpleNamespace(magic=0, position_id=9, entry=1, type=0, order=10, price=3556.89, volume=0.12, profit=-53.28,
                                 time=2, commission=0, swap=0, fee=0, comment="[sl 3556.89]")]
        mt5 = SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_TYPE_BUY=0, history_deals_get=lambda a, b: deals,
                              history_orders_get=lambda position=None: [SimpleNamespace(ticket=9, sl=0, tp=0, price_open=3552.45)])
        rows = analyze_positions(mt5, 30, {}, now=100)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "stop")
        self.assertAlmostEqual(rows[0]["dist"], 4.44, places=2)


class TestNewAccount(unittest.TestCase):
    def test_new_account_does_not_inherit_day(self):
        import os
        import tempfile
        f = ForwardLog(os.path.join(tempfile.mkdtemp(), "reg.jsonl"))
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        s1 = IASession(b, forward=f)
        s1.cfg = IAConfig(mode="reversao", symbol="XAUUSD", timeframe="M2")
        s1._roll_day()
        s1.st.day_pnl = -90.0
        s1._save_day()
        info = b.info
        b.info = lambda: {**info(), "login": "NOVA"}            # outra conta, mesmo dia
        s2 = IASession(b, forward=f)
        s2.cfg = s1.cfg
        s2._roll_day()
        self.assertEqual(s2.st.day_pnl, 0.0)


class TestValidate(unittest.TestCase):
    def rows(self, rs, slip=0.0):
        return [{"r": r, "dist": 1.0, "kind": "stop" if r < 0 else "alvo", "exit_slip": 0.0, "fee_r": 0.0,
                 "entry_slip_bar": slip} for r in rs]

    def events(self, now, cover=True, errors=0):
        from turbo_ia.validate import expected_windows
        ev = [{"t": now - 15 * 86400, "kind": "start", "login": "1"}]          # registro começou há 15 dias
        ev += [{"t": now - 3600, "kind": "janela", "janela": w, "login": "1"} for w in (expected_windows(now) if cover else [])]
        ev += [{"t": now - 60, "kind": "error", "msg": "x", "login": "1"} for _ in range(errors)]
        return ev

    def test_all_pass(self):
        from turbo_ia.validate import evaluate, report
        now = 1_764_000_000
        rs = ([3.0, -1.0, -1.0] * 40)[:120]                    # +0,33 R/op, PF 1,5
        checks = evaluate(self.rows(rs, slip=0.02), self.events(now), "1", now=now)
        self.assertTrue(all(ok for _, ok, _ in checks), checks)
        self.assertIn("VALIDADO", report(checks))

    def test_fails_with_few_trades_and_bad_halves(self):
        from turbo_ia.validate import evaluate, report
        now = 1_764_000_000
        rs = [3.0, -1.0] * 20 + [-1.0] * 20                     # 60 ops, últimas 30 negativas
        checks = dict((n.split(" · ")[0], ok) for n, ok, _ in evaluate(self.rows(rs), self.events(now, errors=5), "1", now=now))
        self.assertFalse(checks["1"])
        self.assertFalse(checks["3"])
        self.assertFalse(checks["7"])                            # 5 erros
        self.assertIn("AINDA NÃO", report(evaluate(self.rows(rs), [], "1", now=now)))

    def test_window_coverage_counts_only_since_logging_started(self):
        from turbo_ia.validate import evaluate
        now = 1_764_000_000
        c7 = lambda ev: [(ok, d) for n, ok, d in evaluate(self.rows([1.0]), ev, "1", now=now) if n.startswith("7")][0]  # noqa: E731
        self.assertIsNone(c7([])[0])                                   # sem registro: "ainda não começou", não ✗
        ok, detail = c7([{"t": now - 2 * 86400, "kind": "start", "login": "1"}])
        self.assertIsNone(ok)                                          # só 2 dias: ainda contando
        self.assertIn("2 de 14 dias", detail)

    def test_cost_criterion_needs_20_logged_trades(self):
        from turbo_ia.validate import evaluate
        now = 1_764_000_000
        rows = self.rows([1.0] * 10)
        c6 = [ok for n, ok, _ in evaluate(rows, [], "1", now=now) if n.startswith("6")][0]
        self.assertIsNone(c6)
        rows = self.rows([1.0] * 30, slip=0.20)                  # 0,20 R de derrapagem: reprova
        c6 = [ok for n, ok, _ in evaluate(rows, [], "1", now=now) if n.startswith("6")][0]
        self.assertFalse(c6)


class TestCross(unittest.TestCase):
    def test_cross_follows_trend_and_reverses(self):
        import math
        from turbo_ia.cross import simulate
        closes = [100 + 10 * math.sin(i / 25) for i in range(600)]   # ondas longas: cruzamentos claros
        c = [{"time": i * 60, "open": p, "high": p + 0.3, "low": p - 0.3, "close": p} for i, p in enumerate(closes)]
        r = simulate(c, "ema", None, False, 0.0)
        self.assertGreater(len(r), 3)
        self.assertGreater(sum(x[2] for x in r), 0)                  # em tendências longas o cruzamento ganha
        r_stop = simulate(c, "ema", 2.0, False, 0.0)
        self.assertTrue(all(x[1] >= -2.0 - 1e-9 for x in r_stop))   # stop de 2 ATR limita a perda


class TestCrossMode(unittest.TestCase):
    def wave(self, n=200, up_at=None):
        import math
        cl = [100 + 5 * math.sin(i / 12) for i in range(n)]
        return [{"time": 1_700_000_000 + i * 3600, "open": p, "high": p + 0.5, "low": p - 0.5, "close": p} for i, p in enumerate(cl)]

    def test_detects_cross_only_on_cross_bar(self):
        from turbo_ia.analysis import analyze, ind
        c = self.wave()
        cl = [x["close"] for x in c]
        f, s = ind.sma(cl, 9), ind.sma(cl, 20)
        ups = [i for i in range(21, len(c)) if f[i - 1] <= s[i - 1] and f[i] > s[i]]
        i = ups[0]
        a = analyze(c[:i + 1], "cruzamento", sl_atr=2.0)
        self.assertEqual(a.signal, "BUY")
        self.assertAlmostEqual(a.price - a.sl, 2 * a.atr)
        self.assertIsNone(a.tp)                                       # sai no cruzamento contrário, sem alvo
        self.assertEqual(analyze(c[:i + 2], "cruzamento").signal, "NONE")   # vela seguinte: já cruzou

    def test_reverses_on_opposite_cross_and_records_result(self):
        from turbo_ia.analysis import Analysis
        import turbo_ia.engine as eng
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.cfg = IAConfig(mode="cruzamento", symbol="XAUUSD", timeframe="H1", risk_pct=2, sl_atr=2.0, rr=0, daily_loss_pct=0)
        s.st.running, s.st.last_bar = True, 0
        price = b.tick("XAUUSD")[0]
        orig = eng.analyze
        try:
            eng.analyze = lambda *a, **k: Analysis("BUY", 100, price, 3.0, price - 6.0, None, mode="cruzamento", label="x")
            s.step()
            first = s.st.open_ticket
            self.assertIsNotNone(first)
            self.assertEqual(s.st.open_side, "BUY")
            s.st.last_bar = 0
            eng.analyze = lambda *a, **k: Analysis("SELL", 100, price, 3.0, price + 6.0, None, mode="cruzamento", label="x")
            s.step()
        finally:
            eng.analyze = orig
        self.assertEqual(s.st.open_side, "SELL")                     # virou a mão
        self.assertNotEqual(s.st.open_ticket, first)
        self.assertEqual(len(b._pos), 1)                              # nunca duas posições ao mesmo tempo
        s._manage_open()
        self.assertEqual(len(s.st.history), 1)                        # a compra fechada entrou no resultado
        self.assertEqual(s.st.closing, [])

    def test_same_side_signal_keeps_position(self):
        from turbo_ia.analysis import Analysis
        import turbo_ia.engine as eng
        b = PaperBroker(seed=1, balance=1000)
        b.connect()
        s = IASession(b, forward=ForwardLog(None))
        s.cfg = IAConfig(mode="cruzamento", symbol="XAUUSD", timeframe="H1", risk_pct=2, sl_atr=2.0, rr=0, daily_loss_pct=0)
        s.st.running, s.st.last_bar = True, 0
        price = b.tick("XAUUSD")[0]
        orig = eng.analyze
        try:
            eng.analyze = lambda *a, **k: Analysis("BUY", 100, price, 3.0, price - 6.0, None, mode="cruzamento", label="x")
            s.step()
            s.st.last_bar = 0
            s.step()
        finally:
            eng.analyze = orig
        self.assertEqual(len(b._pos), 1)


class TestValidateCross(unittest.TestCase):
    def test_cross_thresholds_are_its_own(self):
        from turbo_ia.validate import evaluate
        now = 1_764_000_000
        # o que o histórico 2016–2026 sugere: +0,065 R (em stop de 2 ATR), PF ~1,15
        rs = ([1.4, -0.5, -0.5, -0.27] * 30)[:120]
        rows = [{"r": r, "dist": 1.0, "kind": "stop", "exit_slip": 0.0, "fee_r": 0.0, "entry_slip_bar": 0.005} for r in rs]
        rev = dict((n.split(" · ")[0], ok) for n, ok, _ in evaluate(rows, [], "1", now=now, mode="reversao"))
        cru = dict((n.split(" · ")[0], ok) for n, ok, _ in evaluate(rows, [], "1", now=now, mode="cruzamento"))
        self.assertFalse(rev["2"])           # +0,0325 R não serve para a reversão…
        self.assertFalse(cru["2"])           # …nem para o cruzamento (precisa ≥ +0,05)
        rs2 = ([1.6, -0.5, -0.5, -0.27] * 30)[:120]                  # +0,0825 R
        rows2 = [{**r, "r": x} for r, x in zip(rows, rs2)]
        cru2 = dict((n.split(" · ")[0], ok) for n, ok, _ in evaluate(rows2, [], "1", now=now, mode="cruzamento"))
        rev2 = dict((n.split(" · ")[0], ok) for n, ok, _ in evaluate(rows2, [], "1", now=now, mode="reversao"))
        self.assertTrue(cru2["2"])
        self.assertFalse(rev2["2"])

    def test_cross_presence_counts_all_weekday_hours(self):
        from turbo_ia.validate import expected_windows
        now = 1_764_000_000
        self.assertGreater(len(expected_windows(now, hours=tuple(range(1, 21)))), 5 * len(expected_windows(now)))


class TestRegimeIndicators(unittest.TestCase):
    def series(self, trend):
        import math
        cl = [100 + (0.5 * i if trend else (1.5 if i % 2 else -1.5) + 0.3 * math.sin(i / 7)) for i in range(120)]
        return [{"time": i * 3600, "open": p, "high": p + 0.6, "low": p - 0.6, "close": p} for i, p in enumerate(cl)], cl

    def test_trend_vs_range(self):
        from turbo_ia import indicators as ind
        ct, clt = self.series(True)
        cr, clr = self.series(False)
        self.assertGreater(ind.adx(ct)[-1], 40)                    # tendência forte
        self.assertLess(ind.adx(cr)[-1], ind.adx(ct)[-1])
        self.assertLess(ind.choppiness(ct)[-1], 38.2)              # tendência → CHOP baixo
        self.assertGreater(ind.choppiness(cr)[-1], 50)             # zigue-zague → CHOP alto
        self.assertGreater(ind.efficiency_ratio(clt)[-1], 0.9)
        self.assertLess(ind.efficiency_ratio(clr)[-1], 0.3)

    def test_filter_blocks_entry_but_not_exit(self):
        from turbo_ia.cross import simulate
        import math
        cl = [100 + 10 * math.sin(i / 25) for i in range(800)]
        c = [{"time": i * 3600, "open": p, "high": p + 0.3, "low": p - 0.3, "close": p} for i, p in enumerate(cl)]
        base = simulate(c, "sma", 2.0, False, 0.0)
        filt = simulate(c, "sma", 2.0, False, 0.0, regime="ADX>25")
        self.assertLessEqual(len(filt), len(base))


class TestExits(unittest.TestCase):
    def test_exit_rules(self):
        import math
        from turbo_ia.cross import EXITS, simulate_exit
        cl = [100 + 10 * math.sin(i / 25) for i in range(800)]
        c = [{"time": i * 3600, "open": p, "high": p + 0.3, "low": p - 0.3, "close": p} for i, p in enumerate(cl)]
        res = {n: simulate_exit(c, r, 0.0) for n, r in EXITS.items()}
        self.assertTrue(all(len(v) > 3 for v in res.values()))
        base = res["cruzamento contrário (atual)"]
        self.assertGreater(sum(x[2] for x in base), 0)
        self.assertTrue(all(x[1] >= -2.0 - 1e-9 for v in res.values() for x in v))      # nunca perde mais que o stop inicial
        half = res["metade em +3 ATR"]
        self.assertEqual(len(half), len(base))                                          # mesmas entradas


class TestCandidates(unittest.TestCase):
    def test_only_new_data_counts_and_rule(self):
        import math
        from turbo_ia.validate import candidates, candidates_report, CAND_START
        cl = [3000 + 60 * math.sin(i / 40) + 5 * math.sin(i / 3) for i in range(4000)]
        t0 = CAND_START - 2000 * 3600
        c = [{"time": t0 + i * 3600, "open": p, "high": p + 3, "low": p - 3, "close": p} for i, p in enumerate(cl)]
        rows = candidates(c, 0.3)
        self.assertEqual(len(rows), 3)
        old = candidates(c, 0.3, start=t0 + 4000 * 3600)               # nada depois do início → 0 operações
        self.assertTrue(all(r["n"] == 0 for r in old))
        txt = candidates_report(rows)
        self.assertIn("referência", txt)
        self.assertIn("só dados novos", txt)
        few = [dict(r, n=5) for r in rows]
        self.assertIn("acompanhando (5/100", candidates_report(few))
        win = [rows[0], dict(rows[1], n=150, total=rows[0]["total"] + 10, dd=rows[0]["dd"]), rows[2]]
        self.assertIn("VENCEU", candidates_report(win))


class TestRobust(unittest.TestCase):
    def test_custom_periods_and_year_stats(self):
        import math
        from turbo_ia.cross import simulate, _year_stats
        cl = [100 + 10 * math.sin(i / 30) for i in range(1500)]
        c = [{"time": 1_600_000_000 + i * 3600, "open": p, "high": p + 0.3, "low": p - 0.3, "close": p} for i, p in enumerate(cl)]
        a = simulate(c, "sma", 2.0, False, 0.0, fast=9, slow=20)
        b = simulate(c, "sma", 2.0, False, 0.0, fast=20, slow=50)
        self.assertNotEqual(len(a), len(b))                       # médias diferentes, cruzamentos diferentes
        st = _year_stats([(t, r) for t, r, _ in a], [2020])
        self.assertAlmostEqual(st["tot"], sum(r for _, r, _ in a))
        self.assertGreaterEqual(st["dd"], 0)


class TestEnsemble(unittest.TestCase):
    def _ens(self, balance=10_000, **kw):
        from turbo_ia.ensemble import EnsembleSession
        b = PaperBroker(seed=2, balance=balance)
        b.connect()
        e = EnsembleSession(b, forward=ForwardLog(None))
        e.start(IAConfig(mode="ensemble", symbol="XAUUSD", risk_pct=2, daily_loss_pct=10, loss_pct=50, **kw))
        e._stop.set()                                   # sem thread: o teste dirige os ciclos
        e.st.running = True
        return e, b

    def test_three_systems_own_magic_and_risk(self):
        from turbo_ia.analysis import Analysis
        import turbo_ia.engine as eng
        e, b = self._ens()
        self.assertEqual([c.cfg.label for c in e.children], ["9×20 H1", "20×50 H1", "9×20 M30"])
        self.assertEqual(len({c.cfg.magic for c in e.children}), 3)
        self.assertTrue(all(abs(c.cfg.risk_pct - 2 / 3) < 1e-9 for c in e.children))
        price = b.tick("XAUUSD")[0]
        orig = eng.analyze
        try:
            eng.analyze = lambda *a, **k: Analysis("BUY", 100, price, 3.0, price - 6.0, None, mode="cruzamento", label="x")
            for c in e.children:
                c.st.last_bar = 0
            e.step()
        finally:
            eng.analyze = orig
        self.assertEqual(len(b._pos), 3)                                    # uma posição por sistema
        self.assertEqual(sorted(p["magic"] for p in b._pos), sorted(c.cfg.magic for c in e.children))
        st = e.state()
        self.assertEqual(st["trades"], 3)
        self.assertEqual(len(st["open_tickets"]), 3)

    def test_reversal_closes_only_its_own_position(self):
        from turbo_ia.analysis import Analysis
        import turbo_ia.engine as eng
        e, b = self._ens()
        price = b.tick("XAUUSD")[0]
        orig = eng.analyze
        try:
            eng.analyze = lambda *a, **k: Analysis("BUY", 100, price, 3.0, price - 6.0, None, mode="cruzamento", label="x")
            for c in e.children:
                c.st.last_bar = 0
            e.step()
            first = e.children[0]
            first.st.last_bar = 0
            eng.analyze = lambda *a, **k: Analysis("SELL", 100, price, 3.0, price + 6.0, None, mode="cruzamento", label="x")
            first.step()                                                  # só o 1º sistema vê o cruzamento contrário
        finally:
            eng.analyze = orig
        sides = sorted(p["side"] for p in b._pos)
        self.assertEqual(sides, ["BUY", "BUY", "SELL"])

    def test_portfolio_daily_brake_and_loss_limit(self):
        e, b = self._ens()
        e.st.day_start_equity = b.info()["equity"] / 0.85                   # patrimônio 15 % abaixo do começo do dia
        e._portfolio_limits()
        self.assertTrue(e.st.day_braked)
        self.assertTrue(all(c.block_entries for c in e.children))
        e.st.start_equity = b.info()["equity"] / 0.4                        # 60 % abaixo do início → para
        e._portfolio_limits()
        self.assertFalse(e.st.running)
        self.assertIn("limite de perda", e.st.stopped_reason)

    def test_small_account_warning(self):
        b = PaperBroker(seed=2, balance=1000)
        b.connect()
        b.spec = lambda sym: {"digits": 2, "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100, "value_per_unit": 100.0}
        s = IASession(b, forward=ForwardLog(None))
        s.cfg = IAConfig(mode="cruzamento", symbol="XAUUSD", timeframe="H1", risk_pct=0.5, sl_atr=2.0)
        self.assertIn("CONTA PEQUENA", s.min_lot_check())
        s.cfg = IAConfig(mode="cruzamento", symbol="XAUUSD", timeframe="H1", risk_pct=50, sl_atr=2.0)
        self.assertEqual(s.min_lot_check(), "")

    def test_server_switches_to_ensemble(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from turbo_ia.server import App, make_handler
        from turbo_ia.ensemble import EnsembleSession
        b = PaperBroker(seed=1)
        b.connect()
        app = App(b)
        app.session.forward = ForwardLog(None)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ia/start",
                                         json.dumps({"mode": "ensemble", "symbol": "XAUUSD", "risk_pct": 2}).encode(),
                                         {"Content-Type": "application/json"})
            self.assertTrue(json.loads(urllib.request.urlopen(req).read())["ok"])
            self.assertIsInstance(app.session, EnsembleSession)
            st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/status").read())
            self.assertTrue(st["session"]["running"])
            self.assertEqual(len(st["session"]["systems"]), 3)
        finally:
            app.session.stop()
            srv.shutdown()
            srv.server_close()
