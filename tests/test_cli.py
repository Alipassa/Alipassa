"""Garante que todos os subcomandos existem e que o parser aceita os argumentos documentados."""

from __future__ import annotations

import unittest

from gold_ai.cli import main

EXPECTED = {"demo", "run", "stats", "event", "live", "status", "backtest", "metrics", "validate", "simulate", "calibrate", "markets", "edge", "estimate"}


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
