"""Regressão da auditoria do GOLD BIAS + PAINEL: cada teste corresponde a um defeito encontrado e corrigido."""
import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from gold_ai.bias import (BiasMemory, BiasNotifier, GoldBiasEngine, bias_apply_manual, bias_classify, bias_source_credibility,
                          format_bias_closing, format_bias_message)
from gold_ai.dashboard import DashService, DashState, dash_ema, dash_item, dash_serve, dash_tf_indicators
from gold_ai.models import Candle, EconomicEvent, MarketSnapshot
from gold_ai.sources.sample import SampleSource

T0 = datetime(2026, 9, 14, tzinfo=timezone.utc)


def reading(scenario="premove_alta"):
    return GoldBiasEngine().analyze(SampleSource(scenario).snapshot())


def with_score(base, score, minutes=0):
    r = copy.copy(base)
    r.score, r.label, r.news = score, bias_classify(score)[0], []
    r.time = base.time + timedelta(minutes=minutes)
    return r


class NotifierAudit(unittest.TestCase):
    def test_reversal_through_neutral(self):
        n, e = BiasNotifier(None), GoldBiasEngine()
        kinds = [[k for k, _ in n.decide(e.analyze(SampleSource(sc).snapshot()))] for sc in ("premove_alta", "neutro", "venda")]
        self.assertEqual(kinds[-1], ["reversao"])

    def test_slow_drift_is_reported(self):
        n, base = BiasNotifier(None), reading()
        out = [[k for k, _ in n.decide(with_score(base, sc, i * 5))] for i, sc in enumerate((41, 49, 57, 65, 69))]
        self.assertIn("alerta", out[-1])

    def test_no_flip_flop_at_boundary(self):
        n, base = BiasNotifier(None), reading()
        out = [[k for k, _ in n.decide(with_score(base, sc, i * 5))] for i, sc in enumerate((45, 39.9, 40.0, 39.9, 40.0))]
        self.assertEqual(out[1:], [[], [], [], []])

    def test_state_file_robust_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            with open(p, "w") as fh:
                fh.write("[1, 2]")
            n = BiasNotifier(p)
            self.assertEqual([k for k, _ in n.decide(reading())], ["relatorio"])
            self.assertIsInstance(json.load(open(p)), dict)
            self.assertFalse(os.path.exists(p + ".tmp"))


class MemoryAudit(unittest.TestCase):
    def setUp(self):
        self.cs = [Candle(T0 + timedelta(hours=h), 0, 0, 0, 100 + h) for h in range(10)]

    def test_price_at_never_looks_ahead(self):
        self.assertEqual(BiasMemory.price_at(self.cs, T0 + timedelta(hours=3, minutes=27)), 102)

    def test_price_at_outside_history(self):
        self.assertIsNone(BiasMemory.price_at(self.cs, T0 - timedelta(days=5)))
        self.assertIsNone(BiasMemory.price_at(self.cs, T0 + timedelta(hours=30)))

    def test_price_at_weekend_gap_uses_last_close(self):
        gap = self.cs[:4] + [Candle(T0 + timedelta(hours=h), 0, 0, 0, 100 + h) for h in range(60, 70)]
        self.assertEqual(BiasMemory.price_at(gap, T0 + timedelta(hours=30)), 103)

    def test_factor_power_counted_once_per_prediction(self):
        mem = BiasMemory(":memory:")
        s = SampleSource("premove_alta").snapshot()
        mem.record(GoldBiasEngine().analyze(s))
        later = [Candle(s.time + timedelta(hours=h), 0, 0, 0, s.price * (1 + 0.002 * h)) for h in range(0, 130)]
        self.assertEqual(mem.resolve(later, s.time + timedelta(hours=130)), 3)
        self.assertEqual(mem.stats()["fatores"]["juros_reais"], [1, 1])

    def test_closing_compares_with_morning_not_itself(self):
        mem = BiasMemory(":memory:")
        src = SampleSource("premove_alta")
        morning = GoldBiasEngine().analyze(src.snapshot())
        mem.record(morning, "manha")
        src.now += timedelta(hours=8)
        src.price *= 1.01
        closing = GoldBiasEngine().analyze(src.snapshot())
        txt = format_bias_closing(closing, mem)
        self.assertIn("desde o relatório da manhã", txt)
        self.assertIn("+1.00%", txt)
        self.assertIn("ACERTO", txt)

    def test_local_day_after_21h_brt(self):
        mem = BiasMemory(":memory:")
        r = reading()
        r.time = datetime(2026, 9, 14, 23, 30, tzinfo=timezone.utc)          # 20:30 em Brasília
        mem.record(r, "manha")
        self.assertEqual(len(mem.today(datetime(2026, 9, 15, 1, 0, tzinfo=timezone.utc))), 1)   # 22:00 BRT, mesmo dia local


class InputAudit(unittest.TestCase):
    def test_manual_json_bad_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.json")
            for content in ("[1]", '{"eventos": 5}', '{"china_demand": true}'):
                with open(p, "w") as fh:
                    fh.write(content)
                s = SampleSource("neutro").snapshot()
                self.assertEqual(bias_apply_manual(s, p), [])
                self.assertIsNone(s.china_demand)
            with open(p, "w") as fh:
                json.dump({"china_demand": "0.4", "eventos": [{"name": "CPI", "time": "2026-09-14T11:00:00Z", "consensus": "0.3", "actual": "0.5", "kind": "cpi"}]}, fh)
            s = SampleSource("neutro").snapshot()
            bias_apply_manual(s, p)
            self.assertEqual(s.china_demand, 0.4)
            GoldBiasEngine().analyze(s)                        # texto numérico não derruba o fator de inflação

    def test_source_credibility_word_boundaries(self):
        self.assertLess(bias_source_credibility("Bear Market Blog"), 1.0)
        self.assertEqual(bias_source_credibility("Reuters", "Fed minutes from May meeting"), 0.85)
        self.assertLess(bias_source_credibility("Reuters", "Fed may cut, sources say"), 0.85)

    def test_nan_is_no_data(self):
        s = SampleSource("neutro").snapshot()
        s.dxy_change_pct, s.vix = float("nan"), float("inf")
        r = GoldBiasEngine().analyze(s)
        self.assertFalse(r.factor("dolar").available)
        p = DashState().update(s, r)
        json.dumps(p, allow_nan=False, default=str)
        self.assertIsNone(dash_item("x", float("nan"))["value"])

    def test_no_data_means_no_confidence(self):
        r = GoldBiasEngine().analyze(MarketSnapshot())
        self.assertLessEqual(r.confidence, 10)
        self.assertIn("DADOS INSUFICIENTES", format_bias_message(r))

    def test_stale_cot_ignored(self):
        s = SampleSource("neutro").snapshot()
        s.etf_flow_musd = s.order_flow_imbalance = None
        s.cot_managed_money_net_change, s.cot_age_days = 40000, 60
        self.assertFalse(GoldBiasEngine().analyze(s).factor("fluxo").available)


class PanelAudit(unittest.TestCase):
    def state(self, scenario="venda"):
        s = SampleSource(scenario).snapshot()
        st = DashState()
        return st, st.update(s, GoldBiasEngine().analyze(s)), s

    def test_weak_bias_has_no_plan(self):
        _, p, _ = self.state("pre_evento")
        self.assertIsNone(p["entry"]["plan"])

    def test_price_zero_waits(self):
        s = SampleSource("venda").snapshot()
        s.price = 0.0
        p = DashState().update(s, GoldBiasEngine().analyze(s))
        self.assertEqual(p["light"]["state"], "AGUARDAR")
        self.assertIsNone(p["entry"]["plan"])

    def test_released_event_waits_15_min(self):
        s = SampleSource("venda").snapshot()
        s.events.append(EconomicEvent("CPI EUA", s.time - timedelta(minutes=5), "MUITO ALTO", consensus=0.3, actual=0.3, kind="cpi"))
        p = DashState().update(s, GoldBiasEngine().analyze(s))
        self.assertEqual(p["light"]["state"], "AGUARDAR")
        self.assertIn("RECÉM-DIVULGADO", p["light"]["title"])

    def test_lot_rounding(self):
        st, _, _ = self.state()
        plan = st.entry_for(None)["plan"]
        risk = 0.29 * plan["risk_per_lot_usd"]
        self.assertEqual(st.entry_for(risk)["plan"]["lots"], 0.29)
        self.assertIn("lots_note", st.entry_for(1.0)["plan"])

    def test_entry_alert_not_repeated_when_light_flickers(self):
        st, p, s = self.state()
        sent = 0
        for light in ("VENDA", "AGUARDAR", "VENDA", "AGUARDAR", "VENDA"):
            if st.buy_sell_alert({**p["light"], "state": light}, s, st.reading, p["macro"], p["technical"]):
                sent += 1
        self.assertEqual(sent, 1)

    def test_ema_seeded_with_sma(self):
        vals = [float(i) for i in range(30)]
        e = dash_ema(vals, 10)
        self.assertIsNone(e[8])
        self.assertAlmostEqual(e[9], 4.5)

    def test_vwap_anchor_follows_timeframe(self):
        d1 = SampleSource("neutro").snapshot().candles["D1"]
        self.assertNotAlmostEqual(dash_tf_indicators(d1, "D1")["vwap"], (d1[-1].high + d1[-1].low + d1[-1].close) / 3, places=3)


class PanelHttpAudit(unittest.TestCase):
    def test_bad_params_host_and_errors(self):
        svc = DashService(SampleSource("venda"), GoldBiasEngine(), BiasMemory(":memory:"), BiasNotifier(None), None, interval=3600)
        srv = dash_serve(svc, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for q in ("inf", "1e400", "nan", "-5", "abc"):
                e = json.loads(op.open(f"{base}/api/entry?risk={q}", timeout=30).read())
                self.assertNotIn("lots", e["plan"] or {}, q)
            bad = urllib.request.Request(base + "/api/state", headers={"Host": "evil.example.com"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                op.open(bad, timeout=30)
            self.assertEqual(ctx.exception.code, 403)
            st = json.loads(op.open(base + "/api/state", timeout=30).read())
            self.assertIsInstance(st["alerts"], list)
        finally:
            srv.shutdown()
            srv.server_close()
            svc.shutdown()


if __name__ == "__main__":
    unittest.main()
