@echo off
REM ============================================================================
REM  CHECKUP - o GOLD BIAS + PAINEL esta 100%% funcionando?
REM  Deixe o MetaTrader 5 aberto e logado. Se quiser conferir o painel, deixe o
REM  rodar_painel.bat aberto tambem. Manda uma mensagem de TESTE no Telegram.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
echo Conferindo... pode levar 1 a 3 minutos.
echo.
python market_ai_engine_v6.py bias --mode checkup --source mt5 --send
echo.
pause
