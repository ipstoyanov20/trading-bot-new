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

def calculate_indicators(df):
    """Calculates EMA 50, EMA 200, and Stochastic (5, 3, 3)."""
    if df.empty or len(df) < 200:
        return df
    
    # Calculate EMAs
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # Stochastic (5, 3, 3)
    k_period = 5
    d_period = 3
    slowing = 3

    # Fast %K
    lowest_low = df['low'].rolling(window=k_period).min()
    highest_high = df['high'].rolling(window=k_period).max()
    df['fast_k'] = 100 * ((df['close'] - lowest_low) / (highest_high - lowest_low))
    
    # Smoothed %K (Slow %K)
    df['stoch_k'] = df['fast_k'].rolling(window=slowing).mean()
    
    # %D (SMA of Slow %K)
    df['stoch_d'] = df['stoch_k'].rolling(window=d_period).mean()
    
    # Calculate RSI (14) using EMA
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    return df

def check_scalping_signal(symbol, timeframe=mt5.TIMEFRAME_M1):
    """
    Evaluates scalping strategy logic for XAUUSD.
    Returns: 'BUY', 'SELL', or None, and current stochastic_k for alternative exits.
    """
    df = get_ohlc(symbol, timeframe, count=250)
    if df.empty or len(df) < 205:
        return None, None
    
    df = calculate_indicators(df)
    
    curr_close = df['close'].iloc[-1]
    curr_ema_50 = df['ema_50'].iloc[-1]
    curr_ema_200 = df['ema_200'].iloc[-1]
    
    curr_k = df['stoch_k'].iloc[-1]
    curr_d = df['stoch_d'].iloc[-1]
    curr_rsi = df['rsi'].iloc[-1]
    prev_k = df['stoch_k'].iloc[-2]
    prev_d = df['stoch_d'].iloc[-2]
    
    # Logging
    state = "BULLISH" if curr_ema_50 > curr_ema_200 else "BEARISH"
    log_info(f"SCALP STATUS | {symbol} | Trend: {state} (50EMA: {curr_ema_50:.2f}, 200EMA: {curr_ema_200:.2f}) | Stoch K: {curr_k:.1f} D: {curr_d:.1f} | RSI: {curr_rsi:.1f}")

    # BUY LOGIC
    # 1. 50 EMA > 200 EMA
    is_uptrend = (curr_ema_50 > curr_ema_200)
    
    # 2. Stochastic is below 30 and K is above D (Aggressive)
    stoch_buy_cross = (curr_k < 30) and (curr_k > curr_d)
    
    if is_uptrend and stoch_buy_cross:
        return 'BUY', curr_k
        
    # SELL LOGIC
    # 1. 50 EMA < 200 EMA
    is_downtrend = (curr_ema_50 < curr_ema_200)
    
    # 2. Stochastic is above 70 and K is below D (Aggressive)
    stoch_sell_cross = (curr_k > 70) and (curr_k < curr_d)
    
    if is_downtrend and stoch_sell_cross:
        return 'SELL', curr_k

    return None, curr_k
