"""Testes — DOCTOR: painel de funcionamento e eficiência."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from gold_ai.doctor import run_doctor
from gold_ai.history import EventHistory, HistoricalEvent, save_history
from gold_ai.memory import PredictionMemory


class DoctorTests(unittest.TestCase):
    def test_panel_marks_missing_layers_and_actions(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                rep = run_doctor({}, db_path="x.db", events_path=os.path.join("dados", "n.csv"), markets=("XAUUSD",), start=date(2026, 1, 1), end=date(2026, 3, 1),
                                 data_dir="dados")
                txt = rep.render()
                by = {c.layer: c for c in rep.checks}
                self.assertEqual(by[".env"].status, "❌")
                self.assertEqual(by["Banco de eventos"].status, "❌")
                self.assertEqual(by["Alta resolução XAUUSD"].status, "❌")
                self.assertEqual(by["REACTION EDGE"].status, "❌")
                self.assertEqual(by["Memória do live"].status, "⚠️")
                self.assertTrue(all(c.action for c in rep.checks if c.status == "❌"))    # todo ❌ vem com ação
                self.assertIn("EFICIÊNCIA", txt)
                self.assertIn("META +10%/dia", txt)
                self.assertGreater(rep.score[2], 0)
            finally:
                os.chdir(cwd)

    def test_panel_ok_when_layers_present(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                os.makedirs("dados")
                with open(".env", "w") as f:
                    f.write("TOKEN_TELEGRAM=t\nCHAT_ID=1\nMT5_PATH=x\nRISK_PER_TRADE=3\nMAX_DAILY_LOSS=6\nFRED_API_KEY=k\nDAILY_TARGET=10\n")
                env = {"TOKEN_TELEGRAM": "t", "CHAT_ID": "1", "MT5_PATH": "x", "RISK_PER_TRADE": "3", "MAX_DAILY_LOSS": "6", "FRED_API_KEY": "k", "DAILY_TARGET": "10"}
                evs = []
                for w in range(9):
                    t = datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc).replace(day=5 + 0) 
                    from datetime import timedelta
                    t = datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc) + timedelta(days=7 * w)
                    evs.append(HistoricalEvent(t, t, f"C{w}", "CPI MoM", forecast=0.2, actual=0.3, kind="cpi"))
                    for dd in range(7):
                        tt = t + timedelta(days=dd, hours=2)
                        evs.append(HistoricalEvent(tt, tt, f"N{w}{dd}", "geopolitica: x", "GLOBAL", "", "MÉDIO", category="GEOPOLITICAL", headline="strike"))
                save_history(EventHistory(evs), os.path.join("dados", "n.csv"))
                for name in ("XAUUSD_ticks.csv", "XAUUSD_m1.csv", "USDX_ticks.csv"):
                    with open(os.path.join("dados", name), "w") as f:
                        f.write("time,bid,ask\n2026-01-05T13:30:00+00:00,1,1.1\n")
                with open(os.path.join("dados", "reaction_edge.json"), "w") as f:
                    f.write('{"XAUUSD": {"verdict": "🟢", "n": 25, "net_atr": 0.12, "edge_score": 0.9}}')
                mem = PredictionMemory("x.db")
                from gold_ai.opportunity import DecisionRecord
                mem.record_decision(DecisionRecord(datetime.now(timezone.utc), 1.0, 30.0, "ALTA", "ENTRADA", "", 1.0, None, 3, 60.0), "XAUUSD")
                mem.close()
                with open("prova.txt", "w") as f:
                    f.write("x")
                rep = run_doctor(env, db_path="x.db", events_path=os.path.join("dados", "n.csv"), markets=("XAUUSD",), start=date(2026, 1, 5), end=date(2026, 3, 8),
                                 mt5_probe=lambda: (True, "conectado"), telegram_probe=lambda: (True, "ok"), data_dir="dados")
                by = {c.layer: c for c in rep.checks}
                for layer in (".env", "Telegram", "MetaTrader 5", "Banco de eventos", "Alta resolução XAUUSD", "Líder USD (USDX)", "REACTION EDGE", "Memória do live", "Resultado prova.txt"):
                    self.assertEqual(by[layer].status, "✅", layer)
                self.assertEqual(by["Resultado teste_ab.txt"].status, "⚠️")
                self.assertEqual(rep.score[2], 0)
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
