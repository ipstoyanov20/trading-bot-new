import MetaTrader5 as mt5
import config
from logger import log_trade, log_error, log_info

def initialize_mt5():
    """
    Initializes MetaTrader 5 and handles login based on configurations.
    """
    if not mt5.initialize():
        log_error(f"MT5 initialization failed: {mt5.last_error()}")
        return False
        
    # Programmatic login if details are provided in config
    if config.ACCOUNT_LOGIN != 0:
        authorized = mt5.login(
            login=config.ACCOUNT_LOGIN,
            password=config.ACCOUNT_PASSWORD,
            server=config.ACCOUNT_SERVER
        )
        if not authorized:
            log_error(f"Failed to login to account {config.ACCOUNT_LOGIN}: {mt5.last_error()}")
            return False
            
    # Success check
    account_info = mt5.account_info()
    if account_info is None:
        log_error("Failed to retrieve account details.")
        return False
    
    log_info(f"MT5 Connected. Account: {account_info.login}, Balance: {account_info.balance} {account_info.currency}")
    return True

def get_filling_type(symbol):
    """
    Dynamically determines the correct execution filling mode supported by the broker.
    This prevents ORDER_FILLING rejects.
    """
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return mt5.ORDER_FILLING_FOK
        
    filling_mode = symbol_info.filling_mode
    
    # Define MQL5 filling mode constants missing from the Python library
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    
    # Check flags for filling modes
    if filling_mode & SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    elif filling_mode & SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    else:
        return mt5.ORDER_FILLING_RETURN

def check_open_positions(symbol):
    """
    Checks if there are any active trades for the symbol placed by this bot (filtered by MAGIC_NUMBER).
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        log_error(f"Failed to get positions for {symbol}: {mt5.last_error()}")
        return True  # Default to True to block trading on error (safe mode)
        
    # Filter positions that match our unique MAGIC_NUMBER
    bot_positions = [p for p in positions if p.magic == config.MAGIC_NUMBER]
    return len(bot_positions) > 0

def calculate_lot_size(symbol, risk_percent, sl_price_distance):
    """
    Calculates dynamic lot size based on account equity and risk percentage.
    Formula: Lot = (Equity * Risk%) / (SL_Distance * Tick_Value / Tick_Size)
    """
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)
    
    if account_info is None or symbol_info is None:
        return 0.01  # Safe default fallback
        
    equity = account_info.equity
    risk_amount = equity * (risk_percent / 100.0)
    
    # Tick values
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    
    if sl_price_distance <= 0:
        return symbol_info.volume_min
        
    # Number of ticks inside the Stop Loss distance
    num_ticks = sl_price_distance / tick_size
    
    # Financial risk for 1 standard lot
    risk_per_lot = num_ticks * tick_value
    
    if risk_per_lot == 0:
        return symbol_info.volume_min
        
    # Calculated raw lot size
    lots = risk_amount / risk_per_lot
    
    # Round to volume step requirements (e.g. 0.01 steps)
    step = symbol_info.volume_step
    lots = round(lots / step) * step
    
    # Apply broker restrictions
    lots = max(symbol_info.volume_min, min(symbol_info.volume_max, lots))
    
    # Apply user-defined safety ceiling
    if lots > config.MAX_LOT_SIZE:
        lots = config.MAX_LOT_SIZE
        
    return round(lots, 2)

def get_sl_tp(symbol, order_type, entry_price, atr):
    """
    Calculates Stop Loss and Take Profit levels based on config preferences.
    """
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return 0, 0, 0
        
    point = symbol_info.point
    
    if config.USE_ATR_FOR_EXIT and atr:
        sl_dist = atr * config.ATR_SL_MULT
        tp_dist = atr * config.ATR_TP_MULT
    else:
        sl_dist = config.FIXED_SL_POINTS * point
        tp_dist = config.FIXED_TP_POINTS * point
        
    if order_type == mt5.ORDER_TYPE_BUY:
        sl = entry_price - sl_dist
        tp = entry_price + tp_dist
    else:
        sl = entry_price + sl_dist
        tp = entry_price - tp_dist
        
    return sl, tp, sl_dist

def place_order(symbol, order_type, atr=None, volume=None, sl_price_dist=None, tp_price_dist=None):
    """
    Assembles, validates, and routes order to MT5.
    """
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        log_error(f"Symbol {symbol} not found on broker server.")
        return None
        
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            log_error(f"Failed to add symbol {symbol} to Market Watch.")
            return None
            
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log_error(f"Failed to fetch market rates/ticks for {symbol}.")
        return None
        
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    if sl_price_dist is not None and tp_price_dist is not None:
        if order_type == mt5.ORDER_TYPE_BUY:
            sl = price - sl_price_dist
            tp = price + tp_price_dist
        else:
            sl = price + sl_price_dist
            tp = price - tp_price_dist
        sl_dist = sl_price_dist
    else:
        sl, tp, sl_dist = get_sl_tp(symbol, order_type, price, atr)
        
    if volume is not None:
        lots = volume
    else:
        lots = calculate_lot_size(symbol, config.RISK_PERCENT, sl_dist)
        
    filling_mode = get_filling_type(symbol)
    
    # We will dynamically adjust lots to fit free margin if needed
    account_info = mt5.account_info()
    free_margin = account_info.margin_free if account_info is not None else 50.0
    
    step = symbol_info.volume_step
    min_vol = symbol_info.volume_min
    
    # Ensure lots is rounded properly
    lots = round(round(lots / step) * step, 2)
    lots = max(min_vol, min(symbol_info.volume_max, lots))
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lots),
        "type": order_type,
        "price": price,
        "sl": float(round(sl, symbol_info.digits)),
        "tp": float(round(tp, symbol_info.digits)),
        "magic": config.MAGIC_NUMBER,
        "comment": "Gold AI Bot",
        "deviation": config.DEVIATION,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }
    
    # Validate and scale down if margin is insufficient
    while lots >= min_vol:
        request["volume"] = float(lots)
        check_result = mt5.order_check(request)
        
        # Check if order validation succeeded and margin is within limits
        if check_result.retcode in (0, mt5.TRADE_RETCODE_DONE) and check_result.margin <= free_margin:
            break
            
        log_info(f"Margin check failed for {lots} lots (Required: {check_result.margin}, Free: {free_margin}). Scaling down...")
        lots = round(lots - step, 2)
        
    if lots < min_vol:
        log_error(f"Cannot place trade on {symbol}: Insufficient margin even for minimum lot {min_vol}.")
        return None
        
    log_info(f"Placing trade: {lots} lots on {symbol}...")
    
    # Execute the trade
    result = mt5.order_send(request)
    action_str = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    
    if result is None:
        log_trade(symbol, action_str, lots, price, sl, tp, "FAILED: order_send returned None")
        return None
        
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade(symbol, action_str, lots, price, sl, tp, f"SUCCESS (Ticket: {result.order})")
        return result
    else:
        log_trade(symbol, action_str, lots, price, sl, tp, f"FAILED: Code {result.retcode} ({result.comment})")
        return None

def close_all_bot_positions(symbol):
    """
    Closes all active positions for the symbol that were opened by this bot (magic number check).
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None or len(positions) == 0:
        return
        
    for pos in positions:
        if pos.magic == config.MAGIC_NUMBER:
            ticket = pos.ticket
            order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                log_error(f"Failed to get tick to close position {ticket}")
                continue
                
            price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.volume,
                "type": order_type,
                "position": ticket,
                "price": price,
                "deviation": config.DEVIATION,
                "magic": config.MAGIC_NUMBER,
                "comment": "Close Gold Bot Position",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": get_filling_type(symbol),
            }
            
            result = mt5.order_send(request)
            if result is None:
                log_error(f"Failed to close position {ticket}: order_send returned None")
            elif result.retcode != mt5.TRADE_RETCODE_DONE:
                log_error(f"Failed to close position {ticket}: {result.comment} (Code: {result.retcode})")
            else:
                log_info(f"Successfully closed position {ticket} (Profit: {pos.profit})")

def close_position_by_ticket(symbol, ticket):
    """
    Closes a specific open position by its ticket number.
    """
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
        
    pos = positions[0]
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log_error(f"Failed to get tick to close position {ticket}")
        return False
        
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": pos.volume,
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": config.DEVIATION,
        "magic": config.MAGIC_NUMBER,
        "comment": "Trailing Profit Close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = mt5.last_error() if result is None else result.comment
        log_error(f"Failed to close position {ticket}: {err}")
        return False
    else:
        log_info(f"Successfully closed position {ticket} (Profit: {pos.profit})")
        return True

def monitor_trailing_profits(symbol, peak_profits):
    """
    Monitors open positions for trailing profit lock ($40 peak, $2 pullback).
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
                log_info(
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


