import pandas as pd
import MetaTrader5 as mt5
from logger import log_info

def get_ohlc(symbol, timeframe, count=100):
    """Fetches OHLC data from MT5 and returns it as a pandas DataFrame."""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df

def calculate_indicators(df, k_period=5, d_period=3, slowing=3):
    """Calculates pure Stochastic Oscillator (%K, %D)."""
    if df.empty or len(df) < (k_period + slowing + d_period):
        return df
    
    # Fast %K
    lowest_low = df['low'].rolling(window=k_period).min()
    highest_high = df['high'].rolling(window=k_period).max()
    
    # Avoid division by zero
    diff = highest_high - lowest_low
    df['fast_k'] = 100 * ((df['close'] - lowest_low) / diff.replace(0, 0.00001))
    
    # Smoothed %K (Slow %K)
    df['stoch_k'] = df['fast_k'].rolling(window=slowing).mean()
    
    # %D (SMA of Slow %K)
    df['stoch_d'] = df['stoch_k'].rolling(window=d_period).mean()
    
    return df

def check_scalping_signal(symbol, timeframe=mt5.TIMEFRAME_M1):
    """
    Evaluates pure Stochastic strategy logic without additional filters.
    BUY: %K crosses above %D in/from oversold zone (<= 20).
    SELL: %K crosses below %D in/from overbought zone (>= 80).
    Returns: 'BUY', 'SELL', or None, and current stochastic_k.
    """
    df = get_ohlc(symbol, timeframe, count=100)
    if df.empty or len(df) < 20:
        return None, None
    
    df = calculate_indicators(df)
    
    curr_k = df['stoch_k'].iloc[-1]
    curr_d = df['stoch_d'].iloc[-1]
    prev_k = df['stoch_k'].iloc[-2]
    prev_d = df['stoch_d'].iloc[-2]
    
    # Status Logging
    zone = "NORMAL"
    if curr_k <= 20:
        zone = "OVERSOLD (<= 20)"
    elif curr_k >= 80:
        zone = "OVERBOUGHT (>= 80)"
        
    log_info(f"STOCH STATUS | {symbol} | %K: {curr_k:.1f}, %D: {curr_d:.1f} (Prev %K: {prev_k:.1f}, %D: {prev_d:.1f}) | Zone: {zone}")

    # BUY LOGIC: Stochastic oversold crossing up
    # Either fresh cross (prev_k <= prev_d and curr_k > curr_d) in/near oversold zone (<= 25) or actively oversold and %K > %D
    stoch_buy_cross = (prev_k <= prev_d and curr_k > curr_d and (prev_k <= 25 or curr_k <= 25)) or (curr_k <= 20 and curr_k > curr_d)
    
    if stoch_buy_cross:
        return 'BUY', curr_k
        
    # SELL LOGIC: Stochastic overbought crossing down
    # Either fresh cross (prev_k >= prev_d and curr_k < curr_d) in/near overbought zone (>= 75) or actively overbought and %K < %D
    stoch_sell_cross = (prev_k >= prev_d and curr_k < curr_d and (prev_k >= 75 or curr_k >= 75)) or (curr_k >= 80 and curr_k < curr_d)
    
    if stoch_sell_cross:
        return 'SELL', curr_k

    return None, curr_k

