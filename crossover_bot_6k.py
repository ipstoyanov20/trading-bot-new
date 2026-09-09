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

# --- Trade & Trailing Profit Parameters (Option A: 6K Funded Balanced Setup) ---
TRADE_VOLUME = 0.15                  # Volume: 0.15 lots ($1 move in Gold = $15.00)
SL_POINTS = 500                      # Hard Stop Loss: 500 points ($5.00 move = -$75.00 max risk)
TRAIL_ACTIVATION_USD = 30.0          # Minimum profit in USD to activate trailing lock (+~$2.00 move)
TRAIL_PULLBACK_USD = 5.0             # Pullback/drop in USD from peak profit to trigger exit
MIN_LOCKED_PROFIT_USD = 25.0         # Minimum guaranteed locked profit floor when trailing triggers
# NOTE: Recovery zone hedging and all hedging are removed. Strictly 1 trade at a time.

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log
trade_peak_profit = {}              # Tracks peak floating profit per ticket: {ticket: float}

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

def place_order_safe(symbol, order_type, volume, tp_points=0, sl_points=0, comment="BB_SingleTrade"):
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

def open_single_trade(symbol, direction):
    """
    Opens a single trade (TRADE_VOLUME lots) with hard stop loss (SL_POINTS).
    """
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_{direction}_Trailing"
    
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=TRADE_VOLUME,
        tp_points=0,
        sl_points=SL_POINTS,
        comment=comment
    )
    
    if res:
        logger.info(f"🚀 Single Trade Placed: {direction} {TRADE_VOLUME} lots at {fill_price:.2f} | Hard SL: {SL_POINTS} pts ($5.00 move / -$75.00 max risk) | Trailing Activation: +${TRAIL_ACTIVATION_USD:.2f} (Pullback: ${TRAIL_PULLBACK_USD:.2f})")
        return res, fill_price
    return None, 0.0

def manage_active_trade(symbol):
    """
    Manages the single active trade with Trailing Profit Lock and Hard Stop Loss:
    - Strictly only 1 trade runs at a time.
    - Protected by hard stop loss placed with broker order.
    - Monitors real-time net profit (profit + swap + commission).
    - If profit goes UP, trade stays OPEN (letting profit run).
    - Tracks peak profit achieved.
    - Once profit reaches at least TRAIL_ACTIVATION_USD ($30.00), trailing is ARMED.
    - If price makes a pullback down (drop_from_peak >= TRAIL_PULLBACK_USD ($5.00))
      and profit >= MIN_LOCKED_PROFIT_USD ($25.00), it closes the trade immediately.
    Returns True if a trade is currently open, False if flat.
    """
    global last_close_time, last_status_log_time, trade_peak_profit
    
    positions = get_active_positions(symbol)
    if not positions:
        if trade_peak_profit:
            logger.info("ℹ️ Active position was closed (SL hit or external exit). Resetting bot state.")
            last_close_time = time.time()
            trade_peak_profit.clear()
        return False
        
    position = positions[0]
    ticket = position.ticket
    total_pnl = position.profit + position.swap + getattr(position, 'commission', 0.0)
    
    # Track and update peak profit
    if ticket not in trade_peak_profit:
        trade_peak_profit[ticket] = total_pnl
    else:
        if total_pnl > trade_peak_profit[ticket]:
            old_peak = trade_peak_profit[ticket]
            trade_peak_profit[ticket] = total_pnl
            # Log new high peak if significant or above activation
            if total_pnl >= TRAIL_ACTIVATION_USD and (total_pnl - old_peak >= 2.0):
                logger.info(f"🔥 New Peak Profit for #{ticket}: +${total_pnl:.2f} (Trailing Active, letting profit run)")

    peak = trade_peak_profit[ticket]
    
    # Trailing Profit Exit Condition:
    # 1. Peak reached at least the activation threshold
    # 2. Reversal / pullback drop from peak >= TRAIL_PULLBACK_USD
    # 3. Current net profit >= minimum locked profit floor
    if peak >= TRAIL_ACTIVATION_USD:
        drop_from_peak = peak - total_pnl
        if drop_from_peak >= TRAIL_PULLBACK_USD and total_pnl >= MIN_LOCKED_PROFIT_USD:
            logger.info(
                f"💰 Trailing Profit Lock Triggered for #{ticket}! "
                f"Peak: +${peak:.2f} | Current: +${total_pnl:.2f} | Drop from Peak: -${drop_from_peak:.2f} >= ${TRAIL_PULLBACK_USD:.2f}. "
                f"Closing position to secure profit..."
            )
            if close_position(symbol, position):
                last_close_time = time.time()
                trade_peak_profit.pop(ticket, None)
                logger.info(f"✅ Position #{ticket} closed successfully with +${total_pnl:.2f} profit locked in!")
                return False

    # Periodic status log (every 10 seconds)
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        trade_dir = "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"
        status_trailing = "ARMED" if peak >= TRAIL_ACTIVATION_USD else f"Waiting for +${TRAIL_ACTIVATION_USD:.2f}"
        sl_str = f"{position.sl:.2f}" if position.sl > 0 else "None"
        logger.info(
            f"📈 Active #{ticket} ({trade_dir} {position.volume} lots @ {position.price_open:.2f} | SL: {sl_str}) | "
            f"P&L: ${total_pnl:.2f} | Peak: ${peak:.2f} | Trailing: {status_trailing}"
        )
        
    return True

def run_bot():
    """Main execution loop for the $6K Funded Account XAUUSD M5 BB Single Trade Bot (Option A: Balanced & Protected)."""
    global last_close_time, last_processed_candle_time
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        return
        
    logger.info("=" * 60)
    logger.info(f"🚀 Started $6K Funded Account {SYMBOL} Single Trade Bot (Option A: Balanced)")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Authorized Order Type: {AUTHORIZED_ORDER_TYPE}")
    logger.info(f"Trade Volume: {TRADE_VOLUME} lots (1 trade at a time)")
    logger.info(f"Hard Stop Loss: {SL_POINTS} points ($5.00 move | -$75.00 max risk per trade)")
    logger.info(f"Risk Management: ~1.25% risk per trade (Well below firm 3.0% limit of $180)")
    logger.info(f"Trailing Activation: +${TRAIL_ACTIVATION_USD:.2f} (Let profits run)")
    logger.info(f"Trailing Pullback Exit: -${TRAIL_PULLBACK_USD:.2f} from peak")
    logger.info(f"Minimum Locked Floor: +${MIN_LOCKED_PROFIT_USD:.2f}")
    logger.info("=" * 60)
    
    try:
        while True:
            # 1. Manage Active Trade (if any) - blocks new entries while a trade is active
            has_active_trade = manage_active_trade(SYMBOL)
            if has_active_trade:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 2. Check Cooldown after Trade Close
            time_since_last_close = time.time() - last_close_time
            if time_since_last_close < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 3. Check Bollinger Bands Entry Signal on Candle Shift 1
            signal, candle_time = check_bb_entry_signal(SYMBOL, TIMEFRAME)
            
            if signal and (last_processed_candle_time != candle_time):
                logger.info(f"📊 M5 BB Entry Signal [{candle_time}]: {signal}! Placing trade ({TRADE_VOLUME} lots)...")
                res, fill_price = open_single_trade(SYMBOL, signal)
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
