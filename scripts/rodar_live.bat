@echo off
REM ============================================================================
REM  MARKET AI ENGINE — PAPER multi-mercado 24/7 (reinicia sozinho se cair)
REM  Coloque na MESMA pasta de market_ai_engine_v5.py e do .env. MT5 aberto e logado.
REM  Modo PAPER: nunca envia ordem real. Para parar: feche esta janela ou /STOP no Telegram.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
set MERCADOS=XAUUSD,US500,EURUSD,USDJPY,WTI
:loop
echo [%date% %time%] iniciando PAPER live >> logs\live.log
python market_ai_engine_v5.py live --markets %MERCADOS% --source mt5 --mode paper --send --interval 60 >> logs\live.log 2>&1
echo [%date% %time%] live terminou (codigo %errorlevel%) - reiniciando em 30s >> logs\live.log
timeout /t 30 /nobreak > nul
goto loop
