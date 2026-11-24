@echo off
REM TURBO IA - painel com botao "Iniciar IA" que analisa o grafico e opera no MetaTrader 5
REM Requisitos: Python 3.10+ (64 bits), terminal MT5 aberto e logado (conta DEMO), pip install MetaTrader5
cd /d %~dp0..
python -m pip install --quiet MetaTrader5
python -m turbo_ia --broker mt5
pause
