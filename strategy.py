import pandas as pd
import MetaTrader5 as mt5
import config

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
    """Calculates EMA and ATR indicators."""
    if df.empty:
        return df
    
    # Calculate EMAs
    df['ema_short'] = df['close'].ewm(span=config.EMA_SHORT, adjust=False).mean()
    df['ema_long'] = df['close'].ewm(span=config.EMA_LONG, adjust=False).mean()
    
    # Calculate ATR (Average True Range)
    # TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
    high_low = df['high'] - df['low']
    high_prev_close = (df['high'] - df['close'].shift(1)).abs()
    low_prev_close = (df['low'] - df['close'].shift(1)).abs()
    
    tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=config.ATR_PERIOD).mean()
    
    return df

def check_signal(symbol, timeframe):
    """
    Evaluates strategy logic and returns a signal.
    Returns: 'BUY', 'SELL' or None.
    """
    df = get_ohlc(symbol, timeframe, count=100)
    if df.empty or len(df) < 22:
        return None, None
    
    df = calculate_indicators(df)
    
    # Latest candle (index -1) and previous candle (index -2)
    curr_ema_short = df['ema_short'].iloc[-1]
    curr_ema_long = df['ema_long'].iloc[-1]
    prev_ema_short = df['ema_short'].iloc[-2]
    prev_ema_long = df['ema_long'].iloc[-2]
    
    atr = df['atr'].iloc[-1]
    
    # Log current EMA state for feedback
    state = "ABOVE" if curr_ema_short > curr_ema_long else "BELOW"
    from logger import log_info
    log_info(f"STATUS | {symbol} | EMA9 is {state} EMA21 (EMA9: {curr_ema_short:.5f}, EMA21: {curr_ema_long:.5f})")
    
    # EMA Crossover Logic
    # Buy Cross: Short was below Long, now Short is above Long
    if prev_ema_short < prev_ema_long and curr_ema_short > curr_ema_long:
        return 'BUY', atr
        
    # Sell Cross: Short was above Long, now Short is below Long
    if prev_ema_short > prev_ema_long and curr_ema_short < curr_ema_long:
        return 'SELL', atr
        
    return None, atr
