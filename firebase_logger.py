import os
import firebase_admin
from firebase_admin import credentials
from firebase_admin import db
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

FIREBASE_ACTIVE = False

def init_firebase():
    global FIREBASE_ACTIVE
    try:
        # Check if already initialized
        if not firebase_admin._apps:
            cred_path = os.path.join(os.path.dirname(__file__), "mt5_gold_bot", "firebase_credentials.json")
            if os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
                # The DB URL is usually derived from the project ID
                firebase_admin.initialize_app(cred, {
                    'databaseURL': 'https://tradingbot-d9d9e-default-rtdb.firebaseio.com/'
                })
                FIREBASE_ACTIVE = True
                logger.info("Firebase Realtime DB initialized successfully.")
            else:
                logger.warning(f"Firebase credentials not found at {cred_path}")
        else:
            FIREBASE_ACTIVE = True
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")

def log_trade_to_firebase(ticket, symbol, trade_type, entry_price, close_price, profit, lots):
    """Pushes a completed trade to Firebase Realtime Database."""
    if not FIREBASE_ACTIVE:
        init_firebase()
        
    if not FIREBASE_ACTIVE:
        return
        
    try:
        ref = db.reference('trades')
        timestamp = datetime.now().isoformat()
        trade_data = {
            "ticket": ticket,
            "symbol": symbol,
            "type": trade_type,
            "entry_price": float(entry_price),
            "close_price": float(close_price) if close_price else 0.0,
            "profit": float(profit),
            "lots": float(lots),
            "closed_at": timestamp
        }
        ref.child(str(ticket)).set(trade_data)
        logger.info(f"Successfully logged trade {ticket} to Firebase.")
    except Exception as e:
        logger.error(f"Failed to log trade to Firebase: {e}")

def get_recent_trades(limit=20):
    """Fetches recent trades from Firebase."""
    if not FIREBASE_ACTIVE:
        init_firebase()
        
    if not FIREBASE_ACTIVE:
        return []
        
    try:
        ref = db.reference('trades')
        # Simple fetch of all trades (order by key) and take last N
        trades_dict = ref.order_by_key().limit_to_last(limit).get()
        if trades_dict:
            return list(trades_dict.values())
        return []
    except Exception as e:
        logger.error(f"Failed to fetch trades from Firebase: {e}")
        return []
