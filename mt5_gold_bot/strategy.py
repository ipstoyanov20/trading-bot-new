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
    Evaluates candle-close strategy logic every 30 minutes.
    Uploads data to Firestore and uses AI model predictions to trigger trades.
    Returns: (signal, atr, last_completed_candle_time, confidence, features_dict)
    """
    df = get_ohlc(symbol, timeframe, count=100)
    if df.empty or len(df) < max(config.EMA_LONG, config.ATR_PERIOD) + 2:
        return None, None, None, 0.0, None
    
    df = calculate_indicators(df)
    
    # We evaluate on the last completed candle (index -2) to prevent repainting.
    last_completed_candle = df.iloc[-2]
    atr = last_completed_candle['atr']
    last_completed_time = df.index[-2]
    
    # Extract features for AI model
    from ai_model import get_features_for_signal, predict_signal_confidence
    from firebase_logger import save_market_data
    
    features_dict = get_features_for_signal(df, index=-2)
    
    # 1. Save market data to Firestore/CSV
    save_market_data(last_completed_time, last_completed_candle, features_dict)
    
    # 2. Get AI predictions for both directions
    buy_confidence = predict_signal_confidence(features_dict, 'BUY')
    sell_confidence = predict_signal_confidence(features_dict, 'SELL')
    
    # 3. Determine trade direction
    # If the model is not trained, both probabilities will be 0.5.
    if buy_confidence == 0.5 and sell_confidence == 0.5:
        # Fallback to simple trend following based on EMA crossover
        curr_ema_short = last_completed_candle['ema_short']
        curr_ema_long = last_completed_candle['ema_long']
        if curr_ema_short > curr_ema_long:
            signal = 'BUY'
            confidence = 0.5001  # Slight edge to trigger
        else:
            signal = 'SELL'
            confidence = 0.5001
        log_info(f"AI not trained yet. Fallback to EMA Trend: {signal} (EMA9: {curr_ema_short:.2f}, EMA21: {curr_ema_long:.2f})")
    else:
        # AI is trained: compare probabilities and select the higher one aggressively
        if buy_confidence >= sell_confidence:
            signal = 'BUY'
            confidence = buy_confidence
        else:
            signal = 'SELL'
            confidence = sell_confidence
        log_info(f"AI predictions - BUY: {buy_confidence*100:.1f}%, SELL: {sell_confidence*100:.1f}% -> Selected: {signal}")
        
    return signal, atr, last_completed_time, confidence, features_dict

