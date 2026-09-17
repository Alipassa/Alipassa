# GOLD AI ENGINE 3.0 — inteligência preditiva do ouro (XAU/USD)

> **Missão:** detectar notícias e movimentações no mercado financeiro, analisar o que pode acontecer com o ouro e
> **entrar no mercado antes que a notícia seja absorvida pelo preço** — com risco controlado e sem inventar vantagem
> onde não existe.

Um único mercado (XAU/USD), um único cérebro. O motor não explica por que o ouro subiu ou caiu: ele procura **o que está
começando a mudar** (dólar, juros reais, Fed, inflação, geopolítica, fluxo, COT, opções, técnico, sentimento, notícias) e
converte tudo em **SCORE (−100..+100) × PROBABILIDADE × CONFIANÇA**, com detecção de **pré-movimento**, filtro de
**3 confirmações independentes**, anti-spam, alertas no **Telegram**, execução no **MetaTrader 5** e registro/aprendizado
de cada previsão e de cada operação. A diretriz completa está em [docs/DIRETRIZ.md](docs/DIRETRIZ.md).

Python 3.10+, **sem dependências externas** (MetaTrader5 opcional, só no Windows).

## Como o sistema antecipa a notícia

```text
🌎 MUNDO ──► 📡 DATA ENGINE ── Yahoo (ouro, DXY, 10Y, Fed Funds, VIX…) · FRED (juros reais, breakeven) · CFTC (COT)
                              · RSS (FXStreet, Kitco, MarketWatch) · calendário de eventos · MetaTrader 5 (preço do broker)
                    ▼
             MARKET SNAPSHOT ── notícias interpretadas em 3 níveis (o que aconteceu → o que se esperava → surpresa)
                    ▼
             🧠 PREDICTION ENGINE ── score por fator · P(alta) P(baixa) P(lateral) · confiança · horizonte
                    ▼
             ESTÁGIO ── PRÉ-MOVIMENTO (fundamentos mudaram, preço ainda não) · CONFIRMAÇÃO · MOVIMENTO (tarde demais, não persegue)
                    ▼
        CADEIA DE RACIOCÍNIO (9 passos) + NÍVEL DE EVIDÊNCIA (1–4) + VANTAGEM ESTATÍSTICA ("NÃO SEI" = não envia)
                    ▼
      ⚠️ GOLD WATCH ──► ⚠️ GOLD PRE-MOVE ──► 🟢 GOLD SIGNAL (confirmado)        ou        🟡 SEM VANTAGEM
                    ▼
             DECISION ENGINE ──► TRADE PLAN (stop estrutural · alvo 1R–4R · viabilidade) ──► RISK ENGINE (capital → % fixo → lote)
                    ▼
             EXECUTION ENGINE ──► MT5 ──► confirmação real no broker ──► 🔄 TRADE MONITOR (MANTER · PROTEGER · REDUZIR · ESTENDER · ENCERRAR)
                    ▼
             RESULTADO ──► SQLite ──► PERFORMANCE ──► novo capital ──► novo lote ──► aprendizado (o que antecipou, com quantos minutos)
```

A entrada acontece no estágio **PRÉ-MOVIMENTO**: os fatores (e a notícia) já mudaram, o preço ainda não confirmou.
No estágio **MOVIMENTO** (preço já andou mais de 2 ATR) o motor **não persegue**. Um sinal direcional só sai com
≥ 3 famílias independentes alinhadas (macro, juros, dólar, fluxo, técnico, sentimento, geopolítica) e com vantagem
estatística declarada; caso contrário a resposta é "NÃO SEI" e nada é enviado.

## Uso rápido

```bash
python gold_ai_engine_v3.py demo                      # 7 cenários sintéticos → relatórios e sinais (Telegram em dry-run)
python gold_ai_engine_v3.py live --once               # Yahoo + FRED + CFTC + RSS → snapshot real → relatório
python gold_ai_engine_v3.py live --source mt5 --mode paper --send      # 🟢 24/7 com preço do MT5, operações simuladas, alertas no Telegram
python gold_ai_engine_v3.py status                    # capital, performance, operações abertas, aprendizado
python gold_ai_engine_v3.py stats                     # taxa de acerto, lead time, calibração, poder de cada fator, 1R/2R/3R
python gold_ai_engine_v3.py validate --csv xau_h1.csv --folds 4        # anti look-ahead + walk-forward + calibração + veredito
python gold_ai_engine_v3.py simulate --csv xau_h1.csv --walk-forward   # 1R/2R/3R/4R antes do stop, melhor saída, expectancy
python gold_ai_engine_v3.py backtest --symbol GC=F    # backtest H1 com histórico do Yahoo (~3 meses)
python gold_ai_engine_v3.py calibrate --min-n 30      # gera calibrator.json; o live passa a usá-lo (--calibrator)
python gold_ai_engine_v3.py event --actual 0.1 --dxy -0.3 --us10y -5 --real -4 --gold 0.4 --flow 0.3   # árvore pré-evento + cadeia pós-evento
python -m unittest -q                                 # testes (119)
```

`python -m gold_ai …` (pacote) e `python gold_ai_engine_v3.py …` (arquivo único) são equivalentes. O arquivo único é
gerado com `python tools/build_single_file.py` e é o que se copia para a máquina com o MT5. Os `.bat` em `scripts/`
rodam PAPER 24/7, LIVE em conta demo, resultados e testes com dois cliques.

## Modos de execução

| Modo | Comportamento |
| --- | --- |
| 🟢 `paper` (padrão) | tudo simulado com dados reais, capital virtual — **comece aqui** |
| 🟡 `authorize` | monta a operação, envia o plano ao Telegram e espera `--authorize` para a próxima entrada |
| 🟠 `semi-live` | entra por regras pré-autorizadas; encerramento com lucro decidido pelo monitor pede `/CLOSE CONFIRM` |
| 🔴 `live` | execução automática no MT5 dentro dos limites do `.env`; exige `--authorize` na linha de comando |

Comandos no Telegram (com `--send`): `/STOP` · `/PAUSE` · `/RESUME` · `/STATUS` · `/CLOSE` (exige `/CLOSE CONFIRM`).
Kill switch por arquivo: crie `STOP_TRADING` na pasta do robô. `TRADING_ENABLED=false` no `.env` tem o mesmo efeito.

Caminho recomendado antes de dinheiro real: PAPER com dados reais → dias/semanas → `stats` → `validate` → expectancy
positiva → `simulate` → AUTHORIZE → microvolume em SEMI-LIVE → LIVE.

## Regras que a IA não pode quebrar

1. O lote sai de **capital × risco % fixo × stop**; nunca da confiança. A confiança decide OPERA / NÃO OPERA.
2. A IA nunca aumenta o risco para recuperar perdas. Perda diária ≥ `MAX_DAILY_LOSS` → TRADING STOP até o dia seguinte;
   drawdown ≥ `MAX_DRAWDOWN` → idem.
3. Uma posição por vez em XAUUSD (memória + broker).
4. Ordem enviada ≠ ordem executada: só a posição **confirmada no broker** conta; divergência de volume/preço/SL/TP
   vira ⚠️ EXECUTION MISMATCH (corrige SL/TP; sem SL correto, encerra por segurança).
5. Tese invalidada fecha a operação mesmo com lucro, sem esperar o stop.

## Configuração (`.env`)

Copie `.env.example` para `.env` na pasta onde o robô roda.

| Chave | Função |
| --- | --- |
| `TOKEN_TELEGRAM`, `CHAT_ID` | bot e destinatário dos alertas (aceita também `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) |
| `MT5_PATH`, `MT5_SYMBOL`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` | terminal e conta do MetaTrader 5 (login/senha/servidor opcionais se o terminal já está logado) |
| `RISK_PER_TRADE`, `MAX_DAILY_LOSS`, `MAX_POSITIONS`, `MAX_LOT`, `MAX_SPREAD`, `MAX_SLIPPAGE`, `CONTRACT_SIZE` | gestão de risco (2.2) |
| `TRADING_ENABLED`, `MAX_DRAWDOWN`, `MIN_RR_TO_STRUCTURE` | kill switch e limites (3.0) |

## Módulos

| Módulo | Diretriz | Responsabilidade |
| --- | --- | --- |
| `gold_ai/config.py` | §19, §21, §27, §28, §37 | pesos dos fatores, famílias de confirmação, timeframes, horizontes, limiares |
| `gold_ai/models.py` | §2, §13, §36 | `MarketSnapshot`, `FactorScore`, `Assessment`, `Signal`, eventos, notícias |
| `gold_ai/factors.py` | §5–§12 | pontuação de cada fator (saturação `tanh`, foco em variações) e índice de risco sistêmico |
| `gold_ai/technical.py` | §15–§18 | EMA/RSI/MACD/ATR/ADX/Bollinger/VWAP por timeframe M1…W1, agregação, acumulação/distribuição |
| `gold_ai/premove.py` | §14, §16, §22, §26 | fundamentos × preço → estágio, pressão latente, risco de reversão |
| `gold_ai/events.py` | §32–§34 | calendário de risco, árvore de reação pré-evento, cadeia pós-evento |
| `gold_ai/evidence.py` | B, C, E | cadeia de raciocínio (9 passos), nível de evidência 1–4, vantagem estatística / "NÃO SEI" |
| `gold_ai/engine.py` | §19–§21, §36 | orquestração: score, probabilidades, confiança, horizonte, zona de atenção |
| `gold_ai/signals.py` | §27, §28, §37 | classificação, filtro de confirmações, `SignalGate` anti-spam |
| `gold_ai/telegram.py` | §23–§26 | WATCH / PRE-MOVE / SIGNAL, plano de trade, monitor, resultado; envio via Bot API |
| `gold_ai/memory.py` | §29, §30 | SQLite de previsões, decisões, operações; resolução com MFE/MAE; taxa de acerto; poder dos fatores |
| `gold_ai/data/` | A | `DataEngine`: Yahoo, FRED, CFTC, RSS (`RuleInterpreter`, LLM plugável via `NewsInterpreter`), calendário, `MT5Source` / `MT5Executor` |
| `gold_ai/trading.py` | 2.2 | `StopEngine`, `MaxProfitEngine`, simulador 1R–4R, estratégias de saída, `RiskLimits` / `RiskManager`, NÃO OPERAR |
| `gold_ai/monitor.py` | 2.3 | `Thesis`, `TradeMonitor`: TRADE / THESIS / EXIT SCORE, PROFIT POTENTIAL, trailing adaptativo |
| `gold_ai/execution.py` | H | `ExecutionEngine`: envio, confirmação no broker, EXECUTION MISMATCH, parciais, resultado por deals |
| `gold_ai/guard.py` | 3.0 | `PerformanceEngine` (capital → lote), `GuardLimits`, `KillSwitch`, `TelegramCommands` |
| `gold_ai/validation.py` | G | auditoria anti look-ahead, calibração isotônica, tabela previsto → observado, score por fator |
| `gold_ai/evaluation.py` | F, G | `HistoryFrame`, `Backtester`, `walk_forward`, precisão/recall/lead time/MFE/MAE, `validate` |
| `gold_ai/opportunity.py` | 3.0 | OPPORTUNITY ENGINE: capture rate, entry rate, atribuição por filtro, curva limiar × expectancy |
| `gold_ai/live_engine.py` | 3.0 | `LiveExecutionEngine`: monitor primeiro, depois nova decisão; retoma operações do SQLite; toda a vida da operação no Telegram |
| `gold_ai/cli.py` | — | `demo` · `run` · `live` · `status` · `stats` · `backtest` · `validate` · `simulate` · `calibrate` · `metrics` · `event` |

## Data Engine

| Fonte | Campos do snapshot |
| --- | --- |
| Yahoo `GC=F` / `XAUUSD=X` | candles M1…W1, preço, ATR, variação, fluxo agressor (proxy por volume) |
| Yahoo `DX-Y.NYB`, `^TNX`, `2YY=F`, `ZQ=F` | DXY e Δ%, 10Y e Δbp, 2Y, Δ probabilidade de corte (100 − preço do Fed Funds futuro) |
| FRED `DFII10`, `T10YIE`, `BAMLH0A0HYM2` | juros reais, breakeven, spread high yield |
| Yahoo `^VIX`, `^GSPC`, `SI=F`, `CL=F`, `BTC-USD`, `CNH=X` | risco sistêmico e intermercado |
| CFTC (Disaggregated, Futures Only, 088691) | managed money líquido, Δ semanal, percentil, commercials |
| RSS FXStreet · Kitco · MarketWatch | notícias interpretadas em 3 níveis, sentimento, índice geopolítico, releases (`CPI 2.8% vs 3.0%`) |
| Calendário JSON (`--calendar eventos.json`) | eventos futuros com consenso/anterior |
| MetaTrader 5 (`--source mt5`) | candles, bid/ask e tick volume do broker; execução |

Campo indisponível fica `None`: o motor marca o fator como indisponível e **reduz a confiança** em vez de inventar.

Para plugar outra fonte, implemente `gold_ai.sources.base.DataSource.snapshot()` devolvendo um `MarketSnapshot`. Para
interpretar notícias com um LLM, implemente `NewsInterpreter.interpret(item)` preenchendo `gold_impact` e `priced_in`.

## Validação e aprendizado

- **Anti look-ahead:** cada snapshot do backtest é reconstruído só com o passado; a auditoria varre snapshots e lista
  qualquer candle, notícia ou resultado futuro.
- **Walk-forward:** treina → testa → avança janela; parâmetros escolhidos só no treino. O veredito só sai de
  "INCONCLUSIVO" com ≥ 20 sinais resolvidos fora da amostra.
- **Lead time:** minutos entre o sinal e o momento em que o movimento ficou evidente — a medida de "antes da absorção".
- **Calibração:** Brier, ECE, tabela previsto → observado, mapa isotônico aplicado no `live`.
- **Opportunity Engine:** de N movimentos relevantes, quantos foram capturados antes de ficarem evidentes; qual regra
  bloqueou cada oportunidade e o que teria acontecido. Se o robô entra pouco, a resposta não é mais filtro.
- O `live` resolve as previsões pendentes com os candles reais a cada ciclo e retoma operações abertas ao reiniciar.

## Estrutura do repositório

```text
gold_ai/                 pacote (fonte da verdade)
gold_ai_engine_v3.py     arquivo único gerado por tools/build_single_file.py — leve este para a máquina do MT5
tools/build_single_file.py
tests/                   119 testes (stdlib unittest; rodam também com pytest)
scripts/                 rodar_live.bat · rodar_live_demo.bat · rodar_resultados.bat · verificar.bat
docs/DIRETRIZ.md         a diretriz de inteligência preditiva do ouro, seção a seção → módulo
.env.example             credenciais e limites
```

Depois de alterar o pacote: `python -m unittest -q && python tools/build_single_file.py`.

## Aviso

Motor probabilístico de apoio à decisão. Não garante resultados. Ordens reais só saem em `--mode live --authorize`
(ou `semi-live`), dentro dos limites do `.env`, e sempre depois de o PAPER ter provado estatística positiva.
