//+------------------------------------------------------------------+
//| B3AI_Calendario.mq5                                              |
//| Exporta o calendario economico do MetaTrader para o painel B3 AI. |
//| SOMENTE LEITURA: este robo nao envia, altera nem fecha ordens.   |
//+------------------------------------------------------------------+
#property copyright   "B3 AI"
#property version     "1.00"
#property description "Exporta o calendario economico (EUA, Brasil, Zona do Euro, China) para o painel B3 AI."
#property description "Somente leitura: nao envia ordens."

input int DiasAntes  = 3;    // dias para tras
input int DiasDepois = 21;   // dias para frente
input int Intervalo  = 30;   // segundos entre atualizacoes

string Paises[] = {"US", "BR", "EU", "CN"};
const string ARQ = "b3ai_calendario.csv";
const string TMP = "b3ai_calendario.tmp";

//--- valor do calendario: inteiro multiplicado por 10^6, ou LONG_MIN quando nao existe
string Num(const long v)
  {
   if(v == LONG_MIN)
      return "";
   return DoubleToString((double)v / 1000000.0, 6);
  }

void Exportar()
  {
   datetime agora = TimeTradeServer();
   if(agora == 0)
      agora = TimeCurrent();
   datetime de  = agora - DiasAntes * 86400;
   datetime ate = agora + DiasDepois * 86400;
   long desloc = (long)(TimeTradeServer() - TimeGMT());     // fuso do servidor, em segundos

   int h = FileOpen(TMP, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON, 0, CP_UTF8);
   if(h == INVALID_HANDLE)
     {
      Print("B3AI_Calendario: nao consegui criar o arquivo, erro ", GetLastError());
      return;
     }
   FileWriteString(h, "gerado_utc;" + IntegerToString((long)TimeGMT()) + "\r\n");
   FileWriteString(h, "utc;pais;moeda;importancia;nome;divulgado;esperado;anterior;anterior_revisado;impacto;unidade;multiplicador;evento_id;valor_id\r\n");

   int total = 0;
   for(int p = 0; p < ArraySize(Paises); p++)
     {
      MqlCalendarValue vals[];
      int n = CalendarValueHistory(vals, de, ate, Paises[p]);
      for(int i = 0; i < n; i++)
        {
         MqlCalendarEvent ev;
         if(!CalendarEventById(vals[i].event_id, ev))
            continue;
         if(ev.importance < CALENDAR_IMPORTANCE_MODERATE)
            continue;
         string moeda = "";
         MqlCalendarCountry c;
         if(CalendarCountryById(ev.country_id, c))
            moeda = c.currency;
         string nome = ev.name;
         StringReplace(nome, ";", ",");
         long utc = (long)vals[i].time - desloc;
         string linha = IntegerToString(utc) + ";" + Paises[p] + ";" + moeda + ";" + IntegerToString((int)ev.importance) + ";" + nome + ";" +
                        Num(vals[i].actual_value) + ";" + Num(vals[i].forecast_value) + ";" + Num(vals[i].prev_value) + ";" +
                        Num(vals[i].revised_prev_value) + ";" + IntegerToString((int)vals[i].impact_type) + ";" +
                        IntegerToString((int)ev.unit) + ";" + IntegerToString((int)ev.multiplier) + ";" +
                        IntegerToString((long)ev.id) + ";" + IntegerToString((long)vals[i].id) + "\r\n";
         FileWriteString(h, linha);
         total++;
        }
     }
   FileClose(h);
   if(!FileMove(TMP, FILE_COMMON, ARQ, FILE_COMMON | FILE_REWRITE))
      Print("B3AI_Calendario: nao consegui substituir o arquivo, erro ", GetLastError());
   Comment("B3 AI - calendario exportado: ", total, " eventos as ", TimeToString(TimeLocal(), TIME_SECONDS),
           "\nSomente leitura: nenhuma ordem e enviada.");
  }

int OnInit()
  {
   EventSetTimer(MathMax(Intervalo, 10));
   Exportar();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

void OnTimer()
  {
   Exportar();
  }

void OnTick()
  {
  }
//+------------------------------------------------------------------+
