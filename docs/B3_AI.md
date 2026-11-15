# B3 AI — mercado brasileiro × exterior, probabilidades do mini índice

Painel local (somente leitura, **nunca envia ordens**) em Python puro, sem dependências.

```bash
python -m b3_ai painel        # abre http://127.0.0.1:8766   (Windows: scripts\rodar_b3_ai.bat)
python -m b3_ai relatorio     # grava relatorios/relatorio_AAAAMMDD_HHMM.md e .html
python -m b3_ai analise       # quadro de probabilidades no terminal
```

![painel](b3_ai_painel.png)

## O que o painel mostra

| Bloco | Conteúdo |
| --- | --- |
| Destaque | número verde grande = probabilidade de ALTA; vermelho grande = probabilidade de QUEDA, para o ativo (WIN ou WDO) e o horizonte escolhidos |
| Quadro de probabilidades | 5 min · 10 min · 30 min · 1 hora · fecho de hoje (com pregão aberto) · diário · semanal, com sinal, validação e acerto fora da amostra |
| Confronto exterior × Brasil | quanto os mercados internacionais implicavam para o ativo, quanto ele fez, o descolamento e a contribuição de cada fator |
| Notícias e deduções | manchetes nacionais e internacionais (diário 36 h e semanal 7 dias), tema, direção para bolsa e dólar e a cadeia de dedução |
| Mercados internacionais | S&P, Nasdaq, DXY, Treasuries, VIX, Brent, minério, cobre, emergentes, China, Europa, ouro, bitcoin; Brasil em NY (EWZ, ADRs) |
| B3 — todas as empresas | ações, BDRs, FIIs e ETFs: amplitude, setores, maiores altas/quedas e tabela completa com busca, filtros e ordenação |
| Placar ao vivo | cada previsão é gravada antes do resultado e conferida depois contra o preço real |
| Relatório | botão "Gerar relatório" (HTML imprimível/PDF e Markdown) do mini índice e do mini dólar |

## De onde vêm os dados (todos reais, sem chave)

| Dado | Fonte |
| --- | --- |
| Todas as empresas da B3, cotações, setores, indicadores | TradingView scanner (`data/tradingview.py`) |
| Histórico diário e intraday de WIN, WDO e fatores internacionais | feed público da TradingView (`data/ws.py`, WebSocket em Python puro); reserva: Yahoo (só diário) |
| Selic, PTAX, IPCA | Banco Central — SGS (`data/bcb.py`) |
| Notícias | RSS do Google Notícias (buscas temáticas BR e exterior), InfoMoney e G1 (`data/news.py`) |

Cotações públicas da B3 e das bolsas americanas chegam com **~15 minutos de atraso**. As fontes da TradingView são
endpoints públicos não oficiais: podem mudar sem aviso; quando o histórico falha o motor cai para o Yahoo e avisa no painel.

## Como as probabilidades são calculadas (`model.py`)

1. **Confronto (fair value)** — regressão ridge móvel (250 pregões, ponto-a-ponto) do retorno diário do alvo sobre os
   fatores internacionais do mesmo dia. Dá o "exterior implícito", o descolamento e a contribuição de cada fator.
2. **Diário e semanal** — regressão logística sobre o estado no fechamento: exterior implícito, descolamento,
   momento de 5 pregões, RSI, distância da média de 20, nível do VIX, ativo cruzado (dólar ↔ bolsa) e regime de
   volatilidade. Com a B3 fechada, o que o exterior andou depois do fechamento desloca a probabilidade.
3. **Intraday (5, 10, 30 min, 1 h)** — regressão logística em barras de 5 e 15 min: momento do ativo, S&P futuro,
   dólar, VWAP, RSI, gap de abertura e hora do pregão.
4. **Validação e calibração** — todo modelo roda em walk-forward (treina no passado, prevê o trecho seguinte) e a
   probabilidade final é `base + k · (modelo − base)`, com `k` medido nas previsões fora da amostra. **Sem poder
   preditivo comprovado, k → 0 e o número volta para ~50 %** — o painel marca "não validado" em vez de inventar convicção.

Medido em 15/11/2026 com o histórico real: o modelo diário do WIN acertou a direção em ~54 % de 825 pregões fora da
amostra; os horizontes intraday ficaram em ~47–53 % (sem vantagem comprovada com dado público atrasado em 15 min) e por
isso aparecem perto de 50/50. As barras intraday são acumuladas em `dados/b3_ai.db` a cada ciclo, então a amostra desses
modelos cresce enquanto o painel roda.

As notícias são classificadas por regras (`news_ai.py`) e mostradas **ao lado** da probabilidade — não entram no cálculo,
porque não há histórico de manchetes para validá-las fora da amostra.

## Limites

Probabilidade não é garantia. Direção de curto prazo é quase um cara-ou-coroa; uma vantagem de poucos pontos
percentuais só aparece em muitas operações e some com custos mal controlados. Use o painel como apoio à decisão, junto
com gestão de risco. Não é recomendação de investimento.
