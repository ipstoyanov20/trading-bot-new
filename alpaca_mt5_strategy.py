import time
import pandas as pd
import MetaTrader5 as mt5
from alpaca_trade_api.rest import REST, TimeFrame

# --- CONFIGURATION CHOOSE YOUR KEYS ---
ALPACA_API_KEY = "PKKFQ3P4Q4B5RFDFDPFDMCF47P"
ALPACA_SECRET_KEY = "C2Snty78mS9yor8wmzARSKocibFLjG9R9Bkeq28N34M4"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets" # Use paper-api for testing

ALPACA_SYMBOL = "GLD"  # Gold ETF proxy on US Stock Market for Alpaca
MT5_SYMBOL = "XAUUSD"  # Gold symbol in MetaTrader 5

# Strategy parameters
TIMEFRAME = TimeFrame.Minute
FAST_SMA_PERIOD = 10
SLOW_SMA_PERIOD = 50

# MetaTrader 5 Trading Params
LOT_SIZE = 0.01

# --- INITIALIZATION ---
def init_alpaca():
    print("Initializing Alpaca connection...")
    api = REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')
    return api

def init_mt5():
    print("Connecting to MetaTrader 5...")
    if not mt5.initialize():
        print("MT5 initialization failed. Error code:", mt5.last_error())
        return False
    
    # Check if symbol is available in market watch
    selected = mt5.symbol_select(MT5_SYMBOL, True)
    if not selected:
        print(f"Failed to select {MT5_SYMBOL} in MT5. Please check MT5 symbol nomenclature.")
        mt5.shutdown()
        return False
    
    print("MT5 connected and symbol selected.")
    return True

# --- STRATEGY LOGIC ---
def get_data_and_signal(api):
    # Fetch recent bars
    try:
        bars_iter = api.get_bars(ALPACA_SYMBOL, TIMEFRAME, limit=100)
        bars = bars_iter.df
    except Exception as e:
        print(f"Error fetching data from Alpaca: {e}")
        return None
    
    if bars.empty:
        # Check if the market is actually open
        clock = api.get_clock()
        if not clock.is_open:
            print(f"Market is currently closed (Sunday/Weekend/After-hours). Re-opening at {clock.next_open}.")
        else:
            print("No market data received from Alpaca (Market is open but no new bars).")
        return None

    # Calculate Simple Moving Averages (SMAs)
    bars['fast_sma'] = bars['close'].rolling(window=FAST_SMA_PERIOD).mean()
    bars['slow_sma'] = bars['close'].rolling(window=SLOW_SMA_PERIOD).mean()
    
    if len(bars) < SLOW_SMA_PERIOD:
        return None

    # Get last two completed elements for crossover check
    previous_bar = bars.iloc[-2]
    current_bar = bars.iloc[-1]

    # Crossover Logic
    # BUY Signal: previous fast < previous slow AND current fast > current slow (Cross Up)
    if previous_bar['fast_sma'] <= previous_bar['slow_sma'] and current_bar['fast_sma'] > current_bar['slow_sma']:
        return "BUY"
    
    # SELL Signal: previous fast >= previous slow AND current fast < current slow (Cross Down)
    elif previous_bar['fast_sma'] >= previous_bar['slow_sma'] and current_bar['fast_sma'] < current_bar['slow_sma']:
        return "SELL"
    
    return None

# --- MT5 EXECUTION ---
def execute_trade_mt5(signal):
    # Initialize MT5 only when signal is found
    if not init_mt5():
        print("Could not execute trade: MT5 connection failed.")
        return

    try:
        tick = mt5.symbol_info_tick(MT5_SYMBOL)
        if tick is None:
            print(f"Could not retrieve ticks for {MT5_SYMBOL} from MT5.")
            return

        if signal == "BUY":
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask
        elif signal == "SELL":
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            return

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": MT5_SYMBOL,
            "volume": LOT_SIZE,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": 123456,
            "comment": "Alpaca GLD SMA Crossover",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        print(f"Sending {signal} order to MT5 for {MT5_SYMBOL} at price {price}...")
        result = mt5.order_send(request)
        
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"Order failed, retcode={result.retcode}")
            result_dict = result._asdict()
            for field in result_dict.keys():
                print("   {}={}".format(field, result_dict[field]))
        else:
            print(f"Order successfully placed! MT5 Ticket: {result.order}")
    
    finally:
        # Shutdown MT5 immediately after execution attempt
        mt5.shutdown()
        print("MT5 connection closed after trade execution.")

# --- MAIN EXPERT ADVISOR LOOP ---
def run_bot():
    api = init_alpaca()
    # MT5 is no longer initialized here; it's initialized on-demand when a signal is generated.
    
    print(f"Bot is running...")
    print(f"Monitoring Alpaca for: {ALPACA_SYMBOL}")
    print(f"Executing on MT5 for: {MT5_SYMBOL}")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            signal = get_data_and_signal(api)
            current_time = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
            
            if signal:
                print(f"[{current_time}] SIGNAL GENERATED: {signal}")
                execute_trade_mt5(signal)
            else:
                # Optional: We already handle the closed market message inside get_data_and_signal
                # but we can suppress this specific line if we want it to be cleaner
                pass
                
            time.sleep(60) # Wait 60 seconds (1 minute timeframe) before checking again
    except KeyboardInterrupt:
        print("Bot stopped by user via KeyboardInterrupt.")
    except Exception as e:
        print(f"Unexpected error occurred: {e}")
    finally:
        # Final safety shutdown in case something weird happens
        mt5.shutdown()
        print("Bot loop terminated.")

if __name__ == "__main__":
    run_bot()
