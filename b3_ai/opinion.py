"""Opinião do painel — gratuita, sem chave e sem serviço pago.

Duas saídas, as duas a partir dos dados que já estão na tela:

* `build(state)`  : interpretação escrita na hora pelo próprio painel (regras de leitura de mercado: tendência,
                    probabilidades, exterior, notícias, analistas, agenda, níveis). É o que o botão "Analisar agora" mostra.
* `prompt(state)` : o mesmo conjunto de dados, pronto para colar numa conversa com o Claude em claude.ai (usa a sua
                    assinatura, sem cobrança por uso). É o que o botão "Levar os dados para o Claude" copia.
"""

from __future__ import annotations

import json
from datetime import datetime

from .model import BRT

INSTRUCOES = """Você é um analista experiente de mini índice (WIN) e mini dólar (WDO) da B3. Abaixo está um JSON com os dados
reais do meu painel agora: probabilidades estatísticas validadas fora da amostra, confronto entre os mercados internacionais
e o Brasil, tendência, níveis técnicos, notícias classificadas, opinião de analistas, boletim Focus e agenda de eventos.

Analise e me dê a sua opinião, em português, com estes títulos: Cenário · Notícias que importam e por quê · Mini índice
(viés para o próximo pregão e para a semana, níveis que confirmam e que invalidam) · Mini dólar · Riscos · Conclusão.

Use somente os números do JSON, sem inventar preços ou fatos. Onde a estatística disser que não há vantagem comprovada,
diga isso com clareza. Seja direto, mas honesto sobre a incerteza."""


def _n(v, d: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def s_rt(s: dict) -> bool:
    return bool(s.get("tempo_real"))


def _row(a: dict, hz: str) -> dict:
    return next((r for r in a["prob"] if r["horizonte"] == hz), {})


def _trend(a: dict) -> dict:
    return {t["prazo"]: t["tendencia"] for t in a.get("tendencias") or []}


def _asset_view(a: dict, dec: int) -> tuple[str, list[str]]:
    """(veredito curto, parágrafos) de um ativo."""
    tr, lv = _trend(a), a["niveis"]
    d, w = _row(a, "Diário"), _row(a, "Semanal")
    dia, sem, mes = tr.get("Dia", "—"), tr.get("Semana", "—"), tr.get("Mês", "—")
    px = a["preco"]
    L = [f"Preço {_n(px, dec)} ({a['var']:+.2f}%). Tendência: dia **{dia}**, semana **{sem}**, mês **{mes}**."]
    if mes == "BAIXA" and sem == "ALTA":
        verdict = "repique dentro de tendência de baixa"
        L.append(f"Leitura: a alta recente é um **repique dentro de uma tendência maior de baixa**. Compras pedem cautela enquanto o preço "
                 f"estiver abaixo da média de 20 ({_n(lv['mm20'], dec)}); perder o suporte S1 ({_n(lv['s1'], dec)}) sugere que a baixa "
                 f"retomou, e só acima da média de 20 a leitura de fundo melhora.")
    elif mes == "ALTA" and sem == "BAIXA":
        verdict = "correção dentro de tendência de alta"
        L.append(f"Leitura: a queda recente é uma **correção dentro de uma tendência maior de alta**. A região entre S1 ({_n(lv['s1'], dec)}) "
                 f"e S2 ({_n(lv['s2'], dec)}) é onde a tendência costuma ser testada; fechar abaixo da média de 50 ({_n(lv['mm50'], dec)}) "
                 "enfraquece o quadro.")
    elif mes == "ALTA" and sem == "ALTA":
        verdict = "tendência de alta alinhada"
        L.append(f"Leitura: **semana e mês apontam para cima**. O lado da tendência é o comprador; recuos até o pivô ({_n(lv['pivot'], dec)}) "
                 f"ou S1 ({_n(lv['s1'], dec)}) são a região de interesse, e a leitura perde força abaixo de S2 ({_n(lv['s2'], dec)}).")
    elif mes == "BAIXA" and sem == "BAIXA":
        verdict = "tendência de baixa alinhada"
        L.append(f"Leitura: **semana e mês apontam para baixo**. O lado da tendência é o vendedor; repiques até o pivô ({_n(lv['pivot'], dec)}) "
                 f"ou R1 ({_n(lv['r1'], dec)}) tendem a encontrar venda, e a leitura perde força acima de R2 ({_n(lv['r2'], dec)}).")
    else:
        verdict = "sem tendência definida"
        L.append(f"Leitura: **sem tendência clara** entre semana e mês. O mais provável é mercado de faixa entre S1 ({_n(lv['s1'], dec)}) "
                 f"e R1 ({_n(lv['r1'], dec)}); rompimento de um dos lados é o que define o próximo movimento.")
    if d:
        conf = "validado fora da amostra" if d["validado"] else "sem vantagem estatística comprovada"
        if 0.45 < d["p_alta"] < 0.55:
            L.append(f"Estatística para o próximo pregão ({d['alvo']}): {d['p_alta'] * 100:.0f}% alta × {d['p_queda'] * 100:.0f}% queda — "
                     "**na prática um cara-ou-coroa**. Sem vantagem na direção, o que resta é operar níveis e reduzir o tamanho.")
        else:
            L.append(f"Estatística para o próximo pregão ({d['alvo']}): **{d['p_alta'] * 100:.0f}% alta × {d['p_queda'] * 100:.0f}% queda** "
                     f"({d['sinal']}, {conf}).")
        if d.get("f80_min") is not None:
            L.append(f"Faixa de 80% para o próximo pregão: **{_n(d['f80_min'], dec)} a {_n(d['f80_max'], dec)}** "
                     f"(o preço ficou dentro dela em {d['f80_cobertura'] * 100:.0f}% dos casos medidos) — útil para alvo e stop.")
    if w:
        L.append(f"Para a semana ({w['alvo']}): {w['p_alta'] * 100:.0f}% alta × {w['p_queda'] * 100:.0f}% queda ({w['sinal']}).")
    c = a["confronto"]
    L.append(c["leitura"] + (f" Depois do fechamento da B3 o exterior já implica {c['pos_fechamento_total']:+.2f}% para a abertura."
                              if c.get("pos_fechamento") and abs(c["pos_fechamento_total"]) >= 0.05 else ""))
    pads = a.get("padroes") or {}
    ativos = [x for x in pads.get("padroes", []) if x["ativo"]]
    for x in ativos[:3]:
        L.append(f"Padrão confirmado ativo: quando **{' + '.join(x['condicoes'])}**, o pregão {x['alvo_nome']} — "
                 f"**{x['direcao']} em {x['chance'] * 100:.0f}%** de {x['n']} casos fora da amostra.")
    if pads.get("testados") and not pads.get("confirmados"):
        L.append(f"Padrões: {pads['testados']} combinações de informações testadas e nenhuma se repetiu de forma confiável — "
                 "não há atalho estatístico nos cruzamentos diários.")
    rv = a.get("reversao")
    if rv and (rv["estado"] or rv["sinal"]):
        h = rv["historico"]["30 min"]
        L.append(f"Intraday: **{rv['rotulo']}** (RSI {rv['rsi']:.0f}). Esse sinal acertou {(h['acerto'] or 0) * 100:.0f}% em {h['n']} casos — "
                 + ("use como apoio, não como gatilho único." if rv.get("comprovado") else
                    "não se distingue do acaso: serve para descrever o momento, não para decidir a entrada."))
    L.append(f"Níveis: suportes {_n(lv['s1'], dec)} e {_n(lv['s2'], dec)} · pivô {_n(lv['pivot'], dec)} · resistências "
             f"{_n(lv['r1'], dec)} e {_n(lv['r2'], dec)} · médias de 20/200 em {_n(lv['mm20'], dec)}/{_n(lv['mm200'], dec)}.")
    return verdict, L


def build(s: dict) -> dict:
    """Opinião escrita a partir do estado do painel. Sempre funciona offline; nunca chama serviço externo."""
    if not s.get("ok") or "WIN" not in s.get("ativos", {}):
        return {"ok": False, "erro": "ainda sem dados de mercado"}
    win = s["ativos"]["WIN"]
    T: list[str] = []
    # ---------------- cenário
    T.append("## Cenário")
    sy, b, n = s.get("sintese"), s.get("b3") or {}, s.get("noticias")
    cen = []
    if sy:
        cen.append(f"O conjunto das frentes acompanhadas mostra **{sy['rotulo'].lower()}**: {sy['alta']} a favor da alta, "
                   f"{sy['queda']} a favor da queda e {sy['neutro']} neutras.")
    if b.get("pct_altas") is not None:
        frac = b["acima_mm200"] or 0
        saude = ("mercado saudável por dentro" if frac >= 0.6 else "base frágil: poucas ações em tendência de alta" if frac <= 0.35
                 else "saúde intermediária")
        cen.append(f"Na B3, {b['pct_altas'] * 100:.0f}% das ações subiram no último pregão e {frac * 100:.0f}% estão acima da média de "
                   f"200 dias — {saude}.")
    q = {x["key"]: x for x in s.get("cotacoes", [])}
    ext = [f"{q[k]['nome']} {q[k]['var']:+.2f}%" for k in ("ES", "DXY", "VIX", "BRENT") if k in q and q[k].get("var") is not None]
    if ext:
        cen.append("Exterior no último pregão: " + ", ".join(ext) + ".")
    T.append(" ".join(cen) or "Sem dados suficientes para o cenário.")
    # ---------------- notícias
    T.append("## Notícias que importam e por quê")
    if n and n["diario"]["temas"]:
        dn = n["diario"]
        T.append(f"Viés das últimas 36 horas: **{dn['leitura_ibov']}** para a bolsa (score {dn['score_ibov']:+d}).")
        for t in dn["temas"][:4]:
            line = f"- **{t['tema']}** ({t['n']} manchete{'s' if t['n'] > 1 else ''}): {t['deducao']}."
            imp, h = t.get("impacto"), t.get("historico")
            if imp and imp.get("total"):
                r = imp["confirmam"] / imp["total"]
                line += (" O mercado **está confirmando** essa leitura hoje." if r >= 0.6 else
                         " O mercado **não reagiu** como a notícia sugere — pode já estar no preço ou outro fator domina." if r <= 0.4
                         else " A reação das ações ligadas ao tema está mista.")
            if h:
                lado = "queda" if h["dia_pct_alta"] < 0.5 else "alta"
                line += (f" Quando isso se materializa de verdade ({h['evento']}), o mini índice fechou em {lado} em "
                         f"{max(h['dia_pct_alta'], 1 - h['dia_pct_alta']) * 100:.0f}% de {h['n']} pregões.")
            T.append(line)
        an = n.get("analistas") or {}
        if an.get("total"):
            T.append(f"- Casas de análise na semana: {an['positivas']} manchetes positivas contra {an['negativas']} negativas.")
    else:
        T.append("Nenhum tema de mercado dominante nas manchetes recentes.")
    f = s.get("focus") or {}
    if "IPCA" in f and "Selic" in f:
        i, se = f["IPCA"]["itens"][-1], f["Selic"]["itens"][-1]
        if i.get("revisao") is not None and se.get("revisao") is not None:
            piora = i["revisao"] > 0.03 or se["revisao"] > 0.01
            T.append(f"- Economistas (Focus): em 4 semanas a projeção de IPCA para {i['ano']} mudou {i['revisao']:+.2f} p.p. e a de Selic "
                     f"{se['revisao']:+.2f} p.p. — " + ("**piora**, que pesa em varejo, construção e na bolsa em geral." if piora
                                                         else "sem piora relevante."))
    # ---------------- ativos
    T.append("## Mini índice")
    v_win, lines = _asset_view(win, 0)
    T += lines
    v_wdo = None
    if "WDO" in s["ativos"]:
        T.append("## Mini dólar")
        v_wdo, lines = _asset_view(s["ativos"]["WDO"], 1)
        T += lines
    # ---------------- riscos
    T.append("## Riscos")
    risks = []
    for a in s.get("alertas") or []:
        risks.append(f"- **{a['nivel']}**: {a['titulo']}.")
    for e in (s.get("agenda") or {}).get("eventos", [])[:3]:
        if e["impacto"] == "muito alto" or e["dias"] <= 3:
            risks.append(f"- {e['quando'].capitalize()} ({e['dia_semana']}, {e['hora']}): **{e['evento']}** — antes dele a direção fica "
                         "indefinida e as faixas de preço tendem a ser estreitas demais.")
    h = (s.get("agenda") or {}).get("fomc_historico")
    if h:
        risks.append(f"- Em dias de decisão do Fed o mini índice oscila em média {h['abs_dia']:.2f}%, contra {h['abs_normal']:.2f}% num dia comum.")
    if not s_rt(s):
        risks.append("- Os dados públicos têm cerca de 15 minutos de atraso: no intraday, confirme sempre o preço na corretora.")
    T += risks
    # ---------------- conclusão
    T.append("## Conclusão")
    d = _row(win, "Diário")
    lado = (sy or {}).get("lado", 0)
    if (sy or {}).get("queda", 0) > (sy or {}).get("alta", 0):
        pend = "o pano de fundo pende para o lado vendedor"
    elif (sy or {}).get("alta", 0) > (sy or {}).get("queda", 0):
        pend = "o pano de fundo pende para o lado comprador"
    else:
        pend = "o pano de fundo está dividido"
    forte = "com convergência forte" if lado else "sem convergência forte"
    estat = ("e a estatística do próximo pregão não dá vantagem para nenhum lado" if d and 0.45 < d["p_alta"] < 0.55
             else f"e a estatística do próximo pregão aponta {d['sinal'].lower()}" if d else "")
    T.append(f"**Mini índice: {v_win}.** Hoje {pend}, {forte}, {estat}. "
             + ("Minha opinião: **postura defensiva** — operar menor, respeitar os níveis e não perseguir o movimento." if not lado
                else "Minha opinião: **operar a favor desse lado**, com stop no nível que invalida a leitura."))
    if v_wdo:
        T.append(f"**Mini dólar: {v_wdo}.**")
    T.append("_Opinião gerada pelas regras de leitura do painel a partir dos dados na tela. Apoio à decisão, não recomendação de investimento._")
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m %H:%M")
    return {"ok": True, "texto": "\n\n".join(T), "modelo": f"leitura do painel · dados de {when}"}


def digest(s: dict) -> dict:
    """Resumo compacto do estado para o modelo (sem candles nem a tabela completa da B3)."""
    out: dict = {"gerado": s.get("atualizado"), "pregao": s.get("pregao"), "mercado_aberto": s.get("mercado_aberto"),
                 "atraso_dos_dados": s.get("atraso"), "ativos": {}}
    for k, a in s.get("ativos", {}).items():
        c = a["confronto"]
        out["ativos"][k] = {
            "nome": a["nome"], "preco": a["preco"], "var_pct": a["var"], "max": a["max"], "min": a["min"],
            "vol_diaria_pct": a["sigma_dia_pct"], "niveis": a["niveis"], "intraday": a.get("intraday"), "tendencias": a.get("tendencias"),
            "extremos_e_reversao": {x: (a.get("reversao") or {}).get(x) for x in ("rotulo", "rsi", "vwap_z", "bb_pos", "historico")},
            "probabilidades": [{x: r.get(x) for x in ("horizonte", "alvo", "p_alta", "p_queda", "sinal", "validado", "oos_acerto",
                                                      "oos_n", "f80_min", "f80_max", "f80_cobertura", "motivos", "nota")} for r in a["prob"]],
            "confronto_exterior": {x: c[x] for x in ("implicito", "realizado", "descolamento", "descolamento_sigma", "r2", "leitura",
                                                     "pos_fechamento_total")},
            "fatores": [{x: f[x] for x in ("nome", "var", "tipo", "contrib")} for f in c["fatores"]],
        }
    out["cotacoes"] = [{x: q[x] for x in ("nome", "grupo", "tipo", "ultimo", "var", "var5", "var21")} for q in s.get("cotacoes", [])]
    out["brasil_em_ny"] = s.get("brasil_ny")
    b = s.get("b3") or {}
    out["b3"] = {x: b.get(x) for x in ("altas", "quedas", "pct_altas", "var_media_pond", "acima_mm50", "acima_mm200", "setores",
                                        "maiores_altas", "maiores_quedas", "analistas")}
    n = s.get("noticias") or {}
    out["noticias"] = {w: {"score_bolsa": n[w]["score_ibov"], "score_dolar": n[w]["score_dolar"],
                           "temas": [{x: t.get(x) for x in ("tema", "n", "deducao", "impacto", "historico", "manchetes")} for t in n[w]["temas"]]}
                       for w in ("diario", "semanal") if w in n}
    out["manchetes_nacionais"] = [x["titulo"] + " — " + x["fonte"] for x in n.get("nacionais", [])]
    out["manchetes_internacionais"] = [x["titulo"] + " — " + x["fonte"] for x in n.get("internacionais", [])]
    out["opiniao_de_analistas"] = [f"{x['casa']}: {x['titulo']} — {x['fonte']}" for x in (n.get("analistas") or {}).get("itens", [])]
    out["noticias_de_empresas"] = [f"{x['ticker']}: {x['titulo']}" for x in n.get("empresas", [])]
    out["agenda_de_eventos"] = s.get("agenda")
    out["alertas"] = s.get("alertas")
    out["focus"] = s.get("focus")
    out["banco_central"] = s.get("bcb")
    out["sintese_do_motor"] = s.get("sintese")
    return out


def prompt(state: dict) -> str:
    """Texto pronto para colar numa conversa com o Claude (claude.ai): instruções + dados do painel em JSON compacto."""
    data = json.dumps(digest(state), ensure_ascii=False, separators=(",", ":"), default=lambda o: None)
    return INSTRUCOES + "\n\nDADOS DO PAINEL (JSON):\n" + data
