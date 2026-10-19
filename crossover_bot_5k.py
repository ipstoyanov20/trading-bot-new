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
MAX_RISK_AT_ANY_TIME_USD = 150.0    # Max Risk at any time: -$150.00 limit - MONITORING ONLY

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

# --- Trade Risk & Multi-Stage Recovery Parameters ---
TRADE_VOLUME = 0.20                 # Initial (original) trade volume: 0.20 lots before any hedge
SL_POINTS = 0                       # Hard Stop Loss: Disabled (0 points)
MAX_LOSS_USD = 150.0                # Hard Loss Floor: closes all positions if total basket loss reaches -$150

# --- Exit Rules ---
# SINGLE TRADE ONLY (original 0.20 lot, no hedge open):
#   Trailing profit lock. Tracks the highest floating profit (peak). Once the peak has
#   reached at least TRAILING_PROFIT_DROP_USD, the trade is closed as soon as profit
#   falls TRAILING_PROFIT_DROP_USD below the peak (e.g. peak +$85 -> closes at +$65).
#   The +$2.50 basket target does NOT apply to the single trade.
TRAILING_PROFIT_DROP_USD = 20.00

# HEDGED BASKET ONLY (original 0.20 + first hedge 0.40 and beyond):
#   Closes ALL trades the moment the combined net P&L reaches this value.
HEDGE_TARGET_PROFIT_USD = 2.50

# --- Multi-Trade Hedging (Basket -$70 Recovery) Settings ---
# EVERY hedge opens when the TOTAL basket P&L (sum of all open trades) is at -$70:
#   Level 1 (0.20) alone reaches -$70            -> Level 2 hedge (0.40) opens
#   Level 1 + Level 2 together reach -$70        -> Level 3 hedge (0.80) opens
#   Level 1 + 2 + 3 together reach -$70          -> Level 4 hedge (1.60) opens ... and so on.
# Each hedge is opposite to the previous trade and double its size.
#
# Wait between hedges: after a hedge opens, the bot waits HEDGE_WAIT_SECONDS (10 minutes).
# After the wait, if the basket is STILL at -$70 or worse, the next hedge opens and the
# 10-minute wait starts again. This stops the bot from opening many hedges in one second.
HEDGE_TRIGGER_LOSS_USD = -70.00     # Basket P&L that opens every hedge
HEDGE_WAIT_SECONDS = 600            # 10 minutes between hedges

# --- -$150 Basket Loss Protection ---
# 1. Broker-side basket stop: the bot calculates the exact gold price at which the WHOLE
#    basket equals -$150 and puts SL/TP on EVERY open position at that price. The broker
#    then closes the basket even if the bot freezes, lags or loses connection.
# 2. Hedge room check: a new hedge is only opened if, after paying its spread, the basket
#    still has at least HEDGE_MIN_CUSHION_USD left before -$150 AND gold must move at least
#    HEDGE_MIN_ROOM_PRICE dollars to reach -$150. Bigger hedges that would push the basket
#    straight into the -$150 stop are refused.
# 3. If the broker closes some basket positions (SL/TP), the bot closes the rest at once.
# 4. Software hard stop (MAX_LOSS_USD) stays as the last line of defence.
BROKER_BASKET_STOP = True
HEDGE_MIN_CUSHION_USD = 30.00       # Min $ left between basket (after spread) and -$150 to allow a hedge
HEDGE_MIN_ROOM_PRICE = 0.80         # Min gold price move ($) from new basket to -$150 to allow a hedge
MAX_HEDGES = 20                     # Number of hedges allowed after the original trade
MAX_HEDGE_LEVELS = MAX_HEDGES + 1   # Total levels: original trade + hedges

# Lot sizing progression: each hedge is DOUBLE the previous trade:
# Level 1: 0.20 (Original trade)
# Level 2: 0.40 (1st hedge)
# Level 3: 0.80 (2nd hedge)
# Level 4: 1.60 (3rd hedge) ...
HEDGE_LOT_SIZES = [round(TRADE_VOLUME * (2 ** i), 2) for i in range(MAX_HEDGE_LEVELS)]

def get_volume_for_level(level):
    """Returns lot size for given recovery level (1-indexed), doubling each level."""
    idx = level - 1
    if idx < len(HEDGE_LOT_SIZES):
        return HEDGE_LOT_SIZES[idx]
    return round(TRADE_VOLUME * (2 ** idx), 2)


# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log
trade_peak_profit = {}              # Tracks peak floating profit per ticket: {ticket: float}
phase1_announced = False            # Prevents Phase 1 banner from logging every second
last_hedge_time = None              # time.time() of the last hedge opened (None = no hedge yet in this run)
basket_tickets = None               # Tickets of the basket seen on the previous loop (detects broker SL/TP closes)
broker_stop_state = None            # (tickets, stop_price) last sent to the broker
hedge_blocked_notified = None       # Level for which a "hedge blocked" alert was already sent

# --- Limit Monitoring & Safety State Flags ---
limit_notified_daily = False        # Has daily loss limit been notified?
limit_notified_total = False        # Has total loss limit been notified?
limit_notified_max_risk = False     # Has max risk limit been notified?

def send_push_notification(title, message):
    """
    Sends push notifications across configured channels:
    1. Desktop: Native Windows Toast notification.
    2. Mobile/Browser: ntfy.sh (https://ntfy.sh/{NTFY_TOPIC}).
    3. MT5 mobile app via PushNotificationBridge common file.
    Runs in a background thread to avoid blocking trading execution.
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
                clean_notif = f"{title}: {message}"
                clean_notif = clean_notif.encode('ascii', 'ignore').decode('ascii').strip()
                if not clean_notif:
                    clean_notif = f"{title}: {message}"
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
    """Dynamically determines the correct execution filling mode supported by the broker."""
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

def normalize_volume(symbol_info, volume):
    """Rounds volume to the broker's volume step and clamps it to min/max allowed lots."""
    step = getattr(symbol_info, 'volume_step', 0.01) or 0.01
    vmin = getattr(symbol_info, 'volume_min', 0.01) or 0.01
    vmax = getattr(symbol_info, 'volume_max', volume) or volume
    vol = round(round(volume / step) * step, 2)
    if vol > vmax:
        logger.warning(f"⚠️ Requested volume {volume} exceeds broker max {vmax}. Clamping to {vmax}.")
        vol = vmax
    if 0 < vol < vmin:
        vol = vmin
    return vol

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
    Returns: (signal, candle_time) where signal is 'BUY', 'SELL', or None.
    """
    df = get_bollinger_bands(symbol, timeframe, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    if df.empty or len(df) < BB_PERIOD + 2:
        return None, None

    # iloc[-1] = shift 0 (current, incomplete), iloc[-2] = shift 1 (last completed)
    candle_shift_1 = df.iloc[-2]
    candle_time = candle_shift_1['time']
    c_open = candle_shift_1['open']
    c_close = candle_shift_1['close']
    c_upper = candle_shift_1['upper']
    c_lower = candle_shift_1['lower']
    c_middle = candle_shift_1['middle']

    if pd.isna(c_upper) or pd.isna(c_lower):
        return None, None

    is_buy = bool((c_open < c_lower) and (c_close >= c_lower) and (c_close <= c_upper))
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

def place_order_safe(symbol, order_type, volume, tp_points=0, sl_points=0, comment="BB_Single", notify_on_error=True):
    """Places an order on MT5 with filling mode fallback and returns (result, fill_price)."""
    action_name = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info:
        err_msg = f"Symbol {symbol} not found."
        logger.error(err_msg)
        if notify_on_error:
            send_push_notification(
                f"❌ Trade Execution Failed ({action_name})",
                f"Failed to execute {action_name} {volume} lots on {symbol} ({comment}). Error: {err_msg}"
            )
        return None, 0.0

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    volume = normalize_volume(symbol_info, volume)

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        err_msg = f"Failed to get tick for {symbol}"
        logger.error(err_msg)
        if notify_on_error:
            send_push_notification(
                f"❌ Trade Execution Failed ({action_name})",
                f"Failed to execute {action_name} {volume} lots on {symbol} ({comment}). Error: {err_msg}"
            )
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
            logger.info(f"✅ {action_name} Placed ({comment}) | Vol: {volume} | Price: {price} | Hard SL: {sl} | Filling: {mode}")
            return res, price
        elif res and res.retcode in [10030, getattr(mt5, 'TRADE_RETCODE_UNSUPPORTED_FILLING_MODE', 10030)]:
            continue
        else:
            break

    err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
    logger.error(f"Failed to place order ({comment}): {err} | Request: {request}")
    if notify_on_error:
        send_push_notification(
            f"❌ Trade Execution Failed ({action_name})",
            f"Failed to execute {action_name} {volume} lots on {symbol} ({comment}). Error: {err}"
        )
    return None, 0.0

def get_active_positions(symbol):
    """
    Returns open positions belonging to this bot's MAGIC_NUMBER, sorted chronologically.
    Position 0 is the original trade; subsequent positions are recovery hedge levels.
    """
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    bot_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
    bot_positions.sort(key=lambda p: (p.time_msc if hasattr(p, 'time_msc') else p.time, p.ticket))
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
    Closes all open positions belonging to this bot with retries.
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
        send_push_notification(
            "⚠️ Close Failed",
            f"{len(remaining)} positions still open on {symbol} after 5 attempts ({reason}). Check MT5 now!"
        )
        return False
    return True

def get_daily_realized_pnl(magic=MAGIC_NUMBER):
    """Calculates total closed PnL today (since 00:00 local time) for this bot's magic number."""
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
    """Calculates total cumulative closed PnL for Phase 1 target tracking."""
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
    """Opens the original 0.20 lot trade based on Bollinger Bands entry signal."""
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_{direction}_L1"

    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=TRADE_VOLUME,
        tp_points=0,                 # Managed by trailing profit lock
        sl_points=SL_POINTS,         # 0 = Disabled
        comment=comment
    )

    if res:
        logger.info(
            f"🚀 Level 1 Trade Placed: {direction} {TRADE_VOLUME} lots at {fill_price:.2f} | "
            f"Trailing Lock: close when profit drops ${TRAILING_PROFIT_DROP_USD:.2f} from peak | "
            f"Hedge Trigger: <= ${HEDGE_TRIGGER_LOSS_USD:.2f}"
        )
        send_push_notification(
            f"🟢 Level 1 {direction} Placed",
            f"{SYMBOL} {direction} {TRADE_VOLUME} lots @ {fill_price:.2f} | "
            f"Trailing: -${TRAILING_PROFIT_DROP_USD:.0f} from peak | Hedge at ${HEDGE_TRIGGER_LOSS_USD:.0f}"
        )
        return res, fill_price
    return None, 0.0

hedge_order_in_progress = False

def open_recovery_hedge_level(symbol, next_level, direction, volume, trigger_reason=""):
    """Opens next recovery hedge trade in the sequence with retry protection."""
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
                comment=comment,
                notify_on_error=False
            )
            if res:
                logger.info(
                    f"✅ Level {next_level} Hedge Placed: #{getattr(res, 'order', None)} {direction} {volume} lots @ {fill_price:.2f} | "
                    f"Basket target now active: Net P&L >= +${HEDGE_TARGET_PROFIT_USD:.2f}"
                )
                send_push_notification(
                    f"⚖️ Level {next_level} Hedge Placed",
                    f"{symbol} {direction} {volume} lots @ {fill_price:.2f} | {trigger_reason} | "
                    f"Basket target: +${HEDGE_TARGET_PROFIT_USD:.2f}"
                )
                return res, fill_price
            logger.warning(f"⚠️ Level {next_level} hedge attempt {attempt + 1}/3 failed. Retrying in 0.2s...")
            time.sleep(0.2)

        logger.error(f"❌ Failed to place Level {next_level} hedge trade after 3 attempts!")
        send_push_notification(
            f"❌ Level {next_level} Hedge Failed",
            f"Failed to place {direction} {volume} lots on {symbol} ({comment}) after 3 attempts!"
        )
        return None, 0.0
    finally:
        hedge_order_in_progress = False

def check_challenge_limits(symbol):
    """MONITORING ONLY - Checks challenge limits and sends notifications. Does NOT stop the bot."""
    global limit_notified_daily, limit_notified_total, limit_notified_max_risk

    positions = get_active_positions(symbol)
    daily_realized_pnl = get_daily_realized_pnl(MAGIC_NUMBER)
    floating_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)
    total_today_pnl = daily_realized_pnl + floating_pnl

    total_basket_pnl = floating_pnl
    cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)

    # 1. Daily Loss Limit (4% = -$200)
    if total_today_pnl <= -MAX_DAILY_LOSS_USD and not limit_notified_daily:
        limit_notified_daily = True
        send_push_notification(
            "⚠️ DAILY LOSS LIMIT ALERT",
            f"Today's P&L: ${total_today_pnl:.2f} has reached the daily limit of -${MAX_DAILY_LOSS_USD:.2f}. "
            f"Consider closing positions manually to preserve account."
        )
    elif total_today_pnl > -MAX_DAILY_LOSS_USD + 25.0 and limit_notified_daily:
        limit_notified_daily = False

    # 2. Total Maximum Loss Limit (8% = -$400)
    if cumulative_closed_pnl <= -MAX_TOTAL_LOSS_USD and not limit_notified_total:
        limit_notified_total = True
        send_push_notification(
            "🔴 MAXIMUM LOSS LIMIT ALERT",
            f"Cumulative P&L: ${cumulative_closed_pnl:.2f} has reached the maximum loss limit of -${MAX_TOTAL_LOSS_USD:.2f}. "
            f"This is a critical alert. Consider stopping trading for this challenge."
        )
    elif cumulative_closed_pnl > -MAX_TOTAL_LOSS_USD + 50.0 and limit_notified_total:
        limit_notified_total = False

    # 3. Max Risk / Floating Drawdown Limit (-$150)
    current_loss = min(total_basket_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_AT_ANY_TIME_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(
            f"⚠️ MAX LOSS ALERT (${MAX_RISK_AT_ANY_TIME_USD:.0f})",
            f"Floating loss is ${abs(current_loss):.2f} (Limit: -${MAX_RISK_AT_ANY_TIME_USD:.2f}). "
            f"Hard loss stop will close the basket at -${MAX_LOSS_USD:.2f}."
        )
    elif current_loss > -MAX_RISK_AT_ANY_TIME_USD + 25.0 and limit_notified_max_risk:
        limit_notified_max_risk = False

def get_net_volume(positions):
    """Net lots of the basket: BUY volume minus SELL volume."""
    return sum(p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume for p in positions)

def get_contract_size(symbol_info):
    return getattr(symbol_info, 'trade_contract_size', 100.0) or 100.0

def get_basket_stop_price(positions, basket_pnl, tick, contract_size):
    """
    Gold price at which the whole basket equals -MAX_LOSS_USD.
    Basket P&L changes by (net lots x contract size) dollars per $1 of gold,
    so: stop = mid + (-MAX_LOSS - current P&L) / (net lots x contract size).
    Returns None if the basket is perfectly flat (net lots = 0).
    """
    net = get_net_volume(positions)
    if abs(net) < 1e-9:
        return None
    mid = (tick.bid + tick.ask) / 2.0
    return mid + (-MAX_LOSS_USD - basket_pnl) / (net * contract_size)

def sync_broker_basket_stop(symbol, positions, basket_pnl):
    """Puts SL/TP on every basket position at the price where the basket = -$150."""
    global broker_stop_state
    if not BROKER_BASKET_STOP or not positions:
        return

    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return

    stop_price = get_basket_stop_price(positions, basket_pnl, tick, get_contract_size(symbol_info))
    if stop_price is None:
        return

    digits = getattr(symbol_info, 'digits', 2)
    point = getattr(symbol_info, 'point', 0.01)
    stop_price = round(stop_price, digits)
    tickets = tuple(sorted(p.ticket for p in positions))

    # Only resend when the basket changed or the stop moved by more than $0.10 (swap/spread drift)
    if broker_stop_state and broker_stop_state[0] == tickets and abs(broker_stop_state[1] - stop_price) < 0.10:
        return

    min_dist = (getattr(symbol_info, 'trade_stops_level', 0) or 0) * point
    all_ok = True
    for p in positions:
        sl, tp = 0.0, 0.0
        if p.type == mt5.POSITION_TYPE_BUY:
            if stop_price < tick.bid - min_dist:
                sl = stop_price      # basket stop below price -> SL on BUY
            elif stop_price > tick.bid + min_dist:
                tp = stop_price      # basket stop above price -> TP on BUY
        else:
            if stop_price > tick.ask + min_dist:
                sl = stop_price      # basket stop above price -> SL on SELL
            elif stop_price < tick.ask - min_dist:
                tp = stop_price      # basket stop below price -> TP on SELL

        if sl == 0.0 and tp == 0.0:
            all_ok = False           # too close to price; software hard stop covers it
            continue
        if round(p.sl, digits) == sl and round(p.tp, digits) == tp:
            continue

        res = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": symbol,
            "position": p.ticket,
            "sl": float(sl),
            "tp": float(tp),
            "magic": MAGIC_NUMBER,
        })
        if not res or res.retcode not in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
            all_ok = False
            err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
            logger.warning(f"⚠️ Could not set broker basket stop on #{p.ticket}: {err}")

    if all_ok:
        broker_stop_state = (tickets, stop_price)
        logger.info(
            f"🛡️ Broker basket stop set @ {stop_price:.{digits}f} on {len(positions)} position(s) "
            f"(basket = -${MAX_LOSS_USD:.2f} at that price)"
        )

def hedge_has_room(symbol, positions, basket_pnl, direction, volume):
    """
    Checks that opening this hedge cannot by itself push the basket to -$150:
    - basket after paying the hedge's spread must stay HEDGE_MIN_CUSHION_USD above -$150
    - gold must move at least HEDGE_MIN_ROOM_PRICE to reach -$150 with the new net exposure
    Returns (allowed, reason).
    """
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return False, "no symbol/tick data"

    contract = get_contract_size(symbol_info)
    spread_cost = volume * contract * (tick.ask - tick.bid)
    projected_pnl = basket_pnl - spread_cost
    cushion = projected_pnl + MAX_LOSS_USD

    if cushion < HEDGE_MIN_CUSHION_USD:
        return False, (f"basket after spread ${projected_pnl:.2f} leaves only ${cushion:.2f} "
                       f"before -${MAX_LOSS_USD:.2f} (need ${HEDGE_MIN_CUSHION_USD:.2f})")

    new_net = get_net_volume(positions) + (volume if direction == "BUY" else -volume)
    if abs(new_net) > 1e-9:
        room = cushion / (abs(new_net) * contract)
        if room < HEDGE_MIN_ROOM_PRICE:
            return False, (f"net {new_net:+.2f} lots -> only ${room:.2f} gold move to -${MAX_LOSS_USD:.2f} "
                           f"(need ${HEDGE_MIN_ROOM_PRICE:.2f})")

    return True, f"spread cost ${spread_cost:.2f}, ${cushion:.2f} left before -${MAX_LOSS_USD:.2f}"

def try_open_hedge(symbol, positions, basket_pnl, next_level, direction, volume, trigger_reason):
    """Opens a hedge only if hedge_has_room() allows it. Returns True if the hedge was placed."""
    global hedge_blocked_notified
    allowed, reason = hedge_has_room(symbol, positions, basket_pnl, direction, volume)
    if not allowed:
        if hedge_blocked_notified != next_level:
            hedge_blocked_notified = next_level
            logger.warning(f"🚫 Level {next_level} hedge BLOCKED: {reason}")
            send_push_notification(
                f"🚫 Level {next_level} Hedge Blocked",
                f"{symbol} {direction} {volume} lots not opened: {reason}. "
                f"Basket waits for +${HEDGE_TARGET_PROFIT_USD:.2f} or -${MAX_LOSS_USD:.2f} stop."
            )
        return False

    res, _ = open_recovery_hedge_level(
        symbol=symbol,
        next_level=next_level,
        direction=direction,
        volume=volume,
        trigger_reason=f"{trigger_reason} | {reason}"
    )
    return bool(res)

def reset_basket_state():
    global last_hedge_time, basket_tickets, broker_stop_state, hedge_blocked_notified
    trade_peak_profit.clear()
    last_hedge_time = None
    basket_tickets = None
    broker_stop_state = None
    hedge_blocked_notified = None

def manage_active_trade(symbol):
    """
    Manages active trade(s):
    - SINGLE TRADE (original 0.20 only): trailing profit lock using TRAILING_PROFIT_DROP_USD ($20).
      The +$2.50 basket target is NOT used here. If loss reaches -$70,
      the first hedge (0.40 lots, opposite direction) is opened.
    - HEDGED BASKET (2+ trades): closes ALL trades when net P&L >= HEDGE_TARGET_PROFIT_USD.
      Each further hedge opens when the SUM of all open trades is at -$70 or worse AND at
      least 10 minutes passed since the last hedge, opposite to the last trade, double size.
    - -$150 protection: broker-side basket stop on every position, hedge room check,
      partial-close detection and the software hard stop (MAX_LOSS_USD).
    Returns True if trade(s) active, False if all trades closed.
    """
    global last_close_time, last_status_log_time, last_hedge_time, basket_tickets

    positions = get_active_positions(symbol)
    if not positions:
        if basket_tickets:
            # Whole basket was closed by the broker (basket stop SL/TP hit)
            logger.warning("🛡️ Basket closed by broker-side stop.")
            send_push_notification(
                "🛡️ Broker Basket Stop Hit",
                f"All {symbol} positions were closed by the broker at the -${MAX_LOSS_USD:.0f} basket stop."
            )
            last_close_time = time.time()
        reset_basket_state()
        return False

    total_basket_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)
    current_tickets = set(p.ticket for p in positions)

    # 1a. Broker closed PART of the basket (SL/TP hit on some positions) -> close the rest now
    if basket_tickets and not basket_tickets.issubset(current_tickets):
        logger.warning(
            f"🛡️ {len(basket_tickets - current_tickets)} basket position(s) closed by broker stop. "
            f"Closing remaining {len(positions)} position(s)..."
        )
        send_push_notification(
            "🛡️ Broker Basket Stop Hit",
            f"Broker closed part of the {symbol} basket at the -${MAX_LOSS_USD:.0f} stop. Closing the rest."
        )
        close_all_positions(symbol, "Broker basket stop (partial close)")
        reset_basket_state()
        last_close_time = time.time()
        return False
    basket_tickets = current_tickets

    # 1b. Software Hard Downside Watchdog (applies to single trade AND hedged basket)
    if MAX_LOSS_USD > 0 and total_basket_pnl <= -MAX_LOSS_USD:
        logger.warning(f"🛑 Hard Loss Stop Triggered (P&L: ${total_basket_pnl:.2f} <= -${MAX_LOSS_USD:.2f}). Closing all positions...")
        send_push_notification(
            "🛑 Hard Loss Stop Triggered",
            f"Basket loss reached -${abs(total_basket_pnl):.2f} (Limit: -${MAX_LOSS_USD:.2f}). Closed all positions immediately!"
        )
        close_all_positions(symbol, f"Watchdog Max Loss -${abs(total_basket_pnl):.2f}")
        reset_basket_state()
        last_close_time = time.time()
        return False

    # 1c. Keep the broker-side -$150 basket stop up to date on every position
    sync_broker_basket_stop(symbol, positions, total_basket_pnl)

    # =====================================================================
    # 2. SINGLE TRADE MODE (original 0.20 lot only) -> TRAILING PROFIT LOCK
    # =====================================================================
    if len(positions) == 1:
        pos = positions[0]
        ticket = pos.ticket
        current_pnl = total_basket_pnl

        # Track peak floating profit (stored from the first moment the trade is seen)
        peak_pnl = max(trade_peak_profit.get(ticket, current_pnl), current_pnl)
        trade_peak_profit[ticket] = peak_pnl

        # Trailing lock: active once peak profit reached at least $20.
        # Closes when profit falls $20 below the highest point reached.
        trailing_drop = peak_pnl - current_pnl
        if (peak_pnl >= TRAILING_PROFIT_DROP_USD
                and trailing_drop >= TRAILING_PROFIT_DROP_USD
                and current_pnl > 0):
            logger.info(
                f"🎯 TRAILING PROFIT LOCK! Trade #{ticket} peak was +${peak_pnl:.2f}, dropped by ${trailing_drop:.2f} >= ${TRAILING_PROFIT_DROP_USD:.2f}. "
                f"Closing trade to lock in +${current_pnl:.2f} profit!"
            )
            send_push_notification(
                "🎯 Trailing Profit Locked!",
                f"Level 1 closed at +${current_pnl:.2f} (pulled back ${trailing_drop:.2f} from peak +${peak_pnl:.2f}) on {symbol}!"
            )
            close_all_positions(symbol, f"Trailing Profit Lock +${current_pnl:.2f}")
            reset_basket_state()
            last_close_time = time.time()
            return False

        # Loss reached -$70 -> open FIRST HEDGE: opposite direction, 0.40 lots (double of 0.20)
        if current_pnl <= HEDGE_TRIGGER_LOSS_USD:
            hedge_dir = "SELL" if pos.type == mt5.POSITION_TYPE_BUY else "BUY"
            hedge_vol = get_volume_for_level(2)  # 0.40 when TRADE_VOLUME = 0.20
            if try_open_hedge(symbol, positions, current_pnl, 2, hedge_dir, hedge_vol,
                              f"Level 1 loss ${current_pnl:.2f} <= ${HEDGE_TRIGGER_LOSS_USD:.2f}"):
                last_hedge_time = time.time()   # Level 3 waits at least 10 minutes
            return True

        # Periodic status log for single trade
        now = time.time()
        if now - last_status_log_time >= 10:
            last_status_log_time = now
            trade_dir = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
            lock_state = (
                f"ARMED (closes at +${peak_pnl - TRAILING_PROFIT_DROP_USD:.2f})"
                if peak_pnl >= TRAILING_PROFIT_DROP_USD
                else f"arms at +${TRAILING_PROFIT_DROP_USD:.2f}"
            )
            logger.info(
                f"📈 Active Level 1 #{ticket} ({trade_dir} {pos.volume} lots @ {pos.price_open:.2f}) | "
                f"P&L: ${current_pnl:.2f} (Peak: ${peak_pnl:.2f}) | Trailing Lock: {lock_state} | "
                f"Hedge Trigger: ${HEDGE_TRIGGER_LOSS_USD:.2f}"
            )
        return True

    # =====================================================================
    # 3. HEDGED BASKET MODE (0.20 + 0.40 + ...) -> BASKET TARGET +$2.50
    # =====================================================================
    if total_basket_pnl >= HEDGE_TARGET_PROFIT_USD:
        logger.info(
            f"🎉 BASKET TARGET ACHIEVED! Net PnL: +${total_basket_pnl:.2f} >= +${HEDGE_TARGET_PROFIT_USD:.2f} "
            f"across {len(positions)} trades. Closing all trades!"
        )
        send_push_notification(
            "🎉 Basket Target Achieved!",
            f"Hedged basket closed in profit: +${total_basket_pnl:.2f} across {len(positions)} trades on {symbol}!"
        )
        close_all_positions(symbol, f"Basket Target Profit +${total_basket_pnl:.2f}")
        reset_basket_state()
        last_close_time = time.time()
        return False

    current_level = len(positions)

    latest_pos = positions[-1]
    latest_dir = "BUY" if latest_pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    next_level = current_level + 1
    next_dir = "SELL" if latest_dir == "BUY" else "BUY"
    next_vol = get_volume_for_level(next_level)
    next_trigger = HEDGE_TRIGGER_LOSS_USD

    # Max level reached -> only wait for basket target or hard stop
    if MAX_HEDGE_LEVELS > 0 and current_level >= MAX_HEDGE_LEVELS:
        now = time.time()
        if now - last_status_log_time >= 10:
            last_status_log_time = now
            logger.info(
                f"⚖️ Hedged Basket [Level {current_level}/{MAX_HEDGE_LEVELS} Max] | "
                f"Net P&L: ${total_basket_pnl:.2f} / Target: +${HEDGE_TARGET_PROFIT_USD:.2f} | Waiting for profit..."
            )
        return True

    # Bot restarted with a hedged basket already open -> start the 10-minute wait from now
    if last_hedge_time is None:
        last_hedge_time = time.time()

    # 10-minute wait between hedges
    wait_left = HEDGE_WAIT_SECONDS - (time.time() - last_hedge_time)

    # Wait over and basket (sum of all open trades) at -$70 or worse -> open next hedge
    if wait_left <= 0 and total_basket_pnl <= next_trigger:
        if try_open_hedge(symbol, positions, total_basket_pnl, next_level, next_dir, next_vol,
                          f"Basket of {current_level} trades at ${total_basket_pnl:.2f} <= ${next_trigger:.2f}"):
            last_hedge_time = time.time()   # start the next 10-minute wait
        return True

    # Periodic status log for hedged basket
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        arm_state = (
            f"waiting {int(wait_left // 60)}m {int(wait_left % 60)}s, then opens if basket <= ${next_trigger:.2f}"
            if wait_left > 0
            else f"READY, opens when basket <= ${next_trigger:.2f}"
        )
        logger.info(
            f"⚖️ Hedged Basket [Level {current_level}, {len(positions)} trades] | "
            f"Net P&L: ${total_basket_pnl:.2f} / Target: +${HEDGE_TARGET_PROFIT_USD:.2f} | "
            f"Next: L{next_level} {next_dir} {next_vol}l ({arm_state}) | "
            f"Hard stop: -${MAX_LOSS_USD:.2f}"
        )

    return True

def run_bot():
    """Main execution loop for the $5K Challenge XAUUSD M5 Multi-Stage Hedged Bot."""
    global last_close_time, last_processed_candle_time, phase1_announced

    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        mt5.shutdown()
        return

    logger.info("=" * 65)
    logger.info(f"🚀 Started $5K Challenge {SYMBOL} Multi-Stage Basket Recovery Bot")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Original Trade: {TRADE_VOLUME} lots")
    logger.info(f"Single Trade Exit: Trailing lock, closes when profit drops ${TRAILING_PROFIT_DROP_USD:.2f} from peak")
    logger.info(f"Hedge Trigger: every hedge opens when basket (sum of all trades) <= ${HEDGE_TRIGGER_LOSS_USD:.2f}")
    logger.info(f"Hedge Wait: {HEDGE_WAIT_SECONDS // 60} minutes after each hedge before checking the basket again")
    logger.info(f"Hedged Basket Exit: Close ALL trades when Net PnL >= +${HEDGE_TARGET_PROFIT_USD:.2f}")
    logger.info(f"Hedge Progression: {HEDGE_LOT_SIZES}")
    logger.info(f"Max Hedges: {MAX_HEDGES} (Levels incl. original: {MAX_HEDGE_LEVELS})")
    logger.info(f"Hard Loss Stop: Close ALL trades at basket loss -${MAX_LOSS_USD:.2f}")
    logger.info(f"Broker Basket Stop: {'ON' if BROKER_BASKET_STOP else 'OFF'} (SL/TP on every position at the -${MAX_LOSS_USD:.0f} price)")
    logger.info(f"Hedge Room Check: need ${HEDGE_MIN_CUSHION_USD:.0f} left after spread and >= ${HEDGE_MIN_ROOM_PRICE:.2f} gold move to -${MAX_LOSS_USD:.0f}")
    logger.info(f"⚠️ MONITORING (alerts only):")
    logger.info(f"  - Daily Loss Limit: -${MAX_DAILY_LOSS_USD:.2f} (4% of $5,000)")
    logger.info(f"  - Maximum Loss Limit: -${MAX_TOTAL_LOSS_USD:.2f} (8% of $5,000)")
    logger.info(f"  - Max Risk at any time: ${MAX_RISK_AT_ANY_TIME_USD:.2f}")
    logger.info(f"Phase 1 Profit Target: +${PHASE_1_TARGET_USD:.2f} (8% of $5,000)")
    logger.info(f"Phase 2 Profit Target: +${PHASE_2_TARGET_USD:.2f} (4% of $5,000)")
    logger.info("=" * 65)

    send_push_notification(
        "🚀 5K Bot Online",
        f"Push notification test successful! Bot active on {SYMBOL} (M5). Monitoring Challenge Limits: Daily -${MAX_DAILY_LOSS_USD:.0f}, Max -${MAX_TOTAL_LOSS_USD:.0f}."
    )

    try:
        while True:
            # 1. Phase 1 Target Tracker (announced once)
            cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)
            if cumulative_closed_pnl >= PHASE_1_TARGET_USD and not phase1_announced:
                phase1_announced = True
                logger.info("=" * 65)
                logger.info(f"🎉🎉 PHASE 1 TARGET ACHIEVED! 🎉🎉")
                logger.info(f"Total Cumulative Profit: +${cumulative_closed_pnl:.2f} >= +${PHASE_1_TARGET_USD:.2f} (+8%)")
                logger.info("You may continue to Phase 2 or submit for rewards!")
                logger.info("=" * 65)
                send_push_notification(
                    "🎉 Phase 1 Target Achieved!",
                    f"Cumulative profit +${cumulative_closed_pnl:.2f} >= +${PHASE_1_TARGET_USD:.2f}. Bot keeps running."
                )

            # 2. Challenge Limits (MONITORING ONLY)
            check_challenge_limits(SYMBOL)

            # 3. Manage Active Trade(s)
            has_active_trade = manage_active_trade(SYMBOL)
            if has_active_trade:
                time.sleep(SLEEP_INTERVAL)
                continue

            # 4. Cooldown after Trade Close
            time_since_last_close = time.time() - last_close_time
            if time_since_last_close < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue

            # 5. Bollinger Bands Entry Signal on Candle Shift 1
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
