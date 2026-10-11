//+------------------------------------------------------------------+
//|                                       TestMobileNotification.mq5 |
//|                             Instant Mobile Push Notification Test |
//+------------------------------------------------------------------+
#property copyright "TradingBot"
#property version   "1.00"
#property script_show_inputs

void OnStart()
{
   string msg = "📱 Test notification from MT5: Push notifications are working!";
   ResetLastError();
   
   if(SendNotification(msg))
   {
      Print("✅ Notification successfully sent to your mobile phone!");
   }
   else
   {
      int err = GetLastError();
      Print("❌ Failed to send notification. Error code: ", err);
      Print("💡 Check: Tools -> Options -> Notifications -> Enable Push Notifications and verify your MetaQuotes ID.");
   }
}
