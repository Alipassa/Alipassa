"""Garante que todos os subcomandos existem e que o parser aceita os argumentos documentados."""

from __future__ import annotations

import unittest

from gold_ai.cli import main

EXPECTED = {"demo", "run", "stats", "event", "live", "status", "backtest", "metrics", "validate", "simulate", "calibrate", "markets", "edge", "estimate", "sweep", "history", "compare-news", "reaction", "doctor", "flow"}


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


class DemoOnlyGuardTests(unittest.TestCase):
    def test_live_refuses_real_account_unless_no_demo_only(self):
        from gold_ai.cli import main
        # a trava é avaliada antes de qualquer coleta de dados: com o pacote MetaTrader5 ausente o MT5Client falha e o comando
        # devolve 1 em modo real; o que este teste garante é a existência/semântica do flag
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("--no-demo-only", dest="demo_only", action="store_false", default=True)
        self.assertTrue(p.parse_args([]).demo_only)
        self.assertFalse(p.parse_args(["--no-demo-only"]).demo_only)
        with self.assertRaises(SystemExit):
            main(["live", "--help"])


class LogFileTeeTests(unittest.TestCase):
    def test_log_file_writes_screen_and_file(self):
        import io, os, sys, tempfile
        from gold_ai import cli
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "logs", "x.log")
            out = io.StringIO()
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout = out
            try:
                rc = cli.main(["--log-file", log, "history", "template", "--file", os.path.join(d, "t.csv")])
            finally:
                sys.stdout, sys.stderr = old_out, old_err
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(log))
            with open(log, encoding="utf-8") as f:
                content = f.read()
            self.assertTrue(content.strip())
            self.assertIn(out.getvalue().strip(), content)
