"""Registro de acerto em tempo real (teste em DEMO): cada entrada da IA é conferida `expiry_min` minutos depois.

Mede a mesma coisa que o backtest — o preço no fechamento do candle da entrada × o fechamento `expiry_min` minutos depois —
independente de como a posição saiu no MT5. Fica gravado em JSONL (padrão ~/turbo_ia_registro.jsonl) e sobrevive a reinícios.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional

from .backtest import TF_SEC, wilson

DEFAULT_PATH = os.environ.get("TURBO_IA_REGISTRO") or os.path.join(os.path.expanduser("~"), "turbo_ia_registro.jsonl")


class ForwardLog:
    def __init__(self, path: Optional[str] = DEFAULT_PATH, payout: float = 0.78) -> None:
        self.path = path
        self.payout = payout
        self.pending: list[dict] = []
        self.done: list[dict] = []
        self.lock = threading.RLock()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        self.done.append(json.loads(line))
                    except ValueError:
                        continue

    def add(self, symbol: str, tf: str, mode: str, side: str, bar_time: int, entry: float, expiry_min: int = 10, account: str = "",
            broker: str = "") -> None:
        with self.lock:
            self.pending.append({"symbol": symbol, "tf": tf, "mode": mode, "side": side, "bar_time": int(bar_time), "entry": entry,
                                 "expiry_min": expiry_min, "account": account, "broker": broker})

    def resolve(self, broker: Any) -> list[dict]:
        """Confere as entradas cujo candle de expiração já fechou."""
        out = []
        with self.lock:
            for p in list(self.pending):
                sec = TF_SEC.get(p["tf"], 60)
                target = p["bar_time"] + p["expiry_min"] * 60
                try:
                    cs = broker.candles(p["symbol"], p["tf"], max(10, p["expiry_min"] * 60 // sec + 5))
                except Exception:  # noqa: BLE001
                    continue
                if not cs or int(cs[-1]["time"]) <= target:          # candle-alvo ainda não fechou
                    continue
                c = next((x for x in cs if int(x["time"]) == target), None)
                self.pending.remove(p)
                if c is None:                                         # buraco no histórico: descarta sem contar
                    continue
                win = c["close"] > p["entry"] if p["side"] == "BUY" else c["close"] < p["entry"]
                rec = {**p, "exit": c["close"], "win": bool(win)}
                self.done.append(rec)
                out.append(rec)
                if self.path:
                    with open(self.path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec) + "\n")
        return out

    def stats(self, mode: Optional[str] = None, broker: Optional[str] = None) -> dict:
        """`broker` separa o simulador (paper) do MT5: resultado de simulador não prova nada sobre o mercado real."""
        with self.lock:
            rs = [r for r in self.done if (mode is None or r.get("mode") == mode) and (broker is None or r.get("broker") == broker)]
            n, k = len(rs), sum(r["win"] for r in rs)
            lo, hi = wilson(k, n)
            be = 1 / (1 + self.payout)
            if n < 100:
                verdict = f"coletando: {n}/100 operações no mínimo antes de concluir qualquer coisa"
            elif lo > be:
                verdict = f"VANTAGEM CONFIRMADA: mesmo o pior caso ({lo:.1%}) supera o empate de {be:.1%}"
            elif hi < be:
                verdict = f"SEM VANTAGEM: mesmo o melhor caso ({hi:.1%}) fica abaixo do empate de {be:.1%} — abandone"
            else:
                verdict = f"inconclusivo: {k / n:.1%}, faixa {lo:.1%}–{hi:.1%} contém o empate de {be:.1%} — continue em demo"
            return {"mode": mode, "broker": broker, "n": n, "wins": k, "rate": k / n if n else 0.0, "ci": [lo, hi], "breakeven": be,
                    "pending": len(self.pending), "verdict": verdict, "last": rs[-10:]}
