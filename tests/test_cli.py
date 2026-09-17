"""Garante que todos os subcomandos existem e que o parser aceita os argumentos documentados."""

from __future__ import annotations

import unittest

from gold_ai.cli import main

EXPECTED = {"demo", "run", "stats", "event", "live", "status", "backtest", "metrics", "validate", "simulate", "calibrate", "setup"}


class CliTests(unittest.TestCase):
    def test_all_subcommands_present(self):
        import argparse
        from gold_ai import cli

        captured = {}
        orig = argparse.ArgumentParser.parse_args

        def fake(self, args=None, namespace=None):
            for a in self._subparsers._group_actions:
                captured.update(a.choices)
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = fake
        try:
            with self.assertRaises(SystemExit):
                cli.main(["demo"])
        finally:
            argparse.ArgumentParser.parse_args = orig
        self.assertEqual(EXPECTED, set(captured))

    def test_help_of_each_subcommand(self):
        for cmd in EXPECTED:
            with self.assertRaises(SystemExit) as ctx:
                main([cmd, "--help"])
            self.assertEqual(ctx.exception.code, 0, cmd)

    def test_demo_runs(self):
        self.assertEqual(main(["demo", "--scenarios", "neutro"]), 0)


if __name__ == "__main__":
    unittest.main()




class SetupTests(unittest.TestCase):
    """`setup`: checklist Telegram + MetaTrader 5 + .env, com terminal simulado e sem enviar mensagem."""

    def _run(self, mt5, **kw):
        import os
        import tempfile
        from gold_ai.cli import run_setup
        from gold_ai.telegram import TelegramSender
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                with open(".env", "w", encoding="utf-8") as f:
                    f.write("MT5_SYMBOL=XAUUSD\nRISK_PER_TRADE=0.5\n")
                sender = TelegramSender(token="t", chat_id="1", quiet=True)
                return run_setup({"MT5_SYMBOL": "XAUUSD", "RISK_PER_TRADE": "0.5"}, send_test=False, mt5_module=mt5, sender=sender, **kw)
            finally:
                os.chdir(cwd)

    def test_all_green_with_fake_terminal(self):
        from tests.test_mt5 import FakeMT5
        mt5 = FakeMT5()
        ok, lines = self._run(mt5)
        self.assertTrue(ok, lines)
        self.assertTrue(any("MetaTrader 5: conectado" in ln and "XAUUSD" in ln for ln in lines))
        self.assertTrue(any("candles H1" in ln for ln in lines))
        self.assertTrue(any("Telegram" in ln and ln.startswith("✅") for ln in lines))
        self.assertTrue(any("risco/trade 0.5%" in ln for ln in lines))
        self.assertTrue(mt5.shutdown_called)

    def test_red_when_terminal_or_package_missing(self):
        from tests.test_mt5 import FakeMT5
        ok, lines = self._run(FakeMT5(fail_init=True))
        self.assertFalse(ok)
        self.assertTrue(any(ln.startswith("❌ MetaTrader 5") for ln in lines))
        ok, lines = self._run(False)   # módulo ausente (None cai no pacote real; False simula 'não instalado')
        self.assertFalse(ok)

    def test_red_without_telegram_credentials(self):
        import os
        import tempfile
        from gold_ai.cli import run_setup
        from gold_ai.telegram import TelegramSender
        from tests.test_mt5 import FakeMT5
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            try:
                ok, lines = run_setup({}, send_test=False, mt5_module=FakeMT5(), sender=TelegramSender(dry_run=True, quiet=True))
            finally:
                os.chdir(cwd)
        self.assertFalse(ok)
        self.assertTrue(any("Telegram" in ln and ln.startswith("❌") for ln in lines))
        self.assertTrue(any(".env não encontrado" in ln for ln in lines))

    def test_setup_command_runs_without_terminal(self):
        # sem MetaTrader5 instalado (Linux/CI) o comando termina com 1 e explica o que falta, sem quebrar
        self.assertEqual(main(["setup", "--no-message"]), 1)
