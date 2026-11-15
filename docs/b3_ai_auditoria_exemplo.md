# Auditoria do B3 AI

Gerada em 15/11/2026 17:24 (Brasília) · pregão de referência 2026-11-13 · WIN, WDO e ações da B3 com ~15 min de atraso (use o MetaTrader 5 para tempo real). Mercados internacionais em tempo real, exceto: DI futuro (1º venc.) (15 min), Ibovespa (15 min), Minério de ferro (SGX) (10 min).

## 1. Fontes de dados

| Fonte | Situação | Detalhe |
| --- | --- | --- |
| B3 — todas as empresas (scanner) | ✅ ok | 2324 papéis · 0.6s |
| Histórico WIN (feed público) | ✅ ok | 50 barras · 0.5s |
| Banco Central (Selic, PTAX, IPCA) | ✅ ok | 4 séries · 1.9s |
| Boletim Focus | ✅ ok | 4 indicadores · 2.4s |
| Notícias (RSS) | ✅ ok | 824 manchetes, 0 feeds com erro · 2.5s |
| Agenda (Fed, Copom) | ✅ ok | 4 eventos · 0.9s |
| MetaTrader 5 (tempo real) | ✅ ok | não ativado (rode com --fonte mt5) |

## 2. Direção — acerto fora da amostra (walk-forward)

O modelo é treinado só com o passado e prevê o trecho seguinte. Acerto = previu alta e subiu, ou previu queda e caiu.

| Ativo | Horizonte | Previsões | Acertos | Acerto | Intervalo 95% | Conclusão | Acerto com convicção (≥55/45) |
| --- | --- | ---: | ---: | ---: | --- | --- | --- |
| WIN | 5 min | 2290 | 1111 | **48.5%** | 46.5–50.6% | ⚪ não distinguível do acaso | — (0 casos) |
| WIN | 10 min | 2274 | 1197 | **52.6%** | 50.6–54.7% | ✅ vantagem comprovada | — (0 casos) |
| WIN | 30 min | 2197 | 1044 | **47.5%** | 45.4–49.6% | ❌ pior que o acaso | — (0 casos) |
| WIN | 1 hora | 2080 | 1004 | **48.3%** | 46.1–50.4% | ⚪ não distinguível do acaso | — (0 casos) |
| WIN | Diário | 825 | 443 | **53.7%** | 50.3–57.1% | ✅ vantagem comprovada | 53.9% em 115 |
| WIN | Semanal | 823 | 432 | **52.5%** | 49.1–55.9% | ⚪ não distinguível do acaso | 59.7% em 196 |
| WDO | 5 min | 2105 | 1036 | **49.2%** | 47.1–51.4% | ⚪ não distinguível do acaso | — (0 casos) |
| WDO | 10 min | 2150 | 1070 | **49.8%** | 47.7–51.9% | ⚪ não distinguível do acaso | — (0 casos) |
| WDO | 30 min | 2123 | 1105 | **52.0%** | 49.9–54.2% | ⚪ não distinguível do acaso | — (0 casos) |
| WDO | 1 hora | 2030 | 1054 | **51.9%** | 49.7–54.1% | ⚪ não distinguível do acaso | 57.9% em 95 |
| WDO | Diário | 825 | 420 | **50.9%** | 47.5–54.3% | ⚪ não distinguível do acaso | 50.0% em 16 |
| WDO | Semanal | 823 | 428 | **52.0%** | 48.6–55.4% | ⚪ não distinguível do acaso | 62.5% em 16 |

Total: 10344 acertos em 20545 previsões de direção (50.3%).

## 3. Faixa de preço de 80% — cobertura fora da amostra

A faixa é calculada com a 1ª metade do histórico e conferida na 2ª. O alvo é 80%.

| Ativo | Horizonte | Casos | Dentro da faixa |
| --- | --- | ---: | ---: |
| WIN | 30 min | 2216 | **78.5%** |
| WIN | 1 hora | 2092 | **78.9%** |
| WIN | 5 min | 2319 | **82.0%** |
| WIN | 10 min | 2298 | **83.2%** |
| WIN | Diário | 750 | **82.5%** |
| WIN | Semanal | 748 | **77.1%** |
| WDO | 30 min | 2216 | **79.6%** |
| WDO | 1 hora | 2092 | **80.2%** |
| WDO | 5 min | 2319 | **83.2%** |
| WDO | 10 min | 2298 | **82.8%** |
| WDO | Diário | 750 | **80.1%** |
| WDO | Semanal | 748 | **82.0%** |

## 4. Sinal de reversão nos extremos (5 min)

| Ativo | Sinais | Acerto em 30 min | Intervalo 95% | Conclusão | Acerto em 1 h | Ganho médio em 30 min |
| --- | ---: | ---: | --- | --- | ---: | ---: |
| WIN | 111 | **60.4%** | 51–69% | ✅ vantagem comprovada | 56.9% | +0.051% |
| WDO | 102 | **42.2%** | 33–52% | ⚪ não distinguível do acaso | 46.3% | -0.009% |

## 5. Choques do exterior → pregão do mini índice (base dos alertas)

| Quando… | Pregões | O mini índice fechou em | Frequência | Média no dia | Pregão seguinte em alta |
| --- | ---: | --- | ---: | ---: | ---: |
| VIX (índice do medo) disparando | 98 | queda | **81%** | -1.00% | 54% |
| VIX (índice do medo) despencando | 51 | alta | **88%** | +1.04% | 53% |
| S&P 500 futuro em queda forte | 83 | queda | **78%** | -0.92% | 49% |
| S&P 500 futuro em alta forte | 82 | alta | **78%** | +0.90% | 54% |
| dólar global (DXY) em alta forte | 100 | queda | **65%** | -0.62% | 64% |
| juro americano de 10 anos em alta forte | 106 | queda | **59%** | -0.38% | 57% |
| petróleo Brent em queda forte | 92 | queda | **54%** | -0.18% | 48% |
| minério de ferro em queda forte | 74 | queda | **53%** | -0.24% | 46% |
| dólar disparando contra o real | 93 | queda | **94%** | -1.56% | 58% |
| dólar despencando contra o real | 76 | alta | **88%** | +1.22% | 57% |
| emergentes (EEM) em queda forte | 87 | queda | **89%** | -1.13% | 46% |
| emergentes (EEM) em alta forte | 85 | alta | **80%** | +0.88% | 61% |

Dias de decisão do Fed (45 desde 2021): oscilação média de 1.30% contra 0.90% num dia comum.

## 6. Placar ao vivo (previsões gravadas antes do resultado)

Ainda sem previsões conferidas: o placar começa a encher depois do primeiro pregão com o painel ligado.

## Como ler

- **Direção**: só os horizontes com ✅ têm vantagem estatística; nos demais a probabilidade do painel fica perto de 50% de propósito.
- **Faixa de 80%**: é a previsão mais confiável do sistema — use para alvo e stop.
- **Choques**: relação medida no mesmo pregão; é o que dispara os alertas de 75% e 90%.
- Acerto passado não garante acerto futuro; refaça esta auditoria de tempos em tempos.
