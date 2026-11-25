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

Variáveis opcionais (as mesmas do resto do projeto): `MT5_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`.

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
