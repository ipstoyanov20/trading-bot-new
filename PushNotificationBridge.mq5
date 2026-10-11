//+------------------------------------------------------------------+
//|                                       PushNotificationBridge.mq5 |
//|                                      Simple Mobile Push Notifier |
//+------------------------------------------------------------------+
#property copyright "TradingBot"
#property version   "1.00"
#property description "Notifies your mobile phone when started and forwards bot messages."

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(1);
   
   // Send instant notification to your mobile MT5 app on startup
   SendNotification("🚀 Trading Bot Online: MT5 Mobile Notification Bridge connected!");
   Print("📢 PushNotificationBridge started. Startup mobile notification sent!");
   
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| Timer function: checks for notifications from Python bot         |
//+------------------------------------------------------------------+
void OnTimer()
{
   string filename = "push_notification.txt";
   
   if(!FileIsExist(filename, FILE_COMMON)) 
      return;
   
   // Open strictly as ANSI (1-byte text) to prevent Chinese symbols
   int handle = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE) 
      return;
   
   string message = "";
   while(!FileIsEnding(handle))
      message += FileReadString(handle);
      
   FileClose(handle);
   FileDelete(filename, FILE_COMMON);
   
   if(StringLen(message) > 0)
   {
      if(StringLen(message) > 250) 
         message = StringSubstr(message, 0, 250);
         
      if(SendNotification(message))
         Print("✅ Mobile notification delivered: ", message);
      else
         Print("❌ SendNotification failed. Error code: ", GetLastError());
   }
}
