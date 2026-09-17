"""5.2 — EFICIÊNCIA DO DIA: episódios, entradas, captura, R/USD, lote travado, motivos, R não operado."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


class EfficiencyTests(unittest.TestCase):
    def test_day_report_counts_episodes_entries_and_missed(self):
        from gold_ai.efficiency import day_report
        from gold_ai.memory import PredictionMemory
        from gold_ai.opportunity import DecisionRecord
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            # 3 análises em SETUP na mesma hora (1 episódio), 1 OPPORTUNITY não operada com R hipotético, 1 entrada
            for k in range(3):
                mem.record_decision(DecisionRecord(T0 + timedelta(minutes=k), 1.15, -30.0, "BAIXA", "RULES", "SEM SINAL — faltou: |score| 30 < 50", 0.002),
                                    symbol="EURUSD", stage="VANTAGEM", is_raw=True)
            rid = mem.record_decision(DecisionRecord(T0 + timedelta(hours=1), 1.15, -60.0, "BAIXA", "RULES", "BLOQUEADA — exposição de carteira", 0.002),
                                      symbol="EURUSD", stage="SINAL", is_raw=True)
            mem.conn.execute("UPDATE decisions SET r_hipotetico=1.5, resolvido=1 WHERE id=?", (rid,))
            mem.record_decision(DecisionRecord(T0 + timedelta(hours=2), 1.15, -65.0, "BAIXA", "ENTRADA", "", 0.002), symbol="EURUSD", stage=None, is_raw=True)
            mem.conn.execute("INSERT INTO trades (aberta_em, modo, direcao, entrada, stop, ativo, lote, risco_usd, capital, risco_pct, resultado_r, "
                             "resultado_financeiro, tempo_operacao_min, motivo_saida, fechada_em, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             ((T0 + timedelta(hours=2)).isoformat(), "LIVE", "BAIXA", 1.15, 1.152, "EURUSD", 1.0, 150.0, 50000.0, 3.0, 0.8, 120.0, 45.0,
                              "TARGET", (T0 + timedelta(hours=3)).isoformat(), "CLOSED"))
            mem.conn.commit()
            rep = day_report(mem, T0, ["EURUSD", "XAUUSD"])
            m = rep.markets[0]
            self.assertEqual(m.analyses, 5)
            self.assertEqual(m.episodes_setup, 3)              # 12h (SETUP), 13h (OPORT.), 14h (entrada)
            self.assertEqual(m.episodes_opportunity, 2)        # 13h e 14h
            self.assertEqual(m.entries, 1)
            self.assertEqual(m.closed, 1)
            self.assertAlmostEqual(m.r_sum, 0.8)
            self.assertAlmostEqual(m.usd_sum, 120.0)
            self.assertEqual(m.missed_n, 1)
            self.assertAlmostEqual(m.missed_r, 1.5)
            self.assertAlmostEqual(m.risk_planned, 1500.0)
            self.assertAlmostEqual(m.risk_real, 150.0)
            txt = rep.render()
            self.assertIn("EFICIÊNCIA DO DIA", txt)
            self.assertIn("lote travado", txt)                  # 150 / 1500 = 10% do planejado
            self.assertIn("faltou", txt)
            self.assertEqual(rep.markets[1].analyses, 0)
            mem.close()
