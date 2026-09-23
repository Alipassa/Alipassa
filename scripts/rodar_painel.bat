@echo off
REM ============================================================================
REM  GOLD MARKET INTELLIGENCE - PAINEL (cockpit do ouro) no navegador
REM  Coloque na MESMA pasta de market_ai_engine_v6.py e do .env. MT5 aberto e logado.
REM  Abre http://127.0.0.1:8765 sozinho. Apoio a decisao: o painel NUNCA envia ordens.
REM  Alertas tambem no Telegram (--send). Dados sem fonte automatica: dados\manual.json
REM  (copie dados\manual.exemplo.json). Para desligar feche esta janela.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
if not exist dados mkdir dados
:loop
echo [%date% %time%] iniciando PAINEL - a primeira leitura pode levar alguns minutos
python market_ai_engine_v6.py --log-file logs\painel.log painel --source mt5 --send --interval 60
echo [%date% %time%] painel terminou (codigo %errorlevel%) - reiniciando em 30s
timeout /t 30 /nobreak > nul
goto loop
