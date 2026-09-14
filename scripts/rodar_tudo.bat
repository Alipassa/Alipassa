@echo off
REM ============================================================================
REM  MARKET AI ENGINE - pipeline completa sem acompanhamento (Windows)
REM  Coloque este arquivo na MESMA pasta de market_ai_engine_v4.py e do .env.
REM  Requisitos: MT5 aberto e logado (para as exportacoes MT5); internet.
REM  Tudo fica em logs\ ; resultados: prova.txt, teste_ab.txt, estimativa_news.txt
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
set "PY=python"
set "ENGINE=market_ai_engine_v4.py"
set "MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI"
set "INICIO=2026-01-01"
set "FIM=2026-09-13"
for /f "tokens=1-3 delims=/ " %%a in ("%date%") do set "D=%%c-%%b-%%a"
set "H=%time:~0,2%%time:~3,2%"
set "H=%H: =0%"
set "LOG=logs\pipeline_%D%_%H%.log"

echo [%date% %time%] INICIO DA PIPELINE > "%LOG%"
echo Log: %LOG%

set "NOME=1/8 ALFRED - macro point-in-time"
set "CMD=%PY% %ENGINE% history fetch-alfred --start %INICIO%"
call :passo

set "NOME=2/8 GDELT - manchetes, ate 10 min por rodada, continua de onde parou"
set "CMD=%PY% %ENGINE% history fetch-gdelt --start %INICIO% --pace 12 --max-minutes 10"
call :passo

set "NOME=3/8 MT5 ticks desde junho"
set "CMD=%PY% %ENGINE% history prices --source mt5 --tf TICK --markets %MERCADOS% --extra USDX --start 2026-06-01"
call :passo

set "NOME=4/8 MT5 M1 desde janeiro"
set "CMD=%PY% %ENGINE% history prices --source mt5 --tf M1 --markets %MERCADOS% --extra USDX --start %INICIO%"
call :passo

echo.
echo === 5/8 Dukascopy ticks ao redor dos eventos - repete ate completar ===
set /a TENTATIVA=0
:duka
set /a TENTATIVA+=1
echo [%time%] Dukascopy tentativa !TENTATIVA! >> "%LOG%"
%PY% %ENGINE% history prices --markets %MERCADOS% --extra USDX --start %INICIO% >> "%LOG%" 2>&1
set "RC=!errorlevel!"
if "!RC!"=="2" if !TENTATIVA! LSS 12 (
    echo   horas faltando - aguardando 90s e repetindo !TENTATIVA!/12
    timeout /t 90 /nobreak > nul
    goto duka
)
if "!RC!"=="0" (echo   ok) else (echo   [aviso] terminou com codigo !RC! - veja o log)

set "NOME=6/8 Cobertura do banco"
set "CMD=%PY% %ENGINE% history stats --start %INICIO% --end %FIM%"
call :passo

set "NOME=7/8 PROVA do REACTION CLOCK - TICK e M1"
set "CMD=%PY% %ENGINE% reaction learn --tf BOTH --markets %MERCADOS% --lead-usd USDX --out prova.txt"
call :passo

set "NOME=8/8 TESTE A/B preco x macro x news"
set "CMD=%PY% %ENGINE% compare-news --start %INICIO% --end %FIM% --markets %MERCADOS% --out teste_ab.txt"
call :passo

set "NOME=extra - estimativa de lucro com noticias"
set "CMD=%PY% %ENGINE% estimate --start %INICIO% --end %FIM% --markets %MERCADOS% --equity 10000 --risk 3 --events dados\noticias_historicas.csv --news-mode full --out estimativa_news.txt"
call :passo

set "NOME=DOCTOR - tudo funcionando? eficiencia?"
set "CMD=%PY% %ENGINE% doctor --mt5 --out logs\doctor.txt"
call :passo
type logs\doctor.txt

echo.
echo ============================================================
echo  CONCLUIDO. Resultados: prova.txt, teste_ab.txt, estimativa_news.txt, logs\doctor.txt
echo  Log completo: %LOG%
echo ============================================================
echo [%date% %time%] FIM >> "%LOG%"
pause
exit /b 0

:passo
echo.
echo === !NOME! ===
echo. >> "%LOG%"
echo [%time%] === !NOME! === >> "%LOG%"
echo   comando: !CMD! >> "%LOG%"
!CMD! >> "%LOG%" 2>&1
set "RC=!errorlevel!"
if "!RC!"=="0" (echo   ok) else (echo   [aviso] terminou com codigo !RC! - veja o log)
exit /b 0
