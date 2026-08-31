import MetaTrader5 as mt5
import config
from logger import log_trade, log_error, log_info
from datetime import datetime, timedelta

def check_open_positions(symbol):
    """
    Checks if there are any active trades for a specific symbol.
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return False
    bot_positions = [p for p in positions if p.magic == config.MAGIC_NUMBER]
    return len(bot_positions) > 0

def get_open_positions_count(symbol):
    """Returns the number of open positions for the bot's magic number."""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return 0
    bot_positions = [p for p in positions if p.magic == config.MAGIC_NUMBER]
    return len(bot_positions)

def get_portfolio_pnl(symbol):
    """Returns the total floating profit of all open bot positions."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return 0.0
    return sum([p.profit for p in positions if p.magic == config.MAGIC_NUMBER])

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

def send_order_safe(request):
    """
    Sends order to MT5 with automatic fallback across all supported filling modes
    (FOK, IOC, RETURN) if broker returns 'Unsupported filling mode' (10030).
    """
    symbol = request.get("symbol")
    modes_to_try = [get_filling_type(symbol), mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    unique_modes = []
    for m in modes_to_try:
        if m not in unique_modes:
            unique_modes.append(m)
            
    res = None
    for mode in unique_modes:
        req = request.copy()
        req["type_filling"] = mode
        res = mt5.order_send(req)
        if res and res.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            return res
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break
    return res

def close_all_positions(symbol):
    """Closes all open positions for the bot's magic number."""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return True
    
    success = True
    for p in positions:
        if p.magic != config.MAGIC_NUMBER:
            continue
            
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
            
        order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": p.volume,
            "type": order_type,
            "position": p.ticket,
            "price": price,
            "deviation": config.DEVIATION,
            "magic": config.MAGIC_NUMBER,
            "comment": "Portfolio Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": get_filling_type(symbol),
        }
        
        result = send_order_safe(request)
        if result is None or result.retcode not in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            success = False
            log_error(f"Failed to close position {p.ticket}: {mt5.last_error() if result is None else result.comment}")
        else:
            log_info(f"Closed position {p.ticket} at {price} for profit {p.profit}")
            
    return success

def close_position_by_ticket(symbol, ticket):
    """Closes a specific open position by its ticket number."""
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
        
    p = positions[0]
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False
        
    order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": p.volume,
        "type": order_type,
        "position": p.ticket,
        "price": price,
        "deviation": config.DEVIATION,
        "magic": config.MAGIC_NUMBER,
        "comment": "Close by Ticket",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
    result = send_order_safe(request)
    if result is None or result.retcode not in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
        log_error(f"Failed to close position {p.ticket}: {mt5.last_error() if result is None else result.comment}")
        return False
    else:
        log_info(f"Closed position {p.ticket} at {price} for profit {p.profit}")
        return True

def calculate_lot_size(symbol, risk_percent, sl_price_distance):
    """
    Calculates the appropriate lot size based on account equity and risk percentage.
    Formula: Lot = (Equity * Risk%) / (SL_Distance * Tick_Value / Tick_Size)
    """
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)
    
    if account_info is None or symbol_info is None:
        return 0.01 # Safe default
        
    equity = account_info.equity
    risk_amount = equity * (risk_percent / 100.0)
    
    # Tick value is profit in account currency for 1 lot when price moves by 1 tick
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    
    if sl_price_distance == 0:
        return symbol_info.volume_min
        
    # SL distance in ticks
    num_ticks = sl_price_distance / tick_size
    
    # Financial risk for 1 full lot
    risk_per_lot = num_ticks * tick_value
    
    if risk_per_lot == 0:
        return symbol_info.volume_min
        
    # Calculate initial lot size
    lots = risk_amount / risk_per_lot
    
    # Normalize lots to match symbol's volume step (e.g., 0.01)
    step = symbol_info.volume_step
    lots = round(lots / step) * step
    
    # Apply broker limits (min/max lot)
    lots = max(symbol_info.volume_min, min(symbol_info.volume_max, lots))
    
    # Cap by global max lot setting in config
    if lots > config.MAX_LOT_SIZE:
        lots = config.MAX_LOT_SIZE
        
    return round(lots, 2)

def get_sl_tp(symbol, order_type, entry_price, atr):
    """
    Determines Stop Loss (SL) and Take Profit (TP) levels.
    Uses either ATR-based dynamics or fixed points from config.
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
    General purpose function to place a Market Order.
    Includes validation, margin checks, and logging.
    """
    print(f"--- DIAGNOSTICS: Starting place_order for {symbol} ---")
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            print(f"--- DIAGNOSTICS: Failed to select symbol {symbol} ---")
            log_error(f"Failed to select symbol {symbol}")
            return
            
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"--- DIAGNOSTICS: Failed to get ticks for {symbol} ---")
        log_error(f"Failed to get ticks for {symbol}")
        return
        
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    sl, tp, sl_dist = get_sl_tp(symbol, order_type, price, atr)
    
    # Batch Portfolio uses a fixed lot size
    lots = config.FIXED_LOT_SIZE
    
    # Emergency crash protection: we keep the SL, but remove the hard TP so portfolio logic can manage it
    tp = 0.0
    sl = 0.0
    
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
        "type_filling": get_filling_type(symbol),
    }
    
    print(f"--- DIAGNOSTICS: Built Request: {request} ---")
    
    # Check if order is valid before sending
    check_res = mt5.order_check(request)
    print(f"--- DIAGNOSTICS: order_check returned: {check_res} ---")
    
    if check_res is not None and check_res.retcode != 0 and check_res.retcode not in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
        print(f"--- DIAGNOSTICS: order_check failed! Retcode: {check_res.retcode}, Comment: {check_res.comment} ---")
        log_error(f"Order check failed for {symbol}: {check_res.comment}")
        return None

    print("--- DIAGNOSTICS: Sending order... ---")
    # Execute the trade with filling fallback
    result = send_order_safe(request)
    print(f"--- DIAGNOSTICS: order_send returned: {result} ---")
    
    if result is None:
        print(f"--- DIAGNOSTICS: order_send returned None! MT5 Last Error: {mt5.last_error()} ---")
        log_error(f"Order send failed for {symbol}: {mt5.last_error()}")
        return None

    action_str = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    # 10009 is DONE, 10008 is PLACED, 0 is sometimes returned by brokers/API for success.
    if result.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
        log_trade(symbol, action_str, lots, price, sl, tp, "SUCCESS")
        return result
    else:
        print(f"--- DIAGNOSTICS: order_send returned failed retcode! Retcode: {result.retcode}, Comment: {result.comment} ---")
        log_trade(symbol, action_str, lots, price, sl, tp, f"FAILED (Code {result.retcode}): {result.comment}")
        return None

def execute_signal(signal_data):
    """
    Specific handler for signals parsed from Telegram messages.
    Logic:
    1. Validates symbol availability.
    2. Checks if current price is within the signal's entry range.
    3. Calculates lots and executes at TP1 level only.
    """
    symbol = signal_data['symbol']
    order_type_str = signal_data['type']
    entry_range = signal_data['entry_range']
    sl = signal_data['sl']
    tps = signal_data['tps']
    
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        log_error(f"Symbol {symbol} not found.")
        return
        
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)
        
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log_error(f"Failed to get price for {symbol}")
        return
        
    order_type = mt5.ORDER_TYPE_BUY if order_type_str == "BUY" else mt5.ORDER_TYPE_SELL
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    
    # 2. Entry Range Validation
    # Checks if current broker price matches the signal's suggested entry window.
    low_range, high_range = entry_range
    in_range = low_range <= price <= high_range
    
    if not in_range:
        # Check if the price is even 'better' than the range (slippage in our favor)
        is_better = (order_type == mt5.ORDER_TYPE_BUY and price < low_range) or \
                    (order_type == mt5.ORDER_TYPE_SELL and price > high_range)
        
        if not is_better:
            log_info(f"Price {price} is outside entry range {entry_range}. Signal ignored.")
            return

    # 3. Lot Calculation (Risk Management)
    sl_dist = abs(price - sl)
    total_lots = calculate_lot_size(symbol, config.RISK_PERCENT, sl_dist)
    
    log_info(f"EXECUTING SIGNAL | {symbol} {order_type_str} | Price: {price} | SL: {sl} | TP1: {tps[0]} | Lots: {total_lots}")

    # 4. Execution (TP1 Only)
    # The user specifically requested to use full lot size for the first Take Profit level.
    tp = tps[0]
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(total_lots),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "magic": config.MAGIC_NUMBER,
        "comment": "Telegram Signal TP1",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
    result = send_order_safe(request)
    if result and result.retcode in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
        log_trade(symbol, f"{order_type_str} TP1", total_lots, price, sl, tp, "SUCCESS")
        return result
    else:
        err = mt5.last_error() if not result else result.comment
        log_trade(symbol, f"{order_type_str} TP1", total_lots, price, sl, tp, f"FAILED: {err}")
        return None

def get_last_positions(n=3):
    """
    Fetches the last n executed positions (Active or History).
    Returns:
        list: A list of summaries for the last n positions/deals.
    """
    # 1. Try fetching active positions first
    positions = mt5.positions_get()
    pos_list = []
    
    if positions is not None and len(positions) > 0:
        for p in positions:
            pos_list.append({
                'symbol': p.symbol,
                'volume': p.volume,
                'type': "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                'price_open': p.price_open,
                'profit': round(p.profit, 2),
                'status': 'ACTIVE',
                'time': p.time
            })
    
    # 2. If we have less than n active positions, pull the rest from recent history
    if len(pos_list) < n:
        # Check history for the last 24 hours
        from_date = datetime.now() - timedelta(days=1)
        history_deals = mt5.history_deals_get(from_date, datetime.now())
        
        if history_deals:
            # Sort by time descending
            sorted_deals = sorted(history_deals, key=lambda x: x.time, reverse=True)
            for d in sorted_deals:
                # Filter for actual entries/exits (excluding balance operations)
                if d.entry == mt5.DEAL_ENTRY_IN or d.entry == mt5.DEAL_ENTRY_OUT:
                    pos_list.append({
                        'symbol': d.symbol,
                        'volume': d.volume,
                        'type': "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                        'price_open': d.price,
                        'profit': round(d.profit, 2),
                        'status': 'CLOSED' if d.entry == mt5.DEAL_ENTRY_OUT else 'ENTERED',
                        'time': d.time
                    })
                if len(pos_list) >= n:
                    break
    
    # Sort entire list by time, newest first
    pos_list.sort(key=lambda x: x['time'], reverse=True)
    
    return pos_list[:n]
