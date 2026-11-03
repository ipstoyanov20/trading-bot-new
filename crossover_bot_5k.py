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
MAX_DAILY_LOSS_USD = 200.0          # Daily Loss Limit: 4% (-$200.00) - ALERT ONLY
MAX_TOTAL_LOSS_USD = 400.0          # Maximum Loss Limit: 8% (-$400.00) - ALERT ONLY
MAX_RISK_USD = 150.0                # Max risk at any time (-$150.00) - ALERT ONLY

# --- Push Notification Settings ---
ENABLE_WINDOWS_TOAST = True            # Send native Windows desktop push notifications
ENABLE_NTFY_PUSH = True                # Send instant mobile push notifications via ntfy.sh
NTFY_TOPIC = "tradingbot_5k_ivaylo"    # Topic for ntfy mobile app (https://ntfy.sh/tradingbot_5k_ivaylo)

# --- Trading Strategy & Symbol Settings ---
SYMBOL = "XAUUSD"                   # Gold Bot
TIMEFRAME = mt5.TIMEFRAME_M5        # Bollinger Bands are calculated on M5
SLEEP_INTERVAL = 1                  # Check price every 1 second
COOLDOWN_SECONDS = 60               # Wait after a trade closes before the next entry
MAGIC_NUMBER = 60020                # Unique identifier for this bot's orders
DEVIATION = 20                      # Maximum price slippage in points

# --- Bollinger Bands (classic) ---
BB_PERIOD = 20                      # Period 20
BB_DEVIATION = 2.0                  # Deviation 2.0
# Applied Price: Close

# --- Classic Bollinger Strategy ---
#   Price TOUCHES the UPPER band -> SELL, target = LOWER band
#   Price TOUCHES the LOWER band -> BUY,  target = UPPER band
# Checked every second on the live price. One trade at a time.

# --- Trade Size & Stop Loss ---
TRADE_VOLUME = 0.10                 # Lot size
STOP_LOSS_USD = 140.0               # Broker stop loss in account dollars from the entry price
MAX_LOSS_USD = 140.0                # Software backup stop (broker SL normally closes first)

# --- The bot NEVER stops by itself ---
# Stop losses, loss limits and errors do not stop it. Stop it manually (Ctrl+C).

# --- Bot Runtime State ---
last_close_time = 0                 # Timestamp of last closed trade
last_signal_key = None              # (side, candle_time) of the last entry, prevents repeats
last_status_log_time = 0            # Timestamp of periodic status log
last_wait_log_time = 0              # Timestamp of last "waiting" log
known_tickets = set()               # Tickets seen on the previous loop (detects broker SL closes)
trade_info = {}                     # {ticket: (direction, last P&L)}
phase1_announced = False            # Prevents Phase 1 banner from repeating

# --- Limit Monitoring State Flags ---
limit_notified_daily = False
limit_notified_total = False
limit_notified_max_risk = False


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


def ensure_mt5_connection():
    """Reconnects to MT5 if the connection was lost. Returns True when connected."""
    try:
        if mt5.terminal_info() is not None:
            return True
    except Exception:
        pass
    logger.warning("🔌 MT5 connection lost. Reconnecting...")
    try:
        mt5.shutdown()
    except Exception:
        pass
    if mt5.initialize():
        logger.info("✅ MT5 reconnected.")
        return True
    logger.error(f"MT5 reconnect failed: {mt5.last_error()}. Retrying in 10s.")
    return False


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


def get_position_pnl(pos):
    """Floating P&L of an open position including swap and commission."""
    return pos.profit + pos.swap + getattr(pos, 'commission', 0.0)


def get_bollinger_bands(symbol, timeframe, period=20, deviation=2.0, count=60):
    """
    Fetches OHLC data and calculates Bollinger Bands on Close.
    The LAST row is the forming candle, so its bands update live.
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


def get_current_bands(symbol):
    """Returns the live bands {'upper', 'middle', 'lower', 'time'} of the forming candle, or None."""
    df = get_bollinger_bands(symbol, TIMEFRAME, period=BB_PERIOD, deviation=BB_DEVIATION, count=60)
    if df.empty:
        return None
    cur = df.iloc[-1]
    if pd.isna(cur['upper']) or pd.isna(cur['lower']) or pd.isna(cur['middle']):
        return None
    return {
        'upper': float(cur['upper']),
        'middle': float(cur['middle']),
        'lower': float(cur['lower']),
        'time': cur['time'],
    }


def check_classic_signal(symbol):
    """
    Classic Bollinger Bands, checked every second:
      price touches UPPER band -> SELL
      price touches LOWER band -> BUY
    Returns (side, candle_time) or (None, None).
    """
    global last_wait_log_time

    tick = mt5.symbol_info_tick(symbol)
    bands = get_current_bands(symbol)
    if tick is None or bands is None:
        return None, None

    up, mid, lo = bands['upper'], bands['middle'], bands['lower']
    candle_time = bands['time']

    side = None
    if tick.bid >= up:
        side = 'SELL'
        logger.info(f"🔴 SELL signal [{candle_time}] | Bid {tick.bid:.2f} touched UPPER {up:.2f}")
    elif tick.ask <= lo:
        side = 'BUY'
        logger.info(f"🟢 BUY signal [{candle_time}] | Ask {tick.ask:.2f} touched LOWER {lo:.2f}")

    if side is not None:
        if (side, candle_time) != last_signal_key:
            return side, candle_time
        return None, None

    now = time.time()
    if now - last_wait_log_time >= 15:
        last_wait_log_time = now
        logger.info(
            f"⏳ Waiting for a band touch | Bid {tick.bid:.2f} | "
            f"Upper {up:.2f}, Mid {mid:.2f}, Lower {lo:.2f}"
        )
    return None, None


def place_order_safe(symbol, order_type, volume, sl_price=0.0, comment="BB_Classic", notify_on_error=True):
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
        "tp": 0.0,                      # Take profit is managed by the bot (opposite band)
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
            logger.info(f"Closed #{position.ticket} at {price} (P&L: ${get_position_pnl(position):.2f})")
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


def calculate_stop_loss(symbol, direction, volume, stop_usd):
    """
    Dynamic stop loss from the ENTRY price (current tick):
      BUY:  SL = entry - distance
      SELL: SL = entry + distance
    distance = stop_usd / (volume * contract size)
    e.g. 0.10 lots, 100 oz: $140 / (0.10 * 100) = $14.00 gold price distance.
    """
    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not symbol_info or not tick:
        return None, "no symbol/tick data"

    vol = normalize_volume(symbol_info, volume)
    dollars_per_price = vol * get_contract_size(symbol_info)
    distance = stop_usd / dollars_per_price
    point = getattr(symbol_info, 'point', 0.01)
    min_dist = max((getattr(symbol_info, 'trade_stops_level', 0) or 0) * point, point * 10)

    if direction == "BUY":
        sl = tick.ask - distance
        if sl >= tick.bid - min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.bid:.2f}"
    else:
        sl = tick.bid + distance
        if sl <= tick.ask + min_dist:
            return None, f"SL {sl:.2f} too close to price {tick.ask:.2f}"

    return sl, stop_usd


def open_trade(symbol, direction, volume, stop_usd, comment):
    """Opens one market trade with a dynamic dollar stop loss from the entry price. Returns (result, fill_price)."""
    sl_price, info = calculate_stop_loss(symbol, direction, volume, stop_usd)
    if sl_price is None:
        logger.warning(f"⚠️ {direction} {volume} lots skipped: {info}")
        return None, 0.0

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    res, fill_price = place_order_safe(
        symbol=symbol,
        order_type=order_type,
        volume=volume,
        sl_price=sl_price,
        comment=comment
    )

    if res:
        target = "LOWER band" if direction == "SELL" else "UPPER band"
        logger.info(f"🚀 {direction} {volume} lots @ {fill_price:.2f} | SL {sl_price:.2f} (-${info:.2f}) | Target: {target}")
        send_push_notification(
            f"🟢 {direction} Placed",
            f"{symbol} {direction} {volume} lots @ {fill_price:.2f} | SL {sl_price:.2f} (-${info:.0f}) | Target: {target}"
        )
        return res, fill_price
    return None, 0.0


def check_challenge_limits(symbol):
    """ALERTS ONLY - sends notifications. Never stops the bot."""
    global limit_notified_daily, limit_notified_total, limit_notified_max_risk

    positions = get_active_positions(symbol)
    daily_realized_pnl = get_daily_realized_pnl(MAGIC_NUMBER)
    floating_pnl = sum(get_position_pnl(p) for p in positions)
    total_today_pnl = daily_realized_pnl + floating_pnl
    cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)

    # 1. Daily Loss Limit (4% = -$200)
    if total_today_pnl <= -MAX_DAILY_LOSS_USD and not limit_notified_daily:
        limit_notified_daily = True
        send_push_notification(
            "⚠️ DAILY LOSS LIMIT ALERT",
            f"Today's P&L: ${total_today_pnl:.2f} reached -${MAX_DAILY_LOSS_USD:.2f}. Bot keeps running."
        )
    elif total_today_pnl > -MAX_DAILY_LOSS_USD + 25.0 and limit_notified_daily:
        limit_notified_daily = False

    # 2. Total Maximum Loss Limit (8% = -$400)
    if cumulative_closed_pnl <= -MAX_TOTAL_LOSS_USD and not limit_notified_total:
        limit_notified_total = True
        send_push_notification(
            "🔴 MAXIMUM LOSS LIMIT ALERT",
            f"Cumulative P&L: ${cumulative_closed_pnl:.2f} reached -${MAX_TOTAL_LOSS_USD:.2f}. Bot keeps running."
        )
    elif cumulative_closed_pnl > -MAX_TOTAL_LOSS_USD + 50.0 and limit_notified_total:
        limit_notified_total = False

    # 3. Max risk at any time (-$150)
    current_loss = min(floating_pnl, total_today_pnl)
    if current_loss <= -MAX_RISK_USD and not limit_notified_max_risk:
        limit_notified_max_risk = True
        send_push_notification(
            f"⚠️ MAX RISK ALERT (${MAX_RISK_USD:.0f})",
            f"Loss is ${abs(current_loss):.2f} (Limit: -${MAX_RISK_USD:.2f}). Bot keeps running."
        )
    elif current_loss > -MAX_RISK_USD + 25.0 and limit_notified_max_risk:
        limit_notified_max_risk = False


def forget_ticket(ticket):
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


def close_trade(symbol, pos, title, reason, pnl):
    """Closes one bot trade and sends a notification."""
    global last_close_time
    trade_dir = "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL"
    logger.info(f"🎯 {reason} on #{pos.ticket}: P&L ${pnl:.2f}. Closing...")
    send_push_notification(title, f"{symbol} {trade_dir} #{pos.ticket} closed at ${pnl:.2f}. {reason}.")
    close_position_with_retry(symbol, pos, f"{reason} ${pnl:.2f}")
    forget_ticket(pos.ticket)
    last_close_time = time.time()


def manage_position(symbol, pos, bands, tick):
    """
    Manages one open trade. Returns True while it is still open.
    - Backup stop at -$140 (broker SL normally closes first). Bot keeps running.
    - Take profit at the OPPOSITE band (BUY -> upper, SELL -> lower).
    """
    global last_status_log_time

    pnl = get_position_pnl(pos)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    trade_dir = "BUY" if is_buy else "SELL"
    trade_info[pos.ticket] = (trade_dir, pnl)

    # 1. Backup stop loss
    if MAX_LOSS_USD > 0 and pnl <= -MAX_LOSS_USD:
        logger.warning(f"🛑 Backup Stop: #{pos.ticket} P&L ${pnl:.2f} <= -${MAX_LOSS_USD:.2f}. Closing...")
        close_trade(symbol, pos, "🛑 Stop Loss Triggered", f"Backup stop -${MAX_LOSS_USD:.0f}", pnl)
        return False

    # 2. Take profit at the opposite band
    target_band = None
    price = 0.0
    if bands is not None and tick is not None:
        target_band = bands['upper'] if is_buy else bands['lower']
        price = tick.bid if is_buy else tick.ask
        hit = price >= target_band if is_buy else price <= target_band
        if hit:
            close_trade(symbol, pos, "🏁 Target Band Reached!", f"Opposite band {target_band:.2f} reached", pnl)
            return False

    # 3. Status log every 10 seconds
    now = time.time()
    if now - last_status_log_time >= 10:
        last_status_log_time = now
        tgt = f"{target_band:.2f}" if target_band is not None else "n/a"
        logger.info(
            f"📈 #{pos.ticket} {trade_dir} {pos.volume} lots @ {pos.price_open:.2f} | Price {price:.2f} | "
            f"Target {tgt} | P&L ${pnl:.2f} | SL {pos.sl:.2f}"
        )
    return True


def manage_active_trade(symbol):
    """
    Manages the open trade. Returns True while a bot position is open.
    Broker stop losses are detected and reported. The bot keeps running.
    """
    global known_tickets, last_close_time

    positions = get_active_positions(symbol)
    current = set(p.ticket for p in positions)

    # Trades that disappeared without the bot closing them -> closed by broker (stop loss)
    for ticket in known_tickets - current:
        if ticket in trade_info:
            direction, last_pnl = trade_info[ticket]
            closed_pnl = get_closed_position_pnl(ticket)
            if closed_pnl is None:
                closed_pnl = last_pnl
            logger.warning(f"🛡️ #{ticket} {direction} closed by broker at ${closed_pnl:.2f}.")
            send_push_notification(
                "🛡️ Closed by Broker",
                f"{symbol} {direction} #{ticket} closed at ${closed_pnl:.2f} (stop loss). Bot keeps running."
            )
            last_close_time = time.time()
        forget_ticket(ticket)
    known_tickets = current

    if not positions:
        return False

    tick = mt5.symbol_info_tick(symbol)
    bands = get_current_bands(symbol)

    still_open = False
    for pos in positions:
        if manage_position(symbol, pos, bands, tick):
            still_open = True
    return still_open


def bot_iteration():
    """One pass of the main loop. Any error here is caught by run_bot and the bot continues."""
    global last_signal_key, phase1_announced

    # 1. Phase 1 Target Tracker (announced once)
    cumulative_closed_pnl = get_total_closed_pnl(MAGIC_NUMBER)
    if cumulative_closed_pnl >= PHASE_1_TARGET_USD and not phase1_announced:
        phase1_announced = True
        logger.info(f"🎉 PHASE 1 TARGET ACHIEVED! Total: +${cumulative_closed_pnl:.2f}")
        send_push_notification(
            "🎉 Phase 1 Target Achieved!",
            f"Cumulative profit +${cumulative_closed_pnl:.2f} >= +${PHASE_1_TARGET_USD:.2f}. Bot keeps running."
        )

    # 2. Challenge limits (alerts only)
    check_challenge_limits(SYMBOL)

    # 3. Manage the open trade (stop loss, target band)
    if manage_active_trade(SYMBOL):
        return

    # 4. Cooldown after a trade closes
    if time.time() - last_close_time < COOLDOWN_SECONDS:
        return

    # 5. Classic Bollinger signal (checked every second)
    side, candle_time = check_classic_signal(SYMBOL)
    if side:
        last_signal_key = (side, candle_time)   # one entry per candle per direction
        open_trade(SYMBOL, side, TRADE_VOLUME, STOP_LOSS_USD, f"BB_Classic_{side}")


def run_bot():
    """Main loop: checks price every second and runs 24/7 until stopped manually (Ctrl+C)."""
    while not mt5.initialize():
        logger.error(f"MT5 initialization failed: {mt5.last_error()}. Retrying in 10s...")
        time.sleep(10)

    logger.info("=" * 65)
    logger.info(f"🚀 Started $5K Challenge {SYMBOL} Classic Bollinger Bot (24/7, manual stop only)")
    logger.info(f"Price check: every {SLEEP_INTERVAL}s | Bands: M5 BB({BB_PERIOD}, {BB_DEVIATION}, Close)")
    logger.info(f"Entry: touch UPPER -> SELL | touch LOWER -> BUY")
    logger.info(f"Exit: opposite band | Stop Loss: -${STOP_LOSS_USD:.0f} from entry | Lot: {TRADE_VOLUME}")
    logger.info(f"Alerts only: daily -${MAX_DAILY_LOSS_USD:.0f}, total -${MAX_TOTAL_LOSS_USD:.0f}, max risk -${MAX_RISK_USD:.0f}")
    logger.info("=" * 65)

    send_push_notification(
        "🚀 5K Bot Online",
        f"Classic Bollinger bot on {SYMBOL}. {TRADE_VOLUME} lots, SL -${STOP_LOSS_USD:.0f}, "
        f"TP opposite band. Runs 24/7 until you stop it."
    )

    error_notified = False
    try:
        while True:
            try:
                if not ensure_mt5_connection():
                    time.sleep(10)
                    continue
                bot_iteration()
                error_notified = False
            except Exception as e:
                # Errors never stop the bot: log, notify once, and continue
                logger.error(f"Error in loop (bot continues): {e}", exc_info=True)
                if not error_notified:
                    error_notified = True
                    send_push_notification("⚠️ Bot Error", f"{SYMBOL}: {e}. Bot keeps running.")
                time.sleep(5)
            time.sleep(SLEEP_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user (Ctrl+C).")
    finally:
        mt5.shutdown()
        logger.info("MT5 connection closed.")


if __name__ == "__main__":
    run_bot()
