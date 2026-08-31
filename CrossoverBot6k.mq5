//+------------------------------------------------------------------+
//|                                              CrossoverBot6k.mq5 |
//|                                                     Antigravity |
//+------------------------------------------------------------------+
#property copyright "Antigravity"
#property link      ""
#property version   "1.00"

#include <Trade\Trade.mqh>

//--- Input Parameters
input double InpLotSize = 1.0;         // Lot Size (will be executed 5x)
input double InpStopLoss = 1.5;        // Stop Loss Distance (in price)
input double InpTakeProfit = 3.0;      // Take Profit Distance (in price)
input int    InpMagicNumber = 123456;  // Magic Number

//--- Funded Rules configuration
const double STARTING_BALANCE = 6000.0;
const double PROFIT_TARGET = 480.0;
const double MAX_LOSS_LIMIT = 600.0;
const double DAILY_LOSS_LIMIT = 300.0;

//--- Indicator Handles
int handle_ema50;
int handle_ema200;
int handle_stoch;

CTrade trade;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagicNumber);
   
   // Initialize EMAs
   handle_ema50 = iMA(_Symbol, PERIOD_M1, 50, 0, MODE_EMA, PRICE_CLOSE);
   handle_ema200 = iMA(_Symbol, PERIOD_M1, 200, 0, MODE_EMA, PRICE_CLOSE);
   
   // Initialize Stochastic (5, 3, 3)
   handle_stoch = iStochastic(_Symbol, PERIOD_M1, 5, 3, 3, MODE_SMA, STO_LOWHIGH);
   
   if(handle_ema50 == INVALID_HANDLE || handle_ema200 == INVALID_HANDLE || handle_stoch == INVALID_HANDLE)
     {
      Print("Error initializing indicator handles!");
      return(INIT_FAILED);
     }
     
   Print("CrossoverBot 6k Scalping EA Initialized.");
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(handle_ema50);
   IndicatorRelease(handle_ema200);
   IndicatorRelease(handle_stoch);
  }

//+------------------------------------------------------------------+
//| Expert tick function                                             |
//+------------------------------------------------------------------+
void OnTick()
  {
   // Ensure we only process once per new M1 bar
   static datetime last_time = 0;
   datetime current_time = iTime(_Symbol, PERIOD_M1, 0);
   if(current_time == last_time) return;
   
   // 1. Wait for open positions to close
   if(HasOpenPositions()) return;
   
   // 2. Check Funded Rules
   if(!CheckFundedRules()) return;
   
   // 3. Get Indicator Values
   double ema50[1], ema200[1], stoch_k[1], stoch_d[1];
   
   // Get data for the previously completed bar (index 1)
   if(CopyBuffer(handle_ema50, 0, 1, 1, ema50) <= 0) return;
   if(CopyBuffer(handle_ema200, 0, 1, 1, ema200) <= 0) return;
   if(CopyBuffer(handle_stoch, MAIN_LINE, 1, 1, stoch_k) <= 0) return;
   if(CopyBuffer(handle_stoch, SIGNAL_LINE, 1, 1, stoch_d) <= 0) return;
   
   double curr_ema50 = ema50[0];
   double curr_ema200 = ema200[0];
   double curr_k = stoch_k[0];
   double curr_d = stoch_d[0];
   
   // 4. Evaluate Signals
   bool is_uptrend = (curr_ema50 > curr_ema200);
   bool stoch_buy_cross = (curr_k < 20.0) && (curr_k > curr_d);
   
   bool is_downtrend = (curr_ema50 < curr_ema200);
   bool stoch_sell_cross = (curr_k > 80.0) && (curr_k < curr_d);
   
   if(is_uptrend && stoch_buy_cross)
     {
      ExecuteTrades(ORDER_TYPE_BUY);
      last_time = current_time;
     }
   else if(is_downtrend && stoch_sell_cross)
     {
      ExecuteTrades(ORDER_TYPE_SELL);
      last_time = current_time;
     }
  }

//+------------------------------------------------------------------+
//| Execute 5 positions and send notification                        |
//+------------------------------------------------------------------+
void ExecuteTrades(ENUM_ORDER_TYPE order_type)
  {
   double price = (order_type == ORDER_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   
   double sl, tp;
   if(order_type == ORDER_TYPE_BUY)
     {
      sl = price - InpStopLoss;
      tp = price + InpTakeProfit;
     }
   else
     {
      sl = price + InpStopLoss;
      tp = price - InpTakeProfit;
     }
   
   int success_count = 0;
   
   // Execute 5 trades
   for(int i = 0; i < 5; i++)
     {
      if(order_type == ORDER_TYPE_BUY)
        {
         if(trade.Buy(InpLotSize, _Symbol, price, sl, tp, "1M Scalper Bot")) success_count++;
        }
      else
        {
         if(trade.Sell(InpLotSize, _Symbol, price, sl, tp, "1M Scalper Bot")) success_count++;
        }
     }
     
   // Send MT5 Push Notification
   if(success_count > 0)
     {
      string action_str = (order_type == ORDER_TYPE_BUY) ? "BUY" : "SELL";
      
      // SendNotification sends a push notification directly to the MT5 mobile app!
      string msg = "Bot executed trades! Symbol: " + _Symbol + " | Action: " + action_str + " | Opened: " + IntegerToString(success_count) + " positions | Lots: " + DoubleToString(InpLotSize, 2);
      SendNotification(msg);
      Print(msg);
     }
  }

//+------------------------------------------------------------------+
//| Check if bot has open positions                                  |
//+------------------------------------------------------------------+
bool HasOpenPositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == InpMagicNumber)
        {
         return true;
        }
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Check Funded Rules                                               |
//+------------------------------------------------------------------+
bool CheckFundedRules()
  {
   // 1. Check Profit Target
   if(AccountInfoDouble(ACCOUNT_EQUITY) >= STARTING_BALANCE + PROFIT_TARGET)
     {
      return false; // Stand down, target reached
     }
     
   // 2. Check Max Loss
   if(AccountInfoDouble(ACCOUNT_EQUITY) <= STARTING_BALANCE - MAX_LOSS_LIMIT)
     {
      Print("Max loss limit hit!");
      return false;
     }
     
   // 3. Check Daily Loss and Consecutive Losses
   datetime start_of_day = (TimeCurrent() / 86400) * 86400; // Get 00:00 server time today
   HistorySelect(start_of_day, TimeCurrent());
   
   double daily_pnl = 0;
   int out_deals_count = 0;
   int loss_count = 0;
   
   int total_deals = HistoryDealsTotal();
   
   // Calculate Daily PnL for all trades, and Consecutive Losses for this bot's trades
   for(int i = total_deals - 1; i >= 0; i--)
     {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket > 0)
        {
         if(HistoryDealGetInteger(ticket, DEAL_ENTRY) == DEAL_ENTRY_OUT)
           {
            // Calculate Daily PnL
            double pnl = HistoryDealGetDouble(ticket, DEAL_PROFIT) + HistoryDealGetDouble(ticket, DEAL_COMMISSION) + HistoryDealGetDouble(ticket, DEAL_SWAP);
            daily_pnl += pnl;
            
            // Calculate Consecutive Losses for this bot
            if(HistoryDealGetInteger(ticket, DEAL_MAGIC) == InpMagicNumber && out_deals_count < 3)
              {
               if(pnl < 0) loss_count++;
               out_deals_count++;
              }
           }
        }
     }
     
   daily_pnl += AccountInfoDouble(ACCOUNT_PROFIT); // Add floating PnL
   
   if(daily_pnl <= -DAILY_LOSS_LIMIT)
     {
      Print("Daily loss limit hit!");
      return false;
     }
     
   if(out_deals_count == 3 && loss_count == 3)
     {
      Print("3 Consecutive Losses hit today. Standing down.");
      return false;
     }
     
   return true;
  }
//+------------------------------------------------------------------+
