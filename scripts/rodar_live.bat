@echo off
REM ============================================================================
REM  MARKET AI ENGINE - PAPER multi-mercado 24/7 (reinicia sozinho se cair)
REM  Coloque na MESMA pasta de market_ai_engine_v5.py e do .env. MT5 aberto e logado.
REM  Modo PAPER: nunca envia ordem real. /STOP no Telegram bloqueia NOVAS ENTRADAS (o robo continua analisando);
REM  A saida aparece NESTA JANELA e tambem em logs\live.log.
REM  para desligar de vez: feche esta janela ou crie o arquivo STOP_TRADING nesta pasta.
REM  para TROCAR DE BUILD sem cacar janela: mande /REINICIAR no Telegram (ou crie o arquivo REINICIAR aqui);
REM  o robo sai no fim do ciclo e este .bat sobe de novo em 30s com o market_ai_engine_v5.py da pasta.
REM  STOP_TRADING presente + /REINICIAR = desliga de vez (nao reinicia).
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
set "MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI"
:loop
echo [%date% %time%] iniciando PAPER live - o primeiro ciclo pode levar alguns minutos
echo [%date% %time%] iniciando PAPER live >> logs\live.log
python market_ai_engine_v5.py --log-file logs\live.log live --markets %MERCADOS% --source mt5 --mode paper --send --interval 60
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s >> logs\live.log
if exist STOP_TRADING (echo arquivo STOP_TRADING presente - nao reinicia & goto fim)
timeout /t 30 /nobreak > nul
goto loop
:fim
pause
