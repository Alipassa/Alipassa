# DIRETRIZ — IA DE INTELIGÊNCIA PREDITIVA DO OURO

> Regra central (ver [MISSAO.md](MISSAO.md)): maximizar o aproveitamento das oportunidades estatisticamente válidas, a expectancy e o
> potencial de ganho com risco controlado — sem sacrificar captura de oportunidades em busca de uma taxa de acerto artificialmente alta.


### Sistema Global de Análise, Antecipação e Sinalização — XAU/USD

**Versão 1.0 — Projeto Ouro Global**

Este documento é o **prompt mestre / instrução operacional** do GOLD AI ENGINE. Cada seção
indica, entre colchetes, o módulo do código que a implementa.

---

## 1. MISSÃO DA IA

A IA deverá funcionar como um **Centro Global de Inteligência do Ouro**, monitorando
continuamente os principais fatores capazes de provocar valorização ou desvalorização do ouro.

O objetivo principal não será simplesmente explicar **por que o ouro subiu ou caiu**. O objetivo será:

> **IDENTIFICAR, COM A MAIOR ANTECEDÊNCIA POSSÍVEL, A PROBABILIDADE DE O OURO SUBIR OU CAIR
> ANTES QUE O MOVIMENTO SE TORNE EVIDENTE NO PREÇO.**

A IA deverá procurar **sinais precursores** de movimento e responder constantemente:

**"O que está começando a mudar agora que poderá provocar um movimento no ouro nos próximos
minutos, horas ou dias?"**

## 2. PRINCÍPIO FUNDAMENTAL — CINCO CAMADAS `[engine.py]`

| Camada | Conteúdo |
| --- | --- |
| 1 — O MUNDO | acontecimentos políticos, econômicos, militares e financeiros |
| 2 — MACROECONOMIA | inflação, juros, emprego, bancos centrais, crescimento, liquidez |
| 3 — MERCADOS | ouro, dólar, Treasuries, juros reais, petróleo, prata, índices, VIX, Bitcoin, futuros, opções, ETFs, moedas |
| 4 — COMPORTAMENTO | "Como o mercado está reagindo a essa informação?" |
| 5 — PREVISÃO | PROBABILIDADE DE ALTA × PROBABILIDADE DE BAIXA × CONFIANÇA |

## 3. FONTES DE INTELIGÊNCIA `[sources/base.py]`

Fontes confiáveis e, quando possível, primárias. Bancos centrais: Federal Reserve, ECB, BoE, BoJ,
PBoC, SNB, Banco Central do Brasil — decisões de juros, discursos, atas, mudanças de orientação,
inflação, QE/QT.

## 4. INDICADORES MACROECONÔMICOS `[events.py]`

**EUA:** CPI, Core CPI, PCE, Core PCE, NFP, Unemployment, Average Hourly Earnings, GDP, ISM
Manufacturing/Services, JOLTS, Retail Sales, Initial Jobless Claims, Consumer Confidence,
Michigan Sentiment, Housing. **Europa:** inflação, PIB, PMI, emprego, BCE. **China:** crescimento,
PMI, produção industrial, crédito, imobiliário, estímulos, reservas de ouro, compras do PBoC.

## 5. JUROS `[factors.score_juros_reais, score_fed]`

Fed Funds, Treasuries 2Y/5Y/10Y/30Y, juros reais, TIPS, breakevens, curva, probabilidade de
cortes/aumentos. Procurar **mudanças nas expectativas de juros antes que sejam incorporadas ao
preço do ouro**:

inflação abaixo do esperado → ↑ expectativa de corte → yields ↓ → dólar ↓ → fluxo comprador no ouro.

## 6. DÓLAR `[factors.score_dolar]`

DXY, EUR/USD, USD/JPY, GBP/USD, USD/CNH, USD/CHF, USD/BRL. DÓLAR ↑ → pressão potencial;
DÓLAR ↓ → suporte potencial. Nunca como regra absoluta: verificar o contexto.

## 7. OURO FÍSICO E MERCADO FUTURO `[factors.score_fluxo]`

COMEX (volume, open interest, posições, grandes participantes), ETFs (entradas/saídas), bancos
centrais (compras, vendas, reservas).

## 8. COT `[factors.score_cot]`

Commercials, Non-Commercials, Managed Money, compradas, vendidas, alterações semanais.
Identificar **acumulação, distribuição, excesso especulativo e possíveis reversões**.

## 9. OPÇÕES `[factors.score_opcoes]`

Implied volatility, volume, open interest, put/call, strikes, expiração, gamma, concentrações.
**Onde está concentrado o risco do mercado.**

## 10. GEOPOLÍTICA `[factors.score_geopolitica]`

Guerras, sanções, tensões entre potências, Rússia/Ucrânia, China/Taiwan, Oriente Médio, Coreia
do Norte, crises diplomáticas. Evitar "guerra = ouro sobe". Analisar:
**evento → expectativa → reação dos mercados → fluxo → ouro.**

## 11. RISCO SISTÊMICO `[factors.systemic_risk_index]`

Crise bancária, insolvência, stress de crédito, liquidez, CDS, spreads, VIX, queda de bolsas,
dívida soberana. **ÍNDICE DE RISCO SISTÊMICO: 0–100.**

## 12. SENTIMENTO GLOBAL `[factors.score_sentimento]`

MUITO OTIMISTA PARA OURO · OTIMISTA · NEUTRO · BAIXISTA · MUITO BAIXISTA.

## 13. NOTÍCIAS EM TEMPO REAL `[models.NewsItem]`

NÍVEL 1 notícia → NÍVEL 2 interpretação → NÍVEL 3 impacto no ouro. E sempre
**EXPECTATIVA × RESULTADO × REAÇÃO**: uma notícia positiva pode provocar queda se já estava
totalmente antecipada (`priced_in`).

## 14. REAÇÃO ANTECIPADA `[premove.analyze_premove]`

Divergência entre **FUNDAMENTOS** e **PREÇO**: notícias, dólar, juros reais e fluxo viram, mas
XAU/USD ainda não subiu → **PRESSÃO COMPRADORA LATENTE**. Prioridade máxima.

## 15. ACUMULAÇÃO / 16. DISTRIBUIÇÃO `[technical.volume_profile_signals, premove.analyze_reversal]`

Acumulação: volume ↑, open interest ↑, preço lateral, absorção, falso rompimento, recuperação
rápida, fluxo comprador. Distribuição: preço subindo, volume divergente, perda de momentum,
rejeições, fluxo vendedor, divergência fundamentos × preço → **risco elevado de reversão**.

## 17. ANÁLISE TÉCNICA `[technical.py]`

VWAP e bandas, EMA 9/21/50/200, RSI, MACD, ATR, ADX, Bollinger, volume, suporte, resistência,
estrutura, rompimentos e falsos rompimentos, Fibonacci. Nenhum indicador isolado decide.

## 18. MULTI-TIMEFRAME `[config.TIMEFRAME_WEIGHTS]`

M1 M5 M15 · M30 H1 · H4 D1 · D1 W1 → microestrutura, intraday, swing, macrotendência.

## 19. GOLD AI SCORE `[config.DEFAULT_WEIGHTS, engine.total_score]`

| Fator | Peso máximo |
| --- | ---: |
| Dólar | 15 |
| Juros reais | 18 |
| Fed | 12 |
| Inflação | 8 |
| Geopolítica | 10 |
| Fluxo | 7 |
| COT | 5 |
| Opções | 6 |
| Técnico | 13 |
| Sentimento | 6 |
| **TOTAL** | **100** |

**+100** = extremamente favorável à alta · **0** = neutro · **−100** = extremamente favorável à queda.

## 20. PROBABILIDADE `[engine.probabilities, engine.confidence]`

Nunca "ouro vai subir". Sempre: **ALTA x% · LATERAL y% · BAIXA z%** e **CONFIANÇA n/100**.

## 21. HORIZONTE `[config.HORIZONS]`

Curtíssimo 5–30 min · Curto 30 min–4 h · Intraday 4–24 h · Swing 1–5 dias · Macro 1–4 semanas.

## 22. ESTÁGIOS `[models.Stage]`

1. **PRÉ-MOVIMENTO** — fundamentos mudaram, preço não confirmou → ALERTA PRECOCE.
2. **CONFIRMAÇÃO** — preço começa a acompanhar → SINAL.
3. **MOVIMENTO** — já se moveu fortemente → **EVITAR PERSEGUIR O PREÇO.**

## 23–26. SINAIS PARA TELEGRAM `[telegram.format_signal]`

🚨 GOLD AI ALERT (compra) · 🔴 GOLD AI ALERT (venda) · ⚠️ GOLD PRE-MOVE · 🔄 GOLD REVERSAL ALERT
(+ 🚨 GOLD SYSTEMIC RISK). Cada um com viés, probabilidade, confiança, horizonte, motivos,
antecipação por camada, situação, zona de atenção (entrada, suporte, resistência, invalidação).

## 27. FILTRO CONTRA FALSOS SINAIS `[signals.confirmations]`

Nenhum sinal com um só fator. Mínimo **3 confirmações independentes** entre as famílias
macro, juros, dólar, fluxo, técnico, sentimento, geopolítica.

## 28. CLASSIFICAÇÃO `[signals.classify]`

STRONG BUY ≥ +70 · BUY +50..+69 · NEUTRAL −49..+49 · SELL −69..−50 · STRONG SELL ≤ −70 ·
PRE-MOVE quando os fundamentos antecipam o preço.

## 29–30. APRENDIZADO E FEEDBACK `[memory.PredictionMemory]`

Registrar cada previsão (data, hora, sessão, preço, previsão, probabilidade, score, fatores,
notícias, evento, resultado, tempo até reação, máxima favorável, máxima adversa). Calcular a
**taxa de acerto** por horário, sessão, direção, horizonte, score, estágio e tipo de sinal, e o
**poder preditivo de cada fator**: PREVISÃO → MOVIMENTO REAL → COMPARAÇÃO → ERRO → APRENDIZADO.

## 31. CORRELAÇÃO ≠ CAUSALIDADE

"Esse fator realmente está causando o movimento ou apenas está correlacionado?"

## 32–34. EVENTOS DE ALTO IMPACTO `[events.py]`

Calendário de risco (FOMC, CPI, PCE, NFP, Powell…). Pré-evento: expectativa, consenso,
posicionamento, cenários acima / em linha / abaixo — a **ÁRVORE DE REAÇÃO DO OURO**. Pós-evento:
RESULTADO → DÓLAR → TREASURY → JUROS REAIS → OURO → FLUXO → CONFIRMAÇÃO OU REVERSÃO.

## 35. REGRA DE OURO

> **O QUE O MERCADO AINDA NÃO PRECIFICOU?**

## 36. SAÍDA FINAL `[report.render_report]`

```text
GOLD AI
Preço · Tendência · Score · Prob. alta · Prob. baixa · Prob. lateral
Dólar · Juros · Juros reais · Fed · Inflação · Geopolítica · Fluxo · COT · Opções · Sentimento · Técnico
Pressão dominante · Pré-movimento · Risco de reversão · Evento próximo
Conclusão
```

## 37. REGRA ANTI-SPAM `[signals.SignalGate]`

Enviar somente quando: novo evento relevante · mudança significativa no score · mudança de
direção · surgimento de pré-movimento · confirmação de movimento · reversão · risco excepcional.

## 38. OBJETIVO FINAL

> **"Se o mercado continuar desenvolvendo o cenário atual, qual será o próximo movimento mais
> provável do ouro?"** — e não "o que aconteceu com o ouro?".

---

## ARQUITETURA

```text
                 🌎 MUNDO
                    │
       ┌────────────┼────────────┐
       ↓            ↓            ↓
    NOTÍCIAS      MACRO       GEOPOLÍTICA
       │            │            │
       └────────────┼────────────┘
                    ↓
              🧠 IA CENTRAL  (engine.py)
                    │
      ┌─────────────┼─────────────┐
      ↓             ↓             ↓
    DÓLAR         JUROS          FLUXO       (factors.py)
      │             │             │
      └─────────────┼─────────────┘
                    ↓
              📊 MERCADO
                    │
       ┌────────────┼────────────┐
       ↓            ↓            ↓
     COMEX         ETF          OPÇÕES
       │            │            │
       └────────────┼────────────┘
                    ↓
             📈 ANÁLISE TÉCNICA  (technical.py)
                    │
                    ↓
             🔮 MODELO PREDITIVO  (premove.py, signals.py)
                    │
          ┌─────────┼─────────┐
          ↓         ↓         ↓
       COMPRA    NEUTRO     VENDA
          │         │         │
          └─────────┼─────────┘
                    ↓
             📲 TELEGRAM  (telegram.py)
                    │
                    ↓
             📚 BANCO DE DADOS  (memory.py)
                    │
                    ↓
              🧠 APRENDIZADO
```

O sistema **não adivinha** o ouro. É um **motor probabilístico de antecipação**: o diferencial é
detectar a **mudança de regime antes da confirmação completa no preço**. A conexão com MT5 deve
converter o sinal em operação somente mediante autorização explícita.


---

# ADENDO 2.0 — DO NÚCLEO AO SISTEMA REAL

## A. DATA ENGINE `[gold_ai/data/]`

🌎 MERCADO REAL → COLETOR → NORMALIZAÇÃO → MarketSnapshot. Cada fonte é isolada: falha vira
status, não exceção. Cobertura reportada a cada ciclo.

## B. CADEIA DE RACIOCÍNIO `[evidence.event_chain]`

Nunca "notícia → sentimento → compra". Sempre:
1. O que aconteceu? 2. O que o mercado esperava? 3. Surpresa? 4. Juros? 5. Dólar?
6. Ouro (já reagiu?)? 7. Fluxo? 8. Pressão latente? 9. Só então: PRE-MOVE · prob · confiança.

## C. NÍVEL DE EVIDÊNCIA `[EvidenceLevel]`

🟢 NÍVEL 1 (1–2 fatores) observação · 🟢 NÍVEL 2 (3 famílias independentes) alerta ·
🟢 NÍVEL 3 (macro + fluxo + técnico) sinal · 🔥 NÍVEL 4 (+ notícia/evento + divergência
preço/fundamento) PRE-MOVE FORTE.

## D. TRÊS MENSAGENS `[telegram.format_signal]`

⚠️ GOLD WATCH (ainda não há operação) · 🚨 GOLD PRE-MOVE (preço não confirmou) ·
🟢 GOLD SIGNAL — PRE-MOVE CONFIRMADO. Previsão nunca se confunde com confirmação.

## E. SABER DIZER "NÃO SEI" `[evidence.edge_status]`

🟡 SEM VANTAGEM ESTATÍSTICA — NÃO ENVIAR SINAL quando probabilidade dominante < 55 %,
confiança < 50/100 ou |score| < 25. Uma IA boa não dá sinal o tempo inteiro.

## F. AVALIAÇÃO HONESTA `[evaluation]`

Precisão (compra/venda) · Recall (movimentos relevantes detectados antes de ficarem
evidentes) · MFE · MAE · Lead time · ⏱️ GOLD LEAD SCORE. Um sistema que acerta 80 % mas só
sinaliza depois que o ouro subiu não atende ao objetivo.

## G. VALIDAÇÃO `[Backtester, walk_forward]`

Backtest sem look-ahead (snapshots reconstruídos só com o passado) e walk-forward:
calibra limiares no treino, mede fora da amostra. Só isso diz se a IA antecipa o ouro ou
apenas explica bem depois do movimento.

## H. EXECUÇÃO `[data.mt5.MT5Executor]`

Sinal → plano de ordem (entrada, invalidação = stop, alvo por risco/retorno). Nada é
enviado ao broker sem autorização explícita (`--authorize`) e sem nível de evidência e
confiança mínimos.


---

# ADENDO 2.1 — VALIDATION ENGINE

Missão: provar que o 2.0 antecipa o mercado, não ampliar funcionalidades.

1. **Backtest temporal rigoroso** — nada de misturar futuro com passado (`lookahead_audit`).
2. **Walk-forward** — treina → testa → avança janela → treina novamente → testa.
3. **Lead time** — quantos minutos antes do movimento o sinal apareceu.
4. **MFE / MAE** — comportamento após cada previsão.
5. **Probabilidade calibrada** — Brier, ECE, previsto × observado, mapa isotônico.
6. **Score por fator** — quais informações realmente possuem poder preditivo.

O sistema passa a produzir um painel, não só COMPRA/VENDA:
REGIME · SCORE · PROBABILIDADE · CONFIANÇA · PRE-MOVE · LEAD TIME · DXY · REAL YIELD · FLOW ·
COT · TECHNICAL · NEWS · STATUS · DECISÃO.

Regra: nenhuma conclusão com menos de 20 sinais resolvidos fora da amostra. O próximo passo não
é dinheiro real: é rodar `live` por dias ou semanas, deixar o loop resolver as previsões e ler
`stats` e `validate`.


---

# ADENDO 2.2 — TRADE SIMULATOR

Pergunta central: **quando o motor dá um PRE-MOVE, quantas vezes o preço atinge 1R, 2R e 3R antes
do stop?** E qual saída (1R, 2R, 3R, 4R, trailing, parcial + trailing) tem a melhor expectativa em R?

Hipótese inicial: RISCO = 1R, ALVO = 3R. O Validation Engine mede; se 3R for ruim e 2R + trailing
for melhor, o sistema muda o TP ótimo.

MAX PROFIT ENGINE: alvo estatístico · estrutural · de volatilidade · por risco/retorno, com
probabilidade estimada por R.

Gestão de risco obrigatória, definida pelo usuário: RISK_PER_TRADE, MAX_DAILY_LOSS, MAX_POSITIONS,
MAX_LOT, MAX_SPREAD, MAX_SLIPPAGE. A IA pode dizer "87 % de confiança", mas não pode transformar
isso em mais risco.

Regra NÃO OPERAR: fatores conflitantes, score baixo, confiança baixa → 🟡 NÃO OPERAR. Tão
importante quanto saber comprar e vender.

Modos: 🟡 PAPER · 🟠 AUTHORIZE · 🔴 LIVE. Dinheiro real só depois do simulador validado no
histórico e no walk-forward.


---

# ADENDO 2.3 — GOLD TRADE MONITOR

**Regra central:** "A abertura de uma operação não encerra o processo de análise. Enquanto existir
posição aberta, o GOLD AI ENGINE deverá continuar recebendo dados de mercado, notícias,
macroeconomia, fluxo e indicadores técnicos, comparar o cenário atual com a tese original e
decidir continuamente entre MANTER, PROTEGER, REDUZIR ou ENCERRAR a posição."

Quatro indicadores por operação aberta: TRADE SCORE · THESIS SCORE · EXIT SCORE · PROFIT
POTENTIAL. Tese invalidada → fechar, mesmo com lucro. Cenário mais forte → proteger metade e
estender o restante. Toda decisão e a evolução do score são registradas para descobrir,
empiricamente, qual nível de deterioração realmente indica que é melhor sair.


---

# ADENDO 3.0 — LIVE EXECUTION ENGINE

Objetivo: transformar uma previsão validada em operação real no MT5, controlá-la continuamente,
reagir a mudanças de cenário, proteger o capital, realizar lucro, atualizar o patrimônio e
recalcular o tamanho das próximas posições.

Três cérebros: PREDICTION ENGINE (para onde o ouro vai) · TRADE ENGINE (como estruturar a
operação) · TRADE MONITOR (a operação aberta ainda faz sentido?).

Regras literais da especificação:
1. **A IA nunca poderá aumentar o risco percentual da conta para recuperar perdas.**
2. **Nenhuma nova posição será aberta enquanto existir uma posição ativa no mesmo ativo.**
3. **O lote é determinado por CAPITAL + RISCO + STOP + ESPECIFICAÇÃO DO CONTRATO — nunca pela confiança.**
4. **O sistema não considera a ordem executada porque a requisição foi enviada:** REQUEST → MT5 →
   BROKER → TICKET → POSITION → PREÇO REAL → SL REAL → TP REAL; divergência = EXECUTION MISMATCH.
5. Limites absolutos: RISK_PER_TRADE · MAX_DAILY_LOSS · MAX_DRAWDOWN · MAX_LOT · MAX_POSITIONS ·
   MAX_SPREAD · MAX_SLIPPAGE. Perda diária atingida → 🚨 TRADING STOP.
6. Kill switch obrigatório: TRADING_ENABLED=false · /STOP · /PAUSE · /STATUS · /CLOSE (com confirmação).
7. O 3.0 nasce em PAPER. PAPER → estatística positiva → AUTHORIZE → microvolume → LIVE.


---

# ADENDO 3.0-B — OPPORTUNITY ENGINE

Antes de mudar qualquer regra, medir: OPPORTUNITY CAPTURE RATE (movimentos relevantes capturados),
ENTRY RATE (oportunidades analisadas → entradas; ⚠️ OVERFILTER), atribuição por filtro (qual regra
bloqueou e o que teria acontecido) e a curva limiar × expectancy (a região ideal, não o score máximo).

Se o sistema entra pouco, não se adiciona filtro: identifica-se a regra que elimina oportunidades
lucrativas e retira-se ou reduz-se o seu peso. Analisar muito, decidir simples, agir quando existir
vantagem.


---

# ADENDO 4.0 — MARKET AI ENGINE

GOLD AI ENGINE 3.0 = cérebro + execução para um mercado. MARKET AI ENGINE 4.0 = cérebro único +
múltiplos mercados + seleção dinâmica da melhor oportunidade. Nada do 2.1–3.0 é descartado; o 4.0
orquestra.

Regras:
1. **O Asset Selector NÃO cria entradas.** Só escolhe entre oportunidades já produzidas pelo
   Prediction/Opportunity Engine; o Risk Engine valida depois.
2. Ranking multidimensional (MARKET OPPORTUNITY SCORE), nunca "score 90 > score 80". Pesos iniciais
   a serem testados pelo Validation Engine.
3. Separar QUALIDADE HISTÓRICA de OPORTUNIDADE ATUAL e medir OPPORTUNITY DECAY: não basta existir
   vantagem; precisa existir vantagem para entrar AGORA.
4. STATISTICAL CONFIDENCE: o vencedor não é o de maior expectancy, e sim o de maior expectancy
   ajustada ao tamanho e à estabilidade da amostra.
5. PORTFOLIO EXPOSURE: "estou diversificando ou fazendo a mesma aposta três vezes?" Limites
   MAX_TOTAL_OPEN_RISK, MAX_CORRELATED_RISK, MAX_PORTFOLIO_POSITIONS, MAX_ASSET_EXPOSURE.
6. Fases: 1 EURUSD, US500, XAUUSD, USDJPY, WTI · 2 NAS100, GBPUSD · 3 BTCUSD, ETHUSD — provar que o
   cérebro generaliza antes de transformar volatilidade em falso edge.
7. Nenhum filtro de entrada arbitrário novo; nasce em PAPER.

8. **LIVE EDGE diário** (o teste definitivo): tabela por mercado com trades fora da amostra,
   expectancy, probabilidade calibrada (declarada × observada), capture rate e status por confiança
   estatística — gerada a partir do que o sistema viveu, guardada todos os dias. Não precisamos
   acreditar que um mercado é melhor: os dados mostram.

9. **NEWS ENGINE é entrada central**: notícia → evento → importância → expectativa → surpresa → direção
   esperada por mercado → reação real → divergência → pressão latente → score. Ausência de informação não
   é informação negativa: NEWS ausente = UNKNOWN, peso reduzido. COT = último dado válido com idade, peso
   decaindo. Só depois de o NEWS ENGINE funcionar de verdade se recalibra o funil.

10. **BANCO HISTÓRICO POINT-IN-TIME** `[history, data/history_sources, ablation]`: antes de recalibrar
    qualquer piso, o backtest recebe a mesma informação que o live — calendário macro com consenso/real/
    revisões (Trading Economics, ALFRED) e manchetes com tom e intensidade (GDELT) — vendo em cada instante
    apenas o que estava publicado (`published_at ≤ t`). TESTE A (Preço + Macro) e TESTE B (Preço + Macro +
    News) contra Preço somente, no mesmo walk-forward. Se a informação cria entradas com expectancy ≥ referência,
    ela fica; os efeitos por ativo nascem das regras macro e são substituídos pelos empíricos, nunca inventados.

11. **REACTION ENGINE** `[reaction]`: a informação tem velocidade de transmissão. Por evento e ativo mede-se quanto tempo
    o alvo e os líderes (USD, yields) levam para reagir; a mediana por tipo de evento, aprendida só com eventos já
    concluídos, permite detectar a assimetria temporal (líderes reagiram, alvo ainda não, tempo dentro da janela histórica)
    = PRESSÃO LATENTE, evidência para o pré-movimento. Nunca usa a reação do próprio evento para decidir sobre ele.
    Segundos exigem ticks/M1 (`history prices`, `reaction learn --tf TICK`): dois horizontes (reação 0–5 min, continuação
    5–60 min), lead-lag condicional e simulação com spread + slippage + latência. O robô não opera atraso detectado: opera
    configurações que sobreviveram ao custo, fora da amostra.

12. **PERFIL AGRESSIVO COM TRAVA** `[guard.PerformanceEngine, .env]`: RISK_PER_TRADE=3 (o lote acompanha o capital —
    compounding; nunca sobe após perda; martingale proibido), DAILY_TARGET=10 (meta = trava: ao atingir, sem novas
    entradas até o dia seguinte, posições abertas seguem com o monitor; a meta nunca força entrada — sem edge, não opera),
    MAX_DAILY_LOSS=6 (duas perdas cheias), MAX_LOT=1.0, MAX_TOTAL_OPEN_RISK=6, MAX_CORRELATED_RISK=3. Aritmética: com 3%
    por operação, +10% = +3,33R líquidos no dia. O Reaction Engine só prioriza curtíssimo prazo depois de edge líquido
    comprovado (`reaction learn --tf BOTH`, veredito 🟢 com amostra).

# ADENDO 5.0 — INFORMAÇÃO IMPLÍCITA (FLOW ANOMALY ENGINE)

Explícito (news/macro/Fed) + implícito (fluxo/preço/volume) → EVENT & FLOW ENGINE → REACTION ENGINE → LEAD/LAG → PRESSÃO LATENTE
→ PRE-MOVE → OPPORTUNITY → ASSET SELECTOR → TRADE → MONITOR → LEARNING. Regras: (1) FLOW SCORE 0–100 com componentes visíveis;
(2) assinaturas A/B/C e origem A–E; (3) NUNCA nomear o comprador — "fluxo institucional provável, origem desconhecida" até haver
evidência; (4) assinatura C com score ≥ 70 = ANOMALOUS FLOW REGIME: o modelo normal do ativo é suspenso e entradas contra o fluxo
são adiadas; (5) o evento implícito entra no Reaction Engine como qualquer evento: a propagação é aprendida, não assumida; (6) o
núcleo 4.0 (risco, MT5, monitor, Telegram, OOS, walk-forward, funil, selector) permanece intacto.

13. **CICLO DE VIDA DE PARÂMETROS** `[lifecycle]`: nada vira parâmetro com menos de 20 casos fora da amostra (30 operacional,
    50 validado); 3 perdas seguidas alertam, 4 protegem, 5 suspendem e obrigam a revalidar nos últimos 30/50/total; só a
    deterioração do edge quebra; o parâmetro quebrado vai para sombra (PAPER) e nunca é apagado.


## Adendo 5.2 — FLOW + REACTION + CAPTURA: ANALISAR MUITO, DECIDIR SIMPLES

Um movimento anômalo não é ruído nem sinal de entrada. Ele inicia uma **investigação de fluxo e reação**.

`FLOW ANOMALY = MODO DE INVESTIGAÇÃO + WATCH`, não bloqueio absoluto.

1. **Não operar contra** um fluxo anômalo sem confirmação (a entrada contra o fluxo é adiada, não proibida para sempre).
2. **Não bloquear** o fluxo anômalo: `ORIGEM DESCONHECIDA` ≠ `NO TRADE`. Significa WATCH → buscar explicação → observar propagação → confirmar ou descartar.
3. **Investigar a origem**: notícia, macro, líderes (USD, yields), outros ativos, técnica/VWAP, ticks/volume (assinaturas A/B/C, origens A–E).
4. **Abrir o relógio de reação**: evento implícito `flow_<ATIVO>_<up|down>` no REACTION ENGINE — quem foi o primeiro líder, quem ainda está atrasado, tempo histórico de propagação.
5. **Registrar e medir cada anomalia** (`FLOW SCORE ≥ 70`): ativo, hora, score, ATR do movimento, volume, persistência, cross-market, origem, assinatura, líder, regime, direção;
   60 min depois: MFE/MAE em 5/15/30/60 min, CONTINUOU / REVERTEU / INDEFINIDO, minutos até confirmação. `flow --stats` mostra a tabela; o status do live também.
6. **A entrada só ocorre** quando continuação, direção e/ou propagação ao ativo atrasado tiverem vantagem estatística validada (tiers do ciclo de vida: 20 candidato · 30 operacional · 50 validado).
7. **O histórico decide o parâmetro**, não uma regra arbitrária: a estatística de continuação por ativo × origem entra no relógio quando há ≥ 5 casos (informação) e só vira edge com amostra.

Mais inteligência → mais oportunidades detectadas → mais setups qualificados → melhor captura → maior lucro potencial, **sem aumentar o risco por operação**.


## Adendo 5.2b — PORTFOLIO OPPORTUNITY ENGINE

5 mercados → medir a vantagem de cada um → identificar oportunidades simultâneas → montar uma carteira de entradas → monitorar cada posição.

1. Cada entrada passa pelo seu próprio funil (WATCH → SETUP → OPPORTUNITY → EXECUTION → MONITOR → EXIT). Ter 3 ou 4 sinais não é ter 3 ou 4 entradas.
2. O risco é conjunto: risco individual + correlação (mesma tese econômica, assinada pela direção) + exposição total + qualidade da vantagem.
   Quatro posições a 3% não são 12% de risco independente quando ouro, dólar e juros estão no mesmo movimento — a segunda da mesma tese é barrada.
3. Propagação: quando um ativo faz um movimento anômalo, a pergunta não é "comprar tudo", é "quais ativos historicamente respondem a este movimento
   e ainda não o incorporaram, com expectativa positiva depois de custos?" (Reaction Clock + lead-lag + Edge Bank).
4. Mais oportunidades não significa mais lucro: `portfolio-sim` compara 1 × 2 × 3 × 4 posições simultâneas com as operações fora da amostra,
   líquido de spread/slippage e correlação, e só o retorno líquido com drawdown proporcional justifica subir `MAX_ENTRIES_PER_CYCLE`, `MAX_TOTAL_OPEN_RISK`
   e `MAX_CORRELATED_RISK` no .env.

## Adendo 5.2c — ESCADA DE RISCO `[lifecycle.risk_ladder_pct, guard.GuardLimits.ladder, live_engine.risk_usd]`

1. O percentual de risco por operação não é uma opinião sobre o sistema; é função do tier do ciclo de vida de cada mercado:
   base (RISK_PER_TRADE) enquanto candidato (<30 casos fora da amostra) · degrau 2 em operacional (≥30) · degrau 3 em validado (≥50).
   `.env`: `RISK_LADDER=3,4,5`, `RISK_LADDER_MAX=5` (teto absoluto; nada acima dele, por nenhum motivo).
2. Sobe só com edge confirmado: expectancy positiva em todas as janelas com n ≥ 10, sem deterioração, estado NORMAL ou REATIVADO.
   Em ALERTA (3 perdas seguidas), PROTEÇÃO (4), SUSPENSO (5), QUEBRADO ou expectancy negativa o mercado volta à base — automaticamente.
3. A escada é decidida antes dos resultados e nunca mexida depois de ganhos ou perdas. Não se sobe um degrau por uma boa semana nem se
   desce por uma má; sobe-se por amostra e desce-se por regra. Pedir 10% "porque um bom sistema não perde" continua vetado: cinco perdas
   a 10% são −41% do capital e exigem +69% para voltar; a 5% são −23% e exigem +29%.
4. Os demais limites continuam por cima da escada: MAX_DAILY_LOSS, MAX_TOTAL_OPEN_RISK, MAX_CORRELATED_RISK, MAX_LOT, META DIÁRIA (trava).
