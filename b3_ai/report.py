"""Relatório do mini índice e do mini dólar — Markdown (arquivo/Telegram) e HTML para imprimir ou salvar em PDF."""

from __future__ import annotations

import html
from datetime import datetime

from .model import BRT


def _n(v, d: int = 0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(v, d: int = 2) -> str:
    return "—" if v is None else f"{v:+.{d}f}%".replace(".", ",")


def _p(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _var(q: dict) -> str:
    return "—" if q.get("var") is None else (f"{q['var']:+.1f} pb".replace(".", ",") if q["tipo"] == "yield" else _pct(q["var"]))


def markdown(s: dict) -> str:
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    L: list[str] = [f"# Relatório — Mini Índice (WIN) e Mini Dólar (WDO)", "",
                    f"Gerado em {when} (Brasília) · pregão de referência {s['pregao']} · mercado "
                    f"{'ABERTO' if s['mercado_aberto'] else 'FECHADO'}", "", f"> {s['atraso']}", ""]
    for a in s.get("alertas") or []:
        L += [f"> **{a['nivel']}** {'▲' if a['lado'] > 0 else '▼'} {a['titulo']}. {a['texto']}", ""]
    sy = s.get("sintese")
    if sy:
        L += ["## Síntese para decisão — mini índice", "",
              f"**{sy['rotulo']}**: {sy['alta']} frentes a favor da alta, {sy['queda']} a favor da queda, {sy['neutro']} neutras "
              "(contagem do que concorda — não é probabilidade).", ""]
        L += [f"- {'▲' if x['lado'] > 0 else '▼' if x['lado'] < 0 else '■'} **{x['pilar']}** — {x['detalhe']}" for x in sy["pilares"]] + [""]
    ag = s.get("agenda") or {}
    if ag.get("eventos"):
        L += ["## Agenda: dias que podem mexer com o mercado", "", "| Quando | Data | Hora (Brasília) | Evento | Impacto |", "| --- | --- | --- | --- | --- |"]
        L += [f"| {e['quando']} | {e['dia_semana']} | {e['hora']} | {e['pais']}: {e['evento']}{' (data estimada)' if e['estimada'] else ''} | {e['impacto']} |"
              for e in ag["eventos"][:12]]
        h = ag.get("fomc_historico")
        if h:
            L += ["", f"Medido em {h['n']} decisões do Fed desde 2021: o mini índice oscilou em média {_n(h['abs_dia'], 2)}% no dia e "
                      f"{_n(h['abs_seguinte'], 2)}% no seguinte, contra {_n(h['abs_normal'], 2)}% num dia comum; fechou em alta em "
                      f"{_p(h['pct_alta_dia'])} dos dias de decisão. Extremos: {_pct(h['maior_queda'])} e {_pct(h['maior_alta'])}."]
        L.append("")
    ia = s.get("ia") or {}
    if ia.get("ok"):
        L += ["## Opinião do painel", "", ia["texto"].replace("\n## ", "\n### "), ""]
    L += ["## Leitura da IA", ""] + [f"- {x}" for x in s.get("leitura", [])] + [""]
    for k in ("WIN", "WDO"):
        a = s["ativos"].get(k)
        if not a:
            continue
        dec = 0 if k == "WIN" else 1
        L += [f"## {a['nome']}", "",
              f"Último {_n(a['preco'], dec)} ({_pct(a['var'])}) · máx. {_n(a['max'], dec)} · mín. {_n(a['min'], dec)} · "
              f"volatilidade diária típica {_n(a['sigma_dia_pct'], 2)}%", "",
              "### Quadro de probabilidades", "",
              "| Horizonte | Alvo | ALTA | QUEDA | Sinal | Acerto da direção (fora da amostra) | Faixa de 80% (cobertura medida) |",
              "| --- | --- | ---: | ---: | --- | --- | --- |"]
        for r in a["prob"]:
            oos = f"{r['oos_acerto'] * 100:.1f}% em {r['oos_n']}" if r.get("oos_acerto") is not None else "—"
            L.append(f"| {r['horizonte']} | {r['alvo']} | **{_p(r['p_alta'])}** | **{_p(r['p_queda'])}** | {r['sinal']}"
                     f"{'' if r['validado'] else ' ⚪'} | {oos} | """ + (f"{_n(r['f80_min'], dec)} – {_n(r['f80_max'], dec)} ({r['f80_cobertura'] * 100:.0f}%)" if r.get("f80_min") is not None
                                    else f"{_n(r['faixa_min'], dec)} – {_n(r['faixa_max'], dec)} (±1σ)") + " |")
        L += ["", "⚪ = o modelo desse horizonte não provou vantagem fora da amostra; a probabilidade fica perto da taxa-base.", ""]
        d = next((r for r in a["prob"] if r["horizonte"] == "Diário"), None)
        if d:
            f = d["faixas"]
            L += ["### Cenários para o próximo pregão", "",
                  f"| Alta forte (> +{_n(f['sigma_pct'], 2)}%) | Alta moderada | Queda moderada | Queda forte (< −{_n(f['sigma_pct'], 2)}%) |",
                  "| ---: | ---: | ---: | ---: |",
                  f"| {_p(f['alta_forte'])} | {_p(f['alta'])} | {_p(f['queda'])} | {_p(f['queda_forte'])} |", ""]
            if d["motivos"]:
                L += ["O que mais pesa na previsão diária: " + "; ".join(
                    f"{m['nome']} ({'a favor da alta' if m['efeito'] > 0 else 'a favor da queda'})" for m in d["motivos"]) + ".", ""]
        c = a["confronto"]
        L += ["### Confronto exterior × Brasil", "",
              f"Exterior implicava **{_pct(c['implicito'])}** · realizado **{_pct(c['realizado'])}** · descolamento "
              f"**{_pct(c['descolamento'])}** ({c['descolamento_sigma']:+.1f}σ) · o exterior explica {c['r2'] * 100:.0f}% da variação diária.",
              "", c["leitura"], "", "| Fator internacional | Variação | Contribuição | Correlação 60d |", "| --- | ---: | ---: | ---: |"]
        for x in c["fatores"]:
            v = f"{x['var']:+.1f} pb".replace(".", ",") if x["tipo"] == "yield" else _pct(x["var"])
            L.append(f"| {x['nome']} | {v} | {_pct(x['contrib'])} | {_n(x['corr60'], 2)} |")
        if a.get("tendencias"):
            L += ["", "### Tendência", ""] + [f"- **{t['prazo']}**: {t['tendencia']} ({_pct(t['var'])}; "
                                             f"{sum(1 for c in t['criterios'] if c['alta'])} de {len(t['criterios'])} critérios de alta)" for t in a["tendencias"]]
        rv = a.get("reversao")
        if rv:
            h = rv["historico"]["30 min"]
            L += ["", "### Extremos de compra e venda (5 min)", "",
                  f"{rv['rotulo']} · RSI {_n(rv['rsi'], 0)} · {rv['vwap_z']:+.1f} desvios do VWAP. Sinal medido: em {h['n']} reversões, o preço foi na "
                  f"direção do sinal em {_p(h['acerto'])} das vezes em 30 min."]
        lv = a["niveis"]
        L += ["", "### Níveis técnicos", "",
              f"- Pivô {_n(lv['pivot'], dec)} · R1 {_n(lv['r1'], dec)} · R2 {_n(lv['r2'], dec)} · S1 {_n(lv['s1'], dec)} · S2 {_n(lv['s2'], dec)}",
              f"- Máx./mín. do pregão anterior: {_n(lv['max_ant'], dec)} / {_n(lv['min_ant'], dec)} · 20 pregões: {_n(lv['max20'], dec)} / {_n(lv['min20'], dec)}",
              f"- Médias: MM20 {_n(lv['mm20'], dec)} · MM50 {_n(lv['mm50'], dec)} · MM200 {_n(lv['mm200'], dec)} · RSI14 {_n(lv['rsi14'], 0)} · ATR14 {_n(lv['atr14'], dec)}"]
        it = a.get("intraday")
        if it:
            L.append(f"- Intraday (5 min): tendência {it['tendencia']} · VWAP {_n(it['vwap'], dec)} · EMA9 {_n(it['ema9'], dec)} · "
                     f"EMA21 {_n(it['ema21'], dec)} · RSI {_n(it['rsi'], 0)}")
        L.append("")
    L += ["## Mercados internacionais", "", "| Ativo | Último | Dia | 5 pregões | 21 pregões |", "| --- | ---: | ---: | ---: | ---: |"]
    for q in s["cotacoes"]:
        f5 = "—" if q["var5"] is None else (f"{q['var5']:+.0f} pb" if q["tipo"] == "yield" else _pct(q["var5"]))
        f21 = "—" if q["var21"] is None else (f"{q['var21']:+.0f} pb" if q["tipo"] == "yield" else _pct(q["var21"]))
        L.append(f"| {q['nome']} | {_n(q['ultimo'], 2)} | {_var(q)} | {f5} | {f21} |")
    if s.get("brasil_ny"):
        L += ["", "### Brasil negociado em Nova York", "", "| Em NY | Var. NY | Local | Var. local em US$ | Diferença |", "| --- | ---: | --- | ---: | ---: |"]
        for x in s["brasil_ny"]:
            L.append(f"| {x['ny']} | {_pct(x['var_ny'])} | {x['local']} | {_pct(x['local_em_usd'])} | {_pct(x['diferenca'])} |")
    b = s.get("b3")
    if b:
        L += ["", "## B3 — mercado à vista", "",
              f"- {b['total']} papéis acompanhados ({', '.join(f'{v} {k}' for k, v in b['por_tipo'].items())})",
              f"- Amplitude: {b['altas']} altas × {b['quedas']} quedas ({_p(b['pct_altas'])} subindo) · variação média ponderada {_pct(b['var_media_pond'])}",
              f"- Acima da MM50: {_p(b['acima_mm50'])} · acima da MM200: {_p(b['acima_mm200'])}",
              "- Maiores altas: " + ", ".join(f"{x['t']} {_pct(x['c'])}" for x in b["maiores_altas"][:5]),
              "- Maiores quedas: " + ", ".join(f"{x['t']} {_pct(x['c'])}" for x in b["maiores_quedas"][:5]),
              "- Setores: " + ", ".join(f"{x['setor']} {_pct(x['var'])}" for x in b["setores"][:4]) + " … "
              + ", ".join(f"{x['setor']} {_pct(x['var'])}" for x in b["setores"][-3:])]
    bc = s.get("bcb") or {}
    if bc:
        L += ["", "## Juros e câmbio oficiais (Banco Central)", ""]
        if "selic_meta" in bc:
            L.append(f"- Selic meta: {_n(bc['selic_meta']['valor'], 2)}% a.a.")
        if "ptax_venda" in bc:
            L.append(f"- PTAX venda: R$ {_n(bc['ptax_venda']['valor'], 4)} ({bc['ptax_venda']['data']})")
        if "ipca_12m" in bc:
            L.append(f"- IPCA 12 meses: {_n(bc['ipca_12m']['valor'], 2)}%")
    n = s.get("noticias")
    if n:
        for nome, w in (("Diário (últimas 36 h)", n["diario"]), ("Semanal (7 dias)", n["semanal"])):
            L += ["", f"## Notícias — {nome}", "",
                  f"Viés para a bolsa: **{w['leitura_ibov']}** (score {w['score_ibov']:+d}) · para o real: **{w['leitura_dolar']}** "
                  f"(score dólar {w['score_dolar']:+d}) · {w['classificadas']} manchetes classificadas.", ""]
            for t in w["temas"][:6]:
                seta = "▲ bolsa" if t["ibov"] > 0 else "▼ bolsa" if t["ibov"] < 0 else "■ bolsa"
                L.append(f"- **{t['tema']}** ({t['n']} manchetes, {seta}) — {t['deducao']}.")
                imp, hist = t.get("impacto"), t.get("historico")
                for g in (imp or {}).get("grupos", []):
                    if g["papeis"]:
                        L.append(f"  - {'Tende a ajudar' if g['lado'] == 'ganha' else 'Tende a pesar em'} {g['setor']}: "
                                 + ", ".join(f"{x['t']} {_pct(x['c'], 1)}" for x in g["papeis"]))
                if imp and imp.get("total"):
                    L.append(f"  - Hoje {imp['confirmam']} de {imp['total']} papéis andaram na direção esperada; efeito típico: {imp['prazo']}.")
                if hist:
                    lado = "alta" if hist["dia_pct_alta"] >= 0.5 else "queda"
                    L.append(f"  - Histórico: em {hist['n']} pregões em que {hist['evento']}, o mini índice fechou em {lado} em "
                             f"{max(hist['dia_pct_alta'], 1 - hist['dia_pct_alta']) * 100:.0f}% (média {_pct(hist['dia_media'])}); "
                             f"no seguinte subiu em {hist['seguinte_pct_alta'] * 100:.0f}%.")
                if t["manchetes"]:
                    L.append(f"  - “{t['manchetes'][0]['titulo']}” ({t['manchetes'][0]['fonte']})")
        an = n.get("analistas") or {}
        if an.get("itens"):
            L += ["", "## O que as casas de análise disseram (7 dias)", "", f"{an['positivas']} positivas × {an['negativas']} negativas.", ""]
            L += [f"- {'▲' if v['tom'] > 0 else '▼' if v['tom'] < 0 else '■'} **{v['casa']}**: {v['titulo']} ({v['fonte']})" for v in an["itens"][:10]]
    fo = s.get("focus") or {}
    if fo:
        L += ["", "## Boletim Focus — consenso dos economistas", "", "| Projeção | Ano | Mediana | Revisão em 4 semanas |", "| --- | --- | ---: | ---: |"]
        for v in fo.values():
            L += [f"| {v['nome']} | {i['ano']} | {_n(i['mediana'], 2)} | {'—' if i['revisao'] is None else f'{i['revisao']:+.2f}'.replace('.', ',')} |" for i in v["itens"]]
    ra = (s.get("b3") or {}).get("analistas")
    if ra:
        L += ["", "## Recomendações das casas para ações da B3", "",
              f"{ra['recomendacoes']} recomendações para {ra['cobertas']} ações: {_p(ra['pct_compra'])} compra · {_p(ra['pct_neutro'])} neutro · {_p(ra['pct_venda'])} venda.",
              "- Mais recomendadas: " + ", ".join(f"{x['t']} ({x['ab']}/{x['at']})" for x in ra["preferidas"][:6]),
              "- Menos recomendadas: " + ", ".join(f"{x['t']} ({x['ab']}/{x['at']})" for x in ra["evitadas"][:6])]
    if False:
        for _ in ():
            if True:
                if True:
                    pass
    pl = [x for x in s.get("placar", []) if x["n"]]
    if pl:
        L += ["", "## Placar ao vivo (previsões gravadas × resultado)", "", "| Ativo | Horizonte | Previsões | Acerto |", "| --- | --- | ---: | ---: |"]
        L += [f"| {x['ativo']} | {x['horizonte']} | {x['n']} | {_p(x['acerto'])} |" for x in pl]
    L += ["", "---", "", s["aviso_legal"], ""]
    return "\n".join(L)


def to_html(s: dict) -> str:
    """HTML simples e imprimível a partir do Markdown (títulos, listas, tabelas, negrito)."""
    out: list[str] = []
    in_table = in_list = False

    def inline(t: str) -> str:
        t = html.escape(t)
        parts = t.split("**")
        return "".join(p if i % 2 == 0 else f"<b>{p}</b>" for i, p in enumerate(parts))

    def close() -> None:
        nonlocal in_table, in_list
        if in_table:
            out.append("</table>")
            in_table = False
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in markdown(s).split("\n"):
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if in_list:
                close()
            tag = "td"
            if not in_table:
                out.append("<table>")
                in_table, tag = True, "th"
            out.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if line.lstrip().startswith("- "):
            if in_table:
                close()
            if not in_list:
                out.append("<ul>")
                in_list = True
            cls = ' class="sub"' if line.startswith("  ") else ""
            out.append(f"<li{cls}>{inline(line.lstrip()[2:])}</li>")
            continue
        close()
        if line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("> "):
            out.append(f"<p class='note'>{inline(line[2:])}</p>")
        elif line.strip() == "---":
            out.append("<hr>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    close()
    css = ("body{font:14px/1.5 system-ui,Segoe UI,Arial,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#16181d}"
           "h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid #d5d8de;padding-bottom:4px}h3{font-size:14px;margin-top:18px}"
           "table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}th,td{border:1px solid #d5d8de;padding:5px 8px;text-align:left}"
           "th{background:#f1f3f6}td:nth-child(n+3),th:nth-child(n+3){text-align:right}.note{color:#5b6270}li.sub{list-style:none;color:#5b6270;margin-left:18px}"
           ".bar{display:flex;gap:8px;margin:12px 0}button{font:inherit;padding:7px 14px;border:1px solid #9aa1ad;border-radius:6px;background:#fff;cursor:pointer}"
           "@media print{.bar{display:none}body{margin:0}}")
    return ("<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><title>Relatório WIN / WDO</title><style>" + css +
            "</style></head><body><div class='bar'><button onclick='print()'>Imprimir / salvar em PDF</button>"
            "<a href='/relatorio.md' download='relatorio_win_wdo.md'><button>Baixar Markdown</button></a>"
            "<a href='/'><button>Voltar ao painel</button></a></div>" + "\n".join(out) + "</body></html>")
