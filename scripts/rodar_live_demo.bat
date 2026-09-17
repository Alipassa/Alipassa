@echo off
REM ============================================================================
REM  GOLD AI ENGINE 3.0 - execucao REAL na conta DEMO da corretora (ordens de verdade, dinheiro ficticio)
REM  Coloque na MESMA pasta de gold_ai_engine_v3.py e do .env. MT5 aberto e logado na conta DEMO.
REM  ATENCAO: --mode live --authorize envia ordens. Use SOMENTE com a conta demo selecionada no terminal.
REM  A saida aparece NESTA JANELA; inicio e fim de cada execucao ficam em logs\live_demo.log. Reinicia sozinho se cair.
REM  Para desligar: feche esta janela ou crie o arquivo STOP_TRADING nesta pasta.
REM  /STOP no Telegram bloqueia novas entradas; /CLOSE + /CLOSE CONFIRM fecha tudo; /STATUS mostra capital e posicoes.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
:loop
echo [%date% %time%] iniciando LIVE (conta DEMO) - o primeiro ciclo pode levar alguns minutos
echo [%date% %time%] iniciando LIVE (conta DEMO) >> logs\live_demo.log
python gold_ai_engine_v3.py live --source mt5 --mode live --authorize --send --interval 60
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s >> logs\live_demo.log
if exist STOP_TRADING (echo arquivo STOP_TRADING presente - nao reinicia & goto fim)
timeout /t 30 /nobreak > nul
goto loop
:fim
pause
