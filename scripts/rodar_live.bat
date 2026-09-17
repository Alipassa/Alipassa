@echo off
REM ============================================================================
REM  GOLD AI ENGINE 3.0 - PAPER 24/7 com dados reais do MT5 (reinicia sozinho se cair)
REM  Coloque na MESMA pasta de gold_ai_engine_v3.py e do .env. MT5 aberto e logado.
REM  Modo PAPER: nunca envia ordem real. /STOP no Telegram bloqueia NOVAS ENTRADAS (o robo continua analisando).
REM  A saida aparece NESTA JANELA; inicio e fim de cada execucao ficam em logs\live.log.
REM  Para desligar de vez: feche esta janela ou crie o arquivo STOP_TRADING nesta pasta.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
:loop
echo [%date% %time%] iniciando PAPER live - o primeiro ciclo pode levar alguns minutos
echo [%date% %time%] iniciando PAPER live >> logs\live.log
python gold_ai_engine_v3.py live --source mt5 --mode paper --send --interval 60
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s >> logs\live.log
if exist STOP_TRADING (echo arquivo STOP_TRADING presente - nao reinicia & goto fim)
timeout /t 30 /nobreak > nul
goto loop
:fim
pause
