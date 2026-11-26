"""Relatório de validação para ir à conta REAL — os 7 critérios combinados ANTES de ver os resultados da demo.

    python -m turbo_ia.validate              # lê o histórico do MT5 da conta logada + registros da IA

  1. ≥ 100 operações da IA
  2. média ≥ +0,10 R por operação (resultado real do MT5, com spread, derrapagem e comissão)
  3. primeiras 50 e últimas 50 operações positivas
  4. fator de lucro ≥ 1,15
  5. pior queda ≤ 25 R
  6. custo real ≤ 0,05 R acima do que o backtest desconta
  7. últimas 2 semanas: IA ligada em ≥ 90 % das janelas dos dias úteis e no máximo 2 erros
Só com os 7 ✔ a recomendação é ir para a conta real — começando com 1 % de risco.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import time
from typing import Optional

from .analysis import REVERSION_HOURS
from .costs import DIAG, analyze_positions, load_exec_log

BACKTEST_COST_R = 0.033


def load_events(path: Optional[str] = None) -> list[dict]:
    path = path or os.path.join(os.path.expanduser("~"), "turbo_ia_eventos.jsonl")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def expected_windows(now: float, days: int = 14) -> list[str]:
    """Janelas de operação (dia útil + hora UTC) nos últimos `days` dias, até a hora atual."""
    out = []
    for d in range(days, -1, -1):
        g = time.gmtime(now - d * 86400)
        if g.tm_wday >= 5:                     # sábado/domingo: ouro fechado
            continue
        for h in REVERSION_HOURS:
            t = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, h, 59, 0))
            if t <= now:
                out.append(f"{g.tm_year:04d}-{g.tm_mon:02d}-{g.tm_mday:02d} {h:02d}")
    return out


def evaluate(rows: list[dict], events: list[dict], login: str = "", now: Optional[float] = None) -> list[tuple[str, Optional[bool], str]]:
    now = now or time.time()
    rs = [r["r"] for r in rows if r.get("r") is not None]
    n = len(rs)
    res: list[tuple[str, Optional[bool], str]] = []
    res.append(("1 · pelo menos 100 operações", n >= 100, f"{n} operações"))
    mean = sum(rs) / n if n else 0.0
    res.append(("2 · média ≥ +0,10 R por operação", n > 0 and mean >= 0.10, f"{mean:+.3f} R"))
    k = min(50, n // 2)
    first, last = rs[:k], rs[-k:] if k else []
    mf = sum(first) / len(first) if first else 0.0
    ml = sum(last) / len(last) if last else 0.0
    res.append((f"3 · primeiras e últimas {k or 50} positivas", bool(k) and mf > 0 and ml > 0, f"{mf:+.3f} R · {ml:+.3f} R"))
    gp, gl = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
    pf = gp / gl if gl else (float("inf") if gp else 0.0)
    res.append(("4 · fator de lucro ≥ 1,15", n > 0 and pf >= 1.15, f"{pf:.2f}" if pf != float("inf") else "sem perdas"))
    eq = pk = dd = 0.0
    for x in rs:
        eq += x
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    res.append(("5 · pior queda ≤ 25 R", n > 0 and dd <= 25, f"{dd:.1f} R"))
    bar = [r["entry_slip_bar"] / r["dist"] for r in rows if r.get("entry_slip_bar") is not None]
    ex = [r["exit_slip"] / r["dist"] for r in rows if r.get("kind") in ("stop", "alvo")]
    fee = [r["fee_r"] for r in rows if r.get("fee_r") is not None]
    if len(bar) >= 20:
        cost = sum(bar) / len(bar) + (sum(ex) / len(ex) if ex else 0) + (sum(fee) / len(fee) if fee else 0)
        res.append(("6 · custo real ≤ backtest + 0,05 R", cost - BACKTEST_COST_R <= 0.05,
                    f"{cost:+.3f} R ({cost - BACKTEST_COST_R:+.3f} acima do backtest; {len(bar)} ops com registro)"))
    else:
        res.append(("6 · custo real ≤ backtest + 0,05 R", None, f"só {len(bar)} operações com registro de execução (precisa de 20)"))
    mine = [e for e in events if not login or str(e.get("login", "")) in ("", login)]
    recent = [e for e in mine if e.get("t", 0) >= now - 14 * 86400]
    seen = {e.get("janela") for e in recent if e.get("kind") == "janela"}
    exp = expected_windows(now)
    cover = len([w for w in exp if w in seen]) / len(exp) if exp else 0.0
    errs = len([e for e in recent if e.get("kind") == "error"])
    res.append(("7 · 2 semanas sem falhas", cover >= 0.9 and errs <= 2 if exp else None,
                f"ligada em {cover:.0%} das {len(exp)} janelas · {errs} erros"))
    return res


def report(checks: list[tuple[str, Optional[bool], str]], demo: Optional[bool] = None) -> str:
    lines = ["VALIDAÇÃO PARA CONTA REAL — critérios combinados antes da demo", ""]
    for name, ok, detail in checks:
        mark = "✔" if ok else ("…" if ok is None else "✗")
        lines.append(f"  {mark} {name:38} {detail}")
    done = all(ok for _, ok, _ in checks)
    lines.append("")
    if done:
        lines.append("  ✅ VALIDADO: pode ir para a conta real começando com 1 % de risco (2 % depois de mais 50 operações positivas).")
        lines.append("     Ligar: no .env, TURBO_IA_ALLOW_REAL=1 · login na conta real no MT5 · conferir 'CONTA REAL' no painel.")
    else:
        falta = [name.split(" · ")[1] for name, ok, _ in checks if not ok]
        lines.append(f"  ⏳ AINDA NÃO: falta {', '.join(falta)}. Continue na demo.")
    if demo is False:
        lines.append("  (atenção: a conta logada agora é REAL — este relatório deve ser feito com o histórico da DEMO)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.validate")
    ap.add_argument("--days", type=int, default=180)
    a = ap.parse_args(argv)
    from .settings import load_env, mt5_path
    load_env()
    import MetaTrader5 as mt5  # type: ignore
    path = mt5_path()
    if not (mt5.initialize(path=path) if path else mt5.initialize()):
        print(f"MT5 initialize falhou: {mt5.last_error()}")
        return 1
    acc = mt5.account_info()
    login = str(getattr(acc, "login", "")) if acc else ""
    demo = (getattr(acc, "trade_mode", None) == getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)) if acc else None
    rows = analyze_positions(mt5, a.days, load_exec_log())
    print(f"conta {login} ({'DEMO' if demo else 'REAL'}) · {len(rows)} operações da IA nos últimos {a.days} dias")
    if not rows and DIAG:
        print(f"  diagnóstico: {DIAG}")
    print()
    print(report(evaluate(rows, load_events(), login), demo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
