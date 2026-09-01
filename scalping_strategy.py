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
    """Calculates EMA 50, EMA 200, and Stochastic (%K, %D)."""
    if df.empty or len(df) < 50:
        return df
    
    # EMAs for Trend Filtering
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    
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
    High-Winrate Trend-Following Pullback Strategy:
    - BUY: Uptrend (Close > EMA50 > EMA200) + Stochastic pullback oversold (<= 25) crossing up.
    - SELL: Downtrend (Close < EMA50 < EMA200) + Stochastic pullback overbought (>= 75) crossing down.
    Returns: 'BUY', 'SELL', or None, and current stochastic_k.
    """
    df = get_ohlc(symbol, timeframe, count=250)
    if df.empty or len(df) < 205:
        return None, None
    
    df = calculate_indicators(df)
    
    # Indicators on closed candle (iloc[-2]) and latest (iloc[-1])
    curr_close = df['close'].iloc[-1]
    curr_ema_50 = df['ema_50'].iloc[-1]
    curr_ema_200 = df['ema_200'].iloc[-1]
    
    curr_k = df['stoch_k'].iloc[-1]
    curr_d = df['stoch_d'].iloc[-1]
    prev_k = df['stoch_k'].iloc[-2]
    prev_d = df['stoch_d'].iloc[-2]
    
    # Trend Analysis
    is_uptrend = (curr_close >= curr_ema_50) and (curr_ema_50 > curr_ema_200)
    is_downtrend = (curr_close <= curr_ema_50) and (curr_ema_50 < curr_ema_200)
    
    trend_str = "BULLISH 🟢" if is_uptrend else ("BEARISH 🔴" if is_downtrend else "SIDEWAYS / FLAT ⚪")
    
    log_info(f"STRATEGY STATUS | {symbol} | Trend: {trend_str} (50EMA: {curr_ema_50:.4f}, 200EMA: {curr_ema_200:.4f}) | Stoch %K: {curr_k:.1f}, %D: {curr_d:.1f}")

    # BUY LOGIC: Only buy in strong uptrend on stochastic oversold crossover
    stoch_buy_cross = (prev_k <= prev_d and curr_k > curr_d and (prev_k <= 25 or curr_k <= 25))
    if is_uptrend and stoch_buy_cross:
        return 'BUY', curr_k
        
    # SELL LOGIC: Only sell in strong downtrend on stochastic overbought crossover
    stoch_sell_cross = (prev_k >= prev_d and curr_k < curr_d and (prev_k >= 75 or curr_k >= 75))
    if is_downtrend and stoch_sell_cross:
        return 'SELL', curr_k

    return None, curr_k

