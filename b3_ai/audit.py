"""Auditoria: as fontes estão respondendo? Quanto cada previsão acertou de verdade, fora da amostra?

    python -m b3_ai auditoria            # imprime e grava relatorios/auditoria_AAAAMMDD_HHMM.md

Cada número é medido no histórico real disponível no momento. O intervalo de 95 % diz se o acerto é
distinguível do acaso (50 %): só é "vantagem comprovada" quando o limite inferior passa de 50 %.
"""

from __future__ import annotations

import math
import time
from datetime import datetime

from . import model as M, technical as T
from .data import bcb, calendar as cal, focus, news, tradingview as tv
from .engine import Engine
from .model import BRT
from .universe import TARGETS


def interval(p: float, n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 1.0
    h = 1.96 * math.sqrt(p * (1 - p) / n)
    return p - h, p + h


def verdict(p, n: int) -> str:
    if p is None or not n:
        return "sem amostra"
    lo, hi = interval(p, n)
    return "✅ vantagem comprovada" if lo > 0.5 else "❌ pior que o acaso" if hi < 0.5 else "⚪ não distinguível do acaso"


def check_sources(engine: Engine) -> list[tuple[str, bool, str]]:
    out = []

    def run(name: str, fn) -> None:
        t = time.time()
        try:
            detail = fn()
            out.append((name, True, f"{detail} · {time.time() - t:.1f}s"))
        except Exception as e:  # noqa: BLE001 — auditoria relata a falha, não aborta
            out.append((name, False, str(e)[:140]))

    run("B3 — todas as empresas (scanner)", lambda: f"{len(tv.scan_b3())} papéis")
    run("Histórico WIN (feed público)", lambda: f"{len(tv.fetch_one(TARGETS['WIN'].tv, '1D', 50) or [])} barras")
    run("Banco Central (Selic, PTAX, IPCA)", lambda: f"{len(bcb.snapshot())} séries")
    run("Boletim Focus", lambda: f"{len(focus.fetch_focus())} indicadores")
    run("Notícias (RSS)", lambda: (lambda r: f"{len(r[0])} manchetes, {len(r[1])} feeds com erro")(news.fetch_news(pause=0.3)))
    run("Agenda (Fed, Copom)", lambda: (lambda a: f"{len(a['eventos'])} eventos" + ("; " + "; ".join(a["erros"]) if a["erros"] else ""))(cal.build(engine.data_dir)))
    if engine.mt5 is not None:
        run("MetaTrader 5 (tempo real)", lambda: ", ".join(engine.mt5.snapshot()["simbolos"].values()))
    else:
        out.append(("MetaTrader 5 (tempo real)", True, "não ativado (rode com --fonte mt5)"))
    return out


def s_bars(engine: Engine, key: str) -> list[tuple]:
    return engine._audit_bars.get(key, [])


def run(engine: Engine) -> str:
    sources = check_sources(engine)
    s = engine.cycle()
    dres, ires = engine._daily_cache["res"], engine._intra_cache["res"]
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    L = ["# Auditoria do B3 AI", "", f"Gerada em {when} (Brasília) · pregão de referência {s['pregao']} · {s['atraso']}", "",
         "## 1. Fontes de dados", "", "| Fonte | Situação | Detalhe |", "| --- | --- | --- |"]
    L += [f"| {n} | {'✅ ok' if ok else '❌ falhou'} | {d} |" for n, ok, d in sources]
    for w in s["avisos"]:
        L.append(f"\n> aviso do ciclo: {w}")
    L += ["", "## 2. Direção — acerto fora da amostra (walk-forward)", "",
          "O modelo é treinado só com o passado e prevê o trecho seguinte. Acerto = previu alta e subiu, ou previu queda e caiu.", "",
          "| Ativo | Horizonte | Previsões | Acertos | Acerto | Intervalo 95% | Conclusão | Acerto com convicção (≥55/45) |",
          "| --- | --- | ---: | ---: | ---: | --- | --- | --- |"]
    tot_n = tot_h = 0
    for k in TARGETS:
        rows = []
        for (kk, tf), ir in ires.items():
            if kk == k and ir:
                rows += [(name, m["eval"]) for name, m in ir["models"].items() if m]
        if k in dres:
            rows += [("Diário", dres[k]["models"][1]["eval"]), ("Semanal", dres[k]["models"][5]["eval"])]
        order = {"5 min": 0, "10 min": 1, "30 min": 2, "1 hora": 3, "Diário": 4, "Semanal": 5}
        for name, ev in sorted(rows, key=lambda r: order.get(r[0], 9)):
            n, p = ev["n"], ev["hit"]
            if p is None:
                continue
            lo, hi = interval(p, n)
            tot_n += n
            tot_h += round(p * n)
            conv = f"{ev['hit_conv'] * 100:.1f}% em {ev['n_conv']}" if ev.get("hit_conv") is not None else f"— ({ev['n_conv']} casos)"
            L.append(f"| {k} | {name} | {n} | {round(p * n)} | **{p * 100:.1f}%** | {lo * 100:.1f}–{hi * 100:.1f}% | {verdict(p, n)} | {conv} |")
    if tot_n:
        L += ["", f"Total: {tot_h} acertos em {tot_n} previsões de direção ({tot_h / tot_n * 100:.1f}%)."]
    L += ["", "## 3. Faixa de preço de 80% — cobertura fora da amostra", "",
          "A faixa é calculada com a 1ª metade do histórico e conferida na 2ª. O alvo é 80%.", "",
          "| Ativo | Horizonte | Casos | Dentro da faixa |", "| --- | --- | ---: | ---: |"]
    for k in TARGETS:
        for (kk, tf), ir in sorted(ires.items()):
            if kk == k and ir:
                L += [f"| {k} | {name} | {b['n']} | **{b['cobertura'] * 100:.1f}%** |" for name, b in ir["band80"].items() if b]
        if k in dres:
            L += [f"| {k} | {'Diário' if h == 1 else 'Semanal'} | {b['n']} | **{b['cobertura'] * 100:.1f}%** |" for h, b in dres[k]["band80"].items() if b]
    est = s.get("abertura_estudo") or engine._opening().get("estudo") or {}
    L += ["", "## 3b. Direção da abertura a partir do exterior durante a noite", "",
          "Movimento do líder (S&P para o mini índice, dólar × peso mexicano para o mini dólar) entre o fechamento da B3 e a "
          "abertura seguinte × direção do gap de abertura. A coluna da 2ª metade mostra se o acerto se manteve.", "",
          "| Ativo | Movimento do líder | Casos | Acerto | Acerto na 2ª metade |", "| --- | --- | ---: | ---: | ---: |"]
    nomes = ["pequeno (< 0,25σ)", "médio (0,25 a 0,75σ)", "grande (> 0,75σ)"]
    for k, e in est.items():
        if not e:
            continue
        L.append(f"| {k} | todos | {e['n']} | **{e['acerto'] * 100:.1f}%** | {e['acerto2'] * 100:.1f}% em {e['n2']} |")
        L += [f"| {k} | {nomes[j]} | {b['n']} | **{(b['acerto'] or 0) * 100:.1f}%** | {(b['acerto2'] or 0) * 100:.1f}% em {b['n2']} |" for j, b in enumerate(e["bins"])]
        if e.get("devolve"):
            L.append(f"| {k} | depois de gap grande, pregão fechou CONTRA o gap | {e['devolve']['n']} | **{e['devolve']['pct'] * 100:.0f}%** | — |")
    L += ["", "## 3c. Padrões descobertos (cruzamento das informações, com confirmação fora da amostra)", ""]
    for k, a_ in s["ativos"].items():
        P = a_.get("padroes") or {}
        L.append(f"**{k}**: {P.get('testados', 0)} combinações testadas, {P.get('confirmados', 0)} confirmadas "
                 f"(esperado por acaso: ~{P.get('esperado_por_sorte', 0)}).")
        for x in (P.get("padroes") or [])[:10]:
            L.append(f"- {' + '.join(x['condicoes'])} → {x['alvo_nome']}: **{x['direcao']} em {x['chance'] * 100:.0f}%** de {x['n']} casos "
                     f"(na descoberta {max(x['p_descoberta'], 1 - x['p_descoberta']) * 100:.0f}% de {x['n_descoberta']}; "
                     f"sem o padrão {(x['base'] if x['lado'] > 0 else 1 - x['base']) * 100:.0f}%)" + (" — ATIVO AGORA" if x["ativo"] else ""))
    L += ["", "## 4. Sinal de reversão nos extremos (5 min)", "",
          "| Ativo | Sinais | Acerto em 30 min | Intervalo 95% | Conclusão | Acerto em 1 h | Ganho médio em 30 min |", "| --- | ---: | ---: | --- | --- | ---: | ---: |"]
    for k, a in s["ativos"].items():
        rv = a.get("reversao")
        if rv:
            h30, h60 = rv["historico"]["30 min"], rv["historico"]["60 min"]
            if h30["acerto"] is not None:
                lo, hi = interval(h30["acerto"], h30["n"])
                L.append(f"| {k} | {h30['n']} | **{h30['acerto'] * 100:.1f}%** | {lo * 100:.0f}–{hi * 100:.0f}% | {verdict(h30['acerto'], h30['n'])} | "
                         f"{(h60['acerto'] or 0) * 100:.1f}% | {h30['media_pct']:+.3f}% |")
    L += ["", "## 5. Choques do exterior → pregão do mini índice (base dos alertas)", "",
          "| Quando… | Pregões | O mini índice fechou em | Frequência | Média no dia | Pregão seguinte em alta |", "| --- | ---: | --- | ---: | ---: | ---: |"]
    from .alerts import SHOCKS
    d = M.DailyData(*engine._audit_daily) if getattr(engine, "_audit_daily", None) else None
    if d is not None:
        wdo = engine._daily_cache["res"].get("WDO") and M.DailyData(s_bars(engine, "WDO"), {}, {})
        if wdo:
            cm = dict(zip(wdo.dates, wdo.ret))
            d.x["CROSS"] = [cm.get(x, 0.0) for x in d.dates]
        for fk, direction, desc in SHOCKS:
            series = d.x.get(fk)
            ev = M.event_study(d, series, direction) if series else None
            if ev:
                up = ev["dia_pct_alta"] >= 0.5
                L.append(f"| {desc} | {ev['n']} | {'alta' if up else 'queda'} | **{max(ev['dia_pct_alta'], 1 - ev['dia_pct_alta']) * 100:.0f}%** | "
                         f"{ev['dia_media']:+.2f}% | {ev['seguinte_pct_alta'] * 100:.0f}% |")
    h = (s.get("agenda") or {}).get("fomc_historico")
    if h:
        L += ["", f"Dias de decisão do Fed ({h['n']} desde 2021): oscilação média de {h['abs_dia']:.2f}% contra {h['abs_normal']:.2f}% num dia comum."]
    pl = [x for x in s.get("placar", []) if x["n"]]
    L += ["", "## 6. Placar ao vivo (previsões gravadas antes do resultado)", ""]
    L += (["| Ativo | Horizonte | Conferidas | Acerto |", "| --- | --- | ---: | ---: |"] +
          [f"| {x['ativo']} | {x['horizonte']} | {x['n']} | {x['acerto'] * 100:.0f}% |" for x in pl]) if pl else \
        ["Ainda sem previsões conferidas: o placar começa a encher depois do primeiro pregão com o painel ligado."]
    L += ["", "## Como ler", "",
          "- **Direção**: só os horizontes com ✅ têm vantagem estatística; nos demais a probabilidade do painel fica perto de 50% de propósito.",
          "- **Faixa de 80%**: é a previsão mais confiável do sistema — use para alvo e stop.",
          "- **Choques**: relação medida no mesmo pregão; é o que dispara os alertas de 75% e 90%.",
          "- Acerto passado não garante acerto futuro; refaça esta auditoria de tempos em tempos.", ""]
    return "\n".join(L)
