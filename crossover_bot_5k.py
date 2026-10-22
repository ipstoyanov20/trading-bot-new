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

# --- Entry Signal: candle NEAR the band (does not have to touch it) ---
# Checked on the last completed M5 candle (shift 1):
#   SELL: candle high came within NEAR_BAND_FRACTION of the band width from the UPPER band
#         (touching or crossing it also counts), candle closed inside the bands and above the middle.
#   BUY:  candle low came within NEAR_BAND_FRACTION of the band width from the LOWER band
#         (touching or crossing it also counts), candle closed inside the bands and below the middle.
# Example: bands 4140 - 4160 (width $20), NEAR_BAND_FRACTION 0.05 -> "near" = within $1 of the band.
NEAR_BAND_FRACTION = 0.05           # 5% of the band width = VERY close to the band

# --- Trade Size & Stop Loss ---
# ONE ORDER AT A TIME. No hedging. A new trade opens only when no bot position is open.
TRADE_VOLUME = 0.20                 # Lot size for every trade

# Stop loss = Bollinger band + breathing room:
#   SELL: SL = current UPPER band + buffer
#   BUY:  SL = current LOWER band - buffer
# SL_BUFFER_USD is in ACCOUNT DOLLARS, converted to a gold price distance using the lot size.
#   With 0.20 lots: $20 buffer = $1.00 gold move beyond the band.
SL_BUFFER_USD = 20.00
MAX_SL_LOSS_USD = 50.00             # Max loss per trade: if band + buffer would risk more, the SL is pulled in to -$50
MAX_LOSS_USD = 55.0                 # Software safety stop: closes the trade at -$55 (backup if the broker SL fails or slips)

# --- Pause after a losing trade ---
# After a trade closes with a LOSS, no new trade opens until price comes back to the MIDDLE
# band and crosses it:
#   losing SELL (price went UP)   -> wait until price falls BELOW the middle band
#   losing BUY  (price went DOWN) -> wait until price rises ABOVE the middle band
# Then normal signals resume (near upper -> SELL, near lower -> BUY).
PAUSE_AFTER_LOSS = True

# --- Exit Rules: target = OPPOSITE band, peak-profit lock if price turns back ---
# TARGET: SELL from the upper band aims for the LOWER band, BUY from the lower band aims
#         for the UPPER band. When price reaches the opposite band, the trade is closed.
#
# Price turns back BEFORE the middle band (early phase):
#   Once peak profit reached EARLY_ARM_PROFIT_USD, the trade closes when profit falls to
#   max(EARLY_MIN_PROFIT_USD, peak - EARLY_DROP_USD).
#   e.g. peak +$30 -> closes at +$5 | peak +$100 -> closes at +$60
#   If the peak never reaches +$15, the bot just waits (only the stop loss can close it).
#
# Price passed the middle band and turns back BEFORE the opposite band (late phase):
#   The trade closes when profit falls to max(EARLY_MIN_PROFIT_USD, peak - PEAK_DROP_USD).
#   e.g. peak +$60 -> closes at +$20 | peak +$150 -> closes at +$110
# Once price has passed the middle band, the trade stays in the late phase.
EARLY_ARM_PROFIT_USD = 15.00        # Early phase: peak profit needed before the lock works
EARLY_DROP_USD = 40.00              # Early phase: close when profit falls this much from peak
EARLY_MIN_PROFIT_USD = 5.00         # Minimum profit the lock aims to keep
PEAK_DROP_USD = 40.00               # Late phase (past middle): close when profit falls this much from peak

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log
trade_peak_profit = {}              # Tracks peak floating profit per ticket: {ticket: float}
trade_lock_armed = {}               # Tickets that have passed the middle band (late phase): {ticket: True}
known_tickets = set()               # Tickets seen on the previous loop (detects broker SL closes)
multi_position_warned = False       # Warned once that more than one position is open
trade_info = {}                     # {ticket: (direction, last P&L)} for detecting losses on broker closes
loss_pause_side = None              # 'SELL' or 'BUY' of the last losing trade while the pause is active, else None
last_pause_log_time = 0             # Timestamp of the last "paused" log
phase1_announced = False            # Prevents Phase 1 banner from logging every second

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

def get_contract_size(symbol_info):
    """Ounces per lot (100 for XAUUSD on most brokers)."""
    return getattr(symbol_info, 'trade_contract_size', 100.0) or 100.0

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
    Evaluates the "near the band" entry signal on candle shift 1 (last completed candle):
    - SELL: candle high reached the zone near the UPPER band, close inside the bands and above middle.
    - BUY:  candle low reached the zone near the LOWER band, close inside the bands and below middle.
    Returns: (signal, candle_time, bands) where signal is 'BUY', 'SELL' or None and
    bands = dict(upper, middle, lower) of the CURRENT candle (shift 0), used for the stop loss.
    """
    df = get_bollinger_bands(symbol, timeframe, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    if df.empty or len(df) < BB_PERIOD + 2:
        return None, None, None

    # iloc[-1] = shift 0 (current, incomplete), iloc[-2] = shift 1 (last completed)
    c = df.iloc[-2]
    cur = df.iloc[-1]
    candle_time = c['time']
    c_high, c_low, c_close = c['high'], c['low'], c['close']
    c_upper, c_lower, c_middle = c['upper'], c['lower'], c['middle']

    if pd.isna(c_upper) or pd.isna(c_lower) or pd.isna(cur['upper']):
        return None, None, None

    bands = {'upper': float(cur['upper']), 'middle': float(cur['middle']), 'lower': float(cur['lower'])}
    near = (c_upper - c_lower) * NEAR_BAND_FRACTION

    near_upper = (c_high >= c_upper - near) and (c_close <= c_upper) and (c_close > c_middle)
    near_lower = (c_low <= c_lower + near) and (c_close >= c_lower) and (c_close < c_middle)

    if near_upper and not near_lower and AUTHORIZED_ORDER_TYPE in ["ALL", "SELL"]:
        logger.info(
            f"🔴 SELL Signal (near UPPER band) [{candle_time}] | High: {c_high:.2f} >= {c_upper - near:.2f} "
            f"(Upper {c_upper:.2f} - near ${near:.2f}) | Close: {c_close:.2f} | Mid: {c_middle:.2f}"
        )
        return 'SELL', candle_time, bands
    if near_lower and not near_upper and AUTHORIZED_ORDER_TYPE in ["ALL", "BUY"]:
        logger.info(
            f"🟢 BUY Signal (near LOWER band) [{candle_time}] | Low: {c_low:.2f} <= {c_lower + near:.2f} "
            f"(Lower {c_lower:.2f} + near ${near:.2f}) | Close: {c_close:.2f} | Mid: {c_middle:.2f}"
        )
        return 'BUY', candle_time, bands

    global last_no_signal_log_time, last_logged_no_signal_candle
    now = time.time()
    if (candle_time != last_logged_no_signal_candle) or (now - last_no_signal_log_time >= 15):
        last_no_signal_log_time = now
        last_logged_no_signal_candle = candle_time
        logger.info(
            f"⏳ Waiting for a signal (near BB band, M5) | Shift 1 [{candle_time}] "
            f"High: {c_high:.2f}, Low: {c_low:.2f}, Close: {c_close:.2f} | "
            f"Lower: {c_lower:.2f}, Mid: {c_middle:.2f}, Upper: {c_upper:.2f} | Near zone: ${near:.2f}"
        )

    return None, candle_time, bands

def place_order_safe(symbol, order_type, volume, sl_price=0.0, comment="BB_Single", notify_on_error=True):
    """Places a market order with a stop loss PRICE and filling mode fallback. Returns (result, fill_price)."""
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
    sl = round(sl_price, digits) if sl_price else 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "sl": float(sl),
        "tp": 0.0,
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
            logger.info(f"✅ {action_name} Placed ({comment}) | Vol: {volume} | Price: {price} | SL: {sl} | Filling: {mode}")
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
    """Returns open positions belonging to this bot's MAGIC_NUMBER, oldest first."""
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

def close_position_with_retry(symbol, position, reason=""):
    """Closes one position, retrying up to 5 times."""
    for attempt in range(5):
        still_open = [p for p in get_active_positions(symbol) if p.ticket == position.ticket]
        if not still_open:
            return True
        logger.info(f"Closing #{position.ticket} ({reason}) [Attempt {attempt + 1}/5]...")
        close_position(symbol, still_open[0])
        time.sleep(0.2)

    if any(p.ticket == position.ticket for p in get_active_positions(symbol)):
        send_push_notification(
            "⚠️ Close Failed",
            f"#{position.ticket} still open on {symbol} after 5 attempts ({reason}). Check MT5 now!"
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

def calculate_stop_loss(symbol, direction, bands):
    """
    SELL: SL = upper band + buffer.  BUY: SL = lower band - buffer.
    Buffer = SL_BUFFER_USD converted to a gold price distance for TRADE_VOLUME.
    The SL is pulled in if it would risk more than MAX_SL_LOSS_USD.
    Returns (sl_price, expected_loss_usd) or (None, reason) if no valid SL is possible.
    """
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return None, "no symbol/tick data"

    dollars_per_price = TRADE_VOLUME * get_contract_size(symbol_info)   # $20 per $1 gold at 0.20 lots
    buffer_price = SL_BUFFER_USD / dollars_per_price
    max_price_dist = MAX_SL_LOSS_USD / dollars_per_price
    point = getattr(symbol_info, 'point', 0.01)
    min_dist = max((getattr(symbol_info, 'trade_stops_level', 0) or 0) * point, point * 10)

    if direction == "SELL":
        entry = tick.bid
        sl = bands['upper'] + buffer_price
        if sl - entry > max_price_dist:
            sl = entry + max_price_dist
        if sl <= tick.ask + min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.ask:.2f}"
        dist = sl - entry
    else:
        entry = tick.ask
        sl = bands['lower'] - buffer_price
        if entry - sl > max_price_dist:
            sl = entry - max_price_dist
        if sl >= tick.bid - min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.bid:.2f}"
        dist = entry - sl

    return sl, dist * dollars_per_price

def open_single_trade(symbol, direction, bands):
    """Opens one TRADE_VOLUME trade with the Bollinger band stop loss."""
    sl_price, info = calculate_stop_loss(symbol, direction, bands)
    if sl_price is None:
        logger.warning(f"⚠️ {direction} signal skipped: {info}")
        return None, 0.0
    expected_loss = info

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=TRADE_VOLUME,
        sl_price=sl_price,
        comment=f"BB_Near_{direction}"
    )

    if res:
        band_name = "Upper" if direction == "SELL" else "Lower"
        logger.info(
            f"🚀 {direction} {TRADE_VOLUME} lots @ {fill_price:.2f} | SL {sl_price:.2f} "
            f"({band_name} band {bands['upper' if direction == 'SELL' else 'lower']:.2f} + ${SL_BUFFER_USD:.0f} buffer) | "
            f"Risk ≈ ${expected_loss:.2f} | Target: {'Lower' if direction == 'SELL' else 'Upper'} band "
            f"{bands['lower' if direction == 'SELL' else 'upper']:.2f}"
        )
        send_push_notification(
            f"🟢 {direction} Placed",
            f"{symbol} {direction} {TRADE_VOLUME} lots @ {fill_price:.2f} | SL {sl_price:.2f} (risk ≈ ${expected_loss:.0f}) | "
            f"Target: opposite band"
        )
        return res, fill_price
    return None, 0.0

def check_challenge_limits(symbol):
    """MONITORING ONLY - Checks challenge limits and sends notifications. Does NOT stop the bot."""
    global limit_notified_daily, limit_notified_total, limit_notified_max_risk

    positions = get_active_positions(symbol)
    daily_realized_pnl = get_daily_realized_pnl(MAGIC_NUMBER)
    floating_pnl = sum(p.profit + p.swap + getattr(p, 'commission', 0.0) for p in positions)
    total_today_pnl = daily_realized_pnl + floating_pnl
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
    current_loss = min(floating_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_AT_ANY_TIME_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(
            f"⚠️ MAX LOSS ALERT (${MAX_RISK_AT_ANY_TIME_USD:.0f})",
            f"Loss is ${abs(current_loss):.2f} (Limit: -${MAX_RISK_AT_ANY_TIME_USD:.2f})."
        )
    elif current_loss > -MAX_RISK_AT_ANY_TIME_USD + 25.0 and limit_notified_max_risk:
        limit_notified_max_risk = False

def forget_ticket(ticket):
    trade_peak_profit.pop(ticket, None)
    trade_lock_armed.pop(ticket, None)
    trade_info.pop(ticket, None)

def get_closed_position_pnl(ticket):
    """Realized P&L of a closed position from the deal history (None if not found)."""
    try:
        deals = mt5.history_deals_get(position=ticket)
    except Exception:
        deals = None
    if not deals:
        return None
    return sum(d.profit + d.swap + getattr(d, 'commission', 0.0) for d in deals if d.entry == mt5.DEAL_ENTRY_OUT)

def start_loss_pause(direction, pnl):
    """After a losing trade: no new trades until price crosses the middle band."""
    global loss_pause_side
    if not PAUSE_AFTER_LOSS:
        return
    loss_pause_side = direction
    side_txt = "falls BELOW" if direction == "SELL" else "rises ABOVE"
    logger.warning(f"⏸️ Losing {direction} closed at ${pnl:.2f}. Trading PAUSED until price {side_txt} the middle band.")
    send_push_notification(
        "⏸️ Trading Paused",
        f"Losing {direction} closed at ${pnl:.2f}. Waiting until price {side_txt} the middle band."
    )

def loss_pause_active(symbol):
    """True while the pause is active. Clears the pause once price crossed the middle band."""
    global loss_pause_side, last_pause_log_time
    if loss_pause_side is None:
        return False

    df = get_bollinger_bands(symbol, TIMEFRAME, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    tick = mt5.symbol_info_tick(symbol)
    if df.empty or tick is None or pd.isna(df.iloc[-1]['middle']):
        return True
    middle = float(df.iloc[-1]['middle'])
    price = (tick.bid + tick.ask) / 2.0

    crossed = price < middle if loss_pause_side == "SELL" else price > middle
    if crossed:
        logger.info(f"▶️ Price {price:.2f} crossed the middle band {middle:.2f}. Trading RESUMED.")
        send_push_notification("▶️ Trading Resumed", f"{symbol} price {price:.2f} crossed the middle band {middle:.2f}.")
        loss_pause_side = None
        return False

    now = time.time()
    if now - last_pause_log_time >= 15:
        last_pause_log_time = now
        need = "below" if loss_pause_side == "SELL" else "above"
        logger.info(f"⏸️ Paused after losing {loss_pause_side} | Price {price:.2f}, waiting for {need} middle {middle:.2f}")
    return True

def manage_position(symbol, pos, bands, tick):
    """
    Manages one open trade. Returns True if it is still open, False if the bot closed it.
    - Software safety stop at -MAX_LOSS_USD (the broker SL normally closes it first).
    - Target: closes when price reaches the opposite band.
    - Before the middle band: peak lock (arms at +$15, closes at max($5, peak - $10)).
    - After the middle band: peak lock (closes at max($5, peak - $20)).
    """
    global last_close_time, last_status_log_time

    ticket = pos.ticket
    pnl = pos.profit + pos.swap + getattr(pos, 'commission', 0.0)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    trade_dir = "BUY" if is_buy else "SELL"
    trade_info[ticket] = (trade_dir, pnl)

    # 1. Software safety stop
    if MAX_LOSS_USD > 0 and pnl <= -MAX_LOSS_USD:
        logger.warning(f"🛑 Safety Stop: #{ticket} P&L ${pnl:.2f} <= -${MAX_LOSS_USD:.2f}. Closing...")
        send_push_notification("🛑 Safety Stop Triggered", f"{symbol} #{ticket} closed at ${pnl:.2f} (limit -${MAX_LOSS_USD:.0f}).")
        close_position_with_retry(symbol, pos, f"Safety stop ${pnl:.2f}")
        forget_ticket(ticket)
        last_close_time = time.time()
        start_loss_pause(trade_dir, pnl)
        return False

    # 2. Peak profit tracking
    peak = max(trade_peak_profit.get(ticket, pnl), pnl)
    trade_peak_profit[ticket] = peak

    def close_with_profit(title, reason):
        global last_close_time
        logger.info(f"🎯 {reason} on #{ticket}: P&L ${pnl:.2f} (peak ${peak:.2f}). Closing...")
        send_push_notification(title, f"{symbol} {trade_dir} #{ticket} closed at ${pnl:.2f} (peak ${peak:.2f}). {reason}.")
        close_position_with_retry(symbol, pos, f"{reason} ${pnl:.2f}")
        forget_ticket(ticket)
        last_close_time = time.time()
        if pnl < 0:
            start_loss_pause(trade_dir, pnl)

    middle = bands['middle'] if bands else None
    target_band = (bands['upper'] if is_buy else bands['lower']) if bands else None
    price = (tick.bid if is_buy else tick.ask) if tick is not None else 0.0

    # 3. TARGET: price reached the opposite band -> close
    if target_band is not None and tick is not None:
        hit = price >= target_band if is_buy else price <= target_band
        if hit and pnl > 0:
            close_with_profit("🏁 Target Band Reached!", f"Opposite band {target_band:.2f} reached")
            return False

    # 4. Passed the middle band -> late phase (sticky)
    if middle is not None and tick is not None and not trade_lock_armed.get(ticket):
        past_middle = price > middle if is_buy else price < middle
        if past_middle:
            trade_lock_armed[ticket] = True
            logger.info(f"➡️ #{ticket} passed the middle band {middle:.2f} at P&L ${pnl:.2f}. Now aiming for the opposite band.")

    # 5. Peak-profit lock if price turns back
    if trade_lock_armed.get(ticket):
        lock_level = max(EARLY_MIN_PROFIT_USD, peak - PEAK_DROP_USD)
        if peak > lock_level and pnl <= lock_level:
            close_with_profit("🎯 Profit Locked!", "Turned back after middle band")
            return False
        phase = f"after middle, closes at ${lock_level:.2f}" if peak > lock_level else "after middle, waiting for profit"
    else:
        lock_level = max(EARLY_MIN_PROFIT_USD, peak - EARLY_DROP_USD)
        if peak >= EARLY_ARM_PROFIT_USD:
            if pnl <= lock_level:
                close_with_profit("🎯 Early Profit Locked", "Turned back before middle band")
                return False
            phase = f"before middle, closes at ${lock_level:.2f}"
        else:
            phase = f"before middle, lock arms at peak ${EARLY_ARM_PROFIT_USD:.0f}"

    # 6. Status log every 10 seconds
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        tgt = f"{target_band:.2f}" if target_band is not None else "n/a"
        mid = f"{middle:.2f}" if middle is not None else "n/a"
        logger.info(
            f"📈 #{ticket} {trade_dir} {pos.volume} lots @ {pos.price_open:.2f} | Price {price:.2f} | "
            f"Mid {mid} | Target {tgt} | P&L ${pnl:.2f} (Peak ${peak:.2f}) | SL {pos.sl:.2f} | {phase}"
        )
    return True

def manage_active_trade(symbol):
    """
    Manages the open trade (one at a time, no hedging).
    Returns True while any bot position is open, False when none is open.
    """
    global last_close_time, known_tickets, multi_position_warned

    positions = get_active_positions(symbol)
    current = set(p.ticket for p in positions)

    # Positions that disappeared without the bot closing them -> broker stop loss hit
    for ticket in known_tickets - current:
        if ticket in trade_info:
            direction, last_pnl = trade_info[ticket]
            closed_pnl = get_closed_position_pnl(ticket)
            if closed_pnl is None:
                closed_pnl = last_pnl
            logger.warning(f"🛡️ #{ticket} {direction} closed by broker at ${closed_pnl:.2f}.")
            send_push_notification("🛡️ Closed by Broker", f"{symbol} {direction} #{ticket} closed at ${closed_pnl:.2f} (stop loss).")
            last_close_time = time.time()
            if closed_pnl < 0:
                start_loss_pause(direction, closed_pnl)
        forget_ticket(ticket)
    known_tickets = current

    if not positions:
        multi_position_warned = False
        return False

    if len(positions) > 1 and not multi_position_warned:
        multi_position_warned = True
        logger.warning(f"⚠️ {len(positions)} bot positions open (e.g. leftovers from the old hedge version). Managing each one; no new trades until all are closed.")
        send_push_notification(
            "⚠️ Several Positions Open",
            f"{len(positions)} {symbol} bot positions are open. Each is managed separately; no new trade until all close."
        )

    df = get_bollinger_bands(symbol, TIMEFRAME, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    bands = None
    if not df.empty and not pd.isna(df.iloc[-1]['middle']):
        last = df.iloc[-1]
        bands = {'upper': float(last['upper']), 'middle': float(last['middle']), 'lower': float(last['lower'])}
    tick = mt5.symbol_info_tick(symbol)

    still_open = False
    for pos in positions:
        if manage_position(symbol, pos, bands, tick):
            still_open = True
    return still_open

def run_bot():
    """Main execution loop for the $5K Challenge XAUUSD M5 single-trade bot."""
    global last_close_time, last_processed_candle_time, phase1_announced

    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        mt5.shutdown()
        return

    buffer_price = SL_BUFFER_USD / (TRADE_VOLUME * get_contract_size(symbol_info))

    logger.info("=" * 65)
    logger.info(f"🚀 Started $5K Challenge {SYMBOL} Single-Trade Bot (NO hedging)")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Entry: candle near UPPER band -> SELL, near LOWER band -> BUY (near = {NEAR_BAND_FRACTION:.0%} of band width)")
    logger.info(f"Lot: {TRADE_VOLUME} | One trade at a time")
    logger.info(f"Stop Loss: band ± ${SL_BUFFER_USD:.0f} buffer (= ${buffer_price:.2f} gold) | Max SL risk ${MAX_SL_LOSS_USD:.0f}")
    logger.info(f"Exit: target = opposite band | turns back before middle: arms at peak ${EARLY_ARM_PROFIT_USD:.0f}, "
                f"closes at max(${EARLY_MIN_PROFIT_USD:.0f}, peak - ${EARLY_DROP_USD:.0f}) | "
                f"turns back after middle: closes at max(${EARLY_MIN_PROFIT_USD:.0f}, peak - ${PEAK_DROP_USD:.0f})")
    logger.info(f"Safety Stop: close trade at -${MAX_LOSS_USD:.2f}")
    logger.info(f"Pause after loss: {'ON' if PAUSE_AFTER_LOSS else 'OFF'} (no new trade until price crosses the middle band)")
    logger.info(f"⚠️ MONITORING (alerts only):")
    logger.info(f"  - Daily Loss Limit: -${MAX_DAILY_LOSS_USD:.2f} (4% of $5,000)")
    logger.info(f"  - Maximum Loss Limit: -${MAX_TOTAL_LOSS_USD:.2f} (8% of $5,000)")
    logger.info(f"  - Max Risk at any time: ${MAX_RISK_AT_ANY_TIME_USD:.2f}")
    logger.info(f"Phase 1 Profit Target: +${PHASE_1_TARGET_USD:.2f} (8% of $5,000)")
    logger.info(f"Phase 2 Profit Target: +${PHASE_2_TARGET_USD:.2f} (4% of $5,000)")
    logger.info("=" * 65)

    send_push_notification(
        "🚀 5K Bot Online",
        f"Single-trade bot active on {SYMBOL} (M5), no hedging. Near band {NEAR_BAND_FRACTION:.0%}, "
        f"max loss ${MAX_SL_LOSS_USD:.0f}, pause after loss until middle band. SL band ± ${SL_BUFFER_USD:.0f}, "
        f"target opposite band with peak-profit lock."
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

            # 3. Manage the open trade (one at a time)
            if manage_active_trade(SYMBOL):
                time.sleep(SLEEP_INTERVAL)
                continue

            # 4. Cooldown after a trade closes
            if time.time() - last_close_time < COOLDOWN_SECONDS:
                time.sleep(SLEEP_INTERVAL)
                continue

            # 5. Paused after a losing trade -> wait until price crosses the middle band
            if loss_pause_active(SYMBOL):
                time.sleep(SLEEP_INTERVAL)
                continue

            # 6. "Near the band" entry signal on candle shift 1
            signal, candle_time, bands = check_bb_entry_signal(SYMBOL, TIMEFRAME)

            if signal and (last_processed_candle_time != candle_time):
                last_processed_candle_time = candle_time   # one attempt per candle
                logger.info(f"📊 M5 Signal [{candle_time}]: {signal}! Placing trade ({TRADE_VOLUME} lots)...")
                open_single_trade(SYMBOL, signal, bands)

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
