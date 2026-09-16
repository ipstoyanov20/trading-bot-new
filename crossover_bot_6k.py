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
COOLDOWN_SECONDS = 60               # Cooldown between completed trades
MAGIC_NUMBER = 60020                # Unique identifier for this bot's orders
DEVIATION = 20                      # Maximum price slippage in points

# --- Strategy Parameters: Bollinger Bands ---
BB_PERIOD = 20                      # Period 20
BB_DEVIATION = 2.0                  # Deviation 2.0
BB_SHIFT = 0                        # Shift 0
# Applied Price: Close
AUTHORIZED_ORDER_TYPE = "ALL"       # Authorized order type: ALL (BUY and SELL)

# --- Trade & Trailing Profit Parameters ---
TRADE_VOLUME = 0.15                  # Volume: 0.15 lots for all levels ($1 move in Gold = $15.00)
SL_POINTS = 200                      # Hard Stop Loss: 200 points ($2.00 move = -$30.00 max risk)
MAX_LOSS_USD = 30.0                  # Max allowed dollar loss floor per trade (-$30.00)
BE_ACTIVATION_USD = 10.0             # Profit threshold to activate Break-Even protection (+$10.00)
MIN_LOCKED_PROFIT_USD = 2.0          # Break-Even guaranteed floor once +$10 is reached (+$2.00)
TRAIL_ACTIVATION_USD = 30.0          # Minimum profit in USD to activate peak trailing (+~$2.00 move)
TRAIL_PULLBACK_USD = 5.0             # Pullback/drop in USD from peak profit to trigger exit ($5.00)

# --- Recovery Ladder Parameters ---
MAX_RECOVERY_LEVEL = 4               # 1 Initial trade + up to 3 recoveries (4 trades max)

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log
trade_peak_profit = {}              # Tracks peak floating profit per ticket: {ticket: float}

recovery_state = {
    "is_active": False,
    "current_level": 0,              # 1 = Initial, 2 = Recovery 1, 3 = Recovery 2, 4 = Recovery 3
    "direction": None,               # "BUY" or "SELL"
    "anchor_price": 0.0,             # Entry price of Level 1 trade
    "last_ticket": None,             # Tracked ticket
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

def place_order_safe(symbol, order_type, volume, tp_points=0, sl_points=0, comment="BB_SingleTrade", tp_price=0.0, sl_price=0.0):
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
    if tp_price > 0:
        tp = round(tp_price, digits)
    elif tp_points > 0:
        tp = round(price + (tp_points * point), digits) if order_type == mt5.ORDER_TYPE_BUY else round(price - (tp_points * point), digits)
        
    sl = 0.0
    if sl_price > 0:
        sl = round(sl_price, digits)
    elif sl_points > 0:
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
            logger.info(f"✅ {action_name} Placed ({comment}) | Vol: {volume} | Price: {price} | TP: {tp} | SL: {sl} | Filling: {mode}")
            return res, price
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break
            
    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place order ({comment}): {err} | Request: {request}")
    return None, 0.0

def get_active_positions(symbol):
    """
    Returns open positions belonging to this bot's MAGIC_NUMBER, sorted chronologically.
    """
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    bot_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_positions.sort(key=lambda p: p.time)
    return bot_positions

def close_position(symbol, position):
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

def reset_recovery_state():
    """Resets the recovery ladder state upon successful recovery or sequence termination."""
    recovery_state["is_active"] = False
    recovery_state["current_level"] = 0
    recovery_state["direction"] = None
    recovery_state["anchor_price"] = 0.0
    recovery_state["last_ticket"] = None
    trade_peak_profit.clear()

def get_deal_pnl_by_position(ticket, symbol=SYMBOL, magic=MAGIC_NUMBER):
    """
    Fetches the closing deal PnL for a specific position ticket.
    Retries up to 6 times with 100ms pauses (600ms total) to allow MT5 to write the deal to database.
    If ticket is unknown, falls back safely to the latest closed deal for this magic number.
    """
    # 1. Primary: Match exact position ticket
    if ticket:
        for attempt in range(6):
            deals = mt5.history_deals_get(position=ticket)
            if deals:
                out_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
                if out_deals:
                    deal = out_deals[-1]
                    pnl = deal.profit + deal.swap + getattr(deal, 'commission', 0.0)
                    return deal.ticket, pnl, deal.price
            time.sleep(0.1)

    # 2. Fallback: Query today's deals, waiting briefly if needed
    for attempt in range(4):
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(today, now)
        if deals:
            bot_deals = [d for d in deals if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT]
            if bot_deals:
                deal = bot_deals[-1]
                # If we know the ticket, ensure we don't return an older deal
                if ticket and getattr(deal, 'position_id', None) != ticket:
                    time.sleep(0.1)
                    continue
                pnl = deal.profit + deal.swap + getattr(deal, 'commission', 0.0)
                return deal.ticket, pnl, deal.price
        time.sleep(0.1)

    return None, 0.0, 0.0

def open_initial_trade(symbol, direction):
    """
    Opens Level 1 trade from Bollinger Bands signal.
    Sets anchor price and initializes recovery ladder state.
    """
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_{direction}_L1"
    
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=TRADE_VOLUME,
        tp_points=0,
        sl_points=SL_POINTS,
        comment=comment
    )
    
    if res:
        recovery_state["is_active"] = True
        recovery_state["current_level"] = 1
        recovery_state["direction"] = direction
        recovery_state["anchor_price"] = fill_price
        recovery_state["last_ticket"] = getattr(res, 'order', None)
        logger.info(
            f"🚀 Level 1 Trade Placed: {direction} {TRADE_VOLUME} lots at {fill_price:.2f} | "
            f"Anchor: {fill_price:.2f} | Hard SL: {SL_POINTS} pts (-${MAX_LOSS_USD:.2f}) | "
            f"BE: +${BE_ACTIVATION_USD:.2f} (+${MIN_LOCKED_PROFIT_USD:.2f} floor) | Trailing: +${TRAIL_ACTIVATION_USD:.2f}+ (-${TRAIL_PULLBACK_USD:.2f})"
        )
        return res, fill_price
    return None, 0.0

def open_recovery_trade(symbol, level):
    """
    Opens a recovery trade at Level 2, 3, or 4:
    - Same direction as Level 1
    - Same volume (TRADE_VOLUME lots)
    - Take Profit placed at Anchor Price (the beginning of the first trade)
    - Stop Loss placed at SL_POINTS (200 pts) from new entry price
    - Retries up to 3 times if broker temporarily rejects due to slippage
    """
    direction = recovery_state["direction"]
    anchor = recovery_state["anchor_price"]
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_{direction}_L{level}"
    
    for attempt in range(3):
        res, fill_price = place_order_safe(
            symbol=symbol,
            order_type=order_type,
            volume=TRADE_VOLUME,
            tp_price=anchor,
            sl_points=SL_POINTS,
            comment=comment
        )
        
        if res:
            recovery_state["current_level"] = level
            recovery_state["last_ticket"] = getattr(res, 'order', None)
            logger.info(
                f"🛡️ Level {level} Recovery Trade Placed: {direction} {TRADE_VOLUME} lots at {fill_price:.2f} | "
                f"Take Profit at Anchor: {anchor:.2f} | Hard SL: {SL_POINTS} pts (-${MAX_LOSS_USD:.2f})"
            )
            return res, fill_price
        else:
            logger.warning(f"⚠️ Attempt {attempt + 1}/3 to place Level {level} recovery trade failed. Retrying in 0.5s...")
            time.sleep(0.5)

    logger.error(f"❌ Failed to place Level {level} recovery trade after 3 attempts! Terminating recovery sequence.")
    reset_recovery_state()
    return None, 0.0

def manage_active_trade(symbol):
    """
    Manages the active trade and the 4-Level Recovery Ladder:
    - Level 1: Break-Even floor (+${MIN_LOCKED_PROFIT_USD:.2f}) at +${BE_ACTIVATION_USD:.2f} and Peak Trailing at +${TRAIL_ACTIVATION_USD:.2f}.
    - Level 2, 3, 4: Hard Take Profit at initial anchor price (recoups all losses).
    - If SL hit (-${MAX_LOSS_USD:.2f}):
      - Level 1 -> Immediately opens Level 2 from SL price.
      - Level 2 -> Immediately opens Level 3 from SL price.
      - Level 3 -> Immediately opens Level 4 from SL price.
      - Level 4 -> Terminates sequence, enforces 60s cooldown.
    """
    global last_close_time, last_status_log_time, trade_peak_profit
    
    positions = get_active_positions(symbol)
    
    # CASE A: No active positions found in MT5
    if not positions:
        if recovery_state["is_active"]:
            last_ticket = recovery_state.get("last_ticket")
            deal_ticket, deal_pnl, deal_exit_price = get_deal_pnl_by_position(last_ticket)
            level = recovery_state["current_level"]
            
            # Did the trade close in profit (TP hit at broker level)?
            if deal_pnl is not None and deal_pnl > 0:
                logger.info(f"🎉 Level {level} trade closed in profit (+${deal_pnl:.2f}) at {deal_exit_price:.2f}! Recovery sequence succeeded. Resetting state.")
                reset_recovery_state()
                last_close_time = time.time()
                return False
            else:
                # Closed at loss (SL hit at broker level)
                logger.info(f"⚠️ Level {level} trade was closed at SL (PnL: ${deal_pnl:.2f} at {deal_exit_price:.2f}).")
                if level < MAX_RECOVERY_LEVEL:
                    next_level = level + 1
                    logger.info(f"🔄 Starting Level {next_level} Recovery Trade immediately from SL price...")
                    trade_peak_profit.clear()
                    res, _ = open_recovery_trade(symbol, next_level)
                    return True if res else False
                else:
                    logger.warning(f"❌ Level {MAX_RECOVERY_LEVEL} hit SL. Max recovery levels reached (Sequence Loss: -${MAX_RECOVERY_LEVEL * MAX_LOSS_USD:.2f}). Waiting for 60s cooldown...")
                    reset_recovery_state()
                    last_close_time = time.time()
                    return False
                    
        trade_peak_profit.clear()
        return False

    # CASE B: Trade is currently open
    position = positions[0]
    ticket = position.ticket
    recovery_state["last_ticket"] = ticket  # Always sync exact active ticket
    total_pnl = position.profit + position.swap + getattr(position, 'commission', 0.0)
    level = recovery_state.get("current_level", 1)
    anchor = recovery_state.get("anchor_price", 0.0)
    direction = recovery_state.get("direction", "BUY")

    # Track peak profit
    if ticket not in trade_peak_profit:
        trade_peak_profit[ticket] = total_pnl
    else:
        if total_pnl > trade_peak_profit[ticket]:
            old_peak = trade_peak_profit[ticket]
            trade_peak_profit[ticket] = total_pnl
            if total_pnl >= TRAIL_ACTIVATION_USD and (total_pnl - old_peak >= 2.0):
                logger.info(f"🔥 New Peak Profit for #{ticket} (L{level}): +${total_pnl:.2f}")
            elif total_pnl >= BE_ACTIVATION_USD and old_peak < BE_ACTIVATION_USD and level == 1:
                logger.info(f"🛡️ Position #{ticket} crossed +${BE_ACTIVATION_USD:.2f}! Break-Even floor locked at +${MIN_LOCKED_PROFIT_USD:.2f}.")

    peak = trade_peak_profit[ticket]

    # 1. Hard Downside Protection (-$30.00 max loss check)
    if total_pnl <= -MAX_LOSS_USD:
        logger.info(f"🛑 Level {level} hit Stop Loss threshold (P&L: ${total_pnl:.2f} <= -${MAX_LOSS_USD:.2f}). Closing position...")
        if close_position(symbol, position):
            trade_peak_profit.pop(ticket, None)
            if level < MAX_RECOVERY_LEVEL:
                next_level = level + 1
                logger.info(f"🔄 Starting Level {next_level} Recovery Trade immediately from {position.price_current:.2f}...")
                res, _ = open_recovery_trade(symbol, next_level)
                return True if res else False
            else:
                logger.warning(f"❌ Level {MAX_RECOVERY_LEVEL} hit SL. Max recovery levels reached (Sequence Loss: -${MAX_RECOVERY_LEVEL * MAX_LOSS_USD:.2f}). Waiting for 60s cooldown...")
                reset_recovery_state()
                last_close_time = time.time()
                return False

    # 2. Profit Exit Management
    if level == 1:
        # Level 1 uses 2-Stage Profit: BE between $10 and $30, Peak Trailing from $30+
        if peak >= BE_ACTIVATION_USD and peak < TRAIL_ACTIVATION_USD:
            if total_pnl <= MIN_LOCKED_PROFIT_USD:
                logger.info(f"🛡️ Level 1 Break-Even Floor Triggered! Peak: +${peak:.2f}, dropped to +${total_pnl:.2f}. Securing floor...")
                if close_position(symbol, position):
                    reset_recovery_state()
                    last_close_time = time.time()
                    return False
        elif peak >= TRAIL_ACTIVATION_USD:
            drop_from_peak = peak - total_pnl
            if drop_from_peak >= TRAIL_PULLBACK_USD or total_pnl <= MIN_LOCKED_PROFIT_USD:
                logger.info(f"💰 Level 1 Trailing Exit Triggered! Peak: +${peak:.2f}, Current: +${total_pnl:.2f}. Securing profit...")
                if close_position(symbol, position):
                    reset_recovery_state()
                    last_close_time = time.time()
                    return False
    else:
        # Level 2, 3, 4: Target is Anchor Price (the beginning of the first trade)
        tick = mt5.symbol_info_tick(symbol)
        anchor_reached = False
        if tick and anchor > 0:
            if direction == "BUY" and tick.bid >= anchor:
                anchor_reached = True
            elif direction == "SELL" and tick.ask <= anchor:
                anchor_reached = True

        if anchor_reached:
            logger.info(f"🎉 Level {level} reached Anchor Price ({anchor:.2f})! Sequence recovered (P&L: +${total_pnl:.2f}). Closing position...")
            if close_position(symbol, position):
                reset_recovery_state()
                last_close_time = time.time()
                return False

    # Periodic status log (every 10 seconds)
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        trade_dir = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"
        target_str = f"Anchor TP: {anchor:.2f}" if level > 1 else f"Trailing Target (+${TRAIL_ACTIVATION_USD:.2f})"
        logger.info(
            f"📈 Active L{level} #{ticket} ({trade_dir} {position.volume} lots @ {position.price_open:.2f} | SL: {position.sl:.2f} | {target_str}) | "
            f"P&L: ${total_pnl:.2f} | Peak: ${peak:.2f}"
        )
        
    return True

def run_bot():
    """Main execution loop for the $6K Funded Account XAUUSD M5 BB Recovery Ladder Bot."""
    global last_close_time, last_processed_candle_time
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        return
        
    logger.info("=" * 60)
    logger.info(f"🚀 Started $6K Funded Account {SYMBOL} 4-Stage Recovery Ladder Bot")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Authorized Order Type: {AUTHORIZED_ORDER_TYPE}")
    logger.info(f"Trade Volume: {TRADE_VOLUME} lots (Strictly 1 trade at a time)")
    logger.info(f"Hard Stop Loss per trade: -${MAX_LOSS_USD:.2f} ({SL_POINTS} points / ~$2.00 move)")
    logger.info(f"Level 1: BE at +${BE_ACTIVATION_USD:.2f} (+${MIN_LOCKED_PROFIT_USD:.2f} floor), Trailing at +${TRAIL_ACTIVATION_USD:.2f} (-${TRAIL_PULLBACK_USD:.2f} drop)")
    logger.info(f"Recovery Levels 2 to {MAX_RECOVERY_LEVEL}: TP at Anchor Price (Entry of Level 1)")
    logger.info(f"Max Sequence Risk: 4 trades × -${MAX_LOSS_USD:.2f} = -${MAX_RECOVERY_LEVEL * MAX_LOSS_USD:.2f} (2% of account)")
    logger.info("=" * 60)
    
    try:
        while True:
            # 1. Manage Active Trade & Recovery Sequence
            has_active_trade = manage_active_trade(SYMBOL)
            if has_active_trade:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 2. Check Cooldown after Sequence Close (only when no recovery is active)
            time_since_last_close = time.time() - last_close_time
            if time_since_last_close < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 3. Check Bollinger Bands Entry Signal on Candle Shift 1
            signal, candle_time = check_bb_entry_signal(SYMBOL, TIMEFRAME)
            
            if signal and (last_processed_candle_time != candle_time):
                logger.info(f"📊 M5 BB Entry Signal [{candle_time}]: {signal}! Placing Level 1 ({TRADE_VOLUME} lots)...")
                res, fill_price = open_initial_trade(SYMBOL, signal)
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

