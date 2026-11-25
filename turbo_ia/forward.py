"""Registro de acerto em tempo real (teste em DEMO), medido como no backtest e independente de como a posição saiu no MT5:

- entrada COM alvo e stop ("rr"): anda candle a candle desde a entrada; o primeiro tocado decide (os dois no mesmo candle = stop);
  resultado em R (+rr ou −1);
- entrada SEM alvo ("expiry"): preço no fechamento do candle da entrada × fechamento `expiry_min` minutos depois.
 Fica gravado em JSONL (padrão ~/turbo_ia_registro.jsonl) e sobrevive a reinícios.
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
            broker: str = "", sl: Optional[float] = None, tp: Optional[float] = None) -> None:
        rec = {"symbol": symbol, "tf": tf, "mode": mode, "side": side, "bar_time": int(bar_time), "entry": entry,
               "expiry_min": expiry_min, "account": account, "broker": broker, "kind": "expiry"}
        if sl and tp:
            rec.update(kind="rr", sl=sl, tp=tp, rr=round(abs(tp - entry) / abs(entry - sl), 2))
        with self.lock:
            self.pending.append(rec)

    def _resolve_rr(self, p: dict, broker: Any) -> Optional[dict]:
        sec = TF_SEC.get(p["tf"], 60)
        cs = broker.candles(p["symbol"], p["tf"], 3)
        if not cs:
            return None
        n = min(5000, int((int(cs[-1]["time"]) - p["bar_time"]) // sec) + 3)
        cs = broker.candles(p["symbol"], p["tf"], n)[:-1]              # só candles fechados
        buy = p["side"] == "BUY"
        for c in cs:
            if int(c["time"]) <= p["bar_time"]:
                continue
            if (c["low"] <= p["sl"]) if buy else (c["high"] >= p["sl"]):
                return {**p, "exit": p["sl"], "win": False, "r": -1.0, "exit_time": int(c["time"])}
            if (c["high"] >= p["tp"]) if buy else (c["low"] <= p["tp"]):
                return {**p, "exit": p["tp"], "win": True, "r": p["rr"], "exit_time": int(c["time"])}
        return None

    def resolve(self, broker: Any) -> list[dict]:
        """Confere as entradas cujo candle de expiração já fechou."""
        out = []
        with self.lock:
            for p in list(self.pending):
                if p.get("kind") == "rr":
                    try:
                        rec = self._resolve_rr(p, broker)
                    except Exception:  # noqa: BLE001
                        continue
                    if rec is None:
                        continue
                    self.pending.remove(p)
                    self._save(rec)
                    out.append(rec)
                    continue
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
                self._save(rec)
                out.append(rec)
        return out

    def _save(self, rec: dict) -> None:
        self.done.append(rec)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

    def stats(self, mode: Optional[str] = None, broker: Optional[str] = None, kind: Optional[str] = None, rr_hint: float = 2.0) -> dict:
        """`broker` separa o simulador (paper) do MT5: resultado de simulador não prova nada sobre o mercado real.
        `kind` "rr" (alvo × stop) ou "expiry" (direção em N min); sem ele usa o tipo da maioria dos registros."""
        with self.lock:
            rs = [r for r in self.done if (mode is None or r.get("mode") == mode) and (broker is None or r.get("broker") == broker)]
            if kind is None:
                kind = "rr" if sum(r.get("kind") == "rr" for r in rs) * 2 >= len(rs) and rs else "expiry"
            rs = [r for r in rs if r.get("kind", "expiry") == kind]
            n, k = len(rs), sum(r["win"] for r in rs)
            lo, hi = wilson(k, n)
            if kind == "rr":
                rr = sum(r["rr"] for r in rs) / n if rs else rr_hint
                be = 1 / (1 + rr)
            else:
                rr = None
                be = 1 / (1 + self.payout)
            if n < 100:
                verdict = f"coletando: {n}/100 operações no mínimo antes de concluir qualquer coisa"
            elif lo > be:
                verdict = f"VANTAGEM CONFIRMADA: mesmo o pior caso ({lo:.1%}) supera o empate de {be:.1%}"
            elif hi < be:
                verdict = f"SEM VANTAGEM: mesmo o melhor caso ({hi:.1%}) fica abaixo do empate de {be:.1%} — abandone"
            else:
                verdict = f"inconclusivo: {k / n:.1%}, faixa {lo:.1%}–{hi:.1%} contém o empate de {be:.1%} — continue em demo"
            exp_r = sum(r.get("r", 0.0) for r in rs) / n if kind == "rr" and n else None
            return {"mode": mode, "broker": broker, "kind": kind, "rr": rr, "exp_r": exp_r,
                    "total_r": sum(r.get("r", 0.0) for r in rs) if kind == "rr" else None, "n": n, "wins": k, "rate": k / n if n else 0.0, "ci": [lo, hi], "breakeven": be,
                    "pending": len(self.pending), "verdict": verdict, "last": rs[-10:]}
