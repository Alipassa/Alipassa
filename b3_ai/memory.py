"""Memória em SQLite: barras intraday acumuladas (o histórico público é curto) e previsões × resultado."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional


class Memory:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bars (sym TEXT, tf TEXT, ts REAL, o REAL, h REAL, l REAL, c REAL, v REAL,
                                             PRIMARY KEY (sym, tf, ts));
            CREATE TABLE IF NOT EXISTS forecasts (asset TEXT, horizon TEXT, made_ts REAL, due_key TEXT, ref REAL, p_up REAL,
                                                  outcome INTEGER, final REAL, PRIMARY KEY (asset, horizon, due_key));
        """)

    # ------------------------------------------------------------------ barras
    def merge_bars(self, sym: str, tf: str, bars: list[tuple], keep: int = 60000) -> list[tuple]:
        """Grava as barras novas e devolve o histórico acumulado (mais longo que os ~5000 do feed público)."""
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO bars VALUES (?,?,?,?,?,?,?,?)",
                                [(sym, tf, *b[:6]) for b in bars])
            self.db.commit()
            rows = self.db.execute("SELECT ts,o,h,l,c,v FROM bars WHERE sym=? AND tf=? ORDER BY ts DESC LIMIT ?",
                                   (sym, tf, keep)).fetchall()
        return rows[::-1]

    # ------------------------------------------------------------------ previsões
    def record(self, asset: str, horizon: str, due_key: str, ref: float, p_up: float, replace: bool = False) -> None:
        sql = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        with self.lock:
            self.db.execute(f"{sql} INTO forecasts (asset, horizon, made_ts, due_key, ref, p_up, outcome, final) "
                            "VALUES (?,?,?,?,?,?,NULL,NULL)", (asset, horizon, time.time(), due_key, ref, p_up))
            self.db.commit()

    def pending(self, asset: str, horizon: str) -> list[tuple]:
        with self.lock:
            return self.db.execute("SELECT due_key, ref, p_up FROM forecasts WHERE asset=? AND horizon=? AND outcome IS NULL",
                                   (asset, horizon)).fetchall()

    def resolve(self, asset: str, horizon: str, due_key: str, final: float, ref: float) -> None:
        outcome = 1 if final > ref else 0 if final < ref else -1
        with self.lock:
            self.db.execute("UPDATE forecasts SET outcome=?, final=? WHERE asset=? AND horizon=? AND due_key=?",
                            (outcome, final, asset, horizon, due_key))
            self.db.commit()

    def scoreboard(self) -> list[dict]:
        """Acerto ao vivo por ativo/horizonte: lado previsto (p ≥ 50 % = alta) × o que o preço fez."""
        with self.lock:
            rows = self.db.execute("SELECT asset, horizon, p_up, outcome FROM forecasts WHERE outcome IN (0,1)").fetchall()
            pend = dict(((a, h), n) for a, h, n in self.db.execute(
                "SELECT asset, horizon, COUNT(*) FROM forecasts WHERE outcome IS NULL GROUP BY 1,2"))
        agg: dict[tuple, list[int]] = {}
        for a, h, p, o in rows:
            if abs(p - 0.5) < 0.005:
                continue     # sem lado: não conta como acerto nem erro
            s = agg.setdefault((a, h), [0, 0])
            s[0] += 1
            s[1] += 1 if (p >= 0.5) == (o == 1) else 0
        keys = sorted(set(agg) | set(pend))
        return [{"ativo": a, "horizonte": h, "n": agg.get((a, h), [0, 0])[0],
                 "acerto": (agg[(a, h)][1] / agg[(a, h)][0]) if agg.get((a, h), [0])[0] else None,
                 "pendentes": pend.get((a, h), 0)} for a, h in keys]

    def recent(self, limit: int = 30) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT asset, horizon, made_ts, due_key, ref, p_up, outcome, final FROM forecasts "
                                   "WHERE horizon IN ('Diário','Semanal') ORDER BY made_ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ativo": r[0], "horizonte": r[1], "feita": r[2], "alvo": r[3], "ref": r[4], "p_up": r[5],
                 "resultado": r[6], "final": r[7]} for r in rows]

    def close(self) -> None:
        with self.lock:
            self.db.close()


def optional_float(x: object) -> Optional[float]:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
