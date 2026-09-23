# Auditoria — GOLD BIAS + PAINEL (23/09/2026)

Revisão independente em três frentes (motor da IA, servidor do painel, página do painel), com cada achado confirmado
por execução e coberto por teste de regressão em `tests/test_audit_bias_painel.py`. Nenhum achado crítico; 36 itens corrigidos.

## Motor da IA (`gold_ai/bias.py`)

| Achado | Efeito antes | Correção |
| --- | --- | --- |
| Reversão só contava de ALTA direto para BAIXA | ALTA → NEUTRO → BAIXA nunca gerava "🚨 REVERSÃO DE CENÁRIO" | compara com a última direção não-neutra |
| Comparação com o ciclo anterior | deriva lenta (41 → 69) nunca avisava | compara com a última mensagem **enviada** |
| Sem histerese na fronteira ±40/±70 | score 39,9 ↔ 40,0 mandava atualização a cada ciclo | nova classe só com 3 pontos dentro dela |
| Acerto/erro usava o candle **depois** do alvo | horizonte de 4 h virava 4,5–5,5 h (olhava o futuro) | preço = fechamento do último candle já encerrado no instante-alvo |
| Alvo anterior ao histórico | previsões antigas "resolvidas" com preço de dias depois | não resolve se o histórico não cobre o alvo; fim de semana usa o último fechamento |
| Fatores contados 3× (um por horizonte) | encolhimento pela amostra fraco demais nos pesos sugeridos | poder do fator medido só no horizonte de 1 dia |
| Fechamento comparava a leitura com ela mesma; dia em UTC | "+0,00%" e "sem leituras hoje" depois das 21h de Brasília | usa o relatório da manhã; dia local (UTC−3); formata antes de gravar |
| `dados/manual.json` com formato inesperado | lista, `"eventos": 5`, número como texto ou `true` derrubavam o monitor | validação de formato; texto numérico aceito; booleano ignorado |
| COT antigo | relatório de meses atrás ainda pesava no fluxo | peso por idade (≥ 35 dias descartado) |
| Credibilidade de fonte | "Bear…" = fonte oficial; "May" (maio) = rumor | fronteiras de palavra nos padrões |
| Arquivo de estado frágil | JSON inválido/lista derrubava; queda na gravação corrompia | validação + gravação atômica |
| Sem dados, 38 % de confiança | leitura vazia parecia utilizável | confiança limitada pela cobertura (0 % dos dados → 5 %) + aviso "DADOS INSUFICIENTES" |
| NaN de um feed | DXY "NaN" virava +15 (máximo a favor do ouro) | NaN/inf viram "sem dado" |
| Um ciclo com erro | derrubava o monitor do `bias` | erro registrado; o monitor segue |

## Servidor do painel (`gold_ai/dashboard.py`, `cmd_painel`)

| Achado | Correção |
| --- | --- |
| `risk=inf`/`1e400` derrubava a conexão; qualquer exceção fechava sem resposta | risco precisa ser finito; erros viram resposta 500 com mensagem |
| Painel sem `--send` gravava no **mesmo** estado do `bias --send` → alertas reais não saíam | estado próprio `dados/painel_state.json`; sem `--send` não há remetente |
| `--source sample` gravava dados sintéticos no histórico real | sample usa memória temporária (também no `bias`) |
| ALERTA DE COMPRA/VENDA repetia com o semáforo piscando | no máximo 1 por lado por hora; lembrado entre reinícios |
| "Atualizar" podia devolver leitura de antes do clique; travava 120 s em erro | espera um ciclo iniciado depois do pedido; retorna na falha |
| EMA 50/200 no início do gráfico era a semente decaindo | EMA padrão semeada pela média simples (sem valor antes de N candles) |
| VWAP do D1/H4 ancorado na sessão diária | âncora conforme o timeframe (sessão · semana · mês) |
| NaN virava +100 | NaN/inf → "sem dado"; JSON sempre válido |
| Lote 0,29 arredondado para 0,28; lote < 0,01 mostrado como 0 | tolerância no arredondamento; aviso de lote mínimo |
| Preço 0 (fontes falharam) ainda gerava semáforo e plano | AGUARDAR — SEM PREÇO, sem plano |
| Plano de entrada exibido com viés fraco ("NÃO ENTRAR" + plano completo) | plano só com score ≥ ±40; nota "viés fraco" na confluência |
| Semáforo liberava 1 min depois do CPI | AGUARDAR também nos 15 min após evento de alto impacto |
| Estrutura sem dados contava como neutra | conta como "sem dado" |
| Lista de alertas serializada fora do lock; encerramento fechava o banco no meio do ciclo; MT5 não era fechado | cópia sob lock; parada ordenada (espera o ciclo, fecha banco e MT5) |
| `/api/refresh` por GET e sem checar Host (qualquer site podia disparar coletas/Telegram) | refresh só por POST; Host precisa ser local (exceto com `--host 0.0.0.0`) |

## Página do painel (`gold_ai/dashboard.html`)

| Achado | Correção |
| --- | --- |
| Sinais da IA e eventos no candle errado com buraco de fim de semana; sinal do candle em formação sumia | posição pelo horário real dos candles (busca binária) |
| Resposta atrasada de H1 sobrescrevia o gráfico D1 | respostas de outro timeframe descartadas; ciclos não se sobrepõem |
| Diálogo "POSSO ENTRAR?" misturava erro com dados antigos | limpa o conteúdo no erro |
| Servidor fora apagava o gráfico; erro de desenho aparecia como "sem conexão" | mantém a última leitura; erros de desenho vão para o console |
| Linha do preço atual podia sair da área do gráfico | preço entra na escala |
| Falha do "Atualizar" silenciosa | mensagem no topo |
| Timeframe salvo inválido | volta para H1 |

Verificado sem problema: XSS (manchetes e eventos são escapados), chaves JSON entre servidor e página, dados vazios/nulos,
1 candle, candles planos, telas de 375 px, sinais de compra/venda do stop e alvo, bind padrão em 127.0.0.1.
