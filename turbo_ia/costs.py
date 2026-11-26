"""Item 6 — custo REAL de execução das operações da IA, medido no histórico do MT5.

    python -m turbo_ia.costs --days 30

Para cada posição da IA (número mágico 260611):
  • derrapagem na entrada = preço executado × preço pedido (ordem) — e × fechamento da vela do sinal, quando houver o
    registro `turbo_ia_execucao.jsonl` (é o preço que o backtest supõe)
  • spread no envio (do registro)
  • derrapagem no stop/alvo = preço de saída × nível do SL/TP
  • comissão e swap
Tudo convertido em R (fração do risco da operação) e comparado com o custo que o backtest desconta (spread da vela).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .broker import MAGIC


def load_exec_log(path: Optional[str] = None) -> dict:
    path = path or os.path.join(os.path.expanduser("~"), "turbo_ia_execucao.jsonl")
    out = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out[int(r["ticket"])] = r
                except Exception:  # noqa: BLE001
                    continue
    return out


DIAG: dict = {}


def _is_ours(d: Any) -> bool:
    return getattr(d, "magic", None) == MAGIC or str(getattr(d, "comment", "")).startswith("TurboIA")


def _sl_from_comment(txt: str) -> float:
    """Fechamento por stop no MT5 vem com comentário "[sl 3556.89]"; por alvo, "[tp 3541.14]"."""
    import re
    m = re.search(r"\[(?:sl|tp)\s+([0-9.]+)\]", txt or "")
    return float(m.group(1)) if m else 0.0


def analyze_positions(mt5: Any, days: int = 30, exec_log: Optional[dict] = None, now: Optional[float] = None) -> list[dict]:
    now = now or time.time()
    frm, to = int(now - days * 86400), int(now + 86400)          # segundos desde 1970: aceito por todas as versões do pacote
    raw = mt5.history_deals_get(frm, to)
    if raw is None and hasattr(mt5, "last_error"):
        DIAG["erro_mt5"] = str(mt5.last_error())
        raw = mt5.history_deals_get(datetime.fromtimestamp(frm, tz=timezone.utc), datetime.fromtimestamp(to, tz=timezone.utc))
    raw = list(raw or [])
    deals = [d for d in raw if _is_ours(d)]
    # posições abertas pela IA: os negócios de saída por SL/TP às vezes vêm com magic 0 → junta tudo da mesma posição
    ours = {int(d.position_id) for d in deals}
    deals = [d for d in raw if int(getattr(d, "position_id", 0)) in ours]
    DIAG.update(negocios_no_periodo=len(raw), posicoes_da_ia=len(ours), sem_stop=0)
    IN, OUT = getattr(mt5, "DEAL_ENTRY_IN", 0), getattr(mt5, "DEAL_ENTRY_OUT", 1)
    by_pos: dict = {}
    for d in deals:
        by_pos.setdefault(int(d.position_id), []).append(d)
    exec_log = exec_log or {}
    rows = []
    for pid, ds in by_pos.items():
        din = next((d for d in ds if d.entry == IN), None)
        douts = [d for d in ds if d.entry != IN]
        if din is None or not douts:
            continue
        buy = din.type == getattr(mt5, "DEAL_TYPE_BUY", 0)
        sgn = 1 if buy else -1
        orders = mt5.history_orders_get(position=pid) or []
        oin = next((o for o in orders if int(o.ticket) == int(din.order)), orders[0] if orders else None)
        sl = float(getattr(oin, "sl", 0) or 0)
        tp = float(getattr(oin, "tp", 0) or 0)
        req = float(getattr(oin, "price_open", 0) or 0)
        ex = exec_log.get(pid) or exec_log.get(int(din.order))
        if ex:
            sl, tp = sl or float(ex.get("sl") or 0), tp or float(ex.get("tp") or 0)
        for d in douts:                                   # último recurso: "[sl 3556.89]" no comentário do fechamento
            lv = _sl_from_comment(str(getattr(d, "comment", "")))
            if lv and "[sl" in str(getattr(d, "comment", "")) and not sl:
                sl = lv
            elif lv and "[tp" in str(getattr(d, "comment", "")) and not tp:
                tp = lv
        fill = float(din.price)
        dist = abs(fill - sl) if sl else 0.0
        if dist <= 0:
            DIAG["sem_stop"] = DIAG.get("sem_stop", 0) + 1
            continue
        vol = float(din.volume)
        out_px = sum(float(d.price) * float(d.volume) for d in douts) / max(sum(float(d.volume) for d in douts), 1e-9)
        profit = sum(float(d.profit) for d in ds)
        fees = sum(float(getattr(d, "commission", 0)) + float(getattr(d, "swap", 0)) + float(getattr(d, "fee", 0)) for d in ds)
        money_per_r = abs(profit) / abs((out_px - fill)) * dist if abs(out_px - fill) > 1e-9 and profit else None
        kind = "stop" if sl and abs(out_px - sl) <= abs(out_px - tp if tp else 1e9) else ("alvo" if tp else "outro")
        level = sl if kind == "stop" else (tp if kind == "alvo" else out_px)
        exit_slip = (level - out_px) * sgn if kind in ("stop", "alvo") else 0.0          # > 0 = saiu pior que o nível
        entry_slip_req = (fill - req) * sgn if req else None                              # > 0 = pior que o pedido
        entry_slip_bar = (fill - float(ex["signal_close"])) * sgn if ex and ex.get("signal_close") else None
        spread = (float(ex["ask"]) - float(ex["bid"])) if ex and ex.get("ask") and ex.get("bid") else None
        fee_r = (-fees / money_per_r) if money_per_r else None
        r_net = (out_px - fill) * sgn / dist - (fee_r or 0.0)    # resultado da operação em R, já com comissão/swap
        rows.append({"pos": pid, "time": int(din.time), "side": "BUY" if buy else "SELL", "volume": vol, "fill": fill, "sl": sl, "tp": tp,
                     "exit": out_px, "kind": kind, "dist": dist, "profit": profit, "fees": fees,
                     "entry_slip_req": entry_slip_req, "entry_slip_bar": entry_slip_bar, "exit_slip": exit_slip, "spread": spread,
                     "fee_r": fee_r, "r": r_net})
    return sorted(rows, key=lambda r: r["time"])


def report(rows: list[dict], backtest_cost_r: float = 0.033) -> str:
    if not rows:
        d = DIAG
        return ("Nenhuma operação da IA fechada no período (número mágico 260611).\n"
                f"  diagnóstico: {d.get('negocios_no_periodo', '?')} negócios no histórico · {d.get('posicoes_da_ia', '?')} posições da IA · "
                f"{d.get('sem_stop', 0)} sem stop identificável" + (f" · erro MT5: {d['erro_mt5']}" if d.get("erro_mt5") else "") +
                "\n  Se aparecer 0 negócios: no MT5, aba Histórico → clique direito → 'Todo o histórico', e rode de novo.")
    n = len(rows)
    avg = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    sl_req = [r["entry_slip_req"] / r["dist"] for r in rows if r["entry_slip_req"] is not None]
    sl_bar = [r["entry_slip_bar"] / r["dist"] for r in rows if r["entry_slip_bar"] is not None]
    ex_sl = [r["exit_slip"] / r["dist"] for r in rows if r["kind"] in ("stop", "alvo")]
    spr = [r["spread"] / r["dist"] for r in rows if r["spread"] is not None]
    fee = [r["fee_r"] for r in rows if r["fee_r"] is not None]
    lines = [f"CUSTO REAL DE EXECUÇÃO · {n} operações da IA", ""]
    f = lambda x, unit="R": "—" if x is None else f"{x:+.3f} {unit}"  # noqa: E731
    lines.append(f"  derrapagem na entrada × preço pedido     {f(avg(sl_req))}  ({len(sl_req)} ops)")
    lines.append(f"  entrada × fechamento da vela (backtest)  {f(avg(sl_bar))}  ({len(sl_bar)} ops com registro de execução)")
    lines.append(f"  spread no envio                          {f(avg(spr))}  ({len(spr)} ops)")
    lines.append(f"  derrapagem na saída (stop/alvo)          {f(avg(ex_sl))}  ({len(ex_sl)} ops)")
    lines.append(f"  comissão + swap                          {f(avg(fee))}  ({len(fee)} ops)")
    parts = [x for x in (avg(sl_bar) if sl_bar else avg(sl_req), avg(ex_sl), avg(fee)) if x is not None]
    if not sl_bar and avg(spr) is not None:
        parts.append(avg(spr))
    real = sum(parts) if parts else None
    lines.append("")
    if real is None:
        lines.append("  custo total: sem dados suficientes")
    else:
        lines.append(f"  CUSTO TOTAL REAL ≈ {real:+.3f} R por operação · o backtest desconta ≈ {backtest_cost_r:.3f} R (spread da vela)")
        extra = real - backtest_cost_r
        lines.append(f"  diferença = {extra:+.3f} R por operação → a vantagem esperada de ≈ +0,16 a +0,31 R cai para "
                     f"≈ {0.16 - extra:+.2f} a {0.31 - extra:+.2f} R")
        lines.append("  " + ("✔ custo dentro do previsto" if extra <= 0.05 else
                             "⚠ custo acima do previsto — acompanhe" if extra <= 0.15 else
                             "✗ custo come boa parte da vantagem — reavaliar tempo gráfico/horário"))
    if n < 30:
        lines.append(f"  (só {n} operações: a média ainda varia muito; reavalie com 30+)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.costs")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args(argv)
    from .settings import load_env, mt5_path
    load_env()
    import MetaTrader5 as mt5  # type: ignore
    path = mt5_path()
    if not (mt5.initialize(path=path) if path else mt5.initialize()):
        print(f"MT5 initialize falhou: {mt5.last_error()}")
        return 1
    rows = analyze_positions(mt5, a.days, load_exec_log())
    for r in rows:
        print(f"  {datetime.fromtimestamp(r['time'], tz=timezone.utc):%d/%m %H:%M} {r['side']:4} {r['volume']:.2f} @ {r['fill']} "
              f"→ {r['kind']:5} {r['exit']} · lucro {r['profit']:+.2f} · derrap. saída {r['exit_slip']:+.2f}")
    print()
    print(report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
