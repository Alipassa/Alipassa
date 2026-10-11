//+------------------------------------------------------------------+
//| VWAP Ultimate 2.3 — B3 Mini Dólar (WDO)                         |
//| Otimizado para Mini Dólar WDO (B3)                               |
//| VWAP + 3 Bandas + EMA + MACD + Swing + Sinais + Dashboard        |
//+------------------------------------------------------------------+
#property copyright "Copyright 2024, Alipassa"
#property link      "https://www.mql5.com/en/users/alipassa"
#property version   "2.31"
#property indicator_chart_window
#property indicator_buffers 15
#property indicator_plots   15

//--- Plot 0: VWAP
#property indicator_label1  "VWAP"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrDodgerBlue
#property indicator_width1  3

//--- Plot 1: Upper 3σ
#property indicator_label2  "Upper 3σ"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrRed
#property indicator_style2  STYLE_DOT
#property indicator_width2  1

//--- Plot 2: Lower 3σ
#property indicator_label3  "Lower 3σ"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrRed
#property indicator_style3  STYLE_DOT
#property indicator_width3  1

//--- Plot 3: Upper 2σ
#property indicator_label4  "Upper 2σ"
#property indicator_type4   DRAW_LINE
#property indicator_color4  clrOrange
#property indicator_style4  STYLE_DASH
#property indicator_width4  1

//--- Plot 4: Lower 2σ
#property indicator_label5  "Lower 2σ"
#property indicator_type5   DRAW_LINE
#property indicator_color5  clrOrange
#property indicator_style5  STYLE_DASH
#property indicator_width5  1

//--- Plot 5: Upper 1σ
#property indicator_label6  "Upper 1σ"
#property indicator_type6   DRAW_LINE
#property indicator_color6  clrLimeGreen
#property indicator_style6  STYLE_SOLID
#property indicator_width6  1

//--- Plot 6: Lower 1σ
#property indicator_label7  "Lower 1σ"
#property indicator_type7   DRAW_LINE
#property indicator_color7  clrLimeGreen
#property indicator_style7  STYLE_SOLID
#property indicator_width7  1

//--- Plot 7: EMA Rápida
#property indicator_label8  "EMA 9"
#property indicator_type8   DRAW_LINE
#property indicator_color8  clrYellow
#property indicator_width8  2

//--- Plot 8: EMA Lenta
#property indicator_label9  "EMA 21"
#property indicator_type9   DRAW_LINE
#property indicator_color9  clrMagenta
#property indicator_width9  2

//--- Plot 9: Swing High (Topo)
#property indicator_label10 "Topo"
#property indicator_type10  DRAW_ARROW
#property indicator_color10 clrAqua
#property indicator_width10 2

//--- Plot 10: Swing Low (Fundo)
#property indicator_label11 "Fundo"
#property indicator_type11  DRAW_ARROW
#property indicator_color11 clrCyan
#property indicator_width11 2

//--- Plot 11: Compra
#property indicator_label12 "Compra"
#property indicator_type12  DRAW_ARROW
#property indicator_color12 clrLime
#property indicator_width12 3

//--- Plot 12: Venda
#property indicator_label13 "Venda"
#property indicator_type13  DRAW_ARROW
#property indicator_color13 clrRed
#property indicator_width13 3

//--- Plot 13: Compra Forte
#property indicator_label14 "Compra Forte ★"
#property indicator_type14  DRAW_ARROW
#property indicator_color14 clrGold
#property indicator_width14 4

//--- Plot 14: Venda Forte
#property indicator_label15 "Venda Forte ★"
#property indicator_type15  DRAW_ARROW
#property indicator_color15 clrOrangeRed
#property indicator_width15 4

//==================================================================
// INPUTS
//==================================================================

input group "===== VWAP ====="
input double           Band1Mult        = 1.0;
input double           Band2Mult        = 2.0;
input double           Band3Mult        = 3.0;

input group "===== SESSÃO B3 (WDO) ====="
input int              BrokerGMTOffset  = 0;    // Offset GMT do broker (0=UTC, -3=BRT, 3=Moscou)
input int              SessaoAbre       = 9;    // Abertura B3 em BRT (WDO: 9h)
input int              SessaoFecha      = 18;   // Fechamento B3 em BRT (WDO: 18h)
input int              SessaoFechaMin   = 0;    // Minuto do fechamento
input bool             FiltroSessao     = true; // Sinais apenas dentro da sessão B3

input group "===== MÉDIAS MÓVEIS ====="
input int              FastMAPeriod     = 9;
input int              SlowMAPeriod     = 21;
input ENUM_MA_METHOD   MAMethod         = MODE_EMA;

input group "===== MACD ====="
input int              FastEMA          = 12;
input int              SlowEMA          = 26;
input int              SignalPeriod     = 9;

input group "===== TOPOS E FUNDOS ====="
input int              SwingLookback    = 3;   // Barras de confirmação (3=mais ágil, 5=mais seguro)

input group "===== FILTRO ATR ====="
input bool             UseATRFilter     = true;
input int              ATRPeriod        = 14;
input double           ATRMultiplier    = 1.5;

input group "===== FILTROS DE SINAL ====="
input bool             FilterByVWAP     = true;
input bool             NeedMACross      = true;
input bool             NeedMACD         = true;
input bool             NeedSwing        = true;
input bool             NeedBandTouch    = false;

input group "===== ALERTAS ====="
input bool             EnableAlerts     = true;
input bool             EnablePush       = false;
input int              MaxAlertsPerBar  = 5;

//==================================================================
// BUFFERS
//==================================================================

double VWAPBuf[];
double Up3[], Lo3[];
double Up2[], Lo2[];
double Up1[], Lo1[];
double FastMABuf[], SlowMABuf[];
double SwingHighBuf[], SwingLowBuf[];
double BuyBuf[], SellBuf[];
double StrongBuyBuf[], StrongSellBuf[];

//--- Internos
double CumTPV[], CumVol[], CumTPV2[];
double ATRBuf[];

int hFastMA, hSlowMA, hMACD, hATR;
datetime LastAlertBarTime = 0;
int      AlertBarCount    = 0;

//+------------------------------------------------------------------+
// Converte timestamp do broker para BRT (UTC-3)
//+------------------------------------------------------------------+
datetime ToBRT(datetime brokerTime)
{
   return brokerTime - BrokerGMTOffset * 3600 - 3 * 3600;
}

void GetBRTTime(datetime barTime, int &hora, int &minuto)
{
   MqlDateTime dt;
   TimeToStruct(ToBRT(barTime), dt);
   hora   = dt.hour;
   minuto = dt.min;
}

bool DentroSessaoB3(datetime barTime)
{
   int h, m;
   GetBRTTime(barTime, h, m);
   int minBar   = h * 60 + m;
   int minAbre  = SessaoAbre  * 60;
   int minFecha = SessaoFecha * 60 + SessaoFechaMin;
   return(minBar >= minAbre && minBar <= minFecha);
}

// Reset do VWAP: novo dia calendário BRT OU cruzamento do horário de abertura
bool IsSessionReset(datetime prevTime, datetime currTime)
{
   MqlDateTime p, c;
   TimeToStruct(ToBRT(prevTime), p);
   TimeToStruct(ToBRT(currTime), c);

   int minPrev = p.hour * 60 + p.min;
   int minCurr = c.hour * 60 + c.min;
   int minAbre = SessaoAbre * 60;

   bool novoDia = (c.day != p.day || c.mon != p.mon || c.year != p.year);

   return (novoDia && minCurr >= minAbre) ||
          (!novoDia && minPrev < minAbre && minCurr >= minAbre);
}

//+------------------------------------------------------------------+
int OnInit()
{
   SetIndexBuffer(0,  VWAPBuf,       INDICATOR_DATA);
   SetIndexBuffer(1,  Up3,           INDICATOR_DATA);
   SetIndexBuffer(2,  Lo3,           INDICATOR_DATA);
   SetIndexBuffer(3,  Up2,           INDICATOR_DATA);
   SetIndexBuffer(4,  Lo2,           INDICATOR_DATA);
   SetIndexBuffer(5,  Up1,           INDICATOR_DATA);
   SetIndexBuffer(6,  Lo1,           INDICATOR_DATA);
   SetIndexBuffer(7,  FastMABuf,     INDICATOR_DATA);
   SetIndexBuffer(8,  SlowMABuf,     INDICATOR_DATA);
   SetIndexBuffer(9,  SwingHighBuf,  INDICATOR_DATA);
   SetIndexBuffer(10, SwingLowBuf,   INDICATOR_DATA);
   SetIndexBuffer(11, BuyBuf,        INDICATOR_DATA);
   SetIndexBuffer(12, SellBuf,       INDICATOR_DATA);
   SetIndexBuffer(13, StrongBuyBuf,  INDICATOR_DATA);
   SetIndexBuffer(14, StrongSellBuf, INDICATOR_DATA);

   for(int i = 0; i < 15; i++)
      PlotIndexSetDouble(i, PLOT_EMPTY_VALUE, EMPTY_VALUE);

   PlotIndexSetInteger(9,  PLOT_ARROW, 242); PlotIndexSetInteger(9,  PLOT_ARROW_SHIFT, -10);
   PlotIndexSetInteger(10, PLOT_ARROW, 241); PlotIndexSetInteger(10, PLOT_ARROW_SHIFT,  10);
   PlotIndexSetInteger(11, PLOT_ARROW, 233); PlotIndexSetInteger(11, PLOT_ARROW_SHIFT,  18);
   PlotIndexSetInteger(12, PLOT_ARROW, 234); PlotIndexSetInteger(12, PLOT_ARROW_SHIFT, -18);
   PlotIndexSetInteger(13, PLOT_ARROW, 171); PlotIndexSetInteger(13, PLOT_ARROW_SHIFT,  30);
   PlotIndexSetInteger(14, PLOT_ARROW, 170); PlotIndexSetInteger(14, PLOT_ARROW_SHIFT, -30);

   ArraySetAsSeries(VWAPBuf,      true);
   ArraySetAsSeries(Up3,          true); ArraySetAsSeries(Lo3,          true);
   ArraySetAsSeries(Up2,          true); ArraySetAsSeries(Lo2,          true);
   ArraySetAsSeries(Up1,          true); ArraySetAsSeries(Lo1,          true);
   ArraySetAsSeries(FastMABuf,    true); ArraySetAsSeries(SlowMABuf,    true);
   ArraySetAsSeries(SwingHighBuf, true); ArraySetAsSeries(SwingLowBuf,  true);
   ArraySetAsSeries(BuyBuf,       true); ArraySetAsSeries(SellBuf,      true);
   ArraySetAsSeries(StrongBuyBuf, true); ArraySetAsSeries(StrongSellBuf,true);

   hFastMA = iMA(_Symbol, PERIOD_CURRENT, FastMAPeriod, 0, MAMethod, PRICE_CLOSE);
   hSlowMA = iMA(_Symbol, PERIOD_CURRENT, SlowMAPeriod, 0, MAMethod, PRICE_CLOSE);
   hMACD   = iMACD(_Symbol, PERIOD_CURRENT, FastEMA, SlowEMA, SignalPeriod, PRICE_CLOSE);
   hATR    = iATR(_Symbol, PERIOD_CURRENT, ATRPeriod);

   if(hFastMA == INVALID_HANDLE || hSlowMA == INVALID_HANDLE ||
      hMACD   == INVALID_HANDLE || hATR    == INVALID_HANDLE)
      return(INIT_FAILED);

   IndicatorSetString(INDICATOR_SHORTNAME, "VWAP Ultimate 2.3 [WDO]");
   IndicatorSetInteger(INDICATOR_DIGITS, _Digits);

   CreateDashboard();
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   IndicatorRelease(hFastMA); IndicatorRelease(hSlowMA);
   IndicatorRelease(hMACD);   IndicatorRelease(hATR);
   ObjectDelete(0, "VWAP_DASH");
}

//+------------------------------------------------------------------+
void CreateDashboard()
{
   ObjectDelete(0, "VWAP_DASH");
   ObjectCreate(0, "VWAP_DASH", OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, "VWAP_DASH", OBJPROP_CORNER,    CORNER_RIGHT_UPPER);
   ObjectSetInteger(0, "VWAP_DASH", OBJPROP_XDISTANCE, 20);
   ObjectSetInteger(0, "VWAP_DASH", OBJPROP_YDISTANCE, 20);
   ObjectSetInteger(0, "VWAP_DASH", OBJPROP_FONTSIZE,  10);
   ObjectSetString (0, "VWAP_DASH", OBJPROP_FONT,      "Arial Bold");
   ObjectSetInteger(0, "VWAP_DASH", OBJPROP_COLOR,     clrWhite);
}

void UpdateDashboard(string trend, string momentum, string structure, bool sessaoAtiva)
{
   color  dashColor = (StringFind(trend, "ALTA") >= 0) ? clrLime : clrRed;
   string sessao    = sessaoAtiva ? "ABERTA ●" : "FECHADA ○";

   ObjectSetInteger(0, "VWAP_DASH", OBJPROP_COLOR, dashColor);
   ObjectSetString(0, "VWAP_DASH", OBJPROP_TEXT,
      "VWAP ULTIMATE 2.3 [WDO]\n"
      "------------------------\n"
      "Tendência:  " + trend     + "\n" +
      "Momentum:   " + momentum  + "\n" +
      "Estrutura:  " + structure + "\n" +
      "Sessão WDO: " + sessao);
}

//+------------------------------------------------------------------+
void FireSignal(string message, datetime barTime)
{
   if(!EnableAlerts) return;

   if(barTime != LastAlertBarTime)
   {
      LastAlertBarTime = barTime;
      AlertBarCount    = 0;
   }

   if(AlertBarCount >= MaxAlertsPerBar) return;

   Alert(message);
   if(EnablePush) SendNotification(message);
   AlertBarCount++;
}

//+------------------------------------------------------------------+
bool IsSwingHigh(const double &high[], int idx, int lb, int total)
{
   if(idx + lb >= total || idx - lb < 0) return false;
   for(int k = 1; k <= lb; k++)
      if(high[idx] <= high[idx + k] || high[idx] <= high[idx - k]) return false;
   return true;
}

bool IsSwingLow(const double &low[], int idx, int lb, int total)
{
   if(idx + lb >= total || idx - lb < 0) return false;
   for(int k = 1; k <= lb; k++)
      if(low[idx] >= low[idx + k] || low[idx] >= low[idx - k]) return false;
   return true;
}

bool NearSwingBottom(int bar, int total)
{
   int maxBars = SwingLookback * 3;
   for(int i = 0; i <= maxBars; i++)
   {
      int idx = bar + i;
      if(idx >= total) break;
      if(SwingLowBuf[idx] != EMPTY_VALUE && SwingLowBuf[idx] != 0) return true;
   }
   return false;
}

bool NearSwingTop(int bar, int total)
{
   int maxBars = SwingLookback * 3;
   for(int i = 0; i <= maxBars; i++)
   {
      int idx = bar + i;
      if(idx >= total) break;
      if(SwingHighBuf[idx] != EMPTY_VALUE && SwingHighBuf[idx] != 0) return true;
   }
   return false;
}

bool ATRNearLevel(double price, double level, double atr)
{
   if(!UseATRFilter) return true;
   return(MathAbs(price - level) <= atr * ATRMultiplier);
}

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double   &open[],
                const double   &high[],
                const double   &low[],
                const double   &close[],
                const long     &tick_volume[],
                const long     &volume[],
                const int      &spread[])
{
   int minBars = MathMax(SlowEMA + SignalPeriod + 10, ATRPeriod + 10);
   if(rates_total < minBars) return(0);

   ArraySetAsSeries(time,        true); ArraySetAsSeries(open,        true);
   ArraySetAsSeries(high,        true); ArraySetAsSeries(low,         true);
   ArraySetAsSeries(close,       true); ArraySetAsSeries(volume,      true);
   ArraySetAsSeries(tick_volume, true);

   double FastMA[], SlowMA[], MACDLine[], SignalLine[];
   ArraySetAsSeries(FastMA,     true); ArraySetAsSeries(SlowMA,     true);
   ArraySetAsSeries(MACDLine,   true); ArraySetAsSeries(SignalLine,  true);
   ArraySetAsSeries(ATRBuf,     true);

   if(CopyBuffer(hFastMA, 0, 0, rates_total, FastMA)     <= 0) return(0);
   if(CopyBuffer(hSlowMA, 0, 0, rates_total, SlowMA)     <= 0) return(0);
   if(CopyBuffer(hMACD,   0, 0, rates_total, MACDLine)   <= 0) return(0);
   if(CopyBuffer(hMACD,   1, 0, rates_total, SignalLine)  <= 0) return(0);
   if(CopyBuffer(hATR,    0, 0, rates_total, ATRBuf)      <= 0) return(0);

   ArrayResize(CumTPV,  rates_total);
   ArrayResize(CumVol,  rates_total);
   ArrayResize(CumTPV2, rates_total);
   ArraySetAsSeries(CumTPV,  true);
   ArraySetAsSeries(CumVol,  true);
   ArraySetAsSeries(CumTPV2, true);

   int start = (prev_calculated > 1) ? prev_calculated - 1 : rates_total - 1;

   for(int i = start; i >= 0; i--)
   {
      double tp  = (high[i] + low[i] + close[i]) / 3.0;
      double vol = (volume[i] > 0) ? (double)volume[i] : (double)tick_volume[i];

      //--- Reset do VWAP na abertura da sessão B3 (9h BRT)
      bool reset = (i == rates_total - 1);
      if(!reset)
         reset = IsSessionReset(time[i + 1], time[i]);

      if(reset)
      { CumTPV[i] = tp * vol; CumVol[i] = vol; CumTPV2[i] = tp * tp * vol; }
      else
      { CumTPV[i] = CumTPV[i+1] + tp * vol; CumVol[i] = CumVol[i+1] + vol; CumTPV2[i] = CumTPV2[i+1] + tp * tp * vol; }

      double vwap = 0.0, sd = 0.0;
      if(CumVol[i] > 0.0)
      {
         vwap = CumTPV[i] / CumVol[i];
         double var = (CumTPV2[i] / CumVol[i]) - (vwap * vwap);
         if(var < 0.0) var = 0.0;
         sd = MathSqrt(var);
      }

      VWAPBuf[i] = vwap;
      Up3[i] = vwap + Band3Mult * sd;  Lo3[i] = vwap - Band3Mult * sd;
      Up2[i] = vwap + Band2Mult * sd;  Lo2[i] = vwap - Band2Mult * sd;
      Up1[i] = vwap + Band1Mult * sd;  Lo1[i] = vwap - Band1Mult * sd;

      FastMABuf[i] = FastMA[i];
      SlowMABuf[i] = SlowMA[i];

      SwingHighBuf[i]  = EMPTY_VALUE; SwingLowBuf[i]   = EMPTY_VALUE;
      BuyBuf[i]        = EMPTY_VALUE; SellBuf[i]        = EMPTY_VALUE;
      StrongBuyBuf[i]  = EMPTY_VALUE; StrongSellBuf[i]  = EMPTY_VALUE;

      if(IsSwingHigh(high, i, SwingLookback, rates_total)) SwingHighBuf[i] = high[i];
      if(IsSwingLow(low,   i, SwingLookback, rates_total)) SwingLowBuf[i]  = low[i];

      if(i >= rates_total - 1) continue;

      //--- Filtro de sessão WDO: 9h às 18h BRT
      bool sessaoOK = !FiltroSessao || DentroSessaoB3(time[i]);
      if(!sessaoOK) continue;

      double atr = ATRBuf[i];

      bool maCrossUp   = (FastMA[i] > SlowMA[i])       && (FastMA[i+1]   <= SlowMA[i+1]);
      bool maCrossDown = (FastMA[i] < SlowMA[i])       && (FastMA[i+1]   >= SlowMA[i+1]);
      bool macdCrossUp = (MACDLine[i] > SignalLine[i]) && (MACDLine[i+1] <= SignalLine[i+1]);
      bool macdCrossDn = (MACDLine[i] < SignalLine[i]) && (MACDLine[i+1] >= SignalLine[i+1]);

      bool nearBottom = NearSwingBottom(i, rates_total);
      bool nearTop    = NearSwingTop(i,   rates_total);

      bool buyOK = true;
      if(FilterByVWAP  && close[i] <= vwap)  buyOK = false;
      if(NeedMACross   && !maCrossUp)         buyOK = false;
      if(NeedMACD      && !macdCrossUp)       buyOK = false;
      if(NeedSwing     && !nearBottom)        buyOK = false;
      if(NeedBandTouch && low[i] > Lo1[i])   buyOK = false;

      bool sellOK = true;
      if(FilterByVWAP  && close[i] >= vwap)  sellOK = false;
      if(NeedMACross   && !maCrossDown)       sellOK = false;
      if(NeedMACD      && !macdCrossDn)       sellOK = false;
      if(NeedSwing     && !nearTop)           sellOK = false;
      if(NeedBandTouch && high[i] < Up1[i])  sellOK = false;

      if(buyOK)  BuyBuf[i]  = low[i]  - atr * 0.30;
      if(sellOK) SellBuf[i] = high[i] + atr * 0.30;

      if(maCrossUp && macdCrossUp && nearBottom &&
         close[i] > vwap && close[i] < Up1[i] &&
         ATRNearLevel(close[i], low[i], atr))
         StrongBuyBuf[i] = low[i] - atr;

      if(maCrossDown && macdCrossDn && nearTop &&
         close[i] < vwap && close[i] > Lo1[i] &&
         ATRNearLevel(close[i], high[i], atr))
         StrongSellBuf[i] = high[i] + atr;
   }

   //--- Dashboard
   string trend = (FastMABuf[0] > SlowMABuf[0]) ? "ALTA ▲" : "BAIXA ▼";
   string momentum;
   double macd0[], sig0[];
   ArraySetAsSeries(macd0, true); ArraySetAsSeries(sig0, true);
   if(CopyBuffer(hMACD, 0, 0, 1, macd0) > 0 &&
      CopyBuffer(hMACD, 1, 0, 1, sig0)  > 0)
      momentum = (macd0[0] > sig0[0]) ? "CONFIRMADO ✓" : "FRACO ✗";
   else
      momentum = "---";

   string structure   = (close[0] > VWAPBuf[0]) ? "ACIMA DO VWAP" : "ABAIXO DO VWAP";
   bool   sessaoAtiva = DentroSessaoB3(time[0]);
   UpdateDashboard(trend, momentum, structure, sessaoAtiva);

   //--- Alertas (barra 1 = última fechada)
   if(rates_total > 5)
   {
      datetime barTime1 = time[1];
      if(BuyBuf[1]        != EMPTY_VALUE) FireSignal("WDO COMPRA — "        + _Symbol, barTime1);
      if(SellBuf[1]       != EMPTY_VALUE) FireSignal("WDO VENDA — "         + _Symbol, barTime1);
      if(StrongBuyBuf[1]  != EMPTY_VALUE) FireSignal("WDO COMPRA FORTE ★ — "+ _Symbol, barTime1);
      if(StrongSellBuf[1] != EMPTY_VALUE) FireSignal("WDO VENDA FORTE ★ — " + _Symbol, barTime1);
   }

   return(rates_total);
}
//+------------------------------------------------------------------+
