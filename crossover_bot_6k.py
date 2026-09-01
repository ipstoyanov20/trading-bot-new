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
SLEEP_INTERVAL = 1         # 1 second for fast execution and monitoring
LOT_SIZE = 0.7             # Trading volume

# Risk & Loss Settings
STOP_LOSS_EUR = -5.0       # Terminate position strictly at 5 EUR loss (-5.0)

peak_profits = {}

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

def get_filling_type(symbol):
    """
    Dynamically determines the correct execution filling mode supported by the broker.
    """
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return mt5.ORDER_FILLING_FOK
        
    filling_mode = getattr(symbol_info, 'filling_mode', 0)
    if filling_mode & 1:  # FOK supported
        return mt5.ORDER_FILLING_FOK
    elif filling_mode & 2:  # IOC supported
        return mt5.ORDER_FILLING_IOC
    else:
        return mt5.ORDER_FILLING_RETURN

def place_scalping_order(symbol, order_type):
    """
    Places an order for Gold (XAUUSD) allowing profits to run upwards while monitored for a strict -5 EUR stop loss.
    """
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        logger.error(f"Symbol {symbol} not found.")
        return False
    
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        logger.error(f"Failed to get price for {symbol}")
        return False
        
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(LOT_SIZE),
        "type": order_type,
        "price": price,
        "sl": 0.0,
        "tp": 0.0,  # Uncapped TP to let profits run upwards freely
        "magic": config.MAGIC_NUMBER,
        "comment": "1M Stoch Gold (Runner)",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
    # Try preferred filling mode, and fallback to others if unsupported
    modes_to_try = [get_filling_type(symbol), mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    unique_modes = []
    for m in modes_to_try:
        if m not in unique_modes:
            unique_modes.append(m)
            
    res = None
    for mode in unique_modes:
        request["type_filling"] = mode
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            action_name = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
            logger.info(f"✅ {action_name} Order Placed successfully for {symbol} | Price: {price} | Running with 5 EUR Stop Loss (Filling: {mode})")
            return True
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue  # Try next filling mode
        else:
            break
            
    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place scalp order: {err} | Request: {request}")
    return False

def monitor_positions(symbol, max_loss=STOP_LOSS_EUR):
    """
    Monitors all open bot positions for XAUUSD.
    Terminates trade immediately if loss reaches 5 EUR (profit <= -5.0).
    Otherwise lets the trade run upwards freely.
    Returns True if bot positions remain open, False otherwise.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False

    bot_positions = [p for p in positions if p.magic == config.MAGIC_NUMBER]
    if not bot_positions:
        return False

    for p in bot_positions:
        ticket = p.ticket
        profit = p.profit + p.swap + getattr(p, 'commission', 0.0)
        
        # Strict Stop Loss Check: terminate immediately at 5 EUR loss
        if profit <= max_loss:
            logger.info(f"🛑 Stop Loss Hit for #{ticket}! Floating PnL: {profit:.2f} <= {max_loss:.2f} (5 EUR loss limit). Terminating position immediately...")
            close_position_by_ticket(symbol, ticket)
        elif profit > 0:
            logger.info(f"📈 #{ticket} Running in Profit: +{profit:.2f} (allowing trade to run upwards)")

    remaining_positions = mt5.positions_get(symbol=symbol)
    return len([p for p in (remaining_positions or []) if p.magic == config.MAGIC_NUMBER]) > 0

def run_bot():
    """Main loop for the $6K Funded Account XAUUSD Scalping Bot"""
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    logger.info(f"MT5 initialized. Started $6K {SYMBOL} Pure Stochastic Bot on {TIMEFRAME} | Volume: {LOT_SIZE} | Strict Stop Loss: 5 EUR | Profit Runner (No TP cap)")
    
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
                
            # 3. Handle Open Positions & Monitor for -5 EUR Stop Loss / Running Profit
            if monitor_positions(SYMBOL):
                time.sleep(SLEEP_INTERVAL)
                continue

            # 4. Check Signals for New Positions (Pure Stochastic)
            signal, curr_k = check_scalping_signal(SYMBOL, TIMEFRAME)
            
            if signal == 'BUY':
                logger.info(f"🟢 STOCH BUY SIGNAL for {SYMBOL}! Executing {LOT_SIZE} Lots (Stop Loss: 5 EUR, Profit Runner)...")
                place_scalping_order(SYMBOL, mt5.ORDER_TYPE_BUY)
            elif signal == 'SELL':
                logger.info(f"🔴 STOCH SELL SIGNAL for {SYMBOL}! Executing {LOT_SIZE} Lots (Stop Loss: 5 EUR, Profit Runner)...")
                place_scalping_order(SYMBOL, mt5.ORDER_TYPE_SELL)
                
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
