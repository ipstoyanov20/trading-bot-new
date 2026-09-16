import MetaTrader5 as mt5
import time
import logging
from datetime import datetime
import pandas as pd

import config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- User Configurable Settings ---
SYMBOL = "XAUUSD"                   # Gold Bot
TIMEFRAME = mt5.TIMEFRAME_M5        # M5 Timeframe
SLEEP_INTERVAL = 1                  # 1 second loop interval
COOLDOWN_SECONDS = 60               # Cooldown between completed grids
MAGIC_NUMBER = 60020                # Unique identifier for this bot's orders
DEVIATION = 20                      # Maximum price slippage in points

# --- Strategy Parameters: Bollinger Bands ---
BB_PERIOD = 20                      # Period 20
BB_DEVIATION = 2.0                  # Deviation 2.0
BB_SHIFT = 0                        # Shift 0
# Applied Price: Close
AUTHORIZED_ORDER_TYPE = "ALL"       # Authorized order type: ALL (BUY and SELL)

# --- Hedged Strategy Parameters (No Stop Loss) ---
FIRST_TRADE_VOLUME = 0.15           # First trade volume = 0.15 lots ($1.00 move on Gold = $15.00)
HEDGE_VOLUME = 0.15                 # Recovery hedge volume = 0.15 lots (fits margin cleanly in $6K account)

HEDGE_TRIGGER_POINTS = 100          # Open 2nd recovery hedge at 100 points ($1.00) adverse movement from first trade
HEDGE_EXIT_POINTS = 500             # Close hedged trade when it gains 500 points ($5.00) from ITS OWN entry price
FIRST_TRADE_TP_POINTS = 500         # First trade Take Profit at 500 points ($5.00) in its favor
BASKET_TP_USD = 30.0                # Combined basket Take Profit when both trades are open (+$30.00 net)

# NOTE: All Stop Loss exits are REMOVED entirely as requested.

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed grid basket
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log

grid_state = {
    "active": False,
    "initial_direction": None,      # 'BUY' or 'SELL'
    "anchor_price": 0.0,            # Entry price of first trade
    "hedge_entry_price": 0.0,       # Entry price of recovery hedge trade
    "hedge_triggered": False,       # Flag to prevent infinite re-opening loops
    "opened_levels": set(),         # Set of active level numbers (e.g. {1, 2})
}

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

def get_bollinger_bands(symbol, timeframe, period=20, deviation=2.0, count=60):
    """
    Fetches M5 OHLC data and calculates Bollinger Bands:
    period=20, deviation=2.0, shift=0, applied price=Close.
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) < period + 5:
        return pd.DataFrame()
        
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # Applied price = Close
    df['middle'] = df['close'].rolling(window=period).mean()
    df['std'] = df['close'].rolling(window=period).std(ddof=0)
    df['upper'] = df['middle'] + deviation * df['std']
    df['lower'] = df['middle'] - deviation * df['std']
    return df

def check_bb_entry_signal(symbol, timeframe=TIMEFRAME):
    """
    Evaluates Bollinger Bands entry signal on candle shift 1:
    - BUY: candle opens below Lower Band and closes back inside.
    - SELL: candle opens above Upper Band and closes back inside.
    Authorized order type: ALL.
    Returns: (signal, candle_time) where signal is 'BUY', 'SELL', or None.
    """
    df = get_bollinger_bands(symbol, timeframe, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    if df.empty or len(df) < BB_PERIOD + 2:
        return None, None
        
    # In df:
    # iloc[-1] is candle shift 0 (the current active, incomplete candle).
    # iloc[-2] is candle shift 1 (the latest completed candle).
    candle_shift_1 = df.iloc[-2]
    candle_time = candle_shift_1['time']
    c_open = candle_shift_1['open']
    c_close = candle_shift_1['close']
    c_upper = candle_shift_1['upper']
    c_lower = candle_shift_1['lower']
    c_middle = candle_shift_1['middle']
    
    if pd.isna(c_upper) or pd.isna(c_lower):
        return None, None
        
    # BUY: Opens below Lower Band and closes back inside (between Lower and Upper Band)
    is_buy = bool((c_open < c_lower) and (c_close >= c_lower) and (c_close <= c_upper))
    
    # SELL: Opens above Upper Band and closes back inside (between Lower and Upper Band)
    is_sell = bool((c_open > c_upper) and (c_close <= c_upper) and (c_close >= c_lower))
    
    if is_buy and AUTHORIZED_ORDER_TYPE in ["ALL", "BUY"]:
        logger.info(
            f"🟢 BB BUY Signal on Shift 1 [{candle_time}] | Open: {c_open:.2f} < Lower: {c_lower:.2f} | "
            f"Close: {c_close:.2f} >= Lower (Mid: {c_middle:.2f}, Upper: {c_upper:.2f})"
        )
        return 'BUY', candle_time
    elif is_sell and AUTHORIZED_ORDER_TYPE in ["ALL", "SELL"]:
        logger.info(
            f"🔴 BB SELL Signal on Shift 1 [{candle_time}] | Open: {c_open:.2f} > Upper: {c_upper:.2f} | "
            f"Close: {c_close:.2f} <= Upper (Mid: {c_middle:.2f}, Lower: {c_lower:.2f})"
        )
        return 'SELL', candle_time
        
    global last_no_signal_log_time, last_logged_no_signal_candle
    now = time.time()
    if (candle_time != last_logged_no_signal_candle) or (now - last_no_signal_log_time >= 15):
        last_no_signal_log_time = now
        last_logged_no_signal_candle = candle_time
        logger.info(
            f"⏳ Waiting for a signal (Bollinger Bands M5) | Shift 1 [{candle_time}] "
            f"Open: {c_open:.2f}, Close: {c_close:.2f} | Lower: {c_lower:.2f}, Mid: {c_middle:.2f}, Upper: {c_upper:.2f}"
        )
        
    return None, candle_time

def place_order_safe(symbol, order_type, volume, tp_points=0, sl_points=0, comment="BB_Grid"):
    """
    Places an order on MT5 with filling mode fallback and returns (result, fill_price).
    """
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        logger.error(f"Symbol {symbol} not found.")
        return None, 0.0
        
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)
        
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        logger.error(f"Failed to get tick for {symbol}")
        return None, 0.0
        
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    digits = getattr(symbol_info, 'digits', 2)
    point = getattr(symbol_info, 'point', 0.01)
    
    tp = 0.0
    if tp_points > 0:
        tp = round(price + (tp_points * point), digits) if order_type == mt5.ORDER_TYPE_BUY else round(price - (tp_points * point), digits)
        
    sl = 0.0
    if sl_points > 0:
        sl = round(price - (sl_points * point), digits) if order_type == mt5.ORDER_TYPE_BUY else round(price + (sl_points * point), digits)
        
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": float(tp),
        "magic": MAGIC_NUMBER,
        "comment": comment,
        "deviation": DEVIATION,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
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
            logger.info(f"✅ {action_name} Placed ({comment}) | Vol: {volume} | Price: {price} | TP: {tp} | Filling: {mode}")
            return res, price
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break
            
    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place order ({comment}): {err} | Request: {request}")
    return None, 0.0

def get_active_grid_positions(symbol):
    """
    Returns open positions belonging to this bot's MAGIC_NUMBER, sorted chronologically.
    """
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    bot_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_positions.sort(key=lambda p: p.time)
    return bot_positions

def close_grid_position(symbol, position):
    """Closes a single MT5 position safely."""
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False
        
    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.POSITION_TYPE_BUY else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": position.volume,
        "type": close_type,
        "position": position.ticket,
        "price": price,
        "deviation": DEVIATION,
        "magic": MAGIC_NUMBER,
        "comment": f"Close #{position.ticket}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_type(symbol),
    }
    
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
            logger.info(f"Closed #{position.ticket} at {price} (Profit: ${position.profit:.2f})")
            return True
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break
            
    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to close #{position.ticket}: {err}")
    return False

def close_all_grid_positions(symbol, reason="Grid Close"):
    """Closes all open positions belonging to this bot."""
    global last_close_time
    positions = get_active_grid_positions(symbol)
    if not positions:
        return True
        
    logger.info(f"Closing all {len(positions)} grid positions: {reason}")
    all_closed = True
    for p in positions:
        if not close_grid_position(symbol, p):
            all_closed = False
            
    last_close_time = time.time()
    return all_closed

def sync_grid_state(symbol):
    """
    Synchronizes grid_state with live MT5 positions.
    Handles bot restart gracefully without duplicating positions.
    """
    positions = get_active_grid_positions(symbol)
    if not positions:
        if grid_state["active"]:
            logger.info("All grid positions are closed. Resetting grid state.")
            grid_state["active"] = False
            grid_state["initial_direction"] = None
            grid_state["anchor_price"] = 0.0
            grid_state["hedge_entry_price"] = 0.0
            grid_state["hedge_triggered"] = False
            grid_state["opened_levels"].clear()
        return positions
        
    grid_state["active"] = True
    
    # Reconstruct opened levels from comments or chronological order
    opened_levels = set()
    for idx, p in enumerate(positions):
        lvl = None
        if p.comment and "BB_Grid_L" in p.comment:
            try:
                part = p.comment.split("BB_Grid_L")[1]
                lvl = int(part[0])
            except (IndexError, ValueError):
                lvl = None
        if lvl is None:
            lvl = idx + 1
        opened_levels.add(lvl)
        
    grid_state["opened_levels"] = opened_levels
    
    # If Level 2 is active, ensure hedge_triggered is True
    if 2 in opened_levels:
        grid_state["hedge_triggered"] = True
    
    # Level 1 defines initial direction and anchor price
    pos_l1 = None
    for p in positions:
        if p.comment and "BB_Grid_L1" in p.comment:
            pos_l1 = p
            break
    if pos_l1 is None and len(positions) > 0:
        pos_l1 = positions[0]
        
    if pos_l1:
        initial_dir = "BUY" if pos_l1.type == mt5.POSITION_TYPE_BUY else "SELL"
        grid_state["initial_direction"] = initial_dir
        if grid_state["anchor_price"] == 0.0:
            grid_state["anchor_price"] = pos_l1.price_open
            
    return positions

def place_grid_level(symbol, direction, level):
    """
    Places an order for Level 1 (first trade) or Level 2 (recovery hedge).
    """
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_Grid_L{level}_{direction}"
    volume = FIRST_TRADE_VOLUME if level == 1 else HEDGE_VOLUME
    
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        tp_points=0,
        sl_points=0,
        comment=comment
    )
    
    if res:
        grid_state["active"] = True
        grid_state["opened_levels"].add(level)
        if level == 1:
            grid_state["initial_direction"] = direction
            grid_state["anchor_price"] = fill_price
            grid_state["hedge_triggered"] = False
            logger.info(f"🎯 First Trade Started: Level 1 {direction} at {fill_price:.2f} | Vol: {volume} (Anchor: {fill_price:.2f})")
        else:
            grid_state["hedge_entry_price"] = fill_price
            grid_state["hedge_triggered"] = True
            logger.info(f"🛡️ 2nd Recovery Hedge Placed: Level 2 {direction} at {fill_price:.2f} | Vol: {volume} (Hedge Entry: {fill_price:.2f})")
        return res, fill_price
    return None, 0.0

def manage_hedged_grid(symbol):
    """
    Manages active trades for Hedged Strategy:
    1. Triggers 2nd recovery hedge (HEDGE_VOLUME) once when price moves 100 points against first trade.
    2. Closes the hedged trade when it gains 500 points from ITS OWN entry price.
    3. Closes first trade when price hits 500 points in its favor (Take Profit).
    4. Closes all trades together if combined basket net profit >= +$30.00.
    5. Stop Loss is REMOVED ENTIRELY (no SL exits).
    Returns True if positions remain open, False otherwise.
    """
    global last_status_log_time
    
    positions = sync_grid_state(symbol)
    if not positions:
        return False
        
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        return True
        
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return True
        
    point = getattr(symbol_info, 'point', 0.01)
    anchor = grid_state["anchor_price"]
    initial_dir = grid_state["initial_direction"]
    
    # Identify Level 1 (first trade) and Level 2 (hedge trade)
    pos_l1 = None
    pos_l2 = None
    for p in positions:
        if p.comment and "BB_Grid_L2" in p.comment:
            pos_l2 = p
        elif p.comment and "BB_Grid_L1" in p.comment:
            pos_l1 = p
            
    # Chronological fallback
    if pos_l1 is None and len(positions) > 0:
        pos_l1 = positions[0]
    if pos_l2 is None and len(positions) > 1:
        pos_l2 = positions[1]

    total_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)

    # 1. Combined Basket Take Profit Check: If both trades are open and net profit >= +BASKET_TP_USD
    if len(positions) > 1 and total_pnl >= BASKET_TP_USD:
        close_all_grid_positions(symbol, f"🎯 Combined Basket TP Reached: +${total_pnl:.2f} >= +${BASKET_TP_USD:.2f}")
        return False

    # 2. First Trade (Level 1) Take Profit at 500 points in its favor (when hedge is not open)
    if pos_l1 is not None and pos_l2 is None:
        l1_tp_dist = FIRST_TRADE_TP_POINTS * point
        l1_tp_hit = False
        if initial_dir == "BUY" and tick.bid >= anchor + l1_tp_dist:
            l1_tp_hit = True
        elif initial_dir == "SELL" and tick.ask <= anchor - l1_tp_dist:
            l1_tp_hit = True
            
        if l1_tp_hit:
            close_grid_position(symbol, pos_l1)
            logger.info(f"🎯 First Trade ({initial_dir}) Take Profit hit at +{FIRST_TRADE_TP_POINTS} points from anchor {anchor:.2f}!")
            sync_grid_state(symbol)
            return len(get_active_grid_positions(symbol)) > 0

    # 3. Trigger 2nd Recovery Hedge (Level 2) at 100 points against first trade
    # Guard: Must NOT be currently open AND must not have already triggered for this cycle!
    if pos_l2 is None and not grid_state.get("hedge_triggered", False) and (2 not in grid_state["opened_levels"]):
        hedge_trigger_dist = HEDGE_TRIGGER_POINTS * point
        if initial_dir == "BUY":
            # If price drops 100 points below first trade anchor -> Open SELL hedge
            if tick.bid <= anchor - hedge_trigger_dist:
                logger.info(
                    f"🛡️ Triggering 2nd Recovery Hedge (SELL {HEDGE_VOLUME} lots): "
                    f"Price {tick.bid:.2f} <= {anchor - hedge_trigger_dist:.2f} (100 pts below {anchor:.2f})"
                )
                place_grid_level(symbol, "SELL", level=2)
        elif initial_dir == "SELL":
            # If price rises 100 points above first trade anchor -> Open BUY hedge
            if tick.ask >= anchor + hedge_trigger_dist:
                logger.info(
                    f"🛡️ Triggering 2nd Recovery Hedge (BUY {HEDGE_VOLUME} lots): "
                    f"Price {tick.ask:.2f} >= {anchor + hedge_trigger_dist:.2f} (100 pts above {anchor:.2f})"
                )
                place_grid_level(symbol, "BUY", level=2)

    # 4. Close the Hedged Trade when it gains HEDGE_EXIT_POINTS (500 pts) from ITS OWN entry price
    if pos_l2 is None:
        # Check if it was just opened
        positions_now = get_active_grid_positions(symbol)
        for p in positions_now:
            if (p.comment and "BB_Grid_L2" in p.comment) or (pos_l1 and p.ticket != pos_l1.ticket):
                pos_l2 = p
                break
                
    if pos_l2 is not None:
        hedge_exit_dist = HEDGE_EXIT_POINTS * point
        hedge_exit_hit = False
        hedge_open_price = pos_l2.price_open
        
        if pos_l2.type == mt5.POSITION_TYPE_SELL:
            # SELL hedge makes profit when price drops below its entry price
            if tick.bid <= hedge_open_price - hedge_exit_dist:
                hedge_exit_hit = True
        elif pos_l2.type == mt5.POSITION_TYPE_BUY:
            # BUY hedge makes profit when price rises above its entry price
            if tick.ask >= hedge_open_price + hedge_exit_dist:
                hedge_exit_hit = True
                
        if hedge_exit_hit:
            exit_price = tick.bid if pos_l2.type == mt5.POSITION_TYPE_SELL else tick.ask
            close_grid_position(symbol, pos_l2)
            grid_state["opened_levels"].discard(2)
            grid_state["hedge_triggered"] = True  # Keep True so it does NOT loop and re-enter!
            logger.info(
                f"🎯 Hedged Trade #{pos_l2.ticket} closed at +{HEDGE_EXIT_POINTS} pts profit! "
                f"(Entry: {hedge_open_price:.2f} -> Exit: {exit_price:.2f})"
            )
            sync_grid_state(symbol)

    # Periodic status log (every 10 seconds)
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        pos_details = []
        for p in positions:
            d = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
            pos_details.append(f"#{p.ticket} ({d} {p.volume}l @ {p.price_open:.2f} PnL:${p.profit:.2f})")
        logger.info(f"📊 Active Grid ({len(positions)} trades): {' | '.join(pos_details)} | Basket Net: ${total_pnl:.2f} | Anchor: {anchor:.2f}")

    # NOTE: Stop Loss is REMOVED ENTIRELY — no stop loss exits.
    return len(get_active_grid_positions(symbol)) > 0

def run_bot():
    """Main execution loop for the $6K Funded Account XAUUSD M5 BB Hedged Strategy Bot (No Stop Loss)."""
    global last_close_time, last_processed_candle_time
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        return
        
    logger.info("=" * 60)
    logger.info(f"🚀 Started $6K Funded Account {SYMBOL} Hedged Strategy Bot")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Authorized Order Type: {AUTHORIZED_ORDER_TYPE}")
    logger.info(f"First Trade Volume: {FIRST_TRADE_VOLUME} lots | TP: +{FIRST_TRADE_TP_POINTS} pts")
    logger.info(f"2nd Recovery Hedge: {HEDGE_VOLUME} lots at {HEDGE_TRIGGER_POINTS} pts against first trade")
    logger.info(f"Hedge Exit: Closes at +{HEDGE_EXIT_POINTS} pts from hedge entry price")
    logger.info(f"Combined Basket TP: +${BASKET_TP_USD:.2f}")
    logger.info(f"Stop Loss: REMOVED ENTIRELY (No SL)")
    logger.info("=" * 60)
    
    try:
        while True:
            # 1. Manage Open Hedged Grid (if any)
            has_active_grid = manage_hedged_grid(SYMBOL)
            if has_active_grid:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 2. Check Cooldown after Grid Close
            time_since_last_close = time.time() - last_close_time
            if time_since_last_close < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 3. Check Bollinger Bands Entry Signal on Candle Shift 1
            signal, candle_time = check_bb_entry_signal(SYMBOL, TIMEFRAME)
            
            if signal and (last_processed_candle_time != candle_time):
                logger.info(f"📊 M5 BB Entry Signal [{candle_time}]: {signal}! Placing Level 1 ({FIRST_TRADE_VOLUME} lots)...")
                res, fill_price = place_grid_level(SYMBOL, signal, level=1)
                if res:
                    last_processed_candle_time = candle_time
                    
            time.sleep(SLEEP_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (KeyboardInterrupt).")
    except Exception as e:
        logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed gracefully.")

if __name__ == "__main__":
    run_bot()
