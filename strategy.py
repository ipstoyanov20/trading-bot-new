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
    
    # Calculate RSI (7)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/7, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/7, adjust=False).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
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
    rsi = df['rsi'].iloc[-1]
    
    # Log current EMA state for feedback
    state = "ABOVE" if curr_ema_short > curr_ema_long else "BELOW"
    from logger import log_info
    log_info(f"STATUS | {symbol} | EMA9 is {state} EMA21 (EMA9: {curr_ema_short:.5f}, EMA21: {curr_ema_long:.5f}) | RSI: {rsi:.1f} | ATR: {atr:.5f}")
    
    # Volatility Filter removed by user request (bot will now trade regardless of how flat the market is).
        
    # Momentum Logic
    # Buy: Short EMA above Long EMA, Price above Short EMA, RSI > 50 (momentum up)
    if curr_ema_short > curr_ema_long and df['close'].iloc[-1] > curr_ema_short and rsi > 50:
        return 'BUY', atr
        
    # Sell: Short EMA below Long EMA, Price below Short EMA, RSI < 50 (momentum down)
    if curr_ema_short < curr_ema_long and df['close'].iloc[-1] < curr_ema_short and rsi < 50:
        return 'SELL', atr
        
    return None, atr
