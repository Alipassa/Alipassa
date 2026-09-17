@echo off
REM ============================================================================
REM  MARKET AI ENGINE 6.0 - LEADER PROPAGATION ENGINE (2 cliques)
REM  "O movimento ja comecou no lider; existe evidencia historica de que outros ativos reagem depois."
REM  Coloque este arquivo na MESMA pasta de market_ai_engine_v6.py. Precisa de dados\<SYM>_m1.csv (ou dados\<SYM>_ticks.csv):
REM      python market_ai_engine_v6.py history prices --source mt5 --tf M1 --markets XAUUSD,US500,EURUSD,USDJPY,WTI --start 2026-01-01
REM      python market_ai_engine_v6.py history prices --markets XAUUSD,US500,EURUSD,USDJPY,WTI --start 2026-01-01   (Dukascopy, ticks)
REM  Saida: propagacao.txt (relatorio) + propagacao.json (impulsos, lag map, testes A-E, operacoes) + logs\propagacao_*.log
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
set "PY=python"
set "ENGINE=market_ai_engine_v6.py"
set "MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI"
set "LIDERES=%MERCADOS%"
set "INICIO=2026-01-01"
set "FIM=2026-09-17"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set "STAMP=%%i"
if "%STAMP%"=="" set "STAMP=%RANDOM%"
set "LOG=logs\propagacao_%STAMP%.log"

echo === 6.0 LEADER PROPAGATION - todos os lideres, TESTES A-E (propagacao.txt) ===
%PY% %ENGINE% --log-file "%LOG%" propagation --markets %MERCADOS% --leaders %LIDERES% --start %INICIO% --end %FIM% --events dados\noticias_historicas.csv --out propagacao.txt --json propagacao.json
if errorlevel 1 echo   [aviso] terminou com codigo %errorlevel% - veja %LOG%

echo.
echo === 6.0 LEADER PROPAGATION - so o OURO como lider (propagacao_xau.txt) ===
%PY% %ENGINE% --log-file "%LOG%" propagation --markets %MERCADOS% --leaders XAUUSD --start %INICIO% --end %FIM% --events dados\noticias_historicas.csv --out propagacao_xau.txt --json propagacao_xau.json
if errorlevel 1 echo   [aviso] terminou com codigo %errorlevel% - veja %LOG%

echo.
echo Resultados: propagacao.txt / propagacao_xau.txt  (log: %LOG%)
echo Para afrouxar ou apertar o detector: --z 2.5 (mais impulsos) / --z 4 (so os brutais); --min-n 20 exige mais historico antes de entrar.
pause
