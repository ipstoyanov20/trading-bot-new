import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "mt5_gold_bot")))

import MetaTrader5 as mt5
import config
from strategy import check_signal
from trading_engine import initialize_mt5

if initialize_mt5():
    try:
        print("Testing check_signal...")
        # Override RETRAIN to avoid warnings
        config.RETRAIN_AFTER_N_TRADES = 10
        
        signal, atr, last_completed_time, confidence, features_dict = check_signal(config.SYMBOL, config.TIMEFRAME)
        
        print("\n=== check_signal() OUTPUT ===")
        print(f"Signal: {signal}")
        print(f"ATR: {atr}")
        print(f"Last Completed Time: {last_completed_time}")
        print(f"AI Confidence: {confidence}")
        print(f"Features: {features_dict}")
    except Exception as e:
        print("Error during check_signal:", e)
    finally:
        mt5.shutdown()
        print("MT5 Shutdown completed.")
else:
    print("Failed to connect to MT5.")
