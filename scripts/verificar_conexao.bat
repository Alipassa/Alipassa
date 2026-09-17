@echo off
REM  GOLD AI ENGINE 3.0 - esta tudo pronto? (.env, Telegram, MetaTrader 5). Coloque na MESMA pasta de gold_ai_engine_v3.py.
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
python gold_ai_engine_v3.py setup
pause
