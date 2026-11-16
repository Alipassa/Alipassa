# B3 AI — mercado brasileiro × exterior, probabilidades do mini índice

Painel local (somente leitura, **nunca envia ordens**) em Python puro, sem dependências.

```bash
python -m b3_ai painel        # abre http://127.0.0.1:8766   (Windows: scripts\rodar_b3_ai.bat)
python -m b3_ai relatorio     # grava relatorios/relatorio_AAAAMMDD_HHMM.md e .html
python -m b3_ai analise       # quadro de probabilidades no terminal
python -m b3_ai auditoria     # testa as fontes e mede os acertos fora da amostra (exemplo: docs/b3_ai_auditoria_exemplo.md)
```

![painel](b3_ai_painel.png)

## O que o painel mostra

| Bloco | Conteúdo |
| --- | --- |
| Alertas | 🚨 ALERTA quando a chance de alta ou de queda passa de 90 % (⚠️ ATENÇÃO a partir de 75 %), 🔄 sinal de reversão, 📅 evento de alto impacto hoje/amanhã. Som, notificação do navegador e Telegram (opcional) |
| Destaque | número verde grande = probabilidade de ALTA; vermelho grande = probabilidade de QUEDA, para o ativo (WIN ou WDO) e o horizonte escolhidos; faixa de preço de 80 % |
| Tendência e extremos | tendência do dia, da semana e do mês; extremo de compra/venda em 5 min (RSI, bandas, VWAP) e sinal de reversão com o acerto medido |
| Síntese para decisão | quantas frentes independentes (estatística, exterior, notícias, analistas, Focus, amplitude, técnica) apontam para cada lado |
| Opinião sobre os dados | botão que analisa tudo o que está na tela e escreve a opinião na hora, sem custo; outro botão leva os dados prontos para o Claude |
| Quadro de probabilidades | abertura (com o pregão fechado) · 5 min · 10 min · 30 min · 1 hora · fecho de hoje (com pregão aberto) · diário · semanal, com sinal, validação e acerto fora da amostra |
| Confronto exterior × Brasil | quanto os mercados internacionais implicavam para o ativo, quanto ele fez, o descolamento e a contribuição de cada fator |
| Notícias e deduções | tema, cadeia de causa e efeito, setores e ações atingidos com a variação real do dia (o mercado confirmou a notícia?) e o efeito medido no histórico do mini índice |
| Agenda | decisões de juros dos EUA (calendário oficial do Fed) e do Copom (Banco Central), payroll e eventos do `dados/agenda.json`, com o efeito histórico dos dias de Fed |
| Analistas e economistas | boletim Focus com revisões, consenso de recomendações das casas para as ações da B3, manchetes de casas de análise e notícias por empresa × reação da ação |
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
| Consenso dos economistas | boletim Focus — Banco Central, API Olinda (`data/focus.py`) |
| Agenda | calendário de reuniões do Federal Reserve e próxima reunião do Copom (`data/calendar.py`) |

Os mercados internacionais usam séries que o feed entrega **sem atraso** (índices e commodities em contratos de 24 h,
moedas, juros, VIX); o atraso de cada série é lido do próprio feed e marcado com ⏱ no painel (hoje só o minério de ferro,
10 min). WIN, WDO e ações da B3 chegam com **~15 minutos de atraso** no feed público — use o MetaTrader 5 para tempo real. As fontes da TradingView são
endpoints públicos não oficiais: podem mudar sem aviso; quando o histórico falha o motor cai para o Yahoo e avisa no painel.

## Tempo real pelo MetaTrader 5 (opcional, Windows)

O dado público tem ~15 min de atraso. Com o MetaTrader 5 da corretora aberto e logado, WIN e WDO passam a vir em tempo
real (barras e último negócio), com mais histórico intraday para os modelos:

```bash
pip install MetaTrader5
python -m b3_ai --fonte mt5 painel      # ou scripts\rodar_b3_ai_mt5.bat
```

Ibovespa e DI futuro também passam a vir do MetaTrader quando existem no terminal. Qualquer outra série do painel pode
vir de lá se a corretora tiver o símbolo: defina `MT5_<ATIVO>` no `.env` (ex.: `MT5_IRON=<símbolo do minério>`,
`MT5_ES=WSP$N`). A biblioteca do MetaTrader para Python não dá acesso às notícias do terminal; as notícias seguem dos
feeds públicos.

`MT5_PATH` aponta o `terminal64.exe` (ex.: `C:\Program Files\Mt5xp\terminal64.exe`); `MT5_WIN`/`MT5_WDO` só são
necessários se o painel não achar o símbolo sozinho (tenta `WIN$N`, `WIN$`, `WIN@`... e depois o contrato com tick mais
recente). Somente leitura: nenhuma ordem é enviada. Mercados internacionais e a tabela de ações continuam no feed público.
Se o terminal estiver fechado, o painel avisa e segue com o dado público. O ProfitChart/XPTrader (Nelogica) não é
MetaTrader e não oferece essa interface.

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

## O que acerta 80 % ou mais — e o que não acerta

Medido em 16/11/2026 com o histórico do MetaTrader da XP (20 mil barras de 5 min, 1.248 pregões; `python -m b3_ai auditoria`).
O painel mostra sempre o número medido, nunca um número prometido.

| Previsão | Acerto medido | Observação |
| --- | --- | --- |
| Direção da ABERTURA com exterior forte durante a noite | mini índice 97 % (76 casos); mini dólar 92 % (72) | é o gap de abertura, não a direção do pregão |
| Direção da ABERTURA, todos os casos | mini índice 79 % (206); mini dólar 81 % (194) | estável na 2ª metade do histórico |
| Choque no exterior → pregão do mini índice | dólar disparando: queda em 93 % de 55; emergentes caindo: 84 %; VIX disparando: 81 % | relação no MESMO pregão; dispara os alertas |
| Faixa de preço de 80 % (todos os horizontes) | 79–84 % fora da amostra | serve para alvo e stop |
| Direção do próximo pregão — mini dólar | 57,0 % em 521 (61 % quando há convicção, 162 casos) | vantagem comprovada |
| Direção em 5 e 10 min — mini dólar | 52,5 % em 8.720 e 52,1 % em 8.954 | vantagem pequena, estatisticamente sólida |
| Direção do mini índice (5 min a semanal) | 44–50 % | **sem vantagem**: o painel mostra ~50/50 |
| Sinal de reversão em extremos | 50 % em 483 sinais (WIN); 48 % em 388 (WDO) | **não funciona** como gatilho; fica como descrição |

Uma medição anterior, com a série pública do mini índice (sem ajuste nas trocas de contrato), tinha dado 53,7 % para a direção
diária e 60 % para o sinal de reversão. Com o histórico maior e ajustado do MetaTrader, as duas vantagens desapareceram: eram
ruído. Por isso o alerta de reversão só dispara quando o sinal prova vantagem no histórico disponível.

Prever a DIREÇÃO do mini índice a partir de agora com 80 % de acerto não é alcançável de forma honesta. O que o painel faz é
separar o que é confiável (abertura, choques, faixas) do que é cara-ou-coroa, e avisar quando algo raro e forte acontece.

## Simulação com custos e estratégia de devolução de gap

`python -m b3_ai simulacao` transforma cada previsão em operações de 1 mini contrato, com 2 ticks de escorregamento e custo fixo.
Medido em 16/11/2026 com o histórico do MetaTrader da XP:

| Estratégia | Operações | Acerto | Ganho médio líquido | Conclusão |
| --- | ---: | ---: | ---: | --- |
| Mini índice — devolução de gap grande, sem stop (entra na abertura contra o gap, sai no fechamento) | 99 | 61,6 % | R$ 82,80 (R$ 9,56 a R$ 156,03) | ✅ lucro com evidência; pior queda R$ 1.510,80 |
| Mini índice — idem com stop de 0,5 desvio diário | 99 | 44,4 % | R$ 36,11 | não distinguível de zero |
| Mini dólar — diário com convicção | 162 | 59,3 % | R$ 45,28 (R$ −35,44 a R$ 126,01) | não distinguível de zero |
| Mini dólar — 5 min com convicção | 487 | 56,1 % | R$ −9,27 | ❌ prejuízo: o custo come a vantagem |
| Mini dólar — devolução de gap | 107 | 49,5 % | R$ −17,62 | não distinguível de zero |

Estudo de uso (`python -m b3_ai gap`, 99 operações em barras de 15 min do MetaTrader, 16/11/2026):

| O que muda na regra | Resultado |
| --- | --- |
| Momento da entrada | abertura R$ 82,80 · depois de 15 min R$ 87,50 · 30 min R$ 74,05 · 1 hora R$ 90,86 — todos com evidência: não depende do primeiro negócio |
| Saída | 1 hora R$ 0,28 · 2 horas R$ 36,91 · 4 horas R$ 87,05 · fechamento R$ 82,80 · ao devolver 50 % do gap R$ 30,97: é preciso segurar |
| Stop | sem stop R$ 82,80 · 0,5 desvio R$ 36,11 · 1 desvio R$ 60,41 · 1,5 desvio R$ 62,75: todo stop testado piorou |
| Tamanho do gap | sem padrão estável (na série pública o gap forte foi melhor; no MetaTrader, o moderado): operar todos os ≥ 0,75 desvio |
| Mini dólar | nenhuma variação funcionou |

Pior momento contra a posição: até R$ 380 em 90 % das operações ganhadoras; pior perda histórica R$ 865 por contrato.

Filtros (escolhidos na 1ª metade do histórico e conferidos na 2ª): dois contextos melhoraram o resultado nas duas metades, no
MetaTrader e na série pública de 2 anos — **abrir FORA da faixa do pregão anterior** (R$ 110,48 → R$ 201,91, 88 % de acerto na 2ª
metade) e **o pregão anterior ter fechado na mesma direção do gap** (R$ 135,42 → R$ 219,19, 88 %). Na série pública: os dois juntos
R$ 214,14 em 39 operações (79 %), só um R$ 43,94 (68 %), nenhum R$ 9,07 (43 %). Regra do sinal no painel: 🎯 só com os dois
contextos ("sinal forte"); com um, "sinal fraco" sem alerta; com nenhum, "melhor ficar de fora". Tamanho do gap, exterior,
confirmação nos primeiros 15 minutos e dia da semana não se sustentaram.

A devolução de gap do mini índice é a única com evidência de lucro; o painel mostra o gap do dia, dispara o alerta 🎯 ESTRATÉGIA
quando ele é grande e registra cada ocorrência como operação de papel no placar ao vivo. Ressalvas: ~14 meses de histórico,
várias estratégias testadas (uma pode passar por sorte), abertura com escorregamento maior que o simulado e, sem stop, perda
grande num dia de tendência. `python -m b3_ai extremos` testa os extremos de compra e venda com filtros (dia lateral, fora da
faixa, S2/R2), escolhendo o parâmetro na 1ª metade do histórico e medindo na 2ª.

## Descoberta de padrões (`patterns.py`)

O painel cruza as informações duas a duas — exterior, descolamento Brasil × exterior, momento, RSI, distância da média, VIX,
dólar/bolsa, volatilidade, dia forte, sequências, dia da semana, gap de abertura, primeira hora — e procura combinações que
mudem a chance de o pregão fechar em alta. Para não confundir coincidência com padrão: a combinação precisa aparecer nos
primeiros 60 % do histórico (≥ 40 casos, ≥ 7 pontos acima ou abaixo da taxa-base) e se repetir nos 40 % finais, que a busca não
viu. Só os confirmados aparecem, com a chance medida na confirmação, marcados como "ativo agora" quando as condições valem. O
bloco informa quantas combinações foram testadas e quantas passariam por puro acaso.

Com os dados públicos (16/11/2026): 239 combinações testadas, nenhuma confirmada. Refaça com `--fonte mt5` para usar o
histórico do MetaTrader, que também habilita os padrões de dentro do pregão (gap × primeira hora → resto do dia).

## Opinião sobre os dados (sem custo)

Dois botões no painel, os dois usando só o que já está na tela:

- **Analisar agora** — o painel lê probabilidades, tendência, exterior, notícias, analistas, agenda e níveis e escreve a
  opinião na hora (`opinion.py`, regras de leitura de mercado; local, sem chave e sem serviço pago). No terminal:
  `python -m b3_ai opiniao`.
- **Levar os dados para o Claude** — copia os mesmos dados, com as instruções, e abre o claude.ai; basta colar na conversa
  para o Claude dar a opinião dele usando a sua conta. Nada é enviado automaticamente.

## Limites

Probabilidade não é garantia. Direção de curto prazo é quase um cara-ou-coroa; uma vantagem de poucos pontos
percentuais só aparece em muitas operações e some com custos mal controlados. Use o painel como apoio à decisão, junto
com gestão de risco. Não é recomendação de investimento.
