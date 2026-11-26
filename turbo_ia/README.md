# TURBO IA — botão "Iniciar IA" que analisa o gráfico e opera no MetaTrader 5

Painel local (navegador) no estilo de uma plataforma de trading: gráfico com SuperTrend, LSMA, Keltner, Parabolic SAR e CHoCH,
configurações da IA à esquerda e modo manual à direita. Ao clicar em **Iniciar IA**, a sessão roda sozinha:

```text
candle fecha → análise (8 indicadores votam) → confluência ≥ mínimo do modo → ordem a mercado no MT5 com SL/TP
            → acompanha a posição (SL/TP no servidor, saída por tempo opcional) → soma o resultado da sessão
            → para sozinha ao bater a META, o LIMITE DE PERDA, a PROTEÇÃO DA SESSÃO ou o máximo de operações
```

## Como rodar (Windows + MT5)

1. Instale o MetaTrader 5 da sua corretora, faça login numa conta **DEMO** e deixe o terminal aberto.
   Em *Ferramentas → Opções → Expert Advisors*, marque **Permitir negociação algorítmica**.
2. `pip install MetaTrader5`
3. Na raiz do repositório: `python -m turbo_ia --broker mt5` (ou dê dois cliques em `scripts\rodar_turbo_ia.bat`).
4. O navegador abre em `http://127.0.0.1:8765`. Escolha o mercado (o nome exato do símbolo no MT5, ex.: `Volatility 10 Index`
   na Deriv, `XAUUSD`, `EURUSD`), o tempo gráfico, o modo e os limites, e clique em **Iniciar IA**.

Sem MT5 (ou em Linux/Mac) rode `python -m turbo_ia --broker paper`: um simulador local gera o preço e executa as ordens,
para testar o painel sem risco. `--broker auto` (padrão) tenta o MT5 e cai no simulador.

O painel lê o `.env` da raiz do projeto (o mesmo do resto do repositório): `MT5_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`,
`MT5_SERVER`. Sem `MT5_PATH`, usa o terminal da Pepperstone no caminho padrão
`C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe` (o caminho pode vir com aspas, ou só a pasta).

## A análise

| Indicador | Vota compra quando | Peso |
| --- | --- | --- |
| SuperTrend (10, 3) | tendência de alta | 2,0 |
| CHoCH (ZigZag 5) | mudança de estrutura para cima nos últimos 30 candles | 1,5 |
| LSMA 25 | preço acima e LSMA subindo | 1,0 |
| Keltner (20, 1) | preço acima do meio / rompeu a banda superior | 1,0 |
| Parabolic SAR (0,02 · 0,02 · 0,2) | SAR abaixo do preço | 1,0 |
| EMA 9 × 21 | EMA 9 acima da 21 | 1,0 |
| Momentum (5 candles) | subiu mais que 0,2 ATR | 1,0 |
| RSI 14 | entre 52 e 70 | 0,5 |

Venda é o espelho. A **confluência** é o peso a favor ÷ peso total.

| Modo | Exige |
| --- | --- |
| ⚡ Veloz | confluência ≥ 55 % |
| ⚖ Equilibrado | ≥ 65 %, SuperTrend a favor, RSI fora de exaustão (>75 / <25) |
| 🎯 Preciso | ≥ 75 %, SuperTrend a favor, CHoCH ou LSMA a favor, RSI fora de exaustão |

- **Stop**: logo além da linha do SuperTrend quando ela está entre 0,5 e 3 ATR; senão `Stop (ATR)` × ATR 14.
  **Alvo** = `Risco:Retorno` × distância do stop. A corretora recebe SL/TP já ajustados ao stops level e ao spread.
- A IA só analisa **candles fechados** (o candle em formação é ignorado) e os pivôs do CHoCH só valem depois de confirmados:
  o sinal não "repinta".
- Uma posição por vez. **Duração (velas)** > 0 fecha a posição após N candles — o equivalente mais próximo do Rise/Fall,
  que não existe no MT5 (lá são CFDs com compra/venda).

## Teste de acerto por tempo gráfico (`backtest.py`)

Faz a mesma análise do botão em cada candle fechado do histórico, "entra" no fechamento e confere o preço N minutos depois
(estilo Rise/Fall; empate = erro). Uma operação por vez: a próxima só depois da expiração.

```bash
python -m turbo_ia.backtest --source mt5 --symbols "Volatility 10 Index" --tfs M2,M5 --expiry 10
python -m turbo_ia.backtest --source deriv --symbols R_10 --tfs M2,M5 --expiry 10 --days 30   # pip install websocket-client
python -m turbo_ia.backtest --source yahoo --symbols EURUSD=X,GC=F --tfs M2,M5 --expiry 10
```

Resultado em 60 dias reais (Yahoo, 25/11/2026), expiração 10 min, payout 78 % → precisa de **56,2 %** para empatar:

| Ativo | Gráfico | Modo | Operações | Acerto | Sem contar empates |
| --- | --- | --- | --- | --- | --- |
| Ouro | **M5** | veloz | 5658 | **48,9 %** | 49,6 % |
| Ouro | M2 | veloz | 3144 | 48,2 % | 48,7 % |
| EUR/USD | M2 | veloz | 4069 | 38,7 % | 48,3 % |
| EUR/USD | M5 | veloz | 7371 | 38,2 % | 48,6 % |

M2 e M5 empatam dentro da margem estatística; nenhum chega perto dos 56,2 %. Os outros modos ficaram iguais ou piores.

## 🧪 Modo experimental "Reversão ouro" — teste em DEMO

Resultado da busca por vantagem (≈ 50 regras, escolhidas numa metade dos dados e conferidas na outra): a única que se
sustentou foi **reversão à média no ouro, gráfico M2, só às 16h e 20h UTC (13h e 17h de Brasília)**.

- Regra: no fechamento de cada candle M2, se o preço está a mais de 1 ATR(14) da média de 20, aposta na volta
  (acima → VENDA, abaixo → COMPRA). Stop 1 ATR, alvo 2× o stop (veja a seção ganho × perda). Com "Ganho × perda" = 0 volta ao modo antigo: sai em 10 min, sem alvo, stop 3 ATR.
- Histórico (GC=F, 60 dias): **61,2 % em 209 operações** (faixa provável 54,5–67,6 %), cerca de 6 por dia. No M5: 53,2 %, então só M2.
- **Não está provado.** Os horários foram escolhidos entre 24, então parte desse acerto pode ser sorte. Precisa ficar acima de
  **56,2 %** (empate com payout de 78 %) em operações NOVAS.

Como testar: MT5 em conta demo → mercado `XAUUSD` (ou o nome do ouro na sua corretora) → modo **🧪 Reversão ouro** (o painel
ajusta M2, duração 5, stop 3, R:R 0) → **Iniciar IA** e deixe rodando nos horários. O cartão **Registro de acerto (10 min)**
confere cada entrada 10 min depois e grava em `~/turbo_ia_registro.jsonl` (ou `TURBO_IA_REGISTRO`); simulador e MT5 ficam
separados. O veredito só sai a partir de 100 operações (≈ 3–4 semanas):

| Veredito | Quando |
| --- | --- |
| VANTAGEM CONFIRMADA | até o pior caso da faixa supera 56,2 % |
| SEM VANTAGEM — abandone | até o melhor caso fica abaixo de 56,2 % |
| inconclusivo | a faixa contém 56,2 % — continue em demo |

O fuso do servidor MT5 é detectado sozinho (os candles vêm no horário da corretora); se errar, defina `TURBO_IA_UTC_OFFSET`
(ex.: `2`). Para refazer a conta do histórico: `python -m turbo_ia.backtest --source yahoo --symbols GC=F --tfs M2 --modes reversao`.

## Ganho 2× / perda 1× e ganho 3× / perda 1× (`--rr`)

```bash
python -m turbo_ia.backtest --source yahoo --symbols GC=F,EURUSD=X --tfs M2,M5 --modes veloz,equilibrado,preciso,reversao --rr 2,3 --sl-atr 1,1.5
```

Entra no fechamento do candle do sinal, stop = N × ATR, alvo = 2× ou 3× o stop; o primeiro tocado decide e, se os dois cabem
no mesmo candle, conta o **stop** (pior caso). Spread descontado (ouro 0,30; EUR/USD 0,00012). 60 dias, 25/11/2026:

| Ativo / gráfico | Modo | Ganho × perda | Stop | Ops | Acerto | Precisa | Média por op. | 1ª metade · 2ª | Pior queda |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ouro M2 | **reversão** | **2:1** | 1 ATR | 214 | **43,5 %** | 33,3 % | **+0,18 R** | +0,16 · +0,21 | 9 R |
| Ouro M2 | **reversão** | **3:1** | 1 ATR | 174 | **35,1 %** | 25,0 % | **+0,28 R** | +0,39 · +0,18 | 11 R |
| Ouro M5 | reversão | 2:1 | 1 ATR | 162 | 41,4 % | 33,3 % | +0,17 R | +0,10 · +0,23 | 10 R |
| Ouro M5 | preciso | 3:1 | 1,5 ATR | 559 | 27,5 % | 25,0 % | +0,06 R | −0,02 · +0,13 | 52 R |
| Ouro M2 | veloz/equilibrado/preciso | 2:1 e 3:1 | 1–1,5 ATR | 780–2856 | 24–34 % | — | −0,05 a −0,15 R | — | 70–350 R |
| EUR/USD M2/M5 | todos | 2:1 e 3:1 | 1–1,5 ATR | — | ≈ empate | — | −0,2 a −1,1 R | — | — |

- Os modos de confluência ficam no empate ou abaixo — o alvo maior só troca acerto por tamanho do ganho.
- No EUR/USD de 2–5 min o ATR é do tamanho do spread: o custo come 0,3–1 R por operação. Não opere EUR/USD nesses tempos.
- A reversão no ouro é o único caso positivo nas duas metades, com 2:1 e 3:1. O modo usa **2:1, stop 1 ATR** por padrão
  (campo "Ganho × perda" = 3 para 3:1). Ressalva de sempre: os horários 16h/20h UTC saíram desses mesmos dados.

O **Registro de acerto** do painel mede o mesmo em tempo real: com alvo, cada entrada é seguida candle a candle até tocar
alvo ou stop e vira +2 R / +3 R ou −1 R; o empate passa a ser 33,3 % (2:1) ou 25 % (3:1).

## Confirmação no histórico da Pepperstone (25/11/2026)

50 000 velas M2 de XAUUSD (≈ 3 meses), horário do servidor convertido para UTC vela a vela, spread real de cada vela
(mediana 0,10):

| Ganho × perda | Ops | Acerto | Precisa | Média/op | Metades | PF | Pior queda | Stops seguidos |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **3:1** | 423 | 30,3 % | 25,0 % | **+0,164 R** | +0,165 / +0,163 | 1,22 | 21 R | 13 |
| 3:1 + comissão Razor | 423 | 30,3 % | 25,0 % | +0,137 R | +0,139 / +0,135 | 1,18 | 23 R | 13 |
| 2:1 | 502 | 39,4 % | 33,3 % | +0,137 R | +0,050 / +0,224 | 1,22 | 15 R | 9 |
| 3:1 com spread fixo 0,30 | 423 | 30,3 % | 25,0 % | +0,095 R | +0,102 / +0,087 | 1,12 | 25 R | 13 |

Hora a hora, só 16h e 20h UTC ficam positivas nas duas metades com 3:1. US$ 1.000 em 30 dias (≈ 125 ops, 3:1, spread real):
1 % → típico US$ 1.208 (20 % de chance de prejuízo, queda típica 14 %); 2 % → US$ 1.401 (20 %, 27 %);
5 % → US$ 1.731 (33 %, 58 %). Vantagem ≈ 1,8 desvio acima de zero — promissora, ainda não comprovada.

## Pesquisa de melhorias (`research.py`) — esticada mínima 2 ATR

`python -m turbo_ia.research --source mt5 --symbol XAUUSD` testa 96 variações (esticada 1/1,25/1,5/2 ATR, vela de
confirmação, alvo 2R/3R/4R/média, zero a zero, saída em 30 velas, filtro de spread). A metade A do histórico escolhe; a
metade B prova. Na Pepperstone (101 dias M2, 25/11/2026):

| Variação | Metade A | Metade B | Pior queda |
| --- | --- | --- | --- |
| esticada > 1 ATR, alvo 3R (anterior) | 195 ops · +0,163 R | 228 ops · +0,165 R | 21 R |
| **esticada > 2 ATR, alvo 3R (atual)** | 104 ops · +0,262 R | 107 ops · **+0,340 R** | 17 R |
| esticada > 2 ATR, alvo 4R | 102 ops · +0,377 R | 104 ops · +0,303 R | 16 R |
| alvo 4R com esticada 1 ATR | 174 ops · +0,191 R | 196 ops · +0,077 R | 23 R |

Metade das operações e lucro total parecido, com quedas menores. US$ 1.000 a 2 %: mês típico ≈ US$ 1.380 nos dois casos,
chance de prejuízo 18 % → 11 %, queda típica 27 % → 18 %. A 3 % a nova regra tem a mesma queda típica da antiga a 2 %
(≈ 26 %) com mês típico ≈ US$ 1.570. O campo **Esticada mín. (ATR)** no painel ajusta (padrão 2).

## Tempo gráfico: M1 × M2 × M3 (Pepperstone, 25/11/2026)

Varredura M1–M15 × 24 horas (escolha na metade A, prova na B): escolher novas horas pelo passado fracassou em todos os
gráficos (ex.: M1 +126 R/mês na A → −49 R/mês na B). A regra atual (16h/20h UTC, esticada 2, alvo 3×) foi positiva na prova
em TODOS: M1 +0,32 · M2 +0,33 · M3 +0,41 · M5 +0,21 · M10 +0,10 · M15 +0,05 R/op (M15 cobre ~1,3 ano).

Robustez (50 000 velas cada; spread real × spread pessimista 0,30):

| Gráfico | Dias | Ops | Acerto | R/op real | R/op com 0,30 | R/mês real | R/mês com 0,30 | Pior queda |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M1 | ~50 | 252 | 32,5 % | +0,239 | +0,131 | +36 | +20 | 13–17 R |
| M2 | ~101 | 211 | 33,6 % | +0,302 | +0,235 | +19 | +15 | 17–18 R |
| M3 | ~152 | 195 | 34,9 % | +0,358 | +0,307 | +14 | +12 | 8–9 R |

M1 dá mais lucro por mês, mas depende mais do custo de execução (perde ~45 % com spread pior); M3 é o mais robusto por
operação; M2 fica no meio. Todos positivos nas duas metades nos dois cenários de custo.

## Travas depois de stop (Pepperstone, 62 500 velas, 26/11/2026)

| Gráfico | Regra | Metade A (mais antiga) | Metade B (recente) | Pior queda · stops seguidos |
| --- | --- | --- | --- | --- |
| M1 (63 d) | sem trava | −0,010 R/op · −1,2 R/mês | +0,285 R/op · +51,0 R/mês | 15 R · 10 |
| M1 | não repete o lado | −0,023 · −1,2 | +0,337 · +25,7 | 13 R · 7 |
| M2 (127 d) | **sem trava** | **+0,158 · +10,7** | **+0,308 · +20,3** | 19 R · 15 |
| M2 | após 2 stops encerra | +0,157 · +7,7 | +0,288 · +15,1 | 13 R · 11 |
| M3 (190 d) | sem trava | +0,106 · +4,5 | +0,359 · +12,9 | 20 R · 19 |

Nenhuma trava ganhou da regra sem trava nas duas metades → nenhuma adotada. Achado importante: a vantagem é mais
fraca na metade mais antiga em todos os gráficos, e no M1 ela some (≈ 0). O M2 é o único positivo com folga nas duas
metades → M2 volta a ser o recomendado. Sequências de 10 a 19 stops aconteceram: dimensione o risco para isso.

## Filtro de tendência e freio diário (Pepperstone, 62 500 velas, 26/11/2026)

M2 (127 dias), R por mês nas metades A (antiga) | B (recente):

| Regra | A | B | Pior queda · stops seguidos |
| --- | --- | --- | --- |
| **sem filtro (atual)** | +10,7 | +20,3 | 19 R · 15 |
| só a favor da tendência (EMA 200) | −1,8 | +2,0 | 15 R · 10 |
| só com EMA 200 plana | +13,9 | +10,5 | 13 R · 11 |
| não entra contra impulso > 4 ATR | +0,8 | +10,8 | 16 R · 11 |
| freio diário −3R (6 % a 2 %) | +9,8 | +12,1 | 14 R · 10 |
| freio diário −4R (8 %) | +12,6 | +14,8 | 15 R · 12 |
| **freio diário −5R (10 % a 2 %) — adotado** | **+11,0** | **+21,3** | **16 R · 13** |
| freio diário −6R (12 %) | +9,0 | +20,8 | 19 R · 14 |

Nenhum filtro de tendência ganhou nas duas metades (a estratégia lucra justamente contra a tendência de curto prazo).
O freio diário de 5 × o risco foi o único ajuste igual ou melhor nas duas metades do M2, com queda menor; no M3 ficou
neutro (A +3,3 × +4,5 · B +12,9 × +12,9). Configuração final: M2 · esticada 2 · 3R · 2 % · freio diário 10 % · sem meta.

## Outros mercados (Pepperstone, M2, mesma regra sem ajuste, 26/11/2026)

R/op nas metades A | B (16h/20h UTC): XAUUSD +0,158 | +0,308 ✔ · US30 +0,181 | +0,083 ✔ · AUDUSD +0,104 | +0,213 ✔ ·
XAGUSD −0,201 | +0,073 · US500 +0,044 | +0,036 · NAS100 −0,057 | +0,250 · GER40 +0,073 | −0,037 · UK100 −0,693 | −0,336 ·
SpotCrude −0,231 | −0,101 · NatGas −0,723 | −1,426 · BTCUSD −0,070 | −0,251 · ETHUSD −0,216 | −0,221 · EURUSD +0,027 | −0,513 ·
GBPUSD +0,260 | −0,109 · USDJPY +0,414 | −0,041.

Com 14 mercados testados, 2 a 3 "aprovados" por acaso já seriam esperados; US30 e AUDUSD passaram por pouco e os
vizinhos (US500, NAS100, outras moedas) não confirmam → tratados como ruído. A prata, que no Yahoo parecia ótima, era
efeito de spread zero. Decisão: operar só o ouro.

## Lote pelo risco (% do saldo)

Campo **Risco % / op.** > 0: a cada entrada o lote é `saldo × risco % ÷ (distância do stop × valor de 1,0 de preço por lote)`,
arredondado PARA BAIXO no passo da corretora (máximo 10 %). Se nem o lote mínimo cabe no risco, a operação é pulada.
Ex.: US$ 1.000, 5 %, ouro com stop de US$ 3 → 0,16 lote (risco US$ 48).

O modo **🧪 Reversão ouro** vem com a escolha do usuário: **2 % por operação, ganho 3× / perda 1×**, sem meta de lucro,
proteção desligada e limite de perda = metade do saldo. Simulação (60 dias de GC=F, 20 mil meses sorteados, US$ 1.000):

| Risco | Ganho × perda | Mês ruim (5 %) | Mês típico | Mês bom (95 %) | Chance de prejuízo | Queda no caminho (típica / pior 5 %) |
| --- | --- | --- | --- | --- | --- | --- |
| 5 % | 3:1 | US$ 710 | US$ 4.141 | US$ 27.377 | 9 % | 54 % / 77 % |
| 1 % | 3:1 | US$ 1.024 | US$ 1.472 | US$ 2.174 | 4 % | 13 % / 23 % |

Supõe que a vantagem do histórico se repete — os horários saíram desses mesmos dados. Se a vantagem real for 0,2 R menor,
o mês típico com 5 % fica perto do zero a zero e a queda no caminho continua grande. Confirme em DEMO antes.

## 📈 Modo Cruzamento 9×20 ouro H1 (padrão desde 26/11/2026)

Estratégia do usuário, a única que passou em ~11 anos de XAUUSD H1 na Pepperstone (spread real):
SMA 9 cruza a SMA 20 para cima → compra; para baixo → venda; sai e vira a mão no cruzamento contrário; stop 2 ATR no
servidor; sem alvo; opera o dia todo, só com velas fechadas.

| Ano | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ATR | −26 | +36 | −56 | −6 | +192 | +16 | +41 | +19 | +41 | +85 | +127 |

8 de 11 anos positivos · 3.559 operações · +0,13 ATR por operação (+0,065 R com stop de 2 ATR) · ~27 operações/mês.
Ganha nos anos de tendência e perde nos laterais. Painel: risco 2 % (escolha do usuário; 1 ATR ≈ 1 % do saldo), freio
diário 10 %, limite da sessão 50 %. Validação (`validate.py --modo cruzamento`, critérios fixados antes da demo):
≥ 100 operações · média ≥ +0,05 R · primeiras/últimas 50 positivas · PF ≥ 1,10 · queda ≤ 30 R · custo ≤ backtest + 0,03 R ·
IA ligada ≥ 90 % das horas dos dias úteis com ≤ 2 erros. Testes da estratégia: `python -m turbo_ia.cross --years`.

### Tempo gráfico do Cruzamento (Pepperstone 2016–2026, SMA 9×20 stop 2 ATR, spread real)

| Gráfico | Total ATR | Anos + | Pior ano | ATR/op | Ops/mês | ATR/mês (≈ %/mês a 2 %) |
| --- | --- | --- | --- | --- | --- | --- |
| M1 (2021–26) | −27.385 | 0/6 | −7.506 | −0,241 | 1.387 | −334 |
| M2 | −18.778 | 1/11 | −3.055 | −0,195 | 614 | −120 |
| M5 | −3.629 | 1/11 | −691 | −0,093 | 248 | −23 |
| M10 | −481 | 3/11 | −227 | −0,025 | 124 | −3,1 |
| M15 | +428 | 7/11 | −140 | +0,033 | 83 | +2,7 |
| M30 | +435 | 7/11 | **−35** | +0,065 | 43 | +2,8 |
| **H1** | **+469** | **8/11** | −56 | **+0,132** | 23 | **+3,0** |

Abaixo de M15 o spread destrói a estratégia (centenas de cruzamentos por mês). H1 é o mais consistente, o de maior
vantagem por operação (menos sensível a custo) e o de maior lucro mensal → continua o padrão. M30 é a alternativa com
pior ano menor, mas com metade da vantagem por operação.

### Filtros de mercado lateral no Cruzamento (Pepperstone 2016–2026, regra de adoção fixada antes)

| Filtro | Total | Anos + | Pior ano | R/op | Ops | Cofre | Desenv. |
| --- | --- | --- | --- | --- | --- | --- | --- |
| **sem filtro (atual)** | **+469** | 8/11 | −56 | +0,132 | 3.559 | +392 | **+77** |
| lado da SMA 200 | +504 | **9/11** | −65 | **+0,279** | 1.808 | **+456** | +48 |
| CHOP < 61,8 | +373 | 6/11 | −73 | +0,117 | 3.180 | +310 | +63 |
| SMA 50 inclinada | +64 | 7/11 | −30 | +0,195 | 327 | +53 | +11 |
| CHOP < 50 | +38 | 6/11 | −66 | +0,026 | 1.445 | +22 | +17 |
| ADX > 20 | −16 | 4/11 | −72 | −0,008 | 2.028 | −43 | +27 |
| ER > 0,3 | −27 | 5/11 | −28 | −0,090 | 301 | −25 | −2 |
| ADX > 25 | −91 | 5/11 | −91 | −0,070 | 1.292 | −111 | +20 |

Nenhum venceu em tudo → nenhum adotado. Os filtros barram justamente o começo das tendências (ADX ainda baixo), que é
onde está o lucro (2020: +192 sem filtro × +74 com ADX > 20). O "lado da SMA 200" chegou perto (mais total, mais anos
positivos, o dobro de R por operação), mas teve pior ano mais fundo e menos lucro no período recente — fica como
candidato a reavaliar com os dados da demo, não como regra.

## Programa de validação (itens 2–7) — `program.py` e `costs.py`

Regras escritas ANTES dos resultados. Dados divididos no tempo:
**desenvolvimento** = a partir de 20/05/2026 (já usado para criar a regra) · **cofre** = antes de 20/05/2026 (nunca usado).

| Item | O que testa | Variações fixadas antes |
| --- | --- | --- |
| 2 | Filtro de movimento impulsivo (velocidade da esticada, não tendência) | andou > 3 ATR em 5 velas · > 4 ATR em 5 velas · vela do sinal > 2 ATR · 5+ velas seguidas na mesma direção |
| 3 | Entrada após rejeição | espera até 3 velas por uma vela que feche voltando · idem com stop além do extremo |
| 4 | M2 × M3 walk-forward | todo mês escolhe o gráfico que rendeu mais nos 60 dias anteriores |
| 5 | Saída parcial × 3R fixo | 50 % em +1R / +1,5R / +2R, resto no zero a zero buscando 3R |
| 6 | Custo real de execução | derrapagem na entrada (× pedido e × fechamento da vela), spread no envio, derrapagem no stop/alvo, comissão — em R |
| 7 | Adoção | só se ganhar da regra atual nas 2 metades do desenvolvimento **e** no cofre (≥ 30 operações) |

```bash
python -m turbo_ia.program --source mt5 --symbol XAUUSD     # itens 2, 3, 4, 5, 7
python -m turbo_ia.costs --days 30                          # item 6 (usa turbo_ia_execucao.jsonl gravado pela IA)
```

Para haver cofre, o MT5 precisa entregar velas M2 de antes de 20/05/2026: Ferramentas → Opções → Gráficos →
Máx. barras no gráfico = Ilimitado, reiniciar o MT5 e abrir um gráfico XAUUSD M2 rolando bem para trás.

## Validação para conta real (`validate.py`)

Critérios combinados ANTES da demo (conta demo nova, 26/11/2026): ≥ 100 operações · média ≥ +0,10 R · primeiras e
últimas 50 positivas · fator de lucro ≥ 1,15 · pior queda ≤ 25 R · custo real ≤ backtest + 0,05 R · 2 semanas com a IA
ligada em ≥ 90 % das janelas e ≤ 2 erros. Com os 7 ✔: conta real com 1 % de risco, 2 % depois de mais 50 operações
positivas. A IA grava `~/turbo_ia_eventos.jsonl` (presença nas janelas e erros) e `~/turbo_ia_execucao.jsonl` (preços).

## Auditoria (26/11/2026)

| # | Falha | Risco | Correção |
| --- | --- | --- | --- |
| 1 | Painel reiniciado com operação aberta: a IA não sabia dela e podia abrir outra (risco dobrado) | alto | ao iniciar, adota a posição da IA que já está aberta |
| 2 | Painel ligado no fim de semana / mercado parado: fuso do servidor detectado como 0 → horários errados a semana toda | alto | só aceita tick fresco; senão usa o último fuso bom guardado em `~/.turbo_ia_offset.json` |
| 3 | Troca de horário de verão dos EUA (mar/nov) com o painel ligado: fuso ficava velho → opera 1 h fora | alto | re-detecta o fuso a cada 10 min |
| 4 | Resultado lido antes do negócio de fechamento chegar ao histórico → contava só a comissão (perda falsa) | médio | espera o negócio de saída |
| 5 | Painel e IA chamando o MT5 ao mesmo tempo (o pacote não é seguro entre threads) | médio | todas as chamadas passam por um cadeado |
| 6 | Sem proteção contra spread anormal (notícia, abertura) | médio | pula a entrada se o spread > 30 % do stop |
| 7 | Freio diário "esquecia" as perdas do dia ao reiniciar o painel | médio | resultado do dia gravado em `~/.turbo_ia_dia.json` |
| 8 | Lote calculado pelo fechamento da vela, não pelo preço de execução (25/11: 5,2 % em vez de 5 %) | baixo | lote pelo bid/ask do momento; pula se o preço já passou do stop |
| 9 | Servidor aceitava Host externo (DNS rebinding poderia ler o saldo) | baixo | só atende 127.0.0.1/localhost |
| 10 | Número decimal num campo inteiro derrubava o início da IA | baixo | conversão tolerante |

Conferido e correto: só velas fechadas (sem olhar o futuro); entrada, hora, esticada, stop e alvo ao vivo iguais aos do
backtest; conta real bloqueada; a IA só mexe nas posições com o número mágico dela; SL/TP ficam no servidor da
corretora (protegem mesmo com o PC desligado); senha não aparece em registro.

Riscos que continuam: o registro de acerto mede pelas velas (sem spread/derrapagem reais — confira o lucro no MT5);
o spread de cada vela no MT5 é o mínimo da vela (backtest levemente otimista); o PC, o MT5 e a janela preta precisam
estar ligados nas janelas.

## Segurança

- O servidor só escuta em `127.0.0.1` e recusa POSTs de outras origens.
- Ordens em **conta real são bloqueadas**. Para liberar, conscientemente: `set TURBO_IA_ALLOW_REAL=1` antes de iniciar.
- A IA só fecha/gerencia posições com o seu número mágico (`260611`); suas outras operações não são tocadas.
- Nenhuma combinação de indicadores garante lucro. Rode em DEMO por semanas e compare o resultado antes de pensar em real.

## Arquivos

| Arquivo | O que faz |
| --- | --- |
| `indicators.py` | SuperTrend, LSMA, Keltner, SAR, ZigZag/CHoCH, EMA, RSI, ATR (sem dependências) |
| `analysis.py` | votação, modos, SL/TP |
| `broker.py` | `MT5Broker` (pacote oficial MetaTrader5) e `PaperBroker` (simulador) |
| `engine.py` | sessão da IA: laço, meta, limite de perda, proteção, saída por tempo |
| `forward.py` | registro de acerto em tempo real (teste em demo) |
| `backtest.py` | teste de acerto no histórico |
| `server.py` | servidor HTTP local + API (`/api/chart`, `/api/ia/start`, `/api/ia/stop`, `/api/order`, `/api/close`) |
| `static/index.html` | painel (gráfico TradingView Lightweight Charts) |

Testes: `python -m pytest tests/test_turbo_ia.py`.
