import MetaTrader5 as mt5
import trading_engine
import config

if not mt5.initialize():
    print("MT5 initialize failed")
else:
    print("Testing order placement for BTCUSD BUY...")
    res = trading_engine.place_order("BTCUSD", mt5.ORDER_TYPE_BUY, atr=500.0)
    print(f"Result: {res}")
    if res is None:
        print(f"Last MT5 Error: {mt5.last_error()}")
    mt5.shutdown()
