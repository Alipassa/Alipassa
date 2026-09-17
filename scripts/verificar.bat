@echo off
REM  GOLD AI ENGINE 3.0 - demonstracao com dados ficticios + verificacao de prontidao (sem mandar mensagem). Mesma pasta do gold_ai_engine_v3.py.
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
python gold_ai_engine_v3.py demo --scenarios neutro
python gold_ai_engine_v3.py setup --no-message
pause
