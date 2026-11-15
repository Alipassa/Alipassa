"""Leitura das notícias: identifica o tema de cada manchete, a direção esperada para Ibovespa e dólar e a
cadeia de dedução ("Fed duro → juros dos EUA ↑ → dólar forte → pressão no Ibovespa").

É um classificador por regras (palavras-chave em português e inglês), transparente e auditável: cada tema mostra
as manchetes que o acionaram. O viés das notícias é exibido AO LADO da probabilidade estatística, sem se misturar
a ela — o histórico de manchetes não permite validá-lo fora da amostra.
"""

from __future__ import annotations

import math
import re
import time
from typing import Optional

UP = r"(sobe|sobem|subi|alta|avan[çc]a|dispara|salta|recorde|ganha|rally|rall?ies|rises?|jumps?|surges?|soars?|gains?|climbs?|higher|rebounds?|recupera)"
DOWN = r"(cai|caem|queda|recua|despenca|desaba|tomba|derrete|perde|baixa|falls?|drops?|slumps?|plunges?|tumbles?|sinks?|lower|slides?|selloff|sell-off)"

# tema: (escopo, regex do assunto, regex da polaridade | None, efeito Ibov, efeito dólar, título, dedução)
THEMES: list[tuple] = [
    ("fed_duro", "INT", r"\b(fed|fomc|powell|federal reserve)\b",
     r"(hike|raise|raising|hawkish|higher for longer|tighten|eleva|alta d[eo]s? juros|aperto|sobe juros|aumenta|subir os juros)",
     -1.0, +1.0, "Fed mais duro (juros dos EUA para cima)",
     "Fed duro → juros americanos sobem → dólar global se fortalece → capital sai de emergentes → pressão sobre o Ibovespa e alta do dólar no Brasil"),
    ("fed_brando", "INT", r"\b(fed|fomc|powell|federal reserve)\b",
     r"(cut|cuts|cutting|dovish|easing|pause|corte|cortar|reduz|afroux|queda d[eo]s? juros)",
     +1.0, -1.0, "Fed mais brando (corte ou pausa nos juros)",
     "Fed brando → juros americanos caem → dólar global perde força → fluxo volta para emergentes → favorece o Ibovespa e alivia o dólar"),
    ("inflacao_eua_alta", "INT", r"(us inflation|cpi|pce|inflação (nos|dos) eua|inflação americana)",
     r"(hot|hotter|above|accelerat|rises?|jumps?|surges?|acima|acelera|sobe|surpreende para cima)",
     -0.8, +0.8, "Inflação dos EUA acima do esperado",
     "inflação americana alta → Fed adia cortes ou sobe juros → Treasuries sobem → dólar forte → negativo para a bolsa brasileira"),
    ("inflacao_eua_baixa", "INT", r"(us inflation|cpi|pce|inflação (nos|dos) eua|inflação americana)",
     r"(cools?|cooling|below|slows?|eases?|falls?|abaixo|desacelera|recua|arrefece)",
     +0.8, -0.8, "Inflação dos EUA cedendo",
     "inflação americana cedendo → espaço para o Fed cortar → juros e dólar caem → positivo para emergentes e para o Ibovespa"),
    ("risco_global", "INT", r"(\bwar\b|guerra|airstrike|missile|m[íi]ssil|sanctions|san[çc][õo]es|tariffs?\b|tarifa[çc]o|trade war|recession|recess[ãa]o|debt default|shutdown|geopolit|invas[ãa]o|invasion|bank run|financial crisis|crise financeira)",
     None, -0.8, +0.8, "Aversão a risco (geopolítica, tarifas, crise)",
     "tensão global → investidores buscam proteção (dólar, Treasuries, ouro) → vendem bolsas e moedas emergentes → Ibovespa pressionado, dólar em alta"),
    ("alivio_global", "INT", r"(ceasefire|cessar-fogo|tr[ée]gua|truce|peace deal|acordo de paz|trade deal|acordo comercial|tariff (cut|relief|pause)|record high|all-time high)",
     None, +0.7, -0.6, "Alívio de risco / otimismo global",
     "distensão ou otimismo → apetite a risco volta → fluxo para bolsas e emergentes → favorece o Ibovespa e pesa sobre o dólar"),
    ("wall_street_alta", "INT", r"(wall street|s&p 500|nasdaq|dow jones|stocks|bolsas (de ny|americanas)|stock market)", UP,
     +0.6, -0.4, "Bolsas americanas em alta",
     "Wall Street em alta → apetite a risco global → tende a puxar o Ibovespa na mesma direção"),
    ("wall_street_queda", "INT", r"(wall street|s&p 500|nasdaq|dow jones|stocks|bolsas (de ny|americanas)|stock market)", DOWN,
     -0.6, +0.4, "Bolsas americanas em queda",
     "Wall Street em queda → aversão a risco → emergentes sofrem junto → pressão sobre o Ibovespa"),
    ("china_estimulo", "INT", r"\b(china|chin[êe]s|chinese|beijing|pequim|pboc)\b",
     r"(stimulus|est[íi]mulo|support|rate cut|corte|beats?|rebound|recovery|recupera|acelera|expands?|growth (beats|accelerat))",
     +0.8, -0.5, "China: estímulo ou atividade mais forte",
     "China mais forte → mais demanda por minério, petróleo e soja → Vale, Petrobras e exportadoras sobem → positivo para o Ibovespa e para o real"),
    ("china_fraca", "INT", r"\b(china|chin[êe]s|chinese|beijing|pequim)\b",
     r"(slowdown|slows?|weak|weaker|contracts?|deflation|property crisis|misses|desacelera|fraco|fraca|crise|defla[çc][ãa]o|contra[çc][ãa]o)",
     -0.8, +0.5, "China: atividade fraca",
     "China fraca → menos demanda por commodities → minério e petróleo caem → Vale e siderúrgicas pesam no Ibovespa; real perde suporte"),
    ("petroleo_alta", "INT", r"(oil|crude|brent|wti|opec|opep|petr[óo]leo)", UP,
     +0.4, -0.2, "Petróleo em alta",
     "petróleo em alta → Petrobras sobe e ajuda o Ibovespa; em excesso, pressiona a inflação global e os juros"),
    ("petroleo_queda", "INT", r"(oil|crude|brent|wti|opec|opep|petr[óo]leo)", DOWN,
     -0.4, +0.2, "Petróleo em queda",
     "petróleo em queda → Petrobras pesa no índice; por outro lado, alivia a inflação"),
    ("minerio_alta", "INT", r"(iron ore|min[ée]rio)", UP, +0.5, -0.3, "Minério de ferro em alta",
     "minério em alta → Vale e siderúrgicas sobem → sustenta o Ibovespa e a balança comercial"),
    ("minerio_queda", "INT", r"(iron ore|min[ée]rio)", DOWN, -0.5, +0.3, "Minério de ferro em queda",
     "minério em queda → Vale e siderúrgicas caem → peso negativo relevante no Ibovespa"),
    ("emprego_eua_forte", "INT", r"(jobs report|payrolls?|nonfarm|unemployment|jobless|emprego nos eua)",
     r"(strong|beats?|surges?|jumps?|hot|above|forte|acima)", -0.4, +0.5, "Emprego forte nos EUA",
     "mercado de trabalho americano forte → Fed sem pressa de cortar → juros e dólar firmes → vento contrário para emergentes"),
    ("emprego_eua_fraco", "INT", r"(jobs report|payrolls?|nonfarm|unemployment|jobless|emprego nos eua)",
     r"(weak|misses|slows?|cools?|below|rises to|fraco|abaixo)", +0.3, -0.5, "Emprego fraco nos EUA",
     "emprego americano fraco → Fed mais perto de cortar → dólar perde força; se vier com medo de recessão, o efeito na bolsa pode inverter"),
    # ---------------- Brasil
    ("copom_alta", "BR", r"(copom|selic|banco central|\bbc\b|galípolo|galipolo)",
     r"(eleva|elevar|alta|sobe|subir|aumenta|aperto|mais alt[ao]|hawkish|dur[oa])", -0.5, -0.4, "Copom/Selic para cima",
     "Selic mais alta → renda fixa compete com a bolsa (negativo para ações) → mas o diferencial de juros atrai dólar (real mais forte)"),
    ("copom_corte", "BR", r"(copom|selic|banco central|\bbc\b|galípolo|galipolo)",
     r"(corta|cortar|corte|reduz|reduzir|queda|baixa|afroux|dovish)", +0.6, +0.3, "Copom/Selic para baixo",
     "Selic em queda → custo de capital menor e migração da renda fixa para ações (positivo para o Ibovespa) → carry menor tende a enfraquecer o real"),
    ("fiscal_ruim", "BR", r"(fiscal|arcabou[çc]o|d[ée]ficit|gastos?|d[íi]vida p[úu]blica|rombo|contas p[úu]blicas|or[çc]amento|precat[óo]rio)",
     r"(risco|piora|rombo|aumenta|estoura|preocupa|descumpr|fura|press[ãa]o|alerta|cr[íi]tica|incerteza|maior|recorde|eleva)",
     -0.9, +0.9, "Risco fiscal no Brasil",
     "piora fiscal → prêmio de risco sobe → juros futuros (DI) abrem → ações descontadas a taxas maiores e dólar mais caro"),
    ("fiscal_bom", "BR", r"(super[áa]vit|ajuste fiscal|corte de gastos|contingenciamento|bloqueio de gastos|reforma administrativa aprovada|arrecada[çc][ãa]o recorde|meta fiscal cumprida)",
     None, +0.8, -0.8, "Melhora fiscal / reformas",
     "sinal de ajuste das contas → prêmio de risco cai → juros futuros fecham → bolsa sobe e dólar recua"),
    ("ipca_alto", "BR", r"(ipca|igp-m|infla[çc][ãa]o)", r"(acima|acelera|sobe|avan[çc]a|surpreende|press[ãa]o|estoura|maior)",
     -0.5, +0.2, "Inflação brasileira pressionada",
     "inflação acima do esperado → Copom mais duro por mais tempo → juros futuros sobem → negativo para ações de consumo, construção e varejo"),
    ("ipca_baixo", "BR", r"(ipca|igp-m|infla[çc][ãa]o)", r"(abaixo|desacelera|recua|cai|menor|defla[çc][ãa]o|al[íi]vio)",
     +0.5, -0.1, "Inflação brasileira cedendo",
     "inflação cedendo → espaço para corte da Selic → juros futuros caem → positivo para a bolsa, sobretudo setores domésticos"),
    ("fluxo_entrada", "BR", r"(estrangeiros?|fluxo|investidor(es)? estrangeiro)", r"(entrada|ingress|compra|aporta|positivo|recorde de entrada)",
     +0.7, -0.6, "Entrada de capital estrangeiro",
     "estrangeiro comprando → fluxo sustenta as blue chips e traz dólares → Ibovespa sobe e câmbio cai"),
    ("fluxo_saida", "BR", r"(estrangeiros?|fluxo|investidor(es)? estrangeiro)", r"(sa[íi]da|retira|venda|vende|negativo|foge|fuga)",
     -0.7, +0.6, "Saída de capital estrangeiro",
     "estrangeiro vendendo → pressão nas blue chips e remessa de dólares → Ibovespa cai e câmbio sobe"),
    ("atividade_forte", "BR", r"(pib|varejo|servi[çc]os|produ[çc][ãa]o industrial|caged|ibc-br)", r"(acima|cresce|avan[çc]a|sobe|surpreende|forte|recorde)",
     +0.3, -0.2, "Atividade brasileira forte",
     "atividade forte → lucros maiores (positivo), mas economia aquecida dificulta o corte de juros"),
    ("atividade_fraca", "BR", r"(pib|varejo|servi[çc]os|produ[çc][ãa]o industrial|caged|ibc-br)", r"(abaixo|cai|recua|queda|fraco|fraca|desacelera|encolhe)",
     -0.3, +0.2, "Atividade brasileira fraca",
     "atividade fraca → lucros menores no curto prazo; por outro lado, abre espaço para juros mais baixos"),
    ("politica_ruido", "BR", r"(impeachment|cpi\b|crise pol[íi]tica|interven[çc][ãa]o|ru[íi]do pol[íi]tico|taxa[çc][ãa]o|imposto sobre|tributa[çc][ãa]o de dividendos|iof)",
     None, -0.5, +0.5, "Ruído político/tributário",
     "incerteza política ou tributária → investidor exige mais prêmio → bolsa recua e dólar sobe"),
    # ---------------- o que o mercado local já fez (contexto, peso menor)
    ("ibov_alta", "BR", r"(ibovespa|bolsa brasileira|\bb3\b)", UP, +0.35, 0.0, "Ibovespa vem de alta (manchetes)",
     "o mercado local já vinha comprador — momento positivo, mas parte do movimento pode estar no preço"),
    ("ibov_queda", "BR", r"(ibovespa|bolsa brasileira|\bb3\b)", DOWN, -0.35, 0.0, "Ibovespa vem de queda (manchetes)",
     "o mercado local já vinha vendedor — momento negativo, com risco de repique técnico"),
    ("dolar_alta", "BR", r"(d[óo]lar)", UP, -0.2, +0.35, "Dólar vem de alta (manchetes)",
     "dólar pressionado — sinal de aversão a risco local ou global"),
    ("dolar_queda", "BR", r"(d[óo]lar)", DOWN, +0.2, -0.35, "Dólar vem de queda (manchetes)",
     "dólar em queda — sinal de entrada de fluxo ou alívio de risco"),
]
_COMPILED = [(k, sc, re.compile(subj, re.I), re.compile(pol, re.I) if pol else None, ei, ed, title, ded)
             for k, sc, subj, pol, ei, ed, title, ded in THEMES]


def classify(title: str) -> list[tuple]:
    out = []
    for k, sc, subj, pol, ei, ed, ttl, ded in _COMPILED:
        if subj.search(title) and (pol is None or pol.search(title)):
            out.append((k, sc, ei, ed, ttl, ded))
    # polaridades opostas do mesmo assunto na mesma manchete se anulam (ex.: "sobe após cair")
    keys = {o[0] for o in out}
    for a, b in (("fed_duro", "fed_brando"), ("petroleo_alta", "petroleo_queda"), ("ibov_alta", "ibov_queda"),
                 ("dolar_alta", "dolar_queda"), ("wall_street_alta", "wall_street_queda"), ("copom_alta", "copom_corte"),
                 ("china_estimulo", "china_fraca"), ("minerio_alta", "minerio_queda"), ("ipca_alto", "ipca_baixo"),
                 ("inflacao_eua_alta", "inflacao_eua_baixa"), ("fluxo_entrada", "fluxo_saida")):
        if a in keys and b in keys:
            out = [o for o in out if o[0] not in (a, b)]
    return out


RELEVANT = re.compile(r"(ibovespa|bolsa|d[óo]lar|selic|copom|juros|infla[çc][ãa]o|ipca|\bpib\b|fiscal|petrobras|\bvale\b|\bb3\b|mercado|a[çc][õo]es|"
                      r"c[âa]mbio|banco central|economia|tesouro|d[íi]vida|\bfed\b|stocks?|markets?|\boil\b|china|treasur|inflation|"
                      r"rates?\b|tariff|wall street|s&p|nasdaq|iron ore|dollar|economy|opec)", re.I)


def _window(items: list[dict], now: float, max_age_h: float, half_life_h: float) -> dict:
    themes: dict[str, dict] = {}
    si = sd = 0.0
    used = 0
    for it in items:
        age_h = (now - it["ts"]) / 3600
        if age_h > max_age_h or age_h < -1:
            continue
        cls = it.get("_cls")
        if cls is None:
            cls = it["_cls"] = classify(it["title"])
        if not cls:
            continue
        used += 1
        w = 0.5 ** (max(age_h, 0) / half_life_h)
        for k, sc, ei, ed, ttl, ded in cls:
            t = themes.setdefault(k, {"tema": ttl, "escopo": sc, "n": 0, "peso": 0.0, "ibov": ei, "dolar": ed,
                                      "deducao": ded, "manchetes": []})
            t["n"] += 1
            t["peso"] += w
            if len(t["manchetes"]) < 3:
                t["manchetes"].append({"titulo": it["title"], "fonte": it["source"], "link": it["link"], "ts": it["ts"]})
            si += w * ei
            sd += w * ed
    norm = 4.0 + 0.15 * used   # satura devagar: muitas manchetes repetindo o mesmo fato não viram certeza
    ranked = sorted(themes.values(), key=lambda t: -t["peso"] * max(abs(t["ibov"]), abs(t["dolar"])))
    for t in ranked:
        t["peso"] = round(t["peso"], 2)
    return {"score_ibov": round(100 * math.tanh(si / norm)), "score_dolar": round(100 * math.tanh(sd / norm)),
            "classificadas": used, "temas": ranked[:8]}


def label(score: float) -> str:
    if score >= 35:
        return "favorável"
    if score <= -35:
        return "desfavorável"
    if score >= 12:
        return "levemente favorável"
    if score <= -12:
        return "levemente desfavorável"
    return "neutro"


def analyze(items: list[dict], now: Optional[float] = None) -> dict:
    now = now or time.time()
    daily = _window(items, now, 36, 10)
    weekly = _window(items, now, 7 * 24, 60)

    def top(scope: str, n: int = 14) -> list[dict]:
        out = []
        for it in items:
            if it["scope"] != scope:
                continue
            cls = it.get("_cls") or []
            if not cls and not RELEVANT.search(it["title"]):
                continue   # feeds gerais trazem loteria, esporte, curiosidades: fora das listas
            ei = sum(c[2] for c in cls)
            out.append({"titulo": it["title"], "fonte": it["source"], "link": it["link"], "ts": it["ts"],
                        "temas": [c[4] for c in cls][:2], "ibov": 1 if ei > 0.1 else -1 if ei < -0.1 else 0})
            if len(out) >= n:
                break
        return out

    for w in (daily, weekly):
        w["leitura_ibov"] = label(w["score_ibov"])
        w["leitura_dolar"] = label(-w["score_dolar"])   # dólar em alta é "desfavorável" para o real
    return {"diario": daily, "semanal": weekly, "nacionais": top("BR"), "internacionais": top("INT"),
            "total": len(items), "atualizado": now}
