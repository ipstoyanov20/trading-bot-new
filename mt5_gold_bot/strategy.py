import pandas as pd
import MetaTrader5 as mt5
import config
from logger import log_error, log_info

def get_ohlc(symbol, timeframe, count=100):
    """
    Fetches OHLC data from MT5 and returns it as a pandas DataFrame.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('time', inplace=True)
    return df

def calculate_indicators(df):
    """
    Calculates EMA and ATR indicators.
    """
    if df.empty:
        return df
    
    # Calculate EMAs
    df['ema_short'] = df['close'].ewm(span=config.EMA_SHORT, adjust=False).mean()
    df['ema_long'] = df['close'].ewm(span=config.EMA_LONG, adjust=False).mean()
    
    # Calculate ATR (Average True Range)
    high_low = df['high'] - df['low']
    high_prev_close = (df['high'] - df['close'].shift(1)).abs()
    low_prev_close = (df['low'] - df['close'].shift(1)).abs()
    
    tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=config.ATR_PERIOD).mean()
    
    return df

def check_signal(symbol, timeframe):
    """
    Evaluates strategy logic on completed candles to prevent repainting.
    Returns: (signal, atr, last_completed_candle_time, confidence, features_dict)
        signal: 'BUY', 'SELL', or None
        atr: current ATR value for exits
        last_completed_candle_time: timestamp of the candle the signal was based on
        confidence: AI prediction score
        features_dict: features for logging
    """
    df = get_ohlc(symbol, timeframe, count=100)
    if df.empty or len(df) < max(config.EMA_LONG, config.ATR_PERIOD) + 2:
        return None, None, None, 0.0, None
    
    df = calculate_indicators(df)
    signal = None
    
    # Live candle is df.iloc[-1] (index -1). Price changes with every tick, so checking here causes repainting.
    # To trade like a professional, we check the last completed candle (index -2) and the preceding one (index -3).
    prev_completed_candle = df.iloc[-3]
    last_completed_candle = df.iloc[-2]
    
    prev_ema_short = prev_completed_candle['ema_short']
    prev_ema_long = prev_completed_candle['ema_long']
    
    curr_ema_short = last_completed_candle['ema_short']
    curr_ema_long = last_completed_candle['ema_long']
    
    atr = last_completed_candle['atr']
    last_completed_time = df.index[-2]  # Keep track of this candle's timestamp
    
    # Current trend status info for logging
    state = "ABOVE" if curr_ema_short > curr_ema_long else "BELOW"
    log_info(
        f"STATUS | {symbol} | EMA{config.EMA_SHORT} is {state} EMA{config.EMA_LONG} "
        f"(EMA{config.EMA_SHORT}: {curr_ema_short:.3f}, EMA{config.EMA_LONG}: {curr_ema_long:.3f}) | "
        f"Last Completed Candle Time: {last_completed_time}"
    )
    
    # Crossover detection
    # BUY: Fast EMA was below/equal to slow EMA, and now it is above
    if prev_ema_short <= prev_ema_long and curr_ema_short > curr_ema_long:
        signal = 'BUY'
        
    # SELL: Fast EMA was above/equal to slow EMA, and now it is below
    elif prev_ema_short >= prev_ema_long and curr_ema_short < curr_ema_long:
        signal = 'SELL'
        
    if signal:
        # Extract features and get AI confidence
        from ai_model import get_features_for_signal, predict_signal_confidence
        features_dict = get_features_for_signal(df, index=-2)
        confidence = predict_signal_confidence(features_dict, signal)
        log_info(f"AI Confidence for {signal}: {confidence*100:.1f}%")
        return signal, atr, last_completed_time, confidence, features_dict
        
    return None, atr, last_completed_time, 0.0, None
