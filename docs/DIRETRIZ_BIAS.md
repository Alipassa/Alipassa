# DIRETRIZ OPERACIONAL — IA PREDITIVA DO OURO (GOLD BIAS ENGINE)

**Versão 1.0** · Sistema de Inteligência de Mercado, Notícias e Previsão Direcional do Ouro · implementado em `gold_ai/bias.py`
e no comando `bias`.

> **"Leia o mercado inteiro antes de formar uma opinião sobre o ouro."**
> DECISÃO = MACRO + NOTÍCIAS + FLUXO + SENTIMENTO + TÉCNICO + CONTEXTO
> INFORMAR → ANALISAR → CRUZAR → CALCULAR → EXPLICAR → ALERTAR → APRENDER

O GOLD BIAS ENGINE **não opera**. Ele responde continuamente "qual o viés provável do ouro, com que confiança e por quê?"
e envia o resultado ao Telegram. Usa as mesmas fontes do motor de execução: **preço da corretora via MT5** + camadas macro
do DataEngine (Yahoo, FRED, CFTC, RSS, calendário) e o mesmo `TelegramSender` (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` do `.env`).

```bash
python market_ai_engine_v6.py bias --source mt5 --send                    # monitor 24/7: só envia quando algo muda
python market_ai_engine_v6.py bias --mode manha --send                    # relatório da manhã
python market_ai_engine_v6.py bias --mode fechamento --send               # fechamento: previsão × resultado do dia
python market_ai_engine_v6.py bias --mode stats                           # acerto por horizonte/fator/hora + pesos sugeridos
python market_ai_engine_v6.py bias --source sample --scenario venda --mode relatorio   # demonstração sem dados reais
```

Sem MT5 disponível o comando continua com os dados web (`--source web`). `--interval` define o ciclo (padrão 300 s).
Previsões ficam em `dados/gold_bias.db`; o estado anti-repetição em `dados/gold_bias_state.json`.

## Mapa diretriz → código

| § | Diretriz | Implementação |
| --- | --- | --- |
| 1–2 | Missão; nunca uma variável isolada | `GoldBiasEngine.total`: score normalizado pelos fatores disponíveis e **encolhido quando a cobertura < 70 %** — só o técnico não gera viés |
| 3 | Ouro: preço, volatilidade, suportes, resistências, rompimentos | candles MT5 (M1…W1), `technical.analyze_multi_timeframe`, `bias_structure` |
| 4 | Dólar (DXY, USD/CNH) | `factors.score_dolar` (peso 15) |
| 5 | Juros, Treasury, **juros reais (peso elevado)** | `factors.score_juros_reais` (peso 20); `us2y_change_bp` no snapshot |
| 6 | FED: HAWKISH / DOVISH / NEUTRO e mudança de discurso | `factors.score_fed` (peso 15), `bias_fed_stance`; mudança vs. leitura anterior (`fed_shift`) dispara alerta |
| 7 | Inflação: DADO × EXPECTATIVA × ANTERIOR | `bias_score_inflacao`: surpresa do snapshot ou do CPI/PCE/PPI divulgado (peso 10) |
| 8 | Emprego: EMPREGOS → INFLAÇÃO → FED → JUROS → DÓLAR → OURO | `bias_score_emprego`: `employment_surprise_sigma`, `jobless_claims_change_pct`, NFP/desemprego/claims divulgados (peso 5) |
| 9 | Indicadores; cenário econômico | `bias_economy`: crescimento forte · moderado · desaceleração · recessão provável (`economy_momentum` ou GDP/ISM/PMI/varejo) |
| 10–11 | Bancos centrais e ETFs; divergência preço × fluxo | `bias_score_fluxo` (ETFs, BCs, agressão, OI, COT; peso 10); divergência em `contradictions` |
| 12–13 | China e Índia | `bias_score_china_india` (`china_demand` 65 %, `india_demand` 35 %; peso 5) |
| 14 | Petróleo → inflação → FED | `oil_read` no relatório |
| 15, 17 | Bolsas, VIX, cripto: risk-on × risk-off | `bias_risk_mode` |
| 16 | Geopolítica: BAIXO · MODERADO · ALTO · EXTREMO | `factors.score_geopolitica` (peso 10), `bias_geo_level`; EXTREMO dispara alerta |
| 18–19 | Técnico multi-timeframe; HH+HL / LH+LL | `score_tecnico` (peso 10), `bias_structure` por H1/H4/D1, VWAP, EMA 9/21 |
| 20 | Notícias: impacto −3..+3 × IMPORTÂNCIA × CREDIBILIDADE × RECÊNCIA | `bias_score_news` (meia-vida 3 h); cada notícia alimenta o fator da sua categoria (`bias_blend_news`, 25 %) |
| 21 | Filtro de fontes; rumores valem menos | `BIAS_SOURCE_TIERS`, `BIAS_RUMOR_RE` (credibilidade × 0,5) |
| 22 | EVENTO DE ALTO IMPACTO: expectativa, anterior, impacto, volatilidade | `bias_upcoming_event`, `format_bias_event_warning` (até 60 min antes, uma vez por evento) |
| 23 | GOLD BIAS SCORE com pesos | `BIAS_WEIGHTS` = dólar 15 · juros reais 20 · FED 15 · inflação 10 · emprego 5 · geopolítica 10 · fluxo 10 · China/Índia 5 · técnico 10 |
| 24 | ≥+70 FORTE ALTA · +40 ALTA · ±39 NEUTRO · −40 BAIXA · ≤−70 FORTE BAIXA | `bias_classify` |
| 25 | Próximas horas · próximo dia · próximos 5 dias | `horizon_scores`: M5–H1 + notícias · H4/D1 + score · macro + D1/W1 |
| 26 | Confiança 0–100 % | `confidence`: concordância ponderada + cobertura + força; −8 por contradição, −12 com evento a ≤ 90 min, teto 55 % se lateral, **máximo 90 %** |
| 27 | Detecção de contradições | `contradictions`: técnico × macro, dólar × juros, preço × ETFs, notícia × preço, risk-off sem proteção, COT extremo |
| 28, 33 | Resumo executivo / formato Telegram | `format_bias_message` |
| 29 | "Minha leitura atual é de … porque …" | `GoldBiasEngine.opinion` |
| 30 | 🚨 GOLD ALERT | `format_bias_alert` (salto de score ≥ 25, notícia forte e crível, FED mudou, geopolítica EXTREMA, rompimento) |
| 31 | 🚨 REVERSÃO DE CENÁRIO | `format_bias_reversal` (ALTA ↔ BAIXA) |
| 32 | Manhã · atualizações · alertas · fechamento | `--mode manha`, `monitor`, `fechamento` |
| 34 | Sem mensagens repetitivas | `BiasNotifier.decide` (estado persistido; atualização só com mudança de classe ou Δscore ≥ 15 e ≥ 30 min) |
| 35–36 | Memória; ACERTO/ERRO; pesos por backtesting | `BiasMemory`: resolve cada horizonte contra o preço H1 real (faixa lateral 0,15 % / 0,30 % / 0,80 %), acerto por fator/classe/hora, `suggest_weights` encolhido pela amostra |
| 37 | Nunca certeza | confiança ≤ 90 %; toda mensagem termina com "Viés probabilístico, não garantia de movimento." |

## Campos novos do `MarketSnapshot`

`employment_surprise_sigma`, `jobless_claims_change_pct`, `economy_momentum`, `china_demand`, `india_demand`, `us2y_change_bp`.
Opcionais: ausentes, o fator fica indisponível e só reduz cobertura e confiança, sem virar sinal negativo. Os fatores de
emprego e inflação também leem os eventos divulgados do calendário/RSS (NFP, desemprego, CPI...) quando o campo não vem preenchido.

## Regra de segurança

O score representa o **viés probabilístico do conjunto de informações**, não uma garantia de movimento. Linguagem usada:
"viés atual", "cenário principal", "risco de reversão". Nunca "o ouro certamente vai subir/cair".

## Segunda camada — PAINEL (cockpit do ouro) `[gold_ai/dashboard.py, gold_ai/dashboard.html]`

```bash
python market_ai_engine_v6.py painel --source mt5 --send        # abre http://127.0.0.1:8765  (scripts/rodar_painel.bat)
```

A IA analisa por trás e o painel mostra os dados que sustentam o sinal, para **você** decidir se entra ou não.
Apoio à decisão: o painel **nunca envia ordens**. Servidor local em Python puro (sem dependências); só fica acessível na rede
com `--host 0.0.0.0`.

| Bloco | O que mostra |
| --- | --- |
| 💰 XAU/USD | preço (MT5), variação do dia, máxima, mínima, variação da janela, fonte |
| 🧠 GOLD BIAS | viés, confiança, score −100..+100 (régua), horizontes horas · 1 dia · 5 dias |
| 🚦 Semáforo | 🟢 COMPRA: macro, técnico e estrutura positivos, fluxo e notícias sem contrariar, score ≥ +40 · 🔴 VENDA: o espelho · 🟡 AGUARDAR: divergência, viés fraco ou evento de alto impacto em ≤ 30 min |
| 🎯 Confluência | % do peso dos blocos (macro 40, técnico 20, fluxo 15, estrutura 15, notícias 10) que aponta na direção do viés |
| 📊 Painel de confluência | MACRO (DXY, Treasury 10Y/2Y/30Y, juros reais, FED) · FLUXO (ETF, China, Índia, bancos centrais) · TÉCNICO (EMA 9/21, RSI, MACD, VWAP, ADX) · NOTÍCIAS (Fed, dólar, geopolítica, demanda física) |
| 📈 Gráfico | candles M1…D1, EMA 9/21/50/200, VWAP + bandas ±1σ/±2σ, suportes/resistências, Fibonacci, sinais da IA (▲▼ quando o viés muda), entrada hipotética com zona de risco/alvo, eventos econômicos; cada camada liga/desliga |
| 🧠 Cérebro da IA | fatos do ciclo, fator dominante, fator contrário, conclusão ("baixista, mas com risco de repique") e a leitura da IA |
| 🔥 POSSO ENTRAR? | tendência, macro, notícias, fluxo, técnico, estrutura, volatilidade → confluência e STATUS (confirmado / aguardar / não entrar); entrada = preço atual, stop pela estrutura ou 1,5 ATR, alvo 2R, lote para o risco em US$ informado; aviso de evento |
| 🌎 Macro · 🏦 Fluxo · 📅 Calendário · 📰 Notícias | detalhes com valores, petróleo WTI/Brent, VIX, geopolítica, economia, apetite a risco |
| 🚨 Alertas | os mesmos do Telegram + ALERTA DE COMPRA/VENDA quando o semáforo abre, com a lista do que confirmou |
| 🗂 Histórico | "o que a IA disse às 10:30 × o que o ouro fez depois" (4 h e 1 dia, ✅/❌) |

Dados sem fonte automática (demanda da China/Índia, compras de bancos centrais, fluxo de ETFs, eventos do calendário):
copie `dados/manual.exemplo.json` para `dados/manual.json`. Só preenche o que a coleta deixou vazio.
