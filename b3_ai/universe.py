"""Universo acompanhado: alvos nacionais (WIN, WDO) e os fatores internacionais confrontados com eles.

Os símbolos internacionais foram escolhidos entre os que o feed público entrega SEM atraso (índices e commodities em
contratos contínuos de 24 h, moedas, juros, VIX). Futuros de bolsa (CME, SGX, B3) chegam atrasados nesse feed; o atraso
de cada série é lido do próprio feed e mostrado no painel."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    key: str          # identificador interno
    tv: str           # símbolo TradingView
    yahoo: str        # símbolo Yahoo (reserva do histórico diário)
    name: str
    group: str
    kind: str = "pct"  # "pct": retorno percentual · "yield": variação em pontos percentuais da taxa
    factor: bool = False   # entra na regressão exterior → Brasil
    why: str = ""          # canal de transmissão para o Brasil


TARGETS = {
    "WIN": Asset("WIN", "BMFBOVESPA:WIN1!", "^BVSP", "Mini Índice (WIN)", "Brasil"),
    "WDO": Asset("WDO", "BMFBOVESPA:WDO1!", "BRL=X", "Mini Dólar (WDO)", "Brasil"),
}

ASSETS: list[Asset] = [
    # ---- Brasil
    Asset("IBOV", "BMFBOVESPA:IBOV", "^BVSP", "Ibovespa", "Brasil"),
    Asset("USDBRL", "FX_IDC:USDBRL", "BRL=X", "Dólar comercial (USD/BRL)", "Brasil"),
    Asset("DI", "BMFBOVESPA:DI11!", "", "DI futuro (1º venc.)", "Brasil", "yield"),
    Asset("BR10Y", "TVC:BR10Y", "", "Juro Brasil 10 anos", "Brasil", "yield"),
    # ---- fatores internacionais (entram no confronto)
    Asset("ES", "OANDA:SPX500USD", "ES=F", "S&P 500 (24 h)", "EUA — ações", factor=True,
          why="apetite a risco global: bolsa americana puxa as emergentes"),
    Asset("NQ", "OANDA:NAS100USD", "NQ=F", "Nasdaq 100 (24 h)", "EUA — ações"),
    Asset("DXY", "TVC:DXY", "DX-Y.NYB", "Índice do dólar (DXY)", "Dólar e juros", factor=True,
          why="dólar forte no mundo tira fluxo de emergentes e pressiona o real"),
    Asset("US10Y", "TVC:US10Y", "^TNX", "Treasury 10 anos", "Dólar e juros", "yield", factor=True,
          why="juro americano alto compete com a renda variável e com o carry do real"),
    Asset("VIX", "TVC:VIX", "^VIX", "VIX (medo)", "Risco", factor=True,
          why="aversão a risco: VIX em alta derruba bolsas e moedas emergentes"),
    Asset("BRENT", "TVC:UKOIL", "BZ=F", "Petróleo Brent", "Commodities", factor=True,
          why="Petrobras pesa no Ibovespa; petróleo também entra na balança comercial"),
    Asset("IRON", "SGX:FEF1!", "", "Minério de ferro (SGX)", "Commodities", factor=True,
          why="Vale e siderúrgicas; termômetro da demanda chinesa"),
    Asset("COPPER", "OANDA:XCUUSD", "HG=F", "Cobre", "Commodities", factor=True,
          why="ciclo industrial global e China"),
    Asset("EEM", "AMEX:EEM", "EEM", "Emergentes (EEM)", "Emergentes", factor=True,
          why="fluxo para mercados emergentes como classe"),
    Asset("USDMXN", "FX_IDC:USDMXN", "MXN=X", "Dólar × peso mexicano", "Emergentes", factor=True,
          why="moeda par do real: mede o humor com a América Latina"),
    Asset("HSI", "TVC:HSI", "^HSI", "Hang Seng (China/HK)", "Ásia e Europa", factor=True,
          why="China é o maior parceiro comercial do Brasil"),
    Asset("SX5E", "TVC:SX5E", "^STOXX50E", "Euro Stoxx 50", "Ásia e Europa", factor=True,
          why="sessão europeia dá o tom da abertura brasileira"),
    # ---- contexto (não entram na regressão)
    Asset("GOLD", "TVC:GOLD", "GC=F", "Ouro", "Commodities"),
    Asset("BTC", "BITSTAMP:BTCUSD", "BTC-USD", "Bitcoin", "Risco"),
    Asset("NI225", "TVC:NI225", "^N225", "Nikkei 225", "Ásia e Europa"),
    Asset("DJI", "OANDA:US30USD", "^DJI", "Dow Jones (24 h)", "EUA — ações"),
    Asset("US02Y", "TVC:US02Y", "", "Treasury 2 anos", "Dólar e juros", "yield"),
    # ---- Brasil negociado em Nova York
    Asset("EWZ", "AMEX:EWZ", "EWZ", "EWZ (Brasil em NY)", "Brasil em NY"),
    Asset("PBR", "NYSE:PBR", "PBR", "Petrobras ADR", "Brasil em NY"),
    Asset("VALE_ADR", "NYSE:VALE", "VALE", "Vale ADR", "Brasil em NY"),
]

BY_KEY = {a.key: a for a in list(TARGETS.values()) + ASSETS}
FACTORS = [a for a in ASSETS if a.factor]

SECTOR_PT = {
    "Finance": "Financeiro", "Utilities": "Utilidade pública", "Technology Services": "Serviços de tecnologia",
    "Producer Manufacturing": "Bens industriais", "Retail Trade": "Varejo", "Health Technology": "Saúde — tecnologia",
    "Process Industries": "Indústria de processo", "Electronic Technology": "Tecnologia eletrônica",
    "Consumer Non-Durables": "Consumo não durável", "Non-Energy Minerals": "Mineração e siderurgia",
    "Transportation": "Transporte", "Commercial Services": "Serviços comerciais", "Consumer Services": "Serviços ao consumidor",
    "Consumer Durables": "Consumo durável", "Industrial Services": "Serviços industriais", "Health Services": "Saúde — serviços",
    "Energy Minerals": "Petróleo e gás", "Distribution Services": "Distribuição", "Communications": "Telecomunicações",
    "Miscellaneous": "Diversos", "Government": "Governo",
}
