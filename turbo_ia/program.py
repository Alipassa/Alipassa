"""Programa de validação da Reversão ouro — regras escritas ANTES de ver os resultados.

    python -m turbo_ia.program --source mt5 --symbol XAUUSD

Dados divididos no tempo:
  DESENVOLVIMENTO = a partir de CUTOFF (20/05/2026): já foi usado para criar a regra. Aqui cada variação é comparada
                    com a regra atual nas duas metades.
  COFRE           = antes de CUTOFF: NUNCA usado no desenvolvimento. Cada variação é medida aqui UMA vez, no fim.
Regra de adoção (item 7): uma variação só é recomendada se (a) ganhar da regra atual em R por mês nas DUAS metades do
desenvolvimento e (b) ganhar da regra atual no COFRE com ≥ 30 operações. Nada é ligado automaticamente.

Variações (fixadas antes do teste):
  2 · filtro de movimento impulsivo (NÃO é filtro de tendência — olha só a velocidade da esticada):
        impulso5>3 / impulso5>4: o preço andou > 3 / > 4 ATR nas últimas 5 velas
        vela>2: a vela do sinal tem amplitude > 2 ATR
        sequência≥5: 5 ou mais velas seguidas fechando na mesma direção
  3 · entrada após rejeição (em vez de entrar no fechamento da vela esticada):
        rejeição: espera até 3 velas por uma vela que feche voltando (venda: fecha abaixo da abertura e do fechamento
                  anterior); entra no fechamento dela; stop 1 ATR
        rejeição+estrutura: idem, stop além do extremo da esticada + 0,1 ATR (mín. 0,5 ATR, máx. 2 ATR); alvo 3× o stop
  5 · saída parcial × 3R fixo: 50 % sai em +1R / +1,5R / +2R, o resto vai para o zero a zero e busca 3R
  4 · M2 × M3 em walk-forward: a cada mês escolhe o gráfico que rendeu mais nos 60 dias anteriores e opera o mês seguinte
"""

from __future__ import annotations

import argparse
import calendar
from typing import Optional

from .analysis import REVERSION_HOURS
from .backtest import SPREAD, load_mt5, load_yahoo
from .research import features, summary

CUTOFF = calendar.timegm((2026, 5, 20, 0, 0, 0))        # tudo antes disto nunca foi usado no desenvolvimento

BASE = {"name": "regra atual", "impulse": None, "entry": "now", "stop": "atr", "exit": "3R"}
VARIANTS = [
    {"name": "2 · impulso5>3 ATR", "impulse": ("move", 5, 3.0)},
    {"name": "2 · impulso5>4 ATR", "impulse": ("move", 5, 4.0)},
    {"name": "2 · vela do sinal>2 ATR", "impulse": ("bar", 2.0)},
    {"name": "2 · sequência≥5 velas", "impulse": ("streak", 5)},
    {"name": "3 · rejeição (stop 1 ATR)", "entry": "reject"},
    {"name": "3 · rejeição + stop estrutura", "entry": "reject", "stop": "struct"},
    {"name": "5 · parcial 50% em 1R", "exit": ("partial", 1.0)},
    {"name": "5 · parcial 50% em 1,5R", "exit": ("partial", 1.5)},
    {"name": "5 · parcial 50% em 2R", "exit": ("partial", 2.0)},
]
VARIANTS = [{**BASE, **v} for v in VARIANTS]


def impulsive(c: list[dict], a: float, i: int, buy: bool, rule) -> bool:
    """True = a esticada foi IMPULSIVA (rápida demais) → não entra. Só olha o passado da vela i."""
    if not rule:
        return False
    kind = rule[0]
    if kind == "move":
        n, k = rule[1], rule[2]
        mv = c[i]["close"] - c[i - n]["close"]
        return (mv < -k * a) if buy else (mv > k * a)
    if kind == "bar":
        return (c[i]["high"] - c[i]["low"]) > rule[1] * a
    if kind == "streak":
        n = 0
        j = i
        while j > 0 and ((c[j]["close"] < c[j]["open"]) if buy else (c[j]["close"] > c[j]["open"])):
            n += 1
            j -= 1
        return n >= rule[1]
    return False


def simulate(c: list[dict], f: dict, v: dict, lo: int, hi: int, spread_def: float, z_thr: float = 2.0,
             hours=REVERSION_HOURS) -> list[tuple[int, float]]:
    """Uma operação por vez em [lo, hi). Devolve [(tempo da entrada, R líquido de spread)].
    Ambiguidade dentro da vela sempre resolvida contra nós (stop antes de alvo/parcial; zero a zero só na vela seguinte)."""
    sma, atr = f["sma"], f["atr"]
    out: list[tuple[int, float]] = []
    i = max(lo, 30)
    while i < hi - 1:
        a, m = atr[i], sma[i]
        if not a or m is None or (c[i]["time"] // 3600) % 24 not in hours:
            i += 1
            continue
        z = (c[i]["close"] - m) / a
        if abs(z) <= z_thr:
            i += 1
            continue
        buy = z < 0
        if impulsive(c, a, i, buy, v["impulse"]):
            i += 1
            continue
        k = i                                             # vela de entrada
        extreme = c[i]["low"] if buy else c[i]["high"]
        if v["entry"] == "reject":
            k = None
            for j in range(i + 1, min(i + 4, hi - 1)):
                extreme = min(extreme, c[j]["low"]) if buy else max(extreme, c[j]["high"])
                x, prev = c[j], c[j - 1]
                if (x["close"] > x["open"] and x["close"] > prev["close"]) if buy else (x["close"] < x["open"] and x["close"] < prev["close"]):
                    k = j
                    break
            if k is None:
                i += 1
                continue
        e = c[k]["close"]
        ak = atr[k] or a
        dist = ak
        if v["stop"] == "struct":
            dist = min(max(abs(e - extreme) + 0.1 * ak, 0.5 * ak), 2.0 * ak)
        sl = e - dist if buy else e + dist
        tp = e + 3 * dist if buy else e - 3 * dist
        part = v["exit"][1] if isinstance(v["exit"], tuple) else None
        pl = (e + part * dist if buy else e - part * dist) if part else None
        banked, frac, be, res, j = 0.0, 1.0, False, None, k + 1
        while j < len(c):
            x = c[j]
            if (x["low"] <= sl) if buy else (x["high"] >= sl):
                res = banked + frac * ((sl - e) / dist if buy else (e - sl) / dist)
                break
            if (x["high"] >= tp) if buy else (x["low"] <= tp):
                res = banked + frac * 3.0
                break
            if pl is not None and frac == 1.0 and ((x["high"] >= pl) if buy else (x["low"] <= pl)):
                banked, frac = 0.5 * part, 0.5
                be = True
            elif be:
                sl = e                                    # zero a zero vale a partir da vela seguinte à parcial
            j += 1
        if res is None:
            break
        out.append((c[k]["time"], res - (c[k].get("spread") or spread_def) / dist))
        i = j
    return out


def per_month(r: list[tuple[int, float]], t0: int, t1: int) -> float:
    days = max((t1 - t0) / 86400, 1)
    return sum(x for _, x in r) / days * 30


def idx_at(c: list[dict], t: int) -> int:
    lo, hi = 0, len(c)
    while lo < hi:
        mid = (lo + hi) // 2
        if c[mid]["time"] < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def evaluate(c: list[dict], spread_def: float, cutoff: int) -> dict:
    f = features(c)
    cut = idx_at(c, cutoff)
    n = len(c)
    mid = cut + (n - cut) // 2
    spans = {"A": (cut, mid), "B": (mid, n), "COFRE": (0, cut)}
    rows = {}
    for v in [BASE] + VARIANTS:
        rows[v["name"]] = {}
        for k, (lo, hi) in spans.items():
            if hi - lo < 500:
                rows[v["name"]][k] = None
                continue
            r = simulate(c, f, v, lo, hi, spread_def)
            s = summary([x for _, x in r])
            s["mes"] = per_month(r, c[lo]["time"], c[hi - 1]["time"])
            rows[v["name"]][k] = s
    return {"rows": rows, "cofre_dias": (c[cut - 1]["time"] - c[0]["time"]) / 86400 if cut > 1 else 0,
            "dev_dias": (c[-1]["time"] - c[cut]["time"]) / 86400 if cut < n else 0}


def walk_forward(c2: list[dict], c3: list[dict], spread_def: float, cutoff: int, train_days: int = 60) -> None:
    """Item 4: a cada mês escolhe M2 ou M3 pelo R/mês dos 60 dias anteriores e opera o mês seguinte."""
    f2, f3 = features(c2), features(c3)
    r2 = simulate(c2, f2, BASE, 0, len(c2), spread_def)
    r3 = simulate(c3, f3, BASE, 0, len(c3), spread_def)
    t_start = max(c2[0]["time"], c3[0]["time"]) + train_days * 86400
    t_end = min(c2[-1]["time"], c3[-1]["time"])
    month = 30 * 86400
    tot = {"sempre M2": 0.0, "sempre M3": 0.0, "walk-forward": 0.0}
    cof = dict.fromkeys(tot, 0.0)
    picks = []
    t = t_start
    while t + month <= t_end:
        tr2 = sum(x for tt, x in r2 if t - train_days * 86400 <= tt < t)
        tr3 = sum(x for tt, x in r3 if t - train_days * 86400 <= tt < t)
        m2 = sum(x for tt, x in r2 if t <= tt < t + month)
        m3 = sum(x for tt, x in r3 if t <= tt < t + month)
        pick = "M2" if tr2 >= tr3 else "M3"
        got = m2 if pick == "M2" else m3
        tot["sempre M2"] += m2
        tot["sempre M3"] += m3
        tot["walk-forward"] += got
        if t + month <= cutoff:
            cof["sempre M2"] += m2
            cof["sempre M3"] += m3
            cof["walk-forward"] += got
        picks.append(pick)
        t += month
    nm = len(picks)
    print(f"\n4 · WALK-FORWARD M2 × M3 (treino {train_days} dias → opera o mês seguinte · {nm} meses de teste)")
    if not nm:
        print("  histórico curto demais para walk-forward (precisa de pelo menos 3 meses de M2 e M3)")
        return
    for k in tot:
        print(f"  {k:13} total {tot[k]:+7.1f} R · média {tot[k] / nm:+5.1f} R/mês" + (f" · só no COFRE {cof[k]:+6.1f} R" if any(cof.values()) else ""))
    print(f"  escolhas: {''.join('2' if p == 'M2' else '3' for p in picks)}  (2 = M2, 3 = M3)")
    best = max(tot, key=tot.get)
    print(f"  → melhor: {best}" + ("  (trocar de gráfico todo mês NÃO compensou)" if best != "walk-forward" else "  (o walk-forward compensou)"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.program")
    ap.add_argument("--source", choices=["mt5", "yahoo"], default="mt5")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--bars", type=int, default=2_000_000)
    ap.add_argument("--cutoff", help="AAAA-MM-DD: antes disto = cofre (padrão 2026-05-20)")
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    cutoff = CUTOFF
    if a.cutoff:
        y, mo, d = (int(x) for x in a.cutoff.split("-"))
        cutoff = calendar.timegm((y, mo, d, 0, 0, 0))
    load = (lambda tf: load_mt5(a.symbol, tf, a.bars)) if a.source == "mt5" else (lambda tf: load_yahoo(a.symbol, tf))
    spread_def = SPREAD.get(a.symbol, 0.30)
    data = {}
    for tf in ("M2", "M3"):
        try:
            data[tf] = load(tf)
        except Exception as e:  # noqa: BLE001
            print(f"  {tf}: sem dados ({e})")
    for tf, c in data.items():
        res = evaluate(c, spread_def, cutoff)
        rows = res["rows"]
        print(f"\n{a.symbol} {tf} · desenvolvimento {res['dev_dias']:.0f} dias (A | B) · COFRE nunca usado {res['cofre_dias']:.0f} dias")
        print(f"  {'variação':32} {'A R/mês':>8} {'B R/mês':>8} │ {'COFRE: ops acerto  R/op  R/mês queda':38} │ veredito")
        base = rows[BASE["name"]]
        for name, r in rows.items():
            A, B, C = r["A"], r["B"], r["COFRE"]
            fmt = lambda s: f"{s['mes']:+8.1f}" if s else f"{'—':>8}"  # noqa: E731
            cof = (f"{C['n']:4} {C['win']:6.1%} {C['exp']:+.3f} {C['mes']:+6.1f} {C['dd']:4.0f}R" if C else "sem dados nunca usados".ljust(38))
            if name == BASE["name"]:
                ver = "referência"
            else:
                dev_ok = A and B and base["A"] and base["B"] and A["mes"] >= base["A"]["mes"] and B["mes"] >= base["B"]["mes"]
                if not dev_ok:
                    ver = "✗ perdeu no desenvolvimento"
                elif not C or not base["COFRE"] or C["n"] < 30:
                    ver = "? passou no desenv.; cofre insuficiente"
                elif C["mes"] > base["COFRE"]["mes"]:
                    ver = "✔ ADOTAR (venceu no cofre)"
                else:
                    ver = "✗ perdeu no cofre"
            print(f"  {name:32} {fmt(A)} {fmt(B)} │ {cof:38} │ {ver}", flush=True)
    if "M2" in data and "M3" in data:
        walk_forward(data["M2"], data["M3"], spread_def, cutoff)
    print("\nRegra 7: só mude a configuração para o que estiver marcado ✔ ADOTAR. Com 9 variações testadas, uma vitória "
          "pequena no cofre ainda pode ser sorte — confirme em DEMO.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
