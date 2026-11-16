"""Descoberta de padrões: cruza as informações do painel duas a duas e guarda só o que se repete fora da amostra.

Método (para não confundir coincidência com padrão):
1. CONDIÇÕES conhecidas no fechamento de cada pregão — exterior, descolamento Brasil × exterior, momento, RSI, distância da
   média, VIX, dólar/bolsa cruzados, regime de volatilidade, dia forte, sequência de altas/quedas, dia da semana seguinte —
   e, para o alvo intraday, o tamanho do gap de abertura.
2. ALVOS: o pregão seguinte fecha acima do fechamento? fecha acima da abertura (direção do dia depois de abrir)?
3. DESCOBERTA nos primeiros 60 % do histórico: condição isolada ou par de condições com pelo menos 40 casos, desvio de 7 pontos
   percentuais da taxa-base e z ≥ 2.
4. CONFIRMAÇÃO nos 40 % finais, que a descoberta não viu: mesmo sentido, pelo menos 20 casos, desvio ≥ 4 pontos e z ≥ 1,64.
5. Só os confirmados aparecem, com a chance medida na confirmação. Como são centenas de hipóteses, algumas passam por sorte:
   o módulo informa quantas eram esperadas por acaso.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Optional

from . import model as M, stats as st

WEEK = ["segunda", "terça", "quarta", "quinta", "sexta"]
# (índice do fator diário, limiar, nome quando acima, nome quando abaixo)
FEATS = [(0, 0.5, "exterior positivo no dia", "exterior negativo no dia"),
         (1, 1.0, "Brasil bem acima do que o exterior implicava", "Brasil bem abaixo do que o exterior implicava"),
         (2, 1.0, "5 pregões de alta forte", "5 pregões de queda forte"),
         (3, 0.4, "RSI diário acima de 70", "RSI diário abaixo de 30"),
         (4, 1.0, "preço esticado acima da média de 20", "preço esticado abaixo da média de 20"),
         (5, 1.0, "VIX em nível alto", "VIX em nível baixo"),
         (6, 1.0, "ativo cruzado subiu forte", "ativo cruzado caiu forte"),
         (7, 0.3, "volatilidade recente alta", "volatilidade recente baixa")]
TARGETS = {"fech": "fecha acima do fechamento anterior", "intra": "fecha acima da abertura (direção do dia depois de abrir)"}


def conditions(d: "M.DailyData", res: dict, opens: list[float], cross_name: str) -> dict[str, dict]:
    """{nome: {"v": [bool por pregão t], "grupo": id, "abertura": bool}} — tudo conhecido no fechamento de t,
    menos as de gap ("abertura": True), que usam a abertura de t+1."""
    n = len(d.close)
    feats, sig = res["feats"], res["sigma"]
    C: dict[str, dict] = {}

    def add(name: str, vals: list[bool], group: str, at_open: bool = False) -> None:
        C[name] = {"v": vals, "grupo": group, "abertura": at_open}

    for j, thr, up, dn in FEATS:
        up, dn = up.replace("ativo cruzado", cross_name), dn.replace("ativo cruzado", cross_name)
        add(up, [bool(f) and f[j] > thr for f in feats], f"f{j}")
        add(dn, [bool(f) and f[j] < -thr for f in feats], f"f{j}")
    z = [d.ret[t] / sig[t] if sig[t] else 0.0 for t in range(n)]
    add("pregão de alta forte (> 1 desvio)", [x > 1 for x in z], "dia")
    add("pregão de queda forte (> 1 desvio)", [x < -1 for x in z], "dia")
    add("3 altas seguidas", [t >= 2 and d.ret[t] > 0 and d.ret[t - 1] > 0 and d.ret[t - 2] > 0 for t in range(n)], "seq")
    add("3 quedas seguidas", [t >= 2 and d.ret[t] < 0 and d.ret[t - 1] < 0 and d.ret[t - 2] < 0 for t in range(n)], "seq")
    nxt = [next_weekday(x) for x in d.dates]
    for w in range(5):
        add(f"pregão seguinte é {WEEK[w]}", [x == w for x in nxt], "semana")
    gap = [((opens[t + 1] / d.close[t] - 1) / sig[t] if t + 1 < n and sig[t] else None) for t in range(n)]
    add("abre com gap de alta (> 0,5 desvio)", [g is not None and g > 0.5 for g in gap], "gap", True)
    add("abre com gap de queda (> 0,5 desvio)", [g is not None and g < -0.5 for g in gap], "gap", True)
    add("abre sem gap relevante", [g is not None and abs(g) <= 0.2 for g in gap], "gap", True)
    return C


def next_weekday(iso: str) -> int:
    x = datetime.strptime(iso, "%Y-%m-%d") + timedelta(days=1)
    while x.weekday() > 4:
        x += timedelta(days=1)
    return x.weekday()


def _stat(sel: list[int], y: list[Optional[int]], base: float) -> tuple[int, float, float]:
    ys = [y[t] for t in sel if y[t] is not None]
    n = len(ys)
    if not n:
        return 0, 0.0, 0.0
    p = sum(ys) / n
    return n, p, (p - base) / math.sqrt(base * (1 - base) / n)


def mine(d: "M.DailyData", res: dict, opens: list[float], cross_name: str, complete: bool) -> dict:
    n = len(d.close)
    end = n if complete else n - 1                       # pregões fechados
    C = conditions(d, res, opens, cross_name)
    ys = {"fech": [(1 if d.close[t + 1] > d.close[t] else 0) if t + 1 < end else None for t in range(n)],
          "intra": [(1 if d.close[t + 1] > opens[t + 1] else 0) if t + 1 < end and d.close[t + 1] != opens[t + 1] else None for t in range(n)]}
    valid = [t for t in range(300, end - 1) if res["feats"][t]]
    if len(valid) < 300:
        return {"padroes": [], "testados": 0, "esperado_por_sorte": 0.0, "amostra": len(valid)}
    cut = valid[int(len(valid) * 0.6)]
    tr, te = [t for t in valid if t < cut], [t for t in valid if t >= cut]
    names = list(C)
    live_t = n - 1 if complete else n - 2                 # condição "de ontem" para o pregão seguinte (ou o de hoje, se aberto)
    found, tested = [], 0
    for tgt, y in ys.items():
        base_tr, base_te = st.mean([y[t] for t in tr if y[t] is not None]), st.mean([y[t] for t in te if y[t] is not None])
        usable = [k for k in names if tgt == "intra" or not C[k]["abertura"]]
        combos = [(a,) for a in usable] + [(a, b) for i, a in enumerate(usable) for b in usable[i + 1:] if C[a]["grupo"] != C[b]["grupo"]]
        for combo in combos:
            vs = [C[k]["v"] for k in combo]
            sel_tr = [t for t in tr if all(v[t] for v in vs)]
            n1, p1, z1 = _stat(sel_tr, y, base_tr)
            if n1 < 40:
                continue
            tested += 1
            if abs(p1 - base_tr) < 0.07 or abs(z1) < 2:
                continue
            n2, p2, z2 = _stat([t for t in te if all(v[t] for v in vs)], y, base_te)
            side = 1 if p1 > base_tr else -1
            if n2 < 20 or side * (p2 - base_te) < 0.04 or side * z2 < 1.64:
                continue
            needs_open = any(C[k]["abertura"] for k in combo)
            known = [C[k]["v"][live_t] for k in combo if not C[k]["abertura"]]
            gap_known = not needs_open or not complete          # com o pregão aberto, o gap de hoje já é conhecido
            active = all(known) and (not needs_open or (gap_known and all(C[k]["v"][live_t] for k in combo if C[k]["abertura"])))
            found.append({"condicoes": list(combo), "alvo": tgt, "alvo_nome": TARGETS[tgt], "lado": side,
                          "p_descoberta": p1, "n_descoberta": n1, "p": p2, "n": n2, "base": base_te, "z": side * z2,
                          "chance": p2 if side > 0 else 1 - p2, "direcao": "alta" if side > 0 else "queda",
                          "ativo": bool(active), "aguarda_abertura": bool(all(known) and needs_open and complete)})
    found.sort(key=lambda x: (-x["ativo"], -x["aguarda_abertura"], -x["z"]))
    # por acaso: P(passar na descoberta) ≈ 4,6 % (z ≥ 2 bilateral) × P(confirmar) ≈ 5 % (z ≥ 1,64 no mesmo sentido)
    return {"padroes": found[:40], "confirmados": len(found), "testados": tested, "esperado_por_sorte": round(tested * 0.046 * 0.05, 1),
            "amostra": len(valid), "descoberta": len(tr), "confirmacao": len(te)}


def mine_session(bars15: list[tuple], is_open: bool) -> dict:
    """Padrões DENTRO do pregão (barras de 15 min): o que o gap e a primeira hora dizem sobre o resto do dia.
    Alvo: fechar acima do preço do fim da primeira hora. Mesma regra de descoberta (60 %) e confirmação (40 %)."""
    ss = [x for x in M.sessions_gap(bars15, 2 * 3600) if len(x) >= 12]
    if len(ss) < 120:
        return {"padroes": [], "testados": 0, "confirmados": 0, "esperado_por_sorte": 0.0, "amostra": len(ss)}
    closes = [x[-1][4] for x in ss]
    rows = []            # por pregão k ≥ 21: (gap_z, primeira_hora_z, véspera_z, y)
    for k in range(21, len(ss)):
        sd = st.std([closes[j] / closes[j - 1] - 1 for j in range(k - 20, k)]) or 1e-9
        op, h1, cl = ss[k][0][1], ss[k][3][4], ss[k][-1][4]
        rows.append(((op / closes[k - 1] - 1) / sd, (h1 / op - 1) / sd, (closes[k - 1] / closes[k - 2] - 1) / sd,
                     None if cl == h1 else (1 if cl > h1 else 0)))
    C = {"abre com gap de alta (> 0,5 desvio)": ([r[0] > 0.5 for r in rows], "gap"),
         "abre com gap de queda (> 0,5 desvio)": ([r[0] < -0.5 for r in rows], "gap"),
         "primeira hora de alta forte (> 0,4 desvio)": ([r[1] > 0.4 for r in rows], "h1"),
         "primeira hora de queda forte (> 0,4 desvio)": ([r[1] < -0.4 for r in rows], "h1"),
         "primeira hora parada (< 0,15 desvio)": ([abs(r[1]) < 0.15 for r in rows], "h1"),
         "véspera de alta forte": ([r[2] > 1 for r in rows], "vesp"),
         "véspera de queda forte": ([r[2] < -1 for r in rows], "vesp")}
    y = [r[3] for r in rows]
    n = len(rows) - (1 if is_open else 0)                 # com o pregão aberto, a última sessão ainda não tem resultado
    cut = int(n * 0.6)
    tr, te = list(range(cut)), list(range(cut, n))
    base_tr, base_te = st.mean([y[t] for t in tr if y[t] is not None]), st.mean([y[t] for t in te if y[t] is not None])
    names = list(C)
    combos = [(a,) for a in names] + [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if C[a][1] != C[b][1]]
    found, tested = [], 0
    live_ok = is_open and len(ss[-1]) >= 4                 # a primeira hora de hoje já fechou
    for combo in combos:
        vs = [C[k][0] for k in combo]
        n1, p1, z1 = _stat([t for t in tr if all(v[t] for v in vs)], y, base_tr)
        if n1 < 25:
            continue
        tested += 1
        if abs(p1 - base_tr) < 0.08 or abs(z1) < 2:
            continue
        side = 1 if p1 > base_tr else -1
        n2, p2, z2 = _stat([t for t in te if all(v[t] for v in vs)], y, base_te)
        if n2 < 15 or side * (p2 - base_te) < 0.05 or side * z2 < 1.64:
            continue
        found.append({"condicoes": list(combo), "alvo": "h1", "alvo_nome": "fecha acima do preço do fim da primeira hora", "lado": side,
                      "p_descoberta": p1, "n_descoberta": n1, "p": p2, "n": n2, "base": base_te, "z": side * z2,
                      "chance": p2 if side > 0 else 1 - p2, "direcao": "alta" if side > 0 else "queda",
                      "ativo": bool(live_ok and all(v[-1] for v in vs)), "aguarda_abertura": not live_ok})
    found.sort(key=lambda x: (-x["ativo"], -x["z"]))
    return {"padroes": found, "confirmados": len(found), "testados": tested, "esperado_por_sorte": round(tested * 0.046 * 0.05, 1),
            "amostra": len(rows)}
