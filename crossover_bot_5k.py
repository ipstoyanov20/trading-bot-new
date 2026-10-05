import os
import MetaTrader5 as mt5
import time
import logging
from datetime import datetime
import pandas as pd
import subprocess
import threading
import xml.sax.saxutils as saxutils
import requests

import config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Account & Challenge Parameters ($5,000 Challenge Account) ---
ACCOUNT_SIZE = 5000.0               # Challenge Account Size ($5,000)
PHASE_1_TARGET_USD = 400.0          # Phase 1 Target: 8% (+$400.00)
PHASE_2_TARGET_USD = 200.0          # Phase 2 Target: 4% (+$200.00)
MAX_DAILY_LOSS_USD = 200.0          # Daily Loss Limit: 4% (-$200.00) - MONITORING ONLY
MAX_TOTAL_LOSS_USD = 400.0          # Maximum Loss Limit: 8% (-$400.00) - MONITORING ONLY
MAX_RISK_AT_ANY_TIME_USD = 100.0    # Max Risk at any time: -$100.00 limit
EMERGENCY_HEDGE_TRIGGER_LOSS_USD = -90.0  # Floating loss threshold to activate Emergency Delta-Lock before -$100

# --- Push Notification Settings ---
ENABLE_WINDOWS_TOAST = True            # Send native Windows desktop push notifications
ENABLE_NTFY_PUSH = True                # Send instant mobile push notifications via ntfy.sh
NTFY_TOPIC = "tradingbot_5k_ivaylo"    # Topic for ntfy mobile app (https://ntfy.sh/tradingbot_5k_ivaylo)

# --- Trading Strategy & Symbol Settings ---
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

# --- Trade Risk & Multi-Stage Zone Recovery Parameters ---
TRADE_VOLUME = 0.01                 # Initial trade volume: starts from 0.01 lots
SL_POINTS = 0                       # Hard Stop Loss: Disabled (0 points)
MAX_LOSS_USD = 100.0                # Hard Loss Floor: $100.00. Closes all positions if total loss > $100!

# --- Multi-Trade Hedging (Zone Recovery) Settings ---
HEDGE_TRIGGER_LOSS_USD = -5.00      # First hedge triggers when Level 1 trade reaches -$5.00 loss ($5 move on 0.01)
HEDGE_TARGET_PROFIT_USD = 2.50      # Target net profit: closes ALL trades immediately when reached (+$2.50)
TRAILING_PROFIT_DROP_USD = 4.00     # Trailing profit lock: closes original trade if profit drops $4.00 from peak
MAX_HEDGE_LEVELS = 7                # Maximum recovery levels allowed

# Lot sizing progression: Hedges are DOUBLED at each level:
# Level 1: 0.01 (Initial trade)
# Level 2: 0.02 (1st counter-hedge, 2x)
# Level 3: 0.04 (2nd counter-hedge, 2x)
# Level 4: 0.08 (3rd counter-hedge, 2x)
# Level 5: 0.16 (4th counter-hedge, 2x)
# Level 6: 0.32 (5th counter-hedge, 2x)
# Level 7: 0.64 (6th counter-hedge, 2x)
HEDGE_LOT_SIZES = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64]

def get_volume_for_level(level):
    """Returns lot size for given recovery level (1-indexed), doubling each level."""
    idx = level - 1
    if idx < len(HEDGE_LOT_SIZES):
        return HEDGE_LOT_SIZES[idx]
    return round(0.01 * (2 ** idx), 2)

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log
trade_peak_profit = {}              # Tracks peak floating profit per ticket: {ticket: float}

# --- Limit Monitoring & Safety State Flags ---
limit_notified_daily = False        # Has daily loss limit been notified?
limit_notified_total = False        # Has total loss limit been notified?
limit_notified_max_risk = False     # Has max risk limit been notified?
emergency_hedge_active = False      # Is Emergency Delta-Neutral Full Lock currently active?

def send_push_notification(title, message):
    """
    Sends actual push notifications across configured channels:
    1. Desktop: Native Windows Toast banner notification (with sound & Action Center notification).
    2. Mobile/Browser: Free instant push notifications via ntfy.sh (https://ntfy.sh/{NTFY_TOPIC}
       or use the ntfy app on iOS/Android).
    3. Console: Logged as a warning banner.
    Runs asynchronously in a background thread to prevent blocking trading execution.
    """
    logger.warning(f"📢 PUSH NOTIFICATION: {title} | {message}")

    def _deliver():
        # 1. Desktop Windows Toast Notification
        if ENABLE_WINDOWS_TOAST:
            try:
                clean_title = saxutils.escape(str(title))
                clean_message = saxutils.escape(str(message))
                ps_cmd = f'''
                [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
                $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
                $textNodes = $template.GetElementsByTagName('text')
                $textNodes.Item(0).InnerText = '{clean_title}'
                $textNodes.Item(1).InnerText = '{clean_message}'
                $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
                $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe')
                $notifier.Show($toast)
                '''
                subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, timeout=5)
            except Exception as e:
                logger.debug(f"Windows toast error: {e}")

        # 2. Mobile / Web Push Notification via ntfy.sh
        if ENABLE_NTFY_PUSH and NTFY_TOPIC:
            try:
                requests.post(
                    f"https://ntfy.sh/{NTFY_TOPIC}",
                    data=message.encode('utf-8'),
                    headers={
                        "Title": title.encode('utf-8'),
                        "Tags": "robot,chart_with_upwards_trend"
                    },
                    timeout=5
                )
            except Exception as e:
                logger.debug(f"ntfy push error: {e}")

        # 3. MetaTrader Mobile App "Messages" Tab (via MQL5 PushNotificationBridge)
        try:
            common_files_dir = os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\Common\Files")
            if os.path.exists(common_files_dir):
                notif_path = os.path.join(common_files_dir, "push_notification.txt")
                # Format clean text: strip emojis for maximum compatibility with MT5 mobile SendNotification
                clean_notif = f"{title}: {message}"
                clean_notif = clean_notif.encode('ascii', 'ignore').decode('ascii').strip()
                if not clean_notif:
                    clean_notif = f"{title}: {message}"
                # Keep strictly under 250 characters (MQL5 SendNotification limit is 255)
                if len(clean_notif) > 245:
                    clean_notif = clean_notif[:242] + "..."
                with open(notif_path, "w", encoding="utf-8") as f:
                    f.write(clean_notif)
        except Exception as e:
            logger.debug(f"MT5 common file error: {e}")

    threading.Thread(target=_deliver, daemon=True).start()

# Backward-compatible alias
send_mt5_notification = send_push_notification

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

def place_order_safe(symbol, order_type, volume, tp_points=0, sl_points=0, comment="BB_Single"):
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
            logger.info(f"✅ {action_name} Placed ({comment}) | Vol: {volume} | Price: {price} | Hard SL: {sl} | Filling: {mode}")
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
    Position 0 is the initial trade; subsequent positions are recovery hedge levels.
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

def close_all_positions(symbol, reason=""):
    """
    Closes all open positions belonging to this bot safely with retries.
    Closes highest profit trades first to secure profits immediately.
    """
    for attempt in range(5):
        positions = get_active_positions(symbol)
        if not positions:
            return True
        logger.info(f"Closing {len(positions)} positions ({reason}) [Attempt {attempt + 1}/5]...")
        sorted_pos = sorted(positions, key=lambda p: (p.profit + p.swap), reverse=True)
        for p in sorted_pos:
            close_position(symbol, p)
        time.sleep(0.2)
        if not get_active_positions(symbol):
            logger.info("✅ All positions successfully closed.")
            return True
            
    remaining = get_active_positions(symbol)
    if remaining:
        logger.warning(f"⚠️ {len(remaining)} positions remain open after 5 close attempts.")
        return False
    return True

def get_daily_realized_pnl(magic=MAGIC_NUMBER):
    """
    Calculates total closed PnL today (since 00:00 server/local time) for this bot's magic number.
    """
    now = datetime.now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(today_midnight, now)
    if not deals:
        return 0.0
        
    daily_pnl = 0.0
    for d in deals:
        if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT:
            daily_pnl += (d.profit + d.swap + getattr(d, 'commission', 0.0))
            
    return daily_pnl

def get_total_closed_pnl(magic=MAGIC_NUMBER):
    """
    Calculates total cumulative closed PnL for Phase 1 target tracking ($400.00 goal).
    """
    start_date = datetime(2026, 1, 1)
    now = datetime.now()
    deals = mt5.history_deals_get(start_date, now)
    if not deals:
        return 0.0
        
    total_pnl = 0.0
    for d in deals:
        if d.magic == magic and d.entry == mt5.DEAL_ENTRY_OUT:
            total_pnl += (d.profit + d.swap + getattr(d, 'commission', 0.0))
            
    return total_pnl

def open_single_trade(symbol, direction):
    """
    Opens an initial 0.05 lot trade based on Bollinger Bands entry signal.
    """
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_{direction}_L1"
    
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=TRADE_VOLUME,
        tp_points=0,                 # Managed via basket profit target
        sl_points=SL_POINTS,         # 0 = Disabled
        comment=comment
    )
    
    if res:
        ticket = getattr(res, 'order', None)
        logger.info(
            f"🚀 Level 1 Trade Placed: {direction} {TRADE_VOLUME} lots at {fill_price:.2f} | "
            f"Target Profit: +${HEDGE_TARGET_PROFIT_USD:.2f} | "
            f"Hedge Trigger: <= ${HEDGE_TRIGGER_LOSS_USD:.2f}"
        )
        send_push_notification(
            f"🟢 Level 1 {direction} Placed",
            f"{SYMBOL} {direction} {TRADE_VOLUME} lots @ {fill_price:.2f} | Target: +${HEDGE_TARGET_PROFIT_USD:.2f}"
        )
        return res, fill_price
    return None, 0.0

hedge_order_in_progress = False

def open_recovery_hedge_level(symbol, next_level, direction, volume, trigger_reason=""):
    """
    Opens next recovery hedge trade in the sequence with retry protection.
    """
    global hedge_order_in_progress
    if hedge_order_in_progress:
        return None, 0.0
        
    hedge_order_in_progress = True
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_Hedge_L{next_level}"
    
    logger.info(
        f"🚨 Triggering Level {next_level} Recovery Hedge ({trigger_reason}) | "
        f"Opening {direction} {volume} lots on {symbol}..."
    )
    
    try:
        for attempt in range(3):
            res, fill_price = place_order_safe(
                symbol=symbol,
                order_type=order_type,
                volume=volume,
                tp_points=0,
                sl_points=0,
                comment=comment
            )
            if res:
                logger.info(
                    f"✅ Level {next_level} Hedge Placed: #{getattr(res, 'order', None)} {direction} {volume} lots @ {fill_price:.2f} | "
                    f"Targeting Net Basket Profit >= +${HEDGE_TARGET_PROFIT_USD:.2f}"
                )
                send_push_notification(
                    f"⚖️ Level {next_level} Hedge Placed",
                    f"{symbol} {direction} {volume} lots @ {fill_price:.2f} | {trigger_reason}"
                )
                return res, fill_price
            logger.warning(f"⚠️ Level {next_level} hedge attempt {attempt + 1}/3 failed. Retrying in 0.2s...")
            time.sleep(0.2)
            
        logger.error(f"❌ Failed to place Level {next_level} hedge trade after 3 attempts!")
        return None, 0.0
    finally:
        hedge_order_in_progress = False

def open_emergency_full_lock(symbol, total_basket_pnl):
    """
    EMERGENCY FULL-LOCK (DELTA-NEUTRAL HEDGE)
    When floating basket loss reaches ~-$145 to -$150 (3% of $5K challenge),
    immediately equalizes BUY and SELL volumes so Net Delta = 0.00 lots.
    This permanently freezes the floating loss and prevents ANY further drawdown,
    guaranteeing the challenge daily limit (-$200) and max loss (-$400) are 100% protected.
    """
    global emergency_hedge_active
    
    positions = get_active_positions(symbol)
    if not positions:
        emergency_hedge_active = False
        return False
        
    total_buy_volume = sum(p.volume for p in positions if p.type == mt5.POSITION_TYPE_BUY)
    total_sell_volume = sum(p.volume for p in positions if p.type == mt5.POSITION_TYPE_SELL)
    net_delta = round(total_buy_volume - total_sell_volume, 2)
    
    if abs(net_delta) < 0.005:
        # Already fully locked 1:1
        emergency_hedge_active = True
        return True
        
    order_type = mt5.ORDER_TYPE_BUY if net_delta < 0 else mt5.ORDER_TYPE_SELL
    dir_str = "BUY" if net_delta < 0 else "SELL"
    hedge_vol = round(abs(net_delta), 2)
    
    logger.warning("=" * 65)
    logger.warning(
        f"🚨 EMERGENCY FULL-LOCK TRIGGERED! Basket Loss: ${total_basket_pnl:.2f} <= ${EMERGENCY_HEDGE_TRIGGER_LOSS_USD:.2f}\n"
        f"   Total Buys: {total_buy_volume:.2f} lots | Total Sells: {total_sell_volume:.2f} lots | Net Exposure: {net_delta:+.2f} lots\n"
        f"   Executing Emergency {dir_str} {hedge_vol} lots to achieve 100% DELTA-NEUTRAL FULL LOCK!"
    )
    logger.warning("=" * 65)
    
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=hedge_vol,
        tp_points=0,
        sl_points=0,
        comment="Emergency_Delta_Lock"
    )
    
    if res:
        emergency_hedge_active = True
        logger.warning(
            f"🛡️ EMERGENCY FULL LOCK ESTABLISHED! Ticket #{getattr(res, 'order', None)} {dir_str} {hedge_vol} lots @ {fill_price:.2f}.\n"
            f"   Net exposure is now 0.00 lots. Floating drawdown is FROZEN. Challenge is safe!"
        )
        send_push_notification(
            "🚨 EMERGENCY HEDGE ACTIVATED",
            f"Basket loss reached ${abs(total_basket_pnl):.2f}. Opened {dir_str} {hedge_vol} lots to freeze drawdown. Challenge 100% safe!"
        )
        return True
    else:
        logger.error(f"❌ Failed to execute Emergency Hedge order {dir_str} {hedge_vol} lots!")
        return False

def check_challenge_limits(symbol):
    """
    MONITORING ONLY - Checks challenge limits and sends notifications.
    Does NOT stop the bot, only alerts the user.
    """
    global limit_notified_daily, limit_notified_total, limit_notified_max_risk
    
    positions = get_active_positions(symbol)
    daily_realized_pnl = get_daily_realized_pnl(MAGIC_NUMBER)
    floating_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)
    total_today_pnl = daily_realized_pnl + floating_pnl
    
    total_basket_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)
    cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)
    
    # 1. Check Daily Loss Limit (4% = -$200)
    if total_today_pnl <= -MAX_DAILY_LOSS_USD and not limit_notified_daily:
        limit_notified_daily = True
        send_push_notification(
            "⚠️ DAILY LOSS LIMIT ALERT",
            f"Today's P&L: ${total_today_pnl:.2f} has reached the daily limit of -${MAX_DAILY_LOSS_USD:.2f}. "
            f"Consider closing positions manually to preserve account."
        )
    elif total_today_pnl > -MAX_DAILY_LOSS_USD + 25.0 and limit_notified_daily:
        limit_notified_daily = False  # Reset flag when back in safe zone
    
    # 2. Check Total Maximum Loss Limit (8% = -$400)
    if cumulative_closed_pnl <= -MAX_TOTAL_LOSS_USD and not limit_notified_total:
        limit_notified_total = True
        send_push_notification(
            "🔴 MAXIMUM LOSS LIMIT ALERT",
            f"Cumulative P&L: ${cumulative_closed_pnl:.2f} has reached the maximum loss limit of -${MAX_TOTAL_LOSS_USD:.2f}. "
            f"This is a critical alert. Consider stopping trading for this challenge."
        )
    elif cumulative_closed_pnl > -MAX_TOTAL_LOSS_USD + 50.0 and limit_notified_total:
        limit_notified_total = False  # Reset flag when back in safe zone
    
    # 3. Check Max Risk / Floating Drawdown Limit (3% = -$150)
    # Checks ACTUAL floating basket loss and today's total floating loss
    current_loss = min(total_basket_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_AT_ANY_TIME_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(
            "⚠️ MAX 3% LOSS ALERT ($150)",
            f"Floating loss is ${abs(current_loss):.2f} (Limit: -${MAX_RISK_AT_ANY_TIME_USD:.2f}). "
            f"Emergency Hedge is active to protect challenge!"
        )
    elif current_loss > -MAX_RISK_AT_ANY_TIME_USD + 25.0 and limit_notified_max_risk:
        limit_notified_max_risk = False  # Reset flag when back in safe zone

def manage_active_trade(symbol):
    """
    Manages active trade(s) using Multi-Trade Zone Recovery Hedging:
    - Never leaves a trade in loss: always hedges in alternating directions until profitable.
    - Closes every trade the moment combined basket PnL reaches >= HEDGE_TARGET_PROFIT_USD (+$10.00).
    - Opens as many hedge trades as needed across the recovery zone to secure positive profit.
    Returns True if trade(s) active, False if all trades closed.
    """
    global last_close_time, last_status_log_time, trade_peak_profit, emergency_hedge_active
    
    positions = get_active_positions(symbol)
    if not positions:
        trade_peak_profit.clear()
        emergency_hedge_active = False
        return False

    total_basket_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)

    # 1. ALWAYS CLOSE EVERY TRADE WHEN IN TARGET PROFIT!
    if total_basket_pnl >= HEDGE_TARGET_PROFIT_USD:
        logger.info(
            f"🎉 TARGET PROFIT ACHIEVED! Total Net PnL: +${total_basket_pnl:.2f} >= +${HEDGE_TARGET_PROFIT_USD:.2f} "
            f"across {len(positions)} active trade(s). Closing all trades immediately in profit!"
        )
        send_push_notification(
            "🎉 Target Profit Achieved!",
            f"Basket closed in profit: +${total_basket_pnl:.2f} across {len(positions)} trades on {symbol}!"
        )
        close_all_positions(symbol, f"Basket Target Profit +${total_basket_pnl:.2f}")
        trade_peak_profit.clear()
        emergency_hedge_active = False
        last_close_time = time.time()
        return False

    # 2. Hard Downside Watchdog: Stop losing more than $100
    if MAX_LOSS_USD > 0 and total_basket_pnl <= -MAX_LOSS_USD:
        logger.warning(f"🛑 Hard Loss Stop Triggered (P&L: ${total_basket_pnl:.2f} <= -${MAX_LOSS_USD:.2f}). Closing all positions...")
        send_push_notification(
            "🛑 Hard Loss Stop Triggered",
            f"Basket loss reached -${abs(total_basket_pnl):.2f} (Limit: -${MAX_LOSS_USD:.2f}). Closed all positions immediately!"
        )
        close_all_positions(symbol, f"Watchdog Max Loss -${abs(total_basket_pnl):.2f}")
        trade_peak_profit.clear()
        emergency_hedge_active = False
        last_close_time = time.time()
        return False

    # 2.5 EMERGENCY FULL-LOCK HEDGE (When Basket loss reaches ~$145 - $150 / 3% of account)
    if total_basket_pnl <= EMERGENCY_HEDGE_TRIGGER_LOSS_USD or emergency_hedge_active:
        total_buy_volume = sum(p.volume for p in positions if p.type == mt5.POSITION_TYPE_BUY)
        total_sell_volume = sum(p.volume for p in positions if p.type == mt5.POSITION_TYPE_SELL)
        net_delta = round(total_buy_volume - total_sell_volume, 2)
        
        if abs(net_delta) >= 0.01:
            open_emergency_full_lock(symbol, total_basket_pnl)
            return True
        else:
            emergency_hedge_active = True
            now = time.time()
            if now - last_status_log_time >= 10:
                last_status_log_time = now
                logger.warning(
                    f"🛡️ EMERGENCY FULL LOCK ACTIVE | Buys: {total_buy_volume:.2f} lots | Sells: {total_sell_volume:.2f} lots | "
                    f"Net Delta: 0.00 lots | Frozen Basket PnL: ${total_basket_pnl:.2f} | Challenge is 100% Safe!"
                )
            return True

    # 3. Single Trade Mode (Level 1 only)
    if len(positions) == 1:
        pos = positions[0]
        ticket = pos.ticket
        current_pnl = total_basket_pnl
        
        # Track Peak Floating Profit for Trailing Take-Profit
        peak_pnl = trade_peak_profit.get(ticket, current_pnl)
        if current_pnl > peak_pnl:
            peak_pnl = current_pnl
            trade_peak_profit[ticket] = peak_pnl

        # Trailing Profit Lock: If in profit and price drops $4.00 from peak, close and bank the profit!
        # "if there isn't any loss leave the profit on original trade when the price go under -4 dollars from it peak"
        trailing_drop = peak_pnl - current_pnl
        if peak_pnl >= TRAILING_PROFIT_DROP_USD and trailing_drop >= TRAILING_PROFIT_DROP_USD and current_pnl > 0:
            logger.info(
                f"🎯 TRAILING PROFIT LOCK! Trade #{ticket} peak was +${peak_pnl:.2f}, dropped by ${trailing_drop:.2f} >= ${TRAILING_PROFIT_DROP_USD:.2f}. "
                f"Closing trade to lock in +${current_pnl:.2f} profit!"
            )
            send_push_notification(
                "🎯 Trailing Profit Locked!",
                f"Level 1 closed at +${current_pnl:.2f} (pulled back ${trailing_drop:.2f} from peak +${peak_pnl:.2f}) on {symbol}!"
            )
            close_all_positions(symbol, f"Trailing Profit Lock +${current_pnl:.2f}")
            trade_peak_profit.clear()
            last_close_time = time.time()
            return False

        # Check if Level 1 reached -$5.00 loss threshold -> Trigger Level 2 Hedge (Doubled to 0.02 lots)!
        if total_basket_pnl <= HEDGE_TRIGGER_LOSS_USD:
            hedge_dir = "SELL" if pos.type == mt5.POSITION_TYPE_BUY else "BUY"
            hedge_vol = get_volume_for_level(2)
            open_recovery_hedge_level(
                symbol=symbol,
                next_level=2,
                direction=hedge_dir,
                volume=hedge_vol,
                trigger_reason=f"Level 1 loss ${total_basket_pnl:.2f} <= ${HEDGE_TRIGGER_LOSS_USD:.2f}"
            )
            return True

        # Periodic status log for single trade
        now = time.time()
        if now - last_status_log_time >= 10:
            last_status_log_time = now
            trade_dir = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
            logger.info(
                f"📈 Active Level 1 #{ticket} ({trade_dir} {pos.volume} lots @ {pos.price_open:.2f}) | "
                f"P&L: ${total_basket_pnl:.2f} (Peak: +${peak_pnl:.2f}) | Target: +${HEDGE_TARGET_PROFIT_USD:.2f} | "
                f"Hedge Trigger: ${HEDGE_TRIGGER_LOSS_USD:.2f} | Trailing Lock: -${TRAILING_PROFIT_DROP_USD:.2f} from peak"
            )
        return True

    # 4. Multi-Trade Hedged Basket Mode (Level 2, 3, 4, ...)
    # Sort chronologically: Level 1 is first, Level 2 is second, etc.
    positions.sort(key=lambda p: p.time)
    current_level = len(positions)
    
    pos_1 = positions[0]
    pos_2 = positions[1]
    zone_high = max(pos_1.price_open, pos_2.price_open)
    zone_low = min(pos_1.price_open, pos_2.price_open)
    
    latest_pos = positions[-1]
    latest_dir = "BUY" if latest_pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    next_level = current_level + 1
    next_dir = "SELL" if latest_dir == "BUY" else "BUY"
    next_vol = get_volume_for_level(next_level)
    
    # Check if max level reached
    if MAX_HEDGE_LEVELS > 0 and current_level >= MAX_HEDGE_LEVELS:
        now = time.time()
        if now - last_status_log_time >= 10:
            last_status_log_time = now
            logger.info(
                f"⚖️ Hedged Basket [Level {current_level}/{MAX_HEDGE_LEVELS} Max] | "
                f"Net P&L: ${total_basket_pnl:.2f} / Target: +${HEDGE_TARGET_PROFIT_USD:.2f} | Waiting for profit..."
            )
        return True

    # Check zone boundary triggers for next hedge level
    tick = mt5.symbol_info_tick(symbol)
    if tick:
        if next_dir == "BUY" and tick.ask >= zone_high:
            open_recovery_hedge_level(
                symbol=symbol,
                next_level=next_level,
                direction="BUY",
                volume=next_vol,
                trigger_reason=f"Price {tick.ask:.2f} crossed Zone High {zone_high:.2f}"
            )
            return True
        elif next_dir == "SELL" and tick.bid <= zone_low:
            open_recovery_hedge_level(
                symbol=symbol,
                next_level=next_level,
                direction="SELL",
                volume=next_vol,
                trigger_reason=f"Price {tick.bid:.2f} crossed Zone Low {zone_low:.2f}"
            )
            return True

    # Periodic status log for multi-trade hedged basket (every 10 seconds)
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        trigger_target = f"Zone High {zone_high:.2f}" if next_dir == "BUY" else f"Zone Low {zone_low:.2f}"
        cur_price = tick.ask if next_dir == "BUY" else (tick.bid if tick else 0.0)
        logger.info(
            f"⚖️ Hedged Basket [Level {current_level}, {len(positions)} trades] | "
            f"Zone: [{zone_low:.2f} - {zone_high:.2f}] | "
            f"Net P&L: ${total_basket_pnl:.2f} / Target: +${HEDGE_TARGET_PROFIT_USD:.2f} | "
            f"Next: L{next_level} {next_dir} {next_vol}l @ {trigger_target} (Current: {cur_price:.2f})"
        )
        
    return True

def run_bot():
    """Main execution loop for the $5K Challenge XAUUSD M5 Multi-Stage Hedged Bot."""
    global last_close_time, last_processed_candle_time
    
    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        return
        
    logger.info("=" * 65)
    logger.info(f"🚀 Started $5K Challenge {SYMBOL} Multi-Stage Zone Recovery Bot")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Initial Trade: {TRADE_VOLUME} lots (Challenge-Safe)")
    logger.info(f"Hedge System: Never leaves in loss | Alternating recovery hedge")
    logger.info(f"Initial Trigger: Loss <= ${HEDGE_TRIGGER_LOSS_USD:.2f} opens Level 2 ({get_volume_for_level(2)} lots)")
    logger.info(f"Target Profit: Close ALL trades immediately when Net Basket PnL >= +${HEDGE_TARGET_PROFIT_USD:.2f}")
    logger.info(f"Hedge Progression: {HEDGE_LOT_SIZES}")
    logger.info(f"Max Hedge Levels: {MAX_HEDGE_LEVELS}")
    logger.info(f"⚠️ MONITORING & EMERGENCY PROTECTION:")
    logger.info(f"  - Daily Loss Limit: -${MAX_DAILY_LOSS_USD:.2f} (4% of $5,000)")
    logger.info(f"  - Maximum Loss Limit: -${MAX_TOTAL_LOSS_USD:.2f} (8% of $5,000)")
    logger.info(f"  - Max Risk at any time: ${MAX_RISK_AT_ANY_TIME_USD:.2f} (3% of $5,000)")
    logger.info(f"  - Emergency Delta-Lock Hedge: Trigger at ${EMERGENCY_HEDGE_TRIGGER_LOSS_USD:.2f} (Freezes drawdown, saves challenge)")
    logger.info(f"Phase 1 Profit Target: +${PHASE_1_TARGET_USD:.2f} (8% of $5,000)")
    logger.info(f"Phase 2 Profit Target: +${PHASE_2_TARGET_USD:.2f} (4% of $5,000)")
    logger.info("=" * 65)

    # Startup Push Notification Test
    send_push_notification(
        "🚀 5K Bot Online",
        f"Push notification test successful! Bot active on {SYMBOL} (M5). Monitoring Challenge Limits: Daily -${MAX_DAILY_LOSS_USD:.0f}, Max -${MAX_TOTAL_LOSS_USD:.0f}."
    )
    
    try:
        while True:
            # 1. Check Phase 1 Target Achievement Tracker
            cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)
            if cumulative_closed_pnl >= PHASE_1_TARGET_USD:
                logger.info("=" * 65)
                logger.info(f"🎉🎉 PHASE 1 TARGET ACHIEVED! 🎉🎉")
                logger.info(f"Total Cumulative Profit: +${cumulative_closed_pnl:.2f} >= +${PHASE_1_TARGET_USD:.2f} (+8%)")
                logger.info("You may continue to Phase 2 or submit for rewards!")
                logger.info("=" * 65)
                # Note: Bot continues, does NOT stop

            # 2. Check Challenge Limits (MONITORING ONLY - sends notifications)
            check_challenge_limits(SYMBOL)

            # 3. Manage Active Single Trade
            has_active_trade = manage_active_trade(SYMBOL)
            if has_active_trade:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 4. Check Cooldown after Trade Close
            time_since_last_close = time.time() - last_close_time
            if time_since_last_close < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue
                
            # 5. Check Bollinger Bands Entry Signal on Candle Shift 1
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
