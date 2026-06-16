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

def place_order(symbol, order_type, atr=None):
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
    sl, tp, sl_dist = get_sl_tp(symbol, order_type, price, atr)
    lots = calculate_lot_size(symbol, config.RISK_PERCENT, sl_dist)
    filling_mode = get_filling_type(symbol)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lots),
        "type": order_type,
        "price": price,
        "sl": float(round(sl, symbol_info.digits)),
        "tp": float(round(tp, symbol_info.digits)),
        "magic": config.MAGIC_NUMBER,
        "comment": "Gold Crossover Bot",
        "deviation": config.DEVIATION,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }
    
    # Validate order parameters before transmission
    check_result = mt5.order_check(request)
    if check_result.retcode not in (0, mt5.TRADE_RETCODE_DONE):
        log_error(f"MT5 pre-order validation failed for {symbol}: {check_result.comment} (Code: {check_result.retcode})")
        return None
        
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
