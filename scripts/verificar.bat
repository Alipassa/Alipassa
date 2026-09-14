@echo off
REM  MARKET AI DOCTOR - tudo esta funcionando? qual a eficiencia?  (2 cliques; MT5 aberto para o teste de conexao)
cd /d "%~dp0"
if not exist logs mkdir logs
python market_ai_engine_v4.py doctor --mt5 --telegram --out logs\doctor.txt
echo.
echo (copia salva em logs\doctor.txt)
pause
