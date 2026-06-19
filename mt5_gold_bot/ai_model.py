import os
import pandas as pd
import numpy as np
import xgboost as xgb
from logger import log_info, log_error
import config

MODEL_PATH = "xgboost_model.json"
HISTORY_PATH = "trade_history.csv"

def get_features_for_signal(df, index=-2):
    """
    Extracts features for the AI model from the dataframe at the given index.
    """
    row = df.iloc[index]
    
    # Calculate some derived features
    ema_distance = row['ema_short'] - row['ema_long']
    body_size = abs(row['open'] - row['close'])
    wick_upper = row['high'] - max(row['open'], row['close'])
    wick_lower = min(row['open'], row['close']) - row['low']
    
    features = {
        'ema_distance': ema_distance,
        'atr': row['atr'],
        'tick_volume': row.get('tick_volume', 0),
        'body_size': body_size,
        'wick_upper': wick_upper,
        'wick_lower': wick_lower,
    }
    return features

def train_model():
    """
    Trains the XGBoost model using the trade history CSV.
    """
    if not os.path.exists(HISTORY_PATH):
        log_info("No trade history available for AI training.")
        return False
        
    df = pd.read_csv(HISTORY_PATH)
    if len(df) < config.RETRAIN_AFTER_N_TRADES:
        log_info(f"Not enough data to train AI model yet. Found {len(df)} trades, need {config.RETRAIN_AFTER_N_TRADES}.")
        return False
        
    log_info(f"Training XGBoost AI model with {len(df)} historical trades...")
    
    # Target is 'win' column (1 or 0)
    if 'win' not in df.columns:
        log_error("Trade history missing 'win' column.")
        return False
        
    feature_cols = ['ema_distance', 'atr', 'tick_volume', 'body_size', 'wick_upper', 'wick_lower', 'signal_type']
    
    # Ensure all columns exist
    missing_cols = [c for c in feature_cols if c not in df.columns]
    if missing_cols:
        log_error(f"Missing feature columns in trade history: {missing_cols}")
        return False
        
    X = df[feature_cols]
    y = df['win']
    
    try:
        model = xgb.XGBClassifier(
            n_estimators=100, 
            learning_rate=0.1, 
            max_depth=3,
            eval_metric='logloss',
            random_state=42
        )
        model.fit(X, y)
        model.save_model(MODEL_PATH)
        log_info("AI model successfully trained and saved.")
        return True
    except Exception as e:
        log_error(f"Failed to train AI model: {e}")
        return False

def predict_signal_confidence(features_dict, signal):
    """
    Predicts the win probability (confidence) of a signal.
    """
    if not os.path.exists(MODEL_PATH):
        # Default to 0.5 confidence if no model is trained yet
        return 0.5
        
    try:
        model = xgb.XGBClassifier()
        model.load_model(MODEL_PATH)
        
        signal_type = 1 if signal == 'BUY' else 0
        
        # Create single-row DataFrame for prediction
        input_data = {
            'ema_distance': [features_dict.get('ema_distance', 0)],
            'atr': [features_dict.get('atr', 0)],
            'tick_volume': [features_dict.get('tick_volume', 0)],
            'body_size': [features_dict.get('body_size', 0)],
            'wick_upper': [features_dict.get('wick_upper', 0)],
            'wick_lower': [features_dict.get('wick_lower', 0)],
            'signal_type': [signal_type]
        }
        
        X = pd.DataFrame(input_data)
        
        # Predict probability of class 1 (win)
        prob = model.predict_proba(X)[0][1]
        return round(float(prob), 4)
        
    except Exception as e:
        log_error(f"AI prediction failed: {e}")
        return 0.5

def log_trade_entry(ticket, signal, features_dict):
    """
    Temporarily stores the entry features for a trade, to be matched with outcomes later.
    """
    entry_log = "active_trades.csv"
    signal_type = 1 if signal == 'BUY' else 0
    
    row = {
        'ticket': ticket,
        'signal_type': signal_type,
        'ema_distance': features_dict.get('ema_distance', 0),
        'atr': features_dict.get('atr', 0),
        'tick_volume': features_dict.get('tick_volume', 0),
        'body_size': features_dict.get('body_size', 0),
        'wick_upper': features_dict.get('wick_upper', 0),
        'wick_lower': features_dict.get('wick_lower', 0),
    }
    
    df_row = pd.DataFrame([row])
    
    if os.path.exists(entry_log):
        df_row.to_csv(entry_log, mode='a', header=False, index=False)
    else:
        df_row.to_csv(entry_log, mode='w', header=True, index=False)
