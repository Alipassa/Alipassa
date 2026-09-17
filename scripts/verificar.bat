@echo off
REM  GOLD AI ENGINE 3.0 - roda a suite de testes e um ciclo de demonstracao (2 cliques)
cd /d "%~dp0\.."
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
python -m unittest -q
python gold_ai_engine_v3.py demo --scenarios neutro
pause
