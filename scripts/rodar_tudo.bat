@echo off
REM ============================================================================
REM  MARKET AI ENGINE — pipeline completa sem acompanhamento (Windows)
REM  Coloque este arquivo na MESMA pasta de market_ai_engine_v4.py e do .env.
REM  Requisitos: MT5 aberto e logado (para as exportacoes MT5); internet.
REM  Tudo fica em logs\ ; resultados: prova.txt, teste_ab.txt, estimativa_news.txt
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
if not exist logs mkdir logs
set PY=python
set ENGINE=market_ai_engine_v4.py
set MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI
set INICIO=2026-01-01
set FIM=2026-09-13
set LOG=logs\pipeline_%date:~6,4%-%date:~3,2%-%date:~0,2%_%time:~0,2%%time:~3,2%.log
set LOG=%LOG: =0%

echo [%date% %time%] INICIO DA PIPELINE > "%LOG%"
call :passo "1/8 ALFRED (macro point-in-time)"      %PY% %ENGINE% history fetch-alfred --start %INICIO%
call :passo "2/8 GDELT (manchetes, continua de onde parou)" %PY% %ENGINE% history fetch-gdelt --start %INICIO% --pace 12
call :passo "3/8 MT5 ticks (jun->)"                 %PY% %ENGINE% history prices --source mt5 --tf TICK --markets %MERCADOS% --extra USDX --start 2026-06-01
call :passo "4/8 MT5 M1 (jan->)"                    %PY% %ENGINE% history prices --source mt5 --tf M1 --markets %MERCADOS% --extra USDX --start %INICIO%

echo.
echo === 5/8 Dukascopy ticks ao redor dos eventos (repete ate completar) ===
set /a TENTATIVA=0
:duka
set /a TENTATIVA+=1
echo [%time%] Dukascopy tentativa !TENTATIVA! >> "%LOG%"
%PY% %ENGINE% history prices --markets %MERCADOS% --extra USDX --start %INICIO% >> "%LOG%" 2>&1
if %errorlevel%==2 if !TENTATIVA! LSS 12 (
    echo   horas faltando - aguardando 90s e repetindo (!TENTATIVA!/12)
    timeout /t 90 /nobreak > nul
    goto duka
)

call :passo "6/8 Cobertura do banco"                %PY% %ENGINE% history stats --start %INICIO% --end %FIM%
call :passo "7/8 PROVA do REACTION CLOCK (TICK + M1)" %PY% %ENGINE% reaction learn --tf BOTH --markets %MERCADOS% --lead-usd USDX --out prova.txt
call :passo "8/8 TESTE A/B preco x macro x news"    %PY% %ENGINE% compare-news --start %INICIO% --end %FIM% --markets %MERCADOS% --out teste_ab.txt
call :passo "extra: estimativa de lucro com noticias" %PY% %ENGINE% estimate --start %INICIO% --end %FIM% --markets %MERCADOS% --equity 10000 --risk 0.5 --events dados\noticias_historicas.csv --news-mode full --out estimativa_news.txt

echo.
echo ============================================================
echo  CONCLUIDO. Resultados: prova.txt, teste_ab.txt, estimativa_news.txt
echo  Log completo: %LOG%
echo ============================================================
echo [%date% %time%] FIM >> "%LOG%"
pause
exit /b 0

:passo
set NOME=%~1
shift
echo.
echo === %NOME% ===
echo [%time%] === %NOME% === >> "%LOG%"
%1 %2 %3 %4 %5 %6 %7 %8 %9 %10 %11 %12 %13 %14 %15 %16 %17 %18 %19 %20 >> "%LOG%" 2>&1
if errorlevel 1 (echo   [aviso] terminou com codigo %errorlevel% - veja o log) else (echo   ok)
exit /b 0
