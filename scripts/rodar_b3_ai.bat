@echo off
REM ============================================================================
REM  B3 AI - painel do mini indice, mini dolar e mercado brasileiro x exterior
REM  Abre http://127.0.0.1:8766 sozinho. Somente leitura: o painel NUNCA envia ordens.
REM  Precisa apenas de Python 3.10+ e internet. Para desligar feche esta janela.
REM ============================================================================
cd /d "%~dp0\.."
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist dados mkdir dados
:loop
python -m b3_ai painel --intervalo 60
echo [%date% %time%] painel terminou (codigo %errorlevel%) - reiniciando em 15s
timeout /t 15 /nobreak > nul
goto loop
