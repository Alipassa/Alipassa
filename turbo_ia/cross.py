"""Teste da estratégia do usuário: cruzamento de médias 9 × 20 com filtro de ATR contra mercado lateral.

    python -m turbo_ia.cross --source mt5 --symbol XAUUSD --tfs M5,M15,H1

Regra (fixada antes do teste):
  • média 9 cruza a média 20 para CIMA → compra; para BAIXO → venda (no fechamento da vela do cruzamento)
  • sai no cruzamento contrário (e vira a mão se o filtro deixar)
  • filtro de lateralidade: só entra se ATR(14) > média de 50 velas do próprio ATR (mercado com movimento)
Variações: médias exponenciais (EMA) ou simples (SMA) · sem stop ou stop de 2 ATR (stop antes de tudo na vela).
Resultado em R (1 R = 1 ATR na entrada) e em pontos de preço, com spread descontado. Metades A/B e, no MT5, o COFRE
(antes de 20/05/2026).
"""

from __future__ import annotations

import argparse
import calendar
from typing import Optional

from . import indicators as ind
from .backtest import SPREAD, load_mt5, load_yahoo
from .research import summary

CUTOFF = calendar.timegm((2026, 5, 20, 0, 0, 0))

# B3 — custo de uma operação completa de 1 contrato, em PONTOS (mesmas premissas do B3 AI):
#   WIN: 2 ticks de escorregamento (2 × 5 pts × R$ 0,20) + R$ 0,60 fixo = R$ 2,60 = 13 pontos
#   WDO: 2 × 0,5 pt × R$ 10 + R$ 2,60 fixo = R$ 12,60 = 1,26 ponto
# Proxies do Yahoo: ^BVSP (Ibovespa, mesma escala do WIN) e BRL=X (dólar em R$; WDO = × 1000).
B3_COST = {"WIN": 13.0, "WDO": 1.26, "^BVSP": 13.0, "BRL=X": 0.00126}
# Pregão dos minicontratos (horário de Brasília = UTC−3, sem horário de verão): day trade entra das 09h15 às 17h30 e zera às 17h50
B3_SESSION_UTC = (12 * 60 + 15, 20 * 60 + 30, 20 * 60 + 50)


def b3_key(sym: str) -> Optional[str]:
    s = sym.upper()
    for k in ("WIN", "WDO"):
        if s.startswith(k):
            return k
    return sym if sym in B3_COST else None


def load(a, sym: str, tf: str) -> list[dict]:
    """Carrega velas (MT5 ou Yahoo). Em ativos da B3 o custo de cada operação vira o custo B3 completo (não só o spread)."""
    c = load_mt5(sym, tf, a.bars) if a.source == "mt5" else load_yahoo(sym, tf)
    k = b3_key(sym)
    if k:
        for x in c:
            x["spread"] = B3_COST[k]
    return c


def session_of(a) -> Optional[tuple]:
    return B3_SESSION_UTC if getattr(a, "daytrade", False) else None


REGIME = {
    "ADX>20": ("adx", 20), "ADX>25": ("adx", 25), "CHOP<61.8": ("chop", 61.8), "CHOP<50": ("chop", 50),
    "ER>0.3": ("er", 0.3), "lado da SMA200": ("sma200", None), "SMA50 inclinada": ("slope50", 1.0),
}


def regime_series(c: list[dict]) -> dict:
    cl = [x["close"] for x in c]
    s50 = ind.sma(cl, 50)
    return {"adx": ind.adx(c, 14), "chop": ind.choppiness(c, 14), "er": ind.efficiency_ratio(cl, 20),
            "sma200": ind.sma(cl, 200), "sma50": s50}


def regime_ok(rs: dict, c: list[dict], atr: list, i: int, side: int, rule: Optional[str]) -> bool:
    """True = pode entrar. Só olha o passado (valores até a vela i)."""
    if not rule:
        return True
    kind, thr = REGIME[rule]
    if kind == "adx":
        return rs["adx"][i] is not None and rs["adx"][i] > thr
    if kind == "chop":
        return rs["chop"][i] is not None and rs["chop"][i] < thr
    if kind == "er":
        return rs["er"][i] is not None and rs["er"][i] > thr
    if kind == "sma200":
        m = rs["sma200"][i]
        return m is not None and ((c[i]["close"] > m) if side > 0 else (c[i]["close"] < m))
    if kind == "slope50":
        a, b_ = rs["sma50"][i], rs["sma50"][i - 10] if i >= 10 else None
        return a is not None and b_ is not None and bool(atr[i]) and ((a - b_) * side > thr * atr[i])
    return True


def simulate(c: list[dict], kind: str = "ema", stop_atr: Optional[float] = None, atr_filter: bool = True,
             spread_def: float = 0.3, lo: int = 0, hi: Optional[int] = None, regime: Optional[str] = None,
             rs: Optional[dict] = None, fast: int = 9, slow: int = 20, session: Optional[tuple] = None) -> list[tuple[int, float, float]]:
    """Devolve [(tempo da entrada, R, pontos)] — uma posição por vez. `regime`: filtro de lateralidade na ENTRADA
    (a saída no cruzamento contrário acontece sempre; se o filtro barra a nova entrada, fica de fora até o próximo cruzamento)."""
    hi = hi or len(c)
    if regime and rs is None:
        rs = regime_series(c)
    cl = [x["close"] for x in c]
    f9 = ind.ema(cl, fast) if kind == "ema" else ind.sma(cl, fast)
    f20 = ind.ema(cl, slow) if kind == "ema" else ind.sma(cl, slow)
    atr = ind.atr(c, 14)
    atr_avg = ind.sma([a or 0.0 for a in atr], 50)
    out: list[tuple[int, float, float]] = []
    pos = None                                        # (lado, entrada, índice, atr, stop)
    bar_min = max(1, int((c[min(len(c) - 1, 101)]["time"] - c[min(len(c) - 2, 100)]["time"]) // 60)) if len(c) > 2 else 60
    for i in range(max(lo, 70, slow + 2), hi):
        if session:                                   # DAY TRADE: zera no fim do pregão; só entra dentro do horário
            tod = (c[i]["time"] % 86400) // 60
            if pos and (tod + bar_min > session[2] or c[i]["time"] // 86400 != c[pos[2]]["time"] // 86400):
                side, e, k, a, _ = pos
                pts = (c[i]["open"] - e) * side
                sp = c[k].get("spread") or spread_def
                out.append((c[k]["time"], (pts - sp) / a, pts - sp))
                pos = None
        if f9[i] is None or f20[i] is None or f9[i - 1] is None or f20[i - 1] is None or not atr[i]:
            continue
        x = c[i]
        if pos and stop_atr:                          # stop dentro da vela (antes do cruzamento do fechamento)
            side, e, k, a, sl = pos
            if (x["low"] <= sl) if side > 0 else (x["high"] >= sl):
                pts = (sl - e) * side
                sp = c[k].get("spread") or spread_def
                out.append((c[k]["time"], (pts - sp) / a, pts - sp))
                pos = None
        up = f9[i - 1] <= f20[i - 1] and f9[i] > f20[i]
        dn = f9[i - 1] >= f20[i - 1] and f9[i] < f20[i]
        if not (up or dn):
            continue
        if pos:                                       # cruzamento contrário: sai no fechamento
            side, e, k, a, _ = pos
            if (side > 0 and dn) or (side < 0 and up):
                pts = (x["close"] - e) * side
                sp = c[k].get("spread") or spread_def
                out.append((c[k]["time"], (pts - sp) / a, pts - sp))
                pos = None
        if pos is None:
            if session:
                tod = (c[i]["time"] % 86400) // 60
                if not (session[0] <= tod and tod + bar_min <= session[1]):
                    continue                          # fora da janela de entrada do day trade
            if atr_filter and (atr_avg[i] is None or atr[i] <= atr_avg[i]):
                continue                              # mercado lateral (ATR abaixo da média): não entra
            side = 1 if up else -1
            if not regime_ok(rs, c, atr, i, side, regime):
                continue
            a = atr[i]
            sl = (x["close"] - side * stop_atr * a) if stop_atr else None
            pos = (side, x["close"], i, a, sl)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.cross")
    ap.add_argument("--source", choices=["mt5", "yahoo"], default="mt5")
    ap.add_argument("--symbol", default="XAUUSD", help="um ou vários, separados por vírgula")
    ap.add_argument("--tfs", default="M5,M15,H1")
    ap.add_argument("--bars", type=int, default=2_000_000)
    ap.add_argument("--years", action="store_true", help="mostra o resultado ano a ano")
    ap.add_argument("--regime", action="store_true", help="testa filtros de mercado lateral no SMA 9×20 stop 2 ATR")
    ap.add_argument("--exits", action="store_true", help="testa saídas alternativas no SMA 9×20")
    ap.add_argument("--robust", action="store_true", help="robustez dos parâmetros e ensembles")
    ap.add_argument("--daytrade", action="store_true", help="B3: zera às 17h50 e só entra das 09h15 às 17h30 (Brasília)")
    ap.add_argument("--grid-tf", dest="grid_tf", default="H1", help="tempo gráfico da grade de robustez (padrão H1)")
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    if a.regime:
        return regime_main(a)
    if a.exits:
        return exits_main(a)
    if a.robust:
        return robust_main(a)
    print("CRUZAMENTO 9 × 20 + filtro ATR · saída no cruzamento contrário · spread descontado · 1 R = 1 ATR na entrada"
          + (" · DAY TRADE (zera 17h50)" if getattr(a, "daytrade", False) else ""))
    summary_rows = []
    tf_summary: list = []
    for sym, tf in [(s_, t_) for s_ in a.symbol.split(",") for t_ in a.tfs.split(",")]:
        spread_def = SPREAD.get(sym, 0.0)
        try:
            c = load(a, sym, tf)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {tf}: sem dados ({str(e)[:80]})")
            continue
        if len(c) < 600:
            print(f"  {sym} {tf}: poucas velas ({len(c)})")
            continue
        n = len(c)
        cut = next((i for i, x in enumerate(c) if x["time"] >= CUTOFF), 0)
        active = sorted({x["time"] // 86400 for x in c})                 # dias com velas (histórico antigo tem buracos)
        mid = cut + (n - cut) // 2
        def days(lo: int, hi: int) -> float:
            """Dias corridos EQUIVALENTES aos dias com dados (5 dias de mercado = 7 corridos) — não infla o período com buracos."""
            d0, d1 = c[lo]["time"] // 86400, c[hi - 1]["time"] // 86400
            k = sum(1 for d in active if d0 <= d <= d1)
            return max(k * 7 / 5, 1)
        print(f"\n  {sym} {tf} · spread {spread_def:g} · {n} velas ≈ {days(0, n):.0f} dias" + (f" · cofre {days(0, cut):.0f} dias" if cut > 500 else ""))
        print(f"    {'variação':30} {'ops':>5} {'acerto':>7} {'R/op':>7} {'pts/op':>7} {'R/mês':>7} {'PF':>5} {'queda':>6} │ A R/mês │ B R/mês" + (" │ COFRE R/mês" if cut > 500 else ""))
        for kind in ("ema", "sma"):
            for stop in (None, 2.0):
                for filt in (True, False):
                    tag = f"{kind.upper()} 9×20 {'+ATR' if filt else 'sem filtro'}{' stop 2ATR' if stop else ''}"
                    r = simulate(c, kind, stop, filt, spread_def, cut, n, session=session_of(a))
                    rs = [x[1] for x in r]
                    s = summary(rs)
                    gp, gl = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
                    pts = sum(x[2] for x in r) / len(r) if r else 0.0
                    ra = [x[1] for x in r if x[0] < c[mid]["time"]]
                    rb = [x[1] for x in r if x[0] >= c[mid]["time"]]
                    line = (f"    {tag:30} {s['n']:5} {s['win']:7.1%} {s['exp']:+7.3f} {pts:+7.2f} {sum(rs) / days(cut, n) * 30:+7.1f} "
                            f"{(gp / gl if gl else 0):5.2f} {s['dd']:5.0f}R │ {sum(ra) / days(cut, mid) * 30:+7.1f} │ {sum(rb) / days(mid, n) * 30:+7.1f}")
                    rc_m = None
                    if cut > 500:
                        rc = [x[1] for x in simulate(c, kind, stop, filt, spread_def, 0, cut, session=session_of(a))]
                        rc_m = sum(rc) / days(0, cut) * 30
                        line += f" │ {rc_m:+7.1f} ({summary(rc)['exp']:+.3f}/op)"
                    print(line, flush=True)
                    ok = (ra and rb and sum(ra) > 0 and sum(rb) > 0 and (rc_m is None or rc_m > 0) and s["n"] >= 30)
                    summary_rows.append((sym, tf, tag, ok, s["exp"], rc_m))
        if a.years:
            # resumo do modo do painel (SMA 9×20 sem filtro, stop 2 ATR) para comparar tempos gráficos
            rp = simulate(c, "sma", 2.0, False, spread_def, 0, n, session=session_of(a))
            import time as _tt
            dense = [y for y in sorted({_tt.gmtime(x["time"]).tm_year for x in c})
                     if len([1 for x in c if _tt.gmtime(x["time"]).tm_year == y]) > 1000]
            if dense:
                by = {y: sum(x[1] for x in rp if _tt.gmtime(x[0]).tm_year == y) for y in dense}
                rd = [x for x in rp if _tt.gmtime(x[0]).tm_year >= dense[0]]
                months = max(days(next(i for i, x in enumerate(c) if _tt.gmtime(x["time"]).tm_year >= dense[0]), n) / 30, 1)
                sp_c = [x["spread"] for x in c if x.get("spread")]
                tf_summary.append((sym, tf, dense[0], dense[-1], sum(by.values()), sum(v > 0 for v in by.values()), len(dense),
                                   min(by.values()), sum(x[1] for x in rd) / max(len(rd), 1), len(rd) / months,
                                   sum(x[1] for x in rd) / months, sum(x[1] for x in rd if x[0] < CUTOFF), sum(x[1] for x in rd if x[0] >= CUTOFF)))
            print(f"    ano a ano ({sym} {tf}) — R por operação · total R · operações:")
            import time as _t
            yrs = sorted({_t.gmtime(x["time"]).tm_year for x in c})
            print("      " + "".join(f"{y:>16}" for y in yrs))
            for kind, stop, filt in (("sma", None, False), ("sma", 2.0, False), ("sma", None, True), ("ema", None, False)):
                tag = f"{kind.upper()} 9×20 {'+ATR' if filt else 'sem filtro'}{' stop 2ATR' if stop else ''}"
                r = simulate(c, kind, stop, filt, spread_def, 0, n, session=session_of(a))
                cells = []
                pos_years = 0
                for y in yrs:
                    ry = [x[1] for x in r if _t.gmtime(x[0]).tm_year == y]
                    if ry:
                        pos_years += sum(ry) > 0
                        cells.append(f"{sum(ry) / len(ry):+.2f} {sum(ry):+5.0f} {len(ry):3}")
                    else:
                        cells.append("—")
                print(f"      {tag:28}" + "".join(f"{x:>16}" for x in cells) + f"   → {pos_years}/{len([1 for x in cells if x != '—'])} anos positivos")
    good = [r for r in summary_rows if r[3]]
    print("\nPOSITIVO EM TODOS OS PERÍODOS (A, B" + (" e cofre" if any(r[5] is not None for r in summary_rows) else "") + "):")
    for sym, tf, tag, _, e, rc in good:
        print(f"  ✔ {sym:10} {tf:4} {tag:30} {e:+.3f} R/op" + (f" · cofre {rc:+.1f} R/mês" if rc is not None else ""))
    if not good:
        print("  nenhum")
    tested = len(summary_rows)
    print(f"  ({tested} combinações testadas: espere ~{max(1, tested // 8)} 'aprovação' só por acaso — desconfie de casos isolados)")
    print("\nLeitura: R/op > 0 nas metades A e B (e no cofre) = vantagem que se repete. Spread pesa mais em gráficos curtos.")
    if tf_summary:
        print("\nCOMPARAÇÃO DE TEMPOS GRÁFICOS · modo do painel (SMA 9×20, stop 2 ATR, sem filtro) · anos com dados densos")
        print("  (com risco de 2 % no stop de 2 ATR, 1 ATR ≈ 1 % do saldo → 'ATR/mês' ≈ '% ao mês' em qualquer gráfico)")
        print(f"  {'mercado':8} {'gráfico':7} {'anos':>9} │ {'total':>7} {'anos+':>6} {'pior ano':>8} │ {'ATR/op':>7} {'ops/mês':>7} {'ATR/mês':>8} │ {'cofre':>6} {'desenv':>6}")
        for sym, tf, y0, y1, tot, pos, ny, worst, rop, opm, rpm, cof, dev in tf_summary:
            print(f"  {sym:8} {tf:7} {y0}-{str(y1)[2:]:>2} │ {tot:+7.0f} {pos:3}/{ny:<2} {worst:+8.0f} │ {rop:+7.3f} {opm:7.1f} {rpm:+8.2f} │ {cof:+6.0f} {dev:+6.0f}")
        longos = [r for r in tf_summary if r[6] >= 5]
        if longos:
            best = max(longos, key=lambda r: (r[5] / r[6], r[10]))
            print(f"  → mais consistente (proporção de anos positivos) e depois mais lucrativo, entre os com ≥ 5 anos: {best[1]}")
        else:
            print("  → histórico curto demais (< 5 anos) para escolher — rode no MT5 da Pepperstone")

    return 0


EXITS = {
    "cruzamento contrário (atual)": "cross",
    "fechou do outro lado da 9": "ma9",
    "stop móvel 3 ATR": ("trail", 3.0),
    "stop móvel 2 ATR": ("trail", 2.0),
    "zero a zero em +2 ATR": ("be", 2.0),
    "metade em +3 ATR": ("half", 3.0),
}


def simulate_exit(c: list[dict], exit_rule, spread_def: float = 0.3) -> list[tuple[int, float, float]]:
    """Entrada = modo do painel (SMA 9×20, sem filtro, stop inicial 2 ATR). Saídas alternativas — o cruzamento contrário
    sempre fecha (e vira a mão) se vier antes. Stop móvel/zero a zero usam só velas fechadas e valem a partir da vela
    seguinte; stop sempre antes de qualquer ganho dentro da mesma vela. Devolve [(tempo, R em ATR, pontos)]."""
    cl = [x["close"] for x in c]
    f9, f20 = ind.sma(cl, 9), ind.sma(cl, 20)
    atr = ind.atr(c, 14)
    out: list[tuple[int, float, float]] = []
    pos = None
    kind = exit_rule if isinstance(exit_rule, str) else exit_rule[0]
    par = None if isinstance(exit_rule, str) else exit_rule[1]

    def close_pos(i: int, px: float) -> None:
        nonlocal pos
        side, e, k, a, sl, peak, frac, banked = pos
        pts = banked + frac * (px - e) * side - (c[k].get("spread") or spread_def)
        out.append((c[k]["time"], pts / a, pts))
        pos = None

    for i in range(70, len(c)):
        if f9[i] is None or f20[i] is None or f9[i - 1] is None or f20[i - 1] is None or not atr[i]:
            continue
        x = c[i]
        if pos:
            side, e, k, a, sl, peak, frac, banked = pos
            if (x["low"] <= sl) if side > 0 else (x["high"] >= sl):              # 1) stop (fixo, zero a zero ou móvel)
                close_pos(i, sl)
            else:
                if kind == "half" and frac == 1.0:                                # 2) parcial no alvo
                    lvl = e + side * par * a
                    if (x["high"] >= lvl) if side > 0 else (x["low"] <= lvl):
                        banked, frac = 0.5 * par * a, 0.5
                peak = max(peak, x["high"]) if side > 0 else min(peak, x["low"])
                new_sl = sl
                if kind == "trail":                                               # 3) stop móvel pela vela fechada
                    t = peak - side * par * atr[i]
                    new_sl = max(sl, t) if side > 0 else min(sl, t)
                if kind == "be" and ((peak - e) * side >= par * a):
                    new_sl = max(sl, e) if side > 0 else min(sl, e)
                if kind == "half" and frac < 1.0:
                    new_sl = max(sl, e) if side > 0 else min(sl, e)
                pos = (side, e, k, a, new_sl, peak, frac, banked)
                if kind == "ma9" and ((x["close"] < f9[i]) if side > 0 else (x["close"] > f9[i])):
                    close_pos(i, x["close"])                                      # 4) fechou do outro lado da média 9
        up = f9[i - 1] <= f20[i - 1] and f9[i] > f20[i]
        dn = f9[i - 1] >= f20[i - 1] and f9[i] < f20[i]
        if pos and ((pos[0] > 0 and dn) or (pos[0] < 0 and up)):
            close_pos(i, x["close"])                                              # 5) cruzamento contrário
        if pos is None and (up or dn):
            side = 1 if up else -1
            a = atr[i]
            pos = (side, x["close"], i, a, x["close"] - side * 2.0 * a, x["close"], 1.0, 0.0)
    return out


def _year_stats(trades: list[tuple[int, float]], yrs: list[int]) -> dict:
    import time as _t
    tr = sorted((t, r) for t, r in trades if _t.gmtime(t).tm_year >= yrs[0])
    by = {y: 0.0 for y in yrs}
    for t, r in tr:
        by[_t.gmtime(t).tm_year] += r
    eq = pk = dd = 0.0
    for _, r in tr:
        eq += r
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    tot = sum(by.values())
    worst = min(by.values())
    return {"by": by, "tot": tot, "pos": sum(v > 0 for v in by.values()), "worst": worst, "dd": dd, "n": len(tr),
            "cof": sum(r for t, r in tr if t < CUTOFF), "dev": sum(r for t, r in tr if t >= CUTOFF),
            "per_worst": tot / abs(worst) if worst < 0 else float("inf"), "per_dd": tot / dd if dd else float("inf")}


def robust_main(a) -> int:
    """1) Robustez: grade de médias rápida × lenta (SMA, stop 2 ATR, sem filtro) no ouro H1, anos densos.
    2) Ensembles com o MESMO risco total dividido entre sistemas (cada operação vale 1/k do risco).
    Adoção (fixada antes): lucro/|pior ano| E lucro/queda máxima maiores que o 9×20 sozinho, cofre e desenv. positivos."""
    import time as _t
    sym = a.symbol.split(",")[0]
    spread_def = SPREAD.get(sym, 0.0)
    data = {}
    grid_tf = getattr(a, "grid_tf", "H1") or "H1"
    for tf in dict.fromkeys((grid_tf, "H1", "M30")):
        try:
            data[tf] = load(a, sym, tf)
        except Exception as e:  # noqa: BLE001
            print(f"  {tf}: sem dados ({e})")
    c = data[grid_tf]
    yrs = [y for y in sorted({_t.gmtime(x["time"]).tm_year for x in c})
           if len([1 for x in c if _t.gmtime(x["time"]).tm_year == y]) > 1000]
    runs = {}

    def run(tf: str, fast: int, slow: int) -> list[tuple[int, float]]:
        key = (tf, fast, slow)
        if key not in runs and tf in data:
            runs[key] = [(t, r) for t, r, _ in simulate(data[tf], "sma", 2.0, False, spread_def, fast=fast, slow=slow,
                                                         session=session_of(a))]
        return runs.get(key, [])

    print(f"\n1) ROBUSTEZ DOS PARÂMETROS · {sym} {grid_tf} · SMA rápida × lenta · stop 2 ATR · {yrs[0]}–{yrs[-1]} · total em ATR (anos positivos)"
          + (" · DAY TRADE" if getattr(a, "daytrade", False) else ""))
    fasts, slows = (5, 7, 9, 12, 15), (20, 26, 30, 40, 50)
    print("   rápida\\lenta " + "".join(f"{sl:>14}" for sl in slows))
    grid = {}
    for fa in fasts:
        cells = []
        for sl in slows:
            st = _year_stats(run(grid_tf, fa, sl), yrs)
            grid[(fa, sl)] = st
            cells.append(f"{st['tot']:+7.0f} ({st['pos']:2}/{len(yrs)})")
        print(f"   {fa:>12} " + "".join(f"{x:>14}" for x in cells), flush=True)
    pos_cells = sum(1 for v in grid.values() if v["tot"] > 0)
    print(f"   → {pos_cells} de {len(grid)} combinações positivas" +
          (" — PLATÔ: a vantagem não depende do 9×20 exato" if pos_cells >= 0.7 * len(grid) else
           " — poucas combinações positivas: o 9×20 pode ter sido sorte"))

    if grid_tf != "H1":                                   # os ensembles validados são do H1/M30; aqui só a grade importa
        print("\n(ensembles omitidos: grade feita em " + grid_tf + ")")
        return 0
    ensembles = {
        "9×20 H1 sozinho (atual)": [("H1", 9, 20)],
        "E1 · 9×20 + 12×26 + 20×50 (H1)": [("H1", 9, 20), ("H1", 12, 26), ("H1", 20, 50)],
        "E2 · 9×20 H1 + 9×20 M30": [("H1", 9, 20), ("M30", 9, 20)],
        "E3 · 9×20 H1 + 20×50 H1 + 9×20 M30": [("H1", 9, 20), ("H1", 20, 50), ("M30", 9, 20)],
    }
    print(f"\n2) ENSEMBLES · mesmo risco total dividido entre os sistemas · {yrs[0]}–{yrs[-1]} · em ATR do risco total")
    print(f"   {'':38} " + "".join(f"{y:>6}" for y in yrs) + f" │ {'total':>6} {'anos+':>5} {'pior':>5} {'queda':>6} │ {'÷pior':>6} {'÷queda':>6} │ cofre desenv │ veredito")
    base = None
    for name, parts in ensembles.items():
        k = len(parts)
        tr = [(t, r / k) for tf, fa, sl in parts for t, r in run(tf, fa, sl)]
        st = _year_stats(tr, yrs)
        if base is None:
            base, ver = st, "referência"
        else:
            worst_ok = (st["worst"] >= base["worst"]) if base["worst"] >= 0 else st["per_worst"] > base["per_worst"]
            ok = worst_ok and st["per_dd"] > base["per_dd"] and st["cof"] > 0 and st["dev"] > 0
            ver = "✔ ADOTAR" if ok else "✗"
        pw = "∞" if st["per_worst"] == float("inf") else f"{st['per_worst']:.1f}"
        print(f"   {name:38} " + "".join(f"{st['by'][y]:+6.0f}" for y in yrs) +
              f" │ {st['tot']:+6.0f} {st['pos']:3}/{len(yrs):<2} {st['worst']:+5.0f} {st['dd']:6.0f} │ {pw:>6} {st['per_dd']:6.1f} │ {st['cof']:+5.0f} {st['dev']:+6.0f} │ {ver}", flush=True)
    print("\n÷pior = lucro total ÷ perda do pior ano · ÷queda = lucro total ÷ maior queda (quanto maior, melhor a relação lucro/risco).")
    print("Com o risco total igual, um ensemble melhor permite o MESMO lucro com menos susto — ou mais lucro com o mesmo susto.")
    return 0


def exits_main(a) -> int:
    """Compara saídas no modo do painel, ano a ano. Adoção (fixada antes): vencer a saída atual em lucro total, anos
    positivos, pior ano, cofre (antes de 20/05/2026) e desenvolvimento (depois), nos anos com dados densos."""
    import time as _t
    for sym in a.symbol.split(","):
        for tf in a.tfs.split(","):
            spread_def = SPREAD.get(sym, 0.0)
            c = load(a, sym, tf)
            yrs = [y for y in sorted({_t.gmtime(x["time"]).tm_year for x in c})
                   if len([1 for x in c if _t.gmtime(x["time"]).tm_year == y]) > 1000]
            print(f"\nTESTE DE SAÍDAS · {sym} {tf} · entrada SMA 9×20 (stop inicial 2 ATR) · spread real · {yrs[0]}–{yrs[-1]}")
            print(f"  {'saída':30} " + "".join(f"{y:>6}" for y in yrs) +
                  f" │ {'total':>6} {'anos+':>5} {'pior':>5} {'ATR/op':>7} {'acerto':>6} {'ops':>5} │ cofre desenv │ veredito")
            base = None
            for name, rule in EXITS.items():
                r = simulate_exit(c, rule, spread_def)
                rr = [x for x in r if _t.gmtime(x[0]).tm_year >= yrs[0]]
                by = {y: sum(x[1] for x in rr if _t.gmtime(x[0]).tm_year == y) for y in yrs}
                tot, pos, worst = sum(by.values()), sum(v > 0 for v in by.values()), min(by.values())
                cof = sum(x[1] for x in rr if x[0] < CUTOFF)
                dev = sum(x[1] for x in rr if x[0] >= CUTOFF)
                win = sum(x[1] > 0 for x in rr) / max(len(rr), 1)
                if base is None:
                    base, ver = {"tot": tot, "pos": pos, "worst": worst, "cof": cof, "dev": dev}, "referência"
                else:
                    ok = (tot > base["tot"] and pos >= base["pos"] and worst >= base["worst"] and cof > base["cof"] and dev > base["dev"])
                    ver = "✔ ADOTAR" if ok else "✗"
                print(f"  {name:30} " + "".join(f"{by[y]:+6.0f}" for y in yrs) +
                      f" │ {tot:+6.0f} {pos:3}/{len(yrs):<2} {worst:+5.0f} {tot / max(len(rr), 1):+7.3f} {win:6.0%} {len(rr):5} │ {cof:+5.0f} {dev:+6.0f} │ {ver}",
                      flush=True)
    print("\n5 saídas alternativas testadas: só adoto a que vencer em TUDO (total, anos, pior ano, cofre e desenvolvimento).")
    return 0


def regime_main(a) -> int:
    """Filtros de lateralidade sobre o modo do painel (SMA 9×20, sem filtro de ATR, stop 2 ATR), ano a ano.
    Adoção (fixada antes): mais R total E pelo menos tantos anos positivos quanto sem filtro E pior ano não pior,
    olhando 2016+ (histórico denso) — e melhor também no cofre (antes de 20/05/2026) e no desenvolvimento."""
    import time as _t
    for sym in a.symbol.split(","):
        for tf in a.tfs.split(","):
            spread_def = SPREAD.get(sym, 0.0)
            c = load(a, sym, tf)
            rs = regime_series(c)
            yrs = [y for y in sorted({_t.gmtime(x["time"]).tm_year for x in c})
                   if len([1 for x in c if _t.gmtime(x["time"]).tm_year == y]) > 1000]          # anos com histórico denso
            print(f"\nFILTROS DE LATERALIDADE · {sym} {tf} · SMA 9×20 stop 2 ATR · spread real · anos com dados densos: {yrs[0]}–{yrs[-1]}")
            print(f"  {'filtro':18} " + "".join(f"{y:>6}" for y in yrs) + f" │ {'total':>6} {'anos+':>5} {'pior':>5} {'R/op':>6} {'ops':>5} │ cofre  desenv │ veredito")
            base = None
            for name in [None] + list(REGIME):
                r = simulate(c, "sma", 2.0, False, spread_def, 0, len(c), regime=name, rs=rs)
                by = {y: sum(x[1] for x in r if _t.gmtime(x[0]).tm_year == y) for y in yrs}
                rr = [x for x in r if _t.gmtime(x[0]).tm_year >= yrs[0]]
                tot, pos, worst = sum(by.values()), sum(v > 0 for v in by.values()), min(by.values())
                cof = sum(x[1] for x in rr if x[0] < CUTOFF)
                dev = sum(x[1] for x in rr if x[0] >= CUTOFF)
                row = {"tot": tot, "pos": pos, "worst": worst, "cof": cof, "dev": dev}
                if name is None:
                    base, ver = row, "referência"
                else:
                    ok = (tot > base["tot"] and pos >= base["pos"] and worst >= base["worst"]
                          and cof > base["cof"] and dev > base["dev"])
                    ver = "✔ ADOTAR" if ok else "✗"
                print(f"  {(name or 'sem filtro'):18} " + "".join(f"{by[y]:+6.0f}" for y in yrs) +
                      f" │ {tot:+6.0f} {pos:3}/{len(yrs):<2} {worst:+5.0f} {tot / max(len(rr), 1):+6.3f} {len(rr):5} │ {cof:+5.0f} {dev:+6.0f} │ {ver}",
                      flush=True)
    print("\n7 filtros testados: uma vitória pequena pode ser sorte. Só adoto o que vencer em TUDO (total, anos, pior ano, cofre e desenvolvimento).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
