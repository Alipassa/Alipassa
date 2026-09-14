# MARKET AI ENGINE — MISSÃO E REGRA CENTRAL

> **"MAXIMIZAR O APROVEITAMENTO DAS OPORTUNIDADES ESTATISTICAMENTE VÁLIDAS, MAXIMIZAR A EXPECTANCY E O POTENCIAL DE GANHO,
> MANTENDO O RISCO CONTROLADO — SEM SACRIFICAR CAPTURA DE OPORTUNIDADES EM BUSCA DE UMA TAXA DE ACERTO ARTIFICIALMENTE ALTA."**

O alvo é **acerto + captura + expectancy + ganho + controle de drawdown**. Uma taxa de acerto de 90% é péssima se o sistema
perde 90% dos movimentos bons ou se as perdas são muito maiores que os ganhos. Toda decisão de projeto (um filtro novo, um
piso, uma regra de saída) é julgada por essa combinação, medida fora da amostra — nunca pela taxa de acerto isolada.

---

I intend to develop an advanced autonomous multi-market financial analysis and trading application called MARKET AI ENGINE.
The primary objective is to maximize the exploitation of high-quality market opportunities, combining high probability of
success, positive expectancy, profit potential, and efficient opportunity capture while maintaining strict risk control.

The system will continuously analyze real-time and historical market data, macroeconomic indicators, economic-calendar
events, financial news, geopolitical developments, market sentiment, intermarket relationships, order-flow proxies,
volatility, and technical price behavior. It will not depend on a single indicator or require all factors to agree.
Instead, it will use a dynamic weighted evidence model to determine the probability, strength, quality, timing, and
expected value of each opportunity.

The system must continuously search for opportunities across multiple assets and dynamically allocate attention to the
markets offering the best risk-adjusted expected opportunity. It should identify not only confirmed entries but also
WATCH, SETUP, PRE-MOVE, OPPORTUNITY, and EXECUTION stages, allowing the system to detect developing opportunities before
conventional technical confirmation whenever statistically justified.

The system must optimize for the combination of win probability, expectancy per trade, profit factor, opportunity capture
rate, profit potential, drawdown, and risk-adjusted return. It must avoid excessive filtering that causes the system to
miss profitable opportunities. A factor that disagrees with the majority should reduce the confidence or score rather than
automatically invalidate the opportunity unless historical evidence demonstrates that the factor is a true veto condition.

The system must learn from historical and out-of-sample data to determine the optimal thresholds for each asset and market
regime. Thresholds must be dynamic and empirically validated rather than fixed arbitrarily. The system should identify
which combinations of news, macroeconomic conditions, intermarket relationships, technical signals, volatility, and market
regime historically produce the highest expectancy and best opportunity capture.

News and macroeconomic events must be treated as a transversal market-context layer. The system must distinguish between
expected information, surprises, market reactions, and divergences between the expected and actual market response. It
should detect situations where the market reaction contradicts the fundamental news and evaluate whether this divergence
creates latent pressure and a potential pre-move opportunity.

The application must continuously reevaluate open positions. It should not rely exclusively on stop-loss or take-profit
levels. If the original trading thesis loses statistical advantage, the system may reduce, protect, extend, or close the
position according to the evidence available at that moment. Conversely, when the thesis remains strong and statistical
evidence supports continuation, the system should avoid exiting prematurely and should allow profitable trades to develop.

Position sizing must be dynamically calculated according to account equity, predefined risk percentage, stop-loss
distance, contract specifications, and market conditions. Risk percentage must never increase in an attempt to recover
previous losses. As account equity grows, position size may increase while maintaining the same percentage risk.

The system must include comprehensive historical backtesting, walk-forward validation, strict out-of-sample testing,
Monte Carlo analysis, MFE/MAE analysis, lead-time analysis, opportunity-funnel analysis, calibration testing, and
continuous live-edge measurement. It must compare price-only intelligence against price-plus-macro and
price-plus-macro-plus-news intelligence to determine empirically which information actually improves trading performance.

The system must explicitly measure missed opportunities and rejected opportunities, including the reason each opportunity
was filtered out. The objective is not to maximize win rate at the expense of opportunity capture. The system should seek
the optimal balance between accuracy, frequency, expectancy, profit potential, and drawdown.

The application will initially support EURUSD, US500, XAUUSD, USDJPY, and WTI, with an architecture capable of expanding
to additional markets. It will integrate with MetaTrader 5 for paper trading and, only with explicit authorization, live
execution. It will provide real-time monitoring, execution confirmation, risk controls, emergency stop mechanisms, trade
lifecycle tracking, performance analytics, and Telegram alerts.

The system must never assume or guarantee profitability. All decisions must be based on measurable statistical evidence,
historical validation, current market conditions, and controlled risk. The ultimate objective is to build an adaptive
market intelligence and execution engine capable of capturing as much statistically valid opportunity as possible while
maximizing risk-adjusted profitability and minimizing unnecessary missed opportunities.

---

## Onde cada exigência vive no código

| Exigência | Módulo / comando |
|---|---|
| Evidência ponderada, fator discordante reduz score em vez de vetar | `factors`, `evidence` (nível de evidência), `EngineConfig.min_confirmations` |
| Estágios WATCH → PRE-MOVE → SIGNAL (SETUP/OPPORTUNITY/EXECUTION = funil) | `premove`, `signals`, `opportunity.FUNNEL_STAGES` |
| Vários mercados, atenção ao melhor risco-retorno | `markets`, `selector.AssetSelector`, `market_engine` |
| Limiar dinâmico e validado fora da amostra, nunca fixo arbitrariamente | `sweep` (piso escolhido no treino de cada fold), `validation` |
| Notícia como camada transversal: expectativa, surpresa, reação, divergência, pressão latente | `news_engine`, `history` (point-in-time), `compare-news` |
| Velocidade de transmissão da informação: tempo de reação por evento/ativo, assimetria líderes × alvo | `reaction` (REACTION ENGINE), `reaction learn/stats/clock` |
| Reavaliação contínua da posição (manter/proteger/reduzir/estender/encerrar) | `monitor.TradeMonitor`, ADAPTIVE EXIT |
| Lote por capital × risco % × distância do stop; risco % nunca sobe para recuperar | `trading.size_lots`, `guard.PerformanceEngine` |
| Backtest, walk-forward, OOS, bootstrap, MFE/MAE, lead time, funil, calibração, live edge | `evaluation`, `estimate`, `opportunity`, `edge_report`, `edge` |
| Preço só × +Macro × +Macro+News | `ablation`, `compare-news` |
| Oportunidades perdidas e rejeitadas com o motivo | `opportunity` (ENTRY FUNNEL, atribuição por filtro, curva de limiar) |
| PAPER por padrão; LIVE só com autorização explícita; kill switch | `guard`, `live_engine`, `--authorize` |
| Nunca assumir lucro | `estimate` (ressalvas), `edge_status_from_stats` (⚪ inconclusivo) |
