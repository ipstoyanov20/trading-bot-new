import logging
import os
from datetime import datetime

# Define log file path
LOG_FILE = os.path.join(os.getcwd(), "bot_activity.log")

# Setup logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

def log_trade(symbol, action, lot, price, sl, tp, result):
    """Logs professional trade details."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = (f"TRADE | {timestamp} | Symbol: {symbol} | Action: {action} | "
           f"Lot: {lot} | Price: {price} | SL: {sl} | TP: {tp} | Result: {result}")
    logger.info(msg)

def log_error(msg):
    """Logs an error message."""
    logger.error(f"ERROR | {msg}")

def log_info(msg):
    """Logs general information."""
    logger.info(f"INFO | {msg}")
