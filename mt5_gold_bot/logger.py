import logging
import os
from datetime import datetime

# Setup log directory and file
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "gold_bot_activity.log")

# Setup logger configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

def log_trade(symbol, action, lot, price, sl, tp, result):
    """Logs trade entries and outcomes clearly."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (f"TRADE | {timestamp} | Symbol: {symbol} | Action: {action} | "
           f"Lot: {lot:.2f} | Price: {price:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | Result: {result}")
    logger.info(msg)

def log_error(msg):
    """Logs errors."""
    logger.error(f"ERROR | {msg}")

def log_info(msg):
    """Logs normal informative messages."""
    logger.info(f"INFO | {msg}")
