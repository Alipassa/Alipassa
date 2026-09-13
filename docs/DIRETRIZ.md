# DIRETRIZ — IA DE INTELIGÊNCIA PREDITIVA DO OURO

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
