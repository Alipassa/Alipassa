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

# Critérios por modo, fixados ANTES da demo (26/11/2026). Em R = distância do stop.
#   reversão:   stop 1 ATR → +0,10 R de média · PF 1,15 · queda ≤ 25 R · custo ≤ backtest + 0,05 R · janelas 16h/20h UTC
#   cruzamento: stop 2 ATR → o histórico 2016–2026 deu +0,13 ATR/op = +0,065 R; exige ≥ +0,05 R · PF ≥ 1,10 · queda ≤ 30 R
#               (2018 sozinho caiu 28 R) · custo ≤ backtest + 0,03 R · presença em todas as horas dos dias úteis
CRITERIA = {
    "reversao": {"min_exp": 0.10, "min_pf": 1.15, "max_dd": 25.0, "bt_cost": 0.033, "max_extra_cost": 0.05, "hours": None},
    "cruzamento": {"min_exp": 0.05, "min_pf": 1.10, "max_dd": 30.0, "bt_cost": 0.01, "max_extra_cost": 0.03,
                   "hours": tuple(range(1, 21))},
}


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


def expected_windows(now: float, days: int = 14, hours=None) -> list[str]:
    """Janelas de operação (dia útil + hora UTC) nos últimos `days` dias, até a hora atual."""
    hours = hours or REVERSION_HOURS
    out = []
    for d in range(days, -1, -1):
        g = time.gmtime(now - d * 86400)
        if g.tm_wday >= 5:                     # sábado/domingo: ouro fechado
            continue
        for h in hours:
            t = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, h, 59, 0))
            if t <= now:
                out.append(f"{g.tm_year:04d}-{g.tm_mon:02d}-{g.tm_mday:02d} {h:02d}")
    return out


def evaluate(rows: list[dict], events: list[dict], login: str = "", now: Optional[float] = None,
             mode: str = "reversao") -> list[tuple[str, Optional[bool], str]]:
    now = now or time.time()
    k_ = CRITERIA.get(mode, CRITERIA["reversao"])
    rs = [r["r"] for r in rows if r.get("r") is not None]
    n = len(rs)
    res: list[tuple[str, Optional[bool], str]] = []
    res.append(("1 · pelo menos 100 operações", n >= 100, f"{n} operações"))
    mean = sum(rs) / n if n else 0.0
    res.append((f"2 · média ≥ +{k_['min_exp']:.2f} R por operação".replace(".", ","), n > 0 and mean >= k_["min_exp"], f"{mean:+.3f} R"))
    k = min(50, n // 2)
    first, last = rs[:k], rs[-k:] if k else []
    mf = sum(first) / len(first) if first else 0.0
    ml = sum(last) / len(last) if last else 0.0
    res.append((f"3 · primeiras e últimas {k or 50} positivas", bool(k) and mf > 0 and ml > 0, f"{mf:+.3f} R · {ml:+.3f} R"))
    gp, gl = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
    pf = gp / gl if gl else (float("inf") if gp else 0.0)
    res.append((f"4 · fator de lucro ≥ {k_['min_pf']:.2f}".replace(".", ","), n > 0 and pf >= k_["min_pf"],
                f"{pf:.2f}" if pf != float("inf") else "sem perdas"))
    eq = pk = dd = 0.0
    for x in rs:
        eq += x
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    res.append((f"5 · pior queda ≤ {k_['max_dd']:.0f} R", n > 0 and dd <= k_["max_dd"], f"{dd:.1f} R"))
    bar = [r["entry_slip_bar"] / r["dist"] for r in rows if r.get("entry_slip_bar") is not None]
    ex = [r["exit_slip"] / r["dist"] for r in rows if r.get("kind") in ("stop", "alvo")]
    fee = [r["fee_r"] for r in rows if r.get("fee_r") is not None]
    if len(bar) >= 20:
        cost = sum(bar) / len(bar) + (sum(ex) / len(ex) if ex else 0) + (sum(fee) / len(fee) if fee else 0)
        extra = cost - k_["bt_cost"]
        res.append((f"6 · custo real ≤ backtest + {k_['max_extra_cost']:.2f} R".replace(".", ","), extra <= k_["max_extra_cost"],
                    f"{cost:+.3f} R ({extra:+.3f} acima do backtest; {len(bar)} ops com registro)"))
    else:
        res.append((f"6 · custo real ≤ backtest + {k_['max_extra_cost']:.2f} R".replace(".", ","), None,
                    f"só {len(bar)} operações com registro de execução (precisa de 20)"))
    mine = [e for e in events if not login or str(e.get("login", "")) in ("", login)]
    recent = [e for e in mine if e.get("t", 0) >= now - 14 * 86400]
    seen = {e.get("janela") for e in recent if e.get("kind") == "janela"}
    start = min((e.get("t", now) for e in mine), default=None)     # o registro começa na 1ª marca desta conta
    if start is None:
        res.append(("7 · 2 semanas sem falhas", None, "registro ainda não começou (inicie a IA nesta versão)"))
        return res
    exp = [w for w in expected_windows(now, hours=k_["hours"]) if calendar.timegm(time.strptime(w + ":59", "%Y-%m-%d %H:%M")) >= start - 3600]
    cover = len([w for w in exp if w in seen]) / len(exp) if exp else 0.0
    errs = len([e for e in recent if e.get("kind") == "error"])
    days_logged = (now - start) / 86400
    ok = (cover >= 0.9 and errs <= 2) if days_logged >= 14 else None
    res.append(("7 · 2 semanas sem falhas", ok,
                f"ligada em {cover:.0%} das {len(exp)} janelas desde o início do registro · {errs} erros"
                + (f" · {days_logged:.0f} de 14 dias" if days_logged < 14 else "")))
    return res


# ---------------------------------------------------------------------------------------------------- candidatos
# Regra fixada em 26/11/2026, ANTES de existir qualquer dado novo: os candidatos que quase venceram no histórico são
# julgados só com velas a partir de CAND_START. Um candidato substitui a regra atual quando, com ≥ 100 operações novas,
# tiver lucro total MAIOR e pior queda NÃO MAIOR que a da regra atual. Antes disso, só se acompanha.
CAND_START = calendar.timegm((2026, 11, 27, 0, 0, 0))
CAND_MIN_OPS = 100


def candidates(c: list[dict], spread_def: float = 0.3, start: int = CAND_START) -> list[dict]:
    """Regra atual × candidatos no ouro H1, contando só as operações com entrada a partir de `start`."""
    from .cross import simulate, simulate_exit
    runs = {
        "regra atual (cruzamento contrário)": simulate(c, "sma", 2.0, False, spread_def),
        "candidato: zero a zero em +2 ATR": simulate_exit(c, ("be", 2.0), spread_def),
        "candidato: só do lado da SMA 200": simulate(c, "sma", 2.0, False, spread_def, regime="lado da SMA200"),
    }
    out = []
    for name, r in runs.items():
        rr = [x[1] for x in r if x[0] >= start]
        eq = pk = dd = 0.0
        for x in rr:
            eq += x
            pk = max(pk, eq)
            dd = max(dd, pk - eq)
        out.append({"name": name, "n": len(rr), "total": sum(rr), "exp": sum(rr) / len(rr) if rr else 0.0, "dd": dd,
                    "win": sum(x > 0 for x in rr) / len(rr) if rr else 0.0})
    return out


def candidates_report(rows: list[dict], start: int = CAND_START) -> str:
    base = rows[0]
    lines = [f"CANDIDATOS · só dados novos (entradas desde {time.strftime('%d/%m/%Y', time.gmtime(start))}) · ouro H1 · em ATR",
             f"  {'':38} {'ops':>4} {'total':>7} {'ATR/op':>7} {'acerto':>6} {'queda':>6} │ situação"]
    for r in rows:
        if r is base:
            sit = "referência"
        elif r["n"] < CAND_MIN_OPS:
            sit = f"acompanhando ({r['n']}/{CAND_MIN_OPS} operações)"
        elif r["total"] > base["total"] and r["dd"] <= base["dd"]:
            sit = "✔ VENCEU: pode substituir a regra atual"
        else:
            sit = "✗ não venceu a regra atual"
        lines.append(f"  {r['name']:38} {r['n']:4} {r['total']:+7.1f} {r['exp']:+7.3f} {r['win']:6.0%} {r['dd']:6.1f} │ {sit}")
    lines.append(f"  (regra fixada antes: com ≥ {CAND_MIN_OPS} operações novas, lucro total maior E pior queda não maior)")
    return "\n".join(lines)


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
    ap.add_argument("--modo", choices=["reversao", "cruzamento"], help="padrão: o modo da última entrada registrada")
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
    ex_log = load_exec_log()
    mode = a.modo or (max(ex_log.values(), key=lambda r: r.get("t", 0)).get("mode") if ex_log else None) or "cruzamento"
    rows = analyze_positions(mt5, a.days, ex_log)
    # só as operações do modo avaliado (quando o registro de execução diz qual foi)
    rows = [r for r in rows if (ex_log.get(r["pos"]) or {}).get("mode", mode) == mode]
    print(f"conta {login} ({'DEMO' if demo else 'REAL'}) · modo {mode} · {len(rows)} operações da IA nos últimos {a.days} dias")
    if not rows and DIAG:
        print(f"  diagnóstico: {DIAG}")
    print()
    print(report(evaluate(rows, load_events(), login, mode=mode), demo))
    if mode == "cruzamento":
        try:
            from .backtest import SPREAD, load_mt5
            c = load_mt5("XAUUSD", "H1", 6000)            # ~1 ano: aquecimento das médias (SMA 200) + o período novo
            print()
            print(candidates_report(candidates(c, SPREAD.get("XAUUSD", 0.3))))
        except Exception as e:  # noqa: BLE001
            print(f"\n(comparação dos candidatos indisponível: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
