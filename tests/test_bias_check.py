"""CHECKUP do GOLD BIAS + PAINEL: diz ✅/⚠️/❌ por peça e a ação para cada problema."""
import os
import tempfile
import threading
import unittest
from unittest import mock

from gold_ai.bias import BiasMemory, BiasNotifier, GoldBiasEngine
from gold_ai.bias_check import CheckReport, check_config, check_memory, check_mt5, check_panel, check_telegram, run_checkup
from gold_ai.dashboard import DashService, dash_serve
from gold_ai.data.mt5 import MT5Client, MT5Config, MT5Source
from gold_ai.sources.sample import SampleSource
from tests.test_mt5 import FakeMT5


class FakeSender:
    dry_run = False

    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, text):
        self.sent.append(text)
        return self.ok


class CheckupTests(unittest.TestCase):
    def test_config_flags_example_values(self):
        rep = CheckReport()
        with mock.patch("gold_ai.bias_check.find_env_file", return_value=".env"):
            check_config(rep, {"TOKEN_TELEGRAM": "123456789:AAxxxxxxxx", "CHAT_ID": "123456789"})
        self.assertEqual([i.status for i in rep.items if "Telegram" in i.name], ["❌", "❌"])
        rep = CheckReport()
        with mock.patch("gold_ai.bias_check.find_env_file", return_value=".env"):
            check_config(rep, {"TOKEN_TELEGRAM": "999:REALTOKEN", "CHAT_ID": "924", "MT5_SYMBOL": "XAUUSD"})
        self.assertFalse(rep.errors)

    def test_mt5_ok_with_terminal(self):
        rep = CheckReport()
        price = check_mt5(rep, {}, client_factory=lambda cfg: MT5Client(cfg, FakeMT5()))
        self.assertAlmostEqual(price, 2700.0)
        self.assertFalse(rep.errors, [i.detail for i in rep.errors])

    def test_mt5_terminal_closed(self):
        rep = CheckReport()
        self.assertIsNone(check_mt5(rep, {}, client_factory=lambda cfg: MT5Client(cfg, FakeMT5(fail_init=True))))
        self.assertEqual(rep.errors[0].name, "Conexão com o terminal")
        self.assertIn("abra o MetaTrader", rep.errors[0].action)

    def test_telegram_test_message(self):
        rep = CheckReport()
        s = FakeSender()
        check_telegram(rep, True, s)
        self.assertEqual(rep.items[-1].status, "✅")
        self.assertIn("CHECKUP", s.sent[0])
        rep = CheckReport()
        check_telegram(rep, True, FakeSender(ok=False))
        self.assertEqual(rep.items[-1].status, "❌")

    def test_memory_and_panel(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "b.db")
            rep = CheckReport()
            check_memory(rep, db)
            self.assertEqual(rep.items[-1].status, "⚠️")
            BiasMemory(db).record(GoldBiasEngine().analyze(SampleSource("venda").snapshot()))
            rep = CheckReport()
            check_memory(rep, db)
            self.assertIn("1 leituras", rep.items[0].detail)
        rep = CheckReport()
        check_panel(rep, port=1)                      # nada rodando
        self.assertEqual(rep.items[0].status, "⚠️")

    def test_panel_live_running(self):
        svc = DashService(MT5Source(MT5Config(), mt5=FakeMT5()), GoldBiasEngine(), None, BiasNotifier(None), None, interval=3600, live_interval=0.5)
        srv = dash_serve(svc, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            rep = CheckReport()
            check_panel(rep, port=srv.server_address[1])
            self.assertEqual([i.status for i in rep.items], ["✅", "✅", "✅"], [i.detail for i in rep.items])
        finally:
            srv.shutdown()
            srv.server_close()
            svc.shutdown()

    def test_full_run_sample(self):
        rep = run_checkup(SampleSource("venda"), None, None, send=False, port=1, mt5=False)
        text = rep.render()
        for part in ("1. CONFIGURAÇÃO", "4. ANÁLISE DA IA", "6. PAINEL", "7. TELEGRAM", "RESULTADO"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
