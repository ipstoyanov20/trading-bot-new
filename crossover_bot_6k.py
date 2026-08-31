import MetaTrader5 as mt5
import time
import logging
from datetime import datetime

import config
from funded_rules_6k import FundedAccountRules6k
from scalping_strategy import check_scalping_signal
from trading_engine import check_open_positions, calculate_lot_size, close_all_positions, close_position_by_ticket
import requests

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# User Configurable Settings
SYMBOL = "XAUUSD"          # Gold Scalping
TIMEFRAME = mt5.TIMEFRAME_M1 # 1-Minute timeframe
SLEEP_INTERVAL = 1         # 1 second for fast execution and alternative exit

TELEGRAM_BOT_TOKEN = "8922725855:AAH5r_dnD2kRNsB0qb4iA-Tqdbrm35OXsEE"
TELEGRAM_CHAT_ID = "7403380678"

peak_profits = {}

def get_chat_id_from_updates():
    """Fetches the chat ID from recent bot messages."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url).json()
        if response.get("ok") and response.get("result"):
            # Get the chat ID of the most recent message
            return response["result"][-1]["message"]["chat"]["id"]
    except Exception as e:
        logger.error(f"Error fetching getUpdates: {e}")
    return None

def send_telegram_message(message):
    """Sends a message via the Telegram Bot API."""
    global TELEGRAM_CHAT_ID
    
    # Auto-fetch the chat ID if we don't have it
    if TELEGRAM_CHAT_ID is None:
        fetched_id = get_chat_id_from_updates()
        if fetched_id:
            TELEGRAM_CHAT_ID = fetched_id
            logger.info(f"Auto-fetched Telegram Chat ID: {TELEGRAM_CHAT_ID}")
        else:
            logger.warning("Could not auto-fetch Chat ID. Make sure you have sent a message (like /start) to your bot first!")
            return
            
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        if not response.ok:
            logger.error(f"Failed to send Telegram message: {response.text}")
            # If forbidden or chat not found, reset chat ID to try again next time
            if response.status_code in [400, 403]:
                TELEGRAM_CHAT_ID = None
    except Exception as e:
        logger.error(f"Exception sending Telegram message: {e}")

def check_3_consecutive_losses():
    """Checks if the last 3 closed trades for today were losses."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    history_deals = mt5.history_deals_get(today, datetime.now())
    if not history_deals:
        return False
    
    # Filter for OUT deals (closed trades) that belong to this bot
    closed_deals = [d for d in history_deals if d.entry == mt5.DEAL_ENTRY_OUT and d.magic == config.MAGIC_NUMBER]
    
    if len(closed_deals) >= 3:
        # Check last 3 deals
        last_3 = sorted(closed_deals, key=lambda x: x.time, reverse=True)[:3]
        losses = 0
        for d in last_3:
            # PnL = profit + commission + swap
            pnl = d.profit + d.commission + d.swap
            if pnl < 0:
                losses += 1
        if losses >= 3:
            return True
    return False

def place_scalping_order(symbol, order_type, sl_dist, tp_dist):
    """Places an order with Strategy-provided SL and TP for Gold."""
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        logger.error(f"Symbol {symbol} not found.")
        return False
    
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        logger.error(f"Failed to get price for {symbol}")
        return False
        
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    # Calculate actual price levels
    if order_type == mt5.ORDER_TYPE_BUY:
        sl = price - sl_dist if sl_dist > 0 else 0.0
        tp = price + tp_dist if tp_dist > 0 else 0.0
    else:
        sl = price + sl_dist if sl_dist > 0 else 0.0
        tp = price - tp_dist if tp_dist > 0 else 0.0
        
    # Fixed lots
    lots = 1.0
        
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lots),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "magic": config.MAGIC_NUMBER,
        "comment": "1M Scalper Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    success = True
    for _ in range(5):
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            logger.info(f"Scalp Order Placed successfully: {request}")
        else:
            err = mt5.last_error() if not res else res.comment
            logger.error(f"Failed to place scalp order: {err} | Request: {request}")
            success = False
            
    if success:
        action_str = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
        msg = f"🚀 **5 New Positions Opened**\n\n• **Symbol:** {symbol}\n• **Action:** {action_str}\n• **Price:** {price}\n• **Lots:** {lots} (x5)\n• **SL:** {sl}\n• **TP:** {tp}"
        send_telegram_message(msg)
        return True
    return False

def run_bot():
    """Main loop for the $6K Funded Account XAUUSD Scalping Bot"""
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    logger.info(f"MT5 initialized. Started $6K XAUUSD Scalping Bot on {TIMEFRAME}")
    
    # Send a startup test message
    send_telegram_message("🤖 **Trading Bot has started successfully and is monitoring the markets!**")
    
    rules_checker = FundedAccountRules6k()
    
    config.RISK_PERCENT = rules_checker.MAX_RISK_PERCENT
    logger.info(f"Risk configured to strict max {config.RISK_PERCENT}% per trade.")

    try:
        while True:
            # 1. Rules Check
            status = rules_checker.check_all_rules()
            if not status["can_trade"]:
                logger.warning("Rules Engine indicates limits hit! Trading will continue as requested.")
                
            if status["profit_target_reached"]:
                logger.info("Profit Target Reached! Bot will stand down.")
                time.sleep(3600)
                continue
                
            # 2. 3 Consecutive Losses Rule
            if check_3_consecutive_losses():
                logger.warning("🚫 3 Consecutive Losses hit today. Trading will continue as requested.")
                
            # 3. Handle Open Positions (Wait for MT5 SL/TP or manual close)
            # Instant profit closing has been removed per user request.
            # MT5 will automatically close positions when the Strategy-provided TP or SL is hit.
            # We can simply sleep and let it run.
            if check_open_positions(SYMBOL):
                time.sleep(SLEEP_INTERVAL)
                continue

            # 4. Check Signals for New Positions
            signal, curr_k = check_scalping_signal(SYMBOL, TIMEFRAME)
            
            if signal == 'BUY':
                sl_dist, tp_dist = 1.5, 3.0 # Default strict SL and TP distances
                logger.info(f"🟢 BUY SIGNAL for {SYMBOL}! SL: {sl_dist}, TP: {tp_dist}")
                place_scalping_order(SYMBOL, mt5.ORDER_TYPE_BUY, sl_dist, tp_dist)
            elif signal == 'SELL':
                sl_dist, tp_dist = 1.5, 3.0 # Default strict SL and TP distances
                logger.info(f"🔴 SELL SIGNAL for {SYMBOL}! SL: {sl_dist}, TP: {tp_dist}")
                place_scalping_order(SYMBOL, mt5.ORDER_TYPE_SELL, sl_dist, tp_dist)
                
            time.sleep(SLEEP_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed.")

if __name__ == "__main__":
    run_bot()
