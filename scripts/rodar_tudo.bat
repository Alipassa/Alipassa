@echo off
REM ============================================================================
REM  MARKET AI ENGINE - pipeline completa sem acompanhamento (Windows)
REM  Coloque este arquivo na MESMA pasta de market_ai_engine_v5.py e do .env.
REM  Requisitos: MT5 aberto e logado (para as exportacoes MT5); internet.
REM  A saida aparece NESTA JANELA e tambem em logs\pipeline_*.log ; resultados: prova.txt, teste_ab.txt, estimativa_news.txt
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
set "PY=python"
REM  saida NA TELA e no log ao mesmo tempo (--log-file)
set "ENGINE=market_ai_engine_v5.py"
set "MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI"
set "INICIO=2026-01-01"
set "FIM=2026-09-13"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set "STAMP=%%i"
if "%STAMP%"=="" set "STAMP=%RANDOM%"
set "LOG=logs\pipeline_%STAMP%.log"

echo [%date% %time%] INICIO DA PIPELINE > "%LOG%"
echo Log: %LOG%

set "NOME=1/8 ALFRED - macro point-in-time"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history fetch-alfred --start %INICIO%"
call :passo

set "NOME=2/8 GDELT - manchetes, pula se ja cobre 80%, senao ate 10 min"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history fetch-gdelt --start %INICIO% --pace 12 --max-minutes 10 --skip-if-covered 0.8"
call :passo

set "NOME=3/8 MT5 ticks desde junho"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history prices --source mt5 --tf TICK --markets %MERCADOS% --extra USDX --start 2026-06-01"
call :passo

set "NOME=3b/8 Dukascopy TICKS ao redor dos releases agendados (1 h antes, 1 h depois) - completa as horas que a corretora nao guarda"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history prices --source dukascopy --tf TICK --markets %MERCADOS% --extra USDX --start %INICIO% --end %FIM% --before 1 --after 1"
call :passo

set "NOME=4/8 MT5 M1 desde janeiro (o terminal guarda ~100 000 barras: ~3 meses)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history prices --source mt5 --tf M1 --markets %MERCADOS% --extra USDX --start %INICIO%"
call :passo

set "NOME=4b/8 Dukascopy M1 diario - completa o que o terminal nao guarda (janeiro ate junho)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history prices --source dukascopy --tf M1 --full --markets %MERCADOS% --extra USDX --start %INICIO% --end %FIM%"
call :passo

echo.
echo === 5/8 Dukascopy ticks ao redor dos eventos - repete ate completar ===
set /a TENTATIVA=0
:duka
set /a TENTATIVA+=1
echo [%time%] Dukascopy tentativa !TENTATIVA! >> "%LOG%"
%PY% %ENGINE% --log-file "%LOG%" history prices --markets %MERCADOS% --extra USDX --start %INICIO%
set "RC=!errorlevel!"
if "!RC!"=="2" if !TENTATIVA! LSS 12 (
    echo   horas faltando - aguardando 90s e repetindo !TENTATIVA!/12
    timeout /t 90 /nobreak > nul
    goto duka
)
if "!RC!"=="0" (echo   ok) else (echo   [aviso] terminou com codigo !RC! - veja o log)

set "NOME=6/8 Cobertura do banco"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" history stats --start %INICIO% --end %FIM%"
call :passo

set "NOME=7/8 PROVA do REACTION CLOCK - TICK e M1"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" reaction learn --tf BOTH --markets %MERCADOS% --lead-usd USDX --out prova.txt"
call :passo

set "NOME=8/8 TESTE A/B preco x macro x news"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" compare-news --start %INICIO% --end %FIM% --markets %MERCADOS% --out teste_ab.txt"
call :passo

set "NOME=8b/8 ESCADA A-E (preco, +macro, +news, +flow, +reaction clock) com funil de captura"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" compare-news --ladder --start %INICIO% --end %FIM% --markets %MERCADOS% --out escada.txt"
call :passo

set "NOME=8c/8 EXIT LAB - saida com maior expectancy OOS"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" exit-lab --start %INICIO% --end %FIM% --markets %MERCADOS% --out exit_lab.txt"
call :passo

set "NOME=8d/8 EDGE BANK - o que funciona, onde funciona (dados\edge_bank.json)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" edge-bank --start %INICIO% --end %FIM% --markets %MERCADOS% --out dados\edge_bank.json"
call :passo

set "NOME=8f/8 FLOW LEARN - anomalias de fluxo no M1 desde janeiro, medidas 60 min depois (ledger historico)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" flow --learn --markets %MERCADOS% --lead-usd USDX"
call :passo

set "NOME=8g/8 PORTFOLIO SIM - 1 x 2 x 3 x 4 posicoes simultaneas com as operacoes OOS (liquido de custo e correlacao)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" portfolio-sim --start %INICIO% --end %FIM% --markets %MERCADOS% --out portfolio_sim.txt"
call :passo

set "NOME=8h/8 MATRIZ - confirmacoes 1..5 x posicoes simultaneas 1..4: n, acerto, R, expectancy, lucro, custos, MFE, MAE, DD, sequencia, duracao, por ativo/evento, 1a x 2a metade (matriz.txt)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" matrix --start %INICIO% --end %FIM% --markets %MERCADOS% --out matriz.txt"
call :passo

set "NOME=8e/8 AUTOTUNE - a IA procura piso/confirmacoes/limiar no passado (dados\parametros.json; o live adota so com 20 casos OOS)"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" autotune --start %INICIO% --end %FIM% --markets %MERCADOS% --out dados\parametros.json"
call :passo

set "NOME=extra - estimativa de lucro com noticias"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" estimate --start %INICIO% --end %FIM% --markets %MERCADOS% --equity 10000 --risk 3 --events dados\noticias_historicas.csv --news-mode full --out estimativa_news.txt"
call :passo

set "NOME=DOCTOR - tudo funcionando? eficiencia?"
set "CMD=%PY% %ENGINE% --log-file "%LOG%" doctor --mt5 --out logs\doctor.txt"
call :passo
type logs\doctor.txt

echo.
echo ============================================================
echo  CONCLUIDO. Resultados: prova.txt, teste_ab.txt, escada.txt, exit_lab.txt, estimativa_news.txt, logs\doctor.txt, portfolio_sim.txt, dados\edge_bank.json, dados\parametros.json
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
!CMD!
set "RC=!errorlevel!"
if "!RC!"=="0" (echo   ok) else (echo   [aviso] terminou com codigo !RC! - veja o log)
exit /b 0
