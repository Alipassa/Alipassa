# REACTION ENGINE — checklist anti look-ahead

Invariantes que o código cumpre e onde conferir (`gold_ai/reaction.py`, `evaluation.HistoryFrame`, `market_engine._reaction_clock`):

1. **Registro só existe depois de concluído.** `ReactionRecord.known_at = published_at + horizon_min`; `ReactionStats.at(t)` faz
   `bisect_right(known_at, t)` — um evento publicado às 12:30 com horizonte 240 min só entra na estatística a partir das 16:30.
2. **Medição usa o futuro, a decisão não.** `measure_reaction` percorre preços após `published_at` (é a medição); o relógio (`ReactionClock.assess`)
   só recebe `ReactionStats.at(now)` e o snapshot de `now`.
3. **Início do cronômetro = published_at**, nunca `timestamp` — revisões (`revised is not None`) são ignoradas; manchetes GDELT em modo
   volinfo têm `published_at` no fim do dia (conservador: o cérebro vê depois, nunca antes).
4. **Preço de referência do alvo = último candle ≤ instante do evento** (`p0`), procurado em M5/M15/H1 do snapshot, que só contém candles ≤ now;
   sem candles, usa a variação da janela (última hora). Candle posterior ao evento nunca vira `p0` (teste `test_target_move_measured_since_event_from_past_candles_only`).
5. **ATR do evento** (`atr_at`) usa só candles ≤ published_at.
6. **Líderes**: DXY em % e US10Y em bp medidos a partir do valor ≤ published_at; no live, amostrados por ciclo a partir da identificação do evento.
7. **Eventos identificados no backtest** vêm de `EventHistory.available_at(t)` (published_at ≤ t) e `EventIdentifier` descarta `e.time > now`.
8. **Cache de estatística** por `(id(events), symbol)`: trocar o banco (modo none/macro/full) recomputa; `none` não anexa nada.
9. **Probabilidade**: base = P(direção) histórica do tipo (n ≥ 3) ou 0,5; ajustes pela evidência de `now`; nunca pelo desfecho do evento em curso.
10. **Live**: `pending_reactions` é medido apenas quando `age_min ≥ horizon`; gravação única por (evento, ativo) (`UNIQUE(evento_id, ativo)`).

Limites conhecidos (não são look-ahead, são resolução):
- histórico H1 → tempos em múltiplos de 60 min; o live amostra por ciclo e usa M5 quando disponível;
- no live sem M5, `p0` é a primeira amostra após identificar o evento (alguns minutos depois da publicação);
- o edge financeiro desta camada ainda NÃO foi medido: `compare-news` / `estimate --events` com o relógio ligado é o teste.
