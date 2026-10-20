import os
import json
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

# --- Trade Risk ---
TRADE_VOLUME = 0.20                 # Trade volume: 0.20 lots, one trade at a time (no hedging)

# Stop loss on EVERY trade: a broker-side SL at the price where this trade loses exactly $30.
# It is sent with the order and corrected to the real fill price right after. The bot also
# closes the trade itself at -$30 as a backup (e.g. if the broker refused the SL).
STOP_LOSS_USD = 30.00

# --- Exit Rule: Persistent Trailing Profit Lock ---
# Tracks the highest floating profit (peak). Once the peak reaches TRAILING_PROFIT_DROP_USD,
# the trade is closed when profit falls TRAILING_PROFIT_DROP_USD below the peak
# (e.g. peak +$85 -> closes at +$65).
# Persistent:
#   1. The broker-side SL is moved up to the locked profit level as the peak grows (it never
#      moves back), so the lock holds at the broker even if the bot stops or disconnects.
#   2. The peak is saved to TRAILING_STATE_FILE, so a bot restart continues from the same peak.
TRAILING_PROFIT_DROP_USD = 20.00
TRAILING_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trailing_state.json")
SL_MIN_STEP_PRICE = 0.05            # Only move the broker SL when it improves by at least this much


# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_processed_candle_time = None   # Timestamp of last processed candle shift 1
last_no_signal_log_time = 0         # Timestamp of last "waiting for signal" log
last_logged_no_signal_candle = None # Timestamp of last logged candle for no-signal
last_status_log_time = 0            # Timestamp of periodic open trade status log
trade_peak_profit = {}              # Peak floating profit per ticket: {ticket: float} (saved to disk)
known_tickets = set()               # Tickets seen on the previous loop (detects broker SL closes)
armed_notified = set()              # Tickets whose "trailing armed" alert was already sent
sl_warn_time = {}                   # Last time an "SL could not be set" warning was logged per ticket
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

# =====================================================================
# Persistent trailing state (peak profit per ticket, survives restarts)
# =====================================================================
def load_trailing_state():
    """Loads saved peaks from TRAILING_STATE_FILE into trade_peak_profit."""
    trade_peak_profit.clear()
    try:
        with open(TRAILING_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for ticket, peak in data.items():
            trade_peak_profit[int(ticket)] = float(peak)
        if trade_peak_profit:
            logger.info(f"💾 Loaded trailing peaks from {TRAILING_STATE_FILE}: {trade_peak_profit}")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"⚠️ Could not read {TRAILING_STATE_FILE}: {e}. Starting with no saved peaks.")

def save_trailing_state():
    """Writes trade_peak_profit to disk atomically (temp file + replace)."""
    try:
        tmp = TRAILING_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(t): round(p, 2) for t, p in trade_peak_profit.items()}, f)
        os.replace(tmp, TRAILING_STATE_FILE)
    except Exception as e:
        logger.warning(f"⚠️ Could not save {TRAILING_STATE_FILE}: {e}")

def forget_ticket(ticket):
    """Removes all per-trade state once a trade is closed."""
    known_tickets.discard(ticket)
    armed_notified.discard(ticket)
    sl_warn_time.pop(ticket, None)
    if trade_peak_profit.pop(ticket, None) is not None:
        save_trailing_state()

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
    return getattr(symbol_info, 'trade_contract_size', 100.0) or 100.0

def usd_to_price(usd, volume, contract_size):
    """Gold price move ($) that changes a position of `volume` lots by `usd` dollars."""
    return usd / (volume * contract_size)

def stop_loss_price(is_buy, open_price, volume, contract_size):
    """Price at which the trade has lost exactly STOP_LOSS_USD."""
    dist = usd_to_price(STOP_LOSS_USD, volume, contract_size)
    return open_price - dist if is_buy else open_price + dist

def desired_stop_price(is_buy, open_price, volume, contract_size, peak):
    """
    The broker SL this trade should have now:
    - the -$30 stop loss, or
    - once the peak reached TRAILING_PROFIT_DROP_USD, the trailing lock (peak - drop), whichever
      protects more. Returns (stop_price, locked_profit_or_None).
    """
    stop = stop_loss_price(is_buy, open_price, volume, contract_size)
    locked = None
    if peak >= TRAILING_PROFIT_DROP_USD:
        locked = peak - TRAILING_PROFIT_DROP_USD
        move = usd_to_price(locked, volume, contract_size)
        lock_stop = open_price + move if is_buy else open_price - move
        stop = max(stop, lock_stop) if is_buy else min(stop, lock_stop)
    return stop, locked

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

def place_order_safe(symbol, order_type, volume, tp_points=0, sl_usd=0.0, comment="BB_Single", notify_on_error=True):
    """
    Places an order on MT5 with filling mode fallback and returns (result, fill_price).
    sl_usd > 0 puts a stop loss at the price where this order loses sl_usd dollars.
    """
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

    is_buy = order_type == mt5.ORDER_TYPE_BUY
    price = tick.ask if is_buy else tick.bid
    digits = getattr(symbol_info, 'digits', 2)
    point = getattr(symbol_info, 'point', 0.01)

    tp = 0.0
    if tp_points > 0:
        tp = round(price + (tp_points * point), digits) if is_buy else round(price - (tp_points * point), digits)

    sl = 0.0
    if sl_usd > 0:
        sl = round(stop_loss_price(is_buy, price, volume, get_contract_size(symbol_info)), digits)

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
    """Returns open positions belonging to this bot's MAGIC_NUMBER, sorted chronologically."""
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

def close_trade(symbol, position, reason):
    """Closes one trade with retries. Returns True once it is gone."""
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

def get_closed_trade_pnl(ticket):
    """Realized P&L of a closed position (all its exit deals), or None if not in history yet."""
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None
    exits = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
    if not exits:
        return None
    return sum(d.profit + d.swap + getattr(d, 'commission', 0.0) for d in exits)

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
    """Opens the 0.20 lot trade with a -$30 stop loss, based on the Bollinger Bands signal."""
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    comment = f"BB_{direction}"

    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=TRADE_VOLUME,
        tp_points=0,                 # Exit is the trailing profit lock
        sl_usd=STOP_LOSS_USD,        # -$30 stop loss
        comment=comment
    )

    if res:
        logger.info(
            f"🚀 Trade Placed: {direction} {TRADE_VOLUME} lots at {fill_price:.2f} | "
            f"Stop Loss: -${STOP_LOSS_USD:.2f} | "
            f"Trailing Lock: arms at +${TRAILING_PROFIT_DROP_USD:.2f}, closes ${TRAILING_PROFIT_DROP_USD:.2f} below peak"
        )
        send_push_notification(
            f"🟢 {direction} Placed",
            f"{SYMBOL} {direction} {TRADE_VOLUME} lots @ {fill_price:.2f} | "
            f"SL -${STOP_LOSS_USD:.0f} | Trailing -${TRAILING_PROFIT_DROP_USD:.0f} from peak"
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
            f"Consider stopping the bot for today to preserve the account."
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

    # 3. Max Risk / Drawdown Limit (-$150)
    current_loss = min(floating_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_AT_ANY_TIME_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(
            f"⚠️ MAX LOSS ALERT (${MAX_RISK_AT_ANY_TIME_USD:.0f})",
            f"Loss is ${abs(current_loss):.2f} (Limit: -${MAX_RISK_AT_ANY_TIME_USD:.2f}). "
            f"Each trade is capped by its -${STOP_LOSS_USD:.0f} stop loss."
        )
    elif current_loss > -MAX_RISK_AT_ANY_TIME_USD + 25.0 and limit_notified_max_risk:
        limit_notified_max_risk = False

def sync_broker_stop(symbol, pos, peak):
    """
    Keeps the broker-side SL of one trade at desired_stop_price(): the -$30 stop, raised to the
    trailing lock once armed. The SL only ever moves in the trade's favour.
    Returns (stop_price, locked_profit_or_None).
    """
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return None, None

    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    digits = getattr(symbol_info, 'digits', 2)
    point = getattr(symbol_info, 'point', 0.01)
    stop, locked = desired_stop_price(is_buy, pos.price_open, pos.volume, get_contract_size(symbol_info), peak)
    stop = round(stop, digits)

    # Never loosen: skip unless the new SL is at least SL_MIN_STEP_PRICE better than the current one
    if pos.sl > 0:
        improvement = (stop - pos.sl) if is_buy else (pos.sl - stop)
        if improvement < SL_MIN_STEP_PRICE:
            return pos.sl, locked

    # The broker refuses an SL closer to the price than its stops level (or on the wrong side)
    min_dist = (getattr(symbol_info, 'trade_stops_level', 0) or 0) * point
    valid = stop < tick.bid - min_dist if is_buy else stop > tick.ask + min_dist
    if not valid:
        now = time.time()
        if now - sl_warn_time.get(pos.ticket, 0) >= 30:
            sl_warn_time[pos.ticket] = now
            logger.warning(f"⚠️ SL {stop} too close to price for #{pos.ticket}; the bot's own stop covers it.")
        return pos.sl, locked

    res = mt5.order_send({
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": pos.ticket,
        "sl": float(stop),
        "tp": float(pos.tp),
        "magic": MAGIC_NUMBER,
    })
    if not res or res.retcode not in [mt5.TRADE_RETCODE_DONE, 10008, 0]:
        now = time.time()
        if now - sl_warn_time.get(pos.ticket, 0) >= 30:
            sl_warn_time[pos.ticket] = now
            err = mt5.last_error() if not res else f"{res.comment} (Code: {res.retcode})"
            logger.warning(f"⚠️ Could not set SL {stop} on #{pos.ticket}: {err}")
        return pos.sl, locked

    # Once armed the lock (>= $0 profit) is always tighter than the -$30 stop
    what = f"trailing lock +${locked:.2f}" if locked is not None else f"stop loss -${STOP_LOSS_USD:.2f}"
    logger.info(f"🛡️ Broker SL on #{pos.ticket} set @ {stop:.{digits}f} ({what})")
    return stop, locked

def manage_trade(symbol, pos):
    """
    One trade: -$30 stop loss and persistent trailing profit lock.
    Returns True while the trade stays open.
    """
    global last_close_time, last_status_log_time

    ticket = pos.ticket
    current_pnl = pos.profit + pos.swap + getattr(pos, 'commission', 0.0)

    # Track and persist the peak floating profit
    old_peak = trade_peak_profit.get(ticket)
    peak_pnl = current_pnl if old_peak is None else max(old_peak, current_pnl)
    if old_peak is None or peak_pnl > old_peak + 0.009:
        trade_peak_profit[ticket] = peak_pnl
        save_trailing_state()

    # Backup -$30 stop (the broker SL should normally close it first)
    if current_pnl <= -STOP_LOSS_USD:
        logger.warning(f"🛑 Stop loss: #{ticket} P&L ${current_pnl:.2f} <= -${STOP_LOSS_USD:.2f}. Closing...")
        if close_trade(symbol, pos, f"Stop loss -${abs(current_pnl):.2f}"):
            send_push_notification(
                "🛑 Stop Loss Hit",
                f"{symbol} #{ticket} closed at ${current_pnl:.2f} (stop loss -${STOP_LOSS_USD:.0f})."
            )
            forget_ticket(ticket)
            last_close_time = time.time()
            return False
        return True

    # Backup trailing lock (the broker SL at the lock level should normally close it first)
    trailing_drop = peak_pnl - current_pnl
    if peak_pnl >= TRAILING_PROFIT_DROP_USD and trailing_drop >= TRAILING_PROFIT_DROP_USD:
        logger.info(
            f"🎯 TRAILING PROFIT LOCK! #{ticket} peak +${peak_pnl:.2f}, dropped ${trailing_drop:.2f}. "
            f"Closing at ${current_pnl:.2f}."
        )
        if close_trade(symbol, pos, f"Trailing Profit Lock ${current_pnl:.2f}"):
            send_push_notification(
                "🎯 Trailing Profit Locked!",
                f"{symbol} #{ticket} closed at ${current_pnl:.2f} (pulled back ${trailing_drop:.2f} from peak +${peak_pnl:.2f})."
            )
            forget_ticket(ticket)
            last_close_time = time.time()
            return False
        return True

    # Broker-side SL: -$30, raised to the trailing lock as the peak grows
    stop, locked = sync_broker_stop(symbol, pos, peak_pnl)

    if locked is not None and ticket not in armed_notified:
        armed_notified.add(ticket)
        send_push_notification(
            "🔒 Trailing Lock Armed",
            f"{symbol} #{ticket} peak +${peak_pnl:.2f}. Profit of +${locked:.2f} is now locked at the broker."
        )

    # Periodic status log
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        trade_dir = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
        lock_state = (
            f"ARMED (locked +${locked:.2f})" if locked is not None
            else f"arms at +${TRAILING_PROFIT_DROP_USD:.2f}"
        )
        logger.info(
            f"📈 Active #{ticket} ({trade_dir} {pos.volume} lots @ {pos.price_open:.2f}) | "
            f"P&L: ${current_pnl:.2f} (Peak: ${peak_pnl:.2f}) | Trailing: {lock_state} | "
            f"Broker SL: {stop if stop else pos.sl}"
        )
    return True

def manage_active_trades(symbol):
    """
    Manages every open trade of this bot on its own (normally there is only one; no hedging).
    Detects trades closed by the broker SL (stop loss or trailing lock).
    Returns True if any trade is still open.
    """
    global last_close_time, known_tickets

    positions = get_active_positions(symbol)
    current = {p.ticket for p in positions}

    # Trades that disappeared since the last loop were closed by the broker SL
    for ticket in known_tickets - current:
        pnl = get_closed_trade_pnl(ticket)
        pnl_text = f"${pnl:.2f}" if pnl is not None else "unknown P&L"
        logger.warning(f"🛡️ #{ticket} closed by the broker stop ({pnl_text}).")
        send_push_notification(
            "🛡️ Broker Stop Hit",
            f"{symbol} #{ticket} closed by its broker SL ({pnl_text})."
        )
        forget_ticket(ticket)
        last_close_time = time.time()
    known_tickets = set(current)

    still_open = False
    for pos in positions:
        if manage_trade(symbol, pos):
            still_open = True
    return still_open

def run_bot():
    """Main execution loop for the $5K Challenge XAUUSD M5 single-trade bot."""
    global last_close_time, last_processed_candle_time, phase1_announced, known_tickets

    if not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}")
        return

    symbol_info = mt5.symbol_info(SYMBOL)
    if not symbol_info:
        logger.error(f"Symbol {SYMBOL} not found.")
        mt5.shutdown()
        return

    # Restore saved peaks; drop the ones whose trades are no longer open
    load_trailing_state()
    open_now = {p.ticket for p in get_active_positions(SYMBOL)}
    stale = [t for t in trade_peak_profit if t not in open_now]
    for t in stale:
        trade_peak_profit.pop(t, None)
    if stale:
        save_trailing_state()
    known_tickets = set(open_now)

    logger.info("=" * 65)
    logger.info(f"🚀 Started $5K Challenge {SYMBOL} Single-Trade Bot (no hedging)")
    logger.info(f"Timeframe: M5 | Indicator: Bollinger Bands (20, 2, Shift 0, Close)")
    logger.info(f"Trade: {TRADE_VOLUME} lots, one trade at a time")
    logger.info(f"Stop Loss: -${STOP_LOSS_USD:.2f} on every trade (broker SL + bot backup)")
    logger.info(f"Trailing Lock: arms at +${TRAILING_PROFIT_DROP_USD:.2f}, closes ${TRAILING_PROFIT_DROP_USD:.2f} below the peak")
    logger.info(f"Trailing is persistent: broker SL follows the lock, peaks saved to {TRAILING_STATE_FILE}")
    logger.info(f"⚠️ MONITORING (alerts only):")
    logger.info(f"  - Daily Loss Limit: -${MAX_DAILY_LOSS_USD:.2f} (4% of $5,000)")
    logger.info(f"  - Maximum Loss Limit: -${MAX_TOTAL_LOSS_USD:.2f} (8% of $5,000)")
    logger.info(f"  - Max Risk at any time: ${MAX_RISK_AT_ANY_TIME_USD:.2f}")
    logger.info(f"Phase 1 Profit Target: +${PHASE_1_TARGET_USD:.2f} (8% of $5,000)")
    logger.info(f"Phase 2 Profit Target: +${PHASE_2_TARGET_USD:.2f} (4% of $5,000)")
    if open_now:
        logger.info(f"Resuming {len(open_now)} open trade(s): {sorted(open_now)}")
    logger.info("=" * 65)

    send_push_notification(
        "🚀 5K Bot Online",
        f"Bot active on {SYMBOL} (M5), no hedging. SL -${STOP_LOSS_USD:.0f} per trade, "
        f"trailing -${TRAILING_PROFIT_DROP_USD:.0f} from peak."
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

            # 3. Manage the open trade(s)
            if manage_active_trades(SYMBOL):
                time.sleep(SLEEP_INTERVAL)
                continue

            # 4. Cooldown after Trade Close
            if time.time() - last_close_time < COOLDOWN_SECONDS:
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
