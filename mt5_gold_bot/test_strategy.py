import MetaTrader5 as mt5
import pandas as pd
import sys
import os

# Allow importing sibling modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from trading_engine import initialize_mt5
from strategy import get_ohlc, calculate_indicators

def test_dry_run():
    print("=== MT5 Gold Strategy Dry-Run Verification ===")
    
    # 1. Initialize MT5
    if not initialize_mt5():
        print("[ERROR] Could not connect to MetaTrader 5.")
        return
        
    try:
        # 2. Check symbol info
        symbol_info = mt5.symbol_info(config.SYMBOL)
        if symbol_info is None:
            print(f"[ERROR] Symbol '{config.SYMBOL}' not found on broker server.")
            # Try listing symbols with XAU or GOLD
            print("Broker symbols list matching 'XAU' or 'GOLD':")
            symbols = mt5.symbols_get()
            matches = [s.name for s in symbols if "XAU" in s.name or "GOLD" in s.name]
            print(matches)
            return
            
        print(f"[OK] Symbol '{config.SYMBOL}' found successfully.")
        print(f"   Digits: {symbol_info.digits}")
        print(f"   Spread: {symbol_info.spread} points")
        print(f"   Tick Size: {symbol_info.trade_tick_size}")
        print(f"   Tick Value: {symbol_info.trade_tick_value}")
        
        # Make sure symbol is selected
        mt5.symbol_select(config.SYMBOL, True)
        
        # 3. Check rates
        print(f"\nFetching last 100 bars for {config.SYMBOL} (Timeframe: M15)...")
        df = get_ohlc(config.SYMBOL, config.TIMEFRAME, count=100)
        
        if df.empty:
            print("[ERROR] Received empty OHLC dataset.")
            return
            
        print(f"[OK] Fetched {len(df)} candles.")
        
        # 4. Calculate indicators
        print("Calculating EMA 9, EMA 21, and ATR (14)...")
        df = calculate_indicators(df)
        
        # 5. Display last 5 candles
        print("\n=== Last 5 Candles Indicator Calculations ===")
        last_five = df.tail(5)
        for idx, row in last_five.iterrows():
            print(
                f"Time: {idx} | "
                f"Open: {row['open']:.2f} | "
                f"High: {row['high']:.2f} | "
                f"Low: {row['low']:.2f} | "
                f"Close: {row['close']:.2f} | "
                f"EMA 9: {row['ema_short']:.3f} | "
                f"EMA 21: {row['ema_long']:.3f} | "
                f"ATR: {row['atr']:.3f}"
            )
            
        # Check current trend
        curr_ema_short = df['ema_short'].iloc[-1]
        curr_ema_long = df['ema_long'].iloc[-1]
        state = "BULLISH (EMA9 > EMA21)" if curr_ema_short > curr_ema_long else "BEARISH (EMA9 < EMA21)"
        print(f"\nLive Candle Trend Status: {state}")
        
        # Check crossover state of completed candles
        prev_completed = df.iloc[-3]
        last_completed = df.iloc[-2]
        
        p_short, p_long = prev_completed['ema_short'], prev_completed['ema_long']
        c_short, c_long = last_completed['ema_short'], last_completed['ema_long']
        
        print(f"Completed Candle Crossover States:")
        print(f"  Pre-Last Candle: EMA9 = {p_short:.3f}, EMA21 = {p_long:.3f}")
        print(f"  Last Candle:     EMA9 = {c_short:.3f}, EMA21 = {c_long:.3f}")
        
        if p_short <= p_long and c_short > c_long:
            print("-> Signal status: BUY crossover detected on last completed candle!")
        elif p_short >= p_long and c_short < c_long:
            print("-> Signal status: SELL crossover detected on last completed candle!")
        else:
            print("-> Signal status: No crossover detected on last completed candle.")
            
    finally:
        mt5.shutdown()
        print("\n=== Verification Finished ===")

if __name__ == "__main__":
    test_dry_run()
