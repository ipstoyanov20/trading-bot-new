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
SYMBOL = "EURUSD"          # Forex Scalping
TIMEFRAME = mt5.TIMEFRAME_M1 # 1-Minute timeframe
SLEEP_INTERVAL = 1         # 1 second for fast execution and alternative exit

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

def place_scalping_order(symbol, order_type, sl_dist, tp_dist):
    """Places an order with Strategy-provided SL and TP, supporting all broker filling modes."""
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
    lots = 0.01
        
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lots),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "magic": config.MAGIC_NUMBER,
        "comment": "1M EURUSD Scalper",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
    # Try preferred filling mode, and fallback to others if unsupported
    modes_to_try = [get_filling_type(symbol), mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    # Remove duplicates while preserving order
    unique_modes = []
    for m in modes_to_try:
        if m not in unique_modes:
            unique_modes.append(m)
            
    res = None
    for mode in unique_modes:
        request["type_filling"] = mode
        res = mt5.order_send(request)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            logger.info(f"Scalp Order Placed successfully with filling mode {mode}: {request}")
            return True
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue  # Try next filling mode
        else:
            break  # Other error, stop
            
    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place scalp order: {err} | Request: {request}")
    return False

def monitor_trailing_profits(symbol):
    """
    Monitors all open bot positions for the symbol.
    If floating profit reaches $40 or more, tracks the peak profit.
    If profit drops by $2 or more from its peak, closes the position to lock in profit.
    Returns True if bot positions remain open, False otherwise.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False

    bot_positions = [p for p in positions if p.magic == config.MAGIC_NUMBER]
    if not bot_positions:
        peak_profits.clear()
        return False

    current_tickets = set()
    for p in bot_positions:
        ticket = p.ticket
        current_tickets.add(ticket)
        profit = p.profit + p.swap + getattr(p, 'commission', 0.0)
        
        # Track peak profit
        if ticket not in peak_profits:
            peak_profits[ticket] = profit
        else:
            if profit > peak_profits[ticket]:
                peak_profits[ticket] = profit
                
        # Trailing profit lock condition:
        # Reached at least $40 profit and pulled back by $2
        if peak_profits[ticket] >= 40.0:
            drop_from_peak = peak_profits[ticket] - profit
            if drop_from_peak >= 2.0:
                logger.info(
                    f"💰 Trailing Profit Lock Triggered for #{ticket}! "
                    f"Peak Profit: ${peak_profits[ticket]:.2f}, Current Profit: ${profit:.2f} (Dropped ${drop_from_peak:.2f}). "
                    f"Closing position..."
                )
                if close_position_by_ticket(symbol, ticket):
                    peak_profits.pop(ticket, None)

    # Clean up closed tickets from peak_profits
    closed_tickets = [t for t in list(peak_profits.keys()) if t not in current_tickets]
    for t in closed_tickets:
        peak_profits.pop(t, None)

    remaining_positions = mt5.positions_get(symbol=symbol)
    return len([p for p in (remaining_positions or []) if p.magic == config.MAGIC_NUMBER]) > 0

def run_bot():
    """Main loop for the $6K Funded Account EURUSD Scalping Bot"""
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    logger.info(f"MT5 initialized. Started $6K {SYMBOL} Scalping Bot on {TIMEFRAME}")
    
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
                
            # 3. Handle Open Positions & Monitor Trailing Profit ($40 peak, $2 pullback)
            if monitor_trailing_profits(SYMBOL):
                time.sleep(SLEEP_INTERVAL)
                continue

            # 4. Check Signals for New Positions
            signal, curr_k = check_scalping_signal(SYMBOL, TIMEFRAME)
            
            if signal == 'BUY':
                # $20 SL (0.02000), $50 TP (0.05000) for 0.01 lots on EURUSD
                sl_dist, tp_dist = 0.02000, 0.05000
                logger.info(f"🟢 BUY SIGNAL for {SYMBOL}! SL: {sl_dist}, TP: {tp_dist}")
                place_scalping_order(SYMBOL, mt5.ORDER_TYPE_BUY, sl_dist, tp_dist)
            elif signal == 'SELL':
                sl_dist, tp_dist = 0.02000, 0.05000
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
