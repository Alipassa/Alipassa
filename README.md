# GOLD AI ENGINE

Centro Global de Inteligência do Ouro — motor probabilístico de antecipação para **XAU/USD**.

Implementa a [Diretriz de Inteligência Preditiva do Ouro](docs/DIRETRIZ.md): em vez de explicar
por que o ouro subiu ou caiu, o motor procura **o que está começando a mudar** (dólar, juros
reais, Fed, inflação, geopolítica, fluxo, COT, opções, sentimento, técnico) e converte tudo em
**SCORE (−100..+100) × PROBABILIDADE × CONFIANÇA**, com detecção de **pré-movimento**, **reversão**,
**risco sistêmico**, filtro de **3 confirmações independentes**, **anti-spam**, alertas para
**Telegram** e **registro/aprendizado** das previsões.

Python 3.10+, sem dependências externas.

## Uso rápido

```bash
python -m gold_ai demo                 # roda 7 cenários sintéticos e imprime relatórios + sinais (Telegram em dry-run)
python -m gold_ai demo --db demo.db    # idem, registrando as previsões no SQLite
python -m gold_ai stats --db demo.db   # taxa de acerto por sessão/direção/score/estágio + poder dos fatores
python -m gold_ai event --actual 0.1 --dxy -0.3 --us10y -5 --real -4 --gold 0.4 --flow 0.3   # árvore pré-evento + cadeia pós-evento (CPI)
python -m gold_ai run --once -v        # um ciclo do loop contínuo
python -m unittest -q                  # testes
```

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

## Conectando dados reais

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
