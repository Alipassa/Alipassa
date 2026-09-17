@echo off
REM ============================================================================
REM  MARKET AI ENGINE - execucao REAL na conta DEMO da corretora (ordens de verdade, dinheiro ficticio)
REM  Coloque na MESMA pasta de market_ai_engine_v6.py e do .env. MT5 aberto e logado na conta DEMO.
REM  TRAVA: o programa confere no MT5 se a conta e demo; em conta real ele se recusa a rodar.
REM  A saida aparece NESTA JANELA e tambem em logs\live_demo.log. Reinicia sozinho se cair.
REM  Para desligar: feche esta janela ou crie o arquivo STOP_TRADING nesta pasta.
REM  Telegram: /STATUS carteira e aprendizado · /FLOW anomalias medidas · /EDGE edge do dia · /STOP bloqueia entradas · /CLOSE + /CLOSE CONFIRM fecha tudo.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
set "MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI"
:loop
echo [%date% %time%] iniciando LIVE (conta DEMO) - o primeiro ciclo pode levar alguns minutos
echo [%date% %time%] iniciando LIVE (conta DEMO) >> logs\live_demo.log
python market_ai_engine_v6.py --log-file logs\live_demo.log live --markets %MERCADOS% --source mt5 --mode live --authorize --send --interval 60
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s >> logs\live_demo.log
if exist STOP_TRADING (echo arquivo STOP_TRADING presente - nao reinicia & goto fim)
timeout /t 30 /nobreak > nul
goto loop
:fim
pause
