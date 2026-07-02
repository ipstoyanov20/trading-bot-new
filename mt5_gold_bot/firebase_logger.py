import os
import pandas as pd
from datetime import datetime
from logger import log_info, log_error

FIREBASE_ACTIVE = False
db = None

# Attempt to import firebase-admin and initialize
try:
    import firebase_admin
    from firebase_admin import credentials
    from firebase_admin import firestore
    
    CREDENTIALS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase_credentials.json")
    
    if os.path.exists(CREDENTIALS_FILE):
        cred = credentials.Certificate(CREDENTIALS_FILE)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        FIREBASE_ACTIVE = True
        log_info("Firebase Firestore successfully initialized and connected.")
    else:
        log_info(f"Firebase credentials not found at {CREDENTIALS_FILE}. Running in LOCAL fallback mode.")
except Exception as e:
    log_error(f"Failed to initialize Firebase Admin SDK: {e}. Running in LOCAL fallback mode.")

# Local File Paths
LOCAL_MARKET_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_data.csv")
LOCAL_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.csv")
LOCAL_ACTIVE_TRADES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_trades.csv")

def save_market_data(timestamp, ohlc_row, features_dict):
    """
    Saves M30 market data and calculated indicators to Firestore (or local CSV).
    """
    # Prepare data dictionary
    data = {
        'timestamp': str(timestamp),
        'open': float(ohlc_row['open']),
        'high': float(ohlc_row['high']),
        'low': float(ohlc_row['low']),
        'close': float(ohlc_row['close']),
        'tick_volume': int(ohlc_row.get('tick_volume', 0)),
        'updated_at': datetime.now().isoformat()
    }
    # Add all feature calculations
    for k, v in features_dict.items():
        data[k] = float(v) if isinstance(v, (int, float, bool)) else v

    # 1. Firestore Save
    if FIREBASE_ACTIVE:
        try:
            # Document ID can be the timestamp string (cleaned)
            doc_id = str(timestamp).replace(" ", "_").replace(":", "-")
            db.collection("market_data").document(doc_id).set(data)
            log_info(f"Firestore: Market data saved for {timestamp}.")
        except Exception as e:
            log_error(f"Firestore failed to save market data: {e}")
            
    # 2. Local Fallback Save
    try:
        df_row = pd.DataFrame([data])
        if os.path.exists(LOCAL_MARKET_DATA_PATH):
            df_row.to_csv(LOCAL_MARKET_DATA_PATH, mode='a', header=False, index=False)
        else:
            df_row.to_csv(LOCAL_MARKET_DATA_PATH, mode='w', header=True, index=False)
    except Exception as e:
        log_error(f"Failed to save market data locally: {e}")

def save_trade_entry(ticket, signal, entry_price, lots, confidence, features):
    """
    Saves details of a newly opened trade to Firestore (or local active_trades CSV).
    """
    data = {
        'ticket': int(ticket),
        'signal': signal,
        'entry_price': float(entry_price),
        'lots': float(lots),
        'confidence': float(confidence),
        'status': 'active',
        'opened_at': datetime.now().isoformat(),
        'profit': None,
        'win': None,
        'closed_at': None
    }
    # Merge features directly into data
    for k, v in features.items():
        data[f"feat_{k}"] = float(v) if isinstance(v, (int, float, bool)) else v

    # 1. Firestore Save
    if FIREBASE_ACTIVE:
        try:
            db.collection("trades").document(str(ticket)).set(data)
            log_info(f"Firestore: Trade entry logged for ticket {ticket}.")
        except Exception as e:
            log_error(f"Firestore failed to save trade entry: {e}")

    # 2. Local Fallback Save (append to active_trades.csv)
    try:
        # Flattened row for local active_trades
        local_row = {
            'ticket': ticket,
            'signal_type': 1 if signal == 'BUY' else 0,
            'confidence': confidence,
            'entry_price': entry_price,
            'lots': lots,
            'ema_distance': features.get('ema_distance', 0),
            'atr': features.get('atr', 0),
            'tick_volume': features.get('tick_volume', 0),
            'body_size': features.get('body_size', 0),
            'wick_upper': features.get('wick_upper', 0),
            'wick_lower': features.get('wick_lower', 0)
        }
        df_row = pd.DataFrame([local_row])
        if os.path.exists(LOCAL_ACTIVE_TRADES_PATH):
            df_row.to_csv(LOCAL_ACTIVE_TRADES_PATH, mode='a', header=False, index=False)
        else:
            df_row.to_csv(LOCAL_ACTIVE_TRADES_PATH, mode='w', header=True, index=False)
    except Exception as e:
        log_error(f"Failed to log trade entry locally: {e}")

def update_trade_outcome(ticket, profit, win):
    """
    Updates an active trade with final profit and win outcome when closed.
    """
    # 1. Firestore Save
    if FIREBASE_ACTIVE:
        try:
            trade_ref = db.collection("trades").document(str(ticket))
            trade_ref.update({
                'status': 'closed',
                'profit': float(profit),
                'win': int(win),
                'closed_at': datetime.now().isoformat()
            })
            log_info(f"Firestore: Trade outcome updated for ticket {ticket}. Profit: {profit}, Win: {win}")
        except Exception as e:
            log_error(f"Firestore failed to update trade outcome: {e}")

    # 2. Local Fallback Update
    # Remove from active_trades.csv and append to trade_history.csv
    try:
        if os.path.exists(LOCAL_ACTIVE_TRADES_PATH):
            active_df = pd.read_csv(LOCAL_ACTIVE_TRADES_PATH)
            matched_rows = active_df[active_df['ticket'] == ticket]
            
            if not matched_rows.empty:
                history_row = matched_rows.iloc[0].to_dict()
                history_row['win'] = int(win)
                history_row['profit'] = float(profit)
                history_row['closed_at'] = datetime.now().isoformat()
                
                # Append to history
                df_hist = pd.DataFrame([history_row])
                if os.path.exists(LOCAL_HISTORY_PATH):
                    df_hist.to_csv(LOCAL_HISTORY_PATH, mode='a', header=False, index=False)
                else:
                    df_hist.to_csv(LOCAL_HISTORY_PATH, mode='w', header=True, index=False)
                
                # Remove from active
                active_df = active_df[active_df['ticket'] != ticket]
                active_df.to_csv(LOCAL_ACTIVE_TRADES_PATH, index=False)
    except Exception as e:
        log_error(f"Failed to update trade outcome locally: {e}")

def fetch_historical_trades():
    """
    Fetches all closed trades from Firestore (or local CSV history) for AI model retraining.
    """
    if FIREBASE_ACTIVE:
        try:
            log_info("Fetching historical trades from Firestore...")
            trades_ref = db.collection("trades")
            query = trades_ref.where("status", "==", "closed").stream()
            
            records = []
            for doc in query:
                d = doc.to_dict()
                # Map feat_ keys back to normal keys for XGBoost
                normal_d = {}
                for k, v in d.items():
                    if k.startswith("feat_"):
                        normal_d[k.replace("feat_", "")] = v
                    else:
                        normal_d[k] = v
                # Map signal to signal_type
                if 'signal' in normal_d:
                    normal_d['signal_type'] = 1 if normal_d['signal'] == 'BUY' else 0
                records.append(normal_d)
                
            if records:
                return pd.DataFrame(records)
        except Exception as e:
            log_error(f"Firestore failed to fetch historical trades: {e}. Falling back to CSV.")

    # Local fallback
    if os.path.exists(LOCAL_HISTORY_PATH):
        try:
            return pd.read_csv(LOCAL_HISTORY_PATH)
        except Exception as e:
            log_error(f"Failed to read local trade history: {e}")
            
    return pd.DataFrame()
