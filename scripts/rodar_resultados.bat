@echo off
REM  GOLD AI ENGINE 3.0 - o que o robo viveu: capital, performance, posicoes, previsoes, calibracao, fatores (2 cliques)
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
if not exist logs mkdir logs
echo ===== STATUS ===== > logs\resultados.txt
python gold_ai_engine_v3.py status >> logs\resultados.txt 2>&1
echo. >> logs\resultados.txt
echo ===== STATS ===== >> logs\resultados.txt
python gold_ai_engine_v3.py stats >> logs\resultados.txt 2>&1
type logs\resultados.txt | more
echo.
echo (copia salva em logs\resultados.txt)
pause
