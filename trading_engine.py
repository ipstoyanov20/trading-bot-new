import MetaTrader5 as mt5
import config
from logger import log_trade, log_error, log_info

def check_open_positions(symbol):
    """Checks if there is an open position for the given symbol."""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        log_error(f"Failed to get positions for {symbol}: {mt5.last_error()}")
        return True # Assume exists to be safe
    return len(positions) > 0

def calculate_lot_size(symbol, risk_percent, sl_price_distance):
    """Calculates lot size based on risk and SL distance."""
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)
    
    if account_info is None or symbol_info is None:
        return 0.01 # Fallback to min lot
        
    equity = account_info.equity
    risk_amount = equity * (risk_percent / 100.0)
    
    # tick_value is the profit in account currency for 1 lot when price moves by 1 tick (tick_size)
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    
    if sl_price_distance == 0:
        return symbol_info.volume_min
        
    # How many ticks is the SL distance?
    num_ticks = sl_price_distance / tick_size
    
    # Risk per 1 lot = num_ticks * tick_value
    risk_per_lot = num_ticks * tick_value
    
    if risk_per_lot == 0:
        return symbol_info.volume_min
        
    lots = risk_amount / risk_per_lot
    
    # Normalize lots
    step = symbol_info.volume_step
    lots = round(lots / step) * step
    
    # Constraints
    lots = max(symbol_info.volume_min, min(symbol_info.volume_max, lots))
    
    # Extra safety for small balances
    if lots > config.MAX_LOT_SIZE:
        lots = config.MAX_LOT_SIZE
        
    return round(lots, 2)

def get_sl_tp(symbol, order_type, entry_price, atr):
    """Calculates SL and TP levels."""
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return 0, 0
        
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
    """Constructs and sends an order request."""
    # Check if symbol is available
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            log_error(f"Failed to select symbol {symbol}")
            return
            
    # Get current price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log_error(f"Failed to get ticks for {symbol}")
        return
        
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    # Calculate SL and TP
    sl, tp, sl_dist = get_sl_tp(symbol, order_type, price, atr)
    
    # Calculate Lot Size
    lots = calculate_lot_size(symbol, config.RISK_PERCENT, sl_dist)
    
    # Check Margin
    # Check if we have enough margin
    # (Simplified: we'll check order_check later)
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lots),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "magic": config.MAGIC_NUMBER,
        "comment": "MT5 Python Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    # Send check
    check_res = mt5.order_check(request)
    if check_res.retcode != mt5.TRADE_RETCODE_DONE:
        log_error(f"Order check failed for {symbol}: {check_res.comment}")
        return

    # Send order
    result = mt5.order_send(request)
    if result is None:
        log_error(f"Order send failed for {symbol}: {mt5.last_error()}")
        return

    action_str = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log_trade(symbol, action_str, lots, price, sl, tp, "SUCCESS")
        return result
    else:
        log_trade(symbol, action_str, lots, price, sl, tp, f"FAILED: {result.comment}")
        return None

def execute_signal(signal_data):
    """Executes a parsed signal from Telegram."""
    symbol = signal_data['symbol']
    order_type_str = signal_data['type']
    entry_range = signal_data['entry_range']
    sl = signal_data['sl']
    tps = signal_data['tps']
    
    # Check if symbol is available
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        log_error(f"Symbol {symbol} not found.")
        return
        
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)
        
    # Get current price
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log_error(f"Failed to get price for {symbol}")
        return
        
    order_type = mt5.ORDER_TYPE_BUY if order_type_str == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    # Check if price is in range
    if not (entry_range[0] <= price <= entry_range[1]):
        # Check if price is "better" than range (optional, but safer to stick to range)
        # For BUY, price < entry_range[0] is better. For SELL, price > entry_range[1] is better.
        is_better = (order_type == mt5.ORDER_TYPE_BUY and price < entry_range[0]) or \
                    (order_type == mt5.ORDER_TYPE_SELL and price > entry_range[1])
        
        if not is_better:
            log_info(f"Price {price} is out of entry range {entry_range}. Skipping.")
            return

    # Calculate total lot size based on SL distance
    sl_dist = abs(price - sl)
    total_lots = calculate_lot_size(symbol, config.RISK_PERCENT, sl_dist)
    
    # Split lots among TPs
    num_tps = len(tps)
    lots_per_tp = total_lots / num_tps
    
    # Normalize lots per TP
    step = symbol_info.volume_step
    lots_per_tp = max(symbol_info.volume_min, round(lots_per_tp / step) * step)
    
    log_info(f"EXECUTING SIGNAL | {symbol} {order_type_str} | Price: {price} | SL: {sl} | TPs: {tps} | Total Lots: {total_lots}")

    for i, tp in enumerate(tps):
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots_per_tp),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "magic": config.MAGIC_NUMBER,
            "comment": f"Telegram Signal TP{i+1}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        # In a real scenario, we might want to ensure all orders succeed or handle partial failure
        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log_trade(symbol, f"{order_type_str} TP{i+1}", lots_per_tp, price, sl, tp, "SUCCESS")
        else:
            err = mt5.last_error() if not result else result.comment
            log_trade(symbol, f"{order_type_str} TP{i+1}", lots_per_tp, price, sl, tp, f"FAILED: {err}")
