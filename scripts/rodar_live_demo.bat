@echo off
REM ============================================================================
REM  MARKET AI ENGINE - execucao REAL na conta DEMO da corretora (ordens de verdade, dinheiro ficticio)
REM  Coloque na MESMA pasta de market_ai_engine_v5.py e do .env. MT5 aberto e logado na conta DEMO.
REM  TRAVA: o programa confere no MT5 se a conta e demo; em conta real ele se recusa a rodar.
REM  Reinicia sozinho se cair. Para desligar: feche esta janela ou crie o arquivo STOP_TRADING nesta pasta.
REM  /STOP no Telegram bloqueia novas entradas; /CLOSE + /CLOSE CONFIRM fecha tudo; /STATUS mostra a carteira.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
set "MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI"
:loop
echo [%date% %time%] iniciando LIVE (conta DEMO) >> logs\live_demo.log
python market_ai_engine_v5.py live --markets %MERCADOS% --source mt5 --mode live --authorize --send --interval 60 >> logs\live_demo.log 2>&1
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s >> logs\live_demo.log
if exist STOP_TRADING (echo arquivo STOP_TRADING presente - nao reinicia & goto fim)
timeout /t 30 /nobreak > nul
goto loop
:fim
