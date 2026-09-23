"""PAINEL (cockpit do ouro): blocos, confluência, semáforo, cérebro, POSSO ENTRAR?, gráfico, HTTP local e dados manuais."""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import timedelta

from gold_ai.bias import BiasMemory, BiasNotifier, GoldBiasEngine, bias_apply_manual
from gold_ai.dashboard import DASH_HTML, DashService, DashState, dash_fibonacci, dash_levels, dash_serve, dash_vwap_series
from gold_ai.models import EconomicEvent
from gold_ai.sources.sample import SampleSource


def state_for(scenario: str, event_min=None):
    s = SampleSource(scenario=scenario).snapshot()
    if event_min is not None:
        s.events.append(EconomicEvent("CPI EUA", s.time + timedelta(minutes=event_min), "MUITO ALTO", consensus=0.3, previous=0.4, kind="cpi", unit="%"))
    st = DashState(BiasMemory(":memory:"))
    return st, st.update(s, GoldBiasEngine().analyze(s)), s


class DashboardTests(unittest.TestCase):
    def test_all_blocks_present(self):
        _, p, _ = state_for("venda")
        for k in ("price", "bias", "macro", "context", "flow", "technical", "news", "calendar", "confluence", "light", "brain", "entry"):
            self.assertIn(k, p)
        names = {i["name"] for i in p["macro"]}
        self.assertTrue({"DXY", "Treasury 10Y", "Treasury 2Y", "Treasury 30Y", "Juros reais", "FED"} <= names)
        self.assertEqual({i["name"] for i in p["technical"]["items"]}, {"EMA 9/21", "RSI", "MACD", "VWAP", "ADX"})
        json.dumps(p, default=str)                       # serializável

    def test_traffic_light_sell_and_buy(self):
        _, sell, _ = state_for("venda")
        self.assertEqual(sell["light"]["state"], "VENDA")
        self.assertGreaterEqual(sell["confluence"]["value"], 70)
        _, buy, _ = state_for("confirmacao_alta")
        self.assertIn(buy["light"]["state"], ("COMPRA", "AGUARDAR"))
        self.assertNotEqual(buy["light"]["state"], "VENDA")

    def test_divergence_means_wait(self):
        st = DashState()
        light = st.traffic_light({"macro": -60, "fluxo": 30, "tecnico": 70, "noticias": 0, "estrutura": 80}, 10, None)
        self.assertEqual(light["state"], "AGUARDAR")
        self.assertIn("divergência", light["reason"])

    def test_event_forces_wait(self):
        _, p, _ = state_for("venda", event_min=20)
        self.assertEqual(p["light"]["state"], "AGUARDAR")
        self.assertIn("EVENTO", p["light"]["title"])
        self.assertIn("CPI EUA", p["entry"]["event_warning"])

    def test_brain_explains(self):
        _, p, _ = state_for("venda")
        b = p["brain"]
        self.assertIn("DXY ganhou força.", b["facts"])
        self.assertTrue(b["dominant"].startswith("Juros reais") or b["dominant"])
        self.assertTrue(b["conclusion"].startswith("Cenário baixista"))

    def test_entry_plan_and_lot(self):
        st, p, s = state_for("venda")
        e = st.entry_for(200.0)
        plan = e["plan"]
        self.assertEqual(plan["side"], "VENDA")
        self.assertGreater(plan["stop"], plan["entry"])
        self.assertLess(plan["target"], plan["entry"])
        self.assertAlmostEqual(abs(plan["target"] - plan["entry"]), 2 * plan["risk_points"], delta=0.02)
        self.assertLessEqual(plan["lots"] * plan["risk_per_lot_usd"], 200.0 + 1e-6)
        self.assertIn("não envia ordens", e["note"])

    def test_chart_series(self):
        st, _, _ = state_for("confirmacao_alta")
        for tf in ("M1", "M5", "M15", "M30", "H1", "H4", "D1"):
            ch = st.chart(tf)
            self.assertTrue(ch["candles"], tf)
            c = ch["candles"][-1]
            for k in ("o", "h", "l", "c", "e9", "e21", "e50", "vw", "sd"):
                self.assertIn(k, c)
        ch = st.chart("H1")
        self.assertIsNotNone(ch["candles"][-1]["e200"])
        self.assertIn(ch["fib"]["direction"], ("alta", "baixa"))
        self.assertTrue(all(v > ch["price"] for v in ch["levels"]["resistances"]))
        self.assertTrue(all(v < ch["price"] for v in ch["levels"]["supports"]))

    def test_vwap_resets_each_session_and_fib(self):
        s = SampleSource(scenario="neutro").snapshot()
        h1 = s.candles["H1"]
        vw = dash_vwap_series(h1, "H1")
        self.assertEqual(len(vw), len(h1))
        self.assertEqual(dash_fibonacci(h1)["levels"][0]["ratio"], 0.0)
        lv = dash_levels(h1, s.price, 9.0)
        self.assertLessEqual(len(lv["supports"]), 3)

    def test_buy_sell_alert_text(self):
        st, p, s = state_for("venda")
        st.prev_light = "AGUARDAR"
        txt = st.buy_sell_alert(p["light"], s, st.reading, p["macro"], p["technical"])
        self.assertIn("🚨 ALERTA DE VENDA", txt)
        self.assertIn("XAU/USD: 2.650,00", txt)
        self.assertIsNone(st.buy_sell_alert(p["light"], s, st.reading, p["macro"], p["technical"]), "sem repetir o mesmo alerta")

    def test_manual_data(self):
        s = SampleSource(scenario="neutro").snapshot()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"china_demand": 0.5, "central_bank_buying_tonnes": 20, "eventos": [
                    {"name": "FOMC", "time": (s.time + timedelta(hours=3)).isoformat(), "impact": "MUITO ALTO", "kind": "fomc"}]}, fh)
            applied = bias_apply_manual(s, path)
        self.assertEqual(s.china_demand, 0.5)
        self.assertIn("evento FOMC", applied)
        self.assertEqual(bias_apply_manual(s, "/nao/existe.json"), [])

    def test_html_is_self_contained(self):
        self.assertIn("POSSO ENTRAR?", DASH_HTML)
        self.assertNotIn('"""', DASH_HTML)
        self.assertNotRegex(DASH_HTML, r"<script[^>]+src=|<link[^>]+stylesheet")   # nada externo: funciona offline


class DashboardHttpTests(unittest.TestCase):
    def test_http_endpoints(self):
        svc = DashService(SampleSource("venda"), GoldBiasEngine(), BiasMemory(":memory:"), BiasNotifier(None), None, interval=3600)
        srv = dash_serve(svc, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            get = lambda path: opener.open(base + path, timeout=30).read()  # noqa: E731
            self.assertIn(b"GOLD MARKET INTELLIGENCE", get("/"))
            st = json.loads(get("/api/state"))
            self.assertEqual(st["bias"]["label"], "BAIXA")
            self.assertTrue(json.loads(get("/api/chart?tf=M15"))["candles"])
            self.assertIn("plan", json.loads(get("/api/entry?risk=100")))
            self.assertTrue(json.loads(get("/api/refresh"))["ok"])
            self.assertEqual(len(json.loads(get("/api/history"))), 1)   # 2º ciclo dentro de record_every: não grava de novo
            self.assertTrue(any(a["kind"] == "alerta_entrada" for a in st["alerts"]))
        finally:
            svc.stop_event.set()
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()


class DashboardMT5Tests(unittest.TestCase):
    def test_panel_reads_broker_prices(self):
        """Mesmo caminho do live/bias: MT5Source (corretora) → painel; fonte indicada como MT5 conectado."""
        from gold_ai.data.mt5 import MT5Config, MT5Source
        from tests.test_mt5 import FakeMT5

        svc = DashService(MT5Source(MT5Config(), mt5=FakeMT5()), GoldBiasEngine(), None, None, None, interval=3600)
        p = svc.cycle()
        self.assertEqual(p["status"].get("mt5"), "ok")
        self.assertAlmostEqual(p["price"]["price"], 2700.0)
        self.assertTrue(svc.state.chart("H1")["candles"])
