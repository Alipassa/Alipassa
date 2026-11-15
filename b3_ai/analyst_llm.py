"""Analista com IA (opcional): o Claude lê o estado completo do painel — estatística, confronto exterior × Brasil,
notícias, analistas, Focus, amplitude da B3 — e escreve a análise do mini índice e do mini dólar.

Opcional de propósito: precisa de `pip install anthropic` e da variável ANTHROPIC_API_KEY. Sem isso o painel segue
funcionando só com o motor estatístico. A análise é texto de apoio: não altera as probabilidades calculadas.
"""

from __future__ import annotations

import json
import os
from typing import Optional

MODEL = os.environ.get("B3AI_MODELO", "claude-opus-5-5")

SYSTEM = """Você é o analista-chefe de uma mesa de operações brasileira, especializado em mini índice (WIN) e mini dólar (WDO).
Recebe um JSON com dados reais do momento: probabilidades estatísticas validadas fora da amostra, confronto entre os
mercados internacionais e o Brasil, níveis técnicos, amplitude da B3, notícias classificadas, opinião de casas de
análise, boletim Focus e consenso de recomendações.

Escreva em português do Brasil uma análise para quem vai operar, nesta ordem e com estes títulos em Markdown:

## Cenário
O que está movendo o mercado agora, do exterior para o Brasil, em 3 a 5 frases.

## Notícias: o que importa e por quê
As 3 a 5 notícias que realmente pesam, cada uma com a cadeia de causa e efeito até o mini índice e os setores/ações
atingidos. Diga quando uma notícia ruim já parece estar no preço e quando ainda não está.

## Mini índice
Viés para o próximo pregão e para a semana, os níveis que confirmam e os que invalidam a leitura, e o que observar na abertura.

## Mini dólar
O mesmo, mais curto.

## Riscos
O que pode fazer esta leitura estar errada.

Regras: use somente os números do JSON, sem inventar preços, datas ou fatos. Quando a estatística disser que não há
vantagem comprovada num horizonte, diga isso com clareza em vez de forçar uma direção. Dê a sua opinião de forma
direta, mas com probabilidade honesta: direção de curto prazo é incerta e você não promete acerto. Não recomende
tamanho de posição nem alavancagem. Cite a casa ou a fonte ao mencionar a opinião de analistas."""


def available() -> Optional[str]:
    """None se dá para usar; senão, o motivo."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return "defina a variável ANTHROPIC_API_KEY para ativar o analista com IA"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return "instale o pacote: pip install anthropic"
    return None


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


def analyze(state: dict) -> dict:
    """Devolve {"ok", "texto" | "erro", "modelo"}. Nunca levanta exceção: o painel não depende desta chamada."""
    why = available()
    if why:
        return {"ok": False, "erro": why}
    import anthropic

    client = anthropic.Anthropic()
    payload = json.dumps(digest(state), ensure_ascii=False, default=lambda o: None)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",                      # se o modelo recusar, a API tenta o substituto na mesma chamada
            output_config={"effort": "high"},
            system=SYSTEM,
            messages=[{"role": "user", "content": "Dados do painel agora (JSON):\n\n" + payload + "\n\nEscreva a análise."}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "erro": "chave ANTHROPIC_API_KEY inválida"}
    except anthropic.RateLimitError:
        return {"ok": False, "erro": "limite de uso da API atingido — tente de novo em instantes"}
    except anthropic.APIStatusError as e:
        return {"ok": False, "erro": f"API respondeu {e.status_code}: {e.message}"}
    except anthropic.APIConnectionError:
        return {"ok": False, "erro": "sem conexão com a API da Anthropic"}
    if resp.stop_reason == "refusal":
        return {"ok": False, "erro": "o modelo recusou este pedido"}
    text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        return {"ok": False, "erro": "resposta vazia"}
    if resp.stop_reason == "max_tokens":
        text += "\n\n_(análise cortada pelo limite de tamanho)_"
    return {"ok": True, "texto": text, "modelo": resp.model}
