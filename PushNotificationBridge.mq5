//+------------------------------------------------------------------+
//|                                       PushNotificationBridge.mq5 |
//|                                  Copyright 2026, TradingBot Team |
//|                                             https://www.mql5.com |
//+------------------------------------------------------------------+
#property copyright "TradingBot Team"
#property link      "https://www.mql5.com"
#property version   "1.00"
#property description "Listens for notifications from Python and sends them to MetaTrader Mobile app Messages tab."

input int CheckIntervalSeconds = 1; // Check interval in seconds

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(CheckIntervalSeconds);
   Print("📢 PushNotificationBridge started. Monitoring Common\\Files\\push_notification.txt...");
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
//| Timer function                                                   |
//+------------------------------------------------------------------+
void OnTimer()
{
   string filename = "push_notification.txt";
   
   if(FileIsExist(filename, FILE_COMMON))
   {
      int file_handle = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
      if(file_handle != INVALID_HANDLE)
      {
         string message = "";
         while(!FileIsEnding(file_handle))
         {
            message += FileReadString(file_handle);
         }
         FileClose(file_handle);
         
         // Delete file immediately so it doesn't resend
         FileDelete(filename, FILE_COMMON);
         
         if(StringLen(message) > 0)
         {
            // Truncate to 255 chars if needed (MQL5 SendNotification limit)
            if(StringLen(message) > 255)
               message = StringSubstr(message, 0, 255);
               
            bool res = SendNotification(message);
            if(res)
               Print("✅ Push notification sent to MetaTrader mobile Messages tab: ", message);
            else
               Print("❌ SendNotification failed. Error code: ", GetLastError());
         }
      }
   }
}
