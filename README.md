# MARKET AI ENGINE 4.0

Cérebro único · múltiplos mercados · seleção dinâmica da melhor oportunidade. Nasceu como GOLD AI ENGINE
(motor probabilístico de antecipação para XAU/USD) e preserva integralmente os motores validados nas versões 2.1–3.0.

Implementa a [Diretriz de Inteligência Preditiva do Ouro](docs/DIRETRIZ.md): em vez de explicar
por que o ouro subiu ou caiu, o motor procura **o que está começando a mudar** (dólar, juros
reais, Fed, inflação, geopolítica, fluxo, COT, opções, sentimento, técnico) e converte tudo em
**SCORE (−100..+100) × PROBABILIDADE × CONFIANÇA**, com detecção de **pré-movimento**, **reversão**,
**risco sistêmico**, filtro de **3 confirmações independentes**, **anti-spam**, alertas para
**Telegram** e **registro/aprendizado** das previsões.

Python 3.10+, sem dependências externas.

## 4.0 — MARKET AI ENGINE

> "Analisar vários mercados simultaneamente e operar somente aquele que apresentar a melhor vantagem estatística disponível
> naquele momento, respeitando risco, correlação, qualidade dos dados e custo de execução." A IA não precisa operar ouro; precisa
> encontrar onde existe vantagem.

```text
XAUUSD ─┐
EURUSD ─┤
US500  ─┼──→ 📡 DATA (macro uma vez + candles por mercado) → 🧠 CÉREBRO ÚNICO → 🔥 OPPORTUNITY → 🏆 ASSET SELECTOR
USDJPY ─┤        → 📐 EXPOSIÇÃO/CORRELAÇÃO → RISK ENGINE (capital único) → TRADE ENGINE → MT5 → 🔄 MONITOR 24/7
WTI    ─┘
```

| Módulo | O que faz |
| --- | --- |
| `markets.py` | registro dos mercados (fase 1: EURUSD, US500, XAUUSD, USDJPY, WTI; fase 2: NAS100, GBPUSD; fase 3: BTCUSD) com o **sinal de cada fator** (dólar ↓ é + para XAUUSD/EURUSD e − para USDJPY; geopolítica ↑ é + para ouro e petróleo, − para EURUSD/US500/USDJPY; fator sem relação conhecida = 0, indisponível — nada de inventar edge), valor do ponto, spread típico, sessão, código COT próprio, correlações de referência |
| `engine.py` | o mesmo cérebro: os fatores macro são calculados uma vez e multiplicados pelo sinal do mercado; técnico e fluxo vêm dos candles do próprio mercado |
| `data/multi.py` | macro coletada uma vez; snapshot por mercado com candles/preço/ATR/fluxo próprios (Yahoo ou MT5), qualidade dos dados por mercado |
| `selector.py` | **MARKET OPPORTUNITY SCORE** = 25 % expectancy histórica (encolhida pela amostra) + 20 % probabilidade calibrada + 15 % score + 15 % pré‑movimento + 10 % regime + 5 % captura + 5 % execução + 5 % dados; **três dimensões separadas**: histórico · agora · **OPPORTUNITY DECAY** (estágio × ATR já percorrido × idade do sinal); **STATISTICAL CONFIDENCE** (HIGH/MEDIUM/LOW: 37 trades a +0.9R não vencem 487 a +0.42R); `PortfolioExposureEngine` com risco agregado e **correlacionado** (EURUSD BUY + GBPUSD BUY + XAUUSD BUY = a mesma aposta) |
| `market_engine.py` | um `LiveExecutionEngine` (3.0) por mercado com capital compartilhado; monitor de todas as posições antes de qualquer decisão; **o Asset Selector não cria entradas** — só ordena as que o Prediction/Opportunity Engine produziu; um ciclo, uma entrada (a melhor); os demais recebem veto de PRIORIDADE (registrado no Opportunity Engine) |
| `evaluation.validate_markets` | "qual mercado tem melhor expectativa fora da amostra?" — tabela por ativo (trades OOS, expectancy, ajustada, PF, win, capture, entry, confiança) ordenada pela expectancy **ajustada à amostra** |

Limites novos no `.env`: `MAX_TOTAL_OPEN_RISK`, `MAX_CORRELATED_RISK`, `MAX_PORTFOLIO_POSITIONS`, `MAX_ASSET_EXPOSURE`, `CORRELATION_THRESHOLD`
e `MT5_SYMBOL_<ATIVO>` para símbolos do broker. Nenhum filtro de entrada novo: os vetos do 4.0 são exclusivamente de portfólio e de prioridade.

```bash
python market_ai_engine_v4.py markets                                                          # ranking agora (não opera)
python market_ai_engine_v4.py live --markets EURUSD,US500,XAUUSD,USDJPY,WTI --source mt5 --mode paper --send
python market_ai_engine_v4.py validate --markets EURUSD,US500,XAUUSD,USDJPY,WTI --csv-dir dados/   # <SYMBOL>_h1.csv por mercado
python market_ai_engine_v4.py stats                                                            # inclui resultado por ativo
```

## 🚨 LIVE EDGE — o teste definitivo da 4.0

Uma vez por dia (e sob demanda com `edge` ou `/EDGE` no Telegram) o sistema produz, **a partir do que viveu**, a tabela por mercado:

```text
╔════════════════════════════════════════════╗
║           MARKET AI — LIVE EDGE            ║
╠════════════════════════════════════════════╣
║ EURUSD                                     ║
║ OOS Trades: 427                            ║
║ Expectancy: +0.34R  (ajustada +0.32R)      ║
║ Probabilidade calibrada: 67% (declarada 71%)║
║ Capture Rate: 38%  · Entry Rate: 14%       ║
║ Status: 🟢  edge confirmado (HIGH, LB +0.21R)║
╚════════════════════════════════════════════╝
```

- **Fora da amostra por construção**: toda previsão foi gravada antes do resultado.
- **Probabilidade calibrada** = taxa de acerto observada das previsões direcionais, ao lado da declarada.
- **Status** vem da confiança estatística (amostra ≥ 30, nível HIGH/MEDIUM, limite inferior positivo), não da expectancy bruta.
- Cada relatório fica guardado em `edge_reports`; `edge` mostra a evolução diária da expectancy ajustada por mercado.

Não precisamos acreditar que EURUSD é melhor. Os dados mostram.

## Entrypoint único

Existem exatamente **duas** formas equivalentes de executar, ambas na versão 4.0:

| Forma | Quando usar |
| --- | --- |
| `python -m gold_ai …` | trabalhando no repositório (pacote `gold_ai/`) |
| `python market_ai_engine_v4.py …` | arquivo único gerado por `python tools/build_single_file.py` a partir do pacote |

Bundles antigos (`gold_ai_engine.py`, `gold_ai_engine_v2.py`, `gold_ai_engine_v3.py`) **foram removidos** para impedir a execução
acidental de uma versão errada. Se algum deles ainda existir na sua máquina, apague-o. O número da versão está em
`gold_ai.__version__` e no cabeçalho do bundle, e é impresso no início de `live`.

## O que mudou na 2.0

| Componente | Situação |
| --- | --- |
| 📡 Data Engine (mercado real → coletor → normalização → `MarketSnapshot`) | 🟢 `gold_ai/data/` — Yahoo (XAU, DXY, ^TNX, ZQ=F, VIX, S&P, prata, petróleo, BTC, USD/CNH), FRED (DFII10, T10YIE, HY OAS), CFTC (COT), RSS + calendário |
| MetaTrader 5 como fonte primária de preço | 🟢 `gold_ai/data/mt5.py` (`MT5Source`) — candles M1…W1, bid/ask, tick volume |
| Execução no MT5 | 🟢 `MT5Executor` — **só envia ordem com `--authorize`**; por padrão simula |
| Cadeia de raciocínio (9 passos: aconteceu → esperava → surpresa → juros → dólar → ouro → fluxo → pressão → veredito) | 🟢 `gold_ai/evidence.py` |
| Nível de evidência 1–4 | 🟢 `EvidenceLevel` |
| "NÃO SEI" — sem vantagem estatística, não envia | 🟢 `edge_status` (prob. < 55 %, confiança < 50 ou \|score\| < 25) |
| Três mensagens: ⚠️ GOLD WATCH · 🚨 GOLD PRE-MOVE · 🟢 GOLD SIGNAL (PRE-MOVE CONFIRMADO) | 🟢 `gold_ai/telegram.py` |
| Precisão, recall, MFE, MAE, lead time, ⏱️ GOLD LEAD SCORE | 🟢 `gold_ai/evaluation.py` |
| Backtest + walk-forward (calibra no treino, avalia fora da amostra) | 🟢 `Backtester`, `walk_forward` |
| Registro previsão → resultado real → aprendizado | 🟢 `PredictionMemory.metrics()` |

### Fluxo 2.0

```text
🌎 MUNDO ──► 📡 DATA ENGINE (Yahoo · FRED · CFTC · RSS · calendário · MT5)
                    │  normalização, falhas isoladas por fonte, cache
                    ▼
             MARKET SNAPSHOT
                    ▼
             🧠 GOLD AI ENGINE ──► score · probabilidade · confiança
                    ▼
        CADEIA DE RACIOCÍNIO (9 passos) + NÍVEL DE EVIDÊNCIA + VANTAGEM ESTATÍSTICA
                    ▼
      ⚠️ WATCH ──► 🚨 PRE-MOVE ──► 🟢 SIGNAL (confirmado)      ou      🟡 NÃO SEI (não envia)
                    ▼
             📲 TELEGRAM ──► 📚 SQLite ──► RESULTADO REAL ──► 📊 precisão · recall · MFE/MAE · lead time
```

## 2.1 — VALIDATION ENGINE

A 2.1 não adiciona funcionalidades de sinal: ela **prova ou refuta** que o 2.0 antecipa o XAU/USD.

| Item | Onde | O que faz |
| --- | --- | --- |
| 1. Backtest temporal rigoroso | `validation.lookahead_audit`, `HistoryFrame.snapshot_at` | cada snapshot é reconstruído só com o passado; a auditoria varre snapshots e lista qualquer candle/notícia/resultado futuro |
| 2. Walk-forward | `evaluation.walk_forward(mode="rolling")` | treina → testa → avança janela → treina de novo → testa; parâmetros escolhidos só no treino |
| 3. Lead time | `evaluation.evaluate`, `memory.lead_time_stats` | minutos entre o sinal e o momento em que o movimento ficou evidente |
| 4. MFE / MAE | `evaluation.evaluate`, `memory.auto_resolve` | excursão favorável e adversa após cada previsão |
| 5. Probabilidade calibrada | `validation.calibration_table`, `IsotonicCalibrator`, comando `calibrate` | Brier, ECE, tabela previsto→observado e um mapa isotônico que o motor aplica (`live --calibrator`) |
| 6. Score por fator | `validation.factor_scoreboard` | taxa de acerto quando cada fator (DXY, juros reais, COT…) e cada indicador (RSI, VWAP, EMA, MACD) apontava na direção do sinal, com barras e *lift* |
| Painel | `report.render_dashboard` | REGIME · SCORE · PROBABILIDADE · CONFIANÇA · PRE-MOVE · LEAD TIME · fatores · STATUS · DECISÃO |
| Loop de aprendizado | `live` | a cada ciclo resolve as previsões pendentes com os candles reais antes de prever de novo |

```bash
python -m gold_ai validate --csv xau_h1.csv --folds 4          # relatório completo + VEREDITO
python -m gold_ai validate --symbol GC=F --mode anchored       # histórico Yahoo (~3 meses H1)
python -m gold_ai live --interval 300 --send                   # roda dias/semanas; resolve e aprende sozinho
python -m gold_ai stats                                        # lead time, calibração, score por fator do que foi vivido
python -m gold_ai calibrate --min-n 30                         # gera calibrator.json; `live` passa a usá-lo
```

O veredito só sai de "INCONCLUSIVO" com pelo menos 20 sinais resolvidos fora da amostra. A pergunta
que ele responde: *quando o motor diz PRE-MOVE, o XAU/USD anda na direção prevista, com quantos
minutos de antecedência, e a probabilidade declarada bate com a observada?*

## 2.2 — TRADE SIMULATOR · STOP ENGINE · MAX PROFIT ENGINE · GESTÃO DE RISCO

Prova a capacidade **operacional** antes de qualquer ordem real. Cada sinal vira uma operação simulada:

```text
PRE-MOVE → DIREÇÃO → ENTRADA → STOP → 1R → 2R → 3R → TRAILING → RESULTADO
```

| Módulo | O que faz |
| --- | --- |
| `trading.StopEngine` | stop na invalidação estrutural, limitado a [0.6, 2.5] ATR |
| `trading.simulate_trade` / `excursion_profile` | simula candle a candle (conservador: stop e alvo no mesmo candle = stop); até onde o preço foi antes do stop inicial |
| `trading.STRATEGIES` | saídas comparadas: 1R · 2R · 3R · 4R · trailing (1R após 1R) · parcial 50 % em 2R + trailing |
| `trading.r_stats` | distribuição (stop antes de 1R / 1R / 2R / 3R / +3R e continuou), % que atinge cada R antes do stop, expectancy em R, win rate, profit factor por estratégia, melhor estratégia |
| `trading.MaxProfitEngine` | alvo estatístico (MFE mediana em R), estrutural (próximo S/R), de volatilidade (ATR × √horizonte), por risco/retorno; probabilidade por R do histórico; **TP ótimo** — hipótese inicial 3R até haver ≥ 20 operações |
| `trading.RiskLimits` / `RiskManager` | `RISK_PER_TRADE`, `MAX_DAILY_LOSS`, `MAX_POSITIONS`, `MAX_LOT`, `MAX_SPREAD`, `MAX_SLIPPAGE` no `.env`; lote sai do risco fixo, **nunca da confiança** |
| `trading.no_trade_check` | 🟡 NÃO OPERAR quando não há vantagem, confiança < 60, evidência < nível 2, ≥ 2 fatores contra a direção, estágio 3 ou spread alto |
| `trading.PositionManager` | modos 🟡 PAPER (simula) · 🟠 AUTHORIZE (prepara e espera) · 🔴 LIVE (envia ao MT5, exige `--authorize`) |
| `memory.auto_resolve_trades` | no `live`, fecha as operações simuladas com candles reais e alimenta o Max Profit Engine |

```bash
python -m gold_ai simulate --csv xau_h1.csv                 # 1R/2R/3R antes do stop + melhor saída (backtest)
python -m gold_ai simulate --symbol GC=F --walk-forward     # idem fora da amostra
python -m gold_ai live --mode paper --equity 10000 --send   # operações simuladas com dados reais
python -m gold_ai live --source mt5 --mode authorize        # prepara a ordem e espera
python -m gold_ai live --source mt5 --mode live --authorize # envia ao MT5 dentro dos limites do .env
python -m gold_ai stats                                     # itens 1–15: sinais, win rate, lead, MFE/MAE, Brier, ECE, fatores, 1R/2R/3R, expectancy
```

Sequência: GOLD AI 2.1 → VALIDATION → TRADE SIMULATOR → RESULTADO ESTATÍSTICO → 3.0 → MT5 → corretora.

## 2.3 — GOLD TRADE MONITOR + ADAPTIVE EXIT ENGINE

> **Regra central:** a abertura de uma operação não encerra o processo de análise. Enquanto existir
> posição aberta, o motor continua recebendo mercado, notícias, macro, fluxo e técnico, compara o
> cenário atual com a tese original e decide continuamente entre MANTER, PROTEGER, REDUZIR, ESTENDER
> ou ENCERRAR.

```text
OPERAÇÃO ABERTA → GOLD TRADE MONITOR → NOVO SCORE → COMPARAR COM TESE ORIGINAL
        → 🟢 MANTER (trailing) · 🟡 PROTEGER (parcial 50 % + zero a zero) · 🟠 REDUZIR · 🟢 ESTENDER (4R/5R) · 🔴 ENCERRAR (tese invalidada)
```

| Indicador | Significado |
| --- | --- |
| **TRADE SCORE** (−100..+100) | estado atual do mercado, assinado na direção da operação |
| **THESIS SCORE** (0..100) | quanto dos pilares da tese original (fatores alinhados na entrada) ainda permanece válido |
| **EXIT SCORE** (0..100) | necessidade de encerrar: deterioração da tese, score contra, queda vs entrada, reversão, risco sistêmico, evento próximo, pré‑movimento contrário |
| **PROFIT POTENTIAL** (0..100) | espaço restante: distância ao próximo nível estrutural, força do cenário, probabilidade condicional do próximo R no histórico |

- `monitor.Thesis` fotografa score e pilares na entrada; `monitor.TradeMonitor` reavalia a cada ciclo e verifica stop/trailing candle a candle.
- **Tese invalidada fecha mesmo com lucro**, sem esperar o stop. Cenário mais forte que a tese com potencial alto estende a busca para 4R/5R.
- Cada leitura vai para a tabela `trade_monitor`; a evolução do score de cada operação fica registrada (`render_evolution`).
- Após um fechamento antecipado a operação continua acompanhada até o horizonte para medir o que ficou na mesa; `stats` mostra o **aprendizado de saída**: qual queda do score realmente indicava sair.
- No backtest e no walk-forward a saída adaptativa entra como estratégia `adaptive` ao lado de 1R/2R/3R/4R/trailing.
- O `live` retoma operações abertas do SQLite ao reiniciar.

Sequência até o 3.0: 2.1 provou a previsão → 2.2 provou a operação → 2.3 prova o gerenciamento → 3.0 execução real
(PAPER → BACKTEST → WALK-FORWARD → LIVE SEM ORDEM → AUTHORIZE → LIVE).

## 3.0 — LIVE EXECUTION ENGINE

Primeira versão preparada para execução autônoma, **nascendo em PAPER**. O núcleo preditivo (2.1–2.3) não muda;
o 3.0 acrescenta o ciclo completo decisão → execução → confirmação → gestão → resultado → capital → novo lote.

```text
DADOS → SNAPSHOT → 🧠 PREDICTOR → PRE-MOVE → DECISION ENGINE → TRADE PLAN → RISK ENGINE → POSITION SIZE
→ MT5 EXECUTOR → BROKER → CONFIRMAÇÃO (ticket · preço · SL · TP reais) → 🔄 TRADE MONITOR
→ MANTER / PROTEGER / REDUZIR / ESTENDER / ENCERRAR → RESULTADO → SQLITE → PERFORMANCE → NOVO CAPITAL → PRÓXIMO
```

| Módulo | Função |
| --- | --- |
| `execution.ExecutionEngine` | envia a ordem e **só considera executada após confirmar a posição no broker**; compara volume, preço, SL e TP com o pedido → ⚠️ EXECUTION MISMATCH (corrige SL/TP; sem SL correto, encerra por segurança); parciais, modificação de SL/TP, resultado por deals do histórico |
| `guard.PerformanceEngine` | capital → risco financeiro (`RISK_PER_TRADE` %) → lote. O percentual nunca muda; o valor cresce com o capital. Em LIVE o capital é sincronizado com o broker |
| `guard.size_lots` | CAPITAL + RISCO + STOP + CONTRATO. Nunca confiança |
| `guard.GuardLimits` | + `MAX_DRAWDOWN`, `MIN_RR_TO_STRUCTURE`; perda diária ≥ `MAX_DAILY_LOSS` → 🚨 TRADING STOP até o dia seguinte |
| `guard.KillSwitch` | `TRADING_ENABLED=false`, arquivo `STOP_TRADING`, `/STOP`, `/PAUSE`, `/RESUME` |
| `guard.TelegramCommands` | `/STOP` · `/PAUSE` · `/RESUME` · `/STATUS` · `/CLOSE` (exige `/CLOSE CONFIRM`) |
| `trading.StopEngine` (3.0) | stop inteligente: invalidação, suporte/resistência, candle anterior H1, banda VWAP, ATR — sempre em [0.6, 2.5] ATR |
| `trading.MaxProfitEngine` (3.0) | viabilidade do alvo: resistência/suporte forte (H4/D1) antes de `MIN_RR_TO_STRUCTURE` → 🟡 NÃO OPERAR |
| `monitor.adaptive_trail_r` | trailing inteligente: mercado forte → mais largo; perdendo força → mais apertado |
| `live_engine.LiveExecutionEngine` | os três cérebros num ciclo: monitor primeiro, depois nova decisão; uma posição por ativo (memória + broker); retoma operações do SQLite; Telegram com toda a vida da operação (entrada, monitor, proteção, cenário alterado, resultado com previsão correta e lead time) |

### Modos

| Modo | Comportamento |
| --- | --- |
| 🟢 `paper` (padrão) | tudo simulado, capital virtual |
| 🟡 `authorize` | monta a operação, envia o plano e espera `--authorize` para a próxima entrada |
| 🟠 `semi-live` | entra por regras pré-autorizadas; encerramento com lucro por decisão do monitor pede `/CLOSE CONFIRM` (stop vai ao zero a zero enquanto espera) |
| 🔴 `live` | execução totalmente automática; exige `--authorize` na linha de comando |

### Regras fundamentais (na diretriz)

1. A IA nunca aumenta o risco percentual para recuperar perdas: −1R, −1R, −1R não vira uma operação de 3R.
2. Nenhuma nova posição enquanto existir posição ativa em XAUUSD.
3. O lote não depende da confiança; a confiança decide OPERA / NÃO OPERA.
4. Ordem enviada ≠ ordem executada: só a posição confirmada no broker conta.

```bash
python -m gold_ai live --source mt5 --mode paper --send            # 🟢 começa aqui
python -m gold_ai live --source mt5 --mode authorize --send        # 🟡 plano + espera; --authorize libera uma entrada
python -m gold_ai live --source mt5 --mode semi-live --send        # 🟠 microvolume com confirmação nas ações críticas
python -m gold_ai live --source mt5 --mode live --authorize --send # 🔴 automático dentro dos limites do .env
python -m gold_ai status                                           # capital, performance, posições, aprendizado
touch STOP_TRADING                                                 # kill switch por arquivo
```

Caminho recomendado: PAPER → estatística positiva (`stats`, `validate`, `simulate`) → AUTHORIZE → microvolume em SEMI-LIVE → LIVE.

## OPPORTUNITY ENGINE — medir antes de mudar regras

Sem regras novas. O 3.0 passa a responder, no PAPER real e no backtest:

| Métrica | Pergunta | Onde |
| --- | --- | --- |
| 🔥 **OPPORTUNITY CAPTURE RATE** | de N movimentos relevantes do período, quantos a IA capturou com uma entrada antes de ficarem evidentes? | `stats`, `simulate`, `validate` (por fold OOS) |
| **ENTRY RATE** | de N oportunidades analisadas, quantas viraram entrada? Abaixo de 5 % (ou captura < 25 %) → ⚠️ OVERFILTER | idem |
| **Atribuição por filtro** | qual regra bloqueou cada oportunidade (sem vantagem, confiança, evidência, conflito, viabilidade, kill switch, posição aberta…) e **o que teria acontecido** com a hipótese 3R/stop 1.2 ATR? Regra com expectancy hipotética > +0.2R em ≥ 10 casos é marcada como "regra cara" | `stats` (com preços gravados pelo `live`) |
| **Curva limiar × expectancy** | Score ≥ 50/60/70/80 → entradas e expectancy; a região ideal é a de maior expectancy com volume suficiente, não o score mais alto | `stats`, `simulate`, `backtest` |

O `live` grava cada decisão (`decisions`) e os preços (`prices`), e resolve sozinho o resultado hipotético das
oportunidades bloqueadas. Se o PAPER entrar pouco, a resposta não é mais filtro: é ler a atribuição e reduzir o peso
da regra que elimina oportunidades lucrativas.

### Ordem definitiva antes de dinheiro real

1. PAPER + dados reais → 2. dias/semanas → 3. `stats` → 4. `validate` → 5. expectancy → 6. Entry Rate → 7. Opportunity Capture
→ 8. 3R / trailing / adaptive → 9. AUTHORIZE → 10. só então LIVE.

## Uso rápido

```bash
python -m gold_ai demo                 # roda 7 cenários sintéticos e imprime relatórios + sinais (Telegram em dry-run)
python -m gold_ai demo --db demo.db    # idem, registrando as previsões no SQLite
python -m gold_ai stats --db demo.db   # taxa de acerto por sessão/direção/score/estágio + poder dos fatores
python -m gold_ai event --actual 0.1 --dxy -0.3 --us10y -5 --real -4 --gold 0.4 --flow 0.3   # árvore pré-evento + cadeia pós-evento (CPI)
python -m gold_ai run --once -v        # um ciclo do loop contínuo (cenário sintético)
python -m unittest -q                  # testes

# ---- DADOS REAIS (2.0) ----
python -m gold_ai live --once                      # Yahoo + FRED + CFTC + RSS → snapshot → relatório (+ cobertura das fontes)
python -m gold_ai live --interval 300 --send       # loop com envio ao Telegram (.env)
python -m gold_ai live --source mt5 --once         # preço/candles do MetaTrader 5 (MT5_PATH no .env) + demais camadas web
python -m gold_ai live --source mt5 --execute      # gera plano de ordem SIMULADO
python -m gold_ai live --source mt5 --execute --authorize --volume 0.01   # envia ordens de verdade (sua autorização explícita)
python -m gold_ai backtest --symbol GC=F           # backtest H1 (histórico Yahoo, ~3 meses)
python -m gold_ai backtest --csv xau_h1.csv --walk-forward --folds 4      # walk-forward fora da amostra
python -m gold_ai metrics --db gold_ai.db --path-csv precos.csv --threshold 9   # previsões gravadas × preço real
```

Credenciais: copie `.env.example` para `.env` (nunca versionado) com `TOKEN_TELEGRAM`, `CHAT_ID`, `MT5_PATH`.

Para enviar de fato ao Telegram, exporte `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` e use `--send`.

## Fluxo de um ciclo

```text
MarketSnapshot ──► factors.py ──► score por fator (limitado ao peso)
                       │
                       ├─► technical.py  (EMA/RSI/MACD/ATR/ADX/BB/VWAP × M1…W1)
                       ▼
                 engine.py ──► GOLD AI SCORE ──► P(alta) P(baixa) P(lateral) ──► confiança
                       │
                       ├─► premove.py   estágio: PRÉ-MOVIMENTO / CONFIRMAÇÃO / MOVIMENTO + risco de reversão
                       ├─► events.py    evento próximo, árvore de reação, cadeia pós-evento
                       ├─► report.py    saída interna (§36)
                       ▼
                 signals.py ──► classificação + ≥3 confirmações + anti-spam ──► Signal
                       │
                       ├─► telegram.py  🚨 BUY/SELL · ⚠️ PRE-MOVE · 🔄 REVERSAL · 🚨 SYSTEMIC RISK
                       └─► memory.py    registro → resultado → taxa de acerto → poder dos fatores
```

## Módulos

| Módulo | Diretriz | Responsabilidade |
| --- | --- | --- |
| `gold_ai/config.py` | §19, §21, §27, §28, §37 | pesos, famílias de confirmação, timeframes, horizontes, limiares |
| `gold_ai/models.py` | §2, §13, §36 | `MarketSnapshot`, `FactorScore`, `Assessment`, `Signal`, eventos, notícias |
| `gold_ai/factors.py` | §5–§12 | pontuação de cada fator (saturação `tanh`, foco em variações) e índice de risco sistêmico |
| `gold_ai/technical.py` | §15–§18 | indicadores, leitura por timeframe, agregação multi-timeframe, acumulação/distribuição |
| `gold_ai/premove.py` | §14, §16, §22, §26 | fundamentos × preço → estágio, pressão latente, risco de reversão |
| `gold_ai/events.py` | §32–§34 | calendário de risco, árvore de reação pré-evento, cadeia pós-evento |
| `gold_ai/engine.py` | §19–§21, §36 | orquestração: score, probabilidades, confiança, horizonte, zona de atenção |
| `gold_ai/signals.py` | §27, §28, §37 | classificação, filtro de confirmações, `SignalGate` anti-spam |
| `gold_ai/telegram.py` | §23–§26 | formatação dos alertas e envio via Bot API |
| `gold_ai/memory.py` | §29, §30 | SQLite de previsões, resolução com MFE/MAE, taxa de acerto, poder dos fatores |
| `gold_ai/sources/` | §3 | interface `DataSource` e `SampleSource` (cenários sintéticos) |

## Data Engine

| Fonte | Campos do snapshot | Observação |
| --- | --- | --- |
| Yahoo `GC=F`/`XAUUSD=X` | candles M1…W1, preço, ATR, variação, fluxo agressor (proxy por volume), volume/média | H4 é reamostrado de H1 |
| Yahoo `DX-Y.NYB`, `CNH=X` | DXY, Δ%, USD/CNH | janela configurável (padrão 60 min) |
| Yahoo `^TNX`, `2YY=F` | 10Y, Δbp, 2Y | |
| Yahoo `ZQ=F` | Δ prob. de corte (taxa implícita = 100 − preço) | aproximação de FedWatch |
| FRED `DFII10`, `T10YIE`, `BAMLH0A0HYM2` | juros reais, breakeven, HY OAS | diário; intraday = nominal − breakeven |
| Yahoo `^VIX`, `^GSPC`, `SI=F`, `CL=F`, `BTC-USD` | risco sistêmico e intermercado | |
| CFTC Socrata `72hh-3qpy` (088691) | managed money líquido, Δ semanal, percentil, commercials | |
| RSS (FXStreet, Kitco, MarketWatch) | notícias interpretadas em 3 níveis, sentimento, índice geopolítico, releases (`CPI 2.8% vs 3.0%`) | interpretador por regras; LLM plugável via `NewsInterpreter` |
| Calendário JSON | eventos futuros com consenso/anterior | `--calendar eventos.json` |
| MetaTrader 5 | candles/preço/tick volume do broker | Windows, `pip install MetaTrader5` |

Sem ETF flows e open interest gratuitos: esses campos ficam `None` e o motor reduz a confiança.

## Conectando outras fontes

Implemente `gold_ai.sources.base.DataSource.snapshot()` devolvendo um `MarketSnapshot`. Cada campo
é opcional: o motor marca o fator como indisponível e reduz a confiança em vez de falhar. Fontes
típicas: MT5 (candles XAU/USD, DXY), FRED/Treasury (yields, TIPS), CME FedWatch (probabilidade de
corte), CFTC (COT), fluxos de ETFs, calendário econômico, feed de notícias com interpretação
(`NewsItem.gold_impact` e `priced_in` podem vir de um LLM ou de regras).

## Anti-spam e filtro de sinais

Um sinal direcional só é emitido com **≥ 3 famílias independentes** alinhadas (macro, juros,
dólar, fluxo, técnico, sentimento, geopolítica) e nunca no **estágio 3** (preço já se moveu mais de
2 ATR). O `SignalGate` libera envio apenas por: novo evento, mudança de score ≥ 20, mudança de
direção, surgimento de pré-movimento, confirmação de movimento, reversão ou risco excepcional.

## Aprendizado

```python
from gold_ai.memory import PredictionMemory
mem = PredictionMemory("gold_ai.db")
pid = mem.record(assessment, "GOLD BUY")
# depois, com o caminho real do preço [(datetime, preço), ...] e um limiar (ex.: 1 ATR):
outcome = mem.resolve(pid, path, threshold=atr)
mem.accuracy("sessao"); mem.accuracy("score_bucket"); mem.factor_power()
```

## Aviso

Motor probabilístico de apoio à decisão. Não garante resultados e não executa ordens: a
integração com MT5 deve converter sinais em operações apenas sob autorização explícita.
